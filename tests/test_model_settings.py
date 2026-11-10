"""Model selection is configuration only; offline SDK loops verify the next turn."""
from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip('agents')
from agents.testing import ScriptedModel, assistant_message  # noqa: E402

from qumus2.agents.model_settings import ModelCatalog  # noqa: E402
from qumus2.agents.openai_credentials import OpenAICredentials  # noqa: E402
from qumus2.agents.openai_runtime import OpenAIRuntime  # noqa: E402
from qumus2.app import build_services  # noqa: E402
from qumus2.config import load_yaml  # noqa: E402
from qumus2.ui.app import create_app  # noqa: E402

CREDS = OpenAICredentials('test-key-never-sent', base_url='https://physics.bytefuture.ai/v1',
                          model_id='openai/test', input_price=1, output_price=2)


def test_catalog_lists_all_types_and_preserves_unknown_rates(monkeypatch):
    calls = []
    rows = [
        {'name': 'openai/test', 'modalities': ['multimodal'], 'is_free': False,
         'input_price_per_million': 0, 'output_price_per_million': 0},
        {'name': 'other/text', 'modalities': ['text'], 'input_price_per_million': 200000,
         'output_price_per_million': 1200000},
        {'name': 'image/model', 'modalities': ['image']},
    ]

    class Opener:
        def open(self, req, timeout):
            calls.append((req.full_url, req.get_method()))
            return io.StringIO(json.dumps(rows))

    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    cat = ModelCatalog(CREDS)
    found = cat.list()
    assert found == cat.list()
    assert calls == [('https://physics.bytefuture.ai/api/models', 'GET'), ('https://physics.bytefuture.ai/v1/models', 'GET')]
    assert next(r for r in found if r['id'] == 'olares-qumusphusis/qwen-3.8-27b')['context_window_tokens'] == 131072
    found = [r for r in found if r['id'] in {'image/model', 'openai/test', 'other/text'}]
    assert [r['id'] for r in found] == ['image/model', 'openai/test', 'other/text']
    assert found[0]['conversational'] is False and found[0]['modalities'] == ['image']
    assert found[1]['input_price'] is None and found[1]['vision']
    assert found[2]['input_price'] == .2 and found[2]['output_price'] == 1.2


def test_switch_uses_next_model_fresh_context_and_retains_spend_and_lab(services):
    models = {'openai/test': ScriptedModel([[assistant_message('Before')]]),
              'anthropic/test': ScriptedModel([[assistant_message('After')]])}
    names = []

    class Provider:
        def get_model(self, name):
            names.append(name)
            return models[name]

    cfg = load_yaml('agents.yaml')
    cfg['openai']['min_request_interval_s'] = 0
    rt = OpenAIRuntime(services.gateway, CREDS, model_provider=Provider(), config=cfg)
    services.store.emit('agent.usage', {'cost_usd': 1.5})
    samples = services.store.list_samples()

    async def go():
        await rt.send('old private conversation')
        old_session = rt.session_id
        before_calls = len(models['openai/test'].calls)
        await rt.select_model('anthropic/test', 3, 4)
        assert rt.session_id != old_session
        assert len(models['openai/test'].calls) == before_calls and not models['anthropic/test'].calls
        await rt.send('new comparison')
        assert 'old private conversation' not in str(models['anthropic/test'].first_call.input)
        await rt.close()

    asyncio.run(go())
    assert {'openai/test', 'anthropic/test'} <= set(names)
    assert services.store.spent_usd() >= 1.5
    assert services.store.list_samples() == samples
    usage = [e.payload for e in services.store.events_after(0, limit=10000) if e.kind == 'agent.usage']
    assert usage[-1]['model'] == 'anthropic/test'
    restarted = OpenAIRuntime(services.gateway, CREDS)
    assert restarted.creds.model_id == 'anthropic/test' and restarted.creds.input_price == 3
    assert restarted.session_id == rt.session_id
    asyncio.run(restarted.close())


def test_api_switch_validation_busy_and_billing_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(ModelCatalog, 'list', lambda self: [{'id': 'openai/test'}, {'id': 'anthropic/test'},
                                                       {'id': 'image/test', 'conversational': False}])
    s = build_services(tmp_path / 'run.db', seed=1)
    s.runtime = OpenAIRuntime(s.gateway, CREDS)
    s.driver.runtime = s.runtime
    app = create_app(s, start_driver=False)
    c = TestClient(app)  # no lifespan health polling or inference
    choice = {'model': 'anthropic/test', 'input_price': 3, 'output_price': 4}
    assert c.get('/api/models').json()['model'] == 'openai/test'
    assert c.post('/api/model', json={**choice, 'model': 'not-in-catalog'}).status_code == 400
    assert c.post('/api/model', json={**choice, 'model': 'image/test'}).status_code == 400
    assert c.post('/api/model', json={**choice, 'input_price': -1}).status_code == 422
    for invalid in [0, -1, 1.5, True, '32000']:
        assert c.post('/api/model', json={**choice, 'context_window_tokens': invalid}).status_code == 422
    switched = c.post('/api/model', json=choice)
    assert switched.status_code == 200 and switched.json()['model'] == choice['model']
    assert c.get('/api/health').json()['model'] == choice['model']
    assert c.post('/api/model', json={**choice, 'model': CREDS.model_id, 'input_price': 1,
                                     'output_price': 2}).status_code == 200
    s.runtime._busy = True
    assert c.post('/api/model', json=choice).status_code == 409
    s.runtime._busy = False
    run = s.store.create_run('inspect_bench', {}, project_id=s.project_id)
    for state in ['queued', 'running', 'paused', 'waiting_human', 'parked']:
        s.store.set_run_status(run, state)
        assert c.post('/api/model', json=choice).status_code == 409
    s.store.set_run_status(run, 'cancelled')
    s.store.add_instruction('pending task', project_id=s.project_id)
    assert c.post('/api/model', json=choice).status_code == 409
    assert s.runtime.creds == CREDS
    assert c.get('/api/billing').json()['status'] == 'unavailable'
    saved = {'source_url': 'https://physics.bytefuture.ai/dashboard/usage',
             'verified_at': '2026-10-25T20:00:00Z', 'total_usd': 4.25,
             'period': 'Past 30 days', 'scope': 'Account', 'by_model': []}
    Path(s.store.path).with_name('gateway-billing.json').write_text(json.dumps(saved), encoding="utf-8")
    bill = c.get('/api/billing').json()
    assert bill['status'] == 'snapshot' and bill['snapshot'] == saved
    saved['by_model'] = [{'model': CREDS.model_id, 'usd': 4, 'requests': 10, 'tokens': 1_000_000}]
    Path(s.store.path).with_name('gateway-billing.json').write_text(json.dumps(saved), encoding="utf-8")
    s.store.emit('agent.request_usage', {'provider': 'openai', 'model': CREDS.model_id,
                 'gateway': 'https://physics.bytefuture.ai/v1',
                 'request_id': 'calibrated', 'input_tokens': 900, 'output_tokens': 100,
                 'measurement': 'provider_usage_v1', 'usage_status': 'reported'})
    assert c.get('/api/billing').json()['estimate']['usd'] == .004
    assert CREDS.api_key not in c.get('/api/models').text + c.get('/api/billing').text
    # Changing endpoint cannot import a previous endpoint's saved selection.
    s.store.update_project_state(s.project_id, openai_model_config={
        'model': 'anthropic/test', 'input_price': 3, 'output_price': 4, 'base_url': CREDS.base_url})
    different = OpenAIRuntime(s.gateway, replace(CREDS, base_url='https://api.openai.com/v1'))
    assert different.creds.model_id == 'openai/test'
    asyncio.run(different.close())


@pytest.mark.parametrize('status', ['pending', 'running', 'failed', None, 'cancelled'])
def test_switch_refuses_unfinished_plan_between_jobs(services, status):
    rt = OpenAIRuntime(services.gateway, CREDS)
    services.store.set_plan(services.project_id, [
        {'id': 's1', 'title': 'Unresolved experiment', 'owner': 'hands', 'status': status}], author='orchestrator')
    session = rt.session_id
    try:
        with pytest.raises(ValueError, match='Task Plan'):
            asyncio.run(rt.select_model('anthropic/test', 3, 4))
        assert rt.creds == CREDS and rt.session_id == session
    finally:
        asyncio.run(rt.close())


@pytest.mark.parametrize('status', ['done', 'skipped'])
def test_switch_accepts_resolved_plan_without_active_jobs(services, status):
    rt = OpenAIRuntime(services.gateway, CREDS)
    services.store.set_plan(services.project_id, [
        {'id': 's1', 'title': 'Resolved experiment', 'owner': 'hands', 'status': status}], author='orchestrator')
    try:
        asyncio.run(rt.select_model('anthropic/test', 3, 4))
        assert rt.creds.model_id == 'anthropic/test'
    finally:
        asyncio.run(rt.close())
