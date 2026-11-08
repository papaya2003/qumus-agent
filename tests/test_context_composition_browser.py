"""Detailed estimates stay in the inspector, separate from reported capacity."""

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip('playwright.sync_api')
from playwright.sync_api import expect  # noqa: E402
from test_ui_browser import _page, _Server  # noqa: E402
from test_ui_browser import browser as shared_browser  # noqa: E402

from qumus2.agents.context_composition import input_composition  # noqa: E402
from qumus2.ui.replay import create_replay_app  # noqa: E402

browser = shared_browser


def test_inspector_fine_categories_estimates_missing_history_and_media(browser, tmp_path):
    folder = tmp_path / 'composition'
    shutil.copytree(Path('recordings/openai-collaboration-20261025'), folder)
    path = folder / 'recording.json'
    data = json.loads(path.read_text())
    sections = [{'category': category, 'text': text} for category, text in [
        ('system_instructions', 'Private agent instructions.'),
        ('reference_skills', 'Private reference skill.'),
        ('lab_snapshot', 'Private current plan and job evidence.')]]
    payload = {'instructions': ''.join(s['text'] for s in sections),
        'tools': [{'type': 'function', 'name': 'look', 'description': 'Inspect current lab state.'}],
        'input': [{'role': 'user', 'content': 'Earlier task.'},
                  {'role': 'assistant', 'content': 'Earlier model answer.'},
                  {'role': 'user', 'content': 'Current task.'},
                  {'type': 'function_call', 'name': 'look', 'arguments': '{"region":"lab"}'},
                  {'type': 'function_call_output', 'output': [
                      {'type': 'input_text', 'text': 'Private returned lab record.'},
                      {'type': 'input_image', 'image_url': 'data:image/png;base64,PRIVATE'}]}]}
    composition = input_composition(payload, 'responses', {
        'instruction_sections': sections, 'current_task': 'Current task.'})
    data['meta'].update(title='Synthetic context acceptance', description='Offline fixture; no model calls. Category values are synthetic.', agents='scripted', provider='scripted', model='synthetic-models', cost_usd=0, acceptance_checks={})
    data['events'] = []
    for n in range(1, 5):
        p = {'request_id': f'r{n}', 'call_id': f'c{n}', 'session_id': 's1',
             'model': 'model-a' if n != 4 else 'model-b', 'api': 'responses',
             'token_measurement': 'provider_usage_v1', 'attempt': 1,
             'context_window_tokens': 10000 if n != 4 else None}
        if n != 3:
            p['input_composition'] = composition
        data['events'].append({'id': 2 * n, 'ts': 2 * n, 'wall': 2 * n,
                               'kind': 'agent.request', 'agent': 'eyes', 'payload': p})
        if n != 2:
            data['events'].append({'id': 2 * n + 1, 'ts': 2 * n + 1, 'wall': 2 * n + 1,
                'kind': 'agent.request_usage', 'agent': 'eyes', 'payload': {
                    **{k: v for k, v in p.items() if k != 'input_composition'},
                    'input_tokens': 5000, 'output_tokens': 1000, 'reasoning_output_tokens': 100}})
    data['meta']['n_events'] = len(data['events'])
    path.write_text(json.dumps(data))
    evidence = Path('runs/checks/context-composition-evidence')
    evidence.mkdir(parents=True, exist_ok=True)
    with _Server(create_replay_app(folder)) as url:
        ctx, page, errors = _page(browser, width=1600, height=1100)
        try:
            page.goto(url)
            page.wait_for_selector('.rec-intro')
            page.click('.ab[data-page="history"]')
            page.locator('#hist-index .hrow').filter(has=page.locator('.hr-now')).click()
            page.locator('#hist-body .hist-sub button:has-text("Context")').click()
            chart = page.locator('#hist-body .request-chart')
            inspector = page.locator('#r-detail .request-inspector')
            chart.get_by_role('button', name='Full capacity', exact=True).click()
            expect(chart.locator('line.token-limit')).to_have_count(1)
            assert chart.locator('line.token-limit').evaluate('(e) => getComputedStyle(e).strokeDasharray') == '4px, 4px'
            chart.get_by_role('button', name='Actual usage', exact=True).click()
            chart.locator('[data-number="1"]').click()
            expect(inspector.locator('.composition-part')).to_have_count(10)
            expect(chart.locator('.context-composition')).to_have_count(0)
            expect(inspector).to_contain_text('Input: 5,000')
            expect(inspector).to_contain_text('Used (input + output): 6,000 tokens')
            expect(inspector).to_contain_text('Remaining after input + output: 4,000 tokens')
            expect(inspector).to_contain_text('60.0% used')
            expect(inspector).to_contain_text('Local rough estimate')
            expect(inspector).to_contain_text('of estimated text')
            expect(inspector).to_contain_text('Reasoning output: 100 tokens')
            media = inspector.locator('[data-category="media"]')
            expect(media.locator('.composition-value')).to_have_text('Unknown')
            media.locator('summary').click()
            expect(media).to_contain_text('1 attachment')
            expect(inspector.locator('[data-category="protocol_unassigned"] .composition-value')).to_have_text('Unknown')
            inspector.locator('.composition-method > summary').click()
            expect(inspector.locator('.composition-method')).to_contain_text('UTF-8 bytes ÷ 4')
            expect(inspector.locator('.composition-method')).to_contain_text(
                f'≈ {composition["text_estimated_tokens"]}')
            assert composition['text_estimated_tokens'] != 5000
            expect(inspector).not_to_contain_text('Private agent instructions')
            page.screenshot(path=str(evidence / 'composition-desktop.png'))
            inspector.locator('.context-composition').scroll_into_view_if_needed()
            page.screenshot(path=str(evidence / 'composition-details.png'))
            chart.locator('[data-number="2"]').click()
            expect(inspector).to_contain_text('Input: Unknown')
            expect(inspector.locator('[role="meter"]')).to_have_count(0)
            expect(inspector.locator('.composition-part')).to_have_count(10)
            chart.locator('[data-number="3"]').click()
            expect(inspector).to_contain_text('Composition was not recorded')
            expect(inspector.locator('.composition-part')).to_have_count(0)
            chart.locator('[data-number="4"]').click()
            expect(inspector).to_contain_text('Context limit: Unknown')
            expect(inspector.locator('[role="meter"]')).to_have_count(0)
            page.set_viewport_size({'width': 390, 'height': 844})
            expect(inspector).to_be_visible()
            expect(inspector.locator('.composition-part')).to_have_count(10)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(evidence / 'composition-mobile.png'))
            assert not errors, errors
        finally:
            ctx.close()
