"""Complete original-ready journals built from declared mocks, never a World."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_stamp_discovery_journal import PHASE, journal

from qumus2.core.polygons import rectangle
from qumus2.flows.library.stacking import _digest
from qumus2.records.discovery import (
    OBSTACLE_PROFILE,
    choose_clear_domain,
    discovery_policy,
    nominal_head_projection,
    original_low_z_prefix,
    validate_ready_receipt,
)
from qumus2.records.release_support import _encoder_projection


def ready_journal():
    policy, rows = discovery_policy(), []
    park = {"x": 163., "y": 10., "z": 6.5}
    pose = dict(park)
    cal = {"source": "synthetic", "image_size_px": [480, 320],
        "mm_per_pixel": [2.6/480, 1.72/320], "u_axis": "+world_x", "v_axis": "-world_y",
        "stamp_discovery": policy}
    native20_cal = {**cal, "mm_per_pixel": [.65/480, .43/320]}
    def stage(x=10., y=20.):
        sample = {"x": x, "y": y, "z": 0., "rotation": 0.}
        return {"sample_pose": sample, "coarse_pose": deepcopy(sample), "fine_xy_mm": [0., 0.]}
    def emit(kind, payload, ts):
        row = {"id": len(rows)+1, "ts": ts, "run_id": "r", "kind": kind, "payload": deepcopy(payload)}
        rows.append(row)
        return row
    def receipt(row):
        return {"event_id": row["id"], "event_sha256": _digest(row["payload"])}
    def raw(ts, device="transfer_arm", command="move_axis_to"):
        return emit("lab.frame", {"device": device, "command": command, "manipulator": pose,
            "geometry": {"source": "synthetic", "mechanics_version": 2, "microscope": {
                "source": "synthetic", "stamp_axes_frame": "transfer_arm", "stamp_axes_xyz_mm": pose,
                "stamp_holder_xyz": {"x": 200., "y": 300., "z": pose["z"]},
                "optical_axis_world_mm": [200., 300.]}}}, ts)
    def frame(ts, context, objective=20):
        emit("device.command", {"device": "microscope", "command": "capture", "params": {}}, ts)
        raw(ts, "microscope", "capture")
        sha = f'{len(rows)+1000:064x}'
        meta = {"objective": objective, "focus_z": 0., "focus_plane": "chip",
                "calibration": cal if objective == 5 else native20_cal,
                "stage_kinematics": context, "fov_mm": [2.6, 1.72] if objective == 5 else [.65, .43]}
        record = {"image": "/declared/"+sha+".png", "sha256": sha, "captured_at": ts, "meta": deepcopy(meta)}
        event = emit("lab.frame", {"device": "microscope", "command": "capture", "microscope_preview": {
            **meta, "image": record["image"], "image_sha256": sha, "timestamp": ts,
            "phase": "restore_stamp_discovery_source" if ts >= 107. else "declared_source",
            "purpose": "analysis", "source": "synthetic", "immutable": True, "verification": False}}, ts)
        return record, event
    def focus(ts, context):
        from qumus2.flows.library.stacking import _focus_summary

        interval = [9., 9.1] if ts == 95. else [1., 1.1] if ts == 97. else [2., 2.1]
        af = emit("microscope.focus", {"objective": 20, "plane": "chip", "peak_observable": True,
            "objective_readback": 20, "focus_plane": "chip", "focus_interval_mm": interval,
            "focus_axis_calibration": {"scale_mm_per_focus_mm": 1., "mapping_error_mm": .002},
            "final_focus_readback_z_mm": 0., "readback_focus_z_mm": 0.,
            "image_sha256": f'{len(rows)+2000:064x}', "captured_at": ts,
            "optical_calibration": native20_cal, "stage_kinematics": context, "holder_pose": pose}, ts)
        record, capture = frame(ts, context)
        record["focus_binding"] = {**receipt(af), "capture_event_id": capture["id"],
            "capture_event_sha256": _digest(capture["payload"]), "optical_path": "parked_unobstructed",
            "holder_pose": dict(pose), "park_xy_mm": [park["x"], park["y"]], "clear_z_mm": park["z"],
            "position_tolerance_mm": .005, "observation": _focus_summary(af["payload"])}
        return record
    def move(axis, target, ts):
        command = emit("device.command", {"device": "transfer_arm", "command": "move_axis_to",
            "params": {"axis": axis, "value": target, "from_pose": pose,
                       "duration_bound_s": .01}}, ts)
        before = dict(pose)
        pose[axis] = target
        encoder = raw(ts+.01)
        return command, {"axis": axis, "requested_mm": target, "before": before, "after": dict(pose),
            "actual_delta_mm": {key: pose[key]-before[key] for key in pose}, "readback_at": encoder["ts"]}

    params = {"device_design": {"declared": True}, "stamp_discovery_policy": policy,
        "stamp_id": "stamp", "flake_ids": ["hBN", "Gr"], "flake_chips": {"hBN": "source"},
        "design_park_pose": park}
    emit("job.submitted", {"params": params}, 94.)
    original = focus(95., stage())
    start = emit("step.started", {"name": "prepare_stamp_discovery"}, 95.1)
    maps = []
    for i, offset in enumerate(policy["map_offsets_mm"]):
        centre = [1.5+offset[0], offset[1]]
        record, capture = frame(96.+i*.1, stage(10.-centre[0], 20.-centre[1]), 5)
        obs = {"profile": OBSTACLE_PROFILE, "source": "synthetic", "conditional_visible_completeness": True,
            "unknown_outside_roi": True, "chip_id": "source", "calibration": cal,
            "centroid_sigma_mm": .001, "obstacles": [], "image_sha256": record["sha256"],
            "captured_at": capture["ts"], "supported_roi_px": [8, 30, 472, 288], "unknown_runs_px": []}
        event = emit("lab.evidence", {"property": "stamp_discovery_obstacles", "subject": "source",
            "value": obs, "source": {"event_ids": [capture["id"]], "sha256": record["sha256"]}}, capture["ts"])
        # The producer derives centres from the actual subtraction, including roundoff.
        maps.append({"capture": receipt(capture), "observation_event": receipt(event), "frame": record,
            "camera_center_mm": [10.-record["meta"]["stage_kinematics"]["sample_pose"]["x"],
                                 20.-record["meta"]["stage_kinematics"]["sample_pose"]["y"]]})
    focused = focus(97., stage(8.5))
    low = journal()
    def update_context(value):
        if isinstance(value, dict):
            for key in list(value):
                if key == "calibration":
                    value[key] = deepcopy(cal)
                elif key == "stage_kinematics":
                    value[key] = stage(8.5)
                else:
                    update_context(value[key])
        elif isinstance(value, list):
            for item in value:
                update_context(item)
    update_context(low)
    offset = len(rows)
    for row in low["rows"]:
        row["id"] += offset
        rows.append(row)
    discovery = deepcopy(low["discovery"])
    for key in ("post_candidate_focus_event_id", "clear_lift_end_event_id"):
        discovery[key] += offset
    for key in ("start_event_id", "end_event_id"):
        discovery["hold"][key] += offset
    candidate = low["candidate"]
    candidate.update(aim_center_px=[240., 160.], found=True, profile="v1-normal-sim-v2")
    fact = emit("lab.evidence", {"property": "first_contact_region", "subject": "stamp", "value": candidate,
        "bench": "sim", "observed_at": candidate["current"]["captured_at"], "source": {
            "kind": "synthetic", "reference": "r:"+PHASE, "sha256": candidate["current"]["sha256"]}}, 106.)
    restore_start = emit("step.started", {"name": "restore_stamp_discovery_source"}, 106.1)
    pose.update({"x": 40., "y": 90., "z": 6.51975})
    move("x", park["x"], 106.2)
    move("y", park["y"], 106.3)
    emit("device.command", {"device": "transfer_stage", "command": "move_to", "params": {"pose": stage()["coarse_pose"]}}, 106.4)
    restored = focus(107., stage())
    move("x", 40., 107.1)
    move("y", 90., 107.2)
    overview = emit("device.command", {"device": "overview_camera", "command": "capture", "params": {}}, 107.22)
    raw(107.22, "overview_camera", "capture")
    engagement_body = {"stamp_id": "stamp", "run_id": "r", "handoff_observation": {
        "stamp_id": "stamp", "run_id": "r", "observed_at": 107.22, "captures": [{
            "run_id": "r", "camera": "overview_camera", "region": "stamp_handoff", "frame_of": "arm_right",
            "capture_event_id": overview["id"], "capture_started_at": 107.22, "capture_returned_at": 107.22,
            "frame_device_id": "overview_camera", "frame_timestamp": 107.22,
            "image": None, "image_sha256": None, "detections": []}]}, "observation": {
                "verdict": "ok", "measurements": {key: round(value, 4) for key, value in pose.items()}}}
    engagement = emit("lab.evidence", {"property": "stamp_discovery_source_engagement", "subject": "stamp",
        "bench": "sim", "value": engagement_body, "source": {"kind": "synthetic",
            "event_ids": [overview["id"]], "sha256": _digest(engagement_body)}}, 107.23)
    lo = rows[-1]["id"]
    before = dict(pose)
    sx, sy = cal["mm_per_pixel"][0], -cal["mm_per_pixel"][1]
    shift = [before[key]-candidate["actual_holder_pose"][key] for key in ("x", "y")]
    centre = [candidate["aim_center_px"][i]+shift[i]/scale for i, scale in enumerate((sx, sy))]
    aim = [240., 160.-.219/abs(sy)]
    delta = [(aim[i]-centre[i])*scale for i, scale in enumerate((sx, sy))]
    inputs = [move(axis, before[axis]+delta[i], 107.3+i*.1)[1] for i, axis in enumerate(("x", "y"))]
    hi = rows[-1]["id"]
    placement = {"stamp_id": "stamp", "run_id": "r", "profile": "v1-normal-sim-v2",
        "first_contact_step": PHASE, "candidate_z_mm": candidate["actual_holder_pose"]["z"],
        "candidate_evidence": receipt(fact), "source_anchor": {"source_id": "hBN", "image_sha256": restored["sha256"],
            "focus_binding_sha256": _digest(restored["focus_binding"])},
        "holder_before_reaim": before, "holder_origin_shift_mm": shift, "restored_aim_center_px": centre,
        "image_anchor_px": [240., 160.], "first_contact_target_px": aim, "reaim_requested_mm": delta,
        "reaim_inputs": inputs, "reaim_readback": dict(pose), "working_xy_mm": [pose["x"], pose["y"]]}
    map_values = [{"observation": rows[m["observation_event"]["event_id"]-1]["payload"]["value"],
                   "camera_center_mm": m["camera_center_mm"]} for m in maps]
    chip = [list(p) for p in rectangle(10., 10.)]
    domain = choose_clear_domain(policy, map_values, nominal_center=[1.5, 0.], chip_polygon=chip, chip_id="source")
    head_row = rows[offset+1]
    head_refs = {"capture": receipt(rows[offset+2]), "encoder": {"event_id": head_row["id"],
        "profile": _encoder_projection(head_row)["profile"], "projection_sha256": _digest(_encoder_projection(head_row))}}
    discovery.update(profile=policy["id"], policy=policy, state="ready", source_id="hBN", chip_id="source",
        start_event_id=start["id"], started_at=start["ts"], original_reference=original, original_stage_pose=stage()["sample_pose"],
        focus_reference=focused, discovery_stage_pose=stage(8.5)["sample_pose"], maps=maps,
        nominal_center_mm=[1.5, 0.], chip_polygon_mm=chip, domain=domain, actual_head_reference=head_refs,
        actual_head_context=nominal_head_projection(head_row), actual_head_domain=domain,
        first_contact_reference=low["old_reference"], first_candidate=candidate, candidate_evidence=receipt(fact),
        restored_reference=restored, restored_stage_pose=stage()["sample_pose"], restore_before=stage(8.5)["sample_pose"],
        restore_start_event_id=restore_start["id"], reaim_start_event_id=lo, reaim_end_event_id=hi,
        restore_engagement=receipt(engagement),
        restore_step="restore_stamp_discovery_source", ready_placement=placement)
    discovery["low_z_prefix"] = original_low_z_prefix(rows, run_id="r", candidate=candidate, discovery=discovery)
    emit("step.verified", {"name": "restore_stamp_discovery_source", "verdict": "ok",
        "measurements": {"stamp_discovery_ready": placement}}, 107.5)
    ready = emit("lab.evidence", {"property": "stamp_discovery_ready", "subject": "stamp",
                                  "value": {"discovery_sha256": _digest(discovery), "ready_placement": placement}}, 107.6)
    discovery["ready_evidence"] = receipt(ready)
    return {"events": rows, "run_id": "r", "params": params, "discovery": discovery,
            "placement": placement, "digest": _digest}


def rebind_ready(case):
    discovery = case["discovery"]
    verified = next(row for row in case["events"] if row["kind"] == "step.verified"
                    and row["payload"].get("name") == "restore_stamp_discovery_source")
    verified["payload"]["measurements"]["stamp_discovery_ready"] = deepcopy(discovery["ready_placement"])
    row = next(row for row in case["events"] if row["id"] == discovery["ready_evidence"]["event_id"])
    row["payload"]["value"] = {"discovery_sha256": _digest({k: v for k, v in discovery.items() if k != "ready_evidence"}),
                               "ready_placement": deepcopy(discovery["ready_placement"])}
    discovery["ready_evidence"]["event_sha256"] = _digest(row["payload"])


def test_complete_ready_mock_recomputes_head_maps_restore_and_actual_reaim():
    case = ready_journal()
    result = validate_ready_receipt(**case)
    assert result["restore_reaim"]["working_xy_mm"] == [40., 90.219]
    assert result["actual_head_domain"]["center_mm"] == [1.5, 0.]
    assert result["low_z_prefix"]["first_lift_actual_mm"] == pytest.approx(.02)
    overview = next(r for r in case["events"] if r["payload"].get("device") == "overview_camera")
    assert result["restore_reaim"]["overview_capture_event_id"] == overview["id"]


@pytest.mark.parametrize("fault", ["foreign_camera", "foreign_motion", "camera_operation", "missing_receipt", "changed_observation"])
def test_only_bound_actual_restore_overview_capture_is_admitted(fault):
    case = ready_journal()
    discovery, events = case["discovery"], case["events"]
    overview = next(r for r in events if r["kind"] == "device.command" and r["payload"].get("device") == "overview_camera")
    if fault == "foreign_camera":
        overview["run_id"] = "other"
    elif fault == "foreign_motion":
        overview["payload"] = {"device": "arm_right", "command": "move_to", "params": {}}
    elif fault == "camera_operation":
        overview["payload"]["command"] = "preview"
    elif fault == "missing_receipt":
        discovery.pop("restore_engagement")
    else:
        fact = next(r for r in events if r["id"] == discovery["restore_engagement"]["event_id"])
        fact["payload"]["value"]["observation"]["verdict"] = "correctable"
    rebind_ready(case)
    with pytest.raises(ValueError):
        validate_ready_receipt(**case)


def test_actual_restore_initializer_retains_successful_overview_capture(monkeypatch):
    from qumus2.config import load_yaml
    from qumus2.core.clock import SimClock
    from qumus2.flows.library import stacking
    from qumus2.runtime.store import RunStore

    clock = SimClock(start=100.)
    store = RunStore(":memory:", clock=clock, bench="sim")
    params = {"procedure_contract": stacking.PROCEDURE_ID, "transfer_profile": stacking.TRANSFER_PROFILE_ID,
              "stamp_discovery_policy": discovery_policy()}
    rid = store.create_run("stack_device", params)
    scope = SimpleNamespace(focus=.1, objective=20)
    scope.get_focus = lambda: scope.focus
    scope.get_objective = lambda: scope.objective
    scope.set_focus = lambda value: setattr(scope, "focus", value)
    scope.set_objective = lambda value: setattr(scope, "objective", value)
    def look(ctx, camera, region, frame_of, **kwargs):
        eid = store.emit("device.command", {"device": camera, "command": "capture", "params": {}}, run_id=rid)
        kwargs["captures"].append({"run_id": rid, "camera": camera, "region": region, "frame_of": frame_of,
            "capture_event_id": eid, "capture_started_at": clock.now(), "capture_returned_at": clock.now(),
            "frame_device_id": camera, "frame_timestamp": clock.now(), "image": None, "image_sha256": None,
            "detections": []})
        return []
    monkeypatch.setattr(stacking, "look", look)
    monkeypatch.setattr(stacking.ManipulatorTo, "act", lambda *a, **kw: None)
    ctx = SimpleNamespace(params=params, store=store, run_id=rid, clock=clock, bench=load_yaml("bench.yaml"),
        verification=load_yaml("verification.yaml"), gate=SimpleNamespace(
            microscope=lambda device: scope, manipulator=lambda device: SimpleNamespace(
                get_positions=lambda: {"x": 40., "y": 90., "z": 6.5}),
            limits=SimpleNamespace(microscopes={"microscope": SimpleNamespace(focus=(-5., 20.))})))
    try:
        initializer = stacking.InitializeStamp("stamp", name="initialize_restored_discovery_source")
        initializer.act(ctx, None)
        body = initializer.discovery_engagement
        assert body["observation"]["verdict"] == "ok"
        capture = body["handoff_observation"]["captures"][0]
        command = store.events_after(capture["capture_event_id"]-1, limit=1)[0]
        assert command.payload == {"device": "overview_camera", "command": "capture", "params": {}}
        assert capture["region"] == "stamp_handoff" and capture["frame_of"] == "arm_right"
    finally:
        store.close()


def retained_store(case):
    state = {"discovery": case["discovery"], "placements": {"stamp": case["placement"]},
             "source_references": {}, "phases": {}}
    run = {"id": "r", "params": case["params"], "meta": {"normal_transfer": state}}
    def events_after(after_id=0, limit=500, run_id=None):
        result = []
        for row in case["events"]:
            if row["id"] > after_id and (run_id is None or row["run_id"] == run_id):
                result.append(SimpleNamespace(**row, model_dump=lambda row=row: deepcopy(row)))
        return result[:limit]
    def update_run_meta(rid, **fields):
        run["meta"].update(deepcopy(fields))
    return SimpleNamespace(get_run=lambda rid: run, events_after=events_after, bench="sim", update_run_meta=update_run_meta,
        get_steps=lambda rid: [], last_event_id=lambda: case["events"][-1]["id"]), state, run


@pytest.mark.parametrize("tamper", [False, True])
def test_live_retained_and_scorer_use_same_original_authority(monkeypatch, tamper):
    from qumus2.flows.library import stacking
    from qumus2.testbench.demo_scoring import _recorded_stamp_discovery

    case = ready_journal()
    if tamper:
        case["placement"]["holder_origin_shift_mm"][0] += .1
        rebind_ready(case)
    store, state, run = retained_store(case)
    # File-byte/native-focus readers have independent cohorts. These doubles
    # leave the complete original journal authority executing without World.
    monkeypatch.setattr(stacking, "_reference", lambda record: record)
    monkeypatch.setattr(stacking, "_validate_focus_reference", lambda *a, **kw: None)
    monkeypatch.setattr(stacking, "_process_reference", lambda *a, **kw: {})
    if tamper:
        with pytest.raises(ValueError):
            stacking._validate_discovery_ready(store, "r")
        with pytest.raises(ValueError):
            _recorded_stamp_discovery(case["events"], run_id="r", params=case["params"], state=state, digest=_digest)
        with pytest.raises(ValueError):
            stacking._admit_saved_transfer({**case["params"], "device_design": None}, run, store)
    else:
        expected = validate_ready_receipt(**case)
        assert stacking._validate_discovery_ready(store, "r") == expected
        assert _recorded_stamp_discovery(case["events"], run_id="r", params=case["params"], state=state, digest=_digest) == expected
        stacking._admit_saved_transfer({**case["params"], "device_design": None}, run, store)


def test_first_pickup_height_uses_discovery_to_restored_focus_interval(monkeypatch):
    from qumus2.flows.library import stacking

    case = ready_journal()
    store, state, _ = retained_store(case)
    discovery = case["discovery"]
    state["source_references"]["hBN"] = discovery["restored_reference"]
    state["phases"][PHASE] = {"profile": stacking.TRANSFER_PROFILE_ID, "complete": True,
                             "first_contact_region": discovery["first_candidate"]}
    case["placement"]["source_anchor"]["focus_binding_sha256"] = _digest(discovery["restored_reference"]["focus_binding"])
    rebind_ready(case)
    monkeypatch.setattr(stacking, "_reference", lambda record: record)
    monkeypatch.setattr(stacking, "_validate_focus_reference", lambda *a, **kw: None)
    result = stacking._process_reference(state, "hBN", "stamp", "r", .2, store)
    assert result["delta_interval_mm"] == pytest.approx([.896, 1.104])
    assert result["reference_interval_mm"] == pytest.approx([6.49975+.896, 6.49975+1.104])
    assert "reaim_readback" not in discovery["first_candidate"]


def test_linked_composition_has_one_discovery_then_distinct_restore_before_pickup():
    from test_device_design_pose_screen import bound_design, declared_spec
    from test_v1_transfer_sequence import bound_params

    from qumus2.config import load_yaml
    from qumus2.flows.library import stacking
    from qumus2.flows.workflow import WorkflowMismatch

    design = bound_design(declared_spec())
    params = bound_params(flake_ids=list(design.pickup_order), materials={"hBN": "hBN", "Gr": "Gr"},
        flake_chips={"hBN": "ca", "Gr": "cb"}, device_design=design,
        design_rotations_deg={"hBN": 0., "Gr": 0.}, stamp_discovery_policy=discovery_policy(),
        release_policy=stacking.V1_DROP_POLICY_ID)
    flow = stacking.StackDeviceFlow()
    flow._bench = {**load_yaml("bench.yaml"), "stamp_discovery": discovery_policy()}
    steps = flow.build_steps(params)
    classes = [type(step) for step in steps]
    prepare, first, restore, pickup = (classes.index(cls) for cls in (stacking.PrepareStampDiscovery,
        stacking.AdjustFirstContact, stacking.RestoreStampDiscoverySource, stacking.PickUpFlake))
    assert prepare < first < restore < pickup
    assert classes.count(stacking.PrepareStampDiscovery) == classes.count(stacking.RestoreStampDiscoverySource) == 1
    assert not steps[prepare].safe_to_resume and not steps[restore].safe_to_resume
    with pytest.raises(WorkflowMismatch, match="composed bench"):
        flow.validate_saved_params(bound_params().model_dump(mode="json"))


@pytest.mark.parametrize("fault", [None, "first_count", "restore_count", "restore_inputs"])
def test_recorded_new_phase_projections_bind_counts_and_ready_reaim(fault):
    from qumus2.records.discovery import original_work_usage
    from qumus2.testbench.demo_scoring import _recorded_discovery_phases

    case = ready_journal()
    discovery, events = case["discovery"], case["events"]
    rows = [r for r in events if discovery["start_event_id"] <= r["id"] <= discovery["clear_lift_end_event_id"]]
    first = {"name": PHASE, "profile": "v1-normal-sim-v2", "complete": True, "discovery_only": True,
        "first_contact_region": discovery["first_candidate"], "reference": discovery["first_contact_reference"],
        "reference_event": discovery["actual_head_reference"]["capture"], "started_at": discovery["started_at"],
        "observations": original_work_usage(rows, run_id="r")["capture_count"], "travel_mm": .02025}
    restored = discovery["restored_reference"]
    restore = {"name": "restore_stamp_discovery_source", "profile": "v1-normal-sim-v2", "complete": True,
        "reference": restored, "last_frame": restored, "started_at": restored["captured_at"],
        "observed_at": restored["captured_at"], "travel_mm": 0., "observations": 1,
        "xy_inputs": case["placement"]["reaim_inputs"], "reference_event": {
            "event_id": restored["focus_binding"]["capture_event_id"],
            "event_sha256": restored["focus_binding"]["capture_event_sha256"]}}
    if fault == "first_count":
        first["observations"] -= 1
    elif fault == "restore_count":
        restore["observations"] += 1
    elif fault == "restore_inputs":
        restore["xy_inputs"] = []
    state = {"discovery": discovery, "phases": {PHASE: first, "restore_stamp_discovery_source": restore}}
    assert _recorded_discovery_phases(events, run_id="r", state=state, digest=_digest) is (fault is None)


@pytest.mark.parametrize("fault", ["cancel", "budget", "candidate", "source_return"])
def test_actual_restore_step_stops_before_reaim_on_interrupted_or_changed_source(monkeypatch, fault):
    from qumus2.config import load_yaml
    from qumus2.core.clock import SimClock
    from qumus2.core.errors import NeedsHumanError, StopRequested
    from qumus2.core.types import Pose
    from qumus2.flows.library import stacking
    from qumus2.flows.step import Observation, Verdict

    case = ready_journal()
    discovery = case["discovery"]
    case["events"] = [r for r in case["events"] if r["id"] <= discovery["restore_start_event_id"]]
    discovery["state"] = "contact_discovered"
    store, state, run = retained_store(case)
    state["phases"][PHASE] = {"first_contact_region": deepcopy(discovery["first_candidate"])}
    if fault == "candidate":
        state["phases"][PHASE]["first_contact_region"]["actual_holder_pose"]["z"] -= .01
    verification = load_yaml("verification.yaml")
    params = {**case["params"], "procedure_contract": stacking.PROCEDURE_ID,
        "transfer_profile": stacking.TRANSFER_PROFILE_ID,
        "first_contact_sampling": stacking._first_contact_sampling(verification)}
    calls = []
    actual = Pose(**discovery["discovery_stage_pose"])
    class Stage:
        def get_sample_pose(self):
            return actual
        def get_pose(self):
            return actual
        def get_fine_position(self):
            return (0., 0.)
        def move_to(self, target):
            nonlocal actual
            calls.append("source_return")
            actual = target.model_copy(update={"x": target.x+.1})
    def check_cancel():
        if fault == "cancel":
            raise StopRequested("declared cancellation")
    class Park:
        def __init__(self, *a, **kw):
            pass
        def act(self, ctx, correction):
            check_cancel()
            calls.append("park")
        def verify(self, ctx):
            return Observation(Verdict.OK)
    monkeypatch.setattr(stacking, "ManipulatorTo", Park)
    ctx = SimpleNamespace(params=params, run_id="r", store=store, verification=verification,
        clock=SimClock(start=1000. if fault == "budget" else 106.1), check_cancel=check_cancel,
        gate=SimpleNamespace(stage=lambda device: Stage()))
    expected_error = StopRequested if fault == "cancel" else NeedsHumanError
    with pytest.raises(expected_error):
        stacking.RestoreStampDiscoverySource().act(ctx, None)
    assert calls == (["park", "source_return"] if fault == "source_return" else [])
    assert run["meta"]["normal_transfer"]["source_references"] == {}
    if fault != "candidate":
        # The interrupted state cannot be replayed as another source revisit.
        with pytest.raises(NeedsHumanError, match="original completed"):
            stacking.RestoreStampDiscoverySource().act(ctx, None)


@pytest.mark.parametrize("fault", ["head", "missing_head", "map_pose", "restore_step", "source_return",
    "reaim_axis", "reaim_target", "raw_readback", "candidate_z", "source_focus", "global_gap"])
def test_ready_rejects_semantic_tamper_even_when_ready_assertion_is_rehashed(fault):
    case = ready_journal()
    discovery, events = case["discovery"], case["events"]
    if fault in ("head", "missing_head"):
        row = next(r for r in events if r["id"] == discovery["actual_head_reference"]["encoder"]["event_id"])
        head = row["payload"]["geometry"]["microscope"]
        if fault == "head":
            head["stamp_holder_xyz"]["x"] += .1
        else:
            head.pop("stamp_holder_xyz")
    elif fault == "map_pose":
        discovery["maps"][0]["camera_center_mm"][0] += .01
    elif fault == "restore_step":
        discovery["restore_start_event_id"] -= 1
    elif fault == "source_return":
        next(r for r in events if r["kind"] == "device.command" and r["payload"].get("device") == "transfer_stage")["payload"]["params"]["pose"]["x"] += .1
    elif fault in ("reaim_axis", "reaim_target"):
        row = next(r for r in events if discovery["reaim_start_event_id"] < r["id"] and r["kind"] == "device.command")
        row["payload"]["params"]["axis" if fault == "reaim_axis" else "value"] = "z" if fault == "reaim_axis" else 41.
    elif fault == "raw_readback":
        next(r for r in events if r["id"] == discovery["reaim_end_event_id"])["payload"]["geometry"]["microscope"]["stamp_axes_xyz_mm"]["y"] += .1
    elif fault == "candidate_z":
        case["placement"]["candidate_z_mm"] -= .01
    elif fault == "source_focus":
        discovery["focus_reference"] = deepcopy(discovery["original_reference"])
    else:
        events.pop(discovery["start_event_id"])
    rebind_ready(case)
    with pytest.raises(ValueError):
        validate_ready_receipt(**case)


def test_ready_assertion_binds_the_whole_discovery_by_digest_within_its_bound():
    """a518 seed 2: the full discovery journal is ~120 KB, past the 64 KiB
    assertion bound, so the assertion carries its digest instead."""
    import json

    from qumus2.flows.library.stacking import _discovery_ready_value
    from qumus2.records.lab_geometry import LabEvidence, Source

    case = ready_journal()
    discovery = {k: v for k, v in case["discovery"].items() if k != "ready_evidence"}
    padded = {**discovery, "maps": discovery["maps"]*40}
    assert len(json.dumps(padded)) > 65536
    value = _discovery_ready_value(padded)
    LabEvidence(subject="stamp", property="stamp_discovery_ready", bench="sim", value=value, units="mm,s",
                frame="registered_microscope_axes", observed_at=1., timebase="store_clock",
                source=Source(kind="synthetic", reference="r:restore", sha256=value["discovery_sha256"]))
    assert value == {"discovery_sha256": _digest(padded), "ready_placement": discovery["ready_placement"]}
    validate_ready_receipt(**case)
    case["discovery"]["restore_before"] = {**case["discovery"]["restore_before"], "x": 0.}
    with pytest.raises(ValueError, match="differs from the original verified discovery assertion"):
        validate_ready_receipt(**case)


def _restore_repeats_original_bits(case):
    discovery = case["discovery"]
    original, restored = discovery["original_reference"], discovery["restored_reference"]
    binding = restored["focus_binding"]
    row = next(r for r in case["events"] if r["id"] == binding["capture_event_id"])
    row["payload"]["microscope_preview"]["image_sha256"] = original["sha256"]
    binding["capture_event_sha256"] = _digest(row["payload"])
    restored["sha256"] = original["sha256"]
    anchor = discovery["ready_placement"]["source_anchor"]
    anchor.update(image_sha256=restored["sha256"], focus_binding_sha256=_digest(binding))
    return row


@pytest.mark.parametrize("fault", [None, "same_file", "no_capture_command"])
def test_precise_stage_restore_may_repeat_the_original_exposure_bits(fault):
    """a518 seed 2 with a zero-error stage: the restore returns to the exact
    original pose and the deterministic SIM image equals the original bit for
    bit. It is still its own exposure; a reused file or a capture without its
    own microscope command is not."""
    case = ready_journal()
    discovery = case["discovery"]
    row = _restore_repeats_original_bits(case)
    if fault == "same_file":
        image = discovery["original_reference"]["image"]
        discovery["restored_reference"]["image"] = image
        row["payload"]["microscope_preview"]["image"] = image
        discovery["restored_reference"]["focus_binding"]["capture_event_sha256"] = _digest(row["payload"])
        discovery["ready_placement"]["source_anchor"]["focus_binding_sha256"] = _digest(
            discovery["restored_reference"]["focus_binding"])
    elif fault == "no_capture_command":
        focused = discovery["focus_reference"]["focus_binding"]["capture_event_id"]
        for r in case["events"]:
            if (focused < r["id"] < row["id"] and r["kind"] == "device.command"
                    and r["payload"].get("device") == "microscope" and r["payload"].get("command") == "capture"):
                r["payload"]["command"] = "set_focus"
    rebind_ready(case)
    if fault is None:
        validate_ready_receipt(**case)
    else:
        with pytest.raises(ValueError, match="contexts were conflated"):
            validate_ready_receipt(**case)
