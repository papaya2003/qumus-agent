"""Orchestrator's project-state tools (v1 project_state_tools.py, spec 11.4)."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from qumus2.agents.policy import open_gates
from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.records.material_metal_arguments import (
    OPERATIONS as MATERIAL_METAL_OPERATIONS,
)
from qumus2.records.material_metal_arguments import (
    argument_contract,
    argument_errors,
    argument_feedback,
    normalize_arguments,
)
from qumus2.records.material_metal_plan import (
    correct_material_metal_mechanics,
    operation_errors,
    preparation_evidence,
)
from qumus2.records.plan_completion import (
    ReactorOutcome,
    SampleOutcome,
    completion_contract,
    correct_misassigned_flow_statuses,
    executable_flow_names,
    invalid_flow_sample_locations,
    invalid_flow_sample_statuses,
    preserves_obligation,
    unmet_sample_outcomes,
)
from qumus2.records.search_outcomes import SEARCH_FLOWS, correct_search_source_identity
from qumus2.runtime.store import PlanConflict


class NoArgs(BaseModel):
    pass


class PlanStep(BaseModel):
    id: str = Field("", description="Stable step id, e.g. s1; empty = numbered for you")
    # "step"/"agent" are what Claude wrote in every real run: accept them
    title: str = Field(validation_alias=AliasChoices("title", "step", "name"),
                       description="What the step achieves, e.g. Check the bench")
    owner: Literal["orchestrator", "advisor", "eyes", "hands", "picker", "designer",
                   "reactor", "human"] = Field(validation_alias=AliasChoices("owner", "agent"),
                                    description="Who does it; human for steps a person does")
    status: Literal["pending", "running", "done", "failed", "skipped"] = "pending"
    note: str = ""
    operation: str | None = Field(None, description="For Hands/Reactor, an exact registered flow executable by that owner, "
        "e.g. search_chip or place_chip_on_transfer_stage; never an invented alias or compound name. "
        "Hands runs revisit_chip through search_chip(revisit_only=true). A future step may use null, "
        "but must be refined to a registered flow before declaring an exact destination or hardware delegation. "
        "Known flow outputs are checked by sample role; prepare_material_metal leaves its carrier "
        "material_metal_prepared and its input flakes stacked. Established material-metal requirements "
        "exclude legacy prepare_reactor_pair/reactor_reaction.")
    sample_outcomes: list[SampleOutcome] = Field(default_factory=list, max_length=12,
        description="Explicit final ledger conditions for this step. Use known sample IDs; "
                    "require in_storage after search/exfoliation rather than guessing a slot. "
                    "Search targets the receiving chip, never the raw-material carrier. Leave future outputs empty "
                    "until the actual chip is known, then refine before delegation. "
                    "A successful job cannot complete the step while these conditions are unmet.")
    materials: list[str] = Field(default_factory=list, description="Material identities this step prepares/searches or stacks, in top-first order for assembly.")
    experiment_stage: Literal["screen", "confirm"] | None = Field(None, description="Picker handoff phase for a reactor experiment.")
    operation_args: dict[str, Any] = Field(default_factory=dict, description="Execution parameters and explicit evidence assertions. "
        "For material-metal flows use operation_argument_contracts from get_project_context/get_reactor_setup; unknown names, "
        "wrong types and conflicting aliases are refused before save and delegation. Raw obligations are retained; "
        "normalized_operation_args gives their canonical view. Exfoliation requires material and all six recipe values.")
    reactor_outcome: ReactorOutcome | None = Field(None, description="Required before Reactor work: "
        "cold_storage with heater off/gas zero, or intentional observation_hold with explicit targets. "
        "Product acceptance alone does not complete cooling or storage.")

    @field_validator("owner", mode="before")
    @classmethod
    def _person(cls, v: Any) -> Any:
        return "human" if isinstance(v, str) and v.lower() in ("user", "person") else v


class PhysicalPlanChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    action: Literal["cancel", "supersede"] = Field(description="cancel has no replacements; "
        "supersede retargets to new pending physical step IDs while preserving the original obligation.")
    replacement_step_ids: list[str] = Field(default_factory=list,
        description="New pending physical step IDs for supersede; empty for cancel.")
    instruction_id: str = Field(description="Cite the person's explicit cancellation/retargeting instruction.")


class UpdatePlanArgs(BaseModel):
    plan: list[PlanStep] = Field(description="The whole Task Plan, including required search before selection.")
    note: str = Field(description="Why the plan changed (shown to the user).")
    expected_version: int | None = Field(None, ge=0, description="plan_version last read from "
                                          "get_project_context; rejects stale edits.")
    task_kind: Literal["device", "experiment"] | None = Field(None, description="device for "
        "a device build; experiment only for an explicitly standalone experiment/exploration.")
    physical_changes: list[PhysicalPlanChange] = Field(default_factory=list, description="Unfinished physical "
        "steps cannot disappear, change owner, be skipped or lose conditions. For an explicit user cancellation "
        "or retargeting, cite the instruction and old step ID; supersede names new pending physical step IDs. "
        "Explicit sample retargeting such as 'Keep chip_001 untouched; use chip_002 instead' is supported, "
        "as are step-ID commands and structured plan_changes. Retargeting preserves execution conditions. "
        "Code retains the old step as skipped with the cited instruction, never as executed.")


class SaveStatusArgs(BaseModel):
    summary: str
    status: Literal["active", "blocked", "done"] = "active"


class ExcludeFlakeArgs(BaseModel):
    flake_id: str
    reason: str


def _need_project(ctx: ToolContext) -> str:
    if not ctx.project_id:
        raise ValueError("no active project")
    return ctx.project_id


def _sample_retarget_authorized(text: str, old: dict, replacements: list[dict], store: Any) -> bool:
    """Recognize bounded direct target changes without exposing internal step IDs."""
    if len(replacements) != 1 or any(c in text for c in ('"', '`', '“', '”')) or re.search(r"'[^']+'", text):
        return False
    new = replacements[0]

    def targets(step):
        return {s["sample_id"] for s in step.get("sample_outcomes", [])} | (
            {step["reactor_outcome"]["chip_id"]} if step.get("reactor_outcome") else set())

    before, after = targets(old), targets(new)
    if not before:
        # Future steps may defer outcomes until hardware delegation. In that
        # case allow only a literal known-sample substitution in an otherwise
        # identical title, preserving owner, operation and all existing goals.
        samples = store.list_samples()

        def named(title):
            return {s["id"] for s in samples if re.search(
                r"(?<![\w.-])" + re.escape(s["id"]) + r"(?![\w.-])", title)}

        before, title_after = named(old.get("title", "")), named(new.get("title", ""))
        if len(before) != 1 or len(title_after) != 1 or old.get("operation") != new.get("operation"):
            return False
        source, target = next(iter(before)), next(iter(title_after))
        rewritten = re.sub(r"(?<![\w.-])" + re.escape(source) + r"(?![\w.-])", target, old["title"])
        if rewritten != new["title"]:
            return False
        after |= title_after
    if len(before) != 1 or len(after) != 1 or before == after:
        return False
    source, target = next(iter(before)), next(iter(after))
    source_sample, target_sample = store.get_sample(source), store.get_sample(target)
    if not source_sample or not target_sample or source_sample["kind"] != target_sample["kind"]:
        return False
    remapped = {**old, "sample_outcomes": [{**s, "sample_id": target} for s in old.get("sample_outcomes", [])]}
    if old.get("reactor_outcome"):
        remapped["reactor_outcome"] = {**old["reactor_outcome"], "chip_id": target}
    if not preserves_obligation(remapped, new):
        return False
    clauses = [re.sub(r"^(?:change of plan:\s*|please\s+)", "", clause.strip(), flags=re.I)
               for clause in re.split(r"[.!?;\n]+", text) if clause.strip()]
    source, target = re.escape(source), re.escape(target)
    # A direct negation involving either target invalidates the bounded reading.
    if any(re.match(r"(?:do not|don't|never|avoid)\b", c, re.I)
           and re.search(r"\b(?:" + source + "|" + target + r")\b", c, re.I) for c in clauses):
        return False
    keep = any(re.fullmatch(r"keep\s+" + source + r"\s+untouched(?:\s+for\s+[^:]+)?", c, re.I) for c in clauses)
    use = any(re.fullmatch(r"use\s+" + target + r"(?:\s+for\s+[^:]+)?\s+instead", c, re.I) for c in clauses)
    direct = any(re.fullmatch(r"use\s+" + target + r"(?:\s+for\s+[^:]+)?\s+instead of\s+" + source, c, re.I)
                 for c in clauses)
    return direct or keep and use


def _physical_change_authorized(change: PhysicalPlanChange, ctx: ToolContext,
                                old: dict, replacements: list[dict]) -> bool:
    ins = ctx.store.get_instruction(change.instruction_id)
    if (not ins or ins["source"] != "ui" or ins["kind"] not in ("message", "plan_change")
            or ins["project_id"] not in (None, ctx.project_id)):
        return False
    # An earlier request or an unrelated message is not cancellation authority.
    event = ctx.store._x("SELECT id FROM event WHERE kind='user.message' AND "
                        "json_extract(payload_json,'$.instruction_id')=?", (change.instruction_id,)).fetchone()
    started, active = None, False
    for row in ctx.store._x("SELECT id,payload_json FROM event WHERE kind IN ('plan.updated','plan.step') AND "
                           "json_extract(payload_json,'$.project_id')=? ORDER BY id", (ctx.project_id,)):
        step = next((s for s in json.loads(row["payload_json"]).get("plan", []) if s.get("id") == change.step_id), {})
        now_active = step.get("owner") in ("hands", "reactor") and step.get("status") not in ("done", "skipped")
        if now_active and not active:
            started = row["id"]
        active = now_active
    if not event or started is None or event["id"] <= started:
        return False
    expected = change.model_dump(exclude={"instruction_id"})
    try:
        entries = json.loads(ins["text"])["plan_changes"]
        if any({**item, "replacement_step_ids": item.get("replacement_step_ids", [])} == expected for item in entries):
            return True
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    # Deliberately narrow direct commands; a quote, negation or generic approval
    # does not grant a model permission to discard a different obligation.
    text = ins["text"].strip().rstrip(".").strip()
    if change.action == "cancel" and not change.replacement_step_ids:
        return bool(re.fullmatch(r"(?:please\s+)?cancel\s+" + re.escape(change.step_id), text, re.I))
    if change.action == "supersede" and len(change.replacement_step_ids) == 1:
        return (bool(re.fullmatch(r"(?:please\s+)?replace\s+" + re.escape(change.step_id)
                    + r"\s+with\s+" + re.escape(change.replacement_step_ids[0]), text, re.I))
                or _sample_retarget_authorized(ins["text"], old, replacements, ctx.store))
    return False


#: state the orchestrator does not need in full (the picker's per-candidate
#: assessments run to tens of KB; the subagents' last messages are history)
_BULKY_STATE = ("selection_assessments", "agents_last")


@qumus_tool("get_project_context", "Current project state, Task Plan and plan_version for editing.",
            agents={"orchestrator"}, kind="query")
async def get_project_context(args: NoArgs, ctx: ToolContext) -> ToolResult:
    proj = ctx.store.get_project(_need_project(ctx)) or {}
    state = dict(proj.get("state", {}))
    for k in _BULKY_STATE:
        if k in state:
            v = state.pop(k)
            state[f"{k}_count"] = len(v) if isinstance(v, (list, dict)) else 1
    from qumus2.agents.readiness import plan_readiness_report
    from qumus2.records.reactor_requirements import requirements_record

    state["established_experiment"] = requirements_record(ctx.store, ctx.project_id)
    prepared = [{"step_id": step["id"], **evidence} for step in proj.get("plan", [])
                if step.get("status") not in ("done", "skipped")
                and (evidence := preparation_evidence(ctx.store, ctx.project_id, step))]
    gates = open_gates(proj) if proj else []
    return ToolResult(status="ok", summary=" ".join([f"project {proj.get('name', '?')}.",
                                                      *gates]),
                      data={**proj, "state": state, "before_delegating": gates,
                            **plan_readiness_report(ctx.store, proj, ctx.extra.get("registry")),
                            "completed_preparation_evidence": prepared,
                            "operation_argument_contracts": {op: argument_contract(op) for op in sorted(MATERIAL_METAL_OPERATIONS)}})


@qumus_tool("update_plan", "Replace Qumus's Task Plan (delegation order, not Device Guidance or Layout). "
            "Update changed targets before delegating. Include microscope search before selection "
            "when fresh flakes are needed. Every change is recorded and shown to the "
            "user. Example: {\"note\": \"start\", \"plan\": [{\"id\": \"s1\", \"title\": "
            "\"Check the bench\", \"owner\": \"eyes\"}, {\"id\": \"s2\", \"title\": "
            "\"Exfoliate graphene\", \"owner\": \"hands\"}]}. Step status is kept by code.",
            agents={"orchestrator"}, kind="state")
async def update_plan(args: UpdatePlanArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.agents.readiness import plan_readiness, plan_readiness_report

    with ctx.store._transaction():
        result = await _update_plan(args, ctx)
        if result.status != "ok":
            project = ctx.store.get_project(_need_project(ctx)) or {}
            result.data["current_plan"] = project.get("plan", [])
            result.data["current_plan_version"] = project.get("plan_version")
            result.data["current_plan_revision"] = project.get("plan_revision")
            draft = {**project, "plan": [step.model_dump() for step in args.plan]}
            result.data["readiness_errors"] = plan_readiness(ctx.store, draft, ctx.extra.get("registry"))
            result.data["current_execution"] = plan_readiness_report(ctx.store, project, ctx.extra.get("registry"))
        return result


async def _update_plan(args: UpdatePlanArgs, ctx: ToolContext) -> ToolResult:
    steps = [s.model_dump() for s in args.plan]
    given = {s["id"] for s in steps if s["id"]}
    used = {s["id"] for s in steps if s["id"]} | {
        s["id"] for revision in ctx.store.plan_history(_need_project(ctx)) for s in revision["plan"]}
    existing = (ctx.store.get_project(_need_project(ctx)) or {}).get("plan", [])
    for step in steps:
        matches = [old for old in existing if old["id"] not in given and old.get("owner") == step["owner"]
                   and old.get("title", "").casefold() == step["title"].casefold()]
        if not step["id"] and len(matches) == 1:
            step["id"] = matches[0]["id"]
            given.add(step["id"])
    n = 0
    for s in steps:  # Allocate missing identities without recycling historical IDs.
        while not s["id"]:
            n += 1
            s["id"] = f"s{n}" if f"s{n}" not in used else ""
        used.add(s["id"])
    # progress is tracked by code (gateway.delegation_*): a rewrite that leaves a
    # step's status at the default keeps the progress of the same step (same id
    # and owner, and the id was given or the title is unchanged)
    proj = ctx.store.get_project(_need_project(ctx)) or {"plan": []}
    if proj.get("plan_revision") and args.expected_version != proj["plan_version"]:
        return ToolResult(status="error", summary="Task Plan changed or expected_version is missing. Editing "
            "requires the current version. Read get_project_context, reconcile the plan and retry using the same cited "
            "user instruction; a stale or missing version does not require another user confirmation.",
            data={"error": "plan_version_conflict", "expected_version": args.expected_version,
                  "current_plan_version": proj["plan_version"], "next_action": "get_project_context"})
    from qumus2.records.reactor_requirements import plan_coverage_errors

    if errors := plan_coverage_errors(ctx.store, ctx.project_id, steps):
        return ToolResult(status="error", summary="Plan would drop established experiment requirements. Repair the submission without changing the scheme.",
                          data={"error": "experiment_requirements", "requirement_errors": errors})
    old_steps = {s["id"]: s for s in proj["plan"]}
    changes = {change.step_id: change for change in args.physical_changes}
    if len(changes) != len(args.physical_changes):
        return ToolResult(status="error", summary="Each physical step needs at most one cancellation or replacement.")
    for sid, change in changes.items():
        old = old_steps.get(sid, {})
        replacements = [s for s in steps if s["id"] in change.replacement_step_ids]
        if change.action == "cancel" and change.replacement_step_ids:
            return ToolResult(status="error", summary="A replacement uses action=supersede, not cancel. "
                "Retry with corrected physical_changes and the same instruction_id; its authority will be checked. "
                "This argument error does not require another user confirmation. No plan was saved.",
                data={"error": "replacement_action_required", "physical_changes": [
                    {**c.model_dump(), "action": "supersede" if c.replacement_step_ids else c.action}
                    for c in args.physical_changes]})
        if (old.get("owner") not in ("hands", "reactor") or old.get("status") in ("done", "skipped")
                or not _physical_change_authorized(change, ctx, old, replacements)
                or change.action == "cancel" and change.replacement_step_ids
                or change.action == "supersede" and (not replacements or sid in change.replacement_step_ids
                    or len(replacements) != len(set(change.replacement_step_ids))
                    or any(s["owner"] not in ("hands", "reactor") or s["status"] != "pending"
                           or s["id"] in old_steps for s in replacements))):
            return ToolResult(status="error", summary="Cannot validate this physical change. Check the cited "
                "instruction and replacement structure: retargeting uses new pending IDs, the same owner/operation "
                "and preserved completion conditions. For a future step without outcomes, replace only the known "
                "sample ID in its title. Correct arguments before asking the person to repeat an existing instruction. "
                "No plan was saved.", data={"error": "physical_change_not_validated", "step_id": sid,
                    "physical_change": change.model_dump(), "preserve_contract": completion_contract(old),
                    "original_title": old.get("title"), "replacement_status": "pending"})
        steps = [s for s in steps if s["id"] != sid]
        steps.append({**old, "status": "skipped", "physical_resolution": change.model_dump()})
    for step in steps:
        previous = old_steps.get(step["id"], {})
        same_contract = completion_contract(previous) == completion_contract(step)
        operation = step.get("operation")
        if step["owner"] in ("hands", "reactor") and operation is not None and not same_contract:
            known = executable_flow_names(step["owner"], ctx.extra.get("registry"))
            if operation not in known:
                return ToolResult(status="error", summary=f"Hardware operation {operation!r} on step "
                    f"{step['id']} is not executable by {step['owner']}. Use this role's exact flow name from registered_operations; split compound "
                    "work into named flow steps. Only future steps may leave operation null. No plan was saved.",
                    data={"step_id": step["id"], "owner": step["owner"], "operation": operation, "registered_operations": known})
        historical = (previous.get("status") in ("done", "skipped")
                      and same_contract)
        if historical or step["id"] in changes:
            continue
        if errors := argument_errors(ctx.store, ctx.project_id, step):
            return ToolResult(status="error", summary=argument_feedback(errors) + " No plan was saved.",
                data={"error": "material_metal_arguments", "argument_errors": errors,
                      "operation_argument_contract": argument_contract(operation)})
        if errors := operation_errors(ctx.store, ctx.project_id, step):
            return ToolResult(status="error", summary=errors[0]["message"] + " No plan was saved.",
                              data={"error": "material_metal_operation", "operation_contract_errors": errors})
        locations = invalid_flow_sample_locations(ctx.store, step) if not same_contract else []
        if locations:
            return ToolResult(status="error", summary="Task Plan has unsupported fixed storage destinations. "
                + locations[0]["guidance"] + " No plan was saved.",
                data={"error": "flow_location_contract", "location_contract_errors": locations})
        # Retain unchanged legacy output mistakes as non-executable drafts while
        # other steps are repaired. Readiness still refuses their delegation.
        errors = ([] if same_contract and operation in (*SEARCH_FLOWS, "exfoliate_chips")
                  else invalid_flow_sample_statuses(ctx.store, step))
        if errors:
            guidance = " ".join(dict.fromkeys(error["guidance"] for error in errors))
            return ToolResult(status="error", summary="Task Plan has invalid sample-role outcomes. "
                + guidance + " No plan was saved.",
                data={"outcome_contract_errors": errors})
    corrections = []
    for sid, old in old_steps.items():
        if old.get("owner") not in ("hands", "reactor") or old.get("status") in ("done", "skipped") or sid in changes:
            continue
        current = next((s for s in steps if s["id"] == sid), None)
        preserves = current is not None and preserves_obligation(old, current)
        repair = (correct_misassigned_flow_statuses(ctx.store, ctx.project_id, old, current)
                  if current and current["status"] == "pending" and not preserves else [])
        if current and not preserves and not repair:
            repair = correct_search_source_identity(ctx.store, ctx.project_id, old, current)
        if current and not preserves and not repair:
            repair = correct_material_metal_mechanics(ctx.store, ctx.project_id, old, current)
        if not current or current["status"] == "skipped" or not (preserves or repair):
            suggestions = []
            for candidate in steps:
                if candidate["id"] in old_steps or candidate["status"] != "pending":
                    continue
                for iid in dict.fromkeys(c.instruction_id for c in changes.values()):
                    proposal = PhysicalPlanChange(step_id=sid, action="supersede",
                        replacement_step_ids=[candidate["id"]], instruction_id=iid)
                    if _physical_change_authorized(proposal, ctx, old, [candidate]):
                        suggestions.append(proposal)
            if len(suggestions) == 1:
                return ToolResult(status="error", summary=f"Retargeting omitted physical_changes for {sid}. "
                    "The cited instruction already authorizes the equivalent target substitution. Retry with the "
                    "returned physical_changes; no new user confirmation is needed. No plan was saved.",
                    data={"error": "missing_physical_change", "step_id": sid,
                          "physical_changes": [c.model_dump() for c in [*changes.values(), suggestions[0]]]})
            return ToolResult(status="error", summary=f"Unfinished physical step {sid} must remain in the Task Plan "
                "with its owner and completion conditions. Refine or execute it; cancel/retarget only through "
                "physical_changes citing the person's explicit instruction. Deleting or renaming it is not completion. "
                "A model-written source-carrier/search typo can only be corrected from this project's unique successful "
                "exfoliation output, matching material/provenance and no conflicting user target. "
                "An exfoliation output-identity typo additionally needs this exact step's successful single-chip job and storage verification. "
                "Before submission, only its model-written carrier/exfoliated status may be removed while retaining that carrier's storage requirement. "
                "Model-authored material-metal operation/status mistakes can be corrected against original "
                "requirements, plan history and exact execution evidence; human targets remain protected.",
                data={"error": "physical_obligation_changed", "step_id": sid,
                    "previous_contract": completion_contract(old),
                    "proposed_contract": completion_contract(current) if current else None})
        corrections.extend({**item, "previous_plan_version": proj["plan_version"]} for item in repair)
    for step in steps:
        previous = next((old for old in proj["plan"] if old.get("id") == step.get("id")), {})
        same_contract = completion_contract(previous) == completion_contract(step)
        already_checked = previous.get("status") == "done" and same_contract
        if same_contract and previous.get("physical_resolution"):
            step["physical_resolution"] = previous["physical_resolution"]
        history = [*previous.get("outcome_corrections", []),
                   *(item for item in corrections if item["step_id"] == step["id"])]
        if history:
            step["outcome_corrections"] = history
        if (step["status"] == "pending" and previous and same_contract
                and (step["id"] in given or previous.get("title", "").lower() == step["title"].lower())):
            step["status"] = previous.get("status") or "pending"
        if step.get("status") == "done" and not already_checked:
            if step["owner"] in ("hands", "reactor") or previous.get("owner") in ("hands", "reactor"):
                return ToolResult(status="error", summary="New physical-step completion is written by "
                    "the delegation/job check, not update_plan. Keep this step pending and verify its "
                    "execution and declared outcomes; removing conditions cannot certify completion.")
            missing = unmet_sample_outcomes(ctx.store, ctx.extra.get("bench") or {}, step.get("sample_outcomes") or [])
            if missing:
                return ToolResult(status="error", summary="Cannot mark this step done: " + "; ".join(missing))
    try:
        rev = ctx.store.set_plan(_need_project(ctx), steps, author=ctx.agent, note=args.note,
                                expected_version=(args.expected_version if args.expected_version
                                                  is not None else proj.get("plan_version", 0)),
                                preserve_progress_ids=None)
    except (PlanConflict, ValueError) as exc:
        return ToolResult(status="error", summary=str(exc))
    if args.task_kind:
        ctx.store.update_project_state(_need_project(ctx), task_kind=args.task_kind)
    from qumus2.agents.readiness import plan_readiness_report

    saved = ctx.store.get_project(_need_project(ctx)) or {}
    readiness = plan_readiness_report(ctx.store, saved, ctx.extra.get("registry"))
    gates = open_gates(saved)
    return ToolResult(status="ok", summary=" ".join([f"Task Plan saved as revision {rev}.",
                                                      *( ["Corrected evidenced sample-role conditions; the affected "
                                                          "steps remain pending and need execution/outcome verification."]
                                                         if corrections else []),
                                                      *(["Next:"] if gates else []), *gates]),
                      data={"revision": rev, "before_delegating": gates, **readiness,
                            "outcome_corrections": corrections,
                            "normalized_operation_args": {s["id"]: normalize_arguments(s["operation"], s.get("operation_args") or {})[0]
                                for s in saved.get("plan", []) if s.get("operation") in MATERIAL_METAL_OPERATIONS},
                            "plan_version": (ctx.store.get_project(_need_project(ctx)) or {}).get("plan_version")})


@qumus_tool("save_project_status", "Save a short project status summary. For a final experiment, done first queues "
            "bounded two-stage cooling; wait for its job and call again. Completion needs both stages measured strictly below 40 C.",
            agents={"orchestrator"}, kind="state")
async def save_project_status(args: SaveStatusArgs, ctx: ToolContext) -> ToolResult:
    _need_project(ctx)
    from qumus2.records.experiment_completion import completion_error, requires_stage_cooling

    if args.status == "done":
        if error := completion_error(ctx.store, ctx.project_id):
            return ToolResult(**error)
        if requires_stage_cooling(ctx.store, ctx.project_id):
            return ToolResult(**ctx.jobs.complete_experiment(ctx.project_id, args.summary))
    ctx.store.update_project_state(_need_project(ctx), summary=args.summary, status=args.status)
    return ToolResult(status="ok", summary="status saved")


@qumus_tool("exclude_flake", "Record that the user vetoed a flake; it can no longer be "
            "selected or used.", agents={"orchestrator"}, kind="state", refs={"flake_id": "flake"})
async def exclude_flake(args: ExcludeFlakeArgs, ctx: ToolContext) -> ToolResult:
    pid = _need_project(ctx)
    proj = ctx.store.get_project(pid) or {"state": {}}
    excluded = sorted({*proj["state"].get("excluded_flakes", []), args.flake_id})
    was = proj["state"].get("selected_flakes", [])
    selected = [f for f in was if f != args.flake_id]
    fields: dict[str, Any] = {"excluded_flakes": excluded, "selected_flakes": selected}
    if len(selected) != len(was):  # a selected flake was vetoed: the set must be redone
        fields.update(selected_flakes=[], design_accepted=False)
    ctx.store.update_project_state(pid, **fields)
    ctx.store.upsert_sample(args.flake_id, "flake", status="vetoed",
                            props={"veto_reason": args.reason})
    return ToolResult(status="ok", summary=f"{args.flake_id} excluded"
                      + ("; the selection must be redone" if len(selected) != len(was) else ""),
                      data={"excluded_flakes": excluded,
                            "selected_flakes": fields["selected_flakes"]})


class ArtifactArgs(BaseModel):
    name: str = Field(description="e.g. bottom_electrode_chip, stack_report")
    role: str = ""
    status: Literal["pending", "ready", "done", "failed"] = "pending"
    note: str = ""


@qumus_tool("record_project_artifact", "Register or update a project artifact, e.g. something "
            "waiting on the cleanroom (v1 register/update_project_artifact).",
            agents={"orchestrator"}, kind="state")
async def record_project_artifact(args: ArtifactArgs, ctx: ToolContext) -> ToolResult:
    pid = _need_project(ctx)
    proj = ctx.store.get_project(pid) or {"state": {}}
    arts = dict(proj["state"].get("artifacts", {}))
    arts[args.name] = {**arts.get(args.name, {}), **args.model_dump(), "ts": ctx.store.clock.now()}
    ctx.store.update_project_state(pid, artifacts=arts)
    return ToolResult(status="ok", summary=f"artifact {args.name}: {args.status}")


class FilmArgs(BaseModel):
    film_type: Literal["PC", "mica"] = "PC"
    stamp_id: str = Field(description="The stamp the person put a new contact support on, e.g. stamp_01")
    instruction_id: str = Field(description="Id of the person's message that says so")


@qumus_tool("report_new_film",
            "A person reports replacing PC or mica contact support on a used/torn stamp: record the explicit material, "
            "so the stamp counts as fresh again. Cite their message.",
            agents={"orchestrator"}, kind="state", requires=("person_reported_film",))
async def report_new_film(args: FilmArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.stamps import FilmReportError
    from qumus2.records.stamps import report_new_film as record

    try:
        rec = record(ctx.store, ctx.extra.get("bench") or {}, args.stamp_id, by=ctx.agent,
                     via="chat", instruction_id=args.instruction_id, film_type=args.film_type)
    except FilmReportError as e:
        return ToolResult(status="error", summary=str(e))
    return ToolResult(status="ok", summary=f"{args.stamp_id} has new {args.film_type} support and is fresh "
                      f"again (was {rec['previous_status']})", data=rec)


class PlanRefinement(BaseModel):
    step_id: str = Field(description="Existing stable ID; never its display position.")
    fields: dict[str, Any] = Field(description="Only changed PlanStep fields; id/owner/status cannot be rewritten.")


class PlanAddition(PlanStep):
    before_step_id: str | None = Field(None, description="Insert before this existing stable step ID in the same atomic save. "
        "Use at most one of before_step_id/after_step_id; never reference a new addition. Additions at the same anchor keep input order.")
    after_step_id: str | None = Field(None, description="Insert after this existing stable step ID in the same atomic save. "
        "Use at most one placement field. With neither field, append after all existing steps.")


class RefinePlanArgs(BaseModel):
    expected_revision: int = Field(ge=1, description="Structural plan_revision last read; progress changes are merged automatically.")
    updates: list[PlanRefinement] = Field(default_factory=list)
    additions: list[PlanAddition] = Field(default_factory=list, description="New steps with empty IDs; code assigns never-used IDs. "
        "Place prerequisite steps with before_step_id or after_step_id referencing an existing step so the complete sequence is valid in one save.")
    order: list[str] = Field(default_factory=list, description="Optional full order of existing IDs only; never predict IDs for additions. "
        "Existing steps are ordered first, then additions are inserted at their before_step_id/after_step_id anchors atomically. "
        "Do not append prerequisites and plan to reorder later: the complete sequence must pass validation before anything is saved.")
    note: str


@qumus_tool("refine_plan", "Refine existing steps by stable ID, insert newly numbered steps before/after existing IDs and merge the latest progress atomically. "
            "Use plan_revision, not plan_version; a progress-only change is merged, a structural conflict is returned. "
            "This cannot revise established experiment requirements or cancel obligations.", agents={"orchestrator"}, kind="state")
async def refine_plan(args: RefinePlanArgs, ctx: ToolContext) -> ToolResult:
    with ctx.store._transaction():
        project = ctx.store.get_project(_need_project(ctx))
        if project["plan_revision"] != args.expected_revision:
            return ToolResult(status="error", summary="Structural Task Plan changed. Merge with the returned current plan; preserve the experiment.",
                data={"error": "plan_revision_conflict", "current_plan": project["plan"],
                      "plan_revision": project["plan_revision"], "plan_version": project["plan_version"]})
        steps = {s["id"]: dict(s) for s in project["plan"]}
        seen = set()
        for update in args.updates:
            if update.step_id not in steps or update.step_id in seen or {"id", "owner", "status"} & update.fields.keys():
                return ToolResult(status="error", summary="Refine each existing identity once; id, owner and execution status are not editable here.")
            if set(update.fields) - PlanStep.model_fields.keys():
                return ToolResult(status="error", summary="Unknown PlanStep fields in refinement.")
            seen.add(update.step_id)
            steps[update.step_id].update(update.fields)
        order = args.order or list(steps)
        counts = Counter(order)
        unknown = list(dict.fromkeys(sid for sid in order if sid not in steps))
        missing = [sid for sid in steps if sid not in counts]
        duplicate = [sid for sid, count in counts.items() if count > 1]
        addition_ids = [{"index": i, "id": step.id} for i, step in enumerate(args.additions) if step.id]
        placement_errors = []
        for i, addition in enumerate(args.additions):
            anchors = {key: getattr(addition, key) for key in ("before_step_id", "after_step_id")
                       if getattr(addition, key) is not None}
            if len(anchors) > 1 or any(sid not in steps for sid in anchors.values()):
                placement_errors.append({"index": i, **anchors,
                    "reason": "Use at most one placement field, referencing an existing stable step ID."})
        if unknown or missing or duplicate or addition_ids or placement_errors:
            return ToolResult(status="error", summary="Order must contain every existing stable ID exactly once, excluding additions. "
                "Leave added step IDs empty; code allocates new identities. Use before_step_id or after_step_id on additions "
                "to insert prerequisites at existing IDs in the same save. No plan was saved.", data={"error": "invalid_refinement_ids",
                    "existing_ids": list(steps), "unknown_ids": unknown, "missing_ids": missing,
                    "duplicate_ids": duplicate, "addition_id_errors": addition_ids, "placement_errors": placement_errors,
                    "plan_revision": project["plan_revision"], "plan_version": project["plan_version"]})
        before, after, appended = {}, {}, []
        for addition in args.additions:
            step = PlanStep.model_validate(addition.model_dump(exclude={"before_step_id", "after_step_id"}))
            if addition.before_step_id is not None:
                before.setdefault(addition.before_step_id, []).append(step)
            elif addition.after_step_id is not None:
                after.setdefault(addition.after_step_id, []).append(step)
            else:
                appended.append(step)
        proposed = []
        for sid in order:
            proposed.extend([*before.get(sid, []), PlanStep.model_validate(steps[sid]), *after.get(sid, [])])
        proposed.extend(appended)
        result = await update_plan(UpdatePlanArgs(plan=proposed,
            note=args.note, expected_version=project["plan_version"]), ctx)
        if result.status == "ok":
            result.data["plan"] = ctx.store.get_project(ctx.project_id)["plan"]
            result.data["added_steps"] = [{"id": s["id"], "title": s["title"]}
                for s in result.data["plan"] if s["id"] not in steps]
        else:
            result.summary += (" No plan was saved and no new IDs were allocated. Resubmit all required edits and additions together; "
                "place prerequisites with before_step_id/after_step_id on additions rather than appending them after their dependents.")
            result.data["insertion_guidance"] = {"existing_ids": list(steps),
                "placement_fields": ["before_step_id", "after_step_id"],
                "same_anchor_order": "input order", "unanchored_additions": "append",
                "save_mode": "All edits, placements and existing-ID order are validated and saved atomically."}
        return result


class StampSupportArgs(BaseModel):
    stamp_id: str
    film_type: Literal["PC", "mica"]
    instruction_id: str


@qumus_tool("record_stamp_support", "Record the person's known stamp contact film (PC or mica) without inferring it from images. "
            "This records identity only; it does not refresh a used/torn support or certify a release recipe.",
            agents={"orchestrator"}, kind="state", refs={"stamp_id": "stamp"})
async def record_stamp_support(args: StampSupportArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.stamps import FilmReportError, record_support

    try:
        result = record_support(ctx.store, ctx.extra.get("bench") or {}, args.stamp_id,
                                args.film_type, instruction_id=args.instruction_id, project_id=ctx.project_id)
    except FilmReportError as exc:
        return ToolResult(status="error", summary=str(exc))
    return ToolResult(status="ok", summary=f"Known {args.film_type} contact film recorded on {args.stamp_id}.", data=result)


class MaterialSourceArgs(BaseModel):
    carrier_id: str
    material: str
    instruction_id: str


@qumus_tool("record_material_source", "Record a person's material-tape identity or replacement in storage, citing their message. "
            "Like record_stamp_support, this updates reported properties only, not physical observation or location.",
            agents={"orchestrator"}, kind="state", refs={"carrier_id": "carrier"})
async def record_material_source(args: MaterialSourceArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.materials import record_material_source as record

    try:
        data = record(ctx.store, ctx.extra.get("bench") or {}, args.carrier_id, args.material,
                      instruction_id=args.instruction_id, project_id=ctx.project_id)
        return ToolResult(status="ok", summary="Human-reported material source saved with history.", data=data)
    except ValueError as exc:
        return ToolResult(status="error", summary=str(exc))
