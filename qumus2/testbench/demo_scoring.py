"""Separate pure recorded-process consistency and private terminal-truth checks.

This is not sensing, chemistry validation or a narrative judge. Callers bind the
snapshot to its source/event prefix and perform the ordinary domain checks.
Missing evidence stays unknown; completion stories additionally fail closed.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _same_capture_optics(capture: Any, focus: Any) -> bool:
    # The stacking focus binding's own rule (AF raster vs full-sensor capture).
    from qumus2.flows.library.stacking import _same_capture_optics as same

    return same(capture, focus)


def _pickup_presses(params: dict, fid: str) -> list[str]:
    """Every planned pickup press of a layer (a re-press after a miss, #109)."""
    from qumus2.flows.library.stacking import pickup_press_names

    return pickup_press_names(fid, int(_mapping(params.get("pickup_represses")).get(fid, 0)))


def _committed_pickup(params: dict, fid: str) -> str:
    """The press whose verified endpoint commits the layer's transfer."""
    return _pickup_presses(params, fid)[-1]


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list:
    return value if isinstance(value, list) else []


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def audit_recorded_v1_process(events, runs, *, story, binding=None, execution_source=None):
    """Compare recorded V1 claims only; no pixels, files, World or current config.

    A consistent result describes the retained record, not independent physical
    success. Missing legacy evidence is incomplete, never backfilled from code.
    """
    checks, problems, unknowns, audited = {}, [], [], []
    required = story == "complete"
    rows = list(runs.values()) if isinstance(runs, dict) else _rows(runs)
    transfer = [r for r in rows if _mapping(r).get("flow") in ("stack_device", "prepare_material_metal")]
    result = {"required": required, "state": "not_reached" if required else "not_applicable",
              "consistent": False if required else None, "checks": checks, "runs": audited,
              "problems": problems, "unknowns": unknowns,
              "limitations": ["Recorded hashes are consistency claims; image bytes and segmentation are not reverified.",
                  "Only the retained last motion intent is checked; complete per-command duration/travel and thermal threshold order are not reconstructible from phase summaries.",
                  "Coverage is not transfer, identity, containment, registry, film integrity or chemistry proof; terminal World truth is scored separately."]}
    if not transfer:
        if required:
            unknowns.append("No recorded transfer job; complete cannot pass without new-process execution.")
        return result
    result["consistent"] = False

    def check(name, value):
        checks[name] = value if value is None else bool(value)
        if value is None:
            unknowns.append(name)
        elif not value:
            problems.append(name)

    def finite_list(value, size):
        return isinstance(value, list) and len(value) == size and all(_number(v) for v in value)

    def sha(value):
        return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)

    def digest(value):
        try:
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=True, allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError):
            return None

    binding = _mapping(binding)
    profile = _mapping(binding.get("profile"))
    v2 = profile.get("id") == "v1-normal-sim-v2"
    target_release_policy = "native20-target-reference-parked-release-v1"
    witness_release_policy = "native20-latest-pickup-witness-parked-release-v2"
    observed_release_policy = "native20-observed-loaded-support-parked-release-v3"
    height_mapping = "synthetic-native20-source-focus-positive-up-process-reference-v1"
    digest_contract = "qumus-image-content-evidence-v1"

    def evidence_digest(value):
        # Independent implementation of the recorded, versioned projection.
        # Only a content-hash-bound image locator is portable across exports.
        def project(item):
            if isinstance(item, dict):
                projected = {key: project(value) for key, value in item.items()}
                locator = item.get("image")
                if isinstance(locator, dict):
                    # v1 RGB recognition-chain locator {"path", "sha256", ...}.
                    if not isinstance(locator.get("path"), str) or not locator["path"] or not sha(locator.get("sha256")):
                        raise ValueError("Malformed image locator")
                    projected["image"] = {**projected["image"], "path": "sha256:" + locator["sha256"]}
                    return projected
                if "image" in item and item["image"] is not None and (not isinstance(item["image"], str) or not item["image"]):
                    raise ValueError("Malformed image locator")
                if isinstance(item.get("image"), str):
                    hashes = [item[key] for key in ("image_sha256", "sha256") if key in item]
                    if not hashes or not all(sha(value) for value in hashes) or len(set(hashes)) != 1:
                        raise ValueError("Unbound image locator")
                    projected["image"] = "sha256:" + hashes[0]
                return projected
            if isinstance(item, (list, tuple)):
                return [project(value) for value in item]
            return item
        try:
            return hashlib.sha256(json.dumps(project(value), sort_keys=True, allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError):
            return None

    check("explicit_source_profile_binding", None if not binding else
          sha(execution_source) and binding.get("execution_source_sha256") == execution_source
          and binding.get("procedure_contract") == "v1-normal-sequence-v1"
          and profile.get("id") in ("v1-normal-sim-v1", "v1-normal-sim-v2") and profile.get("source") == "synthetic"
          and profile.get("simulation_only") is True
          and profile.get("holder_xy_mapping") == "synthetic-parallel-world-axes-v1"
          and sha(binding.get("profile_sha256")) and binding["profile_sha256"] == digest(profile)
          and bool(_mapping(binding.get("capture_profile"))))
    if v2:
        check("explicit_v2_process_binding", profile.get("source_height_mapping") == height_mapping
              and binding.get("pickup_max_press_mm") == .2
              and finite_list(binding.get("park_xy_mm"), 2)
              and _number(binding.get("target_tolerance_mm")) and binding["target_tolerance_mm"] > 0)
        result["limitations"].append("V2 visual process confirmation means source disappearance and target material-type appearance; buried-layer identity and stamp contents remain unverified.")
    bounds = ("max_observations", "max_total_s", "max_travel_mm", "max_unobserved_z_mm", "max_unobserved_s")
    check("explicit_recorded_bounds", None if not binding else
          all(_number(profile.get(k)) and profile[k] > 0 for k in bounds)
          and _number(binding.get("axis_tolerance_mm")) and binding["axis_tolerance_mm"] > 0
          and _number(binding.get("safe_z_mm")) and _number(profile.get("focus_offset_mm")))
    if not all(checks.values()):
        result["state"] = "inconsistent" if problems else "incomplete"
        return result
    ids = [e.get("id") for e in events]
    check("ordered_unique_event_prefix", all(type(i) is int for i in ids) and ids == sorted(set(ids)))
    if not checks["ordered_unique_event_prefix"]:
        result["state"] = "inconsistent"
        return result
    run_ids = [run.get("id") for run in transfer]
    check("unique_scoped_transfer_runs", all(isinstance(rid, str) and rid for rid in run_ids)
          and len(run_ids) == len(set(run_ids))
          and (not isinstance(runs, dict) or all(runs.get(run["id"]) == run for run in transfer)))
    tolerance, safe_z = binding["axis_tolerance_mm"], binding["safe_z_mm"]
    image_claims = {}

    def reference(value, objective=None):
        record = _mapping(value)
        meta = _mapping(record.get("meta"))
        calibration = _mapping(meta.get("calibration"))
        image = record.get("image")
        if (any(k not in record for k in ("image", "sha256", "captured_at", "meta"))
                or any(k not in meta for k in ("objective", "focus_z", "focus_plane", "fov_mm", "calibration"))):
            return None
        good = (isinstance(image, str) and bool(image) and sha(record.get("sha256"))
                and _number(record.get("captured_at")) and _number(meta.get("focus_z"))
                and (objective is None or meta.get("objective") == objective)
                and finite_list(meta.get("fov_mm"), 2)
                and calibration.get("source") == "synthetic" and calibration.get("frame") == "microscope_image"
                and calibration.get("u_axis") == "+world_x" and calibration.get("v_axis") == "-world_y"
                and finite_list(calibration.get("mm_per_pixel"), 2)
                and all(v > 0 for v in calibration["mm_per_pixel"])
                and isinstance(calibration.get("image_size_px"), list) and len(calibration["image_size_px"]) == 2
                and all(type(v) is int and v > 0 for v in calibration["image_size_px"])
                and calibration.get("photometry_profile") == binding["capture_profile"])
        claim = (record.get("sha256"), record.get("captured_at"), meta)
        good = good and (image not in image_claims or image_claims[image] == claim)
        if isinstance(image, str):
            image_claims[image] = claim
        return good

    for run in transfer:
        rid, params, state = run.get("id"), _mapping(run.get("params")), _mapping(_mapping(run.get("meta")).get("normal_transfer"))
        run_params = params  # `params` is rebound to command params further down
        local = [e for e in events if e.get("run_id") == rid]
        prefix = str(rid) + ":"
        audited.append(rid)
        if params.get("design_pose_window_admission") is not None:
            check(prefix+"design_pose_window_admission", _recorded_design_pose_window(local, params))
        elif params.get("device_design") and params.get("release_policy") == observed_release_policy:
            check(prefix+"design_pose_window_admission", False)
        check(prefix + "binding", None if not all(k in params for k in ("procedure_contract", "transfer_profile", "stamp_swaps")) else
              isinstance(rid, str) and bool(rid) and params.get("procedure_contract") == binding["procedure_contract"]
              and params.get("transfer_profile") == profile["id"] and run.get("bench") == "sim"
              and params.get("stamp_swaps") == [])
        layers, stamp = params.get("flake_ids"), params.get("stamp_id")
        valid_ids = (isinstance(layers, list) and bool(layers) and all(isinstance(f, str) and f for f in layers)
                     and len(layers) == len(set(layers)) and isinstance(stamp, str) and bool(stamp)
                     and all(isinstance(_mapping(params.get("flake_chips")).get(f), str) and params["flake_chips"][f] for f in layers)
                     and isinstance(params.get("target_chip_id"), str) and bool(params["target_chip_id"]))
        check(prefix + "selected_layers_stamp", valid_ids)
        if not valid_ids:
            continue
        release = "release_material_metal_stack" if run["flow"] == "prepare_material_metal" else "v1_drop_down"
        critical = ["heat_transfer_stage", "load_stamp", "grip_stamp"]
        for index, fid in enumerate(layers):
            critical += [f"refind_{fid}"]
            if index == 0 and params.get("stamp_discovery_policy"):
                critical += ["prepare_stamp_discovery"]
            critical += [f"v1_initialize_{fid}"]
            if index == 0:
                critical += [f"v1_first_contact_{stamp}"]
                if params.get("stamp_discovery_policy"):
                    critical += ["restore_stamp_discovery_source"]
            critical += [_committed_pickup(run_params, fid), f"park_stamp_{fid}"]
        critical += ["go_to_target", "observe_transfer_target", "v1_initialize_target", release,
                     "park_stamp_after_drop", "cool_transfer_stage", "return_stamp"]
        manifest = _mapping(_mapping(run.get("meta")).get("workflow"))
        children = [_mapping(child) for child in _rows(manifest.get("children"))]
        stages = [f"v1_normal_sequence_v1_sim_{'v2' if v2 else 'v1'}_initialize_transfer", *[f"pickup_layer_{fid}" for fid in layers], "release_stack", "return_stamp"]
        if run["flow"] == "prepare_material_metal":
            stages = ["admit_and_handover", *stages, "return_prepared_carrier", "record_terminal"]
        leaves = [_mapping(leaf) for child in children for leaf in _rows(_mapping(child).get("checkpoints"))]
        names = [leaf.get("name") for leaf in leaves]
        workflows = [e for e in local if e.get("kind") == "job.workflow"]
        submissions = [e for e in local if e.get("kind") == "job.submitted"]
        starts = [e for e in local if e.get("kind") == "step.started"]
        finishes = [e for e in local if e.get("kind") == "step.finished"]
        # A pickup press superseded by a planned re-press (#109) finishes
        # "failed" and is then marked "skipped" by the replan; one replanned
        # workflow follows each re-press.
        superseded = {n for fid in layers for n in _pickup_presses(run_params, fid)[:-1]}
        represses = sum(int(v) for v in _mapping(run_params.get("pickup_represses")).values())
        replanned = [e["payload"].get("seq") for e in finishes if e["payload"].get("status") == "skipped"]
        finishes = [e for e in finishes if not (e["payload"].get("status") == "failed"
                    and e["payload"].get("name") in superseded and e["payload"].get("seq") in replanned)]
        check(prefix + "submission", None if len(submissions) != 1 else bool(workflows)
              and submissions[0]["id"] < workflows[0]["id"] and submissions[0]["payload"].get("flow") == run["flow"]
              and all(_mapping(submissions[0]["payload"].get("params")).get(k) == params.get(k)
                      for k in ("procedure_contract", "transfer_profile", "flake_ids", "stamp_id", "target_chip_id")))
        check(prefix + "workflow", None if not leaves or len(workflows) != 1+represses else
              manifest.get("name") == run["flow"] and all(isinstance(name, str) for name in names)
              and manifest.get("level") == "assembly" and [_mapping(child).get("name") for child in children] == stages
              and manifest.get("start_seq") == 0 and manifest.get("end_seq") == len(leaves)-1
              and all(_mapping(child).get("level") == "molecular" and _rows(child.get("checkpoints"))
                      and child.get("start_seq") == _mapping(child["checkpoints"][0]).get("seq")
                      and child.get("end_seq") == _mapping(child["checkpoints"][-1]).get("seq") for child in children)
              and [leaf.get("seq") for leaf in leaves] == list(range(len(leaves)))
              and len(workflows) == 1+represses and workflows[-1]["payload"].get("workflow") == manifest
              and workflows[0]["payload"].get("reason") == "submitted"
              and all(e["payload"].get("reason") == "replanned" for e in workflows[1:])
              and (not starts or workflows[0]["id"] < starts[0]["id"])
              and all(names.count(name) == 1 for name in critical)
              and [names.index(name) for name in critical if name in names] == sorted(names.index(name) for name in critical if name in names)
              and not any(name.startswith(("align_stamp", "refocus_through_stamp", "return_torn_")) for name in names))
        # Unexecuted planned Steps after a truthful hold stay unknown.
        check(prefix + "actual_checkpoint_order", None if run.get("status") != "succeeded" or not starts or not finishes else
              [(e["payload"].get("seq"), e["payload"].get("name")) for e in starts] == [(leaf.get("seq"), leaf.get("name")) for leaf in leaves]
              and len(finishes) == len(leaves) and all(e["payload"].get("status") == (
                  "skipped" if e["payload"].get("name") in superseded else "done") for e in finishes)
              and all(a["id"] < b["id"] and a["payload"].get("seq") == b["payload"].get("seq")
                      and a["payload"].get("name") == b["payload"].get("name")
                      and (i + 1 == len(starts) or b["id"] < starts[i + 1]["id"])
                      for i, (a, b) in enumerate(zip(starts, finishes, strict=True))))
        check(prefix + "observed_checkpoint_prefix", None if not starts else
              [(e["payload"].get("seq"), e["payload"].get("name")) for e in starts]
              == [(leaf.get("seq"), leaf.get("name")) for leaf in leaves[:len(starts)]])
        statuses = [e for e in local if e.get("kind") == "job.status"]
        check(prefix + "terminal_status_binding", None if not statuses else statuses[-1]["payload"].get("status") == run.get("status"))
        check(prefix + "terminal_success", None if not statuses or run.get("status") != "succeeded" else
              statuses[-1]["payload"].get("status") == "succeeded" and bool(finishes)
              and statuses[-1]["id"] > finishes[-1]["id"])
        placement = _mapping(_mapping(state.get("placements")).get(stamp))
        discovery = _mapping(state.get("discovery")) if params.get("stamp_discovery_policy") else {}
        if params.get("stamp_discovery_policy"):
            try:
                discovery_admission = _recorded_stamp_discovery(events, run_id=rid, params=params,
                    state=state, digest=evidence_digest)
            except ValueError:
                discovery_admission = False
            check(prefix+"stamp_discovery_ready", None if not placement and run.get("status") != "succeeded" else bool(discovery_admission))
            check(prefix+"stamp_discovery_phases", None if not placement and run.get("status") != "succeeded" else
                  _recorded_discovery_phases(events, run_id=rid, state=state, digest=evidence_digest))
        working = placement.get("working_xy_mm")
        first = f"v1_first_contact_{stamp}"
        contact_starts = [e["payload"].get("name") for e in starts
                          if str(e["payload"].get("name") or "").startswith("v1_first_contact_")]
        contact_complete = (_mapping(_mapping(state.get("phases")).get(first)).get("complete") is True
            or any(e.get("kind") == "step.finished" and e["payload"].get("name") == first and e["payload"].get("status") == "done"
                   or e.get("kind") == "step.verified" and e["payload"].get("name") == first and e["payload"].get("verdict") == "ok"
                   for e in local))
        pending_boundary = "restore_stamp_discovery_source" if params.get("stamp_discovery_policy") else first
        contact_pending = (run.get("status") != "succeeded" and not state.get("placements")
            and (not contact_complete or bool(params.get("stamp_discovery_policy")))
            and ((not state and not contact_starts) or checks[prefix + "observed_checkpoint_prefix"] is True
                 and pending_boundary in names and all(e["payload"]["seq"] <= names.index(pending_boundary) for e in starts)))
        check(prefix + "one_first_contact", None if contact_pending else
              set(_mapping(state.get("placements"))) == {stamp} and placement.get("stamp_id") == stamp
              and placement.get("run_id") == rid and placement.get("profile") == profile["id"]
              and placement.get("first_contact_step") == first and finite_list(working, 2)
              and contact_starts == [first])
        if not finite_list(working, 2) and not v2:
            continue
        def near_xy(value, working=working):
            return finite_list(working, 2) and finite_list(value, 2) and all(abs(a-b) <= tolerance for a, b in zip(value, working, strict=True))
        tail_witnesses = {name for name, phase in _mapping(state.get("phases")).items()
                          if v2 and ("final_partial_macro" in _mapping(phase) or any(
                              "macro_admission_event_id" in _mapping(_mapping(phase).get(key))
                              or "requested_macro_target_mm" in _mapping(_mapping(phase).get(key)) for key in ("last_intent", "pending_intent")))}
        if v2:
            tail_witnesses.update(event["payload"].get("phase") for event in local if event.get("kind") == "stack.pickup_macro_admission"
                                  and isinstance(event["payload"].get("phase"), str))
            tail_witnesses.update(_mapping(event["payload"].get("microscope_preview")).get("phase") for event in local
                if event.get("kind") == "lab.frame" and "transfer_motion_intent" in _mapping(event["payload"].get("microscope_preview"))
                and isinstance(_mapping(event["payload"].get("microscope_preview")).get("phase"), str))
        open_tail_steps = {name for name in tail_witnesses if name in {n for fid in layers for n in _pickup_presses(run_params, fid)}
            and run.get("status") in ("running", "paused", "parked", "waiting_human", "failed", "cancelled")
            and starts and starts[-1]["payload"].get("name") == name
            and not any(event["payload"].get("name") == name for event in finishes)}
        release_marker = (v2 and (params.get("release_policy") is not None or binding.get("release_policy") is not None
            or any(key in _mapping(_mapping(state.get("phases")).get(release)) for key in ("release_policy", "release_reference", "release_budget"))))
        open_release_steps = ({release} if release_marker and run.get("status") in ("running", "paused", "parked", "waiting_human", "failed", "cancelled")
            and starts and starts[-1]["payload"].get("name") == release
            and not any(event["payload"].get("name") == release for event in finishes) else set())
        def window(name, starts=starts, finishes=finishes, open_steps=open_tail_steps|open_release_steps, local=local):
            a = [e for e in starts if e["payload"].get("name") == name]
            b = [e for e in finishes if e["payload"].get("name") == name]
            if len(a) == 1 and not b and name in open_steps:
                return a[0]["id"], max(event["id"] for event in local)+1
            return (a[0]["id"], b[0]["id"]) if len(a) == len(b) == 1 else (math.inf, -math.inf)
        def measurements(name, local=local, names=names, window=window):
            lo, hi = window(name)
            found = [e for e in local if e.get("kind") == "step.verified" and e["payload"].get("name") == name
                     and e["payload"].get("verdict") == "ok" and lo < e["id"] < hi
                     and e["payload"].get("seq") == names.index(name)] if name in names else []
            return _mapping(found[0]["payload"].get("measurements")) if len(found) == 1 else {}
        def captured(name, record, phase=None, local=local, window=window, *, step_name=None):
            record = _mapping(record)
            lo, hi = window(step_name or name)
            return any(lo < event["id"] < hi and preview.get("phase") == (phase or name)
                and preview.get("image") == record.get("image") and preview.get("timestamp") == record.get("captured_at")
                and preview.get("calibration") == _mapping(record.get("meta")).get("calibration")
                and (not v2 or preview.get("image_sha256") == record.get("sha256")
                     and _number(event.get("ts")) and _number(record.get("captured_at"))
                     and 0 <= event["ts"]-record["captured_at"] <= 2
                     and all(preview.get(key) == _mapping(record.get("meta")).get(key)
                             for key in ("objective", "focus_z", "focus_plane", "stage_kinematics", "fov_mm")))
                and preview.get("immutable") is True and preview.get("verification") is False
                for event in local if event.get("kind") == "lab.frame"
                for preview in [_mapping(event["payload"].get("microscope_preview"))])

        phases = _mapping(state.get("phases"))
        release_phase = _mapping(phases.get(release))
        relative_release = (v2 and (params.get("release_policy") is not None or binding.get("release_policy") is not None
                            or any(key in release_phase for key in ("release_policy", "release_reference", "release_budget"))))
        witnessed_release = (v2 and (witness_release_policy in (params.get("release_policy"), binding.get("release_policy"), release_phase.get("release_policy"))
                                    or "witness" in _mapping(release_phase.get("release_reference"))))
        observed_release = params.get("release_policy") == observed_release_policy
        release_policy = observed_release_policy if observed_release else witness_release_policy if witnessed_release else target_release_policy
        if observed_release:
            check(prefix+"observed_release_support", _recorded_observed_release_support(events, run_id=rid, phase=release_phase,
                  params=params, digest=evidence_digest))
            note = "Observed-release record rechecks original receipts and reconstructed opacity masks, not original RGB classification; its centroid alpha is not transfer confidence."
            if note not in result["limitations"]:
                result["limitations"].append(note)
        if relative_release:
            note = "Target-relative release additionally checks recorded command/encoder/camera history; thermal command order and retained release peak do not reconstruct a full sensor trajectory."
            if note not in result["limitations"]:
                result["limitations"].append(note)
        if witnessed_release:
            note = "Latest-pickup intent is declared SIM process memory. Named load, vacuum command and global ledger continuity do not measure grip pressure, stamp identity or whole-material contact."
            if note not in result["limitations"]:
                result["limitations"].append(note)

        def historical(event_id, kind, name, local=local):
            lo, hi = window(name)
            found = [event for event in local if type(event_id) is int and event_id > 0
                     and event.get("id") == event_id and event.get("kind") == kind and lo < event_id < hi]
            return found[0] if len(found) == 1 else {}

        def verified_endpoint(name, phase, observed, images, *, proof_field="endpoint_verified"):
            proof = _mapping(phase.get(proof_field))
            if not proof or not observed:
                return None
            event = historical(proof.get("event_id"), "step.verified", name)
            payload = _mapping(event.get("payload"))
            body = {key: payload.get(key) for key in ("seq", "name", "verdict", "measurements", "expected")}
            return (all(key in payload for key in body) and payload.get("name") == name
                and payload.get("verdict") == "ok" and payload.get("measurements") == observed
                and payload.get("images") == images and sha(proof.get("observation_sha256"))
                and evidence_digest(body) == proof["observation_sha256"])

        def valid_af(af, *, interval_required=True, flat_reference=None):
            """Rebuild the sampled bracket with readback error included once.

            `flat_reference`: a source-absence look may instead be a flat sweep
            around that textured recorded reference (stacking #109)."""
            try:
                axis = af["focus_axis_calibration"]
                final = af["final_focus_readback_z_mm"]
                if flat_reference is not None and not interval_required:
                    from qumus2.flows.library.stacking import _source_absence_focus_ok
                    peak_ok = _source_absence_focus_ok(af, flat_reference)
                else:
                    peak_ok = af["peak_observable"] is True
                if not (af["objective"] == af["objective_readback"] == 20
                        and af["plane"] == af["focus_plane"] == "chip" and peak_ok
                        and _number(final) and _number(af["readback_focus_z_mm"]) and af["readback_focus_z_mm"] == final and _number(af["captured_at"])
                        and sha(af["image_sha256"]) and bool(_mapping(af["stage_kinematics"]))
                        and all(_number(af["holder_pose"][key]) for key in ("x", "y", "z"))
                        and axis["source"] == "synthetic" and axis["simulation_only"] is True
                        and axis["calibration_id"] == "synthetic-microscope-focus-axis-v1"
                        and axis["objective"] == 20 and axis["comparison_scope"] == "same_objective_only"
                        and all(_number(axis[key]) and axis[key] >= 0 for key in
                                ("scale_mm_per_focus_mm", "mapping_error_mm", "readback_error_mm"))
                        and axis["scale_mm_per_focus_mm"] > 0
                        and af["optical_calibration"]["photometry_profile"] == binding["capture_profile"]):
                    return False
                if not interval_required:
                    return True
                candidates = []
                passes = af["sampled_passes"]
                if not isinstance(passes, list) or any(p["selected"] and p["unresolved_peaks"] for p in passes):
                    return False
                for index, sample_pass in enumerate(passes):
                    bracket, samples = sample_pass["bracket_mm"], sample_pass["samples"]
                    if not finite_list(bracket, 2) or not samples or not all(
                        _number(sample["readback_focus_z_mm"]) and sample["objective_readback"] == 20
                        and sample["focus_axis_calibration"] == axis
                        and all(sample[key] == af[key] for key in ("optical_calibration", "stage_kinematics", "holder_pose"))
                        for sample in samples):
                        continue
                    zs = [sample["readback_focus_z_mm"] for sample in samples]
                    if all(a < b for a, b in zip(zs, zs[1:], strict=False)) and bracket[0] <= final <= bracket[1]:
                        error = axis["readback_error_mm"]
                        candidates.append((bracket[1]-bracket[0], index, [bracket[0]-error, bracket[1]+error]))
                return bool(candidates) and finite_list(af["focus_interval_mm"], 2) and af["focus_interval_mm"] == min(candidates)[2]
            except (KeyError, TypeError, ValueError, IndexError):
                return False

        def focused_reference(record, name, phase, *, parked, rid=rid):
            if not record or not _mapping(record).get("focus_binding"):
                return None
            try:
                proof, meta = record["focus_binding"], record["meta"]
                event = historical(proof["event_id"], "microscope.focus", name)
                capture = historical(proof["capture_event_id"], "lab.frame", name)
                af, preview = event["payload"], capture["payload"]["microscope_preview"]
                summary_keys = ("objective", "objective_readback", "plane", "focus_plane", "peak_observable",
                    "focus_interval_mm", "focus_axis_calibration", "final_focus_readback_z_mm", "readback_focus_z_mm",
                    "optical_calibration", "stage_kinematics", "holder_pose", "image_sha256", "captured_at")
                af_frame = proof["af_frame"]
                good = (reference(record, 20) and reference(af_frame, 20) and valid_af(af)
                    and proof["digest_contract"] == digest_contract and proof["profile"] == profile["id"]
                    and proof["run_id"] == rid and proof["optical_path"] == ("parked_unobstructed" if parked else "held_target")
                    and sha(proof["event_sha256"]) and evidence_digest(af) == proof["event_sha256"]
                    and sha(proof["capture_event_sha256"]) and evidence_digest(capture["payload"]) == proof["capture_event_sha256"]
                    and proof["observation"] == {key: af[key] for key in summary_keys}
                    and event["id"] < capture["id"] and meta["focus_plane"] == "chip"
                    and meta["focus_z"] == af["final_focus_readback_z_mm"]
                    and _same_capture_optics(meta["calibration"], af["optical_calibration"]) and meta["fov_mm"] == af["fov_mm"]
                    and meta["stage_kinematics"] == af["stage_kinematics"] and proof["holder_pose"] == af["holder_pose"]
                    and af_frame["sha256"] == af["image_sha256"] and af_frame["captured_at"] == af["captured_at"]
                    and all(af_frame["meta"][key] == meta[key] for key in
                            ("objective", "focus_z", "focus_plane", "calibration", "stage_kinematics", "fov_mm"))
                    and 0 <= record["captured_at"]-af["captured_at"] <= 2
                    and _number(event.get("ts")) and 0 <= event["ts"]-af["captured_at"] <= 2
                    and _number(capture.get("ts")) and 0 <= capture["ts"]-record["captured_at"] <= 2
                    and preview["image_sha256"] == record["sha256"]
                    and all(preview[key] == meta[key] for key in
                            ("objective", "focus_z", "focus_plane", "calibration", "stage_kinematics", "fov_mm"))
                    and captured(name, record, phase=phase) and proof["park_xy_mm"] == binding["park_xy_mm"]
                    and proof["clear_z_mm"] == safe_z and proof["position_tolerance_mm"] == tolerance)
                if parked:
                    good = (good and proof["holder_pose"]["z"] >= safe_z-tolerance
                        and all(abs(proof["holder_pose"][key]-proof["park_xy_mm"][index]) <= tolerance
                                for index, key in enumerate(("x", "y"))))
                else:
                    good = good and near_xy([proof["holder_pose"][key] for key in ("x", "y")])
                return good
            except (KeyError, TypeError, ValueError, IndexError):
                return False

        def process_reference(fid, source, phase, placement=placement, state=state, layers=layers, local=local, first=first, phases=phases, discovery=discovery, run_params=run_params, *, step_name=None):
            process = _mapping(phase.get("process_reference"))
            if not process or not source or not placement.get("source_anchor"):
                return None
            try:
                anchor = placement["source_anchor"]
                restored_anchor = state["source_references"][layers[0]]
                original = discovery["focus_reference"] if discovery else restored_anchor
                a, b = (ref["focus_binding"]["observation"] for ref in (original, source))
                axis = a["focus_axis_calibration"]
                scale, error = axis["scale_mm_per_focus_mm"], 2*axis["mapping_error_mm"]
                low0, high0 = a["focus_interval_mm"]
                low, high = b["focus_interval_mm"]
                delta = [0., 0.] if not discovery and step_name is None and fid == layers[0] else [scale*(low-high0)-error, scale*(high-low0)+error]
                candidate = placement["candidate_z_mm"]
                interval = [candidate+value for value in delta]
                minimum = interval[1]-.2
                fields = {"profile": profile["id"], "mapping": height_mapping, "digest_contract": digest_contract,
                    "source_id": fid, "anchor_source_id": layers[0],
                    "anchor_focus_binding_sha256": evidence_digest(restored_anchor["focus_binding"]),
                    "current_focus_binding_sha256": evidence_digest(source["focus_binding"]),
                    "first_candidate_z_mm": candidate, "delta_interval_mm": delta, "reference_interval_mm": interval,
                    "max_press_mm": .2, "minimum_z_mm": minimum}
                lo, hi = window(step_name or _committed_pickup(run_params, fid))
                commands = [_mapping(event["payload"].get("params")) for event in local
                    if lo < event["id"] < hi and event.get("kind") == "device.command"
                    and event["payload"].get("device") == "transfer_arm"
                    and event["payload"].get("command") == "move_axis_to"]
                return (anchor["source_id"] == layers[0] and anchor["image_sha256"] == restored_anchor["sha256"]
                    and sha(anchor["focus_binding_sha256"]) and anchor["focus_binding_sha256"] == fields["anchor_focus_binding_sha256"]
                    and a["focus_axis_calibration"] == b["focus_axis_calibration"]
                    and _number(candidate) and candidate == phases[first]["first_contact_region"]["actual_holder_pose"]["z"]
                    and all(_number(value) for value in [*delta, *interval, minimum])
                    and all(process.get(key) == value for key, value in fields.items())
                    and all(command.get("axis") != "z" or _number(command.get("value")) and command["value"] >= minimum for command in commands)
                    and _number(_mapping(phase.get("last_pose")).get("z")) and phase["last_pose"]["z"] >= minimum
                    and (step_name is not None and not phase.get("last_intent") and not commands and phase.get("complete") is not True
                         or all(_number(_mapping(phase.get("last_intent")).get(key)) and phase["last_intent"][key] >= minimum
                                for key in ("from_mm", "to_mm", "macro_target_mm"))))
            except (KeyError, TypeError, ValueError, IndexError, OverflowError):
                return False

        def visual_pickup(fid, source, phase, observed, local=local, run_params=run_params):
            if not phase.get("endpoint") or not observed:
                return None
            try:
                af_event = historical(observed["focus_event_id"], "microscope.focus", _committed_pickup(run_params, fid))
                af = af_event["payload"]
                frames = observed["source_frames"]
                verified = [event for event in local if event.get("kind") == "step.verified"
                            and event["payload"].get("name") == _committed_pickup(run_params, fid) and event["payload"].get("verdict") == "ok"]
                return (phase["endpoint"] == observed and observed["profile"] == profile["id"]
                    and observed["method"] == "source_disappearance" and observed["source_id"] == fid
                    and observed["source_reference_sha256"] == source["sha256"]
                    and observed["flake_on_chip"] is False and type(observed["source_detection_count"]) is int
                    and observed["source_detection_count"] == 0 and observed["process_confirmation"] is True
                    and observed["stamp_evaluated"] is False and observed["flake_on_stamp"] is None
                    and observed["no_transfer_confirmed"] is None
                    and observed["full_identity_completeness_observed"] is False and observed["registry_chemistry_observed"] is False
                    and _mapping(phase.get("film_integrity")).get("film_intact") is not False
                    and valid_af(af, interval_required=False, flat_reference=source)
                    and isinstance(frames, list) and len(frames) >= 2
                    and any(frame["sha256"] != source["sha256"] for frame in frames)
                    and len(verified) == 1 and _number(verified[0].get("ts")) and _number(af_event.get("ts"))
                    and phase["observed_at"] <= af["captured_at"] <= af_event["ts"] <= frames[0]["captured_at"]
                    and [frame["captured_at"] for frame in frames] == sorted(frame["captured_at"] for frame in frames)
                    and all(abs(af["holder_pose"][key]-phase["last_pose"][key]) <= tolerance for key in ("x", "y", "z"))
                    and all(reference(frame, 20) and frame["meta"]["focus_plane"] == "chip"
                        and frame["meta"]["focus_z"] == af["final_focus_readback_z_mm"]
                        and frame["meta"]["calibration"] == source["meta"]["calibration"]
                        and _same_capture_optics(frame["meta"]["calibration"], af["optical_calibration"])
                        and frame["meta"]["stage_kinematics"] == source["meta"]["stage_kinematics"] == af["stage_kinematics"]
                        and frame["meta"]["fov_mm"] == source["meta"]["fov_mm"] == af["fov_mm"]
                        and 0 <= frame["captured_at"]-af["captured_at"] <= 2
                        and frame["captured_at"] <= verified[0]["ts"] for frame in frames))
            except (KeyError, TypeError, ValueError, IndexError):
                return False

        def target_view(view, name, phase, *, parked, rid=rid, params=params):
            if not view:
                return None
            try:
                ref, frames = view["focus_reference"], view["frames"]
                requested, actual = view["requested_stage_xy_mm"], view["stage_kinematics"]["sample_pose"]
                return (view["profile"] == profile["id"] and view["run_id"] == rid
                    and view["target_chip_id"] == params["target_chip_id"] and view["target_xy_mm"] == params["target_xy_mm"]
                    and finite_list(view["target_xy_mm"], 2) and finite_list(requested, 2)
                    and all(_number(actual[key]) for key in ("x", "y"))
                    and math.hypot(actual["x"]-requested[0], actual["y"]-requested[1]) <= binding["target_tolerance_mm"]
                    and focused_reference(ref, name, phase, parked=parked) is True
                    and isinstance(frames, list) and len(frames) == 3
                    and all(ref[key] == frames[0][key] for key in ("image", "sha256", "captured_at", "meta"))
                    and [frame["captured_at"] for frame in frames] == sorted(frame["captured_at"] for frame in frames)
                    and all(reference(frame, 20) and captured(name, frame, phase=phase)
                        and all(frame["meta"][key] == ref["meta"][key] for key in
                                ("objective", "focus_z", "focus_plane", "calibration", "stage_kinematics", "fov_mm"))
                        and frame["meta"]["stage_kinematics"] == view["stage_kinematics"] for frame in frames))
            except (KeyError, TypeError, ValueError, IndexError):
                return False
        expected_phases = [first, *[_committed_pickup(run_params, fid) for fid in layers], release]
        discovery_phases = ({first, "restore_stamp_discovery_source", "park_for_discovery_source_restore_z",
            "initialize_restored_discovery_source_engage_z"} if params.get("stamp_discovery_policy") else set())
        if discovery_phases:
            expected_phases.remove(first)
        lifts = {name: name.removeprefix("clear_lift_") for name in phases
                 if v2 and isinstance(name, str) and name.startswith("clear_lift_")}
        known_transits = {f"v1_initialize_{fid}_engage_z": (f"v1_initialize_{fid}", "initialize", fid) for fid in [*layers, "target"]}
        known_transits.update({f"park_stamp_{fid}_z": (f"park_stamp_{fid}", "park", fid) for fid in layers})
        known_transits.update({"park_stamp_after_drop_z": ("park_stamp_after_drop", "park", "target"),
            "manipulator_to_load_z": ("manipulator_to_load", "load", None),
            "manipulator_to_load_for_return_z": ("manipulator_to_load_for_return", "load", None)})
        transits = {name: info for name, info in known_transits.items() if v2 and name in phases}

        def encoder(event):
            # Both are public holder-axis readbacks; no stamp/world geometry.
            payload, poses = event["payload"], []
            if "manipulator" in payload:
                poses.append(_mapping(payload["manipulator"]))
            geometry = _mapping(payload.get("geometry"))
            optics = _mapping(geometry.get("microscope"))
            if "stamp_axes_xyz_mm" in optics:
                if (optics.get("stamp_axes_frame") != "transfer_arm" or optics.get("source") != "synthetic"
                        or geometry.get("source") != "synthetic"):
                    return {}
                poses.append(_mapping(optics["stamp_axes_xyz_mm"]))
            if not poses:
                return None
            if not all(_number(pose.get(key)) for pose in poses for key in ("x", "y", "z")):
                return {}
            if any(abs(pose[key]-poses[0][key]) > tolerance for pose in poses for key in ("x", "y", "z")):
                return {}
            return poses[-1]

        def auxiliary_motion(name, parent, run=run, local=local, starts=starts, finishes=finishes, phases=phases,
                             expected_phases=expected_phases, lifts=lifts, transits=transits, state=state, layers=layers, stamp=stamp, working=working, names=names, release=release, run_params=run_params):
            try:
                phase = phases[name]
                lo, hi = window(parent)
                begun = [event for event in starts if event["payload"].get("name") == parent]
                finished = [event for event in finishes if event["payload"].get("name") == parent]
                baseline = phase["reference_event"]["event_id"]
                last = [event for event in local if event.get("kind") == "lab.frame"
                        and _mapping(event["payload"].get("microscope_preview")).get("phase") == name
                        and event["payload"]["microscope_preview"].get("image") == phase["last_frame"]["image"]
                        and event["payload"]["microscope_preview"].get("timestamp") == phase["observed_at"]]
                if len(last) != 1:
                    return False
                end = last[0]["id"]+1
                commands = [event for event in local if baseline < event["id"] < end and event.get("kind") == "device.command"]
                moves = [event for event in commands if event["payload"].get("device") == "transfer_arm"]
                contexts = [_mapping(event["payload"].get("microscope_preview")) for event in local if event.get("kind") == "lab.frame"
                            and _mapping(event["payload"].get("microscope_preview")).get("phase") == name]
                allowed_fields = {"name", "profile", "started_at", "observations", "travel_mm", "difference_active",
                    "observed_at", "last_frame", "last_pose", "reference", "reference_event", "holder_xy", "last_intent", "complete", "safe_z_mm"}
                if not (len(begun) == len(finished) == 1 and parent in names and lo < baseline < end <= hi
                        and all(_number(event.get("ts")) for event in [begun[0], finished[0]])
                        and begun[0]["ts"] <= phase["started_at"] <= phase["reference"]["captured_at"]
                        and phase["observed_at"] <= finished[0]["ts"] and set(phase) <= allowed_fields
                        and phase.get("safe_z_mm", safe_z) == safe_z
                        and all(context.get(key) == phase["reference"]["meta"][key]
                                for context in [*contexts, phase["last_frame"]["meta"]]
                                for key in ("objective", "focus_z", "focus_plane", "fov_mm", "calibration", "stage_kinematics"))
                        and phase["last_intent"]["macro_target_mm"] == safe_z and moves
                        and all(event["payload"].get("device") == "transfer_arm" or
                            event["payload"].get("device") == "microscope" and event["payload"].get("command") == "capture" for event in commands)):
                    return False
                lift = name in lifts
                if lift:
                    prior = phases[parent]
                    initial, context_xy = prior["last_pose"], working
                    if not (run.get("status") in ("waiting_human", "parked", "failed", "paused", "cancelled")
                            and parent in expected_phases and len(lifts) == 1
                            and finished[0]["payload"].get("status") == "failed"
                            and starts[-1]["payload"].get("name") == finishes[-1]["payload"].get("name") == parent
                            and prior["profile"] == profile["id"] and initial["z"] < safe_z-tolerance
                            and _number(prior["observed_at"]) and 0 <= phase["started_at"]-prior["observed_at"] <= profile["max_unobserved_s"]
                            and all(phase["reference"]["meta"][key] == prior["last_frame"]["meta"][key]
                                    for key in ("objective", "focus_z", "focus_plane", "fov_mm", "calibration", "stage_kinematics"))):
                        return False
                else:
                    _, kind, fid = transits[name]
                    observed = measurements(parent)
                    verified = [event for event in local if event.get("kind") == "step.verified"
                                and lo < event["id"] < hi and event["payload"].get("name") == parent and event["payload"].get("verdict") == "ok"]
                    prior_readbacks = [event for event in local if lo < event["id"] < baseline and event.get("kind") == "lab.frame"
                                       and event["payload"].get("device") == "transfer_arm" and encoder(event) is not None]
                    if not (finished[0]["payload"].get("status") == "done" and observed and len(verified) == 1 and prior_readbacks
                            and last[0]["id"] < verified[0]["id"] and phase.get("complete") in (None, True)
                            and phase["reference"]["meta"]["objective"] in (5, 20)):
                        return False
                    initial = encoder(prior_readbacks[-1])
                    if kind == "initialize":
                        pose = observed["actual_holder_pose"]
                        context_xy = [pose[key] for key in ("x", "y")]
                        if not (observed["stamp_id"] == stamp and observed["profile"] == profile["id"] and observed["objective"] == 5
                                and phase["reference"]["meta"]["objective"] == 5
                                and phase["reference"]["meta"]["focus_z"] == state["initializations"][parent]["substrate_focus_mm"]
                                and (fid == layers[0] or near_xy(context_xy))):
                            return False
                    else:
                        expected = verified[0]["payload"]["expected"]
                        context_xy = [expected[key] for key in ("x", "y")]
                        pose = observed
                        if not (expected["z"] == safe_z and expected["tolerance_mm"] == tolerance
                                and (kind != "park" or context_xy == binding["park_xy_mm"])):
                            return False
                        if kind == "park":
                            prior_view = (measurements(release)["target_after"]["frames"][-1] if fid == "target" else
                                          measurements(_committed_pickup(run_params, fid))["source_frames"][-1])
                            if any(phase["reference"]["meta"][key] != prior_view["meta"][key] for key in
                                   ("objective", "focus_z", "fov_mm", "calibration", "stage_kinematics")):
                                return False
                    if not (all(_number(pose.get(key)) and abs(pose[key]-phase["last_pose"][key]) <= tolerance for key in ("x", "y", "z"))
                            and initial["z"] >= safe_z-tolerance):
                        return False
                def held_xy(pose):
                    return finite_list(context_xy, 2) and all(_number(pose.get(key)) and abs(pose[key]-context_xy[index]) <= tolerance
                                                             for index, key in enumerate(("x", "y")))
                if not (all(_number(initial.get(key)) for key in ("x", "y", "z")) and held_xy(initial) and held_xy(phase["last_pose"])
                        and finite_list(phase.get("holder_xy"), 2) and all(abs(a-b) <= tolerance for a, b in zip(phase["holder_xy"], context_xy, strict=True))):
                    return False
                previous_z, travel = initial["z"], 0.
                for index, command in enumerate(moves):
                    params = command["payload"]["params"]
                    next_id = moves[index+1]["id"] if index+1 < len(moves) else end
                    readbacks = [event for event in local if command["id"] < event["id"] < next_id
                        and event.get("kind") == "lab.frame" and event["payload"].get("device") == "transfer_arm"
                        and event["payload"].get("command") == "move_axis_to" and encoder(event) is not None]
                    if len(readbacks) != 1:
                        return False
                    reading = encoder(readbacks[0])
                    if not (command["payload"].get("command") == "move_axis_to" and params.get("axis") == "z"
                            and _number(params.get("value")) and abs(params["value"]-previous_z) <= profile["max_unobserved_z_mm"]
                            and (previous_z < params["value"] <= safe_z if lift else
                                 safe_z <= params["value"] <= max(previous_z, safe_z)+tolerance)
                            and _number(params.get("speed")) and params["speed"] > 0
                            and all(_number(reading.get(key)) for key in ("x", "y", "z"))
                            and held_xy(reading)
                            and abs(reading["z"]-params["value"]) <= tolerance
                            and abs(reading["z"]-previous_z) <= profile["max_unobserved_z_mm"]
                            and (reading["z"] >= previous_z-tolerance if lift else reading["z"] >= safe_z-tolerance)
                            and any(readbacks[0]["id"] < event["id"] < next_id
                                and _mapping(event["payload"].get("microscope_preview")).get("phase") == name
                                for event in local if event.get("kind") == "lab.frame")
                            and _number(command.get("ts")) and _number(readbacks[0].get("ts"))
                            and 0 <= readbacks[0]["ts"]-command["ts"] <= profile["max_unobserved_s"]):
                        return False
                    travel += abs(reading["z"]-previous_z)
                    previous_z = reading["z"]
                return (abs(previous_z-safe_z) <= tolerance and abs(phase["last_pose"]["z"]-previous_z) <= tolerance
                    and abs(phase["travel_mm"]-travel) <= tolerance and travel <= profile["max_travel_mm"])
            except (KeyError, TypeError, ValueError, IndexError):
                return False

        release_history = {"valid": None, "park_interval": None, "observations": None}
        if relative_release and release_phase:
            def target_release_history(observed_release=observed_release, phase=release_phase, release=release, local=local, state=state, params=params, starts=starts, names=names,
                                       window=window, encoder=encoder, near_xy=near_xy, valid_af=valid_af):
                lo, hi = window(release)
                views = [row for row in local if lo < row["id"] < hi and row.get("kind") == "lab.frame"
                         and _mapping(row["payload"].get("microscope_preview")).get("phase") == release]
                budget = _mapping(phase.get("release_budget"))
                endpoint_start, endpoint_end = budget.get("endpoint_start_event_id"), budget.get("endpoint_end_event_id")
                native = [row for row in local if type(endpoint_start) is int and type(endpoint_end) is int
                          and endpoint_start < row["id"] <= endpoint_end and row.get("kind") == "device.command"
                          and row["payload"].get("device") == "microscope" and row["payload"].get("command") == "capture"]
                result = {"valid": None, "park_interval": None, "observations": len(views)+len(native)}
                own = [row for row in starts if row["payload"].get("name") == release]
                if (not views or len(own) != 1 or own[0]["payload"].get("seq") != names.index(release)
                        or not _number(own[0].get("ts")) or not _number(phase.get("started_at"))
                        or not _number(views[0]["payload"]["microscope_preview"].get("timestamp"))
                        or not own[0]["ts"] <= phase["started_at"] <= views[0]["payload"]["microscope_preview"]["timestamp"]
                        or _mapping(phase.get("reference_event")).get("event_id") != views[0]["id"]
                        or budget and (type(budget.get("endpoint_observations")) is not int
                            or not 0 <= budget["endpoint_observations"] <= 24 or type(phase.get("observations")) is not int
                            or phase["observations"] != len(views)+budget["endpoint_observations"]
                            or budget.get("start_event_id") != views[0]["id"])):
                    result["valid"] = False
                    return result
                if phase.get("complete") is not True or budget.get("endpoint_status") != "observed":
                    return result
                result["valid"] = False
                try:
                    if "park_pending_intent" in phase or "pending_intent" in phase:
                        return result
                    minimum = phase["process_reference"]["minimum_z_mm"]
                    requested = phase["release_reference"]["requested_z_mm"]
                    if observed_release:
                        accepted = next((row for row in local if row["id"] == phase["accepted_release_support"]["event_id"]), {})
                        requested = accepted["payload"]["actual_command_z_mm"]
                    commands = [row for row in local if lo < row["id"] < hi and row.get("kind") == "device.command"
                                and row["payload"].get("device") == "transfer_arm"]
                    transports = phase["park_transport"]
                    if (not commands or not isinstance(transports, list) or len(transports) != 2
                            or {_mapping(item).get("axis") for item in transports} != {"x", "y"}):
                        return result

                    current = _mapping(_mapping(commands[0]["payload"].get("params")).get("from_pose"))
                    if (not all(_number(current.get(key)) for key in ("x", "y", "z"))
                            or not near_xy([current["x"], current["y"]]) or current["z"] < safe_z-tolerance):
                        return result
                    previous_end = commands[0].get("ts")
                    press, clear, press_settled, clear_settled, first_withdrawal, travel = None, None, None, None, None, 0.
                    z_captures = set()
                    used_transports, park_start, park_end = [], None, None
                    heaters = [row for row in local if lo < row["id"] < hi and row.get("kind") == "device.command"
                               and row["payload"].get("device") == "stamp_heater"]
                    off = [row for row in heaters if row["payload"].get("command") == "off" and row["id"] <= endpoint_end]
                    if len(off) != 1 or not _number(off[0].get("ts")):
                        return result
                    off = off[0]
                    for index, command in enumerate(commands):
                        params_ = _mapping(command["payload"].get("params"))
                        axis, target = params_.get("axis"), params_.get("value")
                        next_id = commands[index+1]["id"] if index+1 < len(commands) else hi
                        if command["payload"].get("command") != "move_axis_to" or axis not in ("x", "y", "z") or not _number(target):
                            return result
                        owned_end = next((row["id"] for row in local if row["id"] > command["id"] and row.get("kind") == "device.command"), hi)
                        owned = [row for row in local if command["id"] < row["id"] < owned_end and row.get("kind") == "lab.frame"
                                 and row["payload"].get("device") == "transfer_arm" and row["payload"].get("command") == "move_axis_to"]
                        poses = [encoder(row) for row in owned]
                        if not owned or any(pose == {} for pose in poses):
                            return result
                        samples = [row for row, pose in zip(owned, poses, strict=True) if pose is not None]
                        declaration = _mapping(params_.get("from_pose"))
                        bound = params_.get("duration_bound_s")
                        if (not all(_number(declaration.get(key)) and abs(declaration[key]-current[key]) <= tolerance for key in ("x", "y", "z"))
                                or not _number(bound) or bound < 0 or not samples and abs(target-declaration[axis]) > tolerance):
                            return result
                        current = {key: declaration[key] for key in ("x", "y", "z")}
                        end_pose = encoder(samples[-1]) if samples else dict(current)
                        if axis != "z":
                            canonical = [row for row in owned if row["payload"].get("manipulator")]
                            if not canonical:
                                return result
                            end_pose = {key: canonical[-1]["payload"]["manipulator"][key] for key in ("x", "y", "z")}
                        if (not _number(previous_end) or not _number(command.get("ts")) or command["ts"] < previous_end
                                or any(not _number(row.get("ts")) for row in owned)
                                or any(a["ts"] > b["ts"] for a, b in zip(owned, owned[1:], strict=False))
                                or not all(command["ts"] <= row["ts"] for row in owned)):
                            return result
                        for sample in samples:
                            pose = encoder(sample)
                            if (pose["z"] < minimum or not min(current[axis], target)-tolerance <= pose[axis] <= max(current[axis], target)+tolerance
                                    or any(abs(pose[key]-current[key]) > tolerance for key in ("x", "y", "z") if key != axis)):
                                return result
                        if abs(end_pose[axis]-target) > tolerance:
                            return result
                        if axis == "z":
                            settled = next((row for row in views if command["id"] < row["id"] < next_id), None)
                            if (target < minimum or abs(target-current["z"]) > profile["max_unobserved_z_mm"]
                                    or not settled or owned[-1]["id"] >= settled["id"] or bound > profile["max_unobserved_s"]
                                    or not _number(settled.get("ts"))
                                    or not 0 <= settled["ts"]-command["ts"] <= profile["max_unobserved_s"]):
                                return result
                            z_captures.add(settled["id"])
                            if command["id"] > off["id"]:
                                if clear is None or current["z"] < safe_z-tolerance or abs(target-safe_z) > tolerance:
                                    return result
                            elif press is None:
                                if target >= current["z"] or target < requested-1e-8:
                                    return result
                                if abs(target-requested) <= 1e-8:
                                    press = command
                                    press_settled = settled
                                    press_pose = dict(end_pose)
                            else:
                                if target <= current["z"] or target > safe_z+tolerance:
                                    return result
                                first_withdrawal = command if first_withdrawal is None else first_withdrawal
                                if abs(end_pose["z"]-safe_z) <= tolerance:
                                    clear = samples[-1]
                                    clear_settled = settled
                            travel += abs(end_pose["z"]-current["z"])
                        else:
                            if (press is None or clear is None or first_withdrawal is None or not clear_settled["id"] < off["id"] < command["id"]
                                    or current["z"] < safe_z-tolerance or target != binding["park_xy_mm"][("x", "y").index(axis)]):
                                return result
                            proof = transports[len(used_transports)] if len(used_transports) < len(transports) else {}
                            event_proof = proof["command_event"]
                            end_id, count = proof["encoder_end_event_id"], proof["encoder_event_count"]
                            duration, start, end = proof["duration_bound_s"], proof["started_at"], proof["ended_at"]
                            if not all(_number(value) for value in (duration, start, end)) or duration < 0 or end < start:
                                return result
                            roundoff = (len(owned)+1)*max(math.ulp(command["ts"]), math.ulp(owned[-1]["ts"]))
                            stream = [{"event_id": row["id"], "timestamp": row["ts"],
                                       "pose": {key: row["payload"]["manipulator"][key] for key in ("x", "y", "z")}
                                           if row["payload"].get("manipulator") else None} for row in owned]
                            if (proof["axis"] != ("x", "y")[len(used_transports)] or proof["axis"] != axis
                                    or params_.get("speed") is not None or proof["requested_mm"] != target or proof["from_pose"] != declaration
                                    or proof["actual_pose"] != end_pose or event_proof["event_id"] != command["id"]
                                    or type(event_proof["event_id"]) is not int or not sha(event_proof["event_sha256"])
                                    or event_proof["event_sha256"] != evidence_digest(command["payload"])
                                    or duration != bound or start != command["ts"] or end != owned[-1]["ts"]
                                    or type(end_id) is not int or end_id != owned[-1]["id"]
                                    or type(count) is not int or count != len(owned)
                                    or not sha(proof["encoder_event_sha256"]) or proof["encoder_event_sha256"] != evidence_digest(stream)
                                    or proof["clock_roundoff_s"] != roundoff
                                    or not clear_settled["ts"] <= off["ts"] <= start
                                    or used_transports and start < used_transports[-1]["ended_at"]
                                    or any(abs(proof["actual_pose"][key]-proof["from_pose"][key]) > tolerance for key in ("x", "y", "z") if key != axis)
                                    or end-start > duration+roundoff):
                                return result
                            used_transports.append(proof)
                            park_start = start if park_start is None else park_start
                            park_end = end
                        current = dict(end_pose)
                        previous_end = owned[-1]["ts"]
                    if press is None or clear is None or not clear_settled["id"] < off["id"] or used_transports != transports:
                        return result
                    targets = [row for row in heaters if row["payload"].get("command") == "set_target"]
                    if (not any(row["id"] < commands[0]["id"] for row in targets)
                            or not any(press_settled["id"] < row["id"] < first_withdrawal["id"] for row in targets)
                            or any(not (row["id"] < commands[0]["id"] or press_settled["id"] < row["id"] < first_withdrawal["id"])
                                   or not _number(_mapping(row["payload"].get("params")).get("celsius")) for row in targets)):
                        return result
                    entry = params.get("drop_temp_c", 155.) if params.get("stamp_film") == "mica" else 155.
                    release_c = params["melt_temp_c"] if params.get("stamp_film") == "mica" else 170.
                    before_press = [row for row in targets if row["id"] < commands[0]["id"]]
                    after_press = [row for row in targets if press_settled["id"] < row["id"] < first_withdrawal["id"]]
                    if (not _number(entry) or not _number(release_c)
                            or before_press[-1]["payload"]["params"]["celsius"] != entry+profile["release_setpoint_headroom_c"]
                            or after_press[-1]["payload"]["params"]["celsius"] != release_c+profile["release_setpoint_headroom_c"]
                            or not _number(phase.get("peak_c"))
                            or not (phase["peak_c"] >= release_c-.25 if params.get("stamp_film") == "mica" else phase["peak_c"] > release_c)
                            or phase["withdrawal"]["requested_sim_target_mm"] != safe_z
                            or any(not _number(phase["withdrawal"]["from_readback"].get(key))
                                or abs(phase["withdrawal"]["from_readback"][key]-press_pose[key]) > tolerance
                                or not _number(phase["withdrawal"]["actual_readback"].get(key))
                                or abs(phase["withdrawal"]["actual_readback"][key]-encoder(clear)[key]) > tolerance for key in ("x", "y", "z"))):
                        return result
                    park = phase["park"]
                    requested_pose = dict(zip(("x", "y", "z"), [*binding["park_xy_mm"], safe_z], strict=True))
                    if (park["requested_pose"] != requested_pose or any(not _number(park["actual_holder_pose"].get(key))
                            or abs(park["actual_holder_pose"][key]-current[key]) > tolerance for key in ("x", "y", "z"))
                            or any(abs(current[key]-requested_pose[key]) > tolerance for key in requested_pose)
                            or not _number(park["completed_at"]) or not park_end <= park["completed_at"]
                            or phase["outputs_off"] is not True or abs(phase["travel_mm"]-travel) > tolerance
                            or not 0 <= travel <= profile["max_travel_mm"]):
                        return result
                    if not (type(endpoint_start) is int and type(endpoint_end) is int and off["id"] < endpoint_start < endpoint_end < hi
                            and any(row["id"] == endpoint_start for row in local) and len(native) == 24
                            and park["completed_at"] <= native[0]["ts"]):
                        return result
                    focus_events = [row for row in local if endpoint_start < row["id"] < endpoint_end and row.get("kind") == "microscope.focus"]
                    focus_moves = [row for row in local if endpoint_start < row["id"] < endpoint_end and row.get("kind") == "device.command"
                                   and row["payload"].get("device") == "microscope" and row["payload"].get("command") == "set_focus"]
                    before = state["target_reference"]["focus_reference"]
                    seed = before["focus_binding"]["observation"]["final_focus_readback_z_mm"]
                    focus_profile = binding["endpoint_focus_profile"]
                    points = [focus_profile[key] for key in ("coarse_points", "fine_points")]
                    if points != [9, 9] or not all(type(value) is int for value in points):
                        return result
                    margin = profile["post_phase_focus_half_range_mm"]*(1+2/points[0])
                    if (len(focus_events) != 1 or not focus_moves or sum(points)+6 != 24
                            or not all(type(value) is int and value >= 3 for value in points)
                            or _mapping(focus_moves[0]["payload"].get("params")).get("z") != seed
                            or not 0 < profile["post_phase_focus_half_range_mm"] <= .08):
                        return result
                    af = focus_events[0]["payload"]
                    if (not valid_af(af) or len(af["sampled_passes"]) != 2
                            or [len(part["samples"]) for part in af["sampled_passes"]] != points
                            or af["holder_pose"] != park["actual_holder_pose"]
                            or any(not _number(_mapping(row["payload"].get("params")).get("z"))
                                or abs(row["payload"]["params"]["z"]-seed) > margin+1e-12 for row in focus_moves)):
                        return result
                    objectives = [row for row in local if endpoint_start < row["id"] < endpoint_end and row.get("kind") == "device.command"
                                  and row["payload"].get("device") == "microscope" and row["payload"].get("command") == "set_objective"]
                    if not objectives or objectives[0]["id"] >= native[0]["id"] or any(_mapping(row["payload"].get("params")).get("magnification") != 20 for row in objectives):
                        return result
                    sample_captures = native[1:10]+native[11:20]
                    samples = [sample for part in af["sampled_passes"] for sample in part["samples"]]
                    if not native[20]["id"] < focus_events[0]["id"] < native[21]["id"]:
                        return result
                    for sample, command in zip(samples, sample_captures, strict=True):
                        move = next(row for row in reversed(focus_moves) if row["id"] < command["id"])
                        following = next(row for row in native if row["id"] > command["id"])
                        if (sample["requested_focus_z_mm"] != move["payload"]["params"]["z"]
                                or not sha(sample["image_sha256"]) or not _number(sample["captured_at"])
                                or not command["ts"] <= sample["captured_at"] <= following["ts"]):
                            return result
                    endpoint_views = [row for row in local if endpoint_start < row["id"] <= endpoint_end and row.get("kind") == "lab.frame"
                                      and _mapping(row["payload"].get("microscope_preview")).get("phase") == release+"_target_after"]
                    if len(endpoint_views) != 3 or endpoint_views[-1]["id"] != endpoint_end or not focus_events[0]["id"] < endpoint_views[0]["id"]:
                        return result
                    camera_times = [row["ts"] for row in native]
                    camera_times += [sample["captured_at"] for part in af["sampled_passes"] for sample in part["samples"]]
                    camera_times += [views[-1]["payload"]["microscope_preview"]["timestamp"], af["captured_at"],
                                     *[row["payload"]["microscope_preview"]["timestamp"] for row in endpoint_views]]
                    if any(not _number(value) for value in camera_times) or any(b-a > profile["max_unobserved_s"] for a, b in zip(sorted(camera_times), sorted(camera_times)[1:], strict=False)):
                        return result
                    used = len(views)+len(native)
                    ordinary_commands = [row for row in local if phase["reference_event"]["event_id"] < row["id"] <= endpoint_end
                        and not endpoint_start < row["id"] <= endpoint_end and row.get("kind") == "device.command"
                        and row["payload"].get("device") == "microscope" and row["payload"].get("command") == "capture"]
                    thermal_count = sum(row["id"] < off["id"] and row["id"] not in z_captures
                                        and row["id"] != phase["reference_event"]["event_id"] for row in views)
                    park_count = sum(row["id"] > off["id"] for row in views)
                    if (budget["stage"] != "observed" or budget["endpoint_status"] != "observed" or budget["endpoint_limit"] != 24
                            or budget["endpoint_observations"] != len(native) or budget["park_complete"] is not True
                            or type(budget["park_observations"]) is not int or budget["park_observations"] != park_count or park_count > 2
                            or type(budget["thermal_observations"]) is not int or budget["thermal_observations"] != thermal_count
                            or len(ordinary_commands) != len(views)-1 or any(not sha(row["payload"]["microscope_preview"].get("image_sha256")) for row in views)
                            or budget["start_event_id"] != phase["reference_event"]["event_id"]
                            or type(phase["observations"]) is not int or phase["observations"] != used <= profile["max_observations"]
                            or not _number(budget["checked_at"]) or not phase["observed_at"] <= budget["checked_at"]
                            or not 0 <= budget["checked_at"]-phase["started_at"] <= profile["max_total_s"]):
                        return result
                    obs, seconds, distance, allowance = (budget[key] for key in ("reserved_observations", "reserved_seconds", "reserved_z_travel_mm", "thermal_allowance_s"))
                    remaining_time = profile["max_total_s"]-(budget["checked_at"]-phase["started_at"])
                    if (type(obs) is not int or obs != 0 or seconds != 0 or distance != 0
                            or not all(_number(value) and value >= 0 for value in (seconds, distance, allowance))
                            or allowance != min(remaining_time, (profile["max_observations"]-used)*(profile["max_unobserved_s"]-profile["capture_reserve_s"]))):
                        return result
                    result.update(valid=True, park_interval=(park_start, park_end), observations=used)
                    return result
                except (KeyError, TypeError, ValueError, IndexError, OverflowError, StopIteration):
                    return result
            release_history = target_release_history()
            check(prefix + "release_history", release_history["valid"])

        if not v2:
            check(prefix + "phase_set", None if not phases or run.get("status") != "succeeded" and set(phases) < set(expected_phases)
                  else set(phases) == set(expected_phases))
        for name in [*expected_phases, *lifts, *transits]:
            phase = _mapping(phases.get(name))
            parent = lifts.get(name, transits[name][0] if name in transits else name)
            lo, hi = window(parent)
            capture_events = [e for e in local if e.get("kind") == "lab.frame"
                              and _mapping(e["payload"].get("microscope_preview")).get("phase") == name]
            captures = [_mapping(e["payload"].get("microscope_preview")) for e in capture_events]
            times = [capture.get("timestamp") for capture in captures]
            end_times = [e.get("ts") for e in finishes if e["payload"].get("name") == parent]
            phase_done = (phase.get("complete") is True or name in transits and bool(measurements(parent))
                          and any(e["payload"].get("name") == parent and e["payload"].get("status") == "done" for e in finishes))
            if relative_release and name == release:
                phase_done = _mapping(phase.get("release_budget")).get("endpoint_status") == "observed"
            check(prefix + name + ":bounds", None if not phase or not captures or name in open_tail_steps or run.get("status") != "succeeded" and not phase_done else
                  phase.get("name") == name and phase.get("profile") == profile["id"] and phase_done
                  and "pending_intent" not in phase and not phase.get("held_reason") and phase.get("difference_active") is False
                  and type(phase.get("observations")) is int
                  and phase["observations"] == (release_history["observations"] if relative_release and name == release else len(captures)) <= profile["max_observations"]
                  and _number(phase.get("travel_mm")) and 0 <= phase["travel_mm"] <= profile["max_travel_mm"]
                  and _number(phase.get("started_at")) and all(_number(t) for t in times) and times == sorted(times)
                  and 0 <= times[-1]-phase["started_at"] <= profile["max_total_s"]
                  and len(end_times) == 1 and _number(end_times[0]) and 0 <= end_times[0]-phase["started_at"] <= profile["max_total_s"]
                  and all(_number(event.get("ts")) and 0 <= event["ts"]-capture["timestamp"] <= profile["max_unobserved_s"]
                          for event, capture in zip(capture_events, captures, strict=True))
                  and all(b-a <= profile["max_unobserved_s"] or relative_release and name == release and release_history["valid"] is True
                      and release_history["park_interval"] and a <= release_history["park_interval"][1] and b >= release_history["park_interval"][0]
                      and max(0, release_history["park_interval"][0]-a) <= profile["max_unobserved_s"]
                      and max(0, b-release_history["park_interval"][1]) <= profile["max_unobserved_s"]
                      for a, b in zip(times, times[1:], strict=False)))
            refs = [_mapping(phase.get(k)) for k in ("reference", "last_frame")]
            objective = _mapping(refs[0].get("meta")).get("objective") if name in transits else 5
            reference_captures = captures
            motion_capture_events = capture_events
            if relative_release and name == release:
                motion_capture_events = capture_events+[e for e in local if e.get("kind") == "lab.frame"
                    and _mapping(e["payload"].get("microscope_preview")).get("phase") == release+"_target_after"]
                reference_captures = [_mapping(e["payload"].get("microscope_preview")) for e in motion_capture_events]
            check(prefix + name + ":references", None if not all(refs) or relative_release and name == release and not phase_done else objective in (5, 20)
                  and all(reference(ref, 20 if relative_release and name == release and index == 1 else objective) for index, ref in enumerate(refs))
                  and phase.get("observed_at") == refs[-1].get("captured_at")
                  and all(any(capture.get("image") == ref.get("image") and capture.get("timestamp") == ref.get("captured_at")
                      and (not v2 or capture.get("image_sha256") == ref.get("sha256"))
                      and capture.get("calibration") == _mapping(ref.get("meta")).get("calibration") for capture in reference_captures) for ref in refs)
                  and all(lo < event["id"] < hi and capture.get("immutable") is True and capture.get("verification") is False
                      and capture.get("calibration") == _mapping(refs[0].get("meta")).get("calibration")
                      for event, capture in zip(capture_events, captures, strict=True)))
            if v2:
                proof = _mapping(phase.get("reference_event"))
                event = historical(proof.get("event_id"), "lab.frame", parent)
                preview = _mapping(_mapping(event.get("payload")).get("microscope_preview"))
                check(prefix + name + ":reference_event", None if not phase or not proof else
                    bool(event) and sha(proof.get("event_sha256")) and evidence_digest(event["payload"]) == proof["event_sha256"]
                    and bool(capture_events) and event["id"] == capture_events[0]["id"]
                    and preview.get("image") == refs[0].get("image") and preview.get("image_sha256") == refs[0].get("sha256")
                    and preview.get("timestamp") == refs[0].get("captured_at") and captured(name, refs[0], step_name=parent))
            intent = _mapping(phase.get("last_intent"))
            motion_lo, motion_hi = lo, hi
            if name in transits:
                baseline_id = _mapping(phase.get("reference_event")).get("event_id")
                motion_lo = baseline_id if type(baseline_id) is int and baseline_id > 0 else math.inf
                motion_hi = max([e["id"] for e, capture in zip(capture_events, captures, strict=True)
                                 if capture.get("image") == _mapping(phase.get("last_frame")).get("image")], default=-math.inf)+1
            commands = [e for e in local if motion_lo < e["id"] < motion_hi and e.get("kind") == "device.command"
                        and e["payload"].get("device") == "transfer_arm" and e["payload"].get("command") == "move_axis_to"]
            check(prefix + name + ":retained_motion", None if not intent else intent.get("axis") == "z"
                  and all(_number(intent.get(k)) for k in ("from_mm", "to_mm", "at", "speed_mm_s", "duration_bound_s", "macro_target_mm"))
                  and 0 < abs(intent["to_mm"]-intent["from_mm"]) <= profile["max_unobserved_z_mm"]
                  and intent["speed_mm_s"] > 0 and _number(_mapping(phase.get("last_pose")).get("z"))
                  and 0 < intent["duration_bound_s"] <= profile["max_unobserved_s"]
                  and abs(_mapping(phase.get("last_pose")).get("z", math.inf)-intent["to_mm"]) <= tolerance
                  and _number(phase.get("observed_at")) and intent["at"] <= phase["observed_at"]
                  and any(_mapping(command["payload"].get("params")).get("axis") == "z"
                      and _mapping(command["payload"].get("params")).get("value") == intent["to_mm"]
                      and any(command["id"] < event["id"] and capture.get("timestamp") == phase["observed_at"]
                              for event, capture in zip(motion_capture_events, reference_captures, strict=True)) for command in commands))
            if name != first:
                lateral = [e for e in local if motion_lo < e["id"] < motion_hi and e.get("kind") == "device.command"
                           and e["payload"].get("device") == "transfer_arm" and e["payload"].get("command") == "move_axis_to"
                           and _mapping(e["payload"].get("params")).get("axis") in ("x", "y")]
                if name in transits:
                    observed = measurements(parent)
                    pose = _mapping(observed.get("actual_holder_pose")) if transits[name][1] == "initialize" else observed
                    contextual = finite_list(phase.get("holder_xy"), 2) and all(_number(pose.get(key))
                        and abs(phase["holder_xy"][index]-pose[key]) <= tolerance for index, key in enumerate(("x", "y")))
                else:
                    contextual = near_xy(phase.get("holder_xy"))
                if relative_release and name == release and release_history["valid"] is True:
                    contextual = phase.get("holder_xy") == [phase["park"]["actual_holder_pose"][axis] for axis in ("x", "y")]
                check(prefix + name + ":working_xy", None if not phase or relative_release and name == release and not phase_done else contextual and
                    (not lateral or relative_release and name == release and release_history["valid"] is True))
            if name in lifts:
                check(prefix + name + ":safety_lift", auxiliary_motion(name, parent))
            elif name in transits:
                check(prefix + name + ":transit", auxiliary_motion(name, parent))
        if v2:
            admitted_lifts = {name for name in lifts if all(checks.get(prefix+name+suffix) is True for suffix in
                (":bounds", ":references", ":reference_event", ":retained_motion", ":working_xy", ":safety_lift"))}
            admitted_transits = {name for name in transits if all(checks.get(prefix+name+suffix) is True for suffix in
                (":bounds", ":references", ":reference_event", ":retained_motion", ":working_xy", ":transit"))}
            normal = set(phases)-admitted_lifts-admitted_transits-discovery_phases
            check(prefix + "phase_set", None if not phases or run.get("status") != "succeeded" and normal < set(expected_phases)
                  else normal == set(expected_phases))
        for index, fid in enumerate([*layers, "target"]):
            name = f"v1_initialize_{fid}"
            init = _mapping(_mapping(state.get("initializations")).get(name))
            observed = measurements(name)
            check(prefix + name, None if not init or not observed else init.get("applied") is True
                  and init.get("stamp_id") == observed.get("stamp_id") == stamp and init.get("profile") == observed.get("profile") == profile["id"]
                  and observed.get("objective") == 5 and _number(init.get("substrate_focus_mm")) and _number(observed.get("focus_mm"))
                  and abs(observed["focus_mm"]-init["substrate_focus_mm"]-profile["focus_offset_mm"]) <= 1e-8
                  and (index == 0 or near_xy([_mapping(observed.get("actual_holder_pose")).get(k) for k in ("x", "y")])))
            if fid == "target":
                continue
            source = (_mapping(discovery.get("original_reference")) if discovery and fid == layers[0]
                      else _mapping(_mapping(state.get("source_references")).get(fid)))
            identity = _mapping(source.get("source_identity"))
            refind = _mapping(measurements(f"refind_{fid}").get("source_identity"))
            check(prefix + fid + ":source", None if not source or not refind or not all(identity.get(k) for k in ("original", "current", "post_shift_capture")) else reference(source, 20)
                  and identity.get("source_id") == fid and identity.get("source_chip_id") == _mapping(params.get("flake_chips")).get(fid)
                  and all(refind.get(k) == identity.get(k) for k in ("source_id", "source_chip_id", "original", "current"))
                  and all(reference(identity.get(k), 20) for k in ("original", "current", "post_shift_capture"))
                  and all(_mapping(_mapping(identity["original"]).get("meta")).get(k) == _mapping(_mapping(identity["current"]).get("meta")).get(k)
                          for k in ("objective", "calibration", "focus_plane", "fov_mm"))
                  and _mapping(source.get("meta")).get("calibration") == _mapping(_mapping(identity["original"]).get("meta")).get("calibration")
                  and captured(f"refind_{fid}", source, phase=f"refind_{fid}_source_reference"))
            pickup_phase = _mapping(phases.get(_committed_pickup(run_params, fid)))
            if v2:
                check(prefix + fid + ":native20_focus", focused_reference(source, f"refind_{fid}", f"refind_{fid}_source_reference", parked=True))
                check(prefix + fid + ":source_roi", None if not source else
                    source.get("source_id") == fid and source.get("source_chip_id") == params["flake_chips"][fid]
                    and finite_list(source.get("target_offset_mm"), 2)
                    and finite_list(identity.get("design_shift_chip_mm"), 2)
                    and identity.get("design_shift_chip_mm") == _mapping(params.get("design_shifts_mm")).get(fid, [0., 0.])
                    and all(_mapping(identity.get("post_shift_capture")).get(key) == source.get(key)
                            for key in ("image", "sha256", "captured_at", "meta")))
                if discovery and fid == layers[0]:
                    source = _mapping(state["source_references"][fid])
                check(prefix + fid + ":process_reference", process_reference(fid, source, pickup_phase))
            coverage = _mapping(_mapping(phases.get(_committed_pickup(run_params, fid))).get("coverage"))
            endpoint = [e for e in local if e.get("kind") == "stack.pickup_endpoint" and e["payload"].get("flake_id") == fid]
            observed = measurements(_committed_pickup(run_params, fid))
            check(prefix + fid + ":pickup_endpoint", None if not coverage or not observed or len(endpoint) != 1 else
                  coverage.get("covered") is True and coverage.get("transfer_confirmed") is False
                  and coverage.get("reference") == _mapping(phases.get(_committed_pickup(run_params, fid))).get("reference")
                  and reference(coverage.get("current"), 5)
                  and captured(_committed_pickup(run_params, fid), coverage.get("current"))
                  and observed.get("coverage") == coverage and observed.get("transfer_confirmed") is True
                  and len(endpoint) == 1 and endpoint[0]["payload"].get("stamp_id") == stamp
                  and endpoint[0]["payload"].get("procedure") == binding["procedure_contract"]
                  and _mapping(endpoint[0]["payload"].get("observation")).get("measurements") == observed
                  and window(_committed_pickup(run_params, fid))[0] < endpoint[0]["id"] < window(_committed_pickup(run_params, fid))[1])
            if v2:
                check(prefix + fid + ":source_disappearance", visual_pickup(fid, source, pickup_phase, observed))
                check(prefix + fid + ":endpoint_verified", verified_endpoint(_committed_pickup(run_params, fid), pickup_phase, observed,
                    [_mapping(frame).get("image") for frame in _rows(observed.get("source_frames"))]))
        candidate = _mapping(_mapping(phases.get(first)).get("first_contact_region"))
        check(prefix + "first_contact_receipt", None if not candidate or not measurements(first) else candidate.get("kind") == "observed_first_contact_candidate"
              and candidate.get("found") is True and candidate.get("profile") == profile["id"]
              and candidate.get("reference") == _mapping(phases.get(first)).get("reference")
              and reference(candidate.get("current"), 5) and measurements(first).get("first_contact_region") == candidate
              and captured(first, candidate.get("current"))
              and near_xy([_mapping((placement if discovery else candidate).get("reaim_readback")).get(k) for k in ("x", "y")]))
        if v2:
            lo, hi = window(first)
            facts = [event for event in local if event.get("kind") == "lab.evidence" and lo < event["id"] < hi
                     and event["payload"].get("property") == "first_contact_region" and event["payload"].get("subject") == stamp]
            try:
                fact = facts[0]["payload"] if len(facts) == 1 else {}
                proof = placement["candidate_evidence"]
                current = candidate["current"]
                calibration = current["meta"]["calibration"]
                acquisition = (fact["schema_version"] == 1 and fact["bench"] == "sim" and fact["status"] == "asserted"
                    and historical(proof["event_id"], "lab.evidence", first) == facts[0]
                    and sha(proof["event_sha256"]) and evidence_digest(fact) == proof["event_sha256"]
                    and isinstance(fact["assertion_id"], str) and bool(fact["assertion_id"])
                    and fact["frame"] == "microscope_image" and fact["units"] == "px,mm"
                    and fact["timebase"] == "store_clock" and fact["observed_at"] == current["captured_at"]
                    and fact["value"] == candidate and fact["source"]["kind"] == "synthetic"
                    and fact["source"]["reference"] == f"{rid}:{first}" and fact["source"]["sha256"] == current["sha256"]
                    and fact["calibration_id"] == calibration["id"] and fact["calibration_version"] == calibration["version"]
                    and all(_number(candidate["actual_holder_pose"][axis]) for axis in ("x", "y", "z"))
                    and candidate["actual_holder_pose"]["z"] == placement["candidate_z_mm"]
                    and _number(facts[0].get("ts")) and current["captured_at"] <= facts[0]["ts"])
            except (KeyError, TypeError, ValueError, IndexError):
                acquisition = False
            check(prefix + "first_contact_acquisition", None if not candidate or len(facts) != 1 else acquisition)
        reaim_placement = placement if discovery else candidate
        reaim_phase = "restore_stamp_discovery_source" if discovery else first
        inputs = [_mapping(item) for item in _rows(reaim_placement.get("reaim_inputs"))]
        lo, hi = ((discovery["reaim_start_event_id"], discovery["reaim_end_event_id"]+1)
                  if type(discovery.get("reaim_start_event_id")) is int and type(discovery.get("reaim_end_event_id")) is int
                  else window(reaim_phase))
        lateral = [_mapping(e["payload"].get("params")) for e in local if lo < e["id"] < hi and e.get("kind") == "device.command"
                   and e["payload"].get("device") == "transfer_arm" and e["payload"].get("command") == "move_axis_to"
                   and _mapping(e["payload"].get("params")).get("axis") in ("x", "y")]
        check(prefix + "once_only_actual_reaim", None if not inputs else
              [item.get("axis") for item in inputs] == ["x", "y"] == [command.get("axis") for command in lateral]
              and inputs == _mapping(phases.get(reaim_phase)).get("xy_inputs")
              and inputs[-1].get("after") == reaim_placement.get("reaim_readback")
              and all(command.get("value") == item.get("requested_mm") for command, item in zip(lateral, inputs, strict=True)) and all(
                  all(_number(_mapping(item.get(key)).get(axis)) for key in ("before", "after", "actual_delta_mm") for axis in ("x", "y", "z"))
                  and _number(item.get("requested_mm")) and _number(item.get("readback_at"))
                  and abs(item["after"][item["axis"]]-item["requested_mm"]) <= tolerance
                  and all(abs(item["after"][axis]-item["before"][axis]-item["actual_delta_mm"][axis]) <= 1e-8
                          and (axis == item["axis"] or abs(item["actual_delta_mm"][axis]) <= tolerance) for axis in ("x", "y", "z"))
                  for item in inputs))
        released = measurements(release)
        phase = _mapping(phases.get(release))
        withdrawal_z = _mapping(_mapping(phase.get("withdrawal")).get("actual_readback")).get("z")
        check(prefix + "release_endpoint", None if not released or not phase.get("withdrawal") else released.get("release_confirmed") is True
              and phase.get("outputs_off") is True and _number(phase.get("peak_c"))
              and _number(withdrawal_z) and abs(withdrawal_z-safe_z) <= tolerance)
        if v2:
            before, after = _mapping(state.get("target_reference")), _mapping(released.get("target_after"))
            check(prefix + "target_before", target_view(before, "go_to_target", "go_to_target_before_release", parked=True))
            before_observed = measurements("go_to_target")
            before_proof = _mapping(state.get("target_reference_verified"))
            try:
                journal = historical(before_proof["event_id"], "step.verified", "go_to_target")
                expected_target = journal["payload"]["expected"]
                frames = before["frames"]
                journal_bound = (sha(before_observed.get("target_reference_sha256"))
                    and before_observed["target_reference_sha256"] == evidence_digest(before)
                    and before_observed.get("on_chip") is True
                    and verified_endpoint("go_to_target", state, before_observed, [frame["image"] for frame in frames],
                                          proof_field="target_reference_verified") is True
                    and expected_target.get("on_chip") == params["target_chip_id"]
                    and finite_list(before.get("requested_stage_xy_mm"), 2)
                    and expected_target.get("stage_xy") == [round(value, 4) for value in before["requested_stage_xy_mm"]]
                    and _number(journal.get("ts")) and frames[-1]["captured_at"] <= journal["ts"]
                    and all(any(event["id"] < journal["id"] and preview.get("image") == frame["image"]
                        and preview.get("timestamp") == frame["captured_at"]
                        for event in local if event.get("kind") == "lab.frame"
                        for preview in [_mapping(event["payload"].get("microscope_preview"))]) for frame in frames))
            except (KeyError, TypeError, ValueError, IndexError):
                journal_bound = False
            check(prefix + "target_before_journal", None if not before or not before_observed or not before_proof else journal_bound)
            if relative_release:
                check(prefix + "release_policy_binding", None if not binding.get("release_policy") else
                    params.get("release_policy") == binding["release_policy"] == release_policy
                    and (not phase or phase.get("release_policy") == release_policy)
                    and all(profile.get(key) == value for key, value in (("max_observations", 600), ("max_total_s", 900),
                        ("max_travel_mm", 8.), ("max_unobserved_s", 2.), ("max_unobserved_z_mm", .025), ("capture_reserve_s", .05)))
                    and all(_mapping(row["payload"].get("params")).get("release_policy") == release_policy for row in submissions))
                check(prefix + "release_process_reference", None if not phase or not before else
                    process_reference(params["target_chip_id"], _mapping(before.get("focus_reference")), phase, step_name=release))
                witness_valid, witness_intent = None, None
                if witnessed_release and phase:
                    def pickup_release_witness(plan, *, local=local, events=events, params=params, state=state, phases=phases,
                                               starts=starts, finishes=finishes, names=names, release=release, layers=layers,
                                               stamp=stamp, rid=rid, prefix=prefix, placement=placement, first=first, run=run, phase=phase, release_policy=release_policy, discovery=discovery, run_params=run_params):
                        try:
                            witness = plan["witness"]
                            load = witness["load"]
                            own_release = [row for row in starts if row["payload"].get("name") == release]
                            cutoff = own_release[0]["id"]

                            selection = ("flake_ids", "flake_chips", "materials", "stamp_id", "stamp_film", "procedure_contract", "transfer_profile", "release_policy")

                            def projected(row, projection):
                                payload = row["payload"]
                                if projection == "witness-submission-selection-v1":
                                    if not isinstance(payload["params"], dict):
                                        raise ValueError("Malformed original witness submission parameters")
                                    return {"flow": payload["flow"], "params": {key: payload["params"].get(key) for key in selection}}
                                if projection == "witness-stamp-lineage-v1":
                                    props = payload["props"]
                                    if not isinstance(props, dict):
                                        raise ValueError("Malformed original stamp properties")
                                    reports, history, support = (props.get(key) for key in ("film_reports", "support_history", "support"))
                                    if (reports is not None and (not isinstance(reports, list) or any(not isinstance(item, dict) for item in reports))
                                            or history is not None and (not isinstance(history, list) or any(not isinstance(item, dict) for item in history))
                                            or support is not None and not isinstance(support, dict)
                                            or not isinstance(props.get("stack", []), list)):
                                        raise ValueError("Malformed original stamp lineage")
                                    return {**{key: payload.get(key) for key in ("id", "kind", "location", "status")}, "props": {
                                        "film": props.get("film"), "support": support, "support_history": history,
                                        "film_reports": None if reports is None else [{key: value for key, value in item.items() if key not in ("what", "images", "notes")} for item in reports],
                                        "stack": props.get("stack", [])}}
                                if projection == "witness-pickup-endpoint-v1":
                                    return {**{key: payload.get(key) for key in ("stamp_id", "flake_id", "procedure")},
                                            "observation": {key: payload["observation"].get(key) for key in ("verdict", "measurements", "expected")}}
                                if projection == "witness-manipulator-encoder-v1":
                                    return {"device": payload["device"], "command": payload["command"], "timestamp": row["ts"],
                                            "manipulator": {key: payload["manipulator"][key] for key in ("x", "y", "z")}}
                                raise ValueError("Unknown witness authority projection")

                            def bound(evidence_ref, kind, rows=local, *, verified=False, projection=None):
                                event_id = evidence_ref["event_id"]
                                found = [row for row in rows if type(event_id) is int and event_id > 0 and row["id"] == event_id and row.get("kind") == kind]
                                if len(found) != 1:
                                    raise ValueError("Missing original witness event")
                                row = found[0]
                                if projection == "witness-stamp-lineage-v1" and row.get("run_id") is not None:
                                    raise ValueError("Original stamp lineage is not a global ledger row")
                                if (evidence_ref.get("projection") != projection or projection is None and "projection" in evidence_ref):
                                    raise ValueError("Wrong witness authority projection")
                                body = (projected(row, projection) if projection is not None else
                                        {key: row["payload"][key] for key in ("seq", "name", "verdict", "measurements", "expected")}
                                        if verified else row["payload"])
                                key = "observation_sha256" if verified else "event_sha256"
                                if not sha(evidence_ref[key]) or evidence_ref[key] != evidence_digest(body):
                                    raise ValueError("Changed original witness event")
                                return row

                            def original(name):
                                begun = [row for row in starts if row["id"] < cutoff and row["payload"].get("name") == name]
                                ended = [row for row in finishes if row["id"] < cutoff and row["payload"].get("name") == name]
                                if (len(begun) != 1 or len(ended) != 1 or not begun[0]["id"] < ended[0]["id"]
                                        or ended[0]["payload"].get("status") != "done"
                                        or any(row["payload"].get("seq") != names.index(name) for row in [*begun, *ended])):
                                    raise ValueError("Missing original completed witness Step")
                                return begun[0], ended[0]

                            if bound(load["release_start"], "step.started") != own_release[0] or own_release[0]["payload"].get("seq") != names.index(release):
                                return False, None
                            submission = bound(load["submission"], "job.submitted", projection="witness-submission-selection-v1")
                            frozen = submission["payload"]["params"]
                            if (submission["payload"].get("flow") != run["flow"]
                                    or any(frozen.get(key) != params.get(key) for key in selection)
                                    or frozen.get("release_policy") != release_policy):
                                return False, None
                            for change in local:
                                if not submission["id"] < change["id"] < cutoff or change.get("kind") != "job.params_changed":
                                    continue
                                path, value = change["payload"]["path"], change["payload"]["value"]
                                if not isinstance(path, list):
                                    return False, None
                                if not path:
                                    if not isinstance(value, dict) or any(value.get(key) != frozen.get(key) for key in selection):
                                        return False, None
                                elif path[0] in selection:
                                    return False, None
                            selected = layers[-1]
                            name = _committed_pickup(run_params, selected)
                            if witness["flake_id"] != selected or witness["phase"] != name or type(witness["seq"]) is not int or witness["seq"] != names.index(name):
                                return False, None
                            required = []
                            verified_rows = []
                            pickup_windows = []
                            for fid in layers:
                                pickup_name = _committed_pickup(run_params, fid)  # noqa: F811
                                begun, ended = original(pickup_name)
                                verified = [row for row in local if begun["id"] < row["id"] < ended["id"] and row.get("kind") == "step.verified"
                                            and row["payload"].get("name") == pickup_name]
                                endpoints = [row for row in local if begun["id"] < row["id"] < ended["id"] and row.get("kind") == "stack.pickup_endpoint"
                                             and row["payload"].get("flake_id") == fid]
                                if (len(verified) != 1 or len(endpoints) != 1 or verified[0]["payload"].get("verdict") != "ok"
                                        or verified[0]["payload"].get("seq") != names.index(pickup_name)
                                        or not verified[0]["id"] < endpoints[0]["id"]
                                        or endpoints[0]["payload"].get("stamp_id") != stamp
                                        or endpoints[0]["payload"].get("procedure") != params["procedure_contract"]
                                        or _mapping(endpoints[0]["payload"].get("observation")).get("verdict") != "ok"
                                        or _mapping(endpoints[0]["payload"].get("observation")).get("measurements") != verified[0]["payload"].get("measurements")
                                        or not all(checks.get(prefix+fid+suffix) is True for suffix in
                                            (":source", ":source_roi", ":native20_focus", ":process_reference", ":pickup_endpoint", ":source_disappearance", ":endpoint_verified"))):
                                    return False, None
                                body = {key: verified[0]["payload"][key] for key in ("seq", "name", "verdict", "measurements", "expected")}
                                required.append({"flake_id": fid,
                                    "verified": {"event_id": verified[0]["id"], "observation_sha256": evidence_digest(body)},
                                    "pickup_endpoint": {"event_id": endpoints[0]["id"], "event_sha256": evidence_digest(projected(endpoints[0], "witness-pickup-endpoint-v1")),
                                                        "projection": "witness-pickup-endpoint-v1"}})
                                verified_rows.append(verified[0])
                                pickup_windows.append((begun, ended))
                            if witness["required_pickup_verified"] != required or witness["verified"] != required[-1]["verified"]:
                                return False, None
                            begun, ended = original(name)
                            if bound(witness["pickup_start"], "step.started") != begun or bound(witness["pickup_finish"], "step.finished") != ended:
                                return False, None
                            load_start, load_end = original("load_stamp")
                            grip_start, grip_end = original("grip_stamp")
                            handoff = bound(load["handoff_verified"], "step.verified", verified=True)
                            grip_observation = bound(load["grip_observation"], "step.verified")
                            vacuum = bound(load["vacuum_command"], "device.command")
                            stamp_event = bound(load["stamp_event"], "sample.updated", events, projection="witness-stamp-lineage-v1")
                            handoff_event = bound(load["handoff_event"], "sample.updated", events, projection="witness-stamp-lineage-v1")
                            if (bound(load["grip_start"], "step.started") != grip_start or bound(load["grip_finish"], "step.finished") != grip_end
                                    or not submission["id"] < load_start["id"] < handoff["id"] < handoff_event["id"] < load_end["id"] < grip_start["id"]
                                    or handoff["payload"].get("name") != "load_stamp" or handoff["payload"].get("seq") != names.index("load_stamp")
                                    or handoff["payload"].get("verdict") != "ok"
                                    or handoff["payload"]["expected"].get("sample") != stamp or handoff["payload"]["expected"].get("at") != "stamp_handoff"
                                    or handoff["payload"]["measurements"].get("found_at") != "stamp_handoff"
                                    or not grip_start["id"] < vacuum["id"] < grip_observation["id"] < stamp_event["id"] < grip_end["id"]
                                    or grip_observation["payload"].get("name") != "grip_stamp"
                                    or grip_observation["payload"].get("seq") != names.index("grip_stamp")
                                    or grip_observation["payload"].get("verdict") != "not_verifiable"
                                    or not grip_end["id"] < pickup_windows[0][0]["id"]
                                    or any(a[1]["id"] >= b[0]["id"] for a, b in zip(pickup_windows, pickup_windows[1:], strict=False))
                                    or vacuum["payload"].get("device") != "transfer_arm_vacuum" or vacuum["payload"].get("command") != "set"
                                    or _mapping(vacuum["payload"].get("params")).get("on") is not True):
                                return False, None
                            vacuum_history = [row for row in events if grip_start["id"] < row["id"] < cutoff and row.get("kind") == "device.command"
                                              and row["payload"].get("device") == "transfer_arm_vacuum"]
                            if vacuum_history != [vacuum] or load["film"] != frozen["stamp_film"] or load["film"] not in ("PC", "mica"):
                                return False, None

                            def film(payload):
                                props = payload["props"]
                                support = props.get("support")
                                return support["film_type"] if support is not None else props.get("film")

                            def lineage(payload):
                                props = projected({"payload": payload}, "witness-stamp-lineage-v1")["props"]
                                return {key: props[key] for key in ("film", "support", "support_history", "film_reports")}

                            initial = stamp_event["payload"]
                            handoff_sample = handoff_event["payload"]
                            if (initial.get("id") != stamp or initial.get("kind") != "stamp" or initial.get("location") != "transfer_arm"
                                    or initial.get("status") != "in_use" or initial["props"].get("stack", []) != []
                                    or film(initial) != load["film"] or handoff_sample.get("id") != stamp or handoff_sample.get("kind") != "stamp"
                                    or handoff_sample.get("location") != "stamp_handoff" or handoff_sample.get("status") != "fresh"
                                    or handoff_sample["props"].get("stack", []) != [] or lineage(handoff_sample) != lineage(initial)):
                                return False, None
                            length = 0
                            for row in events:
                                if not handoff_event["id"] <= row["id"] < cutoff:
                                    continue
                                payload = row["payload"]
                                if row.get("kind") in ("sample.removed", "stamp.film_replaced") and payload.get("id", payload.get("stamp_id")) == stamp:
                                    return False, None
                                if row.get("kind") != "sample.updated":
                                    continue
                                if payload.get("id") != stamp:
                                    if payload.get("kind") == "stamp" and payload.get("location") == "transfer_arm":
                                        return False, None
                                    continue
                                stack = projected(row, "witness-stamp-lineage-v1")["props"]["stack"]
                                mounted = row["id"] >= stamp_event["id"]
                                if (payload.get("kind") != "stamp" or lineage(payload) != lineage(initial) or film(payload) != load["film"]
                                        or payload["props"].get("location_uncertain") is True
                                        or payload.get("location") != ("transfer_arm" if mounted else "stamp_handoff")
                                        or payload.get("status") != ("in_use" if mounted else "fresh")
                                        or not isinstance(stack, list) or stack != layers[:len(stack)] or not length <= len(stack) <= length+1
                                        or not mounted and stack or stack and not verified_rows[len(stack)-1]["id"] < row["id"]):
                                    return False, None
                                length = len(stack)
                            if length != len(layers) or checks.get(prefix+"v1_initialize_target") is not True:
                                return False, None
                            source = state["source_references"][selected]
                            restored_anchor = state["source_references"][layers[0]]
                            anchor = discovery["focus_reference"] if discovery else restored_anchor
                            if not grip_end["id"] < anchor["focus_binding"]["event_id"] < begun["id"]:
                                return False, None
                            a, b = (record["focus_binding"]["observation"] for record in (anchor, source))
                            axis = a["focus_axis_calibration"]
                            low0, high0 = a["focus_interval_mm"]
                            low, high = b["focus_interval_mm"]
                            scale, error = axis["scale_mm_per_focus_mm"], 2*axis["mapping_error_mm"]
                            delta = [0., 0.] if not discovery and selected == layers[0] else [scale*(low-high0)-error, scale*(high-low0)+error]
                            interval = [placement["candidate_z_mm"]+value for value in delta]
                            process = {"profile": profile["id"], "mapping": height_mapping, "digest_contract": digest_contract,
                                "source_id": selected, "anchor_source_id": layers[0], "anchor_focus_binding_sha256": evidence_digest(restored_anchor["focus_binding"]),
                                "current_focus_binding_sha256": evidence_digest(source["focus_binding"]), "first_candidate_z_mm": placement["candidate_z_mm"],
                                "delta_interval_mm": delta, "reference_interval_mm": interval, "max_press_mm": .2, "minimum_z_mm": interval[1]-.2,
                                "scope": "Optical process reference only; not contact/gap"}
                            pickup = phases[name]
                            observed = verified_rows[-1]["payload"]["measurements"]
                            coverage = observed["coverage"]
                            if (pickup["process_reference"] != process or pickup["coverage"] != coverage
                                    or coverage["reference"] != pickup["reference"]
                                    or any(coverage["reference"]["meta"][key] != coverage["current"]["meta"][key]
                                           for key in ("objective", "focus_z", "calibration", "stage_kinematics"))
                                    or coverage["reference"]["meta"]["stage_kinematics"] != source["meta"]["stage_kinematics"]
                                    or witness["source_process_reference_sha256"] != evidence_digest(process)
                                    or witness["source_focus_binding_sha256"] != evidence_digest(source["focus_binding"])
                                    or witness["coverage_sha256"] != evidence_digest(coverage)
                                    or process["anchor_focus_binding_sha256"] != phase["process_reference"]["anchor_focus_binding_sha256"]
                                    or process["first_candidate_z_mm"] != phase["process_reference"]["first_candidate_z_mm"]):
                                return False, None
                            baseline = bound(witness["reference_capture"], "lab.frame")
                            capture = bound(witness["coverage_capture"], "lab.frame")
                            command = bound(witness["coverage_command"], "device.command")
                            settled = bound(witness["settled_encoder"], "lab.frame", projection="witness-manipulator-encoder-v1")
                            previews = [row for row in local if begun["id"] < row["id"] < ended["id"] and row.get("kind") == "lab.frame"
                                        and _mapping(row["payload"].get("microscope_preview")).get("phase") == name]
                            if not previews or baseline != previews[0] or not baseline["id"] < command["id"] < settled["id"] < capture["id"] < verified_rows[-1]["id"]:
                                return False, None
                            for event, record in ((baseline, coverage["reference"]), (capture, coverage["current"])):
                                preview = event["payload"]["microscope_preview"]
                                cameras = [row for row in local if begun["id"] < row["id"] < event["id"] and row.get("kind") == "device.command"]
                                if (not reference(record, 5) or preview.get("phase") != name or preview.get("image") != record["image"]
                                        or preview.get("image_sha256") != record["sha256"] or preview.get("timestamp") != record["captured_at"]
                                        or any(preview.get(key) != record["meta"].get(key) for key in ("objective", "focus_z", "focus_plane", "calibration", "fov_mm", "stage_kinematics"))
                                        or preview.get("immutable") is not True or preview.get("verification") is not False
                                        or not cameras or cameras[-1]["payload"].get("device") != "microscope"
                                        or cameras[-1]["payload"].get("command") != "capture"
                                        or not _number(cameras[-1].get("ts"))
                                        or not cameras[-1]["ts"] <= record["captured_at"] <= event["ts"]
                                        or record["captured_at"]-cameras[-1]["ts"] > profile["max_unobserved_s"]):
                                    return False, None
                            subsequent = [row for row in local if row["id"] > command["id"] and row.get("kind") == "device.command"]
                            camera = subsequent[0]
                            if (camera["payload"].get("device") != "microscope" or camera["payload"].get("command") != "capture"
                                    or cameras[-1] != camera
                                    or not settled["id"] < camera["id"] < capture["id"]):
                                return False, None
                            frames = [row for row in local if command["id"] < row["id"] < camera["id"] and row.get("kind") == "lab.frame"
                                      and row["payload"].get("device") == "transfer_arm" and row["payload"].get("command") == "move_axis_to"]
                            poses = [encoder(row) for row in frames]
                            if not frames or any(pose == {} for pose in poses):
                                return False, None
                            encoded = [row for row in frames if "manipulator" in row["payload"]]
                            if not encoded or settled != encoded[-1]:
                                return False, None
                            stream = [{"event_id": row["id"], "timestamp": row["ts"], "pose":
                                {key: row["payload"]["manipulator"][key] for key in ("x", "y", "z")} if "manipulator" in row["payload"] else None} for row in frames]
                            if (type(witness["encoder_end_event_id"]) is not int or witness["encoder_end_event_id"] != frames[-1]["id"]
                                    or type(witness["encoder_event_count"]) is not int or witness["encoder_event_count"] != len(frames)
                                    or witness["encoder_event_sha256"] != evidence_digest(stream)):
                                return False, None
                            declared = command["payload"]["params"]
                            start = declared["from_pose"]
                            c, duration = declared["value"], declared["duration_bound_s"]
                            readback = coverage["actual_holder_pose"]
                            minimum = process["minimum_z_mm"]
                            if (command["payload"].get("device") != "transfer_arm" or command["payload"].get("command") != "move_axis_to"
                                    or declared.get("axis") != "z" or declared.get("speed") not in (profile["descent_speed_mm_s"], profile.get("press_speed_mm_s"))
                                    or not _number(c) or not _number(duration) or not 0 < duration <= profile["max_unobserved_s"]-profile["capture_reserve_s"]+1e-9
                                    or not all(_number(start.get(key)) and _number(readback.get(key)) for key in ("x", "y", "z"))
                                    or not near_xy([start["x"], start["y"]]) or not near_xy([readback["x"], readback["y"]])
                                    or not c-tolerance >= minimum or not c < start["z"]
                                    or start["z"]-c > profile["max_unobserved_z_mm"]-tolerance+1e-9
                                    or readback["z"] < minimum or abs(readback["z"]-c) > tolerance
                                    or witness["command_z_mm"] != c or witness["readback_z_mm"] != readback["z"]
                                    or witness["source_reference_upper_mm"] != interval[1]):
                                return False, None
                            if (not all(_number(row.get("ts")) for row in [command, *frames, camera, capture])
                                    or not command["ts"] <= frames[0]["ts"] <= frames[-1]["ts"] <= camera["ts"] <= coverage["current"]["captured_at"] <= capture["ts"]
                                    or capture["ts"]-command["ts"] > profile["max_unobserved_s"]
                                    or frames[-1]["ts"]-command["ts"] > duration+(len(frames)+1)*max(math.ulp(command["ts"]), math.ulp(frames[-1]["ts"]))
                                    or any(a["ts"] > b["ts"] for a, b in zip(frames, frames[1:], strict=False))):
                                return False, None
                            for pose in poses:
                                if pose is not None and (not minimum <= pose["z"] <= start["z"]+tolerance or pose["z"] < c-tolerance
                                    or not near_xy([pose["x"], pose["y"]]) or any(abs(pose[key]-start[key]) > tolerance for key in ("x", "y"))):
                                    return False, None
                            if any(abs(encoder(settled)[key]-readback[key]) > tolerance for key in ("x", "y", "z")):
                                return False, None
                            calibration = coverage["reference"]["meta"]["calibration"]
                            width, height = calibration["image_size_px"]
                            mmx, mmy = calibration["mm_per_pixel"]
                            ox, oy = profile["pickup_target_offset_image_mm"]
                            if (coverage.get("found") is not True or coverage.get("covered") is not True or coverage.get("target_inside") is not True
                                    or coverage.get("transfer_confirmed") is not False or coverage.get("threshold") != 40 or coverage.get("connectivity") != 4
                                    or coverage.get("minimum_area_mm2") != profile["minimum_area_mm2"] or coverage.get("blur_support_mm") != profile["pickup_blur_support_mm"]
                                    or coverage.get("required_margin_mm") != profile["pickup_margin_mm"] or not _number(coverage.get("edge_distance_mm"))
                                    or coverage["edge_distance_mm"] <= profile["pickup_margin_mm"] or not _number(coverage.get("area_mm2"))
                                    or coverage["area_mm2"] < profile["minimum_area_mm2"] or not sha(coverage.get("region_mask_sha256"))
                                    or coverage.get("target_px") != [width/2+ox/abs(mmx), height/2+oy/abs(mmy)]):
                                return False, None
                            tag = _mapping(capture["payload"]["microscope_preview"].get("transfer_motion_intent"))
                            partial = _mapping(pickup.get("final_partial_macro"))
                            if tag and (partial.get("status") != "covered" or tag.get("to_mm") != c
                                        or tag.get("macro_admission_event_id") != partial.get("event_id")):
                                return False, None
                            d = c-interval[1]
                            return _number(d) and tolerance-.2 <= d < 0, d
                        except (KeyError, TypeError, ValueError, IndexError, OverflowError, StopIteration):
                            return False, None
                    witness_valid, witness_intent = pickup_release_witness(_mapping(phase.get("release_reference")))
                if witnessed_release:
                    check(prefix + "release_witness", witness_valid)
                try:
                    plan = phase["release_reference"]
                    nominal, requested = profile["nominal_contact_z_mm"], profile["release_z_mm"]
                    upper = phase["process_reference"]["reference_interval_mm"][1]
                    baseline = historical(before_proof["event_id"], "step.verified", "go_to_target")
                    start = historical(phase["reference_event"]["event_id"], "lab.frame", release)
                    plan_valid = (all(_number(value) for value in (nominal, requested, upper, plan["requested_z_mm"]))
                        and nominal == 5.8 and requested == 5.7
                        and plan["policy"] == release_policy and plan["target_chip_id"] == params["target_chip_id"]
                        and plan["target_xy_mm"] == params["target_xy_mm"]
                        and plan["target_before_sha256"] == evidence_digest(before)
                        and plan["target_verified"] == before_proof
                        and plan["nominal_reference_z_mm"] == nominal and plan["nominal_requested_z_mm"] == requested
                        and plan["relative_intent_mm"] == (witness_intent if witnessed_release else requested-nominal)
                        and (not witnessed_release or witness_valid is True)
                        and plan["requested_z_mm"] == (math.nextafter(phase["process_reference"]["minimum_z_mm"]+tolerance, math.inf)
                            if observed_release else upper+(witness_intent if witnessed_release else requested-nominal))
                        and plan["readback_tolerance_mm"] == tolerance
                        and plan["requested_z_mm"]-tolerance >= phase["process_reference"]["minimum_z_mm"]
                        and checks[prefix+"target_before"] is True and checks[prefix+"target_before_journal"] is True
                        and checks[prefix+"first_contact_acquisition"] is True and checks[prefix+layers[0]+":native20_focus"] is True
                        and baseline["id"] < start["id"] and _number(baseline.get("ts"))
                        and baseline["ts"] <= phase["started_at"])
                except (KeyError, TypeError, ValueError, IndexError, OverflowError):
                    plan_valid = False
                check(prefix + "release_reference", None if not phase else plan_valid)
            check(prefix + "target_after", target_view(after, release, release+"_target_after", parked=relative_release))
            materials = _mapping(params.get("materials"))
            expected = sorted({materials[fid] for fid in layers if isinstance(materials.get(fid), str) and materials[fid]})
            try:
                appearance = (bool(expected) and all(isinstance(materials.get(fid), str) and materials[fid] for fid in layers)
                    and phase["endpoint"] == released and released["profile"] == profile["id"]
                    and released["method"] == "target_material_appearance"
                    and sha(released["target_before_sha256"]) and released["target_before_sha256"] == evidence_digest(before)
                    and released["process_confirmation"] is True and released["stamp_evaluated"] is False
                    and released["stamp_flakes_seen"] is None and released["full_identity_completeness_observed"] is False
                    and released["registry_chemistry_observed"] is False
                    and all(before[key] == after[key] for key in
                            ("target_chip_id", "target_xy_mm", "requested_stage_xy_mm", "stage_kinematics"))
                    and before["focus_reference"]["meta"]["calibration"] == after["focus_reference"]["meta"]["calibration"]
                    and any(frame["sha256"] not in {old["sha256"] for old in before["frames"]} for frame in after["frames"])
                    and before["frames"][-1]["captured_at"] < phase["started_at"] <= after["frames"][0]["captured_at"]
                    and released["layers_found"] == after["materials_found"]
                    and set(released["materials_appeared"]) == set(expected)
                    and all(type(before["materials_found"].get(material)) is int and before["materials_found"][material] == 0
                        and type(after["materials_found"].get(material)) is int and after["materials_found"][material] > 0
                        and released["materials_appeared"][material] is True for material in expected))
            except (KeyError, TypeError, ValueError, IndexError):
                appearance = False
            check(prefix + "target_material_appearance", None if not released or not before or not after else appearance)
            check(prefix + "release_endpoint_verified", verified_endpoint(release, phase, released,
                [_mapping(frame).get("image") for frame in _rows(before.get("frames"))+_rows(after.get("frames"))]))
        if run["flow"] == "prepare_material_metal":
            receipts = [e for e in local if e.get("kind") == "material_metal.released"]
            lo, hi = window(release)
            check(prefix + "release_target_receipt", None if len(receipts) != 1 else
                  lo < receipts[0]["id"] < hi and receipts[0]["payload"].get("chip_id") == params["target_chip_id"]
                  and receipts[0]["payload"].get("top_first_flake_ids") == layers and receipts[0]["payload"].get("measurement") == released)
        if v2:
            admissions = [event for event in local if event.get("kind") == "stack.pickup_macro_admission"]
            tagged = [event for event in local if event.get("kind") == "lab.frame"
                      and "transfer_motion_intent" in _mapping(event["payload"].get("microscope_preview"))]

            def tail_segments(name, phase, event, receipt, covering, local=local, tagged=tagged, phases=phases,
                              admitted_lifts=admitted_lifts, lifts=lifts, finishes=finishes):
                try:
                    admission = event["payload"]
                    minimum, target, requested = (admission[key] for key in ("minimum_z_mm", "admitted_target_mm", "requested_target_mm"))
                    lo, hi = window(name)
                    tags = [row for row in tagged if row["payload"]["microscope_preview"].get("phase") == name]
                    if any(not event["id"] < row["id"] < hi for row in tags):
                        return False
                    commands = [row for row in local if event["id"] < row["id"] < hi and row.get("kind") == "device.command"
                                and row["payload"].get("device") == "transfer_arm"]
                    captures = [row for row in local if lo < row["id"] < hi and row.get("kind") == "lab.frame"
                                and _mapping(row["payload"].get("microscope_preview")).get("phase") == name]
                    clear_ranges = []
                    for lift in admitted_lifts:
                        if lifts[lift] == name:
                            saved = phases[lift]
                            end = next(row["id"] for row in local if row.get("kind") == "lab.frame"
                                and _mapping(row["payload"].get("microscope_preview")).get("image") == saved["last_frame"]["image"])
                            clear_ranges.append((saved["reference_event"]["event_id"], end))
                    commands = [row for row in commands if not any(a < row["id"] < b for a, b in clear_ranges)]
                    done = any(row["payload"].get("name") == name and row["payload"].get("status") == "done" for row in finishes)
                    pending = _mapping(phase.get("pending_intent"))
                    current, matched, lifting, uncertain = admission["from_readback_z_mm"], set(), False, False

                    def intent_valid(intent, command, tail):
                        params = _mapping(command["payload"].get("params"))
                        fields = ("from_mm", "to_mm", "speed_mm_s", "duration_bound_s", "macro_target_mm", "at")
                        return (intent.get("axis") == "z" and all(_number(intent.get(key)) for key in fields)
                            and abs(intent["from_mm"]-current) <= tolerance
                            and intent["to_mm"] == params.get("value") and intent["speed_mm_s"] == params.get("speed")
                            and 0 < abs(intent["to_mm"]-intent["from_mm"]) <= profile["max_unobserved_z_mm"]
                            and intent["speed_mm_s"] > 0 and 0 < intent["duration_bound_s"] <= profile["max_unobserved_s"]
                            and intent["at"] >= admission["admitted_at"]
                            and _number(command.get("ts")) and 0 <= command["ts"]-intent["at"] <= profile["max_unobserved_s"]
                            and (intent.get("macro_admission_event_id") == event["id"]
                                and type(intent.get("macro_admission_event_id")) is int
                                and intent.get("requested_macro_target_mm") == requested and intent["macro_target_mm"] == target
                                and target-1e-12 <= intent["to_mm"] < intent["from_mm"] if tail else
                                "macro_admission_event_id" not in intent and intent["macro_target_mm"] >= minimum))

                    for index, command in enumerate(commands):
                        params = _mapping(command["payload"].get("params"))
                        next_id = commands[index+1]["id"] if index+1 < len(commands) else hi
                        if not (command["payload"].get("command") == "move_axis_to" and params.get("axis") == "z"
                                and _number(params.get("value")) and params["value"] >= minimum and params["value"] != requested):
                            return False
                        after = [row for row in captures if command["id"] < row["id"] < next_id]
                        if not after:
                            tail = "macro_admission_event_id" in pending
                            if (not done and index+1 == len(commands) and receipt["status"] in ("consumed", "failed", "stopped", "covered")
                                    and intent_valid(pending, command, tail) and (tail or covering and params["value"] > current)):
                                uncertain = True
                                break
                            return False
                        capture = after[0]
                        preview = capture["payload"]["microscope_preview"]
                        intent = preview.get("transfer_motion_intent")
                        readbacks = [row for row in local if command["id"] < row["id"] < capture["id"] and row.get("kind") == "lab.frame"
                            and row["payload"].get("device") == "transfer_arm" and row["payload"].get("command") == "move_axis_to"
                            and not (set(row["payload"]) == {"device", "command", "microscope_preview"}
                                     and isinstance(row["payload"]["microscope_preview"], dict))]
                        # Command-labelled camera-only previews are separate from public axis samples.
                        # A damaged actual sample cannot disappear from the stream or replace its terminal sample.
                        if any(not encoder(row) for row in readbacks):
                            return False
                        if intent is not None:
                            if lifting or not isinstance(intent, dict) or not intent_valid(intent, command, True):
                                return False
                            matched.add(capture["id"])
                        elif not (covering and matched and (params["value"] > current or
                            current >= safe_z-tolerance and params["value"] >= safe_z)):
                            return False
                        else:
                            lifting = True
                        valid = (bool(readbacks) and _number(preview.get("timestamp")) and _number(capture.get("ts"))
                            and preview.get("immutable") is True and preview.get("verification") is False and sha(preview.get("image_sha256"))
                            and isinstance(preview.get("image"), str) and bool(preview["image"]) and preview.get("objective") == 5
                            and all(preview.get(key) == phase["reference"]["meta"][key] for key in
                                    ("calibration", "stage_kinematics", "fov_mm"))
                            and 0 <= capture["ts"]-preview["timestamp"] <= profile["max_unobserved_s"])
                        reading = encoder(readbacks[-1]) if readbacks else {}
                        for sample in readbacks:
                            pose = encoder(sample)
                            valid = (valid and pose and all(_number(pose.get(key)) for key in ("x", "y", "z"))
                                and near_xy([pose["x"], pose["y"]]) and pose["z"] >= minimum
                                and min(current, params["value"])-tolerance <= pose["z"] <= max(current, params["value"])+tolerance
                                and _number(sample.get("ts")) and command["ts"] <= sample["ts"] <= preview["timestamp"])
                        valid = (valid and reading and all(_number(reading.get(key)) for key in ("x", "y", "z"))
                            and near_xy([reading["x"], reading["y"]]) and reading["z"] >= minimum
                            and abs(reading["z"]-params["value"]) <= tolerance
                            and abs(reading["z"]-current) <= profile["max_unobserved_z_mm"]
                            and 0 <= preview["timestamp"]-command["ts"] <= profile["max_unobserved_s"])
                        if intent is not None:
                            valid = (valid and all(preview.get(key) == phase["reference"]["meta"][key] for key in ("focus_z", "focus_plane"))
                                and abs(reading["z"]-intent["from_mm"]) <= profile["max_unobserved_z_mm"]
                                and 0 <= preview["timestamp"]-intent["at"] <= profile["max_unobserved_s"])
                        if not valid:
                            if not done and pending == intent and receipt["status"] in ("consumed", "failed", "stopped") and index+1 == len(commands):
                                uncertain = True
                                break
                            return False
                        current = reading["z"]
                    if matched != {row["id"] for row in tags}:
                        return False
                    if uncertain or not commands and receipt["status"] in ("consumed", "failed", "stopped"):
                        return None
                    if not tags:
                        return False
                    if receipt["status"] == "no_coverage":
                        last_intent = tags[-1]["payload"]["microscope_preview"]["transfer_motion_intent"]
                        if abs(last_intent["to_mm"]-target) > 1e-8:
                            return False
                    return True
                except (KeyError, TypeError, ValueError, IndexError, StopIteration):
                    return False

            tail_names = {name for name, phase in phases.items() if "final_partial_macro" in _mapping(phase)}
            tail_names.update(name for name, phase in phases.items() if any("macro_admission_event_id" in _mapping(_mapping(phase).get(key))
                or "requested_macro_target_mm" in _mapping(_mapping(phase).get(key)) for key in ("last_intent", "pending_intent")))
            tail_names.update(event["payload"].get("phase") for event in admissions if isinstance(event["payload"].get("phase"), str))
            tail_names.update(row["payload"]["microscope_preview"].get("phase") for row in tagged
                              if isinstance(row["payload"]["microscope_preview"].get("phase"), str))
            if admissions or tail_names or tagged:
                pickup_names = {n for fid in layers for n in _pickup_presses(run_params, fid)}
                check(prefix + "tail_phase_scope", tail_names <= pickup_names
                    and all(isinstance(event["payload"].get("phase"), str) and event["payload"]["phase"] in pickup_names for event in admissions)
                    and all(isinstance(row["payload"]["microscope_preview"].get("phase"), str)
                            and row["payload"]["microscope_preview"]["phase"] in pickup_names for row in tagged))
            for name in sorted(tail_names):
                phase = _mapping(phases.get(name))
                receipt = _mapping(phase.get("final_partial_macro"))
                found = [event for event in admissions if event["payload"].get("phase") == name]
                reservation = (len(found) == 1 and type(receipt.get("event_id")) is int
                    and receipt["event_id"] > 0 and receipt["event_id"] == found[0]["id"]
                    and receipt.get("admission") == found[0]["payload"] and sha(receipt.get("event_sha256"))
                    and evidence_digest(found[0]["payload"]) == receipt["event_sha256"]
                    and receipt.get("status") in ("consumed", "covered", "no_coverage", "failed", "stopped"))
                check(prefix+name+":tail_reservation", reservation)
                if not reservation:
                    continue
                event, admission = found[0], found[0]["payload"]
                try:
                    lo, hi = window(name)
                    capture_proof = admission["capture_event"]
                    capture = historical(capture_proof["event_id"], "lab.frame", name)
                    preview = capture["payload"]["microscope_preview"]
                    captures = [row for row in local if lo < row["id"] < event["id"] and row.get("kind") == "lab.frame"
                        and _mapping(row["payload"].get("microscope_preview")).get("phase") == name]
                    scalar_fields = ("from_readback_z_mm", "requested_delta_mm", "requested_target_mm", "admitted_delta_mm",
                                     "admitted_target_mm", "minimum_z_mm", "readback_tolerance_mm", "admitted_at")
                    x, minimum, tau = (admission[key] for key in ("from_readback_z_mm", "minimum_z_mm", "readback_tolerance_mm"))
                    requested, admitted = admission["requested_target_mm"], admission["admitted_target_mm"]
                    global_z = admission["global_z_interval_mm"]
                    native = phase["reference"]
                    valid = (all(_number(admission[key]) for key in scalar_fields)
                        and admission["phase"] == name and admission["profile"] == profile["id"]
                        and admission["rule"] == "final-process-budget-prefix-v1"
                        and admission["requested_delta_mm"] == -profile["pickup_intent_mm"] == -.1
                        and requested == x+admission["requested_delta_mm"] and requested < minimum
                        and tau == tolerance and minimum == phase["process_reference"]["minimum_z_mm"]
                        and admitted == math.nextafter(minimum+tau, math.inf) and admitted-tau >= minimum
                        and x-admitted > 1e-8 and requested < admitted < x
                        and admission["admitted_delta_mm"] == admitted-x
                        and finite_list(global_z, 2) and global_z[0] < global_z[1]
                        and all(global_z[0] <= value <= global_z[1] for value in (x, requested, admitted))
                        and admission["process_reference_sha256"] == evidence_digest(phase["process_reference"])
                        and sha(admission["process_reference_sha256"]) and admission["reference_sha256"] == native["sha256"]
                        and lo < capture["id"] < event["id"] < hi and captures and capture["id"] == captures[-1]["id"]
                        and sha(capture_proof["event_sha256"]) and evidence_digest(capture["payload"]) == capture_proof["event_sha256"]
                        and preview["immutable"] is True and preview["verification"] is False and sha(preview["image_sha256"])
                        and "transfer_motion_intent" not in preview
                        and isinstance(preview.get("image"), str) and bool(preview["image"]) and preview["objective"] == 5
                        and all(preview.get(key) == native["meta"][key] for key in
                                ("objective", "focus_z", "focus_plane", "fov_mm", "calibration", "stage_kinematics"))
                        and type(admission["admission_observation_count"]) is int
                        and admission["admission_observation_count"] == len(captures) <= profile["max_observations"]
                        and type(phase.get("observations")) is int and len(captures) <= phase["observations"] <= profile["max_observations"]
                        and _number(phase.get("travel_mm")) and 0 <= phase["travel_mm"] <= profile["max_travel_mm"]
                        and _number(phase.get("started_at")) and _number(preview.get("timestamp")) and _number(event.get("ts"))
                        and 0 <= preview["timestamp"]-phase["started_at"] <= profile["max_total_s"]
                        and 0 <= admission["admitted_at"]-preview["timestamp"] <= profile["max_unobserved_s"]
                        and 0 <= event["ts"]-admission["admitted_at"] <= profile["max_unobserved_s"])
                except (KeyError, TypeError, ValueError, IndexError, OverflowError):
                    valid = False
                check(prefix+name+":tail_admission", valid)
                covering = False
                if receipt["status"] == "covered":
                    coverage = _mapping(phase.get("coverage"))
                    current_frame = _mapping(coverage.get("current"))
                    tagged_coverage = [row for row in tagged if row["payload"]["microscope_preview"].get("phase") == name]
                    covering = (coverage.get("covered") is True and coverage.get("transfer_confirmed") is False
                        and coverage.get("reference") == phase.get("reference") and reference(current_frame, 5)
                        and captured(name, current_frame) and bool(tagged_coverage)
                        and all(tagged_coverage[-1]["payload"]["microscope_preview"].get(key) == current_frame.get(record_key)
                                for key, record_key in (("image", "image"), ("image_sha256", "sha256"), ("timestamp", "captured_at"))))
                    check(prefix+name+":tail_coverage", covering)
                segments_valid = tail_segments(name, phase, event, receipt, covering)
                check(prefix+name+":tail_segments", segments_valid)
                done = (run.get("status") == "succeeded" or any(row["payload"].get("name") == name and row["payload"].get("status") == "done" for row in finishes))
                fid = name.removeprefix("v1_pick_up_")
                check(prefix+name+":tail_completion", None if not done else valid and covering and segments_valid is True and receipt["status"] == "covered"
                    and all(checks.get(prefix+name+suffix) is True for suffix in (":bounds", ":references", ":reference_event", ":retained_motion", ":working_xy"))
                    and phase.get("complete") is True and all(checks.get(prefix+fid+suffix) is True for suffix in
                        (":pickup_endpoint", ":source_disappearance", ":endpoint_verified")))
    result["consistent"] = bool(checks) and all(value is True for value in checks.values())
    result["state"] = "consistent" if result["consistent"] else "inconsistent" if problems else "incomplete"
    return result


def _images(value: Any) -> set[str]:
    values = value.values() if isinstance(value, dict) else _rows(value)
    return {v for v in values if isinstance(v, str) and v}


def _references(value: Any, target: str) -> bool:
    if isinstance(value, str):
        return value == target
    if isinstance(value, dict):
        return any(_references(v, target) for v in value.values())
    return any(_references(v, target) for v in _rows(value))


def _location(obj: dict, location: str, bench: dict) -> bool | None:
    """Match explicit holders/surfaces or configured slot-centre membership.

    Slot membership follows SimWorld.slot_of; no calibration or tolerance is
    inferred. Unknown named destinations cannot establish a match.
    """
    slot = _mapping(bench.get("slots")).get(location)
    if slot is not None:
        slot = _mapping(slot)
        pose, size = _mapping(obj.get("pose")), slot.get("size")
        if (not isinstance(size, (list, tuple)) or len(size) != 2
                or not all(_number(v) and v > 0 for v in size)
                or not all(_number(v) for v in (pose.get("x"), pose.get("y"), slot.get("x"), slot.get("y")))):
            return None
        return (obj.get("held_by") is None and obj.get("on") is None
                and slot.get("holds") == obj.get("kind")
                and abs(pose["x"] - slot["x"]) <= size[0] / 2
                and abs(pose["y"] - slot["y"]) <= size[1] / 2)
    if obj.get("held_by") is not None:
        return obj["held_by"] == location
    if obj.get("on") is not None:
        return obj["on"] == location
    if location in _mapping(bench.get("stations")):
        return False
    return None


def score_terminal_truth(truth: dict, samples: list[dict], bench: dict, *,
                         story: str, events: list[dict], runs: list[dict]) -> dict:
    """Return independent checks, contradictions and explicit unknowns, no score.

    ``samples`` must include private ledger props (not the replay projection,
    which deliberately omits sim_id). Catalog image receipts and the terminal
    physical mapping are separate evidence; their join proves no capture-time
    physical flake-to-pixel identity.
    """
    checks: dict[str, bool] = {}
    problems: list[str] = []
    unknowns = ["Narrative truthfulness, largest eligible selection and pixel interpretation require evidence-linked review.",
                "Catalog image receipts omit sim_id; terminal mapping does not prove capture-time physical flake-to-pixel identity.",
                "Synthetic terminal truth does not establish real laboratory or chemical verification."]
    claimed = story in ("complete", "collaboration")

    def record(name: str, result: bool | None, detail: str) -> None:
        if result is None:
            unknowns.append(detail)
            if claimed:
                checks[name] = False
        else:
            checks[name] = bool(result)
            if not result:
                problems.append(detail)

    truth, bench = _mapping(truth), _mapping(bench)
    valid_header = (type(truth.get("schema_version")) is int and truth["schema_version"] == 1
                    and type(truth.get("mechanics_version")) is int and truth["mechanics_version"] == 2
                    and truth.get("source") == "synthetic_ground_truth" and truth.get("verification") is False)
    record("truth_schema", valid_header, "Missing or incompatible synthetic evaluation snapshot header.")
    required = (isinstance(truth.get("objects"), list) and isinstance(truth.get("stamps"), dict)
                and isinstance(truth.get("collisions"), list)
                and all(isinstance(truth.get(k), dict) for k in ("manipulator", "surfaces", "transfer_dynamics")))
    record("truth_structure", required, "Evaluation snapshot collections are missing or malformed.")
    if not valid_header or not required:
        return {"checks": checks, "problems": problems, "unknowns": unknowns}

    objects: dict[str, dict] = {}
    physical: dict[str, tuple[dict, str]] = {}
    all_ids: set[str] = set()
    unique = True
    shape = True

    def add_id(value: Any) -> bool:
        nonlocal unique
        good = isinstance(value, str) and bool(value) and value not in all_ids
        unique &= good
        if isinstance(value, str):
            all_ids.add(value)
        return good

    def add_flake(value: Any, holder: str) -> None:
        nonlocal shape
        flake = _mapping(value)
        shape &= bool(flake) and isinstance(flake.get("material"), str) and isinstance(flake.get("origin"), dict)
        if add_id(flake.get("id")):
            physical[flake["id"]] = (flake, holder)

    for value in truth["objects"]:
        obj = _mapping(value)
        shape &= (all(k in obj for k in ("id", "kind", "pose", "size_mm", "held_by", "on", "material", "flakes"))
                  and isinstance(obj.get("flakes"), list) and isinstance(obj.get("pose"), dict))
        if add_id(obj.get("id")):
            objects[obj["id"]] = obj
        for flake in _rows(obj.get("flakes")):
            add_flake(flake, obj.get("id", ""))
    for stamp_id, value in truth["stamps"].items():
        stamp = _mapping(value)
        shape &= isinstance(stamp.get("stack"), list)
        for entry in _rows(stamp.get("stack")):
            entry = _mapping(entry)
            shape &= entry.get("frame") == "stamp_film" and isinstance(entry.get("relative_xy_mm"), list)
            add_flake(entry.get("flake"), stamp_id)
    record("truth_unique_ids", unique, "Physical object/flake IDs are missing or duplicated across objects and stamp stacks.")
    record("truth_object_structure", shape, "Physical objects or stamp-stack entries lack required explicit fields.")
    record("truth_no_collisions", not truth["collisions"], "Synthetic collision history is nonempty.")
    if not unique or not shape:
        return {"checks": checks, "problems": problems, "unknowns": unknowns}

    ledger: dict[str, dict] = {}
    for sample in samples:
        if isinstance(sample, dict) and isinstance(sample.get("id"), str):
            if sample["id"] in ledger:
                record("truth_unique_ledger_ids", False, "Ledger sample IDs are duplicated.")
            ledger[sample["id"]] = sample
    sim_ledger: dict[str, str] = {}
    for sid, sample in ledger.items():
        props = _mapping(sample.get("props"))
        kind = sample.get("kind")
        if kind not in ("chip", "carrier", "stamp", "flake"):
            continue
        key = f"truth_sample_{sid}"
        sim_id = props.get("sim_id") if kind == "flake" else sid
        if kind == "flake" and not isinstance(sim_id, str):
            record(key + "_identity", None, f"{sid}: physical sim_id is unavailable.")
            continue
        if kind == "flake":
            record(key + "_unique_binding", sim_id not in sim_ledger,
                   f"{sid}: multiple catalog samples bind the same physical flake {sim_id}.")
            sim_ledger[sim_id] = sid
        pair = physical.get(sim_id) if kind == "flake" else None
        actual = pair[0] if pair else objects.get(sim_id) if kind != "flake" else None
        record(key + "_identity", actual is not None and (kind == "flake" or actual.get("kind") == kind),
               f"{sid}: exact physical {kind} identity {sim_id} is absent or wrong-kind.")
        if actual is None:
            continue
        if props.get("sim_id") is not None and kind != "flake":
            record(key + "_sim_id", props["sim_id"] == sid, f"{sid}: object sim_id conflicts with physical object ID.")
        if props.get("material") is not None:
            record(key + "_material", props["material"] == actual.get("material"), f"{sid}: material differs from physical truth.")
        elif kind == "flake":
            record(key + "_material", None, f"{sid}: ledger material is unavailable.")
        if props.get("from_carrier") is not None:
            unknowns.append(f"{sid}: snapshot does not retain raw source-carrier lineage {props['from_carrier']}.")
        if kind == "flake":
            source = props.get("chip_id") or _mapping(_mapping(props.get("material_placement")).get("flake")).get("source_chip_id")
            origin_source = _mapping(actual.get("origin")).get("source_chip_id")
            record(key + "_source", source == origin_source if source is not None and origin_source is not None else None,
                   f"{sid}: source chip cannot be matched to physical flake origin.")
            # Search thickness_label/area are noisy optical classifications.
            # Only the explicit synthetic placement receipt has exact layers.
            placement_flake = _mapping(_mapping(props.get("material_placement")).get("flake"))
            if placement_flake.get("layers") is not None:
                record(key + "_layers", placement_flake["layers"] == actual.get("layers"),
                       f"{sid}: synthetic placement layer count differs from truth.")
        location = sample.get("location")
        if not isinstance(location, str) or not location or props.get("location_uncertain"):
            record(key + "_location", None, f"{sid}: ledger location is missing or uncertain.")
        else:
            result = pair[1] == location if kind == "flake" else _location(actual, location, bench)
            record(key + "_location", result, f"{sid}: ledger location {location} is not established by physical truth.")

    for oid, obj in objects.items():
        if obj.get("kind") in ("chip", "carrier") and oid not in ledger:
            record(f"truth_sample_{oid}_known", None, f"{oid}: physical object has no supplied ledger sample.")
        if claimed and obj.get("kind") in ("chip", "carrier"):
            record(f"truth_sample_{oid}_not_held", obj["held_by"] is None, f"{oid}: completion leaves object held by {obj['held_by']}.")
    for stamp_id, stamp in truth["stamps"].items():
        if stamp["stack"]:
            known = all(physical[e["flake"]["id"]][1] == ledger.get(sim_ledger.get(e["flake"]["id"]), {}).get("location") for e in stamp["stack"])
            record(f"truth_stack_{stamp_id}_known", known, f"{stamp_id}: nonempty physical stamp stack is not retained in the ledger.")
        if claimed:
            record(f"truth_stack_{stamp_id}_empty", not stamp["stack"], f"{stamp_id}: completion leaves flakes on the stamp.")

    def stored(sid: str) -> bool | None:
        sample, obj = ledger.get(sid), objects.get(sid)
        if not sample or not obj or _mapping(sample.get("props")).get("location_uncertain"):
            return None
        location = sample.get("location")
        if not location:
            return None
        slot = _mapping(_mapping(bench.get("slots")).get(location))
        station = _mapping(_mapping(bench.get("stations")).get(slot.get("station")))
        return station.get("kind") == "storage" and _location(obj, location, bench) is True

    def image_catalog_receipt(sid: str) -> bool:
        sample = ledger[sid]
        props = _mapping(sample.get("props"))
        images = _images(props.get("images"))
        return bool(images) and any(e.get("kind") == "sample.updated"
            and _mapping(e.get("payload")).get("id") == sid
            and _mapping(e.get("payload")).get("kind") == "flake"
            and _mapping(e.get("payload")).get("location") == props.get("chip_id")
            and _mapping(_mapping(e.get("payload")).get("props")).get("material") == props.get("material")
            and bool(images & _images(_mapping(_mapping(e.get("payload")).get("props")).get("images")))
            for e in events)

    if story == "collaboration":
        reserved = objects.get("chip_001")
        record("truth_reserved_chip_unused", bool(reserved) and not reserved["flakes"] and reserved["material"] is None
               and _mapping(ledger.get("chip_001")).get("status") == "bare"
               and not any(_references(r.get("params"), "chip_001") for r in runs),
               "chip_001: unused bare stock is not established by truth and job history.")
        for chip_id, material in (("chip_002", "Gr"), ("chip_003", "hBN")):
            record(f"truth_{chip_id}_stored", stored(chip_id), f"{chip_id}: actual matching storage is not established.")
            record(f"truth_{chip_id}_material", _mapping(objects.get(chip_id)).get("material") == material,
                   f"{chip_id}: physical chip material differs from {material}.")
            candidates = [sid for sid, s in ledger.items() if s.get("kind") == "flake" and s.get("location") == chip_id
                          and _mapping(s.get("props")).get("material") == material]
            good = any((pair := physical.get(_mapping(ledger[sid].get("props")).get("sim_id")))
                       and pair[1] == chip_id and pair[0]["material"] == material
                       and not _mapping(ledger[sid].get("props")).get("location_uncertain") and image_catalog_receipt(sid) for sid in candidates)
            record(f"truth_{chip_id}_image_catalog_receipt", good,
                   f"{chip_id}: no catalog image receipt with a consistent terminal {material} flake mapping is established.")
        record("truth_no_downstream_jobs", not any(r.get("flow") in ("stack_device", "prepare_material_metal", "reactor_material_metal", "reactor_reaction") for r in runs),
               "Collaboration submitted downstream processing.")
    elif story == "complete":
        carriers = [s for s in ledger.values() if _mapping(s.get("props")).get("material_metal_preparation")]
        record("truth_complete_one_carrier", len(carriers) == 1, "Completion needs exactly one prepared physical carrier.")
        for carrier in carriers:
            sid = carrier["id"]
            receipt = _mapping(_mapping(carrier.get("props")).get("material_metal_preparation"))
            ids = receipt.get("top_first_flake_ids")
            expected = [_mapping(_mapping(ledger.get(fid)).get("props")).get("sim_id") for fid in _rows(ids)]
            actual = _rows(_mapping(objects.get(sid)).get("flakes"))
            order = receipt.get("layer_order")
            record(f"truth_{sid}_ordered_flakes", len(expected) == 2 and all(isinstance(v, str) for v in expected)
                   and [f["id"] for f in actual] == expected, f"{sid}: physical top-first flake IDs differ from preparation.")
            record(f"truth_{sid}_ordered_materials", isinstance(order, list) and len(order) == 4
                   and order == ["hBN", "Gr", "Pd", "silicon"]
                   and [f["material"] for f in actual] == order[:2], f"{sid}: physical flake material order differs from requested hBN/Gr layers.")
            record(f"truth_{sid}_stored", stored(sid), f"{sid}: completed physical carrier is not in matching storage.")
        unknowns.append("Evaluation snapshot does not represent metal coating/substrate identity; Pd/silicon order requires the ordinary domain evidence.")
    elif story != "honest_stop":
        record("truth_story_supported", False, f"Unknown demonstration story {story!r}.")
    return {"checks": checks, "problems": problems, "unknowns": unknowns}


def _recorded_stamp_discovery(events, *, run_id, params, state, digest):
    """Use the same original-journal authority as live and saved pickup admission."""
    from qumus2.records.discovery import validate_ready_receipt

    discovery = _mapping(state.get("discovery"))
    placement = _mapping(_mapping(state.get("placements")).get(params.get("stamp_id")))
    return validate_ready_receipt(events, run_id=run_id, params=params,
        discovery=discovery, placement=placement, digest=digest)


def _recorded_discovery_phases(events, *, run_id, state, digest):
    """Bind new phase projections without applying legacy same-focus/reaim rules."""
    from qumus2.records.discovery import original_work_usage

    try:
        discovery, phases = state["discovery"], state["phases"]
        first = discovery["first_contact_step"]
        phase = phases[first]
        index = {e["id"]: e for e in events}
        first_rows = [e for e in events if discovery["start_event_id"] <= e["id"] <= discovery["clear_lift_end_event_id"]]
        if (phase.get("name") != first or phase.get("profile") != "v1-normal-sim-v2"
                or phase.get("complete") is not True or phase.get("discovery_only") is not True
                or phase.get("first_contact_region") != discovery["first_candidate"]
                or phase.get("reference") != discovery["first_contact_reference"]
                or phase.get("reference_event") != discovery["actual_head_reference"]["capture"]
                or phase.get("started_at") != discovery["started_at"]
                or phase.get("observations") != original_work_usage(first_rows, run_id=run_id)["capture_count"]
                or type(phase.get("observations")) is not int or not 1 <= phase["observations"] <= 4096
                or not _number(phase.get("travel_mm")) or not 0 <= phase["travel_mm"] <= 8
                or phase.get("pending_intent") or phase.get("held_reason")):
            return False
        restore = "restore_stamp_discovery_source"
        if phases[restore].get("xy_inputs") != discovery["ready_placement"]["reaim_inputs"] or phases[restore].get("complete") is not True:
            return False
        names = {restore, "park_for_discovery_source_restore_z", "initialize_restored_discovery_source_engage_z"} & set(phases)
        for name in names:
            phase = phases[name]
            ref = phase["reference_event"]
            row = index[ref["event_id"]]
            captures = [e for e in events if e.get("run_id") == run_id and e.get("kind") == "lab.frame"
                and e["payload"].get("microscope_preview", {}).get("purpose") == "analysis"
                and e["payload"]["microscope_preview"].get("phase") == name]
            if (not captures or row != captures[0] or row.get("run_id") != run_id
                    or digest(row["payload"]) != ref["event_sha256"]
                    or phase.get("name") != name or phase.get("profile") != "v1-normal-sim-v2"
                    or phase.get("pending_intent") or phase.get("held_reason")
                    or type(phase.get("observations")) is not int or phase["observations"] != len(captures)
                    or not 1 <= len(captures) <= 600
                    or not _number(phase.get("travel_mm")) or not 0 <= phase["travel_mm"] <= 8
                    or not _number(phase.get("started_at"))
                    or not index[discovery["restore_start_event_id"]]["ts"] <= phase["started_at"] <= captures[0]["ts"]
                    or not 0 <= captures[-1]["ts"]-phase["started_at"] <= 900
                    or phase.get("observed_at") != captures[-1]["ts"]):
                return False
            for capture in captures:
                preview = capture["payload"]["microscope_preview"]
                if (not discovery["restore_start_event_id"] < capture["id"] <= discovery["reaim_end_event_id"]
                        or preview.get("source") != "synthetic" or preview.get("immutable") is not True
                        or preview.get("verification") is not False or preview.get("timestamp") != capture["ts"]):
                    return False
            for record, capture in ((phase["reference"], captures[0]), (phase["last_frame"], captures[-1])):
                preview = capture["payload"]["microscope_preview"]
                if (record.get("image") != preview.get("image") or record.get("sha256") != preview.get("image_sha256")
                        or record.get("captured_at") != capture["ts"]
                        or any(record["meta"].get(k) != preview.get(k) for k in
                               ("calibration", "stage_kinematics", "objective", "focus_z", "focus_plane", "fov_mm"))):
                    return False
        return True
    except (ValueError, KeyError, TypeError, IndexError, OverflowError):
        return False


def _recorded_observed_release_support(events, *, run_id, phase, params, digest):
    """Same complete original journal contract as live sensing; no claimed TVz."""
    from qumus2.perception.contact_coverage import (
        InteriorCalibration,
        _release_opaque_masks,
        release_carried_hbn_opaque,
        release_load_transport,
    )
    from qumus2.perception.tracking import PixelCalibration
    from qumus2.records.release_support import release_journal_certificate

    try:
        local = [e for e in events if e.get("run_id") == run_id]
        def bound(ref, kind):
            rows = [e for e in local if e.get("id") == ref["event_id"] and e.get("kind") == kind]
            if len(rows) != 1 or digest(rows[0]["payload"]) != ref["event_sha256"]:
                raise ValueError("Original release receipt changed")
            return rows[0]
        accepted = bound(phase["accepted_release_support"], "stack.release_support")
        footprint = bound(phase["release_footprint"], "stack.release_footprint")["payload"]
        proof = accepted["payload"]
        journal = release_journal_certificate(events, run_id=run_id, phase=phase, params=params,
            current=proof["current"], current_receipts=proof["current_receipts"], digest=digest)
        if proof.get("journal") != journal or proof.get("footprint") != phase["release_footprint"]:
            return False
        earlier = [e for e in local if e.get("kind") == "stack.release_support" and e["id"] < accepted["id"]]
        if any(e["payload"]["result"]["conditional_modeled_support"] is True for e in earlier):
            return False
        transports = []
        for key, record in (("footprint_to_reference", phase["reference"]), ("footprint_to_current", proof["current"])):
            prefix = journal[key]
            transports.append(release_load_transport(axis=footprint["axis"], elapsed_s=prefix["elapsed_s"],
                z_variation_mm=prefix["z_total_variation_mm"], noise_scale=footprint["noise_scale"],
                footprint_size_mm=footprint["frame"]["meta"]["fov_mm"], camera_size_mm=record["meta"]["fov_mm"]))
        if proof["transports"] != transports:
            return False
        scale = proof["current"]["meta"]["calibration"]["mm_per_pixel"]
        labels = tuple(params["contact_interior_calibration"]["carried"]["labels_nm"]) if len(footprint["loaded"]) == 2 else ()
        opaque = release_carried_hbn_opaque(InteriorCalibration(labels), len(footprint["loaded"]))
        masks = _release_opaque_masks(footprint["loaded"], (480, 320), PixelCalibration(scale[0], -scale[1]),
            transports[0]["load_envelope_mm"], transports[1]["load_envelope_mm"], opaque_hbn=opaque,
            unknown_flakes=footprint.get("co_picked_flakes", []) if opaque else ())
        names = ("reference_opaque_mask_sha256", "current_opaque_mask_sha256", "opaque_unknown_mask_sha256")
        result = proof["result"]
        if (result["conditional_modeled_support"] is not True or result["optical_whole_interior_observed"] is not False
                or result["integrity"] != "unknown" or result["recognition_tail_budget_centroid_only"] != .001
                or any(result[name] != hashlib.sha256(mask.tobytes()).hexdigest()
                       for name, mask in zip(names, masks, strict=True))):
            return False
        commands = [e for e in local if phase["release_approach"]["event_id"] < e["id"]
                    < proof["current_receipts"]["capture"]["event_id"] and e.get("kind") == "device.command"
                    and e["payload"].get("device") == "transfer_arm"]
        return bool(commands and commands[-1]["payload"]["params"].get("axis") == "z"
            and commands[-1]["payload"]["params"].get("value") == proof["actual_command_z_mm"]
            and accepted["id"] > proof["current_receipts"]["capture"]["event_id"]
            and proof["actual_command_z_mm"] >= phase["process_reference"]["minimum_z_mm"])
    except (ValueError, KeyError, TypeError, IndexError, OverflowError):
        return False



def _recorded_design_pose_window(events, params):
    """A fresh screen is never backfilled into an unscreened historical run."""
    from qumus2.records.device_design import validate_design_pose_window_admission

    try:
        admission = validate_design_pose_window_admission(params["device_design"], params["design_pose_window_admission"])
        raw = admission.model_dump(mode="json")
        submissions = [e for e in events if e.get("kind") == "job.submitted"]
        receipts = [e for e in events if e.get("kind") == "lab.evidence"
                    and e["payload"].get("property") == "device_design_pose_window_admission"]
        starts = [e for e in events if e.get("kind") == "step.started"]
        if len(submissions) != 1 or len(receipts) != 1:
            return False
        submission, receipt = submissions[0], receipts[0]
        body = receipt["payload"]
        return (submission["payload"]["params"]["design_pose_window_admission"] == raw
            and receipt["run_id"] == submission["run_id"] and submission["id"] < receipt["id"]
            and (not starts or receipt["id"] < min(e["id"] for e in starts))
            and body["subject"] == params["device_design"]["design_id"]
            and body["value"] == raw and body["bench"] == "sim" and body["units"] == "mm"
            and body["frame"] == "chip_bottom_left_axes" and body["timebase"] == "store_clock"
            and _number(body["observed_at"]) and body["observed_at"] >= submission["ts"]
            and body["source"]["kind"] == "synthetic" and body["source"]["sha256"] == admission.report_sha256
            and body["source"]["event_ids"] == [submission["id"]]
            and body["source"]["reference"] == f"{submission['run_id']}:job.submitted")
    except (ValueError, KeyError, TypeError, IndexError, OverflowError):
        return False
