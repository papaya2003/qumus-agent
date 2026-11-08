"""Deterministic hBN/Gr placement for the eight-contact Hall-bar target.

Pure geometry, no model and no KLayout. Given flake-centred outlines (mm) of a
selected hBN and Gr, find rigid poses (rotation about the flake centre, then
translation in target bottom-left chip axes) such that:

  * every contact end lies inside Gr with clearance >= e_Gr;
  * the whole placed Gr lies inside hBN with clearance >= e_Gr + e_hBN;
  * both complete flakes lie inside the target chip with clearance >= own e.

``e`` is the endpoint pose-window displacement used by
``screen_design_pose_window``: configured translation + 2 R sin(rotation/2)
(+ residual guards) + any additional boundary uncertainty of the source.

Among admissible poses the solver picks the one with the largest clearance
(maximum headroom). When no pose is admissible it returns the best attempt and
the measured shortfall in micrometres: how much larger, on every side, the
limiting flake would have to be. The returned placements are FlakePlacement
compatible; acceptance still goes through create_device_design and its screen.

Convex single-component outers use an exact linear programme per rotation
(after a prototype by the pair-selection stream): maximise s subject to
n_i . (q + t) + s <= b_i for every edge half-plane (n_i, b_i) of the outer and
every vertex q of the rotated inner hull. Non-convex outers or outers with
holes use a shapely erosion/bisection search, re-checked exactly afterwards.
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Any

import numpy as np
from shapely import affinity
from shapely.geometry import MultiPolygon, Point
from shapely.geometry import Polygon as GEOSPolygon
from shapely.ops import polylabel, unary_union

from qumus2.core.polygons import convex_polygon
from qumus2.core.regions import (
    PolygonRegion,
    as_region,
    region_clearance,
    region_vertices,
    transform_region,
)
from qumus2.records.device_design import parse_hallbar_target

COARSE_STEP_DEG = 2.0
REFINE_STEPS_DEG = (0.25, 0.02)
REFINE_TOP = 3
MAX_LP_EDGES = 32
# Pixel-traced v1 outlines carry thousands of staircase vertices. The pose
# search runs on a copy eroded and simplified by this much (about one 20x
# pixel), which lies inside the observed outline; every result is re-measured
# on the full outline, so this only changes speed and ranking.
SEARCH_SIMPLIFY_MM = 0.00025
SHAPE_RESIDUAL_MM = 1e-9
FLOATING_GUARD_MM = 1e-12
_ANGLE_CHUNK = 24
# v1 never rotates a chip: a flake lands on the target in the orientation it
# had on its source chip (both chips square on the stage), so each source is
# placed at 0 deg in chip axes (#204). None searches every angle (v2 only).
FIXED_ROTATION_DEG: float | None = 0.0


# ----------------------------------------------------------------- inputs

def _policy(verification: dict | None) -> tuple[float, float]:
    if verification is None:
        from qumus2.config import load_yaml

        verification = load_yaml("verification.yaml")
    translation = verification["refind_flake"]["centre_tolerance_um"]
    from qumus2.records.device_design import design_rotation_window_deg

    rotation = design_rotation_window_deg(verification)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (translation, rotation)):
        raise ValueError("pose-window limits must be finite positive numbers")
    return float(translation), float(rotation)


def _flake(spec: dict, material: str) -> dict:
    """Normalise one caller flake: region (mm), legacy flag, boundary (mm)."""
    if not isinstance(spec, dict) or not isinstance(spec.get("flake_id"), str) or not spec["flake_id"]:
        raise ValueError(f"{material} needs a flake_id")
    if ("outline_mm" in spec) == ("region_mm" in spec):
        raise ValueError(f"{material} needs exactly one of outline_mm or region_mm")
    boundary = float(spec.get("boundary_mm", 0.0) or 0.0)
    if not math.isfinite(boundary) or boundary < 0:
        raise ValueError("boundary_mm must be finite and nonnegative")
    if "outline_mm" in spec:
        outline = convex_polygon(spec["outline_mm"])
        region = as_region(outline)
        vertices = outline
    else:
        region = as_region(spec["region_mm"])
        if not region.components:
            raise ValueError(f"{material} region is empty")
        vertices = region_vertices(region)
    return {"flake_id": spec["flake_id"], "material": material, "region": region,
            "vertices": vertices, "legacy": "outline_mm" in spec, "boundary_mm": boundary}


def pose_error_mm(vertices, translation_um: float, rotation_deg: float, boundary_mm: float = 0.) -> float:
    """Same displacement bound as screen_design_pose_window (rotation invariant)."""
    pivot = tuple(sum(p[axis] for p in vertices)/len(vertices) for axis in (0, 1))
    radius = max(math.dist(p, pivot) for p in vertices)
    return (translation_um/1000 + 2*radius*math.sin(math.radians(rotation_deg)/2)
            + SHAPE_RESIDUAL_MM + FLOATING_GUARD_MM + boundary_mm)


def _geom(region: PolygonRegion):
    parts = [GEOSPolygon(c.exterior, c.holes) for c in region.components]
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _is_convex_simple(geometry) -> bool:
    return (isinstance(geometry, GEOSPolygon) and not geometry.interiors
            and geometry.convex_hull.area - geometry.area <= 1e-9*max(geometry.area, 1e-18))


# ------------------------------------------------------------- convex LP

def _ccw(points: np.ndarray) -> np.ndarray:
    x, y = points[:, 0], points[:, 1]
    area = 0.5*(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    return points if area > 0 else points[::-1]


def _inner_simplify(points: np.ndarray, limit: int) -> np.ndarray:
    """Drop corners of a convex polygon (smallest cut first): a conservative subset."""
    pts = [tuple(p) for p in points]
    while len(pts) > limit:
        areas = []
        for i in range(len(pts)):
            a, b, c = pts[i-1], pts[i], pts[(i+1) % len(pts)]
            areas.append(abs((b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])))
        pts.pop(int(np.argmin(areas)))
    return np.asarray(pts)


class _ConvexLP:
    """max over translation of min clearance of a rotated inner set in a convex outer."""

    def __init__(self, outer_xy):
        q = _ccw(np.asarray(outer_xy, dtype=float))
        if len(q) > MAX_LP_EDGES:
            q = _inner_simplify(q, MAX_LP_EDGES)
        edges = np.roll(q, -1, axis=0) - q
        normals = np.stack([edges[:, 1], -edges[:, 0]], axis=1)
        normals /= np.linalg.norm(normals, axis=1, keepdims=True)
        self.normals = normals
        self.offsets = np.einsum("ij,ij->i", normals, q)
        triples = np.array(list(combinations(range(len(q)), 3)))
        matrix = np.concatenate([normals[triples], np.ones((len(triples), 3, 1))], axis=2)
        ok = np.abs(np.linalg.det(matrix)) > 1e-12
        self.triples = triples[ok]
        self.inverse = np.linalg.inv(matrix[ok])

    def solve(self, inner_xy, angles_deg):
        """Per angle theta: outer placed as R(theta) outer + T keeps fixed inner.

        Equivalent to the inner rotated by -theta inside the unrotated outer with
        translation t'; then T = -R(theta) t'. Returns (s, T) arrays per angle.
        """
        inner = np.asarray(inner_xy, dtype=float)
        theta = np.radians(np.asarray(angles_deg, dtype=float))
        best_s = np.empty(len(theta))
        best_t = np.empty((len(theta), 2))
        for start in range(0, len(theta), _ANGLE_CHUNK):
            th = theta[start:start+_ANGLE_CHUNK]
            cos, sin = np.cos(th), np.sin(th)
            # q = R(-theta) p
            qx = cos[:, None]*inner[None, :, 0] + sin[:, None]*inner[None, :, 1]
            qy = -sin[:, None]*inner[None, :, 0] + cos[:, None]*inner[None, :, 1]
            support = np.max(self.normals[None, :, 0, None]*qx[:, None, :]
                             + self.normals[None, :, 1, None]*qy[:, None, :], axis=2)  # (R,K)
            c = self.offsets[None, :] - support  # n.t' + s <= c
            sol = np.einsum("tij,rtj->rti", self.inverse, c[:, self.triples])  # (R,T,3)
            # Value of the concave objective min_k (c_k - n_k.t') at each vertex.
            value = np.min(c[:, None, :] - sol[..., :2] @ self.normals.T, axis=2)  # (R,T)
            pick = np.argmax(value, axis=1)
            rows = np.arange(len(th))
            best_s[start:start+len(th)] = value[rows, pick]
            tp = sol[rows, pick, :2]
            best_t[start:start+len(th), 0] = -(cos*tp[:, 0] - sin*tp[:, 1])
            best_t[start:start+len(th), 1] = -(sin*tp[:, 0] + cos*tp[:, 1])
        return best_s, best_t


# ---------------------------------------------------------- general search

def _general_clearance(outer, inner_parts, theta_deg, *, lo=-0.05, hi=0.05, steps=22):
    """Bisection on clearance s for a non-convex outer; returns (s, translation).

    Feasible translations are the intersection of the eroded rotated outer
    shifted by every inner vertex: necessary (and sufficient for convex inner
    parts in practice); the caller re-checks the winner exactly.
    """
    rotated = affinity.rotate(outer, theta_deg, origin=(0, 0))
    vertices = [p for part in inner_parts for p in part.exterior.coords[:-1]]

    def feasible(s):
        eroded = rotated.buffer(-s, quad_segs=4) if s else rotated
        if eroded.is_empty:
            return None
        region = None
        for x, y in vertices:
            shifted = affinity.translate(eroded, -x, -y)
            region = shifted if region is None else region.intersection(shifted)
            if region.is_empty:
                return None
        return region

    while hi - lo > 1e-7 and feasible(lo) is None:
        lo, hi = 2*lo - hi, lo  # extend downwards until something fits
        if lo < -1.0:
            return -math.inf, None
    best = feasible(lo)
    for _ in range(steps):
        mid = (lo + hi)/2
        found = feasible(mid)
        if found is None:
            hi = mid
        else:
            lo, best = mid, found
    if best is None:
        return -math.inf, None
    parts = list(getattr(best, "geoms", [best]))
    parts = [p for p in parts if isinstance(p, GEOSPolygon) and not p.is_empty]
    if parts:
        piece = max(parts, key=lambda p: p.area)
        point = polylabel(piece, tolerance=1e-7) if piece.area > 0 else piece.representative_point()
    else:
        point = best.representative_point()
    if not isinstance(point, Point) or point.is_empty:
        return -math.inf, None
    return lo, (-point.x, -point.y)


def _search_copy(outer):
    """A low-vertex copy of a non-convex outer that lies inside it (search only)."""
    inner = outer.buffer(-SEARCH_SIMPLIFY_MM, join_style="mitre")
    copy = inner.simplify(SEARCH_SIMPLIFY_MM, preserve_topology=True)
    return outer if copy.is_empty else copy


# --------------------------------------------------------------- driver

def _search(outer_region: PolygonRegion, inner_parts: list, *, fixed_angle: float | None = None):
    """Best (clearance_mm, rotation_deg, translation_mm) of outer around fixed inner."""
    outer = _geom(outer_region)
    if _is_convex_simple(outer):
        lp = _ConvexLP(outer.exterior.coords[:-1])
        hull = unary_union(inner_parts).convex_hull
        inner = np.asarray(hull.exterior.coords[:-1] if isinstance(hull, GEOSPolygon) else hull.coords)

        def evaluate(angles):
            s, t = lp.solve(inner, angles)
            return list(zip(s.tolist(), list(angles), t.tolist(), strict=True))
    else:
        search_outer = _search_copy(outer)
        search_inner = [part.simplify(SEARCH_SIMPLIFY_MM, preserve_topology=True) for part in inner_parts]

        def evaluate(angles):
            out = []
            for a in angles:
                s, t = _general_clearance(search_outer, search_inner, float(a))
                out.append((s, float(a), t))
            return out

    if fixed_angle is not None:
        results = evaluate([fixed_angle])
    else:
        step = COARSE_STEP_DEG if _is_convex_simple(outer) else 10.0
        results = evaluate(np.arange(0.0, 360.0, step))
        for fine in (REFINE_STEPS_DEG if _is_convex_simple(outer) else (1.0, 0.1)):
            seeds = sorted(results, key=lambda r: r[0], reverse=True)[:REFINE_TOP]
            angles = sorted({round((a + k*fine) % 360.0, 6) for _, a, _ in seeds
                             for k in range(-int(round(step/fine)), int(round(step/fine)) + 1)})
            results += evaluate(angles)
            step = fine
    s, angle, translation = max(results, key=lambda r: r[0])
    if translation is None:
        return -math.inf, 0.0, (0.0, 0.0)
    return float(s), float(angle % 360.0), (float(translation[0]), float(translation[1]))


def _contact_parts(target):
    return [GEOSPolygon(c.points_mm) for c in target.contacts]


def _chip(target):
    w, h = target.size_mm
    return [(0., 0.), (w, 0.), (w, h), (0., h)]


def signed_clearance_mm(outer, inner) -> float:
    """Exact clearance when covered; otherwise minus the farthest protrusion."""
    clearance = region_clearance(outer, inner)
    if clearance is not None:
        return clearance
    a, b = _geom(as_region(outer)), _geom(as_region(inner))
    outside = b.difference(a)
    points = [p for part in getattr(outside, "geoms", [outside]) if isinstance(part, GEOSPolygon)
              for ring in (part.exterior, *part.interiors) for p in ring.coords]
    return -max((a.distance(Point(p)) for p in points), default=0.0)


def _exact_gr(target, placed: PolygonRegion, required_mm: float) -> dict:
    per_contact = {c.id: signed_clearance_mm(placed, as_region(c.points_mm))*1000 for c in target.contacts}
    worst = min(per_contact.values())
    chip = signed_clearance_mm(_chip(target), placed)
    return {"contacts_um": per_contact, "min_clearance_um": worst,
            "headroom_um": worst - required_mm*1000, "chip_headroom_um": (chip - required_mm)*1000}


def solve_gr_placement(target, gr: dict, *, verification: dict | None = None) -> dict:
    """Best Gr pose over the eight contact ends; reusable across hBN candidates."""
    parsed = parse_hallbar_target(target)
    translation_um, rotation_deg = _policy(verification)
    flake = _flake(gr, "Gr")
    error = pose_error_mm(flake["vertices"], translation_um, rotation_deg, flake["boundary_mm"])
    s, angle, offset = _search(flake["region"], _contact_parts(parsed), fixed_angle=FIXED_ROTATION_DEG)
    placed = transform_region(flake["region"], xy=offset, rotation_deg=angle)
    exact = _exact_gr(parsed, placed, error)
    headroom = exact["headroom_um"]
    return {"flake_id": flake["flake_id"], "placement": {"flake_id": flake["flake_id"],
            "translation_mm": [offset[0], offset[1]], "rotation_deg": angle},
            "status": "fit" if min(headroom, exact["chip_headroom_um"]) >= 0 else "no_fit",
            "shortfall_um": max(0.0, -min(headroom, exact["chip_headroom_um"])),
            "limiting": None if min(headroom, exact["chip_headroom_um"]) >= 0
                        else "gr_contacts" if headroom <= exact["chip_headroom_um"] else "chip",
            "gr_headroom_um": headroom, "gr_required_clearance_um": error*1000,
            "gr_best_clearance_um": s*1000, "gr_error_mm": error, "placed_region_mm": placed.model_dump(mode="json"),
            "contacts_clearance_um": exact["contacts_um"], "gr_chip_headroom_um": exact["chip_headroom_um"]}


def solve_pair_placement(target, hbn: dict, gr: dict, *, verification: dict | None = None,
                         gr_solution: dict | None = None) -> dict:
    """Gr over the contacts, then hBN over that whole Gr; measured shortfall if not."""
    parsed = parse_hallbar_target(target)
    translation_um, rotation_deg = _policy(verification)
    gr_part = gr_solution or solve_gr_placement(parsed, gr, verification=verification)
    if gr_part["flake_id"] != gr.get("flake_id"):
        raise ValueError("Reused Gr solution belongs to another flake")
    flake = _flake(hbn, "hBN")
    hbn_error = pose_error_mm(flake["vertices"], translation_um, rotation_deg, flake["boundary_mm"])
    gr_error = gr_part["gr_error_mm"]
    required = hbn_error + gr_error
    placed_gr = as_region(gr_part["placed_region_mm"])
    # Covering the whole Gr is equivalent to covering each of its components;
    # for the search a convex hull per component is a necessary condition.
    hulls = [part.convex_hull for part in getattr(_geom(placed_gr), "geoms", [_geom(placed_gr)])]
    s, angle, offset = _search(flake["region"], hulls, fixed_angle=FIXED_ROTATION_DEG)
    placed = transform_region(flake["region"], xy=offset, rotation_deg=angle)
    hbn_headroom = (signed_clearance_mm(placed, placed_gr) - required)*1000
    hbn_chip = (signed_clearance_mm(_chip(parsed), placed) - hbn_error)*1000
    headrooms = {"gr_contacts": gr_part["gr_headroom_um"], "hbn_cover": hbn_headroom,
                 "chip": min(gr_part["gr_chip_headroom_um"], hbn_chip)}
    limiting, worst = min(headrooms.items(), key=lambda kv: kv[1])
    fit = worst >= 0
    return {"status": "fit" if fit else "no_fit",
            "placements": [{"flake_id": flake["flake_id"], "translation_mm": [offset[0], offset[1]],
                            "rotation_deg": angle}, gr_part["placement"]],
            "gr_headroom_um": gr_part["gr_headroom_um"], "hbn_headroom_um": hbn_headroom,
            "shortfall_um": 0.0 if fit else max(0.0, -worst), "limiting": None if fit else limiting,
            "gr_required_clearance_um": gr_part["gr_required_clearance_um"],
            "hbn_required_clearance_um": required*1000,
            "scope": "Nominal geometry within the configured endpoint pose window; create_device_design re-screens it."}


def placement_summary(result: dict) -> str:
    """One readable line for tools and logs."""
    if result["status"] == "fit":
        return (f"Fits: Gr headroom {result['gr_headroom_um']:.2f} um over the eight contact ends, "
                f"hBN headroom {result['hbn_headroom_um']:.2f} um over the whole Gr.")
    what = {"gr_contacts": "Gr cannot contain all eight contact ends with the pose-window margin",
            "hbn_cover": "hBN cannot cover the whole Gr with both pose-window margins",
            "chip": "a flake cannot stay inside the target chip"}[result["limiting"]]
    return f"Does not fit: {what}; short by {result['shortfall_um']:.2f} um on every side."


def describe_requirement(target, *, verification: dict | None = None) -> dict[str, Any]:
    """Size facts of the contact set, for pickers and humans (not an admission)."""
    parsed = parse_hallbar_target(target)
    hull = unary_union(_contact_parts(parsed)).convex_hull
    minx, miny, maxx, maxy = hull.bounds
    translation_um, _ = _policy(verification)
    return {"contact_hull_area_um2": hull.area*1e6, "contact_span_um": [(maxx-minx)*1000, (maxy-miny)*1000],
            "translation_tolerance_um": translation_um}


def design_margins(spec, *, verification: dict | None = None) -> dict[str, Any]:
    """Distance margins of a built design at its declared poses (µm), for humans.

    Same rule as the solver: every contact end inside Gr by >= e_Gr, the whole
    Gr inside hBN by >= e_Gr + e_hBN. ``*_headroom_um`` is what is left beyond
    that requirement (the official screen states coverage in area, not distance).
    """
    from qumus2.records.device_design import (
        DeviceDesignSpec,
        RegionDesignSource,
        source_additional_boundary_uncertainty,
    )

    parsed = DeviceDesignSpec.model_validate(spec)
    translation_um, rotation_deg = _policy(verification)
    placed, errors = {}, {}
    for source in parsed.sources:
        shape = ({"region_mm": source.region_mm.model_dump(mode="json")} if isinstance(source, RegionDesignSource)
                 else {"outline_mm": [list(p) for p in source.outline_mm]})
        flake = _flake({"flake_id": source.flake_id, **shape,
                        "boundary_mm": source_additional_boundary_uncertainty(source)}, source.material)
        placed[source.material] = transform_region(flake["region"], xy=source.placement.translation_mm,
                                                   rotation_deg=source.placement.rotation_deg)
        errors[source.material] = pose_error_mm(flake["vertices"], translation_um, rotation_deg, flake["boundary_mm"])
    target = parse_hallbar_target(parsed.target)
    gr = _exact_gr(target, placed["Gr"], errors["Gr"])
    required_hbn = errors["Gr"] + errors["hBN"]
    return {"translation_tolerance_um": translation_um, "rotation_tolerance_deg": rotation_deg,
            "gr_required_clearance_um": errors["Gr"]*1000, "gr_min_clearance_um": gr["min_clearance_um"],
            "gr_headroom_um": gr["headroom_um"], "hbn_required_clearance_um": required_hbn*1000,
            "hbn_min_clearance_um": signed_clearance_mm(placed["hBN"], placed["Gr"])*1000,
            "hbn_headroom_um": (signed_clearance_mm(placed["hBN"], placed["Gr"]) - required_hbn)*1000}


def margins_summary(margins: dict[str, Any]) -> str:
    return (f"Gr holds every electrode end at >= {margins['gr_min_clearance_um']:.2f} um from its edge "
            f"(needs {margins['gr_required_clearance_um']:.2f}, spare {margins['gr_headroom_um']:.2f}); "
            f"hBN holds the whole Gr at >= {margins['hbn_min_clearance_um']:.2f} um "
            f"(needs {margins['hbn_required_clearance_um']:.2f}, spare {margins['hbn_headroom_um']:.2f}); "
            f"pose window {margins['translation_tolerance_um']:g} um / {margins['rotation_tolerance_deg']:g} deg per flake.")


def plan_selected_placement(store, project_id: str, *, verification: dict | None = None) -> dict | None:
    """Solve for the current selection of an eight-contact task; None if not one.

    Reads the same registered target and complete source observations that
    build_design_spec uses, so the suggested placements can be passed unchanged
    to create_device_design (which re-screens and saves them).
    """
    from qumus2.records.device_design import (
        FlakePlacement,
        RegionDesignSource,
        _source,
        source_additional_boundary_uncertainty,
    )

    state = (store.get_project(project_id) or {}).get("state", {})
    chip = store.get_sample((state.get("device_plan") or {}).get("target_chip_id", "") or "")
    selected = state.get("selected_flakes") or []
    if not chip or chip.get("props", {}).get("device_design_target") is None or len(selected) != 2:
        return None
    target = parse_hallbar_target(chip["props"]["device_design_target"])
    flakes = {}
    for flake_id in selected:
        source = _source(store, FlakePlacement(flake_id=flake_id, translation_mm=(0., 0.), rotation_deg=0.))
        shape = ({"region_mm": source.region_mm.model_dump(mode="json")} if isinstance(source, RegionDesignSource)
                 else {"outline_mm": [list(p) for p in source.outline_mm]})
        flakes[source.material] = {"flake_id": flake_id, **shape,
                                   "boundary_mm": source_additional_boundary_uncertainty(source)}
    if set(flakes) != {"hBN", "Gr"}:
        raise ValueError("The eight-contact design needs exactly one selected hBN and one selected Gr")
    result = solve_pair_placement(target, flakes["hBN"], flakes["Gr"], verification=verification)
    return {**result, "summary": placement_summary(result)}
