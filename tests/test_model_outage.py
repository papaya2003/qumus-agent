"""Model service outage: classify provider errors, wait, continue the same conversation."""

from __future__ import annotations

import asyncio

import httpx
import openai
import pytest

from qumus2.agents.driver import ConversationDriver
from qumus2.agents.openai_runtime import http_max_retries_from_env, model_outage
from qumus2.agents.runtime import ReportedAgentError
from qumus2.runtime.store import RunStore

REQ = httpx.Request("POST", "https://gateway.example/v1/responses")


def _status_error(cls, status, headers=None, body=""):
    return cls(body or f"HTTP {status}", response=httpx.Response(status, headers=headers or {}, request=REQ),
               body=None)


def test_overloaded_503_with_retry_after_header():
    e = _status_error(openai.InternalServerError, 503, {"retry-after": "7"})
    assert model_outage(e) == {"status": 503, "retry_after_s": 7.0}


def test_retry_after_inside_gateway_message():
    body = ("{'error': {'type': 'upstream_error', 'code': 'upstream_rate_limited', 'message': "
            "'{\"code\":\"server_is_overloaded\",\"headers\":{\"retry-after\":\"321\",\"retry-after-ms\":\"321000\"}}'}}")
    e = _status_error(openai.InternalServerError, 503, body=body)
    assert model_outage(e) == {"status": 503, "retry_after_s": 321.0}


def test_rate_limit_and_connection_and_wrapped():
    assert model_outage(_status_error(openai.RateLimitError, 429))["status"] == 429
    assert model_outage(openai.APIConnectionError(request=REQ)) == {"status": None, "retry_after_s": None}
    assert model_outage(openai.APITimeoutError(request=REQ))["status"] is None
    try:
        try:
            raise _status_error(openai.InternalServerError, 500)
        except Exception as inner:
            raise RuntimeError("agent tool failed") from inner
    except RuntimeError as wrapped:
        assert model_outage(wrapped)["status"] == 500


def test_our_own_mistakes_are_not_outages():
    assert model_outage(_status_error(openai.BadRequestError, 400)) is None
    assert model_outage(_status_error(openai.AuthenticationError, 401)) is None
    assert model_outage(ValueError("bad tool args")) is None


def test_http_retries_env(monkeypatch):
    monkeypatch.delenv("QUMUS_OPENAI_HTTP_RETRIES", raising=False)
    assert http_max_retries_from_env(2) == 2
    monkeypatch.setenv("QUMUS_OPENAI_HTTP_RETRIES", "8")
    assert http_max_retries_from_env(2) == 8


class FlakyRuntime:
    """Fails `fail` times with an outage (or `error`), then succeeds."""

    mode = "sdk"
    budget_exhausted = False

    def __init__(self, store, fail, outage=None):
        self.store, self.fail, self.calls = store, fail, []
        self.outage = {"status": 503, "retry_after_s": None} if outage is None else outage
        self.busy = False
        self.interrupted = False

    async def _turn(self, kind, text):
        self.calls.append((kind, text))
        if self.fail > 0:
            self.fail -= 1
            eid = self.store.emit("agent.error", {"error": "overloaded", "type": "InternalServerError"},
                                  agent="orchestrator")
            raise ReportedAgentError("overloaded", eid, outage=self.outage or None)

    async def send(self, text):
        await self._turn("send", text)

    async def continue_turn(self, text):
        await self._turn("continue", text)

    async def interrupt(self):
        self.interrupted = True


class NoJobs:
    def stop_all(self):
        pass

    def resume_all(self):
        return []


def _driver(fail, outage=None, waits=(0.0, 0.0, 0.0), max_s=None):
    store = RunStore(":memory:")
    rt = FlakyRuntime(store, fail, outage)
    d = ConversationDriver(store, rt, NoJobs(), poll_s=0.01)
    d.outage_waits_s = waits
    d.outage_max_s = max_s
    return store, rt, d


def _kinds(store):
    return [e.kind for e in store.events_after(0, limit=1000)]


def test_outage_waits_then_continues_same_conversation():
    store, rt, d = _driver(fail=2)
    store.add_instruction("Make a Hall bar")
    asyncio.run(d.wait_idle(timeout=5))
    assert [c[0] for c in rt.calls] == ["send", "continue", "continue"]
    assert "model service was unavailable (HTTP 503)" in rt.calls[1][1]
    assert _kinds(store).count("agent.outage_wait") == 2
    assert "agent.outage_gave_up" not in _kinds(store)


def test_retry_after_sets_the_wait(monkeypatch):
    store, rt, d = _driver(fail=1, outage={"status": 503, "retry_after_s": 0.05}, waits=(0.0,))
    store.add_instruction("go")
    asyncio.run(d.wait_idle(timeout=5))
    wait = [e for e in store.events_after(0, limit=100) if e.kind == "agent.outage_wait"][0]
    assert wait.payload["wait_s"] == pytest.approx(0.05)


def test_keeps_waiting_with_the_last_step_by_default():
    store, rt, d = _driver(fail=12, waits=(0.0, 0.001))
    store.add_instruction("go")
    asyncio.run(d.wait_idle(timeout=10))
    assert len(rt.calls) == 13
    waits = [e.payload["wait_s"] for e in store.events_after(0, limit=1000) if e.kind == "agent.outage_wait"]
    assert waits[0] == 0.0 and set(waits[1:]) == {0.001}
    assert "agent.outage_gave_up" not in _kinds(store)


def test_gives_up_at_the_total_limit(monkeypatch):
    store, rt, d = _driver(fail=10, waits=(0.01,), max_s=0.025)
    store.add_instruction("go")
    with pytest.raises(ReportedAgentError):
        asyncio.run(d.wait_idle(timeout=5))
    assert len(rt.calls) == 3
    assert _kinds(store)[-1] == "agent.outage_gave_up"


def test_max_hours_env(monkeypatch):
    from qumus2.agents.driver import outage_max_s

    monkeypatch.delenv("QUMUS_OUTAGE_MAX_HOURS", raising=False)
    assert outage_max_s() is None
    monkeypatch.setenv("QUMUS_OUTAGE_MAX_HOURS", "2")
    assert outage_max_s() == 7200.0


def test_other_errors_do_not_retry():
    store, rt, d = _driver(fail=1, outage={})
    store.add_instruction("go")
    with pytest.raises(ReportedAgentError):
        asyncio.run(d.wait_idle(timeout=5))
    assert len(rt.calls) == 1
    assert "agent.outage_wait" not in _kinds(store)


def test_stop_cancels_the_wait():
    store, rt, d = _driver(fail=1, waits=(30.0,))

    async def go():
        store.add_instruction("go")
        await d.tick()
        for _ in range(100):
            if d._outage_sleep is not None:
                break
            await asyncio.sleep(0.01)
        assert d._outage_sleep is not None
        store.add_instruction("", kind="stop")
        await d.tick()
        await asyncio.wait_for(d._turn, 2)

    asyncio.run(go())
    assert len(rt.calls) == 1 and rt.interrupted
    assert "agent.outage_cancelled" in _kinds(store)
