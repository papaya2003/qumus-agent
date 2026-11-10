"""Provider usage and capacity snapshots, using the real SDK over mock HTTP."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from qumus2.agents.model_context import context_limits
from qumus2.agents.openai_credentials import OpenAICredentials, load_openai_credentials
from qumus2.agents.token_usage import reported_usage


def test_usage_preserves_missing_partial_and_zero_and_does_not_subtract_cache():
    assert reported_usage(None)['input_tokens'] is None
    assert reported_usage({'total_tokens': 100})['total_tokens'] is None
    partial = reported_usage({'prompt_tokens': 100})
    assert partial['usage_status'] == 'partial' and partial['output_tokens'] is None
    result = reported_usage({'input_tokens': 100, 'output_tokens': 0, 'total_tokens': 100,
                             'input_tokens_details': {'cached_tokens': 80},
                             'output_tokens_details': {'reasoning_tokens': 0}})
    assert result['input_tokens'] == result['total_tokens'] == 100
    assert result['output_tokens'] == 0 and result['usage_status'] == 'reported'
    assert result['cached_input_tokens'] == 80
    for invalid in [-1, True, '100', 1.5, 2**53]:
        assert reported_usage({'input_tokens': invalid})['input_tokens'] is None


def test_context_config_is_model_and_endpoint_specific(monkeypatch, tmp_path):
    for name in ['QUMUS_OPENAI_API_KEY', 'QUMUS_OPENAI_MODEL', 'QUMUS_OPENAI_CONTEXT_WINDOW_TOKENS']:
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / '.env'
    env.write_text('QUMUS_OPENAI_API_KEY=test\nQUMUS_OPENAI_MODEL=model-a\n'
                   'QUMUS_OPENAI_CONTEXT_WINDOW_TOKENS=32768\n', encoding="utf-8")
    creds = load_openai_credentials(env)
    assert creds.context_window_tokens == 32768
    state = {'openai_context_limits': {creds.base_url: {'model-b': 65536, 'unknown': None},
                                      'https://other.example/v1': {'model-a': 8192}}}
    assert context_limits(state, creds) == {'model-a': 32768, 'model-b': 65536, 'unknown': None}
    for invalid in [0, -1, True, 1.5, '32768']:
        with pytest.raises(ValueError, match='context window'):
            replace(creds, context_window_tokens=invalid)


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('usage_kind', ['reported', 'zero', 'missing', 'partial'])
def test_wire_usage_survives_protocol_stream_mode_and_sdk_defaults(tmp_path, monkeypatch, api, stream, usage_kind):
    pytest.importorskip('agents')
    import httpx
    import openai

    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.config import load_yaml

    sent = []
    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        # Retry must leave the first attempt's usage unknown.
        if len(sent) == 1:
            return httpx.Response(503, headers={'retry-after-ms': '1'}, json={'error': {'message': 'retry'}})
        inp, out = (0, 0) if usage_kind == 'zero' else (1234, 56)
        if api == 'responses':
            usage = {'input_tokens': inp, 'output_tokens': out, 'total_tokens': inp + out}
            usage['input_tokens_details'] = {'cached_tokens': 100 if inp else 0}
            usage['output_tokens_details'] = {'reasoning_tokens': 20 if out else 0}
            if usage_kind == 'partial':
                usage.pop('output_tokens')
            body = {'id': 'r1', 'object': 'response', 'created_at': 0, 'model': payload['model'],
                    'status': 'completed', 'output': [{'type': 'message', 'id': 'msg1', 'role': 'assistant',
                    'status': 'completed', 'content': [{'type': 'output_text', 'text': 'Done.', 'annotations': []}]}],
                    'usage': None if usage_kind == 'missing' else usage}
            chunks = [{'type': 'response.completed', 'sequence_number': 0, 'response': body}]
        else:
            usage = {'prompt_tokens': inp, 'completion_tokens': out, 'total_tokens': inp + out,
                     'prompt_tokens_details': {'cached_tokens': 100 if inp else 0}}
            if usage_kind == 'partial':
                usage.pop('completion_tokens')
            body = {'id': 'r1', 'object': 'chat.completion', 'created': 0, 'model': payload['model'],
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Done.'},
                                 'finish_reason': 'stop'}], 'usage': None if usage_kind == 'missing' else usage}
            chunks = [{**body, 'object': 'chat.completion.chunk', 'usage': None,
                       'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'Done.'}, 'finish_reason': None}]},
                      {**body, 'object': 'chat.completion.chunk', 'choices': [
                          {'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
        if not stream:
            return httpx.Response(200, json=body)
        if api == 'chat_completions':
            assert payload['stream_options']['include_usage'] is True
        data = ''.join(f'data: {json.dumps(c)}\n\n' for c in chunks) + 'data: [DONE]\n\n'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=data)

    monkeypatch.setattr(openai, 'DefaultAsyncHttpxClient',
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    services = build_services(tmp_path / 'run.db', seed=1)
    cfg = load_yaml('agents.yaml')
    cfg['openai']['min_request_interval_s'] = 0
    creds = OpenAICredentials('test', model_id='model-a', base_url='https://gateway.example/v1',
        api=api, stream=stream, input_price=1, output_price=2, context_window_tokens=32768,
        budget_path=str(tmp_path / 'budget.db'))
    rt = OpenAIRuntime(services.gateway, creds, config=cfg)

    async def go():
        try:
            await rt.send('Inspect usage only')
        finally:
            await rt.close()
    asyncio.run(go())
    events = services.store.events_after(0, limit=10000)
    requests = [e for e in events if e.kind == 'agent.request']
    reports = [e for e in events if e.kind == 'agent.request_usage']
    failures = [e for e in events if e.kind == 'agent.request_failure']
    assert failures[0].payload['http_status'] == 503
    assert failures[0].payload['request_id'] == requests[0].payload['request_id']
    # Partial malformed payloads can be refused by the SDK before completion;
    # they must never become a fabricated full measurement.
    if usage_kind == 'partial' and not reports:
        assert any(e.kind == 'agent.error' for e in events)
        return
    assert len(requests) == 2 and len(reports) == 1
    report = reports[0].payload
    assert report['request_id'] == requests[-1].payload['request_id']
    assert report['request_id'] != requests[0].payload['request_id']
    assert report['call_id'] == requests[0].payload['call_id']
    assert report['context_window_tokens'] == 32768 and report['max_output_tokens'] == 4096
    assert report['model'] == 'model-a' and reports[0].agent == 'orchestrator'
    if usage_kind == 'missing':
        assert report['usage_status'] == 'unavailable'
        assert report['input_tokens'] is None and report['output_tokens'] is None
    elif usage_kind == 'partial':
        assert report['input_tokens'] == 1234 and report['output_tokens'] is None
    else:
        assert report['input_tokens'] == inp_expected(usage_kind)
        assert report['output_tokens'] == (0 if usage_kind == 'zero' else 56)
        assert report['usage_status'] == 'reported'
    assert 'Inspect usage only' not in json.dumps(report)
    budget_events = [e.payload for e in events if e.kind == 'agent.usage']
    assert all(p['token_measurement'] == 'provider_usage_v1' for p in budget_events)
    assert all('cost_usd' not in e.payload for e in reports)
    from qumus2.ui.billing import calibrated_estimate

    calibrated = calibrated_estimate(services.store, {
        'verified_at': '2026-10-28T00:00:00Z',
        'by_model': [{'model': 'model-a', 'usd': 4, 'tokens': 1_000_000}]},
        gateway='https://gateway.example/v1')
    if usage_kind in {'missing', 'partial'}:
        assert calibrated['usd'] is None and calibrated['requests'] == 0
    else:
        assert calibrated['requests'] == 1 and calibrated['unpriced_requests'] == 1
        assert calibrated['usd'] == pytest.approx(0 if usage_kind == 'zero' else 1290 * 4 / 1_000_000)


def inp_expected(kind):
    return 0 if kind == 'zero' else 1234


def test_switch_keeps_per_model_limits_across_restart_without_changing_old_requests(tmp_path, monkeypatch):
    pytest.importorskip('agents')
    import httpx
    import openai

    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.config import load_yaml

    def respond(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={'id': 'r1', 'object': 'response', 'created_at': 0,
            'model': body['model'], 'status': 'completed', 'output': [],
            'usage': {'input_tokens': 40, 'output_tokens': 10, 'total_tokens': 50}})
    monkeypatch.setattr(openai, 'DefaultAsyncHttpxClient',
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    services = build_services(tmp_path / 'run.db', seed=1)
    cfg = load_yaml('agents.yaml')
    cfg['openai']['min_request_interval_s'] = 0
    creds = OpenAICredentials('test', model_id='a', stream=False, input_price=1, output_price=2,
                             context_window_tokens=1000, budget_path=str(tmp_path / 'budget.db'))
    rt = OpenAIRuntime(services.gateway, creds, config=cfg)
    async def go():
        await rt.send('first')
        old_metrics = rt._metrics
        assert old_metrics.prefix_comparison._previous
        await rt.select_model('b', 1, 2, context_window_tokens=2000)
        assert not old_metrics.prefix_comparison._previous
        await rt.send('second')
        await rt.select_model('a', 1, 2)
        assert rt.creds.context_window_tokens == 1000
        await rt.send('third')
        await rt.select_model('c', 1, 2)
        assert rt.creds.context_window_tokens is None
        await rt.send('fourth')
        await rt.select_model('b', 1, 2)
        await rt.close()
    asyncio.run(go())
    reports = [e.payload for e in services.store.events_after(0, limit=10000) if e.kind == 'agent.request_usage']
    assert [(p['model'], p['context_window_tokens']) for p in reports] == [('a', 1000), ('b', 2000), ('a', 1000), ('c', None)]
    assert len({p['session_id'] for p in reports}) == len({p['request_id'] for p in reports}) == 4
    restarted = OpenAIRuntime(services.gateway, creds)
    assert restarted.creds.model_id == 'b' and restarted.creds.context_window_tokens == 2000
    assert restarted.model_settings()['context_limits'] == {'a': 1000, 'b': 2000, 'c': None}
    different = OpenAIRuntime(services.gateway, replace(creds, base_url='https://other.example/v1', context_window_tokens=None))
    assert different.creds.context_window_tokens is None
    asyncio.run(different.close())
    asyncio.run(restarted.close())


@pytest.mark.parametrize('terminal', ['response.incomplete', 'response.failed'])
def test_terminal_stream_failure_retains_reported_usage(tmp_path, monkeypatch, terminal):
    pytest.importorskip('agents')
    import httpx
    import openai

    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.config import load_yaml

    def respond(request):
        body = {'id': 'r1', 'object': 'response', 'created_at': 0, 'model': 'test',
                'status': terminal.removeprefix('response.'), 'output': [],
                'usage': {'input_tokens': 1000, 'output_tokens': 4096, 'total_tokens': 5096},
                'incomplete_details': {'reason': 'max_output_tokens'}}
        event = {'type': terminal, 'sequence_number': 0, 'response': body}
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              content=f'data: {json.dumps(event)}\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(openai, 'DefaultAsyncHttpxClient',
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    services = build_services(tmp_path / 'run.db', seed=1)
    cfg = load_yaml('agents.yaml')
    cfg['openai']['min_request_interval_s'] = 0
    rt = OpenAIRuntime(services.gateway, OpenAICredentials('test', model_id='test', stream=True,
        input_price=1, output_price=2, budget_path=str(tmp_path / 'budget.db')), config=cfg)
    async def go():
        try:
            with pytest.raises(RuntimeError, match='terminal event'):
                await rt.send('test terminal usage')
        finally:
            await rt.close()
    asyncio.run(go())
    events = services.store.events_after(0, limit=10000)
    assert any(e.kind == 'agent.error' for e in events)
    reports = [e.payload for e in events if e.kind == 'agent.request_usage']
    assert len(reports) == 1 and reports[0]['input_tokens'] == 1000 and reports[0]['output_tokens'] == 4096
    failures = [e.payload for e in events if e.kind == 'agent.request_failure']
    assert len(failures) == 1
    assert failures[0]['failure_kind'] == terminal.replace('.', '_')
    assert failures[0]['http_status'] == 200
    assert failures[0]['request_id'] == reports[0]['request_id']
