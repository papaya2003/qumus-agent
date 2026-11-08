"""Known public orientation seeds pixels; it never substitutes for identity."""
import math
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from qumus2.config import load_yaml
from qumus2.flows.library import stacking as flow
from qumus2.perception.tracking import RegistrationConfig, register_specimen
from qumus2.runtime.store import RunStore
from qumus2.testbench.render import MicroscopeRenderer
from tests.test_v1_transfer_sequence import motion_case as _motion_case

POLYGON = [(276.7, 153.), (254.3, 140.), (218.6, 150.), (215.7, 174.7), (241.1, 183.3)]


def scene(angle=0., aspect=1., shift=(0, 0), *, absent=False, shrink=1.):
    a = math.radians(angle)
    center = ((min(x for x, _ in POLYGON)+max(x for x, _ in POLYGON))/2,
              (min(y for _, y in POLYGON)+max(y for _, y in POLYGON))/2)
    sx, sy = .65/480, .65/480*aspect
    points = [[(center[0]+shift[0]-240)*sx+shrink*((x-center[0])*sx*math.cos(a)+(y-center[1])*sy*math.sin(a)),
               -(center[1]+shift[1]-160)*sy+shrink*((x-center[0])*sx*math.sin(a)-(y-center[1])*sy*math.cos(a))]
              for x, y in POLYGON]
    flake = dict(sim_id="qualified-asymmetric-source", material="hBN", layers=95, area_um2=1000,
        dx_mm=(center[0]+shift[0]-240)*sx, dy_mm=-(center[1]+shift[1]-160)*sy, outline_mm=points)
    packet = dict(fov_mm=[sx*480, sy*320], objective=20, on_chip="fixed-textured-substrate",
        sharpness_chip=8., sharpness_stamp=8., flakes=[] if absent else [flake])
    return flow.Frame("scope", 0., data=MicroscopeRenderer(None).render_bytes("scope",packet))


@pytest.mark.parametrize("nominal,aspect", [(125., 1.), (-125., 1.), (123., .8)])
@pytest.mark.parametrize("shift", [(5, -4), (-5, 3), (4, 6), (-4, -6)])
def test_large_known_signed_rotation_uses_only_small_residual_search(nominal, aspect, shift):
    result = register_specimen(scene(aspect=aspect), scene(nominal, aspect, shift), polygon_px=POLYGON,
        config=RegistrationConfig(max_translation_px=12, max_rotation_deg=2,
            nominal_rotation_deg=nominal, pixel_aspect_ratio=aspect))
    assert result.ok, result
    assert result.dx_px == pytest.approx(shift[0], abs=1) and result.dy_px == pytest.approx(shift[1], abs=1)
    assert abs(result.rotation_deg-nominal) < 2


@pytest.mark.parametrize("failure", ["deleted", "wrong_angle", "shrunken", "clipped"])
def test_nominal_angle_cannot_accept_missing_or_wrong_complete_body(failure):
    current = {"deleted": scene(absent=True), "wrong_angle": scene(-60),
               "shrunken": scene(125, shrink=.7), "clipped": scene(125, shift=(240, 0))}[failure]
    result = register_specimen(scene(), current, polygon_px=POLYGON,
        config=RegistrationConfig(max_translation_px=12, max_rotation_deg=2, nominal_rotation_deg=125))
    assert not result.ok, result


def test_default_nominal_preserves_existing_registration_and_rejects_bad_calibration():
    assert register_specimen(scene(), scene(), polygon_px=POLYGON).ok
    assert register_specimen(scene(), scene(), polygon_px=POLYGON) == register_specimen(
        scene(), scene(), polygon_px=POLYGON, config=RegistrationConfig(nominal_rotation_deg=0, pixel_aspect_ratio=1))
    with pytest.raises(ValueError, match="bounds"):
        register_specimen(scene(), scene(), polygon_px=POLYGON, config=RegistrationConfig(pixel_aspect_ratio=0))
    with pytest.raises(ValueError, match="bounds"):
        register_specimen(scene(), scene(), polygon_px=POLYGON, config=RegistrationConfig(nominal_rotation_deg=math.nan))


def test_nominal_orientation_does_not_relax_four_direction_observability():
    unqualified = [(61, 45), (97, 53), (91, 91), (56, 82)]
    image = Image.new("RGB", (160, 144), (125, 102, 171))
    ImageDraw.Draw(image).polygon(unqualified, fill=(45, 40, 63))
    # The fully contrasted outline is observable all around. It only failed
    # here through the raster-stride thinning artefact, now removed.
    assert register_specimen(image, image, polygon_px=unqualified).ok
    # Same material continuing past the left edge leaves that whole direction
    # without boundary contrast, so the specimen is not qualified.
    ImageDraw.Draw(image).rectangle((30, 40, 70, 95), fill=(45, 40, 63))
    assert not register_specimen(image, image, polygon_px=unqualified).ok
    for nominal in (0., 90., 180.):
        assert not register_specimen(image, image, polygon_px=unqualified,
                                     config=RegistrationConfig(nominal_rotation_deg=nominal)).ok


@pytest.fixture
def orientation_case(tmp_path):
    case = _motion_case.__wrapped__(tmp_path)
    case.scope.set_objective(20)
    frame = case.scope.capture()
    original = {"image": frame.saved_path(), "timestamp": frame.timestamp, "meta": frame.meta}
    store = RunStore(":memory:", clock=case.ctx.clock)
    source_run = store.create_run("search_chip", {"chip_id": "chip"})
    run = store.create_run("stack_device", {})
    def measure(rid, name, angle):
        store.emit("step.verified", {"name": name, "verdict": "ok", "measurements": {"rotation_deg": angle}}, run_id=rid)
    measure(source_run, "measure_chip_rotation", 1.5)
    measure(run, "measure_rotation_chip", 126.5)
    original["job_id"] = source_run
    store.upsert_sample("flake", "flake", props={"tracking_references": {"20x": original}})
    verification = load_yaml("verification.yaml")
    c = verification["vision_control"]
    identity = {"source_chip_id": "chip", "original": flow._frame_record(frame), "current": flow._frame_record(frame),
        "orientation": {"original": flow._rotation_observation(store, source_run, "measure_chip_rotation", frame.timestamp),
            "current": flow._rotation_observation(store, run, "measure_rotation_chip", frame.timestamp),
            "nominal_rotation_deg": 125., "pixel_aspect_ratio": 1., "limits": {
                "max_translation_px": c["max_translation_px"], "residual_rotation_deg": c["residual_rotation_deg"],
                "min_quality": c["min_match_quality"], "min_ambiguity": c["min_match_ambiguity"]}}}
    return SimpleNamespace(store=store, run=run, identity=identity, verification=verification)


def test_observed_orientation_receipts_are_exact_and_read_only(orientation_case):
    c = orientation_case
    before = deepcopy(c.identity)
    assert flow._validate_design_source_orientation(c.store, c.run, "flake", c.identity, c.verification)
    assert c.identity == before


@pytest.mark.parametrize("failure", ["missing_event", "changed_angle", "changed_residual", "wrong_exposure", "future_capture"])
def test_saved_orientation_requires_original_and_current_immutable_observations(orientation_case, failure):
    c = orientation_case
    if failure == "missing_event":
        c.identity["orientation"]["current"]["event_id"] += 1000
    elif failure == "changed_angle":
        c.identity["orientation"]["current"]["rotation_deg"] += 1
    elif failure == "changed_residual":
        c.identity["orientation"]["limits"]["residual_rotation_deg"] += 1
    elif failure == "wrong_exposure":
        c.identity["original"]["captured_at"] -= 1
    else:
        c.identity["current"]["captured_at"] -= 1
    with pytest.raises(ValueError):
        flow._validate_design_source_orientation(c.store, c.run, "flake", c.identity, c.verification)
