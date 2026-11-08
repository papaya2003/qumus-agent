"""Default `serve` (testbench in this process) restarts with a fresh bench and
ledger, so jobs the last process left open are closed, not left "running"."""

from __future__ import annotations

from conftest import quiet_bench

from qumus2.app import build_services


def test_restart_closes_jobs_of_the_old_bench(tmp_path):
    db = tmp_path / "q.db"
    s = build_services(db, seed=1, testbench_config=quiet_bench())
    done = s.jobs.wait(s.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_001"}),
                       600)
    assert done.status == "succeeded"
    cut = s.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_002"})
    s.jobs.wait(cut, 600)
    s.store.set_run_status(cut, "running")  # as if the process died during it
    s.store.acquire_locks(["arm_left"], cut)

    s2 = build_services(db, seed=1, testbench_config=quiet_bench())
    assert s2.store.get_sample("chip_001")["location"] == "left_chip_1"  # fresh ledger
    assert s2.store.get_run(cut)["status"] == "cancelled"
    assert s2.store.get_run(done.run_id)["status"] == "succeeded"
    assert not s2.store.locks()
    notes = [e.payload["text"] for e in s2.store.events_after(0, limit=100_000)
             if e.kind == "notification" and e.payload["level"] == "warn"]
    assert any(cut in t and "--lab-process" in t for t in notes)
    # nothing is left for the agents to wait on, and new work runs
    assert s2.jobs.wait(s2.jobs.submit("place_chip_on_transfer_stage",
                                       {"chip_id": "chip_003"}), 600).status == "succeeded"
