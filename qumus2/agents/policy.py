"""PreToolUse policy (spec 6.3): the code check before every tool call.

Pure functions over (agent, tool, args, store) so they are unit-testable and
shared by the SDK hook and the scripted runtime. The tool function and the
SafetyGate are the first line of defence; this is the second.

Checks, in order:
  1. tool exists and this agent may use it          rule = "permission"
  2. args validate against the tool's model         rule = "schema"
  3. referenced ids exist in the sample ledger      rule = "refs"
  4. cross-tool rules named in ToolSpec.requires    rule = <rule name>

Delegations by the orchestrator (the SDK's Agent tool) are checked by
check_delegation: a plan first (rule = "plan_first"), the eyes look at the
bench before the hands are sent (rule = "eyes_first"), as v1's prompt asked,
and the advisor looks up a material's experience before the hands are sent
to exfoliate it (rule = "advisor_first", 2026-10-22: real runs wasted two or
three refused exfoliate_chips calls per task because the hands, who cannot
call the advisor, retried). "Looked" means a successful inspect_bench in this
project (tools/inventory.py); a hands job that did not succeed clears it
(ToolGateway.check).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from qumus2.agents.tooling import ToolRegistry, short_name
from qumus2.runtime.store import RunStore


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    rule: str = ""
    args: Any = None  # validated args model when allowed


# rule name -> fn(store, project_id, args) -> denial reason or None
Rule = Callable[[RunStore, str | None, Any], str | None]


def _flakes_selected(store: RunStore, project_id: str | None, args: Any) -> str | None:
    proj = store.get_project(project_id) if project_id else None
    if not proj or not proj["state"].get("selected_flakes"):
        return "no flakes selected for this project yet; run flake selection first"
    return None


def _design_accepted(store: RunStore, project_id: str | None, args: Any) -> str | None:
    proj = store.get_project(project_id) if project_id else None
    if not proj or not proj["state"].get("design_accepted"):
        return "no accepted device design for this project yet"
    return None


def _not_vetoed(store: RunStore, project_id: str | None, args: Any) -> str | None:
    proj = store.get_project(project_id) if project_id else None
    excluded = set(proj["state"].get("excluded_flakes", [])) if proj else set()
    ids = {getattr(args, "flake_id", None), *getattr(args, "flake_ids", [])} - {None}
    hit = ids & excluded
    return f"flake(s) {sorted(hit)} were vetoed by the user" if hit else None


def _stack_matches_selection(store: RunStore, project_id: str | None, args: Any) -> str | None:
    from qumus2.records.geometry import selection_source_snapshot

    proj = store.get_project(project_id) if project_id else None
    selected = set(proj["state"].get("selected_flakes", [])) if proj else set()
    evidence = proj["state"].get("selection_source_evidence") if proj else None
    if evidence and evidence != selection_source_snapshot(store, list(selected)):
        return "Selected source images/geometry changed; ask Picker to reassess the current specimen."
    if set(getattr(args, "flake_ids", [])) != selected:
        return (f"flake_ids must be exactly the code-checked selection {sorted(selected)}; "
                "a different set needs a new selection")
    return None


def _stack_matches_target(store: RunStore, project_id: str | None, args: Any) -> str | None:
    from qumus2.records.geometry import guidance_binding

    state = ((store.get_project(project_id) or {}).get("state", {}) if project_id else {})
    if (state.get("design_binding") or {}).get("version") == "device_design_v1":
        from qumus2.records.device_design import validate_design_current

        try:
            design = validate_design_current(store, project_id)
        except (ValueError, OSError) as exc:
            return f"Accepted KLayout design is no longer executable: {exc}"
        if args.target_chip_id != design.target_chip_id or tuple(args.flake_ids) != design.pickup_order:
            return "Stack target and hBN/Gr pickup order must match the accepted KLayout design"
        return None
    try:
        binding = guidance_binding(store, state.get("device_plan") or {})
    except ValueError as exc:
        return str(exc)
    if binding["target_chip_id"] != args.target_chip_id:
        return f"Stack target must match accepted Device Guidance target {binding['target_chip_id']}; revalidate a changed target"
    if state.get("design_binding") != binding:
        return "Layout is not bound to the current target and Device Guidance; submit a validated Layout again"
    return None


def _stack_authorized(store: RunStore, project_id: str | None, args: Any) -> str | None:
    """One transfer entry: preserve the target's accepted task contract."""
    from qumus2.records.geometry import selection_source_snapshot
    from qumus2.records.reactor_requirements import established_requirements, input_errors

    req = established_requirements(store, project_id)
    if req:
        state = store.get_project(project_id)["state"]
        selection = state.get("reactor_selection") or {}
        if selection.get("chip_id") != args.target_chip_id or selection.get("confirmed_ids") != args.flake_ids:
            return "Transfer must use the exact confirmed stack and target for the current experiment."
        if selection.get("source_evidence") != selection_source_snapshot(store, args.flake_ids):
            return "Confirmed source evidence changed; ask Picker to confirm the current observed combination."
        if errors := input_errors(store, req, args.flake_ids, carrier_id=args.target_chip_id, require_assessment=True):
            return "; ".join(errors)
        return _reactor_ready(store, project_id, args) or _not_vetoed(store, project_id, args)
    for rule in (_flakes_selected, _design_accepted, _stack_matches_selection, _stack_matches_target, _not_vetoed):
        if reason := rule(store, project_id, args):
            return reason
    return None


def _recovery_budget(store: RunStore, project_id: str | None, args: Any) -> str | None:
    from qumus2.agents.tooling import ToolContext
    from qumus2.agents.tools.recovery import budget_reason

    ctx = ToolContext(store=store, jobs=None, agent="", project_id=project_id)  # type: ignore[arg-type]
    return budget_reason(ctx, getattr(args, "job_id", None))


def _experience_checked(store: RunStore, project_id: str | None, args: Any) -> str | None:
    """Look before you exfoliate: the advisor reads the material's experience
    (get_experience or query_run_records) in this project first (spec 10)."""
    from qumus2.core.materials import normalize_material_code

    proj = store.get_project(project_id) if project_id else None
    if proj is None:
        return None
    mat = normalize_material_code(getattr(args, "material", None))
    if mat in proj["state"].get("experience_checked", []):
        return None
    return (f"The advisor has not looked up what the lab knows about {mat} yet. Do not "
            "retry and do not call a person: you cannot look it up. End your turn and "
            f"report to the orchestrator that the advisor must look up experience for {mat} "
            "first (get_experience: the best known recipe and the spare chips for exploring).")


def _runs_reviewed(store: RunStore, project_id: str | None, args: Any) -> str | None:
    """Real lab only: finished, searched exfoliation runs of this material are
    reviewed before the next one, so each result becomes experience."""
    from qumus2.records.review import unreviewed_runs

    pending = unreviewed_runs(store, material=getattr(args, "material", None))
    if not pending:
        return None
    return (f"{len(pending)} finished experiment(s) of this material are not reviewed yet "
            f"({', '.join(r['id'] for r in pending[:5])}): ask the advisor to run "
            "review_runs first.")


def _exploration_budget(store: RunStore, project_id: str | None, args: Any) -> str | None:
    """Secure first, explore with the rest (Jialei, 2026-10-21)."""
    from qumus2.records.budget import exploration_budget
    from qumus2.records.experience import ExperienceBook
    from qumus2.records.outcomes import changed_keys

    purpose = getattr(args, "purpose", "secure")
    params = args.model_dump()
    if purpose == "user":
        ins = store.get_instruction(args.instruction_id) if args.instruction_id else None
        if ins is None or ins["source"] != "ui":
            return ("purpose 'user' needs instruction_id: the id of the person's message that "
                    "asked for these parameters (shown with each USER INSTRUCTION)")
        return None
    base = ExperienceBook(store).baseline(args.material)
    if purpose == "secure":
        diff = changed_keys(base["recipe"], params) if base and base["enforced"] else []
        if diff:
            return (f"chips the task needs use the confirmed best recipe {base['recipe']} "
                    f"(experience {base['experience_id']}); {diff} differ. To try a change, "
                    "submit purpose 'explore' on spare bare chips.")
        return None
    if not args.hypothesis.strip():
        return "an explore run states its hypothesis: what you expect to change and why"
    if base:
        diff = changed_keys(base["recipe"], params)
        if len(diff) != 1:
            return (f"an explore run changes exactly one recipe parameter from the baseline "
                    f"{base['recipe']} ({base['status']}, experience "
                    f"{base['experience_id']}); this one changes {diff or 'none'}")
        if args.changed and sorted(args.changed) != diff:
            return f"changed says {sorted(args.changed)} but the recipe differs in {diff}"
    b = exploration_budget(store, project_id, args.material)
    if not b["evaluated"]:
        return b["note"]
    n = len(args.chip_ids) or args.num_chips
    if n > b["max_explore_chips_per_run"]:
        return f"an explore run uses at most {b['max_explore_chips_per_run']} chip(s)"
    if n > b["spare_chips"]:
        return (f"only {b['spare_chips']} spare bare chip(s): of the {b['bare_chips']} bare "
                f"chips the task still needs {b['task_chips_needed']} with the best known "
                f"recipe ({b['flakes']['missing']} good flake(s) missing, about "
                f"{b['good_flakes_per_chip']['usable_per_chip']} per chip). Secure the task "
                "first; explore with what is left.")
    return None


def _person_reported_film(store: RunStore, project_id: str | None, args: Any) -> str | None:
    """A new PC film is recorded only on a person's word: the cited message is
    one a person sent (from the UI, in this project) and it names the stamp."""
    from qumus2.records.stamps import names_stamp

    ins = store.get_instruction(args.instruction_id)
    if ins is None or ins["source"] != "ui" or ins["kind"] not in ("message", "plan_change"):
        return (f"{args.instruction_id} is not a message a person sent: a new film is recorded "
                "only when a person reports it (their message id, or the Lab page)")
    if project_id and ins["project_id"] not in (None, project_id):
        return f"{args.instruction_id} belongs to another project"
    if not names_stamp(ins["text"], args.stamp_id):
        return (f"the person's message {args.instruction_id} does not name {args.stamp_id}; "
                "ask them which stamp got the new film")
    import re

    film = getattr(args, "film_type", "PC")
    if not re.search(r"(?<![A-Za-z])" + film + r"(?![A-Za-z])", ins["text"], re.I):
        return "The replacement report must explicitly name the new PC/mica support material."
    if not re.search(r"\b(new|replaced?|refilm(?:ed)?)\b|换|新", ins["text"], re.I):
        return "An identity report alone does not refresh a stamp; cite an actual support replacement."
    return None


def _reactor_ready(store: RunStore, project_id: str | None, args: Any) -> str | None:
    project = store.get_project(project_id) if project_id else None
    if project and not project["state"].get("eyes_checked"):
        return "Before reactor hardware jobs, ask Eyes to inspect the bench and dedicated inventory."
    return None


RULES: dict[str, Rule] = {
    "stack_matches_target": _stack_matches_target,
    "stack_authorized": _stack_authorized,
    "reactor_ready": _reactor_ready,
    "person_reported_film": _person_reported_film,
    "flakes_selected": _flakes_selected,
    "design_accepted": _design_accepted,
    "not_vetoed": _not_vetoed,
    "stack_matches_selection": _stack_matches_selection,
    "recovery_budget": _recovery_budget,
    "experience_checked": _experience_checked,
    "runs_reviewed": _runs_reviewed,
    "exploration_budget": _exploration_budget,
}


def check_pre_tool_use(registry: ToolRegistry, store: RunStore, *, agent: str, tool: str,
                       args: dict[str, Any], project_id: str | None) -> Decision:
    spec = registry.get(tool)
    name = short_name(tool)
    if spec is None:
        return Decision(False, f"unknown tool {name}", "permission")
    if agent not in spec.agents:
        return Decision(False, f"agent {agent!r} may not use {name}", "permission")
    try:
        parsed = spec.args_model.model_validate(args)
    except ValidationError as e:
        return Decision(False, f"invalid arguments: {e.errors(include_url=False)}", "schema")
    for field_name, kind in spec.refs.items():
        val = getattr(parsed, field_name, None)
        for v in val if isinstance(val, list) else [val]:
            if v is None:
                continue
            s = store.get_sample(v)
            if s is None or s["kind"] != kind:
                return Decision(False, f"{field_name}={v!r} is not a known {kind}", "refs")
    for rule in spec.requires:
        reason = RULES[rule](store, project_id, parsed)
        if reason:
            return Decision(False, reason, rule)
    return Decision(True, args=parsed)


#: a delegation text that sends the hands to exfoliate (rule advisor_first)
_EXFOLIATION = re.compile(r"exfoliat|peel", re.IGNORECASE)


def _advisor_first(proj: dict[str, Any], task: str) -> str | None:
    """The advisor must have looked up every material the hands are sent to
    exfoliate; with no material named, any look-up in the project will do
    (the hands' own exfoliate_chips rule checks the material it is given)."""
    from qumus2.core.materials import materials_in_text

    named_step = re.match(r"\s*\[?([A-Za-z]*\d+[A-Za-z0-9_.-]*)\]?\s*[:\-\]]?\s", task or "")
    step = next((s for s in proj.get("plan", []) if named_step and s.get("id") == named_step.group(1)
                 and s.get("owner") == "hands"), {})
    operation = step.get("operation")
    # Structured intent takes precedence. Legacy tasks retain a conservative
    # positive-clause fallback; prohibited alternatives are not prerequisites.
    clauses = re.split(r"[.!?;\n]+|\b(?:but|then|instead)\b", task, flags=re.I)
    requested = any(_EXFOLIATION.search(clause) and not re.search(
        r"(?:\bdo not\b|\bdon't\b|\bmust not\b|\bnever\b|\bno\b|\bwithout\b|\bavoid\b)"
        r"[^.!?;]*\b(?:exfoliat\w*|peel\w*)", clause, re.I) for clause in clauses)
    needs_advice = operation == "exfoliate_chips" if operation else requested
    if not needs_advice:
        return None
    checked = proj["state"].get("experience_checked", [])
    named = materials_in_text(task)
    missing = [m for m in named if m not in checked] if named else (
        [] if checked else ["the material"])
    if not missing:
        return None
    return ("Delegate to the advisor first: it must look up the lab's experience for "
            f"{', '.join(missing)} (get_experience: the best known recipe and the spare chips "
            "for exploring) before the hands exfoliate. The hands cannot do that look-up. "
            "Then delegate this step to the hands again.")


def open_gates(proj: dict[str, Any]) -> list[str]:
    """The delegation rules a project has not met yet, as one line each, put
    into get_project_context and update_plan results so the orchestrator meets
    them in order instead of learning them from refusals (every real run of
    the overnight acceptance hit two or three: ~$0.05-0.1 a round trip)."""
    out = []
    if not proj.get("plan"):
        out.append("No plan yet: write it with update_plan before any delegation "
                   "(plan_first).")
    state = proj.get("state", {})
    if not state.get("eyes_checked"):
        out.append("Before any hardware delegation to the hands, have the eyes look at "
                   "the bench with inspect_bench and check the inventory (eyes_first; "
                   "again after a hardware job did not succeed).")
    if not state.get("experience_checked"):
        out.append("Before the hands exfoliate, have the advisor look up the lab's "
                   "experience for each material (advisor_first).")
    return out


def check_delegation(store: RunStore, project_id: str | None, to: str,
                     task: str = "") -> Decision:
    """Orchestrator -> subagent. Rules v1 only had as "always" in its prompt.
    `task` is the delegation text (the Agent tool's description and prompt)."""
    proj = store.get_project(project_id) if project_id else None
    if proj is None:
        return Decision(True)
    if not proj["plan"]:
        return Decision(False, "Write the plan with update_plan first (steps and which agent "
                               "does each), then delegate the first step.", "plan_first")
    if to == "hands" and not proj["state"].get("eyes_checked"):
        return Decision(False, "Before a hardware specialist moves anything, ask Eyes to look at the "
                               "bench with inspect_bench and check the inventory (what is "
                               "loaded, which chips are where). Needed once per project and "
                               "again after a hardware job did not succeed.", "eyes_first")
    if to == "hands" and (why := _advisor_first(proj, task)):
        return Decision(False, why, "advisor_first")
    return Decision(True)
