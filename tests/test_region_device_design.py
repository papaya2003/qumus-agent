"""Additive regional design/DBU controls, using declared analytic unit shapes."""
import copy

import pytest
from test_device_design_pose_screen import VERIFICATION, declared_spec

from qumus2.core.polygons import rectangle
from qumus2.core.regions import as_region, region_dict
from qumus2.records.device_design import (
    DeviceDesignSpec,
    RegionDesignSource,
    layout_payload,
    screen_design_pose_window,
)


def regional_spec():
    legacy = declared_spec(hbn=rectangle(.200, .220))
    raw = legacy.model_dump(mode="json")
    for i, source in enumerate(raw["sources"]):
        outline = source.pop("outline_mm")
        holes = [rectangle(.002, .002, (-.085, -.095))] if i == 0 else []
        shape = region_dict(as_region({"version": "polygon-region-v1", "components": [
            {"exterior": outline, "holes": holes}]}))
        source["region_mm"] = shape
        source["observation"] = {"source": "declared_analytic_unit_region", "region_mm": shape,
            "boundary_uncertainty_mm": 1e-6,
            "boundary_uncertainty_provenance": {"source": "analytic_unit_fixture_not_observed_material"}}
    return DeviceDesignSpec.model_validate(raw)


def test_legacy_model_roundtrip_has_no_new_source_or_report_fields():
    old = declared_spec()
    before = old.model_dump(mode="json")
    assert DeviceDesignSpec.model_validate(before).model_dump(mode="json") == before
    assert all("region_mm" not in source for source in before["sources"])
    report = screen_design_pose_window(old, verification=VERIFICATION).model_dump(mode="json")
    assert all("boundary_uncertainty_mm" not in source for source in report["sources"])


def test_region_design_keeps_hole_and_combines_pose_and_boundary_budgets():
    spec = regional_spec()
    assert all(isinstance(source, RegionDesignSource) for source in spec.sources)
    assert len(spec.sources[0].placed_region().components[0].holes) == 1
    report = screen_design_pose_window(spec, verification=VERIFICATION)
    assert report.status == "pass"
    for source in report.sources:
        assert source.error_bound_mm == pytest.approx(source.pose_error_bound_mm+1e-6)
        assert source.boundary_uncertainty_mm == 1e-6
    payload = layout_payload(spec)
    assert payload["version"] == "qumus-device-layout-v3"
    assert len(payload["layers"][0]["region_components_dbu"][0]["holes"]) == 1
    assert payload["layers"][0]["quantization"]["boundary_displacement_bound_mm"] > 0


def test_hbn_hole_crossed_by_whole_gr_refuses_at_nominal_validation():
    raw = regional_spec().model_dump(mode="json")
    shape = copy.deepcopy(raw["sources"][0]["region_mm"])
    shape["components"][0]["holes"] = [rectangle(.002, .002)]
    raw["sources"][0]["region_mm"] = shape
    raw["sources"][0]["observation"]["region_mm"] = shape
    with pytest.raises(ValueError, match="complete Gr"):
        DeviceDesignSpec.model_validate(raw)


@pytest.mark.parametrize("change", ["missing", "zero", "negative", "nan", "provenance"])
def test_new_region_cannot_silently_assume_zero_boundary_uncertainty(change):
    raw = regional_spec().model_dump(mode="json")
    observation = raw["sources"][0]["observation"]
    if change == "missing":
        del observation["boundary_uncertainty_mm"]
    elif change == "provenance":
        observation["boundary_uncertainty_provenance"] = {}
    else:
        observation["boundary_uncertainty_mm"] = {"zero": 0, "negative": -1, "nan": float("nan")}[change]
    with pytest.raises(ValueError, match="boundary uncertainty"):
        DeviceDesignSpec.model_validate(raw)
