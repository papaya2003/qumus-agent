"""Rigid preservation is independent of synthetic placement/orientation tolerance."""
import math

import pytest

from qumus2.core.polygons import convex_polygon, rectangle, rigid_polygon_error, transform
from qumus2.records.device_design import bind_artifact, validate_design_current
from tests.test_device_design import design_fixture, fake_artifact, save_design

BODY = convex_polygon([(-.010, -.020), (.013, -.018), (.009, .021), (-.008, .015)])


def test_rigid_fit_allows_pose_without_scaling_or_changing_complete_shape():
    actual = transform(BODY, rotation_deg=1.2, xy=(.003, -.002))
    fit = rigid_polygon_error(BODY, actual)
    assert fit["shape_residual_mm"] < 1e-12
    assert fit["rotation_deg"] == pytest.approx(1.2)
    assert math.hypot(*fit["translation_mm"]) < .006


def test_crop_scale_and_reflection_cannot_use_localization_tolerance():
    for actual in (convex_polygon([(x*.999, y*.999) for x, y in BODY]),
                   convex_polygon([(-x, y) for x, y in BODY])):
        assert rigid_polygon_error(BODY, actual)["shape_residual_mm"] > 1e-9
    assert rigid_polygon_error(BODY, convex_polygon(BODY[:3]))["shape_residual_mm"] is None


def test_rotation_error_is_separately_observable():
    fit = rigid_polygon_error(BODY, transform(BODY, rotation_deg=2.1))
    assert fit["shape_residual_mm"] < 1e-12 and abs(fit["rotation_deg"]) > 2


def test_symmetric_rectangle_uses_smallest_geometrically_equivalent_rotation():
    body = rectangle(.02, .04, (.5, .5))
    fit = rigid_polygon_error(body, transform(rectangle(.02, .04), xy=(.5, .5), rotation_deg=1))
    assert fit["rotation_deg"] == pytest.approx(1)


def test_frozen_tolerance_cannot_inherit_later_configuration(tmp_path, monkeypatch):
    import qumus2.config as config

    store, project, _, spec = design_fixture(tmp_path)
    design = bind_artifact(spec, fake_artifact(store, spec))
    save_design(store, project, design)
    current = config.load_yaml("verification.yaml")
    assert design.pose_tolerance.translation_um == current["refind_flake"]["centre_tolerance_um"]
    assert design.pose_tolerance.rotation_deg == current["design_pose_window"]["rotation_deg"]
    original = config.load_yaml
    def changed(name):
        values = original(name)
        if name == "verification.yaml":
            values["design_pose_window"]["rotation_deg"] += 2
        return values
    monkeypatch.setattr(config, "load_yaml", changed)
    with pytest.raises(ValueError, match="changed"):
        validate_design_current(store, project)
