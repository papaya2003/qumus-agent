"""Deterministic Hall-bar placement solver: fit, measured refusal, official screen."""
from __future__ import annotations

import math
import os
import random
from pathlib import Path

import numpy as np
import pytest
from test_imported_hallbar_target import declared_import

from qumus2.core.polygons import hull, rectangle
from qumus2.core.regions import as_region
from qumus2.records.design_placement import (
    _ConvexLP,
    design_margins,
    margins_summary,
    placement_summary,
    solve_gr_placement,
    solve_pair_placement,
)
from qumus2.records.device_design import (
    FULL_CONTACT_POLICY,
    DesignPoseTolerance,
    DesignSource,
    DeviceDesignSpec,
    FlakePlacement,
    RegionDesignSource,
    screen_design_pose_window,
)

VERIFICATION = {"refind_flake": {"centre_tolerance_um": 1},
                "vision_control": {"residual_rotation_deg": 1.}}
UM = 1e-3


def _random_convex(seed, rx_um, ry_um, n=8):
    """Irregular convex outline of a declared size, flake-centred (mm)."""
    rng = random.Random(seed)
    angles = sorted(rng.uniform(0, 2*math.pi) for _ in range(n))
    points = [(rx_um*UM*math.cos(a)*rng.uniform(.9, 1.1), ry_um*UM*math.sin(a)*rng.uniform(.9, 1.1))
              for a in angles]
    outline = hull(points)
    cx = sum(x for x, _ in outline)/len(outline)
    cy = sum(y for _, y in outline)/len(outline)
    return [(x-cx, y-cy) for x, y in outline]


def _spec(target, hbn, gr, result):
    """Declared unit sources at the solver's poses, screened by the official code."""
    sources = []
    for material, flake, placement in (("hBN", hbn, result["placements"][0]),
                                       ("Gr", gr, result["placements"][1])):
        common = dict(flake_id=flake["flake_id"], material=material, source_chip_id="chip_"+material,
                      evidence_event_ids=(1,), image_sha256="1"*64,
                      placement=FlakePlacement.model_validate(placement))
        if "outline_mm" in flake:
            sources.append(DesignSource(outline_mm=flake["outline_mm"],
                observation={"source": "declared_unit_geometry"}, **common))
        else:
            region = as_region(flake["region_mm"]).model_dump(mode="json")
            sources.append(RegionDesignSource(region_mm=region, observation={
                "source": "declared_unit_region", "region_mm": region,
                "boundary_uncertainty_mm": flake["boundary_mm"],
                "boundary_uncertainty_provenance": {"declared": "unit test"}}, **common))
    return DeviceDesignSpec(design_id="design_"+"0"*32, project_id="unit_project", revision=1,
        bench="sim", target_chip_id="target", target=target, guidance_sha256="2"*64,
        selection_sha256="3"*64, sources=tuple(sources), pickup_order=(hbn["flake_id"], gr["flake_id"]),
        contact_overlap_um=1., overlap_rule=FULL_CONTACT_POLICY,
        pose_tolerance=DesignPoseTolerance(translation_um=1., rotation_deg=1.))


def test_linear_programme_matches_known_optima():
    lp = _ConvexLP(rectangle(10, 10))
    s, _ = lp.solve(rectangle(2, 2), np.arange(0, 90, 1.))
    assert s.max() == pytest.approx(4.0)
    s, _ = lp.solve(rectangle(12, 1), np.arange(0, 180, .05))
    assert s.max() == pytest.approx(.4038, abs=2e-4)


@pytest.mark.parametrize("seed", range(6))
def test_fitting_irregular_pairs_pass_the_official_pose_window_screen(seed):
    target = declared_import()
    # Sized to fit at the v1 fixed 0 deg (no chip rotation, #204).
    gr = {"flake_id": "gr", "outline_mm": _random_convex(seed, 30, 40)}
    hbn = {"flake_id": "hbn", "outline_mm": _random_convex(100+seed, 66, 76)}
    result = solve_pair_placement(target, hbn, gr, verification=VERIFICATION)
    assert result["status"] == "fit", placement_summary(result)
    assert result["shortfall_um"] == 0 and result["limiting"] is None
    report = screen_design_pose_window(_spec(target, hbn, gr, result), verification=VERIFICATION)
    assert report.status == "pass", report.failures
    # The solver's headroom is the official margin, not an optimistic estimate.
    assert report.hbn_headroom_mm*1000 == pytest.approx(result["hbn_headroom_um"], abs=1e-3)
    # The stated distance margins of the built design are the solver's numbers.
    margins = design_margins(_spec(target, hbn, gr, result), verification=VERIFICATION)
    assert margins["gr_headroom_um"] == pytest.approx(result["gr_headroom_um"], abs=1e-6)
    assert margins["hbn_headroom_um"] == pytest.approx(result["hbn_headroom_um"], abs=1e-6)
    assert margins["gr_headroom_um"] >= 0 and margins["hbn_headroom_um"] >= 0
    assert "pose window 1 um / 1 deg" in margins_summary(margins)


def test_shrinking_a_tight_gr_reports_the_shrink_as_shortfall():
    target = declared_import()
    base = solve_gr_placement(target, {"flake_id": "gr", "outline_mm": rectangle(30*UM, 48*UM)},
                              verification=VERIFICATION)
    assert base["status"] == "fit" and base["shortfall_um"] == 0 and base["limiting"] is None
    headroom = base["gr_headroom_um"]
    shrink = headroom + .5  # half a micrometre too small on every side
    small = {"flake_id": "gr", "outline_mm": rectangle((30-2*shrink)*UM, (48-2*shrink)*UM)}
    hbn = {"flake_id": "hbn", "outline_mm": rectangle(60*UM, 80*UM)}
    result = solve_pair_placement(target, hbn, small, verification=VERIFICATION)
    assert result["status"] == "no_fit" and result["limiting"] == "gr_contacts"
    assert result["shortfall_um"] == pytest.approx(.5, abs=.1)
    alone = solve_gr_placement(target, small, verification=VERIFICATION)
    assert alone["limiting"] == "gr_contacts" and alone["shortfall_um"] == pytest.approx(result["shortfall_um"])
    assert "short by" in placement_summary(result)
    # The best attempt is still a valid pose the official screen can explain.
    report = screen_design_pose_window(_spec(target, hbn, small, result), verification=VERIFICATION)
    assert report.status == "fail" and report.failed_contacts


def test_too_small_hbn_is_refused_with_its_own_shortfall():
    target = declared_import()
    gr = {"flake_id": "gr", "outline_mm": rectangle(32*UM, 50*UM)}
    # hBN exactly 1 um larger than Gr on each side cannot hold ~2.4 um margins.
    hbn = {"flake_id": "hbn", "outline_mm": rectangle(34*UM, 52*UM)}
    result = solve_pair_placement(target, hbn, gr, verification=VERIFICATION)
    assert result["status"] == "no_fit" and result["limiting"] == "hbn_cover"
    expected = result["hbn_required_clearance_um"] - 1.0
    assert result["shortfall_um"] == pytest.approx(expected, abs=.1)


def test_non_convex_gr_with_hole_avoids_the_hole_and_passes_the_screen():
    target = declared_import()
    # 70 x 70 um square Gr with a 30 x 30 um hole in one quadrant: the contact
    # set (24 x 42 um) cannot sit in the middle; it must use the solid L part.
    outer = rectangle(70*UM, 70*UM)
    hole = list(reversed(rectangle(30*UM, 30*UM, (17*UM, 17*UM))))
    region = {"components": [{"exterior": [list(p) for p in outer], "holes": [[list(p) for p in hole]]}]}
    gr = {"flake_id": "gr", "region_mm": region, "boundary_mm": .0002}
    hbn = {"flake_id": "hbn", "outline_mm": rectangle(90*UM, 90*UM)}
    result = solve_pair_placement(target, hbn, gr, verification=VERIFICATION)
    assert result["status"] == "fit", placement_summary(result)
    report = screen_design_pose_window(_spec(target, hbn, gr, result), verification=VERIFICATION)
    assert report.status == "pass", report.failures


def test_reused_gr_solution_must_match_the_flake():
    target = declared_import()
    gr = {"flake_id": "gr", "outline_mm": rectangle(32*UM, 50*UM)}
    solution = solve_gr_placement(target, gr, verification=VERIFICATION)
    other = {"flake_id": "other", "outline_mm": rectangle(32*UM, 50*UM)}
    with pytest.raises(ValueError, match="another flake"):
        solve_pair_placement(target, {"flake_id": "h", "outline_mm": rectangle(60*UM, 80*UM)}, other,
                             verification=VERIFICATION, gr_solution=solution)


REAL_TARGET = Path(os.environ.get("QUMUS_REAL_TARGET_BUNDLE",
                                  "/mnt/project-files/lab-inputs/electrode-wafer-20261102/target-a518"))


@pytest.mark.skipif(not (REAL_TARGET/"target.json").exists(), reason="private lab target not present")
def test_owner_target_fixture_pair_fits_and_small_pair_is_refused():
    from qumus2.testbench.hallbar import load_imported_hallbar_target

    target = load_imported_hallbar_target(REAL_TARGET)
    # Sized to fit at the v1 fixed 0 deg (no chip rotation, #204).
    gr = {"flake_id": "gr", "outline_mm": _random_convex(7, 23, 27)}
    hbn = {"flake_id": "hbn", "outline_mm": _random_convex(8, 46, 50)}
    result = solve_pair_placement(target, hbn, gr, verification=VERIFICATION)
    assert result["status"] == "fit", placement_summary(result)
    assert screen_design_pose_window(_spec(target, hbn, gr, result), verification=VERIFICATION).status == "pass"
    tiny = {"flake_id": "gr", "outline_mm": rectangle(10*UM, 18*UM)}
    refused = solve_pair_placement(target, hbn, tiny, verification=VERIFICATION)
    assert refused["status"] == "no_fit" and refused["shortfall_um"] > 0


def test_v1_solver_never_rotates_a_source():
    """#204: v1 has no chip rotation, so every placement is at 0 deg."""
    from qumus2.records import design_placement

    assert design_placement.FIXED_ROTATION_DEG == 0.0
    target = declared_import()
    gr = {"flake_id": "gr", "outline_mm": _random_convex(1, 30, 40)}
    hbn = {"flake_id": "hbn", "outline_mm": _random_convex(101, 66, 76)}
    result = solve_pair_placement(target, hbn, gr, verification=VERIFICATION)
    assert [p["rotation_deg"] for p in result["placements"]] == [0.0, 0.0]
