"""The testbench as the real lab is laid out (bench.yaml, 2026-10-21): three
storage boards, arms that each reach only their side, one overview camera,
the exfoliator gantry with a peel angle, two focal planes under the
microscope; and the faults that come with them."""

from __future__ import annotations

import pytest
from conftest import put_sample

from qumus2.config import load_yaml
from qumus2.core.types import Pose
from qumus2.flows.bench import arm_positions, positions, reachable
from qumus2.flows.context import FlowContext
from qumus2.flows.library.primitives import look


def run(s, flow, params, timeout=120):
    return s.jobs.wait(s.jobs.submit(flow, params), timeout)


def events(s, kind):
    return [e for e in s.store.events_after(0, limit=1_000_000) if e.kind == kind]


def failed_note(st):
    return next(x["note"] for x in st.steps if x["status"] == "failed")


def _ctx(s):
    rid = s.store.create_run("inspect_bench", {})
    return FlowContext(run_id=rid, store=s.store, gate=s.gate, clock=s.clock,
                       locator=s.lab.locator, bench=s.lab.bench,
                       verification=load_yaml("verification.yaml"))


# ----- layout ------------------------------------------------------------------

def test_each_arm_reaches_only_its_side(services):
    b = services.lab.bench
    left, right = set(reachable(b, "arm_left")), set(reachable(b, "arm_right"))
    assert "exfoliation_stage" in left and "transfer_stage" not in left
    assert "transfer_stage" in right and "exfoliation_stage" not in right
    assert {n for n in left & right} == {f"mid_chip_{i}" for i in range(1, 10)}  # hand-over
    assert not any(n.startswith("left_") for n in right)
    assert not any(n.startswith("right_") for n in left)
    for arm in ("arm_left", "arm_right"):  # every place of an arm is inside its reach
        for p in arm_positions(b, arm).values():
            assert (p.x ** 2 + p.y ** 2) ** 0.5 <= b[arm]["reach_mm"]


def test_bare_chip_goes_to_the_stage_over_the_middle_board(services):
    st = run(services, "place_chip_on_transfer_stage", {"chip_id": "chip_003"})
    assert st.status == "succeeded", st.steps
    assert [x["name"] for x in st.steps] == ["hand_over_chip", "place_chip_on_transfer_stage"]
    chip = services.store.get_sample("chip_003")
    assert chip["location"] == "transfer_stage" and chip["props"]["home_slot"] == "mid_chip_1"
    # reported in the world frame: the stage, not arm_right's own coordinates
    exp = st.steps[1]["expected"]
    stage = services.lab.bench["stations"]["transfer_stage"]
    assert (exp["x"], exp["y"], exp["frame"]) == (stage["x"], stage["y"], "world")


def test_arm_cannot_be_sent_where_it_does_not_reach(services):
    from qumus2.flows.library.transfer import MoveObject

    ctx = _ctx(services)
    step = MoveObject("chip_001", "chip", arm="arm_right", camera="overview_camera",
                      dest="transfer_stage", cfg_key="place_chip_on_transfer_stage")
    with pytest.raises(Exception, match="cannot reach"):
        step.preconditions(ctx)


# ----- exfoliator: gantry, peel angle, tape roll ---------------------------------

def test_peel_angle_reaches_the_exfoliator(services):
    s = services
    st = run(s, "exfoliate_chips", {"material": "Gr", "peel_angle_deg": 20})
    assert st.status == "succeeded", st.steps
    peels = [e.payload for e in events(s, "device.command") if e.payload["command"] == "peel"]
    assert peels and all(p["params"]["angle_deg"] == 20 for p in peels)
    assert s.lab.world.tape_snapshot()["last_peel"]["angle_deg"] == 20
    assert s.store.get_sample("chip_001")["location"] == "mid_chip_1"  # middle board


def test_peel_angle_outside_limits_is_refused_at_submit(services):
    with pytest.raises(Exception, match="peel_angle_deg"):
        services.jobs.submit("exfoliate_chips", {"material": "Gr", "peel_angle_deg": 95})


def test_empty_tape_roll_waits_for_a_human(services):
    s = services
    s.lab.faults.inject("tape_roll_empty", {"left_mm": 10})
    st = run(s, "exfoliate_chips", {"material": "Gr"})
    assert st.status == "waiting_human"
    assert any("roll" in (x.get("note") or "") for x in st.steps)
    s.lab.faults.inject("tape_roll_empty", {"left_mm": 3000})  # a new roll is loaded
    assert s.lab.devices.get("exfoliator").remaining_mm() == 3000


def test_tape_roll_is_used_up_and_recorded(services):
    s = services
    assert run(s, "exfoliate_chips", {"material": "Gr"}).status == "succeeded"
    left = s.lab.devices.get("exfoliator").remaining_mm()
    assert left < 3000
    assert s.store.get_sample("tape")["props"]["roll_left_mm"] == left


# ----- microscope: stamp and chip are two focal planes ----------------------------

def test_flakes_are_seen_only_on_the_plane_in_focus(services):
    from qumus2.devices.base import Frame

    an = services.lab.analyzer
    flake = {"sim_id": "f1", "material": "Gr", "layers": 1, "area_um2": 100.0,
             "dx_mm": 0.0, "dy_mm": 0.0}
    meta = {"on_chip": "chip_001", "flakes": [flake], "stamp_flakes": [flake],
            "chip_corners": [{"corner": "bl", "dx_mm": 0.1, "dy_mm": 0.1}]}
    at_chip = Frame("microscope", 0.0, meta={**meta, "sharpness_chip": 10.5,
                                             "sharpness_stamp": 0.5, "sharpness": 10.5})
    at_stamp = Frame("microscope", 0.0, meta={**meta, "sharpness_chip": 0.5,
                                              "sharpness_stamp": 8.5, "sharpness": 8.5})
    assert an.find_flakes(at_chip, "Gr") and not an.find_stamp_flakes(at_chip)
    assert an.find_stamp_flakes(at_stamp) and not an.find_flakes(at_stamp, "Gr")
    assert an.find_corner(at_chip, "bl") and an.find_corner(at_stamp, "bl") is None


# ----- the overview camera: errors and voting ---------------------------------------



def test_voting_survives_misses_and_phantoms(make_services):
    s = make_services()
    s.lab.faults.inject("perception", {"miss_prob": 0.15, "false_positive_prob": 0.1,
                                       "confuse_material_prob": 0.0, "tail_prob": 0.0})
    ctx = _ctx(s)
    wrong = 0
    for _ in range(40):
        dets = look(ctx, "overview_camera", "left_chip_2", "arm_left", label="chip", views=5)
        wrong += len(dets) != 1
    assert wrong <= 2
    s.lab.faults.clear()
    assert s.lab.locator.miss_prob == 0.0  # back to the (quiet) config


def test_dropped_frames_and_unreadable_qr_are_retried(services):
    s = services
    s.lab.faults.inject("camera_frame_drop", {"count": 2})
    s.lab.faults.inject("qr_unreadable", {"board": "middle_storage", "looks": 2})
    put_sample(s, "chip_001", "mid_chip_1")
    st = run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert st.status == "succeeded", st.steps
    assert s.lab.devices.get("overview_camera").drop_next == 0
    assert s.lab.locator.qr_unreadable["middle_storage"] == 0


def test_qr_never_readable_parks_with_the_reason(services):
    s = services
    s.lab.faults.inject("qr_unreadable", {"board": "middle_storage", "looks": 1000})
    put_sample(s, "chip_001", "mid_chip_1")
    st = run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert st.status in ("parked", "failed")
    assert "QR" in failed_note(st)


# ----- things people and machines do to the bench ------------------------------------

def test_chip_taken_away_is_noticed_before_anything_moves(services):
    s = services
    put_sample(s, "chip_001", "mid_chip_1")
    s.lab.faults.inject("chip_removed", {"place": "mid_chip_1"})
    st = run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert st.status == "parked" and "no chip seen at mid_chip_1" in failed_note(st)
    assert not [e for e in events(s, "device.command") if e.payload["command"] == "move_to"]


def test_steps_lost_in_transit_are_corrected(services):
    s = services
    put_sample(s, "chip_001", "mid_chip_1")
    # the steps are lost on the way, with the chip on the nozzle (lost before the
    # pick, the same offset would shift pick and drop alike and cancel out)
    faults, real = s.lab.faults, s.lab.faults.before_call
    fired = []

    def before_call(device_id):
        if not fired and s.lab.world.held_by("arm_right") is not None:
            fired.append(device_id)
            faults.inject("arm_step_loss", {"device": "arm_right", "dx": 1.5, "dy": 0.0})
        real(device_id)

    faults.before_call = before_call
    st = run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert fired and st.status == "succeeded", st.steps
    obs = next(r for r in st.result if r["step"] == "place_chip_on_transfer_stage")
    msgs = [o.get("correction") or o.get("message") or "" for o in obs["observations"]]
    assert st.steps[-1]["corrections"] >= 1, msgs
    # the re-pick happens with the arm already off, so pick and drop shift alike
    chip, stage = s.lab.world.objects["chip_001"], s.lab.world.surfaces["transfer_stage"]
    assert chip.on == "transfer_stage"
    assert stage.contains(chip.pose.x, chip.pose.y)


def test_correction_event_names_the_frame_of_its_pose(services):
    s = services
    put_sample(s, "chip_001", "mid_chip_1")
    s.lab.faults.inject("next_placement", {"dx": 2.0, "dy": 0.0, "drot": 0.0})
    assert run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"}).status == "succeeded"
    c = events(s, "step.corrected")[0].payload["params"]
    assert c["pick_from_frame"] == "arm_right"
    base = s.lab.bench["arm_right"]["base"]
    w, a = c["pick_from_world"], c["pick_from"]
    assert abs(w["x"] - a["x"] - base["x"]) < 1e-6 and abs(w["y"] - a["y"] - base["y"]) < 1e-6


def test_arm_controller_reset_homes_before_moving(services):
    s = services
    put_sample(s, "chip_001", "mid_chip_1")
    s.lab.faults.inject("arm_reset", {"device": "arm_right"})
    st = run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert st.status == "succeeded", st.steps
    texts = [e.payload["text"] for e in events(s, "notification")]
    assert any("lost its position" in t for t in texts)
    assert not events(s, "safety.violation")


def test_arm_reset_between_moves_homes_and_carries_on(services):
    """A reset can come at any device call, also between two moves of one
    pick-and-place: the next move homes first instead of being refused."""
    from qumus2.flows.library.primitives import move_arm

    s = services
    ctx = _ctx(s)
    s.gate.arm("arm_right").move_to(Pose(x=0, y=100, z=100))
    s.lab.devices.get("arm_right").lose_position()
    move_arm(ctx, "arm_right", Pose(x=10, y=120, z=5))
    arm = s.gate.arm("arm_right")
    assert arm.is_homed() and arm.get_pose().xy_distance(Pose(x=10, y=120, z=5)) < 1e-9
    assert not events(s, "safety.violation")
    cmds = [e.payload["command"] for e in events(s, "device.command")
            if e.payload["device"] == "arm_right"]
    assert cmds[-3:] == ["home", "move_to", "move_to"]  # home, above the target, down


def test_place_cut_short_is_placed_again(services):
    """Killed between release and check: the chip lies on the stage, off target,
    and the ledger still has it at the source. The resumed step re-places it."""
    s = services
    put_sample(s, "chip_001", "mid_chip_1")
    stage = positions(s.lab.bench)["transfer_stage"]
    with s.lab.world.lock:
        s.lab.world.objects["chip_001"].pose = Pose(x=stage["x"] + 1.5, y=stage["y"])
    st = run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert st.status == "succeeded", st.steps
    assert any("placing it again" in e.payload["text"] for e in events(s, "notification"))
    assert s.store.get_sample("chip_001")["location"] == "transfer_stage"
    assert s.lab.world.objects["chip_001"].pose.xy_distance(
        Pose(x=stage["x"], y=stage["y"])) < 0.5


def test_chip_of_another_sample_on_the_stage_is_not_taken(services):
    s = services
    put_sample(s, "chip_001", "mid_chip_1")
    put_sample(s, "chip_002", "transfer_stage")
    with s.lab.world.lock:
        del s.lab.world.objects["chip_001"]  # gone from its slot
    st = run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert st.status == "parked" and "not empty" in failed_note(st)


def test_restarted_lab_process_homes_the_arms(services):
    """A reset arm refuses moves through the SafetyGate until it homes."""
    s = services
    arm = s.lab.devices.get("arm_left")
    arm.lose_position()
    from qumus2.core.errors import SafetyViolation

    with pytest.raises(SafetyViolation, match="not homed"):
        s.gate.arm("arm_left").move_to(Pose(x=0, y=100, z=100))
    s.gate.reset()
    s.gate.arm("arm_left").home()
    s.gate.arm("arm_left").move_to(Pose(x=0, y=100, z=100))


# ----- seed-matched worlds --------------------------------------------------------------

def test_exfoliation_outcome_does_not_depend_on_extra_looks(make_services):
    def flakes(extra_looks):
        s = make_services()
        ctx = _ctx(s)
        for _ in range(extra_looks):  # draws from the world's shared random stream
            look(ctx, "overview_camera", "left_storage", "world", views=3)
        assert run(s, "exfoliate_chips", {"material": "Gr"}).status == "succeeded"
        return [(f.layers, f.area_um2) for f in s.lab.world.objects["chip_001"].flakes]

    assert flakes(0) == flakes(7)


def test_named_places_are_on_the_table(services):
    t = services.lab.bench["table"]
    for name, p in positions(services.lab.bench).items():
        assert t["x"][0] <= p["x"] <= t["x"][1] and t["y"][0] <= p["y"] <= t["y"][1], name


# ----- chaos series ---------------------------------------------------------------------

def test_chaos_run_classifies_against_the_world():
    from qumus2.testbench.chaos import render_markdown, run_one

    r = run_one(3, n_faults=1)
    assert r.outcome in ("ok", "honest_stop"), (r.injected, r.flows, r.problems)
    assert r.outcome != "ok" or not r.problems
    text = render_markdown([r], 1)
    assert "not** lab success rates" in text


def test_inspect_follows_the_stage_under_the_objective(services):
    """E4b (real Claude, 2026-10-22): after a failed search the stage was parked
    under the objective and inspect_bench said "transfer_stage" was empty while
    chip_001 rode on it. The look follows the stage, and an empty stage is
    looked at again before it is reported empty."""
    from qumus2.core.types import Pose

    s = services
    put_sample(s, "chip_001", "mid_chip_1")
    assert run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"}).status == "succeeded"
    ta = s.lab.bench["transfer_stage"]["microscope"]
    s.gate.stage("transfer_stage").move_to(Pose(x=ta["x"], y=ta["y"]))
    st = run(s, "inspect_bench", {"regions": ["transfer_stage"]})
    assert st.status == "succeeded"
    m = st.result[0]["observations"][0]["measurements"]
    assert m["empty"] == [] and m["stage_now"]["x"] == s.lab.bench["stations"]["transfer_stage"]["x"] + ta["x"]
    (det,) = m["regions"]["transfer_stage"]
    assert det["at"] == "transfer_stage" and det["label"] == "chip"
    # back at the load position the plain region is enough and nothing is doubled
    s.gate.stage("transfer_stage").move_to(Pose(x=0, y=0))
    m = run(s, "inspect_bench", {"regions": ["transfer_stage"]}).result[0]["observations"][0]["measurements"]
    assert len(m["regions"]["transfer_stage"]) == 1
    assert m["empty"] == [] and m["regions"]["transfer_stage"][0]["at"] == "transfer_stage"
    actual = s.gate.stage("transfer_stage").get_sample_pose()
    load = s.lab.bench["transfer_stage"]["load"]
    home = s.lab.bench["stations"]["transfer_stage"]
    assert m["stage_now"] == {axis: round(home[axis]+getattr(actual, axis)-load[axis], 2)
                               for axis in ("x", "y")}
