"""Advisor's tools (v1 project_manager_agent): run records, lab setup, device plan.

Queries are code (qumus2/records/queries.py), never AI-written SQL. The device
plan replaces v1's DEVICE_GUIDANCE_REQUEST and is checked by the port of v1
`validate_device_guidance_for_layout` before anyone uses it.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.core.materials import (
    THICKNESS_LABELS,
    labels_for_layers,
    labels_for_nm,
    labels_from_text,
    normalize_material_code,
    normalize_thickness_label,
)
from qumus2.records import queries
from qumus2.records.budget import exploration_budget
from qumus2.records.device_design import (
    ImportedHallBarTarget,
    parse_hallbar_target,
    target_import_binding,
    validate_imported_target_files,
)
from qumus2.records.geometry import (
    BridgeRequirement,
    ElectrodeGeometry,
    bridge_footprint,
    direct_contact_intent,
    target_geometry,
    target_snapshot,
)


class RecordsArgs(BaseModel):
    flow: Literal["exfoliate_chips", "search_chip", "place_chip_on_transfer_stage",
                  "stack_device"] = "exfoliate_chips"
    material: str | None = None


class NoArgs(BaseModel):
    pass


class Layer(BaseModel):
    role: str = Field(description="e.g. top_hbn, channel, bottom_hbn, graphite_gate, "
                      "bottom_electrode")
    material: str
    flake: bool = Field(True, description="False for layers not made from an exfoliated "
                        "flake (metal contacts, prefabricated electrodes)")
    thickness: str = Field("", description="Free text, e.g. '10-40 nm' or '1-3 layers'; "
                           "read when no labels or range are given")
    thickness_labels: list[str] = Field(default_factory=list, description="Ledger thickness "
                                        "labels that qualify: graphene monolayer, bilayer, "
                                        "trilayer, fewlayer, manylayer; hBN 0-10, 10-20, 20-30, "
                                        "30-40, >40 (nm). ['any'] = no thickness filter")
    thickness_label: str | None = Field(None, description="One label (same as a one-entry "
                                        "thickness_labels)")
    thickness_nm: list[float] | None = Field(None, description="[min, max] nm; mapped to the "
                                             "ledger bins it overlaps", max_length=2)
    thickness_layers: list[float] | None = Field(None, description="[min, max] layers "
                                                 "(graphene); mapped to the ledger labels",
                                                 max_length=2)
    min_area_um2: float = Field(0, ge=0)
    notes: str = ""


class DevicePlanArgs(BaseModel):
    device_name: str = Field(min_length=1)
    layer_stack: list[Layer] = Field(description="Physical stack, top to bottom")
    contact_scheme: str = ""
    gate_scheme: str = ""
    channel: str = Field("", description="Channel geometry, e.g. Hall bar 10x20 um "
                         "preferred, 5x10 um minimum")
    workflow_plan: list[str] = Field(default_factory=list)
    evidence: str = Field(min_length=1, description="Run records / literature behind it")
    precedent_run_ids: list[str] = Field(default_factory=list,
                                         description="Succeeded runs this plan follows")
    target_chip_id: str | None = Field(None, description="Known physical target for final Device Guidance. "
        "Required on first submission; omission on a revision preserves the accepted target. "
        "For prepatterned targets read get_target_geometry before final requirements.")
    bridge_requirement: BridgeRequirement | None = Field(None, description="For a prepatterned "
        "straight contact bridge: overlap, channel width, placement allowance and rationale.")
    target_contact_region_id: str | None = Field(None, description="Explicit existing prepatterned contact region from get_target_geometry.")
    direct_contact_material: Literal["Gr"] | None = Field(None, description="Explicit sole Gr directly contacting existing electrodes using PC; no intervening transferred layer.")


class TargetArgs(BaseModel):
    chip_id: str


class RegisterGeometryArgs(TargetArgs):
    instruction_id: str = Field(description="Person's message containing either JSON device_design_target (typed Hall-bar target plus chip_id: eight named polygons, chip-bottom-left mm axes, size_mm, source/reference and empty receiving_layers_top_first) or legacy target_geometry: "
        "chip_id, source, reference, units, contact_gap_um, contact_span_um, contact_depth_um, "
        "and um_per_pixel for calibrated_image. Dimensions are copied from that message, never inferred.")


@qumus_tool("register_target_geometry", "Register explicit target design/calibration data from a person's "
            "JSON message {device_design_target: {...}} for a typed eight-contact target (imported targets must already be constructed from the operator bundle), "
            "or {target_geometry: {...}} for legacy two-contact geometry. Cite its instruction_id; do not invent dimensions. "
            "Existing guidance/layout must be revalidated after a change.",
            agents={"orchestrator"}, kind="state", refs={"chip_id": "chip"})
async def register_target_geometry(args: RegisterGeometryArgs, ctx: ToolContext) -> ToolResult:
    ins = ctx.store.get_instruction(args.instruction_id)
    if (not ins or ins["source"] != "ui" or ins["kind"] not in ("message", "plan_change")
            or ins["project_id"] not in (None, ctx.project_id)):
        return ToolResult(status="error", summary="Cite a person's geometry message in this project.")
    try:
        message = json.loads(ins["text"])
        eight = "device_design_target" in message
        raw = dict(message["device_design_target"] if eight else message["target_geometry"])
        if raw.pop("chip_id") != args.chip_id:
            raise ValueError("the message names a different target")
        geometry = parse_hallbar_target(raw) if eight else ElectrodeGeometry.model_validate(raw)
        chip = ctx.store.get_sample(args.chip_id) or {}
        existing = chip.get("props", {}).get("device_design_target")
        if existing is not None and isinstance(parse_hallbar_target(existing), ImportedHallBarTarget):
            if not eight or geometry != _current_hallbar_target(ctx, chip):
                raise ValueError("Imported target registration may only reaffirm the exact constructed target")
        elif isinstance(geometry, ImportedHallBarTarget):
            raise ValueError("Load an imported target through the operator workspace configuration before bench construction")
        if geometry.source == "simulated_design" and ctx.store.bench != "sim":
            raise ValueError("synthetic geometry is not physical lab evidence")
        if not eight and geometry.source == "calibrated_image" and geometry.um_per_pixel is None:
            raise ValueError("calibrated imagery requires um_per_pixel")
        if eight:
            size = ctx.store.get_sample(args.chip_id)["props"].get("size_mm")
            if (not isinstance(size, (list, tuple)) or len(size) != 2
                    or any(type(v) not in (int, float) for v in size) or tuple(size) != geometry.size_mm):
                raise ValueError("eight-contact geometry differs from the current declared chip size")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return ToolResult(status="error", summary="Invalid explicit target geometry: " + str(exc)[:300])
    ctx.store.upsert_sample(args.chip_id, "chip", props={
        ("device_design_target" if eight else "electrode_geometry"): geometry.model_dump(mode="json"),
        "electrode_geometry_instruction": args.instruction_id})
    if ctx.project_id:
        plan = (ctx.store.get_project(ctx.project_id) or {}).get("state", {}).get("device_plan") or {}
        if plan.get("target_chip_id") == args.chip_id:
            ctx.store.update_project_state(ctx.project_id, selected_flakes=[], selected_by_role={},
                                           design_accepted=False, design_binding=None)
    return ToolResult(status="ok", summary="Target geometry recorded from the person's supplied data; "
                      "revalidate Device Guidance before selection or stacking.",
                      data={"geometry": {"status": "resolved", **geometry.model_dump(mode="json")}})


def _current_hallbar_target(ctx: ToolContext, chip: dict[str, Any]):
    """Imported geometry is the operator-constructed target, not a ledger overlay."""
    target = parse_hallbar_target(chip.get("props", {}).get("device_design_target"))
    if isinstance(target, ImportedHallBarTarget):
        if chip["props"].get("imported_target_binding") != target_import_binding(target):
            raise ValueError("Imported target lacks its matching bench-construction binding")
        validate_imported_target_files(ctx.store, target)
    return target


@qumus_tool("get_target_geometry", "Read a target chip's electrode design or calibrated "
            "dimensions before setting flake area/shape requirements. Missing geometry stays "
            "unresolved. Saved images are context, not automatically measured dimensions.",
            agents={"advisor", "designer", "orchestrator"}, kind="query", refs={"chip_id": "chip"})
async def get_target_geometry(args: TargetArgs, ctx: ToolContext) -> ToolResult:
    chip = ctx.store.get_sample(args.chip_id) or {}
    try:
        eight = chip.get("props", {}).get("device_design_target")
        geometry = {"status": "resolved", **_current_hallbar_target(ctx, chip).model_dump(mode="json")} if eight else target_geometry(ctx.store, args.chip_id)
    except (ValueError, OSError) as exc:
        geometry = {"status": "unresolved", "reason": str(exc)}
    photos = chip.get("props", {}).get("images", {})
    return ToolResult(status="ok" if geometry["status"] == "resolved" else "needs_attention",
                      summary=f"Electrode geometry for {args.chip_id}: {geometry['status']}",
                      data=geometry, images=list(photos.values())[:2] if isinstance(photos, dict) else photos[:2])


def validate_device_plan(plan: DevicePlanArgs) -> dict[str, Any]:
    """Port of v1 design_workflows.validate_device_guidance_for_layout: is the plan
    specific enough to design and stack? Checks semantics, not recipes."""
    missing: list[str] = []
    warnings: list[str] = []
    text = json.dumps(plan.model_dump(), ensure_ascii=False).lower()
    layers = plan.layer_stack
    if len(layers) < 2:
        missing.append("layer_stack with at least 2 layers")
    weak = [i for i, layer in enumerate(layers, 1)
            if not layer.role.strip() or not layer.material.strip()]
    if weak:
        missing.append(f"role/material for layer_stack entries {weak}")
    layer_text = " ".join(f"{la.role} {la.material}".lower() for la in layers)
    transistor_like = any(w in text for w in ("transistor", "fet", "source", "drain"))
    electrode_like = any(w in text for w in ("contact", "electrode", "source", "drain", "metal"))
    gate_like = "gate" in " ".join((plan.device_name, plan.gate_scheme, plan.channel, layer_text)).lower()
    has_contact_layers = any(w in layer_text for w in ("contact", "electrode", "source",
                                                       "drain", "metal"))
    has_gate_layers = "gate" in layer_text
    if (transistor_like or electrode_like) and not (plan.contact_scheme or has_contact_layers):
        missing.append("contact_scheme or contact/electrode layer roles")
    if (transistor_like or gate_like) and not (plan.gate_scheme or has_gate_layers):
        missing.append("gate_scheme or gate layer roles")
    if not any(la.flake for la in layers):
        missing.append("at least one layer made from a flake")
    if plan.direct_contact_material or plan.target_contact_region_id:
        if plan.direct_contact_material != "Gr" or not plan.target_contact_region_id:
            missing.append("direct_contact_material Gr and a named target_contact_region_id")
        transferred = [la for la in layers if la.flake]
        if len(transferred) != 1 or normalize_material_code(transferred[0].material) != "Gr":
            missing.append("sole transferred Gr directly facing the existing contacts; no hBN or unknown layer")
        if (len(layers) != 2 or not layers[0].flake or layers[-1].flake
                or not any(w in layers[-1].role.lower() for w in ("contact", "electrode"))):
            missing.append("existing nonflake receiving contacts below the Gr")
    if not plan.channel:
        warnings.append("no channel geometry given; the designer will use skill defaults")
    return {"complete": not missing, "missing": missing, "warnings": warnings}


def flake_roles(plan: DevicePlanArgs) -> list[dict[str, Any]]:
    """Flake roles with counts: the same role twice in the stack needs two flakes."""
    out: dict[str, dict[str, Any]] = {}
    for la in plan.layer_stack:
        if not la.flake:
            continue
        r = out.setdefault(la.role, {"role": la.role,
                                     "material": normalize_material_code(la.material),
                                     "count": 0, "thickness": la.thickness,
                                     "thickness_labels": la.thickness_labels,
                                     "min_area_um2": la.min_area_um2})
        r["count"] += 1
    return list(out.values())


@qumus_tool("query_run_records",
            "Audit query: per-parameter run aggregates and flake yield. Ordinary Advisor "
            "planning uses the bounded get_experience summary instead.",
            agents={"orchestrator"}, kind="query")
async def query_run_records(args: RecordsArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.agents.tools.experience import mark_checked

    mark_checked(ctx, args.material)
    stats = queries.recipe_stats(ctx.store, args.flow, args.material)
    bench = ctx.store.bench
    data = {"flow": args.flow, "material": normalize_material_code(args.material),
            "bench": bench, "recipes": stats,
            "flake_yield": queries.flake_yield(ctx.store, args.material)}
    n = sum(s["runs"] for s in stats)
    where = " on the testbench (simulated, not real-lab rates)" if bench == "sim" else ""
    return ToolResult(status="ok", summary=f"{n} finished run(s){where}, {len(stats)} "
                      "parameter set(s)" + ("" if n else ": no precedent, any recipe is NEW"),
                      data=data)


@qumus_tool("get_lab_setup", "Registered experimental capabilities and prerequisites, not "
            "inventory or current instrument readiness. Use existing inventory observations "
            "or list_samples for materials.", agents={"advisor", "orchestrator", "reactor", "hands"},
            kind="query")
async def get_lab_setup(args: NoArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.agents.tools.hardware import ExfoliateArgs
    from qumus2.agents.tools.reactor import reactor_setup_details
    from qumus2.records.outcomes import recipe_keys
    from qumus2.records.plan_completion import FLOW_SAMPLE_STATUSES, flow_storage_contracts

    reactor = await reactor_setup_details(ctx)
    bench = ctx.extra.get("bench") or {}
    flows = ctx.jobs.flow_names()  # what the connected stations offer (contract describe)
    return ToolResult(status="ok", summary=f"{len(flows)} registered workflows; "
                      "support does not establish current readiness",
                      data={"flows": flows, "bench": ctx.store.bench,
                            "instruments": {**{name: bench[name] for name in
                                ("microscope", "transfer_stage", "exfoliator", "arm_left", "arm_right", "manipulator") if name in bench},
                                "reactor": reactor.data},
                            "stable_process_knowledge": {"pickup": "Top hBN must fully cover every lifted layer at the intended placement; fixed target coatings are excluded.",
                                "stamp": "Contact support (PC/mica) is a reported stamp property; use matching experience and retain its validation status."},
                            "readiness": "not evaluated by this query",
                            "material_identity_policy": {
                                "simulation": "On bench=sim, list_samples simulation_identity.usable=true permits "
                                    "the saved configured material identity for simulated operations. Do not pause solely "
                                    "for human_review=unreviewed; retain that label and report simulated outcomes.",
                                "physical": "Physical material identity confirmation is required for real hardware. "
                                    "A simulation declaration never provides it.",
                                "unresolved": "Missing, uncertain or conflicting identity is not covered. "
                                    "Readiness, actual observations, flow preconditions and SafetyGate remain required."},
                            "fixed_flow_sample_statuses": FLOW_SAMPLE_STATUSES,
                            "flow_storage_contracts": flow_storage_contracts(),
                            "exfoliation_recipe_defaults": {
                                "values": {key: ExfoliateArgs.model_fields[key].default
                                           for key in recipe_keys("exfoliate_chips")
                                           if key in ExfoliateArgs.model_fields and not ExfoliateArgs.model_fields[key].is_required()},
                                "source": "registered_tool_defaults", "validated": False,
                                "usage": "On bench=sim with no enforced confirmed baseline, these values may fill "
                                    "fields missing from a historical trial recipe. Preserve its specified fields, "
                                    "label added values as unvalidated simulation defaults, and return a complete recipe. "
                                    "Do not leave documented defaults unresolved or claim they came from experience. "
                                    "This permission does not apply to real hardware or override explicit user parameters.",
                                "note": "Software implementation defaults; an uncalibrated simulation starting point, "
                                        "not confirmed experience or evidence of a safe physical-lab recipe."},
                            "prerequisites": {"exfoliate_chips": "identified material, bare chips, Advisor recipe",
                                              "search_chip": "known chip and material",
                                              "revisit_chip": "stored candidates on a known chip",
                                              "stack_device": "accepted selection and Layout, target chip, usable stamp"},
                            "external_dependencies": ["lithography and metal deposition",
                                                      "physical material identity confirmation (real hardware only; "
                                                      "not an additional review for a usable configured simulation identity)"],
                            "instrument_state_tool": "get_instrument_status",
                            "inventory_tool": "list_samples"})


ANY_THICKNESS = "any"


def normalize_labels(plan: DevicePlanArgs) -> list[str]:
    """Thickness labels in the ledger's words, in place; returns the problems.

    A real run wrote `thickness_nm: [10, 30]` and `thickness_label: "monolayer"`
    (fields the model did not have) and the plan was accepted with no thickness
    target at all, so the search filtered nothing. Now every way of saying the
    thickness is read (labels, one label, an nm or layer range, the free text),
    and a flake role of a material with a vocabulary must end up with labels,
    or say ['any'] on purpose."""
    errs = []
    for la in plan.layer_stack:
        vocab = THICKNESS_LABELS.get(normalize_material_code(la.material) or "")
        given = [*la.thickness_labels, *([la.thickness_label] if la.thickness_label else [])]
        if any(str(x).strip().lower() == ANY_THICKNESS for x in given):
            la.thickness_labels, la.thickness_label = [], None
            continue
        out: list[str] = []
        for lab in given:
            n = normalize_thickness_label(la.material, lab)
            if n is None:
                errs.append(f"{la.role}: thickness label {lab!r} is not one the search "
                            f"writes; use {vocab}")
            elif n not in out:
                out.append(n)
        if not out and la.thickness_nm:
            lo, hi = la.thickness_nm[0], la.thickness_nm[-1]
            out = labels_for_nm(la.material, lo, hi)
        if not out and la.thickness_layers:
            lo, hi = la.thickness_layers[0], la.thickness_layers[-1]
            out = labels_for_layers(la.material, lo, hi)
        if not out and la.thickness:
            out = labels_from_text(la.material, la.thickness)
        if not out and la.flake and vocab and not errs:
            how = {k: v for k, v in (("thickness_nm", la.thickness_nm),
                                     ("thickness_layers", la.thickness_layers),
                                     ("thickness", la.thickness)) if v}
            errs.append(f"{la.role} ({la.material}): no thickness target the search can use"
                        + (f" (could not map {how})" if how else "")
                        + f"; give thickness_labels from {vocab}, or thickness_nm [min, max]"
                        + (", or thickness_layers [min, max]"
                           if normalize_material_code(la.material) == "Gr" else "")
                        + f", or ['{ANY_THICKNESS}'] for no thickness filter")
        la.thickness_labels, la.thickness_label = out, None
    return errs


def relaxed_roles(old: dict[str, Any] | None, new: list[dict[str, Any]]) -> dict[str, str]:
    """Flake roles the new plan asks less of than the accepted one: role -> what."""
    if not old:
        return {}
    before = {r["role"]: r for r in old.get("flake_roles", [])}
    after = {r["role"]: r for r in new}
    out = {}
    for role, o in before.items():
        n = after.get(role)
        if n is None:
            out[role] = "role dropped"
            continue
        what = []
        if normalize_material_code(n["material"]) != normalize_material_code(o["material"]):
            what.append(f"material {o['material']} -> {n['material']}")
        ol, nl = set(o.get("thickness_labels") or []), set(n.get("thickness_labels") or [])
        if ol and not nl <= ol:
            what.append(f"thickness {sorted(ol)} -> {sorted(nl) or 'any'}")
        if n.get("min_area_um2", 0) < o.get("min_area_um2", 0):
            what.append(f"min area {o['min_area_um2']} -> {n.get('min_area_um2', 0)} um2")
        if n["count"] < o["count"]:
            what.append(f"count {o['count']} -> {n['count']}")
        footprint = o.get("required_footprint")
        replacement = n.get("required_footprint") or {}
        if footprint and any(replacement.get(k, 0) < footprint[k] for k in ("width_um", "height_um")):
            what.append("continuous footprint reduced or removed")
        if what:
            out[role] = "; ".join(what)
    return out


def approved(relaxations: list[dict[str, Any]], role: dict[str, Any] | None, *,
             footprint_relaxed: bool = False) -> bool:
    """Did the designer's latest verdict on this role accept a requirement at
    least as strict as `role`?"""
    if role is None:
        return False
    for r in reversed(relaxations):
        if r["role"] != role["role"]:
            continue
        if not r["acceptable"] or (normalize_material_code(r.get("material"))
                                   != normalize_material_code(role["material"])):
            return False
        ok, mine = set(r.get("thickness_labels") or []), set(role.get("thickness_labels") or [])
        footprint = role.get("required_footprint") or {}
        reviewed = r.get("required_footprint") or {}
        if footprint_relaxed and not reviewed:
            return False
        return (not ok or bool(mine) and mine <= ok) and (
            role.get("min_area_um2", 0) >= r.get("min_area_um2", 0)) and all(
                footprint.get(k, 0) >= reviewed[k] for k in ("width_um", "height_um") if k in reviewed)
    return False


@qumus_tool("get_device_plan",
            "The Device Guidance accepted so far (or none), with any requirement changes the "
            "designer assessed. Read it before resubmitting: keep what you are not asked "
            "to change.", agents={"advisor", "designer", "orchestrator"}, kind="query")
async def get_device_plan(args: NoArgs, ctx: ToolContext) -> ToolResult:
    st = (ctx.store.get_project(ctx.project_id) or {}).get("state", {}) if ctx.project_id else {}
    plan = st.get("device_plan")
    return ToolResult(status="ok", summary="no Device Guidance yet" if not plan else
                      f"Device Guidance {plan['device_name']!r}",
                      data={"device_plan": plan, "relaxations": st.get("relaxations", [])})


@qumus_tool("submit_device_plan",
            "Submit Advisor Device Guidance (layer stack, contacts, gate, channel) the team will "
            "build. Code checks it is complete; it then sets which flakes the picker must "
            "find. A plan with no succeeded precedent runs is marked NEW for the user. "
            "Every flake role needs a thickness target in the ledger's words: thickness_labels "
            "(graphene monolayer/bilayer/trilayer/fewlayer/manylayer; hBN 0-10/10-20/20-30/"
            "30-40/>40 nm) or thickness_nm [min, max], which code maps to those bins. "
            'Example for existing prepatterned contacts: {"device_name": "direct graphene bridge", '
            '"layer_stack": [{"material": "Gr", "role": "channel", "thickness_labels": ["monolayer"], '
            '"min_area_um2": 50}, {"material": "existing electrode", "role": "receiving_contacts", "flake": false}], '
            '"contact_scheme": "Gr directly on the designated existing contact pair", "gate_scheme": "", '
            '"target_chip_id": "chip_e01", "target_contact_region_id": "centre_pair", "direct_contact_material": "Gr", '
            '"bridge_requirement": {"contact_overlap_um": 1, "channel_width_um": 2, "placement_margin_um": 0.5, '
            '"rationale": "explicit synthetic test criterion"}, "channel": "straight contact bridge", '
            '"workflow_plan": ["exfoliate and search", "select", "lay out", "stack"], '
            '"evidence": "..."}',
            agents={"advisor"}, kind="state")
async def submit_device_plan(args: DevicePlanArgs, ctx: ToolContext) -> ToolResult:
    st = (ctx.store.get_project(ctx.project_id) or {}).get("state", {}) if ctx.project_id else {}
    old = st.get("device_plan") or {}
    if not args.target_chip_id:
        args.target_chip_id = old.get("target_chip_id")
    if args.bridge_requirement is None and args.target_chip_id == old.get("target_chip_id"):
        if old.get("bridge_requirement"):
            args.bridge_requirement = BridgeRequirement.model_validate(old["bridge_requirement"])
    registered_target = ctx.store.get_sample(args.target_chip_id or "") or {}
    registered_eight = bool(registered_target.get("props", {}).get("device_design_target"))
    if args.target_chip_id == old.get("target_chip_id") and old.get("direct_contact_material") and not registered_eight:
        args.direct_contact_material = args.direct_contact_material or old["direct_contact_material"]
        args.target_contact_region_id = args.target_contact_region_id or old["target_contact_region_id"]
    label_errs = normalize_labels(args)
    if label_errs:
        return ToolResult(status="error", summary="device plan rejected: "
                          + "; ".join(label_errs), data={"problems": label_errs})
    check = validate_device_plan(args)
    if not check["complete"]:
        return ToolResult(status="error", summary="device plan incomplete: "
                          + "; ".join(check["missing"]), data=check)
    chip = ctx.store.get_sample(args.target_chip_id or "") or {}
    eight = chip.get("props", {}).get("device_design_target")
    try:
        if eight:
            hallbar = _current_hallbar_target(ctx, chip)
            size = chip["props"].get("size_mm")
            if (not isinstance(size, (list, tuple)) or len(size) != 2
                    or any(type(v) not in (int, float) for v in size) or tuple(size) != hallbar.size_mm):
                raise ValueError("eight-contact dimensions differ from the current chip")
            if [normalize_material_code(layer.material) for layer in args.layer_stack if layer.flake] != ["hBN", "Gr"]:
                raise ValueError("eight-contact task requires exactly top hBN then Gr; no bottom hBN")
            if args.direct_contact_material or args.target_contact_region_id:
                raise ValueError("eight-contact hBN/Gr uses device_design_v1, not the legacy sole-Gr contact contract")
            target = {"chip_id": args.target_chip_id, "prepatterned": True,
                      "geometry": {"status": "resolved", **hallbar.model_dump(mode="json")},
                      "size_mm": list(hallbar.size_mm)}
        else:
            target = target_snapshot(ctx.store, args.target_chip_id or "")
    except (ValueError, OSError) as exc:
        return ToolResult(status="error", summary=str(exc), data={"requirements_status": "unresolved"})
    footprint, contact_intent = None, None
    if args.direct_contact_material:
        try:
            contact_intent = direct_contact_intent(ctx.store, args.target_chip_id,
                args.target_contact_region_id, args.bridge_requirement)
        except (ValueError, TypeError) as exc:
            return ToolResult(status="error", summary=str(exc), data={"requirements_status": "unresolved"})
    if eight:
        xs = [x for contact in hallbar.contacts for x, _ in contact.points_mm]
        ys = [y for contact in hallbar.contacts for _, y in contact.points_mm]
        width, height = (max(xs)-min(xs))*1000, (max(ys)-min(ys))*1000
        footprint = {"width_um": width, "height_um": height, "min_area_um2": 0,
            "screening": "device_design_v1_polygons",
            "source": hallbar.source, "reference": hallbar.reference,
            "target_chip_id": args.target_chip_id, "fit_rule": "Eight-contact bounding box is context, not an inscribed-rectangle or minimum-area requirement. Designer checks complete transformed polygons, all eight contact overlaps and whole-Gr hBN coverage."}
    elif target["prepatterned"]:
        geometry = target["geometry"]
        if args.bridge_requirement is None:
            return ToolResult(status="error", summary="Device Guidance needs target electrode "
                              "geometry and a derived bridge_requirement before final flake sizes.",
                              data={"geometry": geometry, "requirements_status": "unresolved"})
        try:
            footprint = bridge_footprint(geometry, args.bridge_requirement)
        except ValueError as exc:
            return ToolResult(status="error", summary=str(exc), data={"geometry": geometry})
        for layer in args.layer_stack:
            if layer.flake:
                layer.min_area_um2 = max(layer.min_area_um2, footprint["min_area_um2"])
    bad = [r for r in args.precedent_run_ids
           if (ctx.store.get_run(r) or {}).get("status") != "succeeded"]
    if bad:
        return ToolResult(status="error", summary=f"precedent runs {bad} do not exist or did "
                          "not succeed", data=check)
    roles = flake_roles(args)
    if footprint:
        for role in roles:
            role["required_footprint"] = footprint
    relaxed = relaxed_roles(st.get("device_plan"), roles)
    by_role = {r["role"]: r for r in roles}
    unapproved = {k: v for k, v in relaxed.items()
                  if not approved(st.get("relaxations", []), by_role.get(k),
                                  footprint_relaxed="continuous footprint" in v)}
    if unapproved:
        return ToolResult(status="error", summary=(
            "this plan asks less than the accepted one ("
            + "; ".join(f"{k}: {v}" for k, v in unapproved.items())
            + "). Only the designer can accept a looser requirement (assess_relaxation). "
            "If the designer has not, keep the accepted requirement and get more material "
            "exfoliated and searched instead. Read the accepted plan with get_device_plan."),
            data={"relaxed": unapproved})
    new = not args.precedent_run_ids
    plan = {**args.model_dump(), "target_snapshot": target, "flake_roles": roles, "is_new_recipe": new,
            **({"design_contract": "device_design_v1"} if eight else {}),
            **({"direct_contact_intent": contact_intent} if contact_intent else {}),
            "warnings": check["warnings"], "required_footprint": footprint}
    for role, what in relaxed.items():
        ctx.store.emit("notification", {"level": "warning", "text": f"Requirement relaxed "
                       f"for {role}: {what} (accepted by the designer)"}, agent=ctx.agent)
    if ctx.project_id:
        ctx.store.update_project_state(ctx.project_id, device_plan=plan, selected_flakes=[],
                                       selected_by_role={}, design_accepted=False, design_binding=None,
                                       device_design=None)
    if new:
        ctx.store.emit("notification", {"level": "info", "text": f"NEW recipe: "
                       f"{args.device_name} has no precedent in the run records"},
                       agent=ctx.agent)
    return ToolResult(status="ok", summary=f"Device Guidance accepted ({len(plan['flake_roles'])} "
                      f"flake role(s){', NEW recipe' if new else ''})",
                      data={"flake_roles": plan["flake_roles"], "warnings": check["warnings"],
                            "is_new_recipe": new,
                            "exploration_allowance": {material: exploration_budget(
                                ctx.store, ctx.project_id, material)
                                for material in sorted({r["material"] for r in roles})}})
