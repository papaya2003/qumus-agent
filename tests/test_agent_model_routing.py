"""Different specialist models execute through the real SDK, entirely offline."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

pytest.importorskip('agents')
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call  # noqa: E402
from agents.usage import Usage  # noqa: E402

from qumus2.agents.model_settings import ModelCatalog  # noqa: E402
from qumus2.agents.openai_credentials import OpenAICredentials  # noqa: E402
from qumus2.agents.openai_runtime import OpenAIRuntime  # noqa: E402
from qumus2.agents.roster import SUBAGENTS  # noqa: E402
from qumus2.app import build_services  # noqa: E402
from qumus2.config import load_yaml  # noqa: E402
from qumus2.ui.app import create_app  # noqa: E402
from qumus2.ui.recording import recorded_models  # noqa: E402

DEFAULT, SPECIALIST = 'openai/gpt-6-astra', 'openai-codex/gpt-6-astra'
CREDS = OpenAICredentials('offline-only', base_url='https://physics.bytefuture.ai/v1',
                         model_id=DEFAULT, input_price=1, output_price=2)
ASSIGNMENT = {'model': SPECIALIST, 'input_price': 3, 'output_price': 6}


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
@pytest.mark.parametrize('stream', [False, True])
def test_mixed_model_wire_composition_keeps_actual_role_identity(tmp_path, monkeypatch, api, stream):
    # Reuse the frozen Context wire fixture rather than a second HTTP simulator.
    from test_request_metrics import (
        test_runtime_records_actual_requests_for_manager_specialist_and_followup as wire_fixture,
    )

    instances = []

    class RoutedRuntime(OpenAIRuntime):
        def __init__(self, gateway, creds, **kwargs):
            gateway.store.update_project_state(gateway.project_id,
                openai_context_limits={creds.base_url: {creds.model_id: 1050000, 'test-specialist': 872000}},
                openai_model_config={'model': creds.model_id, 'input_price': 1, 'output_price': 2,
                    'base_url': creds.base_url,
                    'agent_models': {'eyes': {**ASSIGNMENT, 'model': 'test-specialist'}}})
            super().__init__(gateway, creds, **kwargs)
            instances.append(self)

    monkeypatch.setattr('qumus2.agents.openai_runtime.OpenAIRuntime', RoutedRuntime)
    wire_fixture(tmp_path, monkeypatch, api, stream)
    rt = instances[0]
    events = rt.store.events_after(0, limit=10000)
    requests = [e.payload for e in events if e.kind == 'agent.request']
    assert [p['model'] for p in requests] == [
        'test-model', 'test-specialist', 'test-specialist', 'test-model', 'test-model']
    assert [p['context_window_tokens'] for p in requests] == [1050000, 872000, 872000, 1050000, 1050000]
    assert all(p['input_composition']['status'] == 'available' for p in requests)
    usage = [e.payload for e in events if e.kind == 'agent.usage']
    assert [p['model'] for p in usage] == [p['model'] for p in requests]
    assert [p['input_price'] for p in usage] == [1, 3, 3, 1, 1]
    assert rt.store.spent_usd() == pytest.approx(.00108)


def test_recording_names_failed_specialist_requests_without_pricing_them():
    assert recorded_models([
        {'kind': 'model.changed', 'payload': {'model': 'selected-only'}},
        {'kind': 'agent.request', 'payload': {'model': SPECIALIST}},
        {'kind': 'agent.request_failure', 'payload': {'model': SPECIALIST}},
        {'kind': 'agent.usage', 'payload': {'model': DEFAULT}},
    ]) == sorted([DEFAULT, SPECIALIST])


@pytest.mark.parametrize('role', SUBAGENTS)
def test_every_specialist_builds_with_its_selected_model(services, role):
    provider = SimpleNamespace(get_model=lambda name: ScriptedModel([]))
    rt = OpenAIRuntime(services.gateway, CREDS, model_provider=provider)
    asyncio.run(rt.select_model(DEFAULT, 1, 2, agent_models={role: ASSIGNMENT}))
    rt._ensure()
    assert {name: agent.model for name, agent in rt._agents.items()} == {
        name: SPECIALIST if name == role else DEFAULT for name in rt._agents}
    assert rt.agent_model(role)['context_window_tokens'] == 872000
    asyncio.run(rt.close())


def test_specialist_routing_cost_context_and_persistence(tmp_path):
    s = build_services(tmp_path / 'routing.db', seed=2)
    contexts = []

    async def checked(call):
        contexts.append(rt._request_context.copy())
        return [assistant_message('Inspected')]

    manager = ScriptedModel([
        [function_call('delegate_eyes', {'input': 's1: inspect the bench'}, call_id='d1')],
        [assistant_message('Done')]], default_usage=Usage(input_tokens=100, output_tokens=10))
    specialist = ScriptedModel([
        [function_call('inspect_bench', {}, call_id='e1')], ModelStep.respond(checked)],
        default_usage=Usage(input_tokens=200, output_tokens=20))

    class Provider:
        def get_model(self, name):
            return {DEFAULT: manager, SPECIALIST: specialist}[name]

    cfg = load_yaml('agents.yaml')
    cfg['openai']['min_request_interval_s'] = 0
    rt = OpenAIRuntime(s.gateway, CREDS, model_provider=Provider(), config=cfg)

    async def run():
        await rt.select_model(DEFAULT, 1, 2, agent_models={'eyes': ASSIGNMENT})
        old_session = rt.session_id
        # A default-only save must preserve explicit specialist assignments.
        await rt.select_model(DEFAULT, 1, 2)
        assert rt.agent_model('eyes')['model'] == SPECIALIST
        assert rt.session_id != old_session
        rt._ensure()
        for role, agent in rt._agents.items():
            assert agent.model == (SPECIALIST if role == 'eyes' else DEFAULT)
        plain = SimpleNamespace(name='orchestrator', tools=[])
        expert = SimpleNamespace(name='eyes', tools=[])
        assert rt._request_allowance(expert, 'same', []) == pytest.approx(3 * rt._request_allowance(plain, 'same', []))
        s.store.set_plan(s.project_id, [{'id': 's1', 'title': 'Inspect', 'owner': 'eyes'}], author='orchestrator')
        await rt.send('Inspect the bench')
        assert contexts[-1]['model'] == SPECIALIST
        assert contexts[-1]['context_window_tokens'] == 872000
        assert contexts[-1]['context_limit_source'] == 'administrator_table'
        await rt.close()

    asyncio.run(run())
    manager.assert_complete()
    specialist.assert_complete()
    usage = [e for e in s.store.events_after(0, limit=10000) if e.kind == 'agent.usage']
    assert [e.payload['model'] for e in usage] == [DEFAULT, SPECIALIST, SPECIALIST, DEFAULT]
    assert s.store.spent_usd() == pytest.approx(.00168)
    assert s.store.get_project(s.project_id)['plan'][0]['status'] == 'done'
    restarted = OpenAIRuntime(s.gateway, CREDS)
    assert restarted.agent_model('eyes')['model'] == SPECIALIST
    assert restarted.agent_model('advisor')['inherits_default']
    other = OpenAIRuntime(s.gateway, replace(CREDS, base_url='https://other.example/v1'))
    assert other.agent_model('eyes')['model'] == DEFAULT
    asyncio.run(other.close())
    asyncio.run(restarted.close())


def test_atomic_api_routing_validation_and_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(ModelCatalog, 'list', lambda self: [
        {'id': DEFAULT}, {'id': SPECIALIST}, {'id': 'image/model', 'conversational': False}])
    s = build_services(tmp_path / 'routing.db')
    rt = s.runtime = s.driver.runtime = OpenAIRuntime(s.gateway, CREDS)
    client = TestClient(create_app(s, start_driver=False))
    choice = {'model': DEFAULT, 'input_price': 1, 'output_price': 2,
              'agent_models': {'eyes': ASSIGNMENT}}
    response = client.post('/api/model', json=choice)
    assert response.status_code == 200
    assert response.json()['effective_models']['eyes']['model'] == SPECIALIST
    assert client.get('/api/health').json()['models'] == sorted([DEFAULT, SPECIALIST])
    session, before = rt.session_id, rt.model_settings()
    bad_choices = [
        {**choice, 'agent_models': {'orchestrator': ASSIGNMENT}},
        {**choice, 'agent_models': {'unknown': ASSIGNMENT}},
        {**choice, 'agent_models': {'eyes': {**ASSIGNMENT, 'model': 'missing'}}},
        {**choice, 'agent_models': {'eyes': {**ASSIGNMENT, 'model': 'image/model'}}},
        {**choice, 'agent_models': {'eyes': ASSIGNMENT, 'hands': {**ASSIGNMENT, 'input_price': 4}}},
    ]
    for invalid in bad_choices:
        assert client.post('/api/model', json=invalid).status_code in (400, 409)
        assert rt.session_id == session and rt.model_settings() == before
    # Idle gaps inside an unfinished Task Plan are not a new-plan boundary.
    s.store.set_plan(s.project_id, [{'id': 's1', 'title': 'Inspect', 'owner': 'eyes'}], author='orchestrator')
    assert client.post('/api/model', json={**choice, 'agent_models': {}}).status_code == 409
    assert rt.session_id == session
    s.store.set_plan(s.project_id, [{'id': 's1', 'title': 'Inspect', 'owner': 'eyes', 'status': 'done'}], author='orchestrator')
    assert client.post('/api/model', json={**choice, 'agent_models': {}}).status_code == 200
    assert all(c['model'] == DEFAULT for c in rt.model_settings()['effective_models'].values())
    asyncio.run(rt.close())
