"""Shortage reports must use the required count and auditable optical assessments.

Images and assessments here are synthetic fixtures for contract checks, not
measurements or claims that an actual flake fits a device.
"""
from __future__ import annotations

import asyncio

import pytest
from PIL import Image


def call(s, tool, args):
    return asyncio.run(s.gateway.call("picker", tool, args))[0]


def add_flake(s, tmp_path, fid, *, images=True, material="Gr"):
    paths = {}
    if images:
        for mag in ("20x", "100x"):
            path = tmp_path / f"{fid}_{mag}.png"
            Image.new("RGB", (4, 4), "gray").save(path)
            paths[mag] = str(path)
    # Isolate shortage/optical assessment logic with an explicitly synthetic,
    # current successful search receipt. Missing detail images remain distinct
    # from missing pickup-neighbourhood evidence.
    context = tmp_path / f"{fid}_context.png"
    Image.new("RGB", (4, 4), "gray").save(context)
    search = s.store.create_run("search_chip", {"chip_id": "chip_001", "material": material},
                                project_id=s.project_id)
    s.store.set_run_status(search, "succeeded")
    s.store.upsert_sample(fid, "flake", location="chip_001", status="catalogued",
                          props={"material": material, "thickness_label": "monolayer",
                                 "area_um2": 200, "images": paths,
                                 "pickup_assessment": {"flake_id": fid, "source_chip_id": "chip_001",
                                     "images": [str(context)], "job_id": search, "clear": True}})


def set_count(s, count):
    plan = s.store.get_project(s.project_id)["state"]["device_plan"]
    plan["flake_roles"][0]["count"] = count
    s.store.update_project_state(s.project_id, device_plan=plan)


@pytest.fixture
def picker(services, tmp_path, monkeypatch):
    monkeypatch.setattr("qumus2.agents.tools.picking.require_images", lambda: True)
    services.store.update_project_state(services.project_id, device_plan={"flake_roles": [{
        "role": "channel", "material": "Gr", "count": 1,
        "thickness_labels": ["monolayer"], "min_area_um2": 150,
        "required_footprint": {"width_um": 33, "height_um": 4}}]},
        selection_blocked={"reason": "previous observation"})
    add_flake(services, tmp_path, "f1")
    return services


def view(s, *ids):
    for fid in ids:
        assert call(s, "view_flake_images", {"flake_id": fid}).status == "ok"


def assessment(fid="f1", **overrides):
    return {"flake_id": fid, "feasible": True, "geometry_fitness": 80,
            "visible_cleanliness": 80, "continuity_uniformity": 80,
            "evidence_confidence": 80, "inventory_tiebreaker": 50, "overall_score": 80,
            "reason": "20X continuous region fits the bridge; 100X surface is clean",
            "surface_residue": "clean", "residue_evidence": "100X: no visible residue",
            "usable_width_um": 34, "usable_height_um": 5,
            "geometry_evidence": "Synthetic test fixture: calibrated 20X continuous rectangle",
            **overrides}


def blocked(s, assessments=None, **overrides):
    args = {"role": "channel", "action": "exfoliate_and_search_one_chip",
            "reason": "A further chip is needed", **overrides}
    if assessments is not None:
        args["assessments"] = assessments
    return call(s, "report_selection_blocked", args)


def assert_rejected(s, result, message):
    assert result.status == "error", result.summary
    assert message in result.summary
    assert s.store.get_project(s.project_id)["state"]["selection_blocked"] == {
        "reason": "previous observation"}


def test_one_feasible_candidate_cannot_be_blocked_for_a_backup(picker):
    s = picker
    view(s, "f1")
    result = blocked(s, [assessment()], reason="One primary exists but the backup is missing")
    assert_rejected(s, result, "optional backup")
    assert result.data["required_count"] == 1
    assert s.store.get_project(s.project_id)["state"].get("selected_flakes", []) == []

    # A refused shortage never implies selection acceptance or weaker geometry checks.
    args = {"selected_by_role": {"channel": ["f1"]}, "reasons": "Required set",
            "assessments": [assessment(usable_height_um=2)]}
    assert "does not fit" in call(s, "submit_flake_selection", args).summary
    args["assessments"] = [assessment()]
    assert call(s, "submit_flake_selection", args).status == "ok"


@pytest.mark.parametrize("finding", [
    {"usable_height_um": 2, "reason": "Clean region is too narrow for the 4 um bridge",
     "observed_risks": ["20X: usable continuous region measures only 34 x 2 um"]},
    {"surface_residue": "heavy", "residue_evidence": "100X: residue covers the contact region",
     "reason": "Heavy contamination prevents clean transfer",
     "observed_risks": ["100X: residue touches the intended device region"]},
    {"evidence_confidence": 10, "reason": "Blur prevents a reliable continuity judgment",
     "observed_risks": ["Both images exist but focus is insufficient to resolve the outline"]},
])
def test_optically_infeasible_candidate_can_justify_shortage(picker, finding):
    s = picker
    view(s, "f1")
    a = assessment(feasible=False, **finding)
    result = blocked(s, [a])
    assert result.status == "ok", result.summary
    assert result.data["required_count"] == 1
    saved = s.store.get_project(s.project_id)["state"]["selection_blocked"]
    assert saved == result.data
    assert saved["assessments"][0]["feasible"] is False
    assert saved["assessments"][0]["observed_risks"] == finding["observed_risks"]


@pytest.mark.parametrize("count,candidates", [(2, 1), (1, 0)])
def test_genuine_numeric_shortage_needs_no_optical_rejection(picker, count, candidates):
    s = picker
    set_count(s, count)
    if not candidates:
        s.store.upsert_sample("f1", "flake", status="lost")
    result = blocked(s)
    assert result.status == "ok", result.summary
    assert result.data["required_count"] == count
    assert len(result.data["candidates"]) == candidates
    assert result.data["assessments"] == []


@pytest.mark.parametrize("case,expected", [
    ("missing", "missing: ['f1']"),
    ("partial", "missing: ['f2']"),
    ("duplicate", "duplicate assessment IDs"),
    ("unknown", "not current candidates"),
    ("other_material", "not current candidates"),
    ("unviewed", "call view_flake_images"),
    ("blank_reason", "nonblank assessment reason"),
    ("missing_risks", "observed_risks"),
])
def test_invalid_optical_assessments_do_not_authorize_more_chips(picker, tmp_path, case, expected):
    s = picker
    a = assessment(feasible=False, reason="Image shows an outline too narrow for the bridge",
                   observed_risks=["20X: continuous region is narrower than required"])
    assessments = [a]
    if case != "unviewed":
        view(s, "f1")
    if case == "missing":
        assessments = []
    elif case == "partial":
        add_flake(s, tmp_path, "f2")
        view(s, "f2")
    elif case == "duplicate":
        assessments.append(dict(a))
    elif case == "unknown":
        assessments.append({**a, "flake_id": "invented"})
    elif case == "other_material":
        add_flake(s, tmp_path, "hbn", material="hBN")
        view(s, "hbn")
        assessments.append({**a, "flake_id": "hbn"})
    elif case == "blank_reason":
        a["reason"] = "  "
    elif case == "missing_risks":
        a["observed_risks"] = ["  "]
    assert_rejected(s, blocked(s, assessments), expected)


def test_short_numeric_report_still_rejects_fabricated_assessments(picker):
    s = picker
    set_count(s, 2)
    assert_rejected(s, blocked(s, [assessment("invented")]), "not current candidates")
    assert_rejected(s, blocked(s, [assessment()]), "call view_flake_images")


def test_all_readable_candidates_count_not_just_rejected_subset(picker, tmp_path):
    s = picker
    add_flake(s, tmp_path, "f2")
    view(s, "f1", "f2")
    assessments = [assessment(), assessment("f2", feasible=False,
        reason="Crack crosses the channel", observed_risks=["20X: disconnected region"])]
    assert_rejected(s, blocked(s, assessments), "1 are assessed feasible")
    set_count(s, 2)
    assert blocked(s, assessments).status == "ok"


def test_missing_images_take_precedence_even_when_another_candidate_is_feasible(picker, tmp_path):
    s = picker
    add_flake(s, tmp_path, "f2", images=False)
    view(s, "f1")
    assert_rejected(s, blocked(s, [assessment()]), "report relink_or_capture_optical_images")
    result = blocked(s, action="relink_or_capture_optical_images")
    assert result.status == "ok"
    assert result.data["without_images"] == {"f2": "chip_001"}


def test_an_image_request_cannot_carry_an_unreadable_assessment(picker, tmp_path):
    s = picker
    add_flake(s, tmp_path, "f2", images=False)
    view(s, "f1")
    assert_rejected(s, blocked(s, [assessment(), assessment("f2")],
                              action="relink_or_capture_optical_images"), "no readable 20X/100X images")
