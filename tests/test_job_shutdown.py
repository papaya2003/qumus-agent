"""Local disposal preserves normal safety cleanup and bounds resource lifetime."""
from __future__ import annotations

import asyncio
import gc
import threading
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from qumus2.app import build_services
from qumus2.flows.flow import FLOWS, Flow
from qumus2.flows.step import Observation, Step, Verdict
from tests.conftest import quiet_bench


@pytest.fixture
def controlled_flow(monkeypatch):
    control = SimpleNamespace(started=threading.Event(), release=threading.Event(),
        finished=threading.Event(), parked=threading.Event(), ignore_cancel=False, acts=[])

    class Params(BaseModel):
        label: str = "first"

    class ControlledStep(Step):
        name = "controlled_shutdown_boundary"

        def act(self, ctx, correction):
            control.acts.append(ctx.params["label"])
            ctx.gate.heater("stamp_heater").set_target(40.)
            control.started.set()
            try:
                while not control.release.wait(.005):
                    if not control.ignore_cancel:
                        ctx.check_cancel()
                ctx.check_cancel()
            finally:
                control.finished.set()

        def verify(self, ctx):
            return Observation(Verdict.OK)

        def before_park(self, ctx):
            control.parked.set()

    class ControlledFlow(Flow):
        name = "shutdown_test_boundary"
        devices = ("stamp_heater",)

        def build_steps(self, params):
            return [ControlledStep()]

    ControlledFlow.Params = Params
    monkeypatch.setitem(FLOWS, ControlledFlow.name, ControlledFlow)
    return control, ControlledFlow.name


def new_services():
    return build_services(":memory:", seed=1, testbench_config=quiet_bench())


def test_idle_repeated_disposal_joins_workers_without_changing_ledger():
    for _ in range(4):
        s = new_services()
        before = s.store.list_samples(), s.store.last_event_id()
        result = s.jobs.shutdown(.5)
        assert result["closed"] and result["safe_to_close_resources"], result
        assert result["alive_workers"] == [] and result["unsettled_jobs"] == []
        assert all(not thread.is_alive() for thread in s.jobs._threads)
        assert (s.store.list_samples(), s.store.last_event_id()) == before
        assert s.jobs.shutdown(0)["safe_to_close_resources"]
        asyncio.run(s.close_local(.5))


@pytest.mark.parametrize("operation", ["submit", "enqueue", "recover", "retry", "resume", "resume_all", "reverify"])
def test_closed_service_rejects_admission_before_any_store_mutation(operation):
    s = new_services()
    try:
        assert s.jobs.shutdown(.5)["closed"]
        mark = s.store.last_event_id()
        args = {"submit": ("shutdown_test_boundary", {}), "enqueue": ("unknown",), "retry": ("unknown",),
                "resume": ("unknown",), "reverify": ("unknown",)}.get(operation, ())
        with pytest.raises(ValueError, match="shutting down"):
            getattr(s.jobs, operation)(*args)
        assert s.store.last_event_id() == mark
    finally:
        asyncio.run(s.close_local(.5))


def test_active_and_queued_jobs_use_normal_stop_and_energy_finally(controlled_flow):
    control, flow = controlled_flow
    s = new_services()
    try:
        first = s.jobs.submit(flow, {})
        assert control.started.wait(.5)
        second = s.jobs.submit(flow, {"label": "queued"})
        result = s.jobs.shutdown(.5)
        assert result["safe_to_close_resources"], result
        assert control.finished.is_set() and control.parked.is_set()
        assert control.acts == ["first"]
        assert [s.store.get_run(rid)["status"] for rid in (first, second)] == ["paused", "paused"]
        assert s.store.get_run(first)["meta"]["held_heaters"] == {"stamp_heater": 40.}
        assert s.lab.devices.get("stamp_heater").get_target() is None
        assert s.store.locks() == {}
        assert not any(row["status"] == "done" for row in s.store.get_steps(first))
        assert all(done.is_set() for done in s.jobs._done.values())
    finally:
        control.release.set()
        asyncio.run(s.close_local(.5))


def test_timeout_retains_store_and_image_owner_until_later_settlement(controlled_flow):
    control, flow = controlled_flow
    control.ignore_cancel = True
    s = new_services()
    s._diagnostic_images = TemporaryDirectory(prefix="qumus-shutdown-fixture-")
    from pathlib import Path
    images = Path(s._diagnostic_images.name)
    try:
        rid = s.jobs.submit(flow, {})
        assert control.started.wait(.5)
        started = time.monotonic()
        result = asyncio.run(s.close_local(.02))
        assert time.monotonic()-started < .5
        assert not result["safe_to_close_resources"] and rid in result["unsettled_jobs"]
        assert s.jobs._shutdown_owner is s and images.is_dir()
        assert s.store.get_run(rid)["status"] == "running"
        gc.collect()
        assert images.is_dir()
        with pytest.raises(ValueError, match="shutting down"):
            s.jobs.enqueue(rid)
        control.release.set()
        result = asyncio.run(s.close_local(.5))
        assert result["safe_to_close_resources"], result
        assert not images.exists() and s.jobs._shutdown_owner is None
        assert not any(thread.is_alive() for thread in s.jobs._threads)
    finally:
        control.release.set()
        asyncio.run(s.close_local(.5))


def test_blocked_stop_is_bounded_and_not_invoked_twice(controlled_flow, monkeypatch):
    control, flow = controlled_flow
    s = new_services()
    stop_release, calls = threading.Event(), []
    original = s.jobs.stop_all
    def stop():
        calls.append(1)
        stop_release.wait()
        original()
    monkeypatch.setattr(s.jobs, "stop_all", stop)
    try:
        rid = s.jobs.submit(flow, {})
        assert control.started.wait(.5)
        result = s.jobs.shutdown(.02)
        assert result["stop_pending"] and not result["safe_to_close_resources"]
        assert s.store.get_run(rid)["status"] == "running"
        stop_release.set()
        assert s.jobs.shutdown(.5)["safe_to_close_resources"]
        assert calls == [1]
    finally:
        stop_release.set()
        control.release.set()
        asyncio.run(s.close_local(.5))


def test_durable_failed_energy_shutdown_is_not_released_or_closed():
    s = new_services()
    try:
        rid = s.store.create_run("shutdown_fixture", {})
        s.store.set_run_status(rid, "waiting_human", error={"safe_shutdown_failed": ["stamp_heater"]})
        s.store.acquire_locks(["stamp_heater"], rid)
        s.jobs._done[rid] = threading.Event()
        s.jobs._done[rid].set()
        result = asyncio.run(s.close_local(.5))
        assert result["closed"] and not result["safe_to_close_resources"]
        assert result["unconfirmed_energy_jobs"] == [rid]
        assert s.store.get_run(rid)["status"] == "waiting_human"
        assert s.store.locks() == {"stamp_heater": rid}
        assert s.jobs._shutdown_owner is s
    finally:
        # The synthetic fixture's dead workers are proved above; no hold is acknowledged.
        s.jobs._shutdown_owner = None
        s.store.close()


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True, "1", None])
def test_invalid_shutdown_deadline_is_refused_without_stopping_admission(timeout):
    s = new_services()
    try:
        with pytest.raises(ValueError, match="finite"):
            s.jobs.shutdown(timeout)
        with pytest.raises(ValueError, match="finite"):
            asyncio.run(s.close_local(timeout))
        assert not s.jobs._closing
    finally:
        asyncio.run(s.close_local(.5))


def test_runtime_close_cancellation_propagates_and_keeps_resources(monkeypatch):
    s = new_services()
    original = s.runtime.close

    async def scenario():
        started = asyncio.Event()
        async def close():
            started.set()
            await asyncio.Future()
        monkeypatch.setattr(s.runtime, "close", close)
        task = asyncio.create_task(s.close_local(.5))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert s.jobs._shutdown_owner is s
        assert s.store.list_samples()
        assert not getattr(s, "_local_resources_closed", False)
        monkeypatch.setattr(s.runtime, "close", original)
        assert (await s.close_local(.5))["safe_to_close_resources"]

    asyncio.run(scenario())


def test_manual_queue_entries_are_not_executed_or_fabricated_as_settled(monkeypatch):
    from qumus2.runtime.jobs import ThreadJobService

    s = new_services()
    original = s.jobs
    assert original.shutdown(.5)["closed"]
    s.jobs = ThreadJobService(s.store, s.gate, original.context_factory, workers=0, bench=s.lab.bench)
    monkeypatch.setattr(s.jobs, "stop_all", lambda: pytest.fail("Manual execution has no worker Stop acknowledgement"))
    rid = s.store.create_run("manual_fixture", {})
    s.jobs.enqueue(rid)
    result = asyncio.run(s.close_local(.5))
    assert result["manual_execution"] and not result["closed"] and not result["safe_to_close_resources"]
    assert result["unsettled_jobs"] == [rid] and not result["alive_workers"] and not result["stop_pending"]
    assert not s.jobs._done[rid].is_set()
    assert s.store.get_run(rid)["status"] == "queued"
    assert s.jobs._shutdown_owner is s
    # Explicit synchronous fixture ownership: no executor was started.
    s.jobs._shutdown_owner = None
    s.store.close()


def test_scheduled_crash_callback_after_disposal_never_recovers_or_reads_store(monkeypatch):
    s = new_services()
    callback = s.lab.faults.on_crash
    assert asyncio.run(s.close_local(.5))["safe_to_close_resources"]
    monkeypatch.setattr(s.jobs, "recover", lambda: pytest.fail("Disposed crash callback must not recover"))
    callback()  # The original scheduler can still deliver its already-created callback.


def test_runtime_close_failure_retains_resources_and_allows_explicit_later_close(monkeypatch):
    s = new_services()
    original = s.runtime.close
    async def fail():
        raise RuntimeError("Synthetic runtime disposal failure")
    monkeypatch.setattr(s.runtime, "close", fail)
    report = asyncio.run(s.close_local(.5))
    assert report["closed"] and not report["safe_to_close_resources"]
    assert "Synthetic runtime disposal failure" in report["runtime_close_error"]
    assert s.jobs._shutdown_owner is s and s.store.list_samples()
    monkeypatch.setattr(s.runtime, "close", original)
    assert asyncio.run(s.close_local(.5))["safe_to_close_resources"]


def test_concurrent_service_close_does_not_duplicate_runtime_or_store_disposal(monkeypatch):
    s = new_services()
    async def scenario():
        started, release, calls = asyncio.Event(), asyncio.Event(), []
        async def close():
            calls.append(1)
            started.set()
            await release.wait()
        monkeypatch.setattr(s.runtime, "close", close)
        task = asyncio.create_task(s.close_local(.5))
        await started.wait()
        second = await s.close_local(.5)
        assert second["close_pending"] and not second["safe_to_close_resources"]
        assert s.store.list_samples() and s.jobs._shutdown_owner is s
        release.set()
        assert (await task)["safe_to_close_resources"]
        assert (await s.close_local(.5))["safe_to_close_resources"]
        assert calls == [1]
    asyncio.run(scenario())


def test_final_shutdown_evidence_wait_respects_store_lock_deadline():
    s = new_services()
    locked, release = threading.Event(), threading.Event()
    rid = s.store.create_run("finished_evidence_fixture", {})
    s.store.set_run_status(rid, "succeeded")
    s.jobs._done[rid] = threading.Event()
    s.jobs._done[rid].set()
    def hold():
        with s.store._lock:
            locked.set()
            release.wait()
    holder = threading.Thread(target=hold)
    holder.start()
    assert locked.wait(.5)
    try:
        started = time.monotonic()
        result = s.jobs.shutdown(.02)
        assert time.monotonic()-started < .5
        assert result["closed"] and result["evidence_pending"] and not result["safe_to_close_resources"]
    finally:
        release.set()
        holder.join(.5)
        assert asyncio.run(s.close_local(.5))["safe_to_close_resources"]


def test_store_disposal_after_runtime_close_uses_remaining_deadline(monkeypatch):
    s = new_services()
    locked, release = threading.Event(), threading.Event()
    def hold():
        with s.store._lock:
            locked.set()
            release.wait()
    holder = threading.Thread(target=hold)
    original = s.runtime.close
    async def runtime_close():
        holder.start()
        assert locked.wait(.5)
    monkeypatch.setattr(s.runtime, "close", runtime_close)
    try:
        started = time.monotonic()
        result = asyncio.run(s.close_local(.02))
        assert time.monotonic()-started < .5
        assert result["resource_close_pending"] and not result["safe_to_close_resources"]
        assert s.jobs._shutdown_owner is s
    finally:
        release.set()
        holder.join(.5)
        monkeypatch.setattr(s.runtime, "close", original)
        assert asyncio.run(s.close_local(.5))["safe_to_close_resources"]


def test_service_shutdown_dispatch_queue_does_not_extend_its_deadline():
    from concurrent.futures import ThreadPoolExecutor
    s = new_services()
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        started, release = threading.Event(), threading.Event()
        def block():
            started.set()
            release.wait()
        blocker = asyncio.create_task(asyncio.to_thread(block))
        await asyncio.sleep(0)  # Let the sole worker receive the blocker.
        assert started.wait(.5)
        try:
            before = time.monotonic()
            result = await s.close_local(.02)
            assert time.monotonic()-before < .5
            assert result["shutdown_pending"] and not result["safe_to_close_resources"]
            assert s.jobs._shutdown_owner is s
        finally:
            release.set()
            await blocker
        assert (await s.close_local(.5))["safe_to_close_resources"]
    asyncio.run(scenario())


def test_crash_recovery_waits_for_safety_finally_and_preserves_other_live_job(monkeypatch):
    from qumus2.flows.step import ProbeResult

    s = new_services()
    entered, release_off = threading.Event(), threading.Event()
    callback_done, healthy_started, healthy_release = threading.Event(), threading.Event(), threading.Event()
    probes, acts, armed = [], [], [True]
    heater = s.lab.devices.get("stamp_heater")
    original_off = heater.off
    def off():
        entered.set()
        assert release_off.wait(2.)
        original_off()
    monkeypatch.setattr(heater, "off", off)
    original_callback = s.lab.faults.on_crash
    def callback():
        original_callback()
        callback_done.set()
    s.lab.faults.on_crash = callback

    class Params(BaseModel):
        pass
    class CrashStep(Step):
        name = "crash_after_heat_fixture"
        def act(self, ctx, correction):
            acts.append("crash")
            if armed[0]:
                armed[0] = False
                ctx.gate.heater("stamp_heater").set_target(40.)
                s.lab.faults.inject("crash", {"after_calls": 1})
                ctx.gate.heater("stamp_heater").read_temperature()
        def verify(self, ctx):
            return Observation(Verdict.OK)
        def probe_state(self, ctx):
            probes.append("crash")
            return ProbeResult.NOT_DONE
    class HealthyStep(Step):
        name = "healthy_live_fixture"
        def act(self, ctx, correction):
            acts.append("healthy")
            healthy_started.set()
            while not healthy_release.wait(.005):
                ctx.check_cancel()
        def verify(self, ctx):
            return Observation(Verdict.OK)
        def probe_state(self, ctx):
            pytest.fail("Recovery cannot probe a still-owned healthy job")
    class CrashFlow(Flow):
        name = "blocked_crash_finally_fixture"
        devices = ("stamp_heater",)
        restore_heaters_on_resume = False
        def build_steps(self, params):
            return [CrashStep()]
    class HealthyFlow(Flow):
        name = "healthy_concurrent_fixture"
        def build_steps(self, params):
            return [HealthyStep()]
    for flow in (CrashFlow, HealthyFlow):
        flow.Params = Params
        monkeypatch.setitem(FLOWS, flow.name, flow)
    try:
        healthy = s.jobs.submit(HealthyFlow.name, {})
        assert healthy_started.wait(.5)
        crashed = s.jobs.submit(CrashFlow.name, {})
        assert entered.wait(.5) and callback_done.wait(.5)  # Actual callback arrived; no 20ms assumption.
        assert crashed in s.jobs._active_jobs and healthy in s.jobs._active_jobs
        assert not s.jobs._done[crashed].is_set() and probes == []
        healthy_release.set()  # The healthy owner exits first; the crashed cleanup is still blocked.
        assert s.jobs.wait(healthy, .5).status == "succeeded"
        assert s.jobs._recovery_deferred and not s.jobs._done[crashed].is_set() and probes == []
        release_off.set()
        assert s.jobs.wait(crashed, 2.).status == "succeeded"
        assert probes == ["crash"] and acts.count("healthy") == 1 and acts.count("crash") == 2
    finally:
        release_off.set()
        healthy_release.set()
        assert asyncio.run(s.close_local(1.))["safe_to_close_resources"]


@pytest.mark.parametrize("status", ["paused", "parked", "waiting_human"])
def test_early_terminal_status_never_admits_a_second_executor_before_owner_exit(monkeypatch, status):
    s = new_services()
    published, release = threading.Event(), threading.Event()
    def execute(rid, ctx, **kwargs):
        s.store.set_run_status(rid, status, error={"safe_shutdown_failed": ["stamp_heater"]}
                               if status == "waiting_human" else None)
        published.set()
        assert release.wait(1.)
        return status
    monkeypatch.setattr(s.jobs.executor, "execute", execute)
    rid = s.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    try:
        assert published.wait(.5)
        assert not s.jobs.status(rid).done and not s.jobs.wait(rid, 0).done
        cancel, mark = s.jobs._cancel[rid], s.store.last_event_id()
        operations = [(s.jobs.retry, (rid,), {}), (s.jobs.resume, (rid,), {}),
            (s.jobs.reverify, (rid,), {}), (s.jobs.enqueue, (rid,), {}),
            (s.jobs.review_waiting, (rid,), {"project_id": s.project_id, "failure_event_id": mark,
                "reviewer": "Fixture operator", "reason": "Owner has not finished"})]
        if status == "paused":
            operations.append((s.jobs.resume_all, (), {}))
        for operation, args, kwargs in operations:
            with pytest.raises(ValueError, match="still owned"):
                operation(*args, **kwargs)
        assert s.jobs._cancel[rid] is cancel and s.store.last_event_id() == mark
        assert s.jobs._queued_jobs == set() and rid in s.jobs._active_jobs
        release.set()
        assert s.jobs.wait(rid, .5).done
        assert s.jobs.status(rid).status == status  # Historical durable hold remains readable.
    finally:
        release.set()
        report = s.jobs.shutdown(.5)
        assert report["closed"] and not report["alive_workers"], report
        # This synthetic journal test never heated a device; retain its hold unchanged.
        s.store.close()


def test_continue_before_stop_reaches_running_safe_point_reuses_same_executor(controlled_flow):
    control, flow = controlled_flow
    control.ignore_cancel = True
    s = new_services()
    try:
        rid = s.jobs.submit(flow, {})
        assert control.started.wait(.5)
        cancellation = s.jobs._cancel[rid]
        cancellation.set()
        assert s.jobs.resume_all() == []  # Existing Continue semantics for a still-running job.
        assert s.jobs._cancel[rid] is cancellation and not cancellation.is_set()
        assert s.jobs._queued_jobs == set() and not s.jobs.status(rid).done
        control.release.set()
        assert s.jobs.wait(rid, .5).status == "succeeded"
        assert control.acts == ["first"]
    finally:
        control.release.set()
        s.gate.heater("stamp_heater").off()
        assert asyncio.run(s.close_local(.5))["safe_to_close_resources"]
