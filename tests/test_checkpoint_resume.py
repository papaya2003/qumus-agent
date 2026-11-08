"""Save points: resume a testbench run after the search (testbench/checkpoint.py)."""

import asyncio
import json

import pytest

from qumus2.agents.scripted import load_scenario
from qumus2.app import build_services
from qumus2.testbench import checkpoint as cp


def test_resume_scenario_starts_at_the_first_picker_delegation():
    full = load_scenario("hallbar_device")
    resumed = cp.resume_scenario(full, say="resuming")
    actions = resumed["turns"][0]["actions"]
    assert actions[0] == {"say": {"agent": "orchestrator", "text": "resuming"}}
    assert actions[1]["delegate"]["to"] == "picker"
    assert actions[2] == {"pick_flakes": {"save_as": "pick"}}
    original = full["turns"][0]["actions"]
    assert actions[1:] == original[original.index(actions[1]):]
    # nothing before the picker (plan, exfoliation, search) runs again
    assert not any("exfoliate" in json.dumps(a) for a in actions[1:3])


def test_restore_refuses_a_save_point_whose_search_code_changed(tmp_path, monkeypatch):
    src = tmp_path / "run"
    point = src / "checkpoints" / "cp01-ev5"
    point.mkdir(parents=True)
    for name in ("qumus.db", "lab_state.pkl"):
        (point / name).write_bytes(b"x")
    (src / "timeline.jsonl").write_text("".join(json.dumps({"id": i, "wall": i}) + "\n"
                                                for i in (1, 5, 7)))
    saved = cp.guarded_fingerprint()
    saved[cp.GUARDED[0]] = "older"
    (point / "checkpoint.json").write_text(json.dumps({
        "version": cp.VERSION, "workspace": str(src), "last_event_id": 5,
        "timeline_bytes": (src / "timeline.jsonl").stat().st_size, "guarded": saved}))
    with pytest.raises(cp.StaleCheckpoint) as e:
        cp.restore_checkpoint(src, tmp_path / "new")
    assert e.value.changed == [cp.GUARDED[0]]
    assert not (tmp_path / "new" / "qumus.db").exists()
    got = cp.restore_checkpoint(src, tmp_path / "new", accept_changed=[cp.GUARDED[0]])
    assert got["changed"] == [cp.GUARDED[0]]
    assert got["photo_root"] == src
    # only the timeline up to the save point comes along
    lines = (tmp_path / "new" / "timeline.jsonl").read_text().splitlines()
    assert [json.loads(x)["id"] for x in lines] == [1, 5]
    assert json.loads((tmp_path / "new" / "resumed_from.json").read_text())["changed_accepted"] == [
        cp.GUARDED[0]]
    with pytest.raises(RuntimeError, match="already has a run store"):
        cp.restore_checkpoint(src, tmp_path / "new", accept_changed=["all"])


def _summary(s):
    state = s.store.get_project(s.project_id)["state"]
    return {"selected": state.get("selected_flakes"), "design": state.get("design_accepted"),
            "devices": [(d["id"], d["status"]) for d in s.store.list_samples(kind="device")]}


def test_resumed_run_finishes_like_the_unbroken_run(tmp_path):
    """graphene_device: save before each pick, resume from the last one."""
    s = build_services(tmp_path / "a" / "qumus.db", scenario="graphene_device", seed=3)
    writer = cp.CheckpointWriter(s, tmp_path / "a" / "checkpoints", log=lambda m: None)
    asyncio.run(s.runtime.send("go"))
    unbroken = _summary(s)
    assert unbroken["devices"] == [("device_001", "stacked")]
    assert len(writer.saved) == 1  # keep=1: only the newest stays
    point = cp.find_checkpoint(tmp_path / "a")
    meta = json.loads((point / "checkpoint.json").read_text())
    s.store.close()

    got = cp.restore_checkpoint(tmp_path / "a", tmp_path / "b")
    scenario = cp.resume_scenario(load_scenario("graphene_device"), say="resuming")
    r = build_services(tmp_path / "b" / "qumus.db", scenario=scenario, seed=3,
                       state_path=got["state_path"])
    # the search is not repeated
    assert len(r.store.list_samples(kind="flake")) > 0
    asyncio.run(r.runtime.send("go"))
    assert _summary(r) == unbroken
    tools = [t for _, t, _ in r.runtime.results]
    assert "exfoliate_chips" not in tools and "search_chip" not in tools
    assert tools[0] == "get_flake_candidates"
    # the records before the save point are the original run's
    first = r.store.events_after(0, limit=meta["last_event_id"])
    assert first[-1].id == meta["last_event_id"]
