"""Brain half of split save points (qumus2/stations/savepoint.py)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from qumus2.runtime.store import RunStore
from qumus2.stations import config as stations_config
from qumus2.stations.savepoint import (
    SavePointWriter,
    find_savepoint,
    restore_savepoint,
    resume_scenario,
    savable,
    save_savepoint,
)


class FakeClient:
    def __init__(self):
        self.calls = []

    def _request(self, method, path, body=None, *, timeout_s=None, unknown_outcome=False):
        self.calls.append((method, path, body))
        from pathlib import Path

        lab = Path(body["dir"])
        lab.mkdir(parents=True)
        (lab / "checkpoint.json").write_text(json.dumps({"seed": 3}))
        return {"dir": body["dir"], "seed": 3}


class FakeGateway:
    def __init__(self):
        self.tools = []

    async def call(self, agent, tool, args, tool_use_id=None):
        self.tools.append(tool)
        return "ok", None


def _services(tmp_path, stations=None):
    work = tmp_path / "run1"
    work.mkdir()
    store = RunStore(work / "qumus.db")
    store.emit("notification", {"level": "info", "text": "hello"})
    (work / "images" / "microscope").mkdir(parents=True)
    (work / "images" / "microscope" / "m1.jpg").write_bytes(b"jpg")
    (work / "timeline.jsonl").write_text(json.dumps({"id": 1}) + "\n" + json.dumps({"id": 99}) + "\n")
    client = FakeClient()
    st = stations or stations_config.load(None)
    jobs = SimpleNamespace(clients={st.records_from: client}, mirror=None)
    return SimpleNamespace(store=store, jobs=jobs, stations=st, gateway=FakeGateway(),
                           runtime=SimpleNamespace(mode="scripted")), client


def test_save_and_restore_both_halves(tmp_path):
    s, client = _services(tmp_path)
    assert savable(s) is None
    sp = save_savepoint(s, tmp_path / "run1" / "savepoints" / "sp01", label="t", extra={"seed": 3})
    assert client.calls[0][:2] == ("POST", "api/sim/checkpoint")
    assert (sp / "brain" / "qumus.db").exists() and (sp / "lab" / "checkpoint.json").exists()
    meta = json.loads((sp / "savepoint.json").read_text())
    assert meta["seed"] == 3 and meta["lab"]["seed"] == 3
    # timeline cut at the save point's last event
    assert len((sp / "brain" / "timeline.jsonl").read_text().splitlines()) == 1
    assert find_savepoint(tmp_path / "run1") == sp

    later = tmp_path / "run1" / "images" / "microscope" / "m2.jpg"
    later.write_bytes(b"taken by run1 after the save")  # a resumed lab names its next picture m2 too
    out = restore_savepoint(tmp_path / "run1", tmp_path / "run2" / "qumus.db")
    assert not (tmp_path / "run2" / "images" / "microscope" / "m2.jpg").exists()
    assert out["lab_dir"] == sp / "lab"
    restored = RunStore(tmp_path / "run2" / "qumus.db")
    assert restored.last_event_id() == s.store.last_event_id()
    assert (tmp_path / "run2" / "images").is_dir() and not (tmp_path / "run2" / "images").is_symlink()
    assert (tmp_path / "run2" / "images" / "microscope" / "m1.jpg").read_bytes() == b"jpg"
    with pytest.raises(RuntimeError):
        restore_savepoint(sp, tmp_path / "run2" / "qumus.db")


def test_open_job_or_real_station_refuses(tmp_path):
    s, _ = _services(tmp_path)
    run = s.store.create_run("search_chip", {}, project_id=None) if hasattr(s.store, "create_run") else None
    if run is not None:
        with pytest.raises(RuntimeError, match="jobs still open"):
            save_savepoint(s, tmp_path / "sp")
    st = stations_config.load(None)
    real = SimpleNamespace(**{**st.__dict__})
    real.modules = {m: SimpleNamespace(kind="real" if m == "stacking" else "sim", url=v.url)
                    for m, v in st.modules.items()}
    s.stations = real
    assert savable(s) == "a station is real"


def test_writer_saves_before_flake_candidates_only(tmp_path):
    s, client = _services(tmp_path)
    w = SavePointWriter(s, tmp_path / "run1" / "savepoints", log=lambda *_: None)

    async def go():
        await s.gateway.call("hands", "wait_job", {})
        await s.gateway.call("picker", "get_flake_candidates", {})

    asyncio.run(go())
    assert len(client.calls) == 1 and len(w.saved) == 1


def test_resume_scenario_starts_at_picker():
    sc = {"name": "x", "turns": [{"actions": [{"say": {"text": "a"}}, {"delegate": {"to": "hands"}},
                                              {"delegate": {"to": "picker"}}, {"delegate": {"to": "hands"}}]}]}
    out = resume_scenario(sc)
    assert out["turns"][0]["actions"][1] == {"delegate": {"to": "picker"}}
    assert out["turns"][0]["actions"][0]["say"]["agent"] == "orchestrator"


def test_restore_copies_imported_targets(tmp_path):
    s, _ = _services(tmp_path)
    t = tmp_path / "run1" / "targets" / "abc"
    t.mkdir(parents=True)
    (t / "target.json").write_text("{}")
    save_savepoint(s, tmp_path / "run1" / "savepoints" / "sp01")
    restore_savepoint(tmp_path / "run1", tmp_path / "run2" / "qumus.db")
    copied = tmp_path / "run2" / "targets" / "abc" / "target.json"
    assert copied.is_file() and not (tmp_path / "run2" / "targets").is_symlink()


def test_resume_scenario_can_redo_the_plan():
    sc = {"name": "x", "turns": [{"actions": [
        {"say": {"text": "a"}}, {"delegate": {"to": "advisor"}}, {"tool": {"agent": "advisor", "name": "p"}},
        {"done": {"agent": "advisor"}}, {"delegate": {"to": "hands"}}, {"delegate": {"to": "picker"}}]}]}
    acts = resume_scenario(sc, redo_plan=True)["turns"][0]["actions"]
    assert [list(a)[0] for a in acts] == ["say", "delegate", "tool", "done", "delegate"]
    assert acts[-1] == {"delegate": {"to": "picker"}}


def test_restore_without_picture_list_uses_file_times(tmp_path):
    import os

    s, _ = _services(tmp_path)
    sp = save_savepoint(s, tmp_path / "run1" / "savepoints" / "sp01")
    (sp / "brain" / "images.json").unlink()
    meta = json.loads((sp / "savepoint.json").read_text())
    later = tmp_path / "run1" / "images" / "microscope" / "m2.jpg"
    later.write_bytes(b"later")
    os.utime(later, (meta["saved_wall"] + 60, meta["saved_wall"] + 60))
    restore_savepoint(sp, tmp_path / "run2" / "qumus.db")
    assert (tmp_path / "run2" / "images" / "microscope" / "m1.jpg").exists()
    assert not (tmp_path / "run2" / "images" / "microscope" / "m2.jpg").exists()


def test_mirror_maps_pictures_of_earlier_runs_back_to_image_ids(tmp_path):
    from qumus2.stations.mirror import ImageCache

    s, _ = _services(tmp_path)
    save_savepoint(s, tmp_path / "run1" / "savepoints" / "sp01")
    restore_savepoint(tmp_path / "run1", tmp_path / "run2" / "qumus.db")
    cache = ImageCache(tmp_path / "run2" / "images", None)
    old = str((tmp_path / "run1" / "images" / "microscope" / "m1.jpg").resolve())
    assert cache.to_remote({"image": {"path": old}}) == {"image": {"path": "qumus-image:microscope/m1.jpg"}}
