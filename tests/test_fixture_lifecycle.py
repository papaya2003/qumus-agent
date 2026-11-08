"""Pure fixture ownership controls; no laboratory execution."""
from __future__ import annotations

import pytest


def test_fixture_coroutine_factory_uses_caller_when_no_loop_runs():
    import asyncio
    import threading

    from conftest import run_fixture_coroutine

    called = []
    async def result():
        return 7
    def factory():
        called.append(threading.get_ident())
        return result()
    assert run_fixture_coroutine(factory) == 7
    assert called == [threading.get_ident()]
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()


def test_fixture_coroutine_factory_runs_in_owned_thread_under_existing_loop():
    import asyncio
    import threading

    from conftest import run_fixture_coroutine

    called = []
    async def result():
        return 7
    def factory():
        called.append(threading.get_ident())
        return result()
    async def caller():
        return run_fixture_coroutine(factory)
    assert asyncio.run(caller()) == 7
    assert len(called) == 1 and called[0] != threading.get_ident()


def test_fixture_coroutine_bridge_propagates_original_worker_error():
    import asyncio

    from conftest import run_fixture_coroutine

    error = ValueError("Original fixture exception")
    async def result():
        raise error
    async def caller():
        run_fixture_coroutine(lambda: result())
    with pytest.raises(ValueError, match="Original fixture exception") as raised:
        asyncio.run(caller())
    assert raised.value is error


def test_fixture_coroutine_thread_join_is_bounded_without_killing_owner():
    import asyncio
    import threading

    from conftest import run_fixture_coroutine

    release, entered, workers = threading.Event(), threading.Event(), []
    async def result():
        workers.append(threading.current_thread())
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.001)
    try:
        with pytest.raises(TimeoutError, match="resources remain retained"):
            run_fixture_coroutine(lambda: result(), timeout_s=.02, force_thread=True)
        assert entered.wait(1) and workers[0].is_alive()
    finally:
        release.set()
        if workers:
            workers[0].join(1)
    assert not workers[0].is_alive()


@pytest.fixture
def recording_lifecycle_case():
    """Mocks only: exercise fixture ownership without services, World or jobs."""
    from types import SimpleNamespace

    case = SimpleNamespace(trace=[], failure=None, device=True, job_done=True, close=True, closed=False)
    async def idle(*, timeout):
        case.trace.append(("driver", timeout))
        if case.failure:
            raise case.failure
    def shutdown(timeout):
        case.trace.append(("shutdown", timeout))
        return {"safe_to_close_resources": case.close}
    def stop():
        case.trace.append(("recorder", None))
    async def close(timeout):
        case.trace.append(("close", timeout))
        case.closed = case.close
        return {"closed": case.closed}
    case.services = SimpleNamespace(
        driver=SimpleNamespace(wait_idle=idle, _task=None, _turn=None),
        store=SimpleNamespace(list_runs=lambda: [{"id": "job", "status": "succeeded"}],
            list_samples=lambda **kwargs: [{"id": "device"}] if case.device else []),
        jobs=SimpleNamespace(shutdown=shutdown, status=lambda rid: SimpleNamespace(done=case.job_done)),
        close_local=close)
    case.recorder = SimpleNamespace(stop=stop, _thread=None)
    return case


def test_recording_fixture_closes_after_settled_shutdown_and_final_tick(recording_lifecycle_case):
    import asyncio

    from conftest import drive_recording_fixture

    case = recording_lifecycle_case
    report = asyncio.run(drive_recording_fixture(case.services, case.recorder))
    assert [name for name, _ in case.trace] == ["driver", "shutdown", "recorder", "close"]
    assert case.trace[0] == ("driver", 600.)
    assert report["driver_completed"] and report["device_ids"] == ["device"] and case.closed


def test_recording_fixture_rethrows_driver_timeout_after_cleanup(recording_lifecycle_case):
    import asyncio

    from conftest import drive_recording_fixture

    case = recording_lifecycle_case
    case.failure = TimeoutError("driver did not become idle")
    with pytest.raises(TimeoutError, match="driver did not become idle") as raised:
        asyncio.run(drive_recording_fixture(case.services, case.recorder))
    assert raised.value is case.failure and case.closed
    assert [name for name, _ in case.trace] == ["driver", "shutdown", "recorder", "close"]


@pytest.mark.parametrize("missing", ["device", "job_done"])
def test_recording_fixture_never_accepts_incomplete_output(recording_lifecycle_case, missing):
    import asyncio

    from conftest import drive_recording_fixture

    case = recording_lifecycle_case
    setattr(case, missing, False)
    with pytest.raises(AssertionError, match="did not complete a device|unfinished or unsuccessful jobs"):
        asyncio.run(drive_recording_fixture(case.services, case.recorder))
    assert case.closed


def test_recording_fixture_retains_unsettled_service_resources(recording_lifecycle_case):
    import asyncio

    from conftest import drive_recording_fixture

    case = recording_lifecycle_case
    case.close = False
    with pytest.raises(AssertionError):
        asyncio.run(drive_recording_fixture(case.services, case.recorder))
    assert not case.closed


def test_recording_fixture_preserves_driver_error_when_cleanup_also_fails(recording_lifecycle_case):
    import asyncio

    from conftest import drive_recording_fixture

    case = recording_lifecycle_case
    case.failure = TimeoutError("Original driver timeout")
    def fail(timeout):
        raise RuntimeError("Shutdown failed")
    case.services.jobs.shutdown = fail
    with pytest.raises(TimeoutError, match="Original driver timeout") as raised:
        asyncio.run(drive_recording_fixture(case.services, case.recorder))
    assert raised.value is case.failure and case.closed
    assert "Shutdown failed" in " ".join(raised.value.__notes__)


def test_recording_fixture_does_not_close_under_an_active_recorder(recording_lifecycle_case):
    import asyncio
    from types import SimpleNamespace

    from conftest import drive_recording_fixture

    case = recording_lifecycle_case
    case.recorder._thread = SimpleNamespace(is_alive=lambda: True, join=lambda timeout: None)
    with pytest.raises(AssertionError):
        asyncio.run(drive_recording_fixture(case.services, case.recorder))
    assert not case.closed and not any(name == "close" for name, _ in case.trace)
