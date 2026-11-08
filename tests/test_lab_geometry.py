"""Laboratory evidence has real producers, immutable history and bounded views."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager

import pytest

from qumus2.agents.tooling import ToolContext
from qumus2.agents.tools.inventory import InstrumentStatusArgs, get_instrument_status
from qumus2.records.lab_geometry import LabEvidence, Source, evidence_view, record_evidence
from qumus2.records.materials import record_material_source, review_material
from qumus2.records.stamps import record_support, report_new_film
from qumus2.runtime.store import RunStore


def fact(store, value, **changes):
    return LabEvidence(subject="stage", property="pose", bench=store.bench, value=value,
                       units="mm", frame="world", source=Source(kind="synthetic", reference="fixture"),
                       **changes)


def test_conflicts_and_resolution_are_prefix_bound():
    store = RunStore(":memory:")
    a, b = fact(store, {"x": 1.1234567}), fact(store, {"x": 2})
    first = record_evidence(store, a)
    second = record_evidence(store, b)
    assert evidence_view(store, through_event_id=first)["facts"][0]["status"] == "recorded"
    row = evidence_view(store, through_event_id=second)["facts"][0]
    assert row["status"] == "conflicted" and len(row["assertions"]) == 2
    resolved = fact(store, {"x": 2}, supersedes=[a.assertion_id, b.assertion_id])
    record_evidence(store, resolved)
    assert evidence_view(store)["facts"][0]["status"] == "recorded"
    assert evidence_view(store, through_event_id=second)["facts"][0]["status"] == "conflicted"
    with pytest.raises(ValueError, match="reused"):
        record_evidence(store, a)
    assert len([e for e in store.events_after(limit=100) if e.kind == "lab.evidence"]) == 3


def test_full_prefix_is_paginated_and_old_conflict_survives_bounded_output():
    store = RunStore(":memory:")
    record_evidence(store, fact(store, 1))
    for _ in range(510):
        record_evidence(store, fact(store, 2))
    view = evidence_view(store, scan_limit=31, limit=1)
    row = view["facts"][0]
    assert view["assertions_scanned"] == 511 and view["complete_history"]
    assert row["status"] == "conflicted" and len(row["assertions"]) == 8
    assert row["omitted_assertions"] == 503
    assert view["fresh_measurement"] is False


def test_automatic_supersession_uses_full_property_and_assertion_history():
    store = RunStore(":memory:")
    originals = [fact(store, {"x": 1}) for _ in range(12)]
    for original in originals:
        record_evidence(store, original)
    for i in range(101):
        record_evidence(store, fact(store, i).model_copy(update={"property": f"newer_property_{i}"}))
    # Both UI bounds would otherwise hide active matches: 100 properties and
    # eight assertions per property. Neither can govern a write decision.
    record_evidence(store, fact(store, {"x": 2}), supersede_previous=True)
    row = next(r for r in evidence_view(store)["facts"] if r["property"] == "pose")
    assert row["status"] == "recorded" and len(row["assertions"]) == 1
    assert set(row["assertions"][0]["supersedes"]) == {f.assertion_id for f in originals}


def test_automatic_supersession_refuses_overflow_without_dropping_active_facts():
    store = RunStore(":memory:")
    # Imported typed history can contain many equivalent active assertions.
    for _ in range(1001):
        store.emit("lab.evidence", fact(store, 1).model_dump(mode="json"))
    before = store.last_event_id()
    with pytest.raises(ValueError, match="more than 1000"):
        record_evidence(store, fact(store, 2), supersede_previous=True)
    assert store.last_event_id() == before
    row = evidence_view(store)["facts"][0]
    assert row["status"] == "recorded" and row["omitted_assertions"] == 993


def test_time_uncertainty_source_and_unknowns_are_not_filled():
    store = RunStore(":memory:")
    f = LabEvidence(subject="frame:legacy", property="transform", bench=store.bench,
                    value=None, unknown_reason="original calibration unavailable", units="mm,deg",
                    frame="arm_left", source=Source(kind="historical_constant", reference="private inventory"))
    record_evidence(store, f)
    saved = evidence_view(store)["facts"][0]
    assert saved["status"] == "unknown"
    assert saved["assertions"][0]["uncertainty"] is None
    assert saved["assertions"][0]["observed_at"] is None
    assert saved["assertions"][0]["calibration_id"] is None
    with pytest.raises(ValueError, match="finite"):
        fact(store, {"x": float("nan")})
    with pytest.raises(ValueError, match="observation time"):
        fact(store, 1, observed_at=123)
    with pytest.raises(ValueError, match="unknown earlier"):
        record_evidence(store, fact(store, 1, supersedes=["nonexistent"]))


def test_real_and_synthetic_benches_cannot_be_relabelled():
    store = RunStore(":memory:", bench="real")
    f = fact(store, 1).model_copy(update={"bench": "sim"})
    with pytest.raises(ValueError, match="bench"):
        record_evidence(store, f)


def test_operator_source_label_does_not_replace_human_instruction_admission():
    store = RunStore(":memory:")
    f = LabEvidence(subject="stamp", property="contact_support", bench=store.bench,
                    value="PC", units="categorical", frame="sample_identity",
                    source=Source(kind="operator_report", reference="chat:report",
                                  instruction_id="invented"))
    with pytest.raises(ValueError, match="existing human"):
        record_evidence(store, f)
    f = f.model_copy(update={"source": Source(kind="operator_report", reference="model:claim", reporter="agent")})
    with pytest.raises(ValueError, match="named Lab UI"):
        record_evidence(store, f)
    assert not evidence_view(store)["facts"]


def test_existing_human_reports_append_cited_evidence_and_keep_conflicts(services):
    s = services
    project = s.store.create_project("evidence test")
    first = s.store.add_instruction("carrier_gr_01 contains Gr", project_id=project)
    record_material_source(s.store, s.lab.bench, "carrier_gr_01", "Gr",
                           instruction_id=first, project_id=project)
    second = s.store.add_instruction("carrier_gr_01 now contains hBN", project_id=project)
    record_material_source(s.store, s.lab.bench, "carrier_gr_01", "hBN",
                           instruction_id=second, project_id=project)
    row = evidence_view(s.store, subject="carrier_gr_01")["facts"][0]
    assert row["status"] == "conflicted"
    assert {a["source"]["instruction_id"] for a in row["assertions"]} == {first, second}
    assert all(a["source"]["kind"] == "operator_report" for a in row["assertions"])
    assert s.store.get_sample("carrier_gr_01")["props"]["material"] == "hBN"
    third = s.store.add_instruction("stamp_01 has mica contact support", project_id=project)
    record_support(s.store, s.lab.bench, "stamp_01", "mica", instruction_id=third, project_id=project)
    assert evidence_view(s.store, subject="stamp_01")["facts"][0]["assertions"][0]["value"] == "mica"


def test_report_and_evidence_rollback_together_on_append_failure(services, monkeypatch):
    s = services
    original = s.store.get_sample("carrier_gr_01")
    before = s.store.last_event_id()

    def fail(*args, **kwargs):
        raise ValueError("evidence append failed")

    monkeypatch.setattr("qumus2.records.materials.record_report", fail)
    with pytest.raises(ValueError, match="append failed"):
        review_material(s.store, s.lab.bench, "carrier_gr_01", expected_material="Gr", by="Ada", status="confirmed")
    assert s.store.get_sample("carrier_gr_01") == original
    assert s.store.last_event_id() == before


def test_inspection_is_a_real_evidence_producer_and_status_is_saved_query(services):
    s = services
    job = s.jobs.submit("inspect_bench", {"regions": ["left_storage"]})
    assert s.jobs.wait(job, 30).status == "succeeded"
    view = evidence_view(s.store)
    row = next(r for r in view["facts"] if r["subject"] == "region:left_storage")
    observed = row["assertions"][0]
    assert observed["value"]["detections"] and observed["source"]["kind"] == "synthetic"
    assert observed["observed_at"] is not None and observed["timebase"] == "store_clock"
    before = s.store.last_event_id()
    result = asyncio.run(get_instrument_status(InstrumentStatusArgs(), ToolContext(s.store, s.jobs, "eyes")))
    assert result.data["lab_evidence"]["through_event_id"] == before
    assert result.data["lab_evidence"]["fresh_measurement"] is False
    assert s.store.last_event_id() == before


def test_status_subject_filter_retrieves_old_facts_after_bounded_view_omits_them(services):
    s = services
    historical = LabEvidence(subject="historical:v1:microscope:10x", property="pixel_scale", bench="sim",
        value=.365, units="um/px", frame="v1:microscope_image",
        source=Source(kind="historical_constant", reference="private historical fixture"))
    record_evidence(s.store, historical)
    for i in range(61):
        recent = fact(s.store, i).model_copy(update={"subject": f"region:recent_{i}"})
        record_evidence(s.store, recent)
    before = s.store.last_event_id()
    unfiltered, _ = asyncio.run(s.gateway.call("eyes", "get_instrument_status", {}))
    assert unfiltered.status == "ok"
    assert len(unfiltered.data["lab_evidence"]["facts"]) == 50
    assert unfiltered.data["lab_evidence"]["omitted_properties"] == 12
    assert not any(row["subject"] == historical.subject for row in unfiltered.data["lab_evidence"]["facts"])
    filtered, _ = asyncio.run(s.gateway.call("eyes", "get_instrument_status",
        {"subject": historical.subject, "evidence_limit": 1}))
    row, = filtered.data["lab_evidence"]["facts"]
    assert row["subject"] == historical.subject and row["assertions"][0]["value"] == .365
    assert row["assertions"][0]["observed_at"] is None and row["assertions"][0]["calibration_id"] is None
    assert filtered.data["observations"] == unfiltered.data["observations"]
    # The gateway records its normal tool calls; the status surface itself adds no evidence.
    assert not [e for e in s.store.events_after(before, limit=100) if e.kind == "lab.evidence"]
    assert InstrumentStatusArgs().evidence_limit == 50
    with pytest.raises(ValueError):
        InstrumentStatusArgs(evidence_limit=101)


def test_concurrent_material_reviews_preserve_both_histories_and_evidence(services, monkeypatch):
    store, bench = services.store, services.lab.bench
    barrier, local = threading.Barrier(2), threading.local()
    original_transaction = store._transaction

    @contextmanager
    def enter_together():
        # Both callers reach their first transaction boundary together. Reads
        # before this boundary used to give both writers the same stale history.
        if threading.current_thread().name.startswith("material-review-") and not getattr(local, "entered", False):
            local.entered = True
            barrier.wait(timeout=5)
        with original_transaction():
            yield

    monkeypatch.setattr(store, "_transaction", enter_together)
    errors = []

    def review(name):
        try:
            review_material(store, bench, "carrier_gr_01", expected_material="Gr",
                            by=name, status="confirmed")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=review, args=(name,), name="material-review-"+name)
               for name in ("Ada", "Bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    history = store.get_sample("carrier_gr_01")["props"]["material_reviews"]
    assert len(history) == 2 and {report["by"] for report in history} == {"Ada", "Bob"}
    assertions = evidence_view(store, subject="carrier_gr_01")["facts"][0]["assertions"]
    assert len(assertions) == 2
    assert {fact["source"]["reporter"] for fact in assertions} == {"Ada", "Bob"}


@pytest.mark.parametrize("report_kind,change", [
    ("review", "label"), ("review", "reservation"),
    ("source", "reservation"), ("support", "location"), ("film", "reservation"),
])
def test_report_admission_uses_state_after_transaction_lock(services, monkeypatch, report_kind, change):
    store, bench = services.store, services.lab.bench
    sample_id = "carrier_gr_01" if report_kind in ("review", "source") else "stamp_01"
    if report_kind == "film":
        store.upsert_sample(sample_id, "stamp", status="used")
    instruction = store.add_instruction(f"{sample_id} now has new PC support and contains Gr")
    booking = store.create_run("stack_device", {})
    original_transaction = store._transaction
    changed = False

    @contextmanager
    def waiting_for_writer():
        nonlocal changed
        if not changed:
            changed = True
            # Simulate another writer committing while this request waits to
            # acquire its transaction. Admission must inspect this new state.
            if change == "label":
                store.upsert_sample(sample_id, "carrier", props={"material": "hBN"})
            elif change == "location":
                store.upsert_sample(sample_id, "stamp", location="transfer_arm")
            else:
                store.reserve("sample", sample_id, booking)
        with original_transaction():
            yield

    monkeypatch.setattr(store, "_transaction", waiting_for_writer)
    operations = {
        "review": lambda: review_material(store, bench, sample_id, expected_material="Gr",
                                            by="Ada", status="confirmed"),
        "source": lambda: record_material_source(store, bench, sample_id, "Gr",
                                                   instruction_id=instruction, project_id=None),
        "support": lambda: record_support(store, bench, sample_id, "PC",
                                            instruction_id=instruction, project_id=None),
        "film": lambda: report_new_film(store, bench, sample_id, by="Ada", via="ui:lab"),
    }
    with pytest.raises(ValueError):
        operations[report_kind]()
    assert changed
    props = store.get_sample(sample_id)["props"]
    assert not props.get("material_reviews") and not props.get("support_history")
    assert not props.get("film_reports")
    assert not evidence_view(store, subject=sample_id)["facts"]
