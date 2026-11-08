"""Offline physical-state tests for final experiment cooling and honest recovery."""

import asyncio
import math
import pickle

import pytest

from qumus2.app import build_lab_side, build_services
from qumus2.core.errors import DangerousError, DeviceUnavailable
from qumus2.flows.library.stacking import CoolTransferStage
from qumus2.flows.library.stage_cooling import STAGES, CoolExperimentStages
from qumus2.runtime.jobs import ThreadJobService
from qumus2.runtime.recovery import decide
from qumus2.testbench.persist import save_lab_state


@pytest.fixture
def cooling(services):
    s = services
    s.store.update_project_state(s.project_id, task_kind="experiment", status="running")
    # Deterministic manual execution still uses the production executor/Steps.
    original = s.jobs
    report = original.shutdown(1.)
    assert report["safe_to_close_resources"] and not any(t.is_alive() for t in original._threads), report
    s.jobs = ThreadJobService(s.store, s.gate, s.jobs.context_factory, workers=0, bench=s.lab.bench)
    s._manual_executor_fixture = True  # This fixture owns synchronous executor calls, never worker execution.
    s.gateway.jobs = s.jobs
    return s


def start(s, temperatures=(90, 170)):
    for heater, temperature in zip(STAGES, temperatures, strict=True):
        d = s.lab.devices.get(heater)
        d._temp, d._t = temperature, s.clock.now()
    result = s.jobs.complete_experiment(s.project_id, "Finished after measured stage cooldown")
    assert result["status"] == "pending"
    rid = result["data"]["job_id"]
    return rid, s.jobs.context_factory(rid, {}, s.project_id)


def execute(s, rid, ctx):
    return s.jobs.executor.execute(rid, ctx)


def test_final_completion_waits_for_both_stages_and_records_actual_readings(cooling):
    s = cooling
    rid, ctx = start(s)
    assert s.store.get_project(s.project_id)["state"]["status"] == "active"
    assert s.jobs.complete_experiment(s.project_id, "premature")["data"]["job_id"] == rid
    assert execute(s, rid, ctx) == "succeeded"
    # Intermediate/job completion cannot declare the experiment complete.
    assert s.store.get_project(s.project_id)["state"]["status"] == "active"
    result = asyncio.run(s.gateway.call("orchestrator", "save_project_status", {"status": "done", "summary": "Completed"}))[0]
    assert result.status == "ok", result
    assert s.store.get_project(s.project_id)["state"]["status"] == "done"
    assert all(r["temperature_c"] < 40 and r["target_c"] is None
               and r["fan_commanded_on"] is False for r in result.data["readings"].values())
    events = s.store.events_after(0, limit=10000)
    polls = [e.payload for e in events if e.kind == "experiment.cooling"]
    assert len(polls) > 3 and polls[0]["readings"]["stamp_heater"]["temperature_c"] == 170
    assert any(e.kind == "experiment.stages_verified" for e in events)


@pytest.mark.parametrize("temperature,expected", [(39.999, "ok"), (40, "unclear"), (40.001, "unclear")])
@pytest.mark.parametrize("heater", list(STAGES))
def test_strict_unrounded_40_degree_boundary(cooling, heater, temperature, expected):
    s = cooling
    rid, ctx = start(s, (22, 22))
    s.lab.devices.get(heater).stuck_at = temperature
    assert CoolExperimentStages().verify(ctx).verdict == expected


@pytest.mark.parametrize("fault", ["stuck", "nan", "infinity", "boolean", "offline", "fan_offline", "missing_fan"])
def test_sensor_and_fan_failures_never_complete(cooling, fault):
    s = cooling
    rid, ctx = start(s)
    heater = s.lab.devices.get("stamp_heater")
    if fault in ("stuck", "nan", "infinity", "boolean"):
        heater.stuck_at = {"stuck": 90, "nan": math.nan, "infinity": math.inf, "boolean": True}[fault]
    elif fault == "offline":
        heater.offline = True
    elif fault == "fan_offline":
        s.lab.devices.get("transfer_stage_fan").offline = True
    else:
        del s.lab.devices.devices["transfer_stage_fan"]
    assert execute(s, rid, ctx) in ("failed", "parked")
    assert s.store.get_project(s.project_id)["state"]["status"] != "done"
    result = s.jobs.complete_experiment(s.project_id, "must not pass")
    assert result["status"] == "needs_attention"
    assert result["data"]["job_id"] == rid  # never silently restart a failed cooling attempt


def test_shutdown_attempts_both_heaters_even_if_one_fails(cooling, monkeypatch):
    s = cooling
    rid, ctx = start(s)
    first = s.lab.devices.get("exfoliation_heater")
    second = s.lab.devices.get("stamp_heater")
    first._target, second._target = 90, 170

    def unavailable():
        raise DeviceUnavailable("Synthetic failed heater shutdown")

    monkeypatch.setattr(first, "off", unavailable)
    assert execute(s, rid, ctx) == "failed"
    assert second.get_target() is None
    assert s.store.get_project(s.project_id)["state"]["status"] != "done"


def test_cooldown_timeout_is_bounded_and_does_not_teleport_temperature(cooling):
    s = cooling
    rid, ctx = start(s)
    ctx.verification["heating"]["cool_timeout_s"] = 5
    assert execute(s, rid, ctx) == "parked"
    assert s.store.get_run(rid)["status"] == "waiting_human"
    assert s.lab.devices.get("stamp_heater").read_temperature() > 40
    assert s.clock.now() <= s.store.get_run(rid)["meta"]["stage_cooling_deadline"] + .01


def test_stop_continue_keeps_deadline_and_never_restores_heat(cooling, monkeypatch):
    s = cooling
    rid, ctx = start(s)
    original_sleep = s.clock.sleep

    def stop_after_poll(seconds):
        original_sleep(seconds)
        ctx.cancel.set()

    monkeypatch.setattr(s.clock, "sleep", stop_after_poll)
    assert execute(s, rid, ctx) == "paused"
    deadline = s.store.get_run(rid)["meta"]["stage_cooling_deadline"]
    assert all(s.lab.devices.get(h).get_target() is None for h in STAGES)
    assert decide(s.store, s.jobs.context_factory, s.store.get_run(rid)).action == "resume"
    monkeypatch.setattr(s.clock, "sleep", original_sleep)
    resumed = s.jobs.context_factory(rid, {}, s.project_id)
    # Even a saved pre-actuation target must not be restored by a cooling flow.
    s.store.update_run_meta(rid, held_heaters={"stamp_heater": 170})
    assert s.jobs.executor.execute(rid, resumed, restore=True) == "succeeded"
    assert s.store.get_run(rid)["meta"]["stage_cooling_deadline"] == deadline
    assert all(s.lab.devices.get(h).get_target() is None for h in STAGES)


def test_expired_deadline_survives_interruption(cooling):
    s = cooling
    rid, ctx = start(s)
    s.store.update_run_meta(rid, stage_cooling_deadline=s.clock.now() - 1)
    assert execute(s, rid, ctx) == "parked"
    assert s.lab.devices.get("stamp_heater").read_temperature() > 40


def test_timeout_includes_time_spent_reading_sensors(cooling, monkeypatch):
    s = cooling
    rid, ctx = start(s, (22, 22))

    def delayed_read():
        s.clock.advance(1801)
        return 22

    monkeypatch.setattr(s.lab.devices.get("stamp_heater"), "read_temperature", delayed_read)
    assert execute(s, rid, ctx) == "parked"
    assert s.store.get_run(rid)["status"] == "waiting_human"


def test_saved_physical_heat_and_cooling_deadline_survive_lab_restart(tmp_path):
    side = build_lab_side(tmp_path / "lab.db")
    pid = side.store.create_project("Restart cooling fixture")
    rid = side.store.create_run("cool_experiment_stages", {}, project_id=pid)
    side.store.begin_step(rid, 0, "cool_experiment_stages")
    side.store.set_run_status(rid, "running")
    deadline = side.clock.now() + 30
    side.store.update_run_meta(rid, stage_cooling_deadline=deadline, held_heaters={"stamp_heater": 170})
    for heater in STAGES:
        side.lab.devices.get(heater)._temp = 90
    path = tmp_path / "physical.pkl"
    save_lab_state(side.lab, side.clock, path)
    restored = build_lab_side(tmp_path / "lab.db", state_path=path)
    assert all(restored.lab.devices.get(h).read_temperature() == 90 for h in STAGES)
    decision = decide(restored.store, restored.jobs.context_factory, restored.store.get_run(rid))
    assert decision.action == "resume" and decision.start_seq == 0
    ctx = restored.jobs.context_factory(rid, {}, pid)
    assert restored.jobs.executor.execute(rid, ctx, restore=True) == "parked"
    assert restored.store.get_run(rid)["meta"]["stage_cooling_deadline"] == deadline
    assert all(restored.lab.devices.get(h).get_target() is None for h in STAGES)


@pytest.mark.parametrize("changed", ["hot", "sensor", "target", "fan", "busy", "plan"])
def test_final_commit_rechecks_live_state_instead_of_trusting_old_success(cooling, changed):
    s = cooling
    rid, ctx = start(s, (22, 22))
    assert execute(s, rid, ctx) == "succeeded"
    heater = s.lab.devices.get("stamp_heater")
    if changed == "hot":
        heater._temp = 60
    elif changed == "sensor":
        heater.stuck_at = math.nan
    elif changed == "target":
        heater._target = 80
    elif changed == "fan":
        s.lab.devices.get("transfer_stage_fan").set(True)
    elif changed == "busy":
        assert s.store.acquire_locks(["stamp_heater"], "other_job")
    else:
        s.store.set_plan(s.project_id, [{"id": "s1", "owner": "hands", "status": "pending"}], author="test")
    result = s.jobs.complete_experiment(s.project_id, "must recheck")
    assert result["status"] != "ok", result
    assert s.store.get_project(s.project_id)["state"]["status"] != "done"


def test_simulated_fans_use_actual_switch_state_and_do_not_retroactively_cool(cooling):
    s = cooling
    for heater_id, fan_id in STAGES.items():
        heater, fan = s.lab.devices.get(heater_id), s.lab.devices.get(fan_id)
        heater._temp, heater._t = 90, s.clock.now()
        s.clock.advance(18)
        fan.set(True)
        assert heater.read_temperature() == pytest.approx(22 + 68 * math.exp(-18 / 180))
        s.clock.advance(40)
        assert heater.read_temperature() == pytest.approx(22 + 68 * math.exp(-18 / 180 - 1))
        fan.stuck_at = False
        current = heater.read_temperature()
        s.clock.advance(40)
        assert heater.read_temperature() == pytest.approx(22 + (current - 22) * math.exp(-40 / 180))
        fan.stuck_at = None
        fan.set(True)
        current = heater.read_temperature()
        s.clock.advance(40)
        fan.safe_state()
        assert heater.read_temperature() == pytest.approx(22 + (current - 22) * math.exp(-1))


def test_intermediate_cooldown_keeps_its_handling_threshold_and_uses_fan(cooling):
    s = cooling
    rid, ctx = start(s, (22, 170))
    CoolTransferStage().act(ctx, None)
    observed = CoolTransferStage().verify(ctx)
    assert observed.verdict == "ok"
    assert 40 < observed.measurements["celsius"] <= ctx.verification["drop"]["safe_handling_c"]
    commands = [e.payload for e in s.store.events_after(0, limit=10000)
                if e.kind == "device.command" and e.payload.get("device") == "transfer_stage_fan"]
    assert [c["params"]["on"] for c in commands] == [True, False]


def test_legacy_checkpoint_binds_new_synthetic_fan_without_changing_saved_heat(tmp_path):
    side = build_lab_side(tmp_path / "lab.db")
    side.lab.devices.get("stamp_heater")._temp = 90
    path = tmp_path / "legacy.pkl"
    save_lab_state(side.lab, side.clock, path)
    state = pickle.loads(path.read_bytes())
    del state["devices"]["transfer_stage_fan"]
    state["devices"]["stamp_heater"]["fan_tau"] = None
    state["devices"]["stamp_heater"]["cooling"] = None
    path.write_bytes(pickle.dumps(state))
    restored = build_lab_side(tmp_path / "lab.db", state_path=path)
    heater = restored.lab.devices.get("stamp_heater")
    assert heater.read_temperature() == 90 and heater.tau == 60
    restored.lab.devices.get("transfer_stage_fan").set(True)
    restored.clock.advance(40)
    assert heater.read_temperature() == pytest.approx(22 + 68 * math.exp(-1))


def test_final_completion_uses_real_lab_process_rpc_and_shared_event_store(tmp_path):
    s = build_services(tmp_path / "lab.db", use_openai=False, lab_process=True)
    try:
        s.store.update_project_state(s.project_id, task_kind="experiment")
        requested = s.jobs.complete_experiment(s.project_id, "Cooling requested")
        assert requested["status"] == "pending"
        assert s.jobs.wait(requested["data"]["job_id"], 30).status == "succeeded"
        completed = s.jobs.complete_experiment(s.project_id, "Measured cold in the lab process")
        assert completed["status"] == "ok", completed
        assert s.store.get_project(s.project_id)["state"]["status"] == "done"
        assert all(r["temperature_c"] < 40 for r in completed["data"]["readings"].values())
        assert any(e.kind == "experiment.stages_verified" for e in s.store.events_after(0))
        assert not any(e.kind == "agent.request" for e in s.store.events_after(0))
    finally:
        s.supervisor.stop()


def test_final_commit_refuses_worker_lock_release_race(cooling, monkeypatch):
    s = cooling
    rid, ctx = start(s, (22, 22))
    assert execute(s, rid, ctx) == "succeeded"
    acquire = s.store.acquire_locks

    def worker_releases_before_transaction(devices, run_id):
        acquired = acquire(devices, run_id)
        s.store.release_locks(run_id)
        return acquired

    monkeypatch.setattr(s.store, "acquire_locks", worker_releases_before_transaction)
    assert s.jobs.complete_experiment(s.project_id, "must own locks")["status"] == "needs_attention"
    assert s.store.get_project(s.project_id)["state"]["status"] != "done"


@pytest.mark.parametrize("failure", ["exception", "ignored_command"])
def test_fan_shutdown_failure_still_attempts_the_other_fan_and_blocks_completion(cooling, monkeypatch, failure):
    s = cooling
    rid, ctx = start(s, (22, 22))
    first = s.lab.devices.get("exfoliation_fan")
    original_set = first.set

    def fail_to_stop(on):
        if on:
            return original_set(on)
        if failure == "exception":
            raise DeviceUnavailable("Synthetic fan shutdown failure")

    monkeypatch.setattr(first, "set", fail_to_stop)
    assert execute(s, rid, ctx) == "parked"
    assert s.store.get_run(rid)["status"] == "waiting_human"
    assert first.get() is True
    assert s.lab.devices.get("transfer_stage_fan").get() is False
    assert all(s.lab.devices.get(h).get_target() is None for h in STAGES)
    result = s.jobs.complete_experiment(s.project_id, "must not complete")
    assert result["status"] == "needs_attention" and result["data"]["job_id"] == rid
    assert s.store.get_project(s.project_id)["state"]["status"] != "done"


def test_failure_safe_state_confirms_heater_off_and_attempts_both_outputs(cooling, monkeypatch):
    s = cooling
    rid, ctx = start(s)
    first = s.lab.devices.get("exfoliation_heater")
    second = s.lab.devices.get("stamp_heater")
    first._target, second._target = 90, 170
    monkeypatch.setattr(first, "off", lambda: None)
    with pytest.raises(DangerousError) as error:
        CoolExperimentStages().safe_state(ctx)
    assert error.value.details["failed_devices"] == ["exfoliation_heater"]
    assert first.get_target() == 90 and second.get_target() is None
