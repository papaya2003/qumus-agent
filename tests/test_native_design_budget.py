"""Owner-inclusive budget accounting; analytic geometry, no physical certificate."""
import copy
from types import SimpleNamespace

import pytest
from test_device_design_pose_screen import VERIFICATION
from test_region_device_design import regional_spec

from qumus2.perception.measurement import owner_error_budget_policy
from qumus2.records.device_design import (
    DeviceDesignSpec,
    FlakePlacement,
    _source,
    layout_payload,
    screen_design_pose_window,
    source_additional_boundary_uncertainty,
    source_boundary_uncertainty,
)


def native_spec():
    raw = regional_spec().model_dump(mode="json")
    raw["pose_tolerance"].update(translation_um=1., rotation_deg=1.)
    for source in raw["sources"]:
        source["observation"].update(source="rgb_native100_measured_region",
            frame="native100_derived_centroid_chip_axes_v1", owner_error_budget=owner_error_budget_policy(),
            boundary_uncertainty_mm=.001, boundary_uncertainty_provenance=owner_error_budget_policy())
    verification = copy.deepcopy(VERIFICATION)
    verification["refind_flake"]["centre_tolerance_um"] = 1.
    verification["vision_control"]["residual_rotation_deg"] = 1.
    return raw, verification


def test_owner_budget_is_shape_allowance_but_not_added_twice_to_pose():
    raw, verification = native_spec()
    spec = DeviceDesignSpec.model_validate(raw)
    report = screen_design_pose_window(spec, verification=verification)
    assert report.status == "pass"
    for original, source in zip(spec.sources, report.sources, strict=True):
        assert source_boundary_uncertainty(original) == .001
        assert source_additional_boundary_uncertainty(original) == 0.
        assert source.boundary_uncertainty_mm == 0.
        assert source.error_bound_mm == source.pose_error_bound_mm
        assert source.boundary_uncertainty_provenance["scientific_qualification"] == "not_calibrated_or_certified"
    payload = layout_payload(spec)
    assert payload["version"] == "qumus-device-layout-v3"
    assert len(payload["layers"][0]["region_components_dbu"][0]["holes"]) == 1


@pytest.mark.parametrize("change", ["missing_policy", "changed_policy", "zero_bound"])
def test_unbound_native_budget_cannot_gain_zero_additional_allowance(change):
    raw, _ = native_spec()
    observation = raw["sources"][0]["observation"]
    if change == "missing_policy":
        del observation["owner_error_budget"]
    elif change == "changed_policy":
        observation["owner_error_budget"]["inclusive_error_budget_mm"] = .002
    else:
        observation["boundary_uncertainty_mm"] = 0.
    with pytest.raises(ValueError, match="inclusive engineering error budget"):
        DeviceDesignSpec.model_validate(raw)


@pytest.mark.parametrize("axis", ["translation", "rotation"])
def test_pose_window_cannot_spend_less_than_the_owner_inclusive_budget(axis):
    raw, verification = native_spec()
    if axis == "translation":
        raw["pose_tolerance"]["translation_um"] = .5
        verification["refind_flake"]["centre_tolerance_um"] = .5
    else:
        raw["pose_tolerance"]["rotation_deg"] = .5
        verification["vision_control"]["residual_rotation_deg"] = .5
    with pytest.raises(ValueError, match="smaller than the inclusive owner budget"):
        screen_design_pose_window(raw, verification=verification)


def test_historical_native100_source_is_refused_for_new_designs(tmp_path):
    """The 11-02 original20-mask policy no longer admits derived 100X regions."""
    raw, _ = native_spec()
    body = raw["sources"][0]
    image = tmp_path/"original100.png"
    image.write_bytes(b"UNIT IMAGE BYTES; pixel estimator is independently tested")
    observation = body["observation"]
    observation.update(image=str(image), job_id="original-job", native_source_anchor_event_id=17)
    flake = {"id": body["flake_id"], "kind": "flake", "location": "original-chip",
             "props": {"material": "hBN", "optical_geometry": observation}}
    store = SimpleNamespace(get_sample=lambda fid: flake if fid == body["flake_id"] else None,
        _x=lambda sql, args: SimpleNamespace(fetchone=lambda: {"id": 19}))
    with pytest.raises(ValueError, match="Historical derived100 geometry is not admitted"):
        _source(store, FlakePlacement.model_validate(body["placement"]))

