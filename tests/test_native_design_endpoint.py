"""Actual-region endpoint plumbing, with explicit pure measurement doubles."""
import copy
from types import SimpleNamespace

import pytest
from test_device_design_pose_screen import bound_design
from test_native_design_budget import native_spec

from qumus2.core.optical_profiles import V1_MATERIAL_CAPTURE_PROFILE
from qumus2.core.polygons import rectangle
from qumus2.core.regions import _geos, as_region, region_dict, transform_region
from qumus2.devices.base import Frame
from qumus2.flows.library import stacking
from qumus2.records.device_design import DeviceDesignSpec


def endpoint_fixture(monkeypatch):
    raw, verification = native_spec()
    design = bound_design(DeviceDesignSpec.model_validate(raw))
    monkeypatch.setattr(stacking, "_device_design_public_binding", lambda *a: design)
    ctx = SimpleNamespace(params={}, store=SimpleNamespace(get_run=lambda _: {}), run_id="unit", verification=verification)
    groups = [{"material": s.material, "outlines": [{"region_mm": region_dict(s.placed_region())}],
               "observation_binding": {"frame": "unit original RGB measurement"}}
              for _ in range(3) for s in design.sources]
    return ctx, design, {"materials_found": {"hBN": 0, "Gr": 0}}, {
        "observed_outlines": copy.deepcopy(groups), "device_design_digest": design.canonical_digest}


def test_endpoint_accepts_exact_regions_including_holes_without_placed_outline(monkeypatch):
    ctx, design, before, after = endpoint_fixture(monkeypatch)
    assert len(design.sources[0].region_mm.components[0].holes) == 1
    assert stacking._device_design_endpoint(ctx, before, after)["status"] == "pass"


@pytest.mark.parametrize("problem", ["shift", "rotation", "filled_hole", "coverage"])
def test_endpoint_refuses_changed_pose_topology_and_actual_coverage(monkeypatch, problem):
    ctx, _, before, after = endpoint_fixture(monkeypatch)
    for group in after["observed_outlines"]:
        body = group["outlines"][0]
        if problem == "shift":
            body["region_mm"] = region_dict(transform_region(body["region_mm"], xy=(.0011, 0)))
        elif problem == "rotation":
            region = body["region_mm"]
            centre = _geos(as_region(region)).centroid
            body["region_mm"] = region_dict(transform_region(transform_region(region, xy=(-centre.x, -centre.y)),
                rotation_deg=1.1, xy=(centre.x, centre.y)))
        elif problem == "filled_hole" and group["material"] == "hBN":
            body["region_mm"]["components"][0]["holes"] = []
        elif problem == "coverage" and group["material"] == "hBN":
            body["region_mm"]["components"][0]["holes"].append(rectangle(.001, .001, (.5, .5)))
    assert stacking._device_design_endpoint(ctx, before, after)["status"] == "fail"


def test_native_target_view_uses_measured_absolute_region_not_detection_offsets(tmp_path, monkeypatch):
    angle = 37.
    actual = SimpleNamespace(x=4., y=6., model_dump=lambda **k: {"x": 4., "y": 6.})
    chip = {"kind": "chip", "props": {"size_mm": [1., 1.], "corner_stage_xy": [4.3, 6.7], "rotation_deg": angle}}
    path = tmp_path/"actual.png"
    path.write_bytes(b"measurement pipeline double, not a physical observation")
    frame = Frame("microscope", 1., path=str(path), meta={"focus_z": 1., "objective": 20,
        "calibration": {"material_capture_profile": V1_MATERIAL_CAPTURE_PROFILE},
        "flakes": "forbidden private truth"})
    seed = {"components": [{"exterior": rectangle(.04, .03, (.004, -.002)),
        "holes": [rectangle(.002, .003, (.004, -.002))]}]}
    detection = SimpleNamespace(dx_mm=100., dy_mm=-100., meta={"outline_mm": "forbidden historical outline"})
    chain = {"image": "pure double"}
    an = SimpleNamespace(recognition_backend="v1_lab_rgb", find_flakes=lambda *a: [detection],
                         persist_detection_masks=lambda f, d: chain)
    # The target view uses the original v1 selected mask (no RGB refinement);
    # this double stands for that calibrated exposure-frame region.
    monkeypatch.setattr(stacking, "_original_mask_region", lambda c: seed if c is chain else None)
    ctx = SimpleNamespace(params={"device_design": {"canonical_digest": "unit"}}, run_id="unit",
        store=SimpleNamespace(get_sample=lambda _: chip, last_event_id=lambda: 1), analyzer=an,
        gate=SimpleNamespace(stage=lambda _: SimpleNamespace(get_sample_pose=lambda: actual),
            microscope=lambda _: SimpleNamespace(get_focus=lambda: 1.)),
        verification={"refind_flake": {"centre_tolerance_um": 1.}, "drop": {"stack_radius_um": 20.}})
    monkeypatch.setattr(stacking.GoToTarget, "_xy", lambda *a: (actual.x, actual.y))
    monkeypatch.setattr(stacking, "_capture_chip_focus", lambda *a, **k: (frame, stacking._frame_record(frame)))
    monkeypatch.setattr(stacking, "microscope_frame", lambda *a, **k: frame)
    monkeypatch.setattr(stacking, "_held_source", lambda *a: {"held": True})
    monkeypatch.setattr(stacking, "_pos", lambda *a: {"z": 8.})
    monkeypatch.setattr(stacking, "_require_source_plane", lambda *a: None)
    result = stacking._target_view(ctx, "chip", (.5, .5), ["Gr"], "unit", parked=True)
    from qumus2.flows.library.search import _stage_to_local
    origin = _stage_to_local((4.3, 6.7), angle, 4., 6.)
    expected = region_dict(transform_region(seed, rotation_deg=-angle, xy=origin))
    assert len(result["observed_outlines"]) == 3
    for group in result["observed_outlines"]:
        body = group["outlines"][0]
        assert body["region_mm"] == expected
        assert len(body["region_mm"]["components"][0]["holes"]) == 1
        assert body["geometry_source"] == "v1_original_mask" and body["recognition_chain"] == chain
    assert "forbidden private truth" not in str(result)
    assert "forbidden historical outline" not in str(result)
