"""Pure analytic evaluator controls, never RGB qualification or process proof."""
from copy import deepcopy

import pytest
from test_device_design_pose_screen import bound_design
from test_imported_hallbar_target import imported_spec

from qumus2.core.polygons import rectangle
from qumus2.core.regions import as_region, region_dict, transform_region
from qumus2.records.device_design import DeviceDesignSpec, target_import_binding
from qumus2.testbench.hallbar import electrode_pattern_from_target
from scripts.record_hallbar_demos import truth_geometry


def declared_terminal(*, hbn_holes=(), gr_holes=(), islands=True, bound=1e-6):
    raw = imported_spec().model_dump(mode="json")
    raw["pose_tolerance"].update(translation_um=1., rotation_deg=1.)
    for i, source in enumerate(raw["sources"]):
        outline = source.pop("outline_mm")
        if i == 0:
            outline = [(-.05, -.06), (.05, -.06), (.05, .06), (.035, .06), (.035, .05), (-.05, .05)]
        components = [{"exterior": outline, "holes": list(hbn_holes if i == 0 else gr_holes)}]
        if i == 1 and islands:
            components.append({"exterior": rectangle(.002, .003, (.04, 0)), "holes": []})
        shape = region_dict({"version": "polygon-region-v1", "components": components})
        source["region_mm"] = shape
        source["observation"] = {"source": "declared_analytic_unit_region", "region_mm": shape,
            "boundary_uncertainty_mm": bound,
            "boundary_uncertainty_provenance": {"source": "analytic_unit_fixture_not_observed_material"}}
    design = bound_design(DeviceDesignSpec.model_validate(raw))
    target = {"id": design.target_chip_id, "kind": "chip", "size_mm": list(design.target.size_mm),
        "electrode_geometry": {"polygons": electrode_pattern_from_target(design.target),
                               "source_binding": target_import_binding(design.target)},
        "flakes": [{"id": s.flake_id, "material": s.material, "region_mm": region_dict(s.region_mm),
                    "x_mm": s.placement.translation_mm[0]-.5,
                    "y_mm": s.placement.translation_mm[1]-.5} for s in design.sources]}
    return design, {"objects": [target], "stamps": {}}


def score(design, truth):
    return truth_geometry(truth, design, placement_tolerance_mm=.001)


def test_complete_concave_holed_and_disconnected_material_is_scored_without_mutation():
    design, truth = declared_terminal(hbn_holes=[rectangle(.002, .002, (-.04, .035))],
                                      gr_holes=[rectangle(.002, .002, (.023, .027))])
    before = deepcopy(truth), design.model_dump(mode="json")
    result = score(design, truth)
    assert result["verified"] and result["whole_graphene_covered"]
    assert len(result["contact_coverage"]["contacts"]) == 8
    assert all(c["contained"] for c in result["contact_coverage"]["contacts"].values())
    assert (truth, design.model_dump(mode="json")) == before


def test_region_comparison_does_not_require_equal_vertices_or_a_convex_outline():
    design, truth = declared_terminal(islands=False)
    for flake in truth["objects"][0]["flakes"]:
        part = flake["region_mm"]["components"][0]
        a, b = part["exterior"][:2]
        part["exterior"].insert(1, [(a[0]+b[0])/2, (a[1]+b[1])/2])
    assert score(design, truth)["verified"]
    # Current World exposes one full ring, including concavity, rather than a region.
    for flake in truth["objects"][0]["flakes"]:
        flake["outline_mm"] = flake.pop("region_mm")["components"][0]["exterior"]
    assert score(design, truth)["verified"]


@pytest.mark.parametrize("change", ["lost_hole", "lost_component", "cropped", "reflected", "misplaced", "rotated"])
def test_shape_loss_reflection_and_frozen_pose_fail_closed(change):
    design, truth = declared_terminal(hbn_holes=[rectangle(.002, .002, (-.04, .035))])
    flakes = truth["objects"][0]["flakes"]
    if change == "lost_hole":
        flakes[0]["region_mm"]["components"][0]["holes"] = []
    elif change == "lost_component":
        flakes[1]["region_mm"]["components"].pop()
    elif change in ("cropped", "reflected"):
        for part in flakes[0]["region_mm"]["components"]:
            for ring in [part["exterior"], *part["holes"]]:
                for point in ring:
                    point[0] *= .99 if change == "cropped" else -1
    else:
        for flake in flakes:
            if change == "misplaced":
                flake["x_mm"] += .0011
            else:
                flake["region_mm"] = region_dict(transform_region(flake["region_mm"], rotation_deg=1.1))
    assert not score(design, truth)["verified"]


def test_permitted_rigid_rotation_uses_frozen_pose_without_changing_coverage():
    design, truth = declared_terminal()
    for flake in truth["objects"][0]["flakes"]:
        flake["region_mm"] = region_dict(transform_region(flake["region_mm"], rotation_deg=.5))
    result = score(design, truth)
    assert result["verified"]
    assert all(angle == pytest.approx(.5) for angle in result["rotation_error_deg"].values())
    with pytest.raises(ValueError, match="frozen translation"):
        truth_geometry(truth, design, placement_tolerance_mm=.002)


@pytest.mark.parametrize("contact_index", range(8))
def test_boundary_compatibility_cannot_fill_a_hole_in_any_complete_contact(contact_index):
    contact = imported_spec().target.contacts[contact_index]
    xs, ys = zip(*contact.points_mm, strict=True)
    x, edge = (min(xs)+max(xs))/2-.5, max(ys)-.5
    hole = rectangle(.00008, .00008, (x, edge+.00015))
    design, truth = declared_terminal(gr_holes=[hole], bound=.00025)
    gr = truth["objects"][0]["flakes"][1]
    gr["region_mm"]["components"][0]["holes"][0] = [
        [x, y-.0002] for x, y in gr["region_mm"]["components"][0]["holes"][0]]
    result = score(design, truth)
    assert not result["verified"]
    assert result["whole_graphene_covered"]
    assert result["contact_coverage"]["failed_contacts"] == [contact.id]


def test_compatible_hbn_hole_still_prevents_whole_gr_coverage():
    design, truth = declared_terminal(hbn_holes=[rectangle(.00008, .00008, (.03015, 0))], bound=.00025)
    hbn = truth["objects"][0]["flakes"][0]
    hbn["region_mm"]["components"][0]["holes"][0] = [
        [x-.0002, y] for x, y in hbn["region_mm"]["components"][0]["holes"][0]]
    result = score(design, truth)
    assert not result["verified"] and not result["whole_graphene_covered"]
    assert result["contact_coverage"]["status"] == "pass"


def test_independent_truth_identity_and_all_original_metal_still_bind():
    design, truth = declared_terminal()
    for change in ("binding", "missing_metal", "wrong_material", "extra_layer", "retained_source", "retained_stamp"):
        bad = deepcopy(truth)
        target = bad["objects"][0]
        if change == "binding":
            target["electrode_geometry"]["source_binding"]["source_sha256"] = "0"*64
        elif change == "missing_metal":
            target["electrode_geometry"]["polygons"].pop()
        elif change == "wrong_material":
            target["flakes"][0]["material"] = "Gr"
        elif change == "extra_layer":
            target["flakes"].append(deepcopy(target["flakes"][0]))
        elif change == "retained_source":
            bad["objects"].append({"id": "source", "flakes": [deepcopy(target["flakes"][0])]})
        else:
            bad["stamps"]["stamp"] = {"stack": [{"flake": deepcopy(target["flakes"][0])}]}
        assert not score(design, bad)["verified"], change


def test_mixed_sources_retain_the_legacy_shape_loss_policy():
    design, truth = declared_terminal(islands=False)
    raw = design.model_dump(mode="json", exclude={"artifact", "canonical_digest"})
    source = raw["sources"][1]
    source["outline_mm"] = source.pop("region_mm")["components"][0]["exterior"]
    source["observation"] = {"source": "declared_analytic_legacy_outline"}
    mixed = bound_design(DeviceDesignSpec.model_validate(raw))
    assert score(mixed, truth)["verified"]
    gr = truth["objects"][0]["flakes"][1]
    gr["region_mm"] = region_dict(as_region(rectangle(.060*.99, .080)))
    assert not score(mixed, truth)["verified"]
