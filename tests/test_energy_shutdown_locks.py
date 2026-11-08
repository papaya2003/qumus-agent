"""Failed energy shutdown keeps the existing device locks across every recovery entry."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from test_stage_cooling import cooling as cooling
from test_stage_cooling import execute, start

from qumus2.app import build_lab_side
from qumus2.config import simulation_profile
from qumus2.core.errors import DangerousError, DeviceUnavailable, StopRequested
from qumus2.flows.flow import FLOWS, Flow
from qumus2.flows.library.stacking import (
    PROCEDURE_ID,
    RELEASE_POLICY_ID,
    TRANSFER_PROFILE_ID,
    RefindFlake,
    StackDeviceFlow,
    StackParams,
)
from qumus2.flows.library.stage_cooling import STAGE_DEVICES, CoolExperimentStages
from qumus2.flows.library.transfer import MoveObject
from qumus2.flows.step import Observation, ProbeResult, Step, Verdict
from qumus2.runtime.jobs import ThreadJobService
from qumus2.runtime.recovery import decide, park
from qumus2.testbench.persist import save_lab_state


def failed_shutdown(s, monkeypatch, failure="exception"):
    rid, ctx = start(s)
    first = s.lab.devices.get("exfoliation_heater")
    first._target = 90
    original_off = first.off

    def fail_off():
        if failure == "exception":
            raise DeviceUnavailable("Synthetic energy shutdown failure")

    monkeypatch.setattr(first, "off", fail_off)
    assert execute(s, rid, ctx) == "failed"
    return rid, ctx, original_off


def assert_output_retained(s, rid):
    assert s.store.get_run(rid)["error"]["safe_shutdown_failed"] == ["exfoliation_heater"]
    assert s.store.locks()["exfoliation_heater"] == rid
    assert not s.store.acquire_locks(["exfoliation_heater"], "competing_fixture_job")
    assert s.lab.devices.get("exfoliation_heater").get_target() == 90


@pytest.mark.parametrize("failure", ["exception", "ignored_off"])
def test_dangerous_shutdown_retains_failed_output_and_other_flow_locks(cooling, monkeypatch, failure):
    s = cooling
    rid, ctx, _ = failed_shutdown(s, monkeypatch, failure)
    assert_output_retained(s, rid)
    assert s.store.locks() == {device: rid for device in STAGE_DEVICES}
    assert s.store.get_run(rid)["status"] == "waiting_human"
    assert s.jobs.complete_experiment(s.project_id, "unsafe")["status"] == "needs_attention"


@pytest.mark.parametrize("entry", ["retry", "resume", "execute", "reverify", "restart_running", "restart_queued"])
def test_pending_shutdown_cannot_restore_or_check_process_steps(cooling, monkeypatch, entry):
    s = cooling
    rid, ctx, _ = failed_shutdown(s, monkeypatch)

    def forbidden(*args):
        raise AssertionError("Unconfirmed energy shutdown must not invoke current process Steps")

    monkeypatch.setattr(CoolExperimentStages, "act", forbidden)
    monkeypatch.setattr(CoolExperimentStages, "verify", forbidden)
    monkeypatch.setattr(CoolExperimentStages, "probe_state", forbidden)
    monkeypatch.setattr(CoolExperimentStages, "before_park", forbidden)
    if entry == "retry":
        s.store.set_run_status(rid, "parked")
        with pytest.raises(ValueError, match="unconfirmed energy shutdown"):
            s.jobs.retry(rid)
    elif entry == "resume":
        s.store.set_run_status(rid, "paused")
        with pytest.raises(ValueError, match="unconfirmed energy shutdown"):
            s.jobs.resume(rid)
    elif entry == "execute":
        s.store.set_run_status(rid, "queued")
        assert s.jobs.executor.execute(rid, ctx, restore=True) == "parked"
    elif entry == "reverify":
        assert s.jobs.executor.reverify(rid, ctx) == "parked"
    else:
        s.store.set_run_status(rid, "queued" if entry.endswith("queued") else "running")
        [decision] = s.jobs.recover()
        assert decision.action == "park"
    assert_output_retained(s, rid)


def test_explicit_shutdown_reverification_releases_only_after_confirmed_off(cooling, monkeypatch):
    s = cooling
    rid, ctx, original_off = failed_shutdown(s, monkeypatch)
    s.jobs.reverify(rid)
    queued, _, mode = s.jobs._q.get_nowait()  # initial ordinary submission
    assert queued == rid and mode == "run"
    assert s.jobs._q.get_nowait() == (rid, 0, "reverify")
    assert s.jobs.executor.reverify(rid, ctx) == "parked"
    assert_output_retained(s, rid)
    steps = s.store.get_steps(rid)
    monkeypatch.setattr(s.lab.devices.get("exfoliation_heater"), "off", original_off)

    def forbidden(*args):
        raise AssertionError("Safe shutdown re-verification cannot become process verification")

    monkeypatch.setattr(CoolExperimentStages, "verify", forbidden)
    assert s.jobs.executor.reverify(rid, ctx) == "parked"
    assert s.lab.devices.get("exfoliation_heater").get_target() is None
    assert s.store.locks() == {}
    assert not s.store.get_run(rid)["error"]["safe_shutdown_failed"]
    assert s.store.get_run(rid)["status"] == "waiting_human"
    assert s.store.get_steps(rid) == steps  # original failure remains unfinished
    assert any(e.kind == "job.shutdown_verified" for e in s.store.events_after(0, limit=10000))
    with pytest.raises(ValueError, match="only parked jobs"):
        s.jobs.retry(rid)


@pytest.mark.parametrize("sensor_failure", [False, True])
def test_failed_or_unclear_process_reverify_cannot_release_new_shutdown_fault(cooling, monkeypatch, sensor_failure):
    s = cooling
    rid, ctx = start(s)
    first = s.lab.devices.get("exfoliation_heater")
    first._target = 90
    if sensor_failure:
        first.stuck_at = float("nan")
    monkeypatch.setattr(first, "off", lambda: None)
    s.store.set_run_status(rid, "parked")
    assert s.jobs.executor.reverify(rid, ctx) == "parked"
    assert_output_retained(s, rid)
    assert s.store.get_run(rid)["status"] == "waiting_human"


def test_stop_before_first_actuation_cannot_release_failed_heater(cooling, monkeypatch):
    s = cooling
    rid, ctx = start(s)
    s.lab.devices.get("exfoliation_heater")._target = 90
    monkeypatch.setattr(s.lab.devices.get("exfoliation_heater"), "off", lambda: None)
    ctx.cancel.set()
    assert execute(s, rid, ctx) == "parked"
    assert_output_retained(s, rid)


def test_cancelling_queued_cooldown_does_not_disable_another_jobs_heater(cooling):
    s = cooling
    rid, ctx = start(s)
    assert s.store.acquire_locks(["stamp_heater"], "active_process_fixture")
    s.lab.devices.get("stamp_heater")._target = 170
    ctx.cancel.set()
    assert execute(s, rid, ctx) == "paused"
    assert s.lab.devices.get("stamp_heater").get_target() == 170
    assert s.store.locks()["stamp_heater"] == "active_process_fixture"
    assert "stamp_heater" not in s.store.get_run(rid)["meta"]["held_heaters"]


@pytest.mark.parametrize("failure", ["exception", "ignored_off", "before_park_failure"])
def test_first_restart_parking_detects_shutdown_failure_before_releasing_locks(cooling, monkeypatch, failure):
    s = cooling
    rid, ctx = start(s)
    s.lab.devices.get("exfoliation_heater")._target = 90
    assert s.store.acquire_locks(STAGE_DEVICES, rid)
    s.store.begin_step(rid, 0, "cool_experiment_stages", safe_to_resume=False)
    s.store.set_run_status(rid, "running")

    def fail_off():
        if failure == "exception":
            raise DeviceUnavailable("Synthetic restart shutdown failure")

    monkeypatch.setattr(s.lab.devices.get("exfoliation_heater"), "off", fail_off)
    if failure == "before_park_failure":
        def failed_before_park(*args):
            raise DeviceUnavailable("Synthetic cleanup callback failure")
        monkeypatch.setattr(CoolExperimentStages, "before_park", failed_before_park)
    decision = decide(s.store, s.jobs.context_factory, s.store.get_run(rid))
    assert decision.action == "park" and "exfoliation_heater" in s.store.locks()
    park(s.store, s.jobs.context_factory, decision, gate=s.gate)
    assert_output_retained(s, rid)
    assert s.store.get_run(rid)["status"] == "waiting_human"
    with pytest.raises(ValueError, match="unconfirmed energy shutdown"):
        s.jobs.retry(rid)


def test_disappearing_failed_output_is_not_proof_of_safe_shutdown(cooling, monkeypatch):
    s = cooling
    rid, ctx, _ = failed_shutdown(s, monkeypatch)
    del s.lab.devices.devices["exfoliation_heater"]
    assert s.jobs.executor.reverify(rid, ctx) == "parked"
    assert s.store.locks()["exfoliation_heater"] == rid
    assert s.store.get_run(rid)["error"]["safe_shutdown_failed"] == ["exfoliation_heater"]


def test_watchdog_failure_retains_unconfirmed_outputs(cooling, monkeypatch):
    s = cooling
    rid, ctx = start(s)
    s.lab.devices.get("exfoliation_heater")._target = 90
    monkeypatch.setattr(s.lab.devices.get("exfoliation_heater"), "off", lambda: None)

    def watchdog_failure():
        raise DangerousError("Synthetic watchdog fault")

    monkeypatch.setattr(s.gate, "watchdog_tick", watchdog_failure)
    assert execute(s, rid, ctx) == "failed"
    assert_output_retained(s, rid)


def test_failed_output_locks_and_error_survive_reopened_lab_state(tmp_path, monkeypatch):
    db = tmp_path / "lab.db"
    side = build_lab_side(db)
    pid = side.store.create_project("Persisted shutdown fixture")
    rid = side.store.create_run("cool_experiment_stages", {}, project_id=pid)
    first = side.lab.devices.get("exfoliation_heater")
    first._target = first._temp = 90
    monkeypatch.setattr(first, "off", lambda: None)
    ctx = side.jobs.context_factory(rid, {}, pid)
    assert side.jobs.executor.execute(rid, ctx) == "failed"
    snapshot = tmp_path / "lab.pkl"
    save_lab_state(side.lab, side.clock, snapshot)
    # Simulate a restart with a pending retry tag; the persisted failure must
    # be checked before ordinary queued-job reconstruction or stale lock cleanup.
    side.store.set_run_status(rid, "queued")
    restored = build_lab_side(db, state_path=snapshot)
    [decision] = restored.jobs.recover()
    assert decision.action == "park"
    assert restored.store.get_run(rid)["status"] == "waiting_human"
    assert restored.store.locks()["exfoliation_heater"] == rid
    assert restored.lab.devices.get("exfoliation_heater").get_target() == 90
    with pytest.raises(ValueError, match="unconfirmed energy shutdown"):
        restored.jobs.retry(rid)


@pytest.mark.parametrize("shutdown_fails", [False, True])
@pytest.mark.parametrize("checkpoint", ["refind", "load_stamp"])
def test_stacking_restart_at_optical_checkpoint_checks_previous_step_heating(cooling, monkeypatch, shutdown_fails, checkpoint):
    s = cooling
    # Explicit checkpoint-only fixture: build the real stacking Steps without
    # submitting or claiming a material pickup, image or successful device job.
    params = StackParams(flake_ids=["fixture_flake"], target_chip_id="chip_e01", target_xy_mm=(1, 1),
        procedure_contract=PROCEDURE_ID, transfer_profile=TRANSFER_PROFILE_ID, release_policy=RELEASE_POLICY_ID,
        stamp_id="stamp_01", stamp_slot=s.store.get_sample("stamp_01")["location"],
        flake_chips={"fixture_flake": "chip_001"}, materials={"fixture_flake": "hBN"},
        home_slots={"chip_001": "mid_chip_1", "chip_e01": s.store.get_sample("chip_e01")["location"]})
    flow = StackDeviceFlow()
    flow._bench = s.lab.bench
    steps = flow.build_steps(params)
    checkpoint_type = RefindFlake if checkpoint == "refind" else MoveObject
    seq = next(i for i, step in enumerate(steps) if isinstance(step, checkpoint_type))
    assert "stamp_heater" not in steps[seq].devices
    rid = s.store.create_run(flow.name, params.model_dump(), project_id=s.project_id)
    s.store.update_run_meta(rid, workflow=flow.build_workflow(params).manifest())
    s.store.begin_step(rid, seq, steps[seq].name, safe_to_resume=True)
    s.store.set_run_status(rid, "running")
    assert s.store.acquire_locks(flow.devices, rid)
    heater = s.lab.devices.get("stamp_heater")
    heater._target = heater._temp = 90
    if checkpoint == "refind":
        monkeypatch.setattr(RefindFlake, "probe_state", lambda *args: ProbeResult.UNKNOWN)
    else:
        s.lab.devices.get("overview_camera").offline = True  # normal probe failure, no forced unsafe checkpoint
    if shutdown_fails:
        monkeypatch.setattr(heater, "off", lambda: None)
    decision = decide(s.store, s.jobs.context_factory, s.store.get_run(rid))
    assert decision.action == "park" and s.store.locks()["stamp_heater"] == rid
    park(s.store, s.jobs.context_factory, decision, gate=s.gate)
    assert s.store.get_run(rid)["status"] == "waiting_human"
    if shutdown_fails:
        assert heater.get_target() == 90
        assert s.store.get_run(rid)["error"]["safe_shutdown_failed"] == ["stamp_heater"]
        assert s.store.locks()["stamp_heater"] == rid
        assert not s.store.acquire_locks(["stamp_heater"], "competing_fixture_job")
    else:
        assert heater.get_target() is None
        assert rid not in s.store.locks().values()


@pytest.mark.parametrize("failure_type", [DeviceUnavailable, RuntimeError])
@pytest.mark.parametrize("pause_point", ["before_act", "inside_step"])
def test_unavailable_saved_target_does_not_prevent_off_or_unlock_unverified_output(cooling, monkeypatch, failure_type, pause_point):
    s = cooling
    rid, ctx = start(s)
    first = s.lab.devices.get("exfoliation_heater")
    second = s.lab.devices.get("stamp_heater")
    first._target, second._target = 90, 170
    feedback_failed = [pause_point == "before_act"]

    def target_feedback():
        if feedback_failed[0]:
            raise failure_type("Synthetic unavailable setpoint feedback")
        return first._target

    def stop_at_step(*args):
        feedback_failed[0] = True
        raise StopRequested("Synthetic Stop at checkpoint")

    monkeypatch.setattr(first, "get_target", target_feedback)
    s.jobs.executor.runner = SimpleNamespace(run=stop_at_step)
    if pause_point == "before_act":
        ctx.cancel.set()
    assert execute(s, rid, ctx) == "parked"
    assert first._target is None and second.get_target() is None  # both off attempts still ran
    assert s.store.get_run(rid)["status"] == "waiting_human"
    assert s.store.get_run(rid)["error"]["safe_shutdown_failed"] == ["exfoliation_heater"]
    assert s.store.locks()["exfoliation_heater"] == rid  # off command alone cannot prove off feedback
    assert "exfoliation_heater" not in s.store.get_run(rid)["meta"]["held_heaters"]


@pytest.mark.parametrize("entry", ["executor", "worker"])
def test_unexpected_exception_guards_running_job_and_worker_preserves_failure_marker(cooling, monkeypatch, entry):
    s = cooling
    rid, ctx = start(s)
    first = s.lab.devices.get("exfoliation_heater")
    first._target = 90
    monkeypatch.setattr(first, "off", lambda: None)

    def unexpected_failure(*args):
        raise RuntimeError("Synthetic exception before a terminal status write")

    if entry == "executor":
        s.jobs.executor.runner = SimpleNamespace(run=unexpected_failure)
        with pytest.raises(RuntimeError, match="Synthetic exception"):
            execute(s, rid, ctx)
    else:
        worker = ThreadJobService(s.store, s.gate, s.jobs.context_factory, workers=1, bench=s.lab.bench)
        worker.executor.runner = SimpleNamespace(run=unexpected_failure)
        worker.enqueue(rid)
        assert worker.wait(rid, 5).status == "waiting_human"
    assert_output_retained(s, rid)
    assert s.store.get_run(rid)["status"] == "waiting_human"


@pytest.mark.parametrize("feedback_trusted", [True, False])
def test_real_crash_callback_restores_known_target_only_after_safe_recovery(services, monkeypatch, feedback_trusted):
    s = services
    heater = s.lab.devices.get("stamp_heater")
    armed = [True]
    resumed = []
    probes = []

    class Params(BaseModel):
        pass

    class Heat(Step):
        name = "heat_resume_fixture"
        devices = ("stamp_heater",)

        def act(self, ctx, correction):
            ctx.gate.heater("stamp_heater").set_target(60)
            ctx.clock.sleep(240)

        def verify(self, ctx):
            return Observation(Verdict.OK)

    class CheckAfterCrash(Step):
        name = "check_after_crash"
        devices = ("stamp_heater",)

        def act(self, ctx, correction):
            if armed[0]:
                armed[0] = False
                if not feedback_trusted:
                    def unavailable():
                        raise DeviceUnavailable("Synthetic target readback failed before crash shutdown")
                    monkeypatch.setattr(heater, "get_target", unavailable)
                s.lab.faults.inject("crash", {"after_calls": 1})
                ctx.gate.heater("stamp_heater").read_temperature()
            h = ctx.gate.heater("stamp_heater")
            resumed.append((h.get_target(), h.read_temperature()))

        def verify(self, ctx):
            return Observation(Verdict.OK)

        def probe_state(self, ctx):
            probes.append(True)
            ctx.clock.sleep(240)  # simulated inspection latency after safe off
            return ProbeResult.NOT_DONE

    class HeatedFixture(Flow):
        name = "known_target_crash_fixture"
        devices = ("stamp_heater",)

        def build_steps(self, params):
            return [Heat(), CheckAfterCrash()]

    HeatedFixture.Params = Params
    monkeypatch.setitem(FLOWS, HeatedFixture.name, HeatedFixture)
    rid = s.jobs.submit(HeatedFixture.name, {}, project_id=s.project_id)
    assert s.lab.faults.crashed.wait(5)
    status = s.jobs.wait(rid, 5)
    if feedback_trusted:
        assert status.status == "succeeded", status
        assert probes and resumed and resumed[0][0] == 60 and resumed[0][1] >= 58
        assert s.store.get_run(rid)["meta"]["held_heaters"] == {}
    else:
        assert status.status == "waiting_human"
        assert not probes and not resumed
        assert s.store.get_run(rid)["error"]["safe_shutdown_failed"] == ["stamp_heater"]
        assert s.store.locks()["stamp_heater"] == rid
        assert "stamp_heater" not in s.store.get_run(rid)["meta"]["held_heaters"]
        targets = [e.payload["params"]["celsius"] for e in s.store.events_after(0, limit=10000, run_id=rid)
                   if e.kind == "device.command" and e.payload.get("command") == "set_target"]
        assert targets == [60]  # no target restoration from untrusted feedback


def test_read_only_reactor_recovery_never_restores_old_held_target(monkeypatch):
    from test_reactor_acquisition import CHIP, ready

    from qumus2.app import build_services

    s = build_services(testbench_config=simulation_profile(), use_openai=False)
    ready(s)
    rid = s.store.create_run("verify_reactor_state", {"chip_id": CHIP}, project_id=s.project_id)
    s.store.update_run_meta(rid, held_heaters={"reactor_heater": 300})
    ctx = s.jobs.context_factory(rid, {"chip_id": CHIP}, s.project_id)
    assert s.jobs.executor.execute(rid, ctx, restore=True) == "succeeded"
    assert s.lab.devices.get("reactor_heater").get_target() is None
    assert not any(e.kind == "device.command" for e in s.store.events_after(0, limit=10000, run_id=rid))
