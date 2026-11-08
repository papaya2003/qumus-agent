"""Hall-bar scenario contracts for the existing recorder; no independent runner.

The version-3 protocol is separate from immutable historical material-metal
campaigns. Local acceptance precedes freeze; route qualification is charged
inside the explicitly released first frozen attempt.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from qumus2.core.polygons import (
    contains,
    convex_polygon,
    rigid_polygon_error,
    transform,
)
from qumus2.records.device_design import (
    DeviceDesignV1,
    ImportedHallBarTarget,
    imported_target_directory,
    target_contact_coverage,
    target_import_binding,
    target_metal_primitives,
)
from scripts.record_three_demos import (
    TrialRegistry,
    action_evidence,
    digest,
    http_accounting,
    http_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
SIMULATION_PROFILE = "hallbar-linked"
PROTOCOL_REVISION = "hallbar-linked-owner-native-rgb-demo-v3"
CAMPAIGN_ID = "hallbar-linked-owner-native-rgb-demo-v3-20261102"
OWNER_WORKSPACE = ROOT / "runs/workspace/hallbar-linked-owner-20261101"
FIRST_CELL = "astra-complete"
FIRST_LIMITS = {"estimated_usd": 100., "http_attempts": 120, "input_tokens": 3_000_000,
                "output_tokens": 100_000, "wall_seconds": 7200,
                "http_max_retries": 2, "min_request_interval_s": 60.}
ACCEPTANCE_CHECKS = {"ruff", "pytest", "node", "v1compare", "startup", "scenario_contracts",
                     "process_fault_acceptance", "scorer_negative_controls"}
DEMO_SCOPE = "owner_authorized_unaccepted_recording"
DEMO_CHECKS = {"startup", "native_execution_path", "recording_playback", "budget_guard"}

MODELS = {
    "openai/gpt-6-astra": "openai/gpt-6-astra",
    "atria/atria-dawn": "Atria-Dawn-Preview",
    "olares-qumusphusis/qwen-3.8-27b": "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL",
    "qwen-token-plan/deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "qwen-token-plan/qwen3.8-max": "qwen3.8-max",
}
GOAL = "Make an hBN/graphene Hall device on the supplied eight-electrode wafer, starting from tapes."


def search_faults(number):
    boundary = f"before_search_{number}"
    return [
        {"id": f"empty_{number}", "boundary": boundary,
         "fault": "pick_fail_next", "params": {"count": 1}},
        {"id": f"placement_{number}", "boundary": boundary,
         "fault": "next_placement", "params": {"dx": 2., "dy": -1.5, "drot": 6.}},
        {"id": f"timeout_{number}", "boundary": boundary,
         "fault": "serial_timeout", "params": {"device": "arm_right", "count": 1}},
    ]


def profiles():
    """Candidate finite protocol; repeated faults test recurrence without repair scripts."""
    return {
        "complete": {"prompt": GOAL, "events": search_faults(1) + search_faults(2)},
        "honest_stop": {"prompt": GOAL, "events": search_faults(1) + [{
            "id": "offline", "boundary": "before_search_2", "fault": "device_offline",
            "params": {"device": "microscope", "offline": True}}]},
        "collaboration": {"prompt": "Find graphene; wait.", "events": [
            {"id": "timeout_1", "boundary": "before_search_1", "fault": "serial_timeout",
             "params": {"device": "arm_right", "count": 1}},
            {"id": "geometry", "boundary": "first_review", "message":
             "Use the supplied eight-contact wafer for a Hall device. Prepare hBN too, "
             "select compatible flakes and stop before designing or stacking."},
            {"id": "coverage", "boundary": "second_review", "message":
             "The hBN must cover the entire graphene. Pick up hBN first, then graphene, "
             "and finish hBN/graphene/electrodes/silicon using the checked KLayout design."},
        ]},
    }


def search_submissions(events):
    """One experimental boundary per distinct source chip, not per retry."""
    seen, rows = set(), []
    for event in events:
        payload = event["payload"]
        chip = payload.get("params", {}).get("chip_id")
        if event["kind"] == "job.submitted" and payload.get("flow") == "search_chip" and chip not in seen:
            seen.add(chip)
            rows.append(event)
    return rows


def setup_scenario(services, sequence, manifest):
    """Inject only at existing JobService submission boundaries; never repair state."""
    original = services.jobs.submit

    def submit(flow, params, **kwargs):
        if flow == "search_chip":
            events = [e.model_dump() for e in services.store.events_after(0, limit=1_000_000)]
            previous = search_submissions(events)
            chip = params.get("chip_id") if isinstance(params, dict) else params.chip_id
            if chip not in {e["payload"]["params"]["chip_id"] for e in previous}:
                sequence.apply(f"before_search_{len(previous)+1}", services)
        return original(flow, params, **kwargs)

    services.jobs.submit = submit


async def drive_scenario(services, sequence, manifest, turn):
    if manifest["story"] != "collaboration":
        return
    for boundary in ("first_review", "second_review"):
        if any(r["status"] in ("queued", "running", "waiting_human", "parked")
               for r in services.store.list_runs()):
            raise ValueError("A review point cannot bypass an unfinished physical job")
        if any(r["flow"] == "stack_device" for r in services.store.list_runs()):
            raise ValueError("The requested design review point was bypassed")
        if boundary == "second_review" and any(e.kind == "tool.result"
                and e.payload.get("tool") in ("create_device_design", "submit_design")
                and e.payload.get("status") == "ok"
                for e in services.store.events_after(0, limit=1_000_000)):
            raise ValueError("A design was accepted before the requested second review")
        sequence.apply(boundary, services)
        await turn()


def action_before_search(events, action, submitted, *, after_id=0):
    """Bind one exact persisted injection receipt to its submission boundary."""
    pending = [e for e in events if e["kind"] == "demo.action_pending"
               and e["payload"].get("action", {}).get("id") == action["id"]]
    applied = [e for e in events if e["kind"] == "demo.action_applied"
               and e["payload"].get("action_id") == action["id"]]
    if len(pending) != 1 or len(applied) != 1 or pending[0]["payload"].get("action") != action:
        return False
    effect = [e for e in events if e["id"] == applied[0]["payload"].get("event_id")]
    return (len(effect) == 1 and effect[0]["kind"] == "fault.injected"
        and effect[0]["payload"].get("fault") == action["fault"]
        and effect[0]["payload"].get("params") == action["params"]
        and effect[0]["payload"].get("demo_action", action["id"]) == action["id"]
        and after_id < pending[0]["id"] < effect[0]["id"] < applied[0]["id"] < submitted["id"])


def recovery_evidence(events, runs, number, *, timeout_only=False):
    """Actual same-job effect/correction/outcome, never an injection count or prose."""
    submitted = search_submissions(events)
    out = {"verified": False, "number": number, "run_id": None, "effects": {}}
    if len(submitted) < number:
        return out
    start = submitted[number-1]
    rid = start.get("run_id")
    out["run_id"] = rid
    run = next((r for r in runs if r["id"] == rid), {})
    expected_actions = search_faults(number)[-1:] if timeout_only else search_faults(number)
    prior = submitted[number-2]["id"] if number > 1 else 0
    out["injections_before_search"] = all(action_before_search(events, action, start, after_id=prior)
                                           for action in expected_actions)
    if (not rid or run.get("flow") != "search_chip"
            or run.get("params", {}).get("chip_id") != start["payload"].get("params", {}).get("chip_id")):
        return out
    local = [e for e in events if e.get("run_id") == rid and e["id"] > start["id"]]
    expected = {"timeout": "serial timeout"}
    if not timeout_only:
        expected["empty"] = "pick came up empty"
    for name, phrase in expected.items():
        out["effects"][name] = [e["id"] for e in local if e["kind"] == "notification"
            and "arm_right" in e["payload"].get("text", "").lower()
            and phrase in e["payload"].get("text", "").lower()]
    if not timeout_only:
        out["effects"]["placement"] = [e["id"] for e in local if e["kind"] == "step.corrected"
            and e["payload"].get("name") == "place_chip_on_transfer_stage"
            and all(word in e["payload"].get("action", "").lower() for word in ("re-pick", "re-place"))]
    last_effect = max((i for ids in out["effects"].values() for i in ids), default=0)
    terminals = [e for e in local if e["kind"] == "job.status"]
    out["verified"] = (out["injections_before_search"] and all(out["effects"].values()) and bool(terminals)
        and terminals[-1]["id"] > last_effect
        and terminals[-1]["payload"].get("status") == run.get("status") == "succeeded")
    return out


def human_stop_evidence(events, runs):
    """Require offline injection, actual second-search failure, then escalation."""
    result = {"verified": False, "run_id": None, "failure_ids": [], "escalation_ids": []}
    submitted = search_submissions(events)
    if len(submitted) < 2:
        return result
    start, prior = submitted[1], submitted[0]
    action = profiles()["honest_stop"]["events"][-1]
    if not action_before_search(events, action, start, after_id=prior["id"]):
        return result
    rid = start.get("run_id")
    result["run_id"] = rid
    run = next((r for r in runs if r["id"] == rid), {})
    local = [e for e in events if e.get("run_id") == rid and e["id"] > start["id"]]
    def offline(text):
        return "microscope" in text.lower() and "offline" in text.lower()
    starts = [e for e in local if e["kind"] == "step.started" and e["payload"].get("name") == "chip_to_microscope"]
    failures = [e for e in local if e["kind"] == "step.finished"
        and e["payload"].get("name") == "chip_to_microscope" and e["payload"].get("status") == "failed"
        and offline(e["payload"].get("note", "")) and any(s["id"] < e["id"] and
            s["payload"].get("seq") == e["payload"].get("seq") for s in starts)]
    terminal = [e for e in local if e["kind"] == "job.status"][-1:]
    if (not failures or not terminal or terminal[0]["id"] <= failures[-1]["id"]
            or terminal[0]["payload"].get("status") != run.get("status")
            or run.get("status") not in ("parked", "waiting_human", "failed")
            or not offline(terminal[0]["payload"].get("message", ""))):
        return result
    notices = [e for e in events if e["kind"] == "notification" and e["id"] > terminal[0]["id"]
        and e["payload"].get("level") == "needs_human" and e.get("run_id") in (None, rid)
        and "microscope" in e["payload"].get("text", "").lower()]
    result.update(verified=bool(notices), failure_ids=[e["id"] for e in failures],
                  escalation_ids=[e["id"] for e in notices])
    return result


def truth_geometry(snapshot, record, *, placement_tolerance_mm, physical_ids=None):
    """Private terminal score from actual model polygons, never exposed to agents."""
    design = DeviceDesignV1.model_validate(record)
    if not math.isfinite(placement_tolerance_mm) or placement_tolerance_mm <= 0:
        raise ValueError("Frozen positive placement tolerance is required")
    if not math.isclose(placement_tolerance_mm, design.pose_tolerance.translation_um/1000, abs_tol=1e-12, rel_tol=0):
        raise ValueError("Caller cannot replace the design's frozen translation tolerance")
    result = {"verified": False, "source": "synthetic_ground_truth", "contact_overlap_um2": {}}
    identities = dict(zip(design.pickup_order, design.pickup_order, strict=True)) if physical_ids is None else physical_ids
    if (not isinstance(identities, dict) or set(identities) != set(design.pickup_order)
            or any(not isinstance(v, str) or not v for v in identities.values())
            or len(set(identities.values())) != len(design.pickup_order)):
        return result
    physical_order = [identities[fid] for fid in design.pickup_order]
    targets = [o for o in snapshot.get("objects", []) if o["id"] == design.target_chip_id]
    if len(targets) != 1:
        return result
    target = targets[0]
    if target["kind"] != "chip" or tuple(target["size_mm"]) != design.target.size_mm:
        return result
    geometry = target.get("electrode_geometry", {})
    polygons = geometry.get("polygons", [])
    contacts = {c["id"]: c["points"] for c in polygons}
    expected_metal = target_metal_primitives(design.target)
    if len(polygons) != len(expected_metal) or set(contacts) != {p["id"] for p in expected_metal}:
        return result
    if isinstance(design.target, ImportedHallBarTarget):
        if geometry.get("source_binding") != target_import_binding(design.target):
            return result
        if any(c.get("source_primitive_index") != p["source_primitive_index"]
               for p in expected_metal for c in polygons if c["id"] == p["id"]):
            return result
    for primitive in expected_metal:
        expected = [[x-target["size_mm"][0]/2, y-target["size_mm"][1]/2] for x, y in primitive["points_mm"]]
        if contacts[primitive["id"]] != expected:
            return result
    selected = [f for f in target["flakes"] if f["id"] in physical_order]
    if (len(target["flakes"]) != 2 or [f["id"] for f in selected] != physical_order
            or [f["material"] for f in selected] != ["hBN", "Gr"]):
        return result
    if any(f["id"] in physical_order for o in snapshot["objects"] if o["id"] != target["id"] for f in o["flakes"]):
        return result
    if any(entry["flake"]["id"] in physical_order for stamp in snapshot["stamps"].values() for entry in stamp["stack"]):
        return result
    if any(hasattr(source, "region_mm") for source in design.sources):
        return _region_truth_geometry(design, target, selected, result, placement_tolerance_mm)
    outlines = [transform(convex_polygon(f["outline_mm"]), xy=(f["x_mm"] + target["size_mm"][0]/2,
        f["y_mm"] + target["size_mm"][1]/2)) for f in selected]
    result["shape_error_mm"], result["placement_error_mm"], result["rotation_error_deg"] = {}, {}, {}
    for source, actual in zip(design.sources, outlines, strict=True):
        expected = source.placed_outline()
        fit = rigid_polygon_error(expected, actual)
        result["shape_error_mm"][source.flake_id] = fit["shape_residual_mm"]
        if fit["shape_residual_mm"] is None or fit["shape_residual_mm"] > 1e-9:
            return result
        error = math.hypot(*fit["translation_mm"])
        result["placement_error_mm"][source.flake_id] = error
        result["rotation_error_deg"][source.flake_id] = fit["rotation_deg"]
        if error > placement_tolerance_mm or abs(fit["rotation_deg"]) > design.pose_tolerance.rotation_deg:
            return result
    result["whole_graphene_covered"] = contains(outlines[0], outlines[1])
    coverage = target_contact_coverage(design.target, outlines[1], minimum_overlap_um=design.contact_overlap_um)
    result["contact_coverage"] = coverage
    result["contact_overlap_um2"] = {key: value["intersection_area_um2"] for key, value in coverage["contacts"].items()}
    result["verified"] = result["whole_graphene_covered"] and coverage["status"] == "pass"
    return result


def _region_truth_geometry(design, target, selected, result, placement_tolerance_mm):
    """Independent full-material score; fitted shapes never replace actual truth."""
    from shapely.geometry import MultiPolygon, Polygon

    from qumus2.core.regions import (
        PolygonRegion,
        RegionComponent,
        as_region,
        region_compatible_with_bound,
        region_covers,
        transform_region,
    )
    from qumus2.records.device_design import source_boundary_uncertainty

    def geometry(region):
        return MultiPolygon([Polygon(c.exterior, c.holes) for c in region.components])

    def topology(region):
        return sorted(len(c.holes) for c in region.components)

    actuals = []
    result["shape_error_mm"], result["placement_error_mm"], result["rotation_error_deg"] = {}, {}, {}
    for source, flake in zip(design.sources, selected, strict=True):
        # Current truth has one ring; an explicit regional truth retains all
        # components/holes rather than falling back to a display outline.
        local = (as_region(flake["region_mm"]) if "region_mm" in flake else
                 PolygonRegion(components=(RegionComponent(exterior=flake["outline_mm"]),)))
        actual = transform_region(local, xy=(flake["x_mm"]+target["size_mm"][0]/2,
                                             flake["y_mm"]+target["size_mm"][1]/2))
        actuals.append(actual)
        if not hasattr(source, "region_mm"):
            # A mixed design does not change the legacy source's shape policy.
            if len(actual.components) != 1 or actual.components[0].holes:
                return result
            fit = rigid_polygon_error(source.placed_outline(), actual.components[0].exterior)
            compatible = fit["shape_residual_mm"] is not None and fit["shape_residual_mm"] <= 1e-9
        else:
            expected = source.placed_region()
            if topology(expected) != topology(actual) or not expected.components or not actual.components:
                return result
            a, b = geometry(expected), geometry(actual)
            # Oriented rectangles propose a proper rigid rotation only. Full
            # unmodified regions, including holes, decide compatibility below.
            fit = rigid_polygon_error(tuple(a.minimum_rotated_rectangle.exterior.coords)[:-1],
                                      tuple(b.minimum_rotated_rectangle.exterior.coords)[:-1])
            if fit["rotation_deg"] is None:
                return result
            ea, eb = a.centroid, b.centroid
            aligned = transform_region(transform_region(expected, xy=(-ea.x, -ea.y)),
                                       rotation_deg=fit["rotation_deg"], xy=(eb.x, eb.y))
            fit["translation_mm"] = [eb.x-ea.x, eb.y-ea.y]
            fit["shape_residual_mm"] = float(geometry(aligned).boundary.hausdorff_distance(b.boundary))
            compatible = region_compatible_with_bound(aligned, actual, source_boundary_uncertainty(source))
        result["shape_error_mm"][source.flake_id] = fit["shape_residual_mm"]
        if not compatible:
            return result
        error = math.hypot(*fit["translation_mm"])
        result["placement_error_mm"][source.flake_id] = error
        result["rotation_error_deg"][source.flake_id] = fit["rotation_deg"]
        if error > placement_tolerance_mm or abs(fit["rotation_deg"]) > design.pose_tolerance.rotation_deg:
            return result
    result["whole_graphene_covered"] = region_covers(*actuals)
    coverage = target_contact_coverage(design.target, actuals[1], minimum_overlap_um=design.contact_overlap_um)
    result["contact_coverage"] = coverage
    result["contact_overlap_um2"] = {key: value["intersection_area_um2"] for key, value in coverage["contacts"].items()}
    result["verified"] = result["whole_graphene_covered"] and coverage["status"] == "pass"
    return result


def audit_scenario(story, services, events, manifest):
    from qumus2.records.experiment_completion import completion_error

    store = services.store
    runs = store.list_runs()
    profile = profiles()[story]
    actions = action_evidence(events, profile)
    recovery = [recovery_evidence(events, runs, 1, timeout_only=story == "collaboration")]
    if story == "complete":
        recovery.append(recovery_evidence(events, runs, 2))
    checks = {"hallbar_actions_verified": actions["all_applied"],
              "hallbar_recoveries_verified": all(r["verified"] for r in recovery),
              "hallbar_virtual_learning_disabled": store.bench == "sim" and not store.experience_sandbox}
    evidence = {"hallbar_actions": actions, "hallbar_recoveries": recovery}
    state = (store.get_project(services.project_id) or {}).get("state", {})
    if story == "honest_stop":
        stop = human_stop_evidence(events, runs)
        evidence["hallbar_human_stop"] = stop
        checks.update(hallbar_truthfully_blocked=state.get("status") == "blocked" and stop["verified"],
                      hallbar_no_completed_stack=not any(r["flow"] == "stack_device" and r["status"] == "succeeded" for r in runs))
    else:
        devices = [s for s in store.list_samples(kind="device") if s["props"].get("device_design")]
        results = [s["props"].get("device_design_result", {}) for s in devices]
        checks.update(hallbar_one_checked_device=len(devices) == 1 and results[0].get("status") == "pass",
                      hallbar_project_complete=state.get("status") == "done" and completion_error(store, services.project_id) is None)
        truth = services.lab.world.evaluation_snapshot()
        record = devices[0]["props"]["device_design"] if len(devices) == 1 else {}
        selected = record.get("pickup_order", [])
        identities = {fid: (store.get_sample(fid) or {}).get("props", {}).get("sim_id") for fid in selected}
        catalogue = store.list_samples(kind="flake")
        if any(sum(sample.get("props", {}).get("sim_id") == physical for sample in catalogue) != 1
               for physical in identities.values()):
            identities = {}
        score = (truth_geometry(truth, record, physical_ids=identities,
            placement_tolerance_mm=DeviceDesignV1.model_validate(record).pose_tolerance.translation_um/1000)
            if len(devices) == 1 else {"verified": False})
        checks["hallbar_actual_terminal_geometry"] = score["verified"]
        evidence["hallbar_terminal_geometry"] = score
    return checks, deepcopy(evidence)


def selected_target_bundle(selected=None):
    """Resolve exactly one operator-owned bundle without creating any files."""
    choices = ([Path(selected).resolve()] if selected is not None else
               sorted(OWNER_WORKSPACE.glob("targets/*/target.json")))
    if len(choices) != 1:
        raise ValueError("Select exactly one existing owner target bundle; missing/ambiguous selection cannot use synthetic geometry")
    path = choices[0]
    return path.parent if path.name == "target.json" else path


def target_bundle_snapshot(bundle):
    """Freeze exact source/definition bytes and the shared construction receipt."""
    from qumus2.testbench.hallbar import load_imported_hallbar_target

    bundle = selected_target_bundle(bundle)
    target = load_imported_hallbar_target(bundle, workspace=None)
    return target, {"path": str(bundle / "target.json"),
        "target_json_sha256": _sha_file(bundle / "target.json"),
        "gds_sha256": _sha_file(bundle / "electrode-layout.gds"), "binding": target_import_binding(target)}


def simulation_config(*, target_bundle=None):
    """One owner-imported tapes-only profile, resolved before construction."""
    from qumus2.config import simulation_profile
    from qumus2.core.optical_profiles import V1_MATERIAL_CAPTURE_PROFILE
    from qumus2.testbench.hallbar import with_hallbar_target

    target, _ = target_bundle_snapshot(selected_target_bundle(target_bundle))
    config = with_hallbar_target(simulation_profile(SIMULATION_PROFILE), target=target)
    config["material_recognition"] = {"backend": "v1_lab_rgb", "capture_profile": V1_MATERIAL_CAPTURE_PROFILE}
    return config


def campaign_config(campaign):
    """Validate the immutable campaign bundle read-only before deriving config."""
    frozen = campaign.get("target_bundle")
    if not isinstance(frozen, dict) or not isinstance(frozen.get("path"), str) or not Path(frozen["path"]).is_absolute():
        raise ValueError("Frozen imported target bundle is required; old campaigns cannot be upgraded")
    _, current = target_bundle_snapshot(frozen["path"])
    if current != frozen:
        raise ValueError("Frozen target bundle/source/owner instruction changed")
    return simulation_config(target_bundle=frozen["path"])


def _sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def _read_only_database(path):
    """Read active WAL only with its existing sidecars; never create them."""
    path = Path(path).resolve()
    with path.open("rb") as handle:
        wal_mode = handle.read(20)[18:20] == b"\x02\x02"
    wal, shm = (path.with_name(path.name + suffix) for suffix in ("-wal", "-shm"))
    active = wal.exists() and wal.stat().st_size > 0
    if active and not shm.is_file():
        raise ValueError("Existing WAL requires its original shared-memory evidence; read-only checks cannot create it")
    uri = path.as_uri() + ("?mode=ro&immutable=1" if wal_mode and not active else "?mode=ro")
    connection = sqlite3.connect(uri, uri=True)
    try:
        yield connection
        if not active and wal.exists() and wal.stat().st_size > 0:
            raise ValueError("Database changed during read-only frozen accounting")
    finally:
        connection.close()


def _json_snapshot(path):
    path = Path(path)
    if path.suffix != ".json":
        raise ValueError("Use an explicit frozen JSON snapshot, never a live database")
    value = json.loads(path.read_text())
    return value, _sha_file(path)


def experience_snapshot(store):
    """Canonical advice content, excluding only clock-local seeded row dates.

    Stored rows are never rewritten. Votes, scientific provenance, chosen
    baselines and all non-seed timestamps remain part of the comparison.
    """
    from qumus2.config import load_yaml
    from qumus2.records.experience import ExperienceBook

    summary = deepcopy(ExperienceBook(store).summary())
    seeds = {entry["id"] for entry in load_yaml("experience.yaml").get("seed", [])}
    for entry in summary["experiences"]:
        if entry["id"] in seeds and entry.get("author") == "seed":
            entry.pop("created_at", None)
            entry.pop("updated_at", None)
    summary["experiences"].sort(key=lambda entry: entry["id"])
    return {"version": "hallbar-experience-snapshot-v1", "summary": summary,
            "votes": {entry["id"]: store.votes(entry["id"]) for entry in summary["experiences"]}}


def prepare_campaign(directory, *, target_bundle=None, demo_authorization=None):
    """Create explicit offline input snapshots; leave every execution gate false."""
    import tempfile

    from qumus2.app import build_services
    from qumus2.testbench.hallbar import load_imported_hallbar_target

    bundle = selected_target_bundle(target_bundle)
    target, _ = target_bundle_snapshot(bundle)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    load_imported_hallbar_target(bundle, workspace=directory)
    frozen_bundle = imported_target_directory(directory, target)
    config = simulation_config(target_bundle=frozen_bundle)
    temporary = tempfile.TemporaryDirectory(prefix="qumus-hallbar-inputs-")
    services = None
    try:
        load_imported_hallbar_target(frozen_bundle, workspace=temporary.name)
        services = build_services(Path(temporary.name) / "qumus.db", testbench_config=config, seed=8, use_openai=False)
        services._campaign_inputs = temporary
        truth = services.lab.world.evaluation_snapshot()
        if (services.store.list_samples(kind="flake") or services.store.list_samples(kind="device")
                or any(obj["flakes"] for obj in truth["objects"])
                or any(stamp["stack"] for stamp in truth["stamps"].values())):
            raise ValueError("Initial Hall-bar sources must be tapes, without prepared flakes")
        library = experience_snapshot(services.store)
    finally:
        if services is None:
            temporary.cleanup()
        elif not asyncio.run(services.close_local())["safe_to_close_resources"]:
            raise RuntimeError("Campaign input services did not settle; their private resources are retained")
    initial, experience = directory / "initial-state.json", directory / "experience.json"
    initial.write_text(json.dumps(truth, indent=2, allow_nan=False)+"\n")
    experience.write_text(json.dumps(library, indent=2, allow_nan=False)+"\n")
    identity = source_identity(initial_state=initial, experience=experience, config=config, target_bundle=frozen_bundle)
    campaign = campaign_template(identity, campaign_path=directory / "attempts",
                                 initial_state=initial, experience=experience, config=config, target_bundle=frozen_bundle)
    if demo_authorization is not None:
        path = Path(demo_authorization).resolve(strict=True)
        campaign.update(execution_scope=DEMO_SCOPE, launch_authorized=True,
            demo_authorization={"path": str(path), "sha256": _sha_file(path)}, demo_smoke=None)
        _demo_authorization(campaign)
    path = directory / "campaign.json"
    path.write_text(json.dumps(campaign, indent=2, allow_nan=False)+"\n")
    return path


def source_identity(*, initial_state, experience, config=None, target_bundle=None):
    """Read-only identity; no credentials, model, devices or registry creation."""
    from qumus2.ui.recording import source_fingerprint

    bundle = selected_target_bundle(target_bundle)
    _, target_snapshot = target_bundle_snapshot(bundle)
    expected_config = simulation_config(target_bundle=bundle)
    config = expected_config if config is None else config
    if config != expected_config:
        raise ValueError("Simulation config differs from the immutable owner target and linked raw-source profile")
    _, initial_hash = _json_snapshot(initial_state)
    _, experience_hash = _json_snapshot(experience)
    def files(paths):
        return digest({str(p.relative_to(ROOT)): _sha_file(p) for p in sorted(paths) if p.is_file()})
    return {"application": source_fingerprint(), "simulation_config": digest(config),
        "config": files((ROOT / "qumus2/config").rglob("*")),
        "skills": files((ROOT / "qumus2/agents/skills").rglob("*")),
        "initial_state": initial_hash, "experience": experience_hash,
        "target_bundle": {key: value for key, value in target_snapshot.items() if key != "path"},
        "protocol": digest({"revision": PROTOCOL_REVISION, "simulation_profile": SIMULATION_PROFILE,
                            "scenarios": profiles()}),
        "harness": files([Path(__file__), ROOT / "scripts/record_three_demos.py",
                          ROOT / "scripts/record_model_demos.py", ROOT / "pyproject.toml"])}


def campaign_template(identity, *, campaign_path, initial_state, experience, config=None, target_bundle=None):
    """Unreleased first-delivery plan; fourteen cells have no executable budgets."""
    from qumus2.config import load_yaml

    bundle = selected_target_bundle(target_bundle)
    _, target_snapshot = target_bundle_snapshot(bundle)
    expected_config = simulation_config(target_bundle=bundle)
    config = expected_config if config is None else config
    if config != expected_config:
        raise ValueError("Campaign must freeze the exact imported target config")
    cells = []
    for index, model in enumerate(MODELS):
        for story in profiles():
            first = model == "openai/gpt-6-astra" and story == "complete"
            cells.append({"id": FIRST_CELL if first else f"model{index+1}-{story}", "model": model,
                "story": story, "limits": deepcopy(FIRST_LIMITS) if first else None,
                "release": "pending_first_acceptance" if first else "owner_review_and_new_budget_required"})
    return {"version": 3, "campaign_id": CAMPAIGN_ID, "campaign_path": str(Path(campaign_path).resolve()),
        "protocol_revision": PROTOCOL_REVISION, "simulation_profile": SIMULATION_PROFILE,
        "target_bundle": target_snapshot,
        "first_cell_id": FIRST_CELL, "first_cell_model": "openai/gpt-6-astra", "allow_retries": True,
        "seed": 8, "simulation_config_sha256": digest(config), "identity": deepcopy(identity),
        "initial_state": str(Path(initial_state).resolve()), "experience": str(Path(experience).resolve()),
        "profiles": profiles(), "models": {model: {"deployment": backend, "capabilities": "unverified"}
                                            for model, backend in MODELS.items()},
        "cells": cells, "limits": deepcopy(FIRST_LIMITS), "gateway": load_yaml("model_context_limits.yaml")["base_url"],
        "api": "responses", "stream": False, "web_search": False, "learning_enabled": False,
        "rates": {"input_price": 10., "output_price": 50., "cached_input_price": None,
                  "source": "administrator_quote_2026-10-27 estimates; cache tariff/account multiplier unknown; no cache discount or invoice guarantee"},
        "context_limit_source": "administrator_table", "launch_authorized": False,
        "integrated_acceptance": False, "protocol_ready": False, "local_acceptance": None}


def validate_campaign(campaign):
    """Only the owner-authorized first cell has a funded execution path here."""
    first = TrialRegistry.first_identity(campaign)
    config = campaign_config(campaign)
    expected = {(model, story) for model in MODELS for story in profiles()}
    cells = campaign.get("cells", [])
    if (type(campaign.get("version")) is not int or campaign.get("version") != 3
            or campaign.get("protocol_revision") != PROTOCOL_REVISION
            or campaign.get("campaign_id") != CAMPAIGN_ID
            or campaign.get("simulation_profile") != SIMULATION_PROFILE
            or campaign.get("simulation_config_sha256") != digest(config)
            or first != {"id": FIRST_CELL, "model": "openai/gpt-6-astra", "story": "complete"}
            or len(cells) != 15 or len({c.get("id") for c in cells}) != 15
            or {(c.get("model"), c.get("story")) for c in cells} != expected
            or campaign.get("profiles") != profiles() or campaign.get("seed") != 8
            or campaign.get("models") != {m: {"deployment": b, "capabilities": "unverified"} for m, b in MODELS.items()}):
        raise ValueError("Exact frozen five-model/three-story Hall-bar protocol required")
    if (campaign.get("limits") != FIRST_LIMITS
            or any(c.get("limits") != (FIRST_LIMITS if c["id"] == FIRST_CELL else None) for c in cells)
            or campaign.get("learning_enabled") is not False or campaign.get("stream") is not False
            or campaign.get("web_search") is not False or campaign.get("api") != "responses"):
        raise ValueError("First-cell inclusive100USD limits, unfunded remaining cells and virtual frozen policy required")
    if campaign.get("context_limit_source") != "administrator_table":
        raise ValueError("Configured context capacity must retain administrator-table provenance")
    rates = campaign.get("rates", {})
    if rates.get("input_price") != 10. or rates.get("output_price") != 50. or rates.get("cached_input_price") is not None:
        raise ValueError("Approved administrator input/output estimates without an unverified cache discount required")
    for key in ("initial_state", "experience", "campaign_path"):
        if not isinstance(campaign.get(key), str) or not Path(campaign[key]).is_absolute():
            raise ValueError("Explicit absolute private paths required")
    if campaign.get("execution_scope") is not None:
        _demo_authorization(campaign)


def _demo_authorization(campaign):
    """Operator's explicit owner relay, never a scientific acceptance receipt."""
    if (campaign.get("execution_scope") != DEMO_SCOPE or campaign.get("launch_authorized") is not True
            or campaign.get("integrated_acceptance") is not False or campaign.get("protocol_ready") is not False):
        raise ValueError("Demo-only launch must preserve unaccepted scientific/protocol gates")
    receipt = campaign.get("demo_authorization") or {}
    path = Path(receipt.get("path", ""))
    if not path.is_absolute() or _sha_file(path) != receipt.get("sha256"):
        raise ValueError("Exact owner demo-only authorization receipt required")
    body = json.loads(path.read_text())
    instruction_hash = body.get("instruction_sha256", "")
    if (body.get("version") != 1 or body.get("source_kind") != "direct_owner_instruction_relay"
            or body.get("scope") != DEMO_SCOPE or body.get("authorize_paid_first_recording") is not True
            or body.get("model") != "openai/gpt-6-astra" or body.get("cumulative_estimated_usd") != 100
            or body.get("target_source_sha256") != campaign["target_bundle"]["binding"]["source_sha256"]
            or body.get("remaining_fourteen_authorized") is not False
            or not isinstance(body.get("reference"), str) or not body["reference"].strip()
            or not isinstance(instruction_hash, str) or len(instruction_hash) != 64
            or any(c not in "0123456789abcdef" for c in instruction_hash)):
        raise ValueError("One exact Astra first-recording owner authorization required")
    return body


def preflight(campaign, *, cell_id=FIRST_CELL, config=None):
    """No provider calls; a local receipt is evidence for lead review, not self-acceptance."""
    validate_campaign(campaign)
    config = campaign_config(campaign) if config is None else config
    identity = source_identity(initial_state=campaign["initial_state"], experience=campaign["experience"], config=config,
                               target_bundle=campaign["target_bundle"]["path"])
    blockers = []
    if cell_id != FIRST_CELL:
        blockers.append("The remaining fourteen cells require direct owner review and new budgets; this adapter cannot execute them")
    if identity != campaign.get("identity") or digest(config) != campaign.get("simulation_config_sha256"):
        blockers.append("Frozen source/config/protocol/input identity changed")
    demo = campaign.get("execution_scope") == DEMO_SCOPE
    if not demo:
        for key in ("launch_authorized", "integrated_acceptance", "protocol_ready"):
            if campaign.get(key) is not True:
                blockers.append(key + " is not accepted")
    receipt = campaign.get("demo_smoke" if demo else "local_acceptance")
    try:
        if not isinstance(receipt, dict) or not Path(receipt["path"]).is_absolute() or _sha_file(receipt["path"]) != receipt["sha256"]:
            raise ValueError("Local acceptance receipt missing/changed")
        body = json.loads(Path(receipt["path"]).read_text())
        commands = body.get("commands", [])
        if (body.get("version") != 1 or body.get("identity") != identity
                or (demo and body.get("scope") != DEMO_SCOPE)
                or set(body.get("checks", {})) != (DEMO_CHECKS if demo else ACCEPTANCE_CHECKS)
                or any(value is not True for value in body["checks"].values()) or not commands):
            raise ValueError("Source-bound audited local checks/command receipts incomplete")
        for command in commands:
            if (not isinstance(command, dict) or not isinstance(command.get("command"), str) or not command["command"].strip()
                    or type(command.get("exit_code")) is not int or command["exit_code"] != 0
                    or not Path(command["output_path"]).is_absolute()
                    or _sha_file(command["output_path"]) != command["output_sha256"]):
                raise ValueError("Actual test command/output hash receipt missing/changed")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        blockers.append(str(exc))
    return {"ready_for_qualification": not blockers, "blockers": blockers, "cell_id": cell_id,
            "identity": identity, "execution_source_sha256": digest({"identity": identity, "campaign": campaign}),
            "other_cells_executable": False, "acceptance_receipt_requires_lead_audit": True,
            "execution_scope": DEMO_SCOPE if demo else "scientific_acceptance_required",
            "full_experiment_accepted": False if demo else campaign.get("integrated_acceptance") is True}


class HallbarCampaignAdapter:
    """Thin hooks over the one recorder, one registry and existing shared ledger."""

    def __init__(self, campaign, *, config=None, now=time.time):
        self.campaign = deepcopy(campaign)
        self.config = deepcopy(campaign_config(campaign) if config is None else config)
        self.now, self.services, self.qualification_phase = now, None, None
        self.expected_source = preflight(self.campaign, config=self.config)["execution_source_sha256"]

    def source(self):
        current = source_identity(initial_state=self.campaign["initial_state"], experience=self.campaign["experience"], config=self.config,
                                  target_bundle=self.campaign["target_bundle"]["path"])
        return digest({"identity": current, "campaign": self.campaign})

    def prepare_attempt(self, manifest, *, continuing):
        """Explicit fresh-attempt copy; a continuation only verifies saved bytes."""
        from qumus2.testbench.hallbar import load_imported_hallbar_target

        self.check_ready()
        if self.config != campaign_config(self.campaign):
            raise ValueError("Frozen attempt configuration changed before preparation")
        if (manifest.get("campaign_sha256") != digest(self.campaign)
                or manifest.get("simulation_config_sha256") != digest(self.config)
                or manifest.get("source") != self.expected_source or self.source() != self.expected_source):
            raise ValueError("Frozen attempt source/target/config identity changed before preparation")
        target, frozen = target_bundle_snapshot(self.campaign["target_bundle"]["path"])
        workspace = Path(manifest["workspace"]).resolve()
        if continuing:
            _, saved = target_bundle_snapshot(imported_target_directory(workspace, target))
            if {k: v for k, v in saved.items() if k != "path"} != {k: v for k, v in frozen.items() if k != "path"}:
                raise ValueError("Saved attempt target bundle changed; continuation never repairs files")
        else:
            load_imported_hallbar_target(Path(frozen["path"]).parent, workspace=workspace)

    def check_ready(self):
        result = preflight(self.campaign, config=self.config)
        if result["blockers"]:
            raise ValueError("Hall-bar local acceptance not ready: " + "; ".join(result["blockers"]))

    def guard(self, registry, manifest, *, next_usage=None, request_payload=None, runtime=None):
        from qumus2.core.run_paths import original_run_path
        from scripts.record_model_demos import request_identity_valid

        self.check_ready()
        if next_usage is not None and (set(next_usage) != {"estimated_usd", "input_tokens", "output_tokens", "http_attempts", "wall_seconds"}
                or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in next_usage.values())):
            raise ValueError("Finite nonnegative next-request accounting required")
        if (manifest.get("cell_id") != FIRST_CELL or manifest.get("model") != "openai/gpt-6-astra"
                or manifest.get("source") != self.expected_source or self.source() != self.expected_source
                or manifest.get("campaign_sha256") != digest(self.campaign)
                or manifest.get("simulation_config_sha256") != digest(self.config)):
            raise ValueError("Frozen first-cell identity/config changed")
        if self.services is not None:
            from qumus2.records.device_design import validate_imported_target_files

            target, _ = target_bundle_snapshot(self.campaign["target_bundle"]["path"])
            validate_imported_target_files(self.services.store, target)
            if experience_snapshot(self.services.store) != _json_snapshot(self.campaign["experience"])[0]:
                raise ValueError("Frozen experience library changed")
        if runtime is not None:
            from qumus2.agents.model_context import context_defaults

            contract = manifest.get("runtime_contract", {})
            expected_context = context_defaults(replace(runtime.creds, context_window_tokens=None)).get(runtime.creds.model_id)
            if (runtime.creds.model_id != self.campaign["first_cell_model"]
                    or runtime.creds.base_url.rstrip("/") != self.campaign["gateway"].rstrip("/")
                    or runtime.creds.api != self.campaign["api"] or runtime.creds.stream is not False
                    or any(getattr(runtime.creds, key) != self.campaign["rates"][key]
                           for key in ("input_price", "output_price", "cached_input_price"))
                    or runtime._context_limit_source(runtime.creds.model_id) != "administrator_table"
                    or type(expected_context) is not int or runtime.creds.context_window_tokens != expected_context
                    or contract.get("test_budget_usd") != 100):
                raise ValueError("Actual first-cell routing/prices/context/budget differs from freeze")
        with _read_only_database(registry.db) as db:
            rows = db.execute("SELECT manifest,status,result FROM attempt ORDER BY story,number").fetchall()
        records = [(json.loads(m), status, json.loads(result) if result else {}) for m, status, result in rows]
        if not any(m["workspace"] == manifest["workspace"] for m, _, _ in records):
            records.append((manifest, "started", {}))
        totals = dict.fromkeys(("estimated_usd", "input_tokens", "output_tokens", "http_attempts", "wall_seconds"), 0.)
        for saved, status, result in records:
            if (saved.get("cell_id") != FIRST_CELL or saved.get("campaign_sha256") != digest(self.campaign)
                    or saved.get("ledger") != manifest["ledger"]):
                raise ValueError("Another campaign/cell/ledger cannot enter first-cell accounting")
            current = saved["workspace"] == manifest["workspace"]
            path = Path(saved["workspace"]) / "qumus.db"
            events = []
            if path.exists():
                with _read_only_database(path) as db:
                    events = [{"id": i, "kind": k, "agent": a, "payload": json.loads(p)}
                              for i, k, a, p in db.execute("SELECT id,kind,agent,payload_json FROM event ORDER BY id")]
            elif not current and not (path.parent / "startup-failure.json").is_file():
                raise ValueError("Original failed-attempt evidence missing")
            entries, usage = http_accounting(events)
            if entries and not request_identity_valid(events, self.campaign["first_cell_model"]):
                raise ValueError("Recorded HTTP request/usage/failure identities changed")
            with _read_only_database(saved["ledger"]) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='spend'").fetchone()
                spend = db.execute("SELECT id,reserved,actual FROM spend WHERE scope=?",
                    (str(original_run_path(path)),)).fetchall() if exists else []
            admitted = {entry["admission"].get("reservation_id") for entry in entries}
            if any(not math.isfinite(value) or value < 0 for _, reserved, actual in spend
                   for value in (reserved, actual) if value is not None):
                raise ValueError("Invalid shared-ledger spending cannot establish remaining allowance")
            extra = [row for row in spend if row[0] not in admitted and row[2] != 0]
            if extra and not (current and next_usage is not None and len(extra) == 1 and extra[0][2] is None):
                raise ValueError("Shared-ledger reservation lacks a matching admitted HTTP attempt")
            for entry in entries:
                reservation = next((row for row in spend if row[0] == entry["admission"].get("reservation_id")), None)
                request = entry["request"] or {}
                if (reservation is None or request.get("model") != self.campaign["first_cell_model"]
                        or request.get("gateway") != self.campaign["gateway"] or request.get("api") != self.campaign["api"]
                        or (not entry["usage_known"] and reservation[2] is not None)):
                    raise ValueError("Route identity or retained unknown reservation changed")
            for resource in ("input_tokens", "output_tokens", "http_attempts"):
                totals[resource] += usage[resource]
            totals["estimated_usd"] += sum(reserved if actual is None else actual for _, reserved, actual in spend)
            elapsed = self.now()-saved["started_wall"] if status in ("started", "interrupted") else result.get("elapsed_wall_s")
            if not isinstance(elapsed, int | float) or not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError("Original attempt elapsed-time accounting missing")
            totals["wall_seconds"] += elapsed
        for resource, used in totals.items():
            increment = (next_usage or {}).get(resource, 0)
            if used+increment > FIRST_LIMITS[resource]:
                raise ValueError("Inclusive first-cell " + resource + " ceiling exhausted")
        if self.qualification_phase is not None and next_usage:
            phase = self.qualification_phase
            events = [e.model_dump() for e in runtime.store.events_after(phase["after_event_id"], limit=1_000_000)]
            _, used = http_accounting(events)
            tools = request_payload.get("tools", [])
            if (self.now()-phase["started_wall"] > 300
                    or any(used[k]+next_usage[k] > phase["limits"][k] for k in ("input_tokens", "output_tokens", "http_attempts"))
                    or runtime._ledger.total(runtime._budget_scope)-phase["ledger_before"]+next_usage["estimated_usd"] > 1
                    or runtime._request_context.get("agent") != "orchestrator" or next_usage["output_tokens"] > 256
                    or len(tools) != 1 or tools[0].get("name", tools[0].get("function", {}).get("name")) != "get_project_context"
                    or (used["http_attempts"] == 0 and phase["image_uri"] not in json.dumps(request_payload))):
                raise ValueError("Bounded read-only image/tool qualification contract exceeded")
        return totals

    def check_services(self, services, manifest, continuing):
        from qumus2.agents.policy import Decision
        from qumus2.records.device_design import (
            parse_hallbar_target,
            validate_imported_target_files,
        )
        from qumus2.testbench.bench_geometry import PROFILE, geometry_binding

        self.check_ready()
        expected, _ = target_bundle_snapshot(self.campaign["target_bundle"]["path"])
        chip = services.store.get_sample("chip_e01") or {}
        props = chip.get("props", {})
        if (parse_hallbar_target(props.get("device_design_target")) != expected
                or props.get("imported_target_binding") != target_import_binding(expected)):
            raise ValueError("Actual target ledger differs from the preconstructed imported target")
        validate_imported_target_files(services.store, expected)
        if services.store.bench != "sim" or services.store.experience_sandbox:
            raise ValueError("Only the virtual laboratory with experimental learning disabled is admitted")
        if (geometry_binding(services.lab.bench) or {}).get("id") != PROFILE:
            raise ValueError("Actual Hall-bar bench must use the frozen linked SIM geometry")
        if experience_snapshot(services.store) != _json_snapshot(self.campaign["experience"])[0]:
            raise ValueError("Loaded experience library differs from frozen input")
        if services.supervisor is not None and digest(services.supervisor.testbench_config) != digest(self.config):
            raise ValueError("Actual supervised source configuration differs from freeze")
        if continuing:
            if services.store.get_meta("hallbar.initial_truth_sha256") != self.campaign["identity"]["initial_state"]:
                raise ValueError("Saved initial-state binding missing/changed")
        else:
            truth = services.lab.world.evaluation_snapshot()
            if truth != _json_snapshot(self.campaign["initial_state"])[0]:
                raise ValueError("Fresh tapes-only initial state differs from frozen source")
            if (services.store.list_samples(kind="flake") or services.store.list_samples(kind="device")
                    or any(obj.get("flakes") for obj in truth["objects"])
                    or any(stamp.get("stack") for stamp in truth["stamps"].values())):
                raise ValueError("Hall-bar recording must start without prepared flakes or devices")
            services.store.set_meta("hallbar.initial_truth_sha256", self.campaign["identity"]["initial_state"])
        self.services = services
        original = services.gateway.pre
        def frozen_learning(agent, tool, args, tool_use_id=None):
            spec = services.gateway.registry.get(tool)
            if spec and spec.name in {"teach_experience", "retire_experience", "propose_experience", "vote_experience", "review_runs"}:
                return Decision(False, "Virtual recording keeps the experience library frozen", "hallbar_learning_frozen")
            return original(agent, tool, args, tool_use_id)
        services.gateway.pre = frozen_learning

    async def qualify(self, services, manifest):
        """Reuse normal SDK/tool admission and existing qualification image/checks."""
        from qumus2.agents.policy import Decision
        from scripts.record_model_demos import (
            QUALIFICATION_PROMPT,
            qualification_image,
            request_identity_valid,
        )

        self.check_ready()
        if manifest["cell_id"] != FIRST_CELL or manifest["story"] != "complete":
            raise ValueError("Qualification belongs only to the released Astra first cell")
        rt = services.runtime
        rt._ensure()
        image = qualification_image()
        image_uri = "data:image/png;base64," + base64.b64encode(image).decode()
        physical, library = services.lab.world.evaluation_snapshot(), experience_snapshot(services.store)
        samples, runs = services.store.list_samples(), services.store.list_runs()
        start = services.store.emit("demo.qualification_started", {"model": rt.creds.model_id,
            "source": manifest["source"], "cell_id": FIRST_CELL, "image_sha256": hashlib.sha256(image).hexdigest()})
        self.qualification_phase = {"after_event_id": start, "started_wall": self.now(),
            "ledger_before": rt._ledger.total(rt._budget_scope), "image_uri": image_uri,
            "limits": {"estimated_usd": 1., "input_tokens": 131072, "output_tokens": 512, "http_attempts": 2}}
        phase = self.qualification_phase
        agent, original_pre = rt._agents["orchestrator"], services.gateway.pre
        original = agent.instructions, agent.tools, agent.model_settings, rt.cfg["openai"].get("max_output_tokens", 4096)
        agent.instructions = "Check this exact route's synthetic image and read-only tool protocol. Follow the bounded task."
        agent.tools = [tool for tool in agent.tools if tool.name == "get_project_context"]
        agent.model_settings = replace(agent.model_settings, max_tokens=256)
        rt.cfg["openai"]["max_output_tokens"] = 256
        def readonly(role, tool, args, tool_use_id=None):
            spec = services.gateway.registry.get(tool)
            if role != "orchestrator" or not spec or spec.name != "get_project_context":
                return Decision(False, "Qualification permits only get_project_context", "hallbar_qualification_readonly")
            return original_pre(role, tool, args, tool_use_id)
        services.gateway.pre = readonly
        exception = None
        try:
            await asyncio.wait_for(rt.send_input([{"role": "user", "content": [
                {"type": "input_text", "text": QUALIFICATION_PROMPT},
                {"type": "input_image", "image_url": image_uri}]}]), timeout=300)
        except Exception as exc:
            exception = type(exc).__name__
        finally:
            agent.instructions, agent.tools, agent.model_settings, rt.cfg["openai"]["max_output_tokens"] = original
            services.gateway.pre = original_pre
            self.qualification_phase = None
        events = [e.model_dump() for e in services.store.events_after(start, limit=1_000_000)]
        entries, usage = http_accounting(events)
        calls = [e for e in events if e["kind"] == "tool.call"]
        results = [e for e in events if e["kind"] == "tool.result"]
        try:
            answer = json.loads(str(rt.last_result.final_output))
        except (AttributeError, ValueError, TypeError):
            answer = {}
        checks = {"two_http_sends": len(entries) == 2,
            "provider_usage_complete": http_evidence(events)["provider_usage_complete"],
            "request_identity": request_identity_valid(events, rt.creds.model_id),
            "no_http_failure": not any(e["kind"] == "agent.request_failure" for e in events),
            "readonly_tool_continuation": len(calls) == len(results) == 1 and
                calls[0]["payload"].get("tool") == results[0]["payload"].get("tool") == "get_project_context" and
                bool(calls[0]["payload"].get("tool_use_id")) and calls[0]["payload"].get("tool_use_id") == results[0]["payload"].get("tool_use_id")
                and results[0]["payload"].get("status") == "ok",
            "visual_relation": answer.get("top_colors") == ["orange", "blue", "green"] and answer.get("lower_left_shape") == "triangle",
            "project_from_tool": answer.get("project_id") == services.project_id,
            "physical_state_unchanged": services.lab.world.evaluation_snapshot() == physical,
            "experience_unchanged": experience_snapshot(services.store) == library,
            "samples_and_jobs_unchanged": services.store.list_samples() == samples and services.store.list_runs() == runs,
            "bounded_usage": all(usage[key] <= phase["limits"][key] for key in ("input_tokens", "output_tokens", "http_attempts")),
            "bounded_cost": rt._ledger.total(rt._budget_scope)-phase["ledger_before"] <= 1.,
            "bounded_time": self.now()-phase["started_wall"] <= 300,
            "source_unchanged": self.source() == manifest["source"], "no_exception": exception is None}
        result = {"version": 1, "passed": all(checks.values()), "checks": checks,
            "model": rt.creds.model_id, "gateway": self.campaign["gateway"], "api": self.campaign["api"],
            "deployment": self.campaign["models"][rt.creds.model_id]["deployment"],
            "campaign_sha256": manifest["campaign_sha256"], "source": manifest["source"],
            "after_event_id": start, "through_event_id": services.store.last_event_id(),
            "event_prefix_sha256": digest(events), "usage": usage, "exception_type": exception,
            "cost_basis": "Same shared-ledger estimates and retained unknown reservations; not a provider invoice"}
        services.store.emit("demo.qualification_result", result)
        services.store.set_meta("demo.qualification", json.dumps(result))
        if not result["passed"]:
            raise ValueError("Astra image/tool qualification failed; preserve the attempt and unknown reservations")
        # Existing runtime rebuilds the normal role/session state. Omitting a
        # context override preserves administrator_table provenance.
        await rt.select_model(rt.creds.model_id, rt.creds.input_price, rt.creds.output_price,
                              cached_input_price=rt.creds.cached_input_price)
        return result

    def collect_evidence(self, services, manifest):

        through = services.store.last_event_id()
        runs = services.store.list_runs()
        if any(run["status"] in ("queued", "running") for run in runs):
            raise ValueError("Terminal private truth requires quiescent laboratory jobs")
        truth = services.lab.world.evaluation_snapshot()
        events = [e.model_dump() for e in services.store.events_after(0, limit=1_000_000)]
        if through != services.store.last_event_id():
            raise ValueError("Terminal event prefix changed during private truth capture")
        packet = {"version": 1, "source": "synthetic_ground_truth_private", "truth": truth,
            "samples": services.store.list_samples(), "runs": runs, "bench": services.lab.bench,
            "campaign_sha256": manifest["campaign_sha256"], "cell_id": FIRST_CELL,
            "execution_source_sha256": manifest["source"], "last_event_id": through,
            "event_prefix_sha256": digest(events)}
        path = Path(manifest["workspace"]) / f"terminal-truth-through-{through}.json"
        with path.open("x") as handle:
            json.dump(packet, handle, indent=2, allow_nan=False)
        return {"checks": {"hallbar_private_truth_available": True,
                "hallbar_learning_library_unchanged": experience_snapshot(services.store) == _json_snapshot(self.campaign["experience"])[0]},
            "evidence": {"terminal_truth_file": path.name, "terminal_truth_file_sha256": _sha_file(path),
                         "terminal_truth_last_event_id": through,
                         "execution_scope": self.campaign.get("execution_scope", "scientific_acceptance_required"),
                         "scientific_acceptance_claimed": self.campaign.get("execution_scope") != DEMO_SCOPE}}


async def run_cell(campaign, cell_id=FIRST_CELL, *, resume=False, retry_failed=False,
                   qualify=True, qualification_only=False, live=False, frozen_source=None):
    """Explicitly released normal engine entry; this function never loops attempts."""
    from qumus2.agents.openai_credentials import load_openai_credentials
    from scripts.record_model_demos import observed_qualification
    from scripts.record_three_demos import record_demo

    adapter = HallbarCampaignAdapter(campaign)
    adapter.check_ready()
    if not live or frozen_source != adapter.expected_source or cell_id != FIRST_CELL:
        raise ValueError("Only the explicitly live, source-frozen first Astra cell is executable")
    TrialRegistry.require_delivery(campaign, cell_id, starting=retry_failed, retry_failed=retry_failed)
    qualified = observed_qualification(campaign, campaign["first_cell_model"])
    if not qualified and (not qualify or resume):
        raise ValueError("Source-bound image/tool qualification required within this first attempt")
    if qualified and qualification_only:
        raise ValueError("This source-bound route is already qualified; no extra probe")
    creds = load_openai_credentials(ROOT / ".env")
    rates = campaign["rates"]
    if (creds is None or creds.model_id != campaign["first_cell_model"]
            or creds.base_url.rstrip("/") != campaign["gateway"].rstrip("/") or creds.api != campaign["api"]
            or any(getattr(creds, key) != rates[key] for key in ("input_price", "output_price"))
            or creds.context_window_tokens is not None):
        raise ValueError("Exact Astra route/prices and administrator-table context configuration required")
    creds = replace(creds, stream=False, web_search=False, test_budget_usd=100., cached_input_price=None)
    limits = {story: deepcopy(FIRST_LIMITS) for story in profiles()}
    return await record_demo("complete", adapter.expected_source, model=creds.model_id,
        campaign_path=campaign["campaign_path"], profiles=profiles(), limits_by_story=limits,
        source_reader=adapter.source, credentials=creds, campaign_contract=campaign, campaign_guard=adapter.guard,
        services_check=adapter.check_services, evidence_reader=adapter.collect_evidence,
        pre_story=adapter.qualify if qualify and not qualified else None, qualification_only=qualification_only,
        simulation_config=adapter.config, scenario_setup=setup_scenario, scenario_drive=drive_scenario,
        scenario_audit=audit_scenario, attempt_preparation=adapter.prepare_attempt, resume=resume, retry_failed=retry_failed)


def delivery_registry(campaign):
    adapter = HallbarCampaignAdapter(campaign)
    adapter.check_ready()
    return TrialRegistry(campaign["campaign_path"], model=campaign["first_cell_model"], profiles=profiles(),
        limits_by_story={story: deepcopy(FIRST_LIMITS) for story in profiles()},
        campaign_contract=campaign, campaign_guard=adapter.guard)


def release_first(campaign):
    """The one existing release journal, with explicit unaccepted demo scope."""
    registry = delivery_registry(campaign)
    if campaign.get("execution_scope") != DEMO_SCOPE:
        return registry.release_first()
    # check_ready above verified owner relay + source-bound short smoke. Do not
    # falsify integrated_acceptance/protocol_ready to enter the normal registry.
    with registry.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if registry._delivery(db, campaign)["state"] != "gated" or db.execute("SELECT 1 FROM attempt").fetchone():
            raise ValueError("First-cell release already exists or campaign attempts need review")
        return registry._append_delivery(db, "first_cell_released", execution_scope=DEMO_SCOPE,
                                         scientific_acceptance_claimed=False)


def main(argv=None):
    """Offline by default; --live and exact accepted source are mandatory to send."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Create unreleased offline input snapshots")
    prepare.add_argument("directory", type=Path)
    prepare.add_argument("--target-bundle", type=Path, help="Operator-selected private target.json or its directory")
    prepare.add_argument("--demo-authorization", type=Path,
                         help="Explicit owner authorization for an unaccepted first Astra recording")
    inspect = commands.add_parser("preflight", help="Read source-bound readiness without model calls")
    inspect.add_argument("campaign", type=Path)
    release = commands.add_parser("release-first", help="Release only the accepted first Astra cell")
    release.add_argument("campaign", type=Path)
    release.add_argument("--frozen-source", required=True)
    run = commands.add_parser("run", help="Run only the accepted Astra first cell through the normal engine")
    run.add_argument("campaign", type=Path)
    run.add_argument("--live", action="store_true", required=True)
    run.add_argument("--frozen-source", required=True)
    continuation = run.add_mutually_exclusive_group()
    continuation.add_argument("--resume", action="store_true")
    continuation.add_argument("--retry-failed", action="store_true")
    run.add_argument("--qualification-only", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(prepare_campaign(args.directory, target_bundle=args.target_bundle,
                               demo_authorization=args.demo_authorization))
        return 0
    campaign = json.loads(args.campaign.read_text())
    ready = preflight(campaign)
    if args.command == "preflight":
        print(json.dumps(ready, indent=2))
        return 0 if ready["ready_for_qualification"] else 2
    if args.frozen_source != ready["execution_source_sha256"] or not ready["ready_for_qualification"]:
        raise ValueError("Exact accepted source/local command receipts required")
    if args.command == "release-first":
        print(json.dumps(release_first(campaign), indent=2))
        return 0
    result = asyncio.run(run_cell(campaign, live=args.live, frozen_source=args.frozen_source,
        resume=args.resume, retry_failed=args.retry_failed, qualification_only=args.qualification_only))
    print(json.dumps(result, indent=2))
    return 0 if result.get("machine_checks_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
