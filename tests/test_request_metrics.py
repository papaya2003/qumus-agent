"""Outgoing text measurements with mocked HTTP; no model service or private data."""

import asyncio
import copy
import json

import pytest

from qumus2.agents.request_metrics import RequestMetrics, request_text_size


def test_text_counts_unicode_tools_and_history_without_media_or_metadata():
    tools = [{"type": "function", "name": "look", "parameters": {"type": "object"}}]
    payload = {"instructions": "规则🧪", "model": "not-input", "max_output_tokens": 4096,
               "tools": tools, "input": [
                   {"role": "user", "content": [{"type": "input_text", "text": "你好"},
                       {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 50000}]},
                   {"type": "function_call", "id": "ignore-id", "call_id": "ignore-call",
                    "name": "look", "arguments": '{"x":1}'},
                   {"type": "function_call_output", "call_id": "ignore-call", "output": "found"},
                   {"type": "reasoning", "encrypted_content": "hidden" * 1000,
                    "summary": [{"type": "summary_text", "text": "summary"}]},
               ]}
    before = copy.deepcopy(payload)
    result = request_text_size(payload, "responses")
    assert result["instructions_chars"] == 3  # code points, not UTF-8 bytes or UTF-16 units
    assert result["conversation_chars"] == 2 + 4 + 7 + 5 + 7
    assert result["tools_chars"] == len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    assert result["text_chars"] == 28 + result["tools_chars"]
    assert result["image_count"] == 1
    assert payload == before


def test_chat_count_includes_system_tool_calls_results_and_images():
    payload = {"messages": [
        {"role": "system", "content": "rules"},
        {"role": "developer", "content": "more"},
        {"role": "user", "content": [{"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "ignored", "type": "function", "function": {"name": "look", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "ignored", "content": "done"},
    ]}
    assert request_text_size(payload, "chat_completions") == {
        "text_chars": 21, "instructions_chars": 9, "conversation_chars": 12,
        "tools_chars": 0, "image_count": 1,
        "unit": "unicode_code_points", "measurement": "outgoing_text_v1",
    }


def test_request_hook_records_retries_and_unknown_without_storing_prompts():
    httpx = pytest.importorskip("httpx")
    emitted = []

    class Store:
        def emit(self, kind, payload, **kwargs):
            emitted.append((kind, payload, kwargs))

    hook = RequestMetrics(Store(), lambda: {"session_id": "s1", "call_id": "c1", "agent": "eyes"})

    async def go():
        await hook(httpx.Request("GET", "https://example.invalid/v1/models"))
        for retry in (0, 1):
            await hook(httpx.Request("POST", "https://example.invalid/v1/responses",
                headers={"x-stainless-retry-count": str(retry), "Authorization": "secret-key"},
                json={"model": "test", "input": "private-prompt"}))
        await hook(httpx.Request("POST", "https://example.invalid/v1/responses", content=b"invalid"))

    asyncio.run(go())
    assert len(emitted) == 3
    assert [p["attempt"] for _, p, _ in emitted[:2]] == [1, 2]
    assert len({p["request_id"] for _, p, _ in emitted}) == 3
    assert all(p["call_id"] == "c1" and meta["agent"] == "eyes" for _, p, meta in emitted)
    assert emitted[2][1]["text_chars"] is None
    assert "private-prompt" not in str(emitted) and "secret-key" not in str(emitted)


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
@pytest.mark.parametrize("stream", [False, True])
def test_runtime_records_actual_requests_for_manager_specialist_and_followup(tmp_path, monkeypatch, api, stream):
    pytest.importorskip("agents")
    import httpx
    import openai

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.config import load_yaml

    sent = []
    steps = [("delegate_eyes", {"input": "s1: inspect the bench"}), ("inspect_bench", {}),
             "Inspection done.", "All done.", "I remember the earlier request."]

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        step = steps[len(sent) - 1]
        ident = f"r{len(sent)}"
        usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
        if isinstance(step, tuple):
            name, args = step
            output = [{"type": "function_call", "id": ident, "call_id": ident,
                       "name": name, "arguments": json.dumps(args), "status": "completed"}]
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        else:
            output = [{"type": "message", "id": ident, "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": step, "annotations": []}]}]
            message = {"role": "assistant", "content": step}
        if api == "responses":
            body = {"id": ident, "created_at": 0, "model": "test-model", "object": "response",
                    "status": "completed", "output": output, "usage": usage, "parallel_tool_calls": False}
            if not stream:
                return httpx.Response(200, json=body)
            chunks = [{"type": "response.completed", "sequence_number": 0, "response": body}]
        else:
            finish = "tool_calls" if isinstance(step, tuple) else "stop"
            body = {"id": ident, "created": 0, "model": "test-model", "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
            if not stream:
                return httpx.Response(200, json=body)
            if "tool_calls" in message:
                message["tool_calls"][0]["index"] = 0
            chunks = [{**body, "object": "chat.completion.chunk", "usage": None,
                       "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                      {**body, "object": "chat.completion.chunk",
                       "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
        data = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient",
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    services = build_services(tmp_path / "run.db", seed=2)
    services.store.set_plan(services.project_id, [{"id": "s1", "title": "Look", "owner": "eyes"}], author="orchestrator")
    creds = OpenAICredentials("fake-not-sent", base_url="https://gateway.example/v1", api=api,
                             model_id="test-model", input_price=1, output_price=2, stream=stream,
                             budget_path=str(tmp_path / "budget.db"))
    cfg = load_yaml("agents.yaml")
    cfg["openai"]["min_request_interval_s"] = 0
    rt = OpenAIRuntime(services.gateway, creds, config=cfg)
    allowances = []
    original_allowance = rt._request_allowance

    def measured_allowance(agent, instructions, items):
        assert items[-1]['role'] == 'developer'
        assert 'authoritative over older state' in items[-1]['content']
        assert sum(item.get('role') == 'developer' for item in items) == 1
        allowance = original_allowance(agent, instructions, items)
        assert allowance > original_allowance(agent, instructions, items[:-1])
        allowances.append(allowance)
        return allowance

    rt._request_allowance = measured_allowance

    async def go():
        try:
            await rt.send("Inspect the bench")
            await rt.send("Remember our earlier work?")
        finally:
            await rt.close()

    asyncio.run(go())
    events = [e for e in services.store.events_after(0, limit=10000) if e.kind == "agent.request"]
    assert len(sent) == len(events) == 5
    assert len(allowances) == 5  # Tail state participates before HTTP, including specialists.
    assert [e.agent for e in events] == ["orchestrator", "eyes", "eyes", "orchestrator", "orchestrator"]
    assert all(e.payload["session_id"] == rt.session_id for e in events)
    assert all(e.payload["tools_chars"] > 0 and e.payload["instructions_chars"] > 0 for e in events)
    for event in events:
        composition = event.payload['input_composition']
        parts = {p['category']: p for p in composition['components']}
        assert composition['status'] == 'available'
        assert parts['system_instructions']['estimated_tokens'] > 0
        assert parts['lab_snapshot']['estimated_tokens'] > 0
        assert parts['current_task']['estimated_tokens'] > 0
        assert parts['tool_definitions']['estimated_tokens'] > 0
        assert composition['media_count'] == event.payload['image_count']
        assert event.payload['gateway'] == 'https://gateway.example/v1'
    assert {p['category']: p for p in events[2].payload['input_composition']['components']}['tool_results']['text_bytes'] > 0
    # Both transports must actually send tool-returned pixels, after conversion.
    assert events[2].payload["image_count"] > 0
    assert "data:image/" in json.dumps(sent[2])
    if api == "chat_completions":
        assert sent[2]["messages"][-1]['role'] == 'developer'
        evidence = sent[2]["messages"][-2]
        assert evidence["role"] == "user"
        assert "tool_call_id=r2" in evidence["content"][0]["text"]
        assert any(p["type"] == "image_url" for p in evidence["content"])
        assert sent[2]["messages"][-3]["tool_call_id"] == "r2"
    assert events[4].payload["text_chars"] > events[0].payload["text_chars"]
    assert "Inspect the bench" in json.dumps(sent[4])
    assert "data:image/" not in str([e.payload for e in events])
    assert len({e.payload["request_id"] for e in events}) == 5
    reports = [e for e in services.store.events_after(0, limit=10000) if e.kind == 'agent.request_usage']
    assert len(reports) == 5
    assert [e.payload['request_id'] for e in reports] == [e.payload['request_id'] for e in events]
    assert [e.agent for e in reports] == [e.agent for e in events]
    assert all(e.payload['input_tokens'] == 100 and e.payload['output_tokens'] == 10 for e in reports)
    assert all(e.payload['gateway'] == 'https://gateway.example/v1' for e in reports)
    budget_reports = [e for e in services.store.events_after(0, limit=10000) if e.kind == 'agent.usage']
    assert all(e.payload['gateway'] == 'https://gateway.example/v1' for e in budget_reports)


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
@pytest.mark.parametrize('interval', [3, 60])
def test_state_changed_during_pacing_reaches_wire_composition_and_allowance(tmp_path, monkeypatch, api, interval):
    import httpx
    import openai

    from qumus2.agents.context_composition import _plain_text
    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.config import load_yaml

    sent, reserved_snapshots, waits = [], [], []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if api == 'responses':
            body = {'id': f'r{len(sent)}', 'object': 'response', 'created_at': 0,
                    'status': 'completed', 'model': payload['model'], 'output': [],
                    'usage': {'input_tokens': 100, 'output_tokens': 1, 'total_tokens': 101}}
        else:
            body = {'id': f'r{len(sent)}', 'object': 'chat.completion', 'created': 0,
                    'model': payload['model'], 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': 'Read current state.'}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 1, 'total_tokens': 101}}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(openai, 'DefaultAsyncHttpxClient',
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    services = build_services(tmp_path / 'paced.db', seed=2)
    services.store.set_plan(services.project_id, [{'id': 's1', 'title': 'Inspect', 'owner': 'eyes'}],
                            author='orchestrator')
    cfg = load_yaml('agents.yaml')
    cfg['openai']['min_request_interval_s'] = interval
    rt = OpenAIRuntime(services.gateway, OpenAICredentials('offline', model_id='test-model',
        api=api, stream=False, input_price=1, output_price=2, base_url='https://example.invalid/v1',
        budget_path=str(tmp_path / 'budget.db')), config=cfg)

    async def fake_wait(delay):
        if delay <= 0:
            return  # SDK cooperative yields are not model pacing.
        waits.append(delay)
        # Represents independent lab/plan progress while this model is throttled.
        services.store.set_step_status(services.project_id, 's1', 'done', agent='eyes')
        services.store.update_project_state(services.project_id, human_constraint='keep chip_004 unused after wait')

    monkeypatch.setattr(asyncio, 'sleep', fake_wait)
    original = rt._request_allowance

    def allowance(agent, instructions, items):
        reserved_snapshots.append(items[-1]['content'])
        return original(agent, instructions, items)

    rt._request_allowance = allowance

    async def run():
        try:
            await rt.send('Read state before wait')
            await rt.send('Read state after wait')
            assert not any(item.get('role') == 'developer' for item in await rt._session.get_items())
        finally:
            await rt.close()

    asyncio.run(run())
    assert len(waits) == 1 and interval - 1 <= waits[0] <= interval
    key = 'input' if api == 'responses' else 'messages'
    first, second = [_plain_text(payload[key][-1]['content']) for payload in sent]
    assert '[pending] s1:' in first and '[done] s1:' not in first
    assert '[done] s1:' in second and '[pending] s1:' not in second
    assert 'keep chip_004 unused after wait' in second
    assert reserved_snapshots == [first, second]
    assert all(payload[key][-1]['role'] == 'developer' for payload in sent)
    assert all(sum(item.get('role') == 'developer' for item in payload[key]) == 1 for payload in sent)
    requests = [event.payload for event in services.store.events_after(0, limit=10000)
                if event.kind == 'agent.request']
    parts = {row['category']: row for row in requests[-1]['input_composition']['components']}
    assert parts['lab_snapshot']['text_bytes'] == len(second.encode())
    assert parts['current_task']['text_bytes'] == len('Read state after wait')
    assert requests[-1]['input_composition']['status'] == 'available'


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
@pytest.mark.parametrize('usage_case', ['cached', 'cache_write_only', 'unavailable'])
def test_buffered_raw_usage_reaches_settlement_without_inventing_cache(tmp_path, monkeypatch, api, usage_case):
    """Exercise the real HTTP/provider/buffering/Runner/hooks path, with no network."""
    pytest.importorskip('agents')
    import sqlite3

    import httpx
    import openai

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.config import load_yaml

    sent = []
    details = {'cached_tokens': 80} if usage_case == 'cached' else {'cache_write_tokens': 80}
    measured = usage_case != 'unavailable'

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert payload.get('stream', False) is False
        if api == 'responses':
            usage = {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                     'input_tokens_details': details}
            body = {'id': 'response-buffered', 'created_at': 0, 'model': 'test-model', 'object': 'response',
                    'status': 'completed', 'parallel_tool_calls': False,
                    'output': [{'type': 'message', 'id': 'message-buffered', 'role': 'assistant',
                                'status': 'completed', 'content': [
                                    {'type': 'output_text', 'text': 'Offline answer', 'annotations': []}]}]}
        else:
            usage = {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110,
                     'prompt_tokens_details': details}
            body = {'id': 'response-buffered', 'created': 0, 'model': 'test-model', 'object': 'chat.completion',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Offline answer'},
                                 'finish_reason': 'stop'}]}
        if measured:
            body['usage'] = usage
        return httpx.Response(200, json=body)

    monkeypatch.setattr(openai, 'DefaultAsyncHttpxClient',
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    services = build_services(tmp_path / 'buffered.db', seed=2)
    ledger = tmp_path / 'budget.db'
    creds = OpenAICredentials('fake-not-sent', base_url='https://gateway.example/v1', api=api,
        model_id='test-model', input_price=10, output_price=50, cached_input_price=1,
        stream=False, budget_path=str(ledger))
    cfg = load_yaml('agents.yaml')
    cfg['openai']['min_request_interval_s'] = 0
    runtime = OpenAIRuntime(services.gateway, creds, config=cfg)

    async def exercise():
        try:
            await runtime.send('Reply without laboratory work')
        finally:
            await runtime.close()

    asyncio.run(exercise())
    events = services.store.events_after(0, limit=10000)
    requests = [e.payload for e in events if e.kind == 'agent.request']
    reports = [e.payload for e in events if e.kind == 'agent.request_usage']
    costs = [e.payload for e in events if e.kind == 'agent.usage']
    assert len(sent) == len(requests) == len(reports) == len(costs) == 1
    assert requests[0]['request_id'] == reports[0]['request_id'] == costs[0]['request_id']
    assert requests[0]['call_id'] == reports[0]['call_id'] == costs[0]['call_id']
    assert reports[0]['usage_status'] == ('reported' if measured else 'unavailable')
    assert reports[0]['input_tokens'] == (100 if measured else None)
    cached = 80 if usage_case == 'cached' else None
    assert reports[0]['cached_input_tokens'] == cached
    assert costs[0]['pricing']['measured_cached_input_tokens'] == cached
    assert costs[0]['pricing']['discounted_cached_input_tokens'] == (cached or 0)
    assert costs[0]['pricing']['basis'] == ('measured_cache_reads' if cached else 'full_input_rate')
    with sqlite3.connect(ledger) as db:
        rows = db.execute('SELECT scope,reserved,actual FROM spend').fetchall()
    assert len(rows) == 1 and rows[0][0] == runtime._budget_scope
    assert rows[0][1] > 4096 * 50 / 1_000_000  # Full input and maximum output reservation.
    if measured:
        expected = .00078 if cached else .0015
        assert costs[0]['cost_usd'] == pytest.approx(expected)
        assert rows[0][2] == pytest.approx(expected)
    else:
        assert reports[0]['output_tokens'] is None
        assert rows[0][2] is None  # No usage: never settle the original reservation as free.
        assert runtime._ledger.total(runtime._budget_scope) == rows[0][1]
    assert any(e.kind == 'agent.message' and e.payload.get('text') == 'Offline answer' for e in events)
    services.store.close()


def test_real_client_retries_are_separate_request_points():
    openai = pytest.importorskip("openai")
    import httpx

    sent, measured = [], []

    class Store:
        def emit(self, kind, payload, **kwargs):
            measured.append(payload)

    def respond(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(503, headers={"retry-after-ms": "1"}, json={"error": {"message": "retry"}})
        return httpx.Response(200, json={"id": "r1", "object": "response", "created_at": 0,
            "model": "test", "status": "completed", "output": []})

    async def go():
        async with openai.AsyncOpenAI(api_key="test", base_url="https://example.invalid/v1",
                max_retries=1, http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(respond), event_hooks={"request": [
                        RequestMetrics(Store(), lambda: {"agent": "hands", "call_id": "one"})]})) as client:
            await client.responses.create(model="test", input="abc")

    asyncio.run(go())
    assert len(sent) == len(measured) == 2
    assert [p["attempt"] for p in measured] == [1, 2]
    assert [p["text_chars"] for p in measured] == [3, 3]
    assert measured[0]["request_id"] != measured[1]["request_id"]


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
@pytest.mark.parametrize("stream", [False, True])
def test_grouped_tool_images_survive_final_sdk_payload_and_retry(tmp_path, monkeypatch, api, stream):
    """Check actual wire content, valid tool ordering, attribution and saved history."""
    import httpx
    import openai

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.config import load_yaml

    images = ["data:image/png;base64," + "A" * 40000, "data:image/png;base64," + "B" * 40000]
    history = [
        {"type": "function_call", "call_id": "look-a", "name": "inspect_bench", "arguments": "{}"},
        {"type": "function_call", "call_id": "look-b", "name": "inspect_bench", "arguments": "{}"},
        # Results need not arrive in call order; each image must retain its own ID.
        {"type": "function_call_output", "call_id": "look-b", "output": [
            {"type": "input_image", "image_url": images[1], "detail": "high"}]},
        {"type": "function_call_output", "call_id": "look-a", "output": [
            {"type": "input_text", "text": "甲🧪"},
            {"type": "input_image", "image_url": images[0], "detail": "low"}]},
    ]
    original = copy.deepcopy(history)
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            return httpx.Response(503, headers={"retry-after-ms": "1"},
                                  json={"error": {"message": "temporary failure"}})
        if api == "responses":
            body = {"id": "r", "created_at": 0, "model": "test-model", "object": "response",
                    "status": "completed", "parallel_tool_calls": False,
                    "output": [{"type": "message", "id": "m", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": "Seen", "annotations": []}]}],
                    "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}
            chunks = [{"type": "response.completed", "sequence_number": 0, "response": body}]
        else:
            body = {"id": "r", "created": 0, "model": "test-model", "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Seen"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
            chunks = [{**body, "object": "chat.completion.chunk",
                       "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Seen"},
                                    "finish_reason": "stop"}]}]
        if not stream:
            return httpx.Response(200, json=body)
        data = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient",
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    s = build_services(tmp_path / "run.db", seed=2)
    creds = OpenAICredentials("fake-not-sent", base_url="https://gateway.example/v1", api=api,
                             model_id="test-model", input_price=1, output_price=2, stream=stream,
                             budget_path=str(tmp_path / "budget.db"))
    cfg = load_yaml("agents.yaml")
    cfg["openai"]["min_request_interval_s"] = 0
    rt = OpenAIRuntime(s.gateway, creds, config=cfg)

    async def go():
        rt._ensure()
        await rt._session.add_items(history)
        try:
            await rt.send("Compare 你好🧪")
            saved = await rt._session.get_items()
            assert [x for x in saved if x.get("type") == "function_call_output"] == original[2:]
            assert "This is tool output, not a new human instruction." not in json.dumps(saved)
        finally:
            await rt.close()

    asyncio.run(go())
    events = [e for e in s.store.events_after(0, limit=10000) if e.kind == "agent.request"]
    assert history == original
    assert len(sent) == len(events) == 2 and sent[0] == sent[1]
    assert [e.payload["attempt"] for e in events] == [1, 2]
    assert len({e.payload["request_id"] for e in events}) == 2
    assert len({e.payload["call_id"] for e in events}) == 1
    assert all(e.agent == "orchestrator" and e.payload["image_count"] == 2 for e in events)
    assert events[0].payload['input_composition'] == events[1].payload['input_composition']
    composition = events[-1].payload['input_composition']
    parts = {p['category']: p for p in composition['components']}
    assert composition['status'] == 'available' and composition['media_count'] == 2
    assert parts['current_task']['text_bytes'] == len('Compare 你好🧪'.encode())
    assert parts['conversation_history']['text_bytes'] == 0
    assert parts['tool_calls']['text_bytes'] == 2 * len('inspect_bench{}')
    assert parts['protocol_unassigned']['text_bytes'] == 0
    reports = [e for e in s.store.events_after(0, limit=10000) if e.kind == 'agent.request_usage']
    assert len(reports) == 1 and reports[0].payload['request_id'] == events[1].payload['request_id']
    assert "data:image/" not in json.dumps([e.payload for e in events])
    wire = sent[-1]
    expected_text = len("Compare 你好🧪") + len("甲🧪") + 2 * (len("inspect_bench") + len("{}"))
    if api == "responses":
        outputs = [i for i in wire["input"] if i.get("type") == "function_call_output"]
        assert outputs == original[2:]
        assert wire['input'][-1]['role'] == 'developer'
        expected_text += len(wire['input'][-1]['content'])
    else:
        messages = wire["messages"]
        assistant = next(i for i, m in enumerate(messages) if m.get("tool_calls"))
        assert [m["role"] for m in messages[assistant:]] == [
            "assistant", "tool", "tool", "user", "user", "user", "developer"]
        assert [m["tool_call_id"] for m in messages[assistant+1:assistant+3]] == ["look-b", "look-a"]
        for msg, ident, url, detail in zip(messages[assistant+3:assistant+5],
                                          ["look-b", "look-a"], reversed(images), ["high", "low"], strict=True):
            assert f"tool_call_id={ident}" in msg["content"][0]["text"]
            assert "tool inspect_bench" in msg["content"][0]["text"]
            assert msg["content"][1] == {"type": "image_url", "image_url": {"url": url, "detail": detail}}
            expected_text += len(msg["content"][0]["text"])
        # Image-only results still have a valid text tool reply.
        placeholder = messages[assistant+1]["content"]
        assert all(part["type"] == "text" for m in messages[assistant+1:assistant+3] for part in m["content"])
        expected_text += sum(len(p["text"]) for p in placeholder)
    assert events[-1].payload["conversation_chars"] == expected_text
    call_bytes = 2 * len('inspect_bench{}')
    # All generated image labels and placeholder text remain tool results.
    expected_result_bytes = len('甲🧪'.encode())
    if api == 'chat_completions':
        expected_result_bytes += sum(len(m['content'][0]['text'].encode())
                                     for m in messages[assistant+3:assistant+5])
        expected_result_bytes += sum(len(p['text'].encode()) for p in placeholder)
    assert parts['tool_results']['text_bytes'] == expected_result_bytes
    assert parts['tool_calls']['text_bytes'] == call_bytes
    tool_chars = len(json.dumps(wire["tools"], ensure_ascii=False, separators=(",", ":")))
    instructions = (wire["instructions"] if api == "responses" else
                    wire["messages"][0]["content"] + wire["messages"][-1]["content"])
    assert events[-1].payload["text_chars"] == len(instructions) + expected_text + tool_chars


@pytest.mark.parametrize("failure,reason,attempts", [
    ("duplicate", "duplicate call ID", 0),
    ("file-id", "require Responses", 0),
    ("gateway-no-vision", "image input not supported", 1),
])
def test_chat_image_failures_are_explicit_without_text_only_fallback(tmp_path, monkeypatch, failure, reason, attempts):
    import httpx
    import openai

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.config import load_yaml

    sent = []
    image = ({"type": "input_image", "file_id": "private-file"} if failure == "file-id" else
             {"type": "input_image", "image_url": "data:image/png;base64,AAAA"})
    history = [
        {"type": "function_call", "call_id": "one", "name": "inspect_bench", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "one", "output": [
            {"type": "input_text", "text": "Observation"}, image]},
    ]
    if failure == "duplicate":
        history.insert(1, dict(history[0]))

    def respond(request):
        sent.append(json.loads(request.content))
        assert failure == "gateway-no-vision"
        assert "data:image/" in json.dumps(sent[-1])
        return httpx.Response(400, json={"error": {"message": "image input not supported", "type": "invalid_request_error"}})

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient",
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    s = build_services(tmp_path / "run.db", seed=2)
    creds = OpenAICredentials("fake-not-sent", base_url="https://gateway.example/v1", api="chat_completions",
                             model_id="test-model", input_price=1, output_price=2, stream=False,
                             budget_path=str(tmp_path / "budget.db"))
    cfg = load_yaml("agents.yaml")
    cfg["openai"]["min_request_interval_s"] = 0
    rt = OpenAIRuntime(s.gateway, creds, config=cfg)

    async def go():
        rt._ensure()
        await rt._session.add_items(history)
        try:
            with pytest.raises(RuntimeError, match=reason):
                await rt.send("Read the image")
        finally:
            await rt.close()

    asyncio.run(go())
    events = s.store.events_after(0, limit=10000)
    measured = [e.payload for e in events if e.kind == "agent.request"]
    assert len(sent) == len(measured) == attempts
    assert any(e.kind == "agent.error" and reason in e.payload["error"] for e in events)
    assert all(p["image_count"] == 1 for p in measured)
    assert "data:image/" not in json.dumps(measured)
