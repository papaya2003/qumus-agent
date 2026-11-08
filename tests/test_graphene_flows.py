"""Graphene path on the testbench: exfoliation and chip search as closed-loop
flows (workstreams.md A2), plus inspect_bench and the retry/reverify recovery
calls (C5)."""

from __future__ import annotations

import pytest
from conftest import put_sample

from qumus2.core.types import Pose
from qumus2.flows.library import place_chip
from qumus2.runtime.recovery import plan_recovery


def run(s, flow, params, timeout=60):
    rid = s.jobs.submit(flow, params)
    return s.jobs.wait(rid, timeout)


def events(s, kind):
    return [e for e in s.store.events_after(0, limit=100_000) if e.kind == kind]


def step(st, name, index=0):
    return [x for x in st.steps if x["name"] == name][index]


# ----- exfoliation -----------------------------------------------------------

def test_exfoliation_prepares_tape_and_exfoliates(services):
    st = run(services, "exfoliate_chips", {"material": "graphene"})
    assert st.status == "succeeded", st.steps
    params = services.store.get_run(st.run_id)["params"]
    assert params["material"] == "Gr" and params["carrier_id"] == "carrier_gr_01"
    assert params["chip_ids"] == ["chip_001"] and params["prepare_mother_tape"] == "always"
    chip = services.store.get_sample("chip_001")
    assert (chip["status"], chip["location"], chip["props"]["material"]) == \
        ("exfoliated", "mid_chip_1", "Gr")
    assert services.store.get_sample("carrier_gr_01")["location"] == "left_slide_1"
    assert services.store.get_sample("tape")["props"]["uses"] == 1
    assert services.lab.world.objects["chip_001"].flakes  # material really transferred
    assert step(st, "press_tape")["note"].startswith("NOT_VERIFIABLE")
    assert not events(services, "safety.violation")


def test_second_run_reuses_the_mother_tape(services):
    assert run(services, "exfoliate_chips", {"material": "Gr"}).status == "succeeded"
    st = run(services, "exfoliate_chips", {"material": "Gr"})
    assert st.status == "succeeded"
    assert services.store.get_run(st.run_id)["params"]["prepare_mother_tape"] == "never"
    assert "load_mother_tape" not in [x["name"] for x in st.steps]
    assert services.store.get_sample("chip_002")["status"] == "exfoliated"


def test_material_without_carrier_is_refused_at_submit(services):
    with pytest.raises(ValueError, match="no raw-crystal carrier"):
        services.jobs.submit("exfoliate_chips", {"material": "WTe2"})
    with pytest.raises(ValueError, match="bad params"):
        services.jobs.submit("exfoliate_chips", {"material": "Gr", "target_temp_c": 55})


def test_hot_exfoliation_ramps_within_limits(services):
    st = run(services, "exfoliate_chips", {"material": "Gr", "target_temp_c": 90})
    assert st.status == "succeeded", st.steps
    heat = step(st, "heat_applied_tape")
    assert heat["safe_to_resume"] is False
    assert abs(heat["measurements"]["reached_c"] - 90) <= 2
    assert step(st, "cool_and_peel")["measurements"]["peel_temp_c"] < 60
    setpoints = [e.payload["params"]["celsius"] for e in events(services, "device.command")
                 if e.payload["command"] == "set_target"]
    assert len(setpoints) >= 2 and setpoints[-1] == 90  # ramped, not one jump
    assert not events(services, "safety.violation")
    assert services.lab.devices.get("exfoliation_heater").get_target() is None


def test_stuck_temperature_sensor_is_dangerous(services):
    services.lab.devices.get("exfoliation_heater").stuck_at = 23.0
    st = run(services, "exfoliate_chips", {"material": "Gr", "target_temp_c": 70})
    assert st.status == "failed"
    assert "stuck" in st.error["message"]
    assert services.lab.devices.get("exfoliation_heater").get_target() is None


def test_chip_lifted_by_tape_is_handed_back(services):
    services.lab.devices.get("exfoliation_vacuum").stuck_at = False  # vacuum does not hold
    st = run(services, "exfoliate_chips", {"material": "Gr"})
    assert st.status == "parked"
    failed = [x for x in st.steps if x["status"] == "failed"][0]
    # the carrier is the first thing the tape lifts
    assert failed["name"] == "load_mother_tape" and "came off with the tape" in failed["note"]
    # the note names the object and the ledger stops claiming it is on the stage
    # (chaos seed 98, 2026-10-22: it hung on the tape while the records said stage)
    assert "carrier_gr_01" in failed["note"] and "vacuum reads on" in failed["note"]
    carrier = services.store.get_sample("carrier_gr_01")
    assert carrier["props"]["location_uncertain"] and carrier["props"]["last_seen"]["at"] == "tape"
    assert services.lab.world.objects["carrier_gr_01"].held_by == "exfoliator"
    from qumus2.testbench.chaos import _world_agrees
    assert _world_agrees(services, excused=failed["note"]) == []  # the chaos judge's rule


# ----- chip search -----------------------------------------------------------

@pytest.fixture
def exfoliated(services):
    assert run(services, "exfoliate_chips", {"material": "Gr"}).status == "succeeded"
    return services


def test_search_catalogues_flakes(exfoliated):
    s = exfoliated
    st = run(s, "search_chip", {}, timeout=120)
    assert st.status == "succeeded", st.steps
    assert s.store.get_run(st.run_id)["params"]["chip_id"] == "chip_001"
    chip = s.store.get_sample("chip_001")
    assert (chip["status"], chip["location"]) == ("searched", "mid_chip_1")
    assert chip["props"]["corner_stage_xy"]
    scan = step(st, "scan_chip")["measurements"]
    assert scan["blurred_tiles"] == 0 and scan["tiles_scanned"] > 50
    flakes = s.store.list_samples(kind="flake", location="chip_001")
    assert flakes and all(f["props"]["material"] == "Gr" for f in flakes)
    catalogued = [f for f in flakes if f["status"] == "catalogued"]
    assert catalogued
    f = catalogued[0]["props"]
    assert {"10x", "20x", "100x"} <= set(f["labels"]) and f["thickness_label"] == f["labels"]["100x"]
    # flake positions are real: the stored stage position puts the sim flake under the objective
    world_chip = s.lab.world.objects["chip_001"]
    assert catalogued[0]["props"]["sim_id"] in {x.id for x in world_chip.flakes}
    assert not events(s, "safety.violation")
    # the ledger events carry what the flake library shows (photos, area, place on the
    # chip), so a recording keeps them; never the testbench's ground truth (sim_id)
    last = {e.payload["id"]: e.payload for e in events(s, "sample.updated")}
    ev = last[catalogued[0]["id"]]["props"]
    for k in ("chip_id", "labels", "area_um2", "confidence", "stage_xy", "rel_corner_mm", "images"):
        assert ev[k] == f[k], k
    assert set(ev["images"]) == {"20x", "100x"}
    assert all("sim_id" not in x["props"] for x in last.values())


def test_search_corrects_askew_placement(exfoliated):
    s = exfoliated
    s.lab.world.faults.next_placement = {"dx": 2.0, "dy": -1.5, "drot": 6.0}
    st = run(s, "search_chip", {"chip_id": "chip_001"}, timeout=120)
    assert st.status == "succeeded"
    assert step(st, "place_chip_on_transfer_stage")["corrections"] == 1
    assert step(st, "find_chip_corner")["status"] == "done"


def test_corner_out_of_view_is_found_by_spiral(exfoliated, monkeypatch):
    s = exfoliated
    real = place_chip.PlaceChipOnTransferStage.ledger_props

    def off_by_1mm(self, obs):
        props = real(self, obs)
        props["pose_on_stage"] = {**props["pose_on_stage"], "x": props["pose_on_stage"]["x"] + 1}
        return props

    monkeypatch.setattr(place_chip.PlaceChipOnTransferStage, "ledger_props", off_by_1mm)
    st = run(s, "search_chip", {}, timeout=120)
    assert st.status == "succeeded"
    assert step(st, "find_chip_corner")["corrections"] >= 1


def test_search_needs_an_exfoliated_chip(services):
    with pytest.raises(ValueError, match="no exfoliated chip"):
        services.jobs.submit("search_chip", {})
    with pytest.raises(ValueError, match="not on a board arm_right reaches"):
        services.jobs.submit("search_chip", {"chip_id": "chip_002"})
    put_sample(services, "chip_002", "mid_chip_2")
    with pytest.raises(ValueError, match="no material"):
        services.jobs.submit("search_chip", {"chip_id": "chip_002"})


def test_graphene_path_resumes_after_crash_during_peel(exfoliated):
    """Kill inside cool_and_peel with the tape still down: probe says not done,
    the run resumes from that step."""
    s = exfoliated
    rid = s.jobs.submit("exfoliate_chips", {"material": "Gr"})
    s.jobs.wait(rid, 60)
    # rebuild the crash: the run had reached cool_and_peel (seq of it) with the tape applied
    steps = s.store.get_steps(rid)
    seq = next(x["seq"] for x in steps if x["name"] == "cool_and_peel")
    crashed = s.store.create_run("exfoliate_chips", s.store.get_run(rid)["params"])
    for x in steps[:seq]:
        s.store.begin_step(crashed, x["seq"], x["name"])
        s.store.finish_step(crashed, x["seq"], "done")
    s.store.begin_step(crashed, seq, "cool_and_peel")
    s.store.set_run_status(crashed, "running")
    chip = s.store.get_run(rid)["params"]["chip_ids"][0]
    w = s.lab.world
    ex = s.lab.bench["stations"]["exfoliation_stage"]
    w.objects[chip].pose = Pose(x=ex["x"], y=ex["y"])
    w.objects[chip].on = "exfoliation_stage"
    s.store.upsert_sample(chip, "chip", location="exfoliation_stage")
    s.lab.devices.get("exfoliation_vacuum").set(True)
    s.lab.devices.get("exfoliator").apply()
    [d] = [d for d in plan_recovery(s.store, s.jobs.context_factory, gate=s.gate) if d.run_id == crashed]
    assert (d.action, d.start_seq) == ("resume", seq)
    s.jobs.enqueue(crashed, d.start_seq)
    assert s.jobs.wait(crashed, 60).status == "succeeded"


# ----- inspect, retry, reverify ---------------------------------------------

def test_inspect_bench_reports_regions(services):
    st = run(services, "inspect_bench", {"regions": ["left_storage", "right_storage",
                                                     "exfoliation_stage", "tape"]})
    assert st.status == "succeeded"
    m = st.result[0]["observations"][0]["measurements"]
    assert m["pose_units"] == {"position": "mm", "rotation": "deg"}
    chips = [d for d in m["regions"]["left_storage"] if d["label"] == "chip"]
    assert sorted(d["at"] for d in chips) == [f"left_chip_{i}" for i in range(1, 5)]
    assert len([d for d in m["regions"]["left_storage"] if d["label"] == "carrier"]) == 2
    right = m["regions"]["right_storage"]  # the electrode chip and three stamps
    assert sorted(d["label"] for d in right) == ["chip", "stamp", "stamp", "stamp"]
    assert set(m["empty"]) == {"exfoliation_stage", "tape"}
    assert not [e for e in events(services, "device.command") if e.payload["command"] != "capture"]


def test_retry_after_the_agent_cleared_the_problem(services):
    w = services.lab.world
    put_sample(services, "chip_001", "mid_chip_1")
    t = services.lab.bench["stations"]["transfer_stage"]
    w.objects["chip_002"].pose = Pose(x=t["x"], y=t["y"])  # something left on the stage
    st = run(services, "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert st.status == "parked" and "not empty" in st.steps[0]["note"]
    slot = services.lab.bench["slots"]["left_chip_2"]
    w.objects["chip_002"].pose = Pose(x=slot["x"], y=slot["y"])  # (a human moved it back)
    services.jobs.retry(st.run_id)
    assert services.jobs.wait(st.run_id, 10).status == "succeeded"
    with pytest.raises(ValueError, match="only parked"):
        services.jobs.retry(st.run_id)


def test_reverify_without_moving(services):
    put_sample(services, "chip_001", "mid_chip_1")
    services.lab.world.faults.next_placement = {"dx": 9.0}  # beyond correctable: unclear
    st = run(services, "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert st.status == "parked"
    n_moves = len([e for e in events(services, "device.command")
                   if e.payload["command"] == "move_to"])
    services.jobs.reverify(st.run_id)  # still off: parked again, nothing moved
    assert services.jobs.wait(st.run_id, 10).status == "parked"
    t = services.lab.bench["stations"]["transfer_stage"]
    services.lab.world.objects["chip_001"].pose = Pose(x=t["x"], y=t["y"])  # nudged by hand
    services.jobs.reverify(st.run_id)
    st2 = services.jobs.wait(st.run_id, 10)
    assert st2.status == "succeeded"
    assert services.store.get_steps(st.run_id)[0]["note"] == "re-verified"
    assert n_moves == len([e for e in events(services, "device.command")
                           if e.payload["command"] == "move_to"])
