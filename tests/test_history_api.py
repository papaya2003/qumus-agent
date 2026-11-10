"""Sessions, "this run" and the History endpoints (UI workstream).

A session is one `serve`: the events after the id the store recorded when it
began. The page's "this run" is the current session; /api/history lists the
earlier ones and /api/history/{id}/recording plays one in the page."""

import asyncio
import json

from conftest import v2_recording
from fastapi.testclient import TestClient

from qumus2.app import build_services
from qumus2.ui.app import create_app
from qumus2.ui.recording import attach_recorder, read_timeline, summarize_events, timeline_path


def _session(db, text="Put a chip on the transfer stage", record=True):
    """One scripted serve that runs a request to its end."""
    s = build_services(db, scenario="place_chip_demo", seed=2, step_delay_s=0.0)
    rec = attach_recorder(s, poll_s=0.02, bench_every_s=0.05) if record else None
    s.store.add_instruction(text, project_id=s.project_id)
    asyncio.run(s.driver.wait_idle(timeout=120))
    if rec:
        rec.stop()
    return s


def test_a_new_store_has_no_history(tmp_path):
    s = build_services(tmp_path / "qumus.db", scenario="place_chip_demo", seed=2)
    assert s.session_start == 0 and s.store.sessions() == [0]
    with TestClient(create_app(s, start_driver=False, recordings_dir=tmp_path / "none")) as c:
        assert c.get("/api/history").json() == []
        assert c.get("/api/snapshot").json()["session_start"] == 0
        assert c.get("/api/history/0/recording").status_code == 404
        assert c.get("/api/recordings").json() == []


def test_history_preserves_multiple_executed_models(tmp_path):
    db = tmp_path / 'run.db'
    first = build_services(db, seed=2)
    first.store.add_instruction('Compare models', project_id=first.project_id)
    for model in ['openai/test', 'anthropic/test']:
        first.store.emit('agent.turn_end', {'provider': 'openai', 'model': model})
    first.store.emit('model.changed', {'model': 'not-executed'})
    first.store.close()
    second = build_services(db, seed=2)
    with TestClient(create_app(second, start_driver=False)) as c:
        row = c.get('/api/history').json()[0]
        assert row['models'] == ['anthropic/test', 'openai/test']
        rec = c.get(f"/api/history/{row['id']}/recording").json()
        assert rec['meta']['models'] == row['models']


def test_earlier_sessions_are_history(tmp_path):
    db = tmp_path / "run" / "qumus.db"
    first = _session(db)
    end_first = first.store.last_event_id()
    first.store.close()
    second = build_services(db, scenario="place_chip_demo", seed=2)
    # the new session starts after everything the first one wrote
    assert second.session_start == end_first
    assert second.store.sessions() == [0, end_first]
    assert second.store.run_ids_since(second.session_start) == set()
    with TestClient(create_app(second, start_driver=False)) as c:
        rows = c.get("/api/history").json()
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == 0 and row["until"] == end_first
        assert row["request"] == "Put a chip on the transfer stage"
        assert row["outcome"] == "done" and row["jobs"] == 2 and row["agents"] == "scripted"
        assert row["started"] is not None and row["ended"] >= row["started"]
        rec = c.get("/api/history/0/recording").json()
        ids = [e["id"] for e in rec["events"]]
        assert ids and max(ids) <= end_first
        assert all("wall" in e for e in rec["events"])
        assert rec["meta"]["source"] == "this computer"
        assert any(e["kind"] == "user.message" for e in rec["events"])
        # nothing of this session waits for a person; the first session's jobs are not this run's
        assert c.get("/api/health").json()["waiting"] == 0


def test_a_session_without_a_request_is_not_listed(tmp_path):
    db = tmp_path / "run" / "qumus.db"
    build_services(db, scenario="place_chip_demo", seed=2).store.close()  # opened and closed
    s = build_services(db, scenario="place_chip_demo", seed=2)
    with TestClient(create_app(s, start_driver=False)) as c:
        assert c.get("/api/history").json() == []


def test_history_wall_times_come_from_their_own_serve(tmp_path):
    """A run store deleted and started again reuses event ids; the timeline file
    keeps the old serve's entries. Each session reads its own segment, from its
    start mark to the next one."""
    db = tmp_path / "run" / "qumus.db"
    first = _session(db)
    end_first = first.store.last_event_id()
    first.store.close()
    path = timeline_path(db)
    # an older serve of a deleted store: same ids, other wall times
    old = [{"id": 0, "wall": 1000.0, "start": True}, {"id": 1, "wall": 1001.0}, {"id": 2, "wall": 1002.0}]
    path.write_text("".join(json.dumps(t) + "\n" for t in old) + path.read_text(encoding="utf-8"), encoding="utf-8")
    second = build_services(db, scenario="place_chip_demo", seed=2)
    with TestClient(create_app(second, start_driver=False)) as c:
        row = c.get("/api/history").json()[0]
        mine = [t["wall"] for t in read_timeline(path)[len(old):] if not t.get("start")]
        assert row["started"] == min(mine) and row["started"] > 1002.0
        walls = [e["wall"] for e in c.get("/api/history/0/recording").json()["events"]]
        assert min(walls) > 1002.0
    assert second.session_start == end_first


def test_summarize_events_rules():
    def ev(i, kind, payload=None, run_id=None, ts=None):
        return {"id": i, "kind": kind, "payload": payload or {}, "run_id": run_id, "ts": ts if ts is not None else i}

    seeding = [ev(1, "sample.updated", {"kind": "chip", "id": "chip_001", "status": "exfoliated"})]
    asked = [ev(2, "user.message", {"text": "Make a device"}),
             ev(3, "plan.updated", {"plan": [{"id": "s1", "status": "done"}, {"id": "s2", "status": "skipped"}]}),
             ev(4, "job.submitted", run_id="run_1"), ev(5, "job.status", {"status": "succeeded"}, run_id="run_1"),
             ev(6, "sample.updated", {"kind": "device", "id": "device_001"}),
             ev(7, "agent.turn_end", {"cost_usd": 0.25}, ts=30)]
    out = summarize_events(seeding + asked)
    assert out["request"] == "Make a device" and out["outcome"] == "done"
    assert out["devices"] == ["device_001"] and out["chips"] == 0  # the seeding is not the run's
    assert out["jobs"] == 1 and out["cost_usd"] == 0.25 and out["lab_s"] == 29.0
    waiting = asked[:4] + [ev(5, "job.status", {"status": "waiting_human"}, run_id="run_1")]
    assert summarize_events(waiting)["outcome"] == "needs a person"
    assert summarize_events([ev(1, "user.message", {"text": "hi"})])["outcome"] == "answered"
    assert summarize_events(asked[:4] + [ev(9, "system.stopped")])["outcome"] == "stopped"
    assert summarize_events([])["outcome"] == "nothing asked"


def test_lab_time_skips_jumps_between_clocks():
    """A store older than the session marks holds several serves: wall-time
    stamps (about 1.79e9) then a testbench clock from 1.8e9. The jump between
    them (about 2000 h) is not lab work; only the clocks' own steps count."""
    def ev(i, ts):
        return {"id": i, "kind": "device.command", "payload": {}, "run_id": None, "ts": ts}

    wall, sim = 1_792_604_820.0, 1_800_000_000.0
    events = [ev(1, wall), ev(2, wall + 30), ev(3, sim), ev(4, sim + 120), ev(5, sim + 60), ev(6, sim + 90)]
    assert summarize_events(events)["lab_s"] == 30 + 120 + 30
    assert summarize_events([ev(1, sim)])["lab_s"] == 0.0


def test_review_after_recovered_job_keeps_the_failed_job_evidence():
    """An explicit human review is the current state, not a clean success."""

    data = json.loads((v2_recording("openai-collaboration-20261025") / "recording.json").read_text(encoding="utf-8"))
    summary = summarize_events(data["events"])
    assert summary["outcome"] == "awaiting review"
    assert summary["jobs"] == 7 and summary["flakes"] == 5
    assert any(e["kind"] == "job.status" and e["payload"].get("status") == "failed"
               for e in data["events"])

    def event(kind, payload, **extra):
        return {"kind": kind, "payload": payload, **extra}

    plan = [{"owner": "hands", "status": "done"}, {"owner": "human", "status": "running"}]
    base = [event("user.message", {"text": "Show me the flakes"}),
            event("plan.updated", {"plan": plan}),
            event("job.status", {"status": "failed"}, run_id="old")]
    assert summarize_events(base)["outcome"] == "awaiting review"
    # A planned review must not disguise active work, errors or parked jobs.
    plan[-1]["status"] = "pending"
    assert summarize_events(base)["outcome"] == "failed"
    plan[-1]["status"] = "running"
    assert summarize_events(base + [event("job.status", {"status": "running"}, run_id="new")])["outcome"] != "awaiting review"
    assert summarize_events(base + [event("agent.error", {"error": "offline"})])["outcome"] == "error"
    assert summarize_events(base + [event("job.status", {"status": "parked"}, run_id="new")])["outcome"] == "needs a person"
