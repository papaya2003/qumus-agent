"""The web UI in a real browser (headless Chromium via Playwright).

Skipped when Playwright or its Chromium is missing (CI installs both in the
`ui` job). Checks the replay page end to end (play, pause, seek, reload,
restart, drawer, photos, narrow and wide windows, both themes) and the live
page with scripted agents; any console error or uncaught exception fails.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from conftest import drive_recording_fixture, run_fixture_coroutine, v2_recording

pw = pytest.importorskip("playwright.sync_api")

from qumus2.app import build_services  # noqa: E402
from qumus2.ui.app import create_app  # noqa: E402
from qumus2.ui.recording import attach_recorder, export_recording  # noqa: E402
from qumus2.ui.replay import create_replay_app  # noqa: E402


def _chromium() -> str | None:
    exe = os.environ.get("QUMUS_CHROMIUM") or "/opt/pw-browsers/chromium"
    return exe if Path(exe).exists() else None


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=_chromium())
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {e}")
        yield b
        b.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, app):
        import uvicorn

        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        t0 = time.monotonic()
        while not self.server.started:
            if time.monotonic() - t0 > 20:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        return f"http://127.0.0.1:{self.port}/"

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _page(browser, width=1440, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("response", lambda r: errors.append(f"http {r.status}: {r.url}")
            if r.status >= 400 else None)
    return ctx, page, errors


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    """A scripted 'place a chip' run with one askew placement, recorded."""
    tmp = tmp_path_factory.mktemp("rec")
    s = build_services(tmp / "run" / "qumus.db", scenario="place_chip_demo", seed=2,
                       step_delay_s=0.05)
    rec = attach_recorder(s, poll_s=0.02, bench_every_s=0.05)
    s.lab.faults.inject("next_placement", {"dx": 2.0, "dy": -1.0, "drot": 2.0})
    s.store.emit("fault.injected", {"fault": "next_placement",
                                    "params": {"dx": 2.0, "dy": -1.0, "drot": 2.0}})
    s.store.add_instruction("Put a chip on the transfer stage", project_id=s.project_id)
    # Playwright's sync API keeps an event loop on this thread
    t = threading.Thread(target=lambda: asyncio.run(s.driver.wait_idle(timeout=60)))
    t.start()
    t.join()
    rec.stop()
    return export_recording(tmp / "run" / "qumus.db", tmp / "recordings" / "place",
                            title="Place a chip")


def _time(page) -> str:
    return page.inner_text("#rp-time")


def _wait_finished(page, timeout_s=90):
    page.wait_for_function("document.getElementById('conn-text').textContent === "
                           "'Replay finished'", timeout=timeout_s * 1000)
    # The timeline ends before the next animation frame refreshes the feed.
    page.locator("#feed .working").wait_for(state="hidden", timeout=5000)


def test_live_openai_provider_labels_without_model_requests(browser, tmp_path):
    from qumus2.agents.openai_credentials import OpenAICredentials

    s = build_services(tmp_path / "run.db", use_openai=True,
                       openai_credentials=OpenAICredentials("test-only-key", model_id="openai/test"))
    with _Server(create_app(s, start_driver=False)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        # The title is set at boot; the status chip updates on the next frame.
        page.wait_for_function("document.getElementById('mode-t').textContent.includes('openai/test') "
                               "&& document.getElementById('model-text').textContent.includes('OpenAI')")
        assert "OpenAI" in page.inner_text("#model-text")
        assert not [e for e in s.store.events_after(0) if e.kind == "agent.usage"]
        assert not errors, errors
        ctx.close()


def _unfold(page):
    """Open every folded plan step and job in the progress tree (it opens to
    level 2 by itself), so every lab action has its row."""
    for _ in range(40):
        row = page.locator("#plan-view .tr:not(.ac-row):has(> .chev:not(.open))")
        if not row.count():
            return
        row.first.click()
        page.wait_for_timeout(60)


def test_replay_plays_to_the_end(browser, recording):
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        assert "Place a chip" in page.inner_text(".rec-intro")
        assert page.is_hidden("#stop-btn") and page.is_hidden(".composer")
        page.click('#rp-speed button[data-s="8"]')
        _wait_finished(page)
        feed = page.inner_text("#feed")
        assert "Hands" in feed and "correction" in feed
        # the progress tree lists the job under its plan step, finished
        assert page.locator("#plan-view .tr.hw.ok, #plan-view .tr.hw.fix").count() >= 1
        assert "in tolerance" in page.inner_text("#verify")
        assert page.locator(".rp-tick").count() >= 2
        # the end looks finished: nobody working, no job still running
        assert page.locator(".working:visible").count() == 0
        assert page.locator("#plan-view .tr.hw.cur2").count() == 0
        # the lab column: both camera frames stay on screen; the overview shows a
        # diagram of saved state; the microscope says no micrograph was taken
        assert page.locator("#cam img").count() >= 1
        assert page.locator('.cam[data-cam="overview_camera"] svg.overview-diagram').count() == 1
        assert page.is_visible('.cam[data-cam="microscope"]')
        # the last check is one line; "history" unfolds the attempts
        assert page.locator("#verify .vck").count() == 1
        assert page.is_hidden("#verify .vdetail")
        page.click("#verify .vhist")
        assert page.is_visible("#verify .vdetail") and page.locator("#verify .attempt").count() >= 1
        ctx.close()
        assert not errors, errors


def test_replay_seek_to_the_end_matches_playing_to_the_end(browser, recording, tmp_path):
    """A click at the right end of the track (where the last chapter mark sits)
    shows the whole run, as playing to the end does: the final message is
    there and nobody is still working (redesign B4). As in the recordings of
    real runs, a pause before the last events puts a "skipped" mark at the end."""
    import json
    import shutil

    rec = tmp_path / "rec"
    shutil.copytree(recording, rec)
    data = json.loads((rec / "recording.json").read_text())
    for e in data["events"][-2:]:
        e["wall"] = e["wall"] + 12.0
    (rec / "recording.json").write_text(json.dumps(data))
    with _Server(create_replay_app(rec)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.click('#rp-speed button[data-s="8"]')
        _wait_finished(page)
        page.wait_for_timeout(300)  # the next frame draws the finished state
        assert page.locator(".working:visible").count() == 0
        played = page.inner_text("#feed")
        page.click("#rp-restart")
        page.click("#rp-play")  # pause at the start
        box = page.locator("#rp-track").bounding_box()
        page.mouse.click(box["x"] + box["width"] - 1, box["y"] + box["height"] / 2)
        page.wait_for_timeout(500)
        assert page.inner_text("#conn-text") == "Replay finished"
        assert page.locator(".working:visible").count() == 0
        assert page.inner_text("#feed") == played
        # the mark at the end goes to the end as well
        page.click("#rp-restart")
        page.click("#rp-play")
        last = page.locator(".rp-tick.k-skip").last
        assert float(last.evaluate("e => parseFloat(e.style.left)")) >= 99.5
        last.click(force=True)
        page.wait_for_timeout(300)
        assert page.inner_text("#feed") == played
        ctx.close()
        assert not errors, errors


def test_replay_does_not_count_cross_process_epoch_jumps(browser, recording):
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        duration = page.evaluate("""async () => {
            const {Replay} = await import('/static/replay.js');
            const times = [100, 1800000000, 1800000012, 103, 104, 1800000012];
            const events = times.map((ts, id) => ({id, ts, wall: id, kind: 'notification', payload: {}}));
            return new Replay({events}, {}).duration;
        }""")
        assert duration == 16  # 12 s lab work plus four 1 s agent/wall gaps
        ctx.close()
        assert not errors, errors


def test_results_report_excludes_job_diagnostics(browser, recording):
    """Report products in Results; explicit diagnostic links stay in Progress."""
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        _to_the_end(page)
        page.click('.ab[data-page="results"]')
        page.wait_for_selector("#res-body .verdict")
        assert page.inner_text("#results-index .drow").startswith("Overview")
        verdict = page.inner_text("#res-body .verdict")
        assert "Put a chip on the transfer stage" in verdict
        assert "hardware job" not in verdict
        assert page.locator("#res-body .jrow").count() == 0
        assert "LEFT FOR A PERSON" in page.inner_text("#res-body").upper()
        page.click('.ab[data-page="progress"]')
        page.locator('#plan-view .tr-act').last.click()
        page.wait_for_selector("#res-body .jr-steps li")
        assert page.is_visible("#res-home")
        page.click("#res-home")
        page.wait_for_selector("#res-body .verdict")
        assert page.get_attribute("#lab", "data-r") == "detail"
        ctx.close()
        assert not errors, errors


def test_request_chart_in_recording_history_and_replay(browser, recording, tmp_path):
    """History context shows saved measurements and right-side details."""
    import json
    import shutil

    rec = tmp_path / "request-chart"
    shutil.copytree(recording, rec)
    data = json.loads((rec / "recording.json").read_text())
    last = data["events"][-1]
    for n, (agent, tokens) in enumerate([("orchestrator", 12000), ("eyes", 8000), ("hands", 24000)], 1):
        identity = {"request_id": f"r{n}", "call_id": f"c{n}", "session_id": "s1",
                    "model": "offline-test-model", "context_window_tokens": 32768,
                    "token_measurement": "provider_usage_v1", "attempt": 1}
        for offset, kind, payload in [(0, 'agent.request', identity),
                                     (1, 'agent.request_usage', {**identity, 'input_tokens': tokens, 'output_tokens': 1000})]:
            i = 2 * n + offset
            data["events"].append({"id": last["id"] + i, "kind": kind,
                "ts": last["ts"] + i, "wall": last["wall"] + i, "agent": agent, "run_id": None,
                "payload": payload})
    data["meta"]["n_events"] = len(data["events"])
    (rec / "recording.json").write_text(json.dumps(data))
    with _Server(create_replay_app(rec)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector('.rec-intro')
        _to_the_end(page)
        page.click('.ab[data-page="history"]')
        page.locator('#hist-index .hrow').filter(has=page.locator('.hr-now')).click()
        page.locator('#hist-body .hist-sub button:has-text("Context")').click()
        page.wait_for_selector('#hist-body .request-point')
        assert page.locator('#hist-body .request-point').count() == 3
        assert 'Request tokens' in page.inner_text('#hist-body')
        assert 'Request number' in page.inner_text('#hist-body')
        page.locator('#hist-body .request-point[data-number="2"]').click()
        pw.expect(page.locator('#r-detail .request-inspector')).to_contain_text('Input: 8,000')
        assert 'Output: 1,000' in page.inner_text('#r-detail .request-inspector')
        assert 'Remaining after input + output: 23,768 tokens' in page.inner_text('#r-detail .request-inspector')
        assert 'Eyes' in page.inner_text('#r-detail .request-inspector')
        page.wait_for_selector('#hist-body .request-point')
        assert page.locator('#hist-body .request-point').count() == 3
        page.locator('#hist-body .request-chart').screenshot(path=str(tmp_path / 'request-chart.png'))
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('button[data-back="centre"]:visible').click()
        pw.expect(page.locator('#hist-body .request-point[data-number="1"]')).to_be_visible()
        page.locator('#hist-body .request-point[data-number="1"]').focus()
        pw.expect(page.locator('#r-detail .request-inspector')).to_contain_text('Input: 12,000')
        page.screenshot(path=str(tmp_path / 'request-chart-mobile.png'))
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        ctx.close()
        assert not errors, errors


def test_replay_controls(browser, recording):
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        # seek to the middle
        box = page.locator("#rp-track").bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2)
        page.wait_for_timeout(300)
        assert page.locator("#feed .item").count() > 3
        # pause holds the position
        if page.get_attribute("#rp-play", "aria-label") == "Pause":
            page.click("#rp-play")
        t = _time(page)
        page.wait_for_timeout(1500)
        assert _time(page) == t
        assert page.inner_text("#conn-text") == "Replay paused"
        # a reload keeps position and pause state
        page.reload()
        page.wait_for_selector(".rec-intro")
        page.wait_for_timeout(500)
        assert _time(page) == t and page.inner_text("#conn-text") == "Replay paused"
        # dragging past the right end while playing shows the finished run, paused
        page.click("#rp-play")
        assert page.inner_text("#conn-text") == "Replaying"
        box = page.locator("#rp-track").bounding_box()  # the status text beside the bar changed width
        y = box["y"] + box["height"] - 2  # below the chapter ticks, which take their own clicks
        page.mouse.move(box["x"] + box["width"] * 0.9, y)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] + 30, y, steps=3)
        page.mouse.up()
        page.wait_for_timeout(500)
        assert page.inner_text("#conn-text") == "Replay finished"
        assert page.get_attribute("#rp-play", "aria-label") == "Play"
        assert page.locator(".working:visible").count() == 0
        assert page.locator("#plan-view .tr.hw.cur2").count() == 0
        page.reload()
        page.wait_for_selector(".rec-intro")
        # the job's record opens from its row in the tree (the ↗ button), in Results
        page.locator("#plan-view .tr.hw").first.hover()
        page.locator("#plan-view .tr.hw .tr-act").first.click()
        page.wait_for_selector("#res-body .jr-steps li")
        assert page.get_attribute("#app", "data-scene") == "results"
        page.click('.ab[data-page="progress"]')
        assert page.is_visible("#feed")
        # photos open in the lightbox (the eyes' bench photos sit in their folded card)
        shown = page.locator("#feed .thumbs img:visible")
        if shown.count():
            shown.last.click()
            assert page.is_visible("#lightbox")
            page.keyboard.press("Escape")
        # restart from the beginning and play again to the end, twice
        for _ in range(2):
            before = page.locator("#feed .item").count()
            page.click("#rp-restart")
            assert page.locator("#feed .item").count() < before
            page.click('#rp-speed button[data-s="8"]')
            _wait_finished(page)
        # space toggles play
        page.click("#rp-restart")
        page.evaluate("document.activeElement.blur()")
        page.keyboard.press("Space")
        page.wait_for_timeout(200)
        assert page.inner_text("#conn-text") == "Replay paused"
        ctx.close()
        assert not errors, errors


@pytest.mark.parametrize("width,height,theme", [(390, 844, "dark"), (820, 1180, "light"),
                                                (1280, 800, "dark"), (1920, 1080, "light")])
def test_replay_layouts(browser, recording, width, height, theme):
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser, width, height)
        page.add_init_script(f"localStorage.setItem('qumus.theme', '{theme}')")
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.click('#rp-speed button[data-s="4"]')
        _wait_finished(page)
        scroll_w, inner_w = page.evaluate("[document.scrollingElement.scrollWidth, innerWidth]")
        assert scroll_w <= inner_w, "page scrolls sideways"
        phone = width <= 860  # one column at a time, four tabs at the bottom
        assert page.is_visible("#replay-bar") and page.is_visible("#feed") and page.is_visible("#now")
        # the conversation scrolls inside its column instead of growing the page
        assert page.locator(".ed").bounding_box()["height"] <= height
        assert page.locator(".sb").bounding_box()["y"] + 24 <= height + 1
        if phone:  # the bench column is one tap from the conversation
            page.click('#v-conv .bench-link')
            assert page.is_hidden("#feed")
        for sel in ("#bench", '.cam[data-cam="overview_camera"]', '.cam[data-cam="microscope"]', "#verify"):
            assert page.is_visible(sel), sel
        assert page.locator("#bench").bounding_box()["height"] > 100
        if not phone:  # the three pictures and the check line fit the column without scrolling
            lab = page.locator("#lab").bounding_box()
            v = page.locator("#verify").bounding_box()
            assert v["y"] + v["height"] <= lab["y"] + lab["height"] + 1
        if phone:
            page.click('.ab[data-page="progress"]')
        assert page.is_visible("#plan-view")
        # Lab · Status: the lab map with what needs a person; on a phone the
        # cameras are one tap away
        page.click('.ab[data-page="lab"]')
        page.get_by_role('button', name='Bench overview', exact=True).click()
        page.wait_for_selector("#needs .observed-scene-controls")
        assert page.is_visible("#labmap") and page.is_visible("#needs")
        observed = page.locator('#needs').get_by_role('button', name='Observed evidence', exact=True)
        diagnostics = page.locator('#needs').get_by_role('button', name='Simulation diagnostics', exact=True)
        assert diagnostics.get_attribute('aria-pressed') == 'true'
        assert page.locator('#v-lab .expand-bench').is_enabled()
        observed.click()
        assert observed.get_attribute('aria-pressed') == 'true'
        assert page.locator('#labmap .mapov > *').count() == 0
        pw.expect(page.locator('#needs')).to_contain_text(re.compile(r'No observed scene recorded|0 located'))
        assert page.locator('#v-lab .expand-bench').is_disabled()
        # This legacy layout fixture has no recorded observed scene. Its old
        # instrument/needs pins remain available only as explicit diagnostics.
        page.locator('#needs').get_by_role('button', name='Simulation diagnostics', exact=True).click()
        page.wait_for_function("document.querySelector('#labmap .mapov').childElementCount > 0")
        assert page.locator("#lab-index .drow.inst").count() >= 4 or phone
        if phone:
            assert page.is_hidden("#lab-index")
            page.click("#cams-link")
        assert page.is_visible('.cam[data-cam="overview_camera"]') and page.is_hidden("#bench")
        scroll_w, inner_w = page.evaluate("[document.scrollingElement.scrollWidth, innerWidth]")
        assert scroll_w <= inner_w, "page scrolls sideways"
        ctx.close()
        assert not errors, errors


def test_observed_scene_uses_saved_synthetic_points_reports_and_original_frames(browser, tmp_path):
    """Real browser proof with invented coordinates and explicitly synthetic priors."""
    from qumus2.records.lab_geometry import LabEvidence, Source, record_evidence
    from qumus2.records.materials import record_material_source, review_material
    from tests.conftest import quiet_bench

    s = build_services(tmp_path / "observed.db", seed=7, testbench_config=quiet_bench())
    physical_before = s.lab.world.evaluation_snapshot()

    def observe(x, y):
        record_evidence(s.store, LabEvidence(
            subject="region:synthetic_browser_fixture", property="overview_observation", bench="sim",
            value={"detections": [{"label": "chip", "pose": {"x": x, "y": y}, "confidence": 0.8}]},
            units="mm,deg", frame="world", observed_at=s.clock.now(), timebase="store_clock",
            source=Source(kind="synthetic", reference="synthetic browser camera fixture")),
            supersede_previous=True)

    observe(111, 143)
    review_material(s.store, s.lab.bench, "carrier_gr_01", expected_material="Gr",
                    by="Synthetic fixture operator", status="confirmed")
    record_evidence(s.store, LabEvidence(
        subject="synthetic_old_waypoint", property="software_waypoint", bench="sim",
        value=[11, -7], units="mm", frame="fixture:legacy_arm",
        source=Source(kind="historical_constant", reference="synthetic original-frame browser fixture")))
    with _Server(create_app(s, start_driver=False)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.click('.ab[data-page="lab"]')
        page.get_by_role('button', name='Bench overview', exact=True).click()
        page.locator('#needs').get_by_role('button', name='Observed evidence', exact=True).click()
        marker = page.locator('#labmap .observed-point[data-identity-status="unidentified"]')
        pw.expect(marker).to_have_count(1)
        first_position = marker.get_attribute('transform')
        assert page.locator('#labmap .mapov > *').count() == 0
        pw.expect(page.locator('#needs')).to_contain_text('carrier_gr_01 · unlocated')
        pw.expect(page.locator('#needs')).to_contain_text('Synthetic fixture operator')
        pw.expect(page.locator('#needs .observed-scene-counts')).to_contain_text('1 located · 1 named without a position')
        point_row = page.locator('#needs .observed-evidence-row').first
        disclosure = point_row.locator('details.observed-evidence-details')
        assert not disclosure.evaluate('(e) => e.open')
        assert 'store_clock' not in point_row.inner_text()
        assert 'Assertions:' not in point_row.inner_text()
        disclosure.locator('summary').click()
        pw.expect(disclosure).to_contain_text(re.compile(r'Assertions: fact_.*events:'))
        pw.expect(disclosure).to_contain_text(re.compile(r'Recorded boundary: event prefix \d+.*store_clock.*event_prefix'))
        assert disclosure.evaluate('(e) => e.open')
        disclosure.locator('summary').click()
        assert page.locator('#v-lab .expand-bench').is_disabled()
        observe(157, 121)
        instruction = s.store.add_instruction('carrier_gr_01 now contains hBN')
        record_material_source(s.store, s.lab.bench, 'carrier_gr_01', 'hBN',
                               instruction_id=instruction, project_id=None)
        page.wait_for_function("old => document.querySelector('#labmap .observed-point')?.getAttribute('transform') !== old",
                               arg=first_position)
        pw.expect(page.locator('#needs')).to_contain_text('Reported values: ["Gr","hBN"]')
        pw.expect(page.locator('#needs')).to_contain_text('Conflicting alternatives')
        assert s.lab.world.evaluation_snapshot() == physical_before
        page.locator('#v-lab').screenshot(path=str(tmp_path / 'synthetic-observed-world.png'))
        page.get_by_role('combobox', name='Observed coordinate frame').select_option('fixture:legacy_arm')
        pw.expect(page.locator('#labmap .observed-point[data-identity-status="historical"]')).to_have_count(1)
        assert page.locator('#labmap .s-holder, #labmap .s-stage.moving').count() == 0
        pw.expect(page.locator('#needs')).to_contain_text('Original coordinates: [11,-7]')
        pw.expect(page.locator('#needs')).to_contain_text('no world transform or motion authority')
        page.locator('#v-lab').screenshot(path=str(tmp_path / 'synthetic-observed-original-frame.png'))
        page.get_by_role('combobox', name='Observed coordinate frame').select_option('world')
        pw.expect(page.locator('#labmap .observed-point[data-identity-status="unidentified"]')).to_have_count(1)
        page.locator('#needs').get_by_role('button', name='Simulation diagnostics', exact=True).click()
        assert page.locator('#v-lab .expand-bench').is_enabled()
        assert page.locator('#v-lab .expand-bench').is_visible()
        assert page.locator('#labmap .observed-point').count() == 0
        page.locator('#v-lab .expand-bench').click()
        page.wait_for_selector('#bench-expanded')
        page.keyboard.press('Escape')
        ctx.close()
        assert not errors, errors


def test_live_page_scripted(browser, tmp_path):
    s = build_services(tmp_path / "qumus.db", scenario="place_chip_demo", seed=2,
                       step_delay_s=0.05)
    with _Server(create_app(s)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".empty .suggest button")
        assert page.is_hidden("#replay-bar")
        page.fill("#input", "Put a chip on the transfer stage")
        page.keyboard.press("Enter")
        page.wait_for_selector("#feed .jline .tag.succeeded", timeout=60_000)
        page.wait_for_function("document.querySelector('.working').hidden", timeout=60_000)
        assert "Hands" in page.inner_text("#feed")
        assert page.inner_text("#model-text") == "Scripted · no model calls"
        assert page.inner_text("#safety")
        # the eyes' inspect_bench and the placement, under their plan steps
        page.wait_for_function("document.querySelectorAll('#plan-view .tr.hw.ok, #plan-view .tr.hw.fix').length === 2")
        assert page.locator("#plan-view .tr.hw.ok, #plan-view .tr.hw.fix").count() == 2
        page.click("#tools-btn")
        assert page.locator(".fbtn").count() >= 5
        assert page.is_visible("#speed-seg")  # scripted mode: the sim clock sets the pace
        ctx.close()
        assert not errors, errors


def test_live_stop_and_continue(browser, tmp_path):
    """Stop pauses the running job, Continue appears and finishes it."""
    s = build_services(tmp_path / "qumus.db", scenario="place_chip_demo", seed=2,
                       step_delay_s=0.05)
    faults, real = s.lab.faults, s.lab.faults.before_call
    hit, release = threading.Event(), threading.Event()
    start: list[int] = []

    def before_call(device_id):  # hold the placement at its 6th device call until Stop
        real(device_id)
        if not start and s.store.list_runs("running") and all(  # after the eyes' look
                r["flow"] == "place_chip_on_transfer_stage" for r in s.store.list_runs("running")):
            start.append(faults.calls - 1)
        if start and faults.calls - start[0] == 6 and not hit.is_set():
            hit.set()
            release.wait(30)

    faults.before_call = before_call
    with _Server(create_app(s)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".empty .suggest button")
        assert page.is_hidden("#continue-btn")
        page.fill("#input", "Put a chip on the transfer stage")
        page.keyboard.press("Enter")
        assert hit.wait(60)
        page.click("#stop-btn")
        page.wait_for_selector("#feed .divider.stop", timeout=30_000)
        release.set()
        page.wait_for_selector("#continue-btn:visible", timeout=30_000)
        page.click("#continue-btn")
        page.wait_for_selector("#feed .divider.go", timeout=30_000)
        # the placement's line too, not only the eyes' inspect_bench before it
        page.wait_for_function(
            "document.querySelectorAll('#feed .jline .tag.succeeded').length >= 2",
            timeout=60_000)
        page.wait_for_function("document.getElementById('continue-btn').hidden", timeout=30_000)
        assert "continues from step" in page.inner_text("#feed")
        ctx.close()
        assert not errors, errors


def test_replay_of_a_preserved_claude_run_without_sdk(browser):
    """Historical recordings still render after the execution SDK is removed."""
    from qumus2.ui.recording import load_recording

    rec_dir = v2_recording("archive/v2-baseline-seed1")
    rec = load_recording(rec_dir)
    assert rec["meta"]["agents"] == "sdk"
    assert any(e["kind"] == "agent.turn_end" for e in rec["events"])
    with _Server(create_replay_app(rec_dir)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        assert "Claude" in page.inner_text(".rec-intro")
        assert "Claude" in page.inner_text("#mode")
        _to_the_end(page)
        assert page.inner_text("#feed").strip()
        ctx.close()
        assert not errors, errors


STATION_LAYOUT = {
    "table": {"x": [0, 1260], "y": [0, 630]},
    "stations": {
        "left_storage": {"kind": "storage", "label": "Storage", "region": {"x": [40, 300], "y": [40, 240]}},
        "middle_storage": {"kind": "storage", "label": "Storage", "region": {"x": [500, 760], "y": [40, 240]}},
        "right_storage": {"kind": "storage", "label": "Storage", "region": {"x": [960, 1220], "y": [40, 240]}},
        "exfoliation_stage": {"kind": "exfoliation", "label": "Exfoliation",
                              "region": {"x": [120, 420], "y": [380, 590]}},
        "transfer_stage": {"kind": "microscope", "label": "Search & stacking stage",
                           "region": {"x": [500, 760], "y": [380, 590]}},
        "reactor": {"kind": "reactor", "label": "Reactor", "region": {"x": [960, 1180], "y": [360, 600]}},
    },
    "slots": {"left_chip_1": {"x": 100, "y": 100, "station": "left_storage", "holds": "chip"},
              "left_slide_1": {"x": 220, "y": 160, "station": "left_storage", "holds": "carrier"},
              "mid_chip_1": {"x": 560, "y": 100, "station": "middle_storage", "holds": "chip"},
              "right_stamp_1": {"x": 1020, "y": 100, "station": "right_storage", "holds": "stamp"}},
    "qr_markers": {"left_storage": [[50, 50], [290, 50], [50, 230], [290, 230]]},
    "arm_left": {"home": {"x": 0, "y": 0, "z": 120, "rotation": 0}, "base": {"x": 400, "y": 300},
                 "reach": ["left_storage", "exfoliation_stage", "middle_storage"]},
    "arm_right": {"home": {"x": 0, "y": 0, "z": 120, "rotation": 0}, "base": {"x": 860, "y": 300},
                  "reach": ["middle_storage", "transfer_stage", "right_storage"]},
}


def test_replay_station_layout(browser, recording, tmp_path):
    import json
    import shutil

    rec_dir = tmp_path / "stations"
    shutil.copytree(recording, rec_dir)
    data = json.loads((rec_dir / "recording.json").read_text())
    data["layout"] = STATION_LAYOUT
    (rec_dir / "recording.json").write_text(json.dumps(data))
    with _Server(create_replay_app(rec_dir)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        assert page.locator("#bench .s-station").count() == 6
        assert page.locator("#bench .s-qr").count() == 4
        assert page.locator("#bench .s-slot").count() == 4
        assert "Arm 1" in page.text_content("#bench")
        page.click('#rp-speed button[data-s="8"]')
        _wait_finished(page)
        # arm nozzles are drawn in world coordinates, near their bases
        assert page.locator("#bench .b-nozzle").count() == 2
        ctx.close()
        assert not errors, errors


def test_experience_page(browser, recording, tmp_path):
    """History · Experience (scenario S4), live and in a replay: the switch at the
    top of the left column, "No real experience yet", the cards, and an entry's
    provenance in the right column."""
    s = build_services(tmp_path / "qumus.db", scenario="place_chip_demo", seed=2,
                       step_delay_s=0.05)
    for app in (create_app(s), create_replay_app(recording)):
        with _Server(app) as url:
            ctx, page, errors = _page(browser, 1280, 800)
            page.goto(url)
            page.wait_for_selector("#feed")
            page.click('.ab[data-page="history"]')
            page.click('#hist-tabs button[data-t="experience"]')
            page.wait_for_selector("#xp-body .xp-banner")
            assert "No real experience yet" in page.inner_text("#xp-body")
            assert "Testbench runs are simulated" in page.inner_text("#xp-body")
            assert page.is_visible("#v-xp") and page.is_hidden("#v-history")
            assert "Real lab runs" in page.inner_text("#hist-index")
            cards = page.locator("#xp-body .xp-card")
            if cards.count():
                cards.first.click()
                page.wait_for_selector("#r-detail .detail-body")
                assert "Source" in page.inner_text("#r-detail")
            page.click('#hist-tabs button[data-t="runs"]')
            page.wait_for_selector("#v-history", state="visible")
            page.click('.ab[data-page="progress"]')
            assert page.is_visible("#plan-view")
            ctx.close()
            assert not errors, errors


def test_lab_inventory_and_film_report(browser, recording, tmp_path):
    """Lab (scenario S3) has one switch, Status | Stock. Status lists the
    instruments and supplies; Stock lists the categories with their counts, the
    cards in the centre and an item's details on the right, with the "New PC
    film" report on a used or torn stamp (live only). A replay rebuilds the
    stock from the recorded events."""
    s = build_services(tmp_path / "qumus.db", scenario="place_chip_demo", seed=2,
                       step_delay_s=0.05)
    s.store.upsert_sample("stamp_01", "stamp", status="used", props={"film": None})
    s.store.upsert_sample("stamp_02", "stamp", status="torn")
    with _Server(create_app(s)) as url:
        ctx, page, errors = _page(browser, 1280, 800)
        page.goto(url)
        page.wait_for_selector("#feed")
        page.click('.ab[data-page="lab"]')
        page.wait_for_selector("#lab-index .drow.inst")
        assert page.locator("#lab-index .drow.inst").count() >= 4
        assert "INSTRUMENTS" in page.inner_text("#lab-index").upper() and "SUPPLIES" in page.inner_text("#lab-index").upper()
        assert page.is_visible("#stock-body")
        # Select an instrument group, then its middle card for right details.
        page.locator("#lab-index .drow.inst").first.click()
        page.wait_for_selector('#stock-body .status-card')
        page.locator('#stock-body .status-card').first.click()
        page.wait_for_selector("#r-detail .inst-part")
        page.get_by_role('button', name='Bench overview', exact=True).click()
        page.wait_for_selector('#lab[data-r="cams"]')
        page.click('#lab-tabs button[data-t="stock"]')
        page.wait_for_selector("#stock-body .stamp-pill")
        index = page.inner_text("#lab-index")
        for title in ("Mother tapes & daughter tape", "Blank chips", "Target chips (electrodes)", "Stamps"):
            assert title in index, title
        assert re.search(r"Stamps\s+3 · 1 new", index), index
        inv = page.inner_text("#stock-body")
        assert "left board, slot 1" in inv  # slots in words
        assert page.is_hidden("#labmap")  # no lab map next to the stock
        pill = page.locator('#stock-body .stamp-pill[data-id="stamp_02"]')
        assert pill.locator(".tag.torn").count() == 1
        page.click('#stock-body .stamp-pill[data-id="stamp_03"]')
        page.wait_for_selector('#r-detail .detail-body')
        assert page.locator("#r-detail .inv-film").count() == 0  # a new stamp needs no film
        pill.click()
        page.wait_for_selector("#r-detail .inv-film")
        assert "stamp_02" in page.inner_text("#r-title")
        page.fill("#r-detail .inv-film input", "Jialei")
        page.click("#r-detail .inv-film .inv-btn.primary")
        page.wait_for_selector('#stock-body .stamp-pill[data-id="stamp_02"] .tag.fresh')
        page.wait_for_function("document.getElementById('r-detail').textContent.includes('new film reported by Jialei')")
        assert s.store.get_sample("stamp_02")["status"] == "fresh"
        # the search box finds a stamp by its id
        page.fill("#stock-body .inv-find", "stamp_01")
        page.wait_for_timeout(200)
        assert page.locator('#stock-body [data-id="stamp_01"]').count() == 1
        assert page.locator('#stock-body [data-id="stamp_02"]').count() == 0
        # a category in the index shows only its cards
        page.fill("#stock-body .inv-find", "")
        page.locator('#lab-index .drow:has-text("Blank chips")').click()
        page.wait_for_timeout(200)
        assert page.locator("#stock-body .o-h").count() == 1 and page.locator("#stock-body .stamp-pill").count() >= 1
        ctx.close()
        assert not errors, errors
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser, 1280, 800)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.click('#rp-speed button[data-s="8"]')
        _wait_finished(page)
        page.click('.ab[data-page="lab"]')
        page.click('#lab-tabs button[data-t="stock"]')
        page.wait_for_selector("#stock-body .stamp-pill")
        assert "at this point of the recording" in page.inner_text("#stock-sub")
        assert "Stamps" in page.inner_text("#lab-index")
        assert page.locator("#stock-body .stamp-pill").count() >= 6
        page.locator("#stock-body .stamp-pill").last.click()
        page.wait_for_selector("#r-detail .detail-body")
        assert page.locator("#r-detail .inv-film").count() == 0  # nothing is reported from a replay
        ctx.close()
        assert not errors, errors


@pytest.fixture(scope="module")
def device_recording(tmp_path_factory):
    """A scripted graphene device (exfoliate, search two chips, pick, stack), recorded:
    the flake library's data (flake photos ride on the ledger events)."""
    tmp = tmp_path_factory.mktemp("dev")
    s = build_services(tmp / "run" / "qumus.db", scenario="graphene_device", seed=2,
                       step_delay_s=0.0)
    rec = attach_recorder(s, poll_s=0.02, bench_every_s=0.5)
    s.store.add_instruction("Make a graphene device", project_id=s.project_id)
    try:
        # Keep the original 600s turn budget. Cleanup owns 30s and the bridge
        # has 5s for loop teardown; a background exception reaches pytest.
        run_fixture_coroutine(lambda: drive_recording_fixture(s, rec), timeout_s=635, force_thread=True)
    except BaseException as error:
        import json

        (tmp / "fixture-failure.json").write_text(json.dumps({
            "type": type(error).__name__, "message": str(error),
            "notes": getattr(error, "__notes__", []), "complete_export": False,
            "database": str(tmp / "run" / "qumus.db")}, indent=2) + "\n")
        raise
    return export_recording(tmp / "run" / "qumus.db", tmp / "recordings" / "device",
                            title="Make a graphene device")


def _to_the_end(page):
    box = page.locator("#rp-track").bounding_box()
    page.mouse.click(box["x"] + box["width"] - 1, box["y"] + box["height"] / 2)
    page.wait_for_timeout(500)


def _stock(page, category=None):
    """Lab · Stock, optionally one category of the left index."""
    page.click('.ab[data-page="lab"]')
    if not page.is_visible("#lab-tabs"):
        page.click("#v-lab .pback")  # a phone: the switch sits in the left column
    page.click('#lab-tabs button[data-t="stock"]')
    if category:
        page.locator(f'#lab-index .drow:has-text("{category}")').first.click()
    page.wait_for_timeout(200)


def test_flake_library_in_a_replay(browser, device_recording):
    """Lab · Stock (scenario S3): the searched chips with their flakes' photos
    and the device with its layers; the Flakes category lists every flake with
    its photo and filters it; a flake's details (both photos, labels, area,
    place on the chip, history) take the right column. A replay cannot veto or
    ask."""
    import json

    recorded = json.loads((device_recording / "recording.json").read_text())
    searched = {e["payload"]["params"]["chip_id"] for e in recorded["events"]
                if e["kind"] == "job.submitted" and e["payload"]["flow"] == "search_chip"}
    assert len(searched) >= 2  # supplementary preparation may add source chips
    with _Server(create_replay_app(device_recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        _to_the_end(page)
        _stock(page)
        page.wait_for_selector("#stock-body .chipcard")
        cards = page.locator("#stock-body .chipcard")
        assert {cards.nth(i).get_attribute("data-id") for i in range(cards.count())} == searched
        assert page.locator("#stock-body .chipcard .cc-th img").count() >= 4
        assert "photographed" in cards.first.inner_text()
        dev = page.locator("#stock-body .devcard")
        assert dev.count() == 1 and dev.locator(".dc-l").count() == 3
        assert dev.locator(".dc-l img").count() == 3  # every layer has its photo
        # a thumbnail opens that flake's details in the right column
        th = cards.first.locator(".cc-th img").first
        fid = th.get_attribute("alt")
        th.click()
        page.wait_for_selector(f'#r-detail .fl-detail[data-id="{fid}"]')
        detail = page.locator("#r-detail .fl-detail")
        text = detail.inner_text()
        for words in ("Thickness", "Area", "µm²", "Came from", "mm from the chip's corner", "Found in the 10× scan"):
            assert words in text, words
        assert detail.locator(".fl-big img").count() == 1
        src100 = detail.locator(".fl-big img").get_attribute("src")
        detail.locator('.fl-obj button:has-text("20×")').click()
        assert page.locator("#r-detail .fl-big img").get_attribute("src") != src100
        # no picture of the lab in the details (Jialei, 2026-10-23)
        assert page.locator("#r-detail svg, #lab .bench-wrap:visible").count() == 0
        # replay: nothing is sent
        assert page.locator('#r-detail .fl-actions button:has-text("Don\'t use")').is_disabled()
        assert page.locator('#r-detail .fl-actions button:has-text("Ask Qumus")').is_disabled()
        # a chip card opens the chip, with its flakes
        cards.first.locator(".cc-h").click()
        page.wait_for_selector("#r-detail .thumbgrid .tg img")
        # the Flakes category: every flake, filters (a material, then what was used in the device)
        page.locator('#lab-index .drow:has-text("Flakes")').first.click()
        page.wait_for_selector("#stock-body .fcard")
        all_n = page.locator("#stock-body .fcard").count()
        assert all_n >= 20 and page.locator("#stock-body .fcard img").count() >= 10
        page.click('#stock-body .fchip:has-text("hBN")')
        hbn = page.locator("#stock-body .fcard").count()
        assert 0 < hbn < all_n
        page.click('#stock-body .fl-fg:has-text("Material") .fchip:has-text("All")')
        page.click('#stock-body .fchip:has-text("Used or consumed")')
        used = page.locator("#stock-body .fcard")
        assert used.count() == 3 and all("in device_001" in used.nth(i).inner_text() for i in range(3))
        used.first.click()
        page.wait_for_selector(f'#r-detail .fl-detail[data-id="{used.first.get_attribute("data-id")}"]')
        hist = page.inner_text("#r-detail .fl-hist")
        assert "Stacked into device_001" in hist and "Chosen as" in hist
        # the history reads in time order (a Picker verdict sits at its call's time)
        when = [sum(int(x) * 60 ** k for k, x in enumerate(reversed(t.split(" lab", 1)[0].split(":"))))
                for t in page.locator("#r-detail .fl-hist li .n").all_inner_texts() if t]
        assert when == sorted(when), when
        # the search box finds a flake; its photo opens it
        page.locator('#lab-index .drow:has-text("All")').first.click()
        page.fill("#stock-body .inv-find", fid)
        page.wait_for_timeout(200)
        page.click(f'#stock-body .st-f img[alt="{fid}"]')
        page.wait_for_selector(f'#r-detail .fl-detail[data-id="{fid}"]')
        ctx.close()
        assert not errors, errors


def test_job_record_picker_microscope_and_links(browser, device_recording):
    """Redesign items 4-7: the microscope naming the flake it looks at, the
    Picker's selection with photos (Results · Selected flakes), the job record in
    words (summary, found flakes, one line per step, raw data folded), sample
    ids in the conversation as links; all details open next to what is on
    screen."""
    with _Server(create_replay_app(device_recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        if page.get_attribute("#rp-play", "aria-label") == "Pause":
            page.click("#rp-play")
        # somewhere in a search's revisit the microscope shows a flake photo and names it
        box = page.locator("#rp-track").bounding_box()
        seen = None
        for k in range(8, 80, 3):
            page.mouse.click(box["x"] + box["width"] * k / 100, box["y"] + box["height"] - 2)
            page.wait_for_timeout(150)
            if page.is_visible(".cam .look"):
                seen = page.inner_text(".cam .look")
                break
        assert seen and seen.startswith("Looking at chip_00"), seen
        page.click(".cam .look")  # the flake's details take the bench's place, the conversation stays
        page.wait_for_selector("#r-detail .fl-detail")
        assert page.is_visible("#feed") and page.inner_text("#r-back") == "← Bench"
        page.click("#r-back")
        assert page.is_visible("#bench")
        _to_the_end(page)
        # the Picker's choice with photos
        page.click('.ab[data-page="results"]')
        page.locator('#results-index .drow:has-text("Selected flakes")').click()
        page.wait_for_selector("#res-body .fcard")
        assert page.locator("#res-body .fcard img").count() == 3
        pw.expect(page.locator("#r-detail")).to_contain_text("Choose an entry")
        page.locator("#res-body .fcard").first.click()
        page.wait_for_selector("#r-detail .fl-detail")
        # the device: its layers with their photos in the overview
        page.locator('#results-index .drow:has-text("Overview")').click()
        page.wait_for_selector("#res-body .devblock")
        assert page.locator("#res-body .devblock .ftile img").count() == 3
        # the job record of a search
        page.click('.ab[data-page="progress"]')
        page.locator('#plan-view .tr.hw:has-text("Search a chip") .tr-act').first.click()
        page.wait_for_selector("#res-body .jr-sum")
        rec = page.inner_text("#res-body")
        assert "Scanned" in rec and "found again" in rec and "Found flakes" in rec
        assert page.locator("#res-body .jr-found img").count() >= 4
        assert page.locator("#res-body .jr-steps li").count() >= 6
        assert "/ 0.01 mm" in rec  # measured / allowed
        assert page.locator("#res-body details[open]").count() == 0
        assert page.locator("#res-body .cmds > div").count() == 0  # built when opened
        page.click('#res-body .jr-fold summary:has-text("device commands")')
        page.wait_for_selector("#res-body .cmds > div")
        assert page.locator("#res-body .cmds > div").count() > 10
        page.locator("#res-body .jr-found .jr-f").first.click()
        page.wait_for_selector("#r-detail .fl-detail")
        assert page.locator("#res-body .jr-sum").count() == 1  # the record stays
        # sample ids in the conversation are links: details beside the conversation
        page.click('.ab[data-page="progress"]')
        links = page.locator("#feed .idlink")
        assert links.count() >= 1
        links.last.click()
        page.wait_for_selector("#r-detail .detail-body, #r-detail .fl-detail")
        assert page.is_visible("#feed")
        ctx.close()
        assert not errors, errors


@pytest.mark.parametrize("width,height,theme", [(390, 844, "dark"), (1280, 800, "light")])
def test_flake_library_layouts(browser, device_recording, width, height, theme):
    """Lab · Stock · Flakes and a flake's details fit a phone and a laptop in
    both themes: no sideways scroll."""
    with _Server(create_replay_app(device_recording)) as url:
        ctx, page, errors = _page(browser, width, height)
        page.emulate_media(color_scheme=theme)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        _to_the_end(page)
        _stock(page, "Flakes")
        page.wait_for_selector("#stock-body .fcard img")
        assert page.is_visible("#stock-body")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.locator("#stock-body .fcard").first.click()
        page.wait_for_selector("#r-detail .fl-detail")
        assert page.is_visible("#r-detail .fl-big")
        d = page.locator("#r-detail").bounding_box()
        assert d["x"] >= 0 and d["x"] + d["width"] <= width + 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        if width < 860:  # a phone: ‹ goes back to the cards
            page.click("#lab .pback")
            assert page.is_visible("#stock-body")
        ctx.close()
        assert not errors, errors


def test_flake_library_live_veto_and_ask(browser, tmp_path):
    """Live, a flake's details talk to Qumus: "Don't use this flake" sends a veto
    (after a confirmation), "Ask Qumus about it" starts a message."""
    s = build_services(tmp_path / "qumus.db", scenario="place_chip_demo", seed=2,
                       step_delay_s=0.05)
    s.store.upsert_sample("chip_001", "chip", location="mid_chip_1", status="searched",
                          props={"material": "hBN"})
    s.store.upsert_sample("chip_001_f001", "flake", location="chip_001", status="catalogued",
                          props={"material": "hBN", "chip_id": "chip_001", "thickness_label": "20-30",
                                 "labels": {"10x": "20-30", "20x": "20-30", "100x": "20-30"},
                                 "area_um2": 812.0, "confidence": 0.9, "rel_corner_mm": [3.1, 4.2],
                                 "images": {}})
    with _Server(create_app(s, start_driver=False)) as url:
        ctx, page, errors = _page(browser, 1280, 800)
        page.goto(url)
        page.wait_for_selector("#feed")
        _stock(page, "Flakes")
        page.click('#stock-body .fcard[data-id="chip_001_f001"]')
        page.wait_for_selector('#r-detail .fl-detail[data-id="chip_001_f001"]')
        assert "no photo was taken" in page.inner_text("#r-detail .fl-big")
        assert "20–30 nm" in page.inner_text("#r-detail .fl-detail")
        page.click('#r-detail .fl-actions button:has-text("Don\'t use")')
        page.click("#r-detail .fl-confirm .primary")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:
            vetoes = [e.payload for e in s.store.events_after(0, limit=100_000)
                      if e.kind == "user.message" and e.payload.get("kind") == "veto"]
            if vetoes:
                break
            time.sleep(0.1)
        assert vetoes and "chip_001_f001" in vetoes[0]["text"]
        page.click('#r-detail .fl-actions button:has-text("Ask Qumus")')
        assert page.get_attribute("#app", "data-scene") == "progress"
        assert page.input_value("#input").startswith("About chip_001_f001")
        ctx.close()
        assert not errors, errors

MARKDOWN_SAMPLE = """**Search** found 17 flakes:

| Thickness | Count | Flakes |
|---|:---:|---|
| Monolayer | 3 | f002 (144), f010 |
| Bilayer | 1 | f005 |

See [the ledger](/api/snapshot), <https://example.org/x> and [bad](javascript:alert(1)).
Literal <b>html</b> <script>alert(1)</script> stays text.

1. first
2. second
   - nested `a | b`
   - [x] done

> quoted *text*

```python
x = 1
```
"""


def test_markdown_rendering(browser, recording):
    """Agent text renders as Markdown (tables, lists, code, links) and never
    as HTML: tags stay literal, unsafe link targets become plain text."""
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        r = page.evaluate("""async (src) => {
            const { markdown } = await import('/static/markdown.js');
            const box = document.createElement('div');
            box.append(markdown(src));
            document.getElementById('feed').append(box);
            const q = s => [...box.querySelectorAll(s)];
            return {
              ths: q('th').map(t => t.textContent), rows: q('tbody tr').length,
              center: q('th')[1].style.textAlign, cell: q('tbody td')[2].textContent,
              links: q('a').map(a => a.getAttribute('href')),
              tags: q('script, b').length, text: box.textContent,
              code: q('pre code').map(c => c.textContent), nested: q('ol li ul li').length,
              tasks: q('li.task input[checked]').length, em: q('blockquote em').length,
            };
        }""", MARKDOWN_SAMPLE)
        assert r["ths"] == ["Thickness", "Count", "Flakes"] and r["rows"] == 2
        assert r["center"] == "center" and r["cell"] == "f002 (144), f010"
        assert r["links"] == ["/api/snapshot", "https://example.org/x"]
        assert r["tags"] == 0 and "<script>alert(1)</script>" in r["text"] and "<b>html</b>" in r["text"]
        assert r["code"] == ["x = 1"] and r["nested"] == 2 and r["tasks"] == 1 and r["em"] == 1
        ctx.close()
        assert not errors, errors


def test_safety_strip_is_stable(browser, recording):
    """The lab panel's status chips are rebuilt only when their content changes
    (rebuilding them on every poll replayed their entry animation: a blink)."""
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector("#safety .sf")
        page.wait_for_timeout(1200)  # past the autoplay delay, so the pause sticks
        if page.get_attribute("#rp-play", "aria-label") == "Pause":
            page.click("#rp-play")
        page.wait_for_timeout(300)
        same = page.evaluate("""() => new Promise(res => {
            const first = document.querySelector('#safety .sf');
            setTimeout(() => res(document.querySelector('#safety .sf') === first), 3500);
        })""")
        assert same, "the status chip was rebuilt while nothing changed"
        ctx.close()
        assert not errors, errors


def test_replay_clock_runs_at_speed(browser, recording, tmp_path):
    """The replay clock advances `speed` seconds of original run time per real
    second, the thumb moves steadily, and a pause longer than 5 s is skipped
    with a marker (docs/architecture.md, UI decision 5)."""
    import json
    import shutil

    rec_dir = tmp_path / "timed"
    shutil.copytree(recording, rec_dir)
    data = json.loads((rec_dir / "recording.json").read_text())
    ev = data["events"]
    n = len(ev)
    # events spread evenly over 120 s, plus one 45 s pause a third of the way in
    for i, e in enumerate(ev):
        e["wall"] = 1_000_000 + 120 * i / (n - 1) + (45 if i >= n // 3 else 0)
    data["meta"]["duration_s"] = 165
    (rec_dir / "recording.json").write_text(json.dumps(data))

    def seconds(page):
        m, s = page.inner_text("#rp-time").split("/")[0].strip().split(":")
        return int(m) * 60 + int(s)

    def fill(page):
        return float(page.evaluate("parseFloat(document.getElementById('rp-fill').style.width)"))

    with _Server(create_replay_app(rec_dir)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.wait_for_timeout(300)
        if page.get_attribute("#rp-play", "aria-label") == "Pause":
            page.click("#rp-play")
        page.click('#rp-axis button[data-a="session"]')  # this rule is the session clock's
        assert page.locator(".rp-tick.k-skip").count() == 1  # the skipped pause is marked
        page.click('#rp-speed button[data-s="8"]')
        page.click("#rp-restart")
        # the thumb moves steadily: equal steps of the timeline per real second
        steps = []
        last = fill(page)
        for _ in range(4):
            page.wait_for_timeout(1000)
            cur = fill(page)
            steps.append(cur - last)
            last = cur
        mean = sum(steps) / len(steps)
        assert mean > 0 and all(abs(s - mean) < 0.4 * mean for s in steps), steps
        # 8x: after 4 s the original-run clock reads >= 32 s (minus a little slack)
        assert seconds(page) >= 28, page.inner_text("#rp-time")
        # past the pause: 40 of its 45 s were skipped and said so
        page.wait_for_timeout(5000)
        assert seconds(page) >= 60
        assert re.search(r"4[01] s skipped", page.inner_text("#feed"))
        ctx.close()
        assert not errors, errors


def test_replay_lab_time_and_steps(browser, recording, tmp_path):
    """Lab time (the default axis) gives every instrument action the time the
    real instrument takes, although the session spent ~0 s on it; the caption
    names the instrument and step; the action list lists every step and seeks;
    step by step stops before each action, ← goes back (UI decision 6)."""
    import json
    import shutil

    rec_dir = tmp_path / "lab"
    shutil.copytree(recording, rec_dir)
    data = json.loads((rec_dir / "recording.json").read_text())
    ev = data["events"]
    # the session spends 10 ms per event, the lab clock (ts) keeps the instruments' time
    for i, e in enumerate(ev):
        e["wall"] = 1_000_000 + 0.01 * i
    (rec_dir / "recording.json").write_text(json.dumps(data))
    steps = [e["payload"]["name"] for e in ev if e["kind"] == "step.started"]
    lab_s = ev[-1]["ts"] - ev[0]["ts"]
    assert len(steps) >= 3 and lab_s > 20

    def total(page):
        m, s = page.inner_text("#rp-time").split("/")[1].strip().split(":")
        return int(m) * 60 + int(s)

    def current(page):
        return page.inner_text(".ac-row.cur .ac-name") if page.locator(".ac-row.cur").count() else ""

    def wait_paused(page):
        page.wait_for_function("document.getElementById('conn-text').textContent !== 'Replaying'", timeout=60_000)
        page.wait_for_timeout(150)  # the caption and the list redraw on the next frame

    with _Server(create_replay_app(rec_dir)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.wait_for_timeout(300)
        if page.get_attribute("#rp-play", "aria-label") == "Pause":
            page.click("#rp-play")
        # lab time by default, at 4x; the clock's length is the lab's
        assert "on" in page.get_attribute('#rp-axis button[data-a="lab"]', "class")
        assert "on" in page.get_attribute('#rp-speed button[data-s="4"]', "class")
        assert total(page) >= lab_s - 1
        page.click('#rp-axis button[data-a="session"]')
        assert total(page) <= 3
        page.click('#rp-axis button[data-a="lab"]')
        assert total(page) >= lab_s - 1
        # one row per lab action, in order, readable names (the jobs to come are folded)
        assert page.locator(".ac-row").count() == 0
        assert "Later in this recording" in page.inner_text("#plan-view")
        _unfold(page)
        assert page.locator(".ac-row").count() == len(steps)
        assert page.inner_text(".ac-row .ac-name") == "Observe bench objects"
        # step by step: playback stops before every action; the caption says what ran
        page.click("#bc-mode")
        page.click('#rp-speed button[data-s="8"]')
        page.click("#rp-play")
        seen = []
        for _ in range(len(steps) + 1):  # stops before the first action too
            wait_paused(page)
            if page.inner_text("#conn-text") == "Replay finished":
                break
            if seen:  # the action just watched: its instrument, step number and check
                assert re.search(r"step \d+ of \d+", page.inner_text("#bc-text")), page.inner_text("#bc-text")
            seen.append(current(page))
            page.click("#bc-next")
        assert seen[0] == "" and len(set(seen[1:])) >= 2, seen
        # ← goes back to the start of the last action, paused
        page.evaluate("document.activeElement.blur()")
        before = current(page)
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(300)
        assert page.inner_text("#conn-text") == "Replay paused"
        assert current(page) != before or before == seen[0]
        # a row seeks to its action; the caption then names it
        page.click("#bc-mode")  # off
        page.locator(".ac-row").last.click()
        page.wait_for_timeout(300)
        assert page.locator(".ac-row.cur").count() <= 1
        page.click('#rp-speed button[data-s="8"]')
        if page.get_attribute("#rp-play", "aria-label") == "Play":
            page.click("#rp-play")
        page.wait_for_function("document.querySelector('.ac-row.cur') && "
                               "document.querySelector('.ac-row.cur') === [...document.querySelectorAll('.ac-row')].pop()",
                               timeout=30_000)
        assert "Place chip on transfer stage" in page.inner_text("#bc-text")
        _wait_finished(page)
        # at the end every action has its check result, none is still running
        page.wait_for_function("!document.querySelector('.ac-row.going, .ac-row.todo')", timeout=5_000)
        assert page.locator(".ac-row.fix").count() >= 1  # the askew placement was corrected
        ctx.close()
        assert not errors, errors


def test_scripted_mode_has_no_model_cost(browser, tmp_path):
    """/api/health carries cost_usd when the store can sum the turns; the chip
    shows no model cost in scripted mode."""
    import urllib.request
    s = build_services(tmp_path / "qumus.db", scenario="place_chip_demo", seed=2, step_delay_s=0.05)
    s.store.spent_usd = lambda: 1.234  # the store's sum of agent.turn_end cost_usd
    with _Server(create_app(s)) as url:
        with urllib.request.urlopen(url + "api/health") as r:
            import json
            hl = json.loads(r.read())
        assert hl["mode"] == "scripted" and abs(hl["cost_usd"] - 1.234) < 1e-9
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".empty .suggest button")
        page.wait_for_timeout(500)
        assert page.is_hidden("#model-cost")  # scripted: nothing is spent
        assert "Scripted" in page.inner_text("#model-chip")
        ctx.close()
        assert not errors, errors


def test_conversation_group_chat(browser, recording):
    """The conversation is a group chat: you on the right, each member on the
    left; a hand-off is Qumus's message to the member, the member's report is
    its own message with its tool calls folded at the bottom; Focus hides
    everything that is not a message."""
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.click('#rp-speed button[data-s="8"]')
        _wait_finished(page)
        page.wait_for_timeout(300)
        assert page.locator("#feed .msg.me").count() >= 1
        hand = page.locator("#feed .msg.member.handoff")
        assert hand.count() >= 1 and "→" in hand.first.inner_text()
        done = page.locator("#feed .msg.member.returned")
        assert done.count() >= 1
        assert page.locator("#feed .msg.member.busy").count() == 0  # everybody reported back
        # a report folds its tool calls to one line; the line opens them
        fold = page.locator("#feed .msg.member.returned .fold").first
        assert "tool call" in fold.inner_text()
        row = fold.locator("xpath=..").locator(".tc")
        assert not row.first.is_visible()
        fold.click()
        page.wait_for_timeout(200)
        assert row.first.is_visible() and "⎿" in row.first.inner_text()
        # Focus keeps the messages and hides the tool rows; the choice is remembered
        page.click('#feed-mode button[data-m="focus"]')
        page.wait_for_timeout(200)
        assert page.locator("#feed .tools:visible").count() == 0
        assert page.locator("#feed .msg.member.returned:visible").count() >= 1
        page.reload()
        page.wait_for_selector(".rec-intro")
        assert page.locator('#feed-mode button[data-m="focus"].on').count() == 1
        page.click('#feed-mode button[data-m="full"]')
        ctx.close()
        assert not errors, errors


def test_progress_tree_and_problems_panel(browser, recording):
    """The sidebar is one tree: plan step → hardware job → lab action → device
    command. It opens to level 2 by itself; a replay lists the jobs to come,
    folded, at the end; a job row unfolds its actions (rows to seek to); device
    commands are folded until an action's chevron opens them (in a replay the
    row itself seeks). The Problems panel right above the status bar starts
    collapsed, counts the allowed commands, and opens on its tab."""
    with _Server(create_replay_app(recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.wait_for_timeout(300)
        if page.get_attribute("#rp-play", "aria-label") == "Pause":
            page.click("#rp-play")
        assert page.locator("#plan-view .tr.ac-row").count() == 0
        _unfold(page)
        n_actions = page.locator("#plan-view .tr.ac-row").count()
        assert n_actions >= 3 and page.locator("#plan-view .tr.hw").count() >= 1
        assert page.locator("#plan-view .tr.ac-row.todo").count() == n_actions  # nothing ran yet
        # the panel: collapsed, with the counts in its tab
        assert page.is_hidden("#plist")
        assert "Safety:" in page.inner_text("#panel .ptabs")
        page.click('#rp-speed button[data-s="8"]')
        page.click("#rp-play")
        _wait_finished(page)
        page.wait_for_timeout(300)
        # every action has run; the plan steps carry their jobs
        assert page.locator("#plan-view .tr.ac-row.todo").count() == 0
        # device commands stay folded until a step's chevron opens them (review issue 3)
        assert page.locator("#plan-view .tr.cmd").count() == 0
        chev = page.locator("#plan-view .tr.ac-row .chev").first
        chev.click()
        page.wait_for_timeout(200)
        assert page.locator("#plan-view .tr.cmd").count() >= 1
        chev = page.locator("#plan-view .tr.ac-row .chev.open").first
        chev.click()
        page.wait_for_timeout(200)
        assert page.locator("#plan-view .tr.cmd").count() == 0
        # Problems spans the viewport directly above the bottom status bar.
        panel, bar, sb = (page.locator(s).bounding_box() for s in ("#panel", "#replay-bar", ".sb"))
        assert bar["y"] < panel["y"] and abs(panel["y"] + panel["height"] - sb["y"]) < 2
        assert panel["x"] == 0 and panel["width"] == page.viewport_size["width"]
        assert abs(sb["y"] + sb["height"] - page.viewport_size["height"]) < 2
        assert page.locator("#plan-view .tr.l1").count() >= 1
        assert page.locator("#plan-view .tr.hw.ok, #plan-view .tr.hw.fix").count() >= 1
        ok_text = page.inner_text("#pb-ok")
        assert ok_text.startswith("Safety: all ") and ok_text.endswith("checked and allowed")
        allowed = int(re.search(r"[\d,]+", ok_text).group().replace(",", ""))
        assert allowed > 10
        # a job row folds its actions and opens them again
        job = page.locator("#plan-view .tr.hw").first
        before = page.locator("#plan-view .tr.ac-row").count()
        job.click()
        page.wait_for_timeout(200)
        assert page.locator("#plan-view .tr.ac-row").count() < before
        job.click()
        page.wait_for_timeout(200)
        assert page.locator("#plan-view .tr.ac-row").count() == before
        # the panel opens on its tab and lists the problems (or says there are none)
        page.click('#panel .ptab[data-t="problems"]')
        page.wait_for_timeout(200)
        assert page.is_visible("#plist")
        assert page.locator("#plist > .ar:not(.sub)").count() == int(page.inner_text("#pb-n")) or "No unresolved problems" in page.inner_text("#plist")
        page.click("#panel-toggle")
        page.wait_for_timeout(200)
        assert page.is_hidden("#plist")
        ctx.close()
        assert not errors, errors


def test_replay_moves_are_continuous(browser, recording, tmp_path):
    """Owner's items 6 and 11 (2026-10-22): the tape roll and Arm 1 jumped in
    the top view. A device command and its lab frame need not be neighbours
    in the event list (Claude's tool call lands between them while the arm
    moves), and the drawing places the roll from the twin's `rolls`, which
    were not eased. Both must move continuously in lab time."""
    import json
    import shutil

    rec_dir = tmp_path / "moves"
    shutil.copytree(recording, rec_dir)
    data = json.loads((rec_dir / "recording.json").read_text())
    first = dict(data["frames"][0], id=0)
    twin = first["bench"]["twin"]
    arm0, ex0 = twin["arms"]["arm_left"], twin["exfoliator"]
    assert ex0["rolls"]["movable"]["x"] == 347 and arm0["x"] == 307
    t0, w = 1_800_000_000.0, 1_000_000.0
    applied = {**ex0, "gantry_x": -70.0, "gantry_z": -75.0, "roller_x": 277.0, "applied": True,
               "tape": {"x0": 277.0, "z0": 3.0, "x1": 347.0, "z1": -3.0},
               "rolls": {"movable": {"x": 277.0, "z": 3.0}, "fixed": {"x": 347.0, "z": -3.0}}}
    ev = [
        ("device.command", 0.0, {"device": "arm_left", "command": "move_to",
                                 "params": {"pose": {"x": -232, "y": 59, "z": 95, "rotation": 0}}}),
        ("tool.call", 0.0, {"tool": "wait_job", "args": {}, "tool_use_id": "t1"}),
        ("lab.frame", 3.6, {"device": "arm_left", "command": "move_to",
                            "arms": {"arm_left": {**arm0, "x": 75.0, "y": 185.0, "z": 95.0}}}),
        ("device.command", 3.6, {"device": "exfoliator", "command": "apply", "params": {}}),
        ("lab.frame", 8.6, {"device": "exfoliator", "command": "apply", "exfoliator": applied}),
        ("agent.message", 9.0, {"text": "done"}),
    ]
    data["events"] = [{"id": i + 1, "ts": t0 + dt, "wall": w + 0.01 * i, "kind": k, "run_id": None,
                       "agent": "hands", "payload": p} for i, (k, dt, p) in enumerate(ev)]
    data["frames"] = [first]
    (rec_dir / "recording.json").write_text(json.dumps(data))
    with _Server(create_replay_app(rec_dir)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        page.wait_for_timeout(300)
        if page.get_attribute("#rp-play", "aria-label") == "Pause":
            page.click("#rp-play")
        page.click('#rp-speed button[data-s="2"]')
        page.click("#rp-restart")
        # sample the nozzle and the movable roll (SVG units are mm) every 40 ms
        samples = page.evaluate("""async () => {
          const out = [], t0 = performance.now();
          while (performance.now() - t0 < 6500) {
            const n = document.querySelector('#bench .b-nozzle.left');
            const r = document.querySelector('#bench .s-roller.upper');
            out.push([+n.getAttribute('cx'), +n.getAttribute('cy'), +r.getAttribute('x')]);
            await new Promise(res => setTimeout(res, 40));
          }
          return out;
        }""")
        ctx.close()
        assert not errors, errors
    arm = [((a - samples[0][0]) ** 2 + (b - samples[0][1]) ** 2) ** 0.5 for a, b, _ in samples]
    roll = [r - samples[0][2] for _, _, r in samples]
    assert max(arm) > 230 and min(roll) < -65, (arm[-1], roll[-1])  # both got there
    arm_steps = [abs(b - a) for a, b in zip(arm, arm[1:], strict=False)]
    roll_steps = [abs(b - a) for a, b in zip(roll, roll[1:], strict=False)]
    # 239 mm in 1.8 s and 70 mm in 2.5 s: no single 40 ms step covers a big part of it
    assert max(arm_steps) < 40, arm_steps
    assert max(roll_steps) < 15, roll_steps
    assert sum(s > 0.5 for s in arm_steps) >= 10 and sum(s > 0.2 for s in roll_steps) >= 10


def test_demo_contains_its_own_experience_and_single_tool_markers(browser, device_recording):
    """Review regression: one History entry owns its views and image paths."""
    with _Server(create_replay_app(device_recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector('.rec-intro')
        _to_the_end(page)
        page.click('.ab[data-page="history"]')
        page.locator('#hist-index .hrow').first.click()
        page.wait_for_selector('#hist-body .hist-sub')
        page.locator('#hist-body .hist-sub button:has-text("Experience")').click()
        page.wait_for_selector('#hist-body .xp-banner')
        assert 'Testbench runs are simulated' in page.inner_text('#hist-body')
        assert page.is_visible('#hist-body .play-btn')
        page.locator('#hist-body .hist-sub button:has-text("Conversation")').click()
        page.wait_for_selector('#hist-body .tc .tool-state', state='attached')
        assert page.locator('#hist-body .tc .dot').count() == 0
        assert page.locator('#hist-body .tc .tool-state').count() > 0
        assert page.locator('#hist-body .tc .tool-state').first.get_attribute('aria-label')
        page.locator('#hist-body .hist-sub button:has-text("Results")').click()
        page.wait_for_selector('#hist-body .devblock .sample-photo img')
        page.wait_for_function("[...document.querySelectorAll('#hist-body .sample-photo img')].every(i => i.complete && i.naturalWidth > 0)")
        ctx.close()
        assert not errors, errors


def test_restored_history_waits_for_server_mode(browser):
    """A restored History scene must not guess that a replay server is live."""
    folder = v2_recording("openai-device-approval-20261025")
    app = create_replay_app(folder)

    @app.middleware("http")
    async def slow_health(request, call_next):
        if request.url.path == "/api/health":
            await asyncio.sleep(0.3)
        return await call_next(request)

    with _Server(app) as url:
        ctx, page, errors = _page(browser)
        requests = []
        page.on("request", lambda r: requests.append(r.url))
        page.add_init_script("localStorage.setItem('qumus.shell', JSON.stringify({scene:'history'}))")
        page.goto(url)
        page.wait_for_selector('#app[data-scene="history"] #hist-index .hrow >> visible=true')
        page.wait_for_selector('#hist-body .hist-sub')
        assert not any(u.endswith('/api/history') for u in requests)
        ctx.close()
        assert not errors, errors


def test_openai_history_review_navigation_and_reports(browser):
    """Review shipped events, with no model requests or laboratory jobs."""
    folder = v2_recording("openai-device-approval-20261025")
    with _Server(create_replay_app(folder)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector('.rec-intro')
        page.click('#rp-play')
        page.click('#feed-mode button:has-text("Focus")')
        page.click('.ab[data-page="history"]')
        page.locator('#hist-index .hrow').filter(has_text='Build a device with your approval').click()
        assert 'latest hall-bar recordings' in page.inner_text('#hist-index').lower()
        assert 'no current hall-bar recording' in page.inner_text('#hist-index').lower()
        assert 'historical recordings' in page.inner_text('#hist-index').lower()
        assert '$' not in page.inner_text('#hist-index')
        assert 'Playback is free' in page.inner_text('#model-text')
        page.wait_for_selector('#hist-body .hist-sub')
        assert 'device_001' in page.inner_text('#hist-body .ai-words')
        page.locator('#hist-body .hist-sub button:has-text("Conversation")').click()
        page.wait_for_selector('#hist-body .pv-feed-mode')
        assert page.locator('#hist-body .feed.focus').count() == 1
        page.locator('#hist-body .pv-feed-mode button:has-text("Full")').click()
        assert page.locator('#hist-body .feed.focus').count() == 0
        report = page.locator('#hist-body .msg[data-a="hands"]').last
        assert report.locator('.btext').inner_text().count('Simulated stack completed successfully.') == 1
        report.locator('button.more').click()
        assert report.locator('.report.full').count() == 1
        assert 'Post-stack microscope image' in report.inner_text()
        report.locator('.fold').click()
        assert report.locator('.tc:visible').count() == 5
        assert report.locator('.tc > .tool-state').count() == 5
        assert report.locator('.tc .dot').count() == 0
        page.locator('#hist-body .hist-sub button:has-text("Experience")').click()
        page.wait_for_selector('#hist-body .xp-banner')
        assert 'saved with this recording' in page.inner_text('#hist-body')
        assert page.inner_text('#r-title') == 'Experience'
        page.locator('#hist-body .xp-card').first.click()
        pw.expect(page.locator('#r-detail')).to_contain_text('Source')
        page.locator('#hist-body .hist-sub button:has-text("Results")').click()
        page.set_viewport_size({'width': 390, 'height': 844})
        page.locator('#hist-body button:has-text("Key moments")').click()
        assert page.locator('#r-detail .moments').is_visible()
        page.set_viewport_size({'width': 1440, 'height': 900})
        page.locator('#hist-body .play-btn').click()
        page.wait_for_selector('#app[data-scene="progress"] .rec-intro')
        page.wait_for_function("document.getElementById('rp-track').getAttribute('aria-valuenow') < 5")
        page.get_by_role('button', name='Pause', exact=True).click()
        page.locator('#rp-track').press('End')
        page.wait_for_function("document.getElementById('rp-track').getAttribute('aria-valuenow') === '100'")
        ctx.close()
        assert not errors, errors


def test_camera_pixels_target_and_finished_device(browser, device_recording):
    with _Server(create_replay_app(device_recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector('.rec-intro')
        _to_the_end(page)
        page.wait_for_function("[...document.querySelectorAll('#cam .cam img.on')].filter(i => i.naturalWidth > 0).length === 1")
        # Switch back to an already visible frame while a second frame is pending.
        # The late load must not overwrite the requested original image.
        result = page.evaluate("""async () => {
            const {CameraCard} = await import('/static/camera.js');
            const c = new CameraCard({replay: true, lightbox() {}});
            const a = '/api/image?path=first', b = '/api/image?path=second';
            const f = c.frames.microscope;
            const front = f.imgs[0], back = f.imgs[1];
            front.classList.add('on'); front.dataset.url = a;
            // Replace src setters locally so this race check sends no missing-image requests.
            Object.defineProperty(back, 'src', {set() {}});
            c.show(b, 'microscope'); const late = back.onload;
            c.show(a, 'microscope'); late();
            return {front: front.classList.contains('on'), back: back.classList.contains('on')};
        }""")
        assert result == {'front': True, 'back': False}
        _stock(page, 'Target chips')
        page.wait_for_selector('#stock-body .sample-photo img')
        page.wait_for_function("[...document.querySelectorAll('#stock-body .sample-photo img')].every(i => i.complete && i.naturalWidth > 0)")
        ctx.close()
        assert not errors, errors


def test_results_singleton_categories_keep_three_columns(browser, device_recording):
    """Even one layout/plan/device opens a middle entry before right details."""
    with _Server(create_replay_app(device_recording)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        _to_the_end(page)
        page.click('.ab[data-page="results"]')
        for category, detail in [("layouts", ".layout-svg"), ("plans", ".devplan"), ("devices", ".detail-body")]:
            page.click(f'#results-index [data-key="{category}"]')
            page.wait_for_selector("#res-body .report-card")
            assert page.locator("#res-body .report-card").count() == 1
            card = page.locator("#res-body .report-card")
            assert card.locator(".fimg img, .fimg svg").count() == 1
            assert card.bounding_box()["width"] < page.locator("#res-body").bounding_box()["width"] / 2
            assert page.is_visible("#panel")
            pw.expect(page.locator("#r-detail")).to_contain_text("Choose an entry")
            page.click("#res-body .report-card")
            page.wait_for_selector(f"#r-detail {detail}")
        ctx.close()
        assert not errors, errors


def test_review_lab_status_folders_and_independent_tool_rows(browser, tmp_path):
    """Owner review: categories stay left, siblings middle, details right."""
    folder = v2_recording("openai-device-approval-20261025")
    with _Server(create_replay_app(folder)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url + '?at=80')
        page.wait_for_selector('.rec-intro')
        if page.get_attribute('#rp-play', 'aria-label') == 'Pause':
            page.click('#rp-play')
        for tool in ('inspect_bench', 'list_samples', 'get_lab_setup'):
            assert page.locator(f'#plan-view .tr.tool[data-tool="{tool}"]').count() == 1
        assert page.locator('#plan-view .tr.hw.l3').count() == 1
        page.locator('#plan-view .tr.tool[data-tool="inspect_bench"] .chev').click()
        assert page.locator('#plan-view .tr.hw.l3').count() == 0
        assert page.locator('#plan-view .tr.tool[data-tool="list_samples"]').count() == 1
        page.click('.ab[data-page="lab"]')
        assert page.locator('#stock-body .mother-tape-card').count() == 0
        page.locator('#lab-index .drow').filter(has_text='Mother tapes').click()
        cards = page.locator('#stock-body .mother-tape-card')
        pw.expect(cards).to_have_count(2)
        assert 'Click a card' in page.inner_text('#r-detail')
        cards.first.click()
        pw.expect(page.locator('#r-title')).to_contain_text('carrier_')
        first = page.inner_text('#r-title')
        cards.last.click()
        pw.expect(page.locator('#r-title')).not_to_have_text(first)
        assert cards.count() == 2
        page.screenshot(path=str(tmp_path / 'lab-status-mother-tapes.png'))
        page.locator('#lab-index .drow').filter(has_text='Blank chips').click()
        pw.expect(page.locator('#stock-body .mother-tape-card')).to_have_count(0)
        assert page.locator('#stock-body .status-card').count() == 4
        assert 'carrier_' not in page.inner_text('#r-detail')
        page.locator('#lab-index .drow').filter(has_text='Microscope + stage').click()
        page.wait_for_function("document.querySelector('#stock-body .o-h').textContent.includes('Microscope')")
        assert page.locator('#stock-body .status-card').count() >= 2
        page.locator('#stock-body .status-card').first.click()
        pw.expect(page.locator('#r-detail')).to_contain_text('Last command')
        assert not errors, errors
        ctx.close()


def test_review_inspection_preview_matches_overview_and_is_frozen(browser):
    folder = v2_recording("openai-device-approval-20261025")
    with _Server(create_replay_app(folder)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url + '?at=80')
        page.wait_for_selector('.rec-intro')
        if page.get_attribute('#rp-play', 'aria-label') == 'Pause':
            page.click('#rp-play')
        preview = page.locator('#feed .inspection-preview').first
        assert preview.count() == 1
        frozen = preview.locator('svg').evaluate('(el) => el.outerHTML')
        current = page.locator('#cam .overview-diagram').evaluate('(el) => el.outerHTML')
        assert frozen == current
        assert preview.locator('.inspection-originals button').count() >= 1
        page.locator('#rp-track').press('End')
        page.wait_for_function("document.getElementById('rp-track').getAttribute('aria-valuenow') === '100'")
        assert page.locator('#feed .inspection-preview svg').first.evaluate('(el) => el.outerHTML') == frozen
        page.reload()
        page.wait_for_selector('.rec-intro')
        assert page.locator('#feed .inspection-preview svg').first.evaluate('(el) => el.outerHTML') == frozen
        assert not errors, errors
        ctx.close()


@pytest.mark.parametrize("scenario", ["normal", "camera-loss"])
def test_integrated_pair_recording_shows_real_product_or_human_boundary(browser, scenario):
    path = v2_recording(f"experimental-reactor-pair-{scenario}-20261027-v3")
    with _Server(create_replay_app(path)) as url:
        ctx, page, errors = _page(browser)
        page.goto(url)
        page.wait_for_selector(".rec-intro")
        _to_the_end(page)
        page.click('.ab[data-page="results"]')
        if scenario == "normal":
            page.click('#results-index button[data-key="reactor"]')
            page.wait_for_selector("#res-body .fcard")
            assert page.locator("#res-body .fcard").count() == 1
            page.click("#res-body .fcard")
            page.wait_for_selector("#r-detail .fl-big img")
            text = page.inner_text("#r-detail")
            assert "Source flakes" in text and "Evidence job" in text
            assert "Reactor view" in text and "verified reactor product" in text
            assert "Accepted from a two-flake reaction" in text and "10× scan" not in text
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            page.wait_for_function("document.querySelector('#r-detail .fl-big img').naturalWidth > 0")
        else:
            assert page.locator('#results-index button[data-key="reactor"]').count() == 0
            page.wait_for_selector("#res-body h3")
            assert "needs a person" in page.inner_text("#res-body")
        page.click('.ab[data-page="progress"]')
        assert page.locator('#feed .tag.running:visible').count() == 0
        ctx.close()
        assert not errors, errors
