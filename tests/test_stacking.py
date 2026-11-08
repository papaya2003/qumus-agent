"""Stacking flow on the testbench (spec 4.1, 13; workstreams.md A1): pick up
flakes one by one with the stamp, drop the stack onto the target chip, each step
checked under the microscope; plus faults, interlocks and kill-and-resume."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest

from qumus2.core.errors import SafetyViolation
from qumus2.core.types import Pose
from qumus2.flows.library.stacking import DropDown
from tests.test_approach_alignment import source_case  # noqa: F401
from tests.test_focus_evidence import focus_context, native_reference  # noqa: F401
from tests.test_pickup_admission import observed  # noqa: F401
from tests.test_v1_transfer_sequence import motion_case  # noqa: F401
from tests.test_workflow_consistency import admitted  # noqa: F401

IDEAL_TEST_IDS = ["hbn-a", "gr-a", "hbn-b"]
IDEAL_SELECTION_RULE = "current-clear-native20-compatible-area-desc-id-asc-v1"
IDEAL_SOURCE_RECIPE = [
    ("exfoliate_chips", dict(material=material, num_chips=1, chip_ids=[chip], prepare_mother_tape="always",
        target_temp_c=50., dwell_s=30., num_massages=2, press_depth_mm=.2, peel_speed=200., peel_angle_deg=30.,
        carrier_id=carrier, carrier_slot=slot, home_slots=[home], store_slots=[stored]))
    for material, chip, carrier, slot, home, stored in (
        ("Gr", "chip_001", "carrier_gr_01", "left_slide_1", "left_chip_1", "mid_chip_1"),
        ("hBN", "chip_002", "carrier_hbn_01", "left_slide_2", "left_chip_2", "mid_chip_2"))
] + [
    ("search_chip", dict(chip_id=chip, material=material, min_area_um2=50., max_revisits=20,
                         revisit_quota=None, home_slot=home))
    for chip, material, home in (("chip_001", "Gr", "mid_chip_1"), ("chip_002", "hBN", "mid_chip_2"))
]


def _ideal_source_contract(flake_ids=()):
    if not flake_ids:
        return IDEAL_SOURCE_RECIPE
    assert len(flake_ids) == 3
    return IDEAL_SOURCE_RECIPE + [
        ("revisit_chip", dict(chip_id="chip_002", material="hBN", min_area_um2=50., max_revisits=2,
            revisit_quota=None, home_slot="mid_chip_2", flake_ids=[flake_ids[0], flake_ids[2]])),
        ("revisit_chip", dict(chip_id="chip_001", material="Gr", min_area_um2=50., max_revisits=1,
            revisit_quota=None, home_slot="mid_chip_1", flake_ids=[flake_ids[1]])),
    ]



def _assert_ideal_source_job(index, flow, params, flake_ids=()):
    assert (flow, params) == _ideal_source_contract(flake_ids)[index], "Ideal source recipe/defaults changed"


def _assert_ideal_revisit_reference(reference, evidence, job_id):
    assert reference["job_id"] == evidence["job_id"] == job_id, "Selected source lacks its named final revisit"


def _verify_ideal_checkpoint(folder, config):
    from qumus2.ui.recording import source_fingerprint

    manifest = json.loads((folder / "manifest.json").read_text())
    assert config.get("simulation_condition") == manifest.get("simulation_condition") == "ideal_stacking_v1", "Ideal checkpoint condition changed"
    assert manifest["application_sha256"] == source_fingerprint(), "PC checkpoint application changed"
    assert manifest["config"] == config, "PC checkpoint configuration changed"
    for name, key in (("checkpoint.db", "database_sha256"), ("checkpoint.pkl", "state_sha256")):
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == manifest[key], "PC checkpoint bytes changed"
    for path, digest in manifest["images"].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
    return manifest


def _ideal_without_optional_detector_ids(s, monkeypatch):
    """Observe actual detector calls without supplying classifications or pixels."""
    calls = []
    for name in ("find_flakes", "find_stamp_flakes"):
        real = getattr(s.lab.analyzer, name)
        def without_ids(frame, *args, _real=real, _name=name, **kwargs):
            detected = _real(frame, *args, **kwargs)
            active = [row["name"] for run in s.store.list_runs() for row in s.store.get_steps(run["id"])
                      if row["status"] == "in_progress"]
            assert len(active) <= 1
            for detection in detected:
                detection.meta = {k: v for k, v in detection.meta.items() if k not in {"sim_id", "sim_layers"}}
            calls.append({"method": _name, "phase": active[0] if active else None,
                "objective": frame.meta.get("objective"), "material": args[0] if args else kwargs.get("material"),
                "detections": len(detected), "remaining_ids": any(
                    "sim_id" in d.meta or "sim_layers" in d.meta for d in detected)})
            return detected
        monkeypatch.setattr(s.lab.analyzer, name, without_ids)
    s.detector_id_calls = calls
    return calls


def _assert_ideal_endpoint_detector_calls(calls, flake_ids):
    native = [row for row in calls if row["method"] == "find_flakes" and row["objective"] == 20]
    assert {"v1_pick_up_" + fid for fid in flake_ids} <= {row["phase"] for row in native}, "Pickup detector endpoint not reached"
    assert {"Gr", "hBN"} <= {row["material"] for row in native if row["phase"] == DropDown.name}, "Release detector endpoint not reached"
    assert not any(row["remaining_ids"] for row in calls)


def _assert_designed_overlap(placed, flake_ids, design, criterion):
    from qumus2.core.polygons import EPS, area_um2, convex_polygon, cross, intersection, transform

    target = criterion["target_local_xy_mm"]
    intended_shapes = {fid: transform(convex_polygon(design[fid]),
        rotation_deg=criterion["outline_rotation_deg"][fid]) for fid in flake_ids}
    expected = [transform(intended_shapes[fid], xy=target) for fid in flake_ids]
    actual = []
    for fid in flake_ids:
        body = placed[fid]
        assert math.dist((body["x_mm"], body["y_mm"]), target) <= criterion["allowance_um"]/1000
        shape = convex_polygon(body["outline_mm"])
        intended = intended_shapes[fid]
        assert len(shape) == len(intended) and any(
            all(math.dist(point, intended[(i+start) % len(intended)]) <= 1e-9
                for i, point in enumerate(shape)) for start in range(len(intended))
        ), "Complete outline/orientation changed; only cyclic vertex order and numerical error are allowed"
        assert area_um2(shape) == pytest.approx(area_um2(convex_polygon(design[fid])), rel=1e-8, abs=1e-6)
        assert body["area_um2"] == pytest.approx(area_um2(shape), rel=1e-8, abs=1e-6)
        actual.append(transform(shape, xy=(body["x_mm"], body["y_mm"])))
    expected_common, actual_common = expected[0], actual[0]
    for intended, actual_polygon in zip(expected[1:], actual[1:], strict=True):
        expected_common = intersection(expected_common, intended)
        actual_common = intersection(actual_common, actual_polygon)
    assert area_um2(expected_common) > 0, "The declared design needs three-layer overlap"
    assert area_um2(actual_common) > 0, "Material appearance without designed three-layer overlap is not success"
    assert all(cross(a, b, target) >= -EPS for a, b in zip(actual_common, actual_common[1:]+actual_common[:1], strict=True)), "The intended centre must lie in the common intersection"
    return {"expected_common_area_um2": area_um2(expected_common), "actual_common_area_um2": area_um2(actual_common),
            "expected_outlines_mm": expected, "actual_outlines_mm": actual, "criterion": criterion}


def _assert_terminal_stack_snapshot(terminal, target_id, physical_ids, stamp_id):
    """Post-run conservation oracle; no sensing, selection or control calls."""
    expected = list(physical_ids.values())
    assert len(expected) == len(set(expected)), "Selected physical identities must be distinct"
    targets = [obj for obj in terminal["objects"] if obj["id"] == target_id]
    assert len(targets) == 1
    assert [body["id"] for body in targets[0]["flakes"]] == expected, "Target must contain exactly the selected layers in order"
    for obj in terminal["objects"]:
        if obj["id"] != target_id:
            assert not any(body["id"] in expected for body in obj["flakes"]), "Selected material remains on another object"
    for stamp in terminal["stamps"].values():
        assert not any(entry["flake"]["id"] in expected for entry in stamp["stack"]), "Selected material remains on a stamp"
    assert not terminal["stamps"][stamp_id]["stack"], "Used stamp must be empty"
    assert not terminal["collisions"], "Physical collisions are not accepted"
    return {fid: body for fid, body in zip(physical_ids, targets[0]["flakes"], strict=True)}


def _assert_visible_design(before, after, design, criterion, rotation_deg):
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    from qumus2.core.polygons import convex_polygon, transform

    calibration = before["meta"]["calibration"]
    assert calibration == after["meta"]["calibration"]
    assert calibration["observation_condition"] == {"simulation_condition": "ideal_stacking_v1", "observation_noise_scale": 0}
    for frame in (before, after):
        assert hashlib.sha256(Path(frame["image"]).read_bytes()).hexdigest() == frame["sha256"]
    with Image.open(before["image"]) as old, Image.open(after["image"]) as new:
        assert list(old.size) == list(new.size) == calibration["image_size_px"]
        difference = ImageChops.difference(old.convert("RGB"), new.convert("RGB"))
    visible_mask = Image.new("L", difference.size)
    visible_mask.putdata([255 if max(pixel) else 0 for pixel in difference.get_flattened_data()])
    expected = Image.new("L", difference.size)
    draw = ImageDraw.Draw(expected)
    width, height = difference.size
    sx, sy = calibration["mm_per_pixel"]
    for fid, outline in design.items():
        points = transform(convex_polygon(outline),
            rotation_deg=rotation_deg+criterion["outline_rotation_deg"][fid])
        draw.polygon([(width/2+x/sx, height/2-y/sy) for x, y in points], fill=255)
    radius = math.ceil(criterion["allowance_um"]/1000/min(sx, sy))
    support = ImageFilter.MaxFilter(2*radius+1)
    assert visible_mask.getbbox(), "No visible target appearance"
    assert ImageChops.subtract(visible_mask, expected.filter(support)).getbbox() is None, "Visible appearance exceeds the predeclared placement bound"
    assert ImageChops.subtract(expected, visible_mask.filter(support)).getbbox() is None, "The visible intended stack footprint is missing"
    anchor = Image.new("L", difference.size)
    ImageDraw.Draw(anchor).ellipse((width/2-radius, height/2-radius, width/2+radius, height/2+radius), fill=255)
    assert ImageChops.multiply(visible_mask, anchor).getbbox(), "No visible stack in the intended centre ROI"
    return {"visible_mask_bbox_px": visible_mask.getbbox(), "allowed_radius_px": radius,
        "identity_scope": "Visible stack projection; buried selected identities/order are independent terminal state evidence"}


def run(s, flow, params, timeout=900):
    deadline = monotonic() + timeout
    rid = s.jobs.submit(flow, params)
    try:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Stacking harness timed out after {timeout} wall seconds: {rid}")
            status = s.jobs.wait(rid, min(15, remaining))
            # An admitted wait may return terminal just across the deadline.
            if status.done:
                return status
    except BaseException as error:
        _stop_and_settle_stack_jobs(s, [rid], reason="job_wait_error", original_error=error)
        raise


def _unsettled_stack_jobs(s):
    return sorted({r["id"] for r in s.store.list_runs() if r["status"] in {"queued", "running"}}
                  | {rid for rid, done in s.jobs._done.items() if not done.is_set()})


def _stop_and_settle_stack_jobs(s, job_ids, *, reason, original_error=None):
    """One ordinary Stop and bounded worker settlement; never a recovery run."""
    if hasattr(s, "_stack_wait_cleanup"):
        return s._stack_wait_cleanup
    started = monotonic()
    deadline = started + 30
    record = {"reason": reason, "job_ids": list(job_ids), "wall_limit_s": 30,
        "original_error": ({"type": type(original_error).__name__, "message": str(original_error)}
                           if original_error is not None else None),
        "stop_attempted": True, "stop_error": None, "wait_errors": [], "jobs": {},
        "physical_success_verified": False, "cold_status_verified": False}
    s._stack_wait_cleanup = record  # Cache before Stop, including when Stop itself fails.
    try:
        s.jobs.stop_all()
    except BaseException as error:
        record["stop_error"] = {"type": type(error).__name__, "message": str(error)}
    pending = list(job_ids)
    while pending:
        rid = pending[0]
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            status = s.jobs.wait(rid, min(1, remaining))
        except BaseException as error:
            record["wait_errors"].append({"run_id": rid, "type": type(error).__name__, "message": str(error)})
            break
        done = s.jobs._done.get(rid)
        worker_settled = done is not None and done.is_set()
        record["jobs"][rid] = {"last_status": status.to_dict(), "worker_settled": worker_settled}
        if worker_settled:
            pending.pop(0)
        else:
            pending.append(pending.pop(0))
    record["unsettled_workers"] = [rid for rid in job_ids
        if rid not in s.jobs._done or not s.jobs._done[rid].is_set()]
    record["settled"] = not record["unsettled_workers"] and all(
        record["jobs"].get(rid, {}).get("last_status", {}).get("done") is True for rid in job_ids)
    record["wall_elapsed_s"] = monotonic() - started
    return record


def _finish_stacking_diagnostics(s, folder, config):
    try:
        pending = _unsettled_stack_jobs(s)
        if pending:
            _stop_and_settle_stack_jobs(s, pending, reason="fixture_exit")
    finally:
        binding = _save_stacking_diagnostics(s, folder, config)
    return binding


def events(s, kind):
    return [e for e in s.store.events_after(0, limit=1_000_000) if e.kind == kind]


def prepare(s):
    """Exfoliate and search Gr and hBN chips; return stack params (hBN/Gr/hBN)."""
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from qumus2.testbench.render import MicroscopeRenderer, microscope_photometry_profile

    renderer = s.lab.devices.get("microscope").renderer
    if renderer is None or renderer.writer is None:
        s._optical_test_directory = TemporaryDirectory(prefix="qumus-stack-evidence-")
        s.lab.devices.get("microscope").renderer = MicroscopeRenderer(Path(s._optical_test_directory.name))
    for flow, p in [("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"]}),
                    ("exfoliate_chips", {"material": "hBN", "chip_ids": ["chip_002"]}),
                    ("search_chip", {"chip_id": "chip_001"}),
                    ("search_chip", {"chip_id": "chip_002"})]:
        assert run(s, flow, p).status == "succeeded"

    def best(chip):
        # Only sources the search's own neighbourhood assessment found clear:
        # stacking refuses the others before any pickup.
        fl = [f for f in s.store.list_samples(kind="flake", location=chip)
              if f["status"] == "catalogued" and (f["props"].get("pickup_assessment") or {}).get("clear") is True]
        return [f["id"] for f in sorted(fl, key=lambda f: -f["props"]["area_um2"])]

    h, g = best("chip_002"), best("chip_001")
    assert len(h) >= 2 and g, "Fixture needs actually catalogued hBN/Gr sources"
    pair = [h[0], g[0], h[1]]
    # Preserve native20 captures produced by the current search/revisit path.
    # No old image metadata is relabelled and no hidden shape is enlarged to pass.
    for fid in pair:
        sample = s.store.get_sample(fid)
        ref = sample["props"]["tracking_references"]["20x"]
        assert ref["image"] == sample["props"]["images"]["20x"]
        assert ref["job_id"] == sample["props"]["optical_geometry"]["job_id"]
        assert ref["meta"]["objective"] == 20
        assert Path(ref["image"]).is_file()
        assert s.store.get_run(ref["job_id"])["status"] == "succeeded"
        # a518 seed 2: the release digests the design sources by image content.
        geometry = sample["props"]["optical_geometry"]
        assert geometry["image_sha256"] == hashlib.sha256(Path(geometry["image"]).read_bytes()).hexdigest()
        assert ref["meta"]["calibration"]["photometry_profile"] == microscope_photometry_profile()
    return {"flake_ids": pair, "target_chip_id": "chip_e01"}


def _select_ideal_sources(s):
    """One prospective choice from current public observations, before stacking."""
    from qumus2.records.geometry import current_pickup_evidence, pickup_coverage_errors

    ranked = {}
    for chip, material in (("chip_002", "hBN"), ("chip_001", "Gr")):
        eligible = []
        for sample in s.store.list_samples(kind="flake", location=chip):
            props = sample["props"]
            ref = props.get("tracking_references", {}).get("20x", {})
            if (sample["status"] == "catalogued" and props.get("material") == material
                    and current_pickup_evidence(s.store, sample) is not None
                    and props["pickup_assessment"].get("clear") is True
                    and ref.get("meta", {}).get("objective") == 20
                    and Path(ref.get("image", "")).is_file()
                    and isinstance(props.get("area_um2"), (int, float))
                    and math.isfinite(props["area_um2"]) and props["area_um2"] > 0):
                eligible.append(sample)
        ranked[material] = sorted(eligible, key=lambda f: (-f["props"]["area_um2"], f["id"]))
    for top in ranked["hBN"]:
        for gr in ranked["Gr"]:
            for bottom in ranked["hBN"]:
                ids = [top["id"], gr["id"], bottom["id"]]
                if top["id"] != bottom["id"] and not pickup_coverage_errors(s.store, ids):
                    return ids, {"rule": IDEAL_SELECTION_RULE,
                        "ranked_eligible": {mat: [{"id": f["id"], "area_um2": f["props"]["area_um2"],
                            "reference_job_id": f["props"]["tracking_references"]["20x"]["job_id"]}
                            for f in values] for mat, values in ranked.items()}, "selected": ids}
    pytest.fail("Ideal source catalogue has no compatible clear hBN/Gr/hBN combination; no fallback")


def _save_stacking_diagnostics(s, folder, config):
    """Exit-only evidence; private evaluator data never reaches sensing/control."""
    from qumus2.ui.recording import source_fingerprint

    folder.mkdir(exist_ok=True)
    runs = s.store.list_runs()
    before = {"event_id": s.store.last_event_id(), "clock": s.clock.now()}
    evaluation = s.lab.world.evaluation_snapshot()
    after = {"event_id": s.store.last_event_id(), "clock": s.clock.now()}
    active = [r["id"] for r in runs if r["status"] in {"queued", "running"}]
    unsettled = [rid for rid, done in s.jobs._done.items() if not done.is_set()]
    consistent = not active and not unsettled and before == after
    state = ("unfinished_non_atomic" if not consistent else "no_jobs" if not runs else
             "succeeded" if all(r["status"] == "succeeded" for r in runs) else "unsuccessful_settled")
    binding = {"before": before, "after": after, "job_ids": [r["id"] for r in runs],
        "jobs": [{"id": r["id"], "flow": r["flow"], "status": r["status"]} for r in runs],
        "active_jobs": active, "unsettled_workers": unsettled,
        "state": state, "no_observed_race": consistent,
        "physical_success_verified": False, "cold_status_verified": False,
        "wait_cleanup": getattr(s, "_stack_wait_cleanup", None),
        "source_sha256": source_fingerprint(),
        "config": config, "actual_seed": s.lab.world.seed,
        "configuration_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "verification": False, "usage": "private evaluation only; never control input"}
    (folder / "private-evaluation.json").write_text(json.dumps({"binding": binding, "evaluation": evaluation}, indent=2) + "\n")
    (folder / "public-jobs.json").write_text(json.dumps([
        {"run": r, "steps": s.store.get_steps(r["id"])} for r in runs], indent=2) + "\n")
    with (folder / "public-events.jsonl").open("w") as stream:
        cursor = 0
        while cursor < after["event_id"]:
            batch = s.store.events_after(cursor, limit=500)
            if not batch:
                break
            for event in batch:
                if event.id > after["event_id"]:
                    break
                if event.kind not in {"device.command", "microscope.focus", "lab.frame"} and not event.kind.startswith(("job.", "step.", "stack.")):
                    continue
                body = event.payload
                if event.kind == "lab.frame":
                    body = {k: body[k] for k in ("device", "command", "timestamp", "arms", "stages", "manipulator", "microscope") if k in body}
                    preview = event.payload.get("microscope_preview")
                    if isinstance(preview, dict):
                        body["microscope_preview"] = {k: preview[k] for k in (
                            "image", "image_sha256", "objective", "focus_z", "focus_plane", "calibration", "pose_binding",
                            "stage_kinematics", "timestamp", "purpose", "verification", "source", "immutable", "phase",
                            "fov_mm", "simulation_condition", "observation_noise_scale") if k in preview}
                stream.write(json.dumps({"id": event.id, "ts": event.ts, "run_id": event.run_id,
                    "kind": event.kind, "payload": body}) + "\n")
            cursor = min(batch[-1].id, after["event_id"])
    (folder / "image-sha256.json").write_text(json.dumps({str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*.png"))}, indent=2) + "\n")
    if hasattr(s, "ideal_transfer_laws"):
        (folder / "effective-transfer-laws.json").write_text(json.dumps(s.ideal_transfer_laws, indent=2) + "\n")
    if hasattr(s, "ideal_overlap_observation"):
        (folder / "designed-overlap.json").write_text(json.dumps(s.ideal_overlap_observation, indent=2) + "\n")
    return binding


def _close_settled_stack_store(s):
    if (not any(r["status"] in {"queued", "running"} for r in s.store.list_runs())
            and not any(not done.is_set() for done in s.jobs._done.values())):
        s.store.close()


@pytest.fixture
def seed1_prepared(services, request, tmp_path):
    """Keep the original normal seed1 retained-material negative separate."""
    request.session._qumus_public_case = ("original-seed1", services)
    assert services.lab.world.model.get("simulation_condition") != "ideal_stacking_v1"
    try:
        yield services, prepare(services)
    finally:
        _finish_stacking_diagnostics(services, tmp_path, services.lab.world.model)


@pytest.fixture(scope="module")
def ideal_source_checkpoint(tmp_path_factory, request):
    """One fresh ideal source build; no noisy checkpoint or outcome-based reselection."""
    from qumus2.app import build_services
    from qumus2.config import ideal_stacking_profile, load_yaml
    from qumus2.flows.flow import get_flow
    from qumus2.records.geometry import (
        _pickup_outline,
        current_pickup_evidence,
        pickup_coverage_errors,
    )
    from qumus2.testbench.persist import save_lab_state
    from qumus2.ui.recording import source_fingerprint

    assert not os.environ.get("QUMUS2_CONFIG_DIR")
    assert not os.environ.get("QUMUS2_MICROSCOPE_REPLAY_DIR")
    folder = tmp_path_factory.mktemp("ideal-source-checkpoint")
    config = ideal_stacking_profile()
    config["seed"] = 8
    assert config["simulation_condition"] == "ideal_stacking_v1"
    s = build_services(folder / "source.db", seed=8, testbench_config=config, use_openai=False)
    request.session._qumus_public_case = ("ideal-source-build", s)
    request.addfinalizer(lambda: _close_settled_stack_store(s))
    try:
        recipe = dict(target_temp_c=50, dwell_s=30, num_massages=2, press_depth_mm=.2,
                      peel_speed=200, peel_angle_deg=30.)
        jobs = [("exfoliate_chips", dict(material=material, chip_ids=[chip], **recipe))
                for material, chip in (("Gr", "chip_001"), ("hBN", "chip_002"))]
        jobs += [("search_chip", dict(chip_id=chip, material=material, min_area_um2=50, max_revisits=20))
                 for chip, material in (("chip_001", "Gr"), ("chip_002", "hBN"))]
        deadline, completed, selected = time.monotonic() + 1800, [], []
        for index in range(6):
            if index == 4:
                selected, selection = _select_ideal_sources(s)
                selection["before_stack"] = True
                selection["after_source_job_ids"] = [job["id"] for job in completed]
                (folder / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
                jobs += [("revisit_chip", dict(chip_id="chip_002", material="hBN", max_revisits=2,
                            flake_ids=[selected[0], selected[2]])),
                         ("revisit_chip", dict(chip_id="chip_001", material="Gr", max_revisits=1,
                            flake_ids=[selected[1]]))]
            flow, params = jobs[index]
            assert time.monotonic() < deadline, "Ideal source construction exhausted its single-build budget"
            declared = get_flow(flow)
            declared._bench = s.lab.bench
            _assert_ideal_source_job(index, flow,
                declared.resolve_params(declared.Params.model_validate(params), s.store).model_dump(), selected)
            rid = s.jobs.submit(flow, params, project_id=s.project_id)
            _assert_ideal_source_job(index, flow, s.store.get_run(rid)["params"], selected)
            while time.monotonic() < deadline:
                st = s.jobs.wait(rid, min(15, max(.01, deadline - time.monotonic())))
                if st.status not in {"queued", "running"}:
                    break
            else:
                pytest.fail("Ideal source construction timed out; unfinished, no retry or fallback")
            assert st.status == "succeeded", {"run_id": rid, "flow": flow, "error": st.error, "steps": st.steps}
            completed.append({"id": rid, "flow": flow, "params": s.store.get_run(rid)["params"]})
        assert len(s.store.list_runs()) == 6
        assert not any(r["status"] in {"queued", "running"} for r in s.store.list_runs())
        assert not s.store.locks() and not s.store.reservations()
        images, outlines, calibrations = {}, {}, []
        for fid, material in zip(selected, ("hBN", "Gr", "hBN"), strict=True):
            sample = s.store.get_sample(fid)
            assert sample["status"] == "catalogued" and sample["props"]["material"] == material
            assert (evidence := current_pickup_evidence(s.store, sample)) is not None
            assert sample["props"]["pickup_assessment"]["clear"] is True
            ref = sample["props"]["tracking_references"]["20x"]
            expected_job = completed[5 if material == "Gr" else 4]["id"]
            _assert_ideal_revisit_reference(ref, evidence, expected_job)
            assert ref["meta"]["objective"] == 20 and ref["job_id"] == sample["props"]["optical_geometry"]["job_id"]
            condition = ref["meta"]["calibration"]["observation_condition"]
            assert condition == {"simulation_condition": config["simulation_condition"], "observation_noise_scale": 0}
            images[ref["image"]] = hashlib.sha256(Path(ref["image"]).read_bytes()).hexdigest()
            outlines[fid] = _pickup_outline(s.store, sample)
            calibrations.append(ref["meta"]["calibration"])
        assert not pickup_coverage_errors(s.store, selected)
        with sqlite3.connect(folder / "checkpoint.db") as connection:
            s.store._conn.backup(connection)
        save_lab_state(s.lab, s.clock, folder / "checkpoint.pkl")
        pixel_scales = [calibration["mm_per_pixel"] for calibration in calibrations]
        assert all(scale == pixel_scales[0] for scale in pixel_scales)
        centring_um = load_yaml("verification.yaml")["refind_flake"]["centre_tolerance_um"]
        assert centring_um == 6
        pixel_diagonal_um = math.hypot(*pixel_scales[0])*1000
        criterion = {"source_centring_um": centring_um, "native20_pixel_diagonal_um": pixel_diagonal_um,
            "allowance_um": centring_um+pixel_diagonal_um, "target_local_xy_mm": [0, 0],
            "design_shifts_mm": {}, "target_frame": "declared chip centre", "requires_containment": False,
            # Outlines are source-chip-axis observations. Freeze their public
            # source rotations in the declared nominal target axes before any
            # stacking, never fit orientation from the final physical shape.
            "outline_frame": "target_chip_axes",
            "target_declared_rotation_deg": 0.,
            "outline_rotation_deg": {fid: s.store.get_sample(
                s.store.get_sample(fid)["location"])["props"]["rotation_deg"] for fid in selected},
            "outline_orientation_source": "pre-run public source rotation minus declared nominal target rotation (0 deg)"}
        manifest = {"seed": 8, "simulation_condition": config["simulation_condition"], "config": config,
            "selection": selection, "design_outlines_mm": outlines, "placement_criterion": criterion, "jobs": completed, "images": images,
            "application_sha256": source_fingerprint(),
            "database_sha256": hashlib.sha256((folder / "checkpoint.db").read_bytes()).hexdigest(),
            "state_sha256": hashlib.sha256((folder / "checkpoint.pkl").read_bytes()).hexdigest(),
            "params": {"flake_ids": selected, "target_chip_id": "chip_e01"}}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        yield folder, config
    finally:
        _finish_stacking_diagnostics(s, folder, config)


@pytest.fixture
def ideal_prepared(ideal_source_checkpoint, tmp_path, request, monkeypatch):
    from copy import deepcopy

    from qumus2.app import build_lab_side

    folder, config = ideal_source_checkpoint
    manifest = _verify_ideal_checkpoint(folder, config)
    with sqlite3.connect((folder / "checkpoint.db").as_uri() + "?mode=ro&immutable=1", uri=True) as source:
        with sqlite3.connect(tmp_path / "case.db") as target:
            source.backup(target)
    (tmp_path / "case.pkl").write_bytes((folder / "checkpoint.pkl").read_bytes())
    side = build_lab_side(tmp_path / "case.db", seed=8, testbench_config=config,
                          state_path=tmp_path / "case.pkl", image_dir=tmp_path / "images")
    s = SimpleNamespace(**vars(side), project_id=side.store.list_projects()[0]["id"],
        ideal_config=config, ideal_design_outlines=manifest["design_outlines_mm"],
        ideal_placement_criterion=manifest["placement_criterion"], ideal_transfer_laws=[])
    request.session._qumus_public_case = ("ideal-isolated-stack", s)
    request.addfinalizer(lambda: _close_settled_stack_store(s))
    check_ids = request.node.name == "test_generic_multilayer_pc_stacking_regression"
    calls = _ideal_without_optional_detector_ids(s, monkeypatch) if check_ids else []
    ids = manifest["params"]["flake_ids"]
    oracle_ids = {fid: s.store.get_sample(fid)["props"].get("sim_id") for fid in ids} if check_ids else {}
    transfer_profile = s.lab.world.active_transfer_profile
    def observe_transfer_law(stamp_id):
        effective = transfer_profile(stamp_id)
        s.ideal_transfer_laws.append({"stamp_id": stamp_id, "profile": deepcopy(effective)})
        return effective
    monkeypatch.setattr(s.lab.world, "active_transfer_profile", observe_transfer_law)
    try:
        yield s, deepcopy(manifest["params"])
        if check_ids and any(r["flow"] == "stack_device" and r["status"] == "succeeded" for r in s.store.list_runs()):
            _assert_ideal_endpoint_detector_calls(calls, ids)
            assert {fid: s.store.get_sample(fid)["props"].get("sim_id") for fid in ids} == oracle_ids
    finally:
        try:
            _finish_stacking_diagnostics(s, tmp_path, config)
        finally:
            (tmp_path / "detector-id-calls.json").write_text(json.dumps(calls, indent=2) + "\n")


@pytest.fixture
def prepared(request):
    return request.getfixturevalue("ideal_prepared")


def test_ideal_source_checkpoint_qualifies(ideal_source_checkpoint):
    folder, config = ideal_source_checkpoint
    manifest = _verify_ideal_checkpoint(folder, config)
    assert len(manifest["jobs"]) == 6
    ids = manifest["params"]["flake_ids"]
    assert [(j["flow"], j["params"]) for j in manifest["jobs"]] == _ideal_source_contract(ids)
    assert manifest["selection"]["selected"] == ids and manifest["selection"]["before_stack"] is True
    assert manifest["selection"]["rule"] == IDEAL_SELECTION_RULE


def test_ideal_resolved_source_contract_rejects_changed_recipe_or_defaults():
    from copy import deepcopy

    from qumus2.flows.flow import get_flow

    for index, (flow, expected) in enumerate(_ideal_source_contract(IDEAL_TEST_IDS)):
        resolved = get_flow(flow).Params.model_validate(expected).model_dump()
        _assert_ideal_source_job(index, flow, resolved, IDEAL_TEST_IDS)
        for field in expected:
            changed = deepcopy(resolved)
            changed[field] = "changed"
            with pytest.raises(AssertionError, match="recipe/defaults"):
                _assert_ideal_source_job(index, flow, changed, IDEAL_TEST_IDS)
        with pytest.raises(AssertionError, match="recipe/defaults"):
            _assert_ideal_source_job(index, "other_source_flow", resolved, IDEAL_TEST_IDS)


@pytest.mark.parametrize("changed", ["reference", "assessment"])
def test_ideal_selected_source_requires_the_named_final_revisit(changed):
    reference, assessment = {"job_id": "last-hbn-revisit"}, {"job_id": "last-hbn-revisit"}
    _assert_ideal_revisit_reference(reference, assessment, "last-hbn-revisit")
    for wrong_job in ("old-successful-search", "other-successful-revisit"):
        target = reference if changed == "reference" else assessment
        target["job_id"] = wrong_job
        with pytest.raises(AssertionError, match="named final revisit"):
            _assert_ideal_revisit_reference(reference, assessment, "last-hbn-revisit")


@pytest.mark.parametrize("changed", ["database", "state", "application", "config", "condition"])
def test_ideal_clone_checks_manifest_bytes_and_application_before_restore(tmp_path, changed):
    from qumus2.ui.recording import source_fingerprint

    # Opaque unit bytes test the pre-restore guard; no database/state is loaded.
    (tmp_path / "checkpoint.db").write_bytes(b"component database bytes")
    (tmp_path / "checkpoint.pkl").write_bytes(b"component state bytes")
    config = {"component": "manifest-integrity", "simulation_condition": "ideal_stacking_v1"}
    manifest = {"application_sha256": source_fingerprint(), "config": config, "images": {},
        "simulation_condition": config["simulation_condition"],
        "database_sha256": hashlib.sha256((tmp_path / "checkpoint.db").read_bytes()).hexdigest(),
        "state_sha256": hashlib.sha256((tmp_path / "checkpoint.pkl").read_bytes()).hexdigest()}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    assert _verify_ideal_checkpoint(tmp_path, config) == manifest
    if changed in {"database", "state"}:
        (tmp_path / ("checkpoint.db" if changed == "database" else "checkpoint.pkl")).write_bytes(b"changed")
    elif changed == "application":
        manifest["application_sha256"] = "0" * 64
        path.write_text(json.dumps(manifest))
    elif changed == "condition":
        manifest["simulation_condition"] = "normal"
        path.write_text(json.dumps(manifest))
    else:
        config = {"component": "different"}
    with pytest.raises(AssertionError, match="checkpoint"):
        _verify_ideal_checkpoint(tmp_path, config)


@pytest.mark.parametrize("changed", ["pickup", "release", "remaining_ids"])
def test_ideal_detector_call_guard_requires_both_endpoint_paths(changed):
    calls = [{"method": "find_flakes", "objective": 20, "phase": "v1_pick_up_" + fid,
              "material": material, "remaining_ids": False}
             for fid, material in zip(IDEAL_TEST_IDS, ("hBN", "Gr", "hBN"), strict=True)]
    calls += [{"method": "find_flakes", "objective": 20, "phase": DropDown.name,
               "material": material, "remaining_ids": False} for material in ("Gr", "hBN")]
    _assert_ideal_endpoint_detector_calls(calls, IDEAL_TEST_IDS)
    if changed == "pickup":
        calls.pop(0)
    elif changed == "release":
        calls.pop()
    else:
        calls[0]["remaining_ids"] = True
    with pytest.raises(AssertionError):
        _assert_ideal_endpoint_detector_calls(calls, IDEAL_TEST_IDS)


def test_seed1_coverage_without_source_disappearance_holds(seed1_prepared):
    s, params = seed1_prepared
    assert params["flake_ids"] == ["chip_002_f002", "chip_001_f015", "chip_002_f011"]
    # The first pickup lifts off without its flake: the stamp takes the
    # fault-free path up to the moment of adhesion, then leaves it on the chip.
    faults = s.lab.world.faults
    faults.pickup_fail_next = 1
    st = run(s, "stack_device", params)
    assert faults.pickup_fail_next == 0, "the injected pickup failure must have fired"
    assert st.status == "parked"
    failed = [row for row in st.steps if row["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["name"] == "v1_pick_up_chip_002_f002"
    assert failed[0]["corrections"] == 0
    observation = next(out for out in st.result if out["step"] == failed[0]["name"])["observations"][-1]
    m = observation["measurements"]
    assert observation["verdict"] == "unclear" and m["coverage"]["covered"] is True
    assert m["source_detection_count"] == 1 and m["flake_on_chip"] is True
    assert m["process_confirmation"] is False and m["transfer_confirmed"] is False
    assert m["no_transfer_confirmed"] is None
    assert all(s.store.get_sample(fid)["status"] == "catalogued" for fid in params["flake_ids"])
    assert not events(s, "stack.pickup_endpoint") and not s.store.list_samples(kind="device")
    assert not any(row["name"] == DropDown.name for row in st.steps)
    assert s.gate.heater("stamp_heater").get_target() is None
    assert not events(s, "safety.violation")


def test_ideal_selection_is_prospective_public_quality_area_then_stable_id(services, tmp_path):
    from PIL import Image

    image = tmp_path / "observed.png"
    Image.new("RGB", (32, 32), (120, 100, 140)).save(image)
    for chip, material, ids, half in (
            ("chip_002", "hBN", ["hbn-b", "hbn-a", "unclear-large"], .02),
            ("chip_001", "Gr", ["gr-b", "gr-a"], .005)):
        rid = services.store.create_run("search_chip", {"chip_id": chip}, project_id=services.project_id)
        services.store.set_run_status(rid, "succeeded")
        for fid in ids:
            services.store.upsert_sample(fid, "flake", location=chip, status="catalogued", props={
                "material": material, "area_um2": 9000 if fid == "unclear-large" else (2*half)**2*1e6,
                "images": {"context": str(image)},
                "optical_geometry": {"outline_mm": [[-half, -half], [half, -half], [half, half], [-half, half]],
                    "units": "mm", "frame": "flake_center_chip_axes", "source": "simulated_optical_outline",
                    "source_chip_id": chip, "image": str(image), "job_id": rid},
                "pickup_assessment": {"flake_id": fid, "source_chip_id": chip, "images": [str(image)],
                    "job_id": rid, "clear": fid != "unclear-large"},
                "tracking_references": {"20x": {"image": str(image), "job_id": rid, "meta": {"objective": 20}}}})
    before = services.store.last_event_id()
    ids, selection = _select_ideal_sources(services)
    assert ids == ["hbn-a", "gr-a", "hbn-b"]
    assert selection["rule"] == IDEAL_SELECTION_RULE
    assert services.store.last_event_id() == before  # Choice reads evidence; it performs no trial pickup.
    for fid in ("gr-a", "gr-b"):
        sample = services.store.get_sample(fid)
        services.store.upsert_sample(fid, "flake", props={"pickup_assessment": {
            **sample["props"]["pickup_assessment"], "clear": False}})
    with pytest.raises(pytest.fail.Exception, match="no compatible clear"):
        _select_ideal_sources(services)


def test_ideal_mica_configuration_reaches_saved_admission_and_effective_law():
    from unittest.mock import patch

    from qumus2.app import build_services
    from qumus2.config import ideal_stacking_profile
    from qumus2.records.stamps import record_support
    from tests.test_common_pickup_contract import observed_pair

    config = ideal_stacking_profile()
    config["seed"] = 8
    s = build_services(":memory:", seed=8, testbench_config=config, use_openai=False)
    try:
        s.store.upsert_sample("chip_001", "chip", location="mid_chip_1")
        observed_pair(s)
        s.store.upsert_sample("lower", "flake", props={"rel_corner_mm": [.1, .1]})
        instruction = s.store.add_instruction("stamp_01 carries mica.", project_id=s.project_id)
        record_support(s.store, s.lab.bench, "stamp_01", "mica", instruction_id=instruction, project_id=s.project_id)
        with patch.object(s.jobs, "enqueue") as enqueue:  # Admission only; no physical flow is dispatched.
            rid = s.jobs.submit("stack_device", {"flake_ids": ["lower"], "target_chip_id": "chip_e01"})
            enqueue.assert_called_once_with(rid)
        expected = config["reactor"]["material_metal"]["mica"]
        saved = s.store.get_run(rid)["params"]
        assert saved["support_assumptions"] == expected == s.lab.bench["reactor_module"]["material_metal"]["mica"]
        assert s.lab.world.active_transfer_profile(saved["stamp_id"]) == {"mica": expected}
        assert (expected["pickup_c"], expected["release_c"], expected["support_retained_on_stamp"]) == (80, 150, True)
        assert expected["pickup_probability"] == expected["release_probability"] == 1
        assert not events(s, "device.command") and s.jobs._q.empty()
    finally:
        s.store.close()


@pytest.fixture
def stack_wait_case(monkeypatch):
    """Wall-clock/job API mock only; no laboratory job is dispatched."""
    from threading import Event
    from unittest.mock import Mock

    from qumus2.runtime.jobs import JobStatus

    case = SimpleNamespace(now=0., raw_status="running", trace=[])
    monkeypatch.setattr(f"{__name__}.monotonic", lambda: case.now)
    done = Event()
    def status(name, *, settled=False):
        case.raw_status = name
        if settled:
            done.set()
        return JobStatus("same-job", "mock-stack", name, name not in {"queued", "running"})
    def pending(rid, timeout):
        assert rid == "same-job" and timeout > 0
        case.trace.append("wait")
        case.now += timeout
        return status(case.raw_status)
    jobs = SimpleNamespace(submit=Mock(return_value="same-job"), wait=Mock(side_effect=pending),
        stop_all=Mock(side_effect=lambda: case.trace.append("stop")), _done={"same-job": done})
    store = SimpleNamespace(list_runs=lambda: [{"id": "same-job", "status": case.raw_status}], close=Mock())
    case.s = SimpleNamespace(jobs=jobs, store=store)
    case.pending, case.status = pending, status
    return case


def test_stacking_wait_polls_same_job_beyond_600_seconds(stack_wait_case):
    case = stack_wait_case
    terminal = case.status("succeeded")
    case.raw_status = "running"
    def complete(rid, timeout):
        pending = case.pending(rid, timeout)
        if case.now >= 650:
            case.s.jobs._done[rid].set()
            return terminal
        return pending
    case.s.jobs.wait.side_effect = complete
    assert run(case.s, "mock-stack", {"unchanged": True}) is terminal
    case.s.jobs.submit.assert_called_once_with("mock-stack", {"unchanged": True})
    assert 600 < case.now < 900
    assert all(call.args[0] == "same-job" and 0 < call.args[1] <= 15 for call in case.s.jobs.wait.call_args_list)
    case.s.jobs.stop_all.assert_not_called()


@pytest.mark.parametrize("terminal_status", ["failed", "waiting_human", "parked", "paused", "cancelled"])
def test_stacking_wait_returns_unsuccessful_terminal_without_recovery(stack_wait_case, terminal_status):
    case = stack_wait_case
    terminal = case.status(terminal_status, settled=True)
    case.s.jobs.wait.return_value, case.s.jobs.wait.side_effect = terminal, None
    assert run(case.s, "mock-stack", {}) is terminal
    case.s.jobs.submit.assert_called_once_with("mock-stack", {})
    case.s.jobs.wait.assert_called_once_with("same-job", 15)
    case.s.jobs.stop_all.assert_not_called()


def test_stacking_wait_accepts_terminal_from_wait_admitted_before_deadline(stack_wait_case):
    case = stack_wait_case
    terminal = case.status("succeeded", settled=True)
    def finish_late(rid, timeout):
        case.pending(rid, timeout)
        case.now += .01
        return terminal
    case.s.jobs.wait.side_effect = finish_late
    assert run(case.s, "mock-stack", {}, timeout=2) is terminal
    assert case.now == 2.01
    case.s.jobs.wait.assert_called_once_with("same-job", 2)
    case.s.jobs.stop_all.assert_not_called()


@pytest.mark.parametrize("cleanup", ["settles", "active", "stop_error"])
@pytest.mark.parametrize("timeout", [None, 2, 17.25])
def test_stacking_timeout_preserves_error_and_stops_once_with_bounded_settlement(stack_wait_case, monkeypatch, tmp_path, cleanup, timeout):
    case = stack_wait_case
    limit = 900 if timeout is None else timeout
    def wait(rid, timeout):
        pending = case.pending(rid, timeout)
        if cleanup == "settles" and case.s.jobs.stop_all.called:
            return case.status("paused", settled=True)
        return pending
    case.s.jobs.wait.side_effect = wait
    if cleanup == "stop_error":
        case.s.jobs.stop_all.side_effect = RuntimeError("Stop unavailable")
    with pytest.raises(TimeoutError, match=f"after {limit} wall seconds: same-job"):
        run(case.s, "mock-stack", {}, **({} if timeout is None else {"timeout": timeout}))
    record = case.s._stack_wait_cleanup
    assert record["original_error"]["type"] == "TimeoutError"
    assert record["jobs"]["same-job"]["last_status"]["status"] == ("paused" if cleanup == "settles" else "running")
    assert record["settled"] is (cleanup == "settles")
    assert record["unsettled_workers"] == ([] if cleanup == "settles" else ["same-job"])
    assert record["wall_elapsed_s"] == (1 if cleanup == "settles" else 30)
    assert record["stop_error"] == ({"type": "RuntimeError", "message": "Stop unavailable"} if cleanup == "stop_error" else None)
    assert record["physical_success_verified"] is record["cold_status_verified"] is False
    wait_count = math.ceil(limit/15)
    before_stop = case.s.jobs.wait.call_args_list[:wait_count]
    assert all(call.args[0] == "same-job" and 0 < call.args[1] <= 15 for call in before_stop)
    assert sum(call.args[1] for call in before_stop) == limit
    assert before_stop[-1].args[1] == min(15, limit-15*(wait_count-1))
    assert all(call.args[0] == "same-job" and 0 < call.args[1] <= 1 for call in case.s.jobs.wait.call_args_list[wait_count:])
    waits = case.s.jobs.wait.call_count
    monkeypatch.setattr(f"{__name__}._save_stacking_diagnostics", lambda *args: record)
    assert _finish_stacking_diagnostics(case.s, tmp_path, {}) is record
    assert case.s.jobs.wait.call_count == waits  # Fixture exit adds no second cleanup budget.
    case.s.jobs.stop_all.assert_called_once_with()
    case.s.jobs.submit.assert_called_once_with("mock-stack", {})


def test_stacking_wait_cleanup_errors_preserve_original_wait_exception(stack_wait_case):
    case = stack_wait_case
    original = ValueError("original wait failure")
    case.s.jobs.wait.side_effect = [original, RuntimeError("cleanup wait failure")]
    case.s.jobs.stop_all.side_effect = RuntimeError("cleanup Stop failure")
    with pytest.raises(ValueError) as caught:
        run(case.s, "mock-stack", {}, timeout=2)
    assert caught.value is original
    record = case.s._stack_wait_cleanup
    assert record["original_error"] == {"type": "ValueError", "message": "original wait failure"}
    assert record["stop_error"]["message"] == "cleanup Stop failure"
    assert record["wait_errors"] == [{"run_id": "same-job", "type": "RuntimeError", "message": "cleanup wait failure"}]
    assert record["settled"] is False and record["unsettled_workers"] == ["same-job"]
    case.s.jobs.stop_all.assert_called_once_with()
    case.s.jobs.submit.assert_called_once_with("mock-stack", {})


@pytest.mark.parametrize("settles", [True, False])
def test_stacking_fixture_exit_stops_before_capture_and_only_closes_settled_store(stack_wait_case, monkeypatch, tmp_path, settles):
    case = stack_wait_case
    # A terminal API status alone does not establish worker settlement.
    case.raw_status = "waiting_human"
    def wait(rid, timeout):
        pending = case.pending(rid, timeout)
        return case.status("paused", settled=True) if settles else pending
    case.s.jobs.wait.side_effect = wait
    def save(*args):
        case.trace.append("capture")
        return case.s._stack_wait_cleanup
    monkeypatch.setattr(f"{__name__}._save_stacking_diagnostics", save)
    record = _finish_stacking_diagnostics(case.s, tmp_path, {})
    assert case.trace[0] == "stop" and case.trace[-1] == "capture"
    assert record["reason"] == "fixture_exit" and record["settled"] is settles
    _close_settled_stack_store(case.s)
    assert case.s.store.close.call_count == int(settles)
    case.s.jobs.stop_all.assert_called_once_with()
    case.s.jobs.submit.assert_not_called()


@pytest.mark.parametrize("status", ["queued", "running", "paused", "parked", "waiting_human", "failed", "cancelled", "succeeded", None])
def test_stacking_exit_diagnostics_preserve_raw_af_and_mark_unfinished(services, tmp_path, status):
    from copy import deepcopy

    rid = services.store.create_run("diagnostic_component", {}, project_id=services.project_id) if status else None
    if status:
        services.store.set_run_status(rid, status)
    payload = {"objective": 20, "sampled_passes": [{"samples": [{"focus_mm": .2, "score": 7.}]}],
               "final_focus_readback_z_mm": .2, "source": "component_capture"}
    services.store.emit("microscope.focus", deepcopy(payload), run_id=rid)
    services.store.emit("lab.frame", {"arms": {"transfer_arm": {"x": 1, "y": 2, "z": 3}},
        "objects": [{"private": "not a public encoder field"}]}, run_id=rid)
    before = (services.lab.world.evaluation_snapshot(), services.lab.world.rng.getstate(),
              services.lab.world._observation_rng.getstate(), services.clock.now())
    services._stack_wait_cleanup = {"original_error": {"type": "TimeoutError", "message": "mock timeout"},
        "stop_error": {"type": "RuntimeError", "message": "mock Stop error"},
        "jobs": {rid: {"last_status": {"status": status}}} if rid else {}, "settled": False}
    binding = _save_stacking_diagnostics(services, tmp_path, services.lab.world.model)
    expected_state = ("unfinished_non_atomic" if status in {"queued", "running"} else
                      "no_jobs" if status is None else "succeeded" if status == "succeeded" else "unsuccessful_settled")
    assert binding["state"] == expected_state
    assert binding["active_jobs"] == ([rid] if status in {"queued", "running"} else [])
    assert binding["physical_success_verified"] is binding["cold_status_verified"] is False
    assert [job["status"] for job in binding["jobs"]] == ([status] if status else [])
    private = json.loads((tmp_path / "private-evaluation.json").read_text())
    assert private["binding"]["wait_cleanup"] == services._stack_wait_cleanup
    assert private["evaluation"]["source"] == "synthetic_ground_truth"
    rows = [json.loads(line) for line in (tmp_path / "public-events.jsonl").read_text().splitlines()]
    assert next(row["payload"] for row in rows if row["kind"] == "microscope.focus") == payload
    encoder = next(row["payload"] for row in rows if row["kind"] == "lab.frame")
    assert encoder == {"arms": {"transfer_arm": {"x": 1, "y": 2, "z": 3}}}
    assert before == (services.lab.world.evaluation_snapshot(), services.lab.world.rng.getstate(),
        services.lab.world._observation_rng.getstate(), services.clock.now())


def test_ideal_source_setup_failure_keeps_sidecars_and_store_until_teardown(tmp_path, monkeypatch):
    from qumus2.runtime.jobs import ThreadJobService

    folder = tmp_path / "source"
    folder.mkdir()
    cleanup, submitted = [], []
    request = SimpleNamespace(session=SimpleNamespace(), addfinalizer=cleanup.append)
    factory = SimpleNamespace(mktemp=lambda name: folder)
    def refuse_before_dispatch(self, flow, params, **kwargs):
        submitted.append(flow)
        raise RuntimeError("component refusal before source dispatch")
    monkeypatch.setattr(ThreadJobService, "submit", refuse_before_dispatch)
    builder = ideal_source_checkpoint.__wrapped__(factory, request)
    try:
        with pytest.raises(RuntimeError, match="before source dispatch"):
            next(builder)
        assert submitted == ["exfoliate_chips"]
        private = json.loads((folder / "private-evaluation.json").read_text())
        assert private["binding"]["state"] == "no_jobs"
        assert private["binding"]["actual_seed"] == private["binding"]["config"]["seed"] == 8
        assert private["evaluation"]["source"] == "synthetic_ground_truth"
        assert (folder / "public-events.jsonl").is_file()
        # A setup-failure report hook can still read the service before pytest teardown.
        assert request.session._qumus_public_case[1].store.list_runs() == []
    finally:
        for finish in reversed(cleanup):
            finish()


@pytest.mark.parametrize("changed", ["position", "intersection", "centre", "area", "shear", "rotation"])
def test_ideal_state_overlap_rejects_loose_target_only_or_lost_shape(changed):
    from copy import deepcopy

    from qumus2.core.polygons import area_um2, rectangle, transform

    design = {"a": rectangle(.04, .03), "b": rectangle(.006, .006), "c": rectangle(.006, .006)}
    bodies = {fid: {"id": fid, "outline_mm": shape, "area_um2": area_um2(shape), "x_mm": 0, "y_mm": 0}
              for fid, shape in design.items()}
    criterion = {"allowance_um": 6+math.sqrt(2), "target_local_xy_mm": [0, 0],
                 "outline_rotation_deg": dict.fromkeys(design, 0.)}
    assert _assert_designed_overlap(bodies, list(design), design, criterion)["actual_common_area_um2"] > 0
    bad = deepcopy(bodies)
    if changed == "position":
        bad["b"]["x_mm"] = .012  # Still within the retained 25 um target oracle.
    elif changed == "intersection":
        bad["b"]["x_mm"], bad["c"]["x_mm"] = -.006, .006
    elif changed == "centre":
        bad["b"]["x_mm"] = bad["c"]["x_mm"] = .006
    elif changed == "area":
        bad["b"]["area_um2"] *= .5
    elif changed == "shear":
        bad["b"]["outline_mm"] = [(x+.5*y, y) for x, y in design["b"]]
        assert area_um2(bad["b"]["outline_mm"]) == pytest.approx(bodies["b"]["area_um2"])
    else:
        bad["b"]["outline_mm"] = transform(design["b"], rotation_deg=30)
        assert area_um2(bad["b"]["outline_mm"]) == pytest.approx(bodies["b"]["area_um2"])
    with pytest.raises(AssertionError):
        _assert_designed_overlap(bad, list(design), design, criterion)


def test_ideal_outline_accepts_only_the_prospectively_declared_frame_and_cyclic_order():
    from qumus2.core.polygons import area_um2, rectangle

    design = {"a": rectangle(.04, .03), "b": rectangle(.008, .004), "c": rectangle(.006, .006)}
    bodies = {fid: {"id": fid, "outline_mm": shape, "area_um2": area_um2(shape), "x_mm": 0, "y_mm": 0}
              for fid, shape in design.items()}
    # Independently specified R90 rectangle, starting at a different vertex.
    bodies["b"]["outline_mm"] = ((-.002, .004), (-.002, -.004), (.002, -.004), (.002, .004))
    criterion = {"allowance_um": 6+math.sqrt(2), "target_local_xy_mm": [0, 0],
                 "outline_rotation_deg": {"a": 0, "b": 90, "c": 0}}
    assert _assert_designed_overlap(bodies, list(design), design, criterion)["actual_common_area_um2"] > 0
    criterion["outline_rotation_deg"]["b"] = 0
    with pytest.raises(AssertionError, match="outline/orientation"):
        _assert_designed_overlap(bodies, list(design), design, criterion)


@pytest.mark.parametrize("changed", ["extra_target", "duplicate_target", "other_object", "used_stamp",
                                    "other_stamp", "collision", "order", "missing"])
def test_terminal_stack_oracle_rejects_extras_duplicates_retention_and_collisions(changed):
    from copy import deepcopy

    ids = {"top": "a", "Gr": "b", "bottom": "c"}
    target = [{"id": value} for value in ids.values()]
    terminal = {"objects": [{"id": "target", "flakes": target}, {"id": "source", "flakes": []}],
        "stamps": {"used": {"stack": []}, "other": {"stack": []}}, "collisions": []}
    assert list(_assert_terminal_stack_snapshot(terminal, "target", ids, "used")) == list(ids)
    bad = deepcopy(terminal)
    if changed == "extra_target":
        bad["objects"][0]["flakes"].append({"id": "extra"})
    elif changed == "duplicate_target":
        bad["objects"][0]["flakes"].append({"id": "a"})
    elif changed == "other_object":
        bad["objects"][1]["flakes"].append({"id": "b"})
    elif changed == "used_stamp":
        bad["stamps"]["used"]["stack"].append({"flake": {"id": "extra"}})
    elif changed == "other_stamp":
        bad["stamps"]["other"]["stack"].append({"flake": {"id": "b"}})
    elif changed == "collision":
        bad["collisions"].append({"type": "component collision"})
    elif changed == "order":
        bad["objects"][0]["flakes"].reverse()
    else:
        bad["objects"][0]["flakes"].pop()
    with pytest.raises(AssertionError):
        _assert_terminal_stack_snapshot(bad, "target", ids, "used")


@pytest.mark.parametrize("changed", ["shifted", "only_small_layer", "unrelated_appearance"])
def test_ideal_image_check_rejects_missing_or_displaced_visible_stack(tmp_path, changed):
    from PIL import Image, ImageDraw

    from qumus2.core.polygons import rectangle

    design = {"top": rectangle(.04, .03), "Gr": rectangle(.01, .01), "bottom": rectangle(.02, .016)}
    calibration = {"image_size_px": [128, 128], "mm_per_pixel": [.001, .001],
        "observation_condition": {"simulation_condition": "ideal_stacking_v1", "observation_noise_scale": 0}}
    criterion = {"allowance_um": 6+math.sqrt(2), "outline_rotation_deg": dict.fromkeys(design, 0.)}
    def frame(name, *, shift=0, small=False, stray=False, empty=False):
        image = Image.new("RGB", (128, 128), (90, 90, 90))
        draw = ImageDraw.Draw(image)
        if not empty:
            outline = design["Gr"] if small else design["top"]
            draw.polygon([(64+x/.001+shift, 64-y/.001) for x, y in outline], fill=(80, 130, 170))
        if stray:
            draw.point((2, 2), fill=(255, 255, 255))
        path = tmp_path / name
        image.save(path)
        return {"image": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "meta": {"calibration": calibration}}
    before = frame("before.png", empty=True)
    _assert_visible_design(before, frame("good.png"), design, criterion, 0)
    after = frame("bad.png", shift=12 if changed == "shifted" else 0,
        small=changed == "only_small_layer", stray=changed == "unrelated_appearance")
    with pytest.raises(AssertionError):
        _assert_visible_design(before, after, design, criterion, 0)


def test_generic_multilayer_pc_stacking_regression(prepared):
    """Generic hBN/Gr/hBN PC regression; not direct Gr/electrode acceptance."""
    s, params = prepared
    from qumus2.flows.library.stacking import StackDeviceFlow, StackParams

    assert all(p == 1 for p in s.ideal_config["stamp"]["pickup_first"].values())
    assert s.ideal_config["stamp"]["pickup_onto_stack"] == 1
    flow = StackDeviceFlow()
    flow._bench = s.lab.bench
    resolved = flow.resolve_params(StackParams.model_validate(params), s.store).model_dump(mode="json")
    assert resolved["stamp_film"] == "PC"
    st = run(s, "stack_device", params)
    saved = s.store.get_run(st.run_id)["params"]
    assert saved == resolved
    assert s.ideal_transfer_laws and all(row["profile"] is None for row in s.ideal_transfer_laws)
    assert st.status == "succeeded", [x for x in st.steps if x["status"] != "done"][:2]
    assert not events(s, "safety.violation")

    (dev,) = s.store.list_samples(kind="device")
    assert dev["location"] == "chip_e01" and dev["status"] == "stacked"
    assert dev["props"]["layers_top_first"] == params["flake_ids"]
    assert dev["props"]["materials"] == ["hBN", "Gr", "hBN"]
    for fid in params["flake_ids"]:
        f = s.store.get_sample(fid)
        assert f["status"] == "stacked" and f["location"] == "chip_e01"

    # the layers really are on the target chip, close to the target point
    chip = s.lab.world.objects["chip_e01"]
    sim_ids = {s.store.get_sample(f)["props"]["sim_id"] for f in params["flake_ids"]}
    placed = [f for f in chip.flakes if f.id in sim_ids]
    assert len(placed) == 3
    assert all(math.hypot(f.x_mm, f.y_mm) < 0.025 for f in placed)
    terminal = s.lab.world.evaluation_snapshot()  # Pure evaluator, only after the flow is terminal.
    physical_ids = {fid: s.store.get_sample(fid)["props"]["sim_id"] for fid in params["flake_ids"]}
    ordered = _assert_terminal_stack_snapshot(terminal, params["target_chip_id"], physical_ids, saved["stamp_id"])
    s.ideal_overlap_observation = _assert_designed_overlap(ordered, params["flake_ids"],
        s.ideal_design_outlines, s.ideal_placement_criterion)
    public = s.store.get_run(st.run_id)["meta"]["normal_transfer"]
    endpoint = next(row for row in st.steps if row["name"] == DropDown.name)["measurements"]
    assert endpoint["materials_appeared"] == {"Gr": True, "hBN": True}
    s.ideal_overlap_observation["image"] = _assert_visible_design(
        public["target_reference"]["focus_reference"], endpoint["target_after"]["focus_reference"],
        s.ideal_design_outlines, s.ideal_placement_criterion,
        s.store.get_sample(params["target_chip_id"])["props"]["rotation_deg"])


    stamp = s.store.get_sample("stamp_01")
    assert stamp["status"] == "used" and stamp["location"] == "right_stamp_1"
    assert s.lab.world.stamps["stamp_01"].film is False
    assert s.lab.world.stamp_snapshot()["held"] is None
    assert s.lab.devices.get("stamp_heater").get_target() is None
    assert s.lab.world.objects["chip_e01"].on is None  # back in the rack


def test_failed_pickup_holds_without_an_automatic_repress(prepared):
    s, params = prepared
    s.lab.faults.inject("pickup_fail_next", {"count": 1})
    st = run(s, "stack_device", params)
    assert st.status in ("parked", "waiting_human")
    first = next(x for x in st.steps if x["name"].startswith("v1_pick_up_"))
    assert first["status"] == "failed" and first["corrections"] == 0
    observations = next(result for result in st.result if result["step"] == first["name"])["observations"]
    assert observations and not any(obs["measurements"].get("transfer_confirmed") for obs in observations)
    assert s.store.get_sample(params["flake_ids"][0])["status"] == "catalogued"
    assert not events(s, "stack.pickup_endpoint")


def test_pickup_motion_refuses_a_floor_crossing_before_dispatch(motion_case):  # noqa: F811
    from qumus2.core.errors import NeedsHumanError
    from qumus2.flows.library.stacking import _begin_phase, _guarded_z
    case = motion_case
    phase = _begin_phase(case.ctx, "pickup_floor_boundary")
    with pytest.raises(NeedsHumanError, match="floor/envelope"):
        _guarded_z(case.ctx, phase, case.ctx.gate.manipulator_z_floor("transfer_arm") - .01)
    assert not case.moves
    assert phase["travel_mm"] == 0



def test_a_heater_going_offline_between_steps_stops_the_stack_safely(prepared, monkeypatch):
    """The heater goes offline after the first source initialization finishes;
    the next between-step watchdog must fail with its typed error."""
    s, params = prepared
    faults, heater = s.lab.faults, s.lab.devices.get("stamp_heater")
    first_init = f"v1_initialize_{params['flake_ids'][0]}"
    finish, fired = s.store.finish_step, []

    def finish_then_inject(rid, seq, status, **kwargs):
        finish(rid, seq, status, **kwargs)
        if status != "done" or s.store.get_run(rid)["flow"] != "stack_device":
            return
        boundary = [event for event in events(s, "step.finished")
            if event.run_id == rid and event.payload["seq"] == seq
            and event.payload["name"] == first_init and event.payload["status"] == "done"]
        if boundary:
            assert len(boundary) == 1 and not fired
            assert heater.offline is False
            faults.inject("device_offline", {"device": "stamp_heater", "offline": True})
            assert heater.offline is True
            fired.append(boundary[0])

    # Inject after the real finished event, before the next watchdog's online
    # check. A device before_call hook runs after that check and is too late.
    monkeypatch.setattr(s.store, "finish_step", finish_then_inject)
    st = run(s, "stack_device", params)
    assert len(fired) == 1 and fired[0].run_id == st.run_id
    assert fired[0].payload["name"] == first_init and fired[0].payload["status"] == "done"
    assert not any(event.run_id == st.run_id and event.id > fired[0].id for event in events(s, "step.started"))
    assert st.status == "failed"
    assert st.error["type"] == "DeviceUnavailable" and "stamp_heater" in st.error["message"]
    last = [e for e in events(s, "job.status") if e.run_id == st.run_id][-1]
    assert last.id > fired[0].id
    assert last.payload["message"].startswith("watchdog:")
    danger = [e for e in events(s, "notification") if e.run_id == st.run_id and e.payload.get("level") == "danger"]
    assert danger and "stamp_heater" in danger[0].payload["text"]
    assert not s.lab.world.collisions


@pytest.mark.parametrize("device", ["transfer_arm", "microscope", "stamp_heater", "transfer_stage"])
def test_device_offline_at_a_named_step_returns_a_typed_failure(request, monkeypatch, device):
    import qumus2.flows.library.stacking as module
    from qumus2.flows.step import StepRunner
    if device == module.STAGE:
        s, ctx, _, _ = request.getfixturevalue("source_case")
        step = module.RefindFlake("source", "chip_001", "Gr")
    else:
        s, ctx, _, _, _ = request.getfixturevalue("native_reference")
        step = (module.HeatTransferStage(90) if device == module.HEATER else
                module.InitializeStamp("stamp", "v1_initialize_offline_boundary"))
    faults, fired = s.lab.faults, []
    original = faults.before_call
    def at_named_step(did):
        original(did)
        current = s.store.get_steps(ctx.run_id)
        if not fired and did == device and any(row["name"] == step.name and row["status"] == "in_progress" for row in current):
            fired.append((step.name, did))
            s.lab.devices.get(device).offline = True
    if device == module.STAGE:
        act = step.act
        def offline_before_stage_action(context, correction):
            assert any(row["name"] == step.name and row["status"] == "in_progress"
                       for row in s.store.get_steps(ctx.run_id))
            if not fired:
                fired.append((step.name, device))
                s.lab.devices.get(device).offline = True
            return act(context, correction)
        monkeypatch.setattr(step, "act", offline_before_stage_action)
    else:
        faults.before_call = at_named_step
    outcome = StepRunner().run(step, ctx, 0)
    assert fired == [(step.name, device)]
    assert not outcome.ok and outcome.error["type"] == "DeviceUnavailable"
    row = next(row for row in s.store.get_steps(ctx.run_id) if row["name"] == step.name)
    assert row["status"] == "failed" and device in row["note"]
    assert not events(s, "stack.pickup_endpoint")
    if device != module.HEATER:
        assert ctx.gate.heater(module.HEATER).get_target() is None
    assert not events(s, "safety.violation")



def test_a_frozen_heater_reading_stops_the_stack(prepared):
    s, params = prepared
    s.lab.faults.inject("sensor_stuck", {"device": "stamp_heater", "value": 24.0})
    st = run(s, "stack_device", params)
    assert st.status == "failed"  # dangerous: everything to its safe state
    bad = next(x for x in st.steps if x["status"] == "failed")
    assert "stuck at 24.0" in bad["note"] and "heater off" in bad["note"]
    assert s.lab.devices.get("stamp_heater").get_target() is None


def test_failed_release_holds_without_an_automatic_thermal_retry(prepared):
    s, params = prepared
    s.lab.faults.inject("release_fail_next", {"count": 1})
    st = run(s, "stack_device", params)
    assert st.status in ("parked", "waiting_human")
    drop = next(step for step in st.steps if step["name"] == DropDown.name)
    assert drop["status"] == "failed" and drop["corrections"] == 0
    assert not s.store.list_samples(kind="device")


@pytest.fixture
def handoff_reconciliation_case(tmp_path, request):
    """Public Store/Observation mocks only; no lab, job or device is created."""
    from PIL import Image

    import qumus2.flows.library.stacking as module
    from qumus2.config import load_yaml
    from qumus2.core.clock import SimClock
    from qumus2.flows.step import Observation, Verdict
    from qumus2.runtime.store import RunStore

    def make(*, readable_id="selected", lineage=False):
        clock = SimClock()
        store = RunStore(":memory:", clock=clock)
        request.addfinalizer(store.close)
        params = {"stamp_id": "selected", "procedure_contract": module.PROCEDURE_ID,
            "transfer_profile": module.TRANSFER_PROFILE_ID, "release_policy": module.RELEASE_POLICY_ID}
        rid = store.create_run("handoff_mock_component", params)
        image, load_image = tmp_path / "current.png", tmp_path / "loaded.png"
        for path in (image, load_image):
            Image.new("RGB", (2, 2), "white").save(path)
        store.upsert_sample("selected", "stamp", location=module.MANIP, status="in_use")
        store.upsert_sample("unrelated", "stamp", location="right_stamp_2", status="fresh")
        if lineage:
            expected, measured = {"sample": "selected", "at": module.HANDOFF}, {"found_at": module.HANDOFF}
            store.begin_step(rid, 0, "load_stamp", expected=expected)
            store.emit("step.verified", {"name": "load_stamp", "seq": 0, "verdict": "ok",
                "expected": expected, "measurements": measured, "images": [str(load_image)]}, run_id=rid)
            store.upsert_sample("selected", "stamp", location=module.HANDOFF)
            store.finish_step(rid, 0, "done", measurements=measured)
            store.begin_step(rid, 1, "grip_stamp")
            store.emit("device.command", {"device": module.MANIP_VACUUM, "command": "set", "params": {"on": True}}, run_id=rid)
            store.emit("step.verified", {"name": "grip_stamp", "seq": 1, "verdict": "not_verifiable"}, run_id=rid)
            store.upsert_sample("selected", "stamp", location=module.MANIP)
            store.finish_step(rid, 1, "done")
        store.begin_step(rid, 2, "v1_initialize_unit")
        mark = store.emit("device.command", {"device": module.CAMERA, "command": "capture", "params": {}}, run_id=rid)
        capture = {"run_id": rid, "camera": module.CAMERA, "frame_device_id": module.CAMERA,
            "region": module.HANDOFF, "frame_of": module.ARM, "capture_event_id": mark,
            "capture_started_at": clock.now(), "capture_returned_at": clock.now(), "frame_timestamp": clock.now(),
            "image": str(image), "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "detections": [{"label": "stamp", "pose": {"x": 98., "y": 214., "z": 0., "rotation": 0.},
                "confidence": .9, **({"object_id": readable_id} if readable_id is not None else {})}]}
        obs = Observation(Verdict.CORRECTABLE, {"stamp_at_handoff": 1, "handoff_observation": {
            "stamp_id": "selected", "run_id": rid, "observed_at": clock.now(), "captures": [capture]}},
            images=[str(image)], message="the holder's vacuum did not hold")
        ctx = SimpleNamespace(run_id=rid, params=params, clock=clock, store=store,
            bench=load_yaml("bench.yaml"), verification=load_yaml("verification.yaml"))
        return SimpleNamespace(ctx=ctx, obs=obs, capture=capture, image=image, load_image=load_image,
            step=module.ManipulatorTo("over_stage", name="v1_initialize_unit_engage", stamp_id="selected"))
    return make


@pytest.mark.parametrize("lineage", [False, True])
def test_handoff_reconciliation_qualifies_public_identity_and_only_selected_stamp(handoff_reconciliation_case, lineage):
    case = handoff_reconciliation_case(readable_id=None if lineage else "selected", lineage=lineage)
    before = case.ctx.store.get_sample("unrelated")
    case.step.on_give_up(case.ctx, case.obs)
    selected = case.ctx.store.get_sample("selected")
    assert selected["location"] == "stamp_handoff" and selected["props"]["location_uncertain"] is False
    assert selected["props"]["last_seen"]["identity_basis"] == ("same_run_exclusive_load" if lineage else "readable_object_id")
    assert case.ctx.store.get_sample("unrelated") == before


@pytest.mark.parametrize("bad", ["wrong_id", "missing_id", "multiple", "other_kind", "conflicting_handoff",
    "conflicting_holder", "stale", "unknown_time", "no_image", "changed_image", "old_capture", "wrong_run",
    "missing_load_image", "broken_continuity", "empty", "missing_evidence"])
def test_handoff_reconciliation_keeps_uncertainty_for_unqualified_evidence(handoff_reconciliation_case, bad):
    lineage = bad in {"conflicting_holder", "missing_load_image", "broken_continuity"}
    case = handoff_reconciliation_case(readable_id=None if lineage or bad == "missing_id" else "selected", lineage=lineage)
    if bad == "wrong_id":
        case.capture["detections"][0]["object_id"] = "somebody_else"
    elif bad == "multiple":
        case.capture["detections"] *= 2
    elif bad == "other_kind":
        case.capture["detections"][0]["label"] = "chip"
    elif bad in {"conflicting_handoff", "conflicting_holder"}:
        case.ctx.store.upsert_sample("unrelated", "stamp", location="stamp_handoff" if bad == "conflicting_handoff" else "transfer_arm")
    elif bad == "stale":
        case.capture["frame_timestamp"] -= 1
    elif bad == "unknown_time":
        case.capture["frame_timestamp"] = None
    elif bad == "no_image":
        case.capture["image"] = None
    elif bad == "changed_image":
        case.image.write_bytes(b"changed")
    elif bad == "old_capture":
        case.ctx.store.begin_step(case.ctx.run_id, 3, "v1_initialize_later")
    elif bad == "wrong_run":
        case.capture["run_id"] = "another_run"
    elif bad == "missing_load_image":
        case.load_image.unlink()
    elif bad == "broken_continuity":
        case.ctx.store.upsert_sample("selected", "stamp", location="right_stamp_1")
        case.ctx.store.upsert_sample("selected", "stamp", location="transfer_arm")
    elif bad == "empty":
        case.capture["detections"] = []
        case.obs.measurements["stamp_at_handoff"] = 0
    elif bad == "missing_evidence":
        case.obs.measurements.pop("handoff_observation")
    unrelated = case.ctx.store.get_sample("unrelated")
    case.step.on_give_up(case.ctx, case.obs)
    selected = case.ctx.store.get_sample("selected")
    assert selected["location"] == "transfer_arm" and selected["props"]["location_uncertain"] is True
    assert selected["props"]["last_seen"]["at"] is None
    assert case.ctx.store.get_sample("unrelated") == unrelated


def test_handoff_capture_receipt_whitelists_actual_fields_without_extra_captures(handoff_reconciliation_case):
    from qumus2.devices.base import Frame
    from qumus2.flows.library.primitives import look
    from qumus2.perception.base import Detection
    case = handoff_reconciliation_case()
    calls = []
    detection = Detection(label="stamp", pose=Pose(x=98, y=214), confidence=.9,
        object_id="selected", props={"sim_id": "forbidden"})
    def capture():
        calls.append("capture")
        case.ctx.store.emit("device.command", {"device": "overview_camera", "command": "capture"}, run_id=case.ctx.run_id)
        return Frame("overview_camera", None, path=str(case.image), meta={"sim_objects": "forbidden"})
    case.ctx.gate = SimpleNamespace(camera=lambda _: SimpleNamespace(capture=capture))
    case.ctx.locator = SimpleNamespace(locate=lambda *args, **kwargs: [detection])
    assert look(case.ctx, "overview_camera", "stamp_handoff", "arm_right") == [detection]
    receipts = []
    assert look(case.ctx, "overview_camera", "stamp_handoff", "arm_right", captures=receipts) == [detection]
    assert calls == ["capture", "capture"] and len(receipts) == 1
    assert receipts[0]["frame_timestamp"] is None  # No invented exposure timestamp.
    assert receipts[0]["detections"] == [{"label": "stamp", "pose": detection.pose.model_dump(),
        "confidence": .9, "object_id": "selected"}]
    assert "forbidden" not in json.dumps(receipts)


@pytest.mark.parametrize("route", ["rejected", "axis_error", "success"])
def test_initialization_preserves_observation_and_existing_command_boundary(handoff_reconciliation_case, monkeypatch, route):
    from unittest.mock import Mock

    import qumus2.flows.library.stacking as module
    from qumus2.core.errors import NeedsHumanError
    from qumus2.flows.step import Observation, Verdict
    case = handoff_reconciliation_case()
    commands = []
    scope = SimpleNamespace(get_focus=lambda: 0., get_objective=lambda: 5,
        set_objective=lambda value: commands.append(("objective", value)),
        set_focus=lambda value: commands.append(("focus", value)))
    scope.get_focus = lambda: (commands[-1][1] if commands and commands[-1][0] == "focus" else 0.)
    case.ctx.gate = SimpleNamespace(microscope=lambda _: scope,
        manipulator=lambda _: SimpleNamespace(get_positions=lambda: {
            **case.ctx.bench["transfer_arm"]["over_stage"], "z": case.ctx.bench["transfer_arm"]["safe_z"]}),
        limits=SimpleNamespace(microscopes={"microscope": SimpleNamespace(focus=(-10., 10.))}))
    def act(*args, **kwargs):
        commands.append(("engage",))
        if route == "axis_error":
            raise NeedsHumanError("axis readback unavailable")
    monkeypatch.setattr(module.ManipulatorTo, "act", act)
    verify = Mock(side_effect=lambda *args: commands.append(("capture",)) or
                  (case.obs if route == "rejected" else Observation(Verdict.OK)))
    monkeypatch.setattr(module.ManipulatorTo, "verify", verify)
    handler = Mock(wraps=case.step.on_give_up)
    monkeypatch.setattr(module.ManipulatorTo, "on_give_up", lambda self, ctx, obs: handler(ctx, obs))
    init = module.InitializeStamp("selected", "v1_initialize_unit")
    if route == "success":
        init.act(case.ctx, None)
        assert commands == [("objective", 5), ("engage",), ("capture",),
            ("focus", case.ctx.verification["transfer_sequence"]["focus_offset_mm"])]
        handler.assert_not_called()
    else:
        with pytest.raises(NeedsHumanError) as failure:
            init.act(case.ctx, None)
        if route == "rejected":
            handler.assert_called_once_with(case.ctx, case.obs)
            assert failure.value.details["observation"] == case.obs.to_dict()
            assert case.ctx.store.get_sample("selected")["location"] == "stamp_handoff"
        else:
            assert failure.value.message == "axis readback unavailable" and not failure.value.details
            verify.assert_not_called()
            handler.assert_not_called()
            assert not case.ctx.store.get_sample("selected")["props"]


def test_initialize_stamp_vacuum_loss_keeps_public_handoff_ledger(tmp_path, monkeypatch):
    """Public camera/ledger component; no source build or stack job submission."""
    import qumus2.flows.library.stacking as module
    from qumus2.app import build_services
    from qumus2.config import ideal_stacking_profile
    from qumus2.flows.step import StepRunner

    config = ideal_stacking_profile()
    config["seed"] = 8
    s = build_services(tmp_path / "component.db", seed=8, testbench_config=config,
                       image_dir=tmp_path / "images", use_openai=False)
    def inventory():
        return sorted((row for row in s.store.list_samples() if row["kind"] != "stamp"), key=lambda row: row["id"])
    record = {"scope": "public camera/ledger component only; no physical stacking acceptance",
        "config": config, "seed": 8, "source_inventory_before": inventory(),
        "stamps_before": s.store.list_samples(kind="stamp"), "setup": [], "inner_observations": [],
        "failure": None, "cleanup_error": None, "boundary": "fresh_setup"}
    try:
        selected = module.fresh_stamps(s.store, s.lab.bench)[0]
        sid, slot = selected["id"], selected["location"]
        record["selection"] = {"stamp_id": sid, "source_slot": slot, "rule": "normal fresh_stamps first choice"}
        assert sum(row["location"] == slot for row in s.store.list_samples()) == 1
        params = {"procedure_contract": module.PROCEDURE_ID, "transfer_profile": module.TRANSFER_PROFILE_ID,
            "release_policy": module.RELEASE_POLICY_ID, "stamp_id": sid, "stamp_slot": slot, "stamp_film": "PC"}
        rid = s.store.create_run("stamp_vacuum_ledger_component", params, project_id=s.project_id)
        ctx = s.jobs.context_factory(rid, params, s.project_id)
        record["run_id"] = rid
        before_images = []
        before = module.look(ctx, module.CAMERA, module.HANDOFF, module.ARM, images=before_images)
        record["handoff_before"] = {"detections": [item.model_dump() for item in before], "images": before_images}
        assert not before, "The public handoff must be empty before the named load"
        prefix = [module.HeatTransferStage(module.StackParams.model_fields["pickup_temp_c"].default),
            module.ManipulatorTo("load"),
            module.MoveObject(sid, "stamp", arm=module.ARM, camera=module.CAMERA,
                dest=module.HANDOFF, cfg_key="load_stamp", name="load_stamp")]
        runner = StepRunner()
        record["boundary"] = "normal_loading_prefix"
        for seq, step in enumerate(prefix):
            result = runner.run(step, ctx, seq)
            record["setup"].append(result.to_dict())
            assert result.ok and result.corrections == 0, "Stop if the real loading prefix fails"
        loaded = record["setup"][-1]["observations"][-1]
        assert loaded["verdict"] == "ok"
        assert loaded["expected"]["sample"] == sid and loaded["expected"]["at"] == module.HANDOFF
        assert loaded["measurements"]["found_at"] == module.HANDOFF and loaded["images"]
        assert s.store.get_sample(sid)["location"] == module.HANDOFF
        source_images = []
        source = module.look(ctx, module.CAMERA, slot, module.ARM, label="stamp", images=source_images)
        record["source_slot_after_load"] = {"detections": [item.model_dump() for item in source], "images": source_images}
        assert not source, "The named source slot must be publicly empty after loading"
        assert all(s.store.get_sample(row["id"]) == row for row in record["stamps_before"] if row["id"] != sid)
        fault = {"device": module.MANIP_VACUUM, "value": False}
        record["fault"] = {"name": "vacuum_stuck", "params": fault, "after_event_id": s.store.last_event_id()}
        s.lab.faults.inject("vacuum_stuck", fault)
        grip = runner.run(module.GripStamp(sid), ctx, len(prefix))
        record["setup"].append(grip.to_dict())
        assert grip.status == "unverified" and grip.corrections == 0
        assert s.store.get_sample(sid)["location"] == module.MANIP
        name = "v1_initialize_vacuum_ledger_component"
        real_verify = module.ManipulatorTo.verify
        def retain_actual_observation(step, context):
            observation = real_verify(step, context)
            if context.run_id == rid and step.name == name+"_engage":
                record["inner_observations"].append(observation.to_dict())
            return observation
        monkeypatch.setattr(module.ManipulatorTo, "verify", retain_actual_observation)
        record["boundary"] = "initialization"
        outcome = runner.run(module.InitializeStamp(sid, name), ctx, len(prefix)+1)
        record["outcome"] = outcome.to_dict()
        assert outcome.status == "needs_agent" and outcome.corrections == 0
        assert outcome.error["type"] == "NeedsHumanError" and outcome.error["category"] == "needs_human"
        assert "vacuum did not hold" in outcome.message
        failed = s.store.get_steps(rid)[-1]
        assert failed["name"] == name and failed["status"] == "failed" and failed["corrections"] == 0
        assert len(record["inner_observations"]) == 1
        observed = record["inner_observations"][0]
        assert observed["verdict"] == "correctable" and observed["measurements"]["stamp_at_handoff"] == 1
        assert "vacuum did not hold" in observed["message"] and observed["images"]
        assert all(Path(image).is_file() for image in observed["images"])
        commands = [event for event in events(s, "device.command") if event.run_id == rid]
        assert sum(event.payload["device"] == module.MANIP_VACUUM
                   and event.payload["params"].get("on") is True for event in commands) == 1
        assert not events(s, "stack.pickup_endpoint") and not s.store.list_samples(kind="device")
        assert inventory() == record["source_inventory_before"]
        assert not events(s, "step.corrected") and not events(s, "safety.violation")
        record["boundary"] = "public_identity_verified_ledger_assertion"
        assert s.store.get_sample(sid)["location"] == module.HANDOFF
    except BaseException as error:
        record["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        try:
            s.jobs.stop_all()
        except Exception as error:
            record["cleanup_error"] = {"type": type(error).__name__, "message": str(error)}
        record["source_inventory_after"] = inventory()
        record["stamps_after"] = s.store.list_samples(kind="stamp")
        record["job_service_submissions"] = list(s.jobs._done)
        record["images_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((tmp_path / "images").rglob("*.png"))}
        (tmp_path / "public-component-receipt.json").write_text(json.dumps(record, indent=2) + "\n")
        (tmp_path / "public-jobs.json").write_text(json.dumps([
            {"run": row, "steps": s.store.get_steps(row["id"])} for row in s.store.list_runs()], indent=2) + "\n")
        with (tmp_path / "public-events.jsonl").open("w") as stream:
            for event in s.store.events_after(0, limit=1_000_000):
                payload = event.payload
                if event.kind == "lab.frame":
                    payload = {key: payload[key] for key in ("device", "command", "timestamp", "arms", "stages",
                        "manipulator", "microscope") if key in payload}
                stream.write(json.dumps({"id": event.id, "ts": event.ts, "kind": event.kind,
                    "run_id": event.run_id, "payload": payload}) + "\n")
        s.store.close()


def test_stamp_vacuum_failure_parks_before_the_stamp_touches_anything(prepared):
    """The holder waits at the hand-off (park = load), so a grip that did not
    hold shows on its first trip over the stage: the stamp stays behind, the
    V1 initialization holds without retrying an unverified engagement."""
    s, params = prepared
    s.lab.faults.inject("vacuum_stuck", {"device": "transfer_arm_vacuum", "value": False})
    st = run(s, "stack_device", params)
    assert st.status == "waiting_human"
    bad = next(x for x in st.steps if x["status"] != "done")
    fid = params["flake_ids"][0]
    assert bad["name"] == f"v1_initialize_{fid}" and "vacuum" in bad["note"], bad
    assert bad["status"] == "failed"
    assert bad["corrections"] == 0
    outcome = next(result for result in st.result if result["step"] == bad["name"])
    error = outcome["error"]
    assert error["type"] == "NeedsHumanError" and error["category"] == "needs_human"
    handoff_observation = error["details"]["observation"]
    assert handoff_observation["message"] == error["message"] == outcome["message"] == bad["note"]
    assert "vacuum did not hold" in handoff_observation["message"] and handoff_observation["verdict"] == "correctable"
    assert handoff_observation["measurements"]["stamp_at_handoff"] == 1
    selected = s.store.get_run(st.run_id)["params"]["stamp_id"]
    assert selected == "stamp_01"
    handoff = handoff_observation["measurements"]["handoff_observation"]
    assert handoff["stamp_id"] == selected and handoff["run_id"] == st.run_id
    assert len(handoff["captures"]) == 1
    capture = handoff["captures"][0]
    assert capture["run_id"] == st.run_id and capture["camera"] == capture["frame_device_id"] == "overview_camera"
    assert capture["region"] == "stamp_handoff" and capture["frame_of"] == "arm_right"
    assert len(capture["detections"]) == 1 and capture["detections"][0]["label"] == "stamp"
    assert capture["detections"][0].get("object_id") is None
    image = Path(capture["image"])
    assert str(image) in handoff_observation["images"] and image.is_file()
    assert hashlib.sha256(image.read_bytes()).hexdigest() == capture["image_sha256"]
    command = next(event for event in events(s, "device.command") if event.id == capture["capture_event_id"])
    assert command.run_id == st.run_id and command.payload["device"] == "overview_camera" and command.payload["command"] == "capture"
    seen = s.store.get_sample(selected)["props"]["last_seen"]
    assert seen["at"] == "stamp_handoff" and seen["identity_basis"] == "same_run_exclusive_load"
    assert seen["observation"] == handoff_observation and s.store.get_sample(selected)["props"]["location_uncertain"] is False
    assert s.store.get_sample(fid)["status"] == "catalogued"
    # the ledger follows the stamp: it lies at the hand-off, not on the holder
    assert s.store.get_sample("stamp_01")["location"] == "stamp_handoff"
    assert s.lab.world.held_stamp() is None


def test_bad_params_are_refused_before_enqueue(observed):  # noqa: F811
    from unittest.mock import patch
    s = observed
    params = {"flake_ids": ["lower"], "target_chip_id": "chip_e01"}
    before = (len(s.store.list_runs()), s.store.reservations(), s.store.last_event_id())
    with patch.object(s.jobs, "enqueue") as enqueue:
        for changed, message in [({"flake_ids": ["nope"]}, "not a known flake"),
                                 ({"target_chip_id": "chip_999"}, "not a known chip"),
                                 ({"target_chip_id": "chip_001"}, "carries one of the flakes"),
                                 ({"drop_temp_c": 170, "melt_temp_c": 165}, "bad params")]:
            with pytest.raises(ValueError, match=message):
                s.jobs.submit("stack_device", {**params, **changed})
        enqueue.assert_not_called()
    assert len(s.store.list_runs()) == before[0] and s.store.reservations() == before[1]
    assert not [e for e in s.store.events_after(before[2]) if e.kind == "device.command"]



def test_stamp_interlocks(services):
    gate = services.gate
    m = gate.manipulator("transfer_arm")
    m.move_axis_to("x", 40)
    m.move_axis_to("y", 90)
    m.move_axis_to("z", 5.2)  # stamp low over the stage
    with pytest.raises(SafetyViolation):
        gate.stage("transfer_stage").move_to(Pose(x=10, y=10, z=0))
    with pytest.raises(SafetyViolation):
        gate.arm("arm_right").move_to(Pose(x=180, y=300, z=20))
    m.move_axis_to("z", 6.5)
    gate.stage("transfer_stage").move_to(Pose(x=10, y=10, z=0))  # stamp up: allowed
    with pytest.raises(SafetyViolation):  # but the arm still may not go under it
        gate.arm("arm_right").move_to(Pose(x=180, y=300, z=20))
    with pytest.raises(SafetyViolation):
        m.move_axis_to("z", 3.0)  # below the manipulator's range
    assert len(events(services, "safety.violation")) == 4


def test_default_stage_refuses_nonzero_mechanical_z_before_dispatch(services):
    """Normal focusing is independent; the configured mechanical stage Z stays zero."""
    gate, world = services.gate, services.lab.world
    stage = gate.stage("transfer_stage")
    before = stage.get_pose()
    commands = len(events(services, "device.command"))
    assert gate.limits.stages["transfer_stage"].z == (0., 0.)
    with pytest.raises(SafetyViolation, match=r"z=1\.5 outside \(0\.0, 0\.0\)"):
        stage.move_to(Pose(x=0, y=80, z=1.5))
    assert stage.get_pose() == before
    assert len(events(services, "device.command")) == commands
    assert not world.collisions


def test_stage_height_counts_towards_the_stamp_gap(services, monkeypatch):
    """Isolated raised-readback guard math, without changing limits or moving a stage."""
    gate = services.gate
    readings = {"x": 40., "y": 90., "z": 7.5}
    raised = Pose(x=0, y=80, z=1.2)
    monkeypatch.setattr(services.lab.devices.get("transfer_arm"), "get_positions", lambda: dict(readings))
    monkeypatch.setattr(services.lab.devices.get("transfer_stage"), "get_pose", lambda: raised)
    commands = len(events(services, "device.command"))
    assert gate.limits.stages["transfer_stage"].z == (0., 0.)
    # Prospective higher stage leaves 6.0mm: reject. At 1.2mm it leaves 6.3mm.
    with pytest.raises(SafetyViolation, match="stamp is low"):
        gate.check_stamp_interlocks("transfer_stage", "readback_unit_probe", stage_z=1.5)
    gate.check_stamp_interlocks("transfer_stage", "readback_unit_probe", stage_z=1.2)
    assert gate.stage_lift_under("transfer_arm") == pytest.approx(1.2)
    floor = gate.limits.manipulators["transfer_arm"].z[0]+1.2
    assert gate.manipulator_z_floor("transfer_arm") == pytest.approx(floor)
    with pytest.raises(SafetyViolation, match="raised by 1.200"):
        gate.manipulator("transfer_arm").move_axis_to("z", floor-.1)
    readings["z"] = 6.0
    with pytest.raises(SafetyViolation, match="stamp is low"):
        gate.check_stamp_interlocks("transfer_stage", "readback_unit_probe", stage_z=1.2)
    # The interlock permits an escape that only increases clearance.
    gate.check_stamp_interlocks("transfer_stage", "readback_unit_probe", lowers_only=True, stage_z=1.2)
    assert len(events(services, "device.command")) == commands
    assert not services.lab.world.collisions


def test_focus_axis_moves_independently_while_stage_and_holder_are_held(focus_context):  # noqa: F811
    """Guarded focus motion advances the Clock without moving either sample axis."""
    s, ctx, _ = focus_context
    scope = ctx.gate.microscope("microscope")
    stage = ctx.gate.stage("transfer_stage")
    holder = ctx.gate.manipulator("transfer_arm")
    held_stage, held_holder = stage.kinematics(), holder.get_positions()
    origin = scope.get_focus()
    mark = s.store.last_event_id()
    targets = [origin + .1, origin - .1, origin]
    for target in targets:
        bound = scope.focus_transition_duration_bound(scope.get_focus(), target)
        assert bound is not None and bound > 0
        started = ctx.clock.now()
        scope.set_focus(target)
        settled = ctx.clock.now()
        assert 0 < settled - started <= bound + 2 * math.ulp(settled)
        assert scope.get_focus() == target
        frame = scope.capture()
        assert frame.meta["focus_z"] == target
        assert settled <= frame.timestamp <= ctx.clock.now()
        assert frame.meta["stage_kinematics"] == held_stage
        assert stage.kinematics() == held_stage
        assert holder.get_positions() == held_holder
    commands = [e for e in s.store.events_after(mark, limit=10000) if e.kind == "device.command"]
    assert all(e.payload["device"] == "microscope" for e in commands)
    assert [e.payload["params"]["z"] for e in commands if e.payload["command"] == "set_focus"] == targets
    assert len([e for e in commands if e.payload["command"] == "capture"]) == len(targets)
    assert not events(s, "safety.violation")


def test_raising_the_stage_into_the_stamp_crushes_it(services):
    """Below the safety layer (a sim device driven directly): the world logs the
    collision itself, whatever the software noticed."""
    devs, w = services.lab.devices, services.lab.world
    with w.lock:  # a stamp on the holder over the stage
        st = next(o for o in w.objects.values() if o.kind == "stamp")
        st.held_by, st.on = "transfer_arm", None
    m, stage = devs.get("transfer_arm"), devs.get("transfer_stage")
    m.move_axis_to("x", 40)
    m.move_axis_to("y", 90)
    m.move_axis_to("z", 5.4)
    # no chip under the dome: the bare stage top (a chip thickness, 0.5 mm, below
    # a chip surface) meets it at holder z ~5.0 - 0.5 + 1.5: pressed ~0.6 mm
    stage.move_to(Pose(x=0, y=0, z=1.5))
    assert w.collisions and w.collisions[0]["what"] == "stamp"
    assert w.collisions[0]["by"] == "transfer_stage" and not w.stamps[st.id].film


def test_stage_and_holder_moves_take_time(services):
    clock, gate = services.clock, services.gate
    t0 = clock.now()
    gate.stage("transfer_stage").move_to(Pose(x=0, y=80, z=0))
    t1 = clock.now()
    gate.manipulator("transfer_arm").move_axis_to("x", 40)
    t2 = clock.now()
    assert t1 - t0 >= 7.9  # 80 mm at 10 mm/s (testbench.yaml motion)
    assert t2 - t1 >= 1.37  # 110 mm rail travel at 80 mm/s (testbench.yaml, #181; from x 150)



def test_holder_rails_use_guarded_clear_entry_and_exit(native_reference):  # noqa: F811
    import qumus2.flows.library.stacking as module
    s, ctx, _, _, _ = native_reference
    mark = s.store.last_event_id()
    for destination in ("over_stage", "park"):
        step = module.ManipulatorTo(destination, stamp_id="stamp")
        step.act(ctx, None)
        assert step.verify(ctx).verdict == module.Verdict.OK
    commands = [e.payload for e in s.store.events_after(mark, limit=10000)
                if e.kind == "device.command" and e.payload["device"] == module.MANIP
                and e.payload["command"] == "move_axis_to" and e.payload["params"]["axis"] in ("x", "y")]
    assert [(p["params"]["axis"], p["params"]["value"]) for p in commands] == [
        ("y", ctx.bench[module.MANIP]["over_stage"]["y"]),
        ("x", ctx.bench[module.MANIP]["over_stage"]["x"]),
        ("x", ctx.bench[module.MANIP]["park"]["x"]),
        ("y", ctx.bench[module.MANIP]["park"]["y"])]
    assert all(p["params"]["from_pose"]["z"] >= ctx.bench[module.MANIP]["safe_z"]
               - ctx.verification["manipulator"]["tolerance_mm"] for p in commands)
    assert not events(s, "safety.violation")


@pytest.mark.parametrize("kind", ["pickup", "release"])
def test_interrupted_current_unsafe_step_never_replays(admitted, monkeypatch, kind):  # noqa: F811
    import qumus2.flows.library.stacking as module
    from qumus2.flows.workflow import Assembly, Molecule
    from qumus2.runtime.recovery import decide
    h = admitted
    step = (module.PickUpFlake("a", "stamp", "hBN") if kind == "pickup" else
            module.DropDown("stamp", ["a"], {"a": "hBN"}, "target", (0., 0.), 170., None))
    assert step.safe_to_resume is False
    monkeypatch.setattr(h.Original, "build_workflow", lambda self, params:
                        Assembly(self.name, "Unsafe production Step recovery boundary",
                                 (Molecule("transfer", "Original unsafe Step", (step,)),)))
    rid = h.service.submit(h.Original.name, {})
    h.service._q.get_nowait()
    h.store.set_run_status(rid, "running")
    h.store.begin_step(rid, 0, step.name, safe_to_resume=step.safe_to_resume)
    monkeypatch.setattr(step, "act", lambda *args: pytest.fail("Unsafe act must not replay"))
    monkeypatch.setattr(step, "probe_state", lambda *args: pytest.fail("Unsafe progress must not be probed"))
    decision = decide(h.store, h.factory, h.store.get_run(rid))
    assert decision.action == "park" and "not safe" in decision.reason
    assert h.store.get_steps(rid)[0]["status"] == "in_progress"
    assert not h.trace



def test_selected_catalogue_inputs_are_admitted_without_claiming_transfer(observed):  # noqa: F811
    from unittest.mock import patch
    s = observed
    s.store.upsert_sample("lower", "flake", status="selected")
    params = {"flake_ids": ["lower"], "target_chip_id": "chip_e01"}
    with patch.object(s.jobs, "enqueue") as enqueue:
        rid = s.jobs.submit("stack_device", params)
        enqueue.assert_called_once_with(rid)
    assert s.store.get_run(rid)["params"]["flake_ids"] == ["lower"]
    assert s.store.get_run(rid)["status"] == "queued"
    assert not events(s, "stack.pickup_endpoint")
    s.store.upsert_sample("lower", "flake", status="stacked")
    with pytest.raises(ValueError, match="only catalogued or selected"):
        s.jobs.submit("stack_device", params)



def test_named_source_registration_needs_no_simulator_ids(source_case, monkeypatch):  # noqa: F811
    from qumus2.flows.library.stacking import RefindFlake
    from qumus2.flows.step import StepRunner
    s, ctx, _, original = source_case
    step = RefindFlake("source", "chip_001", "Gr")
    calls = _ideal_without_optional_detector_ids(s, monkeypatch)
    outcome = StepRunner().run(step, ctx, 0)
    assert outcome.ok
    assert any(row["phase"] == step.name and row["method"] == "find_flakes" and row["detections"] for row in calls)
    assert not any(row["remaining_ids"] for row in calls)
    reference = s.store.get_run(ctx.run_id)["meta"]["normal_transfer"]["source_references"]["source"]
    assert reference["source_identity"]["original"]["image"] == original.saved_path()
    assert reference["source_id"] == "source" and reference["meta"]["objective"] == 20



def test_single_flake_schema_and_job_image_delivery_are_component_contracts(native_reference):  # noqa: F811
    from qumus2.agents.tools.hardware import StackDeviceArgs, job_result
    from qumus2.runtime.jobs import JobStatus
    _, _, _, frame, reference = native_reference
    args = StackDeviceArgs(flake_ids=["single-source"], target_chip_id="chip_e01")
    assert args.flake_ids == ["single-source"] and args.target_chip_id == "chip_e01"
    # A response fixture tests image delivery; it creates no ordinary physical receipt.
    status = JobStatus("image-response-component", "image_response_component", "succeeded", True,
        result=[{"status": "succeeded", "step": "retained_image", "attempts": 1, "corrections": 0,
                 "observations": [{"verdict": "ok", "measurements": {}, "expected": {},
                                   "images": [frame.saved_path()], "message": ""}]}])
    delivered = job_result(status)
    assert delivered.status == "ok" and delivered.images == [reference["image"]]
    assert Path(delivered.images[0]).is_file()
