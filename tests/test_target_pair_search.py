"""Target-aware hBN/Gr pairing in get_flake_candidates (device_design_v1 only).

The placement solver belongs to device design (records.design_placement); here a
declared centred-placement double stands in for it, so these tests check the
picker wiring, ranking, shortage reports and the unchanged code re-check.
Outlines are synthetic fixtures, not measured flakes.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

import qumus2.records
from qumus2.agents.tools import picking
from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.core.polygons import containment_clearance, transform
from qumus2.testbench.hallbar import hallbar_target

GR_REQUIRED_UM, HBN_REQUIRED_UM = 1.2, 2.5


def square(side_um):
    h = side_um / 2000
    return ((-h, -h), (h, -h), (h, h), (-h, h))


class CentredSolver:
    """Places every flake unrotated at the contact centre; optional hBN offset."""

    def __init__(self, hbn_offset_mm=(0.0, 0.0)):
        self.hbn_offset_mm = hbn_offset_mm

    @staticmethod
    def centre(target):
        points = [p for c in target.contacts for p in c.points_mm]
        xs, ys = zip(*points, strict=True)
        return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)

    def solve_gr_placement(self, target, gr, *, verification=None):
        centre = self.centre(target)
        placed = transform(tuple(map(tuple, gr["outline_mm"])), xy=centre)
        clearance = min(containment_clearance(placed, c.points_mm) for c in target.contacts) * 1000
        headroom = clearance - GR_REQUIRED_UM
        # Same keys as records.design_placement.solve_gr_placement (no shortfall there).
        return {"flake_id": gr["flake_id"], "status": "fit" if headroom >= 0 else "no_fit",
                "gr_headroom_um": headroom, "gr_required_clearance_um": GR_REQUIRED_UM, "placed_gr": placed,
                "placement": {"flake_id": gr["flake_id"], "translation_mm": list(centre), "rotation_deg": 0.0}}

    def solve_pair_placement(self, target, hbn, gr, *, verification=None, gr_solution=None):
        gr_solution = gr_solution or self.solve_gr_placement(target, gr)
        centre = self.centre(target)
        at = (centre[0] + self.hbn_offset_mm[0], centre[1] + self.hbn_offset_mm[1])
        placed = transform(tuple(map(tuple, hbn["outline_mm"])), xy=at)
        headroom = containment_clearance(placed, gr_solution["placed_gr"]) * 1000 - HBN_REQUIRED_UM
        fit = headroom >= 0 and gr_solution["status"] == "fit"
        return {"status": "fit" if fit else "no_fit", "gr_headroom_um": gr_solution["gr_headroom_um"],
                "hbn_headroom_um": headroom, "shortfall_um": max(0.0, -min(headroom, gr_solution["gr_headroom_um"])),
                "limiting": None if fit else "hbn_cover", "hbn_required_clearance_um": HBN_REQUIRED_UM,
                "placements": [{"flake_id": hbn["flake_id"], "translation_mm": list(at), "rotation_deg": 0.0},
                               gr_solution["placement"]]}


@pytest.fixture
def s(monkeypatch):
    # Image capture is covered elsewhere; this isolates geometry ranking.
    monkeypatch.setattr("qumus2.agents.tools.picking.require_images", lambda: False)
    services = build_services(testbench_config=simulation_profile(), use_openai=False)
    target = hallbar_target()
    services.store.upsert_sample("target", "chip", props={
        "size_mm": list(target.size_mm), "device_design_target": target.model_dump(mode="json")})
    services.store.update_project_state(services.project_id, device_plan={
        "design_contract": "device_design_v1", "target_chip_id": "target", "flake_roles": [
            {"role": "top_hbn", "material": "hBN", "count": 1},
            {"role": "channel", "material": "Gr", "count": 1}]})
    return services


@pytest.fixture
def solver(monkeypatch):
    double = CentredSolver()
    monkeypatch.setattr(qumus2.records, "design_placement", double, raising=False)
    return double


def add(s, fid, material, side_um, rel_corner_mm=(5.0, 5.0)):
    rid = s.store.create_run("search_chip", {}, project_id=s.project_id)
    s.store.set_run_status(rid, "succeeded")
    if not s.store.get_sample("chip_001"):
        s.store.upsert_sample("chip_001", "chip", props={"size_mm": [10.0, 10.0]})
    s.store.upsert_sample(fid, "flake", location="chip_001", status="catalogued", props={
        "material": material, "chip_id": "chip_001", "rel_corner_mm": list(rel_corner_mm),
        "area_um2": side_um ** 2, "images": {"context": "optical-fixture.png"},
        "optical_geometry": {"outline_mm": square(side_um), "units": "mm", "frame": "flake_center_chip_axes",
            "source": "simulated_optical_outline", "source_chip_id": "chip_001",
            "image": "optical-fixture.png", "job_id": rid},
        "pickup_assessment": {"flake_id": fid, "source_chip_id": "chip_001",
            "images": ["optical-fixture.png"], "job_id": rid, "clear": True}})


def candidates(s):
    ctx = SimpleNamespace(store=s.store, project_id=s.project_id)
    return asyncio.run(picking.get_flake_candidates(picking.NoArgs(), ctx)).data


def test_ranked_pair_passes_the_unchanged_selection_code_check(s, solver):
    add(s, "gr_small", "Gr", 50)
    add(s, "gr_big", "Gr", 70)
    add(s, "hbn", "hBN", 90)
    data = candidates(s)
    assert data["target_pair_shortage"] is None
    best = data["target_pairs"][0]
    assert best["flake_ids"] == ["hbn", "gr_big"]  # larger worst-case headroom first
    assert best["selected_by_role"] == {"top_hbn": ["hbn"], "channel": ["gr_big"]}
    assert [p["flake_id"] for p in best["design_placements"]] == ["hbn", "gr_big"]
    assert best["headroom_um"] == min(best["gr_headroom_um"], best["hbn_headroom_um"]) >= 0
    ctx = SimpleNamespace(store=s.store, project_id=s.project_id)
    args = picking.SubmitSelectionArgs(selected_by_role=best["selected_by_role"], assessments=[],
                                       reasons="fixture", design_placements=best["design_placements"])
    assert picking.selection_coverage_errors(ctx, args, best["flake_ids"]) == []


def test_no_gr_reaching_the_contacts_is_a_gr_shortage(s, solver):
    add(s, "gr_tiny", "Gr", 20)
    add(s, "hbn", "hBN", 90)
    data = candidates(s)
    assert data["target_pairs"] == []
    shortage = data["target_pair_shortage"]
    assert shortage["limiting"] == "gr_contacts" and shortage["material"] == "Gr"
    assert shortage["next_action"] == "exfoliate_and_search_one_chip"
    assert shortage["closest_gr"]["flake_id"] == "gr_tiny" and shortage["closest_gr"]["shortfall_um"] > 0


def test_no_hbn_covering_a_fitting_gr_is_an_hbn_shortage(s, solver):
    add(s, "gr", "Gr", 70)
    add(s, "hbn_small", "hBN", 72)
    data = candidates(s)
    assert data["target_pairs"] == []
    shortage = data["target_pair_shortage"]
    assert shortage["limiting"] == "hbn_cover" and shortage["material"] == "hBN"
    assert shortage["gr_fitting_contacts"] == 1
    assert shortage["closest_pair"]["flake_ids"] == ["hbn_small", "gr"]


def test_solver_fit_that_fails_the_code_check_is_never_returned(s, monkeypatch):
    # A wrong solver answer (hBN shifted off Gr while claiming a fit) is dropped.
    class Lying(CentredSolver):
        def solve_pair_placement(self, *args, **kwargs):
            result = super().solve_pair_placement(*args, **kwargs)
            return {**result, "status": "fit", "hbn_headroom_um": 1.0}
    monkeypatch.setattr(qumus2.records, "design_placement", Lying(hbn_offset_mm=(0.05, 0)), raising=False)
    add(s, "gr", "Gr", 70)
    add(s, "hbn", "hBN", 90)
    data = candidates(s)
    assert data["target_pairs"] == []
    assert data["target_pair_shortage"]["limiting"] == "hbn_cover"


def test_missing_solver_is_reported_not_raised(s, monkeypatch):
    monkeypatch.delattr(qumus2.records, "design_placement", raising=False)
    monkeypatch.setitem(sys.modules, "qumus2.records.design_placement", None)
    add(s, "gr", "Gr", 70)
    data = candidates(s)
    assert data["target_pairs"] == []
    assert data["target_pair_shortage"]["limiting"] == "solver_unavailable"


def test_other_plans_carry_no_target_pairs(s, solver):
    s.store.update_project_state(s.project_id, device_plan={"flake_roles": [
        {"role": "channel", "material": "Gr", "count": 1}]})
    add(s, "gr", "Gr", 70)
    data = candidates(s)
    assert "target_pairs" not in data and "target_pair_shortage" not in data


def test_with_the_device_design_solver(s):
    """Same check through the real solver, when its module is present."""
    pytest.importorskip("qumus2.records.design_placement")
    add(s, "gr", "Gr", 70)
    add(s, "hbn", "hBN", 90)
    add(s, "hbn_small", "hBN", 60)
    data = candidates(s)
    assert data["target_pair_shortage"] is None, data["target_pair_shortage"]
    assert [p["flake_ids"] for p in data["target_pairs"]] == [["hbn", "gr"]]
    best = data["target_pairs"][0]
    ctx = SimpleNamespace(store=s.store, project_id=s.project_id)
    args = picking.SubmitSelectionArgs(selected_by_role=best["selected_by_role"], assessments=[],
                                       reasons="fixture", design_placements=best["design_placements"])
    assert picking.selection_coverage_errors(ctx, args, best["flake_ids"]) == []


@pytest.mark.parametrize("material", ["hBN", "Gr"])
def test_source_flake_near_the_chip_edge_is_not_paired(s, solver, material):
    """a518 seed 2: the picked hBN sat 0.35 mm from its chip edge, so the
    pickup contact front was clipped by the edge before it covered the flake
    plus its margin. Sources need 1.0 mm from outline to chip edge."""
    add(s, "gr", "Gr", 70, rel_corner_mm=(5.0, 0.4) if material == "Gr" else (5.0, 5.0))
    add(s, "hbn", "hBN", 90, rel_corner_mm=(9.6, 5.0) if material == "hBN" else (5.0, 5.0))
    data = candidates(s)
    assert data["target_pairs"] == []
    near = data["target_pair_shortage"]["near_chip_edge"]
    assert [item["flake_id"] for item in near] == ["hbn" if material == "hBN" else "gr"]
    assert near[0]["required_mm"] == 1.0 and near[0]["chip_edge_clearance_mm"] < 1.0


def test_chip_edge_clearance_counts_the_outline_reach(s):
    from qumus2.agents.tools.picking import _solver_source, chip_edge_clearance_mm

    # centroid 1.05 mm from the edge; a 90 um square reaches 0.0636 mm.
    add(s, "hbn", "hBN", 90, rel_corner_mm=(1.05, 5.0))
    flake = s.store.get_sample("hbn")
    clearance = chip_edge_clearance_mm(s.store, flake, _solver_source(s.store, flake))
    assert clearance == pytest.approx(1.05 - 0.045 * 2 ** 0.5)
    assert clearance < 1.0
