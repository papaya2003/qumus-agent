"""Scripted runtime + UI API: the stage-1 demo path without an API key (spec 11.5)."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from qumus2.ui.app import create_app


def test_scenario_runs_through_driver_and_takes_new_instruction(services):
    s = services
    s.lab.world.faults.next_placement = {"dx": 2.0, "drot": 5.0}

    async def go():
        s.store.add_instruction("Put a chip on the transfer stage", project_id=s.project_id)
        await s.driver.wait_idle(timeout=20)
        s.store.add_instruction("Change the plan a bit", project_id=s.project_id)
        await s.driver.wait_idle(timeout=20)

    asyncio.run(go())
    proj = s.store.get_project(s.project_id)
    assert proj["plan_revision"] == 2
    texts = [e.payload.get("text", "") for e in s.store.events_after(0, limit=10_000)
             if e.kind == "agent.message"]
    assert any("re-placed it automatically" in t for t in texts)
    assert s.store.get_sample("chip_001")["location"] == "transfer_stage"


def test_stop_instruction_interrupts_and_stops_jobs(services):
    s = services
    called = {}
    s.jobs.stop_all = lambda: called.setdefault("stop", True)  # type: ignore[method-assign]

    async def go():
        s.store.add_instruction("STOP", kind="stop", project_id=s.project_id)
        await s.driver.tick()

    asyncio.run(go())
    assert called.get("stop")


def test_ui_only_endpoints(services, tmp_path):
    (tmp_path / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    app = create_app(services, start_driver=False, data_dir=tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/plan/history").json() == []
        assert c.post("/api/sim", json={"speed": 4}).json()["speed"] == 4
        assert c.post("/api/sim", json={"speed": 100}).status_code == 422
        assert c.get("/api/image", params={"path": "frame.png"}).status_code == 200
        assert c.get("/api/image", params={"path": "secret.txt"}).status_code == 404
        assert c.get("/api/image", params={"path": "../x.png"}).status_code == 404
        assert c.get("/static/app.js").status_code == 200
        assert "feed.js" in c.get("/static/app.js").text
