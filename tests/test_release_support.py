"""Portable conditional release controls; no laboratory, World or job execution."""
from copy import deepcopy

import pytest
from PIL import Image, ImageDraw

from qumus2.core.optical_profiles import RELEASE_SUPPORT_ASSUMPTIONS, SYNTHETIC_CONTACT_PHOTOMETRY
from qumus2.flows.library import stacking as m
from qumus2.perception.contact_coverage import (
    InteriorCalibration,
    release_load_transport,
    release_support_evidence,
)
from qumus2.perception.tracking import PixelCalibration
from qumus2.records.device_design import bind_artifact
from tests.test_contact_context import AXIS, projection
from tests.test_device_design import design_fixture, fake_artifact

PIXELS = PixelCalibration(2.6 / 480, -1.72 / 320)
AXIS = {**AXIS, 'contact_slip_rate_bound_deg_s': 0.002}
LOAD = [{'flake_id': 'gr', 'material': 'Gr', 'outline_mm': [[-0.02, -0.01], [0.02, -0.01], [0.02, 0.01], [-0.02, 0.01]]}]

def transport(t=0.0, variation=0.0, **change):
    args = dict(axis=AXIS, elapsed_s=t, z_variation_mm=variation, noise_scale=1.0, footprint_size_mm=[0.65, 0.43], camera_size_mm=[2.6, 1.72])
    args.update(change)
    return release_load_transport(**args)

def pair(radius=96, front_only=False):
    """Declared renderer composition, including its unchanged blanket tint."""
    import io

    from qumus2.testbench.render import Canvas, _camera
    output=[]
    photo=SYNTHETIC_CONTACT_PHOTOMETRY
    for current in (False,True):
        canvas=Canvas(480,320,(122,96,168))
        canvas.rect(0,0,480,320,(150,160,196),.10)
        if current:
            canvas.disc(240,160,radius+max(2,radius*.06),photo.front_color,photo.front_opacity,segments=64)
            if not front_only:
                canvas.disc(240,160,radius,photo.interior_color,photo.interior_opacity,segments=64)
        canvas.polygon([(240+x/PIXELS.mm_per_pixel_x,160+y/PIXELS.mm_per_pixel_y) for x,y in LOAD[0]['outline_mm']],(222,210,172))
        _camera(canvas,2406001,1.)
        output.append(Image.open(io.BytesIO(canvas.png())).convert('RGB'))
    return tuple(output)


def measure(images, **change):
    args = dict(calibration=PIXELS, loaded=deepcopy(LOAD), optical=InteriorCalibration(), reference_transport=transport(), current_transport=transport(), profile=deepcopy(RELEASE_SUPPORT_ASSUMPTIONS))
    args.update(change)
    return release_support_evidence(*images, **args)

def test_conditional_positive_keeps_hidden_integrity_and_transfer_unknown():
    result = measure(pair())
    assert result['conditional_modeled_support'] is True, result
    assert result['optical_whole_interior_observed'] is False
    assert result['integrity'] == 'unknown' and result['transfer_confirmed'] is None
    assert result['recognition_tail_budget_centroid_only'] == 0.001
    assert result['camera_bounds']['derived_dn']['contact_rgb'] < 9

@pytest.mark.parametrize('kind', ['unchanged', 'front_only', 'small', 'visible_pocket', 'crack', 'saturation', 'reference_neighbour'])
def test_no_contact_or_visible_damage_is_not_healed_by_convex_hull(kind):
    reference, current = pair(radius=32 if kind == 'small' else 96, front_only=kind == 'front_only')
    if kind == 'unchanged':
        current = reference.copy()
    if kind == 'visible_pocket':
        current.putpixel((284, 161), reference.getpixel((284, 161)))
    if kind == 'crack':
        ImageDraw.Draw(current).line((284, 153, 284, 169), fill=(30, 22, 40))
    if kind == 'saturation':
        current.putpixel((284, 161), (255, 255, 255))
    if kind == 'reference_neighbour':
        reference.putpixel((286, 163), (255, 255, 255))
    result = measure((reference, current))
    assert result['conditional_modeled_support'] is False, result

def test_mask_substitution_is_recomputed_not_a_case_name_rejection():
    images = pair()
    original = measure(images)
    with pytest.raises(ValueError, match='independently reconstructed'):
        measure(images, claimed_mask_sha256='0' * 64)
    assert measure(images, claimed_mask_sha256=original['opaque_unknown_mask_sha256'])['conditional_modeled_support']

@pytest.mark.parametrize('change', [{'profile': {}}, {'loaded': []}, {'loaded': LOAD * 2}, {'loaded': [{**LOAD[0], 'material': 'metal'}]}, {'calibration': PixelCalibration(True, -0.005)}, {'calibration': PixelCalibration(2.6 / 480, 1.72 / 320)}, {'optical': InteriorCalibration(('10-20',))}])
def test_unbounded_or_wrong_declared_profiles_cannot_admit(change):
    with pytest.raises(ValueError):
        measure(pair(), **change)

def test_actual_clock_and_total_variation_increase_the_frozen_envelope():
    first = transport()
    later = transport(300.0, 1.5)
    assert first['centroid_envelope_mm'] == pytest.approx(0.01035843532639364)
    assert later['load_envelope_mm'] == pytest.approx(0.043113792156627)
    assert later['load_envelope_mm'] > first['load_envelope_mm']
    assert measure(pair(), current_transport=later)['required_clearance_mm'] > measure(pair())['required_clearance_mm']

@pytest.mark.parametrize('change', [{'elapsed_s': True}, {'noise_scale': 1.01}, {'z_variation_mm': float('nan')}, {'axis': {}}])
def test_transport_rejects_unknown_units_or_finite_declarations(change):
    with pytest.raises(ValueError):
        transport(**change)

def test_explicit_hallbar_dispatch_and_legacy_three_layer_boundary(tmp_path):
    store, _, _, design = design_fixture(tmp_path)
    try:
        design=bind_artifact(design,fake_artifact(store,design))
        raw = {'device_design': design.model_dump(mode='json'), 'flake_ids': list(design.pickup_order), 'materials': {s.flake_id: s.material for s in design.sources}, 'stamp_film': 'PC'}
        assert m._fresh_release_policy(raw) == m.V1_DROP_POLICY_ID  # #204: the v1 drop
        raw['flake_ids'].reverse()
        with pytest.raises(ValueError):
            m._fresh_release_policy(raw)
        assert m._fresh_release_policy({'flake_ids': ['a', 'b', 'c'], 'materials': {'a': 'hBN', 'b': 'Gr', 'c': 'hBN'}, 'stamp_film': 'PC'}) == m.RELEASE_POLICY_ID
        assert m._fresh_release_policy({'flake_ids': ['a', 'b'], 'materials': {'a': 'hBN', 'b': 'Gr'}, 'stamp_film': 'PC'}) == m.RELEASE_POLICY_ID
        assert m._fresh_release_policy({'stamp_film': 'mica'}) == m.RELEASE_POLICY_ID
    finally:
        store.close()

def test_valid_incomplete_front_is_false_but_corrupt_geometry_still_raises():
    front = {'has_contact': True, 'angular_coverage': 31 / 36, 'film_intact': None, 'interface_uncertainty_px': 1.1}
    result = projection(front=front)
    assert result['status'] == 'insufficient_observation' and result['admitted'] is False
    assert result['projection_displacement_bound_mm'] is None
    for broken in ({**front, 'angular_coverage': True}, {**front, 'interface_uncertainty_px': float('nan')}):
        with pytest.raises(ValueError):
            projection(front=broken)


@pytest.mark.parametrize("kind", ["r12_rings", "null", "partial"])
def test_r12_finite_white_light_cannot_waive_release_collar_or_interior(kind):
    """Actual optical forward function, no World or physical release claim."""
    import io

    from qumus2.testbench.optics import WHITE_LIGHT_PROFILE
    from qumus2.testbench.render import Canvas, MicroscopeRenderer, _camera

    images = []
    for current in (False, True):
        canvas = Canvas(480, 320, (122, 96, 168))
        canvas.rect(0, 0, 480, 320, (150, 160, 196), .10)
        if current and kind != "null":
            MicroscopeRenderer._white_light_rings(canvas, {"signed_apex_gap_mm": .0002,
                "dome_radius_mm": 12., "centre_dx_mm": 0., "centre_dy_mm": 0.,
                "current_contact_radius_mm": 0., "has_contact": False,
                "profile_id": WHITE_LIGHT_PROFILE}, 480/2.6, 320/1.72)
            if kind == "partial":
                photo = SYNTHETIC_CONTACT_PHOTOMETRY
                canvas.disc(240, 160, 32, photo.front_color, photo.front_opacity, segments=64)
                canvas.disc(240, 160, 30, photo.interior_color, photo.interior_opacity, segments=64)
        canvas.polygon([(240+x/PIXELS.mm_per_pixel_x, 160+y/PIXELS.mm_per_pixel_y)
                        for x, y in LOAD[0]["outline_mm"]], (222, 210, 172))
        _camera(canvas, 2406001, 1.)
        images.append(Image.open(io.BytesIO(canvas.png())).convert("RGB"))
    result = measure(images)
    assert result["conditional_modeled_support"] is False
    assert result["collar_mm"] == .1752
    assert result["erosion_mm"] == .05963091755805144
    assert result["optical_whole_interior_observed"] is False

