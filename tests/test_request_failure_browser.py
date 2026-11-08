"""Attempt diagnostics and recovery links remain accurate in replay and History."""

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402
from test_ui_browser import _page, _Server, _to_the_end  # noqa: E402
from test_ui_browser import browser as shared_browser  # noqa: E402

from qumus2.ui.replay import create_replay_app  # noqa: E402

browser = shared_browser


def test_failed_attempt_retry_recovery_and_unknown_reason(browser, tmp_path):
    folder = tmp_path / "requests"
    shutil.copytree(Path("recordings/openai-collaboration-20261025"), folder)
    path = folder / "recording.json"
    data = json.loads(path.read_text())
    data["events"] = []

    def emit(kind, payload, agent="hands"):
        n = len(data["events"]) + 1
        data["events"].append(
            {"id": n, "kind": kind, "payload": payload, "agent": agent, "ts": n, "wall": n}
        )

    for n in range(1, 6):
        p = {
            "request_id": f"r{n}",
            "call_id": "retry-call" if n < 4 else "old-call",
            "session_id": "s1",
            "model": "model-a",
            "context_window_tokens": 1050000,
            "context_limit_source": "administrator_table",
            "token_measurement": "provider_usage_v1",
            "attempt": n if n < 4 else n - 3,
        }
        emit("agent.request", p)
        if n == 1:
            emit(
                "agent.request_failure",
                {
                    **p,
                    "failure_kind": "server_error",
                    "http_status": 503,
                    "provider_request_id": "gateway-r1",
                },
            )
        if n == 2:
            emit(
                "agent.request_failure",
                {**p, "failure_kind": "timeout", "exception_type": "ReadTimeout"},
            )
        if n in {3, 5}:
            emit("agent.request_usage", {**p, "input_tokens": 9356, "output_tokens": 189})
            emit("agent.usage", {**p, "input_tokens": 9356, "output_tokens": 189})
    data["meta"]["n_events"] = len(data["events"])
    path.write_text(json.dumps(data))
    evidence = Path("runs/checks/request-failure-evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    with _Server(create_replay_app(folder)) as url:
        ctx, page, errors = _page(browser, width=1600, height=1000)
        try:
            page.goto(url)
            page.wait_for_selector(".rec-intro")
            _to_the_end(page)
            page.click('.ab[data-page="history"]')
            page.locator('#hist-index .hrow').filter(has=page.locator('.hr-now')).click()
            page.locator('#hist-body .hist-sub button:has-text("Context")').click()
            chart = page.locator("#hist-body .request-chart")
            expect(chart.locator(".request-point")).to_have_count(5)
            expect(chart).to_contain_text("3 retried attempts · 3 recovered")
            expect(chart.locator('[data-number="1"] .token-unknown')).to_have_text("↻")
            chart.locator('[data-number="1"]').click()
            expect(page.locator("#r-title")).to_have_text("Request 1 · Hands")
            inspector = page.locator("#r-detail .request-inspector")
            expect(inspector).to_contain_text("Recovered by #3")
            expect(inspector).to_contain_text("Service error · HTTP 503")
            expect(inspector).to_contain_text("gateway-r1")
            expect(inspector).to_contain_text("Input: Unknown")
            inspector.get_by_role("button", name="Next attempt: request #2", exact=True).click()
            expect(page.locator("#r-title")).to_have_text("Request 2 · Hands")
            expect(inspector).to_contain_text("Request timed out · ReadTimeout")
            inspector.get_by_role("button", name="Recovered by: request #3", exact=True).click()
            expect(page.locator("#r-title")).to_have_text("Request 3 · Hands")
            expect(chart.locator(".request-point.selected")).to_have_attribute("data-number", "3")
            expect(inspector).to_contain_text("Response received")
            expect(inspector).to_contain_text("Input: 9,356")
            inspector.get_by_role("button", name="Previous attempt: request #2", exact=True).click()
            expect(page.locator("#r-title")).to_have_text("Request 2 · Hands")
            page.screenshot(path=str(evidence / "timeout-recovered.png"))
            chart.locator('[data-number="4"]').click()
            expect(page.locator("#r-title")).to_have_text("Request 4 · Hands")
            expect(inspector).to_contain_text(
                "The reason for this earlier attempt was not recorded."
            )
            expect(inspector).to_contain_text("Recovered by #5")
            page.locator('#hist-body [data-number="1"]').click()
            expect(page.locator("#r-title")).to_have_text("Request 1 · Hands")
            expect(inspector).to_contain_text("HTTP 503")
            inspector.get_by_role("button", name="Recovered by: request #3", exact=True).click()
            expect(page.locator("#r-title")).to_have_text("Request 3 · Hands")
            expect(page.locator("#hist-body .request-point.selected")).to_have_attribute(
                "data-number", "3"
            )
            page.set_viewport_size({"width": 390, "height": 844})
            expect(inspector).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(evidence / "recovery-mobile.png"))
            assert not errors, errors
        finally:
            ctx.close()
