"""Local prefix diagnostics observe final payloads without retaining private content."""

import asyncio
import copy
import json

import httpx
import pytest

from qumus2.agents.request_metrics import PrefixComparison, RequestMetrics


def identity(request='r1', **changes):
    return {'provider': 'openai', 'gateway': 'https://gateway.example/v1',
            'model': 'exact-model', 'session_id': 'session', 'request_id': request, **changes}


def payload():
    return {'model': 'exact-model', 'instructions': 'Stable private rules',
            'tools': [{'type': 'function', 'name': 'look', 'parameters': {'type': 'object'}}],
            'input': [{'role': 'user', 'content': 'Private human constraint'}]}


def test_identical_append_changed_and_reset_keep_only_private_keyed_fingerprints():
    compare = PrefixComparison()
    first = payload()
    original = copy.deepcopy(first)
    assert compare.compare(first, 'responses', identity(), 'eyes')['status'] == 'not_recorded'
    same = compare.compare(first, 'responses', identity('r2'), 'eyes')
    assert same['change'] == 'identical' and same['first_changed_component'] is None
    first['input'].append({'role': 'assistant', 'content': 'Another private reply'})
    added = compare.compare(first, 'responses', identity('r3'), 'eyes')
    assert added['change'] == 'appended' and added['matched_components'] == same['component_count']
    first['instructions'] += ' changed'
    changed = compare.compare(first, 'responses', identity('r4'), 'eyes')
    assert changed['first_changed_category'] == 'system_instructions'
    assert changed['first_changed_component'] == 1
    assert original['input'] == [first['input'][0]]
    assert all(s not in repr(compare._previous) for s in [
        'Stable private rules', 'Private human constraint', 'Another private reply'])
    assert all(s not in json.dumps(changed) for s in ['rules', 'constraint', 'digest', 'key'])
    compare.clear()
    assert not compare._previous
    assert compare.compare(first, 'responses', identity('r5'), 'eyes')['status'] == 'not_recorded'


@pytest.mark.parametrize('field,value,category', [
    ('instructions', 'Changed rules', 'system_instructions'),
    ('tools', [{'type': 'function', 'name': 'other'}], 'tool_definitions'),
    ('temperature', 0.2, 'request_settings'),
    ('input', [{'type': 'reasoning', 'encrypted_content': 'opaque secret'}], 'conversation_history'),
    ('input', [{'role': 'user', 'content': [{'type': 'input_image', 'detail': 'high',
                                          'image_url': 'data:image/png;base64,secret'}]}],
     'conversation_history'),
])
def test_every_wire_value_participates_without_exposing_content(field, value, category):
    compare = PrefixComparison()
    first = payload()
    compare.compare(first, 'responses', identity(), 'eyes')
    second = {**first, field: value}
    result = compare.compare(second, 'responses', identity('r2'), 'eyes')
    assert result['first_changed_category'] == category
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('changes,api,role', [
    ({'provider': 'another'}, 'responses', 'eyes'),
    ({'gateway': 'https://other.example/v1'}, 'responses', 'eyes'),
    ({'model': 'other-model'}, 'responses', 'eyes'),
    ({'session_id': 'other-session'}, 'responses', 'eyes'),
    ({}, 'chat_completions', 'eyes'), ({}, 'responses', 'hands'),
])
def test_comparison_never_crosses_recorded_scope(changes, api, role):
    compare = PrefixComparison()
    compare.compare(payload(), 'responses', identity(), 'eyes')
    assert compare.compare(payload(), api, identity('r2', **changes), role)['status'] == 'not_recorded'


def test_bounded_components_bytes_and_scope_eviction_are_explicit():
    compare = PrefixComparison()
    compare.MAX_SCOPES = 2
    for session in ['one', 'two', 'three']:
        compare.compare(payload(), 'responses', identity(session_id=session), 'eyes')
    assert len(compare._previous) == 2
    assert compare.compare(payload(), 'responses', identity(session_id='one'), 'eyes')['status'] == 'not_recorded'
    compare.MAX_COMPONENTS = 1
    result = compare.compare(payload(), 'responses', identity(), 'eyes')
    assert result['status'] == 'unavailable' and result['reason'] == 'comparison_limit'
    compare.MAX_COMPONENTS = 100
    compare.MAX_BYTES = 5
    assert compare.compare(payload(), 'responses', identity(), 'eyes')['status'] == 'unavailable'
    assert compare.compare(payload(), 'responses', identity(session_id=None), 'eyes')['reason'] == 'unknown_scope'


def test_filter_diagnostics_accept_only_bounded_allowlisted_counts():
    result = PrefixComparison().compare(payload(), 'responses', identity(), 'eyes', {
        'filter_actions': {'omitted_images': 2, 'superseded_snapshots': 1,
                           'duplicate_images': True, 'private_prompt': 'secret'}})
    assert result['filter_actions'] == {'omitted_images': 2, 'superseded_snapshots': 1}
    assert 'secret' not in json.dumps(result)


def test_image_detail_and_pixel_changes_and_tool_order_remain_observable():
    compare = PrefixComparison()
    first = payload()
    first['input'] = [{'role': 'user', 'content': [
        {'type': 'input_image', 'detail': 'low', 'image_url': 'data:image/png;base64,AAAA'}]}]
    compare.compare(first, 'responses', identity(), 'eyes')
    changed = copy.deepcopy(first)
    changed['input'][0]['content'][0]['detail'] = 'high'
    result = compare.compare(changed, 'responses', identity('r2'), 'eyes')
    assert result['change'] == 'changed'
    changed['input'][0]['content'][0]['image_url'] = 'data:image/png;base64,BBBB'
    assert compare.compare(changed, 'responses', identity('r3'), 'eyes')['change'] == 'changed'
    changed['tools'].append({'type': 'function', 'name': 'second'})
    compare.compare(changed, 'responses', identity('r4'), 'eyes')
    changed['tools'].reverse()
    assert compare.compare(changed, 'responses', identity('r5'), 'eyes')['first_changed_category'] == 'tool_definitions'
    assert all(secret not in repr(compare._previous) for secret in ['AAAA', 'BBBB', 'image/png'])


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
def test_moving_state_behind_history_delays_first_changed_local_component(api):
    # Same final transport fields, with only the former dynamic state placement
    # reconstructed. This proves local prefix assembly, not provider cache hits.
    old, new = PrefixComparison(), PrefixComparison()
    old_results, new_results = [], []
    for n, state in enumerate(['plan version 1', 'plan version 2']):
        history = [{'role': 'user', 'content': 'task'}, {'role': 'assistant', 'content': 'history'}]
        snapshot = {'role': 'developer', 'content': state}
        if api == 'responses':
            before = {'instructions': 'Rules\n' + state, 'input': history}
            after = {'instructions': 'Rules', 'input': [*history, snapshot]}
        else:
            before = {'messages': [{'role': 'system', 'content': 'Rules\n' + state}, *history]}
            after = {'messages': [{'role': 'system', 'content': 'Rules'}, *history, snapshot]}
        hints = {'application_context': {'role': 'developer', 'text': state}}
        old_results.append(old.compare(before, api, identity(str(n)), 'eyes'))
        new_results.append(new.compare(after, api, identity(str(n)), 'eyes', hints))
    assert old_results[-1]['first_changed_category'] == 'system_instructions'
    assert new_results[-1]['first_changed_category'] == 'lab_snapshot'
    assert new_results[-1]['matched_components'] > old_results[-1]['matched_components']


def test_http_retries_get_private_final_comparisons_and_diagnostic_failure_is_harmless(monkeypatch):
    events = []

    class Store:
        def emit(self, kind, payload, **kwargs):
            events.append((kind, payload))

    context = {'agent': 'eyes', 'session_id': 'session', 'call_id': 'call'}
    observer = RequestMetrics(Store(), lambda: context, track_tokens=True)

    async def run():
        for attempt in range(2):
            await observer(httpx.Request('POST', 'https://gateway.example/v1/responses',
                headers={'Authorization': 'secret bearer', 'x-stainless-retry-count': str(attempt)},
                json=payload()))

    asyncio.run(run())
    first, second = [event for _, event in events]
    assert first['request_id'] != second['request_id']
    assert second['prefix_comparison']['baseline_request_id'] == first['request_id']
    assert second['prefix_comparison']['change'] == 'identical'
    assert all(s not in json.dumps(events) for s in ['secret bearer', 'Stable private rules', 'Private human'])

    def fail(*args):
        raise RuntimeError('secret diagnostic failure')

    monkeypatch.setattr(observer.prefix_comparison, 'compare', fail)
    asyncio.run(observer(httpx.Request('POST', 'https://gateway.example/v1/responses', json=payload())))
    assert events[-1][1]['prefix_comparison']['status'] == 'unavailable'
    assert 'secret diagnostic failure' not in json.dumps(events)
