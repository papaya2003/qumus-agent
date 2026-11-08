"""Navigation chosen while the page boots must survive initialization."""
from fnmatch import fnmatch
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
from qumus2.ui.replay import create_replay_app  # noqa: E402

browser = shared_browser


@pytest.fixture
def offline_live_app(tmp_path, monkeypatch):
    model = 'openai/gpt-6-astra'
    monkeypatch.setattr(ModelCatalog, 'list', lambda self: [{'id': model}])
    services = build_services(tmp_path / 'run.db', seed=1)
    services.runtime = OpenAIRuntime(services.gateway, OpenAICredentials(
        'offline-only', base_url='https://physics.bytefuture.ai/v1', model_id=model,
        input_price=1, output_price=2))
    services.driver.runtime = services.runtime
    return create_app(services, start_driver=False)


@pytest.mark.parametrize('phase,path', [
    ('module', '**/static/app.js'), ('health', '**/api/health'),
    ('snapshot', '**/api/snapshot'), ('events', '**/api/events?*'),
])
def test_reload_keeps_settings_chosen_before_boot_finishes(browser, offline_live_app, phase, path):
    with _Server(offline_live_app) as url:
        ctx, page, errors = _page(browser)
        pending, held = [], []

        def hold_first(route):
            if held:
                route.continue_()
            else:
                held.append(True)
                pending.append(route)

        try:
            with page.expect_websocket():
                page.goto(url)
            expect(page.locator('#app')).to_have_attribute('data-scene', 'progress')
            page.route(path, hold_first)
            with page.expect_request(lambda request: fnmatch(request.url, path)):
                page.reload(wait_until='commit')
            # The static button may be visible before its module has executed.
            expect(page.locator('.ab[data-page="settings"]')).to_be_visible()
            if phase != 'module':
                page.wait_for_function("document.querySelector('#app').dataset.scene === 'progress'")
            page.click('.ab[data-page="settings"]')
            assert pending, phase
            with page.expect_websocket() as connected:
                pending.pop().continue_()
            assert '/ws/events' in connected.value.url
            expect(page.locator('#app')).to_have_attribute('data-scene', 'settings')
            expect(page.locator('#page-settings')).to_be_visible()
            expect(page.locator('#model-context-limit')).to_have_value('1050000')
            if phase == 'module':
                # The bootstrap listener is gone: navigation runs once, and
                # the full-page Settings view remains open on a second click.
                page.click('.ab[data-page="progress"]')
                expect(page.locator('#app')).to_have_attribute('data-scene', 'progress')
                page.click('.ab[data-page="settings"]')
                expect(page.locator('#page-settings')).to_be_visible()
                page.click('.ab[data-page="settings"]')
                assert 'no-side' not in page.locator('#app').get_attribute('class')
                expect(page.locator('#page-settings')).to_be_visible()
                expect(page.locator('#ed')).to_be_hidden()
                page.click('.ab[data-page="settings"]')
                assert 'no-side' not in page.locator('#app').get_attribute('class')
                assert page.evaluate('document.documentElement.dataset.startupPage === undefined')
            assert not errors, errors
        finally:
            ctx.close()


@pytest.mark.parametrize('saved_scene,choices', [
    ('progress', ['history', 'settings']), ('settings', ['settings']),
    ('progress', ['settings', 'history']),
])
def test_latest_early_choice_opens_even_when_already_saved(browser, offline_live_app, saved_scene, choices):
    with _Server(offline_live_app) as url:
        ctx, page, errors = _page(browser)
        pending = []
        page.add_init_script(f"localStorage.setItem('qumus.shell', JSON.stringify({{scene: '{saved_scene}', side: true}}));")
        page.route('**/static/app.js', lambda route: pending.append(route))
        try:
            with page.expect_request('**/static/app.js'):
                page.goto(url, wait_until='commit')
            for choice in choices:
                page.click(f'.ab[data-page="{choice}"]')
            with page.expect_websocket():
                pending.pop().continue_()
            expect(page.locator('#app')).to_have_attribute('data-scene', choices[-1])
            expect(page.locator(f'#page-{choices[-1]}')).to_be_visible()
            assert 'no-side' not in page.locator('#app').get_attribute('class')
            assert not errors, errors
        finally:
            ctx.close()


def test_explicit_replay_start_retains_its_progress_route(browser):
    with _Server(create_replay_app(Path('recordings/openai-collaboration-20261025'))) as url:
        ctx, page, errors = _page(browser)
        page.add_init_script("localStorage.setItem('qumus.shell', JSON.stringify({scene: 'settings', side: true}));")
        try:
            page.goto(url + '?at=0')
            page.wait_for_function("document.body.classList.contains('replay') && !location.search.includes('at=')")
            expect(page.locator('#app')).to_have_attribute('data-scene', 'progress')
            expect(page.locator('#replay-bar')).to_be_visible()
            assert not errors, errors
        finally:
            ctx.close()
