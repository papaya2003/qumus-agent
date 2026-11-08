"""Testbench model fixes from the manual review (notes/v2-testbench/
testbench-rework-plan.md section 3): what the simulated world remembers, how
the detector errs, what clearing faults does, what Claude may see."""

from __future__ import annotations

import statistics

from conftest import quiet_bench

from qumus2.agents.tooling import ToolResult
from qumus2.app import build_lab_side
from qumus2.core.clock import SimClock
from qumus2.core.types import Pose
from qumus2.testbench.lab import build_sim_lab
from qumus2.testbench.persist import save_lab_state
from qumus2.testbench.world import SimFlake, SimObject, SimWorld


def test_restart_keeps_the_stage_offset_so_the_chip_still_rides_it(tmp_path):
    db, state = tmp_path / "q.db", tmp_path / "state.pkl"
    a = build_lab_side(db, seed=1, testbench_config=quiet_bench())
    w, stage = a.lab.world, a.lab.world.surfaces["transfer_stage"]
    cx, cy = (sum(stage.box["x"]) / 2, sum(stage.box["y"]) / 2)
    with w.lock:
        chip = w.objects["chip_002"]
        chip.pose, chip.on = Pose(x=cx, y=cy), "transfer_stage"
    a.lab.devices.get("transfer_stage").move_to(Pose(x=40, y=25, z=0))
    save_lab_state(a.lab, a.clock, state)

    b = build_lab_side(db, seed=1, testbench_config=quiet_bench(), state_path=state)
    b.lab.devices.get("transfer_stage").move_to(Pose(x=0, y=0, z=0))  # back to load
    back = b.lab.world.objects["chip_002"]
    assert abs(back.pose.x - cx) < 0.05 and abs(back.pose.y - cy) < 0.05


def test_an_off_centre_pick_stays_off_centre_until_put_down():
    w = SimWorld(seed=1)
    w.faults.placement_sigma_mm = w.faults.placement_sigma_deg = 0.0
    w.add(SimObject(id="c", kind="chip", pose=Pose(x=100, y=100)))
    assert w.pick("arm", Pose(x=101.5, y=100, z=2, rotation=90)) is not None
    # the nozzle turns and travels; the chip hangs 1.5 mm off its axis
    w.carry("arm", Pose(x=200, y=50, z=60, rotation=0))
    c = w.objects["c"]
    assert abs(c.pose.x - 200) > 1.0 or abs(c.pose.y - 50) > 1.0
    w.release("arm", Pose(x=300, y=300, z=3, rotation=90))
    assert abs(c.pose.x - 298.5) < 1e-6 and abs(c.pose.y - 300) < 1e-6


def test_averaging_frames_does_not_remove_the_detector_bias():
    cfg = quiet_bench()
    cfg["locator"].update(bias_sigma_mm=0.3, sigma_mm=0.05)
    lab = build_sim_lab(SimClock(), seed=3, config=cfg)
    lab.world.faults.locator_sigma_mm = 0.05
    cam = lab.devices.get("overview_camera")
    truth = lab.world.objects["chip_001"].pose
    region = next(n for n, s in lab.bench["slots"].items()
                  if abs(s["x"] - truth.x) < 1 and abs(s["y"] - truth.y) < 1)
    xs, ys = [], []
    for _ in range(30):
        d = [x for x in lab.locator.locate(cam.capture(), region=region, frame_of="world")
             if x.label == "chip"][0]
        xs.append(d.pose.x)
        ys.append(d.pose.y)
    mean_err = ((statistics.mean(xs) - truth.x) ** 2 + (statistics.mean(ys) - truth.y) ** 2) ** .5
    assert statistics.pstdev(xs) < 0.1  # the frame-to-frame jitter is small ...
    assert mean_err > 0.02  # ... but the mean keeps the bias (0.3 mm sigma, seed 3)
    # moving the chip draws a new bias
    first = lab.locator._bias["chip_001"][1]
    with lab.world.lock:
        lab.world.objects["chip_001"].pose = truth.offset(dx=0.5)
    lab.locator.locate(cam.capture(), region=region, frame_of="world")
    assert lab.locator._bias["chip_001"][1] != first


def test_clear_stops_faults_and_restores_config_probabilities():
    cfg = quiet_bench()
    lab = build_sim_lab(SimClock(), seed=1, config=cfg)
    f, fi = lab.world.faults, lab.faults
    before = (f.pick_fail_prob, f.big_error_prob)
    fi.inject("pick_fail_prob", {"value": 0.9})
    fi.inject("big_error_prob", {"value": 0.9})
    fi.inject("pick_fail_next", {"count": 3})
    fi.inject("stamp_torn", {})
    fi.inject("arm_step_loss", {"device": "arm_left", "dx": 2, "dy": 0})
    fi.inject("device_offline", {"device": "microscope", "offline": True})
    fi.clear()
    assert (f.pick_fail_prob, f.big_error_prob) == before
    assert f.pick_fail_next == 0 and not f.tear_next_stamp
    assert lab.devices.get("arm_left").step_offset == (0.0, 0.0)
    assert not lab.devices.get("microscope").offline


def test_simulator_ids_never_reach_claude():
    r = ToolResult(status="ok", summary="s", data={
        "samples": [{"id": "chip_001_f001", "props": {"sim_id": "simflake_00007", "area": 3}}],
        "sim_id": "x"})
    text = r.to_text()
    assert "sim_id" not in text and "simflake" not in text and '"area": 3' in text


def test_v1compare_counts_a_wrong_ledger_as_a_failure():
    from v1compare.scoring import score

    w = SimWorld(seed=1)
    chip = SimObject(id="chip_001", kind="chip", pose=Pose(x=500, y=150))
    chip.flakes.append(SimFlake(id="sf1", material="Gr", layers=1, x_mm=0, y_mm=0,
                                area_um2=200))
    w.add(chip)
    rec = [{"sim_id": "sf1", "label": "monolayer", "chip": "chip_001"}]
    good = score("v2", w, claimed_success=True, records=rec, ledger={"chip_001": (500, 150)})
    bad = score("v2", w, claimed_success=True, records=rec, ledger={"chip_001": (560, 150)})
    assert good.truth_success and not good.false_success
    assert not bad.truth_success and bad.false_success and bad.ledger_ok is False
