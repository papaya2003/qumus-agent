"""Scripted runtime + UI API: the stage-1 demo path without an API key (spec 11.5)."""

from __future__ import annotations

import asyncio
import os

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


def test_ui_api(services):
    with TestClient(create_app(services, start_driver=False)) as c:
        health = c.get("/api/health").json()
        laboratory = health.pop("laboratory")
        assert health.pop("pid") == os.getpid()   # the launcher identifies the server by it
        assert health == {"ok": True, "mode": "scripted", "busy": False,
                                                 "workspace": "Previous two-arm lab",
                                                 "model_status": {"state": "scripted"},
                                                 "cost_usd": 0.0,  # nothing spent: scripted agents
                                                 "waiting": 0}  # no job waits for a person
        assert laboratory == c.get("/api/laboratory").json()
        assert laboratory["mode"] == "virtual" and laboratory["backend"] == "qumus2.sim"
        modes = {mode["id"]: mode for mode in laboratory["modes"]}
        assert modes["virtual"]["available"] is True and modes["real"]["available"] is False
        assert laboratory["learning"]["enabled"] is False
        assert laboratory["learning"]["status"] == "disabled_virtual"
        snap = c.get("/api/snapshot").json()
        assert {a["name"] for a in snap["agents"]} >= {"orchestrator", "hands"}
        assert len(snap["world"]) >= 3  # chips + carriers of the testbench
        iid = c.post("/api/messages", json={"text": "hi"}).json()["instruction_id"]
        assert iid.startswith("ins_")
        evs = c.get("/api/events", params={"after": 0}).json()
        assert evs[-1]["kind"] == "user.message"
        assert c.post("/api/faults", json={"fault": "next_placement",
                                            "params": {"dx": 1}}).json() == {"ok": True}
        assert services.lab.world.faults.next_placement == {"dx": 1.0}
        assert c.get("/api/runs/nope").status_code == 404
        assert c.post("/api/stop").status_code == 200
        assert "Qumus" in c.get("/").text
        with c.websocket_connect("/ws/events?after=0") as ws:
            assert "kind" in ws.receive_json()
        assert not services.store.list_runs()  # Driver-off API reads never dispatch a lab job.


def test_ui_only_endpoints(services, tmp_path):
    (tmp_path / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "secret.txt").write_text("no")
    app = create_app(services, start_driver=False, data_dir=tmp_path)
    with TestClient(app) as c:
        b = c.get("/api/bench").json()
        assert "transfer_stage" in b["layout"]["regions"]
        arm = next(d for d in b["devices"] if d["id"] == "arm_right")
        assert arm["pose"]["z"] > 0 and arm["offline"] is False
        assert len(b["objects"]) >= 3 and b["busy"] is False  # chips + carriers
        stamps = [o for o in b["objects"] if o["kind"] == "stamp"]
        assert stamps and all(o["status"] == "fresh" for o in stamps)
        assert b["stamp"]["held"] is None and set(b["stamp"]["manipulator"]) >= {"x", "z"}
        manip = next(d for d in b["devices"] if d["id"] == "transfer_arm")
        assert set(manip["positions"]) == {"x", "y", "z"}
        c.post("/api/faults", json={"fault": "pickup_fail_next", "params": {"count": 1}})
        assert c.get("/api/bench").json()["faults"]["armed"]["pickup_fail_next"] == 1
        c.post("/api/faults", json={"fault": "device_offline",
                                     "params": {"device": "arm_right"}})
        arm = next(d for d in c.get("/api/bench").json()["devices"] if d["id"] == "arm_right")
        assert arm["offline"] is True
        c.post("/api/faults", json={"fault": "stuck_at",
                                     "params": {"device": "exfoliation_heater", "value": 22.0}})
        heater = next(d for d in c.get("/api/bench").json()["devices"]
                      if d["id"] == "exfoliation_heater")
        assert heater["stuck_at"] == 22.0
        assert c.post("/api/faults", json={"fault": "stuck_at",
                                            "params": {"device": "overview_camera"}}).status_code == 400
        assert c.get("/api/plan/history").json() == []
        assert c.post("/api/sim", json={"speed": 4}).json()["speed"] == 4
        assert c.post("/api/sim", json={"speed": 100}).status_code == 422
        assert c.get("/api/image", params={"path": "frame.png"}).status_code == 200
        assert c.get("/api/image", params={"path": "secret.txt"}).status_code == 404
        assert c.get("/api/image", params={"path": "../x.png"}).status_code == 404
        assert c.get("/static/app.js").status_code == 200
        assert "feed.js" in c.get("/static/app.js").text
