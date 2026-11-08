"""Replay captions and both saved product observations on the accepted reactor run."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402
from test_ui_browser import _page, _Server  # noqa: E402
from test_ui_browser import browser as shared_browser  # noqa: E402

from qumus2.ui.replay import create_replay_app  # noqa: E402

browser = shared_browser
RECORDING = Path(__file__).resolve().parents[1] / "recordings/openai-integrated-reactor-20261027-v5"


def _end(page, url):
    page.goto(url)
    page.wait_for_selector(".rec-intro")
    page.locator("#rp-track").press("End")
    expect(page.locator("#conn-text")).to_have_text("Replay finished")


def test_reactor_final_record_action_has_no_device_placeholder(browser):
    with _Server(create_replay_app(RECORDING)) as url:
        ctx, page, errors = _page(browser)
        try:
            _end(page, url)
            caption = page.locator("#bc-text")
            expect(caption.locator(".bc-act")).to_have_text("Record reactor process")
            expect(caption.locator(".bc-check")).to_have_text("check ✓")
            expect(caption.locator(".bc-where")).to_contain_text("step 14 of 14")
            expect(caption.locator(".bc-dev")).to_have_count(0)
            assert caption.inner_text().startswith("Record reactor process")
            # Rebuilding the event prefix must not retain an earlier instrument.
            page.locator("#rp-track").press("Home")
            expect(caption.locator(".bc-act")).to_have_count(0)
            page.locator("#rp-track").press("End")
            expect(caption.locator(".bc-act")).to_have_text("Record reactor process")
            assert caption.inner_text().startswith("Record reactor process")
            assert not errors, errors
        finally:
            ctx.close()


@pytest.mark.parametrize("width", [1440, 1024])
def test_product_switches_between_original_saved_observations(browser, tmp_path, width):
    data = json.loads((RECORDING / "recording.json").read_text())
    product = next(e["payload"] for e in reversed(data["events"])
                   if e["kind"] == "sample.updated" and e["payload"].get("status") == "reactor_product")
    images = product["props"]["images"]
    assert {"reactor", "ordinary_microscope"} <= images.keys()
    with _Server(create_replay_app(RECORDING)) as url:
        ctx, page, errors = _page(browser, width=width)
        try:
            _end(page, url)
            page.click('.ab[data-page="results"]')
            page.click('#results-index button[data-key="reactor"]')
            page.locator(f'#res-body .fcard[data-id="{product["id"]}"]').click()
            detail = page.locator("#r-detail .fl-detail")
            expect(detail.locator(".fl-obj button")).to_have_text(["Reactor view", "Ordinary microscope"])

            def saved_image(key, alt):
                image = detail.locator(".fl-big img")
                expect(image).to_have_attribute("alt", f'{product["id"]}: {alt}')
                expect(image).to_have_js_property("complete", True)
                assert image.evaluate("el => el.naturalWidth") > 0
                source = image.get_attribute("src")
                response = page.request.get(urljoin(url, source))
                assert response.ok
                assert hashlib.sha256(response.body()).digest() == hashlib.sha256(
                    (RECORDING / images[key]).read_bytes()).digest()
                return source

            # The reactor acceptance image remains the initial view.
            expect(detail.locator(".fl-obj button.on")).to_have_text("Reactor view")
            reactor_source = saved_image("reactor", "reactor observation")
            detail.get_by_role("button", name="Ordinary microscope", exact=True).click()
            expect(detail.locator(".fl-obj button.on")).to_have_text("Ordinary microscope")
            ordinary_source = saved_image("ordinary_microscope", "ordinary microscope inspection")
            assert ordinary_source != reactor_source
            # Enlarge the same recorded image, then return to the acceptance view.
            detail.locator(".fl-big img").click()
            expect(page.locator("#lightbox img")).to_have_attribute("src", ordinary_source)
            page.keyboard.press("Escape")
            expect(page.locator("#lightbox")).to_be_hidden()
            page.screenshot(path=str(tmp_path / f"ordinary-microscope-{width}.png"))
            detail.get_by_role("button", name="Reactor view", exact=True).click()
            assert saved_image("reactor", "reactor observation") == reactor_source
            expect(detail).to_contain_text("Source flakes")
            expect(detail).to_contain_text("Evidence job")
            assert detail.locator(".fl-obj").evaluate(
                "el => el.getBoundingClientRect().left >= el.parentElement.getBoundingClientRect().left")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            assert not errors, errors
        finally:
            ctx.close()
