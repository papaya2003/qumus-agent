"""Wire-input attribution is mutually exclusive, private and explicitly estimated."""

import asyncio
import copy
import json

import pytest

from qumus2.agents.context_composition import LAB_MARKER, input_composition, instruction_sections
from qumus2.agents.request_metrics import RequestMetrics


def rows(data):
    return {row['category']: row for row in data['components']}


def test_instruction_offsets_ignore_headings_inside_prompts_and_skills():
    system = 'Rules with a fake REFERENCE SKILL and ' + LAB_MARKER
    skill = '\nREFERENCE SKILL: private reference' + LAB_MARKER
    tail, lab = '\nRuntime rules', LAB_MARKER + 'private actual snapshot'
    prefix = system + skill + tail
    sections = instruction_sections(prefix + lab, {
        'prefix': prefix, 'references': [(len(system), len(system + skill))]})
    assert ''.join(s['text'] for s in sections) == prefix + lab
    p = rows(input_composition({'instructions': prefix + lab, 'input': 'task'}, 'responses', {
        'instruction_sections': sections, 'current_task': 'task'}))
    assert p['system_instructions']['text_bytes'] == len((system + tail).encode())
    assert p['reference_skills']['text_bytes'] == len(skill.encode())
    assert p['lab_snapshot']['text_bytes'] == len(lab.encode())
    assert instruction_sections('changed prefix' + lab, {'prefix': prefix, 'references': []}) is None


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
def test_actual_wire_items_count_once_and_do_not_store_content(api):
    image = {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'SECRET' * 1000}
    instructions = 'RulesSkillsSnapshot'
    hints = {'instruction_sections': [
        {'category': category, 'text': text} for category, text in [
            ('system_instructions', 'Rules'), ('reference_skills', 'Skills'), ('lab_snapshot', 'Snapshot')]],
        'current_task': 'same task', 'tool_evidence_texts': ['Generated tool evidence']}
    tools = [{'type': 'function', 'name': 'look', 'parameters': {'type': 'object'}}]
    if api == 'responses':
        payload = {'instructions': instructions, 'tools': tools, 'input': [
            {'role': 'user', 'content': 'same task'}, {'role': 'assistant', 'content': 'old answer'},
            {'role': 'user', 'content': 'same task'},
            {'type': 'function_call', 'call_id': 'private-id', 'name': 'look', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'private-id', 'output': [
                {'type': 'input_text', 'text': 'private lab result'}, image]},
            {'type': 'reasoning', 'encrypted_content': 'opaque secret',
             'summary': [{'type': 'summary_text', 'text': 'visible summary'}]}]}
    else:
        payload = {'tools': tools, 'messages': [
            {'role': 'system', 'content': instructions},
            {'role': 'user', 'content': 'same task'}, {'role': 'assistant', 'content': 'old answer'},
            {'role': 'user', 'content': 'same task'},
            {'role': 'assistant', 'tool_calls': [{'id': 'private-id', 'type': 'function',
             'function': {'name': 'look', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'private-id', 'content': 'private lab result'},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'Generated tool evidence'},
                {'type': 'image_url', 'image_url': {'url': image['image_url']}}]}]}
    before = copy.deepcopy(payload)
    result = input_composition(payload, api, hints)
    p = rows(result)
    assert len(p) == 10 and payload == before
    assert p['current_task']['text_bytes'] == len('same task')
    assert p['conversation_history']['text_bytes'] == len('same taskold answer')
    assert p['tool_calls']['text_bytes'] == len('look{}')
    assert p['tool_results']['text_bytes'] == len('private lab result') + (
        len('Generated tool evidence') if api == 'chat_completions' else 0)
    assert p['tool_definitions']['text_bytes'] == len(json.dumps(tools, separators=(',', ':')).encode())
    assert p['media']['item_count'] == 1 and p['media']['estimated_tokens'] is None
    assert p['protocol_unassigned']['estimated_tokens'] is None
    assert result['text_estimated_tokens'] == sum(r['text_estimated_tokens'] for r in p.values())
    assert all(secret not in json.dumps(result) for secret in [
        'private lab result', 'private-id', 'opaque secret', 'SECRET', 'old answer', instructions])


def test_missing_provenance_is_unknown_and_unicode_estimation_is_explicit():
    payload = {'instructions': 'private instructions', 'input': [{'role': 'user', 'content': '你好'}]}
    result = input_composition(payload, 'responses')
    p = rows(result)
    assert result['method'] == 'utf8_bytes_div4_v1' and result['status'] == 'partial'
    assert p['system_instructions']['estimated_tokens'] is None
    assert p['current_task']['estimated_tokens'] is None
    assert p['protocol_unassigned']['text_bytes'] == len('private instructions你好'.encode())
    known = input_composition({'input': '你好'}, 'responses', {
        'instruction_sections': [], 'current_task': '你好'})
    assert rows(known)['current_task']['estimated_tokens'] == 2  # proxy, not actual Chinese tokens
    assert rows(known)['reference_skills']['status'] == 'not_present'


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
def test_ephemeral_application_context_uses_position_role_and_exact_text(api):
    text = 'Private current state: plan version 9'
    hints = {'instruction_sections': [{'category': 'system_instructions', 'text': 'Rules'}],
             'current_task': 'task', 'application_context': {'role': 'developer', 'text': text}}
    items = [{'role': 'user', 'content': text}, {'role': 'user', 'content': 'task'},
             {'role': 'developer', 'content': text}]
    payload = ({'instructions': 'Rules', 'input': items} if api == 'responses' else
               {'messages': [{'role': 'system', 'content': 'Rules'}, *items]})
    before = copy.deepcopy(payload)
    result = input_composition(payload, api, hints)
    parts = rows(result)
    assert parts['system_instructions']['text_bytes'] == len('Rules')
    assert parts['lab_snapshot']['text_bytes'] == len(text)
    assert parts['conversation_history']['text_bytes'] == len(text)
    assert parts['current_task']['text_bytes'] == len('task')
    assert parts['protocol_unassigned']['text_bytes'] == 0
    assert result['status'] == 'available' and payload == before
    assert text not in json.dumps(result)
    # A matching heading or user-authored copy cannot claim trusted provenance.
    items[-1]['role'] = 'user'
    forged = ({'instructions': 'Rules', 'input': items} if api == 'responses' else
              {'messages': [{'role': 'system', 'content': 'Rules'}, *items]})
    assert rows(input_composition(forged, api, hints))['lab_snapshot']['text_bytes'] == 0


def test_stable_instruction_sections_need_no_embedded_state():
    prefix = 'RulesREFERENCE'
    sections = instruction_sections(prefix, {'prefix': prefix, 'references': [(5, len(prefix))]})
    assert ''.join(part['text'] for part in sections) == prefix
    assert sum(len(part['text']) for part in sections if part['category'] == 'lab_snapshot') == 0


@pytest.mark.parametrize('kind,arg', [('function_call', 'arguments'), ('custom_tool_call', 'input')])
def test_retained_response_tool_calls_and_results_have_separate_source(kind, arg):
    payload = {'input': [
        {'role': 'user', 'content': 'task'},
        {'type': kind, 'name': 'analyze', arg: 'private argument', 'call_id': 'ignored'},
        {'type': kind + '_output', 'output': 'private result', 'call_id': 'ignored'}]}
    result = rows(input_composition(payload, 'responses', {
        'instruction_sections': [], 'current_task': 'task'}))
    assert result['tool_calls']['text_bytes'] == len('analyzeprivate argument')
    assert result['tool_results']['text_bytes'] == len('private result')
    assert result['conversation_history']['text_bytes'] == 0
    assert result['protocol_unassigned']['text_bytes'] == 0


@pytest.mark.parametrize('api,path', [('responses', '/responses/'), ('chat_completions', '/chat/completions')])
def test_gateway_is_actual_deployment_without_secrets_and_usage_stays_separate(api, path):
    import httpx

    class Store:
        def __init__(self):
            self.events = []

        def emit(self, kind, payload, **kwargs):
            self.events.append((kind, payload))

    store = Store()
    hook = RequestMetrics(store, lambda: {'agent': 'eyes', 'call_id': 'c'}, track_tokens=True)
    request = httpx.Request('POST', 'https://user:secret@Example.invalid:443/deploy/v1' + path
                            + '?key=hidden#private', json={'model': 'm', 'input': 'private request'})
    asyncio.run(hook(request))
    hook.record_usage({'input_tokens': 123, 'output_tokens': 4})
    request_event, usage = [p for _, p in store.events]
    assert request_event['gateway'] == usage['gateway'] == 'https://example.invalid/deploy/v1'
    assert 'input_tokens' not in request_event
    assert usage['input_tokens'] == 123 and usage['total_tokens'] == 127
    assert all(s not in str(store.events) for s in ['user:secret', 'key=hidden', '#private', 'private request'])


def test_diagnostic_failure_does_not_break_send_or_fabricate_usage(monkeypatch):
    import httpx

    events = []

    class Store:
        def emit(self, kind, payload, **kwargs):
            events.append(payload)

    def fail(*args):
        raise RuntimeError('private failure message')

    monkeypatch.setattr('qumus2.agents.request_metrics.input_composition', fail)
    hook = RequestMetrics(Store(), lambda: {}, track_tokens=True)
    asyncio.run(hook(httpx.Request('POST', 'https://example.invalid/v1/responses', json={'input': 'hi'})))
    assert events[0]['input_composition']['status'] == 'unavailable'
    assert 'input_tokens' not in events[0] and 'private failure message' not in str(events)
