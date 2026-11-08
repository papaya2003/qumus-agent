"""Request-length views on genuine measured, old and scripted recordings."""

import json
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from conftest import v2_recording
from playwright.sync_api import expect  # noqa: E402
from test_ui_browser import _page, _Server  # noqa: E402
from test_ui_browser import browser as shared_browser  # noqa: E402

from qumus2.ui.replay import create_replay_app  # noqa: E402

browser = shared_browser


def test_context_cache_metrics_match_backend_and_render(browser):
    from qumus2.agents.token_usage import cache_usage_summary

    events = []
    for n, (inp, cached) in enumerate([(100, 80), (100, 0), (100, None), (100, 101)], 1):
        identity = dict(request_id=f'r{n}', call_id=f'c{n}', session_id='s',
                        model='test-model', gateway='https://example.test/v1', api='responses', attempt=1)
        events.append(dict(id=len(events)+1, kind='agent.request', agent='advisor', payload={
            **identity, 'token_measurement': 'provider_usage_v1'}))
        events.append(dict(id=len(events)+1, kind='agent.request_usage', agent='advisor', payload={
            **identity, 'measurement': 'provider_usage_v1', 'input_tokens': inp,
            'output_tokens': 10, 'cached_input_tokens': cached}))
    events.extend([events[0], events[1]])  # mirrored events are not extra requests/hits
    variants = [events[:1], events[:2], events, events + [{**events[1], 'payload': {
        **events[1]['payload'], 'cached_input_tokens': 1}}]]
    with _Server(create_replay_app(v2_recording('openai-collaboration-20261025'))) as url:
        ctx, page, errors = _page(browser)
        try:
            page.goto(url)
            for sequence in variants:
                got = page.evaluate("""async events => {
                    const m = await import('/static/requestchart.js');
                    return m.requestCacheSummary(events);
                }""", sequence)
                expected = cache_usage_summary(sequence)['totals']
                for key, value in got.items():
                    assert value == expected[key], (key, value, expected[key])
            conflicted = events + [{**events[0], 'agent': 'eyes'}]
            filtered = page.evaluate("""async events => {
                const m = await import('/static/requestchart.js');
                return m.requestCacheSummary(events, 'eyes');
            }""", conflicted)
            assert filtered['cache_hit_requests'] == 0
            assert filtered['unknown_cache_requests'] == 1
            page.evaluate("""async events => {
                const m = await import('/static/requestchart.js');
                const host = document.createElement('div');
                host.id = 'cache-test'; host.append(m.requestChart(events));
                document.body.append(host);
            }""", events)
            expect(page.get_by_role('region', name='Cache usage')).to_contain_text('40.0%')
            expect(page.get_by_role('region', name='Cache usage')).to_contain_text('1 / 2 (50.0%)')
            expect(page.get_by_role('region', name='Cache usage')).to_contain_text('invalid cache: 1')
            assert not errors, errors
        finally:
            ctx.close()


@pytest.mark.parametrize('mode', ['sdk', 'replay'])
def test_settings_waits_for_server_mode_before_loading(browser, tmp_path, monkeypatch, mode):
    from qumus2.agents.model_settings import ModelCatalog
    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.ui.app import create_app

    model = 'openai/gpt-6-astra'
    if mode == 'sdk':
        monkeypatch.setattr(ModelCatalog, 'list', lambda self: [{'id': model}])
        services = build_services(tmp_path / 'run.db', seed=1)
        services.runtime = OpenAIRuntime(services.gateway, OpenAICredentials(
            'offline-only', base_url='https://physics.bytefuture.ai/v1', model_id=model,
            input_price=1, output_price=2))
        services.driver.runtime = services.runtime
        app = create_app(services, start_driver=False)
    else:
        app = create_replay_app(v2_recording('openai-collaboration-20261025'))
    with _Server(app) as url:
        ctx, page, errors = _page(browser)
        page.add_init_script("localStorage.setItem('qumus.shell', JSON.stringify({scene: 'settings', side: true}));")
        pending, model_requests = [], []
        page.route('**/api/health', lambda route: pending.append(route))
        page.on('request', lambda request: model_requests.append(request.url)
                if request.url.endswith('/api/models') else None)
        try:
            # Hold health until Settings has rendered: this deterministically
            # reproduces the startup/reload race, regardless of machine speed.
            page.goto(url)
            page.wait_for_function("document.querySelector('#model-settings').textContent.length > 0")
            expect(page.locator('#model-context-limit')).to_have_count(0)
            assert not model_requests
            assert pending
            pending.pop(0).fulfill(json={'mode': mode, 'provider': 'openai', 'model': model,
                                        'model_status': {'state': 'ok'}})
            if mode == 'sdk':
                expect(page.locator('#model-context-limit')).to_have_value('1050000')
                expect(page.locator('#model-apply')).to_be_disabled()
                expect(page.locator('.model-context-source')).to_contain_text('gateway administrator')
                assert len(model_requests) == 1
                expect(page.locator('#model-context-limit')).to_have_attribute('readonly', 'true')
                expect(page.get_by_role('button', name='Use default limit', exact=True)).to_have_count(0)
                expect(page.get_by_role('button', name='Use for next task', exact=True)).to_have_count(0)
                page.locator('#theme-btn').click()
                expect(page.locator('#model-context-limit')).to_have_value('1050000')
                assert len(model_requests) == 1
            else:
                page.wait_for_selector('.rec-intro', state='attached')
                expect(page.locator('#model-settings')).to_contain_text('Open the normal Qumus app')
                expect(page.locator('#model-context-limit')).to_have_count(0)
                assert not model_requests
            assert not errors, errors
        finally:
            ctx.close()


def test_context_is_only_in_history_and_shows_complete_recording(browser):
    folder = v2_recording("openai-collaboration-20261027-v3")
    data = json.loads((folder / "recording.json").read_text())
    counts = [e["payload"]["input_tokens"] for e in data["events"] if e["kind"] == "agent.usage"]
    with _Server(create_replay_app(folder)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.click('.ab[data-page="results"]')
        expect(page.locator('#results-index [data-key="context"]')).to_have_count(0)
        expect(page.locator('#res-body .request-chart')).to_have_count(0)
        page.click('.ab[data-page="history"]')
        page.locator('#hist-index .hrow').filter(has=page.locator('.hr-now')).click()
        page.locator('#hist-body .hist-sub button:has-text("Context")').click()
        expect(page.locator("#hist-body .request-point")).to_have_count(len(counts))
        for n, title in enumerate(page.locator("#hist-body .request-point title").all_text_contents()):
            assert f"input {counts[n]:,}" in title
        expect(page.locator('#hist-body .request-detail')).to_have_count(0)
        expect(page.locator('#hist-body .request-table')).to_have_count(0)
        expect(page.locator('#r-detail .request-inspector')).to_contain_text('Context limit: Unknown')
        ctx.close()
        assert not errors, errors


@pytest.mark.parametrize("name,expected", [
    ("openai-collaboration-20261025", "historical response usage"),
    ("experimental-reactor-pair-normal-20261027-v3", "Scripted execution: no model requests are made"),
])
def test_complete_history_distinguishes_legacy_missing_data_from_scripted_execution(browser, name, expected):
    with _Server(create_replay_app(Path("recordings") / name)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.locator("#rp-track").press("End")
        page.click('.ab[data-page="history"]')
        page.locator("#hist-index .hrow").filter(has=page.locator(".hr-now")).click()
        page.locator('#hist-body .hist-sub button:has-text("Context")').click()
        expect(page.locator("#r-detail" if name.startswith("openai") else "#hist-body")).to_contain_text(expected)
        if name.startswith('openai'):
            assert page.locator('#hist-body .request-point').count() > 0
        else:
            expect(page.locator("#hist-body .request-point")).to_have_count(0)
        ctx.close()
        assert not errors, errors


def test_token_chart_switch_boundaries_unknowns_and_over_limit(browser, tmp_path):
    import shutil

    folder = tmp_path / 'tokens'
    shutil.copytree(v2_recording('openai-collaboration-20261025'), folder)
    path = folder / 'recording.json'
    data = json.loads(path.read_text())
    # This fixture is synthetic evidence, kept only in pytest's private directory.
    data['events'] = []
    for n, (model, limit, inp, out) in enumerate([
            ('model-a', 32000, 24000, 4000), ('model-b', 64000, 60000, 5000),
            ('model-c', None, 1000, 0), ('model-c', None, None, None)], 1):
        p = {'request_id': f'r{n}', 'call_id': f'c{n}', 'session_id': model, 'model': model,
             'context_window_tokens': limit, 'token_measurement': 'provider_usage_v1'}
        data['events'].append({'id': n * 2, 'ts': n * 2, 'wall': n * 2,
                               'kind': 'agent.request', 'agent': 'eyes', 'payload': p})
        if inp is not None:
            data['events'].append({'id': n * 2 + 1, 'ts': n * 2 + 1, 'wall': n * 2 + 1,
                'kind': 'agent.request_usage', 'agent': 'eyes',
                'payload': {**p, 'input_tokens': inp, 'output_tokens': out}})
    data['meta']['n_events'] = len(data['events'])
    path.write_text(json.dumps(data))
    with _Server(create_replay_app(folder)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector('.rec-intro')
        page.click('.ab[data-page="history"]')
        page.locator('#hist-index .hrow').filter(has=page.locator('.hr-now')).click()
        page.locator('#hist-body .hist-sub button:has-text("Context")').click()
        points = page.locator('#hist-body .request-point')
        expect(points).to_have_count(4)
        expect(page.locator('.request-model-boundary')).to_have_count(2)
        detail = page.locator('#r-detail .request-inspector')
        points.nth(0).click()
        expect(detail).to_contain_text('Remaining after input + output: 4,000 tokens')
        expect(detail).not_to_contain_text('null')
        points.nth(1).click()
        expect(detail).to_contain_text('Over the configured limit by 1,000 tokens')
        points.nth(2).click()
        expect(detail).to_contain_text('Output: 0')
        expect(detail).to_contain_text('Context limit: Unknown')
        points.nth(3).click()
        expect(detail).to_contain_text('Input: Unknown')
        expect(points.nth(3).locator('circle')).to_have_count(0)
        points.nth(0).click()
        expect(page.locator('#r-title')).to_have_text('Request 1 · Eyes')
        evidence = Path('runs/checks/context-token-evidence')
        evidence.mkdir(parents=True, exist_ok=True)
        page.locator('#hist-body .request-chart').screenshot(path=str(evidence / 'tokens-desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        points.nth(0).focus()
        expect(detail).to_contain_text('4,000 tokens')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(evidence / 'tokens-mobile.png'))
        ctx.close()
        assert not errors, errors


def test_model_limit_is_read_only_and_switch_preserves_unknown(browser, tmp_path, monkeypatch):
    from qumus2.agents.model_settings import ModelCatalog
    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.ui.app import create_app

    monkeypatch.setattr(ModelCatalog, 'list', lambda self: [{'id': 'a'}, {'id': 'b'}])
    services = build_services(tmp_path / 'run.db', seed=1)
    services.runtime = OpenAIRuntime(services.gateway, OpenAICredentials(
        'test-no-calls', base_url='https://gateway.example/v1', model_id='a',
        input_price=1, output_price=2, context_window_tokens=32000))
    services.driver.runtime = services.runtime
    with _Server(create_app(services, start_driver=False)) as url:
        ctx, page, errors = _page(browser)
        # No health or inference request leaves this local fixture.
        page.route('**/api/health', lambda route: route.fulfill(json={
            'mode': 'sdk', 'provider': 'openai', 'model': 'a', 'model_status': {'state': 'ok'}}))
        page.goto(url)
        page.click('.ab[data-page="settings"]')
        choice = page.locator('#model-choice')
        limit = page.locator('#model-context-limit')
        expect(limit).to_have_value('32000')
        choice.select_option('b')
        expect(limit).to_have_value('')
        page.locator('#model-input-rate').fill('1')
        page.locator('#model-output-rate').fill('2')
        page.get_by_role('button', name='Save settings', exact=True).click()
        expect(page.locator('.model-status')).to_contain_text('Saved.')
        assert services.runtime.creds.context_window_tokens is None
        choice.select_option('a')
        expect(limit).to_have_value('32000')
        choice.select_option('b')
        expect(limit).to_have_value('')
        expect(limit).to_have_attribute('readonly', 'true')
        evidence = Path('runs/checks/context-token-evidence')
        evidence.mkdir(parents=True, exist_ok=True)
        page.locator('#model-settings').screenshot(path=str(evidence / 'model-settings.png'))
        ctx.close()
        assert not errors, errors


def test_administrator_limits_match_on_switch_and_show_used_remaining(browser, tmp_path, monkeypatch):
    from qumus2.agents.model_settings import ModelCatalog
    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.ui.app import create_app

    astra, codex, qwen = 'openai/gpt-6-astra', 'openai-codex/gpt-6-astra', 'olares-qumusphusis/qwen-3.8-27b'
    monkeypatch.setattr(ModelCatalog, 'list', lambda self: [
        {'id': model, 'input_price': 1, 'output_price': 2} for model in (astra, codex, qwen, 'unknown/model')])
    services = build_services(tmp_path / 'run.db', seed=1)
    runtime = OpenAIRuntime(services.gateway, OpenAICredentials(
        'offline-only', base_url='https://physics.bytefuture.ai/v1', model_id=qwen, input_price=1, output_price=2))
    services.runtime = services.driver.runtime = runtime
    # Synthetic provider reports exercise the live view; no inference is needed.
    for n, model in enumerate((astra, codex, qwen), 1):
        settings = runtime.model_settings()
        payload = {'request_id': f'r{n}', 'call_id': f'c{n}', 'model': model,
                   'session_id': f's{n}', 'token_measurement': 'provider_usage_v1',
                   'context_window_tokens': settings['context_limits'][model],
                   'context_limit_source': settings['context_limit_sources'][model]}
        services.store.emit('agent.request', payload, agent='orchestrator')
        services.store.emit('agent.request_usage', {**payload, 'input_tokens': 80000, 'output_tokens': 1000},
                            agent='orchestrator')
    with _Server(create_app(services, start_driver=False)) as url:
        ctx, page, errors = _page(browser)
        page.route('**/api/health', lambda route: route.fulfill(json={
            'mode': 'sdk', 'provider': 'openai', 'model': qwen, 'model_status': {'state': 'ok'}}))
        page.goto(url)
        page.click('.ab[data-page="settings"]')
        choice, limit = page.locator('#model-choice'), page.locator('#model-context-limit')
        source = page.locator('.model-context-source')
        expect(limit).to_have_value('131072')
        for model, value in [(astra, '1050000'), (codex, '872000'), (qwen, '131072')]:
            choice.select_option(model)
            expect(limit).to_have_value(value)
            expect(source).to_contain_text('gateway administrator')
        choice.select_option('unknown/model')
        expect(limit).to_have_value('')
        expect(source).to_contain_text('No exact administrator ID match')
        choice.select_option(astra)
        page.get_by_role('button', name='Save settings', exact=True).click()
        expect(page.locator('.model-status')).to_contain_text('Saved.')
        choice.select_option(qwen)
        page.get_by_role('button', name='Save settings', exact=True).click()
        expect(page.locator('.model-status')).to_contain_text('Saved.')
        assert runtime.creds.context_window_tokens == 131072
        page.reload()
        page.click('.ab[data-page="progress"]')
        page.click('.ab[data-page="settings"]')
        expect(limit).to_have_value('131072')
        expect(source).to_contain_text('gateway administrator')
        assert runtime.model_settings()['context_limit_source'] == 'administrator_table'
        evidence = Path('runs/checks/administrator-context-evidence')
        evidence.mkdir(parents=True, exist_ok=True)
        page.locator('#model-settings').screenshot(path=str(evidence / 'automatic-model-limit.png'))
        page.click('.ab[data-page="history"]')
        page.locator('#hist-index .hrow[data-kind="current"]').click()
        page.locator('#hist-body .hist-sub button:has-text("Context")').click()
        points, detail = page.locator('#hist-body .request-point'), page.locator('#r-detail .request-inspector')
        expect(points).to_have_count(3)
        for index, capacity, remaining in [(0, '1,050,000', '969,000'), (1, '872,000', '791,000'),
                                           (2, '131,072', '50,072')]:
            points.nth(index).click()
            expect(detail).to_contain_text('Used (input + output): 81,000 tokens')
            expect(detail).to_contain_text(f'Administrator table context limit: {capacity} tokens')
            expect(detail).to_contain_text(f'Remaining after input + output: {remaining} tokens')
            expect(page.locator('#r-title')).to_have_text(f'Request {index + 1} · Qumus')
        page.locator('#hist-body .request-chart').screenshot(path=str(evidence / 'used-remaining.png'))
        page.locator('#hist-body .request-chart').evaluate('(el) => { window.contextChartNode = el; }')
        services.store.emit('lab.note', {'text': 'Unrelated status update'})
        page.wait_for_function("document.querySelector('#hist-body .request-chart') === window.contextChartNode")
        extra = {**payload, 'request_id': 'r4', 'call_id': 'c4'}
        services.store.emit('agent.request', extra, agent='eyes')
        services.store.emit('agent.request_usage', {**extra, 'input_tokens': 1000, 'output_tokens': 100}, agent='eyes')
        expect(points).to_have_count(4)
        points.nth(3).click()
        expect(detail).to_contain_text('Input: 1,000')
        expect(page.locator('#r-title')).to_have_text('Request 4 · Eyes')
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(detail).to_contain_text('1,100 tokens')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        ctx.close()
        assert not errors, errors


def test_actual_scale_agent_lanes_filter_and_request_inspector(browser, tmp_path):
    """A dense small-usage chart stays legible, with exact agent attribution in both views."""
    import shutil

    from test_ui_browser import _to_the_end

    folder = tmp_path / 'multi-agent-tokens'
    shutil.copytree(v2_recording('openai-collaboration-20261025'), folder)
    path = folder / 'recording.json'
    data = json.loads(path.read_text())
    data['events'] = []
    agents = ['orchestrator', 'advisor', 'eyes', 'hands', 'picker', 'reactor']
    for n in range(1, 64):
        agent = agents[(n - 1) % len(agents)]
        p = {'request_id': f'r{n}', 'call_id': f'c{n}', 'session_id': f's-{agent}',
             'model': 'openai/gpt-6-astra', 'context_window_tokens': 1050000,
             'context_limit_source': 'administrator_table', 'token_measurement': 'provider_usage_v1',
             'attempt': 2 if n == 62 else 1}
        data['events'].append({'id': n * 2, 'ts': n * 2, 'wall': n * 2,
                               'kind': 'agent.request', 'agent': agent, 'payload': p})
        if n not in {8, 24, 32, 48, 60}:
            inp, out = (32000, 616) if n == 61 else (4000 + n * 73, 189 + n * 2)
            data['events'].append({'id': n * 2 + 1, 'ts': n * 2 + 1, 'wall': n * 2 + 1,
                'kind': 'agent.request_usage', 'agent': agent,
                'payload': {**p, 'input_tokens': inp, 'output_tokens': out}})
    data['meta']['n_events'] = len(data['events'])
    path.write_text(json.dumps(data))
    evidence = Path('runs/checks/token-chart-redesign-evidence')
    evidence.mkdir(parents=True, exist_ok=True)
    with _Server(create_replay_app(folder)) as url:
        ctx, page, errors = _page(browser)
        page.set_viewport_size({'width': 1600, 'height': 1000})
        page.goto(url)
        page.wait_for_selector('.rec-intro')
        _to_the_end(page)
        page.click('.ab[data-page="history"]')
        page.locator('#hist-index .hrow').filter(has=page.locator('.hr-now')).click()
        page.locator('#hist-body .hist-sub button:has-text("Context")').click()
        chart = page.locator('#hist-body .request-chart')
        plot = chart.locator('svg.request-series')
        expect(chart.locator('.request-point')).to_have_count(63)
        expect(chart.locator('.request-agent-label')).to_have_count(6)
        expect(plot).to_have_attribute('data-max-tokens', '40000')
        assert float(chart.locator('[data-number="61"] .token-total').get_attribute('height')) > 100
        expect(page.locator('#r-title')).to_have_text('Request 63 · Eyes')
        chart.get_by_role('button', name='Full capacity', exact=True).click()
        expect(plot).to_have_attribute('data-max-tokens', '1200000')
        expect(plot).to_have_attribute('data-zooming', 'true')
        page.wait_for_function("!document.querySelector('.request-chart svg.request-series').dataset.zooming")
        assert float(chart.locator('[data-number="61"] .token-total').get_attribute('height')) < 10
        expect(chart.locator('.token-limit')).to_have_count(64)  # 63 recorded limits and legend
        chart.get_by_role('button', name='Actual usage', exact=True).click()
        expect(plot).to_have_attribute('data-max-tokens', '40000')
        page.wait_for_function("!document.querySelector('.request-chart svg.request-series').dataset.zooming")
        assert float(chart.locator('[data-number="61"] .token-total').get_attribute('height')) > 100
        expect(chart.locator('svg .token-limit')).to_have_count(0)
        chart.locator('.request-agent-point[data-request-number="62"]').click()
        expect(page.locator('#r-title')).to_have_text('Request 62 · Advisor')
        expect(page.locator('#r-detail .request-inspector')).to_contain_text('Retry 1')
        expect(page.locator('#r-detail .request-facts')).to_contain_text('r62')
        expect(chart.locator('.request-table')).to_have_count(0)
        expect(chart.locator('.request-detail')).to_have_count(0)
        assert chart.locator('.request-chart-scroll').evaluate('(el) => el.scrollWidth > el.clientWidth')
        assert float(chart.locator('[data-number="61"] .token-total').get_attribute('width')) == 14
        expect(chart.locator('.token-input, .token-output')).to_have_count(0)
        navigator = chart.get_by_role('slider', name='Scroll requests')
        navigator.fill('0')
        assert chart.locator('.request-chart-scroll').evaluate('(e) => e.scrollLeft') == 0
        navigator.press('End')
        assert chart.locator('.request-chart-scroll').evaluate('(e) => Math.abs(e.scrollWidth - e.clientWidth - e.scrollLeft) < 2')

        axis_before = chart.locator('.request-fixed-axis').bounding_box()
        chart.locator('.request-chart-scroll').evaluate('(el) => { el.scrollLeft = el.scrollWidth; }')
        assert chart.locator('.request-fixed-axis').bounding_box()['x'] == axis_before['x']
        chart.locator('.request-point[data-number="48"]').click()
        expect(page.locator('#r-title')).to_have_text('Request 48 · Reactor')
        expect(page.locator('#r-detail .request-inspector')).to_contain_text('Input: Unknown')
        expect(page.locator('#r-detail [role="meter"]')).to_have_count(0)
        chart.get_by_label('Filter requests by agent').select_option('hands')
        expect(chart.locator('.request-point')).to_have_count(10)
        expect(chart.locator('.request-agent-label')).to_have_text('Hands')
        assert chart.locator('.request-point').evaluate_all('(xs) => xs.map(x => +x.dataset.number)') == list(range(4, 64, 6))
        expect(page.locator('#r-title')).to_have_text('Request 58 · Hands')
        chart.locator('.request-point[data-number="4"]').click()
        expect(page.locator('#r-title')).to_have_text('Request 4 · Hands')
        expect(page.locator('#r-detail .request-facts')).to_contain_text('s-hands')
        expect(page.locator('#r-detail .request-inspector')).to_contain_text('1,045,511 tokens')
        chart.get_by_label('Filter requests by agent').select_option('')
        expect(chart.locator('.request-point')).to_have_count(63)
        chart.locator('.request-point[data-number="61"]').focus()
        page.keyboard.press('Enter')
        expect(page.locator('#r-title')).to_have_text('Request 61 · Qumus')
        expect(page.locator('#r-detail .request-inspector')).to_contain_text('3.1% used')
        page.locator('#hist-body').evaluate('(el) => el.scrollTop = 0')
        page.screenshot(path=str(evidence / 'usage-agents-light.png'))
        scroll_before = chart.locator('.request-chart-scroll').evaluate('(el) => el.scrollLeft')
        assert scroll_before > 0
        page.click('.ab[data-page="settings"]')
        page.click('#theme-btn')
        page.click('.ab[data-page="history"]')
        expect(chart.locator('.request-chart-scroll')).to_be_visible()
        page.wait_for_function("([x]) => Math.abs(document.querySelector('.request-chart-scroll').scrollLeft - x) < 1", arg=[scroll_before])
        expect(chart.locator('.request-point.selected')).to_have_attribute('data-number', '61')
        page.locator('#hist-body').evaluate('(el) => el.scrollTop = 0')
        page.screenshot(path=str(evidence / 'usage-agents-dark.png'))
        history = page.locator('#hist-body .request-chart')
        expect(history.locator('.request-point')).to_have_count(63)
        history.get_by_label('Filter requests by agent').select_option('reactor')
        history.locator('.request-point[data-number="6"]').click()
        expect(page.locator('#r-title')).to_have_text('Request 6 · Reactor')
        expect(page.locator('#r-detail .request-facts')).to_contain_text('r6')
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.locator('#r-detail .request-inspector')).to_be_visible()
        page.screenshot(path=str(evidence / 'request-inspector-mobile.png'))
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        # Back to the centre on a phone; retain the filter and request selection.
        page.locator('button[data-back="centre"]:visible').click()
        expect(history.locator('.request-point.selected')).to_have_attribute('data-number', '6')
        page.screenshot(path=str(evidence / 'usage-agents-mobile.png'))
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        ctx.close()
        assert not errors, errors


def test_model_settings_shows_specialized_models_without_claiming_chat_support(browser, tmp_path, monkeypatch):
    from qumus2.agents.model_settings import ModelCatalog
    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.ui.app import create_app

    models = [{'id': 'chat/model', 'conversational': True, 'vision': True,
               'input_price': 1, 'output_price': 2},
              {'id': 'image/model', 'conversational': False, 'modalities': ['image']},
              {'id': 'video/model', 'conversational': False, 'modalities': ['video']},
              {'id': 'decision/model', 'conversational': False, 'modalities': ['decision']}]
    monkeypatch.setattr(ModelCatalog, 'list', lambda self: models)
    services = build_services(tmp_path / 'run.db', seed=1)
    services.runtime = OpenAIRuntime(services.gateway, OpenAICredentials(
        'offline-only', model_id='chat/model', input_price=1, output_price=2))
    services.driver.runtime = services.runtime
    with _Server(create_app(services, start_driver=False)) as url:
        ctx, page, errors = _page(browser)
        try:
            page.goto(url)
            page.click('.ab[data-page="settings"]')
            choice = page.locator('#model-choice')
            expect(choice.locator('option')).to_have_count(4)
            expect(page.locator('.model-catalog-count')).to_contain_text('4 catalog IDs')
            for name in ['image/model', 'video/model', 'decision/model']:
                choice.select_option(name)
                expect(choice).to_have_value(name)
                expect(page.locator('#model-apply')).to_be_disabled()
                expect(page.locator('#model-settings')).to_contain_text('specialized API')
            choice.select_option('chat/model')
            expect(page.locator('#model-apply')).to_be_disabled()
            assert services.runtime.creds.model_id == 'chat/model'
            assert not errors, errors
        finally:
            ctx.close()
