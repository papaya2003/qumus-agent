"""Actual serialized first-send reconciliation; MockTransport only."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import openai
import pytest
from agents.run_config import ModelInputData

from qumus2.agents.openai_budget import SpendLedger
from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.agents.openai_runtime import BudgetExceeded, OpenAIRuntime
from qumus2.app import build_services
from scripts import record_three_demos as demo


def rows(ledger):
    with sqlite3.connect(ledger.path) as db:
        return db.execute("SELECT id,scope,reserved,actual FROM spend ORDER BY rowid").fetchall()


def test_increase_preserves_id_order_max_and_checks_delta_before_mutation(tmp_path):
    ledger = SpendLedger(tmp_path/"ledger.db", limit=100)
    first = ledger.reserve("prior", 4, 100)
    held = ledger.reserve("current", .05, 100)
    observations = []
    def check(delta):
        observations.append((delta, rows(ledger)))
    assert ledger.increase_reservation(held, "current", 1.06, 100, check=check) == 1.06
    assert observations[0][0] == pytest.approx(1.01)
    assert observations[0][1] == [(first, "prior", 4., None), (held, "current", .05, None)]
    assert [row[0] for row in rows(ledger)] == [first, held]
    assert ledger.increase_reservation(held, "current", .1, 100, check=check) == 1.06
    assert observations[-1][0] == 0
    before = rows(ledger)
    def reject(delta):
        assert delta > 0
        raise ValueError("inclusive campaign cap")
    with pytest.raises(ValueError, match="campaign cap"):
        ledger.increase_reservation(held, "current", 2, 100, check=reject)
    assert rows(ledger) == before


@pytest.mark.parametrize("bad", ["missing", "scope", "settled", "negative", "nan", "zero_cap"])
def test_increase_rejects_bad_identity_or_limits_without_mutation(tmp_path, bad):
    ledger = SpendLedger(tmp_path/"ledger.db", limit=100)
    held = ledger.reserve("current", .05, 100)
    if bad == "settled":
        ledger.settle(held, .01)
    before = rows(ledger)
    amount = -1 if bad == "negative" else float("nan") if bad == "nan" else 1
    with pytest.raises(ValueError):
        ledger.increase_reservation("absent" if bad == "missing" else held,
            "other" if bad == "scope" else "current", amount, 0 if bad == "zero_cap" else 100)
    assert rows(ledger) == before


def test_pending_increases_share_atomic_scope_and_stage_caps(tmp_path):
    path = tmp_path/"ledger.db"
    ledgers = [SpendLedger(path, limit=40), SpendLedger(path, limit=40)]
    held = [ledger.reserve("same", 5, 40) for ledger in ledgers]
    def increase(pair):
        ledger, ident = pair
        try:
            ledger.increase_reservation(ident, "same", 30, 40)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(increase, zip(ledgers, held, strict=True))) == 1
    assert ledgers[0].total() == 35 and len(rows(ledgers[0])) == 2


def wire_fixture(tmp_path, monkeypatch, *, prior=0., cap=100., first_hold=None):
    limits = {**demo.story_limits("collaboration"), "estimated_usd": 100., "http_max_retries": 0}
    folder = tmp_path/"wire"
    services = build_services(folder/"qumus.db", use_openai=False)
    manifest = {"story": "collaboration", "source": "frozen", "started_wall": time.time(),
        "limits": limits, "workspace": str(folder), "ledger": str(tmp_path/"budget.db"), "attempt_budget_usd": 100.}
    sent, checks = [], []
    def transport(request):
        sent.append(request)
        return httpx.Response(503, json={"error": {"message": "offline unknown response usage"}})
    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(transport), **kw))
    def inclusive(registry, record, *, next_usage, runtime, **kw):
        # Observe the same original row before it is updated; include only delta.
        checks.append((next_usage["estimated_usd"], rows(runtime._ledger)))
        if runtime._ledger.total()+next_usage["estimated_usd"] > cap:
            raise ValueError("inclusive first-cell/qualification cap")
    creds = OpenAICredentials("offline", model_id=demo.MODEL, input_price=10, output_price=50,
        test_budget_usd=100, stage_budget_usd=None, context_window_tokens=2_000_000, budget_path=manifest["ledger"])
    rt = OpenAIRuntime(services.gateway, creds, run_budget_usd=100, http_max_retries=0,
        pre_send_admission=demo.DemoAdmission(manifest, source_reader=lambda: "frozen",
            limits_reader=lambda _: limits, campaign_guard=inclusive))
    demo.configure_demo_pacing(rt, limits)
    payload = {"model": demo.MODEL, "input": "bounded task", "max_output_tokens": 256,
        "tools": [{"type": "function", "name": "get_project_context", "description": "a"*100000, "parameters": {}}]}
    async def run():
        rt._ensure()
        rt.cfg["openai"]["max_output_tokens"] = 256
        if prior:
            prior_id = rt._ledger.reserve("prior", prior, 100)
            rt._ledger.settle(prior_id, prior)
        agent = rt._agents["orchestrator"]
        agent.tools = [SimpleNamespace(name="get_project_context", params_json_schema={})]
        prepared = await rt._config.call_model_input_filter(SimpleNamespace(agent=agent,
            model_data=ModelInputData(input=[], instructions="bounded task")))
        await rt._hooks.on_llm_start(None, agent, prepared.instructions, prepared.input)
        ident = rt._reservations[agent.name]
        initial = rows(rt._ledger)
        if first_hold:
            rt._ledger.increase_reservation(ident, rt._budget_scope, first_hold, 100)
        error = None
        try:
            await rt._client.responses.create(**payload)
        except Exception as exc:
            error = exc
        after = rows(rt._ledger)
        events = services.store.events_after(0, limit=10000)
        await rt.close()
        return SimpleNamespace(ident=ident, initial=initial, rows=after, error=error, events=events)
    result = asyncio.run(run())
    result.sent, result.checks = sent, checks
    result.wire = (demo.input_allowance(payload)*10+256*50)/1_000_000
    return result


@pytest.mark.parametrize("prior,cap", [(99., 100.), (0., 1.)])
def test_first_wire_allowance_cannot_cross_inclusive100_or_qualification1(tmp_path, monkeypatch, prior, cap):
    result = wire_fixture(tmp_path, monkeypatch, prior=prior, cap=cap)
    assert isinstance(result.error, BudgetExceeded) and not result.sent
    assert result.wire > 1
    current = next(row for row in result.rows if row[0] == result.ident)
    assert current[3] == 0  # Known-unsent first reservation released, original ID kept.
    assert result.checks and result.checks[0][0] == pytest.approx(result.wire-current[2])
    assert not [e for e in result.events if e.kind == "agent.request"]


def test_unknown_first_send_retains_entire_wire_bound_on_original_row(tmp_path, monkeypatch):
    result = wire_fixture(tmp_path, monkeypatch)
    assert isinstance(result.error, openai.APIStatusError) and len(result.sent) == 1
    assert len(result.rows) == 1 and result.rows[0][0] == result.ident
    assert result.initial[0][2] < .06 and result.rows[0][2] == pytest.approx(result.wire)
    assert result.rows[0][3] is None
    admitted = next(e for e in result.events if e.kind == "demo.http_admitted")
    assert admitted.payload["reservation_id"] == result.ident


def test_larger_existing_bound_is_not_added_again(tmp_path, monkeypatch):
    result = wire_fixture(tmp_path, monkeypatch, first_hold=1.5)
    assert len(result.sent) == 1 and result.rows[0][2:] == (1.5, None)
    assert result.checks[0][0] == 0
