"""Real OpenAI Agents SDK loops, deterministic model responses, no network or keys.

These tests check Qumus' adapter (permissions, delegation, images, state and
stopping). They do not claim that the live model makes the scripted decisions.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

pytest.importorskip("agents")

from agents import RunContextWrapper  # noqa: E402
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402
from agents.usage import Usage  # noqa: E402

from qumus2.agents.openai_credentials import (  # noqa: E402
    OpenAICredentials,
    load_openai_credentials,
)
from qumus2.agents.openai_runtime import OpenAIRuntime  # noqa: E402
from qumus2.app import build_services  # noqa: E402
from qumus2.config import load_yaml  # noqa: E402
from qumus2.ui.app import create_app  # noqa: E402


class Provider:
    def __init__(self, model):
        self.model = model

    def get_model(self, name):
        assert name == "openai/test-model"
        return self.model


CREDS = OpenAICredentials("test-key-never-sent", model_id="openai/test-model",
                          input_price=1, output_price=2)


def runtime(s, steps, **kwargs):
    model = ScriptedModel(steps)
    if "config" not in kwargs:
        cfg = load_yaml("agents.yaml")
        cfg["openai"]["min_request_interval_s"] = 0
        kwargs["config"] = cfg
    rt = OpenAIRuntime(s.gateway, CREDS, model_provider=Provider(model), **kwargs)
    return rt, model


def fc(name, args, ident):
    return function_call(name, args, call_id=ident)


def said(text):
    return [assistant_message(text)]


def events(s, kind):
    return [e for e in s.store.events_after(0, limit=100000) if e.kind == kind]


def test_real_runner_reply_and_usage(services):
    rt, model = runtime(services, [ModelStep(output=said("Hello from Qumus"),
                          usage=Usage(input_tokens=100, output_tokens=10, total_tokens=110))])

    async def go():
        await rt.send("Who are you?")
        await rt.close()

    asyncio.run(go())
    model.assert_complete()
    assert events(services, "agent.message")[-1].payload["text"] == "Hello from Qumus"
    assert not events(services, "agent.turn_end")[-1].payload["is_error"]
    assert services.store.spent_usd() == pytest.approx(0.00012)  # never double-count the turn
    assert services.store.get_project(services.project_id)["state"]["openai_session_id"] == rt.session_id
    assert model.first_call.tracing.is_disabled()


def test_delegation_denied_without_plan(services):
    rt, model = runtime(services, [[fc("delegate_hands", {"input": "move chip"}, "d1")], said("Need a plan")])
    asyncio.run(rt.send("Place a chip"))
    model.assert_complete()
    assert not services.store.list_runs()
    assert events(services, "tool.denied")[-1].payload["tool"] == "delegate"


def test_expert_tools_permissions_images_and_checked_progress(services, tmp_path):
    s = build_services(tmp_path / "run.db", seed=2)
    s.store.set_plan(s.project_id, [{"id": "s1", "title": "Look", "owner": "eyes"}], author="orchestrator")
    rt, model = runtime(s, [
        [fc("delegate_eyes", {"input": "s1: inspect the bench"}, "d1")],
        [fc("inspect_bench", {}, "e1")],
        said("Camera inspection done."),
        said("Inspection complete."),
    ])
    asyncio.run(rt.send("Inspect"))
    model.assert_complete()
    assert [(e.agent, e.payload["tool"]) for e in events(s, "tool.call")] == [("eyes", "inspect_bench")]
    assert events(s, "agent.done")[-1].agent == "eyes"
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "done"
    # The specialist saw actual image content; only its permitted tools were exposed.
    assert "data:image/" in str(model.calls[2].input)
    assert "place_chip_on_transfer_stage" not in {t.name for t in model.calls[1].tools}
    assert "Code check of the eyes delegation" in str(model.calls[3].input)
    asyncio.run(rt.close())


def test_hardware_tool_rejects_invented_sample(services):
    s = services
    s.store.set_plan(s.project_id, [{"id": "s1", "title": "Place", "owner": "hands",
        "operation": "place_chip_on_transfer_stage",
        "sample_outcomes": [{"sample_id": "chip_001", "location": "transfer_stage"}]}], author="orchestrator")
    s.store.update_project_state(s.project_id, eyes_checked=True)

    def wait(call):
        job = s.store.list_runs()[0]["id"]
        return [fc("wait_job", {"job_id": job, "timeout_s": 10}, "h3")]

    rt, model = runtime(s, [
        [fc("delegate_hands", {"input": "s1: place chip_001"}, "d1")],
        [fc("place_chip_on_transfer_stage", {"chip_id": "chip_999"}, "h1")],
        [fc("place_chip_on_transfer_stage", {"chip_id": "chip_001"}, "h2")],
        ModelStep.respond(wait), said("Placed and checked."), said("Done."),
    ])
    asyncio.run(rt.send("Place"))
    model.assert_complete()
    assert events(s, "tool.denied")[0].payload["rule"] == "refs"
    assert len(s.store.list_runs()) == 1 and s.store.list_runs()[0]["status"] == "succeeded"
    assert s.store.get_sample("chip_001")["location"] == "transfer_stage"


def test_pending_instruction_reaches_expert_and_manager(services):
    s = services
    s.store.set_plan(s.project_id, [{"id": "s1", "title": "Look", "owner": "eyes"}], author="orchestrator")

    def look(call):
        s.store.add_instruction("Use chip_002 instead", project_id=s.project_id)
        return [fc("list_samples", {"kind": "chip"}, "e1")]

    rt, model = runtime(s, [[fc("delegate_eyes", {"input": "s1: inventory"}, "d1")],
                           ModelStep.respond(look), said("Inventory read"), said("Plan changed")])
    asyncio.run(rt.send("Look"))
    assert "Use chip_002 instead" in str(model.calls[2].input)
    assert "Use chip_002 instead" in str(model.calls[3].input)
    assert s.store.pending_instructions() == []


def test_session_survives_restart_without_changing_legacy_session(tmp_path):
    s = build_services(tmp_path / "run.db", seed=2)
    s.store.set_session_id(s.project_id, "legacy-claude-id")
    rt, _ = runtime(s, [said("Remember specimen A")])
    asyncio.run(rt.send("Specimen A"))
    sid = rt.session_id
    asyncio.run(rt.close())
    rt2, model = runtime(s, [said("Specimen A remains in our conversation")])
    asyncio.run(rt2.send("Which specimen?"))
    assert rt2.session_id == sid
    assert "Specimen A" in str(model.first_call.input)
    assert s.store.get_project(s.project_id)["session_id"] == "legacy-claude-id"
    asyncio.run(rt2.close())


def test_budget_stops_before_next_model_call(services):
    rt, model = runtime(services, [ModelStep(output=[fc("list_samples", {}, "c1")],
                        usage=Usage(input_tokens=1000, total_tokens=1000)), said("must not run")],
                        run_budget_usd=0.0001)
    rt._request_allowance = lambda *a: 0.0  # the spent check alone (reserve: next test)
    asyncio.run(rt.send("List inventory"))
    assert len(model.calls) == 1
    assert events(services, "agent.turn_end")[-1].payload["reason"] == "budget_exhausted"
    asyncio.run(rt.send("Again"))
    assert len(model.calls) == 1 and rt.budget_exhausted


def test_run_budget_holds_back_the_next_request(services):
    # $0.60 left but this request may cost $1: it is not sent (no overshoot)
    rt, model = runtime(services, [said("must not run")], run_budget_usd=0.6)
    rt._request_allowance = lambda *a: 1.0
    asyncio.run(rt.send("Pick flakes"))
    assert len(model.calls) == 0
    end = events(services, "agent.turn_end")[-1].payload
    assert end["reason"] == "budget_exhausted"


def test_unknown_gateway_prices_refuse_paid_calls(services):
    rt, model = runtime(services, [said("must not run")])
    rt.creds = replace(CREDS, input_price=None, output_price=None)
    with pytest.raises(RuntimeError, match="token.*prices"):
        asyncio.run(rt.send("Go"))
    assert len(model.calls) == 0


def test_stop_cancels_nested_agent_and_prevents_late_tools(services):
    s = services
    s.store.set_plan(s.project_id, [{"id": "s1", "title": "Look", "owner": "eyes"}], author="orchestrator")

    async def go():
        waiting = asyncio.Event()

        async def blocked(call):
            waiting.set()
            await asyncio.Event().wait()
            return [fc("inspect_bench", {}, "never")]

        rt, model = runtime(s, [[fc("delegate_eyes", {"input": "s1: look"}, "d1")],
                               ModelStep.respond(blocked)])
        task = asyncio.create_task(rt.send("Look"))
        await asyncio.wait_for(waiting.wait(), 5)
        await asyncio.wait_for(rt.interrupt(), 5)
        await asyncio.wait_for(task, 5)
        assert not rt.busy and not events(s, "tool.call")
        s.store.update_project_state(s.project_id, human_constraint='keep chip_004 unused after Stop')
        model.enqueue(said("Continued without replaying the old call."))
        await rt.continue_turn("Continue after Stop")
        assert not events(s, "tool.call")
        assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"
        assert 'keep chip_004 unused after Stop' in model.calls[-1].input[-1]['content']
        assert '[pending] s1:' in model.calls[-1].input[-1]['content']
        await rt.close()

    asyncio.run(go())


def test_stop_during_pacing_prevents_snapshot_assembly_and_model_call(services, monkeypatch):
    cfg = load_yaml('agents.yaml')
    cfg['openai']['min_request_interval_s'] = 60
    rt, model = runtime(services, [said('This response must not run.')], config=cfg)

    async def run():
        waiting = asyncio.Event()
        cooperative_sleep = asyncio.sleep

        async def blocked_wait(delay):
            if delay <= 0:
                await cooperative_sleep(delay)
                return
            waiting.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(asyncio, 'sleep', blocked_wait)
        rt._next_model_call = asyncio.get_running_loop().time() + 60
        task = asyncio.create_task(rt.send('Read after pacing'))
        await asyncio.wait_for(waiting.wait(), 5)
        await asyncio.wait_for(rt.interrupt(), 5)
        await asyncio.wait_for(task, 5)
        assert not rt.busy and not model.calls
        assert rt._composition_snapshot == '' and rt._request_context == {}
        assert not rt._reservations and not events(services, 'agent.request')
        await rt.close()

    asyncio.run(run())


def test_direct_tool_wrapper_cannot_bypass_role_policy(services):
    rt, _ = runtime(services, [])
    spec = services.gateway.registry.get("place_chip_on_transfer_stage")
    tool = rt._domain_tool("eyes", spec)  # even if incorrectly exposed, the gateway checks
    ctx = ToolContext.from_agent_context(RunContextWrapper(None), tool_call_id="bad",
                                        tool_name=tool.name, tool_arguments='{"chip_id":"chip_001"}')
    out = asyncio.run(tool.on_invoke_tool(ctx, '{"chip_id":"chip_001"}'))
    assert '"status": "denied"' in out[0].text
    assert services.store.list_runs() == []


def test_credentials_are_project_scoped_and_never_printed(tmp_path, monkeypatch):
    monkeypatch.delenv("QUMUS_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")
    monkeypatch.setenv("QUMUS_ANTHROPIC_API_KEY", "another-secret")
    assert load_openai_credentials(None) is None
    p = tmp_path / ".env"
    p.write_text("QUMUS_OPENAI_API_KEY=private-token\nQUMUS_OPENAI_MODEL=openai/gpt-6-astra\n"
                 "QUMUS_OPENAI_BASE_URL=https://physics.bytefuture.ai/v1\n"
                 "QUMUS_OPENAI_API=chat_completions\n", encoding="utf-8")
    c = load_openai_credentials(p)
    assert c.base_url.endswith("/v1") and c.model_id == "openai/gpt-6-astra"
    assert "private-token" not in repr(c)
    with pytest.raises(ValueError, match="HTTPS"):
        replace(c, base_url="http://remote.example/v1")


def test_openai_wiring_and_model_health_contract(tmp_path):
    from fastapi.testclient import TestClient

    s = build_services(tmp_path / "run.db", use_openai=True, openai_credentials=CREDS, seed=2)
    assert s.runtime.provider == "openai"
    with TestClient(create_app(s, start_driver=False)) as client:
        h = client.get("/api/health").json()
        assert h["provider"] == "openai" and h["mode"] == "sdk"
        assert h["model"] == CREDS.model_id and h["model_status"]["state"] == "unknown"
        assert "claude" not in h
        assert client.post("/api/claude/check").status_code == 404
    assert load_yaml("agents.yaml")["openai"]["max_output_tokens"] > 0


def test_persistent_stage_budget_counts_separate_tests_and_unknown_spend(tmp_path):
    from qumus2.agents.openai_budget import SpendLedger

    path = tmp_path / "budget.db"
    ledger = SpendLedger(path, limit=2)
    a = ledger.reserve("test-one", 0.8, 1)
    with pytest.raises(ValueError, match="budget"):
        ledger.reserve("test-one", 0.3, 1)
    ledger.settle(a, 0.1)
    ledger.reserve("test-two", 0.9, 1)  # unknown response: reservation survives restart
    again = SpendLedger(path, limit=2)
    assert again.total() == pytest.approx(1.0)
    again.reserve("test-three", 1, 1)
    with pytest.raises(ValueError, match="budget"):
        again.reserve("test-four", 0.01, 1)


def test_models_probe_does_not_treat_missing_route_as_ready(monkeypatch):
    import urllib.error

    from qumus2.agents.openai_health import OpenAIHealth

    state = {"code": 404}

    def fail(req, **kwargs):
        assert req.full_url == "https://gateway.example/v1/models"
        assert req.get_header("Authorization") == "Bearer test-key-never-sent"
        raise urllib.error.HTTPError(req.full_url, state["code"], "test", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fail)
    h = OpenAIHealth(replace(CREDS, base_url="https://gateway.example/v1"))
    assert h.check()["state"] == "error"
    state["code"] = 429
    assert h.check()["state"] == "rate_limited"
    state["code"] = 401
    assert h.check()["state"] == "auth_failed"
    assert CREDS.api_key not in str(h.state)


def test_buffered_transport_uses_complete_responses_and_keeps_expert_messages(services):
    s = services
    s.store.set_plan(s.project_id, [{"id": "s1", "title": "Look", "owner": "eyes"}],
                     author="orchestrator")
    rt, model = runtime(s, [[fc("delegate_eyes", {"input": "s1: inventory"}, "d1")],
                           [fc("list_samples", {"kind": "chip"}, "e1")],
                           said("Inventory read."), said("Done.")])
    rt.creds = replace(CREDS, stream=False)
    asyncio.run(rt.send("Inspect inventory"))
    model.assert_complete()
    assert not any(c.streamed for c in model.calls)
    assert [(e.agent, e.payload["text"]) for e in events(s, "agent.message")] == [
        ("eyes", "Inventory read."), ("orchestrator", "Done.")]
    assert events(s, "tool.result")[-1].payload["status"] == "ok"


def test_merged_json_arguments_are_refused_without_executing_tools(services):
    rt, model = runtime(services, [[fc("list_samples", '{"kind":"chip"}{"kind":"flake"}', "bad")],
                                   said("The malformed call was refused.")])
    asyncio.run(rt.send("Look at inventory"))
    model.assert_complete()
    assert events(services, "tool.denied")[-1].payload["rule"] == "schema"
    assert not events(services, "tool.call")


def test_multiple_tool_calls_keep_distinct_ids_and_results(services):
    rt, model = runtime(services, [[fc("list_samples", {"kind": "chip"}, "one"),
                                   fc("get_project_context", {}, "two")], said("Done")])
    asyncio.run(rt.send("Read state"))
    model.assert_complete()
    calls = events(services, "tool.call")
    assert [e.payload["tool_use_id"] for e in calls] == ["one", "two"]
    assert [e.payload["tool"] for e in events(services, "tool.result")] == [
        "list_samples", "get_project_context"]


def test_specialist_receives_latest_state_within_one_turn(services):
    s = services
    s.store.set_plan(s.project_id, [{"id": "s1", "title": "Look", "owner": "eyes"}], author="orchestrator")

    def change_before_delegation(call):
        s.store.update_project_state(s.project_id, human_constraint='keep chip_004 unused')
        s.store.set_step_status(s.project_id, 's1', 'done', agent='orchestrator')
        return [fc('delegate_eyes', {'input': 's1: inspect inventory'}, 'd1')]

    rt, model = runtime(s, [ModelStep.respond(change_before_delegation),
                           said('Inventory read.'), said('Done.')])
    asyncio.run(rt.send('Check'))
    snapshot = model.calls[1].input[-1]
    assert snapshot['role'] == 'developer'
    assert 'keep chip_004 unused' in snapshot['content']
    assert '[running] s1:' in snapshot['content']
    assert '[done] s1:' not in snapshot['content']
    assert 'keep chip_004 unused' not in str(model.calls[1].system_instructions)


def test_current_application_context_refreshes_without_accumulating_saved_snapshots(tmp_path):
    s = build_services(tmp_path / 'run.db', seed=2)
    s.store.update_project_state(s.project_id, human_constraint='keep chip_001 unused')

    def change_state(call):
        s.store.update_project_state(s.project_id, human_constraint='keep chip_004 unused')
        return [fc('list_samples', {'kind': 'chip'}, 'inventory')]

    rt, model = runtime(s, [ModelStep.respond(change_state), said('Checked current inventory.'),
                           said('Follow-up retains the constraint.')])

    async def go():
        await rt.send('Check the inventory')
        await rt.send('Remember the constraint')
        saved = await rt._session.get_items()
        assert not any(item.get('role') == 'developer' for item in saved)
        assert 'application state, not a human instruction' not in str(saved)
        await rt.close()

    asyncio.run(go())
    assert len({call.system_instructions for call in model.calls}) == 1
    assert 'keep chip_001 unused' in model.calls[0].input[-1]['content']
    for call in model.calls[1:]:
        assert 'keep chip_004 unused' in call.input[-1]['content']
        assert 'keep chip_001 unused' not in call.input[-1]['content']
        assert sum(item.get('role') == 'developer' for item in call.input) == 1
    s.store.update_project_state(s.project_id, human_constraint='keep chip_007 unused after restart')
    restarted, new_model = runtime(s, [said('Restart saw current state.')])
    asyncio.run(restarted.send('Continue after restart'))
    assert restarted.session_id == rt.session_id
    assert 'keep chip_007 unused after restart' in new_model.first_call.input[-1]['content']
    assert 'Checked current inventory.' in str(new_model.first_call.input[:-1])
    asyncio.run(restarted.close())


def test_repeated_image_filter_preserves_distinct_evidence_and_tool_history():
    import copy
    from types import SimpleNamespace

    from agents.run_config import ModelInputData

    from qumus2.agents.openai_runtime import unique_image_input

    image = {'type': 'input_image', 'image_url': 'data:image/png;base64,AAAA'}
    other = {'type': 'input_image', 'image_url': 'data:image/png;base64,BBBB'}
    items = [
        {'type': 'function_call_output', 'call_id': 'one', 'output': [image]},
        {'type': 'function_call_output', 'call_id': 'two', 'output': [image, other,
         {'type': 'input_text', 'text': 'Measured evidence'}]},
    ]
    original = copy.deepcopy(items)
    filtered = unique_image_input(SimpleNamespace(model_data=ModelInputData(items, 'Keep rules')))
    assert items == original  # saved history is not edited
    assert filtered.instructions == 'Keep rules'
    assert filtered.input[0] == items[0]
    assert filtered.input[1]['call_id'] == 'two'
    assert filtered.input[1]['output'][0]['type'] == 'input_text'
    assert filtered.input[1]['output'][1:] == items[1]['output'][1:]


def test_visual_context_keeps_recent_images_with_explicit_omission():
    from types import SimpleNamespace

    from agents.run_config import ModelInputData

    from qumus2.agents.openai_runtime import unique_image_input

    items = [{'role': 'user', 'content': [
        {'type': 'input_image', 'image_url': f'data:image/png;base64,{i}'} for i in range(6)]}]
    diagnostics = []
    output = unique_image_input(SimpleNamespace(model_data=ModelInputData(items, None)),
                                max_images=4, diagnostic_observer=diagnostics.append)
    content = output.input[0]['content']
    assert [v['image_url'] for v in content if v['type'] == 'input_image'] == [
        f'data:image/png;base64,{i}' for i in range(2, 6)]
    assert all('Retrieve' in v['text'] for v in content[:2])
    assert all(v['type'] == 'input_image' for v in items[0]['content'])
    assert diagnostics == [{'superseded_snapshots': 0, 'omitted_images': 2, 'duplicate_images': 0}]


def test_superseded_project_snapshots_keep_latest_state_and_user_instructions():
    import copy
    from types import SimpleNamespace

    from agents.run_config import ModelInputData

    from qumus2.agents.openai_runtime import unique_image_input

    items = [
        {'type': 'function_call', 'call_id': 'c1', 'name': 'get_project_context', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'c1', 'output': 'old full state'},
        {'role': 'user', 'content': 'Keep chip_001 untouched'},
        {'type': 'function_call', 'call_id': 'action', 'name': 'update_plan', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'action', 'output': 'Plan changed'},
        {'type': 'function_call', 'call_id': 'c2', 'name': 'get_project_context', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'c2', 'output': 'latest full state'},
    ]
    original = copy.deepcopy(items)
    result = unique_image_input(SimpleNamespace(model_data=ModelInputData(items, 'Rules')))
    assert items == original
    assert 'superseded' in result.input[1]['output']
    assert result.input[2:] == original[2:]
    assert result.input[0] == original[0]


def test_historical_inventory_projection_preserves_history_and_call_identity():
    import copy
    import json
    from types import SimpleNamespace

    from agents.run_config import ModelInputData

    from qumus2.agents.openai_runtime import unique_image_input

    row = {'id': 'flake_1', 'kind': 'flake', 'location': 'chip_1', 'status': 'candidate',
           'props': {'material': 'Gr', 'area_um2': 90, 'measurement_event_100x': 72,
                     'optical_geometry': {'frame': 'native100_derived_centroid_chip_axes_v1',
                                          'outline_mm': [[0, 0], [.01, 0], [0, .02]]},
                     'measurement_100x': {'raw_private_receipt': 'retained in store'}}}
    text = json.dumps({'status': 'ok', 'data': {'samples': [row]}})
    items = [{'type': 'function_call', 'call_id': 'catalogue', 'name': 'list_samples', 'arguments': '{}'},
             {'type': 'function_call_output', 'call_id': 'catalogue', 'output': [
                 {'type': 'input_text', 'text': text}]},
             {'role': 'user', 'content': 'Keep all eight contact ends covered'}]
    original = copy.deepcopy(items)
    diagnostics = []
    filtered = unique_image_input(SimpleNamespace(model_data=ModelInputData(items, 'Rules')),
                                  diagnostic_observer=diagnostics.append)
    assert items == original
    assert filtered.input[0] == original[0]
    assert filtered.input[1]['call_id'] == 'catalogue'
    assert filtered.input[2] == original[2]
    result = json.loads(filtered.input[1]['output'][0]['text'])
    compact = result['data']['samples'][0]
    assert compact['id'] == 'flake_1'
    assert compact['props']['material'] == 'Gr'
    assert compact['props']['evidence']['measurement_event_100x'] == 72
    assert 'optical_geometry' not in compact['props']
    assert 'Historical availability is not reassessed' in result['data']['historical_catalogue_projection']
    assert diagnostics[0]['compacted_catalogues'] == 1
    again = unique_image_input(SimpleNamespace(model_data=filtered))
    assert again.input == filtered.input


def test_modern_hardware_delegation_needs_explicit_sample_completion(services):
    s = services
    s.store.set_plan(s.project_id, [{'id': 's1', 'title': 'Place', 'owner': 'hands'}], author='orchestrator')
    s.store.update_project_state(s.project_id, eyes_checked=True)
    rt, model = runtime(s, [[fc('delegate_hands', {'input': 's1: place chip_001'}, 'd1')],
                           said('I must declare the intended final sample state first.')])
    asyncio.run(rt.send('Place the chip'))
    model.assert_complete()
    assert not s.store.list_runs()
    assert events(s, 'tool.denied')[-1].payload['rule'] == 'sample_outcomes_required'
    asyncio.run(rt.close())


@pytest.mark.parametrize('operation', [None, 'exfoliate_and_search_one_chip'])
def test_modern_hardware_delegation_rejects_missing_or_invented_operation(services, operation):
    s = services
    s.store.set_plan(s.project_id, [{'id': 's1', 'title': 'Place the chip', 'owner': 'hands',
        'operation': operation, 'sample_outcomes': [{'sample_id': 'chip_001', 'location': 'transfer_stage'}]}],
        author='fixture')
    s.store.update_project_state(s.project_id, eyes_checked=True)
    rt, model = runtime(s, [[fc('delegate_hands', {'input': 's1: place chip_001'}, 'd1')],
                           said('The step needs an explicit registered flow before delegation.')])
    asyncio.run(rt.send('Place the chip'))
    model.assert_complete()
    assert not s.store.list_runs() and not events(s, 'agent.delegate')
    denial = events(s, 'tool.denied')[-1].payload
    assert denial['rule'] == 'physical_operation_required'
    assert 'place_chip_on_transfer_stage' in denial['reason']
    asyncio.run(rt.close())


def test_fresh_tool_images_are_all_shown(services):
    """A tool result with more images than the bound is shown whole; only older ones go."""
    from types import SimpleNamespace

    from agents.run_config import ModelInputData

    from qumus2.agents.openai_runtime import unique_image_input

    def img(n):
        return {"type": "input_image", "image_url": f"data:image/png;base64,{n:04d}"}

    items = [{"type": "function_call", "call_id": "a", "name": "get_sample_images", "arguments": "{}"},
             {"type": "function_call_output", "call_id": "a", "output": [img(i) for i in range(3)]},
             {"type": "function_call", "call_id": "b", "name": "get_sample_images", "arguments": "{}"},
             {"type": "function_call_output", "call_id": "b", "output": [img(i) for i in range(10, 16)]}]
    out = unique_image_input(SimpleNamespace(model_data=ModelInputData(items, None)), max_images=4)
    shown = [p["image_url"] for p in out.input[3]["output"] if p.get("type") == "input_image"]
    assert len(shown) == 6
    old = [p for p in out.input[1]["output"] if p.get("type") == "input_image"]
    assert old == []  # the bound (4) is already used by the fresh ones


def test_picker_comparing_flakes_keeps_every_viewed_flake(monkeypatch):
    """Viewing f002, f004, f005 one call at a time keeps all three image sets.

    With a bound of 4 the picker lost the previous flake each time it viewed the
    next one, retrieved it again and looped (full paid run, 2026-11-10).
    """
    from types import SimpleNamespace

    from agents.run_config import ModelInputData

    from qumus2.agents.openai_runtime import unique_image_input

    monkeypatch.delenv("QUMUS_OPENAI_MAX_CONTEXT_IMAGES", raising=False)

    def view(call, flake):
        return [{"type": "reasoning", "id": f"r{call}", "summary": []},
                {"type": "function_call", "call_id": call, "name": "view_flake_images",
                 "arguments": f'{{"flake_id": "{flake}"}}'},
                {"type": "function_call_output", "call_id": call, "output": [
                    {"type": "input_text", "text": f"{flake}: 20x, 100x image(s) attached"},
                    *({"type": "input_image", "image_url": f"data:image/png;base64,{flake}{k}"}
                      for k in range(5))]}]

    items = [{"role": "user", "content": "Select flakes"},
             *view("v1", "f002"), *view("v2", "f004"), *view("v3", "f005")]
    out = unique_image_input(SimpleNamespace(model_data=ModelInputData(items, None)))
    shown = [p["image_url"] for item in out.input if item.get("type") == "function_call_output"
             for p in item["output"] if p.get("type") == "input_image"]
    assert len(shown) == 15
    monkeypatch.setenv("QUMUS_OPENAI_MAX_CONTEXT_IMAGES", "8")
    out = unique_image_input(SimpleNamespace(model_data=ModelInputData(items, None)))
    shown = [p["image_url"] for item in out.input if item.get("type") == "function_call_output"
             for p in item["output"] if p.get("type") == "input_image"]
    assert len(shown) == 8 and shown[-5:] == [f"data:image/png;base64,f005{k}" for k in range(5)]
