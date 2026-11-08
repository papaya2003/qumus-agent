"""Administrator catalog matching and request snapshots, without external calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest

from qumus2.agents.model_context import context_limits
from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.config import load_yaml

BASE = 'https://physics.bytefuture.ai/v1'
CREDS = OpenAICredentials('offline-only', base_url=BASE, model_id='openai/gpt-6-astra',
                          input_price=1, output_price=2, stream=False)


def test_all_administrator_rows_match_the_owner_supplied_context_projection():
    models = load_yaml('model_context_limits.yaml')['models']
    # Independently calculated from the owner's original tab-separated attachment.
    encoded = json.dumps(models, sort_keys=True, separators=(',', ':')).encode()
    assert hashlib.sha256(encoded).hexdigest() == '7d2465ff09aac4387f0ce64681c98ecf456a53aa865ab8ac8cca932a14d89c4e'
    assert len(models) == 91 and sum(v == 0 for v in models.values()) == 20
    resolved = context_limits({}, CREDS)
    assert resolved == {model: value or None for model, value in models.items()}
    assert resolved['openai/gpt-6-astra'] == 1050000
    assert resolved['openai-codex/gpt-6-astra'] == 872000
    assert resolved['github-copilot/gpt-6-astra'] == 1000000
    assert resolved['olares-qumusphusis/qwen-3.8-27b'] == 131072
    assert resolved['openai/gpt-image-2'] is None
    # No suffix matching: an unlisted alias does not borrow another route's limit.
    assert resolved.get('gpt-6-astra') is None
    assert resolved.get('anthropic/claude-fable-5') is None


@pytest.mark.parametrize('base', ['https://other.example/v1', 'https://physics.bytefuture.ai/other',
                                'https://physics.bytefuture.ai.evil.example/v1'])
def test_catalog_never_applies_to_another_deployment(base):
    assert context_limits({}, replace(CREDS, base_url=base)) == {CREDS.model_id: None}


def test_manual_override_and_unknown_override_take_precedence_over_defaults():
    env = replace(CREDS, context_window_tokens=64000)
    state = {'openai_context_limits': {BASE: {CREDS.model_id: 32000,
        'openai-codex/gpt-6-astra': None}}}
    assert context_limits({}, env)[CREDS.model_id] == 64000
    resolved = context_limits(state, env)
    assert resolved[CREDS.model_id] == 32000
    assert resolved['openai-codex/gpt-6-astra'] is None
    assert resolved['github-copilot/gpt-6-astra'] == 1000000


def test_every_model_switch_resolves_without_a_manual_limit_or_network(services):
    pytest.importorskip('agents')
    from qumus2.agents.openai_runtime import OpenAIRuntime

    runtime = OpenAIRuntime(services.gateway, CREDS)
    async def go():
        for model, value in load_yaml('model_context_limits.yaml')['models'].items():
            await runtime.select_model(model, 1, 2)
            settings = runtime.model_settings()
            assert settings['context_window_tokens'] == (value or None)
            assert settings['context_limit_source'] == ('administrator_table' if value else None)
        await runtime.close()
    asyncio.run(go())
    state = services.store.get_project(services.project_id)['state']
    assert state['openai_context_limits'][BASE] == {}  # defaults are not frozen as overrides
    assert not [e for e in services.store.events_after(0, limit=10000) if e.kind == 'agent.request']


def test_switch_request_usage_overrides_reset_and_restart(tmp_path, monkeypatch):
    pytest.importorskip('agents')
    import httpx
    import openai

    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services

    def respond(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={'id': 'r', 'object': 'response', 'created_at': 0,
            'model': body['model'], 'status': 'completed', 'output': [],
            'usage': {'input_tokens': 80000, 'output_tokens': 1000, 'total_tokens': 81000}})

    monkeypatch.setattr(openai, 'DefaultAsyncHttpxClient',
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    services = build_services(tmp_path / 'run.db', seed=1)
    config = load_yaml('agents.yaml')
    config['openai']['min_request_interval_s'] = 0
    creds = replace(CREDS, budget_path=str(tmp_path / 'budget.db'))
    runtime = OpenAIRuntime(services.gateway, creds, config=config)
    qwen = 'olares-qumusphusis/qwen-3.8-27b'

    async def go():
        await runtime.send('first')
        await runtime.select_model('openai-codex/gpt-6-astra', 1, 2)
        await runtime.send('second')
        await runtime.select_model(qwen, 1, 2)
        await runtime.send('third')
        await runtime.select_model(qwen, 1, 2, context_window_tokens=100000)
        await runtime.send('override')
        await runtime.select_model(qwen, 1, 2, context_window_tokens=None)
        await runtime.send('unknown')
        await runtime.select_model(qwen, 1, 2, context_window_tokens=131072)
        await runtime.send('restored')
        await runtime.close()
    asyncio.run(go())
    usage = [e.payload for e in services.store.events_after(0, limit=10000)
             if e.kind == 'agent.request_usage']
    assert [u['context_window_tokens'] for u in usage] == [1050000, 872000, 131072, 100000, None, 131072]
    assert [u['context_limit_source'] for u in usage] == [
        'administrator_table', 'administrator_table', 'administrator_table', 'configured', None,
        'administrator_table']
    assert all(u['input_tokens'] + u['output_tokens'] == 81000 for u in usage)
    assert len({u['session_id'] for u in usage}) == 6
    restarted = OpenAIRuntime(services.gateway, creds)
    assert restarted.creds.model_id == qwen and restarted.creds.context_window_tokens == 131072
    assert restarted.model_settings()['context_limit_source'] == 'administrator_table'
    assert services.store.get_project(services.project_id)['state']['openai_context_limits'][BASE] == {}
    asyncio.run(restarted.close())
