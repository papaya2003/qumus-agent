"""Whole-page Settings edits one default and six specialist routes atomically."""
from pathlib import Path

import pytest

pytest.importorskip('playwright.sync_api')
from playwright.sync_api import expect  # noqa: E402
from test_ui_browser import _page, _Server  # noqa: E402
from test_ui_browser import browser as shared_browser  # noqa: E402

from qumus2.agents.model_settings import ModelCatalog  # noqa: E402
from qumus2.agents.openai_credentials import OpenAICredentials  # noqa: E402
from qumus2.agents.openai_runtime import OpenAIRuntime  # noqa: E402
from qumus2.app import build_services  # noqa: E402
from qumus2.ui.app import create_app  # noqa: E402

browser = shared_browser
DEFAULT, SPECIALIST = 'openai/gpt-6-astra', 'openai-codex/gpt-6-astra'


@pytest.mark.parametrize('width,height', [(1440, 1000), (1024, 900), (390, 844)])
def test_full_page_team_assignment_save_discard_and_reload(browser, tmp_path, monkeypatch, width, height):
    models = [
        {'id': DEFAULT, 'input_price': 1, 'output_price': 2, 'vision': True},
        {'id': SPECIALIST, 'input_price': 3, 'output_price': 6, 'vision': True},
        {'id': 'text/model', 'input_price': .2, 'output_price': .4, 'vision': False},
        {'id': 'image/model', 'conversational': False, 'modalities': ['image']}]
    monkeypatch.setattr(ModelCatalog, 'list', lambda self: models)
    s = build_services(tmp_path / 'settings.db')
    rt = s.runtime = s.driver.runtime = OpenAIRuntime(s.gateway, OpenAICredentials(
        'offline-only', base_url='https://physics.bytefuture.ai/v1', model_id=DEFAULT, input_price=1, output_price=2))
    evidence = Path('runs/checks/agent-model-settings-evidence')
    evidence.mkdir(parents=True, exist_ok=True)
    with _Server(create_app(s, start_driver=False)) as url:
        ctx, page, errors = _page(browser, width=width, height=height)
        page.add_init_script("localStorage.setItem('qumus.shell', JSON.stringify({scene:'settings', side:false}));")
        page.route('**/api/health', lambda route: route.fulfill(json={
            'mode': 'sdk', 'model': rt.creds.model_id, 'provider': 'openai', 'model_status': {'state': 'ok'},
            'models': sorted({c['model'] for c in rt.model_settings()['effective_models'].values()})}))
        try:
            page.goto(url)
            expect(page.locator('.routing-row')).to_have_count(6)
            expect(page.locator('#model-apply')).to_be_disabled()
            expect(page.locator('#ed')).to_be_hidden()
            expect(page.locator('#lab')).to_be_hidden()
            assert page.locator('#side').bounding_box()['width'] > width * .8
            advisor = page.locator('#model-agent-advisor')
            advisor.select_option(SPECIALIST)
            expect(page.locator('[data-agent-model="advisor"]')).to_contain_text('872,000 tokens')
            expect(page.locator('[data-agent-model="advisor"] .routing-badge')).to_have_text('Custom model')
            expect(page.locator('[data-agent-model="picker"] .routing-badge')).to_have_text('Default')
            assert rt.agent_model('advisor')['model'] == DEFAULT
            page.get_by_role('button', name='Discard changes', exact=True).click()
            expect(advisor).to_have_value('')
            expect(page.locator('#model-apply')).to_be_disabled()
            advisor.select_option(SPECIALIST)
            page.locator('#model-agent-eyes').select_option('text/model')
            expect(page.locator('[data-agent-model="eyes"]')).to_contain_text('Text only')
            expect(page.locator('[data-agent-model="eyes"]')).to_contain_text('Context unknown')
            expect(page.locator('#model-agent-eyes option[value="image/model"]')).to_be_disabled()
            expect(page.locator('.routing-summary')).to_contain_text('2 specialists use a custom model')
            page.locator('#page-settings h1').scroll_into_view_if_needed()
            page.screenshot(path=str(evidence / f'settings-{width}.png'))
            page.get_by_role('button', name='Save settings', exact=True).click()
            expect(page.locator('.model-status')).to_contain_text('Saved.')
            if width > 860:
                expect(page.locator('#mode-t')).to_contain_text('3 team models')
            assert rt.agent_model('advisor')['model'] == SPECIALIST
            assert rt.agent_model('eyes')['model'] == 'text/model'
            assert rt.agent_model('hands')['model'] == DEFAULT
            page.reload()
            expect(advisor).to_have_value(SPECIALIST)
            expect(page.locator('#model-agent-eyes')).to_have_value('text/model')
            expect(page.locator('#model-apply')).to_be_disabled()
            page.get_by_role('button', name='Use default for everyone', exact=True).click()
            expect(page.locator('.routing-summary')).to_contain_text('All 6 specialists')
            # Draft reset does not silently change the active saved team.
            assert rt.agent_model('advisor')['model'] == SPECIALIST
            page.get_by_role('button', name='Discard changes', exact=True).click()
            page.locator('#model-choice').select_option('text/model')
            expect(page.locator('[data-agent-model="hands"] .routing-effective')).to_contain_text('text/model')
            expect(advisor).to_have_value(SPECIALIST)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
        finally:
            ctx.close()
