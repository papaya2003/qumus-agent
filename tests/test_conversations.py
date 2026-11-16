"""Several conversations on one run store (qumus2/conversations.py).

A new conversation starts blank (no chat, no plan, a fresh model session) and
keeps the settings; the lab, experience and spend stay shared. A switch is
refused while anything is under way, so no job crosses conversations."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from qumus2.app import FIRST_CONVERSATION, build_services
from qumus2.conversations import (
    ConversationBusy,
    create_conversation,
    list_conversations,
    switch_blocker,
    switch_conversation,
)
from qumus2.ui.app import create_app

PLAN = [{"id": "s1", "title": "Find graphene", "owner": "hands", "status": "pending"}]


def _client(s, tmp_path):
    return TestClient(create_app(s, start_driver=False, recordings_dir=tmp_path / "none"))


def test_a_new_store_has_one_conversation(services, tmp_path):
    with _client(services, tmp_path) as c:
        got = c.get("/api/conversations").json()
    assert got["active"] == services.project_id and got["blocked"] is None
    [row] = got["conversations"]
    assert row["name"] == FIRST_CONVERSATION and row["active"] and row["owner"] == ""
    assert services.store.active_project_id() == services.project_id


def test_new_conversation_is_blank_and_shares_the_lab(services, tmp_path):
    s = services
    first = s.project_id
    s.store.set_plan(first, PLAN, author="orchestrator")
    s.store.update_project_state(first, openai_model_config={"model": "m"}, notes="first only")
    s.store.add_instruction("Search chip 1", project_id=first)
    asyncio.run(s.driver.wait_idle(timeout=60))
    samples = s.store.list_samples()
    before = s.store.last_event_id()

    with _client(s, tmp_path) as c:
        r = c.post("/api/conversations", json={"name": "  Hall bar  B ", "owner": "Alice"})
        assert r.status_code == 200, r.text
        second = r.json()["id"]
        assert second != first and s.project_id == second
        assert r.json()["session_start"] == s.session_start >= before
        snap = c.get("/api/snapshot").json()
        assert snap["project"]["id"] == second and snap["project"]["name"] == "Hall bar B"
        assert snap["project"]["plan"] == [] and snap["session_start"] == s.session_start
        rows = c.get("/api/conversations").json()["conversations"]
        assert [x["id"] for x in rows] == [second, first]  # the one in use first
        assert rows[0]["owner"] == "Alice" and rows[1]["plan_steps"] == 1 and rows[1]["messages"] == 1
        # the earlier session is History now, under its conversation
        hist = c.get("/api/history").json()
        assert hist and hist[0]["conversation"]["id"] == first
        assert hist[0]["conversation"]["name"] == FIRST_CONVERSATION and not hist[0]["conversation"]["active"]
        plans = c.get(f"/api/history/{hist[0]['id']}/recording").json()["plans"]
        assert [x["id"] for x in plans[0]["plan"]] == ["s1"]

    state = s.store.get_project(second)["state"]
    assert state["owner"] == "Alice" and state["openai_model_config"] == {"model": "m"}  # settings kept
    assert "notes" not in state  # nothing else carries over
    assert [x["id"] for x in s.store.get_project(first)["plan"]] == ["s1"]  # the old one is untouched
    assert s.store.list_samples() == samples  # the lab is shared
    assert s.gateway.project_id == second and s.store.active_project_id() == second
    assert s.store.session_project(s.session_start) == second
    switched = [e for e in s.store.events_after(before) if e.kind == "conversation.switched"]
    assert switched[-1].payload == {"from": first, "to": second, "name": "Hall bar B",
                                    "owner": "Alice", "created": True}
    # new messages belong to the new conversation
    s.store.add_instruction("hello", project_id=s.project_id)
    assert s.store.pending_instructions()[0]["project_id"] == second


def test_switch_is_refused_while_anything_is_under_way(services, tmp_path):
    s = services
    first = s.project_id
    s.store.add_instruction("not read yet", project_id=first)
    with _client(s, tmp_path) as c:
        r = c.post("/api/conversations", json={"name": "B"})
        assert r.status_code == 409 and "message" in r.json()["detail"]
        assert c.get("/api/conversations").json()["blocked"]
    assert len(s.store.list_projects()) == 1 and s.project_id == first  # nothing was created

    s.store.mark_instruction(s.store.pending_instructions()[0]["id"], "delivered")
    rid = s.store.create_run("search_chip", {}, project_id=first)
    for status in ("running", "paused", "waiting_human"):
        s.store.set_run_status(rid, status)
        assert f"Job {rid}" in switch_blocker(s)
        with pytest.raises(ConversationBusy):
            asyncio.run(create_conversation(s, "B"))
    s.store.set_run_status(rid, "succeeded")
    assert switch_blocker(s) is None

    with _client(s, tmp_path) as c:
        assert c.post("/api/conversations", json={"name": "  "}).status_code == 400
        assert c.post("/api/conversations/prj_nope/switch").status_code == 404


def test_switch_back_continues_and_survives_a_restart(tmp_path):
    db = tmp_path / "run" / "qumus.db"
    s = build_services(db, scenario="place_chip_demo")
    first = s.project_id
    turns = len(s.runtime.turns)
    s.store.add_instruction("Put a chip on the transfer stage", project_id=first)
    asyncio.run(s.driver.wait_idle(timeout=120))
    left_first = len(s.runtime.turns)
    assert left_first < turns

    second = asyncio.run(create_conversation(s, "Second", "Bob"))["id"]
    assert len(s.runtime.turns) == turns  # a new conversation replays the scenario from its start
    back = asyncio.run(switch_conversation(s, first))
    assert back["id"] == first and len(s.runtime.turns) == left_first
    assert asyncio.run(switch_conversation(s, first))["id"] == first  # already in use: nothing happens
    sessions = s.store.sessions()
    assert [s.store.session_project(a) for a in sessions] == [first, second, first]
    asyncio.run(s.close())

    again = build_services(db, scenario="place_chip_demo")  # the next serve continues the one in use
    try:
        assert again.project_id == first
        rows = list_conversations(again)["conversations"]
        assert {r["name"]: r["owner"] for r in rows} == {FIRST_CONVERSATION: "", "Second": "Bob"}
        assert again.store.session_project(again.session_start) == first
    finally:
        asyncio.run(again.close())


def test_recorder_marks_each_conversation_session(tmp_path):
    from qumus2.ui.recording import attach_recorder, read_timeline, timeline_path

    db = tmp_path / "run" / "qumus.db"
    s = build_services(db, scenario="place_chip_demo")
    s.recorder = attach_recorder(s, poll_s=0.02, bench_every_s=0.05)
    try:
        s.store.add_instruction("Put a chip on the transfer stage", project_id=s.project_id)
        asyncio.run(s.driver.wait_idle(timeout=120))
        asyncio.run(create_conversation(s, "Second"))
        marks = [t["id"] for t in read_timeline(timeline_path(db)) if t.get("start")]
        assert len(marks) == 2 and s.session_start <= marks[1]
        with _client(s, tmp_path) as c:
            [row] = c.get("/api/history").json()
            assert row["started"] is not None and row["conversation"]["name"] == FIRST_CONVERSATION
    finally:
        s.recorder.stop()
        asyncio.run(s.close())


def test_openai_conversations_keep_their_own_model_session(services):
    pytest.importorskip("agents")
    from agents.testing import ModelStep, ScriptedModel, assistant_message
    from agents.usage import Usage

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.config import load_yaml

    class Provider:
        def __init__(self, model):
            self.model = model

        def get_model(self, name):
            return self.model

    s = services
    model = ScriptedModel([ModelStep(output=[assistant_message(t)], usage=Usage(
        input_tokens=100, output_tokens=10, total_tokens=110)) for t in ("A noted", "B here", "A again")])
    cfg = load_yaml("agents.yaml")
    cfg["openai"]["min_request_interval_s"] = 0
    creds = OpenAICredentials("test-key-never-sent", model_id="openai/test-model", input_price=1, output_price=2)
    rt = OpenAIRuntime(s.gateway, creds, model_provider=Provider(model), config=cfg)
    s.runtime = s.driver.runtime = rt
    first, first_session = s.project_id, rt.session_id

    async def go():
        await rt.send("Remember specimen A")
        await create_conversation(s, "B")
        second_session = rt.session_id
        await rt.send("Which specimen?")
        assert "specimen A" not in str(model.last_call.input)  # the new conversation starts blank
        await switch_conversation(s, first)
        assert rt.session_id == first_session
        await rt.send("Which specimen?")
        assert "specimen A" in str(model.last_call.input)  # the earlier one goes on
        await rt.close()
        return second_session

    second_session = asyncio.run(go())
    assert second_session != first_session
    rows = {r["name"]: r for r in list_conversations(s)["conversations"]}
    assert rows[FIRST_CONVERSATION]["cost_usd"] == pytest.approx(0.00024)
    assert rows["B"]["cost_usd"] == pytest.approx(0.00012)
    assert s.store.spent_usd() == pytest.approx(0.00036)  # one shared budget
