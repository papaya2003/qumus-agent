"""Hall-bar scoring negative controls; fixtures are never process acceptance."""
from __future__ import annotations

from copy import deepcopy

import pytest

from qumus2.records.device_design import bind_artifact
from qumus2.testbench.hallbar import electrode_pattern_from_target
from scripts.record_hallbar_demos import MODELS, profiles, search_submissions, truth_geometry
from tests.test_device_design import design_fixture, fake_artifact


def terminal_fixture(tmp_path):
    store, _, _, spec = design_fixture(tmp_path)
    design = bind_artifact(spec, fake_artifact(store, spec))
    target = {"id": design.target_chip_id, "kind": "chip", "size_mm": list(design.target.size_mm),
              "electrode_geometry": {"polygons": electrode_pattern_from_target(design.target)},
              "flakes": [{"id": s.flake_id, "material": s.material,
                          "outline_mm": list(s.outline_mm),
                          "x_mm": s.placement.translation_mm[0] - .5,
                          "y_mm": s.placement.translation_mm[1] - .5} for s in design.sources]}
    return store, design, {"objects": [target], "stamps": {}}


def test_protocol_five_models_short_goal_and_shared_failure_prefix():
    scenario = profiles()
    assert len(MODELS) == 5
    assert len(scenario["complete"]["prompt"].split()) <= 20
    assert len(scenario["collaboration"]["prompt"].split()) <= 5
    assert "setup" not in scenario["complete"]
    assert scenario["honest_stop"]["events"][:-1] == scenario["complete"]["events"][:3]
    assert scenario["honest_stop"]["events"][-1]["fault"] == "device_offline"
    assert not any(a.get("fault") == "device_offline" for a in scenario["complete"]["events"])


def test_search_boundaries_follow_distinct_sources_not_failed_retries():
    events = [{"id": i, "kind": "job.submitted", "run_id": f"r{i}",
               "payload": {"flow": "search_chip", "params": {"chip_id": chip}}}
              for i, chip in enumerate(("hbn", "hbn", "gr", "hbn"), 1)]
    assert [e["run_id"] for e in search_submissions(events)] == ["r1", "r3"]


def test_terminal_actual_polygons_required(tmp_path):
    store, design, truth = terminal_fixture(tmp_path)
    try:
        score = truth_geometry(truth, design, placement_tolerance_mm=.006)
        assert score["verified"] and len(score["contact_overlap_um2"]) == 8
    finally:
        store.close()


def test_catalogue_ids_bind_to_distinct_actual_simulated_flakes(tmp_path):
    store, design, truth = terminal_fixture(tmp_path)
    identities = {design.pickup_order[0]: "physical_hbn", design.pickup_order[1]: "physical_gr"}
    for flake in truth["objects"][0]["flakes"]:
        flake["id"] = identities[flake["id"]]
    try:
        assert truth_geometry(truth, design, placement_tolerance_mm=.006, physical_ids=identities)["verified"]
        assert not truth_geometry(truth, design, placement_tolerance_mm=.006)["verified"]
        duplicate = dict.fromkeys(design.pickup_order, "physical_hbn")
        assert not truth_geometry(truth, design, placement_tolerance_mm=.006, physical_ids=duplicate)["verified"]
    finally:
        store.close()


@pytest.mark.parametrize("change", ["extra_layer", "wrong_order", "wrong_material", "retained_source",
    "retained_stamp", "missing_contact", "changed_contact", "wrong_size", "cropped_outline", "misplaced"])
def test_no_ledger_success_can_hide_wrong_terminal_geometry(tmp_path, change):
    store, design, truth = terminal_fixture(tmp_path)
    target = truth["objects"][0]
    if change == "extra_layer":
        target["flakes"].append({"id": "unselected", "material": "hBN"})
    elif change == "wrong_order":
        target["flakes"].reverse()
    elif change == "wrong_material":
        target["flakes"][0]["material"] = "Gr"
    elif change == "retained_source":
        truth["objects"].append({"id": "source", "flakes": [deepcopy(target["flakes"][0])]})
    elif change == "retained_stamp":
        truth["stamps"]["stamp"] = {"stack": [{"flake": deepcopy(target["flakes"][0])}]}
    elif change == "missing_contact":
        target["electrode_geometry"]["polygons"].pop()
    elif change == "changed_contact":
        target["electrode_geometry"]["polygons"][0]["points"][0][0] += .001
    elif change == "wrong_size":
        target["size_mm"][0] = 2
    elif change == "cropped_outline":
        # Still covers Gr and all contacts; a 1% shape loss is not a rigid transfer.
        target["flakes"][0]["outline_mm"] = [(x*.99, y*.99) for x, y in target["flakes"][0]["outline_mm"]]
    else:
        for flake in target["flakes"]:
            flake["x_mm"] += .01
    try:
        assert not truth_geometry(truth, design, placement_tolerance_mm=.006)["verified"]
    finally:
        store.close()
