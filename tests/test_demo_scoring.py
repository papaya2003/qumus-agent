"""Offline fixtures for conservative private terminal-truth comparisons."""
from copy import deepcopy

import pytest

from qumus2.testbench.demo_scoring import score_terminal_truth


@pytest.fixture
def fixture():
    bench = {"stations": {"stock": {"kind": "storage"}, "transfer_stage": {"kind": "stage"}},
             "slots": {f"slot_{i}": {"x": i * 20, "y": 10, "size": [10, 10],
                                    "station": "stock", "holds": "chip"} for i in range(1, 5)}}
    truth = {"schema_version": 1, "mechanics_version": 2, "source": "synthetic_ground_truth",
             "verification": False, "objects": [], "stamps": {}, "collisions": [],
             "manipulator": {}, "surfaces": {}, "transfer_dynamics": {}}
    samples, events = [], []
    for i in range(1, 4):
        chip_id = f"chip_{i:03}"
        material = {1: None, 2: "Gr", 3: "hBN"}[i]
        flakes = [] if i == 1 else [{"id": f"actual_{i}", "material": material, "layers": 1,
                                    "x_mm": 0, "y_mm": 0, "area_um2": 100, "outline_mm": [],
                                    "origin": {"source_chip_id": chip_id}}]
        truth["objects"].append({"id": chip_id, "kind": "chip", "pose": {"x": i * 20, "y": 10, "z": 0, "rotation": 0},
                                 "size_mm": [10, 10], "held_by": None, "on": None,
                                 "material": material, "flakes": flakes})
        samples.append({"id": chip_id, "kind": "chip", "location": f"slot_{i}",
                        "status": "bare" if i == 1 else "searched", "props": {"material": material}})
        if flakes:
            fid = f"catalog_{i}"
            props = {"material": material, "chip_id": chip_id, "sim_id": f"actual_{i}",
                     "images": {"100x": f"recorded/{fid}.png"}}
            samples.append({"id": fid, "kind": "flake", "location": chip_id, "props": props})
            events.append({"kind": "sample.updated", "payload": {"id": fid, "kind": "flake",
                           "location": chip_id, "props": {k: v for k, v in props.items() if k != "sim_id"}}})
    return truth, samples, bench, events


def score(fixture, *, story="collaboration", runs=None):
    truth, samples, bench, events = fixture
    return score_terminal_truth(truth, samples, bench, story=story, events=events, runs=runs or [])


def test_pure_collaboration_compares_terminal_ids_and_catalog_image_receipts(fixture):
    before = deepcopy(fixture)
    result = score(fixture)
    assert not result["problems"]
    assert all(result["checks"].values())
    assert result["unknowns"]  # No narrative/pixel/largest-selection certification.
    assert any("capture-time physical flake-to-pixel identity" in u for u in result["unknowns"])
    assert "score" not in result
    assert fixture == before


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 2),
    ("mechanics_version", 1), ("source", "synthetic"), ("verification", True)])
def test_snapshot_header_fails_closed(fixture, field, value):
    fixture[0][field] = value
    assert score(fixture)["checks"]["truth_schema"] is False


def test_missing_truth_never_vacuously_passes():
    result = score_terminal_truth({}, [], {}, story="complete", events=[], runs=[])
    assert not result["checks"]["truth_schema"]
    assert result["problems"]


def test_duplicate_id_across_object_and_stamp_stack_is_rejected(fixture):
    flake = deepcopy(fixture[0]["objects"][1]["flakes"][0])
    fixture[0]["stamps"]["stamp_001"] = {"stack": [{"flake": flake, "relative_xy_mm": [0, 0], "frame": "stamp_film"}]}
    assert not score(fixture)["checks"]["truth_unique_ids"]


def test_wrong_sim_id_material_and_source_are_detected(fixture):
    fixture[1][2]["props"].update(material="hBN", chip_id="chip_003")
    result = score(fixture)
    assert not result["checks"]["truth_sample_catalog_2_material"]
    assert not result["checks"]["truth_sample_catalog_2_source"]
    fixture[1][2]["props"]["sim_id"] = "invented"
    assert not score(fixture)["checks"]["truth_sample_catalog_2_identity"]


@pytest.mark.parametrize("mutate", ["uncertain", "missing", "moved", "held", "wrong_kind"])
def test_claimed_storage_does_not_substitute_for_truth(fixture, mutate):
    truth, samples, bench, _ = fixture
    if mutate == "uncertain":
        samples[1]["props"]["location_uncertain"] = True
    elif mutate == "missing":
        samples[1]["location"] = None
    elif mutate == "moved":
        truth["objects"][1]["pose"]["x"] += 6
    elif mutate == "held":
        truth["objects"][1]["held_by"] = "arm_right"
    else:
        bench["slots"]["slot_2"]["holds"] = "carrier"
    result = score(fixture)
    assert not result["checks"]["truth_chip_002_stored"]
    assert not result["checks"]["truth_sample_chip_002_location"]


def test_storage_uses_only_configured_size_without_invented_margin(fixture):
    fixture[0]["objects"][1]["pose"]["x"] += 5
    assert score(fixture)["checks"]["truth_chip_002_stored"]
    fixture[0]["objects"][1]["pose"]["x"] += 0.001
    assert not score(fixture)["checks"]["truth_chip_002_stored"]


def test_missing_slot_size_is_unknown(fixture):
    del fixture[2]["slots"]["slot_2"]["size"]
    result = score(fixture)
    assert not result["checks"]["truth_sample_chip_002_location"]
    assert any("chip_002" in u for u in result["unknowns"])


def test_image_paths_without_recorded_receipt_do_not_pass(fixture):
    fixture[3].clear()
    assert not score(fixture)["checks"]["truth_chip_002_image_catalog_receipt"]


def test_reserved_and_downstream_job_checks_use_exact_parameter_values(fixture):
    okay = score(fixture, runs=[{"flow": "inspect", "params": {"note": "chip_001_extra"}}])
    assert okay["checks"]["truth_reserved_chip_unused"]
    result = score(fixture, runs=[{"flow": "stack_device", "params": {"chip_ids": ["chip_001"]}}])
    assert not result["checks"]["truth_reserved_chip_unused"]
    assert not result["checks"]["truth_no_downstream_jobs"]


def test_honest_stop_allows_known_stage_and_held_object_without_storage_claim(fixture):
    fixture[0]["objects"][1]["on"] = "transfer_stage"
    fixture[1][1]["location"] = "transfer_stage"
    fixture[0]["objects"][2]["held_by"] = "arm_right"
    fixture[1][3]["location"] = "arm_right"
    result = score(fixture, story="honest_stop")
    assert not result["problems"]
    assert all(result["checks"].values())
    fixture[1][1]["props"]["location_uncertain"] = True
    result = score(fixture, story="honest_stop")
    assert "truth_sample_chip_002_location" not in result["checks"]
    assert any("uncertain" in u for u in result["unknowns"])


def test_nonempty_stamp_stack_cannot_disappear_from_ledger(fixture):
    flake = fixture[0]["objects"][1]["flakes"].pop()
    fixture[0]["stamps"]["stamp_001"] = {"stack": [{"flake": flake, "relative_xy_mm": [0, 0], "frame": "stamp_film"}]}
    result = score(fixture, story="honest_stop")
    assert not result["checks"]["truth_stack_stamp_001_known"]
    fixture[1][2]["location"] = "stamp_001"
    assert score(fixture, story="honest_stop")["checks"]["truth_stack_stamp_001_known"]
    assert not score(fixture)["checks"]["truth_stack_stamp_001_empty"]


def test_collisions_never_certify_terminal_state(fixture):
    fixture[0]["collisions"].append({"object": "chip_002", "kind": "overlap"})
    assert not score(fixture)["checks"]["truth_no_collisions"]


def test_complete_exact_carrier_flake_order_and_missing_coating_truth(fixture):
    truth, samples, _, _ = fixture
    flakes = [truth["objects"][2]["flakes"].pop(), truth["objects"][1]["flakes"].pop()]
    truth["objects"].append({"id": "reactor_chip_01", "kind": "chip", "pose": {"x": 80, "y": 10},
                             "size_mm": [10, 10], "held_by": None, "on": None, "material": None, "flakes": flakes})
    for s in samples:
        if s["kind"] == "flake":
            s["location"] = "reactor_chip_01"
    samples.append({"id": "reactor_chip_01", "kind": "chip", "location": "slot_4", "props": {
        "material_metal_preparation": {"top_first_flake_ids": ["catalog_3", "catalog_2"],
                                       "layer_order": ["hBN", "Gr", "Pd", "silicon"]}}})
    result = score(fixture, story="complete")
    assert not result["problems"]
    assert all(result["checks"].values())
    assert any("coating/substrate" in u for u in result["unknowns"])
    truth["objects"][-1]["flakes"].reverse()
    result = score(fixture, story="complete")
    assert not result["checks"]["truth_reactor_chip_01_ordered_flakes"]
    assert not result["checks"]["truth_reactor_chip_01_ordered_materials"]


def test_optical_estimates_do_not_become_exact_layer_or_area_claims(fixture):
    fixture[1][2]["props"].update(thickness_label="2L", layers=2, area_um2=103.7)
    result = score(fixture)
    assert not result["problems"]
    assert "truth_sample_catalog_2_layers" not in result["checks"]
    fixture[1][2]["props"]["material_placement"] = {"flake": {"layers": 2, "source_chip_id": "chip_002"}}
    assert not score(fixture)["checks"]["truth_sample_catalog_2_layers"]


def test_missing_private_binding_and_source_stay_unknown_and_fail_completion(fixture):
    del fixture[1][2]["props"]["sim_id"]
    result = score(fixture)
    assert not result["checks"]["truth_sample_catalog_2_identity"]
    assert any("sim_id" in u for u in result["unknowns"])
    fixture[1][2]["props"]["sim_id"] = "actual_2"
    del fixture[1][2]["props"]["chip_id"]
    result = score(fixture)
    assert not result["checks"]["truth_sample_catalog_2_source"]
    assert any("source chip" in u for u in result["unknowns"])


def v2_evidence_digest(value):
    """Fixed schema fixture; no producer or auditor implementation is imported."""
    import hashlib
    import json

    def project(item):
        if isinstance(item, dict):
            return {key: "sha256:"+item.get("image_sha256", item.get("sha256")) if key == "image" and isinstance(value, str)
                    else project(value) for key, value in item.items()}
        return [project(value) for value in item] if isinstance(item, list) else item
    return hashlib.sha256(json.dumps(project(value), sort_keys=True, allow_nan=False).encode()).hexdigest()


def native_process_recording(*, v2=False, transits=False, tail=False, working_xy=None, park_xy=None, clock_epoch=0., witnessed=False, mica=False, witness_depth_mm=.13):
    """Synthetic record claims around the actual native workflow; no execution."""
    import hashlib
    import json

    from qumus2.config import load_yaml
    from qumus2.flows.flow import get_flow
    from qumus2.testbench.render import microscope_photometry_profile
    from scripts.record_three_demos import digest
    from tests.test_workflow_hierarchy import prepared_params

    params = prepared_params().model_dump(mode="json")
    # Historical fixtures keep their captured policy absence when fresh resolution advances.
    params.pop("release_policy", None)
    if witnessed:
        params.update(release_policy="native20-latest-pickup-witness-parked-release-v2", stamp_film="mica" if mica else "PC")
        if mica:
            params.update(drop_temp_c=135., melt_temp_c=150.)
    manifest = get_flow("prepare_material_metal").build_workflow(prepared_params()).manifest()
    profile = load_yaml("verification.yaml")["transfer_sequence"]
    # Historical v1 stays a read-only fixture when the producer advances to v2.
    profile["id"] = params["transfer_profile"] = "v1-normal-sim-v2" if v2 else "v1-normal-sim-v1"
    if v2:
        profile["source_height_mapping"] = "synthetic-native20-source-focus-positive-up-process-reference-v1"
    else:
        profile.pop("source_height_mapping", None)
    manifest["children"][1]["name"] = f"v1_normal_sequence_v1_sim_{'v2' if v2 else 'v1'}_initialize_transfer"
    binding = {"execution_source_sha256": "a" * 64, "procedure_contract": params["procedure_contract"],
               "profile": profile, "profile_sha256": digest(profile), "capture_profile": microscope_photometry_profile(),
               "axis_tolerance_mm": .005, "safe_z_mm": 6.5}
    if v2:
        binding.update(pickup_max_press_mm=.2, target_tolerance_mm=.006, park_xy_mm=[0., 0.] if park_xy is None else list(park_xy))
    if witnessed:
        binding.update(release_policy=params["release_policy"], endpoint_focus_profile=json.loads(json.dumps(load_yaml("verification.yaml")["focus"])))
    state = {"phases": {}, "initializations": {}, "placements": {}, "source_references": {}}
    run = {"id": "transfer", "flow": "prepare_material_metal", "bench": "sim", "params": params,
           "status": "succeeded", "meta": {"workflow": manifest, "normal_transfer": state}}
    events, clock = [], [10.+clock_epoch]
    def emit(kind, payload, *, global_row=False, tick=.1):
        events.append({"id": len(events)+1, "run_id": None if global_row else "transfer", "kind": kind, "payload": deepcopy(payload), "ts": clock[0]})
        clock[0] += tick
        return events[-1]
    def image(label, objective, captured_at):
        record = {"image": label+".png", "sha256": hashlib.sha256(label.encode()).hexdigest(), "captured_at": captured_at,
                "meta": {"objective": objective, "focus_z": .3, "focus_plane": "chip" if objective == 20 else None,
                    "fov_mm": [.96, .64], "calibration": {"source": "synthetic", "frame": "microscope_image",
                        "u_axis": "+world_x", "v_axis": "-world_y", "image_size_px": [480, 320],
                        "mm_per_pixel": [.002, .002], "photometry_profile": deepcopy(binding["capture_profile"])}}}
        if v2:
            record["meta"]["stage_kinematics"] = {"source": "synthetic", "sample_pose": {"x": 1., "y": 2., "z": .5}}
            record["meta"]["calibration"].update(id="synthetic-image-calibration", version="1")
        return record

    def exposure(record, phase, intent=None):
        if witnessed:
            emit("device.command", {"device": "microscope", "command": "capture", "params": {}}, tick=0.)
        return emit("lab.frame", {"microscope_preview": {"phase": phase, "image": record["image"], "image_sha256": record["sha256"],
            "timestamp": record["captured_at"], **deepcopy(record["meta"]), "immutable": True, "verification": False,
            **({"transfer_motion_intent": deepcopy(intent)} if intent is not None else {})}})

    def stamp_props(stack):
        return {"film": "PC", "stack": list(stack), "location_uncertain": False,
            **({"support": {"film_type": "mica", "instruction_id": "synthetic-support-report", "source": "operator_report"},
                "support_history": [{"film_type": "mica", "instruction_id": "synthetic-support-report", "source": "operator_report"}]} if mica else {})}

    def witnessed_pickup(phase, *, partial=False):
        current = 6.5
        depth = .16 if phase["name"] == "v1_pick_up_hbn" else witness_depth_mm
        goal = phase["process_reference"]["minimum_z_mm"]+.05 if partial else phase["process_reference"]["reference_interval_mm"][1]-depth
        def segment(to, speed):
            nonlocal current
            before, at = {"x": working[0], "y": working[1], "z": current}, clock[0]
            duration = abs(to-current)/speed
            emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {
                "axis": "z", "value": to, "speed": speed, "from_pose": before, "duration_bound_s": duration}}, tick=0.)
            for index in (1, 2):
                clock[0] += duration/2
                emit("lab.frame", {"device": "transfer_arm", "command": "move_axis_to", "manipulator": {
                    "x": working[0], "y": working[1], "z": current+(to-current)*index/2}}, tick=0.)
            record = image(phase["name"]+f"-witness-{phase['observations']}", 5, clock[0])
            record["meta"] = deepcopy(phase["reference"]["meta"])
            exposure(record, phase["name"])
            phase.update(last_frame=record, last_pose={"x": working[0], "y": working[1], "z": to}, observed_at=record["captured_at"],
                last_intent={"axis": "z", "from_mm": current, "to_mm": to, "at": at, "speed_mm_s": speed,
                             "duration_bound_s": duration, "macro_target_mm": goal if to < current else 6.7 if to > 6.5 else 6.5})
            phase["observations"] += 1
            phase["travel_mm"] += abs(to-current)
            current = to
            return record
        while current > goal+1e-8:
            covered = segment(max(goal, current-.005), .005)
        phase["coverage"].update(current=covered, actual_holder_pose=deepcopy(phase["last_pose"]), covered=not partial,
            found=True, threshold=40, connectivity=4, minimum_area_mm2=profile["minimum_area_mm2"],
            blur_support_mm=profile["pickup_blur_support_mm"], required_margin_mm=profile["pickup_margin_mm"],
            target_inside=True, edge_distance_mm=.2, area_mm2=.3, region_mask_sha256="c"*64)
        calibration = covered["meta"]["calibration"]
        width, height = calibration["image_size_px"]
        mmx, mmy = calibration["mm_per_pixel"]
        ox, oy = profile["pickup_target_offset_image_mm"]
        phase["coverage"]["target_px"] = [width/2+ox/abs(mmx), height/2+oy/abs(mmy)]
        if not partial:
            while current < 6.7-1e-8:
                segment(min(6.7, current+.01), .01)
            while current > 6.5+1e-8:
                segment(max(6.5, current-.02), .1)

    def autofocus_record(label, focus, holder):
        frame = image(label+"-af", 20, clock[0])
        frame["meta"]["focus_z"] = focus
        axis = {"source": "synthetic", "simulation_only": True, "calibration_id": "synthetic-microscope-focus-axis-v1",
            "objective": 20, "comparison_scope": "same_objective_only", "scale_mm_per_focus_mm": 1.2,
            "mapping_error_mm": .003, "readback_error_mm": .002}
        af = {"objective": 20, "objective_readback": 20, "plane": "chip", "focus_plane": "chip", "peak_observable": True,
            "focus_interval_mm": [focus-.01-.002, focus+.01+.002], "focus_axis_calibration": axis,
            "final_focus_readback_z_mm": focus, "readback_focus_z_mm": focus,
            "optical_calibration": deepcopy(frame["meta"]["calibration"]), "fov_mm": frame["meta"]["fov_mm"],
            "stage_kinematics": deepcopy(frame["meta"]["stage_kinematics"]), "holder_pose": deepcopy(holder),
            "image_sha256": frame["sha256"], "image": frame["image"], "captured_at": frame["captured_at"]}
        samples = [{key: deepcopy(af[key]) for key in ("objective_readback", "focus_axis_calibration", "optical_calibration",
            "stage_kinematics", "holder_pose")} | {"readback_focus_z_mm": z} for z in (focus-.01, focus, focus+.01)]
        af["sampled_passes"] = [{"selected": True, "unresolved_peaks": False, "bracket_mm": [focus-.01, focus+.01], "samples": samples}]
        event = emit("microscope.focus", af)
        return event, frame

    def focused_capture(label, phase, focus, *, parked):
        holder = {"x": binding["park_xy_mm"][0] if parked else working[0],
                  "y": binding["park_xy_mm"][1] if parked else working[1], "z": 6.5}
        event, af_frame = autofocus_record(label, focus, holder)
        record = image(label, 20, clock[0])
        record["meta"]["focus_z"] = focus
        capture = exposure(record, phase)
        summary_keys = ("objective", "objective_readback", "plane", "focus_plane", "peak_observable", "focus_interval_mm",
            "focus_axis_calibration", "final_focus_readback_z_mm", "readback_focus_z_mm", "optical_calibration",
            "stage_kinematics", "holder_pose", "image_sha256", "captured_at")
        record["focus_binding"] = {"profile": profile["id"], "run_id": "transfer", "digest_contract": "qumus-image-content-evidence-v1",
            "optical_path": "parked_unobstructed" if parked else "held_target", "event_id": event["id"],
            "capture_event_id": capture["id"], "event_sha256": v2_evidence_digest(event["payload"]),
            "capture_event_sha256": v2_evidence_digest(capture["payload"]),
            "observation": {key: deepcopy(event["payload"][key]) for key in summary_keys}, "af_frame": af_frame,
            "holder_pose": holder, "park_xy_mm": list(binding["park_xy_mm"]), "clear_z_mm": 6.5, "position_tolerance_mm": .005}
        return record

    def target_view(label, phase, *, parked):
        ref = focused_capture(label, phase, .4, parked=parked)
        frames = [{key: deepcopy(ref[key]) for key in ("image", "sha256", "captured_at", "meta")}]
        for index in (1, 2):
            record = image(label+f"-{index}", 20, clock[0])
            record["meta"]["focus_z"] = .4
            frames.append(record)
            exposure(record, phase)
        return {"profile": profile["id"], "run_id": "transfer", "target_chip_id": params["target_chip_id"],
            "target_xy_mm": params["target_xy_mm"], "requested_stage_xy_mm": [1., 2.],
            "stage_kinematics": deepcopy(ref["meta"]["stage_kinematics"]), "focus_reference": ref, "frames": frames,
            "materials_found": {material: 0 if parked else 1 for material in params["materials"].values()}}

    def transit_record(name, xy, metadata):
        initial, target = 6.5004, 6.5

        def readback(pose):
            emit("lab.frame", {"device": "transfer_arm", "command": "move_axis_to", "geometry": {"source": "synthetic",
                "microscope": {"source": "synthetic", "stamp_axes_frame": "transfer_arm", "stamp_axes_xyz_mm": pose}}})
            emit("lab.frame", {"device": "transfer_arm", "command": "move_axis_to", "microscope_preview": {
                "image": "camera-preview", "objective": 5, "timestamp": clock[0], "immutable": True,
                "verification": False, "source": "synthetic_scene"}})
        for axis, value in zip(("x", "y"), xy, strict=True):
            emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {"axis": axis, "value": value}})
            readback({"x": xy[0], "y": xy[1], "z": initial})
        ref = image(name+"-before", metadata["objective"], clock[0])
        ref["meta"] = deepcopy(metadata)
        baseline = exposure(ref, name)
        at = clock[0]
        emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {"axis": "z", "value": target, "speed": .1}})
        pose = {"x": xy[0], "y": xy[1], "z": target}
        readback(pose)
        after = image(name+"-after", metadata["objective"], clock[0])
        after["meta"] = deepcopy(metadata)
        exposure(after, name)
        state["phases"][name] = {"name": name, "profile": profile["id"], "started_at": ref["captured_at"],
            "observations": 2, "travel_mm": initial-target, "difference_active": False, "reference": ref,
            "reference_event": {"event_id": baseline["id"], "event_sha256": v2_evidence_digest(baseline["payload"])},
            "holder_xy": list(xy), "last_frame": after, "observed_at": after["captured_at"], "last_pose": pose,
            "last_intent": {"axis": "z", "from_mm": initial, "to_mm": target, "speed_mm_s": .1,
                "duration_bound_s": (initial-target)/.1, "macro_target_mm": target, "at": at}}
        return pose

    def tail_records(phase):
        import math

        name = phase["name"]
        minimum = phase["process_reference"]["minimum_z_mm"]
        current, target = minimum+.05, math.nextafter(minimum+.005, math.inf)
        capture = next(event for event in reversed(events) if event["kind"] == "lab.frame" and
                       event["payload"].get("microscope_preview", {}).get("image") == phase["last_frame"]["image"])
        if not witnessed:
            next(event for event in reversed(events) if event["kind"] == "device.command")["payload"]["params"].update(value=current, speed=.1)
        admission = {"phase": name, "profile": profile["id"], "rule": "final-process-budget-prefix-v1", "from_readback_z_mm": current,
            "requested_delta_mm": -.1, "requested_target_mm": current-.1, "admitted_delta_mm": target-current, "admitted_target_mm": target,
            "minimum_z_mm": minimum, "readback_tolerance_mm": .005, "global_z_interval_mm": [0., 10.],
            "process_reference_sha256": v2_evidence_digest(phase["process_reference"]), "reference_sha256": phase["reference"]["sha256"],
            "admission_observation_count": phase["observations"], "admitted_at": clock[0],
            "capture_event": {"event_id": capture["id"], "event_sha256": v2_evidence_digest(capture["payload"])}}
        event = emit("stack.pickup_macro_admission", admission)
        phase["final_partial_macro"] = {"admission": deepcopy(admission), "event_id": event["id"], "event_sha256": v2_evidence_digest(admission), "status": "covered"}

        def segment(to, speed, tagged):
            nonlocal current
            duration = abs(to-current)/speed
            intent = {"axis": "z", "from_mm": current, "to_mm": to, "speed_mm_s": speed,
                "duration_bound_s": duration, "macro_target_mm": target if tagged else 6.5, "at": clock[0]}
            if tagged:
                intent.update(requested_macro_target_mm=admission["requested_target_mm"], macro_admission_event_id=event["id"])
            emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {"axis": "z", "value": to, "speed": speed,
                **({"from_pose": {"x": working[0], "y": working[1], "z": current}, "duration_bound_s": duration} if witnessed else {})}}, tick=0. if witnessed else .1)
            if tagged:
                clock[0] += duration/2
                emit("lab.frame", {"device": "transfer_arm", "command": "move_axis_to",
                    **({"manipulator": {"x": working[0], "y": working[1], "z": (current+to)/2}} if witnessed else {}), "geometry": {"source": "synthetic",
                    "microscope": {"source": "synthetic", "stamp_axes_frame": "transfer_arm",
                        "stamp_axes_xyz_mm": {"x": working[0], "y": working[1], "z": (current+to)/2}}}}, tick=0. if witnessed else .1)
            clock[0] += duration/2 if tagged else duration
            pose = {"x": working[0], "y": working[1], "z": to}
            emit("lab.frame", {"device": "transfer_arm", "command": "move_axis_to", **({"manipulator": pose} if witnessed else {}), "geometry": {"source": "synthetic",
                "microscope": {"source": "synthetic", "stamp_axes_frame": "transfer_arm", "stamp_axes_xyz_mm": pose}}}, tick=0. if witnessed else .1)
            record = image(name+f"-tail-motion-{phase['observations']}", 5, clock[0])
            record["meta"] = deepcopy(phase["reference"]["meta"])
            exposure(record, name, intent if tagged else None)
            phase.update(last_frame=record, last_pose=pose, last_intent=intent, observed_at=record["captured_at"])
            phase["observations"] += 1
            phase["travel_mm"] += abs(to-current)
            current = to
            return record
        start = current
        for index in range(1, 2 if witnessed else 8):
            covered = segment(start+(target-start)*index/7, .005, True)
        phase["coverage"]["current"] = covered
        if witnessed:
            phase["coverage"]["actual_holder_pose"] = deepcopy(phase["last_pose"])
        while current < 6.7-1e-8:
            segment(min(current+.02, 6.7), .1, False)
        while current > 6.5+1e-8:
            segment(max(current-.02, 6.5), .1, False)
    working = [.02, -.01] if working_xy is None else list(working_xy)
    first = "v1_first_contact_"+params["stamp_id"]
    release = "release_material_metal_stack"
    phase_names = [first, *[f"v1_pick_up_{fid}" for fid in params["flake_ids"]], release]
    emit("job.submitted", {"flow": run["flow"], "params": params})
    emit("job.workflow", {"workflow": manifest, "reason": "submitted"})
    leaves = [leaf for child in manifest["children"] for leaf in child["checkpoints"]]
    for leaf in leaves:
        name = leaf["name"]
        emit("step.started", {"seq": leaf["seq"], "name": name})
        measurements = {}
        transit_expected = None
        if witnessed and name == "load_stamp":
            measurements = {"found_at": "stamp_handoff"}
            transit_expected = {"sample": params["stamp_id"], "at": "stamp_handoff"}
        if witnessed and name == "grip_stamp":
            emit("device.command", {"device": "transfer_arm_vacuum", "command": "set", "params": {"on": True}})
        if witnessed and name == "return_stamp":
            emit("device.command", {"device": "transfer_arm_vacuum", "command": "set", "params": {"on": False}})
            emit("sample.updated", {"id": params["stamp_id"], "kind": "stamp", "location": "right_stamp_1", "status": "used", "props": stamp_props([])}, global_row=True)
        if name.startswith("refind_"):
            fid = name.removeprefix("refind_")
            original = image(fid+"-original", 20, clock[0]-1)
            current = image(fid+"-current", 20, clock[0])
            post = (focused_capture(fid+"-post-shift", name+"_source_reference", .3 if fid == "hbn" else .34, parked=True)
                    if v2 else image(fid+"-post-shift", 20, clock[0]))
            identity = {"source_id": fid, "source_chip_id": params["flake_chips"][fid], "original": original, "current": current}
            measurements = {"source_identity": deepcopy(identity)}
            state["source_references"][fid] = {**post, "source_identity": {
                **identity, "post_shift_capture": {key: deepcopy(post[key]) for key in ("image", "sha256", "captured_at", "meta")}, "design_shift_chip_mm": [0., 0.]}}
            if v2:
                state["source_references"][fid].update(source_id=fid, source_chip_id=params["flake_chips"][fid], target_offset_mm=[0., 0.])
            else:
                emit("lab.frame", {"microscope_preview": {"phase": name+"_source_reference", "image": post["image"],
                "timestamp": post["captured_at"], "calibration": post["meta"]["calibration"], "immutable": True, "verification": False}})
        if v2 and name == "go_to_target":
            state["target_reference"] = target_view("target-before", "go_to_target_before_release", parked=True)
            measurements = {"on_chip": True, "sharpness": 10., "target_reference_sha256": v2_evidence_digest(state["target_reference"])}
        if name.startswith("v1_initialize_"):
            state["initializations"][name] = {"applied": True, "stamp_id": params["stamp_id"], "profile": profile["id"], "substrate_focus_mm": 0.}
            measurements = {"stamp_id": params["stamp_id"], "profile": profile["id"], "objective": 5,
                            "focus_mm": profile["focus_offset_mm"], "actual_holder_pose": {"x": working[0], "y": working[1], "z": 6.5}}
            if v2 and transits:
                fid = name.removeprefix("v1_initialize_")
                ref = state["target_reference"]["focus_reference"] if fid == "target" else state["source_references"][fid]
                focus = ref["meta"]["focus_z"]
                state["initializations"][name]["substrate_focus_mm"] = focus
                metadata = image(name+"-context", 5, clock[0])["meta"]
                metadata["focus_z"] = focus
                xy = [.1, .2] if fid == params["flake_ids"][0] else working
                measurements.update(focus_mm=focus+profile["focus_offset_mm"], actual_holder_pose=transit_record(name+"_engage_z", xy, metadata))
        if v2 and transits and (name.startswith("park_stamp_") or name in ("manipulator_to_load", "manipulator_to_load_for_return")):
            if name.startswith("park_stamp_"):
                fid = "target" if name == "park_stamp_after_drop" else name.removeprefix("park_stamp_")
                ref = (state["phases"][release]["endpoint"]["target_after"]["frames"][-1] if fid == "target" else
                       state["phases"][f"v1_pick_up_{fid}"]["endpoint"]["source_frames"][-1])
            else:
                ref = image(name+"-context", 20, clock[0])
            metadata = deepcopy(ref["meta"])
            if name.startswith("park_stamp_"):
                metadata["focus_plane"] = None  # Park is outside the source optical path.
            pose = transit_record(name+"_z", binding["park_xy_mm"], metadata)
            measurements = {**pose, "error_mm": 0.}
            transit_expected = {**pose, "tolerance_mm": .005}
        if name in phase_names:
            frames = []
            witnessed_pickup_phase = witnessed and name.startswith("v1_pick_up_")
            for index in range(1 if witnessed_pickup_phase else 3):
                if index == 2:
                    emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {"axis": "z", "value": 6.5}})
                record = image(name+f"-{index}", 5, clock[0])
                if v2 and transits:
                    fid = params["flake_ids"][0] if name == first else "target" if name == release else name.removeprefix("v1_pick_up_")
                    record["meta"]["focus_z"] = state["initializations"][f"v1_initialize_{fid}"]["substrate_focus_mm"]+profile["focus_offset_mm"]
                frames.append(record)
                captured = exposure(record, name)
                if index == 0:
                    baseline = captured
            phase = {"name": name, "profile": profile["id"], "started_at": frames[0]["captured_at"], "observations": len(frames),
                     "travel_mm": 0. if witnessed_pickup_phase else .02, "reference": frames[0], "last_frame": frames[-1], "observed_at": frames[-1]["captured_at"],
                     "complete": True, "difference_active": False, "holder_xy": list(working), "last_pose": {"x": working[0], "y": working[1], "z": 6.5},
                     "last_intent": {"axis": "z", "from_mm": 6.49, "to_mm": 6.5, "at": frames[0 if witnessed_pickup_phase else 1]["captured_at"],
                                     "speed_mm_s": .1, "duration_bound_s": .1, "macro_target_mm": 6.5}}
            state["phases"][name] = phase
            if v2:
                phase["reference_event"] = {"event_id": baseline["id"], "event_sha256": v2_evidence_digest(baseline["payload"])}
            if name == first:
                old, middle, new = ({"x": working[0]-.02, "y": working[1]+.01, "z": 6.5},
                                    {"x": working[0], "y": working[1]+.01, "z": 6.5},
                                    {"x": working[0], "y": working[1], "z": 6.5})
                inputs = [{"axis": axis, "before": a, "after": b, "requested_mm": b[axis], "readback_at": clock[0],
                           "actual_delta_mm": {key: b[key]-a[key] for key in a}}
                          for axis, a, b in [("x", old, middle), ("y", middle, new)]]
                candidate = {"found": True, "kind": "observed_first_contact_candidate", "profile": profile["id"],
                             "reference": frames[0], "current": frames[1], "reaim_readback": new, "reaim_inputs": inputs}
                phase["xy_inputs"] = deepcopy(inputs)
                for item in inputs:
                    emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {"axis": item["axis"], "value": item["requested_mm"]}})
                phase["first_contact_region"] = candidate
                state["placements"][params["stamp_id"]] = {"stamp_id": params["stamp_id"], "run_id": "transfer",
                    "profile": profile["id"], "working_xy_mm": working, "first_contact_step": first}
                if v2:
                    candidate["actual_holder_pose"] = {"x": old["x"], "y": old["y"], "z": 6.0}
                    anchor = state["source_references"][params["flake_ids"][0]]
                    state["placements"][params["stamp_id"]].update(candidate_z_mm=6., source_anchor={"source_id": params["flake_ids"][0],
                        "image_sha256": anchor["sha256"], "focus_binding_sha256": v2_evidence_digest(anchor["focus_binding"])})
                measurements = {"first_contact_region": candidate}
            elif name == release:
                phase.update(outputs_off=True, peak_c=172., withdrawal={"actual_readback": {"x": working[0], "y": working[1], "z": 6.5}})
                measurements = {"release_confirmed": True}
                if v2:
                    after = target_view("target-after", name+"_target_after", parked=False)
                    measurements.update(profile=profile["id"],
                        method="target_material_appearance", target_before_sha256=v2_evidence_digest(state["target_reference"]),
                        target_after=after, materials_appeared={material: True for material in params["materials"].values()},
                        layers_found=after["materials_found"], peak_c=172., stamp_evaluated=False, stamp_flakes_seen=None,
                        process_confirmation=True, full_identity_completeness_observed=False, registry_chemistry_observed=False)
                    phase["endpoint"] = deepcopy(measurements)
            else:
                phase["coverage"] = {"covered": True, "transfer_confirmed": False, "reference": frames[0], "current": frames[0] if witnessed_pickup_phase else frames[1]}
                measurements = {"coverage": phase["coverage"], "transfer_confirmed": True}
                if v2:
                    fid = name.removeprefix("v1_pick_up_")
                    source = state["source_references"][fid]
                    original = state["source_references"][params["flake_ids"][0]]
                    low0, high0 = original["focus_binding"]["observation"]["focus_interval_mm"]
                    low, high = source["focus_binding"]["observation"]["focus_interval_mm"]
                    delta = [0., 0.] if fid == params["flake_ids"][0] else [1.2*(low-high0)-.006, 1.2*(high-low0)+.006]
                    interval = [6.+value for value in delta]
                    phase["process_reference"] = {"profile": profile["id"], "mapping": profile["source_height_mapping"],
                        "digest_contract": "qumus-image-content-evidence-v1", "source_id": fid, "anchor_source_id": params["flake_ids"][0],
                        "anchor_focus_binding_sha256": v2_evidence_digest(original["focus_binding"]),
                        "current_focus_binding_sha256": v2_evidence_digest(source["focus_binding"]), "first_candidate_z_mm": 6.,
                        "delta_interval_mm": delta, "reference_interval_mm": interval, "max_press_mm": .2, "minimum_z_mm": interval[1]-.2}
                    if witnessed:
                        phase["process_reference"]["scope"] = "Optical process reference only; not contact/gap"
                        witnessed_pickup(phase, partial=tail and fid == "gr")
                    if tail and fid == "gr":
                        tail_records(phase)
                        if witnessed:
                            phase["coverage"]["covered"] = True
                        measurements["coverage"] = phase["coverage"]
                    focus = source["meta"]["focus_z"]
                    af, _ = autofocus_record(name+"-endpoint", focus, phase["last_pose"])
                    source_frames = []
                    for index in range(2):
                        record = image(name+f"-source-after-{index}", 20, clock[0])
                        record["meta"]["focus_z"] = focus
                        source_frames.append(record)
                        clock[0] += .1
                    measurements.update(profile=profile["id"], source_id=fid, source_reference_sha256=source["sha256"],
                        method="source_disappearance", flake_on_chip=False, source_frames=source_frames, focus_event_id=af["id"],
                        source_detection_count=0, stamp_evaluated=False, flake_on_stamp=None, process_confirmation=True,
                        no_transfer_confirmed=None, full_identity_completeness_observed=False, registry_chemistry_observed=False)
                    phase["endpoint"] = deepcopy(measurements)
        observation = {"verdict": "ok", "measurements": measurements}
        if v2:
            images = ([frame["image"] for frame in measurements["source_frames"]] if name.startswith("v1_pick_up_")
                else [frame["image"] for frame in state["target_reference"]["frames"]+measurements["target_after"]["frames"]]
                if name == release else [measurements["first_contact_region"]["current"]["image"]] if name == first else [])
            observation.update(expected={}, images=images)
            if transit_expected is not None:
                observation["expected"] = transit_expected
            if name == "go_to_target":
                observation.update(expected={"stage_xy": [1., 2.], "on_chip": params["target_chip_id"], "min_sharpness": 1.},
                    images=[frame["image"] for frame in state["target_reference"]["frames"]])
        verified = emit("step.verified", {"seq": leaf["seq"], "name": name,
            **({"verdict": "not_verifiable", "reason": "No vacuum sensor; declared command/ledger continuity only"} if witnessed and name == "grip_stamp" else observation)})
        if witnessed and name in ("load_stamp", "grip_stamp"):
            emit("sample.updated", {"id": params["stamp_id"], "kind": "stamp", "location": "stamp_handoff" if name == "load_stamp" else "transfer_arm",
                                   "status": "fresh" if name == "load_stamp" else "in_use", "props": stamp_props([])}, global_row=True)
        if v2 and name == "go_to_target":
            state["target_reference_verified"] = {"event_id": verified["id"], "observation_sha256": v2_evidence_digest(
                {key: verified["payload"][key] for key in ("seq", "name", "verdict", "measurements", "expected")})}
        if v2 and (name.startswith("v1_pick_up_") or name == release):
            phase["endpoint_verified"] = {"event_id": verified["id"], "observation_sha256": v2_evidence_digest(
                {key: verified["payload"][key] for key in ("seq", "name", "verdict", "measurements", "expected")})}
        if v2 and name == first:
            candidate = phase["first_contact_region"]
            fact = emit("lab.evidence", {"schema_version": 1, "assertion_id": "fact-first-contact", "subject": params["stamp_id"],
                "property": "first_contact_region", "bench": "sim", "value": candidate, "units": "px,mm", "frame": "microscope_image",
                "observed_at": candidate["current"]["captured_at"], "timebase": "store_clock", "status": "asserted",
                "calibration_id": "synthetic-image-calibration", "calibration_version": "1",
                "source": {"kind": "synthetic", "reference": "transfer:"+name, "sha256": candidate["current"]["sha256"]}})
            state["placements"][params["stamp_id"]]["candidate_evidence"] = {"event_id": fact["id"], "event_sha256": v2_evidence_digest(fact["payload"])}
        if name.startswith("v1_pick_up_"):
            if witnessed:
                emit("sample.updated", {"id": params["stamp_id"], "kind": "stamp", "location": "transfer_arm", "status": "in_use",
                                       "props": stamp_props(params["flake_ids"][:params["flake_ids"].index(name.removeprefix("v1_pick_up_"))+1])}, global_row=True)
            emit("stack.pickup_endpoint", {"flake_id": name.removeprefix("v1_pick_up_"), "stamp_id": params["stamp_id"],
                                           "procedure": params["procedure_contract"], "observation": observation})
        if name == release:
            emit("material_metal.released", {"chip_id": params["target_chip_id"], "top_first_flake_ids": params["flake_ids"], "measurement": measurements})
        emit("step.finished", {"seq": leaf["seq"], "name": name, "status": "done"})
    emit("job.status", {"status": "succeeded"})
    return {"events": events, "runs": {"transfer": run}, "meta": {"execution_source_sha256": "a"*64,
            "acceptance_evidence": {"v1_process_binding": binding}}}


def process_score(recording, story="complete"):
    from qumus2.testbench.demo_scoring import audit_recorded_v1_process

    return audit_recorded_v1_process(recording["events"], recording["runs"], story=story,
        binding=recording["meta"]["acceptance_evidence"].get("v1_process_binding"), execution_source=recording["meta"]["execution_source_sha256"])


def test_recorded_process_is_pure_and_separate_from_physical_truth():
    recording = native_process_recording()
    before = deepcopy(recording)
    result = process_score(recording)
    assert result["consistent"] is True, result
    assert result["state"] == "consistent" and result["limitations"]
    assert recording == before
    assert "independent_world_truth" not in result


def test_v2_visual_process_uses_first_source_intervals_and_keeps_stamp_unknown():
    recording = native_process_recording(v2=True)
    before = deepcopy(recording)
    result = process_score(recording)
    assert result["consistent"] is True, result
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    assert state["phases"]["v1_pick_up_hbn"]["process_reference"]["delta_interval_mm"] == [0., 0.]
    later = state["phases"]["v1_pick_up_gr"]["process_reference"]
    assert later["delta_interval_mm"] == pytest.approx([.0132, .0828])
    assert later["minimum_z_mm"] == pytest.approx(5.8828)
    assert state["phases"]["v1_pick_up_gr"]["endpoint"]["flake_on_stamp"] is None
    assert state["phases"]["release_material_metal_stack"]["endpoint"]["stamp_flakes_seen"] is None
    assert recording == before and "independent_world_truth" not in result


def target_relative_release_recording(*, complete=False, mica=False, witnessed=False, tail=False, witness_depth_mm=.13):
    """Start with the verified historical antecedents; add a fresh release plan."""
    import json

    from qumus2.config import load_yaml

    recording = native_process_recording(v2=True, working_xy=[40., 90.] if complete else None,
        park_xy=[150., 10.] if complete else None, clock_epoch=1_800_000_000. if complete else 0., witnessed=witnessed, mica=mica, tail=tail, witness_depth_mm=witness_depth_mm)
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    policy = "native20-latest-pickup-witness-parked-release-v2" if witnessed else "native20-target-reference-parked-release-v1"
    run["params"]["release_policy"] = policy
    if mica:
        run["params"].update(stamp_film="mica", drop_temp_c=135., melt_temp_c=150.)
    next(event for event in recording["events"] if event["kind"] == "job.submitted")["payload"]["params"]["release_policy"] = policy
    if mica:
        next(event for event in recording["events"] if event["kind"] == "job.submitted")["payload"]["params"].update(stamp_film="mica", drop_temp_c=135., melt_temp_c=150.)
    binding = recording["meta"]["acceptance_evidence"]["v1_process_binding"]
    binding.update(release_policy=policy, endpoint_focus_profile=json.loads(json.dumps(load_yaml("verification.yaml")["focus"])))
    phase = state["phases"]["release_material_metal_stack"]
    phase["release_policy"] = policy
    source = state["source_references"][run["params"]["flake_ids"][0]]
    target = state["target_reference"]["focus_reference"]
    low0, high0 = source["focus_binding"]["observation"]["focus_interval_mm"]
    low, high = target["focus_binding"]["observation"]["focus_interval_mm"]
    delta = [1.2*(low-high0)-.006, 1.2*(high-low0)+.006]
    interval = [6.+value for value in delta]
    phase["process_reference"] = {"profile": binding["profile"]["id"], "mapping": binding["profile"]["source_height_mapping"],
        "digest_contract": "qumus-image-content-evidence-v1", "source_id": run["params"]["target_chip_id"],
        "anchor_source_id": run["params"]["flake_ids"][0], "anchor_focus_binding_sha256": v2_evidence_digest(source["focus_binding"]),
        "current_focus_binding_sha256": v2_evidence_digest(target["focus_binding"]), "first_candidate_z_mm": 6.,
        "delta_interval_mm": delta, "reference_interval_mm": interval, "max_press_mm": .2, "minimum_z_mm": interval[1]-.2}
    if witnessed:
        phase["process_reference"]["scope"] = "Optical process reference only; not contact/gap"
    nominal, requested = binding["profile"]["nominal_contact_z_mm"], binding["profile"]["release_z_mm"]
    phase["release_reference"] = {"policy": policy, "target_chip_id": run["params"]["target_chip_id"],
        "target_xy_mm": run["params"]["target_xy_mm"], "target_before_sha256": v2_evidence_digest(state["target_reference"]),
        "target_verified": deepcopy(state["target_reference_verified"]), "nominal_reference_z_mm": nominal,
        "nominal_requested_z_mm": requested, "relative_intent_mm": requested-nominal,
        "requested_z_mm": interval[1]+(requested-nominal), "readback_tolerance_mm": .005}
    if witnessed:
        pickup = state["phases"]["v1_pick_up_"+run["params"]["flake_ids"][-1]]
        capture = next(row for row in recording["events"] if row["kind"] == "lab.frame"
                       and row["payload"].get("microscope_preview", {}).get("image") == pickup["coverage"]["current"]["image"])
        command = next(row for row in reversed(recording["events"]) if row["id"] < capture["id"] and row["kind"] == "device.command"
                       and row["payload"].get("device") == "transfer_arm" and row["payload"].get("command") == "move_axis_to")
        d = command["payload"]["params"]["value"]-pickup["process_reference"]["reference_interval_mm"][1]
        phase["release_reference"].update(relative_intent_mm=d, requested_z_mm=interval[1]+d)
    if not complete:
        return recording

    import math

    events = recording["events"]
    name = phase["name"]
    old_finish = next(row for row in events if row["kind"] == "step.finished" and row["payload"]["name"] == name)
    suffix = deepcopy([row for row in events if row["id"] > old_finish["id"]])
    init_verified = next(row for row in events if row["kind"] == "step.verified" and row["payload"]["name"] == "v1_initialize_target")
    init_finish = next(row for row in events if row["kind"] == "step.finished" and row["payload"]["name"] == "v1_initialize_target")
    started = next(row for row in events if row["kind"] == "step.started" and row["payload"]["name"] == name)
    recording["events"] = events = [row for row in events if row["id"] < init_verified["id"]]
    clock = [events[-1]["ts"]+.01]
    current = {"x": 40., "y": 90., "z": 6.5}

    def emit(kind, payload, *, tick=.01):
        row = {"id": len(events)+1, "run_id": "transfer", "kind": kind, "payload": deepcopy(payload), "ts": clock[0]}
        events.append(row)
        clock[0] += tick
        return row

    def public_pose():
        return emit("lab.frame", {"device": "transfer_arm", "command": "move_axis_to", "manipulator": deepcopy(current)}, tick=0.)

    emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {
        "axis": "z", "value": 6.5, "speed": .1, "from_pose": deepcopy(current), "duration_bound_s": 0.}})
    public_pose()
    emit(init_verified["kind"], init_verified["payload"])
    emit(init_finish["kind"], init_finish["payload"])
    emit(started["kind"], started["payload"])
    original_ref = deepcopy(phase["reference"])
    counter = [0]

    def frame(objective=5):
        counter[0] += 1
        record = deepcopy(original_ref if objective == 5 else target)
        record.pop("focus_binding", None)
        record.update(image=f"relative-release-{counter[0]}.png", sha256=f"{counter[0]+10000:064x}", captured_at=clock[0])
        record["meta"]["objective"] = objective
        return record

    def capture(phase_name=name, *, objective=5):
        emit("device.command", {"device": "microscope", "command": "capture", "params": {}}, tick=0.)
        record = frame(objective)
        row = emit("lab.frame", {"device": "microscope", "command": "capture", "microscope_preview": {
            "phase": phase_name, "image": record["image"], "image_sha256": record["sha256"], "timestamp": record["captured_at"],
            **deepcopy(record["meta"]), "immutable": True, "verification": False}})
        phase.update(last_frame=record, observed_at=record["captured_at"], last_pose=deepcopy(current))
        phase["observations"] += 1
        return record, row

    phase.update(observations=0, travel_mm=0., complete=True, outputs_off=True, peak_c=150. if mica else 172., park_transport=[], holder_xy=[40., 90.])
    reference, first_capture = capture()
    phase.update(reference=reference, reference_event={"event_id": first_capture["id"], "event_sha256": v2_evidence_digest(first_capture["payload"])},
                 started_at=reference["captured_at"])
    emit("device.command", {"device": "stamp_heater", "command": "set_target", "params": {"celsius": 135.5 if mica else 155.5}})
    capture()

    def zmove(target_z, speed):
        before, at = deepcopy(current), clock[0]
        duration = abs(target_z-current["z"])/speed
        emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {
            "axis": "z", "value": target_z, "speed": speed, "from_pose": before, "duration_bound_s": duration}}, tick=0.)
        for index in (1, 2):
            clock[0] += duration/2
            current["z"] = before["z"]+(target_z-before["z"])*index/2
            public_pose()
        emit("lab.frame", {"device": "transfer_arm", "command": "move_axis_to", "microscope_preview": {
            "image": "motion-preview", "timestamp": clock[0], "immutable": True, "verification": False}}, tick=0.)
        phase["travel_mm"] += abs(current["z"]-before["z"])
        phase["last_intent"] = {"axis": "z", "from_mm": before["z"], "to_mm": target_z, "at": at,
            "speed_mm_s": speed, "duration_bound_s": duration, "macro_target_mm": target_z}
        capture()

    requested = phase["release_reference"]["requested_z_mm"]
    while current["z"] > requested+1e-8:
        zmove(max(requested, current["z"]-.005), .005)
    emit("device.command", {"device": "stamp_heater", "command": "set_target", "params": {"celsius": 150.5 if mica else 170.5}})
    capture()
    from_pose = deepcopy(current)
    while current["z"] < 6.5-1e-8:
        zmove(min(6.5, current["z"]+.01), .01)
    phase["withdrawal"] = {"from_readback": from_pose, "requested_sim_target_mm": 6.5, "actual_readback": deepcopy(current)}
    emit("device.command", {"device": "stamp_heater", "command": "off", "params": {}})
    for axis, destination in zip(("x", "y"), binding["park_xy_mm"], strict=True):
        start, origin = clock[0], deepcopy(current)
        duration = abs(destination-current[axis])/2
        command = emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {
            "axis": axis, "value": destination, "speed": None, "from_pose": origin, "duration_bound_s": duration}}, tick=0.)
        count = math.ceil(duration/.2)
        for index in range(1, count+1):
            clock[0] += duration/count
            current[axis] = origin[axis]+(destination-origin[axis])*index/count
            public_pose()
        end = clock[0]
        preview = emit("lab.frame", {"device": "transfer_arm", "command": "move_axis_to", "microscope_preview": {
            "image": "park-preview", "timestamp": end, "immutable": True, "verification": False}}, tick=0.)
        stream = [{"event_id": row["id"], "timestamp": row["ts"], "pose": deepcopy(row["payload"].get("manipulator"))}
                  for row in events[events.index(command)+1:]]
        phase["park_transport"].append({"axis": axis, "from_pose": origin, "requested_mm": destination,
            "duration_bound_s": duration, "started_at": start, "actual_pose": deepcopy(current), "ended_at": end,
            "encoder_end_event_id": preview["id"], "encoder_event_count": len(stream), "encoder_event_sha256": v2_evidence_digest(stream),
            "clock_roundoff_s": (len(stream)+1)*max(math.ulp(start), math.ulp(end)),
            "command_event": {"event_id": command["id"], "event_sha256": v2_evidence_digest(command["payload"])}})
    phase["holder_xy"] = [current["x"], current["y"]]
    capture()
    phase["park"] = {"requested_pose": {"x": 150., "y": 10., "z": 6.5}, "actual_holder_pose": deepcopy(current), "completed_at": clock[0]}
    endpoint_start = events[-1]["id"]
    emit("device.command", {"device": "microscope", "command": "set_objective", "params": {"magnification": 20}})
    seed = target["meta"]["focus_z"]
    emit("device.command", {"device": "microscope", "command": "set_focus", "params": {"z": seed}})
    af = deepcopy(next(row for row in events if row["kind"] == "microscope.focus")["payload"])
    af.update(holder_pose=deepcopy(current), stage_kinematics=deepcopy(target["meta"]["stage_kinematics"]),
        optical_calibration=deepcopy(target["meta"]["calibration"]), fov_mm=target["meta"]["fov_mm"], final_focus_readback_z_mm=seed,
        readback_focus_z_mm=seed, focus_interval_mm=[seed-.012, seed+.012])
    af["sampled_passes"] = []
    def raw_capture():
        emit("device.command", {"device": "microscope", "command": "capture", "params": {}}, tick=.02)
        phase["observations"] += 1
        return clock[0]-.02
    raw_capture()
    for half in (.08, .02):
        samples = []
        for index in range(9):
            z = seed-half+2*half*index/8
            emit("device.command", {"device": "microscope", "command": "set_focus", "params": {"z": z}})
            at = raw_capture()
            samples.append({key: deepcopy(af[key]) for key in ("objective_readback", "focus_axis_calibration", "optical_calibration", "stage_kinematics", "holder_pose")}
                           | {"requested_focus_z_mm": z, "readback_focus_z_mm": z, "captured_at": at, "image_sha256": f"{len(events):064x}"})
        af["sampled_passes"].append({"selected": True, "unresolved_peaks": False, "bracket_mm": [seed-.01, seed+.01], "samples": samples})
        emit("device.command", {"device": "microscope", "command": "set_focus", "params": {"z": seed}})
        final_at = raw_capture()
    af_frame = frame(20)
    af_frame["captured_at"] = final_at
    af.update(captured_at=final_at, image_sha256=af_frame["sha256"], image=af_frame["image"])
    af_event = emit("microscope.focus", af)
    after_ref, after_capture = capture(name+"_target_after", objective=20)
    summary = target["focus_binding"]["observation"]
    after_ref["focus_binding"] = {**deepcopy(target["focus_binding"]), "event_id": af_event["id"], "capture_event_id": after_capture["id"],
        "event_sha256": v2_evidence_digest(af), "capture_event_sha256": v2_evidence_digest(after_capture["payload"]),
        "observation": {key: deepcopy(af[key]) for key in summary}, "af_frame": af_frame, "holder_pose": deepcopy(current)}
    frames = [{key: deepcopy(after_ref[key]) for key in ("image", "sha256", "captured_at", "meta")}]
    for _ in range(2):
        record, _ = capture(name+"_target_after", objective=20)
        frames.append(record)
    after = {**deepcopy(state["target_reference"]), "focus_reference": after_ref, "frames": frames,
             "materials_found": {material: 1 for material in run["params"]["materials"].values()}}
    measured = {**deepcopy(phase["endpoint"]), "target_after": after, "peak_c": phase["peak_c"],
                "target_before_sha256": v2_evidence_digest(state["target_reference"])}
    phase["endpoint"] = deepcopy(measured)
    phase["release_budget"] = {"stage": "observed", "start_event_id": first_capture["id"], "endpoint_limit": 24,
        "endpoint_observations": 24, "park_observations": 1, "thermal_observations": 2, "park_complete": True,
        "reserved_observations": 0, "reserved_seconds": 0., "reserved_z_travel_mm": 0., "thermal_allowance_s": 0.,
        "checked_at": clock[0], "endpoint_status": "observed", "endpoint_start_event_id": endpoint_start, "endpoint_end_event_id": events[-1]["id"]}
    phase["release_budget"]["thermal_allowance_s"] = min(binding["profile"]["max_total_s"]-(clock[0]-phase["started_at"]),
        (binding["profile"]["max_observations"]-phase["observations"])*(binding["profile"]["max_unobserved_s"]-binding["profile"]["capture_reserve_s"]))
    verified = emit("step.verified", {"seq": started["payload"]["seq"], "name": name, "verdict": "ok", "measurements": measured,
        "expected": {}, "images": [record["image"] for record in state["target_reference"]["frames"]+frames]})
    phase["endpoint_verified"] = {"event_id": verified["id"], "observation_sha256": v2_evidence_digest(
        {key: verified["payload"][key] for key in ("seq", "name", "verdict", "measurements", "expected")})}
    emit("material_metal.released", {"chip_id": run["params"]["target_chip_id"], "top_first_flake_ids": run["params"]["flake_ids"], "measurement": measured})
    emit("step.finished", old_finish["payload"])
    for row in suffix:
        appended = emit(row["kind"], row["payload"])
        if witnessed:
            appended["run_id"] = row.get("run_id")
    return recording


def witness_event_ref(row, projection=None, *, observation=False):
    payload = row["payload"]
    if observation:
        return {"event_id": row["id"], "observation_sha256": v2_evidence_digest({key: payload[key] for key in ("seq", "name", "verdict", "measurements", "expected")})}
    body = payload
    if projection == "witness-submission-selection-v1":
        body = {"flow": payload["flow"], "params": {key: payload["params"].get(key) for key in
            ("flake_ids", "flake_chips", "materials", "stamp_id", "stamp_film", "procedure_contract", "transfer_profile", "release_policy")}}
    elif projection == "witness-stamp-lineage-v1":
        props = payload["props"]
        reports = props.get("film_reports")
        body = {**{key: payload[key] for key in ("id", "kind", "location", "status")}, "props": {
            "film": props.get("film"), "support": props.get("support"), "support_history": props.get("support_history"),
            "film_reports": None if reports is None else [{key: value for key, value in item.items() if key not in ("what", "images", "notes")} for item in reports],
            "stack": props.get("stack", [])}}
    elif projection == "witness-pickup-endpoint-v1":
        body = {**{key: payload[key] for key in ("stamp_id", "flake_id", "procedure")},
                "observation": {key: payload["observation"].get(key) for key in ("verdict", "measurements", "expected")}}
    elif projection == "witness-manipulator-encoder-v1":
        body = {"device": payload["device"], "command": payload["command"], "timestamp": row["ts"],
                "manipulator": {key: payload["manipulator"][key] for key in ("x", "y", "z")}}
    return {"event_id": row["id"], "event_sha256": v2_evidence_digest(body), **({"projection": projection} if projection else {})}


def witnessed_release_recording(*, mica=False, tail=False, incidental_paths=False, depth_mm=.13):
    recording = target_relative_release_recording(complete=True, mica=mica, witnessed=True, tail=tail, witness_depth_mm=depth_mm)
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    events = recording["events"]
    phase = state["phases"]["release_material_metal_stack"]
    selected = run["params"]["flake_ids"][-1]
    pickup_name = "v1_pick_up_"+selected
    pickup = state["phases"][pickup_name]
    def started(name):
        return next(row for row in events if row["kind"] == "step.started" and row["payload"].get("name") == name)
    def finished(name):
        return next(row for row in events if row["kind"] == "step.finished" and row["payload"].get("name") == name)
    def verified(name):
        return next(row for row in events if row["kind"] == "step.verified" and row["payload"].get("name") == name)
    submission = next(row for row in events if row["kind"] == "job.submitted")
    capture = next(row for row in events if row["kind"] == "lab.frame" and row["payload"].get("microscope_preview", {}).get("image") == pickup["coverage"]["current"]["image"])
    command = next(row for row in reversed(events) if row["id"] < capture["id"] and row["kind"] == "device.command" and row["payload"].get("device") == "transfer_arm")
    camera = next(row for row in events if row["id"] > command["id"] and row["kind"] == "device.command")
    frames = [row for row in events if command["id"] < row["id"] < camera["id"] and row["kind"] == "lab.frame"
              and row["payload"].get("device") == "transfer_arm"]
    settled = next(row for row in reversed(frames) if "manipulator" in row["payload"])
    if incidental_paths:
        run["params"]["design_path"] = submission["payload"]["params"]["design_path"] = "/Users/witness-fixture/synthetic-design.gds"
        for row in events:
            if row["kind"] == "sample.updated" and row["payload"].get("id") == run["params"]["stamp_id"]:
                row["payload"]["props"]["film_reports"] = [{"instruction_id": "synthetic-report", "ts": 1., "by": "operator", "via": "human",
                    "previous_state": {"film": "PC"}, "what": "Report from /Users/witness-fixture/synthetic-report.txt",
                    "images": ["/Users/witness-fixture/synthetic-report.png"], "notes": "Stored in /Users/witness-fixture/synthetic-notes.txt"}]
            if row["kind"] == "stack.pickup_endpoint":
                row["payload"]["observation"].update(images=["/Users/witness-fixture/synthetic-display.png"], message="See /Users/witness-fixture/synthetic-display.txt")
        settled["payload"]["microscope_preview"] = {"image": "/Users/witness-fixture/synthetic-preview.png"}

    proof = witness_event_ref

    required = []
    for fid in run["params"]["flake_ids"]:
        endpoint = next(row for row in events if row["kind"] == "stack.pickup_endpoint" and row["payload"].get("flake_id") == fid)
        required.append({"flake_id": fid, "verified": proof(verified("v1_pick_up_"+fid), observation=True),
                         "pickup_endpoint": proof(endpoint, "witness-pickup-endpoint-v1")})
    grip_start, grip_finish = started("grip_stamp"), finished("grip_stamp")
    vacuum = next(row for row in events if grip_start["id"] < row["id"] < grip_finish["id"] and row["kind"] == "device.command"
                  and row["payload"].get("device") == "transfer_arm_vacuum")
    holder = next(row for row in events if grip_start["id"] < row["id"] < grip_finish["id"] and row["kind"] == "sample.updated")
    handoff = next(row for row in events if started("load_stamp")["id"] < row["id"] < finished("load_stamp")["id"] and row["kind"] == "sample.updated")
    baseline = next(row for row in events if row["id"] == pickup["reference_event"]["event_id"])
    witness = {"flake_id": selected, "phase": pickup_name, "seq": started(pickup_name)["payload"]["seq"],
        "load": {"submission": proof(submission, "witness-submission-selection-v1"), "film": run["params"]["stamp_film"],
            "handoff_verified": proof(verified("load_stamp"), observation=True), "handoff_event": proof(handoff, "witness-stamp-lineage-v1"),
            "grip_start": proof(grip_start), "grip_finish": proof(grip_finish), "grip_observation": proof(verified("grip_stamp")),
            "vacuum_command": proof(vacuum), "stamp_event": proof(holder, "witness-stamp-lineage-v1"), "release_start": proof(started(phase["name"]))},
        "required_pickup_verified": required, "pickup_start": proof(started(pickup_name)), "pickup_finish": proof(finished(pickup_name)),
        "verified": required[-1]["verified"], "source_process_reference_sha256": v2_evidence_digest(pickup["process_reference"]),
        "source_focus_binding_sha256": v2_evidence_digest(state["source_references"][selected]["focus_binding"]),
        "reference_capture": proof(baseline), "coverage_capture": proof(capture), "coverage_command": proof(command),
        "settled_encoder": proof(settled, "witness-manipulator-encoder-v1"), "encoder_end_event_id": frames[-1]["id"],
        "encoder_event_count": len(frames), "encoder_event_sha256": v2_evidence_digest([{"event_id": row["id"], "timestamp": row["ts"],
            "pose": row["payload"].get("manipulator")} for row in frames]), "coverage_sha256": v2_evidence_digest(pickup["coverage"]),
        "command_z_mm": command["payload"]["params"]["value"], "readback_z_mm": pickup["coverage"]["actual_holder_pose"]["z"],
        "source_reference_upper_mm": pickup["process_reference"]["reference_interval_mm"][1]}
    phase["release_reference"]["witness"] = witness
    return recording


@pytest.mark.parametrize("mica,tail", [(False, False), (True, False), (False, True)])
def test_v2_witness_release_uses_latest_original_covered_command_and_preserves_later_unload(mica, tail, monkeypatch):
    from pathlib import Path

    recording = witnessed_release_recording(mica=mica, tail=tail)
    before = deepcopy(recording)
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    plan = state["phases"]["release_material_metal_stack"]["release_reference"]
    witness = plan["witness"]
    assert witness["flake_id"] == run["params"]["flake_ids"][-1]
    assert [item["flake_id"] for item in witness["required_pickup_verified"]] == run["params"]["flake_ids"]
    assert plan["relative_intent_mm"] == witness["command_z_mm"]-witness["source_reference_upper_mm"]
    assert plan["relative_intent_mm"] != pytest.approx(plan["nominal_requested_z_mm"]-plan["nominal_reference_z_mm"])
    first_pickup = state["phases"]["v1_pick_up_"+run["params"]["flake_ids"][0]]
    assert first_pickup["coverage"]["actual_holder_pose"]["z"]-first_pickup["process_reference"]["reference_interval_mm"][1] < plan["relative_intent_mm"]
    assert plan["requested_z_mm"] == state["phases"]["release_material_metal_stack"]["process_reference"]["reference_interval_mm"][1]+plan["relative_intent_mm"]
    assert any(row["id"] > witness["load"]["release_start"]["event_id"] and row["kind"] == "device.command"
        and row["payload"].get("device") == "transfer_arm_vacuum" and row["payload"]["params"]["on"] is False for row in recording["events"])
    if mica:
        stamp = next(row for row in recording["events"] if row["id"] == witness["load"]["stamp_event"]["event_id"])
        assert stamp["payload"]["props"]["film"] == "PC"
        assert stamp["payload"]["props"]["support"]["film_type"] == "mica"
    if tail:
        pickup = state["phases"][witness["phase"]]
        assert witness["command_z_mm"] > pickup["final_partial_macro"]["admission"]["admitted_target_mm"]
        assert witness["command_z_mm"] != pickup["last_intent"]["to_mm"]
    # F6 consumes public record claims; original pixels remain a separate producer check.
    monkeypatch.setattr(Path, "open", lambda *_a, **_k: pytest.fail("pure auditor opened a file"))
    result = process_score(recording)
    assert result["consistent"] is True, result
    assert result["checks"]["transfer:release_witness"] is True
    assert "independent_world_truth" not in result
    assert any("declared SIM process memory" in text for text in result["limitations"])
    assert recording == before


@pytest.mark.parametrize("change", [
    "earlier", "wrong_phase", "wrong_seq", "required_omitted", "required_reordered", "required_verify", "latest_verify",
    "later_pickup_start", "wrong_run", "source_process", "source_focus", "source_upper", "command_value", "readback_value",
    "baseline", "coverage_event", "covered_digest", "command_ref", "stream_end", "stream_count", "stream_digest",
    "cutoff", "binding_downgrade", "phase_downgrade", "raw_downgrade", "missing_witness",
])
def test_v2_witness_release_has_no_fallback_for_missing_or_changed_original_authority(change):
    recording = witnessed_release_recording()
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    plan = state["phases"]["release_material_metal_stack"]["release_reference"]
    witness = plan["witness"]
    events = {row["id"]: row for row in recording["events"]}
    if change == "earlier":
        witness["flake_id"] = run["params"]["flake_ids"][0]
    elif change == "wrong_phase":
        witness["phase"] = "v1_pick_up_hbn"
    elif change == "wrong_seq":
        witness["seq"] -= 1
    elif change == "required_omitted":
        del witness["required_pickup_verified"][0]
    elif change == "required_reordered":
        witness["required_pickup_verified"].reverse()
    elif change == "required_verify":
        witness["required_pickup_verified"][0]["verified"]["observation_sha256"] = "b"*64
    elif change == "latest_verify":
        witness["verified"] = deepcopy(witness["required_pickup_verified"][0]["verified"])
    elif change == "later_pickup_start":
        witness["pickup_start"] = deepcopy(witness["load"]["release_start"])
    elif change == "wrong_run":
        events[witness["coverage_command"]["event_id"]]["run_id"] = "another-run"
    elif change in ("source_process", "source_focus", "covered_digest"):
        witness[{"source_process": "source_process_reference_sha256", "source_focus": "source_focus_binding_sha256", "covered_digest": "coverage_sha256"}[change]] = "b"*64
    elif change in ("source_upper", "command_value", "readback_value"):
        witness[{"source_upper": "source_reference_upper_mm", "command_value": "command_z_mm", "readback_value": "readback_z_mm"}[change]] += .01
    elif change == "baseline":
        witness["reference_capture"] = deepcopy(witness["coverage_capture"])
    elif change == "coverage_event":
        witness["coverage_capture"] = deepcopy(witness["reference_capture"])
    elif change == "command_ref":
        witness["coverage_command"]["event_id"] += 1
    elif change in ("stream_end", "stream_count"):
        witness["encoder_end_event_id" if change == "stream_end" else "encoder_event_count"] += 1
    elif change == "stream_digest":
        witness["encoder_event_sha256"] = "b"*64
    elif change == "cutoff":
        witness["load"]["release_start"] = deepcopy(witness["pickup_start"])
    elif change == "binding_downgrade":
        recording["meta"]["acceptance_evidence"]["v1_process_binding"]["release_policy"] = "native20-target-reference-parked-release-v1"
    elif change == "phase_downgrade":
        state["phases"]["release_material_metal_stack"]["release_policy"] = "native20-target-reference-parked-release-v1"
    elif change == "raw_downgrade":
        run["params"]["release_policy"] = "native20-target-reference-parked-release-v1"
    else:
        del plan["witness"]
    result = process_score(recording)
    assert result["consistent"] is False, change
    key = "release_policy_binding" if change in ("binding_downgrade", "phase_downgrade") else "release_witness"
    assert result["checks"]["transfer:"+key] is False, result


@pytest.mark.parametrize("change", ["vacuum_on", "vacuum_off", "vacuum_malformed", "other_run_vacuum", "moved_out", "removed",
    "removed_stamp_id", "refilmed", "other_stamp", "stack_reset", "stack_null", "stack_reordered", "film", "support",
    "report_instruction", "report_time", "report_by", "report_via", "report_previous", "report_malformed", "support_history_malformed",
    "handoff_wrong_run", "grip_verifiable", "grip_not_done", "selection_change", "selection_replace", "selection_nested"])
def test_v2_witness_release_checks_global_continuity_and_original_load_semantics(change):
    recording = witnessed_release_recording(incidental_paths=True)
    run = recording["runs"]["transfer"]
    witness = run["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]["release_reference"]["witness"]
    load = witness["load"]
    events = {row["id"]: row for row in recording["events"]}
    # Replace an unreferenced observation before the immutable cutoff with a global ledger/command row.
    slot = events[witness["pickup_finish"]["event_id"]+2]
    stamp = deepcopy(events[load["stamp_event"]["event_id"]]["payload"])
    stamp["props"]["stack"] = list(run["params"]["flake_ids"])
    slot.update(kind="sample.updated", run_id=None, payload=stamp)
    if change.startswith("vacuum_") or change == "other_run_vacuum":
        slot.update(kind="device.command", run_id="another-run" if change == "other_run_vacuum" else None,
            payload={"device": "transfer_arm_vacuum", "command": "set", "params": {"on": {
                "vacuum_on": True, "vacuum_off": False, "vacuum_malformed": "unknown", "other_run_vacuum": True}[change]}})
    elif change == "moved_out":
        stamp["location"] = "stamp_handoff"
    elif change in ("removed", "removed_stamp_id", "refilmed"):
        slot.update(kind="stamp.film_replaced" if change == "refilmed" else "sample.removed",
                    payload={"stamp_id" if change == "removed_stamp_id" else "id": run["params"]["stamp_id"]})
    elif change == "other_stamp":
        stamp["id"] = "other-stamp"
    elif change.startswith("stack_"):
        stamp["props"]["stack"] = {"stack_reset": [], "stack_null": None, "stack_reordered": list(reversed(run["params"]["flake_ids"]))}[change]
    elif change == "film":
        stamp["props"]["film"] = "mica"
    elif change == "support":
        stamp["props"]["support"] = {"film_type": "PC", "instruction_id": "replaced", "source": "operator_report"}
    elif change.startswith("report_") and change != "report_malformed":
        key = {"report_instruction": "instruction_id", "report_time": "ts", "report_by": "by", "report_via": "via", "report_previous": "previous_state"}[change]
        stamp["props"]["film_reports"][0][key] = "changed"
    elif change == "report_malformed":
        stamp["props"]["film_reports"] = [None]
    elif change == "support_history_malformed":
        stamp["props"]["support_history"] = [None]
    elif change == "handoff_wrong_run":
        events[load["handoff_event"]["event_id"]]["run_id"] = run["id"]
    elif change in ("grip_verifiable", "grip_not_done"):
        key = "grip_observation" if change == "grip_verifiable" else "grip_finish"
        row = events[load[key]["event_id"]]
        row["payload"]["verdict" if change == "grip_verifiable" else "status"] = "ok" if change == "grip_verifiable" else "skipped"
        load[key] = witness_event_ref(row)  # Even a rebound claim must retain original lifecycle semantics.
    else:
        value = deepcopy(run["params"])
        value["stamp_id"] = "other-stamp"
        path = [] if change == "selection_replace" else ["flake_ids", "0"] if change == "selection_nested" else ["stamp_id"]
        slot.update(kind="job.params_changed", run_id=run["id"], payload={"path": path,
                    "value": value if not path else "other-flake" if len(path) == 2 else "other-stamp"})
    result = process_score(recording)
    assert result["checks"]["transfer:release_witness"] is False, (change, result)


@pytest.mark.parametrize("change", ["axis", "speed", "upward", "too_large", "duration", "from_xy", "camera_owner", "last_sample",
    "malformed_final", "nonfinite", "xy_drift", "below_minimum", "late", "reordered", "projection", "capture_context", "baseline_context",
    "baseline_camera", "baseline_camera_late"])
def test_v2_witness_release_validates_rebound_commands_and_complete_owned_encoder_stream(change):
    recording = witnessed_release_recording()
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    witness = state["phases"]["release_material_metal_stack"]["release_reference"]["witness"]
    events = {row["id"]: row for row in recording["events"]}
    command = events[witness["coverage_command"]["event_id"]]
    settled = events[witness["settled_encoder"]["event_id"]]
    midpoint = events[settled["id"]-1]
    camera = events[settled["id"]+1]
    p = command["payload"]["params"]
    if change == "axis":
        p["axis"] = "x"
    elif change == "speed":
        p["speed"] = .1
    elif change == "upward":
        p["value"] = p["from_pose"]["z"]+.01
    elif change == "too_large":
        p["from_pose"]["z"] += .03
    elif change == "duration":
        p["duration_bound_s"] = 2.
    elif change == "from_xy":
        p["from_pose"]["x"] += .02
    elif change == "camera_owner":
        camera["payload"]["command"] = "set_objective"
    elif change == "last_sample":
        witness["settled_encoder"] = witness_event_ref(midpoint, "witness-manipulator-encoder-v1")
    elif change == "malformed_final":
        settled["payload"]["manipulator"] = None
    elif change == "nonfinite":
        midpoint["payload"]["manipulator"]["z"] = float("nan")
    elif change == "xy_drift":
        midpoint["payload"]["manipulator"]["x"] += .02
    elif change == "below_minimum":
        midpoint["payload"]["manipulator"]["z"] = state["phases"][witness["phase"]]["process_reference"]["minimum_z_mm"]-.01
    elif change == "late":
        settled["ts"] = camera["ts"]+3.
    elif change == "reordered":
        midpoint["ts"] = settled["ts"]+.01
    elif change == "projection":
        witness["settled_encoder"]["projection"] = "witness-stamp-lineage-v1"
    elif change in ("baseline_camera", "baseline_camera_late"):
        baseline = events[witness["reference_capture"]["event_id"]]
        camera_before = events[baseline["id"]-1]
        if change == "baseline_camera":
            camera_before["payload"]["command"] = "set_focus"
        else:
            camera_before["ts"] = baseline["ts"]+1.
    else:
        key = "coverage_capture" if change == "capture_context" else "reference_capture"
        events[witness[key]["event_id"]]["payload"]["microscope_preview"]["focus_z"] += .1
        witness[key] = witness_event_ref(events[witness[key]["event_id"]])
    witness["coverage_command"] = witness_event_ref(command)
    if change not in ("last_sample", "malformed_final", "projection"):
        witness["settled_encoder"] = witness_event_ref(settled, "witness-manipulator-encoder-v1")
    frames = [midpoint, settled]
    if change != "nonfinite":
        witness["encoder_event_sha256"] = v2_evidence_digest([{"event_id": row["id"], "timestamp": row["ts"], "pose": row["payload"].get("manipulator")} for row in frames])
    result = process_score(recording)
    assert result["checks"]["transfer:release_witness"] is False, (change, result)


@pytest.mark.parametrize("change", ["found", "covered", "threshold", "connectivity", "minimum_area", "blur", "margin", "edge", "area", "mask", "target"])
def test_v2_witness_release_checks_scalar_coverage_consistency_after_rebinding_original_copies(change):
    recording = witnessed_release_recording()
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    witness = state["phases"]["release_material_metal_stack"]["release_reference"]["witness"]
    pickup = state["phases"][witness["phase"]]
    coverage = pickup["coverage"]
    key, value = {"found": ("found", False), "covered": ("covered", False), "threshold": ("threshold", 41), "connectivity": ("connectivity", 8),
        "minimum_area": ("minimum_area_mm2", 0.), "blur": ("blur_support_mm", 0.), "margin": ("required_margin_mm", 0.),
        "edge": ("edge_distance_mm", 0.), "area": ("area_mm2", 0.), "mask": ("region_mask_sha256", "missing"), "target": ("target_px", [1., 1.])}[change]
    coverage[key] = value
    pickup["endpoint"]["coverage"] = deepcopy(coverage)
    events = {row["id"]: row for row in recording["events"]}
    verified = events[witness["verified"]["event_id"]]
    verified["payload"]["measurements"] = deepcopy(pickup["endpoint"])
    proof = witness_event_ref(verified, observation=True)
    pickup["endpoint_verified"] = deepcopy(proof)
    witness["verified"] = deepcopy(proof)
    required = witness["required_pickup_verified"][-1]
    required["verified"] = deepcopy(proof)
    endpoint = events[required["pickup_endpoint"]["event_id"]]
    endpoint["payload"]["observation"]["measurements"] = deepcopy(pickup["endpoint"])
    required["pickup_endpoint"] = witness_event_ref(endpoint, "witness-pickup-endpoint-v1")
    witness["coverage_sha256"] = v2_evidence_digest(coverage)
    result = process_score(recording)
    assert result["checks"]["transfer:release_witness"] is False, (change, result)


def test_v2_witness_release_accepts_absent_empty_stack_and_final_unchanged_delta_only():
    recording = witnessed_release_recording()
    witness = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]["release_reference"]["witness"]
    events = {row["id"]: row for row in recording["events"]}
    for key in ("handoff_event", "stamp_event"):
        row = events[witness["load"][key]["event_id"]]
        del row["payload"]["props"]["stack"]
        assert witness["load"][key] == witness_event_ref(row, "witness-stamp-lineage-v1")
    final = events[witness["settled_encoder"]["event_id"]]
    previous = events[final["id"]-1]
    # The final event can be an unchanged delta. The last explicit sample stays authoritative.
    previous["payload"]["manipulator"] = final["payload"].pop("manipulator")
    witness["settled_encoder"] = witness_event_ref(previous, "witness-manipulator-encoder-v1")
    witness["encoder_event_sha256"] = v2_evidence_digest([{"event_id": row["id"], "timestamp": row["ts"], "pose": row["payload"].get("manipulator")} for row in (previous, final)])
    result = process_score(recording)
    assert result["consistent"] is True, result
    final["payload"]["manipulator"] = None  # A present malformed field is never an unchanged delta.
    assert process_score(recording)["checks"]["transfer:release_witness"] is False


@pytest.mark.parametrize("key", ["submission", "handoff_event", "stamp_event", "settled_encoder", "coverage_command"])
def test_v2_witness_release_projection_tags_are_explicit_and_limited_to_new_refs(key):
    recording = witnessed_release_recording()
    witness = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]["release_reference"]["witness"]
    ref = witness["load"][key] if key in witness["load"] else witness[key]
    if "projection" in ref:
        del ref["projection"]
    else:
        ref["projection"] = "witness-manipulator-encoder-v1"
    assert process_score(recording)["checks"]["transfer:release_witness"] is False


@pytest.mark.parametrize("malformed", [None, [], True, 3.25], ids=["none", "list", "bool", "number"])
@pytest.mark.parametrize("location,held", [("submission", False), ("handoff_event", True), ("stamp_event", False), ("later_ledger", True)])
def test_v2_witness_release_rejects_malformed_projection_containers_without_crashing(location, held, malformed):
    recording = witnessed_release_recording()
    run = recording["runs"]["transfer"]
    witness = run["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]["release_reference"]["witness"]
    events = {row["id"]: row for row in recording["events"]}
    if location == "later_ledger":
        row = events[witness["pickup_finish"]["event_id"]+2]
        row.update(kind="sample.updated", run_id=None, payload=deepcopy(events[witness["load"]["stamp_event"]["event_id"]]["payload"]))
        row["payload"]["props"] = malformed
    else:
        ref = witness["load"][location]
        row = events[ref["event_id"]]
        container = "params" if location == "submission" else "props"
        row["payload"][container] = malformed
        # Even a receipt computed by coercing unknown authority to {} must fail closed.
        coerced = deepcopy(row)
        coerced["payload"][container] = {}
        ref.update(witness_event_ref(coerced, ref["projection"]))
    if held:
        run["status"] = "waiting_human"
    before = deepcopy(recording)
    result = process_score(recording, story="honest_stop" if held else "complete")
    assert result["state"] == "inconsistent", result
    assert result["consistent"] is False
    assert result["checks"]["transfer:release_witness"] is False
    assert recording == before


def test_v2_witness_release_never_rebases_original_cutoff_or_original_pickup_verification():
    recording = witnessed_release_recording()
    witness = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]["release_reference"]["witness"]
    events = {row["id"]: row for row in recording["events"]}
    later = deepcopy(events[witness["verified"]["event_id"]])
    later.update(id=recording["events"][-1]["id"]+1, ts=recording["events"][-1]["ts"]+1)
    later["payload"]["verdict"] = "not_verifiable"
    recording["events"].append(later)
    assert process_score(recording)["consistent"] is True
    # A subsequent release start cannot move continuity past the original release boundary.
    new_start = deepcopy(events[witness["load"]["release_start"]["event_id"]])
    new_start.update(id=later["id"]+1, ts=later["ts"]+1)
    recording["events"].append(new_start)
    assert process_score(recording)["checks"]["transfer:release_witness"] is True
    witness["load"]["release_start"] = witness_event_ref(new_start)
    assert process_score(recording)["checks"]["transfer:release_witness"] is False


@pytest.mark.parametrize("depth,accepted", [(.1949, True), (.1951, False), (.2, False), (0., False), (-.01, False)])
def test_v2_witness_release_rejects_unsafe_relative_intent_without_clamp_or_nominal_fallback(depth, accepted):
    recording = witnessed_release_recording(depth_mm=depth)
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    plan = phase["release_reference"]
    witness = plan["witness"]
    assert plan["relative_intent_mm"] == witness["command_z_mm"]-witness["source_reference_upper_mm"]
    assert plan["relative_intent_mm"] == pytest.approx(-depth)
    result = process_score(recording)
    assert result["checks"]["transfer:release_witness"] is accepted, result
    assert result["checks"]["transfer:release_reference"] is accepted, result
    assert result["consistent"] is accepted, result


@pytest.mark.parametrize("change", ["stamp", "procedure", "verdict", "skip_finish", "missing_original"])
def test_v2_witness_release_requires_every_original_success_even_with_rebound_refs(change):
    recording = witnessed_release_recording()
    witness = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]["release_reference"]["witness"]
    required = witness["required_pickup_verified"][0]
    events = {row["id"]: row for row in recording["events"]}
    row = events[required["pickup_endpoint"]["event_id"]]
    if change in ("stamp", "procedure"):
        row["payload"]["stamp_id" if change == "stamp" else "procedure"] = "other"
        required["pickup_endpoint"] = witness_event_ref(row, "witness-pickup-endpoint-v1")
    elif change == "verdict":
        row = events[required["verified"]["event_id"]]
        row["payload"]["verdict"] = "not_verifiable"
        required["verified"] = witness_event_ref(row, observation=True)
    elif change == "skip_finish":
        finished = next(row for row in recording["events"] if row["kind"] == "step.finished" and row["payload"].get("name") == "v1_pick_up_"+required["flake_id"])
        finished["payload"]["status"] = "skipped"
    else:
        recording["events"].remove(events[required["verified"]["event_id"]])
    assert process_score(recording)["checks"]["transfer:release_witness"] is False


@pytest.mark.parametrize("mica", [False, True])
def test_v2_witness_release_ordinary_export_scrubs_display_paths_and_retains_authority(tmp_path, monkeypatch, mica):
    import json
    from types import SimpleNamespace

    from qumus2.runtime import store as store_module
    from qumus2.ui import recording as recording_module

    recording = witnessed_release_recording(mica=mica, incidental_paths=True)
    data_root = tmp_path/"synthetic-record"
    data_root.mkdir()
    # These are synthetic locator fixtures, not original raster evidence. F6 never opens pixels.
    def fixture_media(value):
        if value in ("motion-preview", "park-preview"):
            value += ".png"
        if value == "/Users/witness-fixture/synthetic-preview.png":
            value = str(data_root/"synthetic-preview.png")
        if value.endswith(".png") and (not value.startswith("/") or value.startswith(str(data_root))):
            path = data_root/value
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic display locator fixture")
        return value
    recording = recording_module._walk(recording, fixture_media)
    before = deepcopy(recording)
    raw_audit = process_score(recording)
    assert raw_audit["consistent"] is True, raw_audit
    monkeypatch.setattr(recording_module, "SECRET_ENV", ())
    monkeypatch.setattr(recording_module, "resolve_run_path", lambda path: path)
    monkeypatch.setattr(store_module, "RunStore", lambda _path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(recording_module, "build_recording", lambda *_a, **_k: deepcopy(recording))
    folder = recording_module.export_recording(data_root/"synthetic.db", tmp_path/"portable-export")
    exported = json.loads((folder/"recording.json").read_text())
    assert process_score(exported) == raw_audit
    assert recording == before
    assert exported["runs"]["transfer"]["params"]["design_path"] == "~/synthetic-design.gds"
    witness = exported["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]["release_reference"]["witness"]
    events = {row["id"]: row for row in exported["events"]}
    settled = events[witness["settled_encoder"]["event_id"]]
    assert settled["payload"]["microscope_preview"]["image"].startswith("media/")
    stamp = events[witness["load"]["stamp_event"]["event_id"]]
    report = stamp["payload"]["props"]["film_reports"][0]
    assert report["images"] == ["~/synthetic-report.png"]
    assert report["instruction_id"] == "synthetic-report"
    report["instruction_id"] = "changed-authority"
    assert process_score(exported)["checks"]["transfer:release_witness"] is False


def test_v2_target_release_plan_reuses_verified_first_and_parked_target_antecedents():
    recording = target_relative_release_recording()
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    before = deepcopy(recording)
    result = process_score(recording)
    for name in ("release_policy_binding", "release_process_reference", "release_reference"):
        assert result["checks"]["transfer:"+name] is True, result
    assert phase["process_reference"]["reference_interval_mm"] == pytest.approx([6.0852, 6.1548])
    assert phase["release_reference"]["requested_z_mm"] == pytest.approx(6.0548)
    assert phase["process_reference"]["minimum_z_mm"] == pytest.approx(5.9548)
    assert result["checks"]["transfer:target_after"] is False  # The old held-target endpoint is insufficient.
    assert recording == before


@pytest.mark.parametrize("mica", [False, True])
def test_v2_target_release_counts_native_camera_and_proves_long_clear_off_park(mica):
    recording = target_relative_release_recording(complete=True, mica=mica)
    before = deepcopy(recording)
    result = process_score(recording)
    assert result["consistent"] is True, result
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    assert sum(item["ended_at"]-item["started_at"] for item in phase["park_transport"]) >= 95
    assert phase["last_frame"]["meta"]["objective"] == 20
    assert phase["release_budget"]["endpoint_observations"] == 24
    assert phase["observations"] > 24
    assert any(item["ended_at"]-item["started_at"] > item["duration_bound_s"] for item in phase["park_transport"])
    ordinary = [row for row in recording["events"] if row["kind"] == "lab.frame"
                and row["payload"].get("microscope_preview", {}).get("phase") == phase["name"]]
    assert phase["observations"] == len(ordinary)+24
    assert phase["observed_at"]-phase["started_at"] >= 95
    assert recording == before


@pytest.mark.parametrize("change", [
    "duration_under", "duration_enlarged", "from_pose", "actual_pose", "end_cursor", "encoder_count", "roundoff",
    "ended_at", "command_digest", "terminal_missing", "terminal_conflict", "post_off_z", "off_missing", "off_late",
    "withdrawal", "entry_setpoint", "release_setpoint", "weak_peak", "total_count", "endpoint_count", "native_window",
    "extra_native_capture", "wrong_seed", "wide_focus", "sample_command", "sample_late", "park_count", "thermal_count",
    "reserve_seconds", "reserve_travel", "reserve_observations", "thermal_allowance", "inflated_limits", "active_gap",
])
def test_v2_target_release_actual_history_cannot_be_replaced_by_mutable_claims(change):
    recording = target_relative_release_recording(complete=True)
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    events = recording["events"]
    item, budget = phase["park_transport"][0], phase["release_budget"]
    command = next(row for row in events if row["id"] == item["command_event"]["event_id"])
    samples = [row for row in events if command["id"] < row["id"] <= item["encoder_end_event_id"] and row["payload"].get("manipulator")]
    focuses = [row for row in events if budget["endpoint_start_event_id"] < row["id"] < budget["endpoint_end_event_id"]
               and row["kind"] == "device.command" and row["payload"].get("command") == "set_focus"]
    af = next(row for row in events if budget["endpoint_start_event_id"] < row["id"] < budget["endpoint_end_event_id"] and row["kind"] == "microscope.focus")
    heat = [row for row in events if row["kind"] == "device.command" and row["payload"].get("device") == "stamp_heater"]
    if change == "duration_under":
        item["duration_bound_s"] -= 1
    elif change == "duration_enlarged":
        item["duration_bound_s"] += 10
    elif change == "from_pose":
        item["from_pose"]["x"] += .001
    elif change == "actual_pose":
        item["actual_pose"]["x"] += .001
    elif change == "end_cursor":
        item["encoder_end_event_id"] = samples[-2]["id"]
    elif change == "encoder_count":
        item["encoder_event_count"] += 1
    elif change == "roundoff":
        item["clock_roundoff_s"] *= 2
    elif change == "ended_at":
        item["ended_at"] += 1
    elif change == "command_digest":
        item["command_event"]["event_sha256"] = "b"*64
    elif change == "terminal_missing":
        del samples[-1]["payload"]["manipulator"]
    elif change == "terminal_conflict":
        samples[-1]["payload"]["geometry"] = {"source": "synthetic", "microscope": {"source": "synthetic", "stamp_axes_frame": "transfer_arm",
            "stamp_axes_xyz_mm": {**samples[-1]["payload"]["manipulator"], "z": 6.4}}}
    elif change == "post_off_z":
        command["payload"]["params"].update(axis="z", value=phase["release_reference"]["requested_z_mm"])
    elif change == "off_missing":
        heat[-1]["payload"]["command"] = "get_target"
    elif change == "off_late":
        heat[-1]["ts"] = item["ended_at"]+1
    elif change == "withdrawal":
        phase["withdrawal"]["actual_readback"]["z"] -= .01
    elif change == "entry_setpoint":
        heat[0]["payload"]["params"]["celsius"] = 154.
    elif change == "release_setpoint":
        heat[1]["payload"]["params"]["celsius"] = 169.
    elif change == "weak_peak":
        phase["peak_c"] = 170.
    elif change == "total_count":
        phase["observations"] -= 1
    elif change == "endpoint_count":
        budget["endpoint_observations"] = 23
    elif change == "native_window":
        budget["endpoint_start_event_id"] = focuses[1]["id"]
    elif change == "extra_native_capture":
        focuses[-1]["payload"]["command"] = "capture"
    elif change == "wrong_seed":
        focuses[0]["payload"]["params"]["z"] += .3
    elif change == "wide_focus":
        focuses[1]["payload"]["params"]["z"] -= .1
    elif change == "sample_command":
        af["payload"]["sampled_passes"][0]["samples"][0]["requested_focus_z_mm"] += .001
    elif change == "sample_late":
        af["payload"]["sampled_passes"][0]["samples"][0]["captured_at"] += 3
    elif change == "park_count":
        budget["park_observations"] = 0
    elif change == "thermal_count":
        budget["thermal_observations"] = 0
    elif change == "reserve_seconds":
        budget["reserved_seconds"] = 1.
    elif change == "reserve_travel":
        budget["reserved_z_travel_mm"] = .01
    elif change == "reserve_observations":
        budget["reserved_observations"] = 1
    elif change == "thermal_allowance":
        budget["thermal_allowance_s"] += 1
    elif change == "inflated_limits":
        from scripts.record_three_demos import digest

        binding = recording["meta"]["acceptance_evidence"]["v1_process_binding"]
        binding["profile"]["max_total_s"] += 1
        binding["profile_sha256"] = digest(binding["profile"])
    elif change == "active_gap":
        view = next(row for row in events if row["kind"] == "lab.frame" and row["payload"].get("microscope_preview", {}).get("phase") == phase["name"]
                    and row["id"] > phase["reference_event"]["event_id"])
        view["payload"]["microscope_preview"]["timestamp"] += 3
        view["ts"] += 3
    result = process_score(recording)
    assert result["consistent"] is False, change
    assert result["state"] == "inconsistent", result


@pytest.mark.parametrize("change", ["stream_digest", "stream_pose", "stream_time", "omit_null_end", "pending_empty",
                                    "missing_declaration", "unknown_duration", "nondefault_speed", "rail_order", "stage"])
def test_v2_target_release_uses_protected_command_and_entire_owned_stream(change):
    import math

    recording = target_relative_release_recording(complete=True)
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    item = phase["park_transport"][0]
    command = next(row for row in recording["events"] if row["id"] == item["command_event"]["event_id"])
    frames = [row for row in recording["events"] if command["id"] < row["id"] <= item["encoder_end_event_id"]]
    if change == "stream_digest":
        item["encoder_event_sha256"] = "b"*64
    elif change in ("stream_pose", "stream_time"):
        if change == "stream_pose":
            frames[2]["payload"]["manipulator"]["z"] = 6.4
        else:
            frames[2]["ts"] = frames[0]["ts"]-.01
        item["encoder_event_sha256"] = v2_evidence_digest([{"event_id": row["id"], "timestamp": row["ts"],
            "pose": row["payload"].get("manipulator")} for row in frames])
    elif change == "omit_null_end":
        assert "manipulator" not in frames[-1]["payload"]
        item.update(encoder_end_event_id=frames[-2]["id"], encoder_event_count=len(frames)-1, ended_at=frames[-2]["ts"],
            encoder_event_sha256=v2_evidence_digest([{"event_id": row["id"], "timestamp": row["ts"],
                "pose": row["payload"].get("manipulator")} for row in frames[:-1]]),
            clock_roundoff_s=len(frames)*max(math.ulp(command["ts"]), math.ulp(frames[-2]["ts"])))
    elif change == "pending_empty":
        phase["park_pending_intent"] = {}
    elif change == "missing_declaration":
        del command["payload"]["params"]["from_pose"]
    elif change == "unknown_duration":
        command["payload"]["params"]["duration_bound_s"] = None
    elif change == "nondefault_speed":
        command["payload"]["params"]["speed"] = 2.
        item["command_event"]["event_sha256"] = v2_evidence_digest(command["payload"])
    elif change == "rail_order":
        phase["park_transport"].reverse()
    elif change == "stage":
        phase["release_budget"]["stage"] = "off"
    result = process_score(recording)
    assert result["checks"]["transfer:release_history"] is False, result
    assert result["consistent"] is False


def test_v2_target_release_empty_delta_frame_does_not_replace_public_canonical_pose():
    recording = target_relative_release_recording(complete=True)
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    end = phase["park_transport"][0]
    terminal = next(row for row in recording["events"] if row["id"] == end["encoder_end_event_id"])
    assert "manipulator" not in terminal["payload"]
    terminal["payload"]["geometry"] = {"source": "synthetic", "microscope": {"source": "synthetic", "stamp_axes_frame": "transfer_arm",
        "stamp_axes_xyz_mm": deepcopy(end["actual_pose"])}}
    result = process_score(recording)
    assert result["consistent"] is True, result


@pytest.mark.parametrize("malformed", [{}, None, [], False, 0, ""], ids=["object", "null", "array", "false", "zero", "empty_string"])
def test_v2_target_release_rejects_present_empty_encoder_even_with_recomputed_digest(malformed):
    recording = target_relative_release_recording(complete=True)
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    item = phase["park_transport"][0]
    frames = [row for row in recording["events"] if item["command_event"]["event_id"] < row["id"] <= item["encoder_end_event_id"]]
    frames[-1]["payload"]["manipulator"] = malformed
    item["encoder_event_sha256"] = v2_evidence_digest([{"event_id": row["id"], "timestamp": row["ts"],
        "pose": row["payload"].get("manipulator") or None} for row in frames])
    result = process_score(recording)
    assert result["checks"]["transfer:release_history"] is False
    assert result["consistent"] is False


def test_v2_target_release_counter_reset_cannot_make_actual_work_unexecuted():
    recording = target_relative_release_recording(complete=True)
    run = recording["runs"]["transfer"]
    phase = run["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    run["status"] = "parked"
    phase.update(complete=False, observations=1)
    phase["release_budget"].update(stage="entry_heat", endpoint_observations=0, park_observations=0, thermal_observations=0, park_complete=False)
    for key in ("endpoint_status", "endpoint_start_event_id", "endpoint_end_event_id"):
        phase["release_budget"].pop(key)
    phase.pop("park_transport")
    phase.pop("park")
    result = process_score(recording)
    assert result["checks"]["transfer:release_history"] is False
    assert result["consistent"] is False


def test_v2_target_release_cannot_rebase_phase_to_hide_earlier_actual_work():
    recording = target_relative_release_recording(complete=True)
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    views = [row for row in recording["events"] if row["kind"] == "lab.frame"
             and row["payload"].get("microscope_preview", {}).get("phase") == phase["name"]]
    later = views[1]
    preview = later["payload"]["microscope_preview"]
    phase["reference"] = {"image": preview["image"], "sha256": preview["image_sha256"], "captured_at": preview["timestamp"],
                          "meta": deepcopy(phase["reference"]["meta"])}
    phase["reference_event"] = {"event_id": later["id"], "event_sha256": v2_evidence_digest(later["payload"])}
    phase["started_at"] = preview["timestamp"]
    phase["release_budget"]["start_event_id"] = later["id"]
    phase["observations"] -= 1
    result = process_score(recording)
    assert result["consistent"] is False
    assert result["checks"]["transfer:release_material_metal_stack:reference_event"] is False


@pytest.mark.parametrize("open_step", [False, True])
def test_v2_unexecuted_target_release_hold_does_not_invent_future_motion_or_endpoint(open_step):
    import math

    recording = target_relative_release_recording(complete=True)
    run = recording["runs"]["transfer"]
    old = run["meta"]["normal_transfer"]["phases"]["release_material_metal_stack"]
    capture = next(row for row in recording["events"] if row["id"] == old["reference_event"]["event_id"])
    recording["events"] = [row for row in recording["events"] if row["id"] <= capture["id"]]
    phase = {key: deepcopy(old[key]) for key in ("name", "profile", "started_at", "reference", "reference_event", "release_policy", "process_reference", "release_reference")}
    phase.update(observations=1, travel_mm=0., last_frame=deepcopy(phase["reference"]), observed_at=phase["reference"]["captured_at"],
                 last_pose={"x": 40., "y": 90., "z": 6.5}, holder_xy=[40., 90.], difference_active=False, complete=False)
    target = phase["release_reference"]["requested_z_mm"]
    def segments(a, b):
        return 0 if abs(a-b) <= 1e-8 else math.ceil(max(0., abs(a-b)-.005)/.015)+1
    n = segments(6.5, target)+segments(target-.005, 6.5)
    reserve_seconds = (n+2)*2+24*.05+95.
    phase["release_budget"] = {"stage": "entry_heat", "start_event_id": capture["id"], "endpoint_limit": 24,
        "endpoint_observations": 0, "park_observations": 0, "thermal_observations": 0, "park_complete": False,
        "reserved_observations": n+26, "reserved_seconds": reserve_seconds, "reserved_z_travel_mm": (n+2)*.025,
        "thermal_allowance_s": min(900-reserve_seconds, (600-1-n-26)*1.95), "checked_at": capture["ts"]}
    run["meta"]["normal_transfer"]["phases"][phase["name"]] = phase
    seq = next(row["payload"]["seq"] for row in recording["events"] if row["kind"] == "step.started" and row["payload"]["name"] == phase["name"])
    if not open_step:
        recording["events"].append({"id": capture["id"]+1, "ts": capture["ts"]+.01, "run_id": "transfer", "kind": "step.finished",
            "payload": {"name": phase["name"], "seq": seq, "status": "failed"}})
    last = recording["events"][-1]
    recording["events"].append({"id": last["id"]+1, "ts": last["ts"]+.01, "run_id": "transfer", "kind": "job.status", "payload": {"status": "parked"}})
    run["status"] = "parked"
    result = process_score(recording)
    assert result["state"] == "incomplete" and not result["problems"], result
    assert result["checks"]["transfer:release_reference"] is True
    assert result["checks"]["transfer:release_history"] is None


@pytest.mark.parametrize("change,failed", [
    ("raw_policy", "release_policy_binding"), ("loaded_policy", "release_policy_binding"),
    ("phase_policy", "release_policy_binding"), ("candidate", "release_process_reference"),
    ("after_focus_input", "release_process_reference"), ("double_mapping_error", "release_process_reference"),
    ("minimum", "release_process_reference"), ("midpoint_request", "release_reference"),
    ("nominal_difference", "release_reference"), ("verified_baseline", "release_reference"),
    ("baseline_digest", "release_reference"), ("baseline_late", "release_reference"), ("wrong_roi", "release_reference"),
])
def test_v2_target_release_plan_rejects_unbound_or_rewritten_antecedents(change, failed):
    recording = target_relative_release_recording()
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    phase = state["phases"]["release_material_metal_stack"]
    process, plan = phase["process_reference"], phase["release_reference"]
    if change == "raw_policy":
        del run["params"]["release_policy"]
    elif change == "loaded_policy":
        recording["meta"]["acceptance_evidence"]["v1_process_binding"]["release_policy"] = "previous-policy"
    elif change == "phase_policy":
        phase["release_policy"] = "previous-policy"
    elif change == "candidate":
        process["first_candidate_z_mm"] -= .4
    elif change == "after_focus_input":
        process["current_focus_binding_sha256"] = v2_evidence_digest(phase["endpoint"]["target_after"]["focus_reference"]["focus_binding"])
    elif change == "double_mapping_error":
        process["delta_interval_mm"][0] -= .006
        process["delta_interval_mm"][1] += .006
    elif change == "minimum":
        process["minimum_z_mm"] -= .01
    elif change == "midpoint_request":
        plan["requested_z_mm"] = sum(process["reference_interval_mm"])/2+plan["relative_intent_mm"]
    elif change == "nominal_difference":
        plan["relative_intent_mm"] = -.2
    elif change == "verified_baseline":
        plan["target_verified"]["event_id"] += 1
    elif change == "baseline_digest":
        plan["target_before_sha256"] = "b"*64
    elif change == "baseline_late":
        next(event for event in recording["events"] if event["id"] == state["target_reference_verified"]["event_id"])["ts"] = phase["started_at"]+1
    elif change == "wrong_roi":
        plan["target_xy_mm"] = [100., 100.]
    result = process_score(recording)
    assert result["checks"]["transfer:"+failed] is False, result
    assert result["consistent"] is False


@pytest.mark.parametrize("also_shift_observation", [False, True])
def test_v2_joint_candidate_metadata_shift_cannot_override_recorded_acquisition(also_shift_observation):
    recording = native_process_recording(v2=True)
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    state["placements"]["stamp"]["candidate_z_mm"] -= .4
    state["phases"]["v1_first_contact_stamp"]["first_contact_region"]["actual_holder_pose"]["z"] -= .4
    for fid in ("hbn", "gr"):
        process = state["phases"][f"v1_pick_up_{fid}"]["process_reference"]
        process["first_candidate_z_mm"] -= .4
        process["reference_interval_mm"] = [value-.4 for value in process["reference_interval_mm"]]
        process["minimum_z_mm"] -= .4
    if also_shift_observation:
        next(event for event in recording["events"] if event["kind"] == "step.verified"
             and event["payload"]["name"] == "v1_first_contact_stamp")["payload"]["measurements"]["first_contact_region"] = deepcopy(
                state["phases"]["v1_first_contact_stamp"]["first_contact_region"])
    result = process_score(recording)
    assert result["consistent"] is False
    assert result["checks"]["transfer:first_contact_acquisition"] is False
    assert result["checks"]["transfer:first_contact_receipt"] is also_shift_observation


def sync_v2_endpoint(recording, name):
    """Keep redundant receipts consistent so tests exercise the scientific check."""
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    endpoint = state["phases"][name]["endpoint"]
    for event in recording["events"]:
        payload = event["payload"]
        if event["kind"] == "step.verified" and payload["name"] == name:
            payload["measurements"] = deepcopy(endpoint)
            state["phases"][name]["endpoint_verified"]["observation_sha256"] = v2_evidence_digest(
                {key: payload[key] for key in ("seq", "name", "verdict", "measurements", "expected")})
        if event["kind"] == "stack.pickup_endpoint" and "v1_pick_up_"+payload["flake_id"] == name:
            payload["observation"]["measurements"] = deepcopy(endpoint)
        if event["kind"] == "material_metal.released" and name == "release_material_metal_stack":
            payload["measurement"] = deepcopy(endpoint)


@pytest.mark.parametrize("change,failed", [
    ("missing_af", "hbn:native20_focus"), ("borrowed_af", "hbn:native20_focus"),
    ("boolean_af_id", "hbn:native20_focus"), ("af_hash", "hbn:native20_focus"),
    ("capture_pose", "hbn:native20_focus"), ("capture_hash", "hbn:native20_focus"),
    ("unresolved_peak", "hbn:native20_focus"), ("double_readback_error", "hbn:native20_focus"),
    ("wrong_anchor", "gr:process_reference"), ("first_nonzero_delta", "hbn:process_reference"),
    ("negative_up", "gr:process_reference"), ("mapping_error_once", "gr:process_reference"),
    ("minimum_lower_edge", "gr:process_reference"), ("wider_press", "gr:process_reference"),
    ("below_minimum_command", "gr:process_reference"), ("source_present", "hbn:source_disappearance"),
    ("source_damage", "hbn:source_disappearance"), ("source_stale", "hbn:source_disappearance"),
    ("source_wrong_view", "hbn:source_disappearance"), ("source_wrong_focus", "hbn:source_disappearance"),
    ("target_preexisting", "target_material_appearance"), ("target_unknown", "target_material_appearance"),
    ("target_wrong_chip", "target_after"), ("target_held_path", "target_after"),
    ("target_moved", "target_after"), ("target_stale_before", "target_material_appearance"),
])
def test_v2_audit_recomputes_intervals_and_requires_qualified_visual_evidence(change, failed):
    recording = native_process_recording(v2=True)
    events = recording["events"]
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    source = state["source_references"]["hbn"]
    proof = source["focus_binding"]
    pickup = state["phases"]["v1_pick_up_hbn"]
    later = state["phases"]["v1_pick_up_gr"]["process_reference"]
    release = state["phases"]["release_material_metal_stack"]["endpoint"]
    af = next(event for event in events if event["id"] == proof["event_id"])
    capture = next(event for event in events if event["id"] == proof["capture_event_id"])
    if change == "missing_af":
        events.remove(af)
    elif change == "borrowed_af":
        af["run_id"] = "another"
    elif change == "boolean_af_id":
        proof["event_id"] = True
    elif change == "af_hash":
        proof["event_sha256"] = "f"*64
    elif change == "capture_pose":
        capture["payload"]["microscope_preview"]["stage_kinematics"]["sample_pose"]["x"] += .1
        proof["capture_event_sha256"] = v2_evidence_digest(capture["payload"])
    elif change == "capture_hash":
        capture["payload"]["microscope_preview"]["image_sha256"] = "f"*64
        proof["capture_event_sha256"] = v2_evidence_digest(capture["payload"])
    elif change in ("unresolved_peak", "double_readback_error"):
        if change == "unresolved_peak":
            af["payload"]["sampled_passes"][0]["unresolved_peaks"] = True
        else:
            af["payload"]["focus_interval_mm"][0] -= .002
            af["payload"]["focus_interval_mm"][1] += .002
            proof["observation"]["focus_interval_mm"] = deepcopy(af["payload"]["focus_interval_mm"])
        proof["event_sha256"] = v2_evidence_digest(af["payload"])
    elif change == "wrong_anchor":
        state["placements"]["stamp"]["source_anchor"]["source_id"] = "gr"
    elif change == "first_nonzero_delta":
        state["phases"]["v1_pick_up_hbn"]["process_reference"]["delta_interval_mm"] = [-.03, .03]
    elif change in ("negative_up", "mapping_error_once"):
        low0, high0 = source["focus_binding"]["observation"]["focus_interval_mm"]
        low, high = state["source_references"]["gr"]["focus_binding"]["observation"]["focus_interval_mm"]
        delta = [-1.2*(high-low0)-.006, -1.2*(low-high0)+.006] if change == "negative_up" else [1.2*(low-high0)-.003, 1.2*(high-low0)+.003]
        later.update(delta_interval_mm=delta, reference_interval_mm=[6.+value for value in delta], minimum_z_mm=6.+delta[1]-.2)
    elif change == "minimum_lower_edge":
        later["minimum_z_mm"] = later["reference_interval_mm"][0]-.2
    elif change == "wider_press":
        later["max_press_mm"] = .3
        later["minimum_z_mm"] = later["reference_interval_mm"][1]-.3
    elif change == "below_minimum_command":
        start = next(event for event in events if event["kind"] == "step.started" and event["payload"]["name"] == "v1_pick_up_gr")
        next(event for event in events if event["id"] > start["id"] and event["kind"] == "device.command")["payload"]["params"]["value"] = later["minimum_z_mm"]-.01
    elif change == "source_present":
        pickup["endpoint"].update(flake_on_chip=True, source_detection_count=1)
    elif change == "source_damage":
        pickup["film_integrity"] = {"film_intact": False}
    elif change == "source_stale":
        pickup["endpoint"]["source_frames"][0]["captured_at"] -= 10
    elif change == "source_wrong_view":
        pickup["endpoint"]["source_frames"][0]["meta"]["stage_kinematics"]["sample_pose"]["x"] += 1
    elif change == "source_wrong_focus":
        pickup["endpoint"]["source_frames"][0]["meta"]["focus_z"] += .1
    elif change == "target_preexisting":
        state["target_reference"]["materials_found"]["Gr"] = 1
        release["target_before_sha256"] = v2_evidence_digest(state["target_reference"])
    elif change == "target_unknown":
        del release["target_after"]["materials_found"]["Gr"]
    elif change == "target_wrong_chip":
        release["target_after"]["target_chip_id"] = "other"
    elif change == "target_held_path":
        release["target_after"]["focus_reference"]["focus_binding"]["optical_path"] = "parked_unobstructed"
    elif change == "target_moved":
        release["target_after"]["requested_stage_xy_mm"][0] += .1
    elif change == "target_stale_before":
        state["target_reference"]["frames"][-1]["captured_at"] = state["phases"]["release_material_metal_stack"]["started_at"]
        release["target_before_sha256"] = v2_evidence_digest(state["target_reference"])
    # Rebind redundant hashes after intentional focus changes; a hash mismatch
    # alone must not be the only reason malformed optical intervals are refused.
    anchor_hash = v2_evidence_digest(proof)
    state["placements"]["stamp"]["source_anchor"]["focus_binding_sha256"] = anchor_hash
    for fid in ("hbn", "gr"):
        process = state["phases"][f"v1_pick_up_{fid}"]["process_reference"]
        process["anchor_focus_binding_sha256"] = anchor_hash
        process["current_focus_binding_sha256"] = v2_evidence_digest(state["source_references"][fid]["focus_binding"])
        sync_v2_endpoint(recording, f"v1_pick_up_{fid}")
    sync_v2_endpoint(recording, "release_material_metal_stack")
    result = process_score(recording)
    assert result["consistent"] is False, result
    assert result["checks"]["transfer:"+failed] is False, result


def test_v2_export_path_projection_preserves_audit_without_erasing_other_evidence():
    recording = native_process_recording(v2=True)
    exported = deepcopy(recording)

    def rewrite(value):
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return "media/"+value if isinstance(value, str) and value.endswith(".png") else value
    exported = rewrite(exported)
    assert process_score(recording) == process_score(exported)
    assert process_score(exported)["consistent"] is True
    proof = exported["runs"]["transfer"]["meta"]["normal_transfer"]["source_references"]["hbn"]["focus_binding"]
    focus = next(event for event in exported["events"] if event["id"] == proof["event_id"])
    focus["payload"]["holder_pose"]["z"] -= .1
    assert process_score(exported)["checks"]["transfer:hbn:native20_focus"] is False


@pytest.mark.parametrize("change", ["missing_content_hash", "conflicting_content_hash", "empty_locator", "nonstring_locator", "other_field"])
def test_v2_portability_cannot_erase_unbound_images_or_other_event_fields(change):
    recording = native_process_recording(v2=True)
    proof = recording["runs"]["transfer"]["meta"]["normal_transfer"]["source_references"]["hbn"]["focus_binding"]
    af = next(event for event in recording["events"] if event["id"] == proof["event_id"])["payload"]
    if change == "missing_content_hash":
        del af["image_sha256"]
    elif change == "conflicting_content_hash":
        af["sha256"] = "f"*64
    elif change == "empty_locator":
        af["image"] = ""
    elif change == "nonstring_locator":
        af["image"] = {"path": "old.png"}
    else:
        af["extra_recorded_field"] = "changed"
    result = process_score(recording)
    assert result["checks"]["transfer:hbn:native20_focus"] is False


@pytest.mark.parametrize("change,failed", [
    ("candidate_event_id", "first_contact_acquisition"), ("candidate_event_hash", "first_contact_acquisition"),
    ("candidate_pose", "first_contact_acquisition"), ("baseline_event_id", "v1_pick_up_hbn:reference_event"),
    ("baseline_event_hash", "v1_pick_up_hbn:reference_event"), ("missing_baseline", "v1_pick_up_hbn:reference_event"),
    ("endpoint_event_id", "hbn:endpoint_verified"), ("endpoint_event_hash", "hbn:endpoint_verified"),
    ("endpoint_expected", "hbn:endpoint_verified"), ("endpoint_image_order", "hbn:endpoint_verified"),
    ("missing_release_observation", "release_endpoint_verified"),
])
def test_v2_historical_candidate_baseline_and_endpoint_bindings_cannot_be_borrowed(change, failed):
    recording = native_process_recording(v2=True)
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    placement = state["placements"]["stamp"]
    pickup = state["phases"]["v1_pick_up_hbn"]
    events = recording["events"]
    if change == "candidate_event_id":
        placement["candidate_evidence"]["event_id"] = pickup["reference_event"]["event_id"]
    elif change == "candidate_event_hash":
        placement["candidate_evidence"]["event_sha256"] = "f"*64
    elif change == "candidate_pose":
        fact = next(event for event in events if event["id"] == placement["candidate_evidence"]["event_id"])
        fact["payload"]["value"]["actual_holder_pose"]["z"] -= .4
        placement["candidate_evidence"]["event_sha256"] = v2_evidence_digest(fact["payload"])
    elif change == "baseline_event_id":
        pickup["reference_event"] = deepcopy(state["phases"]["v1_pick_up_gr"]["reference_event"])
    elif change == "baseline_event_hash":
        pickup["reference_event"]["event_sha256"] = "f"*64
    elif change == "missing_baseline":
        del pickup["reference_event"]
    elif change == "endpoint_event_id":
        pickup["endpoint_verified"] = deepcopy(state["phases"]["v1_pick_up_gr"]["endpoint_verified"])
    elif change == "endpoint_event_hash":
        pickup["endpoint_verified"]["observation_sha256"] = "f"*64
    elif change in ("endpoint_expected", "endpoint_image_order"):
        payload = next(event for event in events if event["id"] == pickup["endpoint_verified"]["event_id"])["payload"]
        if change == "endpoint_expected":
            payload["expected"] = {"changed": True}
        else:
            payload["images"].reverse()
    elif change == "missing_release_observation":
        del state["phases"]["release_material_metal_stack"]["endpoint_verified"]
    result = process_score(recording)
    assert result["consistent"] is False, result
    assert result["checks"]["transfer:"+failed] is not True, result


def test_v2_target_classification_change_on_identical_image_content_is_not_appearance():
    recording = native_process_recording(v2=True)
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    endpoint = state["phases"]["release_material_metal_stack"]["endpoint"]
    before, after = state["target_reference"], endpoint["target_after"]
    for old, new in zip(before["frames"], after["frames"], strict=True):
        new["sha256"] = old["sha256"]
        capture = next(event for event in recording["events"] if event["kind"] == "lab.frame"
                       and event["payload"]["microscope_preview"]["image"] == new["image"])
        capture["payload"]["microscope_preview"]["image_sha256"] = new["sha256"]
    after["focus_reference"]["sha256"] = after["frames"][0]["sha256"]
    proof = after["focus_reference"]["focus_binding"]
    capture = next(event for event in recording["events"] if event["id"] == proof["capture_event_id"])
    proof["capture_event_sha256"] = v2_evidence_digest(capture["payload"])
    sync_v2_endpoint(recording, "release_material_metal_stack")
    result = process_score(recording)
    assert result["checks"]["transfer:target_before"] is True
    assert result["checks"]["transfer:target_after"] is True
    assert result["checks"]["transfer:target_material_appearance"] is False


@pytest.mark.parametrize("fid", ["hbn", "gr"])
def test_v2_source_disappearance_requires_changed_content_despite_fresh_consistent_receipts(fid):
    recording = native_process_recording(v2=True)
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    source = state["source_references"][fid]
    name = f"v1_pick_up_{fid}"
    frames = state["phases"][name]["endpoint"]["source_frames"]
    for frame in frames:
        assert frame["image"] != source["image"] and frame["captured_at"] > source["captured_at"]
        frame["sha256"] = source["sha256"]
    sync_v2_endpoint(recording, name)
    result = process_score(recording)
    assert result["consistent"] is False
    assert result["problems"] == [f"transfer:{fid}:source_disappearance"]
    assert not result["unknowns"]


@pytest.mark.parametrize("original_count,changed_count", [(0, 1), (1, 0)])
def test_v2_target_baseline_counts_cannot_change_after_journaled_classification(original_count, changed_count):
    recording = native_process_recording(v2=True)
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    before = state["target_reference"]
    before["materials_found"]["Gr"] = original_count
    proof = state["target_reference_verified"]
    payload = next(event for event in recording["events"] if event["id"] == proof["event_id"])["payload"]
    payload["measurements"]["target_reference_sha256"] = v2_evidence_digest(before)
    proof["observation_sha256"] = v2_evidence_digest(
        {key: payload[key] for key in ("seq", "name", "verdict", "measurements", "expected")})
    before["materials_found"]["Gr"] = changed_count
    state["phases"]["release_material_metal_stack"]["endpoint"]["target_before_sha256"] = v2_evidence_digest(before)
    sync_v2_endpoint(recording, "release_material_metal_stack")
    result = process_score(recording)
    assert result["consistent"] is False
    assert result["checks"]["transfer:target_before"] is True
    assert result["checks"]["transfer:target_before_journal"] is False
    if original_count == 1:
        assert result["problems"] == ["transfer:target_before_journal"] and not result["unknowns"]


@pytest.mark.parametrize("change", ["missing_binding", "missing_event", "borrowed_event", "wrong_run", "changed_expected", "reordered_images", "wrong_expected_target", "wrong_expected_roi", "wrong_expected_roi_subtolerance", "not_on_chip", "future_baseline"])
def test_v2_target_baseline_requires_its_own_target_roi_and_verified_history(change):
    recording = native_process_recording(v2=True)
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    proof = state["target_reference_verified"]
    event = next(event for event in recording["events"] if event["id"] == proof["event_id"])
    if change == "missing_binding":
        del state["target_reference_verified"]
    elif change == "missing_event":
        recording["events"].remove(event)
    elif change == "borrowed_event":
        state["target_reference_verified"] = deepcopy(state["phases"]["v1_pick_up_hbn"]["endpoint_verified"])
    elif change == "wrong_run":
        event["run_id"] = "other"
    elif change == "changed_expected":
        event["payload"]["expected"]["min_sharpness"] += 1
    elif change == "reordered_images":
        event["payload"]["images"].reverse()
    elif change == "future_baseline":
        event["ts"] = state["target_reference"]["frames"][-1]["captured_at"]-.1
    else:
        if change == "wrong_expected_target":
            event["payload"]["expected"]["on_chip"] = "other"
        elif change == "wrong_expected_roi":
            event["payload"]["expected"]["stage_xy"] = [4., 5.]
        elif change == "wrong_expected_roi_subtolerance":
            event["payload"]["expected"]["stage_xy"] = [1.0001, 2.]
        else:
            event["payload"]["measurements"]["on_chip"] = False
        proof["observation_sha256"] = v2_evidence_digest(
            {key: event["payload"][key] for key in ("seq", "name", "verdict", "measurements", "expected")})
    result = process_score(recording)
    assert result["checks"]["transfer:target_before_journal"] is not True
    assert result["consistent"] is False


@pytest.mark.parametrize("reason", ["source_present", "film_damage", "focus_unknown"])
def test_v2_truthful_pickup_hold_never_becomes_process_success(reason):
    recording = native_process_recording(v2=True)
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    phase = state["phases"]["v1_pick_up_hbn"]
    if reason == "source_present":
        phase["endpoint"].update(flake_on_chip=True, source_detection_count=1, transfer_confirmed=False, process_confirmation=False)
    elif reason == "film_damage":
        phase["film_integrity"] = {"film_intact": False}
    else:
        del phase["endpoint"]
    run["status"] = "waiting_human"
    finish = next(event for event in recording["events"] if event["kind"] == "step.finished" and event["payload"]["name"] == "v1_pick_up_hbn")
    finish["payload"]["status"] = "failed"
    recording["events"] = [event for event in recording["events"] if event["id"] <= finish["id"]
        and event["kind"] != "stack.pickup_endpoint"
        and not (event["kind"] == "step.verified" and event["payload"]["name"] == "v1_pick_up_hbn")]
    recording["events"].append({"id": finish["id"]+1, "run_id": "transfer", "kind": "job.status", "payload": {"status": "waiting_human"}})
    state["phases"] = {key: value for key, value in state["phases"].items() if key in ("v1_first_contact_stamp", "v1_pick_up_hbn")}
    state["initializations"] = {"v1_initialize_hbn": state["initializations"]["v1_initialize_hbn"]}
    state["source_references"] = {"hbn": state["source_references"]["hbn"]}
    del state["target_reference"]
    del state["target_reference_verified"]
    result = process_score(recording)
    assert result["state"] == "incomplete" and result["consistent"] is False, result
    assert not result["problems"] and result["checks"]["transfer:hbn:source_disappearance"] is None


def held_recording_with_clear_lift():
    """Recorded-only failed second pickup followed by two observed lift segments."""
    import hashlib

    recording = native_process_recording(v2=True)
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    parent, name = "v1_pick_up_gr", "clear_lift_v1_pick_up_gr"
    phase = state["phases"][parent]
    last = next(event for event in recording["events"] if event["kind"] == "lab.frame"
                and event["payload"]["microscope_preview"]["image"] == phase["last_frame"]["image"])
    recording["events"] = [event for event in recording["events"] if event["id"] <= last["id"]]
    command = next(event for event in reversed(recording["events"]) if event["kind"] == "device.command")
    command["payload"]["params"]["value"] = 6.47
    phase.update(complete=False, difference_active=True, last_pose={"x": .02, "y": -.01, "z": 6.47})
    phase["last_intent"].update(from_mm=6.48, to_mm=6.47, macro_target_mm=6.47)
    for field in ("coverage", "endpoint", "endpoint_verified"):
        phase.pop(field, None)
    del state["phases"]["release_material_metal_stack"], state["initializations"]["v1_initialize_target"]
    del state["target_reference"], state["target_reference_verified"]
    run["status"] = "waiting_human"
    clock = [last["ts"]+.1]

    def emit(kind, payload):
        event = {"id": recording["events"][-1]["id"]+1, "ts": clock[0], "run_id": run["id"], "kind": kind, "payload": deepcopy(payload)}
        recording["events"].append(event)
        return event

    def capture(index):
        record = {"image": f"clear-{index}.png", "sha256": hashlib.sha256(f"clear-{index}".encode()).hexdigest(),
                  "captured_at": clock[0], "meta": deepcopy(phase["last_frame"]["meta"])}
        event = emit("lab.frame", {"microscope_preview": {"image": record["image"], "image_sha256": record["sha256"],
            "timestamp": clock[0], "phase": name, "immutable": True, "verification": False, **record["meta"]}})
        return record, event

    reference, baseline = capture(0)
    lift = {"name": name, "profile": phase["profile"], "started_at": clock[0], "observations": 3,
        "travel_mm": .03, "difference_active": False, "reference": reference,
        "reference_event": {"event_id": baseline["id"], "event_sha256": v2_evidence_digest(baseline["payload"])},
        "holder_xy": [.02, -.01], "complete": True}
    previous = 6.47
    for index, target in enumerate((6.49, 6.5), 1):
        at, duration = clock[0], (target-previous)/.1
        emit("device.command", {"device": "transfer_arm", "command": "move_axis_to", "params": {"axis": "z", "value": target, "speed": .1}})
        clock[0] += duration
        pose = {"x": .02, "y": -.01, "z": target}
        emit("lab.frame", {"device": "transfer_arm", "command": "move_axis_to", "manipulator": pose})
        frame, _ = capture(index)
        lift.update(last_frame=frame, last_pose=pose, observed_at=clock[0], last_intent={"axis": "z", "from_mm": previous,
            "to_mm": target, "speed_mm_s": .1, "duration_bound_s": duration, "macro_target_mm": 6.5, "at": at})
        previous = target
    state["phases"][name] = lift
    seq = next(event["payload"]["seq"] for event in recording["events"] if event["kind"] == "step.started" and event["payload"]["name"] == parent)
    emit("step.finished", {"seq": seq, "name": parent, "status": "failed", "note": "Candidate-relative pickup budget exhausted"})
    emit("job.status", {"status": "waiting_human"})
    return recording


def test_v2_failed_pickup_clear_lift_is_a_truthful_incomplete_prefix():
    result = process_score(held_recording_with_clear_lift())
    assert result["state"] == "incomplete" and result["consistent"] is False, result
    assert not result["problems"]
    assert result["checks"]["transfer:clear_lift_v1_pick_up_gr:safety_lift"] is True
    assert result["checks"]["transfer:phase_set"] is None
    assert result["checks"]["transfer:gr:pickup_endpoint"] is None
    assert result["checks"]["transfer:release_endpoint"] is None


@pytest.mark.parametrize("change", ["arbitrary_phase", "wrong_parent", "downward_press", "missing_baseline", "missing_readback",
    "wrong_run", "changed_stage", "unbounded_travel", "duplicate_contact", "hidden_xy", "successful_job", "successful_parent"])
def test_v2_clear_lift_name_cannot_hide_extra_control_or_unbound_evidence(change):
    recording = held_recording_with_clear_lift()
    run = recording["runs"]["transfer"]
    phases = run["meta"]["normal_transfer"]["phases"]
    name = "clear_lift_v1_pick_up_gr"
    lift = phases[name]
    baseline = lift["reference_event"]["event_id"]
    events = recording["events"]
    moves = [event for event in events if event["id"] > baseline and event["kind"] == "device.command"]
    if change == "arbitrary_phase":
        phases["another_descent"] = deepcopy(lift)
    elif change == "wrong_parent":
        lift["name"] = "clear_lift_v1_pick_up_hbn"
        phases[lift["name"]] = phases.pop(name)
    elif change == "downward_press":
        moves[0]["payload"]["params"]["value"] = 6.46
        next(event for event in events if event["id"] > baseline and event["payload"].get("manipulator"))["payload"]["manipulator"]["z"] = 6.46
    elif change == "missing_baseline":
        del lift["reference_event"]
    elif change == "missing_readback":
        events.remove(next(event for event in events if event["id"] > baseline and event["payload"].get("manipulator")))
    elif change == "wrong_run":
        next(event for event in events if event["id"] == baseline)["run_id"] = "other"
    elif change == "changed_stage":
        lift["last_frame"]["meta"]["stage_kinematics"]["sample_pose"]["x"] += .1
        next(event for event in events if event["kind"] == "lab.frame" and event["payload"].get("microscope_preview", {}).get("image") == lift["last_frame"]["image"])["payload"]["microscope_preview"]["stage_kinematics"]["sample_pose"]["x"] += .1
    elif change == "unbounded_travel":
        lift["travel_mm"] = float("inf")
    elif change == "duplicate_contact":
        lift["first_contact_region"] = deepcopy(phases["v1_first_contact_stamp"]["first_contact_region"])
    elif change == "hidden_xy":
        moves[0]["payload"]["params"]["axis"] = "x"
    elif change == "successful_job":
        run["status"] = events[-1]["payload"]["status"] = "succeeded"
    else:
        next(event for event in events if event["kind"] == "step.finished" and event["payload"]["name"] == "v1_pick_up_gr")["payload"]["status"] = "done"
    result = process_score(recording)
    assert result["consistent"] is False
    assert result["checks"]["transfer:phase_set"] is False, result
    if change == "successful_job":
        assert result["checks"]["transfer:clear_lift_v1_pick_up_gr:safety_lift"] is False


def test_v2_complete_process_accepts_only_valid_known_optional_transits():
    result = process_score(native_process_recording(v2=True, transits=True))
    assert result["consistent"] is True, result
    assert result["checks"]["transfer:phase_set"] is True
    assert result["checks"]["transfer:park_stamp_hbn_z:references"] is True  # Native20 after pickup.
    assert result["checks"]["transfer:v1_initialize_hbn_engage_z:transit"] is True


def test_v2_first_initialization_transit_is_bound_before_working_placement_exists():
    recording = native_process_recording(v2=True, transits=True)
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    finish = next(event for event in recording["events"] if event["kind"] == "step.finished" and event["payload"]["name"] == "v1_initialize_hbn")
    recording["events"] = [event for event in recording["events"] if event["id"] <= finish["id"]]
    recording["events"].append({"id": finish["id"]+1, "ts": finish["ts"]+.1, "run_id": "transfer", "kind": "job.status", "payload": {"status": "waiting_human"}})
    run["status"] = "waiting_human"
    state["placements"] = {}
    state["phases"] = {name:phase for name,phase in state["phases"].items() if phase["reference_event"]["event_id"] < finish["id"]}
    state["initializations"] = {"v1_initialize_hbn": state["initializations"]["v1_initialize_hbn"]}
    state["source_references"] = {"hbn": state["source_references"]["hbn"]}
    del state["target_reference"], state["target_reference_verified"]
    result = process_score(recording)
    assert result["state"] == "incomplete" and not result["problems"], result
    assert result["checks"]["transfer:v1_initialize_hbn_engage_z:transit"] is True
    assert result["checks"]["transfer:one_first_contact"] is None


@pytest.mark.parametrize("change", ["unknown_operation", "borrowed_baseline", "missing_baseline", "malformed_event_id", "wrong_held_xy", "wrong_objective",
    "below_clear_target", "missing_encoder", "bad_initial_encoder", "world_pose_only", "conflicting_encoder", "failed_parent", "duplicate_contact", "extra_xy"])
def test_v2_optional_transit_cannot_hide_press_or_borrow_parent_context(change):
    recording = native_process_recording(v2=True, transits=True)
    run = recording["runs"]["transfer"]
    phases = run["meta"]["normal_transfer"]["phases"]
    name = "v1_initialize_gr_engage_z"
    phase = phases[name]
    baseline = phase["reference_event"]["event_id"]
    after = next(event for event in recording["events"] if event["kind"] == "lab.frame" and event["payload"].get("microscope_preview", {}).get("image") == phase["last_frame"]["image"])
    moves = [event for event in recording["events"] if baseline < event["id"] < after["id"] and event["kind"] == "device.command"]
    readback = next(event for event in recording["events"] if baseline < event["id"] < after["id"] and event["kind"] == "lab.frame" and event["payload"].get("device") == "transfer_arm")
    if change == "unknown_operation":
        phases["v1_initialize_unknown_engage_z"] = deepcopy(phase)
    elif change == "borrowed_baseline":
        phase["reference_event"] = deepcopy(phases["park_stamp_hbn_z"]["reference_event"])
    elif change == "missing_baseline":
        del phase["reference_event"]
    elif change == "malformed_event_id":
        phase["reference_event"]["event_id"] = "not-an-event"
    elif change == "wrong_held_xy":
        phase["holder_xy"][0] += .1
    elif change == "wrong_objective":
        phase["reference"]["meta"]["objective"] = phase["last_frame"]["meta"]["objective"] = 20
        for event in recording["events"]:
            preview = event["payload"].get("microscope_preview", {})
            if preview.get("phase") == name:
                preview["objective"] = 20
        base = next(event for event in recording["events"] if event["id"] == baseline)
        phase["reference_event"]["event_sha256"] = v2_evidence_digest(base["payload"])
    elif change == "below_clear_target":
        target = 6.499
        moves[0]["payload"]["params"]["value"] = target
        readback["payload"]["geometry"]["microscope"]["stamp_axes_xyz_mm"]["z"] = target
        phase["last_pose"]["z"] = target
        phase["last_intent"].update(to_mm=target, duration_bound_s=(6.5004-target)/.1)
        phase["travel_mm"] = 6.5004-target
    elif change == "missing_encoder":
        recording["events"].remove(readback)
    elif change == "bad_initial_encoder":
        parent_start = next(event["id"] for event in reversed(recording["events"])
            if event["id"] < baseline and event["kind"] == "step.started"
            and event["payload"].get("name") == "v1_initialize_gr")
        initial = next(event for event in reversed(recording["events"])
            if parent_start < event["id"] < baseline and event["kind"] == "lab.frame"
            and event["payload"].get("device") == "transfer_arm"
            and ("manipulator" in event["payload"] or "stamp_axes_xyz_mm" in
                 event["payload"].get("geometry", {}).get("microscope", {})))
        # Corrupt the latest actual encoder, retaining the malformed event so an
        # older good encoder cannot supply a different initial transit context.
        if "manipulator" in initial["payload"]:
            initial["payload"]["manipulator"]["z"] = None
        else:
            initial["payload"]["geometry"]["microscope"]["stamp_axes_frame"] = "world"
    elif change == "world_pose_only":
        optics = readback["payload"]["geometry"]["microscope"]
        optics["stamp_holder_xyz"] = optics.pop("stamp_axes_xyz_mm")
    elif change == "conflicting_encoder":
        pose = deepcopy(readback["payload"]["geometry"]["microscope"]["stamp_axes_xyz_mm"])
        pose["z"] -= .1
        readback["payload"]["manipulator"] = pose
    elif change == "failed_parent":
        next(event for event in recording["events"] if event["kind"] == "step.finished" and event["payload"].get("name") == "v1_initialize_gr")["payload"]["status"] = "failed"
    elif change == "duplicate_contact":
        phase["first_contact_region"] = deepcopy(phases["v1_first_contact_stamp"]["first_contact_region"])
    elif change == "extra_xy":
        moves[0]["payload"]["params"]["axis"] = "x"
    result = process_score(recording)
    assert result["consistent"] is False
    assert result["checks"]["transfer:phase_set"] is False, result
    if change != "unknown_operation":
        assert result["checks"]["transfer:"+name+":transit"] is False


def reserved_tail_recording(status="consumed"):
    """One immutable reservation followed by a truthful pre-execution hold."""
    import math

    recording = native_process_recording(v2=True)
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    name = "v1_pick_up_gr"
    phase = state["phases"][name]
    capture = next(event for event in recording["events"] if event["kind"] == "lab.frame" and
                   event["payload"].get("microscope_preview", {}).get("image") == phase["last_frame"]["image"])
    recording["events"] = [event for event in recording["events"] if event["id"] <= capture["id"]]
    minimum, tau = phase["process_reference"]["minimum_z_mm"], .005
    current = minimum+.05
    admitted = math.nextafter(minimum+tau, math.inf)
    next(event for event in reversed(recording["events"]) if event["kind"] == "device.command")["payload"]["params"]["value"] = current
    phase["last_pose"]["z"] = current
    phase["last_intent"].update(from_mm=current+.01, to_mm=current, macro_target_mm=current)
    phase.update(complete=False, difference_active=True)
    for field in ("coverage", "endpoint", "endpoint_verified"):
        phase.pop(field, None)
    del state["phases"]["release_material_metal_stack"], state["initializations"]["v1_initialize_target"]
    del state["target_reference"], state["target_reference_verified"]
    admission = {"phase": name, "profile": "v1-normal-sim-v2", "rule": "final-process-budget-prefix-v1",
        "from_readback_z_mm": current, "requested_delta_mm": -.1, "requested_target_mm": current-.1,
        "admitted_delta_mm": admitted-current, "admitted_target_mm": admitted, "minimum_z_mm": minimum,
        "readback_tolerance_mm": tau, "global_z_interval_mm": [0., 10.],
        "process_reference_sha256": v2_evidence_digest(phase["process_reference"]), "reference_sha256": phase["reference"]["sha256"],
        "admission_observation_count": 3, "admitted_at": capture["ts"]+.1,
        "capture_event": {"event_id": capture["id"], "event_sha256": v2_evidence_digest(capture["payload"])}}
    event = {"id": capture["id"]+1, "ts": admission["admitted_at"], "run_id": "transfer", "kind": "stack.pickup_macro_admission", "payload": admission}
    recording["events"].append(event)
    phase["final_partial_macro"] = {"admission": deepcopy(admission), "event_id": event["id"], "event_sha256": v2_evidence_digest(admission), "status": status}
    seq = next(row["payload"]["seq"] for row in recording["events"] if row["kind"] == "step.started" and row["payload"]["name"] == name)
    recording["events"] += [{"id": event["id"]+1, "ts": event["ts"]+.1, "run_id": "transfer", "kind": "step.finished",
        "payload": {"seq": seq, "name": name, "status": "failed", "note": "Interrupted after one-shot reservation"}},
        {"id": event["id"]+2, "ts": event["ts"]+.2, "run_id": "transfer", "kind": "job.status", "payload": {"status": "waiting_human"}}]
    run["status"] = "waiting_human"
    return recording


@pytest.mark.parametrize("status", ["consumed", "failed", "stopped"])
def test_v2_reserved_tail_is_consumed_and_incomplete_without_inventing_future_endpoints(status):
    result = process_score(reserved_tail_recording(status))
    assert result["state"] == "incomplete" and not result["problems"], result
    assert result["checks"]["transfer:v1_pick_up_gr:tail_admission"] is True
    assert result["checks"]["transfer:v1_pick_up_gr:tail_completion"] is None
    assert result["checks"]["transfer:gr:pickup_endpoint"] is None


@pytest.mark.parametrize("change", ["missing_receipt", "missing_event", "duplicate_event", "borrowed_run", "wrong_status",
    "requested_target", "admitted_target", "minimum", "tau", "hardware_original_request", "process_hash", "reference_hash", "capture_hash", "observation_count", "stale_admission", "nonfinite_type"])
def test_v2_tail_reservation_and_finite_formula_cannot_be_rewritten(change):
    recording = reserved_tail_recording()
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["v1_pick_up_gr"]
    receipt = phase["final_partial_macro"]
    event = next(row for row in recording["events"] if row["kind"] == "stack.pickup_macro_admission")
    admission = event["payload"]
    if change == "missing_receipt":
        del phase["final_partial_macro"]
    elif change == "missing_event":
        recording["events"].remove(event)
    elif change == "duplicate_event":
        duplicate = deepcopy(event)
        duplicate["id"] += 1
        for row in recording["events"]:
            if row["id"] > event["id"]:
                row["id"] += 1
        recording["events"].insert(recording["events"].index(event)+1, duplicate)
    elif change == "borrowed_run":
        event["run_id"] = "other"
    elif change == "wrong_status":
        receipt["status"] = "available_again"
    else:
        if change == "requested_target":
            admission["requested_target_mm"] -= .01
        elif change == "admitted_target":
            admission["admitted_target_mm"] = admission["minimum_z_mm"]
            admission["admitted_delta_mm"] = admission["admitted_target_mm"]-admission["from_readback_z_mm"]
        elif change == "minimum":
            admission["minimum_z_mm"] -= .01
        elif change == "tau":
            admission["readback_tolerance_mm"] = .001
        elif change == "hardware_original_request":
            admission["global_z_interval_mm"][0] = admission["requested_target_mm"]+.01
        elif change == "process_hash":
            admission["process_reference_sha256"] = "f"*64
        elif change == "reference_hash":
            admission["reference_sha256"] = "f"*64
        elif change == "capture_hash":
            admission["capture_event"]["event_sha256"] = "f"*64
        elif change == "observation_count":
            admission["admission_observation_count"] -= 1
        elif change == "stale_admission":
            admission["admitted_at"] += 10
        elif change == "nonfinite_type":
            admission["from_readback_z_mm"] = "5.9"
        receipt["admission"] = deepcopy(admission)
        receipt["event_sha256"] = v2_evidence_digest(admission)
    result = process_score(recording)
    assert result["state"] == "inconsistent" and not result["consistent"], result
    suffix = "tail_reservation" if change in ("missing_receipt", "missing_event", "duplicate_event", "borrowed_run", "wrong_status") else "tail_admission"
    assert result["checks"]["transfer:v1_pick_up_gr:"+suffix] is False


def test_v2_completed_tail_uses_durable_tags_and_later_lifts_remain_ordinary():
    recording = native_process_recording(v2=True, tail=True)
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["v1_pick_up_gr"]
    assert "macro_admission_event_id" not in phase["last_intent"]
    result = process_score(recording)
    assert result["consistent"] is True, result
    for suffix in ("tail_reservation", "tail_admission", "tail_segments", "tail_coverage", "tail_completion"):
        assert result["checks"]["transfer:v1_pick_up_gr:"+suffix] is True
    assert phase["final_partial_macro"]["admission"]["requested_target_mm"] < phase["process_reference"]["minimum_z_mm"]


@pytest.mark.parametrize("sample_index", [0, -1], ids=["midpoint", "terminal"])
@pytest.mark.parametrize("change", ["missing_axes", "malformed_axes", "missing_z", "nonfinite_z", "wrong_frame", "conflicting_pose"])
def test_v2_tail_rejects_damaged_public_sample_without_substituting_another(sample_index, change):
    recording = native_process_recording(v2=True, tail=True)
    events = recording["events"]
    capture = next(row for row in events if "transfer_motion_intent" in row["payload"].get("microscope_preview", {}))
    command = next(row for row in reversed(events[:events.index(capture)]) if row["kind"] == "device.command")
    readbacks = [row for row in events if command["id"] < row["id"] < capture["id"] and row["kind"] == "lab.frame"
                 and row["payload"].get("device") == "transfer_arm" and "geometry" in row["payload"]]
    assert len(readbacks) == 2
    assert abs(readbacks[0]["payload"]["geometry"]["microscope"]["stamp_axes_xyz_mm"]["z"]-command["payload"]["params"]["value"]) <= .005
    sample = readbacks[sample_index]
    optics = sample["payload"]["geometry"]["microscope"]
    if change == "missing_axes":
        del optics["stamp_axes_xyz_mm"]
    elif change == "malformed_axes":
        optics["stamp_axes_xyz_mm"] = None
    elif change == "missing_z":
        del optics["stamp_axes_xyz_mm"]["z"]
    elif change == "nonfinite_z":
        optics["stamp_axes_xyz_mm"]["z"] = float("nan")
    elif change == "wrong_frame":
        optics["stamp_axes_frame"] = "world"
    elif change == "conflicting_pose":
        sample["payload"]["manipulator"] = {**optics["stamp_axes_xyz_mm"], "z": optics["stamp_axes_xyz_mm"]["z"]+.1}
    result = process_score(recording)
    assert result["state"] == "inconsistent" and result["consistent"] is False
    assert result["checks"]["transfer:v1_pick_up_gr:tail_segments"] is False
    assert result["checks"]["transfer:v1_pick_up_gr:tail_completion"] is False


@pytest.mark.parametrize("change", ["missing_middle_tag", "missing_all_tags", "admission_id", "macro_target", "requested_provenance",
    "dispatch_original_R", "command_disagrees", "from_readback", "readback_below_M", "readback_xy", "missing_readback",
    "late_admission", "deleted_event_and_receipt", "tagged_lift", "wrong_completed_status"])
def test_v2_tail_tags_bind_actual_commands_readbacks_and_capture_order(change):
    recording = native_process_recording(v2=True, tail=True)
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["v1_pick_up_gr"]
    events = recording["events"]
    admission = next(row for row in events if row["kind"] == "stack.pickup_macro_admission")
    captures = [row for row in events if "transfer_motion_intent" in row["payload"].get("microscope_preview", {})]
    capture = captures[0]
    intent = capture["payload"]["microscope_preview"]["transfer_motion_intent"]
    command = next(row for row in reversed(events[:events.index(capture)]) if row["kind"] == "device.command" and row["payload"].get("device") == "transfer_arm")
    readback = next(row for row in events if command["id"] < row["id"] < capture["id"] and row["kind"] == "lab.frame" and row["payload"].get("device") == "transfer_arm")
    if change == "missing_middle_tag":
        del captures[2]["payload"]["microscope_preview"]["transfer_motion_intent"]
    elif change == "missing_all_tags":
        for row in captures:
            del row["payload"]["microscope_preview"]["transfer_motion_intent"]
    elif change == "admission_id":
        intent["macro_admission_event_id"] += 1
    elif change == "macro_target":
        intent["macro_target_mm"] = admission["payload"]["requested_target_mm"]
    elif change == "requested_provenance":
        intent["requested_macro_target_mm"] = admission["payload"]["admitted_target_mm"]
    elif change == "dispatch_original_R":
        command["payload"]["params"]["value"] = intent["to_mm"] = admission["payload"]["requested_target_mm"]
        readback["payload"]["geometry"]["microscope"]["stamp_axes_xyz_mm"]["z"] = intent["to_mm"]
    elif change == "command_disagrees":
        command["payload"]["params"]["value"] += .001
    elif change == "from_readback":
        intent["from_mm"] += .01
    elif change == "readback_below_M":
        readback["payload"]["geometry"]["microscope"]["stamp_axes_xyz_mm"]["z"] = phase["process_reference"]["minimum_z_mm"]-.001
    elif change == "readback_xy":
        readback["payload"]["geometry"]["microscope"]["stamp_axes_xyz_mm"]["x"] += .1
    elif change == "missing_readback":
        events[:] = [row for row in events if not (command["id"] < row["id"] < capture["id"] and row["kind"] == "lab.frame" and row["payload"].get("device") == "transfer_arm")]
    elif change == "late_admission":
        a, c = events.index(admission), events.index(command)
        assert c == a+1
        admission["id"], command["id"] = command["id"], admission["id"]
        events[a], events[c] = command, admission
        admission["ts"] = admission["payload"]["admitted_at"] = command["ts"]+.001
        phase["final_partial_macro"].update(event_id=admission["id"], admission=deepcopy(admission["payload"]), event_sha256=v2_evidence_digest(admission["payload"]))
        for row in captures:
            row["payload"]["microscope_preview"]["transfer_motion_intent"]["macro_admission_event_id"] = admission["id"]
    elif change == "deleted_event_and_receipt":
        events.remove(admission)
        del phase["final_partial_macro"]
    elif change == "tagged_lift":
        later = next(row for row in events if row["id"] > captures[-1]["id"] and row["payload"].get("microscope_preview", {}).get("phase") == "v1_pick_up_gr")
        later["payload"]["microscope_preview"]["transfer_motion_intent"] = deepcopy(intent)
    elif change == "wrong_completed_status":
        phase["final_partial_macro"]["status"] = "no_coverage"
    result = process_score(recording)
    assert result["state"] == "inconsistent" and result["consistent"] is False
    suffix = "tail_reservation" if change == "deleted_event_and_receipt" else "tail_segments"
    assert result["checks"]["transfer:v1_pick_up_gr:"+suffix] is False, result
    if change == "missing_middle_tag":
        assert result["problems"] == ["transfer:v1_pick_up_gr:tail_segments", "transfer:v1_pick_up_gr:tail_completion"]


@pytest.mark.parametrize("open_step", [False, True])
def test_v2_interrupted_reserved_tail_does_not_require_future_step_finish(open_step):
    recording = reserved_tail_recording("stopped")
    if open_step:
        recording["events"] = [row for row in recording["events"] if not (row["kind"] == "step.finished" and row["payload"].get("name") == "v1_pick_up_gr")]
    result = process_score(recording)
    assert result["state"] == "incomplete" and not result["problems"], result
    assert result["checks"]["transfer:v1_pick_up_gr:tail_segments"] is None


@pytest.mark.parametrize("delete_pending,delete_reservation", [(False, False), (True, False), (False, True)])
def test_v2_unsettled_tail_command_keeps_pending_proof_and_never_claims_capture(delete_pending, delete_reservation):
    recording = reserved_tail_recording("stopped")
    phase = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["v1_pick_up_gr"]
    admission = next(row for row in recording["events"] if row["kind"] == "stack.pickup_macro_admission")
    recording["events"] = [row for row in recording["events"] if row["id"] <= admission["id"]]
    current = admission["payload"]["from_readback_z_mm"]
    to = current-.005
    phase["pending_intent"] = {"axis": "z", "from_mm": current, "to_mm": to, "speed_mm_s": .005, "duration_bound_s": 1.,
        "macro_target_mm": admission["payload"]["admitted_target_mm"], "requested_macro_target_mm": admission["payload"]["requested_target_mm"],
        "macro_admission_event_id": admission["id"], "at": admission["ts"]+.01}
    recording["events"].append({"id": admission["id"]+1, "ts": phase["pending_intent"]["at"], "run_id": "transfer", "kind": "device.command",
        "payload": {"device": "transfer_arm", "command": "move_axis_to", "params": {"axis": "z", "value": to, "speed": .005}}})
    recording["events"].append({"id": admission["id"]+2, "ts": admission["ts"]+1.1, "run_id": "transfer", "kind": "job.status", "payload": {"status": "waiting_human"}})
    if delete_pending:
        del phase["pending_intent"]
    if delete_reservation:
        recording["events"].remove(admission)
        del phase["final_partial_macro"]
    result = process_score(recording)
    assert result["state"] == ("inconsistent" if delete_pending or delete_reservation else "incomplete"), result
    if delete_reservation:
        assert result["checks"]["transfer:v1_pick_up_gr:tail_reservation"] is False
    else:
        assert result["checks"]["transfer:v1_pick_up_gr:tail_segments"] is (False if delete_pending else None)


def test_v2_no_coverage_tail_at_admitted_bound_remains_incomplete():
    recording = native_process_recording(v2=True, tail=True)
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    phase = state["phases"]["v1_pick_up_gr"]
    tagged = [row for row in recording["events"] if "transfer_motion_intent" in row["payload"].get("microscope_preview", {})]
    last = tagged[-1]
    preview = last["payload"]["microscope_preview"]
    intent = preview["transfer_motion_intent"]
    phase.update(complete=False, difference_active=True, last_frame=deepcopy(phase["coverage"]["current"]),
        observed_at=preview["timestamp"], last_pose={"x": .02, "y": -.01, "z": intent["to_mm"]}, last_intent=deepcopy(intent),
        observations=3+len(tagged), travel_mm=.02+phase["final_partial_macro"]["admission"]["from_readback_z_mm"]-intent["to_mm"])
    phase["final_partial_macro"]["status"] = "no_coverage"
    phase["coverage"]["covered"] = False
    del phase["endpoint"], phase["endpoint_verified"]
    del state["phases"]["release_material_metal_stack"], state["initializations"]["v1_initialize_target"]
    del state["target_reference"], state["target_reference_verified"]
    recording["events"] = [row for row in recording["events"] if row["id"] <= last["id"]]
    recording["events"].append({"id": last["id"]+1, "ts": last["ts"]+.1, "run_id": "transfer", "kind": "job.status", "payload": {"status": "waiting_human"}})
    run["status"] = "waiting_human"
    result = process_score(recording)
    assert result["state"] == "incomplete" and not result["problems"], result
    assert result["checks"]["transfer:v1_pick_up_gr:tail_segments"] is True
    assert result["checks"]["transfer:v1_pick_up_gr:tail_completion"] is None


@pytest.mark.parametrize("change", ["source", "profile", "workflow", "order", "second_contact", "working_xy", "calibration",
    "reference_hash", "pending_intent", "observations", "phase_time", "travel", "motion", "missing_endpoint", "coverage_only",
    "wrong_run", "no_release", "failed_job", "missing_phase", "missing_binding", "missing_native_source", "same_source_id", "reaim_input", "difference_active"])
def test_recorded_process_rejects_unbound_incomplete_or_contradictory_claims(change):
    recording = native_process_recording()
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    phase = state["phases"]["v1_pick_up_hbn"]
    if change == "source":
        recording["meta"]["acceptance_evidence"]["v1_process_binding"]["execution_source_sha256"] = "b"*64
    elif change == "profile":
        run["params"]["transfer_profile"] = "previous-process"
    elif change == "workflow":
        run["meta"]["workflow"]["children"][1]["checkpoints"][0]["name"] = "align_stamp"
    elif change == "order":
        starts = [e for e in recording["events"] if e["kind"] == "step.started"]
        starts[3]["payload"]["seq"], starts[4]["payload"]["seq"] = starts[4]["payload"]["seq"], starts[3]["payload"]["seq"]
    elif change == "second_contact":
        state["placements"]["other_stamp"] = deepcopy(state["placements"]["stamp"])
    elif change == "working_xy":
        phase["holder_xy"][0] += 1
    elif change == "calibration":
        phase["reference"]["meta"]["calibration"]["photometry_profile"]["raster"] = "old-profile"
    elif change == "reference_hash":
        phase["reference"]["sha256"] = "changed"
    elif change == "pending_intent":
        phase["pending_intent"] = {"axis": "z"}
    elif change == "observations":
        phase["observations"] = 0
    elif change == "phase_time":
        phase["started_at"] -= 10000
    elif change == "travel":
        phase["travel_mm"] = float("inf")
    elif change == "motion":
        phase["last_intent"]["duration_bound_s"] = float("nan")
    elif change == "missing_endpoint":
        recording["events"] = [e for e in recording["events"] if e["kind"] != "stack.pickup_endpoint"]
    elif change == "coverage_only":
        for event in recording["events"]:
            if event["kind"] == "step.verified" and event["payload"]["name"] == "v1_pick_up_hbn":
                event["payload"]["measurements"]["transfer_confirmed"] = False
    elif change == "wrong_run":
        next(e for e in recording["events"] if e["kind"] == "stack.pickup_endpoint")["run_id"] = "another"
    elif change == "no_release":
        next(e for e in recording["events"] if e["kind"] == "step.verified" and e["payload"]["name"] == "release_material_metal_stack")["payload"]["measurements"]["release_confirmed"] = False
    elif change == "failed_job":
        run["status"] = "parked"
    elif change == "missing_phase":
        del state["phases"]["v1_pick_up_hbn"]
    elif change == "missing_binding":
        recording["meta"]["acceptance_evidence"] = {}
    elif change == "missing_native_source":
        state["source_references"] = {}
    elif change == "same_source_id":
        state["source_references"]["hbn"]["source_identity"]["source_id"] = "another"
    elif change == "reaim_input":
        state["phases"]["v1_first_contact_stamp"]["first_contact_region"]["reaim_inputs"][0]["actual_delta_mm"]["y"] = .5
    elif change == "difference_active":
        phase["difference_active"] = True
    result = process_score(recording)
    assert result["consistent"] is False, result
    assert result["state"] in ("incomplete", "inconsistent")


@pytest.mark.parametrize("story", ["complete", "collaboration", "honest_stop"])
def test_absent_or_legacy_transfer_is_readable_and_never_relabelled_v1(story):
    from qumus2.testbench.demo_scoring import audit_recorded_v1_process

    empty = audit_recorded_v1_process([], [], story=story)
    assert empty["consistent"] is (False if story == "complete" else None)
    assert empty["state"] == ("not_reached" if story == "complete" else "not_applicable")
    old = audit_recorded_v1_process([], [{"flow": "stack_device", "id": "old", "params": {}}], story=story)
    assert old["consistent"] is False and old["state"] == "incomplete"


@pytest.mark.parametrize("change", ["hidden_xy", "endpoint_after_step", "no_reaim_commands", "duplicate_run", "wrong_release_target", "missing_last_pose", "borrowed_phase_capture"])
def test_recorded_process_cannot_borrow_or_invent_motion_and_endpoint_receipts(change):
    recording = native_process_recording()
    events = recording["events"]
    if change == "hidden_xy":
        finish = next(e for e in events if e["kind"] == "step.finished" and e["payload"]["name"] == "v1_pick_up_hbn")
        events.insert(events.index(finish), {"id": 0, "run_id": "transfer", "kind": "device.command",
            "payload": {"device": "transfer_arm", "command": "move_axis_to", "params": {"axis": "x", "value": .3}}})
    elif change == "endpoint_after_step":
        endpoint = next(e for e in events if e["kind"] == "stack.pickup_endpoint")
        events.remove(endpoint)
        events.append(endpoint)
    elif change == "no_reaim_commands":
        events[:] = [e for e in events if not (e["kind"] == "device.command" and e["payload"]["params"]["axis"] in ("x", "y"))]
    elif change == "duplicate_run":
        recording["runs"]["borrowed"] = deepcopy(recording["runs"]["transfer"])
    elif change == "wrong_release_target":
        next(e for e in events if e["kind"] == "material_metal.released")["payload"]["chip_id"] = "other"
    elif change == "missing_last_pose":
        del recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["v1_pick_up_hbn"]["last_pose"]
    else:
        phases = recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]
        foreign = deepcopy(phases["v1_first_contact_stamp"]["first_contact_region"]["current"])
        phases["v1_pick_up_hbn"]["coverage"]["current"] = foreign
        for event in events:
            if event["kind"] == "step.verified" and event["payload"]["name"] == "v1_pick_up_hbn":
                event["payload"]["measurements"]["coverage"]["current"] = deepcopy(foreign)
            if event["kind"] == "stack.pickup_endpoint" and event["payload"]["flake_id"] == "hbn":
                event["payload"]["observation"]["measurements"]["coverage"]["current"] = deepcopy(foreign)
    for index, event in enumerate(events, 1):
        event["id"] = index
    assert process_score(recording)["consistent"] is False


def test_recorded_coverage_with_unverified_endpoint_remains_incomplete_execution():
    recording = native_process_recording()
    run = recording["runs"]["transfer"]
    run["status"] = "waiting_human"
    phase = run["meta"]["normal_transfer"]["phases"]["v1_pick_up_hbn"]
    phase["complete"] = False  # Coverage was reached; endpoint focus never completed.
    assert phase["coverage"]["covered"] is True
    finish = next(e for e in recording["events"] if e["kind"] == "step.finished" and e["payload"]["name"] == "v1_pick_up_hbn")
    finish["payload"]["status"] = "failed"
    recording["events"] = [e for e in recording["events"] if e["id"] <= finish["id"] and e["kind"] != "stack.pickup_endpoint"
                           and not (e["kind"] == "step.verified" and e["payload"]["name"] == "v1_pick_up_hbn")]
    recording["events"].append({"id": finish["id"]+1, "run_id": "transfer", "kind": "job.status", "payload": {"status": "waiting_human"}})
    state = run["meta"]["normal_transfer"]
    del state["phases"]["v1_pick_up_gr"], state["phases"]["release_material_metal_stack"]
    del state["initializations"]["v1_initialize_gr"], state["initializations"]["v1_initialize_target"]
    del state["source_references"]["gr"]
    result = process_score(recording)
    assert result["consistent"] is False
    assert result["state"] == "incomplete" and not result["problems"]
    assert result["checks"]["transfer:hbn:pickup_endpoint"] is None


def test_ambiguous_endpoint_receipts_are_unknown_not_verified():
    recording = native_process_recording()
    events = recording["events"]
    endpoint = next(e for e in events if e["kind"] == "stack.pickup_endpoint")
    events.insert(events.index(endpoint)+1, deepcopy(endpoint))
    for index, event in enumerate(events, 1):
        event["id"] = index
    result = process_score(recording)
    assert result["consistent"] is False and result["state"] == "incomplete"
    assert result["checks"]["transfer:hbn:pickup_endpoint"] is None


@pytest.fixture
def early_initialization_hold():
    """Native prefix: initialization is saved, then objective readback refuses."""
    recording = native_process_recording()
    run = recording["runs"]["transfer"]
    run["status"] = "parked"
    started = next(event for event in recording["events"] if event["kind"] == "step.started"
                   and event["payload"]["name"] == "v1_initialize_hbn")
    recording["events"] = [event for event in recording["events"] if event["id"] <= started["id"]]
    recording["events"].extend([
        {"id": started["id"]+1, "ts": started["ts"]+.1, "run_id": "transfer", "kind": "step.finished",
         "payload": {"seq": started["payload"]["seq"], "name": "v1_initialize_hbn", "status": "failed",
                     "note": "Low-power objective readback failed before holder engagement"}},
        {"id": started["id"]+2, "ts": started["ts"]+.2, "run_id": "transfer", "kind": "job.status",
         "payload": {"status": "parked"}},
    ])
    state = run["meta"]["normal_transfer"]
    run["meta"]["normal_transfer"] = {"source_references": {"hbn": state["source_references"]["hbn"]},
        "initializations": {"v1_initialize_hbn": {"stamp_id": run["params"]["stamp_id"],
            "profile": run["params"]["transfer_profile"], "substrate_focus_mm": .3, "objective": 20}}}
    return recording


def test_early_initialization_hold_leaves_unexecuted_first_contact_unknown(early_initialization_hold):
    recording = early_initialization_hold
    state = recording["runs"]["transfer"]["meta"]["normal_transfer"]
    assert state["initializations"] and "placements" not in state
    assert not any(event["kind"] == "step.started" and event["payload"]["name"].startswith("v1_first_contact_")
                   for event in recording["events"])
    result = process_score(recording)
    assert result["state"] == "incomplete" and result["consistent"] is False
    assert result["checks"]["transfer:observed_checkpoint_prefix"] is True
    assert result["checks"]["transfer:one_first_contact"] is None and not result["problems"]


@pytest.mark.parametrize("claim", ["premature", "wrong_stamp", "wrong_run", "wrong_profile", "repeat_contact", "completed_without_placement"])
def test_early_hold_cannot_hide_claimed_placement_or_contact_contradictions(early_initialization_hold, claim):
    recording = early_initialization_hold
    run = recording["runs"]["transfer"]
    state = run["meta"]["normal_transfer"]
    if claim == "repeat_contact":
        manifest = run["meta"]["workflow"]
        first = next(checkpoint for child in manifest["children"] for checkpoint in child["checkpoints"]
                     if checkpoint["name"] == "v1_first_contact_stamp")
        # Even missing state cannot erase repeated executed first-contact claims.
        run["meta"]["normal_transfer"] = {}
        for _ in range(2):
            recording["events"].insert(-1, {"kind": "step.started", "run_id": "transfer", "payload": deepcopy(first)})
        for index, event in enumerate(recording["events"], 1):
            event["id"] = index
    elif claim == "completed_without_placement":
        run["status"] = "succeeded"
        recording["events"][-1]["payload"]["status"] = "succeeded"
    else:
        placement = {"stamp_id": "stamp", "run_id": "transfer", "profile": run["params"]["transfer_profile"],
                     "working_xy_mm": [.02, -.01], "first_contact_step": "v1_first_contact_stamp"}
        if claim == "wrong_stamp":
            placement["stamp_id"] = "another_stamp"
        elif claim == "wrong_run":
            placement["run_id"] = "another_run"
        elif claim == "wrong_profile":
            placement["profile"] = "another_profile"
        state["placements"] = {"stamp": placement}
    result = process_score(recording)
    assert result["state"] == "inconsistent" and result["consistent"] is False
    assert result["checks"]["transfer:one_first_contact"] is False
    assert "transfer:one_first_contact" in result["problems"]
