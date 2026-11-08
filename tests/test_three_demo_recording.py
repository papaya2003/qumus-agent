"""Offline-only admission and evidence tests; no live runtime or model client."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from qumus2.runtime.store import RunStore
from scripts.record_three_demos import (
    MODEL,
    STORIES,
    StorySequence,
    TrialRegistry,
    evidence_manifest,
    main,
    story_limits,
    usage_report,
)


def setup_registry(tmp_path):
    ledger = tmp_path / 'shared-budget.db'
    ledger.touch()
    return TrialRegistry(tmp_path / 'synthetic-campaign'), ledger


def start(registry, ledger, story):
    return registry.start(story, source='frozen-source', ledger=ledger,
                          limits=story_limits(story))


def test_fixed_story_allocations_preserve_original_total_and_enter_freeze(monkeypatch, tmp_path):
    from scripts import record_three_demos as demo

    allocations = {story: demo.story_limits(story) for story in STORIES}
    assert {s: v['estimated_usd'] for s, v in allocations.items()} == {
        'complete': 75, 'collaboration': 25, 'honest_stop': 20}
    assert sum(v['estimated_usd'] for v in allocations.values()) == demo.MAX_TOTAL_ESTIMATED_USD == 120
    assert {s: v['http_attempts'] for s, v in allocations.items()} == {
        'complete': 180, 'collaboration': 120, 'honest_stop': 120}
    assert {s: v['input_tokens'] for s, v in allocations.items()} == {
        'complete': 4_500_000, 'collaboration': 3_000_000, 'honest_stop': 3_000_000}
    assert all(v['output_tokens'] == 120_000
               and v['wall_seconds'] == 7200 and v['http_max_retries'] == 2
               and v['min_request_interval_s'] == 60 for v in allocations.values())
    registry, ledger = setup_registry(tmp_path)
    with pytest.raises(ValueError, match='Approved story limits'):
        registry.start('honest_stop', source='frozen', ledger=ledger, limits=allocations['complete'])
    before = demo.frozen_source()
    changed = {s: dict(v) for s, v in demo.STORY_LIMITS.items()}
    monkeypatch.setattr(demo, 'STORY_LIMITS', changed)
    changed['complete']['input_tokens'] += 1
    assert demo.frozen_source() != before
    changed['complete']['input_tokens'] -= 1
    changed['complete']['estimated_usd'] -= 1
    changed['honest_stop']['estimated_usd'] += 1
    assert demo.frozen_source() != before
    changed['complete']['estimated_usd'] += 1
    with pytest.raises(ValueError, match='original campaign maximum'):
        demo.story_limits('complete')


@pytest.mark.parametrize('story', STORIES)
@pytest.mark.parametrize('resource', ['estimated_usd', 'input_tokens', 'http_attempts'])
def test_registry_rejects_unapproved_resource_overrides(tmp_path, story, resource):
    registry, ledger = setup_registry(tmp_path)
    limits = story_limits(story)
    limits[resource] += 1
    with pytest.raises(ValueError, match='Approved story limits'):
        registry.start(story, source='frozen', ledger=ledger, limits=limits)
    with registry.connect() as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 0
    assert not (registry.root / story).exists()


def test_reopening_never_implicitly_retries_a_failed_story(tmp_path):
    registry, ledger = setup_registry(tmp_path)
    for story in STORIES:
        start(registry, ledger, story)
        registry.finish(story, passed=False)
    reopened = TrialRegistry(registry.root)
    with pytest.raises(ValueError, match='retry-failed'):
        start(reopened, ledger, 'complete')
    with reopened.connect() as db:
        assert db.execute('select count(*) from trial').fetchone()[0] == 3


def test_competing_starts_only_consume_one_slot(tmp_path):
    registry, ledger = setup_registry(tmp_path)
    def attempt(_):
        try:
            return start(registry, ledger, 'complete')
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(r is not None for r in results) == 1


def test_resume_requires_same_contract_state_and_database(tmp_path):
    registry, ledger = setup_registry(tmp_path)
    manifest = start(registry, ledger, 'complete')
    path = registry.root / 'complete' / 'qumus.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE event (id INTEGER PRIMARY KEY, payload TEXT)')
        db.execute("INSERT INTO event VALUES (1, 'original')")
    checkpoint = registry.interrupt('complete', 'same-session')
    with pytest.raises(ValueError, match='exact saved'):
        registry.resume('complete', {**manifest, 'source': 'different'}, checkpoint)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE event SET payload='rewritten'")
    with pytest.raises(ValueError, match='events changed'):
        registry.resume('complete', manifest, checkpoint)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE event SET payload='original'")
    assert registry.resume('complete', manifest, checkpoint) == manifest
    with registry.connect() as db:
        assert db.execute('select count(*) from trial').fetchone()[0] == 1


def test_cannot_use_private_fresh_ledger_or_existing_workspace(tmp_path):
    registry, ledger = setup_registry(tmp_path)
    bad = registry.root / 'reset-budget.db'
    bad.touch()
    with pytest.raises(ValueError, match='shared ledger'):
        start(registry, bad, 'complete')
    (registry.root / 'complete').mkdir()
    with pytest.raises(ValueError, match='cannot be reset'):
        start(registry, ledger, 'complete')


def test_write_ahead_fault_is_not_silently_reinjected(tmp_path):
    path = tmp_path / 'synthetic-qumus.db'
    store = RunStore(path)
    sequence = StorySequence('complete', store)
    assert sequence.propose('wrong_boundary') is None
    assert sequence.propose('before_first_search')['fault'] == 'pick_fail_next'
    store.close()
    store = RunStore(path)
    reopened = StorySequence('complete', store)
    with pytest.raises(ValueError, match='Reconcile pending'):
        reopened.propose('before_first_search')
    with pytest.raises(ValueError, match='persisted'):
        reopened.acknowledge('empty', {'event_id': 9999})
    receipt = store.emit('fault.injected', {'fault': 'pick_fail_next', 'params': {'count': 1}})
    reopened.acknowledge('empty', {'event_id': receipt})
    assert reopened.propose('before_first_search')['fault'] == 'next_placement'
    store.close()
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='action'").fetchone()


def test_collaboration_has_no_fault_and_keeps_four_changes(tmp_path):
    store = RunStore(tmp_path / 'synthetic-qumus.db')
    sequence = StorySequence('collaboration', store)
    for event in STORIES['collaboration']['events']:
        action = sequence.propose(event['boundary'])
        assert 'fault' not in action
        receipt = store.emit('user.message', {'text': action['message']})
        sequence.acknowledge(action['id'], {'event_id': receipt})
    assert len(STORIES['collaboration']['events']) == 4
    store.close()


def test_usage_does_not_double_count_or_treat_missing_as_zero(tmp_path):
    path = tmp_path / 'recording.json'
    data = {'meta': {}, 'events': [
        {'kind': 'agent.request', 'payload': {'request_id': 'r1'}},
        {'kind': 'agent.usage', 'payload': {'input_tokens': 999}},
        {'kind': 'agent.request_usage', 'payload': {'request_id': 'r1', 'input_tokens': 100, 'output_tokens': 20}},
    ]}
    path.write_text(json.dumps(data))
    assert usage_report(path)['input_tokens'] == 100
    assert usage_report(path)['usage_complete']
    data['events'] = data['events'][:2]
    path.write_text(json.dumps(data))
    assert usage_report(path)['output_tokens'] is None
    assert not usage_report(path)['usage_complete']


def test_evidence_rejects_wrong_model_missing_composition_and_unknown_usage():
    event = {'id': 1, 'kind': 'agent.request',
             'payload': {'request_id': 'r1', 'model': 'wrong'}}
    m = evidence_manifest([event])
    assert not m['all_requests_astra'] and not m['composition_complete']
    assert not m['capacity_complete']
    assert m['unaccounted_request_ids'] == ['r1']
    event['payload'].update(model=MODEL, input_composition={'status': 'unavailable'})
    assert not evidence_manifest([event])['composition_complete']


def test_live_cli_requires_story_and_frozen_source():
    with pytest.raises(SystemExit):
        main(['--live'])


def test_failed_story_cannot_be_resumed(tmp_path):
    registry, ledger = setup_registry(tmp_path)
    manifest = start(registry, ledger, 'honest_stop')
    registry.finish('honest_stop', passed=False)
    with pytest.raises(ValueError, match='exact saved'):
        registry.resume('honest_stop', manifest, {})
    with registry.connect() as db:
        assert db.execute('select status from trial').fetchone()[0] == 'failed'


def test_runtime_rejection_precedes_transport_and_metrics_and_releases_only_unsent(services, tmp_path, monkeypatch):
    """Real SDK/client path, with an offline MockTransport that must not be reached."""
    import asyncio

    import httpx
    import openai

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import BudgetExceeded, OpenAIRuntime

    sent = []
    def transport(request):
        sent.append(request)
        return httpx.Response(503, json={'error': {'message': 'synthetic transport failure'}})
    monkeypatch.setattr(openai, 'DefaultAsyncHttpxClient', lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(transport), **kw))
    allow = False
    async def admission(request, runtime):
        if not allow:
            raise ValueError('synthetic admission rejection')
    creds = OpenAICredentials('offline-key', model_id=MODEL, input_price=10, output_price=50,
                              stage_budget_usd=None, budget_path=str(tmp_path / 'synthetic-budget.db'))
    rt = OpenAIRuntime(services.gateway, creds, pre_send_admission=admission, http_max_retries=0)
    async def run():
        nonlocal allow
        rt._ensure()
        assert rt._client.max_retries == 0
        previous = rt._ledger.reserve('prior-work', 7, 40)
        await rt._hooks.on_llm_start(None, rt._agents['orchestrator'], 'offline', [])
        with pytest.raises(BudgetExceeded):
            await rt._client.responses.create(model=MODEL, input='offline', max_output_tokens=10)
        assert sent == []
        assert not [e for e in services.store.events_after(0, limit=10000)
                    if e.kind in ('agent.request', 'agent.request_failure')]
        assert rt._ledger.total(rt._budget_scope) == 0
        assert rt._ledger.total('prior-work') == 7
        with sqlite3.connect(creds.budget_path) as db:
            assert db.execute('SELECT actual FROM spend WHERE id!=?', (previous,)).fetchall() == [(0.0,)]
        # A real send with unknown usage must retain its allowance. There is no retry.
        allow = True
        await rt._hooks.on_llm_start(None, rt._agents['orchestrator'], 'offline', [])
        with pytest.raises(openai.APIStatusError):
            await rt._client.responses.create(model=MODEL, input='offline', max_output_tokens=10)
        assert len(sent) == 1
        reserved = rt._ledger.total(rt._budget_scope)
        assert reserved > 0
        allow = False
        with pytest.raises(BudgetExceeded):
            await rt._client.responses.create(model=MODEL, input='offline', max_output_tokens=10)
        assert len(sent) == 1
        assert rt._ledger.total(rt._budget_scope) == reserved
        events = services.store.events_after(0, limit=10000)
        assert len([e for e in events if e.kind == 'agent.request']) == 1
        await rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('story', STORIES)
@pytest.mark.parametrize('condition', ['source', 'model', 'time', 'requests', 'input', 'output', 'dollars', 'limits', 'unknown', 'valid'])
def test_cumulative_admission_fails_closed_across_turns_and_reopen(tmp_path, condition, story):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from qumus2.agents.openai_budget import SpendLedger
    from qumus2.core.run_paths import original_run_path
    from scripts.record_three_demos import DemoAdmission

    folder = tmp_path / 'synthetic-workspace'
    folder.mkdir()
    ledger = SpendLedger(tmp_path / 'synthetic-shared-ledger.db', None)
    store = RunStore(folder / 'qumus.db')
    limits = story_limits(story)
    manifest = {'story': story, 'source': 'frozen', 'started_wall': 100, 'limits': limits,
                'ledger': str(ledger.path), 'workspace': str(folder)}
    if condition == 'limits':
        manifest['limits'] = {**limits, 'input_tokens': limits['input_tokens'] + 1}
    scope = str(original_run_path(folder / 'qumus.db'))
    reservation = ledger.reserve(scope, 1, 40)
    if condition == 'dollars':
        ledger.reserve(scope, limits['estimated_usd'], 120)
    n = limits['http_attempts'] if condition == 'requests' else 1
    for i in range(n):
        store.emit('demo.http_admitted', {'call_id': str(i), 'input_allowance': 5, 'output_allowance': 2})
        store.emit('agent.request', {'call_id': str(i), 'request_id': str(i), 'attempt': 1})
        if condition != 'unknown':
            store.emit('agent.request_usage', {'call_id': str(i), 'request_id': str(i), 'input_tokens': limits['input_tokens'] if condition == 'input' else 5,
                       'output_tokens': limits['output_tokens'] - 1 if condition == 'output' else 2})
    store.close()
    store = RunStore(folder / 'qumus.db')
    rt = SimpleNamespace(store=store, _http_max_retries=2, _budget_scope=scope,
                         cfg={"openai": {"min_request_interval_s": 60.0}},
                         _ledger=ledger, run_budget_usd=limits['estimated_usd'],
                         _reservations={'orchestrator': reservation},
                         _request_context={'agent': 'orchestrator', 'call_id': 'next', 'context_window_tokens': 2_000_000},
                         creds=SimpleNamespace(model_id=MODEL, web_search=False, budget_path=str(ledger.path),
                                               input_price=10., output_price=50., test_budget_usd=120.))
    request = httpx.Request('POST', 'https://offline.invalid/v1/responses',
                            json={'model': 'wrong' if condition == 'model' else MODEL,
                                  'input': 'synthetic', 'max_output_tokens': 10})
    guard = DemoAdmission(manifest, source_reader=lambda: 'changed' if condition == 'source' else 'frozen',
                          now=lambda: 7400 if condition == 'time' else 101)
    before = store.last_event_id()
    if condition == 'valid':
        asyncio.run(guard(request, rt))
        assert store.events_after(before)[0].kind == 'demo.http_admitted'
        store.close()
        return
    expected = {'source': 'source changed', 'model': 'Only Astra', 'time': 'wall-clock',
                'requests': 'HTTP attempt limit', 'input': 'input limit', 'output': 'output limit',
                   'dollars': 'estimated spend', 'limits': 'limit contract changed',
                'unknown': 'unresolved usage'}[condition]
    with pytest.raises(ValueError, match=expected):
        asyncio.run(guard(request, rt))
    assert store.last_event_id() == before
    assert ledger.total(scope) == (limits['estimated_usd'] + 1 if condition == 'dollars' else 1)
    store.close()


def test_search_faults_arm_before_submit_once_and_are_real_injector_primitives(tmp_path):
    from types import SimpleNamespace

    from qumus2.testbench.faults import FAULTS
    from scripts.record_three_demos import install_search_faults

    store = RunStore(tmp_path / 'synthetic-qumus.db')
    calls = []
    s = SimpleNamespace(store=store, project_id='synthetic',
                        lab=SimpleNamespace(faults=SimpleNamespace(inject=lambda name, params: calls.append(name))),
                        jobs=SimpleNamespace(submit=lambda flow, params, **kw: calls.append('submit:' + flow)))
    sequence = StorySequence('complete', store)
    install_search_faults(s, sequence)
    s.jobs.submit('exfoliate_chips', {})
    s.jobs.submit('search_chip', {})
    s.jobs.submit('search_chip', {})
    assert calls == ['submit:exfoliate_chips', 'pick_fail_next', 'next_placement',
                     'serial_timeout', 'submit:search_chip', 'submit:search_chip']
    assert all(e['fault'] in FAULTS for e in STORIES['complete']['events'])
    assert len([e for e in store.events_after(0) if e.kind == 'fault.injected']) == 3
    store.close()


def test_offline_driver_preserves_private_failure_export_and_consumes_start(tmp_path, monkeypatch):
    """Exercise the executable driver/export/registry path with no model provider."""
    import asyncio
    from dataclasses import replace

    from qumus2 import app
    from qumus2.agents import openai_credentials
    from qumus2.agents.driver import ConversationDriver
    from qumus2.ui import recording
    from scripts import record_three_demos as demo

    campaign = tmp_path / 'SYNTHETIC-OFFLINE-campaign'
    ledger = tmp_path / 'SYNTHETIC-OFFLINE-budget.db'
    ledger.touch()
    creds = openai_credentials.OpenAICredentials('offline', model_id=MODEL, input_price=10, output_price=50,
                                                budget_path=str(ledger))
    monkeypatch.setattr(openai_credentials, 'load_openai_credentials', lambda *a: creds)
    real_registry = demo.TrialRegistry
    monkeypatch.setattr(demo, 'TrialRegistry', lambda: real_registry(campaign))
    monkeypatch.setattr(demo, 'frozen_source', lambda: 'synthetic-source')
    real_build, real_export = app.build_services, recording.export_recording
    captured = []
    def build(path, **kwargs):
        assert kwargs['http_max_retries'] == 2 and kwargs['run_budget_usd'] == 75
        assert isinstance(kwargs['pre_send_admission'], demo.DemoAdmission)
        s = real_build(path, use_openai=False)
        class OfflineRuntime:
            busy = False
            budget_exhausted = False
            session_id = 'synthetic-session'
            async def send(self, text):
                captured.append(text)
                # This is deliberately incomplete. It must never earn acceptance.
                s.store.emit('synthetic.offline_fixture', {'note': 'No LLM or physical experiment executed'})
            async def close(self):
                pass
            async def interrupt(self):
                pass
        runtime = OfflineRuntime()
        return replace(s, runtime=runtime, driver=ConversationDriver(s.store, runtime, s.jobs))
    def export(*args, **kwargs):
        kwargs['title'] = 'SYNTHETIC OFFLINE DRIVER FIXTURE'
        kwargs['description'] = 'No API or agent experiment; recorder lifecycle only'
        kwargs['meta']['synthetic_offline_fixture'] = True
        return real_export(*args, **kwargs)
    monkeypatch.setattr(app, 'build_services', build)
    monkeypatch.setattr(recording, 'export_recording', export)
    summary = asyncio.run(demo.record_demo('complete', 'synthetic-source'))
    assert not summary['machine_checks_passed']
    assert captured and '20 nm Pd' in captured[0] and 'stamp_01 carries mica' in captured[0]
    assert 'Build hBN/graphene' in captured[0]
    export_data = json.loads((Path(summary['export']) / 'recording.json').read_text())
    assert export_data['meta']['synthetic_offline_fixture'] is True
    assert not any(e['kind'] == 'agent.request' for e in export_data['events'])
    with pytest.raises(ValueError, match='already started'):
        asyncio.run(demo.record_demo('complete', 'synthetic-source'))
    with real_registry(campaign).connect() as db:
        assert db.execute('SELECT story,status FROM trial').fetchall() == [('complete', 'failed')]
    original_hash = __import__('hashlib').sha256((Path(summary['export']) / 'recording.json').read_bytes()).hexdigest()
    replacement = asyncio.run(demo.record_demo('complete', 'synthetic-source', retry_failed=True))
    assert replacement['attempt_number'] == 2 and replacement['export'] != summary['export']
    assert __import__('hashlib').sha256((Path(summary['export']) / 'recording.json').read_bytes()).hexdigest() == original_hash
    assert replacement['story_cumulative']['http_attempts'] == 0
    def startup_failure(*a, **kw):
        raise RuntimeError('synthetic startup failure before runtime construction')
    monkeypatch.setattr(app, 'build_services', startup_failure)
    failed_start = asyncio.run(demo.record_demo('complete', 'synthetic-source', retry_failed=True))
    assert failed_start['attempt_number'] == 3 and 'synthetic startup failure' in failed_start['error']
    assert failed_start['story_cumulative']['estimated_usd'] == 0
    with real_registry(campaign).connect() as db:
        assert db.execute('SELECT number,status FROM attempt ORDER BY number').fetchall() == [(1,'failed'),(2,'failed'),(3,'failed')]



def test_supported_fault_chain_really_occurs_in_one_simulated_search(services):
    """Offline device fixture, not an agent trial or a claim about model recovery."""
    from scripts.record_three_demos import install_search_faults

    s = services
    prepared = s.jobs.submit('exfoliate_chips', {'material': 'Gr', 'chip_ids': ['chip_001']})
    assert s.jobs.wait(prepared, 60).status == 'succeeded'
    sequence = StorySequence('complete', s.store)
    install_search_faults(s, sequence)
    search = s.jobs.submit('search_chip', {'chip_id': 'chip_001', 'max_revisits': 2})
    # Bounded wall wait for the full 20x/100x revisit; physical budgets stay unchanged.
    result = s.jobs.wait(search, 120)
    assert result.status == 'succeeded', result
    events = s.store.events_after(0, limit=100000, run_id=search)
    text = '\n'.join(e.payload.get('text', '') for e in events if e.kind == 'notification').lower()
    assert 'serial timeout' in text and 'pick came up empty' in text
    assert any(e.kind == 'step.corrected' for e in events)
    assert not any(e.kind == 'safety.violation' for e in events)
    assert not any(e.kind == 'agent.request' for e in s.store.events_after(0, limit=100000))


def legacy_failed_fixture(tmp_path):
    """Old one-row registry + real-shaped evidence, including an unknown 429."""
    import time

    from qumus2.agents.openai_budget import SpendLedger
    old_limits = {'input_tokens': 1_800_000, 'output_tokens': 40_000,
                  'http_attempts': 120, 'estimated_usd': 40, 'wall_seconds': 7200,
                  'http_max_retries': 2, 'min_request_interval_s': 60}

    root = tmp_path / 'synthetic-legacy-campaign'
    folder = root / 'complete'
    folder.mkdir(parents=True)
    ledger = SpendLedger(tmp_path / 'original-shared.db', None)
    manifest = {'story': 'complete', 'model': MODEL, 'source': 'original-freeze',
                'workspace': str(folder), 'ledger': str(ledger.path), 'limits': old_limits,
                'started_wall': time.time() - 5000, 'seed': 8}
    with sqlite3.connect(root / 'attempts.sqlite3') as db:
        db.execute('CREATE TABLE trial(story TEXT PRIMARY KEY,manifest TEXT,status TEXT,checkpoint TEXT)')
        db.execute("INSERT INTO trial VALUES ('complete',?,'failed',NULL)", (json.dumps(manifest),))
    store = RunStore(folder / 'qumus.db')
    for call, bound, output in [('known', 100, 10), ('unknown', 200, 30)]:
        store.emit('demo.http_admitted', {'call_id': call, 'input_allowance': bound, 'output_allowance': output})
        store.emit('agent.request', {'call_id': call, 'request_id': call, 'model': MODEL})
    store.emit('agent.request_usage', {'call_id': 'known', 'request_id': 'known', 'input_tokens': 70, 'output_tokens': 5})
    store.emit('agent.request_failure', {'call_id': 'unknown', 'request_id': 'unknown', 'http_status': 429})
    store.close()
    first = ledger.reserve(str(folder / 'qumus.db'), 1, 40)
    ledger.settle(first, .25)
    unknown = ledger.reserve(str(folder / 'qumus.db'), 2, 40)
    (folder / 'original-export.json').write_text('{"synthetic_original_evidence": true}')
    return root, manifest, ledger, unknown


def retry(registry, manifest, **kwargs):
    return registry.start('complete', source='new-freeze', ledger=manifest['ledger'],
                          limits=story_limits('complete'), retry_failed=True, **kwargs)


def test_legacy_migration_and_explicit_retry_preserve_all_original_evidence(tmp_path):
    import hashlib

    root, original, ledger, unknown = legacy_failed_fixture(tmp_path)
    folder = Path(original['workspace'])
    saved = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    with sqlite3.connect(root / 'attempts.sqlite3') as db:
        old_row = db.execute('SELECT * FROM trial').fetchone()
    registry = TrialRegistry(root)
    with pytest.raises(ValueError, match='retry-failed'):
        registry.start('complete', source='new', ledger=ledger.path, limits=story_limits('complete'))
    new = retry(registry, original)
    assert new['attempt_number'] == 2 and new['source'] == 'new-freeze'
    assert Path(new['workspace']) != folder and new['started_wall'] > original['started_wall']
    assert new['story_usage_before'] == {'input_tokens': 270, 'output_tokens': 35,
        'http_attempts': 2, 'estimated_usd': 2.25, 'unknown_reserved_usd': 2.0, 'unknown_usage_attempts': 1}
    assert new['attempt_budget_usd'] == 72.75
    with registry.connect() as db:
        assert db.execute('SELECT * FROM trial').fetchone() == old_row
        assert db.execute('SELECT number,status FROM attempt ORDER BY number').fetchall() == [(1, 'failed'), (2, 'started')]
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.name in saved} == saved
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT reserved,actual FROM spend WHERE id=?', (unknown,)).fetchone() == (2.0, None)
    # Migration is idempotent, including after a second attempt has started.
    reopened = TrialRegistry(root)
    with reopened.connect() as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 2


def test_concurrent_explicit_retries_only_start_one_and_success_cannot_repeat(tmp_path):
    root, original, _, _ = legacy_failed_fixture(tmp_path)
    registry = TrialRegistry(root)
    def attempt(_):
        try:
            return retry(registry, original)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(r is not None for r in results) == 1
    registry.finish('complete', passed=True)
    with pytest.raises(ValueError, match='successful stories are locked'):
        retry(TrialRegistry(root), original)


def test_driver_fifth_attempt_preserves_four_failures_and_finished_other_story(tmp_path, monkeypatch):
    """Source changes and fresh workspaces cannot erase settled or unknown spending."""
    import asyncio
    import hashlib

    from qumus2 import app
    from qumus2.agents import openai_credentials
    from scripts import record_three_demos as demo

    root, original, ledger, _ = legacy_failed_fixture(tmp_path)
    registry = TrialRegistry(root)
    old_manifests = [original]
    for number, cost in [(2, 5), (3, 7), (4, 9)]:
        manifest = registry.start('complete', source=f'freeze-{number}', ledger=ledger.path,
                                  limits=story_limits('complete'), retry_failed=True)
        old_manifests.append(manifest)
        path = Path(manifest['workspace']) / 'qumus.db'
        store = RunStore(path)
        store.emit('demo.http_admitted', {'call_id': f'call-{number}',
                                          'input_allowance': 1000, 'output_allowance': 4096})
        store.emit('agent.request', {'call_id': f'call-{number}', 'request_id': f'req-{number}'})
        store.emit('agent.request_failure', {'call_id': f'call-{number}', 'request_id': f'req-{number}',
                                            'http_status': 429})
        store.close()
        ledger.reserve(str(path), cost, manifest['attempt_budget_usd'])
        registry.finish('complete', passed=False)
    accepted = start(registry, ledger.path, 'honest_stop')
    old_manifests.append(accepted)
    accepted_export = Path(accepted['workspace']) / 'accepted-export.json'
    accepted_export.write_text('{"synthetic_accepted_evidence":true}')
    registry.finish('honest_stop', passed=True, result={'export': str(accepted_export)})
    files = [p for m in old_manifests for p in Path(m['workspace']).iterdir() if p.is_file()]
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    with registry.connect() as db:
        old_rows = db.execute('SELECT story,number,manifest,status,result FROM attempt ORDER BY story,number').fetchall()
    creds = openai_credentials.OpenAICredentials('offline', model_id=MODEL, input_price=10,
        output_price=50, test_budget_usd=120, budget_path=str(ledger.path))
    monkeypatch.setattr(openai_credentials, 'load_openai_credentials', lambda *a: creds)
    monkeypatch.setattr(demo, 'TrialRegistry', lambda: registry)
    monkeypatch.setattr(demo, 'frozen_source', lambda: 'fifth-freeze')
    captured = []
    def build(path, **kwargs):
        captured.append(kwargs)
        assert kwargs['run_budget_usd'] == 75 - 2.25 - 5 - 7 - 9
        assert kwargs['openai_credentials'].test_budget_usd == 75
        assert kwargs['pre_send_admission'].manifest['story_usage_before'] == {
            'input_tokens': 3270, 'output_tokens': 12323, 'http_attempts': 5,
            'estimated_usd': 23.25, 'unknown_reserved_usd': 23, 'unknown_usage_attempts': 4}
        raise RuntimeError('Synthetic offline stop before service construction')
    monkeypatch.setattr(app, 'build_services', build)
    result = asyncio.run(demo.record_demo('complete', 'fifth-freeze', retry_failed=True))
    assert captured and result['attempt_number'] == 5
    assert result['story_cumulative']['estimated_usd'] == 23.25
    assert result['story_cumulative']['unknown_reserved_usd'] == 23
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files} == hashes
    with registry.connect() as db:
        assert db.execute('SELECT story,number,manifest,status,result FROM attempt WHERE number<=4 ORDER BY story,number').fetchall() == old_rows
    with pytest.raises(ValueError, match='successful stories are locked'):
        registry.start('honest_stop', source='fifth-freeze', ledger=ledger.path,
                       limits=story_limits('honest_stop'), retry_failed=True)


@pytest.mark.parametrize('resource', ['input_tokens', 'output_tokens', 'http_attempts', 'estimated_usd'])
def test_retry_cannot_reset_any_cumulative_resource(tmp_path, resource):
    limits = story_limits('complete')
    root, original, ledger, _ = legacy_failed_fixture(tmp_path)
    path = Path(original['workspace']) / 'qumus.db'
    store = RunStore(path)
    if resource in ('input_tokens', 'output_tokens'):
        row = store._x("SELECT id,payload_json FROM event WHERE kind='agent.request_usage'").fetchone()
        payload = json.loads(row['payload_json'])
        payload[resource] = limits[resource] - (200 if resource == 'input_tokens' else 30)
        store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(payload), row['id']))
    elif resource == 'http_attempts':
        for n in range(limits['http_attempts'] - 2):
            store.emit('demo.http_admitted', {'call_id': f'c{n}', 'input_allowance': 1, 'output_allowance': 1})
            store.emit('agent.request', {'call_id': f'c{n}', 'request_id': f'c{n}', 'attempt': 1})
            store.emit('agent.request_usage', {'call_id': f'c{n}', 'request_id': f'c{n}', 'input_tokens': 1, 'output_tokens': 1})
    else:
        ledger.reserve(str(path), limits['estimated_usd'] - 2.25, limits['estimated_usd'])
        store.emit('demo.http_admitted', {'call_id': 'cost-cap', 'input_allowance': 1, 'output_allowance': 1})
    store.close()
    registry = TrialRegistry(root)
    with pytest.raises(ValueError, match='cumulative ' + resource):
        retry(registry, original)
    with registry.connect() as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 1


def test_unknown_old_usage_without_bound_or_missing_store_fails_closed(tmp_path):
    root, original, _, _ = legacy_failed_fixture(tmp_path)
    path = Path(original['workspace']) / 'qumus.db'
    store = RunStore(path)
    store._x("UPDATE event SET payload_json=json_remove(payload_json,'$.input_allowance') "
             "WHERE kind='demo.http_admitted' AND json_extract(payload_json,'$.call_id')='unknown'")
    store.close()
    registry = TrialRegistry(root)
    with pytest.raises(ValueError, match='lacks conservative token allowance'):
        retry(registry, original)
    path.rename(path.with_suffix('.preserved'))
    with pytest.raises(ValueError, match='evidence missing'):
        retry(registry, original)


def test_retry_admission_charges_old_unknown_usage_and_stops_current_unknown(tmp_path):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from qumus2.core.run_paths import original_run_path
    from scripts.record_three_demos import DemoAdmission

    root, original, ledger, _ = legacy_failed_fixture(tmp_path)
    new = retry(TrialRegistry(root), original)
    store = RunStore(Path(new['workspace']) / 'qumus.db')
    scope = str(original_run_path(Path(store.path)))
    reserve = ledger.reserve(scope, 1, new['attempt_budget_usd'])
    rt = SimpleNamespace(store=store, _http_max_retries=2, _budget_scope=scope,
                         cfg={"openai": {"min_request_interval_s": 60.0}}, _ledger=ledger,
        run_budget_usd=new['attempt_budget_usd'], _reservations={'orchestrator': reserve},
        _request_context={'agent': 'orchestrator', 'call_id': 'new', 'context_window_tokens': 2_000_000},
        creds=SimpleNamespace(model_id=MODEL, web_search=False, budget_path=str(ledger.path),
                              input_price=10., output_price=50., test_budget_usd=120.))
    guard = DemoAdmission(new, source_reader=lambda: 'new-freeze')
    request = httpx.Request('POST', 'https://offline.invalid/v1/responses',
                           json={'model': MODEL, 'input': 'synthetic', 'max_output_tokens': 10})
    asyncio.run(guard(request, rt))
    event = store.events_after(0)[-1]
    assert event.payload['ordinal'] == 3
    assert event.payload['story_input_charged_before'] == 270  # includes old unknown bound
    assert event.payload['story_output_charged_before'] == 35
    with pytest.raises(ValueError, match='Repeated logical call'):
        asyncio.run(guard(request, rt))
    assert ledger.total(str(Path(original['workspace']) / 'qumus.db')) == 2.25
    store.close()


def test_saved_retry_after_blocks_early_explicit_retry_without_new_allocation(tmp_path):
    import time

    root, original, _, _ = legacy_failed_fixture(tmp_path)
    store = RunStore(Path(original['workspace']) / 'qumus.db')
    store.emit('demo.retry_guidance', {'request_id': 'unknown', 'retry_after_status': 'parsed',
                                     'not_before_unix': time.time() + 3600})
    store.close()
    registry = TrialRegistry(root)
    with pytest.raises(ValueError, match='backoff has not elapsed'):
        retry(registry, original)
    with registry.connect() as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 1


@pytest.mark.parametrize('header,status', [(None, 'absent'), ('60', 'parsed'),
    ('Thu, 29 Oct 2026 12:00:00 GMT', 'parsed'), ('invalid', 'invalid')])
def test_backoff_observer_retains_only_parsed_safe_metadata(tmp_path, header, status):
    import asyncio
    import time
    from types import SimpleNamespace

    import httpx

    from scripts.record_three_demos import install_retry_observer, retry_guidance

    store = RunStore(tmp_path / 'synthetic.db')
    hooks = {'response': []}
    from openai import AsyncOpenAI
    client = SimpleNamespace(_client=SimpleNamespace(event_hooks=hooks, send=lambda *a, **kw: None),
        _parse_retry_after_header=lambda h: AsyncOpenAI._parse_retry_after_header(None, h))
    client._should_retry = lambda response: AsyncOpenAI._should_retry(client, response)
    rt = SimpleNamespace(_ensure=lambda: None, store=store,
        _client=client,
        _metrics=SimpleNamespace(latest={'request_id': 'r1'}))
    install_retry_observer(rt)
    headers = {'x-private': 'must-not-persist'}
    if header is not None:
        headers['retry-after'] = header
    response = httpx.Response(429, headers=headers)
    store.emit('agent.request_failure', {'request_id': 'r1', 'http_status': 429})
    before = time.time()
    asyncio.run(hooks['response'][0](response))
    events = [e.model_dump() for e in store.events_after(0)]
    guidance = retry_guidance(events)
    assert guidance['retry_after_status'] == status
    assert 'must-not-persist' not in json.dumps(events)
    assert not guidance['automatic_retry']
    if header == '60':
        assert before + 60 <= guidance['not_before_unix'] <= time.time() + 60
    if status != 'parsed':
        assert guidance['not_before_unix'] is None
    store.close()


def test_successful_story_and_unknown_story_are_never_replaced(tmp_path):
    registry, ledger = setup_registry(tmp_path)
    start(registry, ledger, 'honest_stop')
    registry.finish('honest_stop', passed=True)
    with pytest.raises(ValueError, match='successful stories are locked'):
        registry.start('honest_stop', source='new', ledger=ledger, limits=story_limits('honest_stop'), retry_failed=True)
    with pytest.raises(ValueError, match='Known story'):
        registry.start('benchmark', source='new', ledger=ledger, limits={'test': True})


def test_explicit_retry_uses_new_model_session_without_contacting_provider(tmp_path):
    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.app import build_services

    root, original, ledger, _ = legacy_failed_fixture(tmp_path)
    old = RunStore(Path(original['workspace']) / 'qumus.db')
    pid = old.create_project('synthetic-old')
    old.update_project_state(pid, openai_session_id='original-session')
    old.close()
    new = retry(TrialRegistry(root), original)
    creds = OpenAICredentials('offline', model_id=MODEL, input_price=10, output_price=50, budget_path=str(ledger.path))
    services = build_services(Path(new['workspace']) / 'qumus.db', use_openai=True,
                              openai_credentials=creds, run_budget_usd=new['attempt_budget_usd'])
    assert services.runtime.session_id != 'original-session'
    assert services.runtime._client is None
    assert not any(e.kind == 'agent.request' for e in services.store.events_after(0))
    services.store.close()


def test_demo_uses_existing_shared_sixty_second_pacing_without_global_config_change(services, tmp_path, monkeypatch):
    import asyncio

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.config import load_yaml
    from scripts.record_three_demos import LIMITS, configure_demo_pacing

    before = load_yaml('agents.yaml')
    creds = OpenAICredentials('offline', model_id=MODEL, input_price=10, output_price=50,
                              budget_path=str(tmp_path / 'synthetic-budget.db'))
    rt = OpenAIRuntime(services.gateway, creds, http_max_retries=0)
    configure_demo_pacing(rt)
    assert rt.cfg['openai']['min_request_interval_s'] == LIMITS['min_request_interval_s'] == 60
    sleeps = []
    async def fake_sleep(delay):
        sleeps.append(delay)
    monkeypatch.setattr(asyncio, 'sleep', fake_sleep)
    async def run():
        from types import SimpleNamespace

        from agents.run_config import ModelInputData

        rt._ensure()
        for role in ('orchestrator', 'eyes'):
            agent = rt._agents[role]
            prepared = await rt._config.call_model_input_filter(SimpleNamespace(
                agent=agent, model_data=ModelInputData(input=[], instructions='synthetic')))
            await rt._hooks.on_llm_start(None, agent, prepared.instructions, prepared.input)
        assert len(sleeps) == 1 and 59 <= sleeps[0] <= 60
        assert rt._client.max_retries == 0
        assert not any(e.kind == 'agent.request' for e in services.store.events_after(0, limit=10000))
        await rt.close()
    asyncio.run(run())
    assert load_yaml('agents.yaml') == before


def run_sdk_retry_fixture(tmp_path, monkeypatch, statuses, *, exhausted=None, failure_header='35', next_call=False, early_wake=False,
                          qualification_reset=False):
    """Actual SDK retries + actual metrics/ledger hooks, entirely MockTransport."""
    import asyncio
    import time
    from types import SimpleNamespace

    import anyio
    import httpx
    import openai
    from agents.usage import Usage

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from scripts import record_three_demos as demo

    limits = demo.story_limits('collaboration')
    monkeypatch.setattr(demo, 'LIMITS', limits)
    monkeypatch.setattr(demo, 'story_limits', lambda story: limits)
    payload = {'model': MODEL, 'input': 'synthetic', 'max_output_tokens': 10}
    if exhausted == 'http_attempts':
        limits['http_attempts'] = 1
    elif exhausted == 'input_tokens':
        limits['input_tokens'] = demo.input_allowance(payload)
    elif exhausted == 'output_tokens':
        limits['output_tokens'] = 10
    folder = tmp_path / 'synthetic-attempt'
    services = build_services(folder / 'qumus.db', use_openai=False)
    manifest = {'story': 'collaboration', 'source': 'frozen-test', 'started_wall': time.time(), 'limits': limits,
                'workspace': str(folder), 'ledger': str(tmp_path / 'synthetic-ledger.db'),
                'attempt_budget_usd': limits['estimated_usd']}
    sent, sleeps, logical_sleeps, send_times, waiting = [], [], [], [], []
    elapsed = [0.0]
    def transport(request):
        send_times.append(elapsed[0])
        sent.append(request)
        state = statuses[len(sent) - 1]
        if state == 'connection':
            raise httpx.ConnectError('synthetic connection loss', request=request)
        if state != 200:
            return httpx.Response(state, headers={'retry-after': failure_header} if failure_header is not None else {},
                                  json={'error': {'message': 'synthetic error', 'type': 'rate_limit'}})
        return httpx.Response(200, json={'id': 'synthetic-response', 'object': 'response',
            'created_at': 1, 'status': 'completed', 'model': MODEL, 'output': [],
            'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}})
    async def sdk_sleep(delay):
        sleeps.append(delay)
        elapsed[0] += delay
    monkeypatch.setattr(anyio, 'sleep', sdk_sleep)
    async def logical_sleep(delay):
        logical_sleeps.append(delay)
        waiting.append({'sent': len(sent), 'admitted': sum(e.kind == 'demo.http_admitted'
            for e in services.store.events_after(0, limit=100000))})
        elapsed[0] += min(10.0, delay) if early_wake else delay
    monkeypatch.setattr(asyncio, 'sleep', logical_sleep)
    monkeypatch.setattr(openai, 'DefaultAsyncHttpxClient', lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(transport), **kw))
    creds = OpenAICredentials('offline', model_id=MODEL, input_price=10, output_price=50,
        context_window_tokens=2_000_000, stage_budget_usd=None, budget_path=manifest['ledger'])
    rt = OpenAIRuntime(services.gateway, creds, run_budget_usd=limits['estimated_usd'],
        http_max_retries=limits['http_max_retries'],
        pre_send_admission=demo.DemoAdmission(manifest, source_reader=lambda: 'frozen-test'))
    demo.configure_demo_pacing(rt)
    async def run():
        from agents.run_config import ModelInputData

        # Advance only this event loop's monotonic clock; no real waiting or network.
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, 'time', lambda: 10_000.0 + elapsed[0])
        demo.install_retry_observer(rt)
        agent = rt._agents['orchestrator']
        if exhausted == 'estimated_usd':
            cap = rt._request_allowance(agent, 'synthetic', []) + .001
            limits['estimated_usd'] = cap
            manifest['attempt_budget_usd'] = cap
            rt.run_budget_usd = cap
        # This transport fixture supplies its own synthetic HTTP body and
        # allowance inputs. Exercise SDK ordering: input filter, then hooks.
        await rt._config.call_model_input_filter(SimpleNamespace(
            agent=agent, model_data=ModelInputData(input=[], instructions='synthetic')))
        await rt._hooks.on_llm_start(None, agent, 'synthetic', [])
        error = None
        try:
            response = await rt._client.responses.create(**payload)
            # Normal runtime end hook, not manual test settlement of a chosen row.
            await rt._hooks.on_llm_end(None, agent, SimpleNamespace(
                raw_usage=response.usage.model_dump(),
                usage=Usage(requests=1, input_tokens=10, output_tokens=2, total_tokens=12)))
            if next_call or qualification_reset:
                if qualification_reset:
                    previous_client = rt._client._client
                    await rt.select_model(rt.creds.model_id, rt.creds.input_price, rt.creds.output_price,
                                          cached_input_price=rt.creds.cached_input_price)
                    demo.install_retry_observer(rt)
                    assert rt._client._client is not previous_client
                    wrapped = rt._client._client.send
                    observer_count = len(rt._client._client.event_hooks['response'])
                    demo.install_retry_observer(rt)
                    assert rt._client._client.send is wrapped
                    assert len(rt._client._client.event_hooks['response']) == observer_count
                    agent = rt._agents['orchestrator']
                await rt._config.call_model_input_filter(SimpleNamespace(
                    agent=agent, model_data=ModelInputData(input=[], instructions='synthetic next logical call')))
                await rt._hooks.on_llm_start(None, agent, 'synthetic next logical call', [])
                response = await rt._client.responses.create(**payload)
                await rt._hooks.on_llm_end(None, agent, SimpleNamespace(
                    raw_usage=response.usage.model_dump(),
                    usage=Usage(requests=1, input_tokens=10, output_tokens=2, total_tokens=12)))
                if qualification_reset:
                    await rt._config.call_model_input_filter(SimpleNamespace(
                        agent=agent, model_data=ModelInputData(input=[], instructions='synthetic after story retry')))
                    await rt._hooks.on_llm_start(None, agent, 'synthetic after story retry', [])
                    response = await rt._client.responses.create(**payload)
                    await rt._hooks.on_llm_end(None, agent, SimpleNamespace(raw_usage=response.usage.model_dump(),
                        usage=Usage(requests=1, input_tokens=10, output_tokens=2, total_tokens=12)))
        except Exception as exc:
            error = exc
        events = [e.model_dump() for e in services.store.events_after(0, limit=100000)]
        next_delay = rt._next_model_call - asyncio.get_running_loop().time()
        with sqlite3.connect(manifest['ledger']) as db:
            rows = db.execute('SELECT id,reserved,actual FROM spend ORDER BY rowid').fetchall()
        await rt.close()
        return error, events, rows, next_delay
    error, events, rows, next_delay = asyncio.run(run())
    charged = demo.attempt_usage(manifest)
    services.store.close()
    return SimpleNamespace(error=error, events=events, rows=rows, sent=sent, sleeps=sleeps,
                           charged=charged, next_delay=next_delay, manifest=manifest, logical_sleeps=logical_sleeps, send_times=send_times, waiting=waiting)


def test_sdk_429_then_success_preserves_failed_reservation_and_pairs_requests(tmp_path, monkeypatch):
    from scripts.record_three_demos import (
        http_accounting,
        http_evidence,
        validate_http_continuation,
    )
    result = run_sdk_retry_fixture(tmp_path, monkeypatch, [429, 200])
    assert result.error is None
    evidence = http_evidence(result.events)
    assert not evidence['provider_usage_complete']
    assert evidence['all_http_attempts_accounted_conservatively']
    assert evidence['failed_http_recoveries_verified']
    assert len(evidence['unknown_usage_http_attempts']) == 1
    assert len(result.sent) == 2 and result.sleeps == [35]
    entries, charged = http_accounting(result.events)
    assert entries[0]['admission']['call_id'] == entries[1]['admission']['call_id']
    assert entries[0]['request']['request_id'] != entries[1]['request']['request_id']
    assert [e['request']['attempt'] for e in entries] == [1, 2]
    assert [e['usage_known'] for e in entries] == [False, True]
    assert charged['input_tokens'] == entries[0]['admission']['input_allowance'] + 10
    assert charged['output_tokens'] == 12  # first bound 10 + actual final 2
    assert len(result.rows) == 2 and result.rows[0][2] is None
    assert result.rows[1][2] == pytest.approx(.0002)
    assert result.rows[0][0] == entries[0]['admission']['reservation_id']

    assert result.rows[1][0] == entries[1]['admission']['reservation_id']
    assert result.charged['estimated_usd'] == pytest.approx(result.rows[0][1] + .0002)
    assert result.charged['unknown_usage_attempts'] == 1
    assert 59 <= result.next_delay <= 60  # success after SDK wait delays NEXT logical call
    validate_http_continuation(entries, 'next-logical-call', 0)
    # Same call_id cannot cause the final usage report to fill the failed send.
    duplicate = next(e for e in result.events if e['kind'] == 'agent.request_usage')
    with pytest.raises(ValueError, match='Duplicated HTTP usage'):
        http_accounting(result.events + [duplicate])


def test_qualification_client_reset_keeps_retry_evidence_and_paces_next_call(tmp_path, monkeypatch):
    from scripts.record_three_demos import http_accounting

    result = run_sdk_retry_fixture(tmp_path, monkeypatch, [200, 429, 200, 200], qualification_reset=True)
    assert result.error is None and len(result.sent) == 4
    assert all(b-a >= 60 for a, b in zip(result.send_times, result.send_times[1:], strict=False))
    guidance = [e for e in result.events if e['kind'] == 'demo.retry_guidance']
    assert len(guidance) == 1 and guidance[0]['payload']['http_status'] == 429
    entries, _ = http_accounting(result.events)
    assert [entry['usage_known'] for entry in entries] == [True, False, True, True]
    assert entries[1]['admission']['call_id'] == entries[2]['admission']['call_id']
    assert entries[3]['admission']['call_id'] != entries[2]['admission']['call_id']
    assert entries[1]['admission']['reservation_id'] != entries[2]['admission']['reservation_id']
    assert result.rows[1][2] is None  # Failed story attempt survives qualification's client reset.


def test_sdk_three_429s_exit_with_three_unsettled_rows_and_no_fourth_send(tmp_path, monkeypatch):
    import openai
    result = run_sdk_retry_fixture(tmp_path, monkeypatch, [429, 429, 429])
    assert isinstance(result.error, openai.RateLimitError)
    from scripts.record_three_demos import http_evidence
    evidence = http_evidence(result.events)
    assert evidence['all_http_attempts_accounted_conservatively']
    assert not evidence['provider_usage_complete'] and not evidence['failed_http_recoveries_verified']
    assert len(result.sent) == 3 and result.sleeps == [35, 35]
    assert [r[2] for r in result.rows] == [None, None, None]
    assert result.charged['http_attempts'] == result.charged['unknown_usage_attempts'] == 3
    assert result.charged['estimated_usd'] == sum(r[1] for r in result.rows)
    assert len([e for e in result.events if e['kind'] == 'agent.request_failure']) == 3


@pytest.mark.parametrize('resource', ['input_tokens', 'output_tokens', 'http_attempts', 'estimated_usd'])
def test_each_cumulative_budget_can_block_sdk_retry_before_second_send(tmp_path, monkeypatch, resource):
    from qumus2.agents.openai_runtime import BudgetExceeded
    result = run_sdk_retry_fixture(tmp_path, monkeypatch, [429], exhausted=resource)
    assert isinstance(result.error, BudgetExceeded)
    assert len(result.sent) == 1 and len(result.rows) == 1
    assert result.rows[0][2] is None  # rejection cannot release the failed first send
    assert result.charged['http_attempts'] == 1
    assert len([e for e in result.events if e['kind'] == 'agent.request']) == 1


def test_sdk_transport_retry_needs_recorded_failure_and_keeps_unknown_cost(tmp_path, monkeypatch):
    from scripts.record_three_demos import http_accounting, validate_http_continuation
    result = run_sdk_retry_fixture(tmp_path, monkeypatch, ['connection', 200])
    assert result.error is None and len(result.sent) == 2
    entries, _ = http_accounting(result.events)
    assert entries[0]['failure']['failure_kind'] == 'connection_error'
    assert result.rows[0][2] is None and result.rows[1][2] is not None
    incomplete = [dict(entries[0], failure=None, guidance=None)]
    with pytest.raises(ValueError, match='recorded retryable failure'):
        validate_http_continuation(incomplete, entries[0]['admission']['call_id'], 1)


def test_sdk_nonretryable_response_and_excessive_backoff_do_not_retry(tmp_path, monkeypatch):
    result = run_sdk_retry_fixture(tmp_path, monkeypatch, [400])
    assert result.error is not None and len(result.sent) == 1 and result.sleeps == []
    long_backoff = run_sdk_retry_fixture(tmp_path / 'long', monkeypatch, [429], failure_header='3600')
    assert long_backoff.error is not None and len(long_backoff.sent) == 1 and long_backoff.sleeps == []


def test_recovered_unknown_send_does_not_block_next_paced_logical_call(tmp_path, monkeypatch):
    from scripts.record_three_demos import http_accounting, http_evidence
    result = run_sdk_retry_fixture(tmp_path, monkeypatch, [429, 200, 200], next_call=True)
    assert result.error is None and len(result.sent) == 3
    assert result.sleeps == [35]
    assert result.logical_sleeps == [25, 60]
    assert result.send_times == [0, 60, 120]
    entries, _ = http_accounting(result.events)
    assert entries[0]['admission']['call_id'] == entries[1]['admission']['call_id']
    assert entries[2]['admission']['call_id'] != entries[1]['admission']['call_id']
    assert [e['request']['attempt'] for e in entries] == [1, 2, 1]
    assert len({row[0] for row in result.rows}) == 3
    assert result.rows[0][2] is None and all(row[2] is not None for row in result.rows[1:])
    evidence = http_evidence(result.events)
    assert evidence['all_http_attempts_accounted_conservatively'] and evidence['failed_http_recoveries_verified']
    assert not evidence['provider_usage_complete']


def test_same_call_id_never_allows_usage_to_fill_another_http_send(tmp_path, monkeypatch):
    from copy import deepcopy

    from scripts.record_three_demos import http_accounting, http_evidence
    result = run_sdk_retry_fixture(tmp_path, monkeypatch, [429, 200])
    altered = deepcopy(result.events)
    sends = [e for e in altered if e['kind'] == 'agent.request']
    report = next(e for e in altered if e['kind'] == 'agent.request_usage')
    report['payload']['request_id'] = sends[0]['payload']['request_id']
    evidence = http_evidence(altered)
    assert not evidence['provider_usage_complete']
    assert not evidence['all_http_attempts_accounted_conservatively']
    assert not evidence['failed_http_recoveries_verified']
    duplicate_reservation = deepcopy(result.events)
    admissions = [e for e in duplicate_reservation if e['kind'] == 'demo.http_admitted']
    admissions[1]['payload']['reservation_id'] = admissions[0]['payload']['reservation_id']
    with pytest.raises(ValueError, match='cannot reuse a spend reservation'):
        http_accounting(duplicate_reservation)


@pytest.mark.parametrize('header,expected', [('35', 60), ('90', 90), (None, 60)])
def test_all_actual_http_sends_observe_sixty_seconds_or_longer_server_backoff(tmp_path, monkeypatch, header, expected):
    result = run_sdk_retry_fixture(tmp_path, monkeypatch, [429, 200], failure_header=header)
    assert result.error is None
    assert result.send_times == pytest.approx([0, expected])
    assert all(b - a >= 60 - 1e-9 for a, b in zip(result.send_times[:-1], result.send_times[1:], strict=True))
    # No second admission/send is recorded while the residual pacing wait runs.
    assert all(state == {'sent': 1, 'admitted': 1} for state in result.waiting)
    if header == '90':
        assert result.sleeps == [90] and result.logical_sleeps == []
    elif header == '35':
        assert result.sleeps == [35] and result.logical_sleeps == [25]


def test_retry_pacing_rechecks_clock_after_early_wake_and_spaces_transport_failures(tmp_path, monkeypatch):
    early = run_sdk_retry_fixture(tmp_path / 'early', monkeypatch, [429, 200], early_wake=True)
    assert early.error is None and early.send_times == [0, 60]
    assert early.logical_sleeps == [25, 15, 5]
    assert early.waiting == [{'sent': 1, 'admitted': 1}] * 3
    connection = run_sdk_retry_fixture(tmp_path / 'connection', monkeypatch, ['connection', 200])
    assert connection.error is None and connection.send_times == pytest.approx([0, 60])



def test_early_logical_wake_cannot_send_before_previous_http_terminal_deadline(tmp_path, monkeypatch):
    result = run_sdk_retry_fixture(tmp_path, monkeypatch, [429, 200, 200], next_call=True, early_wake=True)
    assert result.error is None
    assert result.send_times == [0, 60, 120]
    assert result.sleeps == [35]
    # Logical pacing requests60s but the fake scheduler returns after10s.
    # Initial-send admission must then wait the remaining50s, rechecking wakes.
    assert result.logical_sleeps == [25, 15, 5, 60, 50, 40, 30, 20, 10]
    assert result.waiting[:3] == [{'sent': 1, 'admitted': 1}] * 3
    assert result.waiting[3:] == [{'sent': 2, 'admitted': 2}] * 6


def closeout_fixture(tmp_path, story='complete'):
    registry, ledger = setup_registry(tmp_path)
    binding = {'base_url': 'https://offline.invalid', 'api': 'responses', 'model_id': MODEL}
    manifest = registry.start(story, source='frozen-source', ledger=ledger, limits=story_limits(story),
                              runtime_contract=binding)
    store = RunStore(Path(manifest['workspace']) / 'qumus.db')
    pid = store.create_project('Synthetic closeout only')
    store.update_project_state(pid, openai_session_id='synthetic-saved-session')
    if story == 'collaboration':
        store.emit('user.message', {'text': STORIES[story]['events'][-1]['message']})
    store.emit('agent.turn_end', {'is_error': True, 'reason': 'budget_exhausted',
                                 'session_id': 'synthetic-saved-session'})
    registry.finish(story, passed=False, result={'error': None, 'checks': {'model_turns_completed': False, 'no_safety_violation': True}})
    contract = {**binding, 'test_budget_usd': 80 if story == 'complete' else 30, 'cached_input_price': 1}
    return registry, ledger, manifest, store, pid, contract


def test_closeout_appends_authorization_and_result_without_rewriting_failure(tmp_path):
    registry, ledger, manifest, store, _, contract = closeout_fixture(tmp_path)
    original = registry.latest('complete')
    segment = registry.authorize_closeout('complete', source='new-source', ledger=ledger, runtime_contract=contract)
    assert segment['original_manifest'] == manifest
    assert segment['checkpoint']['session_id'] == 'synthetic-saved-session'
    assert segment['extra_limits'] == {'http_attempts': 12, 'wall_seconds': 1200}
    assert segment['allocation'] == {'complete': 80, 'collaboration': 30, 'honest_stop': 10}
    assert segment['runtime_contract']['cached_input_price'] == 1
    assert registry.latest('complete') == original
    registry.finish_closeout(segment, {'machine_checks_passed': True})
    assert registry.latest('complete') == original
    with pytest.raises(sqlite3.IntegrityError):
        registry.finish_closeout(segment, {'machine_checks_passed': False})
    with pytest.raises(ValueError, match='already authorized'):
        registry.authorize_closeout('complete', source='new-source', ledger=ledger, runtime_contract=contract)
    with pytest.raises(ValueError, match='closeout already exists'):
        registry.start('complete', source='new-source', ledger=ledger, limits=story_limits('complete'), retry_failed=True)
    store.close()


@pytest.mark.parametrize('condition', ['not_budget', 'session', 'active_job', 'parked', 'lock', 'reservation', 'pending', 'safety', 'ledger', 'hidden_cap', 'gateway', 'model'])
def test_closeout_rejects_unproven_or_changed_binding(tmp_path, condition):
    registry, ledger, _, store, pid, contract = closeout_fixture(tmp_path)
    if condition == 'not_budget':
        store.emit('agent.turn_end', {'is_error': True, 'reason': 'other', 'session_id': 'synthetic-saved-session'})
    if condition == 'session':
        store.update_project_state(pid, openai_session_id='different-session')
    if condition == 'active_job':
        store.create_run('search_chip', {}, project_id=pid)
    if condition == 'parked':
        rid = store.create_run('search_chip', {}, project_id=pid)
        store.set_run_status(rid, 'parked')
    if condition == 'lock':
        store.acquire_locks(['microscope'], 'synthetic-run')
    if condition == 'reservation':
        store.reserve('sample', 'chip_001', 'synthetic-run')
    if condition == 'pending':
        store.add_instruction('Unprocessed operator request')
    if condition == 'safety':
        store.emit('safety.violation', {'note': 'Synthetic test'})
    if condition == 'ledger':
        ledger = tmp_path / 'other-ledger.db'
        ledger.touch()
    if condition == 'hidden_cap':
        contract['test_budget_usd'] = 40
    if condition == 'gateway':
        contract['base_url'] = 'https://changed.invalid'
    if condition == 'model':
        contract['model_id'] = 'changed-model'
    with pytest.raises(ValueError):
        registry.authorize_closeout('complete', source='new-source', ledger=ledger, runtime_contract=contract)
    store.close()


def test_closeout_requires_actual_model_reply_and_preserved_prefix(tmp_path):
    from types import SimpleNamespace

    from scripts.record_three_demos import audit_closeout

    registry, ledger, _, store, pid, contract = closeout_fixture(tmp_path, 'collaboration')
    segment = registry.authorize_closeout('collaboration', source='new-source', ledger=ledger, runtime_contract=contract)
    services = SimpleNamespace(store=store, project_id=pid, runtime=SimpleNamespace(session_id='synthetic-saved-session'))
    def check():
        checks, evidence = {'model_turns_completed': False}, {}
        audit_closeout(segment, services, [e.model_dump() for e in store.events_after(0)], checks, evidence)
        return checks
    assert not check()['last_instruction_has_model_response']
    store.emit('agent.request_usage', {'request_id': 'synthetic'})
    store.emit('agent.message', {'text': 'Synthetic final reply'}, agent='orchestrator')
    store.emit('agent.turn_end', {'is_error': False})
    checks = check()
    assert checks['last_instruction_has_model_response']
    assert checks['historical_budget_stop_acknowledged'] and checks['original_event_prefix_preserved']
    assert 'model_turns_completed' not in checks
    assert not checks['independent_review_recorded']
    assert checks['review_not_committed_as_device_pair']
    store.create_run('search_chip', {}, project_id=pid)
    assert not check()['no_repeated_physical_work']
    store._x("UPDATE event SET payload_json='{}' WHERE id=1")
    assert not check()['original_event_prefix_preserved']
    store.close()


def test_closeout_gateway_blocks_physical_restart_but_allows_final_status(services):
    from scripts.record_three_demos import install_closeout_guard

    install_closeout_guard(services)
    for name, agent in [('search_chip', 'hands'), ('exfoliate_chips', 'hands'),
                        ('reactor_material_metal', 'reactor'), ('stack_device', 'hands')]:
        decision = services.gateway.pre(agent, name, {})
        assert not decision.allow and decision.rule == 'demo_closeout'
    assert services.gateway.pre('orchestrator', 'save_project_status', {'status': 'done', 'summary': 'test'}).allow
    assert services.gateway.pre('orchestrator', 'list_samples', {}).rule != 'demo_closeout'


@pytest.mark.parametrize('story', ['complete', 'collaboration'])
@pytest.mark.parametrize('boundary', ['requests', 'deadline', 'cached_rate'])
def test_closeout_http_admission_enforces_extra_bounds_and_frozen_rate(tmp_path, story, boundary):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from qumus2.agents.openai_budget import SpendLedger
    from qumus2.core.run_paths import original_run_path
    from scripts.record_three_demos import CLOSEOUT_EXTRA, DemoAdmission

    registry, ledger_path, original, store, _, contract = closeout_fixture(tmp_path, story)
    segment = registry.authorize_closeout(story, source='new-source', ledger=ledger_path, runtime_contract=contract)
    limits = segment['limits']
    contract = {'base_url': 'https://offline.invalid', 'api': 'responses', 'input_price': 10,
                'output_price': 50, 'cached_input_price': 1, 'stage_budget_usd': None,
                'test_budget_usd': segment['attempt_budget_usd']}
    manifest = {**original, 'source': 'new-source', 'started_wall': 100,
                'limits': limits, 'runtime_contract': contract, 'closeout': segment,
                'attempt_budget_usd': segment['attempt_budget_usd']}
    ledger = SpendLedger(ledger_path, None)
    scope = str(original_run_path(Path(original['workspace']) / 'qumus.db'))
    reservation = ledger.reserve(scope, 1, segment['attempt_budget_usd'])
    for i in range(CLOSEOUT_EXTRA[story]['http_attempts'] if boundary == 'requests' else 1):
        store.emit('demo.http_admitted', {'call_id': str(i), 'input_allowance': 5, 'output_allowance': 2})
        store.emit('agent.request', {'call_id': str(i), 'request_id': str(i)})
        store.emit('agent.request_usage', {'request_id': str(i), 'call_id': str(i), 'input_tokens': 5, 'output_tokens': 2})
    creds = SimpleNamespace(**{**contract, 'cached_input_price': 2 if boundary == 'cached_rate' else 1},
                            model_id=MODEL, web_search=False, budget_path=str(ledger_path))
    runtime = SimpleNamespace(store=store, creds=creds, _http_max_retries=2, _budget_scope=scope,
        cfg={'openai': {'min_request_interval_s': 60.0}}, _ledger=ledger,
        run_budget_usd=segment['attempt_budget_usd'], _reservations={'orchestrator': reservation},
        _request_context={'agent': 'orchestrator', 'call_id': 'next', 'context_window_tokens': 2_000_000})
    now = 100 + CLOSEOUT_EXTRA[story]['wall_seconds'] if boundary == 'deadline' else 101
    guard = DemoAdmission(manifest, source_reader=lambda: 'new-source', now=lambda: now)
    request = httpx.Request('POST', 'https://offline.invalid/v1/responses',
                            json={'model': MODEL, 'input': 'synthetic', 'max_output_tokens': 10})
    before = store.last_event_id()
    expected = {'requests': 'extra HTTP', 'deadline': 'wall-clock', 'cached_rate': 'cached input pricing'}[boundary]
    with pytest.raises(ValueError, match=expected):
        asyncio.run(guard(request, runtime))
    assert store.last_event_id() == before
    assert ledger.total(scope) == 1
    store.close()


def test_closeout_driver_retains_failed_export_and_does_not_replay_story(tmp_path, monkeypatch):
    import asyncio
    from dataclasses import replace

    from qumus2 import app
    from qumus2.agents import openai_credentials
    from qumus2.agents.driver import ConversationDriver
    from scripts import record_three_demos as demo

    registry, ledger, manifest, store, _, _ = closeout_fixture(tmp_path, 'collaboration')
    last = store.last_event_id()
    store.close()
    folder = Path(manifest['workspace'])
    old_export = folder / f'export-through-{last}'
    old_export.mkdir()
    (old_export / 'recording.json').write_text('SYNTHETIC ORIGINAL FAILED EXPORT')
    old_summary = folder / f'summary-through-{last}.json'
    old_summary.write_text('SYNTHETIC ORIGINAL FAILED SUMMARY')
    before = registry.latest('collaboration')
    class LocalRegistry(TrialRegistry):
        def __init__(self):
            super().__init__(registry.root)
    monkeypatch.setattr(demo, 'TrialRegistry', LocalRegistry)
    monkeypatch.setattr(demo, 'frozen_source', lambda: 'synthetic-closeout-source')
    creds = openai_credentials.OpenAICredentials('offline', model_id=MODEL, input_price=10,
        output_price=50, test_budget_usd=40, budget_path=str(ledger), base_url='https://offline.invalid')
    monkeypatch.setattr(openai_credentials, 'load_openai_credentials', lambda *a: creds)
    real_build = app.build_services
    sent = []
    def build(path, **kwargs):
        assert kwargs['run_budget_usd'] == kwargs['openai_credentials'].test_budget_usd == 30
        s = real_build(path, use_openai=False)
        class OfflineRuntime:
            busy = False
            budget_exhausted = False
            session_id = 'synthetic-saved-session'
            gateway = s.gateway
            async def send(self, text):
                sent.append(text)
                s.store.emit('synthetic.closeout', {'note': 'No provider request'})
            async def interrupt(self):
                s.store.emit('agent.interrupted', {})
            async def close(self):
                pass
        runtime = OfflineRuntime()
        return replace(s, runtime=runtime, driver=ConversationDriver(s.store, runtime, s.jobs))
    monkeypatch.setattr(app, 'build_services', build)
    result = asyncio.run(demo.record_demo('collaboration', 'synthetic-closeout-source', closeout=True))
    assert not result['machine_checks_passed']  # Synthetic lifecycle is never model evidence.
    assert len(sent) == 1 and 'recorded budget stop' in sent[0]
    assert 'After any delegated image review returns' in sent[0]
    assert 'update_plan' in sent[0] and 'independent_flake_review' in sent[0]
    assert 'Preserve all completed physical plan steps' in sent[0]
    assert 'chip_002_f004' not in sent[0] and 'chip_003_f005' not in sent[0]
    assert 'export-closeout-through-' in result['export']
    assert registry.latest('collaboration') == before
    assert (old_export / 'recording.json').read_text() == 'SYNTHETIC ORIGINAL FAILED EXPORT'
    assert old_summary.read_text() == 'SYNTHETIC ORIGINAL FAILED SUMMARY'
    with registry.connect() as db:
        assert db.execute('SELECT kind FROM closeout ORDER BY kind').fetchall() == [('authorization',), ('result',)]


@pytest.mark.parametrize('condition', ['valid', 'missing', 'hot', 'target', 'one_stage', 'active_job', 'lock', 'reservation', 'not_done'])
def test_closeout_requires_fresh_two_stage_completion_and_released_resources(tmp_path, condition):
    from types import SimpleNamespace

    from scripts.record_three_demos import audit_closeout

    registry, ledger, _, store, pid, contract = closeout_fixture(tmp_path, 'collaboration')
    segment = registry.authorize_closeout('collaboration', source='new-source', ledger=ledger, runtime_contract=contract)
    store.update_project_state(pid, status='active' if condition == 'not_done' else 'done')
    rid = store.create_run('cool_experiment_stages', {}, project_id=pid)
    store.set_run_status(rid, 'running' if condition == 'active_job' else 'succeeded')
    readings = {stage: {'temperature_c': 22, 'target_c': None, 'fan_commanded_on': False}
                for stage in ('stamp_heater', 'exfoliation_heater')}
    if condition == 'hot':
        readings['stamp_heater']['temperature_c'] = 40
    if condition == 'target':
        readings['stamp_heater']['target_c'] = 22
    if condition == 'one_stage':
        del readings['stamp_heater']
    if condition == 'lock':
        store.acquire_locks(['stamp_heater'], rid)
    if condition == 'reservation':
        store.reserve('sample', 'chip_002', rid)
    if condition != 'missing':
        store.emit('experiment.stages_verified', {'project_id': pid, 'cooling_job_id': rid,
                   'below_c': 40, 'readings': readings}, run_id=rid)
    services = SimpleNamespace(store=store, project_id=pid, runtime=SimpleNamespace(session_id='synthetic-saved-session'))
    checks = {'model_turns_completed': False}
    audit_closeout(segment, services, [e.model_dump() for e in store.events_after(0)], checks, {})
    terminal = [checks[k] for k in ('project_done', 'no_unfinished_jobs',
                'no_resource_locks_or_reservations', 'fresh_stages_verified_below_40')]
    assert all(terminal) == (condition == 'valid')
    store.close()


def interrupted_closeout_fixture(tmp_path):
    from qumus2.agents.openai_budget import SpendLedger
    from qumus2.core.run_paths import original_run_path

    registry, ledger_path, manifest, store, pid, contract = closeout_fixture(tmp_path, 'collaboration')
    parent = registry.authorize_closeout('collaboration', source='first-closeout', ledger=ledger_path,
                                        runtime_contract=contract)
    ledger = SpendLedger(ledger_path, None)
    scope = str(original_run_path(Path(manifest['workspace']) / 'qumus.db'))
    for i in range(11):
        known = i < 9
        call_id = str(i) if known else 'interrupted-call'
        reservation = ledger.reserve(scope, .1 if known else 2.95236, 30)
        store.emit('demo.http_admitted', {'call_id': call_id, 'reservation_id': reservation,
                   'sdk_retry_count': 0 if known else i - 9, 'input_allowance': 100, 'output_allowance': 20})
        store.emit('agent.request', {'call_id': call_id, 'request_id': str(i), 'attempt': 1 if known else i - 8})
        if known:
            store.emit('agent.request_usage', {'call_id': call_id, 'request_id': str(i), 'input_tokens': 10, 'output_tokens': 2})
            ledger.settle(reservation, .1)
        else:
            store.emit('agent.request_failure', {'call_id': call_id, 'request_id': str(i),
                       'failure_kind': 'connection_error', 'exception_type': 'ReadError' if i == 9 else 'ConnectError'})
    review = {'name': 'independent_flake_review', 'role': 'review', 'status': 'done', 'note': 'Synthetic completed review'}
    store.update_project_state(pid, artifacts={'independent_flake_review': review})
    store.emit('notification', {'text': 'lab process restarted after it stopped unexpectedly'})
    store.emit('agent.turn_end', {'is_error': True, 'reason': 'budget_exhausted', 'session_id': 'synthetic-saved-session'})
    folder = Path(manifest['workspace'])
    export = folder / f'export-closeout-through-{store.last_event_id()}'
    export.mkdir()
    bench = {'devices': [*({'id': name, 'kind': 'Heater', 'temperature': 22, 'target': None}
                           for name in ('stamp_heater', 'exfoliation_heater', 'reactor_heater')),
                         {'id': 'gas', 'flow_target_sccm': 0, 'flow_sccm': 0}]}
    (export / 'recording.json').write_text(json.dumps({'frames': [{'id': store.last_event_id(), 'bench': bench}]}))
    from scripts.record_three_demos import digest
    failed = {'machine_checks_passed': False, 'error': None, 'export': str(export),
              'evidence': {'event_prefix_sha256': digest([e.model_dump() for e in store.events_after(0)])},
              'checks': {'independent_review_recorded': True, 'no_safety_violation': True,
                         'request_accounting_complete': False, 'closeout_turn_completed': False}}
    (folder / f'summary-closeout-through-{store.last_event_id()}.json').write_text(json.dumps(failed))
    registry.finish_closeout(parent, failed)
    receipt = tmp_path / 'synthetic-host-interruption.json'
    receipt.write_text(json.dumps({'paid_processes_running': False,
        'power_events': ['Synthetic Clamshell Sleep', 'Synthetic FullWake'],
        'provider_dashboard': {'account_private_balance': 'DO-NOT-EXPORT'},
        'collaboration_failed_export': str(export.relative_to(registry.root))}))
    contract = {**contract, 'test_budget_usd': 35}
    return registry, ledger_path, manifest, store, pid, parent, failed, contract, receipt


def test_interrupted_closeout_is_append_only_and_consumes_remaining_requests(tmp_path):
    registry, ledger, _, store, _, parent, failed, contract, receipt = interrupted_closeout_fixture(tmp_path)
    before = registry.latest('collaboration')
    segment = registry.authorize_interrupted_closeout('collaboration', source='new-frozen-source', ledger=ledger,
        runtime_contract=contract, interruption_receipt=receipt)
    assert segment['extra_limits'] == {'http_attempts': 9, 'wall_seconds': 900}
    assert segment['allocation'] == {'complete': 75, 'collaboration': 35, 'honest_stop': 10}
    assert segment['historical_failed_request_ids'] == ['9', '10']
    assert 'provider_dashboard' not in json.dumps(segment) and 'DO-NOT-EXPORT' not in json.dumps(segment)
    assert segment['usage_before']['unknown_reserved_usd'] == pytest.approx(5.90472)
    assert segment['usage_before']['http_attempts'] == 11
    assert segment['checkpoint']['session_id'] == parent['checkpoint']['session_id']
    registry.finish_closeout(segment, {'machine_checks_passed': False, 'note': 'Synthetic result'})
    assert registry.latest('collaboration') == before
    with registry.connect() as db:
        rows = dict(db.execute('SELECT kind,payload FROM closeout'))
    assert json.loads(rows['authorization']) == parent and json.loads(rows['result']) == failed
    assert set(rows) == {'authorization', 'result', 'interrupted_authorization', 'interrupted_result'}
    with pytest.raises(ValueError, match='Exactly one'):
        registry.authorize_interrupted_closeout('collaboration', source='again', ledger=ledger,
            runtime_contract=contract, interruption_receipt=receipt)
    store.close()


@pytest.mark.parametrize('condition', ['settled_unknown', 'changed_review', 'active_job', 'lock', 'pending',
                                      'unsafe_bench', 'host_receipt', 'missing_restart', 'bad_network', 'gateway'])
def test_interrupted_closeout_rejects_unsupported_recovery(tmp_path, condition):
    registry, ledger, _, store, pid, _, failed, contract, receipt = interrupted_closeout_fixture(tmp_path)
    if condition == 'settled_unknown':
        with sqlite3.connect(ledger) as db:
            db.execute('UPDATE spend SET actual=0 WHERE actual IS NULL')
    if condition == 'changed_review':
        store.update_project_state(pid, artifacts={})
    if condition == 'active_job':
        store.create_run('search_chip', {}, project_id=pid)
    if condition == 'lock':
        store.acquire_locks(['stamp_heater'], 'synthetic')
    if condition == 'pending':
        store.add_instruction('pending synthetic instruction')
    if condition == 'unsafe_bench':
        path = Path(failed['export']) / 'recording.json'
        data = json.loads(path.read_text())
        data['frames'][-1]['bench']['devices'][0]['target'] = 200
        path.write_text(json.dumps(data))
    if condition == 'host_receipt':
        receipt.write_text(json.dumps({'paid_processes_running': False, 'power_events': []}))
    if condition == 'missing_restart':
        store._x("UPDATE event SET payload_json='{}' WHERE kind='notification'")
    if condition == 'bad_network':
        store._x("UPDATE event SET payload_json=json_set(payload_json,'$.exception_type','UnknownError') WHERE kind='agent.request_failure'")
    if condition == 'gateway':
        contract['base_url'] = 'https://wrong.invalid'
    with pytest.raises(ValueError):
        registry.authorize_interrupted_closeout('collaboration', source='new', ledger=ledger,
            runtime_contract=contract, interruption_receipt=receipt)
    with registry.connect() as db:
        assert db.execute("SELECT count(*) FROM closeout WHERE kind='interrupted_authorization'").fetchone()[0] == 0
    store.close()


def test_only_explicit_historical_failure_ids_can_cross_logical_calls(tmp_path):
    from scripts.record_three_demos import http_accounting, validate_http_continuation

    _, _, _, store, _, _, _, _, _ = interrupted_closeout_fixture(tmp_path)
    entries, _ = http_accounting([e.model_dump() for e in store.events_after(0)])
    with pytest.raises(ValueError, match='unresolved usage'):
        validate_http_continuation(entries, 'new-call', 0)
    validate_http_continuation(entries, 'new-call', 0, ['9', '10'])
    with pytest.raises(ValueError, match='unresolved usage'):
        validate_http_continuation(entries, 'new-call', 0, ['9'])
    store.close()


def test_interrupted_guard_rejects_picker_images_and_new_artifact(services):
    from scripts.record_three_demos import install_closeout_guard

    install_closeout_guard(services, interrupted=True)
    assert not services.gateway.delegate('picker', task='review again').allow
    for agent, tool in [('picker', 'view_flake_images'), ('orchestrator', 'record_project_artifact'),
                        ('hands', 'search_chip')]:
        assert services.gateway.pre(agent, tool, {}).rule == 'demo_interrupted_closeout'
    assert services.gateway.pre('orchestrator', 'get_project_context', {}).allow
    assert services.gateway.pre('orchestrator', 'save_project_status', {'status': 'done', 'summary': 'test'}).allow


@pytest.mark.parametrize('condition', ['valid', 'requests_exhausted', 'changed_unknown'])
def test_interrupted_admission_retains_unknowns_and_enforces_remaining_http(tmp_path, condition):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from qumus2.agents.openai_budget import SpendLedger
    from qumus2.core.run_paths import original_run_path
    from scripts.record_three_demos import DemoAdmission

    registry, ledger_path, original, store, _, _, _, contract, receipt = interrupted_closeout_fixture(tmp_path)
    contract.update(input_price=10, output_price=50, stage_budget_usd=None)
    segment = registry.authorize_interrupted_closeout('collaboration', source='new', ledger=ledger_path,
        runtime_contract=contract, interruption_receipt=receipt)
    manifest = {**original, 'closeout': segment, 'source': 'new', 'limits': segment['limits'],
                'started_wall': 100, 'runtime_contract': contract, 'attempt_budget_usd': 35}
    if condition == 'requests_exhausted':
        for i in range(9):
            ident = f'suffix-{i}'
            store.emit('demo.http_admitted', {'call_id': ident, 'input_allowance': 10, 'output_allowance': 5})
            store.emit('agent.request', {'call_id': ident, 'request_id': ident})
            store.emit('agent.request_usage', {'call_id': ident, 'request_id': ident, 'input_tokens': 10, 'output_tokens': 5})
    if condition == 'changed_unknown':
        with sqlite3.connect(ledger_path) as db:
            db.execute('UPDATE spend SET actual=0 WHERE actual IS NULL')
    ledger = SpendLedger(ledger_path, None)
    scope = str(original_run_path(Path(original['workspace']) / 'qumus.db'))
    reservation = ledger.reserve(scope, 1, 35)
    runtime = SimpleNamespace(store=store, creds=SimpleNamespace(**contract, web_search=False, budget_path=str(ledger_path)),
        _http_max_retries=2, _budget_scope=scope, _ledger=ledger, run_budget_usd=35,
        cfg={'openai': {'min_request_interval_s': 60.0}}, _reservations={'orchestrator': reservation},
        _request_context={'agent': 'orchestrator', 'call_id': 'final-call', 'context_window_tokens': 2_000_000})
    request = httpx.Request('POST', 'https://offline.invalid/v1/responses',
                           json={'model': MODEL, 'input': 'synthetic final report', 'max_output_tokens': 10})
    guard = DemoAdmission(manifest, source_reader=lambda: 'new', now=lambda: 101)
    before = store.last_event_id()
    if condition == 'valid':
        asyncio.run(guard(request, runtime))
        assert store.events_after(before)[0].kind == 'demo.http_admitted'
    else:
        with pytest.raises(ValueError, match='extra HTTP' if condition == 'requests_exhausted' else 'reservations changed'):
            asyncio.run(guard(request, runtime))
        assert store.last_event_id() == before
    store.close()


def test_interrupted_acceptance_distinguishes_old_network_failures_from_successful_suffix(tmp_path):
    from types import SimpleNamespace

    from scripts.record_three_demos import audit_closeout, http_evidence

    registry, ledger, _, store, pid, _, _, contract, receipt = interrupted_closeout_fixture(tmp_path)
    segment = registry.authorize_interrupted_closeout('collaboration', source='new', ledger=ledger,
        runtime_contract=contract, interruption_receipt=receipt)
    store.emit('demo.http_admitted', {'call_id': 'final', 'input_allowance': 10, 'output_allowance': 5})
    store.emit('agent.request', {'call_id': 'final', 'request_id': 'final'})
    store.emit('agent.request_usage', {'call_id': 'final', 'request_id': 'final', 'input_tokens': 10, 'output_tokens': 5})
    store.emit('agent.message', {'text': 'Synthetic final response acknowledges prior interruptions'}, agent='orchestrator')
    store.emit('agent.turn_end', {'is_error': False, 'reason': 'success'})
    events = [e.model_dump() for e in store.events_after(0)]
    services = SimpleNamespace(store=store, project_id=pid, runtime=SimpleNamespace(session_id='synthetic-saved-session'))
    checks, evidence = {'model_turns_completed': False, 'request_accounting_complete': False}, {}
    audit_closeout(segment, services, events, checks, evidence)
    assert checks['historical_http_failures_disclosed'] and checks['historical_unknown_reservations_preserved']
    assert checks['all_http_attempts_accounted_conservatively'] and checks['final_suffix_http_recovered']
    assert not http_evidence(events)['failed_http_recoveries_verified']
    assert evidence['historical_network_failures_recovered'] is False
    assert checks['closeout_model_responded'] and checks['closeout_turn_completed']
    assert not checks['project_done'] and not checks['fresh_stages_verified_below_40']
    assert 'request_accounting_complete' not in checks
    store.close()
