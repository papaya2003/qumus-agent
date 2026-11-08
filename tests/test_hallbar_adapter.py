"""Offline first-delivery adapter/admission checks, never release acceptance."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from qumus2.agents.openai_budget import SpendLedger
from qumus2.core.clock import SimClock
from qumus2.runtime.store import RunStore
from scripts import record_hallbar_demos as hallbar
from scripts.record_three_demos import TrialRegistry
from tests.test_model_demo_recording import request_events


@pytest.fixture
def target_bundle(tmp_path):
    from qumus2.testbench.hallbar import build_imported_hallbar_target
    from tests.test_imported_hallbar_target import declared_import

    bundle = tmp_path / "declared-import"
    bundle.mkdir()
    declared = declared_import()
    target = build_imported_hallbar_target(primitives=declared.metal_primitives,
        source_sha256=declared.import_receipt.source_sha256,
        owner_instruction_id=declared.import_receipt.owner_instruction_id,
        owner_instruction_sha256=declared.import_receipt.owner_instruction_sha256,
        size_mm=(10., 10.))
    (bundle / "target.json").write_text(json.dumps(target.model_dump(mode="json")))
    (bundle / "electrode-layout.gds").write_bytes(b"UNIT ONLY; NOT ACTUAL GDS")
    return bundle


def declared_campaign(directory, target_bundle):
    """Pure campaign identity for accounting/credential controls, without a bench."""
    directory.mkdir()
    initial, experience = directory / "initial.json", directory / "experience.json"
    initial.write_text(json.dumps({"objects": [], "stamps": {}}))
    experience.write_text(json.dumps({"declared_unit_library": True}))
    config = hallbar.simulation_config(target_bundle=target_bundle)
    identity = hallbar.source_identity(initial_state=initial, experience=experience,
        config=config, target_bundle=target_bundle)
    return hallbar.campaign_template(identity, campaign_path=directory / "attempts",
        initial_state=initial, experience=experience, config=config, target_bundle=target_bundle)


def test_prepare_is_tapes_only_unreleased_and_blocked_without_credentials(tmp_path, monkeypatch, target_bundle):
    from qumus2.agents import openai_credentials

    path = hallbar.prepare_campaign(tmp_path / "draft", target_bundle=target_bundle)
    campaign = json.loads(path.read_text())
    truth = json.loads(Path(campaign["initial_state"]).read_text())
    assert not any(obj["flakes"] for obj in truth["objects"])
    assert not any(stamp["stack"] for stamp in truth["stamps"].values())
    assert len(campaign["cells"]) == 15
    assert [c["id"] for c in campaign["cells"] if c["limits"] is not None] == [hallbar.FIRST_CELL]
    assert campaign["limits"]["estimated_usd"] == 100
    assert not (path.parent / "attempts").exists()
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", lambda *a: pytest.fail("Blocked draft read credentials"))
    result = hallbar.preflight(campaign)
    assert not result["ready_for_qualification"]
    for cell in campaign["cells"][1:]:
        assert not hallbar.preflight(campaign, cell_id=cell["id"])["ready_for_qualification"]
    with pytest.raises(ValueError, match="not ready"):
        asyncio.run(hallbar.run_cell(campaign, live=True, frozen_source=result["execution_source_sha256"]))
    assert not (path.parent / "attempts").exists()


def test_canonical_library_changes_content_but_not_seed_clock_dates():
    stores = [RunStore(":memory:", clock=SimClock(start=t)) for t in (100, 200)]
    try:
        first, second = [hallbar.experience_snapshot(store) for store in stores]
        assert first == second
        raw = stores[0].list_experiences()[0]
        original_dates = raw["created_at"], raw["updated_at"]
        stores[1]._x("UPDATE experience SET claim=claim || ' changed' WHERE id=?", (raw["id"],))
        assert hallbar.experience_snapshot(stores[1]) != first
        assert (stores[0].get_experience(raw["id"])["created_at"],
                stores[0].get_experience(raw["id"])["updated_at"]) == original_dates
        assert all("created_at" not in e for e in first["summary"]["experiences"] if e["author"] == "seed")
        stores[0].insert_experience(experience_id="manual", topic="equipment", claim="Synthetic owner advice",
            source="human", status="confirmed", author="synthetic owner")
        manual = hallbar.experience_snapshot(stores[0])
        stores[0]._x("UPDATE experience SET updated_at=updated_at+1 WHERE id='manual'")
        assert hallbar.experience_snapshot(stores[0]) != manual
    finally:
        for store in stores:
            store.close()


@pytest.fixture
def accounting(tmp_path, monkeypatch, target_bundle):
    """Bypass only local test-readiness to isolate actual registry/ledger math."""
    campaign = declared_campaign(tmp_path / "accounting-fixture", target_bundle)
    campaign.update(launch_authorized=True, integrated_acceptance=True, protocol_ready=True)
    adapter = hallbar.HallbarCampaignAdapter(campaign)
    monkeypatch.setattr(adapter, "check_ready", lambda: None)
    ledger = SpendLedger(tmp_path / "ledger.db", limit=100)
    registry = TrialRegistry(campaign["campaign_path"], model=campaign["first_cell_model"], profiles=hallbar.profiles(),
        limits_by_story={story: deepcopy(hallbar.FIRST_LIMITS) for story in hallbar.profiles()},
        campaign_contract=campaign, campaign_guard=adapter.guard)
    registry.release_first()
    manifest = registry.start("complete", source=adapter.expected_source, ledger=ledger.path,
                              limits=registry.limits_by_story["complete"])
    return adapter, registry, ledger, manifest


def unknown_send(accounting, amount=60):
    _, _, ledger, manifest = accounting
    path = Path(manifest["workspace"]) / "qumus.db"
    reservation = ledger.reserve(str(path), amount, 100)
    store = RunStore(path)
    for event in request_events("openai/gpt-6-astra", failed=True):
        if event["kind"] == "demo.http_admitted":
            event["payload"]["reservation_id"] = reservation
        store.emit(event["kind"], event["payload"], agent=event["agent"])
    store.close()
    return reservation


def test_inclusive100_charges_unknown_reservation_and_explicit_retry(accounting):
    adapter, registry, ledger, manifest = accounting
    reservation = unknown_send(accounting)
    used = adapter.guard(registry, manifest)
    assert used["estimated_usd"] == 60 and used["http_attempts"] == 1
    with pytest.raises(ValueError, match="estimated_usd.*exhausted"):
        adapter.guard(registry, manifest, next_usage={"estimated_usd": 41, "input_tokens": 1,
            "output_tokens": 1, "http_attempts": 1, "wall_seconds": 0})
    registry.finish("complete", passed=False, result={"machine_checks_passed": False, "elapsed_wall_s": 2})
    second = registry.start("complete", source=adapter.expected_source, ledger=ledger.path,
        limits=registry.limits_by_story["complete"], retry_failed=True)
    assert second["attempt_number"] == 2 and second["attempt_budget_usd"] == 40
    assert adapter.guard(registry, second)["estimated_usd"] == 60
    ledger.settle(reservation, 0)
    with pytest.raises(ValueError, match="retained unknown"):
        adapter.guard(registry, second)


def test_known_usage_cannot_erase_a_missing_settled_ledger_row(accounting):
    adapter, registry, ledger, manifest = accounting
    reservation = unknown_send(accounting)
    store = RunStore(Path(manifest["workspace"]) / "qumus.db")
    usage = next(e for e in request_events("openai/gpt-6-astra", known=True) if e["kind"] == "agent.request_usage")
    store.emit(usage["kind"], usage["payload"], agent=usage["agent"])
    store.close()
    ledger.settle(reservation, .0002)
    assert adapter.guard(registry, manifest)["estimated_usd"] == pytest.approx(.0002)
    with sqlite3.connect(ledger.path) as db:
        db.execute("DELETE FROM spend WHERE id=?", (reservation,))
    with pytest.raises(ValueError, match="retained unknown"):
        adapter.guard(registry, manifest)


def test_new_run_overrides_only_test_budget_and_unverified_cache_discount(tmp_path, monkeypatch, target_bundle):
    from qumus2.agents import openai_credentials
    from qumus2.agents.openai_credentials import OpenAICredentials
    from scripts import record_model_demos, record_three_demos

    campaign = declared_campaign(tmp_path / "credential-fixture", target_bundle)
    observed = []
    fake = SimpleNamespace(expected_source="synthetic-only", check_ready=lambda: None,
        source=lambda: "synthetic-only", guard=lambda *a, **kw: None, check_services=lambda *a: None,
        collect_evidence=lambda *a: None, qualify=lambda *a: None,
        prepare_attempt=lambda *a, **kw: None, config=hallbar.campaign_config(campaign))
    monkeypatch.setattr(hallbar, "HallbarCampaignAdapter", lambda *a, **kw: fake)
    monkeypatch.setattr(TrialRegistry, "require_delivery", lambda *a, **kw: None)
    monkeypatch.setattr(record_model_demos, "observed_qualification", lambda *a: {"synthetic": True})
    creds = OpenAICredentials("synthetic-never-sent", base_url=campaign["gateway"], api="responses",
        model_id=campaign["first_cell_model"], input_price=10, output_price=50,
        cached_input_price=1, test_budget_usd=40, context_window_tokens=None)
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", lambda *a: creds)
    async def offline_engine(*a, **kwargs):
        observed.append(kwargs)
        return {"machine_checks_passed": False, "synthetic_no_model_call": True}
    monkeypatch.setattr(record_three_demos, "record_demo", offline_engine)
    result = asyncio.run(hallbar.run_cell(campaign, live=True, frozen_source="synthetic-only"))
    assert result["synthetic_no_model_call"]
    passed = observed[0]["credentials"]
    assert passed.test_budget_usd == 100 and passed.cached_input_price is None
    assert passed.context_window_tokens is None and passed.input_price == 10 and passed.output_price == 50
    assert creds.test_budget_usd == 40 and creds.cached_input_price == 1


def receipt_events(actions, *, start=1, profile=None):
    events = []
    for action in actions:
        ident = start+len(events)
        events.extend([
            {"id": ident, "kind": "demo.action_pending", "payload": {"action": action}},
            {"id": ident+1, "kind": "fault.injected", "payload": {
                "fault": action["fault"], "params": action["params"], "demo_action": action["id"]}},
            {"id": ident+2, "kind": "demo.action_applied", "payload": {"action_id": action["id"], "event_id": ident+1}},
        ])
    return events


def search_case():
    events = receipt_events(hallbar.search_faults(1))
    events += [{"id": 10, "kind": "job.submitted", "run_id": "r1", "payload": {"flow": "search_chip", "params": {"chip_id": "source1"}}},
        {"id": 11, "kind": "notification", "run_id": "r1", "payload": {"text": "arm_right serial timeout"}},
        {"id": 12, "kind": "notification", "run_id": "r1", "payload": {"text": "arm_right pick came up empty"}},
        {"id": 13, "kind": "step.corrected", "run_id": "r1", "payload": {"name": "place_chip_on_transfer_stage", "action": "re-pick and re-place"}},
        {"id": 14, "kind": "job.status", "run_id": "r1", "payload": {"status": "succeeded"}}]
    runs = [{"id": "r1", "flow": "search_chip", "params": {"chip_id": "source1"}, "status": "succeeded"}]
    return events, runs


@pytest.mark.parametrize("change", ["late_receipts", "cross_job", "early_second_chain"])
def test_fault_receipts_cannot_borrow_incidental_search_success(change):
    events, runs = search_case()
    assert hallbar.recovery_evidence(events, runs, 1)["verified"]
    if change == "late_receipts":
        for event in events[:9]:
            event["id"] += 20
            if event["kind"] == "demo.action_applied":
                event["payload"]["event_id"] += 20
    elif change == "cross_job":
        events[10]["run_id"] = "another"
    else:
        second = receipt_events(hallbar.search_faults(2), start=30)
        # Valid receipt triples before search1 are not search2's trigger window.
        for event in events[9:]:
            event["id"] += 40
        events = [*events, *second]
        events += [{"id": 60, "kind": "job.submitted", "run_id": "r2", "payload": {"flow": "search_chip", "params": {"chip_id": "source2"}}}]
        assert not hallbar.recovery_evidence(sorted(events, key=lambda e: e["id"]), runs, 2)["verified"]
        return
    assert not hallbar.recovery_evidence(sorted(events, key=lambda e: e["id"]), runs, 1)["verified"]


def test_human_escalation_follows_actual_second_search_offline_failure():
    events, runs = search_case()
    events += receipt_events([hallbar.profiles()["honest_stop"]["events"][-1]], start=15)
    events += [{"id": 18, "kind": "job.submitted", "run_id": "r2", "payload": {"flow": "search_chip", "params": {"chip_id": "source2"}}},
        {"id": 19, "kind": "step.started", "run_id": "r2", "payload": {"name": "chip_to_microscope", "seq": 4}},
        {"id": 20, "kind": "step.finished", "run_id": "r2", "payload": {"name": "chip_to_microscope", "seq": 4, "status": "failed", "note": "microscope offline"}},
        {"id": 21, "kind": "job.status", "run_id": "r2", "payload": {"status": "parked", "message": "microscope offline"}},
        {"id": 22, "kind": "notification", "run_id": "r2", "payload": {"level": "needs_human", "text": "microscope offline"}}]
    runs += [{"id": "r2", "flow": "search_chip", "params": {"chip_id": "source2"}, "status": "parked"}]
    assert hallbar.human_stop_evidence(events, runs)["verified"]
    earlier = deepcopy(events)
    earlier[-1]["id"] = 1
    assert not hallbar.human_stop_evidence(sorted(earlier, key=lambda e: e["id"]), runs)["verified"]
    failed = deepcopy(events)
    failed[-3]["run_id"] = "another"
    assert not hallbar.human_stop_evidence(failed, runs)["verified"]


def test_terminal_rigid_body_uses_frozen_pose_tolerance_without_shape_loss(tmp_path):
    from qumus2.core.polygons import transform
    from qumus2.records.device_design import DeviceDesignV1
    from tests.test_hallbar_recording import terminal_fixture

    store, design, truth = terminal_fixture(tmp_path)
    frozen = DeviceDesignV1.model_validate(design).pose_tolerance
    tolerance_mm, within = frozen.translation_um/1000, frozen.rotation_deg/2
    try:
        for flake in truth["objects"][0]["flakes"]:
            flake["outline_mm"] = transform(flake["outline_mm"], rotation_deg=within)
        score = hallbar.truth_geometry(truth, design, placement_tolerance_mm=tolerance_mm)
        assert score["verified"]
        assert all(abs(angle) == pytest.approx(within) for angle in score["rotation_error_deg"].values())
        for flake in truth["objects"][0]["flakes"]:
            flake["outline_mm"] = transform(flake["outline_mm"], rotation_deg=2*frozen.rotation_deg)
        assert not hallbar.truth_geometry(truth, design, placement_tolerance_mm=tolerance_mm)["verified"]
        with pytest.raises(ValueError, match="frozen translation"):
            hallbar.truth_geometry(truth, design, placement_tolerance_mm=tolerance_mm+.004)
    finally:
        store.close()
