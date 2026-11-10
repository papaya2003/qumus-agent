"""Eyes' read-only tools (v1 lab_manager_agent): inventory and camera looks.

Eyes cannot move anything: inspect_bench runs a flow that only captures and
detects (it locks and moves nothing), and the tool waits for it itself.
"""

from __future__ import annotations

import asyncio
import json
import math
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, Field

from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.agents.tools.hardware import job_result, submit


class ListSamplesArgs(BaseModel):
    kind: str | None = Field(None, description="chip, flake, carrier, tape, ... ; None = all")
    location: str | None = Field(None, description="Slot/location, or parent chip ID for flakes. "
                                "For a one-chip search report, set this to that chip ID.")
    status: str | None = Field(None, description="e.g. bare, exfoliated, searched, candidate")


class InspectArgs(BaseModel):
    regions: list[str] | None = Field(None, description="e.g. exfoliation_stage, "
                                      "transfer_stage, middle_storage, left_chip_3; "
                                      "None = whole bench")
    camera: Literal["overview", "left", "right", "both"] = Field(
        "overview", description="One overview camera sees everything; left/right only "
        "narrow a scan without regions to that half of the bench")


def flake_catalogue_view(row, store=None):
    """Observed catalogue metadata, never repeated pixel/contour payloads.

    This projection has no write or admission authority. Picker and Designer
    continue reading complete original ledger records through their validators.
    """
    if row.get("record_reference", {}).get("view") == "observed_flake_catalogue_v1":
        return deepcopy(row)

    props = row.get("props") or {}
    small = {key: props[key] for key in ("material", "thickness_label", "area_um2", "confidence",
        "image_source", "recognition_status", "recognition_reason", "native_source_admission", "veto_reason") if key in props}
    objectives = ("context", "5x", "10x", "20x", "50x", "100x")
    small["labels"] = {key: props["labels"][key] for key in objectives if key in (props.get("labels") or {})}
    small["images"] = {key: props["images"][key] for key in objectives if key in (props.get("images") or {})}
    assessment = props.get("pickup_assessment") or props.get("reactor_pickup_assessment") or {}
    small["pickup_assessment"] = {key: assessment[key] for key in ("clear", "clear_radius_mm",
        "nearest_other_mm", "source", "timestamp", "job_id", "policy", "assessment_status",
        "required_edge_clearance_mm", "distance_basis", "detection_scope") if key in assessment}
    for name, keys in (("residue", ("level", "evidence", "by", "project_id")),
                       ("hbn_color_judgement", ("relative_body_color", "background_comparison",
                         "classification", "thick_edge_visible", "evidence"))):
        if isinstance(props.get(name), dict):
            small[name] = {key: props[name][key] for key in keys if key in props[name]}
    pickup_error = None
    if store is not None:
        from qumus2.records.materials import pickup_admission_error

        pickup_error = pickup_admission_error(store, row)
    selectable = row["status"] in ("candidate", "catalogued")
    small["availability"] = {"selectable_status": selectable,
        "pickup_neighbourhood_admitted": None if store is None else pickup_error is None,
        "reason": ("status_not_selectable" if not selectable else
                   "historical_availability_not_reassessed" if store is None else
                   "pickup_neighbourhood_unknown_stale_or_unclear" if pickup_error else "eligible_for_picker_review")}
    geometry = props.get("optical_geometry") or {}
    if not geometry:
        geometry = props.get("native_source_anchor") or {}
    shape = {key: geometry[key] for key in ("source", "units", "frame", "source_chip_id", "job_id",
        "boundary_uncertainty_mm") if key in geometry}
    try:
        components = geometry.get("region_mm", {}).get("components")
        if components is not None:
            rings = [ring for component in components for ring in (component["exterior"], *component.get("holes", []))]
            shape.update(components=len(components), holes=sum(len(c.get("holes", [])) for c in components))
        elif geometry.get("outline_mm"):
            rings = [geometry["outline_mm"]]
            shape.update(components=1, holes=0)
        else:
            rings = []
        points = [point for ring in rings for point in ring]
        if points and all(len(p) == 2 and all(type(v) in (int, float) and math.isfinite(v) for v in p) for p in points):
            shape.update(vertices=len(points), bounds_mm=[min(p[0] for p in points), min(p[1] for p in points),
                max(p[0] for p in points), max(p[1] for p in points)])
    except (KeyError, TypeError, AttributeError):
        shape["summary_status"] = "unavailable_from_recorded_geometry"
    small["geometry_summary"] = shape
    evidence = {key: props[key] for key in ("recognition_event_id", "measurement_event_20x",
        "measurement_event_100x", "native_source_anchor_event_id") if props.get(key) is not None}
    for objective in ("20x", "100x"):
        chain = props.get("recognition_"+objective) or {}
        if chain:
            evidence["recognition_"+objective] = {key: chain[key] for key in
                ("algorithm_sha256", "material_policy_sha256", "selected_label_ids") if key in chain}
            evidence["recognition_"+objective]["artifact_sha256"] = {key: chain[key].get("sha256")
                for key in ("image", "original_mask", "geometry_mask") if isinstance(chain.get(key), dict)}
        measured = props.get("measurement_"+objective) or {}
        if measured.get("receipt_artifact"):
            artifact = measured["receipt_artifact"]
            evidence["measurement_"+objective] = {key: artifact[key] for key in ("path", "sha256") if key in artifact}
    for key in ("native_source_anchor_sha256", "owner_error_budget"):
        if key in geometry:
            # The policy itself is retained in the ledger; this is its identity.
            evidence[key] = (geometry[key].get("policy_sha256") if key == "owner_error_budget" else geometry[key])
    small["evidence"] = evidence
    return {key: row[key] for key in ("id", "kind", "location", "status", "updated_at") if key in row} | {
        "props": small, "record_reference": {"sample_id": row["id"], "full_props_retained": True,
            "view": "observed_flake_catalogue_v1",
            "omitted_details": "Full contours, masks, chains, receipts and tracking data remain in the original sample record/artifacts."}}


def observed_flake_geometry_view(row):
    """One exact placement body and its frame, without repeated evidence trees."""
    catalogue = flake_catalogue_view(row)
    original = row.get("props") or {}
    geometry = original.get("optical_geometry") or {}
    observation = {key: geometry[key] for key in ("source", "units", "frame", "source_chip_id",
        "job_id", "image", "chip_rotation_deg", "boundary_uncertainty_mm", "owner_error_budget",
        "boundary_uncertainty_provenance", "native_source_anchor_event_id", "native_source_anchor_sha256")
        if key in geometry}
    for shape in ("region_mm", "outline_mm"):
        if shape in geometry:
            observation[shape] = geometry[shape]
            break
    anchor = original.get("native_source_anchor") or {}
    for key in ("anchor_exposure_mm", "anchor_definition"):
        if key in anchor:
            observation[key] = anchor[key]
    return {**catalogue, "props": {**catalogue["props"], "optical_geometry": observation},
        "record_reference": {**catalogue["record_reference"], "view": "observed_flake_geometry_v1",
            "omitted_details": "Duplicate masks, chains, receipts and tracking data remain in original records; one complete optical body is retained here."}}


@qumus_tool("list_samples", "List samples (chips, flakes, carriers, tape) with where they are "
            "and their status and material provenance, from the sample ledger. For a single "
            "chip's flakes use kind=flake and location=chip_id. Reuse job outcome for normal "
            "completion reports; query only missing or newly changed information.",
            agents={"orchestrator", "eyes", "hands", "picker", "advisor", "designer", "reactor"},
            kind="query")
async def list_samples(args: ListSamplesArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.materials import material_review, simulation_identity

    rows = ctx.store.list_samples(kind=args.kind, location=args.location)
    if args.status:
        rows = [r for r in rows if r["status"] == args.status]
    restored = json.loads(ctx.store.get_meta("simulation_identity_restore") or "null")
    rows = [{**r, "material_source": "inventory_label", "human_review": material_review(r),
             "simulation_identity": simulation_identity(r, ctx.store.bench, restored=restored)}
            if r["kind"] == "carrier" else r for r in rows]
    rows = [flake_catalogue_view(r, ctx.store) if r["kind"] == "flake" else r for r in rows]
    images = []
    for row in rows:
        if row["kind"] in ("chip", "device"):
            photos = row["props"].get("images", [])
            images.extend(photos.values() if isinstance(photos, dict) else photos)
    return ToolResult(status="ok", summary=f"{len(rows)} sample(s) on {ctx.store.bench} bench",
                      data={"bench": ctx.store.bench, "samples": rows,
                            "flake_view": "observed_catalogue_v1",
                            "detail_policy": {"omitted": ["contour_coordinates", "pixel_masks", "full_recognition_chains",
                                "full_measurement_receipts", "registration_diagnostics", "tracking_references", "stage_bookkeeping"],
                                "retained": "Complete original sample props, artifacts and journal events remain in the store; evidence pointers identify them.",
                                "admission_scope": "Catalogue availability is not final role/geometry admission; Picker and Designer validate full records internally."}},
                      images=images[:4])


class InstrumentStatusArgs(BaseModel):
    subject: str | None = Field(None, min_length=1, max_length=256,
        description="Exact laboratory evidence subject, e.g. historical:v1:microscope:10x; "
                    "omit for the latest bounded view. Device observations remain included.")
    evidence_limit: int = Field(50, ge=1, le=100,
        description="Maximum laboratory evidence properties to return (1-100). "
                    "Older omitted properties are disclosed; use subject to retrieve one.")


@qumus_tool("get_instrument_status", "Read the latest stored device observations, with "
            "event time and age. No new physical check; absent readings are unknown. "
            "Optionally filter saved laboratory evidence by exact subject. "
            "This is instrument state, not inventory or supported capabilities.",
            agents={"eyes", "orchestrator", "advisor"}, kind="query")
async def get_instrument_status(args: InstrumentStatusArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.lab_geometry import evidence_view

    readings = ctx.store.instrument_observations()
    reactor = {"enabled": "reactor_module" in (ctx.extra.get("bench") or {}),
               "fresh_measurement": False, "readiness": "unknown",
               "observations": [r for r in readings if
                   "reactor" in str(r["frame"].get("device", ""))]}
    return ToolResult(status="ok", summary=f"{len(readings)} stored instrument observation(s); "
                      "not a fresh readiness check", data={"source": "stored lab.frame events",
                      "fresh_measurement": False, "observations": readings, "reactor": reactor,
                      "lab_evidence": evidence_view(ctx.store, subject=args.subject, limit=args.evidence_limit),
                      "missing_readings": "unknown; no successful command or readiness implied"})


@qumus_tool("inspect_bench", "Look at the bench with the overview camera and report what is "
            "where (detections per region, with photos). Moves nothing. Use it to check an "
            "area is clear or that the ledger matches the bench.",
            agents={"eyes"}, kind="query")
async def inspect_bench(args: InspectArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.core.bench import regions

    valid = regions(ctx.extra.get("bench") or {})
    unknown = [r for r in args.regions or [] if r not in valid]
    if unknown:
        return ToolResult(status="error", summary=f"Unknown bench region: {', '.join(unknown)}. "
                          "Choose a region ID from valid_regions or omit regions for the whole bench.",
                          data={"valid_regions": list(valid)})
    sub = submit(ctx, "inspect_bench", args.model_dump())
    if sub.status != "pending":
        return sub
    st = await asyncio.to_thread(ctx.jobs.wait, sub.data["job_id"], 120)
    out = job_result(st, compact=False)  # the eyes need every detection
    if st.status == "succeeded" and ctx.project_id:  # rule eyes_first: the bench was seen
        ctx.store.update_project_state(ctx.project_id, eyes_checked=True)
    images = [img for o in (st.result or []) if isinstance(o, dict)
              for obs in o.get("observations", []) for img in obs.get("images", [])]
    return out.model_copy(update={"images": list(dict.fromkeys(out.images or images))})
