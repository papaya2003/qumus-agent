"""Stop, Continue and station errors never take the brain down (review 2026-11-13).

- a station that does not confirm its Stop is reported; the driver keeps running
- stop_all reaches every station at once, whatever one of them raises
- a station refusal or transport error is a tool error, not a crashed turn
- a picture id never leaves the image folder, also in Windows spellings
"""

from __future__ import annotations

import asyncio
import time

import pytest

from qumus2.agents.driver import ConversationDriver
from qumus2.contract.errors import TransportError
from qumus2.runtime.store import RunStore
from qumus2.stations.jobs import ContractJobService
from qumus2.stations.mirror import ImageCache


class IdleRuntime:
    mode = "sdk"
    budget_exhausted = False
    busy = False

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)

    async def continue_turn(self, text):
        self.sent.append(text)

    async def interrupt(self):
        pass


class BrokenJobs:
    def __init__(self, stop_error=None, resume_error=None):
        self.stop_error, self.resume_error = stop_error, resume_error

    def stop_all(self):
        if self.stop_error:
            raise self.stop_error

    def resume_all(self):
        if self.resume_error:
            raise self.resume_error
        return []


def _kinds(store):
    return [e.kind for e in store.events_after(0, limit=1000)]


def test_unconfirmed_stop_is_reported_and_later_messages_still_arrive():
    store = RunStore(":memory:")
    rt = IdleRuntime()
    d = ConversationDriver(store, rt, BrokenJobs(stop_error=ConnectionError("stacking: timed out")),
                           poll_s=0.01)

    async def go():
        task = asyncio.create_task(d.run_forever())
        store.add_instruction("", kind="stop")
        await asyncio.sleep(0.1)
        store.add_instruction("are you there?")
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(go())
    kinds = _kinds(store)
    assert "system.stop_unconfirmed" in kinds and "system.stopped" not in kinds
    assert any("are you there?" in t for t in rt.sent)  # the driver kept running
    assert not [i for i in store.pending_instructions() if i["kind"] == "stop"]


def test_failed_continue_is_reported_without_killing_the_driver():
    store = RunStore(":memory:")
    rt = IdleRuntime()
    d = ConversationDriver(store, rt, BrokenJobs(resume_error=KeyError("stacking")), poll_s=0.01)

    async def go():
        task = asyncio.create_task(d.run_forever())
        store.add_instruction("", kind="continue")
        await asyncio.sleep(0.1)
        store.add_instruction("hello")
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(go())
    assert "system.continue_failed" in _kinds(store)
    assert any("hello" in t for t in rt.sent)


class SlowLink:
    contract = 1

    def __init__(self, module, delay=0.0, error=None):
        self.module, self.delay, self.error = module, delay, error
        self.stopped_at: float | None = None

    def stop(self, wait_s=1.0):
        time.sleep(self.delay)
        if self.error:
            raise self.error
        self.stopped_at = time.monotonic()

    def release(self):
        pass


def test_stop_all_stops_every_station_at_once_whatever_one_raises():
    links = {"exfoliation": SlowLink("exfoliation", delay=0.5, error=TransportError("no route")),
             "stacking": SlowLink("stacking"),
             "reactor": SlowLink("reactor", error=ValueError("bad JSON"))}
    jobs = ContractJobService(RunStore(":memory:"), {m: None for m in links}, links=links)
    start = time.monotonic()
    with pytest.raises(ConnectionError) as raised:
        jobs.stop_all()
    assert links["stacking"].stopped_at is not None
    assert links["stacking"].stopped_at - start < 0.4  # not behind the slow station
    assert "exfoliation" in str(raised.value) and "reactor" in str(raised.value)


def test_lab_wide_commands_use_a_connected_module_without_stacking():
    links = {"exfoliation": SlowLink("exfoliation")}
    jobs = ContractJobService(RunStore(":memory:"), {"exfoliation": None}, links=links)
    assert jobs._lab_wide_module() == "exfoliation"


@pytest.mark.parametrize("image_id", ["/Windows/x.png", "C:/x.png", "C:\\Windows\\x.png",
                                      "..\\..\\x.png", "microscope/../../x.png", "\\\\server\\share\\x.png"])
def test_picture_ids_stay_inside_the_image_folder(tmp_path, image_id):
    cache = ImageCache(tmp_path / "images", None)
    path = cache.local_path(image_id)
    assert path is not None
    path.resolve().relative_to((tmp_path / "images").resolve())
    assert path.name == "x.png"


def test_station_errors_become_tool_errors(services, monkeypatch):
    """A transport error under a tool is an error result the agent can act on."""
    def unreachable(*args, **kwargs):
        raise TransportError("stacking does not answer")

    monkeypatch.setattr(services.jobs, "submit", unreachable)
    result, _ = asyncio.run(services.gateway.call("hands", "place_chip_on_transfer_stage",
                                                  {"chip_id": "chip_001"}))
    assert result.status == "error"
    assert "TransportError" in result.summary and "does not answer" in result.summary


def test_a_late_run_record_keeps_the_job_id(services, monkeypatch):
    """Accepted but the record is late: the agent gets the id, never a crash."""
    from qumus2.runtime.jobs import JobRecordLate

    def late(*args, **kwargs):
        raise JobRecordLate("record late", run_id="run_late")

    monkeypatch.setattr(services.jobs, "submit", late)
    result, _ = asyncio.run(services.gateway.call("hands", "place_chip_on_transfer_stage",
                                                  {"chip_id": "chip_001"}))
    assert result.status == "pending" and result.data["job_id"] == "run_late"
    assert "do not submit it again" in result.summary
