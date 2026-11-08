"""One read-only prerequisite report shared by plan editing and delegation."""
from __future__ import annotations

from typing import Any

from qumus2.agents.policy import _advisor_first
from qumus2.records.material_metal_arguments import argument_errors
from qumus2.records.material_metal_plan import operation_errors
from qumus2.records.plan_completion import (
    executable_flow_names,
    invalid_flow_sample_locations,
    invalid_flow_sample_statuses,
)

RECIPE_FIELDS = ("target_temp_c", "dwell_s", "num_massages", "press_depth_mm", "peel_speed", "peel_angle_deg")


def step_readiness(store: Any, project: dict, step: dict, registry: Any, task: str = "") -> list[dict]:
    errors = []

    def add(rule, message, **details):
        errors.append({"step_id": step.get("id"), "rule": rule, "message": message, **details})

    owner = step.get("owner")
    if owner not in ("hands", "reactor"):
        return errors
    if not project.get("state", {}).get("eyes_checked"):
        add("eyes_first", "Ask Eyes to inspect the bench and reconcile inventory before hardware work.")
    if owner == "hands" and (why := _advisor_first(project, task or f"{step.get('id')}: {step.get('title', '')}")):
        add("advisor_first", why)
    if owner == "reactor" and not step.get("reactor_outcome"):
        add("reactor_outcome_required", "Declare reactor_outcome with chip_id, disposition, explicit temperature/flow targets and rationale.")
    if owner == "hands" and not step.get("sample_outcomes"):
        add("sample_outcomes_required", "Declare sample_outcomes for known samples, including their required final storage/status.")
    known = executable_flow_names(owner, registry)
    if step.get("operation") not in known:
        add("physical_operation_required", f"Declare one registered operation executable by {owner}: " + ", ".join(known) + ".", registered_operations=known)
    for error in invalid_flow_sample_locations(store, step):
        add("flow_location_contract", error["guidance"])
    errors.extend(operation_errors(store, project.get("id"), step))
    errors.extend(argument_errors(store, project.get("id"), step, phase="delegate"))
    for error in invalid_flow_sample_statuses(store, step):
        rule = "search_outcome_contract" if step.get("operation") in ("search_chip", "revisit_chip") else "flow_status_contract"
        add(rule, error["guidance"], sample_id=error["sample_id"])
    if step.get("operation") == "exfoliate_chips":
        recipe = step.get("operation_args") or {}
        missing = [key for key in ("material", *RECIPE_FIELDS) if recipe.get(key) is None]
        if missing:
            add("recipe_incomplete", "Ask Advisor to supply every missing recipe value before delegating to Hands.", missing_fields=missing)
        elif registry:
            from qumus2.agents.policy import check_pre_tool_use
            decision = check_pre_tool_use(registry, store, agent="hands", tool="exfoliate_chips",
                                         args=recipe, project_id=project.get("id"))
            if not decision.allow:
                add(decision.rule, decision.reason)
    return errors


def plan_readiness(store: Any, project: dict, registry: Any) -> list[dict]:
    return [error for step in project.get("plan", []) if step.get("status") not in ("done", "skipped")
            for error in step_readiness(store, project, step, registry)]


def plan_readiness_report(store: Any, project: dict, registry: Any) -> dict:
    """Separate the next delegation's prerequisites from later draft gaps."""
    errors = plan_readiness(store, project, registry)
    pending = [s for s in project.get("plan", []) if s.get("status") not in ("done", "skipped")]
    step = pending[0] if pending else None
    current = [e for e in errors if step and e["step_id"] == step["id"]]
    return {"readiness_errors": errors, "all_steps_ready": not errors,
            "executable_scope": "next_pending_step", "executable": bool(step and not current),
            "next_pending_step": {"step_id": step["id"], "owner": step["owner"],
                "operation": step.get("operation"), "readiness_errors": current} if step else None,
            "readiness_guidance": "Delegation checks only the named step. Later draft gaps do not block a ready earlier step. "
                "For exfoliation, preserve the source carrier with in_storage=true; only the receiving chip becomes exfoliated. "
                "Bind each future search to the actual receiving chip returned by that job; do not guess chip IDs, "
                "delete searches, or ask a person merely because future outputs do not exist yet. "
                "Ready here means planning prerequisites; execution still checks device and sample state."}
