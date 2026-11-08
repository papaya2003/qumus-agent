"""Laboratory provenance and mode admission, without physical devices."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from qumus2.records.experience import ExperienceBook, ExperienceError, learning_origin
from qumus2.records.review import counts_as_evidence, review, unreviewed_runs
from qumus2.runtime.store import RunStore
from qumus2.ui.app import create_app


def recorded_run(store, *, kind=None, project="batch_one"):
    """Construct trusted backend receipts explicitly; this never operates hardware."""
    rid = store.create_run("search_chip", {}, project_id=project)
    if kind:
        store.emit("laboratory.origin", {"version": "laboratory_origin_v1", "kind": kind,
            "backend_id": "test_fixture.physical", "submission_event_id": store.last_event_id()}, run_id=rid)
    store.set_run_status(rid, "succeeded")
    return rid


def propose(book, rid):
    return book.propose(topic="equipment", claim="Inspect the fixture after each batch.",
                        scope={}, evidence_run_ids=[rid], observation="Explicit controlled fixture evidence.")


def test_sim_store_refuses_imported_real_label_and_receipt():
    store = RunStore(":memory:")
    rid = recorded_run(store, kind="physical")
    store._x("UPDATE run SET bench='real' WHERE id=?", (rid,))
    run = store.get_run(rid)
    assert learning_origin(store, run)["kind"] == "mixed"
    assert not counts_as_evidence(store, run)
    book = ExperienceBook(store)
    with pytest.raises(ExperienceError, match="disagrees"):
        propose(book, rid)
    with pytest.raises(ExperienceError, match="disagrees"):
        book.vote("prior_v1_exfoliation_default_Gr", rid, "support", note="Imported label is insufficient.")
    assert not book.auto_vote(run)
    assert review(store)["runs"] == []
    assert not store.get_run(rid)["meta"].get("reviewed")
    assert book.summary()["real_runs"] == 0


def test_mutable_mode_label_cannot_reclassify_original_virtual_store():
    store = RunStore(":memory:")
    rid = recorded_run(store, kind="physical")
    store._x("UPDATE run SET bench='real' WHERE id=?", (rid,))
    store.bench = "real"  # A UI/session label cannot replace persisted backend origin.
    assert store.get_meta("bench") == "sim"
    assert learning_origin(store, store.get_run(rid))["kind"] == "mixed"
    with pytest.raises(ExperienceError, match="disagrees"):
        propose(ExperienceBook(store), rid)


@pytest.mark.parametrize("kind", [None, "simulated", "mixed", "unknown"])
def test_real_store_label_alone_or_nonphysical_origin_never_qualifies(kind):
    store = RunStore(":memory:", bench="real")
    rid = recorded_run(store, kind=kind)
    store.update_run_meta(rid, laboratory_origin={"kind": "physical"}, actual_backend="real")
    assert not learning_origin(store, store.get_run(rid))["eligible"]
    assert unreviewed_runs(store) == []
    with pytest.raises(ExperienceError, match="cannot teach"):
        propose(ExperienceBook(store), rid)


def test_later_receipt_cannot_reclassify_legacy_record():
    store = RunStore(":memory:", bench="real")
    rid = recorded_run(store)
    submitted = store.events_after(0, run_id=rid)[0]
    store.emit("laboratory.origin", {"version": "laboratory_origin_v1", "kind": "physical",
        "backend_id": "test_fixture.physical", "submission_event_id": submitted.id}, run_id=rid)
    assert not learning_origin(store, store.get_run(rid))["eligible"]


def test_conflicting_backend_receipt_becomes_mixed():
    store = RunStore(":memory:", bench="real")
    rid = recorded_run(store, kind="physical")
    store.emit("laboratory.origin", {"version": "laboratory_origin_v1", "kind": "simulated",
        "backend_id": "test_fixture.simulated", "submission_event_id": 1}, run_id=rid)
    assert learning_origin(store, store.get_run(rid))["kind"] == "mixed"


@pytest.mark.parametrize("bad", [{"submission_event_id": True}, {"backend_id": " "}, {"version": "old"}])
def test_invalid_submission_receipt_is_not_physical(bad):
    store = RunStore(":memory:", bench="real")
    rid = store.create_run("search_chip", {})
    store.emit("laboratory.origin", {"version": "laboratory_origin_v1", "kind": "physical",
        "backend_id": "test_fixture.physical", "submission_event_id": store.last_event_id(), **bad}, run_id=rid)
    store.set_run_status(rid, "succeeded")
    assert not learning_origin(store, store.get_run(rid))["eligible"]


def test_explicit_physical_fixture_keeps_existing_independent_batch_vote_rules():
    store = RunStore(":memory:", bench="real")
    book = ExperienceBook(store)
    first = recorded_run(store, kind="physical")
    eid = propose(book, first)
    assert store.get_experience(eid)["status"] == "trial"
    second = recorded_run(store, kind="physical")
    book.vote(eid, second, "support", note="A second independent recording.")
    third = recorded_run(store, kind="physical", project="batch_two")
    assert book.vote(eid, third, "support", note="Another batch.")["status"] == "confirmed"
    assert book.summary()["real_runs"] == 3
    assert next(e for e in book.summary()["experiences"] if e["id"] == eid)["verified_by_real_runs"] == 3
    assert review(store)["runs"]


def test_sandbox_votes_cannot_change_nonsandbox_lab_entries():
    store = RunStore(":memory:", experience_sandbox=True)
    book = ExperienceBook(store)
    rid = recorded_run(store)
    with pytest.raises(ExperienceError, match="Sandbox"):
        book.vote("prior_v1_exfoliation_default_Gr", rid, "support", note="Sandbox only.")
    assert store.get_experience("prior_v1_exfoliation_default_Gr")["support"] == []
    eid = propose(book, rid)
    assert store.get_experience(eid)["sandbox"]
    assert eid not in {e["id"] for e in book.summary()["experiences"]}


def test_sandbox_recipes_never_leak_into_laboratory_summary_baselines():
    store = RunStore(":memory:", experience_sandbox=True)
    book = ExperienceBook(store)
    instruction = store.add_instruction("Use the synthetic 77 C sandbox recipe.")
    eid = book.teach(topic="exfoliation", claim="Synthetic sandbox recipe.", scope={"material": "Gr"},
                     instruction_id=instruction, recipe={"target_temp_c": 77})
    assert book.baseline("Gr")["experience_id"] == eid
    assert book.summary()["baselines"]["Gr"]["experience_id"] != eid


def test_unverified_legacy_votes_do_not_supply_confirmation_threshold():
    store = RunStore(":memory:", bench="real")
    book = ExperienceBook(store)
    eid = store.insert_experience(topic="equipment", claim="Preserve fixture checks.", source="review",
                                  status="trial", scope={}, author="test")
    for _ in range(2):
        legacy = recorded_run(store)
        store.add_vote(eid, legacy, "support", author="legacy", batch="legacy_batch", note="Unverified history.")
    first = recorded_run(store, kind="physical")
    assert book.vote(eid, first, "support", note="One physical run.")["status"] == "trial"
    second = recorded_run(store, kind="physical")
    assert book.vote(eid, second, "support", note="Same physical batch.")["status"] == "trial"
    third = recorded_run(store, kind="physical", project="another_batch")
    assert book.vote(eid, third, "support", note="An independent batch.")["status"] == "confirmed"
    assert len(store.get_experience(eid)["support"]) == 5  # Original evidence retained.


def test_submission_records_actual_simulated_devices_not_caller_origin(services, monkeypatch):
    monkeypatch.setattr(services.jobs, "enqueue", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="laboratory_origin"):
        services.jobs.submit("cool_experiment_stages", {"laboratory_origin": {"kind": "physical"}})
    rid = services.jobs.submit("cool_experiment_stages", {})
    events = services.store.events_after(0, run_id=rid)
    assert events[0].kind == "job.submitted"
    assert events[1].kind == "laboratory.origin"
    assert events[1].payload["kind"] == "simulated"
    assert events[1].payload["submission_event_id"] == events[0].id


def test_mode_api_reports_real_setup_and_preserves_original_runs(services):
    rid = recorded_run(services.store)
    with TestClient(create_app(services, start_driver=False)) as client:
        capability = client.get("/api/laboratory").json()
        assert capability == client.get("/api/health").json()["laboratory"]
        assert capability["mode"] == "virtual"
        assert capability["backend"] == "qumus2.sim"
        assert not capability["learning"]["enabled"]
        assert capability["modes"][1]["available"] is False
        response = client.post("/api/laboratory/mode", json={"mode": "real"})
        assert response.status_code == 409
        assert "physical device adapters" in response.json()["detail"]
        assert client.post("/api/laboratory/mode", json={"mode": "virtual"}).status_code == 200
        assert client.get("/api/experience").json()["learning"]["status"] == "disabled_virtual"
        assert services.store.bench == services.store.get_run(rid)["bench"] == "sim"


def test_mode_api_refuses_busy_models_jobs_and_pending_turns(services):
    with TestClient(create_app(services, start_driver=False)) as client:
        services.runtime._busy = True
        assert client.post("/api/laboratory/mode", json={"mode": "real"}).status_code == 409
        services.runtime._busy = False
        rid = services.store.create_run("search_chip", {})
        assert "active" in client.post("/api/laboratory/mode", json={"mode": "virtual"}).json()["detail"]
        services.store.set_run_status(rid, "succeeded")
        class PendingTurn:
            def done(self):
                return False
        services.driver._turn = PendingTurn()
        assert "active" in client.post("/api/laboratory/mode", json={"mode": "real"}).json()["detail"]
        services.driver._turn = None
