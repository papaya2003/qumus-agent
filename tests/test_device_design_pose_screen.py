"""Pure declared-polygon screening controls; no bench, jobs or KLayout process."""
from __future__ import annotations

import asyncio
import copy
import math
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from qumus2.core.polygons import (
    area_um2,
    contains,
    convex_polygon,
    erode_convex,
    intersection,
    rectangle,
    rigid_polygon_error,
    transform,
)
from qumus2.records.device_design import (
    DesignArtifact,
    DesignPoseTolerance,
    DesignPoseWindowError,
    DesignSource,
    DeviceDesignSpec,
    DeviceDesignV1,
    FlakePlacement,
    HallBarTarget,
    bind_artifact,
    build_design_pose_window_admission,
    design_pose_window_policy,
    require_design_pose_window,
    screen_design_pose_window,
    validate_design_pose_window_admission,
)
from qumus2.records.geometry import geometry_digest

VERIFICATION = {"refind_flake": {"centre_tolerance_um": 6},
                "vision_control": {"residual_rotation_deg": 2.}}
ROLES = ("source", "drain", "voltage_left_1", "voltage_left_2", "voltage_left_3",
         "voltage_right_1", "voltage_right_2", "voltage_right_3")


def declared_spec(*, hbn=None, gr=None, center=(.5, .5), small_contacts=False):
    """Analytic unit data, not experimental stock or a next-run donor layout."""
    hbn = hbn if hbn is not None else rectangle(.100, .120)
    gr = gr if gr is not None else rectangle(.060, .080)
    side, top, contact_size = (.001, .025, .0002) if small_contacts else (.010, .020, .004)
    centers = [(0, top), (0, -top), *[(-side, y) for y in (-.01, 0, .01)],
               *[(side, y) for y in (-.01, 0, .01)]]
    target = HallBarTarget(source="simulated_design", reference="Declared analytic unit fixture",
        size_mm=(1., 1.), contacts=[{"id": role, "role": role,
            "points_mm": rectangle(contact_size, contact_size, (center[0]+x, center[1]+y))}
            for role, (x, y) in zip(ROLES, centers, strict=True)])
    sources = tuple(DesignSource(flake_id=material, material=material, source_chip_id="chip_"+material,
        outline_mm=outline, observation={"source": "declared_unit_geometry"}, evidence_event_ids=(1,),
        image_sha256="1"*64, placement=FlakePlacement(flake_id=material,
            translation_mm=center, rotation_deg=0)) for material, outline in (("hBN", hbn), ("Gr", gr)))
    return DeviceDesignSpec(design_id="design_"+"0"*32, project_id="unit_project", revision=1,
        bench="sim", target_chip_id="target", target=target, guidance_sha256="2"*64,
        selection_sha256="3"*64, sources=sources, pickup_order=("hBN", "Gr"),
        contact_overlap_um=.01 if small_contacts else 1.,
        pose_tolerance=DesignPoseTolerance(translation_um=6., rotation_deg=2.))


def bound_design(spec):
    # Artifact metadata only; this does not claim real save/reopen validation.
    return bind_artifact(spec, DesignArtifact(path="/unit-only/device.gds", sha256="4"*64,
        geometry_sha256="5"*64))


def test_exact_disk_erosion_rectangle_and_redundant_parallel_edges():
    polygon = convex_polygon(((0, 0), (1, 0), (2, 0), (2, 1), (0, 1)))
    result = erode_convex(polygon, .1)
    assert area_um2(result) == pytest.approx(1.8*.8*1e6)
    assert min(x for x, _ in result) == pytest.approx(.1)
    assert max(x for x, _ in result) == pytest.approx(1.9)
    assert min(y for _, y in result) == pytest.approx(.1)
    assert max(y for _, y in result) == pytest.approx(.9)
    assert erode_convex(polygon, 0) == polygon
    assert erode_convex(polygon, .5) == ()
    assert erode_convex(polygon, 2) == ()


@pytest.mark.parametrize("distance", [-1., float("nan"), float("inf"), True, "0.1"])
def test_erosion_bad_distance_refuses(distance):
    with pytest.raises(ValueError):
        erode_convex(rectangle(1, 1), distance)


@pytest.mark.parametrize("points", [[], [(0, 0), (1, 0), (1, 0), (0, 1)],
    [(0, 0), (1, 0), (.1, .1), (0, 1)], [(0, 0), (1, 0), (2, 0)]])
def test_erosion_malformed_or_degenerate_input_refuses(points):
    with pytest.raises(ValueError):
        erode_convex(points, .01)


def test_wide_analytic_fixture_certifies_existing_window_only():
    report = require_design_pose_window(declared_spec(), verification=VERIFICATION)
    assert report.status == "pass" and not report.failed_contacts
    assert report.hbn_clearance_mm == pytest.approx(.020)
    assert .016 < report.hbn_required_clearance_mm < .017
    assert all(contact.certified_area_um2 == pytest.approx(16.) for contact in report.contacts)
    assert all(source.chip_headroom_mm > 0 for source in report.sources)
    assert "no physical registration" in report.scope


# Original r003 PUBLIC placed polygons, translated to the declared unit origin.
# No PNG, world state, actual donor pose or hidden geometry is used.
PUBLIC_HBN = ((-.013619664703357895, -.028328642671316594),
    (.011779834876324141, -.028518878211205897), (.01927831364728491, .008266064739151199),
    (.0003607426793853369, .04806598562638173), (-.01899167650490377, .007797275890180332))
PUBLIC_GR = ((.00012599236291826934, -.02444683085254344),
    (.010530971045034154, -.015435052569028603), (.013574838170815857, .0028825917811083116),
    (.007415170582305198, .02428634307622457), (-.0028206354041608295, .022083600969219512),
    (-.009520473771225378, .017945640063554816), (-.01403607258939525, .0005588763351880577),
    (-.009967228644875963, -.016314669027719643))


def test_public_four_micrometre_witness_is_within_pose_window_but_loses_geometry():
    spec = declared_spec(hbn=PUBLIC_HBN, gr=PUBLIC_GR)
    hbn, gr = (source.placed_outline() for source in spec.sources)
    shifted = transform(gr, xy=(.0039193967570445055, -.0007989549805020396))
    fit = rigid_polygon_error(gr, shifted)
    assert math.hypot(*fit["translation_mm"]) == pytest.approx(.004)
    assert abs(fit["rotation_deg"]) < 1e-8 and fit["shape_residual_mm"] < 1e-9
    assert contains(hbn, gr) and not contains(hbn, shifted)
    contacts = {c.id: area_um2(intersection(shifted, c.points_mm)) for c in spec.target.contacts}
    assert contacts["voltage_left_1"] < 1 and contacts["voltage_left_3"] == 0
    report = screen_design_pose_window(spec, verification=VERIFICATION)
    assert report.hbn_clearance_mm == pytest.approx(.003837045127795773)
    assert report.status == "fail" and "whole_gr_coverage" in report.failures
    with pytest.raises(DesignPoseWindowError) as failure:
        require_design_pose_window(spec, verification=VERIFICATION)
    assert failure.value.report == report


def test_contact_and_chip_margin_failures_are_independent():
    contact = screen_design_pose_window(declared_spec(hbn=rectangle(.2, .2),
        gr=rectangle(.02, .04)), verification=VERIFICATION)
    assert contact.hbn_headroom_mm > 0 and contact.failed_contacts
    assert "source" in contact.failed_contacts
    assert all(source.chip_headroom_mm > 0 for source in contact.sources)
    chip = screen_design_pose_window(declared_spec(center=(.055, .5)), verification=VERIFICATION)
    assert chip.hbn_headroom_mm > 0 and not chip.failed_contacts
    assert "chip_fit:hBN" in chip.failures


def test_empty_eroded_core_has_zero_certified_contact_area():
    report = screen_design_pose_window(declared_spec(gr=rectangle(.004, .060), small_contacts=True),
        verification=VERIFICATION)
    assert report.status == "fail" and len(report.failed_contacts) == 8
    assert all(contact.nominal_area_um2 > 0 and contact.certified_area_um2 == 0 for contact in report.contacts)


def test_vertex_mean_pivot_matches_actual_comparator_not_placement_or_area_centroid():
    # Extra collinear vertex changes the vertex mean, not the physical polygon
    # or its area centroid. Both the comparator and screen must use that mean.
    polygon = convex_polygon(((-.030, -.040), (.030, -.040), (.030, .040), (0., .040), (-.030, .040)))
    spec = declared_spec(gr=polygon)
    report = require_design_pose_window(spec, verification=VERIFICATION)
    source = report.sources[1]
    assert source.pivot_mm == pytest.approx((.5, .508))
    placed = spec.sources[1].placed_outline()
    centered = transform(placed, xy=tuple(-value for value in source.pivot_mm))
    actual = transform(centered, xy=(source.pivot_mm[0]+.006, source.pivot_mm[1]), rotation_deg=2.)
    fit = rigid_polygon_error(placed, actual)
    assert math.hypot(*fit["translation_mm"]) == pytest.approx(.006)
    assert fit["rotation_deg"] == pytest.approx(2.)
    assert max(math.dist(a, b) for a, b in zip(placed, actual, strict=True)) <= source.error_bound_mm
    rotated_order = polygon[2:]+polygon[:2]
    reordered = require_design_pose_window(declared_spec(gr=rotated_order), verification=VERIFICATION)
    assert reordered.sources[1].pivot_mm == pytest.approx(source.pivot_mm)
    assert reordered.sources[1].error_bound_mm == pytest.approx(source.error_bound_mm)


@pytest.mark.parametrize("value", [True, float("nan"), 0., 181.])
def test_invalid_or_unsupported_policy_refuses(value):
    config = copy.deepcopy(VERIFICATION)
    config["vision_control"]["residual_rotation_deg"] = value
    with pytest.raises(ValueError):
        screen_design_pose_window(declared_spec(), verification=config)


def test_frozen_window_and_config_source_are_both_bound():
    design = bound_design(declared_spec())
    sidecar = build_design_pose_window_admission(design, verification=VERIFICATION)
    assert validate_design_pose_window_admission(design, sidecar, verification=VERIFICATION) == sidecar
    changed = copy.deepcopy(VERIFICATION)
    changed["vision_control"]["residual_rotation_deg"] = 1.
    with pytest.raises(ValueError, match="frozen design pose window"):
        build_design_pose_window_admission(design, verification=changed)
    changed = {**VERIFICATION, "unrelated_but_bound_source": True}
    with pytest.raises(ValueError, match="exact design/configuration"):
        validate_design_pose_window_admission(design, sidecar, verification=changed)


@pytest.mark.parametrize("change", ["design", "report", "rehash_report", "extra", "version"])
def test_sidecar_is_not_a_caller_waiver(change):
    design = bound_design(declared_spec())
    body = build_design_pose_window_admission(design, verification=VERIFICATION).model_dump(mode="json")
    if change == "design":
        body["canonical_design_digest"] = "f"*64
    elif change in {"report", "rehash_report"}:
        body["report"]["sources"][0]["error_bound_mm"] = .000001
        if change == "rehash_report":
            body["report_sha256"] = geometry_digest(body["report"])
    elif change == "extra":
        body["waive_screening"] = True
    else:
        body["version"] = "unversioned"
    with pytest.raises(ValueError):
        validate_design_pose_window_admission(design, body, verification=VERIFICATION)


def test_historical_nominal_design_parsing_and_digest_do_not_gain_new_semantics():
    design = bound_design(declared_spec(hbn=PUBLIC_HBN, gr=PUBLIC_GR))
    raw = design.model_dump(mode="json")
    before = geometry_digest(raw)
    assert DeviceDesignV1.model_validate(raw) == design
    assert not any("pose_window" in key for key in raw)
    with pytest.raises(DesignPoseWindowError):
        build_design_pose_window_admission(design, verification=VERIFICATION)
    assert geometry_digest(raw) == before and DeviceDesignV1.model_validate(raw) == design


class UnitDesignStore:
    def __init__(self):
        self.state = {}
        self.samples = {}

    def get_project(self, project):
        return {"state": self.state}

    def list_samples(self, *, kind):
        return [sample for sample in self.samples.values() if sample["kind"] == kind]

    def _transaction(self):
        return nullcontext()

    def update_project_state(self, project, **values):
        self.state.update(values)


def test_fresh_tool_refuses_before_export_and_reports_geometry(monkeypatch):
    from qumus2.agents.tools import design as tools

    spec = declared_spec(hbn=PUBLIC_HBN, gr=PUBLIC_GR)
    monkeypatch.setattr(tools, "build_design_spec", lambda *args, **kwargs: spec)
    monkeypatch.setattr("qumus2.config.load_yaml", lambda name: copy.deepcopy(VERIFICATION))
    def unexpected_export(*args):
        pytest.fail("a failed screen must precede KLayout/device work")
    monkeypatch.setattr("qumus2.agents.klayout.create_artifact", unexpected_export)
    store = UnitDesignStore()
    ctx = SimpleNamespace(store=store, project_id="unit_project")
    args = tools.CreateDeviceDesignArgs(placements=[source.placement for source in spec.sources], contact_overlap_um=1.)
    result = asyncio.run(tools.create_device_design(args, ctx))
    assert result.status == "error" and result.data["pose_window_screen"]["status"] == "fail"
    assert not store.state


def test_tool_rechecks_current_policy_after_export_before_accepting(monkeypatch):
    from qumus2.agents.tools import design as tools

    spec = declared_spec()
    monkeypatch.setattr(tools, "build_design_spec", lambda *args, **kwargs: spec)
    config = copy.deepcopy(VERIFICATION)
    monkeypatch.setattr("qumus2.config.load_yaml", lambda name: copy.deepcopy(config))
    def export(*args):
        config["changed_during_export"] = True
        return bound_design(spec).artifact
    monkeypatch.setattr("qumus2.agents.klayout.create_artifact", export)
    store = UnitDesignStore()
    ctx = SimpleNamespace(store=store, project_id="unit_project")
    args = tools.CreateDeviceDesignArgs(placements=[source.placement for source in spec.sources], contact_overlap_um=1.)
    result = asyncio.run(tools.create_device_design(args, ctx))
    assert result.status == "error" and "changed during KLayout" in result.summary
    assert not store.state


def test_tool_positive_returns_recomputable_sidecar_without_altering_design_schema(monkeypatch):
    from qumus2.agents.tools import design as tools

    spec = declared_spec()
    monkeypatch.setattr(tools, "build_design_spec", lambda *args, **kwargs: spec)
    monkeypatch.setattr("qumus2.config.load_yaml", lambda name: copy.deepcopy(VERIFICATION))
    monkeypatch.setattr("qumus2.agents.klayout.create_artifact", lambda *args: bound_design(spec).artifact)
    store = UnitDesignStore()
    monkeypatch.setattr(tools, "validate_design_current",
        lambda *args: DeviceDesignV1.model_validate(store.state["device_design"]))
    ctx = SimpleNamespace(store=store, project_id="unit_project")
    args = tools.CreateDeviceDesignArgs(placements=[source.placement for source in spec.sources], contact_overlap_um=1.)
    result = asyncio.run(tools.create_device_design(args, ctx))
    assert result.status == "ok"
    design = DeviceDesignV1.model_validate(result.data["device_design"])
    admission = validate_design_pose_window_admission(design, result.data["pose_window_admission"],
        verification=VERIFICATION)
    assert admission.canonical_design_digest == design.canonical_digest
    assert not any("pose_window" in key for key in store.state["device_design"])


def test_public_policy_is_the_exact_policy_consumed_by_screening():
    policy = design_pose_window_policy(VERIFICATION)
    report = require_design_pose_window(declared_spec(), verification=VERIFICATION)
    assert report.policy == policy
    assert policy.verification_sha256 == geometry_digest(VERIFICATION)


def test_registered_design_context_exposes_actual_config_policy_before_creation(monkeypatch):
    from qumus2.agents.tools import design as tools

    config = copy.deepcopy(VERIFICATION)
    config["refind_flake"]["centre_tolerance_um"] = 7.
    config["vision_control"]["residual_rotation_deg"] = 1.5
    monkeypatch.setattr("qumus2.config.load_yaml", lambda name: copy.deepcopy(config))
    store = UnitDesignStore()
    store.state["device_plan"] = {"target_chip_id": "target"}
    store.samples["target"] = {"id": "target", "kind": "chip", "props": {
        "device_design_target": declared_spec().target.model_dump(mode="json")}}
    result = asyncio.run(tools.get_design_context(tools.NoArgs(),
        SimpleNamespace(store=store, project_id="unit_project")))
    assert result.status == "ok"
    exposed = result.data["endpoint_pose_window"]
    assert exposed["target_chip_id"] == "target"
    assert exposed["policy"] == design_pose_window_policy(config).model_dump(mode="json")
    assert exposed["policy"]["translation_um"] == 7.
    assert exposed["policy"]["rotation_deg"] == 1.5
    assert exposed["scope"] == require_design_pose_window(declared_spec(), verification=VERIFICATION).scope
    assert len(exposed["required_geometry"]) == 3


@pytest.mark.parametrize("unrelated_registered_target", [False, True])
def test_legacy_context_is_not_promoted_by_policy_or_unrelated_inventory(monkeypatch, unrelated_registered_target):
    from qumus2.agents.tools import design as tools

    def unexpected_policy_read(name):
        pytest.fail("Legacy context must not acquire the new endpoint screening policy")
    monkeypatch.setattr("qumus2.config.load_yaml", unexpected_policy_read)
    store = UnitDesignStore()
    store.state["device_plan"] = {"target_chip_id": "legacy"}
    store.samples["legacy"] = {"id": "legacy", "kind": "chip", "props": {"electrodes": "legacy pair"}}
    if unrelated_registered_target:
        store.samples["other"] = {"id": "other", "kind": "chip", "props": {
            "device_design_target": declared_spec().target.model_dump(mode="json")}}
    result = asyncio.run(tools.get_design_context(tools.NoArgs(),
        SimpleNamespace(store=store, project_id="unit_project")))
    assert result.status == "ok" and "endpoint_pose_window" not in result.data
