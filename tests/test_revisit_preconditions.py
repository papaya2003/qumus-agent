"""Revisit admission must not masquerade as a first scan of a new specimen."""
from __future__ import annotations

import asyncio

import pytest

from qumus2.app import build_services
from qumus2.flows.library.search import RevisitChipFlow, RevisitParams, prior_scan_evidence
from qumus2.runtime.store import RunStore


def ledger_chip(s, chip="chip_001", *, status="exfoliated"):
    # Ledger-only fixtures below test admission, never claim physical execution.
    s.store.upsert_sample(chip, "chip", location="mid_chip_1" if chip == "chip_001" else "mid_chip_2",
                          status=status, props={"material": "Gr"})


def recorded_scan(s, *, chip="chip_001", flow="search_chip", material="Gr", verified=True,
                  finished=True, verdict="ok", tiles=10, candidates=0):
    rid = s.store.create_run(flow, {"chip_id": chip, "material": material})
    expected = {"tiles": 10, "max_blurred_tiles": 0}
    measured = {"tiles_scanned": tiles, "blurred_tiles": 0, "candidates": candidates}
    s.store.begin_step(rid, 5, "scan_chip", expected=expected, safe_to_resume=True)
    if verified:
        s.store.emit("step.verified", {"seq": 5, "name": "scan_chip", "verdict": verdict,
            "measurements": measured, "expected": expected}, run_id=rid)
    if finished:
        s.store.finish_step(rid, 5, "done", measurements=measured)
    s.store.set_run_status(rid, "succeeded")
    s.store.upsert_sample(chip, "chip", status="searched")
    return rid


def resolve(s, **args):
    flow = RevisitChipFlow()
    flow._bench = s.lab.bench
    return flow.resolve_params(RevisitParams(chip_id="chip_001", **args), s.store)


def assert_refused_without_physical_writes(s, *, flake_ids=None):
    before = (s.store.list_runs(), s.store.reservations(), s.store.list_samples())
    last = s.store.last_event_id()
    with pytest.raises(ValueError, match="no verified microscope scan"):
        s.jobs.submit("revisit_chip", {"chip_id": "chip_001", "flake_ids": flake_ids or []})
    assert s.store.last_event_id() == last
    result = asyncio.run(s.gateway.call("hands", "search_chip", {
        "chip_id": "chip_001", "revisit_only": True, "flake_ids": flake_ids or []}))[0]
    assert result.status == "error" and "revisit_only=false" in result.summary
    assert (s.store.list_runs(), s.store.reservations(), s.store.list_samples()) == before
    assert not any(e.kind in ("job.submitted", "device.command", "sample.updated")
                   for e in s.store.events_after(last, limit=10000))


def test_freshly_exfoliated_chip_cannot_take_an_empty_revisit(services):
    s = services
    run = s.jobs.wait(s.jobs.submit("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"]}), 600)
    assert run.status == "succeeded"
    assert s.store.get_sample("chip_001")["status"] == "exfoliated"
    assert not s.store.list_samples(kind="flake", location="chip_001")
    assert_refused_without_physical_writes(s)
    assert s.store.get_sample("chip_001")["status"] == "exfoliated"


@pytest.mark.parametrize("legacy", ["status_only", "corner_only", "candidate_only", "successful_empty_revisit"])
def test_legacy_labels_coordinates_or_candidates_cannot_invent_scan_history(services, legacy):
    s = services
    ledger_chip(s, status="searched")
    ids = []
    if legacy == "corner_only":
        s.store.upsert_sample("chip_001", "chip", props={"corner_stage_xy": [0, 0]})
    elif legacy == "candidate_only":
        ids = ["old_candidate"]
        s.store.upsert_sample(ids[0], "flake", location="chip_001", status="candidate",
            props={"chip_id": "chip_001", "material": "Gr", "labels": {"10x": "bilayer"},
                   "stage_xy": [0, 0], "rel_corner_mm": [0, 0], "images": {}})
    elif legacy == "successful_empty_revisit":
        rid = s.store.create_run("revisit_chip", {"chip_id": "chip_001", "material": "Gr"})
        s.store.set_run_status(rid, "succeeded")
    assert_refused_without_physical_writes(s, flake_ids=ids)


@pytest.mark.parametrize("bad", ["other_chip", "other_flow", "other_material", "missing_verification",
                                  "missing_finish", "unclear", "incomplete", "missing_measurement"])
def test_scan_evidence_must_be_completed_verified_and_chip_bound(services, bad):
    s = services
    ledger_chip(s)
    ledger_chip(s, "chip_002")
    kwargs = {"other_chip": {"chip": "chip_002"}, "other_flow": {"flow": "revisit_chip"},
              "other_material": {"material": "hBN"}, "missing_verification": {"verified": False},
              "missing_finish": {"finished": False}, "unclear": {"verdict": "unclear"},
              "incomplete": {"tiles": 1}, "missing_measurement": {"tiles": None}}[bad]
    recorded_scan(s, **kwargs)
    assert_refused_without_physical_writes(s)


@pytest.mark.parametrize("change", ["reset", "replacement", "material_round_trip", "preparation_attempt"])
def test_prior_specimen_scan_and_candidates_do_not_authorize_revisit(services, change):
    s = services
    ledger_chip(s)
    recorded_scan(s, candidates=1)
    assert prior_scan_evidence(s.store, "chip_001", "Gr")
    if change == "reset":
        s.store.upsert_sample("chip_001", "chip", status="bare")
    elif change == "replacement":
        s.store.remove_sample("chip_001", reason="new specimen on reused identifier")
        ledger_chip(s, status="searched")
    elif change == "material_round_trip":
        s.store.upsert_sample("chip_001", "chip", props={"material": "hBN"})
        s.store.upsert_sample("chip_001", "chip", props={"material": "Gr"})
    else:
        rid = s.store.create_run("exfoliate_chips", {"chip_ids": ["chip_001"], "material": "Gr"})
        s.store.set_run_status(rid, "failed")
    s.store.upsert_sample("old_candidate", "flake", location="chip_001", status="candidate",
        props={"material": "Gr", "chip_id": "chip_001", "rel_corner_mm": [0, 0]})
    assert_refused_without_physical_writes(s, flake_ids=["old_candidate"])
    # A subsequent actual scan record belongs to the new specimen again.
    rid = recorded_scan(s)
    assert prior_scan_evidence(s.store, "chip_001", "Gr")["job_id"] == rid
    assert resolve(s).chip_id == "chip_001"


def test_old_verified_zero_yield_scan_survives_reopen_and_location_updates(tmp_path):
    path = tmp_path / "legacy.db"
    s = build_services(path)
    ledger_chip(s)
    rid = recorded_scan(s)
    s.store.upsert_sample("chip_001", "chip", location="mid_chip_2", props={"corner_stage_xy": [1, 2]})
    assert resolve(s).home_slot == "mid_chip_2"
    s.store.close()
    store = RunStore(path)
    evidence = prior_scan_evidence(store, "chip_001", "Gr")
    assert evidence["job_id"] == rid and evidence["candidates"] == 0
    assert not store.list_samples(kind="flake", location="chip_001")
    assert not store.get_sample("chip_001")["props"].get("scan_provenance")
    store.close()


def test_real_scan_with_zero_candidates_still_permits_a_revisit(services):
    s = services
    assert s.jobs.wait(s.jobs.submit("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"]}), 600).status == "succeeded"
    first = s.jobs.wait(s.jobs.submit("search_chip", {"chip_id": "chip_001", "min_area_um2": 1e12,
                                                     "max_revisits": 0}), 600)
    assert first.status == "succeeded"
    evidence = prior_scan_evidence(s.store, "chip_001", "Gr")
    assert evidence["job_id"] == first.run_id and evidence["tiles_scanned"] > 0 and evidence["candidates"] == 0
    assert not s.store.list_samples(kind="flake", location="chip_001")
    response = asyncio.run(s.gateway.call("hands", "search_chip", {"chip_id": "chip_001", "revisit_only": True}))[0]
    assert response.status == "pending"
    second = s.jobs.wait(response.data["job_id"], 600)
    assert second.status == "succeeded" and "scan_chip" not in [step["name"] for step in second.steps]
    assert prior_scan_evidence(s.store, "chip_001", "Gr")["job_id"] == first.run_id
    assert s.store.get_sample("chip_001")["status"] == "searched"
