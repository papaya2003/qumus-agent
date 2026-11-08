"""Source-sequence observations, composition and raw recovery admission."""
from __future__ import annotations

import math
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from qumus2.config import load_yaml
from qumus2.core.clock import SimClock
from qumus2.core.errors import NeedsHumanError, StopRequested
from qumus2.devices.base import Frame
from qumus2.flows.context import FlowContext
from qumus2.flows.library.stacking import (
    PROCEDURE_ID,
    RELEASE_POLICY_ID,
    TRANSFER_PROFILE_ID,
    AdjustFirstContact,
    DropDown,
    InitializeStamp,
    ManipulatorTo,
    PickUpFlake,
    StackDeviceFlow,
    StackParams,
    _begin_phase,
    _guarded_z,
)
from qumus2.flows.workflow import WorkflowMismatch
from qumus2.perception.tracking import PixelCalibration, v1_first_contact, v1_pickup_coverage
from qumus2.runtime.jobs import build_run_steps
from qumus2.runtime.recovery import decide
from qumus2.testbench.optics import WHITE_LIGHT_PROFILE
from qumus2.testbench.render import microscope_photometry_profile
from tests.test_workflow_consistency import admitted  # noqa: F401


def test_fixed_reference_largest_admitted_round_region():
    reference = Image.new("RGB", (240, 180), "black")
    current = reference.copy()
    draw = ImageDraw.Draw(current)
    draw.rectangle((10, 10, 150, 25), fill="white")  # bigger, but not near-round
    draw.ellipse((160, 55, 210, 105), fill="white")
    observed = v1_first_contact(reference, current, PixelCalibration(.002, -.002))
    assert observed["found"]
    assert observed["centroid_px"] == pytest.approx([185, 80])
    assert not v1_first_contact(reference, reference, PixelCalibration(.002, -.002))["found"]


def test_coverage_uses_strict_physical_external_edge_margin():
    reference = Image.new("RGB", (220, 220), "black")
    current = reference.copy()
    draw = ImageDraw.Draw(current)
    draw.rectangle((10, 10, 209, 209), fill="white")
    draw.rectangle((100, 100, 119, 119), fill="black")  # external contour ignores this hole
    cal = PixelCalibration(.002, -.003)
    result = v1_pickup_coverage(reference, current, cal, (109.5, 109.5), blur_support_mm=0)
    assert result["covered"] and result["target_inside"]
    assert result["edge_distance_mm"] == pytest.approx(.2)
    equal = v1_pickup_coverage(reference, current, cal, (109.5, 109.5), margin_mm=.2, blur_support_mm=0)
    assert not equal["covered"]
    # Optical coverage contains no transfer/identity/registry conclusion.
    assert "transfer_confirmed" not in result


def bound_params(**updates):
    values = dict(flake_ids=["a", "b"], target_chip_id="target", stamp_id="stamp",
        stamp_slot="right_stamp_1", target_xy_mm=(5, 5), flake_chips={"a": "ca", "b": "cb"},
        materials={"a": "hBN", "b": "Gr"}, home_slots={"ca": "ca_home", "cb": "cb_home", "target": "t_home"},
        procedure_contract=PROCEDURE_ID, transfer_profile=TRANSFER_PROFILE_ID, release_policy=RELEASE_POLICY_ID)
    return StackParams(**{**values, **updates})


def test_composition_has_one_first_contact_and_no_target_alignment():
    steps = StackDeviceFlow().build_steps(bound_params())
    assert sum(isinstance(s, AdjustFirstContact) for s in steps) == 1
    assert sum(isinstance(s, InitializeStamp) for s in steps) == 3
    assert sum(isinstance(s, PickUpFlake) for s in steps) == 2
    release = next(i for i, s in enumerate(steps) if isinstance(s, DropDown))
    assert isinstance(steps[release-1], InitializeStamp)
    assert not any("align_stamp" in s.name for s in steps)
    assert all(s.design_shift_mm == (0., 0.) for s in steps if hasattr(s, "design_shift_mm"))


@pytest.mark.parametrize("binding", [{}, {"procedure_contract": "old"},
    {"procedure_contract": PROCEDURE_ID, "transfer_profile": "old"}])
@pytest.mark.parametrize("flow", ["stack_device", "prepare_reactor_pair", "prepare_material_metal"])
def test_raw_missing_old_binding_refused_before_defaults_or_context(binding, flow):
    run = {"id": "old", "flow": flow, "params": binding,
           "meta": {}, "project_id": None, "error": None}
    with pytest.raises(WorkflowMismatch, match="binding"):
        build_run_steps(run, {})
    store = SimpleNamespace(get_steps=lambda _: [])
    def forbidden_context(*args):
        raise AssertionError("old transfer must hold before context/probe/restore")
    decision = decide(store, forbidden_context, run)
    assert decision.action == "park" and decision.workflow_error


def test_unrelated_legacy_flow_keeps_no_in_progress_fast_path():
    run = {"id": "old", "flow": "inspect_bench", "params": {},
           "meta": {}, "project_id": None, "error": None}
    store = SimpleNamespace(get_steps=lambda _: [])
    decision = decide(store, lambda *args: pytest.fail("no context needed"), run)
    assert decision.action == "resume"


@pytest.fixture
def motion_case(tmp_path):
    """A duration-declaring device fake; the production motion/capture code runs."""
    clock = SimClock()
    metadata = {}
    params = {"procedure_contract": PROCEDURE_ID, "transfer_profile": TRANSFER_PROFILE_ID,
              "release_policy": RELEASE_POLICY_ID}
    events = []
    def emit(kind, payload=None, **kwargs):
        events.append(SimpleNamespace(id=len(events)+1, kind=kind,
            payload=deepcopy(payload or {}), run_id=kwargs.get("run_id")))
        return events[-1].id
    store = SimpleNamespace(bench="sim", get_run=lambda _: {"meta": metadata},
        update_run_meta=lambda _, **fields: metadata.update(deepcopy(fields)), emit=emit,
        _transaction=nullcontext,
        last_event_id=lambda: len(events), events_after=lambda after, limit=500, run_id=None:
            [e for e in events if e.id > after and (run_id is None or e.run_id == run_id)][:limit])
    stage_state = {"sample_pose": {"x": 0., "y": 80., "z": 0., "rotation": 0.}}
    stage = SimpleNamespace(kinematics=lambda: deepcopy(stage_state), motion_active=False)
    moves = []
    class Scope:
        def __init__(self):
            self.focus, self.objective, self.profile = .8, 5, WHITE_LIGHT_PROFILE
            self.frames = []
            self.photometry = None

        def get_focus(self):
            return self.focus

        def get_objective(self):
            return self.objective

        def set_focus(self, z):
            self.focus = z

        def set_objective(self, objective):
            self.objective = objective

        def focus_transition_duration_bound(self, start, target):
            return abs(target-start)

        def capture(self):
            path = tmp_path/f"capture-{len(self.frames)}.png"
            Image.new("RGB", (480, 320), (120, 130, 140)).save(path)
            calibration = {"id": "unit-synthetic-optics", "version": "1", "source": "synthetic",
                "frame": "microscope_image", "image_size_px": [480, 320],
                "mm_per_pixel": [.002, .002], "u_axis": "+world_x", "v_axis": "-world_y",
                "photometry_profile": (self.photometry if self.photometry is not None else
                                       {**microscope_photometry_profile(), "fringes": self.profile})}
            frame = Frame("microscope", clock.now(), path=str(path), meta={
                "focus_z": self.focus, "objective": self.objective, "focus_plane": "chip",
                "fov_mm": [.96, .64], "calibration": calibration,
                "stage_kinematics": deepcopy(stage_state), "optical_gap": {"has_contact": True}})
            self.frames.append(frame)
            return frame

    class Manipulator:
        def __init__(self):
            self.positions = {"x": 40., "y": 90., "z": 6.5}
            self.duration_known, self.extra_duration, self.offset = True, 0., 0.
            self.reject = False

        def get_positions(self):
            return dict(self.positions)

        def axis_motion_duration_bound(self, axis, start, target, speed=None):
            return abs(target-start)/(speed or 2.) if self.duration_known else None

        def move_axis_to(self, axis, value, speed=None):
            phase = next(p for p in metadata["normal_transfer"]["phases"].values() if p.get("pending_intent"))
            assert phase["pending_intent"]["to_mm" if axis == "z" else "requested_mm"] == value
            moves.append((axis, dict(self.positions), value, deepcopy(phase)))
            if self.reject:
                raise NeedsHumanError("command rejected")
            clock.sleep(abs(value-self.positions[axis])/(speed or 2.)+self.extra_duration)
            self.positions[axis] = value+self.offset

    scope, manip = Scope(), Manipulator()
    manip.motion_active = False
    gate = SimpleNamespace(microscope=lambda _: scope, manipulator=lambda _: manip,
        stage=lambda _: stage, manipulator_z_floor=lambda _: 4.7, watchdog_tick=lambda: None,
        limits=SimpleNamespace(manipulators={"transfer_arm": SimpleNamespace(z=(4.7, 12.))}))
    ctx = FlowContext("unit", store, gate, clock, None,
        {"transfer_arm": {"safe_z": 6.5, "over_stage": {"x": 40., "y": 90.},
                          "park": {"x": 150., "y": 10.}}},
        load_yaml("verification.yaml"), params=params)
    return SimpleNamespace(ctx=ctx, manip=manip, scope=scope, moves=moves, metadata=metadata)


def test_large_intent_is_observed_subsegments_with_durable_actual_readbacks(motion_case):
    case = motion_case
    phase = _begin_phase(case.ctx, "test_stroke")
    _guarded_z(case.ctx, phase, 6.4, speed=.005)
    assert len(case.moves) > 4
    assert case.manip.positions == pytest.approx({"x": 40., "y": 90., "z": 6.4})
    assert all(axis == "z" and abs(target-before[axis]) <= .025 for axis, before, target, _ in case.moves)
    assert all(move[3]["pending_intent"]["duration_bound_s"] <= 1.95+1e-9 for move in case.moves)
    assert phase["observations"] == len(case.moves)+1
    assert phase["travel_mm"] == pytest.approx(.1)
    assert not phase.get("pending_intent")


@pytest.fixture
def tail_case(motion_case, monkeypatch):
    """Real journal transactions, Step, raster coverage and segmented motion."""
    import qumus2.flows.library.stacking as module
    from qumus2.runtime.store import RunStore
    case = motion_case
    store = RunStore(":memory:", clock=case.ctx.clock, bench="sim")
    case.ctx.params = bound_params().model_dump(mode="json")
    case.ctx.run_id = store.create_run("stack_device", case.ctx.params)
    case.ctx.store = store
    update = store.update_run_meta
    def save(run_id, **fields):
        update(run_id, **fields)
        case.metadata.clear()
        case.metadata.update(store.get_run(run_id)["meta"])
    monkeypatch.setattr(store, "update_run_meta", save)
    case.show_coverage = False
    case.minimum = 6.42
    monkeypatch.setattr(module, "_process_reference", lambda *a, **kw: {
        "profile": TRANSFER_PROFILE_ID, "minimum_z_mm": case.minimum})
    # Native AF/source qualification has separate tests; this fixture exercises
    # the actual fixed-reference coverage raster and the existing motion helper.
    monkeypatch.setattr(module, "autofocus", lambda *a, **kw: None)
    capture = case.scope.capture
    def pixels():
        frame = capture()
        if case.manip.positions["z"] < 6.5:
            image = Image.open(frame.path).copy()
            radius = 120 if case.show_coverage and case.manip.positions["z"] <= 6.46 else 40
            ImageDraw.Draw(image).ellipse((240-radius, 160-radius, 240+radius, 160+radius), fill=(230, 230, 230))
            image.save(frame.path)
        return frame
    monkeypatch.setattr(case.scope, "capture", pixels)
    case.step = PickUpFlake("a", "stamp", "hBN")
    store.begin_step(case.ctx.run_id, 0, case.step.name, safe_to_resume=False)
    yield case
    store.close()


def _tail_phase(case):
    return case.ctx.store.get_run(case.ctx.run_id)["meta"]["normal_transfer"]["phases"][case.step.name]


def test_final_prefix_no_coverage_is_consumed_once_and_never_uses_original_target(tail_case):
    case = tail_case
    with pytest.raises(NeedsHumanError, match="one final"):
        case.step.act(case.ctx, None)
    phase = _tail_phase(case)
    receipt = phase["final_partial_macro"]
    body = receipt["admission"]
    target = math.nextafter(case.minimum+.005, math.inf)
    assert body["requested_target_mm"] == 6.4 and body["admitted_target_mm"] == target
    assert receipt["status"] == "no_coverage" and phase["held_reason"] == "pickup_final_partial_exhausted"
    assert phase["last_pose"]["z"] == target and target-.005 >= case.minimum
    events = case.ctx.store.events_after(0, limit=1000, run_id=case.ctx.run_id)
    assert len([e for e in events if e.kind == "stack.pickup_macro_admission"]) == 1
    assert receipt["event_id"] > body["capture_event"]["event_id"]
    for axis, before, end, saved in case.moves:
        intent = saved["pending_intent"]
        assert axis == "z" and target <= end < before["z"]
        assert before["z"]-end <= .02
        assert intent["macro_target_mm"] == target
        assert intent["requested_macro_target_mm"] == 6.4
        assert intent["macro_admission_event_id"] == receipt["event_id"]
        assert saved["final_partial_macro"]["status"] == "consumed"
    count = len(case.moves)
    # Erasing the mutable reservation cannot erase the actual admission event.
    state = case.ctx.store.get_run(case.ctx.run_id)["meta"]["normal_transfer"]
    del state["phases"][case.step.name]["final_partial_macro"]
    case.ctx.store.update_run_meta(case.ctx.run_id, normal_transfer=state)
    with pytest.raises(NeedsHumanError, match="cannot be replayed"):
        PickUpFlake("a", "stamp", "hBN").act(case.ctx, None)
    assert len(case.moves) == count


def test_final_prefix_coverage_stops_early_and_source_presence_still_holds(tail_case, monkeypatch):
    import qumus2.flows.library.stacking as module
    case = tail_case
    case.show_coverage = True
    case.step.act(case.ctx, None)
    phase = _tail_phase(case)
    assert phase["complete"] and phase["coverage"]["covered"]
    receipt = phase["final_partial_macro"]
    assert receipt["status"] == "covered"
    descents = [move for move in case.moves if "macro_admission_event_id" in move[3]["pending_intent"]]
    assert descents[-1][2] > receipt["admission"]["admitted_target_mm"]
    assert all(move[2] >= case.minimum for move in case.moves)
    assert any("macro_admission_event_id" not in move[3]["pending_intent"] for move in case.moves)  # existing lifts
    state = case.ctx.store.get_run(case.ctx.run_id)["meta"]["normal_transfer"]
    state["source_references"] = {"a": phase["reference"]}
    case.ctx.store.update_run_meta(case.ctx.run_id, normal_transfer=state)
    monkeypatch.setattr(module, "_chip_near_centre", lambda *a, **kw: [object()])
    observed = case.step.verify(case.ctx)
    assert observed.verdict == "unclear" and not observed.measurements["process_confirmation"]
    with pytest.raises(NeedsHumanError, match="cannot commit"):
        case.step.on_success(case.ctx, observed)
    assert case.ctx.store.get_sample("a") is None


def test_full_pickup_macro_retains_historical_target_without_prefix_fields(tail_case):
    case = tail_case
    case.minimum, case.show_coverage = 6.2, True
    case.step.act(case.ctx, None)
    assert "final_partial_macro" not in _tail_phase(case)
    assert case.moves[0][3]["pending_intent"]["macro_target_mm"] == 6.4
    assert all("requested_macro_target_mm" not in move[3]["pending_intent"] for move in case.moves)


def test_tail_exposure_tags_survive_actual_guarded_lift_and_return(tail_case):
    """Read real command/readback/capture journal rows after last_intent changes."""
    import hashlib
    from pathlib import Path

    from qumus2.core.types import Pose
    from qumus2.devices.base import Arm, DeviceBundle, StampManipulator
    from qumus2.safety.gate import SafetyGate
    from qumus2.safety.limits import Limits
    case = tail_case
    class ClearArm(Arm):
        def move_to(self, pose, speed=None):
            pytest.fail("No arm motion in this isolated test")
        def get_pose(self):
            return Pose(x=0, y=0, z=120)
        def home(self):
            pytest.fail("No arm homing")
        def set_vacuum(self, on):
            pytest.fail("No vacuum change")
        def vacuum_on(self):
            return False
    class Adapter(StampManipulator):
        is_simulated = True
        def get_positions(self):
            return case.manip.get_positions()
        def axis_motion_duration_bound(self, axis, start, target, speed=None):
            return case.manip.axis_motion_duration_bound(axis, start, target, speed)
        def move_axis_to(self, axis, value, speed=None):
            case.manip.move_axis_to(axis, value, speed)
    devices = DeviceBundle()
    adapter = Adapter("transfer_arm")
    devices.add(adapter)
    devices.add(ClearArm("arm_right"))
    gate = SafetyGate(devices, Limits.load(), case.ctx.clock,
        sink=lambda kind, payload: case.ctx.store.emit(kind, payload, run_id=case.ctx.run_id),
        frames=lambda: {"manipulator": adapter.get_positions()})
    case.ctx.gate.manipulator = gate.manipulator
    case.ctx.gate.manipulator_z_floor = gate.manipulator_z_floor
    case.show_coverage = True
    case.step.act(case.ctx, None)
    phase = _tail_phase(case)
    assert phase["complete"] and phase["last_pose"]["z"] == pytest.approx(6.5, abs=1e-8)
    assert "macro_admission_event_id" not in phase["last_intent"]  # overwritten by ordinary return
    events = case.ctx.store.events_after(0, limit=10000, run_id=case.ctx.run_id)
    captures = [e for e in events if "microscope_preview" in e.payload]
    tagged = [e for e in captures if "transfer_motion_intent" in e.payload["microscope_preview"]]
    moves = [move for move in case.moves if "macro_admission_event_id" in move[3]["pending_intent"]]
    assert len(tagged) == len(moves) > 1
    assert len(captures) == phase["observations"] == len(case.moves)+2  # original + post-coverage; no extra exposure
    for capture, move in zip(tagged, moves, strict=True):
        preview = capture.payload["microscope_preview"]
        intent = preview["transfer_motion_intent"]
        assert intent == move[3]["pending_intent"]
        command = next(e for e in reversed(events) if e.id < capture.id and e.kind == "device.command")
        readback = next(e for e in events if command.id < e.id < capture.id
                        and e.kind == "lab.frame" and "manipulator" in e.payload)
        assert intent["macro_admission_event_id"] < command.id < readback.id < capture.id
        assert command.payload["command"] == "move_axis_to"
        assert command.payload["params"] == {"axis": intent["axis"], "value": intent["to_mm"], "speed": intent["speed_mm_s"],
            "from_pose": move[1],
            "duration_bound_s": intent["duration_bound_s"]}
        assert readback.payload["manipulator"]["z"] == intent["to_mm"]
        assert command.ts == intent["at"] <= readback.ts <= capture.ts
        assert hashlib.sha256(Path(preview["image"]).read_bytes()).hexdigest() == preview["image_sha256"]
    assert all("transfer_motion_intent" not in e.payload["microscope_preview"]
               for e in captures if e.id > tagged[-1].id)  # post-focus, lift and return remain unchanged
    assert captures[-1].id > tagged[-1].id
    preserved = [e.payload["microscope_preview"]["transfer_motion_intent"] for e in tagged]
    state = case.ctx.store.get_run(case.ctx.run_id)["meta"]["normal_transfer"]
    state["phases"][case.step.name]["last_intent"].clear()
    case.ctx.store.update_run_meta(case.ctx.run_id, normal_transfer=state)
    reread = case.ctx.store.events_after(0, limit=10000, run_id=case.ctx.run_id)
    assert [e.payload["microscope_preview"]["transfer_motion_intent"] for e in reread
            if "transfer_motion_intent" in e.payload.get("microscope_preview", {})] == preserved


def test_tail_exposure_refuses_a_local_intent_that_is_not_the_saved_intent(tail_case):
    import qumus2.flows.library.stacking as module
    case = tail_case
    phase = _begin_phase(case.ctx, case.step.name)
    phase["pending_intent"] = {"macro_admission_event_id": 1, "to_mm": 6.425}
    module._save_phase(case.ctx, phase)
    state = case.ctx.store.get_run(case.ctx.run_id)["meta"]["normal_transfer"]
    state["phases"][case.step.name]["pending_intent"]["to_mm"] = 6.424
    case.ctx.store.update_run_meta(case.ctx.run_id, normal_transfer=state)
    before = len(case.scope.frames)
    with pytest.raises(NeedsHumanError, match="saved pre-command"):
        module._phase_capture(case.ctx, phase)
    assert len(case.scope.frames) == before and not case.moves


@pytest.mark.parametrize("fault", ["stop", "reference", "focus", "duration", "time", "travel", "observations",
                                   "readback", "below_minimum", "late", "rejected", "capture"])
def test_final_prefix_keeps_stop_evidence_and_each_segment_guard(tail_case, monkeypatch, fault):
    case = tail_case
    emit = case.ctx.store.emit
    def reserve(kind, payload=None, **kw):
        event_id = emit(kind, payload, **kw)
        if kind == "stack.pickup_macro_admission":
            if fault == "stop":
                case.ctx.cancel.set()
            elif fault == "reference":
                Image.new("RGB", (480, 320), "black").save(_tail_phase(case)["reference"]["image"])
            elif fault == "focus":
                case.scope.focus += .1
            elif fault == "duration":
                case.manip.duration_known = False
            elif fault == "time":
                case.ctx.clock.sleep(901)
            elif fault == "travel":
                case.ctx.verification["transfer_sequence"]["max_travel_mm"] = 0
            elif fault == "observations":
                case.ctx.verification["transfer_sequence"]["max_observations"] = 1
            elif fault in ("readback", "below_minimum"):
                case.manip.offset = -.006 if fault == "readback" else -.1
            elif fault == "late":
                case.manip.extra_duration = 3
            elif fault == "rejected":
                case.manip.reject = True
            else:
                case.scope.profile = "unsupported"
        return event_id
    monkeypatch.setattr(case.ctx.store, "emit", reserve)
    with pytest.raises((NeedsHumanError, StopRequested, ValueError)):
        case.step.act(case.ctx, None)
    phase = _tail_phase(case)
    assert phase["final_partial_macro"]["status"] == ("stopped" if fault == "stop" else "failed")
    assert len(case.moves) <= 1
    count = len(case.moves)
    with pytest.raises(NeedsHumanError, match="cannot be replayed"):
        case.step.act(case.ctx, None)
    assert len(case.moves) == count


@pytest.mark.parametrize("fault", ["at_margin", "at_minimum", "below_minimum", "illegal_original", "raised_floor", "zero_tol", "nan_minimum"])
def test_final_prefix_never_clamps_or_creates_a_boundary_micro_retry(tail_case, fault):
    import qumus2.flows.library.stacking as module
    case = tail_case
    if fault == "at_margin":
        case.manip.positions["z"] = case.minimum+.005
    elif fault == "at_minimum":
        case.manip.positions["z"] = case.minimum
    elif fault == "below_minimum":
        case.manip.positions["z"] = case.minimum-.001
    elif fault == "illegal_original":
        case.minimum, case.manip.positions["z"] = 4.75, 4.76
    elif fault == "raised_floor":
        case.ctx.gate.manipulator_z_floor = lambda _: 6.41  # R invalid while the proposed prefix would fit
    elif fault == "zero_tol":
        case.ctx.verification["manipulator"]["tolerance_mm"] = 0
    else:
        case.minimum = float("nan")
    with pytest.raises(NeedsHumanError):
        case.step.act(case.ctx, None)
    assert not case.moves
    assert not list(module._transfer_events(case.ctx.store, case.ctx.run_id, "stack.pickup_macro_admission"))


@pytest.mark.parametrize("fault", ["stop", "uncertain_capture"])
def test_final_prefix_boundary_stop_or_uncertainty_cannot_start_another_macro(tail_case, monkeypatch, fault):
    case = tail_case
    target = math.nextafter(case.minimum+.005, math.inf)
    move = case.manip.move_axis_to
    def boundary(axis, value, speed=None):
        move(axis, value, speed)
        if value == target:
            if fault == "stop":
                case.ctx.cancel.set()
            else:
                case.scope.profile = "unsupported"
    monkeypatch.setattr(case.manip, "move_axis_to", boundary)
    with pytest.raises((StopRequested, NeedsHumanError)):
        case.step.act(case.ctx, None)
    phase = _tail_phase(case)
    assert phase["pending_intent"]["to_mm"] == target
    assert case.manip.positions["z"] == target >= case.minimum
    assert phase["final_partial_macro"]["status"] == ("stopped" if fault == "stop" else "failed")
    count = len(case.moves)
    with pytest.raises(NeedsHumanError, match="cannot be replayed"):
        case.step.act(case.ctx, None)
    assert len(case.moves) == count


def test_final_prefix_reservation_and_event_roll_back_together_before_commands(tail_case, monkeypatch):
    case = tail_case
    update = case.ctx.store.update_run_meta
    def unavailable(run_id, **fields):
        phase = fields.get("normal_transfer", {}).get("phases", {}).get(case.step.name, {})
        if "final_partial_macro" in phase:
            raise RuntimeError("storage unavailable before reservation commit")
        update(run_id, **fields)
    monkeypatch.setattr(case.ctx.store, "update_run_meta", unavailable)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        case.step.act(case.ctx, None)
    assert not case.moves and "final_partial_macro" not in _tail_phase(case)
    assert not any(e.kind == "stack.pickup_macro_admission" for e in case.ctx.store.events_after(0, limit=1000))


@pytest.mark.parametrize("fault", ["intact", "deleted_record", "deleted_phase", "deleted_event", "changed_request", "late_event"])
def test_saved_incomplete_prefix_refuses_before_any_context_or_future_proof(tail_case, fault):
    case = tail_case
    if fault == "late_event":
        for _ in range(600):
            case.ctx.store.emit("diagnostic.public", run_id=case.ctx.run_id)
    case.manip.duration_known = False  # reserve, then stop before the first command/second observation
    with pytest.raises(NeedsHumanError):
        case.step.act(case.ctx, None)
    state = case.ctx.store.get_run(case.ctx.run_id)["meta"]["normal_transfer"]
    phase = state["phases"][case.step.name]
    assert phase["observations"] == 1 and not case.moves
    assert "endpoint" not in phase
    if fault == "deleted_record":
        del phase["final_partial_macro"]
    elif fault == "deleted_phase":
        state["phases"] = {}
    elif fault == "deleted_event":
        case.ctx.store._x("DELETE FROM event WHERE kind='stack.pickup_macro_admission'")
    elif fault == "changed_request":
        phase["final_partial_macro"]["admission"]["requested_target_mm"] -= .01
    case.ctx.store.update_run_meta(case.ctx.run_id, normal_transfer=state, held_heaters={"stamp_heater": 170.})
    run = case.ctx.store.get_run(case.ctx.run_id)
    with pytest.raises(WorkflowMismatch) as caught:
        build_run_steps(run, case.ctx.bench, store=case.ctx.store)
    assert "prefix" in str(caught.value.__cause__).lower()
    decision = decide(case.ctx.store, lambda *args: pytest.fail("prefix refusal must precede context/restore"), run)
    assert decision.action == "park" and decision.workflow_error


def test_completed_prefix_still_needs_future_endpoint_binding(tail_case):
    case = tail_case
    case.show_coverage = True
    case.step.act(case.ctx, None)
    case.ctx.store.finish_step(case.ctx.run_id, 0, "done")
    run = case.ctx.store.get_run(case.ctx.run_id)
    assert "endpoint_verified" not in _tail_phase(case)
    with pytest.raises(WorkflowMismatch) as caught:
        build_run_steps(run, case.ctx.bench, store=case.ctx.store)
    assert str(caught.value.__cause__) == "'endpoint_verified'"


@pytest.mark.parametrize("fault", ["request", "target", "digest", "duplicate", "missing", "capture", "source_image"])
def test_completed_prefix_receipt_requires_its_real_historical_binding(tail_case, fault):
    import qumus2.flows.library.stacking as module
    case = tail_case
    case.show_coverage = True
    case.step.act(case.ctx, None)
    phase = _tail_phase(case)
    events = list(module._transfer_events(case.ctx.store, case.ctx.run_id, "stack.pickup_macro_admission"))
    assert module._pickup_partial_receipt(case.ctx.store, case.ctx.run_id, phase, events)
    record = phase["final_partial_macro"]
    if fault in ("request", "target"):
        record["admission"]["requested_target_mm" if fault == "request" else "admitted_target_mm"] -= .01
    elif fault == "digest":
        record["event_sha256"] = "0"*64
    elif fault == "duplicate":
        case.ctx.store.emit("stack.pickup_macro_admission", record["admission"], run_id=case.ctx.run_id)
    elif fault == "missing":
        del phase["final_partial_macro"]
    elif fault == "capture":
        record["admission"]["capture_event"]["event_id"] += 1
    else:
        Image.new("RGB", (480, 320), "black").save(phase["reference"]["image"])
    events = list(module._transfer_events(case.ctx.store, case.ctx.run_id, "stack.pickup_macro_admission"))
    with pytest.raises((ValueError, NeedsHumanError)):
        module._pickup_partial_receipt(case.ctx.store, case.ctx.run_id, phase, events)


@pytest.mark.parametrize("failure", ["duration", "late", "readback", "rejected"])
def test_failed_admission_or_segment_cannot_authorize_another_phase(motion_case, failure):
    case = motion_case
    phase = _begin_phase(case.ctx, "test_stroke")
    if failure == "duration":
        case.manip.duration_known = False
    elif failure == "late":
        case.manip.extra_duration = 3.
    elif failure == "readback":
        case.manip.offset = .02
    else:
        case.manip.reject = True
    with pytest.raises(NeedsHumanError):
        _guarded_z(case.ctx, phase, 6.4, speed=.005)
    if failure == "duration":
        assert not case.moves
    else:
        assert len(case.moves) == 1
        with pytest.raises(NeedsHumanError, match="Pending transfer intent"):
            _begin_phase(case.ctx, "new_name_cannot_bypass_pending")


@pytest.mark.parametrize("failure", ["reference", "profile", "focus", "stop", "floor", "budget"])
def test_fixed_reference_and_safety_refuse_before_motion(motion_case, failure):
    case = motion_case
    phase = _begin_phase(case.ctx, "test_stroke")
    target = 6.4
    if failure == "reference":
        Image.new("RGB", (480, 320), "black").save(phase["reference"]["image"])
    elif failure == "profile":
        case.scope.profile = "different-profile"
        case.ctx.clock.sleep(2)
    elif failure == "focus":
        case.scope.focus += .1
        case.ctx.clock.sleep(2)
    elif failure == "stop":
        case.ctx.cancel.set()
    elif failure == "floor":
        target = 4.6
    else:
        case.ctx.verification["transfer_sequence"]["max_observations"] = 1
    with pytest.raises((NeedsHumanError, StopRequested)):
        _guarded_z(case.ctx, phase, target)
    assert not case.moves


def test_park_does_not_erase_working_xy_or_grant_it_to_another_stamp(motion_case):
    case = motion_case
    case.metadata["normal_transfer"] = {"placements": {"stamp": {"stamp_id": "stamp",
        "run_id": "unit", "profile": TRANSFER_PROFILE_ID, "working_xy_mm": [40.12, 89.7]}}}
    working = ManipulatorTo("over_stage", stamp_id="stamp")._target(case.ctx)
    assert working == {"x": 40.12, "y": 89.7, "z": 6.5}
    assert ManipulatorTo("park")._target(case.ctx) == {"x": 150., "y": 10., "z": 6.5}
    assert ManipulatorTo("over_stage", stamp_id="stamp")._target(case.ctx) == working
    assert ManipulatorTo("over_stage", stamp_id="new_stamp")._target(case.ctx)["x"] == 40.


@pytest.mark.parametrize("entry", ["retry", "reverify", "resume", "recover", "worker"])
@pytest.mark.parametrize("flow", ["stack_device", "prepare_reactor_pair", "prepare_material_metal"])
def test_actual_service_rejects_old_transfer_before_context(admitted, entry, flow):  # noqa: F811
    h = admitted
    h.store.set_run_status(h.rid, "succeeded")
    rid = h.store.create_run(flow, {})
    h.store.set_run_status(rid, "paused" if entry == "resume" else
                           "queued" if entry in ("recover", "worker") else "parked")
    h.service.context_factory = lambda *args: pytest.fail("raw transfer rejection must precede process context")
    if entry == "worker":
        h.service.enqueue(rid)
        h.service._spawn(99)
        assert h.service._done[rid].wait(1)
    else:
        getattr(h.service, entry)(rid) if entry != "recover" else h.service.recover()
    run = h.store.get_run(rid)
    assert run["status"] == "waiting_human"
    assert run["error"]["reason"] == "transfer_procedure_changed"
    assert not h.trace


@pytest.mark.parametrize("key", ["source", "contact", "carried_hbn", "raster", "fringes"])
@pytest.mark.parametrize("change", ["missing", "old"])
def test_incomplete_or_unsupported_full_profile_refuses_initial_admission(motion_case, key, change):
    case = motion_case
    case.scope.photometry = microscope_photometry_profile()
    if change == "missing":
        case.scope.photometry.pop(key)
    else:
        case.scope.photometry[key] = "old-profile"
    with pytest.raises(NeedsHumanError, match="complete supported capture"):
        _begin_phase(case.ctx, "test_stroke")
    assert not case.moves


def test_initialize_selects_and_verifies_5x_before_engagement_and_never_accumulates_focus(motion_case, monkeypatch):
    case = motion_case
    trace = []
    case.scope.objective = 20
    case.ctx.gate.limits.microscopes = {"microscope": SimpleNamespace(focus=(-3., 3.))}
    case.scope.set_objective = lambda value: (trace.append(("objective", value)), setattr(case.scope, "objective", value))
    case.scope.set_focus = lambda value: (trace.append(("focus", value)), setattr(case.scope, "focus", value))
    def engage(step, ctx, correction):
        assert case.scope.objective == 5
        trace.append(("engage",))
    monkeypatch.setattr(ManipulatorTo, "act", engage)
    monkeypatch.setattr(ManipulatorTo, "verify", lambda *args: SimpleNamespace(verdict="ok"))
    step = InitializeStamp("stamp", "initialize")
    step.act(case.ctx, None)
    assert trace[:2] == [("objective", 5), ("engage",)]
    assert case.scope.focus == pytest.approx(1.1)
    step.act(case.ctx, None)
    assert case.scope.focus == pytest.approx(1.1)
    assert case.metadata["normal_transfer"]["initializations"]["initialize"]["substrate_focus_mm"] == .8


@pytest.mark.parametrize("film,entry,release", [("PC", 155., 170.), ("mica", 150., 165.)])
def test_release_orders_measured_thresholds_preset_clear_withdrawal_and_off(motion_case, monkeypatch, film, entry, release):
    import qumus2.flows.library.stacking as module
    case = motion_case
    trace = []
    class Heater:
        target = None
        temperature = 90.
        def read_temperature(self):
            return self.temperature
        def get_target(self):
            return self.target
        def off(self):
            trace.append(("off", case.manip.positions["z"]))
            self.target = None
    heater = Heater()
    case.ctx.gate.heater = lambda _: heater
    case.ctx.params.update(stamp_film=film, drop_temp_c=entry,
                           support_assumptions={"source": "synthetic"} if film == "mica" else {})
    def ramp(ctx, device, target, *args, **kwargs):
        trace.append(("temperature", target, case.manip.positions["z"]))
        heater.target = heater.temperature = target
    def wait(ctx, device, predicate, **kwargs):
        assert predicate(heater.temperature)
        if film == "PC" and kwargs["what"] == ">170 C PC release":
            assert not predicate(170.)
        trace.append(("threshold", kwargs["what"], case.manip.positions["z"]))
        return heater.temperature, 0.
    monkeypatch.setattr(module, "ramp_heater", ramp)
    monkeypatch.setattr(module, "wait_temperature", wait)
    # Native target admission is tested separately; this case isolates thermal/motion order.
    monkeypatch.setattr(module, "_target_baseline", lambda state, *args, **kwargs: state["target_reference"])
    case.metadata["normal_transfer"] = {"target_reference": {
        "target_chip_id": "target", "target_xy_mm": [5., 5.], "focus_reference": {},
        "materials_found": {"Gr": 0}, "stage_kinematics": case.ctx.gate.stage("transfer_stage").kinematics()}}
    _isolated_release_reference(case, monkeypatch)
    step = DropDown("stamp", ["a"], {"a": "Gr"}, "target", (5., 5.), release, None)
    step.act(case.ctx, None)
    phase = case.metadata["normal_transfer"]["phases"][step.name]
    assert trace[0] == ("temperature", entry+.5, 6.5)
    assert trace[1][0] == "threshold" and trace[1][2] == 6.5
    assert trace[2][:2] == ("temperature", release+.5)
    assert trace[2][2] == pytest.approx(5.7)
    assert trace[-1] == ("off", 6.5)
    assert phase["outputs_off"] and phase["complete"]
    assert phase["withdrawal"]["historical_delta_mm"] == 5.
    assert phase["withdrawal"]["requested_sim_target_mm"] == 6.5
    assert max(move[2] for move in case.moves) <= 6.5
    assert phase["withdrawal"]["actual_readback"]["z"] == pytest.approx(6.5)


def _isolated_release_reference(case, monkeypatch):
    """Thermal order fixture only; event-bound reference math has its own tests."""
    import qumus2.flows.library.stacking as module
    case.metadata["normal_transfer"]["target_reference"]["focus_reference"] = {
        "meta": {"focus_z": case.scope.focus}}
    process = {"profile": TRANSFER_PROFILE_ID, "minimum_z_mm": 5.6, "first_candidate_z_mm": 5.8,
               "max_press_mm": .2, "reference_interval_mm": [5.8, 5.8], "delta_interval_mm": [0., 0.]}
    reference = {"policy": RELEASE_POLICY_ID, "requested_z_mm": 5.7}
    monkeypatch.setattr(module, "_release_reference", lambda *a, **kw: (deepcopy(process), deepcopy(reference)))


def _real_release_heater(case, monkeypatch, film, entry, release):
    """Public first-order heater behind its real Gate; optical/motion fixture only."""
    import qumus2.flows.library.stacking as module
    from qumus2.devices.base import DeviceBundle
    from qumus2.devices.sim.devices import SimHeater
    from qumus2.safety.gate import SafetyGate
    from qumus2.safety.limits import Limits
    heater = SimHeater("stamp_heater", case.ctx.clock, ambient=80. if film == "mica" else 90., tau_s=60.)
    monkeypatch.setattr(heater, "true_temperature", lambda: pytest.fail("Only the public sensor may admit entry"))
    devices, trace = DeviceBundle(), []
    devices.add(heater)
    gate = SafetyGate(devices, Limits.load(), case.ctx.clock, sink=lambda kind, payload: trace.append((kind, payload)))
    case.ctx.gate.heater = gate.heater
    case.ctx.gate.watchdog_tick = gate.watchdog_tick
    case.ctx.gate.limits.heaters = gate.limits.heaters
    case.ctx.params.update(stamp_film=film, drop_temp_c=entry,
                           support_assumptions={"source": "synthetic"} if film == "mica" else {})
    monkeypatch.setattr(module, "_target_baseline", lambda state, *a, **kw: state["target_reference"])
    case.metadata["normal_transfer"] = {"target_reference": {
        "materials_found": {"Gr": 0}, "stage_kinematics": case.ctx.gate.stage("transfer_stage").kinematics()}}
    _isolated_release_reference(case, monkeypatch)
    move = case.manip.move_axis_to
    def measured_motion(axis, value, speed=None):
        trace.append(("preset_motion", {"axis": axis, "target": value, "temperature": gate.heater("stamp_heater").read_temperature()}))
        move(axis, value, speed)
    monkeypatch.setattr(case.manip, "move_axis_to", measured_motion)
    return DropDown("stamp", ["a"], {"a": "Gr"}, "target", (5., 5.), release, None), heater, trace


@pytest.mark.parametrize("film,entry,release", [("mica", 135., 150.), ("PC", 155., 170.)])
def test_release_real_first_order_entry_crosses_exact_threshold_before_preset(motion_case, monkeypatch, film, entry, release):
    case = motion_case
    step, heater, trace = _real_release_heater(case, monkeypatch, film, entry, release)
    step.act(case.ctx, None)  # real ramp_heater, wait_temperature, Clock and Gate
    first_motion = next(i for i, (kind, _) in enumerate(trace) if kind == "preset_motion")
    assert trace[first_motion][1]["temperature"] >= entry
    targets = [(i, p["params"]["celsius"]) for i, (k, p) in enumerate(trace)
               if k == "device.command" and p.get("command") == "set_target"]
    assert any(i < first_motion and target == entry+.5 for i, target in targets)
    assert any(i > first_motion and target == release+.5 for i, target in targets)
    assert step._peak_c >= release-.25 if film == "mica" else step._peak_c > release
    phase = case.metadata["normal_transfer"]["phases"][step.name]
    assert phase["complete"] and phase["outputs_off"] and heater.get_target() is None
    assert phase["observations"] <= 600 and case.ctx.clock.now()-phase["started_at"] < 900
    assert phase["release_budget"]["endpoint_limit"] == 24
    assert phase["release_budget"]["thermal_observations"] > 0
    intervals = [b.timestamp-a.timestamp for a, b in zip(case.scope.frames, case.scope.frames[1:], strict=False)]
    assert max(intervals) <= 2. and max(intervals) > 1.8


@pytest.mark.parametrize("fault", ["observations", "time", "travel", "duration", "focus_duration"])
def test_release_reserves_mandatory_exit_before_heating(motion_case, monkeypatch, fault):
    import qumus2.flows.library.stacking as module
    case = motion_case
    step, heater, trace = _real_release_heater(case, monkeypatch, "mica", 135., 150.)
    guard = module._release_budget_guard
    def scarce(ctx, phase, **kwargs):
        if fault == "observations":
            phase["observations"] = 595
        elif fault == "time":
            phase["started_at"] -= 895
        elif fault == "travel":
            phase["travel_mm"] = 7.99
        elif fault == "duration":
            case.manip.duration_known = False
        else:
            case.scope.focus_transition_duration_bound = lambda *a: None
        return guard(ctx, phase, **kwargs)
    monkeypatch.setattr(module, "_release_budget_guard", scarce)
    with pytest.raises(NeedsHumanError):
        step.act(case.ctx, None)
    assert not case.moves and heater.get_target() is None
    assert not any(k == "device.command" and p.get("command") == "set_target" for k, p in trace)


def test_late_heat_exposure_is_counted_and_refuses_further_motion(motion_case, monkeypatch):
    case = motion_case
    step, heater, _ = _real_release_heater(case, monkeypatch, "mica", 135., 150.)
    capture = case.scope.capture
    def late():
        frame = capture()
        if heater.get_target() is not None:
            case.ctx.clock.sleep(2.1)
        return frame
    monkeypatch.setattr(case.scope, "capture", late)
    with pytest.raises(NeedsHumanError, match="exposure"):
        step.act(case.ctx, None)
    phase = case.metadata["normal_transfer"]["phases"][step.name]
    assert phase["observations"] == len(case.scope.frames) == 2
    assert phase["release_budget"]["thermal_observations"] == 1
    assert phase["held_reason"] == "release_exposure_budget_or_deadline"
    assert not case.moves and heater.get_target() is None


def test_target_relative_release_refuses_dynamic_floor_without_clamping(motion_case, monkeypatch):
    case = motion_case
    step, heater, trace = _real_release_heater(case, monkeypatch, "mica", 135., 150.)
    case.ctx.gate.manipulator_z_floor = lambda _: 5.8
    with pytest.raises(NeedsHumanError, match="descending envelope"):
        step.act(case.ctx, None)
    assert not case.moves and heater.get_target() is None
    assert not any(k == "device.command" and p.get("command") == "set_target" for k, p in trace)


@pytest.mark.parametrize("film,entry,release", [("mica", 135., 150.), ("PC", 155., 170.)])
def test_release_real_stuck_public_sensor_still_turns_off_before_preset(motion_case, monkeypatch, film, entry, release):
    from qumus2.core.errors import DangerousError
    case = motion_case
    step, heater, trace = _real_release_heater(case, monkeypatch, film, entry, release)
    heater.stuck_at = math.nextafter(entry, -math.inf)  # rounds to entry but remains below it
    with pytest.raises(DangerousError, match="reading stuck"):
        step.act(case.ctx, None)
    assert not case.moves and heater.get_target() is None
    assert not any(kind == "preset_motion" for kind, _ in trace)
    targets = [p["params"]["celsius"] for k, p in trace if k == "device.command" and p.get("command") == "set_target"]
    assert targets == [entry+.5]  # no escalation or later melt ramp


def test_real_first_order_heater_exposes_original_equal_target_deadlock(motion_case, monkeypatch):
    from qumus2.core.errors import DangerousError
    from qumus2.flows.library.primitives import ramp_heater, wait_temperature
    case = motion_case
    _, heater, _ = _real_release_heater(case, monkeypatch, "mica", 135., 150.)
    ramp_heater(case.ctx, "stamp_heater", 135.)
    h = case.ctx.verification["heating"]
    with pytest.raises(DangerousError, match="reading stuck"):
        wait_temperature(case.ctx, "stamp_heater", lambda t: t >= 135., what=">=135 C",
            timeout_s=h["timeout_s"], stuck_window_s=h["stuck_window_s"], stuck_delta_c=h["stuck_delta_c"], poll_s=1.)
    assert heater.read_temperature() < 135. and heater.get_target() is None
    assert not case.moves


def test_release_entry_headroom_cannot_bypass_heater_maximum(motion_case, monkeypatch):
    from qumus2.core.errors import SafetyViolation
    case = motion_case
    step, heater, _ = _real_release_heater(case, monkeypatch, "mica", 199.8, 150.)
    with pytest.raises(SafetyViolation, match="max"):
        step.act(case.ctx, None)
    assert not case.moves and heater.get_target() is None


@pytest.mark.parametrize("fault", ["stop", "timeout"])
def test_release_entry_keeps_stop_and_timeout_before_preset(motion_case, monkeypatch, fault):
    case = motion_case
    step, heater, _ = _real_release_heater(case, monkeypatch, "mica", 135., 150.)
    if fault == "stop":
        set_target = heater.set_target
        def stopped(value):
            set_target(value)
            case.ctx.cancel.set()
        monkeypatch.setattr(heater, "set_target", stopped)
    else:
        case.ctx.verification["heating"]["timeout_s"] = 1
    with pytest.raises(StopRequested if fault == "stop" else NeedsHumanError):
        step.act(case.ctx, None)
    assert not case.moves and heater.get_target() is None


def test_once_only_reaim_records_actual_axis_displacements(motion_case):
    case = motion_case
    phase = _begin_phase(case.ctx, "test_stroke")
    case.manip.offset = .002
    actual = AdjustFirstContact("stamp", "first")._reaim(case.ctx, phase, .01, -.03)
    assert [m[0] for m in case.moves] == ["x", "y"]
    assert actual["x"] == pytest.approx(40.012)
    assert actual["y"] == pytest.approx(89.972)
    inputs = case.metadata["normal_transfer"]["phases"]["test_stroke"]["xy_inputs"]
    assert inputs[0]["actual_delta_mm"]["x"] == pytest.approx(.012)
    assert inputs[1]["actual_delta_mm"]["y"] == pytest.approx(-.028)
    assert all(i["actual_delta_mm"]["z"] == 0 for i in inputs)


def test_stop_between_reaim_axes_retains_intent_and_cannot_replay(motion_case, monkeypatch):
    case = motion_case
    phase = _begin_phase(case.ctx, "test_stroke")
    move = case.manip.move_axis_to
    def first_only(*args, **kwargs):
        move(*args, **kwargs)
        case.ctx.cancel.set()
    monkeypatch.setattr(case.manip, "move_axis_to", first_only)
    with pytest.raises(StopRequested):
        AdjustFirstContact("stamp", "first")._reaim(case.ctx, phase, .01, -.03)
    assert len(case.moves) == 1
    case.ctx.cancel.clear()
    with pytest.raises(NeedsHumanError, match="Pending transfer intent"):
        _begin_phase(case.ctx, "different_name")


@pytest.mark.parametrize("failure", ["low", "unknown_mapping", "readback"])
def test_one_time_reaim_refuses_unsafe_or_unknown_actual_execution(motion_case, failure):
    case = motion_case
    phase = _begin_phase(case.ctx, "test_stroke")
    if failure == "low":
        case.manip.positions["z"] = 6.1
    elif failure == "unknown_mapping":
        case.ctx.verification["transfer_sequence"]["holder_xy_mapping"] = "unknown"
    else:
        case.manip.offset = .02
    from qumus2.core.errors import PreconditionFailed
    with pytest.raises((NeedsHumanError, PreconditionFailed)):
        AdjustFirstContact("stamp", "first")._reaim(case.ctx, phase, .01, -.03)
    assert len(case.moves) == (1 if failure == "readback" else 0)


def test_actual_first_contact_observation_uses_pixels_then_one_signed_reaim(motion_case, monkeypatch):
    import qumus2.flows.library.stacking as module
    import qumus2.records.lab_geometry as evidence
    case = motion_case
    original_capture = case.scope.capture
    def changed_pixels_with_false_truth():
        frame = original_capture()
        if case.manip.positions["z"] < 6.5:
            image = Image.open(frame.path).convert("RGB")
            ImageDraw.Draw(image).ellipse((210, 130, 270, 190), fill="white")
            image.save(frame.path)
        frame.meta["optical_gap"] = {"has_contact": False, "signed_apex_gap_mm": 999.}
        return frame
    monkeypatch.setattr(case.scope, "capture", changed_pixels_with_false_truth)
    calls = []
    monkeypatch.setattr(module, "autofocus", lambda *args, **kwargs: calls.append(kwargs))
    facts = []
    def retain_fact(store, fact, **kwargs):
        facts.append(fact)
        return store.emit("lab.evidence", fact.model_dump(mode="json"), run_id=kwargs["run_id"])
    monkeypatch.setattr(evidence, "record_evidence", retain_fact)
    # This camera/motion fixture consumes an admitted source; native AF proof has separate tests.
    case.ctx.params["flake_ids"] = ["a"]
    case.metadata["normal_transfer"] = {"source_references": {"a": {"sha256": "source", "focus_binding": {}}}}
    monkeypatch.setattr(module, "_validate_focus_reference", lambda *args, **kwargs: None)
    step = AdjustFirstContact("stamp", "observed_first")
    step.act(case.ctx, None)
    observation = step.verify(case.ctx)
    step.on_success(case.ctx, observation)
    region = observation.measurements["first_contact_region"]
    assert observation.verdict == "ok" and region["centroid_px"] == pytest.approx([240., 160.])
    assert region["reaim_requested_mm"] == pytest.approx([0., .219])
    assert [move[0] for move in case.moves if move[0] != "z"] == ["x", "y"]
    assert region["reaim_readback"]["y"] == pytest.approx(90.219)
    assert len(calls) == 1 and calls[0]["attempts"] == 1
    assert facts[0].property == "first_contact_region" and facts[0].source.kind == "synthetic"
    assert facts[0].source.sha256 == region["current"]["sha256"]
    assert "optical_gap" not in region["current"]["meta"] and "world_point" not in region


@pytest.mark.parametrize("entry", ["retry", "reverify", "resume", "recover", "worker"])
@pytest.mark.parametrize("corrupt", ["unknown_source", "malformed_raw"])
def test_service_parks_unavailable_source_or_malformed_raw_without_context(admitted, entry, corrupt):  # noqa: F811
    h = admitted
    h.store.set_run_status(h.rid, "succeeded")
    rid = h.store.create_run("removed_historical_flow" if corrupt == "unknown_source" else
                            "consistency_fixture", {} if corrupt == "unknown_source" else [])
    h.store.update_run_meta(rid, workflow=h.hierarchy().manifest())
    h.store.set_run_status(rid, "paused" if entry == "resume" else
                           "queued" if entry in ("recover", "worker") else "parked")
    h.service.context_factory = lambda *args: pytest.fail("invalid saved source must precede context")
    if entry == "worker":
        h.service.enqueue(rid)
        h.service._spawn(99)
        assert h.service._done[rid].wait(1)
    else:
        getattr(h.service, entry)(rid) if entry != "recover" else h.service.recover()
    assert h.store.get_run(rid)["status"] == "waiting_human"
    assert h.store.get_run(rid)["error"]["reason"] == "construction_failed"
    assert not h.trace


def test_unknown_historical_source_does_not_interrupt_recovery_of_other_jobs(admitted):  # noqa: F811
    h = admitted
    rid = h.store.create_run("removed_historical_flow", {})
    h.store.update_run_meta(rid, workflow=h.hierarchy().manifest())
    factory = h.service.context_factory
    def current_only(job, *args):
        if job == rid:
            pytest.fail("unknown source cannot construct its old context")
        return factory(job, *args)
    h.service.context_factory = current_only
    decisions = {d.run_id: d for d in h.service.recover()}
    assert decisions[rid].action == "park"
    assert decisions[h.rid].action == "resume"
    assert not h.trace


@pytest.mark.parametrize("source_present", [True, False])
def test_pickup_source_presence_holds_before_stamp_focus_without_relocation(motion_case, monkeypatch, source_present):
    import hashlib
    from pathlib import Path

    import qumus2.flows.library.stacking as module
    from qumus2.flows.step import StepOutcome, StepStatus, Verdict
    case = motion_case
    step = PickUpFlake("a", "stamp", "hBN")
    reference = {"original_source": "retained", "sha256": "source", "source_id": "a"}
    case.metadata["normal_transfer"] = {"phases": {step.name: {
        "name": step.name, "profile": TRANSFER_PROFILE_ID, "complete": True, "coverage": {"covered": True}}}, "source_references": {"a": reference}}
    source_frames = [case.scope.capture(), case.scope.capture()]
    source_hashes = [hashlib.sha256(Path(f.saved_path()).read_bytes()).hexdigest() for f in source_frames]
    reference["sha256"] = source_hashes[0]
    def source(context, material, radius, images, *, frames, source_reference):
        assert source_reference == reference and material == "hBN"
        frames.extend(source_frames)
        images.append(source_frames[0].saved_path())
        return [SimpleNamespace(material="hBN")] if source_present else []
    monkeypatch.setattr(module, "_chip_near_centre", source)
    monkeypatch.setattr(module, "autofocus", lambda *args, **kwargs: pytest.fail("no mandatory stamp AF"))
    case.ctx.store.get_sample = lambda _: {"props": {"stack": []}}
    case.ctx.store.upsert_sample = lambda *args, **kwargs: pytest.fail("unconfirmed pickup cannot relocate samples")
    observed = step.verify(case.ctx)
    assert not case.moves
    if source_present:
        assert observed.verdict == Verdict.UNCLEAR
        assert observed.measurements["flake_on_chip"] is True
        assert observed.measurements["flake_on_stamp"] is None
        assert observed.measurements["stamp_evaluated"] is False
        assert observed.measurements["transfer_confirmed"] is False
        assert observed.measurements["no_transfer_confirmed"] is None
        assert observed.measurements["source_detection_count"] == 1
        assert observed.measurements["method"] == "source_disappearance"
        assert "Partial transfer cannot be excluded" in observed.message
        assert observed.images == [f.saved_path() for f in source_frames]
        assert [f["sha256"] for f in observed.measurements["source_frames"]] == source_hashes
        with pytest.raises(NeedsHumanError, match="cannot commit a transfer"):
            step.on_success(case.ctx, observed)
        assert step.correct(case.ctx, observed) is None
        outcome = StepOutcome(StepStatus.NEEDS_AGENT, step.name, 1, 0, [observed])
        assert StackDeviceFlow().on_step_failed(bound_params(), step, outcome, case.ctx) is None
    else:
        assert observed.verdict == Verdict.UNCLEAR
        assert observed.measurements["transfer_confirmed"] is False
        assert observed.measurements["process_confirmation"] is False
        assert observed.measurements["flake_on_chip"] is False
        assert observed.measurements["flake_on_stamp"] is None
        assert observed.measurements["stamp_evaluated"] is False
        # A fake observer's booleans alone cannot pass the actual ledger guard.
        with pytest.raises(NeedsHumanError, match="cannot commit a transfer"):
            step.on_success(case.ctx, observed)


@pytest.mark.parametrize("key", ["source", "contact", "carried_hbn", "raster", "fringes"])
@pytest.mark.parametrize("change", ["missing", "old"])
def test_unsupported_profile_is_refused_before_any_pixel_load(motion_case, monkeypatch, key, change):
    import qumus2.flows.library.stacking as module
    frame = motion_case.scope.capture()
    if change == "missing":
        frame.meta["calibration"]["photometry_profile"].pop(key)
    else:
        frame.meta["calibration"]["photometry_profile"][key] = "unsupported"
    frame.path, frame.data = "/unreadable-unsupported-reference.png", b"invalid pixels"
    monkeypatch.setattr(module, "load_pixels", lambda *args: pytest.fail("unsupported profile opened pixels"))
    with pytest.raises(NeedsHumanError, match="complete supported capture"):
        module._image_calibration(frame)


@pytest.mark.parametrize("fault", ["source", "frame", "u", "v", "scales_missing", "scales_length",
    "scales_zero", "scales_nan", "scales_bool", "size_missing", "size_length", "size_zero", "size_float",
    "size_bool", "calibration", "metadata"])
def test_malformed_capture_metadata_refuses_before_pixel_access(motion_case, monkeypatch, fault):
    import qumus2.flows.library.stacking as module
    frame = motion_case.scope.capture()
    c = frame.meta["calibration"]
    if fault in ("source", "frame"):
        c[fault] = "unsupported"
    elif fault in ("u", "v"):
        c[fault+"_axis"] = "unknown"
    elif fault.startswith("scales"):
        if fault == "scales_missing":
            c.pop("mm_per_pixel")
        else:
            c["mm_per_pixel"] = {"scales_length": [.002], "scales_zero": [0., .002],
                "scales_nan": [float("nan"), .002], "scales_bool": [True, .002]}[fault]
    elif fault.startswith("size"):
        if fault == "size_missing":
            c.pop("image_size_px")
        else:
            c["image_size_px"] = {"size_length": [480], "size_zero": [0, 320],
                "size_float": [480., 320], "size_bool": [True, 320]}[fault]
    elif fault == "calibration":
        frame.meta["calibration"] = "invalid"
    else:
        frame.meta = None
    monkeypatch.setattr(module, "load_pixels", lambda *args: pytest.fail("invalid metadata opened pixels"))
    with pytest.raises(NeedsHumanError, match="complete supported capture"):
        module._image_calibration(frame)


def test_admitted_metadata_still_requires_the_actual_declared_raster_size(motion_case, monkeypatch):
    import qumus2.flows.library.stacking as module
    frame = motion_case.scope.capture()
    frame.meta["calibration"]["image_size_px"] = [400, 320]
    original, calls = module.load_pixels, []
    def load(source):
        calls.append(source)
        return original(source)
    monkeypatch.setattr(module, "load_pixels", load)
    with pytest.raises(NeedsHumanError, match="Actual raster size"):
        module._image_calibration(frame)
    assert calls == [frame]


def test_historical_swap_data_cannot_construct_or_resume_replacement_steps():
    params = bound_params(stamp_swaps=[{"flake": "a", "torn": "old", "torn_slot": "right_stamp_2"}])
    raw = params.model_dump()
    preserved = deepcopy(raw)
    with pytest.raises(WorkflowMismatch, match="Historical automatic stamp replacement"):
        StackDeviceFlow().build_steps(params)
    run = {"id": "old_plan", "flow": "stack_device", "params": raw,
           "meta": {}, "project_id": None, "error": None}
    decision = decide(SimpleNamespace(get_steps=lambda _: []),
                      lambda *args: pytest.fail("historical swap plan constructed context"), run)
    assert decision.action == "park"
    assert decision.workflow_error["reason"] == "historical_stamp_swap_plan"
    assert raw == preserved  # Read-only history is not rewritten into a new plan.


@pytest.mark.parametrize('entry', ['retry', 'reverify', 'resume', 'recover', 'worker', 'executor'])
@pytest.mark.parametrize('claim', ['completed_refind_deleted_meta', 'unsafe_pickup_deleted_meta', 'partial_focus_binding'])
def test_v2_claimed_progress_refuses_before_context_or_energy(admitted, entry, claim):  # noqa: F811
    h = admitted
    h.store.set_run_status(h.rid, 'succeeded')
    rid = h.store.create_run('stack_device', bound_params().model_dump())
    name = 'v1_pick_up_a' if claim == 'unsafe_pickup_deleted_meta' else 'refind_a'
    h.store.begin_step(rid, 0, name, safe_to_resume=claim != 'unsafe_pickup_deleted_meta')
    if claim != 'unsafe_pickup_deleted_meta':
        h.store.finish_step(rid, 0, 'done')
    h.store.update_run_meta(rid, held_heaters={'stamp_heater': 170.})
    if claim == 'partial_focus_binding':
        h.store.update_run_meta(rid, normal_transfer={'source_references': {'a': {'source_id': 'a'}}})
    h.store.set_run_status(rid, 'paused' if entry == 'resume' else
                           'queued' if entry in ('recover', 'worker') else 'parked')
    h.service.context_factory = lambda *args: pytest.fail('binding refusal must precede context and heat restoration')
    if entry == 'worker':
        h.service.enqueue(rid)
        h.service._spawn(93)
        assert h.service._done[rid].wait(1)
    elif entry == 'executor':
        assert h.service.executor.execute(rid, h.factory(rid, bound_params().model_dump(), None), restore=True) == 'parked'
    else:
        getattr(h.service, entry)(rid) if entry != 'recover' else h.service.recover()
    assert h.store.get_run(rid)['status'] == 'waiting_human'
    assert not h.trace


def test_v2_pre_refind_recovery_requires_no_future_focus_evidence(admitted):  # noqa: F811
    h = admitted
    rid = h.store.create_run('stack_device', bound_params().model_dump())
    h.store.begin_step(rid, 0, 'heat_transfer_stage')
    h.store.finish_step(rid, 0, 'done')
    run = h.store.get_run(rid)
    decision = decide(h.store, lambda *args: pytest.fail('no context on completed early checkpoint'), run)
    assert decision.action == 'resume'


def test_v1_drop_heats_with_autofocus_lowers_to_4_9_then_lifts_5mm(motion_case, monkeypatch):
    """#204: molecule_drop_down step for step: initialize Z 5.4 at 0.1 mm/s,
    heat to 155 C with an autofocus every 10 s, Z 4.9 at 0.02 mm/s, wait
    above 170 C, lift 5 mm at 0.1 mm/s, heater off. No peel, no checks."""
    import qumus2.flows.library.stacking as module

    case = motion_case
    step, heater, trace = _real_release_heater(case, monkeypatch, "PC", 155., 170.)
    case.ctx.params["release_policy"] = module.V1_DROP_POLICY_ID
    focus = []
    monkeypatch.setattr(module, "autofocus", lambda ctx, objective, **kw: focus.append(ctx.clock.now()) or case.scope.focus)
    step.act(case.ctx, None)
    phase = case.metadata["normal_transfer"]["phases"][step.name]
    assert phase["complete"] and phase["outputs_off"] and phase["release_policy"] == module.V1_DROP_POLICY_ID
    assert heater.get_target() is None
    z_moves = [(move[2], move[3]["pending_intent"]["speed_mm_s"]) for move in case.moves if move[0] == "z"]
    temps = [p["temperature"] for kind, p in trace if kind == "preset_motion"]
    down = [i for i, (z, speed) in enumerate(z_moves) if speed == .02]
    assert {speed for z, speed in z_moves[:down[0]]} == {.1} and z_moves[down[0]-1][0] == pytest.approx(5.4)
    assert z_moves[down[-1]][0] == pytest.approx(4.9) and all(temps[i] >= 155. for i in down)
    lift = z_moves[down[-1]+1:]
    assert {speed for z, speed in lift} == {.1} and lift[-1][0] == pytest.approx(9.9)
    assert all(temps[i] > 170. for i in range(down[-1]+1, len(z_moves)))
    assert phase["withdrawal"]["actual_readback"]["z"] == pytest.approx(9.9) and phase["peak_c"] > 170.
    # one autofocus per 10 s temperature check while heating, none while waiting for the melt
    assert len(focus) >= 2 and all(b-a >= 10.-1e-6 for a, b in zip(focus, focus[1:], strict=False))
    drop_at = phase["drop_readback"]
    assert drop_at["z"] == pytest.approx(4.9)
