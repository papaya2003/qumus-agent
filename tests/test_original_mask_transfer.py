"""Original-mask transfer geometry must retain topology and producer limits."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_owner20_mask_source import source20
from test_recognition_provenance import outlined_evidence

from qumus2.core.regions import _geos
from qumus2.flows.library.stacking import _original_mask_region
from qumus2.records.device_design import (
    source_additional_boundary_uncertainty,
    source_boundary_uncertainty,
)


def test_transfer_uses_original_mask_holes_without_rgb_refinement(tmp_path, monkeypatch):
    observation = outlined_evidence(tmp_path, hole=True)
    monkeypatch.setattr("qumus2.perception.measurement.measure_recognition_boundary",
                        lambda *a, **k: pytest.fail("Original-mask transfer called RGB refinement"))
    region = _original_mask_region(observation["recognition_chain"])
    assert len(region.components[0].holes) == 1
    assert _geos(region).area > 0


@pytest.mark.parametrize("explicit_support", [False, True])
def test_internal_roi_clipping_cannot_be_promoted_to_complete_mask(tmp_path, explicit_support):
    chain = deepcopy(outlined_evidence(tmp_path)["recognition_chain"])
    if explicit_support:
        chain["recognizer_support"] = {"whole_body_status": "unsupported"}
    else:
        chain["derivation"].update(status="unsupported", unsupported_reason="recovery_roi_clipped")
    with pytest.raises(ValueError, match="complete supported"):
        _original_mask_region(chain)


def test_original20_owner_budget_is_not_charged_twice(tmp_path):
    store, result, _ = source20(tmp_path)
    try:
        source = SimpleNamespace(observation=result["optical_geometry"])
        assert source_boundary_uncertainty(source) == .001
        assert source_additional_boundary_uncertainty(source) == 0
        source.observation["boundary_uncertainty_mm"] = 0
        with pytest.raises(ValueError):
            source_additional_boundary_uncertainty(source)
    finally:
        store.close()


def test_historical_derived100_geometry_cannot_reenter_active_selection():
    from qumus2.records.geometry import _pickup_region

    flake = {"props": {"optical_geometry": {
        "source": "rgb_native100_measured_region", "region_mm": {}}}}
    with pytest.raises(ValueError, match="fresh normal20X"):
        _pickup_region(None, flake)
