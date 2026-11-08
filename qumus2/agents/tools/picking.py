"""Picker's tools (v1 flake_selection_agent). No human gate: a selection takes
effect once the code check passes; a human can veto later in the chat.

The checks v1 made around its Claude SDK call are here, in code
(v1 sdk_agent.py: `_candidate_payload`, `_blocked_for_roles`,
`validate_selection_result`):
  - candidates come from the ledger only, minus vetoed and lost flakes;
  - every role of the device plan gets exactly its required count;
  - one physical flake serves one role instance only;
  - each selected flake's optical images were actually shown to the picker
    (view_flake_images records it; v1 checked the SDK's Read calls);
  - scores are 0-100, a reason is given, hBN flakes carry the colour check and
    are not selected when it says too_thick;
  - each selected flake carries a surface-residue judgement and it is not
    heavy (a good flake has a clean surface; the judgement is kept on the
    flake for experiment outcomes, and heavy-residue flakes stop being
    candidates);
  - a design-proven impossible combination is not returned again;
  - "blocked" must name the right action: missing images -> relink/capture,
    never exfoliation; too few good flakes -> exfoliate and search one chip.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.config import load_yaml
from qumus2.core.materials import normalize_material_code
from qumus2.core.run_paths import resolve_run_path
from qumus2.records.device_design import (
    FlakePlacement,
    ImportedHallBarTarget,
    parse_hallbar_target,
    target_import_binding,
    validate_imported_target_files,
)
from qumus2.records.materials import pickup_admission_error

# The selection lives in project state; the ledger status stays as the search
# left it (stack_device takes catalogued flakes).
SELECTABLE = {"candidate", "catalogued"}
SCORES = ("geometry_fitness", "visible_cleanliness", "continuity_uniformity",
          "evidence_confidence", "inventory_tiebreaker", "overall_score")
IMAGE_KEYS = ("100x", "20x")
PENDING_REVISIT_LIMIT = 20


def require_images() -> bool:
    return bool(load_yaml("agents.yaml").get("picking", {}).get("require_optical_images", True))


def _state(ctx: ToolContext) -> dict[str, Any]:
    proj = ctx.store.get_project(ctx.project_id) if ctx.project_id else None
    return (proj or {"state": {}})["state"]


def plan_roles(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Roles the device plan needs flakes for, with counts (v1 _required_flake_counts)."""
    return list((state.get("device_plan") or {}).get("flake_roles", []))


def images_of(flake: dict[str, Any]) -> dict[str, str]:
    imgs = flake["props"].get("images") or {}
    return {k: imgs[k] for k in ("context", *IMAGE_KEYS) if imgs.get(k)}


def has_images(flake: dict[str, Any]) -> bool:
    imgs = images_of(flake)
    return all(k in imgs and resolve_run_path(imgs[k]).is_file() for k in IMAGE_KEYS)


#: flake props the picker does not need (stage bookkeeping for the hands' flows)
_INTERNAL_PROPS = frozenset({"stage_xy", "rel_corner_mm", "stage_from_corner_mm", "sim_id"})


def candidate_view(flake: dict[str, Any]) -> dict[str, Any]:
    """A candidate as the picker sees it: id, status, what the search measured,
    the picker's own earlier residue judgement and the image paths."""
    from qumus2.agents.tools.inventory import flake_catalogue_view

    catalogue = flake_catalogue_view(flake)
    return {"id": flake["id"], "status": flake["status"], "location": flake["location"],
            **catalogue["props"], "record_reference": catalogue["record_reference"]}


def shown_candidates(cands: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """The candidates the picker judges: those with 20X/100X images (v1 showed
    only flakes with optical artifacts). Without require_optical_images, all."""
    if not require_images():
        return cands
    return {role: [f for f in fs if has_images(f)] for role, fs in cands.items()}


def candidates_by_role(ctx: ToolContext, inventory: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
    state = _state(ctx)
    excluded = set(state.get("excluded_flakes", []))
    flakes = [f for f in (ctx.store.list_samples(kind="flake") if inventory is None else inventory)
              if f["status"] in SELECTABLE and f["id"] not in excluded
              and (f["props"].get("residue") or {}).get("level") != "heavy"
              and pickup_admission_error(ctx.store, f) is None]
    out: dict[str, list[dict[str, Any]]] = {}
    for r in plan_roles(state):
        mat = normalize_material_code(r["material"])
        labels = set(r.get("thickness_labels") or [])
        out[r["role"]] = [
            f for f in flakes
            if normalize_material_code(f["props"].get("material")) == mat
            and (not labels or f["props"].get("thickness_label") in labels)
            and f["props"].get("area_um2", 0) >= r.get("min_area_um2", 0)]
    return out


def revisited_unresolved(store, evidence: dict | None) -> bool:
    """A current neighbourhood check from a targeted revisit that still could not decide.

    Revisiting again repeats the same observation, so such a flake is no longer
    pending evidence repair. It stays unadmitted for pickup (unchanged rule).
    """
    if evidence is None or evidence.get("assessment_status") != "unknown":
        return False
    run = store.get_run(evidence.get("job_id", ""))
    return bool(run) and run["flow"] == "revisit_chip"


def pending_revisits_by_role(ctx: ToolContext, inventory: list[dict[str, Any]],
                             unresolved: dict[str, list[dict]] | None = None) -> dict[str, list[dict]]:
    """Public stock awaiting observations; none of these entries grants selection.

    Flakes whose targeted revisit already ended unresolved are left out (they would
    otherwise block exfoliation forever) and, when `unresolved` is given, listed there.
    """
    from qumus2.records.geometry import current_pickup_evidence

    state = _state(ctx)
    excluded = set(state.get("excluded_flakes", []))
    out = {}
    for role in plan_roles(state):
        pending = []
        wanted = set(role.get("thickness_labels") or [])
        for flake in inventory:
            props = flake["props"]
            if (flake["status"] not in SELECTABLE or flake["id"] in excluded
                    or normalize_material_code(props.get("material")) != normalize_material_code(role["material"])
                    or (props.get("residue") or {}).get("level") == "heavy"
                    or (props.get("hbn_color_judgement") or {}).get("classification") == "too_thick"
                    or props.get("recognition_status") in ("unsupported", "unknown")
                    or props.get("location_uncertain")):
                continue
            area = props.get("area_um2")
            if isinstance(area, (int, float)) and area < role.get("min_area_um2", 0):
                continue
            labels = props.get("labels") or {}
            detailed_label = labels.get("100x") or labels.get("20x")
            if wanted and detailed_label and detailed_label not in wanted:
                continue
            missing = [key for key in IMAGE_KEYS if not (images_of(flake).get(key)
                       and resolve_run_path(images_of(flake)[key]).is_file())]
            error = pickup_admission_error(ctx.store, flake)
            if error:
                observed = current_pickup_evidence(ctx.store, flake)
                if observed is not None and observed.get("assessment_status") == "occupied":
                    continue  # A current observed obstacle is not missing evidence.
                if not missing and revisited_unresolved(ctx.store, observed):
                    if unresolved is not None:
                        unresolved.setdefault(role["role"], []).append({
                            "id": flake["id"], "location": flake["location"], "area_um2": area,
                            "evidence_status": "revisit_tried_unresolved", "job_id": observed.get("job_id"),
                            "reasons": sorted({v["reason"] for v in observed.get("views", []) if v.get("reason")}),
                            "selection_admitted": False})
                    continue
            if not error and not (missing and require_images()):
                continue
            coarse_label = labels.get("10x") or props.get("thickness_label")
            pending.append({"id": flake["id"], "location": flake["location"],
                "material": props.get("material"), "thickness_label": coarse_label, "area_um2": area,
                "evidence_status": "awaiting_optical_images" if missing else "pickup_reinspection_required",
                "missing_objectives": missing, "pickup_admission_error": error,
                "has_10x_observation": bool(labels.get("10x")) or flake["status"] == "candidate",
                "coarse_geometry_10x_available": bool(props.get("coarse_geometry_10x")),
                "thickness_screen": ("unknown" if not coarse_label else "coarse_label_mismatch_unverified"
                    if wanted and not detailed_label and coarse_label not in wanted else "observed_role_label_match"),
                "selection_admitted": False})
        # Matching coarse labels first; unknown/mismatched coarse observations
        # remain visible without promoting them to measured role eligibility.
        pending.sort(key=lambda item: (item["thickness_screen"] != "observed_role_label_match", item["id"]))
        out[role["role"]] = pending
    return out


def _revisit_requests(entries: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for item in entries:
        if item["location"]:
            grouped.setdefault((item["location"], item["material"]), []).append(item["id"])
    return [{"tool": "hands.search_chip", "args": {"chip_id": chip, "revisit_only": True,
             "material": material, "max_revisits": 1},
             "pending_flake_ids": ids,
             "explicit_target_args": {"chip_id": chip, "revisit_only": True, "material": material,
                                      "flake_ids": ids, "max_revisits": len(ids)},
             "note": "Default request uses normal automatic priority; explicit IDs are optional targets, not a qualified selection."}
            for (chip, material), ids in grouped.items()]


def candidate_diagnostics(ctx: ToolContext, roles: list[dict[str, Any]], flakes: list[dict[str, Any]]) -> dict:
    """Bounded rejection reasons from the same ledger, not another inventory tool."""
    state = _state(ctx)
    out = {}
    for role in roles:
        counts: dict[str, int] = {}
        dispositions: dict[str, int] = {}
        disposition_examples = []
        examples = []
        for f in flakes:
            p = f["props"]
            reasons = []
            if f["status"] not in SELECTABLE or f["id"] in state.get("excluded_flakes", []):
                reasons.append("unavailable_or_vetoed")
            if (p.get("residue") or {}).get("level") == "heavy":
                reasons.append("heavy_residue")
            if pickup_admission_error(ctx.store, f):
                reasons.append("pickup_neighbourhood_needs_inspection")
            if normalize_material_code(p.get("material")) != normalize_material_code(role["material"]):
                reasons.append("material")
            if role.get("thickness_labels") and p.get("thickness_label") not in role["thickness_labels"]:
                reasons.append("thickness")
            if p.get("area_um2", 0) < role.get("min_area_um2", 0):
                reasons.append("area")
            if normalize_material_code(p.get("material")) == normalize_material_code(role["material"]):
                labels = p.get("labels") or {}
                detailed_label = labels.get("100x") or labels.get("20x")
                disposition = ("user_excluded" if f["id"] in state.get("excluded_flakes", []) else
                    "revisit_failed_or_lost" if f["status"] == "lost" or p.get("recognition_status") in ("unsupported", "unknown") else
                    "unavailable_status" if f["status"] not in SELECTABLE else
                    "heavy_residue" if (p.get("residue") or {}).get("level") == "heavy" else
                    "observed_thickness_unsuitable" if role.get("thickness_labels") and detailed_label
                        and detailed_label not in role["thickness_labels"] else
                    "observed_area_below_minimum" if p.get("area_um2") is not None
                        and p["area_um2"] < role.get("min_area_um2", 0) else None)
                if disposition:
                    dispositions[disposition] = dispositions.get(disposition, 0)+1
                elif "pickup_neighbourhood_needs_inspection" in reasons:
                    from qumus2.records.geometry import current_pickup_evidence

                    evidence = current_pickup_evidence(ctx.store, f)
                    status = "observed_pickup_occupied" if evidence and evidence.get("assessment_status") == "occupied" else "inspection_unknown_or_stale"
                    dispositions[status] = dispositions.get(status, 0)+1
                    disposition = status
                if disposition and len(disposition_examples) < 5:
                    disposition_examples.append({"id": f["id"], "status": disposition,
                        "recognition_reason": p.get("recognition_reason")})
            for reason in reasons:
                counts[reason] = counts.get(reason, 0) + 1
            if reasons and len(examples) < 5:
                examples.append({"id": f["id"], "reasons": reasons})
        out[role["role"]] = {"inventory_flakes": len(flakes), "rejection_counts": counts,
                              "public_disposition_counts": dispositions,
                              "public_disposition_examples": disposition_examples,
                              "rejected_examples": examples,
                              "note": "Counts may overlap; global stored inventory included."}
    return out


# ----- tools ------------------------------------------------------------------

class NoArgs(BaseModel):
    pass


class ViewArgs(BaseModel):
    flake_id: str


class HbnColor(BaseModel):
    relative_body_color: str
    background_comparison: str
    thick_edge_visible: bool
    classification: Literal["usable_color_family", "too_thick", "uncertain"]
    evidence: str


class Assessment(BaseModel):
    flake_id: str
    feasible: bool
    geometry_fitness: float = Field(ge=0, le=100)
    visible_cleanliness: float = Field(ge=0, le=100)
    continuity_uniformity: float = Field(ge=0, le=100)
    evidence_confidence: float = Field(ge=0, le=100)
    inventory_tiebreaker: float = Field(ge=0, le=100)
    overall_score: float = Field(ge=0, le=100)
    observed_strengths: list[str] = []
    observed_risks: list[str] = []
    segmentation_disagreements: list[str] = []
    hbn_color_judgement: HbnColor | None = None
    surface_residue: Literal["clean", "light", "heavy", "uncertain"] | None = Field(
        None, description="Tape residue on the chip around the flake, from the 100X image "
        "(heavy = too much for a device). Required for every flake you select.")
    residue_evidence: str = ""
    reason: str = Field(min_length=1)

    @field_validator("observed_strengths", "observed_risks", "segmentation_disagreements", mode="before")
    @classmethod
    def _one_note(cls, value: Any) -> Any:
        # A model sometimes writes one note as a plain sentence; keep it as that one item.
        return [value] if isinstance(value, str) and value.strip() else [] if isinstance(value, str) else value

    usable_width_um: float | None = Field(None, gt=0, allow_inf_nan=False)
    usable_height_um: float | None = Field(None, gt=0, allow_inf_nan=False)
    geometry_evidence: str = Field("", description="Calibrated 20X evidence for a clean continuous "
        "inscribed rectangle (not the total flake area); state scale and orientation.")


class SubmitSelectionArgs(BaseModel):
    selected_by_role: dict[str, list[str]] = Field(
        description="role -> flake ids; every role of the device plan, exact counts")
    assessments: list[Assessment] = Field(
        description="One entry for EVERY candidate you were given, selected or not")
    reasons: str = Field(min_length=1, description="Why this combination")
    design_placements: list[FlakePlacement] = Field(default_factory=list, description=
        "For device_design_v1 only: proposed full-source rigid placements in target chip-bottom-left mm axes. Exactly selected hBN and Gr; code checks whole-Gr coverage at these transforms. Designer separately validates all eight contacts in the saved GDS.")

    @property
    def flake_ids(self) -> list[str]:  # for the refs / not_vetoed policy checks
        return [f for ids in self.selected_by_role.values() for f in ids]


class BlockedArgs(BaseModel):
    role: str
    action: Literal["exfoliate_and_search_one_chip", "relink_or_capture_optical_images"]
    reason: str
    assessments: list[Assessment] = Field(default_factory=list, description=
        "When readable candidates meet the role's required count, assess every one before "
        "requesting more exfoliation. View their images first. For each infeasible candidate, "
        "give a nonblank reason and observed_risks describing the optical geometry, "
        "cleanliness or evidence limitation; an optional backup is not a requirement.")


#: pairs returned to the picker, and Gr candidates paired per call (best Gr headroom first)
TARGET_PAIR_LIMIT = 5
TARGET_PAIR_GR_LIMIT = 5


def _solver_source(store, flake: dict[str, Any]) -> dict[str, Any]:
    """One ledger observation in the solver's shape, through the same readers as the code check."""
    from qumus2.records.geometry import _pickup_outline, _pickup_region

    geometry = flake["props"].get("optical_geometry") or {}
    if "region_mm" in geometry:
        _pickup_region(store, flake)  # provenance only; raises when not admitted
        return {"flake_id": flake["id"], "region_mm": geometry["region_mm"]}
    return {"flake_id": flake["id"], "outline_mm": [list(p) for p in _pickup_outline(store, flake)]}


def _source_points_mm(source: dict[str, Any]) -> list[tuple[float, float]]:
    if "outline_mm" in source:
        return [(float(x), float(y)) for x, y in source["outline_mm"]]
    region = source["region_mm"]
    return [(float(x), float(y)) for component in region["components"] for x, y in component["exterior"]]


def chip_edge_clearance_mm(store, flake: dict[str, Any], source: dict[str, Any]) -> float:
    """Smallest gap from the observed flake outline to its chip's edge.

    Orientation-free and conservative: the centroid's distance to the nearest
    chip side minus the outline's largest centre-relative reach. A contact
    front that must cover the flake plus its margin is clipped by a near chip
    edge before it covers (a518 seed 2, flake 0.35 mm from the edge).
    """
    chip = store.get_sample(flake["props"].get("chip_id") or flake.get("location") or "")
    size = (chip or {}).get("props", {}).get("size_mm")
    corner = flake["props"].get("rel_corner_mm")
    if not size or not corner:
        raise ValueError("no chip size or chip-corner position for the chip-edge clearance")
    x, y = float(corner[0]), float(corner[1])
    centre = min(x, y, float(size[0])-x, float(size[1])-y)
    reach = max(math.hypot(px, py) for px, py in _source_points_mm(source))
    return centre-reach


def target_pair_search(store, target, hbn: list[dict[str, Any]], gr: list[dict[str, Any]], *,
                       solver: Any = None, verification: dict | None = None) -> dict[str, Any]:
    """Rank admissible hBN/Gr pairs for an eight-contact target, or name the shortage.

    Placements come from the device-design solver (records.design_placement):
    Gr holds every contact end with its pose-window clearance; hBN holds the
    whole placed Gr. Each returned pair is re-checked with the unchanged
    selection code check, so this ranks; it never admits by itself.
    """
    from qumus2.records.geometry import pickup_coverage_errors

    if solver is None:
        from qumus2.records import design_placement as solver
    if verification is None:
        verification = load_yaml("verification.yaml")  # once per search, not per solve
    unreadable: list[dict[str, str]] = []
    near_edge: list[dict[str, Any]] = []
    edge_mm = float(verification.get("pickup", {}).get("source_chip_edge_clearance_mm", 1.0))

    def sources(flakes):
        out = []
        for flake in flakes:
            try:
                source = _solver_source(store, flake)
                clearance = chip_edge_clearance_mm(store, flake, source)
            except (ValueError, TypeError, KeyError) as exc:
                unreadable.append({"flake_id": flake["id"], "reason": str(exc)})
                continue
            if clearance < edge_mm:
                near_edge.append({"flake_id": flake["id"], "chip_edge_clearance_mm": round(clearance, 4),
                                  "required_mm": edge_mm})
                continue
            out.append((flake, source))
        return out

    gr_fits, gr_best = [], None
    for flake, source in sources(gr):
        solution = solver.solve_gr_placement(target, source, verification=verification)
        if solution["status"] == "fit":
            gr_fits.append((flake, source, solution))
            continue
        shortfall = solution.get("shortfall_um", max(0.0, -solution["gr_headroom_um"]))
        if gr_best is None or shortfall < gr_best["shortfall_um"]:
            gr_best = {"flake_id": flake["id"], "shortfall_um": shortfall,
                       "gr_headroom_um": solution["gr_headroom_um"],
                       "gr_required_clearance_um": solution.get("gr_required_clearance_um")}
    hbn_sources = sources(hbn)
    report: dict[str, Any] = {"gr_checked": len(gr), "gr_fitting_contacts": len(gr_fits),
                              "hbn_checked": len(hbn), "unreadable": unreadable, "near_chip_edge": near_edge}
    if not gr_fits:
        return {"pairs": [], "shortage": {**report, "limiting": "gr_contacts", "material": "Gr",
            "next_action": "exfoliate_and_search_one_chip", "closest_gr": gr_best,
            "reason": "No observed Gr candidate holds all eight contact ends with the pose-window clearance."}}
    gr_fits.sort(key=lambda item: -item[2]["gr_headroom_um"])
    pairs, closest, rejected = [], None, []
    for gr_flake, gr_source, gr_solution in gr_fits[:TARGET_PAIR_GR_LIMIT]:
        for hbn_flake, hbn_source in hbn_sources:
            result = solver.solve_pair_placement(target, hbn_source, gr_source, verification=verification,
                                                 gr_solution=gr_solution)
            if result["status"] != "fit":
                if closest is None or result["shortfall_um"] < closest["shortfall_um"]:
                    closest = {"flake_ids": [hbn_flake["id"], gr_flake["id"]], **{k: result.get(k) for k in (
                        "shortfall_um", "hbn_headroom_um", "hbn_required_clearance_um", "limiting")}}
                continue
            ids = [hbn_flake["id"], gr_flake["id"]]
            placements = {p["flake_id"]: {"xy_mm": p["translation_mm"], "rotation_deg": p["rotation_deg"]}
                          for p in result["placements"]}
            if errors := pickup_coverage_errors(store, ids, placements):
                rejected.append({"flake_ids": ids, "problems": errors})
                continue
            pairs.append({"flake_ids": ids, "design_placements": [
                FlakePlacement(flake_id=p["flake_id"], translation_mm=tuple(p["translation_mm"]),
                               rotation_deg=p["rotation_deg"]).model_dump(mode="json")
                for p in result["placements"]],
                "headroom_um": min(result["gr_headroom_um"], result["hbn_headroom_um"]),
                "gr_headroom_um": result["gr_headroom_um"], "hbn_headroom_um": result["hbn_headroom_um"]})
    pairs.sort(key=lambda pair: -pair["headroom_um"])
    if pairs:
        return {"pairs": pairs[:TARGET_PAIR_LIMIT], "shortage": None, "rejected_by_code_check": rejected,
                "near_chip_edge": near_edge}
    return {"pairs": [], "rejected_by_code_check": rejected, "shortage": {**report, "limiting": "hbn_cover",
        "material": "hBN", "next_action": "exfoliate_and_search_one_chip", "closest_pair": closest,
        "reason": "Gr candidates hold the contacts, but no observed hBN candidate covers any of them "
                  "with the combined pose-window clearance."}}


def target_pairs_for_plan(ctx: ToolContext, roles: list[dict[str, Any]], shown: dict[str, list[dict[str, Any]]],
                          target) -> dict[str, Any]:
    """device_design_v1 only: the plan's single hBN role over its single Gr role."""
    by_material = {}
    for role in roles:
        by_material.setdefault(normalize_material_code(role["material"]), []).append(role["role"])
    if len(by_material.get("hBN", [])) != 1 or len(by_material.get("Gr", [])) != 1 or len(roles) != 2:
        return {"pairs": [], "shortage": {"limiting": "plan", "reason":
                "device_design_v1 pairing needs exactly one hBN role and one Gr role"}}
    hbn_role, gr_role = by_material["hBN"][0], by_material["Gr"][0]
    found = target_pair_search(ctx.store, target, shown[hbn_role], shown[gr_role])
    for pair in found["pairs"]:
        pair["selected_by_role"] = {hbn_role: [pair["flake_ids"][0]], gr_role: [pair["flake_ids"][1]]}
    return found


@qumus_tool("get_flake_candidates",
            "The device plan's flake roles (material, count, thickness, minimum area) and "
            "the candidate flakes for each role from the sample ledger. Vetoed and lost "
            "flakes are removed. Includes bounded rejection and missing-image diagnostics; "
            "10X-only stock is pending revisit, never qualified selection. "
            "do not routinely repeat list_samples to interpret an empty result.",
            agents={"picker", "designer", "orchestrator"}, kind="query")
async def get_flake_candidates(args: NoArgs, ctx: ToolContext) -> ToolResult:
    state = _state(ctx)
    roles = plan_roles(state)
    if not roles:
        return ToolResult(status="error", summary="no device plan with flake roles yet; the "
                          "advisor submits one with submit_device_plan")
    inventory = ctx.store.list_samples(kind="flake")
    cands = candidates_by_role(ctx, inventory)
    shown = shown_candidates(cands)
    unresolved: dict[str, list[dict]] = {}
    pending = pending_revisits_by_role(ctx, inventory, unresolved)
    bounded = {role: items[:PENDING_REVISIT_LIMIT] for role, items in pending.items()}
    unseen = {role: [item for item in items if item["missing_objectives"]]
              for role, items in bounded.items()}
    diagnostics = candidate_diagnostics(ctx, roles, inventory)
    for role in roles:
        key = role["role"]
        failure = state.get("design_fit_failure") or {}
        replace = bool(failure.get("layout_impossible") and
                       (not failure.get("failed_roles") or key in failure["failed_roles"]))
        awaiting = [item for item in pending[key] if item["missing_objectives"]]
        next_action = ("relink_or_capture_optical_images" if pending[key] and replace else
                       "assess_images" if len(shown[key]) >= role["count"] else
                       "relink_or_capture_optical_images" if awaiting and any(
                           item["pickup_admission_error"] is None or item["has_10x_observation"]
                           for item in awaiting) else
                       "inspect_pickup_neighbourhood" if pending[key] else "exfoliate_and_search_one_chip")
        diagnostics[key].update(eligible_with_images=len(shown[key]),
                                eligible_missing_images=sum(not has_images(f) for f in cands[key]),
                                pending_revisit_count=len(pending[key]),
                                pending_revisit_omitted=max(0, len(pending[key])-PENDING_REVISIT_LIMIT),
                                missing=max(0, role["count"] - len(shown[key])),
                                next_action=next_action,
                                blocker_type="image_evidence_repair" if pending[key] else None,
                                exfoliation_allowed=not pending[key] and len(shown[key]) < role["count"])
    data = {"roles": roles, "diagnostics_by_role": diagnostics,
            "candidates_by_role": {r: [candidate_view(f) for f in fs]
                                   for r, fs in shown.items()},
            # 10X-only flakes that fit a role: not selectable until the hands
            # photograph them (search_chip revisit_only), never assessed blind
            "unphotographed_by_role": unseen,
            "pending_revisit_by_role": bounded,
            # targeted revisit already tried and still undecided: not pending, not admitted
            "revisit_unresolved_by_role": {role: items[:PENDING_REVISIT_LIMIT] for role, items in unresolved.items()},
            "revisit_requests_by_role": {role: _revisit_requests(items) for role, items in bounded.items()},
            "pending_revisit_policy": "Provisional public observations only; normal targeted20X/100X revisit and pickup admission are required. Omitted entries remain pending, not exhausted stock.",
            "excluded_flakes": state.get("excluded_flakes", []),
            "design_fit_failure": state.get("design_fit_failure")}
    plan = state.get("device_plan") or {}
    if plan.get("design_contract") == "device_design_v1":
        chip = ctx.store.get_sample(plan["target_chip_id"]) or {}
        data["design_contract"] = "device_design_v1"
        try:
            target = parse_hallbar_target(chip.get("props", {}).get("device_design_target"))
            if isinstance(target, ImportedHallBarTarget):
                if chip["props"].get("imported_target_binding") != target_import_binding(target):
                    raise ValueError("Imported target lacks its matching bench-construction binding")
                validate_imported_target_files(ctx.store, target)
            data["device_design_target"] = {"chip_id": plan["target_chip_id"], **target.model_dump(mode="json")}
        except (ValueError, OSError) as exc:
            data["device_design_target"] = {"status": "unresolved", "reason": str(exc)}
        else:
            try:
                found = target_pairs_for_plan(ctx, roles, shown, target)
            except ImportError as exc:  # solver module not present in this build
                found = {"pairs": [], "shortage": {"limiting": "solver_unavailable", "reason": str(exc)}}
            data["target_pairs"] = found["pairs"]
            data["target_pair_shortage"] = found["shortage"]
            data["target_pair_policy"] = (
                "Ranked by worst pose-window headroom from current ledger outlines; submit_flake_selection "
                "with a pair's selected_by_role and design_placements still runs the full code check.")
    n = sum(len(v) for v in shown.values())
    m = sum(sum(bool(item["missing_objectives"]) for item in items) for items in pending.values())
    return ToolResult(status="ok", summary=f"{len(roles)} role(s), {n} candidate(s) with "
                      f"20X/100X images to assess" + (f"; {m} more at 10X only or missing detail images "
                                                      "(unqualified pending revisit)"
                                                      if m else ""), data=data)


@qumus_tool("view_flake_images",
            "Show the captured 100X and 20X optical images, original label mask and "
            "separate selected-component geometry mask when available, of one "
            "flake. Selection requires that you looked at both images of every flake you "
            "select.", agents={"picker", "designer"}, kind="query", refs={"flake_id": "flake"})
async def view_flake_images(args: ViewArgs, ctx: ToolContext) -> ToolResult:
    flake = ctx.store.get_sample(args.flake_id)
    assert flake is not None  # refs check
    imgs = images_of(flake)
    present = [p for p in imgs.values() if resolve_run_path(p).is_file()]
    if ctx.project_id and ctx.agent == "picker":
        viewed = sorted({*_state(ctx).get("viewed_flake_images", []), args.flake_id})
        ctx.store.update_project_state(ctx.project_id, viewed_flake_images=viewed)
    mask = flake["props"].get("mask_20x")
    images = present + ([mask] if mask and resolve_run_path(mask).is_file() else [])
    recognition = flake["props"].get("recognition_20x")
    recognition_evidence = None
    if recognition:
        from qumus2.records.recognition import validate_recognition_chain

        try:
            validate_recognition_chain(recognition, image_path=imgs.get("20x", ""))
            original = recognition["original_mask"]["path"]
            geometry = recognition["geometry_mask"]["path"]
            images = list(dict.fromkeys([*present, original, geometry]))
            recognition_evidence = {
                "status": "original_masks_verified",
                "original_label_mask": original, "selected_component_mask": geometry,
                "geometry_status": recognition["derivation"].get("status", "unknown"),
                "geometry_reason": recognition["derivation"].get("unsupported_reason"),
                "scope": "Mask identity only; material accuracy and design admission are separate.",
            }
        except ValueError as exc:
            images = present
            recognition_evidence = {"status": "unresolved", "reason": str(exc)}
    summary = (f"{args.flake_id}: {', '.join(imgs)} image(s) attached" if present
               else f"{args.flake_id} has no readable optical images")
    from qumus2.agents.tools.inventory import observed_flake_geometry_view

    catalogue = observed_flake_geometry_view(flake)
    return ToolResult(status="ok" if present else "needs_attention", summary=summary,
                      data={"flake_id": args.flake_id, "images": imgs,
                            "props": catalogue["props"],
                            "record_reference": catalogue["record_reference"],
                            "recognition_evidence": recognition_evidence}, images=images)


def optical_selection_errors(ctx: ToolContext, flake: dict, assessment: Assessment | None) -> list[str]:
    """Task-independent optical quality acceptance for any picked-up layer."""
    fid, errors = flake["id"], []
    if require_images() and not has_images(flake):
        errors.append(f"{fid} has no readable 100X and 20X optical images")
    if fid not in _state(ctx).get("viewed_flake_images", []):
        errors.append(f"you did not look at the images of {fid}; call view_flake_images")
    if assessment is None:
        return [*errors, f"{fid}: provide an optical assessment using the common selection principles"]
    if not assessment.feasible:
        errors.append(f"{fid} is selected but assessed as not feasible")
    if assessment.surface_residue is None:
        errors.append(f"{fid}: surface_residue is required for a selected flake")
    elif assessment.surface_residue == "heavy":
        errors.append(f"{fid} has heavy tape residue; a good flake has a clean surface")
    elif assessment.surface_residue == "uncertain" or not assessment.residue_evidence.strip():
        errors.append(f"{fid}: selected surfaces require clean/light residue with image evidence")
    if normalize_material_code(flake["props"].get("material")) == "hBN":
        if assessment.hbn_color_judgement is None:
            errors.append(f"{fid} is hBN: hbn_color_judgement is required")
        elif assessment.hbn_color_judgement.classification == "too_thick":
            errors.append(f"{fid} failed the hBN colour check (too_thick)")
    return errors


def validate_selection(ctx: ToolContext, args: SubmitSelectionArgs) -> list[str]:
    """Port of v1 validate_selection_result. Returns the problems found."""
    state = _state(ctx)
    roles = {r["role"]: r for r in plan_roles(state)}
    cands = candidates_by_role(ctx)
    errs: list[str] = []
    if not roles:
        return ["no device plan with flake roles yet"]
    missing = sorted(set(roles) - set(args.selected_by_role))
    extra = sorted(set(args.selected_by_role) - set(roles))
    if missing:
        errs.append(f"roles missing from the selection: {missing}")
    if extra:
        errs.append(f"roles not in the device plan: {extra}")
    by_id = {a.flake_id: a for a in args.assessments}
    all_cands = {f["id"]: f for fs in cands.values() for f in fs}
    shown = {f["id"] for fs in shown_candidates(cands).values() for f in fs}
    unassessed = sorted(shown - set(by_id))
    if unassessed:
        errs.append(f"assess every candidate you were shown, including rejected ones; "
                    f"missing: {unassessed}")
    used: set[str] = set()
    for role, ids in args.selected_by_role.items():
        spec = roles.get(role)
        if spec is None:
            continue
        need = int(spec.get("count", 1))
        if len(ids) != need:
            errs.append(f"role {role!r} needs exactly {need} flake(s), got {len(ids)}")
        ok_ids = {f["id"] for f in cands.get(role, [])}
        for fid in ids:
            if fid in used:
                errs.append(f"{fid} is used twice; one physical flake serves one role")
            used.add(fid)
            selected = ctx.store.get_sample(fid)
            if selected and selected["kind"] == "flake" and (error := pickup_admission_error(ctx.store, selected)):
                errs.append(error)
            if fid not in ok_ids:
                errs.append(f"{fid} is not a candidate for role {role!r} (material, "
                            "thickness, size, vetoed or lost)")
                continue
            flake = all_cands[fid]
            a = by_id.get(fid)
            errs += optical_selection_errors(ctx, flake, a)
            if a is None:
                continue
            footprint = spec.get("required_footprint")
            if footprint and (state.get("device_plan") or {}).get("design_contract") != "device_design_v1":
                width, height = a.usable_width_um, a.usable_height_um
                if not width or not height or not a.geometry_evidence.strip():
                    errs.append(f"{fid}: calibrated clean continuous footprint dimensions and "
                                "geometry_evidence are required; total area is insufficient")
                elif not ((width >= footprint["width_um"] and height >= footprint["height_um"])
                          or (height >= footprint["width_um"] and width >= footprint["height_um"])):
                    errs.append(f"{fid}: clean footprint does not fit target electrodes")
    failure = state.get("design_fit_failure") or {}
    if failure.get("layout_impossible") and failure.get("flake_ids"):
        if set(failure["flake_ids"]) == used:
            errs.append("this exact combination was proven impossible to lay out by the "
                        "designer; choose another")
    return errs


def selection_coverage_errors(ctx: ToolContext, args: SubmitSelectionArgs, ordered: list[str]) -> list[str]:
    from qumus2.records.geometry import pickup_coverage_errors

    new_design = (_state(ctx).get("device_plan") or {}).get("design_contract") == "device_design_v1"
    if not new_design:
        return (["design_placements requires the eight-contact device_design_v1 plan"]
                if args.design_placements else pickup_coverage_errors(ctx.store, ordered))
    if (len(args.design_placements) != len(ordered)
            or sorted(p.flake_id for p in args.design_placements) != sorted(ordered)):
        return ["device_design_v1 selection requires exactly one proposed rigid placement per selected source"]
    placements = {p.flake_id: {"xy_mm": p.translation_mm, "rotation_deg": p.rotation_deg}
                  for p in args.design_placements}
    return pickup_coverage_errors(ctx.store, ordered, placements)


def save_residue(ctx: ToolContext, assessments: list[Assessment]) -> None:
    """Keep the picker's residue judgement on each flake: a good flake has a
    clean surface (Jialei, 2026-10-21); experiment outcomes count it."""
    for a in assessments:
        flake = ctx.store.get_sample(a.flake_id)
        if a.surface_residue is None or flake is None or flake["kind"] != "flake":
            continue
        ctx.store.upsert_sample(a.flake_id, "flake", props={"residue": {
            "level": a.surface_residue, "evidence": a.residue_evidence, "by": ctx.agent,
            "project_id": ctx.project_id}})


@qumus_tool("submit_flake_selection",
            "Submit the complete flake set for the device. Code checks it (roles and counts, "
            "candidates only, no reuse, images viewed, scores, hBN colour check, surface "
            "residue judged and not heavy); if it passes it takes effect at once. Example: "
            '{"selected_by_role": {"channel": ["chip_002_f002"]}, "assessments": [{"flake_id": '
            '"chip_002_f002", "feasible": true, "geometry_fitness": 80, "visible_cleanliness": '
            '75, "continuity_uniformity": 80, "evidence_confidence": 70, '
            '"inventory_tiebreaker": 50, "overall_score": 78, "surface_residue": "clean", '
            '"residue_evidence": "100X: substrate around the flake is clear", "reason": '
            '"largest uniform monolayer"}], "reasons": "..."} (an hBN flake adds '
            '"hbn_color_judgement": {"relative_body_color": "...", "background_comparison": '
            '"...", "thick_edge_visible": false, "classification": "usable_color_family", '
            '"evidence": "20X image"}).',
            agents={"picker"}, kind="state", refs={"flake_ids": "flake"},
            requires=("not_vetoed",))
async def submit_flake_selection(args: SubmitSelectionArgs, ctx: ToolContext) -> ToolResult:
    errs = validate_selection(ctx, args)
    if errs:
        return ToolResult(status="error", summary="selection rejected by the code check: "
                          + "; ".join(errs), data={"problems": errs})
    from qumus2.records.geometry import selection_source_snapshot

    # Device role order is the accepted top-first layer order, never dict insertion order.
    ordered = [fid for role in plan_roles(_state(ctx)) for fid in args.selected_by_role.get(role["role"], [])]
    if coverage_errors := selection_coverage_errors(ctx, args, ordered):
        return ToolResult(status="error", summary="; ".join(coverage_errors), data={"problems": coverage_errors})
    flat = args.flake_ids
    save_residue(ctx, args.assessments)
    if ctx.project_id:
        ctx.store.update_project_state(
            ctx.project_id, selected_flakes=flat, selected_by_role=args.selected_by_role,
            selection_reasons=args.reasons, selection_source_evidence=selection_source_snapshot(ctx.store, flat),
            design_accepted=False, design_fit_failure=None,
            selection_design_placements=[p.model_dump(mode="json") for p in args.design_placements],
            selection_assessments=[a.model_dump() for a in args.assessments])
    return ToolResult(status="ok", summary=f"selected {len(flat)} flake(s); code check passed",
                      data={"selected_by_role": args.selected_by_role})


def blocked_assessment_errors(args: BlockedArgs, ctx: ToolContext,
                              candidates: list[dict[str, Any]], required: int) -> list[str]:
    """Check shortage evidence without turning coarse eligibility into optical acceptance."""
    eligible = {f["id"] for f in candidates}
    readable = {f["id"] for f in candidates if has_images(f)}
    ids = [a.flake_id for a in args.assessments]
    assessed = set(ids)
    errors = []
    if len(ids) != len(assessed):
        errors.append("duplicate assessment IDs are not allowed")
    if unknown := assessed - eligible:
        errors.append(f"assessment IDs are not current candidates for {args.role!r}: {sorted(unknown)}")
    if unreadable := (assessed & eligible) - readable:
        errors.append(f"assessment IDs have no readable 20X/100X images: {sorted(unreadable)}")
    if unviewed := assessed - set(_state(ctx).get("viewed_flake_images", [])):
        errors.append(f"call view_flake_images before assessing: {sorted(unviewed)}")
    needs_evidence = (args.action == "exfoliate_and_search_one_chip" and len(readable) >= required)
    if needs_evidence or args.assessments:
        if missing := readable - assessed:
            errors.append(f"assess every readable candidate for {args.role!r}; missing: {sorted(missing)}")
    for a in args.assessments:
        if not a.reason.strip():
            errors.append(f"{a.flake_id}: a nonblank assessment reason is required")
        if not a.feasible and not any(risk.strip() for risk in a.observed_risks):
            errors.append(f"{a.flake_id}: explain the optical rejection in observed_risks "
                          "(geometry, cleanliness or insufficient evidence)")
    if needs_evidence and not errors:
        feasible = sum(a.feasible for a in args.assessments)
        if feasible >= required:
            errors.append(f"role {args.role!r} requires {required} flake(s), and {feasible} "
                          "are assessed feasible; submit the required set for code validation. "
                          "An optional backup does not justify more exfoliation")
    return errors


@qumus_tool("report_selection_blocked",
            "Report that a role cannot be filled: too few good flakes "
            "(exfoliate_and_search_one_chip) or candidates without readable images "
            "(relink_or_capture_optical_images: the hands photograph them with search_chip "
            "revisit_only, no new exfoliation). If readable candidates meet the structured "
            "Pending role-relevant10X stock must be revisited before claiming depletion. "
            "role count, include per-candidate optical assessments explaining the shortage. "
            "Optional backups do not increase the required count.", agents={"picker"}, kind="state")
async def report_selection_blocked(args: BlockedArgs, ctx: ToolContext) -> ToolResult:
    inventory = ctx.store.list_samples(kind="flake")
    cands = candidates_by_role(ctx, inventory).get(args.role)
    if cands is None:
        return ToolResult(status="error", summary=f"{args.role!r} is not a role of the plan")
    pending = pending_revisits_by_role(ctx, inventory)[args.role]
    bounded = pending[:PENDING_REVISIT_LIMIT]
    no_images = sorted({f["id"] for f in cands if not has_images(f)} |
                       {item["id"] for item in pending if item["missing_objectives"]})
    if args.action == "exfoliate_and_search_one_chip" and pending:
        return ToolResult(status="error", summary="Existing candidates need normal targeted revisit: report "
                          "relink_or_capture_optical_images, not exfoliation", data={
                              "blocker_type": "image_evidence_repair", "exfoliation_allowed": False,
                              "pending_revisit_count": len(pending), "pending_revisit": bounded,
                              "revisit_requests": _revisit_requests(bounded)})
    if args.action == "exfoliate_and_search_one_chip" and no_images and require_images():
        return ToolResult(status="error", summary=f"candidates {no_images} have no readable "
                          "images: report relink_or_capture_optical_images, not exfoliation")
    if args.action == "relink_or_capture_optical_images" and not pending and not no_images:
        return ToolResult(status="error", summary=("No eligible candidates; inspect the candidate "
                          "query's rejection diagnostics, not an image problem" if not cands else
                          "all candidates have images; this is not an image problem"))
    role = next(r for r in plan_roles(_state(ctx)) if r["role"] == args.role)
    required = int(role.get("count", 1))
    errors = blocked_assessment_errors(args, ctx, cands, required)
    if errors:
        return ToolResult(status="error", summary="shortage report rejected: " + "; ".join(errors),
                          data={"required_count": required, "problems": errors})
    blocked = {"role": args.role, "action": args.action, "reason": args.reason,
               "candidates": [f["id"] for f in cands],
               "without_images": {item["id"]: item["location"] for item in bounded if item["missing_objectives"]},
               "pending_revisit_count": len(pending), "pending_revisit": bounded,
               "revisit_requests": _revisit_requests(bounded),
               "blocker_type": "image_evidence_repair" if pending else "observed_stock_shortage",
               "exfoliation_allowed": not pending,
               "required_count": required,
               "assessments": [a.model_dump() for a in args.assessments]}
    if ctx.project_id:
        ctx.store.update_project_state(ctx.project_id, selection_blocked=blocked)
    return ToolResult(status="ok", summary=f"role {args.role} blocked: {args.action}",
                      data=blocked)
