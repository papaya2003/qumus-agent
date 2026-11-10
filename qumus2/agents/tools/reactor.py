"""Reactor-specific job tools and code-checked synthetic growth results."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import ConfigDict, Field

from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.agents.tools.hardware import submit
from qumus2.agents.tools.picking import Assessment, optical_selection_errors
from qumus2.agents.tools.project import NoArgs
from qumus2.agents.tools.reactor_params import (
    ChipParams,
    GrowthParams,
    ObserveParams,
    PositionParams,
    ShutdownParams,
)
from qumus2.agents.tools.recovery import RecoverArgs, retry_job, reverify_job
from qumus2.records.reactor_pairs import (
    _record_pair_product,
    prepared_pair,
    validate_pair_observation,
)


class ReactorGoalArgs(ChipParams):
    target_coverage: float = Field(0.75, gt=0, le=1)
    target_uniformity: float = Field(0.8, ge=0, le=1)
    max_defect_fraction: float = Field(0.2, ge=0, le=1)


class ReactorResultArgs(ChipParams):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    growth_job_id: str


class PairGoalArgs(ChipParams):
    target_reaction_fraction: float = Field(1, gt=0, le=1)


class PairResultArgs(ChipParams):
    reaction_job_id: str


class PrepareReactorPairArgs(ChipParams):
    chip_id: str = Field(description="Empty narrow reactor carrier ID from list_samples(kind=chip), "
        "such as reactor_chip_01; not an ordinary source chip such as chip_001")
    flake_ids: list[str] = Field(min_length=2, max_length=2,
        description="Two searched/catalogued flake IDs in pickup order: top, then bottom")


class AssessReactorInputsArgs(ChipParams):
    # The carrier ID connects this preparatory inspection to its explicit task
    # disposition; it is not moved by the source inspection.
    chip_id: str = Field(description="Dedicated narrow reactor carrier ID for this preparation task "
        "(for example reactor_chip_01), NOT the ordinary source chip. The carrier stays in reactor "
        "storage during assessment; source chips are resolved from the flake IDs.")
    flake_ids: list[str] = Field(min_length=1, max_length=20,
        description="1..20 catalogued/selected flake IDs with saved microscope images from "
        "get_reactor_pair_candidates.candidates where eligible_for_assessment is true. "
        "Raw candidate IDs without catalogue photographs are not accepted.")


@qumus_tool("get_reactor_pair_candidates", "List actual catalogued source flakes, saved-image "
            "availability and optical pickup-isolation evidence. Reuse adequate saved search "
            "evidence and request targeted observations only for named gaps; confirm a compatible clear pair.",
            agents={"orchestrator", "picker", "hands", "advisor"}, kind="query")
async def get_reactor_pair_candidates(args: NoArgs, ctx: ToolContext) -> ToolResult:
    rows, unavailable = [], []
    counts = {name: 0 for name in ("catalogued", "selected", "raw_candidates", "clear",
                                   "unassessed", "rejected", "stale", "unavailable")}
    for flake in ctx.store.list_samples(kind="flake"):
        if flake["status"] not in {"candidate", "catalogued", "selected"}:
            continue
        props = flake["props"]
        counts["raw_candidates" if flake["status"] == "candidate" else flake["status"]] += 1
        row = {"flake_id": flake["id"], "source_chip_id": flake["location"],
               "status": flake["status"], "material": props.get("material"),
               "area_um2": props.get("area_um2"), "has_images": bool(props.get("images")),
               "images": props.get("images") or {}, "optical_geometry": props.get("optical_geometry")}
        if flake["status"] == "candidate" or not row["has_images"]:
            counts["unavailable"] += 1
            unavailable.append({**row, "eligible_for_assessment": False,
                "reason": "not_catalogued" if flake["status"] == "candidate" else "missing_images",
                "next": "Hands must obtain catalogue photographs before Hands can locally check this flake"})
            continue
        from qumus2.records.geometry import current_pickup_evidence

        a = props.get("pickup_assessment") or props.get("reactor_pickup_assessment") or {}
        current = current_pickup_evidence(ctx.store, flake) is not None
        state = "unassessed" if not a else "stale" if not current else "clear" if a.get("clear") else "rejected"
        counts[state] += 1
        rows.append({**row, "eligible_for_assessment": True, "assessment_status": state,
                     "pickup_clear": state == "clear" if current else None, "assessment": a or None})
    priority = {"clear": 0, "unassessed": 1, "stale": 2, "rejected": 3}
    rows.sort(key=lambda r: (priority[r["assessment_status"]], r["flake_id"]))
    if counts["clear"] >= 2:
        action = "Picker confirms the required hBN/material pair after reviewing local evidence; then Hands prepares it"
    elif counts["unassessed"] + counts["stale"] and len(rows) >= 2:
        action = "Picker reviews saved context/detail images; request assess_reactor_inputs only for named evidence gaps"
    elif len(rows) < 2:
        action = ("Obtain both required materials as catalogued photographed flakes before pair selection. "
                  "Have Hands photograph unavailable candidates first; check revisit eligibility if a job returns no images.")
    else:
        action = "Fewer than two assessed clear inputs; obtain additional catalogued candidates and assess their isolation"
    return ToolResult(status="ok", summary=f"{len(rows)} photographed catalogue inputs: {counts['clear']} confirmed clear, "
                      f"{counts['unassessed']} unassessed, {counts['rejected']} rejected, {counts['stale']} stale; "
                      f"{counts['unavailable']} unavailable. Unassessed is not rejected.",
                      data={"counts": counts, "candidates": rows[:20], "omitted": max(0, len(rows) - 20),
                            "unavailable_candidates": unavailable[:20], "unavailable_omitted": max(0, len(unavailable) - 20),
                            "assessment_chip_id": "Dedicated reactor carrier from list_samples(kind=chip); never the source_chip_id",
                            "selection_owner": "Picker screens and confirms; Hands measures locally and stacks",
                            "inspection_batches": {chip: [r["flake_id"] for r in rows if r["source_chip_id"] == chip]
                                for chip in sorted({r["source_chip_id"] for r in rows})},
                            "next": action},
                      images=[path for row in rows[:20] for path in (
                          row["images"].values() if isinstance(row["images"], dict) else row["images"])][:20])


@qumus_tool("assess_reactor_inputs", "Bring catalogued source chips to ordinary optics, measure "
            "the full pickup neighbourhood for each candidate without moving flakes, then return "
            "source chips to storage. chip_id is the dedicated reactor carrier, not the source chip. "
            "Use only photographed catalogue IDs returned as eligible_for_assessment; unassessed is not rejected.",
            agents={"hands"}, kind="hardware", refs={"chip_id": "chip", "flake_ids": "flake"},
            requires=("reactor_ready",))
async def assess_reactor_inputs(args: AssessReactorInputsArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.reactor_requirements import established_requirements

    if established_requirements(ctx.store, ctx.project_id):
        selection = ctx.store.get_project(ctx.project_id)["state"].get("reactor_selection") or {}
        if selection.get("chip_id") != args.chip_id or not set(args.flake_ids) <= set(selection.get("screened_ids", [])):
            return ToolResult(status="error", summary="Picker must screen these candidates before Hands checks their neighbourhoods.")
    return submit(ctx, "assess_reactor_inputs", args.model_dump())


@qumus_tool("prepare_reactor_pair", "Legacy two-reactant fixture only. For hBN/reactive-material/metal/silicon "
            "use prepare_material_metal directly; this legacy tool rejects established material-metal experiments. "
            "Physically route an empty narrow carrier through shared "
            "storage, stack exactly two catalogued flakes with the guarded transfer sequence, "
            "verify their identities and geometry, then return the prepared carrier to reactor storage.",
            agents={"hands"}, kind="hardware", refs={"chip_id": "chip", "flake_ids": "flake"},
            requires=("reactor_ready",))
async def prepare_reactor_pair(args: PrepareReactorPairArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.reactor_requirements import established_requirements, input_errors

    req = established_requirements(ctx.store, ctx.project_id)
    if req:
        selection = ctx.store.get_project(ctx.project_id)["state"].get("reactor_selection") or {}
        errors = input_errors(ctx.store, req, args.flake_ids, carrier_id=args.chip_id, require_assessment=True)
        if selection.get("confirmed_ids") != args.flake_ids or selection.get("chip_id") != args.chip_id:
            errors.append("Picker must confirm this exact top-first pair after Hands local checks.")
        errors.append("Use prepare_material_metal for this established material-metal simulation and its known stamp film. "
                      "prepare_reactor_pair is only the legacy two-reactant fixture; it is not an alternative preparation route.")
        return ToolResult(status="needs_attention", summary="; ".join(errors), data={"preparation_errors": errors})
    return submit(ctx, "prepare_reactor_pair", {"target_chip_id": args.chip_id, "flake_ids": list(args.flake_ids)})


@qumus_tool("hold_reactor_observation", "Verify a loaded carrier and record measured process "
            "conditions for an explicitly declared observation hold. Does not change target settings.",
            agents={"reactor"}, kind="hardware", refs={"chip_id": "chip"}, requires=("reactor_ready",))
async def hold_reactor_observation(args: ChipParams, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "hold_reactor_observation", args.model_dump())


@qumus_tool("verify_reactor_state", "Refresh cold-storage process evidence for a dedicated carrier "
            "already in Reactor Storage, including an empty or prepared carrier. Reads current "
            "temperature, gas flow, targets and stage positions through SafetyGate; no heating "
            "or sample movement. Targets must already be off, residual conditions cold and insertion "
            "retracted. Failed checks use normal safe parking. Wait for its job before claiming cold_storage.",
            agents={"reactor"}, kind="hardware", refs={"chip_id": "chip"}, requires=("reactor_ready",))
async def verify_reactor_state(args: ChipParams, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "verify_reactor_state", args.model_dump())


@qumus_tool("inspect_reactor_product", "Move a cold accepted product carrier from reactor storage "
            "through the shared Arm3/Arm2 storage slot to the ordinary microscope, verify the same "
            "product and save its image, then return the carrier to dedicated storage.", agents={"reactor"},
            kind="hardware", refs={"chip_id": "chip"}, requires=("reactor_ready",))
async def inspect_reactor_product(args: ChipParams, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "inspect_reactor_product", args.model_dump())


@qumus_tool("recover_reactor_load", "Resume a parked cold reactor loading/acquisition job "
            "under its existing reservations and recovery budget. Never repeats reaction exposure.",
            agents={"reactor"}, kind="recovery", requires=("recovery_budget",))
async def recover_reactor_load(args: RecoverArgs, ctx: ToolContext) -> ToolResult:
    run = ctx.store.get_run(args.job_id)
    if not run or run["flow"] != "load_reactor_chip":
        return ToolResult(status="error", summary="Only a parked reactor loading job can use cold-load recovery.")
    chip = ctx.store.get_sample(run["params"].get("chip_id", ""))
    if not chip or chip["props"].get("reactor_state_uncertain"):
        return ToolResult(status="error", summary="Uncertain exposure requires review, not cold-load recovery.")
    return await retry_job(args, ctx)


@qumus_tool("recover_reactor_preparation", "Resume a parked preparation transport, inspection "
            "or other reversible step under its original reservations; a parked verify_reactor_state "
            "only repeats readback verification, never restores heater targets. Interrupted hot release "
            "requires human review; this tool cannot repeat it.", agents={"reactor", "hands"},
            kind="recovery", requires=("recovery_budget",))
async def recover_reactor_preparation(args: RecoverArgs, ctx: ToolContext) -> ToolResult:
    run = ctx.store.get_run(args.job_id)
    if not run or run["flow"] not in {"prepare_reactor_pair", "inspect_reactor_product", "assess_reactor_inputs", "verify_reactor_state"}:
        return ToolResult(status="error", summary="Choose a parked reactor preparation job.")
    if ctx.agent == "reactor" and run["flow"] in {"prepare_reactor_pair", "assess_reactor_inputs"}:
        return ToolResult(status="error", summary="Hands owns preparation and local inspection recovery.")
    if ctx.agent == "hands" and run["flow"] not in {"prepare_reactor_pair", "assess_reactor_inputs"}:
        return ToolResult(status="error", summary="Reactor owns reaction/process recovery.")
    if run["flow"] == "verify_reactor_state":
        return await reverify_job(args, ctx)
    step = next((s for s in ctx.store.get_steps(args.job_id) if s["status"] not in ("done", "skipped")), None)
    if step is None or not step.get("safe_to_resume"):
        return ToolResult(status="error", summary="This preparation step cannot be repeated safely; review its evidence.")
    return await retry_job(args, ctx)


async def reactor_setup_details(ctx: ToolContext) -> ToolResult:
    """Device detail provider used by the common laboratory setup query."""
    bench = ctx.extra.get("bench") or {}
    module = bench.get("reactor_module")
    if not module:
        return ToolResult(status="ok", summary="Experimental reactor is disabled on this bench.",
                          data={"enabled": False})
    profile = module["growth"]
    pair_mode = module.get("experiment_kind") == "two_flake_reaction"
    reaction_profile = module.get("reaction", {}).get("profile", {})
    duration = min(900, max(5, float(reaction_profile.get("incubation_s", 0))
                   + float(reaction_profile.get("exposure_to_completion_s", 60)))) if pair_mode \
        else profile["optimum_time_s"]
    from qumus2.records.reactor_requirements import requirements_record

    material_metal = requirements_record(ctx.store, ctx.project_id) is not None
    mm_profile = module.get("material_metal", {})
    from qumus2.records.material_metal_arguments import OPERATIONS, argument_contract
    from qumus2.records.plan_completion import FLOW_SAMPLE_STATUSES

    return ToolResult(status="ok", summary="Synthetic reactor enabled: dedicated storage, "
                      "XY reactor assembly, separate insertion rod, independent microscope Z "
                      "focus, temperature and gas flow.",
                      data={"enabled": True, "source": module["provenance"],
                            "geometry": module["geometry"],
                            "mechanics_version": module["mechanics_version"],
                            "experiment_kind": "material_metal" if material_metal else module.get("experiment_kind", "legacy_field_growth"),
                            "controller_mapping": module["controller_mapping"],
                            "nominal_recipe": ({k: mm_profile[k] for k in ("temperature_c", "flow_sccm", "duration_s")} if material_metal else
                                {"temperature_c": profile["optimum_temperature_c"], "flow_sccm": profile["optimum_flow_sccm"], "duration_s": duration}),
                            "recipe_provenance": "configurable uncalibrated material-metal software assumptions; not a physical recipe" if material_metal else "bounded two-flake simulation window from incubation + exposure; "
                                "not a real recipe" if pair_mode else "legacy field-growth simulation fixture, not a real recipe",
                            "measurement_scope": "qualitative interface progress; hBN and reactive flake preserved; no product composition inferred" if material_metal else "one product from exactly two prepared flakes"
                                if module.get("experiment_kind") == "two_flake_reaction" else
                                "observed microscope field, not the whole chip",
                            "reaction_fixture": module.get("reaction"),
                            "material_metal_capability": {"preparation": "guarded simulated transport/pickup/release with known PC/mica and optical verification",
                                "reaction": "qualitative interface progress without a chemical product claim; legacy consumption model excluded",
                                "default_protocol": "qualitative-material-metal-v1",
                                "simulation_assumptions": module.get("material_metal"),
                                "software_workflow_adapter": "synthetic-material-metal-workflow-v1; explicit user authorization required; abstract state only, no physical sample motion or chemistry",
                                "software_workflow": ["Hands.prepare_material_metal", "Qumus.delegate_reactor(objective)",
                                    "Reactor.reactor_material_metal", "Reactor.submit_material_metal_result"],
                                "software_result": "simulation_accepted=true can complete a simulated task; experiment_accepted remains false"},
                            "completion_contract_scope": "Device simulation has separate evidence-based acceptance; physical experiment criteria remain external",
                            "terminal_dispositions": {"cold_storage": "unload_reactor_chip",
                                "observation_hold": "hold_reactor_observation"},
                            "stored_carrier_verification": "verify_reactor_state refreshes cold_storage evidence "
                                "for a carrier already in dedicated storage, after any later control command. "
                                "No preparation manifest is needed for an empty carrier. It does not cool or unload.",
                            "completion_contracts": {
                                **{operation: {role: {"status": status} for role, status in FLOW_SAMPLE_STATUSES[operation].items()}
                                   for operation in sorted(OPERATIONS)},
                                "assess_reactor_inputs": {"carrier": {"in_storage": True},
                                    "status_note": "Carrier and input flake statuses are preserved"},
                                "prepare_reactor_pair": {"carrier": {"status": "reactor_pair_prepared", "in_storage": True},
                                    "input_flakes": {"status": "stacked", "location": "the carrier chip_id"}},
                                "load_reactor_chip": {"carrier": {"location": "reactor_port"},
                                    "status_note": "Carrier status is preserved"},
                                "reactor_reaction": {"carrier": {"status": "reactor_processed", "location": "reactor_port"}},
                                "submit_reactor_pair_result": {"input_flakes": {"status": "reactor_consumed"},
                                    "product": {"status": "reactor_product", "location": "the carrier chip_id"},
                                    "status_note": "Acceptance does not move or rename the carrier status"},
                                "unload_reactor_chip": {"carrier": {"in_storage": True},
                                    "status_note": "Carrier status is preserved; do not invent stored"},
                                "inspect_reactor_product": {"carrier": {"in_storage": True},
                                    "status_note": "Carrier status is preserved; do not invent stored"},
                                "verify_reactor_state": {"carrier": {"in_storage": True},
                                    "status_note": "All sample statuses and locations are preserved"}},
                            "operation_argument_contracts": {operation: argument_contract(operation) for operation in sorted(OPERATIONS)},
                            "cold_load_recovery": "recover_reactor_load",
                            "preparation_recovery": "recover_reactor_preparation",
                            "product_followup": "inspect_reactor_product routes the cold carrier through shared storage "
                                "to ordinary optics and back; no manual relocation is needed."})


@qumus_tool("get_reactor_setup", "Compatibility view of get_lab_setup instruments.reactor. Prefer the common laboratory setup query; "
            "inventory and current readiness are separate queries.", agents={"orchestrator", "reactor", "advisor"}, kind="query", model_visible=False)
async def get_reactor_setup(args: NoArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.agents.tools.advisor import get_lab_setup

    result = await get_lab_setup(args, ctx)
    return ToolResult(status="ok", summary="Reactor details from the common laboratory setup.",
                      data=result.data["instruments"]["reactor"])


@qumus_tool("set_reactor_goal", "Set the synthetic growth objective for a dedicated chip. "
            "Thresholds apply to the observed microscope field. The Reactor specialist cannot "
            "change them. State the objective before delegating a growth task.",
            agents={"orchestrator"}, kind="state", refs={"chip_id": "chip"})
async def set_reactor_goal(args: ReactorGoalArgs, ctx: ToolContext) -> ToolResult:
    if not ctx.project_id:
        return ToolResult(status="error", summary="No active project.")
    if "reactor_module" not in (ctx.extra.get("bench") or {}):
        return ToolResult(status="error", summary="Experimental reactor is disabled on this bench.")
    if ctx.extra["bench"]["reactor_module"].get("experiment_kind") == "two_flake_reaction":
        return ToolResult(status="error", summary="Use set_reactor_pair_goal for this two-flake experiment.")
    chip = ctx.store.get_sample(args.chip_id)
    if not chip or not chip["props"].get("reactor_substrate"):
        return ToolResult(status="error", summary="Choose a dedicated reactor substrate.")
    previous = (ctx.store.get_project(ctx.project_id) or {"state": {}})["state"].get("reactor_goal")
    revision = int((previous or {}).get("revision", 0)) + 1
    goal = {**args.model_dump(), "revision": revision, "scope": "observed_field",
            "source": "synthetic_experiment_objective"}
    ctx.store.update_project_state(ctx.project_id, reactor_goal=goal, reactor_result=None)
    ctx.store.emit("reactor.goal", {"project_id": ctx.project_id, "goal": goal}, agent=ctx.agent)
    return ToolResult(status="ok", summary=f"Synthetic reactor goal {revision} saved for "
                      f"{args.chip_id}; thresholds apply to its observed field.", data={"goal": goal})


@qumus_tool("load_reactor_chip", "Load a dedicated narrow chip into the carrier slot, advance "
            "the separate insertion rod and focus the microscope. Returns a job; wait_job before continuing.",
            agents={"reactor"}, kind="hardware", refs={"chip_id": "chip"},
            requires=("reactor_ready",))
async def load_reactor_chip(args: ChipParams, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "load_reactor_chip", args.model_dump())


@qumus_tool("reactor_growth", "Apply temperature/flow targets, verify measured feedback and "
            "observe one bounded synthetic growth window. Job success does not certify a "
            "growth target. Read its measured outcome; submit_reactor_result checks the goal.",
            agents={"reactor"}, kind="hardware", refs={"chip_id": "chip"},
            requires=("reactor_ready",))
async def reactor_growth(args: GrowthParams, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "reactor_growth", args.model_dump())


@qumus_tool("set_reactor_pair_goal", "Fix a two-flake preparation and local reaction target. "
            "A successful result needs one verified small product with both source identities; "
            "field coverage is not a reaction objective.", agents={"orchestrator"}, kind="state",
            refs={"chip_id": "chip"})
async def set_reactor_pair_goal(args: PairGoalArgs, ctx: ToolContext) -> ToolResult:
    if not ctx.project_id:
        return ToolResult(status="error", summary="No active project.")
    from qumus2.records.reactor_requirements import established_requirements

    if established_requirements(ctx.store, ctx.project_id):
        return ToolResult(status="error", summary="Pass the material-metal objective in the Reactor task dialogue; legacy pair goals cannot redefine it.")
    if (ctx.extra.get("bench") or {}).get("reactor_module", {}).get(
            "experiment_kind") != "two_flake_reaction":
        return ToolResult(status="error", summary="Two-flake reaction backend is not enabled.")
    try:
        pair = prepared_pair(ctx.store, args.chip_id)
    except ValueError as exc:
        return ToolResult(status="error", summary=str(exc))
    previous = (ctx.store.get_project(ctx.project_id) or {"state": {}})["state"].get("reactor_goal")
    goal = {**args.model_dump(), "revision": int((previous or {}).get("revision", 0)) + 1,
            "preparation_id": pair.preparation_id, "input_flake_ids": list(pair.input_flake_ids),
            "scope": "two_flake_reaction", "source": "synthetic_experiment_objective"}
    ctx.store.update_project_state(ctx.project_id, reactor_goal=goal, reactor_result=None)
    ctx.store.emit("reactor.goal", {"project_id": ctx.project_id, "goal": goal}, agent=ctx.agent)
    return ToolResult(status="ok", summary="Two-flake reaction goal saved.", data={"goal": goal})


@qumus_tool("reactor_reaction", "Apply guarded temperature and flow to exactly two prepared "
            "small flakes, with bounded observation. Wait for the job and then use "
            "submit_reactor_pair_result; job success alone does not prove a product exists.",
            agents={"reactor"}, kind="hardware", refs={"chip_id": "chip"}, requires=("reactor_ready",))
async def reactor_reaction(args: GrowthParams, ctx: ToolContext) -> ToolResult:
    from qumus2.records.reactor_requirements import established_requirements

    if established_requirements(ctx.store, ctx.project_id):
        return ToolResult(status="needs_attention", summary="Material-metal reaction needs a matching observation/kinetics adapter; "
            "the legacy two-flake model would consume the protective hBN and cannot execute this objective.")
    return submit(ctx, "reactor_reaction", args.model_dump())


@qumus_tool("submit_reactor_pair_result", "Check the latest successful reaction job, current "
            "preparation, fresh saved material evidence and fixed goal. Record at most one product "
            "with both input identities. No product or a failed model result cannot be accepted.",
            agents={"reactor"}, kind="state", refs={"chip_id": "chip"})
async def submit_reactor_pair_result(args: PairResultArgs, ctx: ToolContext) -> ToolResult:
    # Lock before reading goals, jobs, evidence or identities. All product/source
    # writes, project acceptance and events commit together or roll back.
    try:
        with ctx.store._transaction():
            return _submit_pair_result(args, ctx)
    except ValueError as exc:
        return ToolResult(status="error", summary=str(exc))


def _submit_pair_result(args: PairResultArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.reactor_requirements import acceptance_errors

    if errors := acceptance_errors(ctx.store, ctx.project_id):
        return ToolResult(status="error", summary="; ".join(errors), data={"requirement_errors": errors})
    project = ctx.store.get_project(ctx.project_id) if ctx.project_id else None
    goal = (project or {"state": {}})["state"].get("reactor_goal")
    if not goal or goal.get("scope") != "two_flake_reaction" or goal.get("chip_id") != args.chip_id:
        return ToolResult(status="error", summary="Qumus must set a matching two-flake goal first.")
    run = ctx.store.get_run(args.reaction_job_id)
    if not run or run["flow"] != "reactor_reaction" or run["status"] != "succeeded" \
            or run["project_id"] != ctx.project_id or run["params"].get("chip_id") != args.chip_id:
        return ToolResult(status="error", summary="Cite this project's successful reaction job for this chip.")
    jobs = {r["id"] for r in ctx.store.list_runs() if r["flow"] == "reactor_reaction"
            and r["project_id"] == ctx.project_id and r["params"].get("chip_id") == args.chip_id}
    submitted = [e.run_id for e in ctx.store.events_after(0, limit=1_000_000)
                 if e.kind == "job.submitted" and e.run_id in jobs]
    if not submitted or submitted[-1] != args.reaction_job_id:
        return ToolResult(status="error", summary="A later reaction attempt exists; review it first.")
    chip = ctx.store.get_sample(args.chip_id)
    result = chip["props"].get("reactor_result") or {}
    if chip["props"].get("reactor_state_uncertain") or result.get("reaction_job_id") != args.reaction_job_id:
        return ToolResult(status="error", summary="Current reaction evidence is missing or uncertain.")
    try:
        obs = validate_pair_observation(ctx.store, args.chip_id, result.get("pair_observation") or {})
        if obs.preparation_id != goal["preparation_id"] or list(obs.input_flake_ids) != goal["input_flake_ids"]:
            raise ValueError("the current goal names a different preparation")
    except ValueError as exc:
        return ToolResult(status="error", summary=str(exc))
    accepted = (obs.visible and obs.product is not None and obs.status == "complete"
                and not obs.failure_reason and obs.reaction_fraction is not None
                and obs.reaction_fraction >= goal["target_reaction_fraction"])
    data = {"chip_id": args.chip_id, "evidence_job_id": args.reaction_job_id,
            "goal": goal, "observation": result, "accepted": accepted,
            "scope": "qualitative synthetic two-flake reaction; not validated chemistry"}
    if accepted:
        data["product_id"] = _record_pair_product(ctx.store, args.chip_id,
            obs.model_dump(mode="json"), reaction_job_id=args.reaction_job_id, project_id=ctx.project_id)
        ctx.store.update_project_state(ctx.project_id, reactor_result=data)
        ctx.store.emit("reactor.result_accepted", data, agent=ctx.agent)
    else:
        ctx.store.update_project_state(ctx.project_id, reactor_result=None)
    return ToolResult(status="ok" if accepted else "needs_attention", data=data,
                      summary="One verified synthetic product recorded." if accepted else
                      "No verified product meets the reaction goal; inspect the saved material outcome.",
                      images=result.get("images") or [])


@qumus_tool("position_reactor_slot", "Fine-position the entire reactor chamber and rod assembly "
            "in XY relative to the fixed microscope, in absolute millimetres from the loading "
            "reference. Bottom moves X and top moves Y; insertion is separate. No Z or rotation.",
            agents={"reactor"}, kind="hardware", refs={"chip_id": "chip"},
            requires=("reactor_ready",))
async def position_reactor_slot(args: PositionParams, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "position_reactor_slot", args.model_dump())


@qumus_tool("observe_reactor", "Capture a fresh reactor image and save its synthetic field "
            "measurements. Optional autofocus moves only microscope Z. Returns a job.",
            agents={"reactor"}, kind="hardware", refs={"chip_id": "chip"},
            requires=("reactor_ready",))
async def observe_reactor(args: ObserveParams, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "observe_reactor", args.model_dump())


@qumus_tool("shutdown_reactor", "Disable reactor heat/flow targets through a serialized job. "
            "Residual temperature and flow remain in feedback. This is not an immediate emergency "
            "stop; use the application Stop control to interrupt an active job.",
            agents={"reactor"}, kind="hardware")
async def shutdown_reactor(args: ShutdownParams, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "shutdown_reactor", {})


@qumus_tool("unload_reactor_chip", "Disable targets, wait for measured virtual access conditions, "
            "retract the carrier and return its chip to the original dedicated storage slot.",
            agents={"reactor"}, kind="hardware", refs={"chip_id": "chip"},
            requires=("reactor_ready",))
async def unload_reactor_chip(args: ChipParams, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "unload_reactor_chip", args.model_dump())


@qumus_tool("submit_reactor_result", "Check the latest successful growth job and saved field "
            "measurements against the orchestrator's reactor goal. No model-supplied measurements "
            "or threshold changes are accepted. An unmet goal returns needs_attention.",
            agents={"reactor"}, kind="state", refs={"chip_id": "chip"})
async def submit_reactor_result(args: ReactorResultArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.reactor_requirements import acceptance_errors

    if errors := acceptance_errors(ctx.store, ctx.project_id):
        return ToolResult(status="error", summary="; ".join(errors), data={"requirement_errors": errors})
    project = ctx.store.get_project(ctx.project_id) if ctx.project_id else None
    goal = (project or {"state": {}})["state"].get("reactor_goal")
    if not goal or goal.get("chip_id") != args.chip_id:
        return ToolResult(status="error", summary="Qumus must set a matching reactor goal first.")
    run = ctx.store.get_run(args.growth_job_id)
    if not run or run["flow"] != "reactor_growth" or run["status"] != "succeeded" \
            or run["project_id"] != ctx.project_id or run["params"].get("chip_id") != args.chip_id:
        return ToolResult(status="error", summary="Cite this project's successful growth job for this chip.")
    growth_jobs = {r["id"] for r in ctx.store.list_runs() if r["flow"] == "reactor_growth"
                   and r["project_id"] == ctx.project_id
                   and r["params"].get("chip_id") == args.chip_id}
    submitted = [e.run_id for e in ctx.store.events_after(0, limit=1_000_000)
                 if e.kind == "job.submitted" and e.run_id in growth_jobs]
    if not submitted or submitted[-1] != args.growth_job_id:
        return ToolResult(status="error", summary="A later growth attempt exists; review its result first.")
    chip = ctx.store.get_sample(args.chip_id)
    result = chip["props"].get("reactor_result") or {}
    evidence_job = result.get("growth_job_id") or result.get("job_id")
    if chip["props"].get("reactor_state_uncertain") or evidence_job != args.growth_job_id:
        return ToolResult(status="error", summary="Current field evidence is missing or uncertain.")
    values = [result.get(k) for k in ("coverage", "uniformity", "defect_fraction")]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1 for v in values):
        return ToolResult(status="error", summary="Saved observation has no valid field measurements.")
    coverage, uniformity, defects = values
    accepted = (coverage >= goal["target_coverage"] and uniformity >= goal["target_uniformity"]
                and defects <= goal["max_defect_fraction"])
    data = {"chip_id": args.chip_id, "evidence_job_id": args.growth_job_id,
            "goal": goal, "observation": result, "accepted": accepted,
            "scope": "synthetic observed field; not validated physical material growth"}
    if accepted:
        ctx.store.update_project_state(ctx.project_id, reactor_result=data)
        ctx.store.emit("reactor.result_accepted", data, agent=ctx.agent)
    else:
        ctx.store.update_project_state(ctx.project_id, reactor_result=None)
    return ToolResult(status="ok" if accepted else "needs_attention",
                      summary=("Synthetic field target met." if accepted else
                               "Synthetic field target not met; inspect the measured outcome."),
                      data=data, images=result.get("images") or [])




class ReactorSelectionArgs(ChipParams):
    assessments: list[Assessment] = Field(default_factory=list, description="Common image-quality assessments for every confirmed layer")
    phase: str = Field(pattern="^(screen|confirm)$")
    flake_ids: list[str] = Field(min_length=2, max_length=20)
    rationale: str = Field(min_length=1)


@qumus_tool("submit_reactor_selection", "Picker screens photographed candidates (include backups per source chip), then confirms exactly two "
            "after Hands returns local-neighbourhood evidence. hBN is first/top; the second flake is the specified reactive material. "
            "This is independent of Device Guidance.", agents={"picker"}, kind="state", refs={"chip_id": "chip", "flake_ids": "flake"}, requires=("not_vetoed",))
async def submit_reactor_selection(args: ReactorSelectionArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.reactor_requirements import (
        established_requirements,
        input_errors,
        norm,
        requirements_record,
    )

    req = established_requirements(ctx.store, ctx.project_id)
    if not req:
        return ToolResult(status="error", summary="Qumus must establish the experiment before Picker selection.")
    previous = ctx.store.get_project(ctx.project_id)["state"].get("reactor_selection") or {}
    errors = []
    rows = [ctx.store.get_sample(fid) for fid in args.flake_ids]
    if len(set(args.flake_ids)) != len(args.flake_ids):
        errors.append("Candidate IDs must be distinct.")
    if any(f["status"] not in ("catalogued", "selected") or not f["props"].get("images") for f in rows):
        errors.append("Screen only catalogued photographed flakes.")
    if any(norm(f["props"].get("material")) not in ("hBN", norm(req.reactive_material)) for f in rows):
        errors.append("Candidates must match the established materials.")
    if not {"hBN", norm(req.reactive_material)} <= {norm(f["props"].get("material")) for f in rows}:
        errors.append("Screen both hBN and the specified reactive material.")
    if args.phase == "confirm":
        record = requirements_record(ctx.store, ctx.project_id)
        if previous.get("chip_id") != args.chip_id or previous.get("requirements_event_id") != record["event_id"] or not set(args.flake_ids) <= set(previous.get("screened_ids", [])):
            errors.append("Confirm only candidates from the current screened set for this carrier and requirement revision.")
        errors += input_errors(ctx.store, req, args.flake_ids, carrier_id=args.chip_id, require_assessment=True)
        by_id = {assessment.flake_id: assessment for assessment in args.assessments}
        for flake in rows:
            errors += optical_selection_errors(ctx, flake, by_id.get(flake["id"]))
    if errors:
        return ToolResult(status="error", summary="; ".join(errors), data={"selection_errors": errors})
    record = requirements_record(ctx.store, ctx.project_id)
    selection = {"phase": args.phase, "chip_id": args.chip_id, "requirements_event_id": record["event_id"],
        "screened_ids": args.flake_ids if args.phase == "screen" else previous["screened_ids"],
        "confirmed_ids": args.flake_ids if args.phase == "confirm" else [], "rationale": args.rationale,
        "source_locations": {f["id"]: f["location"] for f in rows}}
    if args.phase == "confirm":
        from qumus2.agents.tools.picking import save_residue
        from qumus2.records.geometry import selection_source_snapshot

        save_residue(ctx, args.assessments)
        selection["source_evidence"] = selection_source_snapshot(ctx.store, args.flake_ids)
        selection["assessments"] = [a.model_dump() for a in args.assessments]
    ctx.store.update_project_state(ctx.project_id, reactor_selection=selection)
    ctx.store.emit("reactor.selection", {"project_id": ctx.project_id, **selection}, agent="picker")
    return ToolResult(status="ok", summary=f"Picker {args.phase} saved with source locations.", data=selection,
                      images=[p for f in rows for p in (f["props"].get("pickup_assessment") or f["props"].get("reactor_pickup_assessment") or {}).get("images", [])][:20])


class MaterialMetalPrepareArgs(PrepareReactorPairArgs):
    stamp_id: str | None = Field(None, description="Known fresh PC/mica stamp; omit to select an available known stamp")
    protocol_id: str = Field("qualitative-material-metal-v1", description="Default: guarded qualitative device simulation. The historical abstract fixture remains separately available.")
    fixture_instruction_id: str | None = Field(None, description="Only the historical abstract fixture requires explicit fixture authority; omit for ordinary simulation")


class MaterialMetalObserveArgs(ChipParams):
    protocol_id: str = "qualitative-material-metal-v1"
    fixture_instruction_id: str | None = None
    temperature_c: float | None = Field(None, ge=22, le=950, description="Simulation setpoint; omit for the recorded qualitative profile")
    flow_sccm: float | None = Field(None, ge=0, le=400)
    duration_s: float | None = Field(None, gt=0, le=900)


@qumus_tool("prepare_material_metal", "The normal hBN/reactive-material/metal/silicon preparation tool; use directly, not prepare_reactor_pair. "
            "After Picker confirmation, run guarded simulated carrier transport, hBN-first pickup, "
            "reactive-material pickup and release onto the reported metal/Si carrier. Verify actual target optics and return to storage. "
            "Known PC/mica and configurable uncalibrated assumptions are retained in the evidence; no real hardware is supported.",
            agents={"hands"}, kind="hardware", refs={"chip_id": "chip", "flake_ids": "flake", "stamp_id": "stamp"},
            requires=("reactor_ready", "not_vetoed"), model_visible=False)
async def prepare_material_metal(args: MaterialMetalPrepareArgs, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "prepare_material_metal", {"project_id": ctx.project_id,
        "target_chip_id": args.chip_id, **args.model_dump(exclude={"chip_id"})})


@qumus_tool("reactor_material_metal", "Execute the full guarded material-metal simulation: load the prepared carrier, "
            "acquire both layers, expose under the configurable qualitative profile, save observations, turn targets off, "
            "cool and finish in the task's declared storage or observation hold. Simulation progress is not chemical conversion. "
            "Then use submit_material_metal_result to check the frozen task goal; process completion alone does not guarantee success.",
            agents={"reactor"}, kind="hardware", refs={"chip_id": "chip"}, requires=("reactor_ready",))
async def reactor_material_metal(args: MaterialMetalObserveArgs, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "reactor_material_metal", {"project_id": ctx.project_id, **args.model_dump()})


@qumus_tool("submit_material_metal_result", "Check a material-metal adapter observation against the original requirements, "
            "preparation provenance and successful Reactor job. A synthetic workflow may be verified while experiment_accepted stays false.",
            agents={"reactor"}, kind="state", refs={"chip_id": "chip"})
async def submit_material_metal_result(args: PairResultArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.material_metal import workflow_result_errors
    from qumus2.records.material_metal_simulation import simulation_result_errors

    with ctx.store._transaction():
        project = ctx.store.get_project(ctx.project_id)
        chip = ctx.store.get_sample(args.chip_id)
        if chip["props"].get("material_metal_preparation", {}).get("scope") == "device_simulation":
            import json

            terminal = ctx.store._x("SELECT payload_json FROM event WHERE kind='reactor.process' AND run_id=? ORDER BY id DESC LIMIT 1", (args.reaction_job_id,)).fetchone()
            result = {"chip_id": args.chip_id, "evidence_job_id": args.reaction_job_id,
                "scope": "device_simulation", "goal": project["state"].get("reactor_goal"),
                "observation": chip["props"].get("material_metal_observation"),
                "terminal_evidence": json.loads(terminal["payload_json"]) if terminal else None,
                "simulation_accepted": True, "experiment_accepted": False, "chemistry_verified": False,
                "physical_motion_verified": False, "simulated_motion_verified": True}
            if errors := simulation_result_errors(ctx.store, ctx.project_id, result, bench=ctx.extra.get("bench") or {}):
                return ToolResult(status="error", summary="; ".join(errors), data={"simulation_errors": errors})
            ctx.store.update_project_state(ctx.project_id, reactor_simulation_result=result)
            ctx.store.emit("material_metal.simulation_accepted", {"project_id": ctx.project_id, **result}, agent=ctx.agent)
            return ToolResult(status="ok", summary="Qualitative device-simulation target met with observed stack, interface progress and safe final disposition. Real chemistry remains unverified.", data=result)
        result = {"chip_id": args.chip_id, "evidence_job_id": args.reaction_job_id,
            "scope": "synthetic_workflow", "goal": project["state"].get("reactor_goal"),
            "observation": chip["props"].get("material_metal_observation"),
            "workflow_accepted": True, "experiment_accepted": False,
            "chemistry_verified": False, "physical_motion_verified": False}
        errors = workflow_result_errors(ctx.store, ctx.project_id, result)
        if errors:
            return ToolResult(status="error", summary="; ".join(errors), data={"workflow_errors": errors})
        ctx.store.update_project_state(ctx.project_id, reactor_workflow_result=result)
        ctx.store.emit("material_metal.workflow_accepted", {"project_id": ctx.project_id, **result}, agent=ctx.agent)
        return ToolResult(status="ok", summary="Abstract material-metal software workflow verified. Physical transfer and chemistry are unverified; the experiment is not accepted.", data=result)


class RecoverMaterialMetalArgs(RecoverArgs):
    action: Literal["reverify", "retry"] = "reverify"


@qumus_tool("recover_material_metal_job", "Bounded recovery of a parked material-metal job in its original reservations. "
            "Reverify checks observed postconditions; retry is limited to reversible steps. Interrupted exposure and uncertain "
            "release cannot be replayed automatically. Use the original job ID and evidence-based reason.",
            agents={"hands", "reactor"}, kind="recovery", requires=("recovery_budget",))
async def recover_material_metal_job(args: RecoverMaterialMetalArgs, ctx: ToolContext) -> ToolResult:
    run = ctx.store.get_run(args.job_id)
    expected = "prepare_material_metal" if ctx.agent == "hands" else "reactor_material_metal"
    if not run or run["flow"] != expected or run["project_id"] != ctx.project_id:
        return ToolResult(status="error", summary="Recover only this role's original material-metal job in this project.")
    # The lab's step rows (mirrored) name the interrupted step and whether it may be repeated.
    step = next((s for s in ctx.store.get_steps(args.job_id) if s["status"] not in ("done", "skipped")), None)
    if step and (step["name"] in ("set_reactor_conditions", "observe_material_metal_progress") or
                 args.action == "retry" and not step.get("safe_to_resume")):
        return ToolResult(status="error", summary="Interrupted exposure or uncertain release requires review; it cannot be replayed as ordinary recovery.")
    recover = RecoverArgs(job_id=args.job_id, reason=args.reason)
    return await (reverify_job(recover, ctx) if args.action == "reverify" else retry_job(recover, ctx))
