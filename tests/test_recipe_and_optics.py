"""Testbench model changes of 2026-10-22 (Jialei, simulation workflow review):

- exfoliation outcome = a probability model around a hidden best recipe
  (removed by Jialei on 2026-11-03: the simulation no longer scores recipes;
  the parameters are still carried out by the devices);
- press depth is a recipe parameter, with a floor in the SafetyGate;
- the stamp in the light path shifts the chip's focus (refocus step), Newton
  rings show a few um before contact, the gap is estimated from two focal
  planes so the stamp approaches quickly and touches lightly;
- an arm nozzle passing through an exfoliator roll is a collision.
"""

from __future__ import annotations

import math

import pytest

from qumus2.app import build_services
from qumus2.config import load_yaml
from qumus2.core.errors import SafetyViolation
from qumus2.core.types import Pose
from tests.conftest import quiet_bench
from tests.test_focus_evidence import focus_context, native_reference  # noqa: F401
from tests.test_stacking import run


def _exf(services):
    return services.lab.world.model["exfoliation"]


def _flakes(services, **params):
    st = run(services, "exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"], **params},
             timeout=900)
    assert st.status == "succeeded", [x for x in st.steps if x["status"] != "done"][:2]
    return services.lab.world.objects["chip_001"].flakes


def _lab(seed):
    return build_services(":memory:", seed=seed, testbench_config=quiet_bench())


def _summary(flakes):
    return [(f.id, f.layers, round(f.area_um2, 6), round(f.x_mm, 9), round(f.y_mm, 9)) for f in flakes]


def test_the_recipe_does_not_change_what_a_seed_exfoliates():
    """2026-11-03: no recipe model, so the same seed gives the same flakes for
    v1's flow defaults and for a hot, slow, deep, much-massaged recipe."""
    other = dict(target_temp_c=70, num_massages=3, peel_speed=150, peel_angle_deg=25,
                 press_depth_mm=0.3)
    for sd in (1, 2):
        default = _summary(_flakes(_lab(sd)))
        assert default and default == _summary(_flakes(_lab(sd), **other))


def test_a_fast_steep_peel_with_the_vacuum_on_never_lifts_the_chip():
    cfg = load_yaml("testbench.yaml")  # quiet_bench would hide a lift probability
    s = build_services(":memory:", seed=3, testbench_config=cfg)
    _flakes(s, peel_speed=500, peel_angle_deg=60)
    assert s.lab.world.objects["chip_001"].held_by is None


def test_the_nozzle_presses_as_deep_as_asked_and_the_gate_has_a_floor(services):
    s = services
    _flakes(s, press_depth_mm=0.35)
    assert s.lab.world.tape.press_depth == pytest.approx(0.35, abs=1e-6)
    bench = s.lab.bench["arm_left"]
    stage = s.lab.bench["stations"]["exfoliation_stage"]
    x, y = stage["x"] - bench["base"]["x"], stage["y"] - bench["base"]["y"]
    arm = s.gate.arm("arm_left")
    arm.move_to(Pose(x=x, y=y, z=bench["safe_z"]))
    with pytest.raises(SafetyViolation, match="floor"):
        arm.move_to(Pose(x=x, y=y, z=0.5))  # 0.7 below contact: could crack the chip
    with pytest.raises(ValueError, match="bad params"):
        s.jobs.submit("exfoliate_chips", {"material": "Gr", "press_depth_mm": 0.9})




def test_v1_initialization_and_sequence_preserve_qualified_source_focus(native_reference):  # noqa: F811
    import hashlib
    from copy import deepcopy
    from pathlib import Path

    import qumus2.flows.library.stacking as module
    from tests.test_v1_transfer_sequence import bound_params

    s, ctx, _, _, reference = native_reference
    original = deepcopy(reference)
    module._validate_focus_reference(reference, ctx.run_id, parked=True, store=s.store)
    scope = ctx.gate.microscope(module.MICROSCOPE)
    held_stage = ctx.gate.stage(module.STAGE).kinematics()
    substrate = scope.get_focus()
    assert substrate == reference["meta"]["focus_z"]
    expected_focus = substrate + ctx.verification["transfer_sequence"]["focus_offset_mm"]
    mark = s.store.last_event_id()
    step = module.InitializeStamp("stamp", "v1_initialize_first")
    for _ in range(2):
        step.act(ctx, None)
        assert step.verify(ctx).verdict == module.Verdict.OK
        assert scope.get_objective() == 5
        assert scope.get_focus() == pytest.approx(expected_focus)
        assert ctx.gate.stage(module.STAGE).kinematics() == held_stage
        saved = module._transfer_state(ctx)["initializations"][step.name]
        assert saved["substrate_focus_mm"] == substrate
        assert saved["objective"] == 20 and saved["applied"]
    new_events = s.store.events_after(mark, limit=10000)
    commands = [e for e in new_events if e.kind == "device.command"]
    assert commands[0].payload == {"device": module.MICROSCOPE, "command": "set_objective", "params": {"magnification": 5}}
    assert any(e.payload["device"] == module.MANIP for e in commands)
    assert [e.payload["params"]["z"] for e in commands
            if e.payload["device"] == module.MICROSCOPE and e.payload["command"] == "set_focus"] == [expected_focus] * 2
    assert not any(e.kind == "microscope.focus" for e in new_events)
    assert reference == original
    assert hashlib.sha256(Path(reference["image"]).read_bytes()).hexdigest() == reference["sha256"]
    module._validate_focus_reference(reference, ctx.run_id, parked=True, store=s.store)

    steps = module.StackDeviceFlow().build_steps(bound_params())
    expected = ["refind_a", "v1_initialize_a", "v1_first_contact_stamp", "v1_pick_up_a", "park_stamp_a",
                "refind_b", "v1_initialize_b", "v1_pick_up_b", "park_stamp_b", "v1_initialize_target", "v1_drop_down"]
    assert [step.name for step in steps if step.name in expected] == expected
    assert sum(isinstance(step, module.AdjustFirstContact) for step in steps) == 1
    assert [step.name for step in steps if isinstance(step, module.InitializeStamp)] == [
        "v1_initialize_a", "v1_initialize_b", "v1_initialize_target"]
    assert not any("align_stamp" in step.name or "refocus_through_stamp" in step.name for step in steps)


def test_focus_capture_keeps_the_public_optical_packet_and_stage_bound(tmp_path):
    from copy import deepcopy
    from pathlib import Path

    from tests.test_white_light_optics import optical_lab

    _, clock, gate, raw = optical_lab(tmp_path)
    scope = gate.microscope("microscope")
    held = gate.stage("transfer_stage").kinematics()
    before = scope.capture()
    frozen = deepcopy(before.meta["optical_gap"])
    original_bytes = Path(before.saved_path()).read_bytes()
    focus = scope.get_focus()
    scope.set_focus(focus + .1)
    refocused = scope.capture()
    assert refocused.meta["focus_z"] == pytest.approx(focus + .1)
    assert gate.stage("transfer_stage").kinematics() == held
    assert before.meta["optical_gap"] == frozen
    assert Path(before.saved_path()).read_bytes() == original_bytes
    assert refocused.meta["optical_gap"]["profile_id"] == frozen["profile_id"]
    assert refocused.meta["optical_gap"]["has_contact"] is False
    assert before.meta["objective"] == refocused.meta["objective"] == 20
    # Existing optical fixture, real Gate/Clock motion; no source preparation or inferred-gap controller.
    gate.manipulator("transfer_arm").move_axis_to("z", 5.2998)
    clock.sleep(.2)
    changed = scope.capture()
    assert changed.meta["optical_gap"]["has_contact"] is True
    assert changed.meta["optical_gap"]["current_contact_radius_mm"] > 0
    assert changed.meta["optical_gap"] != refocused.meta["optical_gap"]
    assert gate.stage("transfer_stage").kinematics() == held
    assert raw.get_focus() == pytest.approx(focus + .1)



def test_an_arm_path_through_an_exfoliator_roll_is_a_collision(services):
    s = services
    w, arm = s.lab.world, s.lab.devices.get("arm_left")
    ex = s.lab.bench["exfoliator"]
    # the movable roll parks above the fixed one, park_height_mm over the stage
    rx, ry, rz = ex["origin"]["x"], ex["origin"]["y"], ex["park_height_mm"]
    bx, by = arm.base
    arm.move_to(Pose(x=rx - 40 - bx, y=ry - by, z=rz))
    assert not w.collisions
    arm.move_to(Pose(x=rx + 40 - bx, y=ry - by, z=rz))  # straight through it
    assert [c["what"] for c in w.collisions] == ["exfoliator_movable_roll"]
    assert math.isclose(w.collisions[0]["at"][2], rz, abs_tol=0.1)
