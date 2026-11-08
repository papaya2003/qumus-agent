"""Coarse observed stock stays pending; selection and pickup admission stay strict."""
import asyncio

import pytest
from test_picking_shortage import add_flake

from qumus2.agents.tooling import ToolContext
from qumus2.agents.tools.picking import (
    PENDING_REVISIT_LIMIT,
    SubmitSelectionArgs,
    candidates_by_role,
    validate_selection,
)


def call(s, name, args=None):
    return asyncio.run(s.gateway.call("picker", name, args or {}))[0]


@pytest.fixture
def stock(services, monkeypatch):
    monkeypatch.setattr("qumus2.agents.tools.picking.require_images", lambda: True)
    services.store.update_project_state(services.project_id, device_plan={"flake_roles": [{
        "role": "channel", "material": "Gr", "count": 1,
        "thickness_labels": ["monolayer"], "min_area_um2": 150}]},
        selection_blocked={"reason": "original"})
    return services


def coarse(s, fid="pending", *, label="monolayer", status="candidate", **props):
    s.store.upsert_sample(fid, "flake", location="chip_001", status=status, props={
        "material": "Gr", "thickness_label": label, "labels": {"10x": label},
        "area_um2": 300., "images": {}, **props})


def test_uninspected_10x_stock_is_visible_but_cannot_be_selected(stock):
    s = stock
    coarse(s)
    before = s.store.list_samples(kind="flake")
    result = call(s, "get_flake_candidates")
    assert result.data["candidates_by_role"]["channel"] == []
    item = result.data["pending_revisit_by_role"]["channel"][0]
    assert item["id"] == "pending" and item["selection_admitted"] is False
    assert item["evidence_status"] == "awaiting_optical_images"
    assert set(item["missing_objectives"]) == {"20x", "100x"}
    assert result.data["unphotographed_by_role"]["channel"] == [item]
    diagnostic = result.data["diagnostics_by_role"]["channel"]
    assert diagnostic["next_action"] == "relink_or_capture_optical_images"
    assert diagnostic["blocker_type"] == "image_evidence_repair"
    assert diagnostic["exfoliation_allowed"] is False
    request = result.data["revisit_requests_by_role"]["channel"][0]
    assert request["args"] == {"chip_id": "chip_001", "material": "Gr", "revisit_only": True,
                               "max_revisits": 1}
    assert request["explicit_target_args"]["flake_ids"] == ["pending"]
    ctx = ToolContext(s.store, s.jobs, "picker", s.project_id)
    assert candidates_by_role(ctx)["channel"] == []
    errors = validate_selection(ctx, SubmitSelectionArgs(selected_by_role={"channel": ["pending"]},
                                                       assessments=[], reasons="Unqualified control"))
    assert any("fresh inspection" in error for error in errors)
    assert s.store.list_samples(kind="flake") == before


def test_pending_revisit_prevents_exfoliation_and_reuses_existing_action(stock):
    s = stock
    coarse(s)
    result = call(s, "report_selection_blocked", {"role": "channel",
        "action": "exfoliate_and_search_one_chip", "reason": "No admitted pair"})
    assert result.status == "error" and result.data["exfoliation_allowed"] is False
    assert s.store.get_project(s.project_id)["state"]["selection_blocked"] == {"reason": "original"}
    repaired = call(s, "report_selection_blocked", {"role": "channel",
        "action": "relink_or_capture_optical_images", "reason": "Normal targeted revisit required"})
    assert repaired.status == "ok"
    assert repaired.data["without_images"] == {"pending": "chip_001"}
    assert repaired.data["blocker_type"] == "image_evidence_repair"


def test_unknown_coarse_label_is_pending_but_detailed_mismatch_failed_and_excluded_are_distinct(stock):
    s = stock
    coarse(s, "unknown", label=None)
    coarse(s, "coarse_mismatch", label="manylayer")
    coarse(s, "thick", label="manylayer", labels={"20x": "manylayer"})
    coarse(s, "lost", status="lost", recognition_reason="No centered material")
    coarse(s, "failed", recognition_status="unsupported", recognition_reason="clipped_component")
    coarse(s, "vetoed")
    coarse(s, "small", area_um2=50.)
    coarse(s, "dirty", residue={"level": "heavy"})
    s.store.update_project_state(s.project_id, excluded_flakes=["vetoed"])
    data = call(s, "get_flake_candidates").data
    pending = {item["id"]: item for item in data["pending_revisit_by_role"]["channel"]}
    assert set(pending) == {"unknown", "coarse_mismatch"}
    assert pending["unknown"]["thickness_screen"] == "unknown"
    assert pending["coarse_mismatch"]["thickness_screen"] == "coarse_label_mismatch_unverified"
    counts = data["diagnostics_by_role"]["channel"]["public_disposition_counts"]
    assert counts["user_excluded"] == 1
    assert counts["revisit_failed_or_lost"] == 2
    assert counts["observed_thickness_unsuitable"] == 1
    assert counts["observed_area_below_minimum"] == 1


def test_current_occupied_or_heavy_stock_does_not_force_endless_revisit(stock, tmp_path):
    s = stock
    add_flake(s, tmp_path, "occupied")
    props = s.store.get_sample("occupied")["props"]
    s.store.upsert_sample("occupied", "flake", props={"pickup_assessment": {
        **props["pickup_assessment"], "clear": False, "assessment_status": "occupied"}})
    coarse(s, "heavy", residue={"level": "heavy"})
    data = call(s, "get_flake_candidates").data
    assert data["pending_revisit_by_role"]["channel"] == []
    assert data["diagnostics_by_role"]["channel"]["public_disposition_counts"]["observed_pickup_occupied"] == 1
    assert data["diagnostics_by_role"]["channel"]["exfoliation_allowed"] is True
    result = call(s, "report_selection_blocked", {"role": "channel",
        "action": "exfoliate_and_search_one_chip", "reason": "Current observed stock unsuitable"})
    assert result.status == "ok"


def test_pending_examples_are_bounded_without_claiming_exhaustion(stock):
    for index in range(PENDING_REVISIT_LIMIT+3):
        coarse(stock, f"pending_{index:03}")
    data = call(stock, "get_flake_candidates").data
    assert len(data["pending_revisit_by_role"]["channel"]) == PENDING_REVISIT_LIMIT
    diagnostic = data["diagnostics_by_role"]["channel"]
    assert diagnostic["pending_revisit_count"] == PENDING_REVISIT_LIMIT+3
    assert diagnostic["pending_revisit_omitted"] == 3
    assert diagnostic["exfoliation_allowed"] is False


def test_failed_pair_keeps_pending_replacements_visible_without_qualifying_them(stock, tmp_path):
    add_flake(stock, tmp_path, "photographed")
    coarse(stock)
    failure = {"layout_impossible": True, "flake_ids": ["photographed", "other"],
               "failed_roles": ["channel"], "replacement_requirements": {"channel": "Wider original mask"}}
    stock.store.update_project_state(stock.project_id, design_fit_failure=failure)
    data = call(stock, "get_flake_candidates").data
    assert [item["id"] for item in data["candidates_by_role"]["channel"]] == ["photographed"]
    assert data["diagnostics_by_role"]["channel"]["next_action"] == "relink_or_capture_optical_images"
    assert data["design_fit_failure"] == failure
    assert data["pending_revisit_by_role"]["channel"][0]["selection_admitted"] is False
