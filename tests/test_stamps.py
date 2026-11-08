"""Stamp states and a person's report of a new PC film (item 15, 2026-10-22):
fresh / used / torn, the Lab page's control (POST /api/stamps/{id}/film), the
orchestrator's tool report_new_film (only on a person's message), the ledger as
events for the Lab page and replays (sample.updated)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from qumus2.records.stamps import FilmReportError, names_stamp, report_new_film
from qumus2.ui.app import create_app


def call(s, agent, tool, args):
    return asyncio.run(s.gateway.call(agent, tool, args))[0]


def events(s, kind):
    return [e for e in s.store.events_after(0, limit=1_000_000) if e.kind == kind]


def used(s, sid="stamp_01", status="used"):
    s.store.upsert_sample(sid, "stamp", status=status, props={"film": None})


def test_the_lab_page_records_a_new_film_on_a_used_stamp(services):
    s = services
    used(s)
    s.clock.advance(120)
    with TestClient(create_app(s, start_driver=False)) as c:
        r = c.post("/api/stamps/stamp_01/film", json={"by": "Jialei"})
        assert r.status_code == 200, r.text
        rec = r.json()
        # a fresh stamp needs no new film: refused with the reason
        again = c.post("/api/stamps/stamp_01/film", json={})
        assert again.status_code == 409 and "fresh" in again.json()["detail"]
        assert c.post("/api/stamps/stamp_99/film", json={}).status_code == 409
    st = s.store.get_sample("stamp_01")
    assert st["status"] == "fresh" and st["props"]["film"] == "PC"
    (report,) = st["props"]["film_reports"]
    assert {**report, "stamp_id": "stamp_01", "status": "fresh"} == rec
    assert report["by"] == "Jialei" and report["via"] == "ui:lab"
    assert report["previous_status"] == "used" and report["what"] == "new PC film"
    assert report["ts"] == pytest.approx(s.clock.now())  # Clock time, not wall time
    (ev,) = events(s, "stamp.film_replaced")
    assert ev.payload["stamp_id"] == "stamp_01" and ev.payload["previous_status"] == "used"


def test_a_stamp_in_use_or_booked_cannot_be_refilmed(services):
    s = services
    bench = s.lab.bench
    used(s, status="torn")
    s.store.upsert_sample("stamp_01", "stamp", location="transfer_arm")
    with pytest.raises(FilmReportError, match="not in its slot"):
        report_new_film(s.store, bench, "stamp_01", by="p", via="ui:lab")
    s.store.upsert_sample("stamp_01", "stamp", location="right_stamp_1")
    rid = s.store.create_run("stack_device", {})
    s.store.reserve("sample", "stamp_01", rid, "stack_device")
    with pytest.raises(FilmReportError, match="booked by job"):
        report_new_film(s.store, bench, "stamp_01", by="p", via="ui:lab")
    s.store.set_run_status(rid, "succeeded")  # its bookings lapse
    assert report_new_film(s.store, bench, "stamp_01", by="p", via="ui:lab")[
        "previous_status"] == "torn"
    with pytest.raises(FilmReportError, match="not a stamp"):
        report_new_film(s.store, bench, "chip_001", by="p", via="ui:lab")


def test_the_orchestrator_records_a_film_only_on_a_persons_message(services):
    s = services
    used(s, status="torn")
    # no such message, or one that does not name the stamp: refused before the tool runs
    r = call(s, "orchestrator", "report_new_film", {"stamp_id": "stamp_01",
                                                    "instruction_id": "ins_nope"})
    assert r.status == "denied" and "not a message a person sent" in r.summary
    denied = events(s, "tool.denied")
    assert denied and denied[-1].payload["rule"] == "person_reported_film"
    other = s.store.add_instruction("I put a new PC film on stamp_02", project_id=s.project_id)
    call(s, "orchestrator", "report_new_film", {"stamp_id": "stamp_01",
                                                "instruction_id": other})
    assert "does not name stamp_01" in events(s, "tool.denied")[-1].payload["reason"]
    assert s.store.get_sample("stamp_01")["status"] == "torn"  # never fresh on its own
    # only the orchestrator has the tool
    call(s, "hands", "report_new_film", {"stamp_id": "stamp_01", "instruction_id": other})
    assert events(s, "tool.denied")[-1].payload["rule"] == "permission"

    said = s.store.add_instruction("New PC film on stamp 1, it is back in its slot",
                                   project_id=s.project_id)
    r = call(s, "orchestrator", "report_new_film", {"stamp_id": "stamp_01",
                                                    "instruction_id": said})
    assert r.status == "ok", r.summary
    st = s.store.get_sample("stamp_01")
    assert st["status"] == "fresh"
    rep = st["props"]["film_reports"][-1]
    assert rep["via"] == "chat" and rep["by"] == "orchestrator"
    assert rep["instruction_id"] == said and rep["previous_status"] == "torn"
    # the same message cannot reset the stamp a second time
    used(s)
    r = call(s, "orchestrator", "report_new_film", {"stamp_id": "stamp_01",
                                                    "instruction_id": said})
    assert r.status == "error" and "already recorded" in r.summary


def test_names_stamp():
    assert names_stamp("I put a new PC film on stamp_01", "stamp_01")
    assert names_stamp("stamp 1 has a new film", "stamp_01")
    assert names_stamp("Stamp #3 re-filmed", "stamp_03")
    assert not names_stamp("stamp 12 has a new film", "stamp_01")
    assert not names_stamp("the stamp has a new film", "stamp_01")


def test_the_testbench_stamp_gets_its_film_when_a_person_reports_it(services):
    """The testbench has no person: the report stands for the act, and the sim
    stamp gets an intact film the next time the holder grips it."""
    s = services
    w = s.lab.world
    used(s)
    w.stamps["stamp_01"].film = False  # its film stayed on a device
    w._refilm("stamp_01")
    assert not w.stamps["stamp_01"].film  # nothing reported: still no film
    report_new_film(s.store, s.lab.bench, "stamp_01", by="p", via="ui:lab")
    w._refilm("stamp_01")
    assert w.stamps["stamp_01"].film and w.stamps["stamp_01"].stack == []
    w.stamps["stamp_01"].film = False  # torn again later: the old report does not mend it
    w._refilm("stamp_01")
    assert not w.stamps["stamp_01"].film


def test_the_ledger_can_be_rebuilt_from_events(services):
    """Every ledger write is a `sample.updated` event, so the Lab page's
    inventory can be rebuilt in a replay from the recorded events alone."""
    s = services
    s.store.upsert_sample("chip_001", "chip", status="exfoliated", props={"material": "Gr"})
    used(s, "stamp_02", status="torn")
    rebuilt: dict[str, dict] = {}
    for e in events(s, "sample.updated"):
        rebuilt[e.payload["id"]] = e.payload
    ledger = s.store.list_samples()
    assert set(rebuilt) == {x["id"] for x in ledger}
    for x in ledger:
        r = rebuilt[x["id"]]
        assert (r["kind"], r["location"], r["status"]) == (x["kind"], x["location"], x["status"])
    assert rebuilt["chip_001"]["props"]["material"] == "Gr"
    assert rebuilt["stamp_02"]["status"] == "torn"


def test_the_scripted_runtime_can_cite_the_persons_message(make_services):
    from qumus2.agents.scripted import ScriptedRuntime

    s = make_services()
    used(s)
    scenario = {"turns": [{"actions": [
        {"tool": {"agent": "orchestrator", "name": "report_new_film",
                  "args": {"stamp_id": "stamp_01", "instruction_id": "{{turn.message_id}}"},
                  "save_as": "film"}},
        {"say": {"agent": "orchestrator", "text": "{{film._summary}}"}}]}]}
    rt = ScriptedRuntime(s.gateway, scenario)
    iid = s.store.add_instruction("I put a new PC film on stamp_01", project_id=s.project_id)
    asyncio.run(rt.send(f"I put a new PC film on stamp_01 [message id: {iid}]"))
    assert s.store.get_sample("stamp_01")["status"] == "fresh"
    said = events(s, "agent.message")[-1].payload["text"]
    assert "fresh again" in said
