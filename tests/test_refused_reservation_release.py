"""A model call the gateway refused unprocessed (429/503 on every send) releases its reservation."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import httpx
import openai
import pytest
from agents.run_config import ModelInputData

from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.agents.openai_runtime import OpenAIRuntime
from qumus2.app import build_services


def _run(tmp_path, monkeypatch, replies):
    services = build_services(tmp_path / "ws" / "qumus.db", use_openai=False)
    queue = list(replies)

    def transport(request):
        reply = queue.pop(0) if queue else 503
        if reply == "drop":
            raise httpx.ConnectError("connection dropped", request=request)
        return httpx.Response(reply, headers={"retry-after-ms": "1"},
                              json={"error": {"message": "server_is_overloaded"}})

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(transport), **kw))
    creds = OpenAICredentials("offline", model_id="openai/test-model", input_price=10, output_price=50,
                              test_budget_usd=100, stage_budget_usd=None, context_window_tokens=400_000,
                              budget_path=str(tmp_path / "budget.db"))
    rt = OpenAIRuntime(services.gateway, creds, run_budget_usd=100, http_max_retries=2)

    async def go():
        rt._ensure()
        agent = rt._agents["orchestrator"]
        prepared = await rt._config.call_model_input_filter(SimpleNamespace(
            agent=agent, model_data=ModelInputData(input=[], instructions="task")))
        await rt._hooks.on_llm_start(None, agent, prepared.instructions, prepared.input)
        ident = rt._reservations[agent.name]
        try:
            await rt._client.responses.create(model="openai/test-model", input="task")
        except Exception as exc:  # what UsageModel hands to its failure callback
            rt._model_failure(exc)
        with sqlite3.connect(rt._ledger.path) as db:
            row = db.execute("SELECT reserved, actual FROM spend WHERE id=?", (ident,)).fetchone()
        events = [e.kind for e in services.store.events_after(0, limit=100000)]
        await rt.close()
        return row, events

    return asyncio.run(go())


@pytest.mark.parametrize("status", [503, 429])
def test_every_send_refused_settles_the_reservation_to_zero(tmp_path, monkeypatch, status):
    (reserved, actual), events = _run(tmp_path, monkeypatch, [status, status, status])
    assert reserved > 0 and actual == 0
    assert "agent.reservation_released" in events


def test_a_send_without_a_reply_keeps_the_full_reservation(tmp_path, monkeypatch):
    (reserved, actual), events = _run(tmp_path, monkeypatch, ["drop", 503, 503])
    assert reserved > 0 and actual is None
    assert "agent.reservation_released" not in events


def test_other_errors_keep_the_reservation(tmp_path, monkeypatch):
    (reserved, actual), events = _run(tmp_path, monkeypatch, [500, 500, 500])
    assert actual is None and "agent.reservation_released" not in events


def test_the_model_provider_reports_failures_to_the_release_hook(tmp_path, monkeypatch):
    services = build_services(tmp_path / "ws" / "qumus.db", use_openai=False)
    creds = OpenAICredentials("offline", model_id="openai/test-model", input_price=10, output_price=50,
                              test_budget_usd=100, budget_path=str(tmp_path / "budget.db"))
    rt = OpenAIRuntime(services.gateway, creds, run_budget_usd=100)
    rt._ensure()
    provider = rt._config.model_provider
    while not hasattr(provider, "failure_callback"):
        provider = provider.inner
    assert provider.failure_callback == rt._model_failure
    asyncio.run(rt.close())
