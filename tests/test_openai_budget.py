"""An explicit unlimited stage gate preserves finite workspace caps and accounting."""
from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from qumus2.agents.openai_budget import SpendLedger
from qumus2.agents.openai_credentials import OpenAICredentials, load_openai_credentials


@pytest.fixture
def isolated_credentials(monkeypatch):
    import os

    for key in os.environ:
        if key.startswith("QUMUS_OPENAI_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("QUMUS_OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setenv("QUMUS_OPENAI_MODEL", "openai/test-model")


def test_default_budgets_remain_finite(isolated_credentials, tmp_path):
    creds = load_openai_credentials(None)
    assert creds.stage_budget_usd == 200 and creds.test_budget_usd == 40
    assert SpendLedger(tmp_path / "budget.db").limit == 200


@pytest.mark.parametrize("setting", ["unlimited", "UNLIMITED", " unlimited "])
def test_explicit_environment_stage_opt_out(isolated_credentials, monkeypatch, setting):
    monkeypatch.setenv("QUMUS_OPENAI_STAGE_BUDGET_USD", setting)
    creds = load_openai_credentials(None)
    assert creds.stage_budget_usd is None and creds.test_budget_usd == 40


def test_dotenv_opt_out_and_environment_precedence(isolated_credentials, monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("QUMUS_OPENAI_STAGE_BUDGET_USD=unlimited\n")
    assert load_openai_credentials(path).stage_budget_usd is None
    monkeypatch.setenv("QUMUS_OPENAI_STAGE_BUDGET_USD", "250")
    assert load_openai_credentials(path).stage_budget_usd == 250


@pytest.mark.parametrize("setting", ["-1", "0", "nan", "NaN", "inf", "-inf", "Infinity", "disabled"])
def test_invalid_stage_setting_is_not_an_opt_out(isolated_credentials, monkeypatch, setting):
    monkeypatch.setenv("QUMUS_OPENAI_STAGE_BUDGET_USD", setting)
    with pytest.raises(ValueError):
        load_openai_credentials(None)


@pytest.mark.parametrize("setting", ["unlimited", "-1", "0", "nan", "inf"])
def test_workspace_budget_cannot_be_unlimited(isolated_credentials, monkeypatch, setting):
    monkeypatch.setenv("QUMUS_OPENAI_STAGE_BUDGET_USD", "unlimited")
    monkeypatch.setenv("QUMUS_OPENAI_TEST_BUDGET_USD", setting)
    with pytest.raises(ValueError):
        load_openai_credentials(None)


@pytest.mark.parametrize("amount", [-1, 0, float("nan"), float("inf"), -float("inf")])
def test_invalid_direct_stage_limits_are_rejected_before_creating_a_ledger(tmp_path, amount):
    with pytest.raises(ValueError, match="positive and finite"):
        OpenAICredentials("offline", model_id="test", stage_budget_usd=amount)
    path = tmp_path / "budget.db"
    with pytest.raises(ValueError, match="positive and finite"):
        SpendLedger(path, limit=amount)
    assert not path.exists()


def rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT id,scope,reserved,actual FROM spend ORDER BY rowid").fetchall()


def test_switching_stage_gate_preserves_history_unknown_spend_and_settlement(tmp_path):
    path = tmp_path / "budget.db"
    finite = SpendLedger(path)
    for n in range(5):
        ident = finite.reserve(f"prior-{n}", 40, 40)
        if n < 4:
            finite.settle(ident, 40)
    original = rows(path)
    assert finite.total() == 200
    with pytest.raises(ValueError, match="budget"):
        finite.reserve("new", 1, 40)
    unlimited = SpendLedger(path, limit=None)
    assert rows(path) == original
    ident = unlimited.reserve("new", 40, 40)
    assert unlimited.total() == 240 and unlimited.total("new") == 40
    with pytest.raises(ValueError, match=r"phase \$240.00/unlimited; test \$40.00/\$40.00"):
        unlimited.reserve("new", 0.01, 40)
    unlimited.settle(ident, 4)
    unresolved = unlimited.reserve("new", 36, 40)
    assert rows(path)[:len(original)] == original
    reopened = SpendLedger(path, limit=None)
    assert reopened.total("new") == 40 and reopened.total() == 240
    assert rows(path)[-1] == (unresolved, "new", 36, None)
    # Re-enabling the default finite gate retains the accumulated total.
    finite_again = SpendLedger(path)
    with pytest.raises(ValueError, match="budget"):
        finite_again.reserve("another", 0.01, 40)
    assert rows(path)[:len(original)] == original


def test_unlimited_stage_keeps_workspace_reservations_atomic(tmp_path):
    path = tmp_path / "budget.db"
    ledgers = [SpendLedger(path, limit=None), SpendLedger(path, limit=None)]

    def reserve(ledger):
        try:
            return ledger.reserve("same-workspace", 30, 40)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ledgers))
    assert sum(ident is not None for ident in results) == 1
    assert ledgers[0].total() == 30 and len(rows(path)) == 1


def test_unlimited_stage_runtime_still_records_configured_rate_usage(services, tmp_path):
    pytest.importorskip("agents")
    from agents.testing import ModelStep, ScriptedModel, assistant_message
    from agents.usage import Usage

    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.config import load_yaml

    model = ScriptedModel([ModelStep(output=[assistant_message("Offline reply")],
                                     usage=Usage(input_tokens=100, output_tokens=10, total_tokens=110))])

    class Provider:
        def get_model(self, name):
            return model

    creds = OpenAICredentials("offline", model_id="test", input_price=1, output_price=2,
                              stage_budget_usd=None, budget_path=str(tmp_path / "budget.db"))
    config = load_yaml("agents.yaml")
    config["openai"]["min_request_interval_s"] = 0
    runtime = OpenAIRuntime(services.gateway, creds, model_provider=Provider(), config=config)
    # The offline model bypasses client creation; attach the same production
    # ledger used by its ordinary SDK hooks. No network request is made.
    runtime._ledger = SpendLedger(tmp_path / "budget.db", creds.stage_budget_usd)
    prior = runtime._ledger.reserve("previous", 250, 300)
    runtime._ledger.settle(prior, 250)
    asyncio.run(runtime.send("Hello"))
    model.assert_complete()
    saved = rows(tmp_path / "budget.db")
    assert len(saved) == 2 and saved[0] == (prior, "previous", 250, 250)
    assert saved[1][2] > 0 and saved[1][3] == pytest.approx(0.00012)
    usage = [e for e in services.store.events_after(0, limit=10000) if e.kind == "agent.usage"]
    assert usage[-1].payload["cost_basis"] == "configured_token_rates"
    assert usage[-1].payload["cost_usd"] == pytest.approx(0.00012)
    asyncio.run(runtime.close())


@pytest.mark.parametrize('rate', [-1, float('nan'), float('inf'), 11, True])
def test_invalid_cached_input_rates_are_rejected(rate):
    with pytest.raises(ValueError):
        OpenAICredentials('offline', model_id='test', input_price=10, output_price=50,
                          cached_input_price=rate)


def test_cache_rate_requires_full_tariff_and_loads_from_environment(isolated_credentials, monkeypatch):
    with pytest.raises(ValueError, match='requires input/output'):
        OpenAICredentials('offline', model_id='test', cached_input_price=1)
    assert load_openai_credentials(None).cached_input_price is None
    monkeypatch.setenv('QUMUS_OPENAI_INPUT_USD_PER_MILLION', '10')
    monkeypatch.setenv('QUMUS_OPENAI_OUTPUT_USD_PER_MILLION', '50')
    monkeypatch.setenv('QUMUS_OPENAI_CACHED_INPUT_USD_PER_MILLION', '0')
    assert load_openai_credentials(None).cached_input_price == 0
    monkeypatch.setenv('QUMUS_OPENAI_CACHED_INPUT_USD_PER_MILLION', '1')
    assert load_openai_credentials(None).cached_input_price == 1


@pytest.mark.parametrize('details,discounted', [
    ({'cached_tokens': 80}, 80), ({'cached_tokens': 0}, 0),
    ({}, 0), ({'cache_write_tokens': 80}, 0),
    ({'cached_tokens': None}, 0), ({'cached_tokens': True}, 0),
    ({'cached_tokens': '80'}, 0), ({'cached_tokens': 80.0}, 0),
    ({'cached_tokens': -1}, 0), ({'cached_tokens': 101}, 0),
    ({'cached_tokens': 80, 'cache_write_tokens': 20}, 80),
    ([], 0), ('unknown', 0),
])
def test_measured_cache_settles_existing_reservation_without_repricing_history(
        services, tmp_path, details, discounted):
    pytest.importorskip('agents')
    from agents.testing import ModelStep, ScriptedModel, assistant_message
    from agents.usage import Usage

    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.config import load_yaml

    raw = {'input_tokens': 100, 'output_tokens': 10, 'input_tokens_details': details}
    model = ScriptedModel([ModelStep(output=[assistant_message('Offline reply')],
        usage=Usage(input_tokens=100, output_tokens=10, total_tokens=110), raw_usage=raw)])

    class Provider:
        def get_model(self, name):
            return model

    creds = OpenAICredentials('offline', model_id='test', input_price=10, output_price=50,
        cached_input_price=1, stage_budget_usd=None, budget_path=str(tmp_path / 'budget.db'))
    config = load_yaml('agents.yaml')
    config['openai']['min_request_interval_s'] = 0
    runtime = OpenAIRuntime(services.gateway, creds, model_provider=Provider(), config=config)
    runtime._ledger = SpendLedger(tmp_path / 'budget.db', None)
    unknown = runtime._ledger.reserve(runtime._budget_scope, 1, 40)
    history = rows(tmp_path / 'budget.db')
    asyncio.run(runtime.send('Hello'))
    model.assert_complete()
    saved = rows(tmp_path / 'budget.db')
    assert saved[:1] == history and saved[0] == (unknown, runtime._budget_scope, 1, None)
    expected = ((100 - discounted) * 10 + discounted + 10 * 50) / 1_000_000
    assert len(saved) == 2 and saved[1][3] == pytest.approx(expected)
    assert saved[1][2] > 4096 * 50 / 1_000_000  # full input plus maximum output reserved
    event = [e for e in services.store.events_after(0, limit=10000) if e.kind == 'agent.usage'][-1]
    assert event.payload['cost_usd'] == pytest.approx(expected)
    assert event.payload['pricing']['discounted_cached_input_tokens'] == discounted
    assert event.payload['pricing']['cached_input_usd_per_million'] == 1
    assert event.payload['cost_basis'] == 'configured_token_rates'
    asyncio.run(runtime.close())


@pytest.mark.parametrize('raw', [None, {}, {'input_tokens_details': {'cached_tokens': 80}},
    {'input_tokens': 99, 'output_tokens': 10, 'input_tokens_details': {'cached_tokens': 80}},
    {'input_tokens': 100, 'input_tokens_details': {'cached_tokens': 80}},
    {'input_tokens': 100, 'output_tokens': 11, 'input_tokens_details': {'cached_tokens': 80}}])
def test_missing_or_mismatched_raw_usage_cannot_receive_cache_credit(services, raw):
    from types import SimpleNamespace

    from qumus2.agents.openai_runtime import OpenAIRuntime

    runtime = OpenAIRuntime(services.gateway, OpenAICredentials('offline', model_id='test',
        input_price=10, output_price=50, cached_input_price=1))
    cost, pricing = runtime._response_cost(SimpleNamespace(input_tokens=100, output_tokens=10), raw)
    assert cost == pytest.approx(0.0015)
    assert pricing['basis'] == 'full_input_rate'
    assert pricing['measured_cached_input_tokens'] is None
    asyncio.run(runtime.close())


def test_chat_cache_usage_and_optional_default_preserve_no_cache_reservation(services):
    from types import SimpleNamespace

    from qumus2.agents.openai_runtime import OpenAIRuntime

    creds = OpenAICredentials('offline', model_id='test', input_price=10, output_price=50)
    runtime = OpenAIRuntime(services.gateway, creds)
    raw = {'prompt_tokens': 100, 'completion_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 80}}
    usage = SimpleNamespace(input_tokens=100, output_tokens=10)
    full, pricing = runtime._response_cost(usage, raw)
    assert full == pytest.approx(0.0015) and pricing['basis'] == 'full_input_rate'
    agent = SimpleNamespace(tools=[])
    reserve = runtime._request_allowance(agent, 'instructions', [{'text': 'hello'}])
    runtime.creds = replace(runtime.creds, cached_input_price=1)
    assert runtime._request_allowance(agent, 'instructions', [{'text': 'hello'}]) == reserve
    assert runtime._response_cost(usage, raw)[0] == pytest.approx(0.00078)
    runtime.creds = replace(runtime.creds, cached_input_price=0)
    assert runtime._response_cost(usage, raw)[0] == pytest.approx(0.0007)
    asyncio.run(runtime.close())


@pytest.mark.parametrize('saved_model,saved_input,saved_cache,expected', [
    ('astra', 10, 'absent', 1), ('other', 10, 'absent', None),
    ('astra', 20, 'absent', None), ('other', 10, None, None),
    ('other', 10, 2, 2), ('astra', 10, None, None),
])
def test_restored_model_cache_rate_is_bound_to_saved_model_and_tariff(
        services, saved_model, saved_input, saved_cache, expected):
    from qumus2.agents.openai_runtime import OpenAIRuntime

    creds = OpenAICredentials('offline', model_id='astra', input_price=10, output_price=50,
                            cached_input_price=1)
    saved = {'base_url': creds.base_url, 'model': saved_model, 'input_price': saved_input, 'output_price': 50}
    if saved_cache != 'absent':
        saved['cached_input_price'] = saved_cache
    services.store.update_project_state(services.project_id, openai_model_config=saved)
    runtime = OpenAIRuntime(services.gateway, creds)
    assert runtime.creds.cached_input_price == expected
    asyncio.run(runtime.close())


def test_model_switch_clears_inapplicable_cache_rate_and_persists_explicit_choice(services):
    from qumus2.agents.openai_runtime import OpenAIRuntime

    creds = OpenAICredentials('offline', model_id='astra', input_price=10, output_price=50,
                            cached_input_price=1)
    runtime = OpenAIRuntime(services.gateway, creds)

    async def exercise():
        await runtime.select_model('astra', 10, 50)
        assert runtime.model_settings()['cached_input_price'] == 1
        await runtime.select_model('astra', 20, 50)
        assert runtime.creds.cached_input_price is None
        await runtime.select_model('astra', 10, 50, cached_input_price=1)
        await runtime.select_model('other', 10, 50)
        assert runtime.creds.cached_input_price is None
        await runtime.select_model('other', 10, 50, cached_input_price=2)
        assert runtime.creds.cached_input_price == 2
        await runtime.close()

    asyncio.run(exercise())
    restored = OpenAIRuntime(services.gateway, creds)
    assert restored.creds.model_id == 'other' and restored.creds.cached_input_price == 2
    asyncio.run(restored.close())
