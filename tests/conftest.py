from __future__ import annotations

import asyncio
import copy
import threading
import time
from typing import Any

import pytest

from qumus2.app import Services, build_services
from qumus2.config import load_yaml


def display_matches(display: bytes, capture: bytes) -> bool:
    """A display-only motion frame (JPEG, #205) shows the same picture as the
    lossless capture: same size, JPEG-level differences only."""
    import io

    import numpy as np
    from PIL import Image

    a, b = (np.asarray(Image.open(io.BytesIO(x)).convert("RGB"), dtype=np.int16) for x in (display, capture))
    if display[:2] != b"\xff\xd8" or a.shape != b.shape:
        return False
    error = abs(a-b)
    return float(error.mean()) < 3 and float(np.percentile(error, 99.9)) <= 24


def quiet_bench(**overrides: Any) -> dict[str, Any]:
    """Testbench config with no random big errors unless asked for."""
    cfg = copy.deepcopy(load_yaml("testbench.yaml"))
    cfg["placement"]["big_error_prob"] = 0.0
    cfg["placement"]["sigma_mm"] = 0.05
    cfg["placement"]["sigma_deg"] = 0.2
    # no random detector errors either (tests that want them turn them on)
    for k in ("tail_prob", "miss_prob", "false_positive_prob", "confuse_material_prob",
              "bias_sigma_mm", "bias_sigma_deg"):
        cfg["locator"][k] = 0.0
    for k, v in overrides.items():
        section, key = k.split("__")
        cfg[section][key] = v
    return cfg


@pytest.fixture
def services() -> Services:
    services = build_services(":memory:", seed=1, testbench_config=quiet_bench())
    yield services
    _close_owned_services(services)


@pytest.fixture
def make_services():
    owned = []
    def _make(image_dir: Any = None, **overrides: Any) -> Services:
        services = build_services(":memory:", seed=1,
                                  testbench_config=quiet_bench(**overrides), image_dir=image_dir)
        owned.append(services)
        return services
    yield _make
    for services in reversed(owned):
        _close_owned_services(services)


def run_fixture_coroutine(factory, *, timeout_s=35.0, force_thread=False):
    """Run a coroutine factory without nesting Playwright's running event loop.

    The factory is invoked on its owning thread, never pre-created and left
    unawaited. A timed-out worker retains its objects; it is never killed.
    """
    if not force_thread:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())
    outcome = {}
    def run():
        try:
            outcome["value"] = asyncio.run(factory())
        except BaseException as error:
            outcome["error"] = error
    worker = threading.Thread(target=run, name="qumus-fixture-async", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError("Owned fixture coroutine has not settled; its resources remain retained")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


async def drive_recording_fixture(services, recorder, *, timeout_s=600., cleanup_s=30.):
    """Own one scripted recording turn; incomplete work is never a replay fixture."""
    failure, device_ids = None, []
    try:
        await services.driver.wait_idle(timeout=timeout_s)
        runs = services.store.list_runs()
        assert runs and all(run["status"] == "succeeded" and services.jobs.status(run["id"]).done
                            for run in runs), "Recording fixture has unfinished or unsuccessful jobs"
        device_ids = [sample["id"] for sample in services.store.list_samples(kind="device")]
        assert device_ids, "Recording fixture did not complete a device; incomplete evidence is retained"
    except BaseException as error:
        failure = error
    finally:
        deadline = time.monotonic() + cleanup_s
        def remaining():
            return max(0., deadline-time.monotonic())
        errors, report = [], {}
        tasks = {task for task in (services.driver._task, services.driver._turn)
                 if isinstance(task, asyncio.Task) and task is not asyncio.current_task()}
        for task in tasks:
            if not task.done():
                task.cancel()
        # Request ordinary cooperative Stop before awaiting cancelled turns or
        # closing their loop. Cancelling asyncio does not stop physical workers.
        try:
            report["jobs"] = await asyncio.to_thread(services.jobs.shutdown, remaining())
        except BaseException as error:
            errors.append(error)
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), remaining())
            except BaseException as error:
                errors.append(error)
        # Final recorder tick needs an open store, after the shutdown attempt.
        try:
            recorder.stop()
        except BaseException as error:
            errors.append(error)
        recorder_thread = getattr(recorder, "_thread", None)
        if recorder_thread is not None and recorder_thread.is_alive():
            recorder_thread.join(remaining())
        pending_recorder = recorder_thread is not None and recorder_thread.is_alive()
        if pending_recorder or any(not task.done() for task in tasks):
            errors.append(RuntimeError("Recording/turn owner is still active; store and images retained"))
            report["services"] = {"closed": False, "pending_recorder": pending_recorder}
        else:
            try:
                report["services"] = await services.close_local(remaining())
                assert report["services"]["closed"], report["services"]
            except BaseException as error:
                errors.append(error)
        if failure is not None:
            failure.add_note(f"Recording cleanup: {report}; cleanup errors: {[str(error) for error in errors]}")
            raise failure
        assert not errors, (report, errors)
    return {"driver_completed": True, "device_ids": device_ids, "cleanup": report}


def _close_owned_services(services):
    # close_local owns its existing 30s budget; the bridge allows loop teardown.
    report = run_fixture_coroutine(lambda: services.close_local())
    # Intentional energy holds/manual journal entries retain their resources;
    # worker exit is distinct from proof that their outputs are safely off.
    assert not report["alive_workers"] and not report["stop_pending"] and report["stop_error"] is None, report
    if getattr(services, "_manual_executor_fixture", False):
        assert report["manual_execution"], report
    else:
        assert report["closed"] and not report["unsettled_jobs"], report
        assert not report["evidence_pending"], report
    assert "runtime_close_error" not in report, report


@pytest.fixture
def pickup_inspection(tmp_path):
    """Synthetic current search evidence for isolated Picker contract fixtures.

    This does not execute or qualify a physical search. Detail-image availability
    remains independent from the explicit pickup-neighbourhood context receipt.
    """
    from PIL import Image

    def add(services, flake_ids, chip_id="chip_001"):
        image = tmp_path / f"{chip_id}-pickup-context.png"
        Image.new("RGB", (4, 4), "gray").save(image)
        job = services.store.create_run("search_chip", {"chip_id": chip_id}, project_id=services.project_id)
        services.store.set_run_status(job, "succeeded")
        for fid in flake_ids:
            services.store.upsert_sample(fid, "flake", location=chip_id, props={"pickup_assessment": {
                "flake_id": fid, "source_chip_id": chip_id, "images": [str(image)], "job_id": job, "clear": True}})
        return job
    return add


def put_sample(s: Services, sample_id: str, place: str) -> None:
    """Set a sample down at a named place by hand (world and ledger), e.g. a
    bare chip on the middle board where arm_right can take it."""
    from qumus2.core.types import Pose
    from qumus2.flows.bench import positions

    p = positions(s.lab.bench)[place]
    w = s.lab.world
    with w.lock:
        o = w.objects[sample_id]
        o.pose = Pose(x=p["x"], y=p["y"], z=o.pose.z, rotation=0.0)
        o.held_by, o.on = None, None
    s.store.upsert_sample(sample_id, s.store.get_sample(sample_id)["kind"], location=place)
