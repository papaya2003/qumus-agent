"""Small fixes from the stage-1 wrap-up (notes/v2-review/stage1-wrapup-2026-10-22.md,
items D1 and D2): the orchestrator is told which delegation rules are still
open before it meets them as refusals, and tool results name image files
without the computer's absolute paths."""

from __future__ import annotations

import asyncio
import json

from qumus2.agents.tooling import ToolResult


def call(s, tool, args):
    return asyncio.run(s.gateway.call("orchestrator", tool, args))[0]


def test_project_context_and_plan_say_which_rules_are_still_open(services):
    s = services
    r = call(s, "get_project_context", {})
    assert [g.split("(")[-1] for g in r.data["before_delegating"]] == [
        "plan_first).", "eyes_first; again after a hardware job did not succeed).",
        "advisor_first)."]
    assert "update_plan before any delegation" in r.summary

    r = call(s, "update_plan", {"note": "start", "plan": [
        {"id": "s1", "title": "Check the bench", "owner": "eyes"},
        {"id": "s2", "title": "Exfoliate graphene", "owner": "hands"}]})
    assert r.status == "ok" and r.summary.startswith("Task Plan saved as revision 1. Next:")
    assert "inspect_bench" in r.summary and "plan_first" not in r.summary

    s.store.update_project_state(s.project_id, eyes_checked=True,
                                 experience_checked=["Gr"])
    r = call(s, "get_project_context", {})
    assert r.data["before_delegating"] == [] and r.summary == "project demo."
    r = call(s, "update_plan", {"note": "same", "expected_version": r.data["plan_version"], "plan": [
        {"id": "s1", "title": "Check the bench", "owner": "eyes"},
        {"id": "s2", "title": "Exfoliate graphene", "owner": "hands"}]})
    assert r.summary == "Task Plan saved as revision 2."


def test_tool_text_names_images_by_file_only():
    r = ToolResult(status="ok", summary="s", images=["/Users/x/runs/leader/e9/images/m_000012.png"],
                   data={"flake": {"images": {"20x": "/tmp/run/images/microscope_000007.png"}},
                         "note": "see runs/images/a.png", "path": "/var/data/run.db"})
    body = json.loads(r.to_text())
    assert body["images"] == ["m_000012.png"]
    assert body["data"]["flake"]["images"]["20x"] == "microscope_000007.png"
    assert body["data"]["note"] == "see runs/images/a.png"  # relative text stays
    assert body["data"]["path"] == "/var/data/run.db"  # not an image
    assert r.images[0].startswith("/Users/")  # the records keep the full path


def test_below_floor_intent_holds_without_clamping_or_motion():
    from types import SimpleNamespace

    import pytest

    from qumus2.config import load_yaml
    from qumus2.core.errors import NeedsHumanError
    from qumus2.flows.library.stacking import PROCEDURE_ID, TRANSFER_PROFILE_ID, _guarded_z

    def forbidden(*_):
        pytest.fail("Below-floor intent must be refused before any motion adapter access")
    ctx = SimpleNamespace(store=SimpleNamespace(bench="sim"), verification=load_yaml("verification.yaml"),
        params={"procedure_contract": PROCEDURE_ID, "transfer_profile": TRANSFER_PROFILE_ID},
        gate=SimpleNamespace(manipulator_z_floor=lambda _: 4.7, manipulator=forbidden,
            limits=SimpleNamespace(manipulators={"transfer_arm": SimpleNamespace(z=[0., 20.])})))
    with pytest.raises(NeedsHumanError, match="no clamp applied"):
        _guarded_z(ctx, {}, 4.683)


def test_replay_plays_the_recording_named_in_default(tmp_path, monkeypatch):
    from qumus2.ui.recording import FORMAT
    from qumus2.ui.replay import resolve_recording

    monkeypatch.chdir(tmp_path)
    rec = tmp_path / "recordings"
    for name, at in (("demo", "2026-10-22T10:00"), ("newer", "2026-10-22T11:00")):
        (rec / name).mkdir(parents=True)
        (rec / name / "recording.json").write_text(json.dumps(
            {"format": FORMAT, "meta": {"agents": "sdk", "recorded_at": at}}))
    assert resolve_recording(None).name == "newer"  # no DEFAULT: the newest
    (rec / "DEFAULT").write_text("demo\n")
    assert resolve_recording(None).name == "demo"
    (rec / "DEFAULT").write_text("gone\n")  # a stale name is ignored
    assert resolve_recording(None).name == "newer"
