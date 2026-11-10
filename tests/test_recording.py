"""Recorder, export and the replay server (UI workstream)."""

import asyncio
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from qumus2.app import build_services
from qumus2.ui.recording import (
    attach_recorder,
    check_recording,
    export_recording,
    load_recording,
    read_timeline,
    scrub_text,
    timeline_path,
)
from qumus2.ui.replay import create_replay_app, find_recordings, resolve_recording


def _record(tmp_path, monkeypatch, scenario="place_chip_demo"):
    monkeypatch.setenv("QUMUS_ANTHROPIC_API_KEY", "sk-test-0123456789abcdefghij")
    db = tmp_path / "run" / "qumus.db"
    s = build_services(db, scenario=scenario, seed=2)
    rec = attach_recorder(s, poll_s=0.02, bench_every_s=0.05)
    s.store.emit("fault.injected", {"fault": "next_placement", "params": {"dx": 2.0}})
    # something secret-looking and a home path, as a careless tool might log them
    s.store.emit("notification", {"level": "info", "text": "key sk-test-0123456789abcdefghij "
                                  "in /Users/someone/Projects/x"})
    s.store.add_instruction("Put a chip on the transfer stage", project_id=s.project_id)
    asyncio.run(s.driver.wait_idle(timeout=60))
    rec.stop()
    return s, db


def test_recorder_and_export(tmp_path, monkeypatch):
    s, db = _record(tmp_path, monkeypatch)
    tl = read_timeline(timeline_path(db))
    assert tl[0]["start"] and "layout" in tl[0]
    frames = [t for t in tl if "bench" in t]
    assert frames and {"devices", "objects", "busy"} <= set(frames[-1]["bench"])
    # the last frame is the settled state after the last event
    assert frames[-1]["id"] == s.store.last_event_id()

    out = export_recording(db, tmp_path / "rec", title="t")
    rec = load_recording(out)
    assert rec["meta"]["agents"] == "scripted" and rec["meta"]["title"] == "t"
    assert rec["layout"] and rec["frames"] and rec["runs"]
    assert all(e["wall"] is not None for e in rec["events"])
    walls = [e["wall"] for e in rec["events"]]
    assert walls == sorted(walls)
    # photos copied into media/ and every reference points there
    imgs = [i for e in rec["events"] for i in (e["payload"].get("images") or [])]
    assert imgs and all(i.startswith("media/") and (out / i).is_file() for i in imgs)
    text = (out / "recording.json").read_text()
    assert "sk-test" not in text and "/Users/someone" not in text
    assert "[redacted]" in text
    assert check_recording(out) == []
    (out / imgs[0]).unlink()
    assert any("photo" in x for x in check_recording(out))


def test_scrub_text():
    assert scrub_text("Bearer abcdefghijklmnopqrstuvwxyz", []) == "[redacted]"
    assert scrub_text("x secretvalue123 y", ["secretvalue123"]) == "x [redacted] y"
    assert scrub_text("/home/jialei/runs/a.png", []) == "~/runs/a.png"


def test_request_length_events_survive_recording_export(tmp_path):
    db = tmp_path / "run.db"
    services = build_services(db, seed=1)
    metrics = {"request_id": "request-one", "call_id": "call-one", "session_id": "session-one",
               "text_chars": 12345, "instructions_chars": 1000, "conversation_chars": 10000,
               "tools_chars": 1345, "image_count": 2, "measurement": "outgoing_text_v1",
               "unit": "unicode_code_points", "model": "test-model", "attempt": 1}
    services.store.emit("agent.request", metrics, agent="eyes")
    from qumus2.agents.token_usage import reported_usage

    usage = {**reported_usage({'input_tokens': 4000, 'output_tokens': 100}),
             'request_id': 'request-one', 'call_id': 'call-one', 'session_id': 'session-one',
             'context_window_tokens': 32768, 'model': 'test-model'}
    services.store.emit('agent.request_usage', usage, agent='eyes')
    unknown = {**reported_usage(None), 'request_id': 'request-two', 'context_window_tokens': None}
    services.store.emit('agent.request_usage', unknown, agent='hands')
    out = export_recording(db, tmp_path / "recording")
    rec = load_recording(out)
    points = [e for e in rec["events"] if e["kind"] == "agent.request"]
    assert len(points) == 1 and points[0]["payload"] == metrics
    assert points[0]["agent"] == "eyes"
    assert [e['payload'] for e in rec['events'] if e['kind'] == 'agent.request_usage'] == [usage, unknown]


def test_replay_server(tmp_path, monkeypatch):
    _, db = _record(tmp_path, monkeypatch)
    out = export_recording(db, tmp_path / "recordings" / "one")
    assert find_recordings(tmp_path / "recordings") == [out]
    # an export cut off before its final rename is never picked
    shutil.copytree(out, tmp_path / "recordings" / "two.partial")
    assert find_recordings(tmp_path / "recordings") == [out]
    assert resolve_recording(out) == out
    c = TestClient(create_replay_app(out))
    assert c.get("/api/health").json()["mode"] == "replay"
    rec = c.get("/api/recording").json()
    assert rec["format"] == "qumus2-recording"
    img = next(i for e in rec["events"] for i in (e["payload"].get("images") or []))
    assert c.get("/api/image", params={"path": img}).status_code == 200
    for bad in ("recording.json", "../run/qumus.db", "/etc/passwd", "media/../recording.json"):
        assert c.get("/api/image", params={"path": bad}).status_code == 404
    # the page, its scripts and the photos are revalidated on every load, so a
    # browser never mixes files cached before a `git pull` with new ones
    for url in ("/", "/static/app.js", "/static/style.css"):
        r = c.get(url)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", url
        if url.startswith("/static/"):  # unchanged files: a short "not modified"
            assert c.get(url, headers={"if-none-match": r.headers["etag"]}).status_code == 304
    assert c.get("/api/image", params={"path": img}).headers["cache-control"] == "no-cache"
    assert [r["name"] for r in c.get("/api/recordings").json()] == ["one"]
    assert json.loads((out / "recording.json").read_text())["meta"]["n_events"] == len(rec["events"])


def test_the_old_replay_command_is_gone():
    """Jialei 2026-11-06: recordings are watched only in History; the old
    command-line replay entry was removed."""
    from qumus2.cli import main

    with pytest.raises(SystemExit) as result:
        main(["replay", "--list"])
    assert result.value.code == 2


def test_nested_sample_photos_and_provider_metadata(tmp_path, monkeypatch):
    from qumus2.ui.replay import recording_row

    s, db = _record(tmp_path, monkeypatch)
    s.store.emit('agent.turn_end', {'provider': 'openai', 'model': 'anthropic/test', 'is_error': False,
                                  'cost_usd': 1.25, 'cost_basis': 'configured_token_rates'})
    out = export_recording(db, tmp_path / 'rec', meta={'agents': 'sdk', 'provider': 'openai'})
    row = recording_row(out)
    assert row['provider'] == 'openai'
    assert row['models'] == ['anthropic/test']  # model vendor is independent of the SDK adapter
    assert row['cost_basis'] == 'configured_token_rates'
    rec = load_recording(out)
    rec['runs']['nested-test'] = {'result': {'device': {'props': {
        'images': {'final': 'media/missing-device.png'}}}}}
    (out / 'recording.json').write_text(json.dumps(rec))
    assert any('missing-device.png' in problem for problem in check_recording(out))
