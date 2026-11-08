"""Design tools: verified KLayout hBN/Gr artifacts and legacy rectangle records.

New eight-contact targets use create_device_design and device_design_v1. Local
KlayoutClaw MCP saves and reopens complete polygons in one owned native view; an arbitrary caller GDS
path cannot authorize the new task. Legacy rectangle checks remain for older
tasks and recordings, explicitly without KLayout certification:
  - the device plan is complete (port of v1 validate_device_guidance_for_layout);
  - the selection it designs for is the current one;
  - every flake role of the plan has geometry, metal layers too if the plan has
    contacts;
  - the channel meets the resolved target footprint when supplied, otherwise
    the plan's minimum (legacy graphene QH default 5 x 10 um);
  - gate metal keeps 3.0 um from contact metal (v1 graphene-qh-device skill);
  - encapsulating hBN covers the channel.
The legacy optional gds_path is historical metadata, not artifact validation.
If the selected flakes cannot hold the device, report_design_infeasible hands
the combination back to the picker (v1 flake_fit_failure_report).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.agents.tools.picking import images_of
from qumus2.records.design_placement import plan_selected_placement
from qumus2.records.device_design import (
    DesignPoseWindowError,
    DesignPoseWindowReport,
    FlakePlacement,
    ImportedHallBarTarget,
    bind_artifact,
    build_design_pose_window_admission,
    build_design_spec,
    design_pose_window_policy,
    parse_hallbar_target,
    require_design_pose_window,
    target_import_binding,
    target_metal_primitives,
    validate_design_current,
)
from qumus2.records.geometry import contact_bridge_geometry, geometry_digest, guidance_binding

GATE_CONTACT_CLEARANCE_UM = 3.0
DEFAULT_MIN_CHANNEL_UM = (5.0, 10.0)


class Rect(BaseModel):
    layer: str = Field(description="A stack role (e.g. channel, top_hbn) or a metal layer "
                       "(contact_metal, gate_metal)")
    x_um: float
    y_um: float
    w_um: float = Field(gt=0)
    h_um: float = Field(gt=0)

    def gap(self, o: Rect) -> float:
        dx = max(o.x_um - (self.x_um + self.w_um), self.x_um - (o.x_um + o.w_um), 0.0)
        dy = max(o.y_um - (self.y_um + self.h_um), self.y_um - (o.y_um + o.h_um), 0.0)
        return (dx * dx + dy * dy) ** 0.5

    def contains(self, o: Rect) -> bool:
        return (self.x_um <= o.x_um and self.y_um <= o.y_um
                and o.x_um + o.w_um <= self.x_um + self.w_um
                and o.y_um + o.h_um <= self.y_um + self.h_um)


class SubmitDesignArgs(BaseModel):
    flake_ids: list[str] = Field(description="The selected flakes this layout is for")
    shapes: list[Rect] = Field(min_length=1)
    channel_role: str = Field("channel", description="Which role is the active channel")
    gds_path: str | None = None
    notes: str = ""


class InfeasibleArgs(BaseModel):
    flake_ids: list[str] = Field(min_length=1)
    failed_roles: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, description="Which placements were tried and why none "
                        "fits; aesthetics or a lower score are not enough")


class NoArgs(BaseModel):
    pass


def _state(ctx: ToolContext) -> dict[str, Any]:
    proj = ctx.store.get_project(ctx.project_id) if ctx.project_id else None
    return (proj or {"state": {}})["state"]


def _min_channel(plan: dict[str, Any]) -> tuple[float, float]:
    import re

    # Prepatterned targets have a code-derived footprint from measured/design
    # contact geometry. A generic legacy QH default must not override it.
    footprint = plan.get("required_footprint")
    if footprint:
        return float(footprint["width_um"]), float(footprint["height_um"])
    m = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(?:um|µm)?\s*min",
                  plan.get("channel", "").lower())
    return (float(m.group(1)), float(m.group(2))) if m else DEFAULT_MIN_CHANNEL_UM


def validate_layout(state: dict[str, Any], args: SubmitDesignArgs) -> list[str]:
    plan = state.get("device_plan") or {}
    errs: list[str] = []
    if not plan:
        return ["no device plan"]
    if sorted(args.flake_ids) != sorted(state.get("selected_flakes", [])):
        errs.append("flake_ids are not the current selection")
    layers: dict[str, list[Rect]] = {}
    for s in args.shapes:
        layers.setdefault(s.layer, []).append(s)
    roles = [r["role"] for r in plan.get("flake_roles", [])]
    missing = [r for r in roles if r not in layers]
    if missing:
        errs.append(f"no geometry for flake roles {missing}")
    contact_intent = plan.get("direct_contact_intent")
    if plan.get("direct_contact_material") and not contact_intent:
        errs.append("explicit direct contact requires bound named existing contact regions")
    if plan.get("contact_scheme") and not contact_intent and "contact_metal" not in layers:
        errs.append("plan has contacts but the layout has no contact_metal")
    chan = layers.get(args.channel_role, [])
    if args.channel_role not in roles:
        errs.append(f"channel_role {args.channel_role!r} is not a flake role of the plan")
    elif chan:
        need = sorted(_min_channel(plan))
        body = max(chan, key=lambda r: r.w_um * r.h_um)
        if contact_intent:
            from qumus2.core.polygons import rectangle

            intent = dict(contact_intent)
            if intent.pop("intent_sha256", None) != geometry_digest(intent):
                errs.append("direct contact target intent changed")
            else:
                outline = rectangle(body.w_um/1000, body.h_um/1000,
                    ((body.x_um+body.w_um/2)/1000, (body.y_um+body.h_um/2)/1000))
                result = contact_bridge_geometry(outline, contact_intent["target_contact"], contact_intent["bridge_requirement"])
                if result["status"] != "pass":
                    errs.append("channel layout does not bridge both bound existing contacts with the declared overlap/margin")
        have = sorted((body.w_um, body.h_um))
        if have[0] < need[0] or have[1] < need[1]:
            errs.append(f"channel {body.w_um}x{body.h_um} um is below the minimum "
                        f"{need[0]}x{need[1]} um")
        for role in roles:
            if "hbn" in role.lower() and role in layers:
                if not any(c.contains(body) for c in layers[role]):
                    errs.append(f"{role} does not cover the channel")
    contact_rects = list(layers.get("contact_metal", []))
    if contact_intent:
        for p in contact_intent["target_contact"]["region"]["polygons"]:
            xs, ys = [x*1000 for x, _ in p["points_mm"]], [y*1000 for _, y in p["points_mm"]]
            contact_rects.append(Rect(layer="existing_contact", x_um=min(xs), y_um=min(ys),
                                      w_um=max(xs)-min(xs), h_um=max(ys)-min(ys)))
    for g in layers.get("gate_metal", []):
        for c in contact_rects:
            if g.gap(c) < GATE_CONTACT_CLEARANCE_UM:
                errs.append(f"gate metal is {g.gap(c):.2f} um from contact metal; keep "
                            f">= {GATE_CONTACT_CLEARANCE_UM} um")
                break
    return errs


def _design_transport() -> str:
    from qumus2.agents.klayout import KLayoutError, selected_backend

    try:
        backend = selected_backend()
    except KLayoutError as exc:
        return str(exc)
    if backend == "python":
        return ("Headless klayout Python module (QUMUS_KLAYOUT_BACKEND=python): the same generated save/reopen "
                "GDS checks as KlayoutClaw, with a rendered preview instead of a native view.")
    return ("KlayoutClaw MCP on the existing loopback bridge; one atomic owned native view, exact saved/reopened "
            "GDS checks and a device-scale native viewport. No automatic switch to another backend.")


def _design_source_view(flake):
    """One exact placement body, plus the shared bounded catalogue metadata."""
    from qumus2.agents.tools.inventory import observed_flake_geometry_view

    view = observed_flake_geometry_view(flake)
    return {"id": flake["id"], **view["props"], "images": images_of(flake),
            "record_reference": view["record_reference"]}


def model_device_design_view(record):
    """Display the original design identity without duplicating source bodies.

    The original canonical digest identifies the complete stored record. This
    explicit model-only view is never used by design/stacking validation.
    """
    if not isinstance(record, dict) or record.get("model_view") == "device_design_summary_v1":
        return record
    if not any("region_mm" in source or (source.get("observation") or {}).get("recognition_chain")
               for source in record.get("sources", [])):
        return record  # Small historical convex records keep their original API.
    sources = []
    keys = ("source", "units", "frame", "source_chip_id", "job_id", "image", "chip_rotation_deg",
        "boundary_uncertainty_mm", "boundary_uncertainty_provenance", "owner_error_budget",
        "native_source_anchor_event_id", "native_source_anchor_sha256")
    for source in record.get("sources", []):
        raw = source.get("observation") or {}
        observation = {key: raw[key] for key in keys if key in raw}
        chain = raw.get("recognition_chain") or {}
        if chain:
            observation["recognition_reference"] = {key: chain[key] for key in
                ("algorithm_sha256", "material_policy_sha256", "selected_label_ids") if key in chain}
            observation["recognition_reference"]["image_sha256"] = chain.get("image", {}).get("sha256")
        if raw.get("original20_identity"):
            observation["original20_identity_sha256"] = raw["original20_identity"].get("recognition_chain_sha256")
        sources.append({**source, "observation": observation})
    return {**record, "sources": sources, "model_view": "device_design_summary_v1", "full_record_retained": True,
        "canonical_digest_scope": "Identifies the original complete stored design; this projected view is not an executable design record."}


@qumus_tool("get_design_context",
            "The device plan, the selected flakes by role with their size, thickness and "
            "image paths, and any earlier design-fit failure. Registered eight-contact targets also expose their config-derived endpoint pose window for geometric screening, "
            "and suggested_placement: the deterministic solver's best hBN/Gr placements (pass them unchanged to create_device_design when status is fit) or, when status is no_fit, the limiting constraint and the measured shortfall in um for report_design_infeasible.",
            agents={"designer"}, kind="query", requires=("flakes_selected",))
async def get_design_context(args: NoArgs, ctx: ToolContext) -> ToolResult:
    st = _state(ctx)
    by_role = {role: [_design_source_view(sample) if (sample := ctx.store.get_sample(f))
                      else {"id": f, "images": {}} for f in ids]
               for role, ids in (st.get("selected_by_role") or {}).items()}
    targets = [c for c in ctx.store.list_samples(kind="chip")
               if c["props"].get("electrodes") or c["props"].get("device_design_target")]
    pose_context = {}
    target_id = (st.get("device_plan") or {}).get("target_chip_id")
    target = next((chip for chip in targets if chip["id"] == target_id), None)
    if target and target["props"].get("device_design_target") is not None:
        from qumus2.config import load_yaml

        try:
            typed_target = parse_hallbar_target(target["props"]["device_design_target"])
            policy = design_pose_window_policy(load_yaml("verification.yaml"))
        except ValueError as exc:
            return ToolResult(status="error", summary=f"Registered eight-contact design context is invalid: {exc}")
        pose_context["endpoint_pose_window"] = {
            "target_chip_id": target_id, "policy": policy.model_dump(mode="json"),
            "scope": DesignPoseWindowReport.model_fields["scope"].default,
            "required_geometry": [
                "hBN whole-Gr containment clearance >= sum of both source displacement envelopes",
                ("Every complete original contact end must be contained inside the displacement-eroded Gr core"
                 if isinstance(typed_target, ImportedHallBarTarget) else
                 "Every contact intersection with the displacement-eroded Gr core >= contact_overlap_um squared (um2)"),
                "Each complete source chip-boundary clearance >= its own displacement envelope"],
        }
        if isinstance(typed_target, ImportedHallBarTarget):
            pose_context["imported_target"] = {
                "target_chip_id": target_id, "version": typed_target.version,
                "coverage_policy": typed_target.coverage_policy,
                "contact_regions": [contact.model_dump(mode="json") for contact in typed_target.contacts],
                "metal_primitive_count": len(target_metal_primitives(typed_target)),
                "source_binding": target_import_binding(typed_target),
                "scope": "Owner-imported full metal; fixed original complete boxes and continuous first 2um voltage ends. Original source flake drawings are references, never selected inventory.",
            }
        try:
            pose_context["suggested_placement"] = plan_selected_placement(ctx.store, ctx.project_id)
        except (ValueError, KeyError, OSError, AttributeError, TypeError) as exc:
            pose_context["suggested_placement"] = {"status": "unavailable", "summary": str(exc)}
    photos = [p for c in targets for p in c["props"].get("images", {}).values()]
    photos += [images.get("100x") or images.get("20x") or next(iter(images.values())) for fs in by_role.values()
               for f in fs if (images := f["images"])]
    return ToolResult(status="ok", summary=f"{sum(map(len, by_role.values()))} selected "
                      "flake(s)", data={"device_plan": st.get("device_plan"),
                                        "selected_by_role": by_role,
                                        "selection_design_placements": st.get("selection_design_placements"),
                                        "target_chips": targets,
                                        "device_design": model_device_design_view(st.get("device_design")),
                                        **pose_context,
                                        "design_transport": _design_transport(),
                                        "design_fit_failure": st.get("design_fit_failure")},
                      images=photos[:4])


@qumus_tool("submit_design", "Submit the device layout for the selected flakes. Code checks "
            "it independently (roles covered, channel size, hBN coverage, gate-contact "
            "clearance); if it passes it is accepted and stacking may start. Shapes are "
            "rectangles by their lower-left corner and size in um, NOT x0/y0/x1/y1. Example: "
            '{"flake_ids": ["chip_001_f035", "chip_002_f002", "chip_001_f032"], "shapes": '
            '[{"layer": "channel", "x_um": 0, "y_um": 0, "w_um": 6, "h_um": 12}, {"layer": '
            '"top_hbn", "x_um": -4, "y_um": -4, "w_um": 14, "h_um": 20}, {"layer": '
            '"bottom_hbn", "x_um": -4, "y_um": -4, "w_um": 14, "h_um": 20}, {"layer": '
            '"contact_metal", "x_um": 1, "y_um": -8, "w_um": 4, "h_um": 6}], "channel_role": '
            '"channel", "notes": "..."}',
            agents={"designer"}, kind="state", refs={"flake_ids": "flake"},
            requires=("flakes_selected",))
async def submit_design(args: SubmitDesignArgs, ctx: ToolContext) -> ToolResult:
    state = _state(ctx)
    target_id = (state.get("device_plan") or {}).get("target_chip_id")
    target = ctx.store.get_sample(target_id) if target_id else None
    if target and target["props"].get("device_design_target"):
        return ToolResult(status="error", summary="This eight-contact task requires create_device_design with complete selected-flake transforms and a verified KLayout artifact; legacy rectangles cannot authorize it.")
    errs = validate_layout(_state(ctx), args)
    if errs:
        return ToolResult(status="error", summary="design rejected by the code check: "
                          + "; ".join(errs), data={"problems": errs})
    try:
        binding = guidance_binding(ctx.store, _state(ctx).get("device_plan") or {})
    except ValueError as exc:
        return ToolResult(status="error", summary=str(exc))
    if ctx.project_id:
        ctx.store.update_project_state(ctx.project_id, design=args.model_dump(),
                                       design_path=args.gds_path, design_accepted=True,
                                       design_binding=binding, device_design=None)
    return ToolResult(status="ok", summary="design accepted by the code check",
                      data={"shapes": len(args.shapes), "gds_path": args.gds_path,
                            "validation": "legacy_rectangles", "klayout_verified": False})


class CreateDeviceDesignArgs(BaseModel):
    placements: list[FlakePlacement] = Field(min_length=2, max_length=2,
        description="Rigid placements of the complete selected hBN and Gr outlines: translation in target bottom-left chip axes (mm), rotation in degrees. No resizing or cutting.")
    contact_overlap_um: float = Field(gt=0, allow_inf_nan=False,
        description="Legacy eight-contact targets require this value squared in um2. Imported saved-GDS targets always require each complete fixed contact end, and this argument cannot reduce that requirement. Geometric criterion only.")


@qumus_tool("create_device_design", "Create a real editable KLayout GDS for the eight-contact wafer and selected hBN/Gr. Read endpoint_pose_window from get_design_context first. Complete-source chip fit, whole-Gr hBN coverage and target-specific contact coverage must hold throughout that config-derived endpoint pose window, not only nominally. This geometric screen is not an optical or physical positioning guarantee. Imported saved-GDS targets require complete original contact ends inside displacement-eroded Gr; the overlap argument cannot waive this. All original metal is preserved. Requires registered device_design_target geometry and current complete observed outlines. Saves and reopens the GDS before accepting an immutable device_design_v1. Uses the operator-selected KLayout backend: KlayoutClaw MCP (default; atomic new-view isolation and a native viewport) or the headless klayout Python module (QUMUS_KLAYOUT_BACKEND=python; same generated save/reopen checks, rendered preview). An unavailable backend blocks acceptance; there is no automatic switch.",
            agents={"designer"}, kind="state", requires=("flakes_selected",))
async def create_device_design(args: CreateDeviceDesignArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.agents.klayout import KLayoutError, create_design_artifact
    from qumus2.records.design_placement import design_margins, margins_summary

    if not ctx.project_id:
        return ToolResult(status="error", summary="An active project is required for a device design.")
    try:
        previous = _state(ctx).get("device_design") or {}
        revisions = [int(r.get("revision", 0)) for r in _state(ctx).get("device_design_history", [])]
        spec = build_design_spec(ctx.store, ctx.project_id, args.placements,
            args.contact_overlap_um, design_id="design_" + uuid4().hex,
            revision=max([int(previous.get("revision", 0)), *revisions]) + 1)
        screening = require_design_pose_window(spec)
        artifact = await asyncio.to_thread(create_design_artifact, ctx.store, spec)
        design = bind_artifact(spec, artifact)
        # Recheck facts after external work and commit only the same snapshot.
        with ctx.store._transaction():
            current = build_design_spec(ctx.store, ctx.project_id, args.placements,
                args.contact_overlap_um, design_id=spec.design_id, revision=spec.revision)
            if current != spec or (_state(ctx).get("device_design") or {}) != previous:
                raise ValueError("Design sources or accepted revision changed during KLayout export; reassess before retrying.")
            admission = build_design_pose_window_admission(design)
            if admission.report != screening:
                raise ValueError("Pose-window screening configuration changed during KLayout export; reassess before retrying.")
            state = _state(ctx)
            ctx.store.update_project_state(ctx.project_id,
                device_design=design.model_dump(mode="json"),
                device_design_history=[*state.get("device_design_history", []), design.model_dump(mode="json")],
                design=design.model_dump(mode="json"), design_path=artifact.path,
                design_accepted=True,
                # Old stacking guards cannot treat this new binding as legacy guidance.
                design_binding={"version": design.version, "canonical_digest": design.canonical_digest})
            validate_design_current(ctx.store, ctx.project_id)
        try:  # informative only: the design is already admitted and committed
            margins = design_margins(spec)
            stated = f"Margins: {margins_summary(margins)} "
        except (ValueError, KeyError, TypeError) as exc:
            margins, stated = None, f"Margins not stated ({exc}). "
        return ToolResult(status="ok", summary="Eight-contact hBN/Gr design accepted after actual KLayout GDS save and reopen. "
                          f"{stated}Stacking must validate and consume this design revision; electrical performance is unverified.",
            data={"device_design": model_device_design_view(design.model_dump(mode="json")), "klayout_verified": True,
                  "margins_um": margins,
                  "transport": artifact.backend, "pose_window_admission": admission.model_dump(mode="json"),
                  "native_viewport_scope": "Native KLayout visual evidence, not human approval or electrical certification" if artifact.backend == "klayout_mcp"
                      else "Rendered preview of the reopened GDS, not a native KLayout view" if artifact.backend == "klayout_python" else None},
            images=[str(Path(artifact.path).with_name("native.png"))] if artifact.backend == "klayout_mcp"
                   else [str(Path(artifact.path).with_name("preview.png"))] if artifact.backend == "klayout_python" else [])
    except DesignPoseWindowError as exc:
        return ToolResult(status="error", summary=f"Device design not accepted: {exc}",
            data={"klayout_verified": False, "pose_window_screen": exc.report.model_dump(mode="json"),
                  "next_action": "The complete observed sources and target-specific eight-contact coverage must remain valid throughout the existing endpoint pose window; no positioning or optical guarantee is implied."})
    except (ValueError, OSError, KLayoutError) as exc:
        return ToolResult(status="error", summary=f"Device design not accepted: {exc}",
            data={"klayout_verified": False, "next_action": "Check registered target/source evidence, transforms and the KlayoutClaw atomic-new-layout-v1 capability on localhost8767; no arbitrary GDS path or batch fallback is accepted."})


class RelaxationArgs(BaseModel):
    role: str = Field(description="Flake role of the device plan, e.g. bottom_hbn")
    thickness_labels: list[str] = Field(default_factory=list,
                                        description="The looser set of thickness labels "
                                        "proposed (ledger words, e.g. 10-20, 20-30)")
    min_area_um2: float = Field(0, ge=0, description="The proposed minimum area")
    acceptable: bool = Field(description="Does the device still work and fit with this?")
    reason: str = Field(min_length=1, description="Physics and layout reasons")


@qumus_tool("assess_relaxation",
            "Judge whether a looser requirement for one flake role still gives a working "
            "device that fits the layout. Only your yes lets the advisor resubmit the plan "
            "with it; a no means more material must be exfoliated and searched.",
            agents={"designer"}, kind="state")
async def assess_relaxation(args: RelaxationArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.core.materials import normalize_thickness_label

    st = _state(ctx)
    plan = st.get("device_plan") or {}
    role = next((r for r in plan.get("flake_roles", []) if r["role"] == args.role), None)
    if role is None:
        return ToolResult(status="error", summary=f"{args.role!r} is not a flake role of "
                          "the accepted device plan")
    labels = [normalize_thickness_label(role["material"], x) for x in args.thickness_labels]
    if None in labels:
        return ToolResult(status="error", summary=f"unknown thickness label in "
                          f"{args.thickness_labels}")
    rec = {**args.model_dump(), "thickness_labels": labels, "material": role["material"],
           "by": ctx.agent, "required_footprint": role.get("required_footprint"),
           "was": {"thickness_labels": role.get("thickness_labels"),
                   "min_area_um2": role.get("min_area_um2")}}
    if ctx.project_id:
        ctx.store.update_project_state(ctx.project_id,
                                       relaxations=[*st.get("relaxations", []), rec])
    verdict = "acceptable" if args.acceptable else "NOT acceptable"
    ctx.store.emit("notification", {"level": "info" if args.acceptable else "warning",
                   "text": f"Designer: looser {args.role} ({labels or 'any thickness'}, "
                   f">= {args.min_area_um2} um2) is {verdict}: {args.reason}"}, agent=ctx.agent)
    nxt = ("the advisor may resubmit the plan with exactly this" if args.acceptable else
           "keep the requirement; more material must be exfoliated and searched")
    return ToolResult(status="ok", summary=f"{args.role}: looser requirement {verdict}; {nxt}",
                      data=rec)


@qumus_tool("report_design_infeasible",
            "The selected flakes cannot hold the device after bounded placement attempts: "
            "hand the combination back for a new selection.",
            agents={"designer"}, kind="state", refs={"flake_ids": "flake"},
            requires=("flakes_selected",))
async def report_design_infeasible(args: InfeasibleArgs, ctx: ToolContext) -> ToolResult:
    failure = {"layout_impossible": True, **args.model_dump()}
    if ctx.project_id:
        try:
            measured = plan_selected_placement(ctx.store, ctx.project_id)
        except (ValueError, KeyError, OSError):
            measured = None
        if measured is not None:
            if measured["status"] == "fit":
                return ToolResult(status="error", summary="The selected pair fits: " + measured["summary"]
                    + " Use suggested_placement from get_design_context with create_device_design.",
                    data={"suggested_placement": measured})
            failure["measured"] = {key: measured[key] for key in ("limiting", "shortfall_um", "gr_headroom_um",
                "hbn_headroom_um", "gr_required_clearance_um", "hbn_required_clearance_um", "summary")}
    if ctx.project_id:
        ctx.store.update_project_state(ctx.project_id, design_fit_failure=failure,
                                       selected_flakes=[], design_accepted=False)
    return ToolResult(status="ok", summary="combination handed back for a new selection",
                      data=failure)
