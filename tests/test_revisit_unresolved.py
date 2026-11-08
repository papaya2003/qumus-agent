"""#117: a targeted revisit that still cannot decide the neighbourhood must not
block exfoliation forever. Fixture images/evidence are synthetic contract inputs."""
from __future__ import annotations

import asyncio

import pytest
from PIL import Image


def call(s, tool, args):
    return asyncio.run(s.gateway.call("picker", tool, args))[0]


def add_unresolved(s, tmp_path, fid, flow):
    paths = {}
    for mag in ("20x", "100x"):
        path = tmp_path / f"{fid}_{mag}.png"
        Image.new("RGB", (4, 4), "gray").save(path)
        paths[mag] = str(path)
    run = s.store.create_run(flow, {"chip_id": "chip_001", "material": "Gr"}, project_id=s.project_id)
    s.store.set_run_status(run, "succeeded")
    views = [{"reason": "observed_boundary_unavailable", "detections": 2}] * 3
    s.store.upsert_sample(fid, "flake", location="chip_001", status="catalogued", props={
        "material": "Gr", "thickness_label": "monolayer", "area_um2": 800, "images": paths,
        "labels": {"20x": "monolayer", "100x": "monolayer"},
        "pickup_assessment": {"flake_id": fid, "source_chip_id": "chip_001", "images": [paths["20x"]],
                              "job_id": run, "clear": False, "assessment_status": "unknown",
                              "neighbours_per_view": [-1, -1, -1], "views": views}})


@pytest.fixture
def picker(services, monkeypatch):
    monkeypatch.setattr("qumus2.agents.tools.picking.require_images", lambda: True)
    services.store.update_project_state(services.project_id, device_plan={"flake_roles": [{
        "role": "channel", "material": "Gr", "count": 1, "thickness_labels": ["monolayer"]}]})
    return services


def exfoliate_request(s):
    return call(s, "report_selection_blocked", {"role": "channel", "action": "exfoliate_and_search_one_chip",
                                                "reason": "No admissible channel flake"})


def test_unresolved_after_first_search_still_asks_for_one_revisit(picker, tmp_path):
    add_unresolved(picker, tmp_path, "f1", "search_chip")
    result = exfoliate_request(picker)
    assert result.status == "error" and "targeted revisit" in result.summary
    data = call(picker, "get_flake_candidates", {}).data
    assert [i["id"] for i in data["pending_revisit_by_role"]["channel"]] == ["f1"]
    assert data["revisit_unresolved_by_role"] == {}


def test_unresolved_after_targeted_revisit_no_longer_blocks_exfoliation(picker, tmp_path):
    add_unresolved(picker, tmp_path, "f1", "revisit_chip")
    data = call(picker, "get_flake_candidates", {}).data
    assert data["pending_revisit_by_role"]["channel"] == []
    unresolved = data["revisit_unresolved_by_role"]["channel"]
    assert [(i["id"], i["evidence_status"], i["reasons"]) for i in unresolved] == [
        ("f1", "revisit_tried_unresolved", ["observed_boundary_unavailable"])]
    assert data["candidates_by_role"]["channel"] == []  # still never admitted for pickup
    assert data["diagnostics_by_role"]["channel"]["next_action"] == "exfoliate_and_search_one_chip"
    result = exfoliate_request(picker)
    assert result.status == "ok", result.summary
    assert result.data["exfoliation_allowed"] is True
