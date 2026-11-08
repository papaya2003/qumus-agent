"""Bounded model views preserve complete original inventory and evidence."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from qumus2.agents.tooling import ToolContext
from qumus2.agents.tools.inventory import ListSamplesArgs, flake_catalogue_view, list_samples
from qumus2.agents.tools.picking import ViewArgs, candidate_view, view_flake_images


def flake():
    chain = {"algorithm_sha256": "a"*64, "selected_label_ids": [7],
        "image": {"path": "/original.png", "sha256": "b"*64},
        "original_mask": {"path": "/labels.png", "sha256": "c"*64},
        "geometry_mask": {"path": "/seed.png", "sha256": "d"*64}, "pixels": list(range(20000))}
    return {"id": "f1", "kind": "flake", "location": "chip1", "status": "catalogued", "updated_at": 1.,
        "props": {"material": "Gr", "labels": {"20x": "monolayer", "100x": "monolayer"},
            "area_um2": 500., "images": {"20x": "/20.png", "100x": "/100.png"},
            "optical_geometry": {"source": "rgb_native100_measured_region", "frame": "native100_centroid",
                "region_mm": {"components": [{"exterior": [[0., 0.], [.02, 0.], [.02, .03], [0., .03]],
                                              "holes": [[[.001, .001], [.002, .001], [.002, .002]]]}]},
                "boundary_uncertainty_mm": .001, "native_source_anchor_sha256": "e"*64},
            "recognition_20x": chain, "recognition_100x": copy.deepcopy(chain),
            "native_source_anchor_event_id": 17, "measurement_event_100x": 16,
            "measurement_100x": {"original_recognition_chain": chain, "receipt_artifact": {
                "path": "/measurement.json", "sha256": "f"*64}},
            "pickup_assessment": {"clear": True, "clear_radius_mm": .01, "full_image_data": [0]*20000},
            "unexpected_future_receipt": {"pixels": [0]*20000}}}


def test_pure_projection_retains_decisions_hashes_pointers_and_geometry_counts():
    original = flake()
    before = copy.deepcopy(original)
    result = flake_catalogue_view(original)
    props = result["props"]
    assert original == before
    assert props["labels"] == original["props"]["labels"]
    assert props["geometry_summary"]["bounds_mm"] == [0., 0., .02, .03]
    assert props["geometry_summary"]["vertices"] == 7
    assert props["geometry_summary"]["holes"] == props["geometry_summary"]["components"] == 1
    assert props["evidence"]["native_source_anchor_event_id"] == 17
    assert props["evidence"]["recognition_100x"]["artifact_sha256"]["geometry_mask"] == "d"*64
    assert props["evidence"]["measurement_100x"]["path"] == "/measurement.json"
    assert props["availability"]["pickup_neighbourhood_admitted"] is None
    assert props["availability"]["reason"] == "historical_availability_not_reassessed"
    assert result["record_reference"]["full_props_retained"] is True
    assert "region_mm" not in json.dumps(result) and "pixels" not in json.dumps(result)
    assert len(json.dumps(result)) < len(json.dumps(original))/50
    assert flake_catalogue_view(result) == result


@pytest.mark.parametrize("reason,admitted", [(None, True), ("stale observed evidence", False)])
def test_live_projection_uses_existing_current_admission_validator(monkeypatch, reason, admitted):
    calls = []
    store = object()
    def check(actual, row):
        assert actual is store and row["id"] == "f1"
        calls.append(row["id"])
        return reason
    monkeypatch.setattr("qumus2.records.materials.pickup_admission_error", check)
    result = flake_catalogue_view(flake(), store)
    assert result["props"]["availability"]["pickup_neighbourhood_admitted"] is admitted
    assert calls == ["f1"]
    assert flake_catalogue_view(result, store) == result and calls == ["f1"]


def test_tool_filters_and_projection_leave_raw_records_and_nonflakes_unchanged(monkeypatch):
    first = flake()
    other = copy.deepcopy(first)
    other.update(id="f2", status="lost")
    chip = {"id": "chip1", "kind": "chip", "location": "slot1", "status": "searched", "props": {"size_mm": [10., 10.]}}
    rows = [first, other, chip]
    before = copy.deepcopy(rows)
    def read(kind=None, location=None):
        return [r for r in rows if (kind is None or r["kind"] == kind) and (location is None or r["location"] == location)]
    store = SimpleNamespace(bench="sim", list_samples=read, get_meta=lambda _: None)
    ctx = ToolContext(store=store, jobs=None, agent="eyes")
    monkeypatch.setattr("qumus2.records.materials.pickup_admission_error", lambda *a: "missing")
    result = asyncio.run(list_samples(ListSamplesArgs(kind="flake", location="chip1", status="catalogued"), ctx))
    assert [r["id"] for r in result.data["samples"]] == ["f1"]
    assert result.data["detail_policy"]["retained"].startswith("Complete original")
    all_rows = asyncio.run(list_samples(ListSamplesArgs(), ctx)).data["samples"]
    assert all_rows[-1] == chip and rows == before


def test_picker_candidate_view_uses_same_projection_without_changing_source():
    original = flake()
    original["props"]["residue"] = {"level": "light", "evidence": "Observed edge residue", "by": "picker", "project_id": "project"}
    before = copy.deepcopy(original)
    result = candidate_view(original)
    assert result["material"] == "Gr" and result["id"] == "f1"
    assert result["geometry_summary"]["holes"] == 1
    assert result["residue"] == original["props"]["residue"]
    assert result["record_reference"]["full_props_retained"] is True
    assert "recognition_100x" not in result and original == before


def test_image_view_keeps_validation_and_attachments_but_compacts_returned_props(tmp_path, monkeypatch):
    original = flake()
    paths = {}
    for key in ("20x", "100x", "original_mask", "geometry_mask"):
        path = tmp_path/(key+".png")
        path.write_bytes(b"unit image")
        paths[key] = str(path)
    original["props"]["images"] = {key: paths[key] for key in ("20x", "100x")}
    chain = original["props"]["recognition_20x"]
    chain["image"]["path"] = paths["20x"]
    chain["original_mask"]["path"] = paths["original_mask"]
    chain["geometry_mask"]["path"] = paths["geometry_mask"]
    chain["derivation"] = {"status": "supported"}
    calls = []
    def validate(value, **kwargs):
        assert value is chain and len(value["pixels"]) == 20000
        calls.append(True)
    monkeypatch.setattr("qumus2.records.recognition.validate_recognition_chain", validate)
    ctx = ToolContext(store=SimpleNamespace(get_sample=lambda _: original), jobs=None, agent="picker")
    result = asyncio.run(view_flake_images(ViewArgs(flake_id="f1"), ctx))
    assert result.status == "ok" and calls == [True]
    assert set(result.images) == set(paths.values())
    assert "recognition_20x" not in result.data["props"]
    assert result.data["props"]["optical_geometry"]["region_mm"] == original["props"]["optical_geometry"]["region_mm"]
    assert result.data["record_reference"]["full_props_retained"] is True
