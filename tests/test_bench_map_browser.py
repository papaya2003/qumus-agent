"""The two virtual bench views share geometry, state and physical scale."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402
from test_ui_browser import _page, _Server  # noqa: E402
from test_ui_browser import browser as shared_browser  # noqa: E402

from qumus2.app import build_services  # noqa: E402
from qumus2.ui.app import create_app  # noqa: E402
from tests.conftest import quiet_bench  # noqa: E402

browser = shared_browser


@pytest.mark.parametrize("with_reactor", [False, True, "hallbar-linked"])
def test_virtual_map_geometry_matches_progress_through_resize_and_evidence_switch(browser, tmp_path, with_reactor):
    config = quiet_bench()
    if with_reactor == "hallbar-linked":
        from qumus2.config import simulation_profile

        config = simulation_profile("hallbar-linked")
    elif with_reactor:
        config["reactor"] = {"enabled": True, "synthetic_prepared_pairs": False}
    services = build_services(tmp_path / "bench.db", seed=7, testbench_config=config)
    screenshot_dir = Path(os.environ.get("QUMUS_B1_SCREENSHOT_DIR", str(tmp_path)))
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    prefix = "b1-integrated" if with_reactor else "b1"
    geometry = """selector => {
      const root = document.querySelector(selector), matrix = root.getScreenCTM();
      return {box: root.getAttribute('viewBox'), scale: [matrix.a, matrix.d],
        objects: [...root.querySelectorAll('.b-obj')].map(node => ({
          title: node.querySelector('title').textContent,
          transform: node.getAttribute('transform'),
          size: ['width', 'height'].map(key => +node.querySelector('rect').getAttribute(key))
        })), stage: root.querySelector('.s-stage.moving')?.parentNode.getAttribute('transform')};
    }"""
    with _Server(create_app(services, start_driver=False)) as url:
        ctx, page, errors = _page(browser)
        try:
            page.goto(url)
            page.wait_for_function("document.querySelectorAll('#bench .b-obj').length > 0")
            progress = page.evaluate(geometry, "#bench")
            page.locator("#lab").screenshot(path=str(screenshot_dir / f"{prefix}-progress-map.png"))
            page.click('.ab[data-page="lab"]')
            page.get_by_role("button", name="Bench overview", exact=True).click()
            diagnostics = page.locator("#needs").get_by_role("button", name="Simulation diagnostics", exact=True)
            expect(diagnostics).to_have_attribute("aria-pressed", "true")
            expect(page.locator("#needs")).to_contain_text("laboratory positions remain uncalibrated")
            page.wait_for_function("document.querySelectorAll('#labmap .b-obj').length > 0")
            lab = page.evaluate(geometry, "#labmap")
            assert {key: lab[key] for key in ("box", "objects", "stage")} == {
                key: progress[key] for key in ("box", "objects", "stage")
            }
            assert lab["scale"][0] == pytest.approx(lab["scale"][1])
            if with_reactor == "hallbar-linked":
                assert services.lab.bench["geometry_profile"]["id"] == page.locator("#labmap").get_attribute("data-geometry-profile")
                assert page.locator("#bench").get_attribute("data-geometry-profile") == page.locator("#labmap").get_attribute("data-geometry-profile")
            world = {obj["id"]: obj for obj in services.lab.world.snapshot()}
            for obj in lab["objects"]:
                sample_id = obj["title"].split(" · ", 1)[0]
                assert obj["size"] == world[sample_id]["size_mm"]
            page.locator("#v-lab").screenshot(path=str(screenshot_dir / f"{prefix}-lab-map-wide.png"))
            page.set_viewport_size({"width": 1000, "height": 900})
            resized = page.evaluate(geometry, "#labmap")
            assert resized["box"] == lab["box"]
            assert resized["objects"] == lab["objects"]
            assert resized["scale"][0] == pytest.approx(resized["scale"][1])
            page.locator("#v-lab").screenshot(path=str(screenshot_dir / f"{prefix}-lab-map-narrow.png"))
            page.locator("#needs").get_by_role("button", name="Observed evidence", exact=True).click()
            assert page.locator("#labmap .b-obj").count() == 0
            expect(page.locator("#v-lab .expand-bench")).to_be_disabled()
            diagnostics.click()
            restored = page.evaluate(geometry, "#labmap")
            assert restored["objects"] == lab["objects"]
            assert restored["stage"] == lab["stage"]
            assert not errors, errors
        finally:
            ctx.close()
