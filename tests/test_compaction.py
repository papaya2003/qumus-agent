"""Context compaction: real SDK loops with scripted model replies, no network or keys."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from qumus2.agents import compaction
from qumus2.agents.compaction import (
    CompactionSettings,
    apply_view,
    choose_cut,
    find_cut,
    load_settings,
    render_transcript,
    safe_cuts,
    summary_item,
    validate,
)
from qumus2.config import load_yaml

# -- pure parts ----------------------------------------------------------------


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def call(ident, name="list_samples"):
    return {"type": "function_call", "call_id": ident, "name": name, "arguments": "{}"}


def output(ident, text="ok"):
    return {"type": "function_call_output", "call_id": ident, "output": text}


def test_safe_cuts_never_split_a_tool_call_from_its_result():
    items = [user("a"), call("c1"), call("c2"), output("c1"), output("c2"),
             {"type": "reasoning", "id": "r1", "summary": []}, assistant("b"), user("c")]
    assert safe_cuts(items) == [1, 5, 7, 8]  # never inside c1/c2, never right after reasoning


def test_choose_cut_keeps_the_newest_items_and_prefers_a_human_message():
    big = "x" * 4000  # about 1000 tokens each
    items = [user("first " + big), assistant(big), user("second " + big), call("c1"),
             output("c1", big), assistant(big), user("third")]
    cut = choose_cut(items, keep_tokens=2600)
    tail = items[cut:]
    assert sum(compaction.item_tokens(i) for i in tail) <= 2600
    assert cut in safe_cuts(items)
    # the newest items that fit: c1 and its result, the answer and the last message
    assert tail[0] == call("c1")
    # a human message boundary wins when it keeps at least half of the allowance
    assert choose_cut(items, keep_tokens=4000) == 2  # "second ..." starts the kept part


def test_choose_cut_with_nothing_to_fold():
    assert choose_cut([], 1000) is None
    only = [summary_item("old", folded=3, when="t")]
    assert choose_cut(only, 1000) is None
    # newest items larger than the allowance: keep only what must stay whole
    items = [user("a"), call("c1"), output("c1", "y" * 40000)]
    assert choose_cut(items, 10) == 3


def test_find_cut_and_view_follow_the_last_folded_item():
    items = [user("a"), assistant("b"), call("c1"), output("c1"), user("d")]
    mark = compaction.anchor(items[3])
    assert mark == {"kind": "function_call_output", "call_id": "c1"}
    assert find_cut(items, 4, mark) == 4
    assert find_cut([user("new"), *items], 4, mark) == 5   # searched when shifted
    assert find_cut(items[:3], 4, mark) is None
    item = summary_item("S", folded=4, when="t")
    view = {"cut": 4, "anchor": mark, "summary_item": item}
    assert apply_view([*items, assistant("e")], view) == [item, user("d"), assistant("e")]
    assert apply_view([user("changed"), *items[1:]], {**view, "anchor": compaction.anchor(user("zzz"))}) is None


def test_transcript_has_no_pixels_and_shortens_long_results():
    items = [user("keep chip_012 [message id: ins_1]"), call("c1", "get_images"),
             {"type": "function_call_output", "call_id": "c1", "output": [
                 {"type": "input_text", "text": "z" * 20000},
                 {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}]
    text = render_transcript(items, max_item_chars=1000)
    assert "keep chip_012 [message id: ins_1]" in text and "[TOOL RESULT get_images id=c1]" in text
    assert "base64" not in text and "[image]" in text and "characters omitted" in text


def test_settings_threshold_is_a_share_of_the_real_window(monkeypatch):
    monkeypatch.delenv("QUMUS_COMPACTION", raising=False)
    s = load_settings(load_yaml("agents.yaml"))
    assert (s.auto, s.window_percent, s.keep_recent_tokens) == (True, 50.0, 25000)
    assert s.threshold(1_000_000) == 500_000 and s.threshold(131_072) == 65_536
    assert s.threshold(None) == 150_000  # window unknown
    saved = load_settings(load_yaml("agents.yaml"), {"compaction_settings": {"window_percent": 70}})
    assert saved.threshold(200_000) == 140_000
    monkeypatch.setenv("QUMUS_COMPACTION", "off")
    assert load_settings(load_yaml("agents.yaml")).auto is False
    with pytest.raises(ValueError, match="between 10% and 95%"):
        validate({"window_percent": 5})
    with pytest.raises(ValueError, match="Unknown"):
        validate({"keep_recent_tokens": 1})  # not a setting the page may change
    assert validate({"auto": False, "window_percent": 60}) == {"auto": False, "window_percent": 60.0}


# -- the OpenAI runtime ---------------------------------------------------------

pytest.importorskip("agents")

from agents import SQLiteSession  # noqa: E402
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call  # noqa: E402
from agents.usage import Usage  # noqa: E402

from qumus2.agents.openai_credentials import OpenAICredentials  # noqa: E402
from qumus2.agents.openai_runtime import OpenAIRuntime  # noqa: E402


class Provider:
    def __init__(self, model):
        self.model = model

    def get_model(self, name):
        assert name == "openai/test-model"
        return self.model


CREDS = OpenAICredentials("test-key-never-sent", model_id="openai/test-model", input_price=1,
                          output_price=2, context_window_tokens=1_000_000)


def runtime(s, steps, model=None, **settings):
    model = model or ScriptedModel(steps)
    cfg = load_yaml("agents.yaml")
    cfg["openai"]["min_request_interval_s"] = 0
    cfg["compaction"].update(settings)
    return OpenAIRuntime(s.gateway, CREDS, model_provider=Provider(model), config=cfg), model


def step(output, tokens):
    return ModelStep(output=output, usage=Usage(input_tokens=tokens, output_tokens=10,
                                                total_tokens=tokens + 10))


def said(text, tokens=100):
    return step([assistant_message(text)], tokens)


def events(s, kind):
    return [e for e in s.store.events_after(0, limit=100000) if e.kind == kind]


def saved_items(s, session_id):
    session = SQLiteSession(session_id, db_path=str(Path(s.store.path).with_suffix(".openai-sessions.db")))
    try:
        return asyncio.run(session.get_items())
    finally:
        session.close()


def test_auto_compaction_mid_turn_keeps_the_old_session_and_the_experiment_going(services):
    s = services
    rt, model = runtime(s, [
        said("Noted: specimen A, keep chip_004 unused."),
        step([function_call("list_samples", "{}", call_id="c1")], 600_000),  # over 50% of 1M
        said("## Current goal and plan position\nSpecimen A ...", 900),        # the summary
        said("Inventory checked."),
    ])
    asyncio.run(rt.send("Remember specimen A; keep chip_004 unused."))
    first = rt.session_id
    asyncio.run(rt.send("Check the inventory."))
    model.assert_complete()

    summary_call, after = model.calls[2], model.calls[3]
    assert "Remember specimen A" in str(summary_call.input)        # the folded part
    assert "Check the inventory" not in str(summary_call.input)    # kept word for word
    assert after.input[0]["content"].startswith(compaction.SUMMARY_MARKER)
    assert "Remember specimen A" not in str(after.input)
    assert "Check the inventory." in str(after.input) and "c1" in str(after.input)

    done = events(s, "context.compacted")[-1]
    assert done.agent == "compactor" and done.payload["trigger"] == "auto" and done.payload["during_turn"]
    assert done.payload["tokens_before"] == 600_000 and done.payload["threshold_tokens"] == 500_000
    assert done.payload["cost_usd"] == pytest.approx((900 + 20) / 1e6)
    assert any(e.agent == "compactor" for e in events(s, "agent.usage"))  # billed separately

    # The session switched at the end of the turn; the old one is unchanged.
    state = s.store.get_project(s.project_id)["state"]
    assert rt.session_id != first and state["openai_session_id"] == rt.session_id
    assert state["openai_compaction_pending"] is None
    assert state["openai_session_chain"][-1]["from"] == first
    old, new = saved_items(s, first), saved_items(s, rt.session_id)
    assert "Remember specimen A" in str(old) and "Inventory checked." in str(old)
    assert new[0]["content"].startswith(compaction.SUMMARY_MARKER)
    assert "Remember specimen A" not in str(new) and "Inventory checked." in str(new)
    assert not events(s, "agent.error") and not events(s, "notification") or all(
        e.payload.get("reason") != "compaction_failed" for e in events(s, "notification"))

    # The next turn starts from the new session; no second compaction yet.
    model.enqueue(said("Still specimen A."))
    asyncio.run(rt.send("Which specimen?"))
    assert model.calls[-1].input[0]["content"].startswith(compaction.SUMMARY_MARKER)
    assert len(events(s, "context.compacted")) == 1
    asyncio.run(rt.close())


def test_no_compaction_below_the_threshold_or_when_switched_off(services):
    rt, model = runtime(services, [said("a", 400_000), said("b", 499_999), said("c")])
    for text in ("one", "two", "three"):
        asyncio.run(rt.send(text))
    model.assert_complete()
    assert not events(services, "context.compacted")
    rt.compaction = replace(rt.compaction, auto=False)
    model.enqueue(said("d", 900_000))
    model.enqueue(said("e"))
    asyncio.run(rt.send("four"))
    asyncio.run(rt.send("five"))
    assert not events(services, "context.compacted")
    asyncio.run(rt.close())


def test_failed_summary_does_not_stop_the_turn_and_waits_before_retrying(services):
    s = services
    rt, model = runtime(s, [
        said("a", 600_000),
        ModelStep(error=RuntimeError("summary service down")),
        said("b", 610_000),     # the request still goes out, with the full conversation
        said("c", 615_000),     # +15k: not yet worth another try (retry after +20k)
    ])
    asyncio.run(rt.send("one"))
    asyncio.run(rt.send("two"))
    asyncio.run(rt.send("three"))
    model.assert_complete()
    assert not events(s, "context.compacted")
    failed = [e for e in events(s, "notification") if e.payload.get("reason") == "compaction_failed"]
    assert len(failed) == 1 and "summary service down" in failed[0].payload["text"]
    assert all(not e.payload["is_error"] for e in events(s, "agent.turn_end"))
    assert rt._next_auto_at == 620_000
    asyncio.run(rt.close())


def test_manual_compaction_when_idle_through_the_driver(services):
    s = services
    rt, model = runtime(s, [said("Plan: graphene Hall bar on chip_001."), said("hBN chosen."),
                            said("## Current goal and plan position\nHall bar.")])
    s.driver.runtime = rt
    asyncio.run(rt.send("Build a graphene Hall bar on chip_001."))
    asyncio.run(rt.send("Use hBN."))
    first = rt.session_id

    async def press():
        s.store.add_instruction("keep every chip id", kind="compact", project_id=s.project_id)
        await s.driver.wait_idle(10)

    asyncio.run(press())
    model.assert_complete()
    assert "keep every chip id" in str(model.calls[-1].input)  # the focus reaches the summary request
    done = events(s, "context.compacted")[-1].payload
    assert done["trigger"] == "manual" and not done["during_turn"] and done["focus"] == "keep every chip id"
    assert rt.session_id != first
    assert s.store.get_instruction(done["instruction_id"])["status"] == "handled"
    status = rt.context_status()
    assert status["estimated"] and status["compactions"] == 1 and status["threshold_tokens"] == 500_000
    asyncio.run(rt.close())


def test_manual_request_during_a_turn_compacts_before_the_next_request(services):
    s = services

    def ask(call):
        rt.request_compaction("chip ids", "ins_x")
        return [function_call("list_samples", "{}", call_id="c1")]

    rt, model = runtime(s, [said("first answer"), ModelStep.respond(ask),
                            said("summary text"), said("done")])
    asyncio.run(rt.send("one"))
    asyncio.run(rt.send("two"))
    model.assert_complete()
    assert model.calls[3].input[0]["content"].startswith(compaction.SUMMARY_MARKER)
    assert events(s, "context.compacted")[-1].payload["trigger"] == "manual"
    asyncio.run(rt.close())


def test_saved_cut_switches_after_a_restart(services):
    s = services
    rt, _ = runtime(s, [said("Remember specimen A"), said("ok")])
    asyncio.run(rt.send("Specimen A"))
    asyncio.run(rt.send("Then specimen B"))
    first = rt.session_id
    asyncio.run(rt.close())
    items = saved_items(s, first)
    item = summary_item("Specimen A was named first.", folded=2, when="t")
    # as a crash after a mid-turn compaction leaves it: the cut saved, no switch yet
    s.store.update_project_state(s.project_id, openai_compaction_pending={
        "session_id": first, "cut": 2, "anchor": compaction.anchor(items[1]),
        "summary_item": item, "event_id": None})
    rt2, model = runtime(s, [said("Specimen A, then B.")])
    asyncio.run(rt2.send("Which specimens?"))
    assert rt2.session_id != first
    sent = model.calls[0].input
    assert sent[0] == item and "Then specimen B" in str(sent) and "Remember specimen A" not in str(sent)
    assert len(saved_items(s, first)) == len(items)  # the old session is not touched
    asyncio.run(rt2.close())


def test_stop_cancels_a_running_summary(services):
    s = services

    async def go():
        waiting = asyncio.Event()

        async def blocked(call):
            waiting.set()
            await asyncio.Event().wait()

        rt, model = runtime(s, [said("a", 600_000), ModelStep.respond(blocked)])
        await rt.send("one")
        first = rt.session_id
        task = asyncio.create_task(rt.send("two"))
        await asyncio.wait_for(waiting.wait(), 5)
        await asyncio.wait_for(rt.interrupt(), 5)
        await asyncio.wait_for(task, 5)
        assert not rt.busy and not events(s, "context.compacted") and rt.session_id == first
        assert events(s, "agent.turn_end")[-1].payload["reason"] == "interrupted"
        await rt.close()

    asyncio.run(go())


def test_page_api_meter_settings_and_scripted_compaction(services):
    from fastapi.testclient import TestClient

    from qumus2.ui.app import create_app

    s = services
    with TestClient(create_app(s, start_driver=False)) as client:
        meter = client.get("/api/context").json()
        assert meter["supported"] and meter["mode"] == "scripted" and meter["window_percent"] == 50
        assert client.post("/api/context/settings", json={"window_percent": 5}).status_code == 422
        changed = client.post("/api/context/settings", json={"window_percent": 65, "auto": True}).json()
        assert changed["window_percent"] == 65
        assert s.store.get_project(s.project_id)["state"]["compaction_settings"] == {
            "window_percent": 65.0, "auto": True}
        iid = client.post("/api/compact", json={"focus": "keep chip ids"}).json()["instruction_id"]
        asyncio.run(s.driver.wait_idle(10))
        done = events(s, "context.compacted")[-1].payload
        assert done["instruction_id"] == iid and done["scripted"] and done["cost_usd"] == 0
        assert client.get("/api/context").json()["compactions"] == 1


def test_page_shows_the_compaction_controls():
    static = Path(__file__).resolve().parents[1] / "qumus2" / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert 'id="ctx"' in html and "contextmeter.js" in (static / "app.js").read_text(encoding="utf-8")
    meter = (static / "contextmeter.js").read_text(encoding="utf-8")
    assert "'/api/compact'" in meter and "'/api/context/settings'" in meter
    feed = (static / "feed.js").read_text(encoding="utf-8")
    assert "'context.compacted'" in feed
    assert json.loads(json.dumps(CompactionSettings().to_dict()))["window_percent"] == 50.0
