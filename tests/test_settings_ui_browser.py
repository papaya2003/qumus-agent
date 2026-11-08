"""Simplified settings and consistent microscope symbols, using offline fixtures."""

import json
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
from qumus2.config import simulation_profile  # noqa: E402
from qumus2.ui.app import create_app  # noqa: E402

browser = shared_browser


def test_settings_cost_calibration_and_microscope_symbols(browser, tmp_path, monkeypatch):
    model = 'openai/gpt-6-astra'
    monkeypatch.setattr(ModelCatalog, 'list', lambda self: [
        {'id': model, 'input_price': None, 'output_price': None, 'vision': True}])
    s = build_services(tmp_path / 'qumus.db', seed=7, testbench_config=simulation_profile('integrated'))
    s.runtime = s.driver.runtime = OpenAIRuntime(s.gateway, OpenAICredentials(
        'offline-only', base_url='https://physics.bytefuture.ai/v1', model_id=model,
        input_price=20, output_price=100))
    usage = {'provider': 'openai', 'model': model, 'request_id': 'calibrated',
                 'gateway': 'https://physics.bytefuture.ai/v1', 'input_tokens': 900000,
                 'output_tokens': 100000, 'cost_usd': 28, 'token_measurement': 'provider_usage_v1'}
    s.store.emit('agent.usage', usage)
    s.store.emit('agent.request_usage', {**usage, 'measurement': 'provider_usage_v1', 'usage_status': 'reported'})
    (tmp_path / 'gateway-billing.json').write_text(json.dumps({
        'source_url': 'https://physics.bytefuture.ai/dashboard/usage',
        'verified_at': '2026-10-28T03:38:47Z', 'total_usd': 40,
        'period': 'Past 30 days', 'scope': 'Account',
        'by_model': [{'model': model, 'usd': 40, 'requests': 100, 'tokens': 10000000}]}))
    evidence = Path('runs/checks/settings-ui-evidence')
    evidence.mkdir(parents=True, exist_ok=True)
    with _Server(create_app(s, start_driver=False)) as url:
        ctx, page, errors = _page(browser)
        page.route('**/api/health', lambda route: route.fulfill(json={
            'mode': 'sdk', 'provider': 'openai', 'model': model, 'model_status': {'state': 'ok'}}))
        try:
            page.goto(url)
            for device in ('microscope', 'reactor_microscope'):
                symbol = page.locator(f'#bench .s-objective[data-device="{device}"]')
                expect(symbol.locator('circle')).to_have_count(2)
                expect(symbol.locator('.s-cross')).to_have_count(1)
            expect(page.locator('.s-reactor-window')).to_have_count(0)
            page.click('.ab[data-page="settings"]')
            expect(page.locator('#billing-settings')).to_contain_text('$4.00 estimated')
            expect(page.locator('#billing-settings')).to_contain_text('average billed cost per model')
            expect(page.locator('#model-context-limit')).to_have_value('1050000')
            expect(page.locator('#model-input-rate')).to_be_hidden()
            expect(page.locator('#conn-info')).to_be_hidden()
            expect(page.locator('#team-roles')).to_be_hidden()
            page.screenshot(path=str(evidence / 'settings-desktop.png'))
            page.get_by_text('Budget guard rates', exact=True).click()
            page.locator('#model-input-rate').fill('21')
            page.locator('#theme-btn').click()
            expect(page.locator('#model-input-rate')).to_have_value('21')
            assert s.store.spent_usd() == 28
            page.get_by_text('Budget guard rates', exact=True).click()
            page.screenshot(path=str(evidence / 'settings-dark.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('.ab[data-page="more"]').click()
            page.locator('#v-results .pback').click()
            page.locator('#phone-pages [data-page="settings"]').click()
            page.locator('#page-settings').screenshot(path=str(evidence / 'settings-mobile.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
        finally:
            ctx.close()
