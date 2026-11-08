"""The lab side as a separate supervised process (spec 5.3, 5.4; workstreams.md
A1 item 6): same JobService interface, heartbeat, restart on exit or freeze,
safe state + recovery on start-up, sim world kept across restarts."""

from __future__ import annotations

import pytest
from conftest import quiet_bench
from fastapi.testclient import TestClient

from qumus2.app import build_lab_side, build_services
from qumus2.core.types import Pose
from qumus2.testbench.persist import save_lab_state
from qumus2.ui.app import create_app

PLACE = ("place_chip_on_transfer_stage", {"chip_id": "chip_001"})


def notes(s):
    return [e.payload["text"] for e in s.store.events_after(0, limit=100_000)
            if e.kind == "notification"]


@pytest.fixture
def proc(tmp_path):
    s = build_services(tmp_path / "qumus.db", seed=1,
                       testbench_config=quiet_bench(), lab_process=True)
    yield s
    s.supervisor.stop()


def test_jobs_and_ui_through_the_lab_process(proc):
    s = proc
    st = s.jobs.wait(s.jobs.submit(*PLACE), 30)
    assert st.status == "succeeded" and st.done
    assert s.store.get_sample("chip_001")["location"] == "transfer_stage"  # shared store
    with pytest.raises(ValueError, match="bad params"):
        s.jobs.submit("exfoliate_chips", {"material": "Gr", "target_temp_c": 55})
    with TestClient(create_app(s, start_driver=False)) as c:
        bench = c.get("/api/bench").json()
        arm = next(d for d in bench["devices"] if d["id"] == "arm_right")
        assert arm["kind"] == "Arm" and set(arm["pose"]) >= {"x", "y", "z"}
        heater = next(d for d in bench["devices"] if d["id"] == "stamp_heater")
        assert heater["temperature"] == pytest.approx(22, abs=1)
        assert any(o["id"] == "chip_001" and o["on"] == "transfer_stage"
                   for o in bench["objects"])
        assert "hang" in [f["fault"] for f in c.get("/api/faults").json()]
        assert c.post("/api/faults", json={"fault": "pick_fail_prob",
                                           "params": {"value": 0.5}}).status_code == 200
        assert c.get("/api/bench").json()["faults"]["pick_fail_prob"] == 0.5
        assert c.post("/api/faults", json={"fault": "nope"}).status_code == 400


def test_killed_lab_process_is_restarted_and_the_run_resumes(proc):
    s = proc
    first = s.supervisor.pid
    s.lab.faults.inject("crash", {"after_calls": 8})
    st = s.jobs.wait(s.jobs.submit(*PLACE), 60)
    assert st.status == "succeeded", st.steps
    assert s.supervisor.pid != first and len(s.supervisor.restarts) == 1
    texts = notes(s)
    assert any("exited with code 137" in t for t in texts)
    assert any("all devices put in their safe state" in t for t in texts)
    notices = [e.payload for e in s.store.events_after(0, limit=100_000) if e.kind == "notification"]
    restarting = next(p for p in notices if p.get("reason") == "lab_restarting")
    assert any(p.get("reason") == "lab_restarted" and p.get("restart_id") == restarting["restart_id"]
               for p in notices)
    assert s.lab.world.snapshot()[0]["on"] == "transfer_stage"


def test_frozen_lab_process_is_restarted_with_the_bench_as_it_was(proc):
    s = proc
    s.supervisor.stale_s = 1.0
    assert s.jobs.wait(s.jobs.submit(*PLACE), 30).status == "succeeded"
    first = s.supervisor.pid
    s.lab.faults.inject("hang")
    s.supervisor.wall.sleep(3.0)
    assert s.supervisor.pid != first
    assert any("stopped responding" in t for t in notes(s))
    chip = next(o for o in s.lab.world.snapshot() if o["id"] == "chip_001")
    assert chip["on"] == "transfer_stage"  # the restarted simulator kept the world
    assert s.store.get_sample("chip_001")["location"] == "transfer_stage"


def test_restarts_stop_after_the_budget(proc):
    s = proc
    s.supervisor.max_restarts = 0
    s.lab.faults.inject("crash", {"after_calls": 1})
    st = s.jobs.wait(s.jobs.submit(*PLACE), 3)
    assert not st.done
    s.supervisor.wall.sleep(1.0)
    assert s.supervisor.gave_up
    assert any("automatic restarts are stopped" in t for t in notes(s))
    assert not s.supervisor._ready.is_set()
    with pytest.raises(ConnectionError):
        s.supervisor.call("status")


def test_hang_needs_a_lab_process(services):
    with pytest.raises(ValueError, match="separate lab process"):
        services.lab.faults.inject("hang")
    assert "hang" not in [f["fault"] for f in services.lab.faults.describe()]


def test_saved_world_is_restored_and_the_ledger_kept(tmp_path):
    db, state = tmp_path / "q.db", tmp_path / "state.pkl"
    a = build_lab_side(db, seed=1, testbench_config=quiet_bench())
    a.gate.arm("arm_right").move_to(Pose(x=50, y=60, z=90))
    a.store.upsert_sample("chip_001", "chip", location="transfer_stage")
    a.lab.world.objects["chip_001"].pose = Pose(x=1, y=2)
    a.clock.sleep(100)
    save_lab_state(a.lab, a.clock, state)

    b = build_lab_side(db, seed=1, testbench_config=quiet_bench(), state_path=state)
    assert b.lab.devices.get("arm_right").get_pose().x == 50
    assert b.lab.world.objects["chip_001"].pose.y == 2
    assert b.clock.now() == a.clock.now()
    assert b.store.get_sample("chip_001")["location"] == "transfer_stage"  # not re-seeded


def test_a_lab_process_left_running_is_stopped_first(tmp_path):
    a = build_services(tmp_path / "q.db", seed=1,
                       testbench_config=quiet_bench(), lab_process=True)
    orphan = a.supervisor.proc  # its app process "died" without stopping it
    a.supervisor._stopping.set()  # monitor off, as if the app were gone
    b = build_services(tmp_path / "q.db", seed=1,
                       testbench_config=quiet_bench(), lab_process=True)
    try:
        assert orphan.wait(10) is not None
        assert b.jobs.wait(b.jobs.submit(*PLACE), 30).status == "succeeded"
    finally:
        b.supervisor.stop()


def test_saved_world_belongs_to_its_database(tmp_path):
    a = build_lab_side(tmp_path / "a.db", seed=1, testbench_config=quiet_bench())
    save_lab_state(a.lab, a.clock, tmp_path / "a.testbench.pkl")
    # a fresh database next to a stale state file is seeded, not restored
    b = build_lab_side(tmp_path / "b.db", seed=1, testbench_config=quiet_bench(),
                       state_path=tmp_path / "a.testbench.pkl")
    assert len(b.store.list_samples()) == len(a.store.list_samples()) > 0


def test_scripted_graphene_device_through_the_lab_process(tmp_path):
    import asyncio

    s = build_services(tmp_path / "qumus.db", scenario="graphene_device", seed=4,
                       lab_process=True)  # seeds 1 and 2 park (test_graphene_scenarios)
    try:
        asyncio.run(s.runtime.send("go"))
        status = {t: r.status for a, t, r in s.runtime.results}
        assert status["submit_design"] == "ok" and status["stack_device"] == "pending"
        assert [d["status"] for d in s.store.list_samples(kind="device")] == ["stacked"]
    finally:
        s.supervisor.stop()
