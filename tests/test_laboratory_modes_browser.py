"""Visible laboratory selection and truthful learning status in the existing UI."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from conftest import v2_recording
from playwright.sync_api import expect  # noqa: E402
from test_ui_browser import _page, _Server  # noqa: E402
from test_ui_browser import browser as shared_browser  # noqa: E402

from qumus2.ui.app import create_app  # noqa: E402
from qumus2.ui.replay import create_replay_app  # noqa: E402

browser = shared_browser


@pytest.mark.parametrize("replay", [False, True])
def test_laboratory_selector_and_learning_status(browser, services, replay):
    app = (create_replay_app(v2_recording("openai-collaboration-20261025")) if replay
           else create_app(services, start_driver=False))
    with _Server(app) as url:
        context, page, errors = _page(browser)
        try:
            page.goto(url)
            page.locator('#act [data-page="settings"]').click()
            selector = page.get_by_label("Laboratory mode", exact=True)
            if replay:
                expect(selector).to_be_disabled()
                expect(page.locator("#laboratory-status")).to_contain_text("Playback: learning is disabled")
            else:
                expect(selector).to_have_value("virtual")
                expect(selector.locator('option[value="real"]')).to_be_disabled()
                expect(page.locator("#laboratory-status")).to_contain_text("Learning is disabled")
                expect(page.locator("#laboratory-setup")).to_contain_text("physical device adapters")
            page.locator('#act [data-page="history"]').click()
            page.locator('#hist-tabs button[data-t="experience"]').click()
            expect(page.get_by_role("status", name="Experience learning status")).to_contain_text("learning disabled" if replay else "Learning disabled")
            assert not errors, errors
        finally:
            context.close()
