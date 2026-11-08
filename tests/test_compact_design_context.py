"""Designer receives one exact body without duplicated native evidence trees."""
import asyncio
import copy
from types import SimpleNamespace

from test_inventory_catalogue import flake

from qumus2.agents.tools.design import (
    NoArgs,
    _design_source_view,
    get_design_context,
    model_device_design_view,
)


def test_selected_context_keeps_exact_body_placements_and_targets_without_receipt_duplication():
    source = flake()
    source["props"]["optical_geometry"].update(units="mm", owner_error_budget={"inclusive_error_budget_mm": .001})
    before = copy.deepcopy(source)
    placements = [{"flake_id": "f1", "translation_mm": [.5, .5], "rotation_deg": 37.}]
    state = {"selected_by_role": {"channel": ["f1"]}, "selection_design_placements": placements,
             "device_plan": {"channel": "unchanged"}}
    target = {"id": "target", "props": {"electrodes": {"unchanged": True}, "images": {}}}
    store = SimpleNamespace(get_project=lambda _: {"state": state}, get_sample=lambda _: source,
                            list_samples=lambda **kw: [target])
    result = asyncio.run(get_design_context(NoArgs(), SimpleNamespace(store=store, project_id="unit")))
    view = result.data["selected_by_role"]["channel"][0]
    assert view["optical_geometry"]["region_mm"] == source["props"]["optical_geometry"]["region_mm"]
    assert view["optical_geometry"]["boundary_uncertainty_mm"] == .001
    assert view["optical_geometry"]["frame"] == "native100_centroid"
    assert "recognition_100x" not in view and "measurement_100x" not in view
    assert "recognition_chain" not in view["optical_geometry"]
    assert result.data["selection_design_placements"] == placements
    assert result.data["target_chips"] == [target] and source == before
    legacy = copy.deepcopy(source)
    geometry = legacy["props"]["optical_geometry"]
    geometry["outline_mm"] = geometry.pop("region_mm")["components"][0]["exterior"]
    assert _design_source_view(legacy)["optical_geometry"]["outline_mm"] == geometry["outline_mm"]


def test_design_record_projection_preserves_identity_exact_geometry_and_original_record():
    source = flake()
    observation = source["props"]["optical_geometry"]
    observation["recognition_chain"] = source["props"]["recognition_100x"]
    record = {"version": "device_design_v1", "design_id": "original", "canonical_digest": "a"*64,
        "target": {"original": True}, "artifact": {"path": "original.gds", "sha256": "b"*64},
        "sources": [{"flake_id": "f1", "region_mm": observation["region_mm"], "observation": observation,
            "placement": {"translation_mm": [.5, .5], "rotation_deg": 37.}, "evidence_event_ids": [17], "image_sha256": "c"*64}]}
    before = copy.deepcopy(record)
    view = model_device_design_view(record)
    assert record == before and view["canonical_digest"] == record["canonical_digest"]
    assert view["sources"][0]["region_mm"] == observation["region_mm"]
    assert "region_mm" not in view["sources"][0]["observation"]
    assert "recognition_chain" not in view["sources"][0]["observation"]
    assert view["artifact"] == record["artifact"] and view["target"] == record["target"]
    assert view["sources"][0]["placement"] == record["sources"][0]["placement"]
    assert view["full_record_retained"] is True and model_device_design_view(view) == view
