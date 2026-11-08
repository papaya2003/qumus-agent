"""Scenario `repeat` with `until.status_not`: wait out a long job in bounded rounds."""
import asyncio
from types import SimpleNamespace

from qumus2.agents.scripted import ScriptedRuntime


class _Counter(ScriptedRuntime):
    def __init__(self, statuses):
        super().__init__(SimpleNamespace(store=None), {"turns": []})
        self.statuses = list(statuses)
        self.calls = 0

    async def _do(self, action):
        (kind, body), = action.items()
        if kind == "poll":
            self.calls += 1
            self.saved["job"] = {"_status": self.statuses.pop(0)}
            return
        await super()._do(action)


def _run(statuses, times):
    rt = _Counter(statuses)
    rt.saved["job"] = {"_status": "pending"}
    asyncio.run(rt._do({"repeat": {"times": times, "until": {"result_of": "job", "status_not": "pending"},
                                    "actions": [{"poll": {}}]}}))
    return rt


def test_status_not_stops_at_the_first_terminal_status():
    rt = _run(["pending", "pending", "ok", "pending"], times=10)
    assert rt.calls == 3 and rt.saved["job"]["_status"] == "ok"


def test_status_not_is_bounded_by_times():
    rt = _run(["pending"] * 5, times=3)
    assert rt.calls == 3 and rt.saved["job"]["_status"] == "pending"


def test_status_still_stops_on_the_named_status():
    rt = _Counter(["failed", "ok"])
    asyncio.run(rt._do({"repeat": {"times": 5, "until": {"result_of": "job", "status": "ok"},
                                    "actions": [{"poll": {}}]}}))
    assert rt.calls == 2
