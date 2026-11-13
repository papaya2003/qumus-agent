"""ToolGateway: the single path from any agent runtime to a tool.

    pre()      PreToolUse  - policy check; logs tool.call or tool.denied
    delegate() PreToolUse on the SDK's Agent tool - plan first, eyes before hands
    delegation_started/finished, turn_finished - plan step status, set by code
               after checking the records of what the subagent did
    execute()  the tool function itself
    post()     PostToolUse - logs tool.result; returns pending user
               instructions to append for the model (spec 11.4)

The SDK runtime calls pre/post from its hooks and execute from the MCP tool
wrapper; the scripted runtime calls `call()`, which does all three.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from qumus2.agents.policy import Decision, check_delegation, check_pre_tool_use
from qumus2.agents.roster import ORCHESTRATOR
from qumus2.agents.tooling import ToolContext, ToolRegistry, ToolResult, short_name
from qumus2.contract.errors import ContractError
from qumus2.core.errors import QumusError
from qumus2.interaction.inbox import Inbox
from qumus2.records.plan_completion import (
    cancelled_job_retired,
    completion_contract,
    linked_job_evidence,
    linked_physical_jobs,
    unmet_sample_outcomes,
)
from qumus2.records.search_outcomes import SEARCH_FLOWS, search_target_error
from qumus2.runtime.jobs import JobService
from qumus2.runtime.store import RunStore

#: a delegation names its plan step first: "s3: select flakes", "[s3] ...", "s3 - ..."
_STEP_REF = re.compile(r"\s*\[?([A-Za-z]*\d+[A-Za-z0-9_.-]*)\]?\s*[:\-\]]?\s")

#: what the picker / designer must have had accepted for its step to be done
DELIVERABLES = {"picker": ("submit_flake_selection",),
                "designer": ("submit_design", "create_device_design", "assess_relaxation")}
_GOOD = ("ok", "corrected")
#: job states after which the bench may not be what the ledger says
_JOB_BAD = ("failed", "cancelled", "parked", "waiting_human")
#: words in a hands step title that name the flow it is about (plan steps are
#: marked by the jobs that ran, not all on the delegated step)
_FLOW_WORDS = {"exfoliate_chips": ("exfoliat",),
               "search_chip": ("search", "scan", "catalog"),
               "revisit_chip": ("revisit", "photograph", "re-photograph", "search"),
               "stack_device": ("stack", "assembl"),
               "place_chip_on_transfer_stage": ("place", "load", "transfer stage")}


@dataclass
class _Delegation:
    to: str
    step: str | None
    calls: list[dict[str, Any]] = field(default_factory=list)  # tool, status, summary, data
    sample_outcomes: list[dict[str, Any]] = field(default_factory=list)
    reactor_outcome: dict[str, Any] | None = None
    contracts: dict[str, dict] = field(default_factory=dict)
    started_event: int = 0


class ToolGateway:
    def __init__(self, registry: ToolRegistry, store: RunStore, jobs: JobService,
                 project_id: str | None = None, bench: dict[str, Any] | None = None,
                 stations: dict[str, Any] | None = None) -> None:
        self.registry, self.store, self.jobs = registry, store, jobs
        self.project_id = project_id
        self.bench = bench
        #: stations outside contract v1 that tools drive directly (reactor Lab Console)
        self.stations = dict(stations or {})
        self.inbox = Inbox(store)
        self._open: dict[str, _Delegation] = {}  # delegation key -> what it is doing
        self._facts: dict[str, str] = {}  # key -> facts of a checked delegation, once

    def pre(self, agent: str, tool: str, args: dict[str, Any],
            tool_use_id: str | None = None) -> Decision:
        name = short_name(tool)
        d = check_pre_tool_use(self.registry, self.store, agent=agent, tool=name, args=args,
                               project_id=self.project_id)
        if d.allow and agent == "hands" and name == "exfoliate_chips":
            from qumus2.agents.readiness import RECIPE_FIELDS

            active = next((s for s in self._plan() if s.get("status") == "running" and s.get("owner") == "hands"
                           and s.get("operation") == "exfoliate_chips" and s.get("operation_args")), None)
            if active and any(args.get(k) != active["operation_args"].get(k) for k in ("material", *RECIPE_FIELDS)):
                d = Decision(False, "Use the complete agreed Advisor recipe in the named step; do not fill or change it during execution.", "recipe_mismatch")
        if d.allow and agent == "hands" and name == "search_chip":
            active = next((s for s in self._plan() if s.get("status") == "running" and s.get("owner") == "hands"
                           and s.get("operation") in SEARCH_FLOWS and s.get("sample_outcomes")), None)
            if active and (why := search_target_error(self.store, active, args.get("chip_id"))):
                d = Decision(False, why, "search_target_mismatch")
        contract_name, contract_args = name, args
        if name == "stack_device":
            from qumus2.records.reactor_requirements import established_requirements

            if established_requirements(self.store, self.project_id):
                contract_name = "prepare_material_metal"
                contract_args = {"chip_id": args.get("target_chip_id"), "flake_ids": args.get("flake_ids"),
                                 "stamp_id": args.get("stamp_id")}
        if d.allow and contract_name in ("prepare_material_metal", "reactor_material_metal"):
            from qumus2.records.material_metal_arguments import argument_feedback, submission_errors

            contracts = [{**item.contracts[item.step], "id": item.step}
                         for item in self._open.values() if item.to == agent
                         and item.step in item.contracts and item.contracts[item.step].get("operation") == contract_name]
            if not contracts:
                contracts = [s for s in self._plan() if s.get("owner") == agent
                             and s.get("status") == "running" and s.get("operation") == contract_name]
            if len(contracts) > 1:
                d = Decision(False, "Resolve the ambiguous active material-metal step before submission.", "material_metal_arguments")
            elif contracts and (errors := submission_errors(self.store, self.project_id, contracts[0], contract_args, self.bench)):
                d = Decision(False, argument_feedback(errors), "material_metal_arguments", {"argument_errors": errors})
        if d.allow:
            self.store.emit("tool.call", {"tool": name, "args": args, "tool_use_id": tool_use_id},
                            agent=agent)
        else:
            self.store.emit("tool.denied", {"tool": name, "args": args, "reason": d.reason,
                                            "rule": d.rule, "tool_use_id": tool_use_id},
                            agent=agent)
        return d

    def delegate(self, to: str, tool_use_id: str | None = None, task: str = "", *,
                 require_sample_outcomes: bool = False, objective: dict | None = None) -> Decision:
        d = check_delegation(self.store, self.project_id, to, task)
        if to in ("hands", "reactor") and require_sample_outcomes:
            from qumus2.agents.readiness import step_readiness

            project = self.store.get_project(self.project_id) if self.project_id else None
            match = _STEP_REF.match(task or "")
            selected = next((s for s in self._plan() if match and s.get("id") == match.group(1)
                             and s.get("owner") == to), {"owner": to})
            errors = step_readiness(self.store, project or {}, selected, self.registry, task)
            from qumus2.records.reactor_requirements import established_requirements

            if selected.get("operation") in ("assess_reactor_inputs", "prepare_reactor_pair", "reactor_reaction", "prepare_material_metal", "reactor_material_metal") and not established_requirements(self.store, self.project_id):
                errors.append({"rule": "reactor_requirements_required", "message": "Establish the user's material-metal experiment before this delegation; legacy two-flake fixtures do not define it."})
            if not d.allow and not any(e["rule"] == d.rule for e in errors):
                errors.insert(0, {"rule": d.rule, "message": d.reason})
            if errors:
                d = Decision(False, "Delegation prerequisites: " + "; ".join(
                    e["message"] + (" Missing: " + ", ".join(e["missing_fields"]) if e.get("missing_fields") else "")
                    for e in errors), errors[0]["rule"], {"readiness_errors": errors})
        if d.allow and to == "picker":
            from qumus2.records.reactor_requirements import established_requirements, norm

            req = established_requirements(self.store, self.project_id)
            if req:
                available = {norm(f["props"].get("material")) for f in self.store.list_samples(kind="flake")
                    if f["status"] in ("catalogued", "selected") and f["props"].get("images")}
                missing = [m or "unspecified reactive material" for m in ("hBN", req.reactive_material)
                           if not m or norm(m) not in available]
                if missing:
                    d = Decision(False, "No photographed candidates for " + ", ".join(missing)
                        + "; plan Hands preparation/search before Picker screening.", "missing_photographed_inputs")
        if not d.allow:
            self.store.emit("tool.denied", {"tool": "delegate",
                                            "args": {"to": to, "task": task[:300]},
                                            "reason": d.reason, "rule": d.rule,
                                            "readiness_errors": (d.args or {}).get("readiness_errors", []),
                                            "tool_use_id": tool_use_id}, agent=ORCHESTRATOR)
        return d

    # ----- plan step status (code, not the prompt, keeps it current) --------

    def _plan(self) -> list[dict[str, Any]]:
        proj = self.store.get_project(self.project_id) if self.project_id else None
        return proj["plan"] if proj else []

    def delegation_started(self, key: str, to: str, description: str = "") -> str | None:
        """The orchestrator sent `to` off: the step it names ("s3: ..." or "[s3]"
        in the description) is running; without a name, the first step owned by
        `to` that is not finished. A finished step named again is reopened."""
        plan = self._plan()
        m = _STEP_REF.match(description or "")
        ids = {s.get("id") for s in plan}
        step = m.group(1) if m and m.group(1) in ids else next(
            (s["id"] for s in plan if s.get("owner") == to
             and s.get("status") in ("pending", "running")), None)
        self._facts.pop(key, None)
        selected = next((s for s in plan if s.get("id") == step), {})
        self._open[key] = _Delegation(to, step, sample_outcomes=deepcopy(selected.get("sample_outcomes") or []),
            reactor_outcome=deepcopy(selected.get("reactor_outcome")),
            contracts={s["id"]: deepcopy(completion_contract(s)) for s in plan},
            started_event=self.store.last_event_id())
        if step is not None:
            self.store.set_step_status(self.project_id, step, "running", agent=ORCHESTRATOR)
        return step

    def delegation_finished(self, key: str, status: str = "done") -> str | None:
        """The subagent returned. Its step is done only if the records show it
        did the work (`check`); otherwise failed (a hardware job did not
        succeed) or pending again. A status other than "done" (an error, a
        step handed back unfinished) is kept. Returns the code-written facts
        for the orchestrator. The SDK runtime calls this from the Agent tool's
        PostToolUse hook and from the message stream; the second call only
        gets the same facts back."""
        d = self._open.pop(key, None)
        if d is None:
            return self._facts.pop(key, None)
        ok, checked, lines = self.check(d)
        if checked == "done" and d.sample_outcomes:
            missing = self._unmet_contract(d, d.step)
            if not ok:
                missing.append("another job in this delegation has not completed successfully")
            if missing:
                ok, checked = False, "pending"
                lines += ["Required final sample state is not met: " + "; ".join(missing) + "."]
            elif d.reactor_outcome and d.reactor_outcome["disposition"] == "observation_hold":
                lines.append("Intentional observation hold verified at the declared process targets; not cold storage.")
        if status == "done":
            status = checked
        current = next((s for s in self._plan() if s.get("id") == d.step), {})
        if current.get("status") == "skipped" and current.get("physical_resolution"):
            status = "skipped"
            lines.append("The person's recorded cancellation/replacement remains in effect; job outcomes are separate.")
        if d.step is not None:
            lines.append(f"Plan step {d.step} is now {status}.")
        if d.step is not None and d.step not in {o.step for o in self._open.values()}:
            self.store.set_step_status(self.project_id, d.step, status, agent=ORCHESTRATOR)
        project = self.store.get_project(self.project_id) if self.project_id else {}
        versions = {k: (project or {}).get(k) for k in ("plan_revision", "plan_version")}
        lines.append(f"Current plan_revision={versions['plan_revision']}, plan_version={versions['plan_version']}. "
                     "Use refine_plan for ordinary edits; merge progress without reusing an old update_plan version.")
        facts = (f"Code check of the {d.to} delegation (from the run records, not the "
                 "subagent's words): " + " ".join(lines))
        self.store.emit("agent.delegation_checked", {"to": d.to, "step": d.step, "ok": ok,
                                                     "status": status, "facts": lines, **versions},
                        agent=ORCHESTRATOR)
        self._facts[key] = facts
        return facts

    def _unmet_contract(self, delegation: _Delegation, step_id: str | None) -> list[str]:
        captured = delegation.contracts.get(step_id, {})
        current = next((completion_contract(s) for s in self._plan() if s.get("id") == step_id), None)
        missing = unmet_sample_outcomes(self.store, self.bench or {}, captured.get("sample_outcomes") or [])
        if current != captured:
            missing.append("the active step's completion conditions changed; review the current goal")
        return missing

    def check(self, d: _Delegation) -> tuple[bool, str, list[str]]:
        """(ok, step status, facts) of one finished delegation, from its tool
        results and the job records."""
        good = [c for c in d.calls if c["status"] in _GOOD]
        if d.to == "hands":
            return self._check_jobs(d)
        if d.to == "reactor":
            return self._check_reactor(d)
        if d.to in DELIVERABLES:
            got = [c for c in good if c["tool"] in DELIVERABLES[d.to]]
            if got:
                return True, "done", [f"{got[-1]['tool']}: {got[-1]['summary']}."]
            other = [f"{c['tool']}: {c['summary']}" for c in d.calls
                     if c["tool"] in ("report_selection_blocked", "report_design_infeasible")]
            return False, "pending", [
                f"no {' / '.join(DELIVERABLES[d.to])} was accepted"
                + (f" ({'; '.join(other)})" if other else "") + "."]
        plans = [c for c in d.calls if c["tool"] == "submit_device_plan"]
        if plans and plans[-1]["status"] not in _GOOD:
            return False, "pending", [f"the device plan was refused: {plans[-1]['summary']}."]
        if not good:
            return False, "pending", ["it returned without a successful tool call."]
        looked = [c for c in good if c["tool"] in ("inspect_bench", "submit_device_plan")]
        return True, "done", [f"{c['tool']}: {c['summary']}." for c in looked] or [
            f"{len(good)} successful tool call(s)."]

    def _check_reactor(self, d: _Delegation) -> tuple[bool, str, list[str]]:
        """A reactor delegation from its reactor tool results (qumus2/reactor/tools.py):
        done when the last reactor action succeeded, pending while a person is asked
        or after a refusal, failed after an unclear outcome."""
        from qumus2.reactor.tools import REACTOR_TOOLS

        calls = [c for c in d.calls if c["tool"] in REACTOR_TOOLS]
        actions = [c for c in calls if c["tool"] != "reactor_status"]
        if not actions:
            return False, "pending", ["Reactor returned without a reactor action."]
        lines = [f"{c['tool']}: {c['status']}: {c['summary']}" for c in actions[-6:]]
        last = actions[-1]
        if last["status"] in _GOOD:
            return True, "done", lines
        if last["status"] == "needs_attention":
            return False, "failed", [*lines, "A reactor outcome is unclear; read reactor_status before anything moves."]
        if last["status"] == "pending":
            return False, "pending", [*lines, "Waiting for the person's handover."]
        return False, "pending", lines

    def _step_for_flow(self, d: _Delegation, flow: str) -> str | None:
        """The hands' plan step a flow belongs to: the delegated step when its
        title names the flow, else the first open step of the hands that does,
        else the first one that does; None when no title names it."""
        words = _FLOW_WORDS.get(flow, ())
        plan = [s for s in self._plan() if s.get("owner") == d.to
                and (s.get("operation") == flow if s.get("operation") else
                     any(w in (s.get("title") or "").lower() for w in words))]
        if not plan:
            return None
        if d.step in {s.get("id") for s in plan}:
            return d.step
        return next((s["id"] for s in plan if s.get("status") in ("pending", "running")),
                    plan[0]["id"])

    def _check_jobs(self, d: _Delegation) -> tuple[bool, str, list[str]]:
        # Deduplicate observations of one run, never distinct runs of one flow.
        # Prior attempts remain attached to this step across delegations/restarts.
        jobs = {run["id"]: run["flow"] for run in linked_physical_jobs(self.store, self.project_id, d.step)
                if run["status"] != "succeeded" and not cancelled_job_retired(self.store, run)} \
            if d.step is not None else {}
        for c in d.calls:
            rid = c["data"].get("job_id") or c["data"].get("run_id")
            run = self.store.get_run(rid) if rid else None
            flow = (run or {}).get("flow") or c["data"].get("flow")
            if rid and flow:
                jobs[rid] = flow
                self._link_job(d, rid)
        if not jobs:
            return False, "pending", ["the hands returned without running a hardware job."]
        lines, bad, unfinished, succeeded, invalid_arguments = [], False, False, False, False
        by_step: dict[str, list[str]] = {}  # plan step -> statuses of its jobs
        for rid, flow in jobs.items():
            st = self.jobs.status(rid)
            if cancelled_job_retired(self.store, self.store.get_run(rid)):
                lines.append(f"job {rid} ({flow}) was cancelled, with no held reservations or uncertain physical state; "
                             "it is accounted for, not successful execution.")
                continue
            step = self._step_for_flow(d, flow)
            if step is not None:
                by_step.setdefault(step, []).append(st.status)
            at = f" [plan step {step}]" if step is not None and step != d.step else ""
            run = self.store.get_run(rid)
            captured = d.contracts.get(step, {})
            current = next((s for s in self._plan() if s.get("id") == d.step), {})
            repaired_jobs = {c["execution_evidence"]["job_id"] for c in current.get("outcome_corrections", [])
                             if c.get("correction_kind") == "exfoliation_source_to_receiving_chip"}
            if repaired_jobs and (run["project_id"] != self.project_id or rid not in repaired_jobs
                                  or flow != "exfoliate_chips" or step != d.step):
                bad = True
                if step in by_step:
                    by_step[step][-1] = "failed"
                lines.append(f"job {rid} cannot certify completion: recheck the original successful exfoliation "
                             "job recorded by the output-identity correction; do not repeat exfoliation.")
                continue
            if st.status == "succeeded" and d.contracts.get(d.step, {}).get("operation") == "prepare_material_metal":
                from qumus2.records.material_metal_arguments import (
                    argument_feedback,
                    normalize_arguments,
                )
                from qumus2.records.material_metal_plan import preparation_job_error

                own_contract = d.contracts[d.step]
                _, errors = normalize_arguments(own_contract["operation"], own_contract.get("operation_args") or {})
                if errors:
                    invalid_arguments = True
                    if step in by_step:
                        by_step[step][-1] = "pending"
                    lines.append(f"job {rid} succeeded; plan argument validation remains unresolved: "
                                 + argument_feedback(errors) + ". Repair the plan contract and recheck this exact job; do not repeat preparation.")
                    continue
                error = preparation_job_error(self.store, self.project_id,
                    {**d.contracts[d.step], "id": d.step}, run)
                if error:
                    bad = True
                    if step in by_step:
                        by_step[step][-1] = "failed"
                    lines.append(f"job {rid} cannot certify preparation: {error}")
                    continue
            if flow in SEARCH_FLOWS and captured.get("sample_outcomes") and (run["project_id"] != self.project_id
                    or search_target_error(self.store, captured, run["params"].get("chip_id"))):
                bad = True
                by_step[step][-1] = "failed"
                lines.append(f"job {rid} ({flow}) does not match the step's actual search target; it cannot certify completion.")
                continue
            if st.status == "succeeded":
                succeeded = True
                lines.append(f"job {rid} ({flow}) succeeded{at}.")
                continue
            where = next((s.get("name") for s in st.steps
                          if s.get("status") not in ("succeeded", "skipped")), None)
            why = (st.error or {}).get("message") if isinstance(st.error, dict) else st.error
            lines.append(f"job {rid} ({flow}) is {st.status}"
                         + (f" at step {where}" if where else "") + (f": {why}" if why else "")
                         + f"{at}.")
            bad |= st.status in _JOB_BAD
            unfinished |= st.status not in _JOB_BAD
        # steps named by a flow's title get that flow's status (a parked search
        # no longer marks the exfoliation step before it as failed)
        for step, statuses in by_step.items():
            if step == d.step:
                continue
            current = next((s for s in self._plan() if s.get("id") == step), {})
            if current.get("status") == "skipped" and current.get("physical_resolution"):
                continue
            status = ("failed" if any(x in _JOB_BAD for x in statuses) else
                      "running" if any(x not in ("succeeded",) for x in statuses) else "done")
            missing = self._unmet_contract(d, step)
            captured = d.contracts.get(step, {})
            if d.sample_outcomes and not captured.get("sample_outcomes"):
                missing.append("the other physical step had no declared completion conditions")
            if d.to == "reactor":
                missing.append("another reactor step requires its own delegation and product acceptance check")
            if status == "done" and missing:
                status = "pending"
                lines.append("Required final sample state is not met: " + "; ".join(missing) + ".")
            self.store.set_step_status(self.project_id, step, status, agent=ORCHESTRATOR)
            lines.append(f"Plan step {step} is now {status}.")
        # the delegated step is judged by its own flow(s) when its title names one
        own = by_step.get(d.step or "", [])
        own_status = ("failed" if any(x in _JOB_BAD for x in own) else
                      "done" if own and all(x == "succeeded" for x in own) else "running")
        if bad:
            if self.project_id:  # the bench may differ from the ledger now
                self.store.update_project_state(self.project_id, eyes_checked=False)
            lines.append("The eyes must inspect the bench again before the next hands "
                         "delegation.")
            return False, own_status if own else "failed", lines
        if unfinished:
            return False, own_status if own else "running", lines
        if invalid_arguments:
            return False, "pending", lines
        if not succeeded:
            return False, "pending", [*lines, "No hardware job completed successfully."]
        return True, "done", lines

    def _link_job(self, d: _Delegation, run_id: str) -> None:
        if not self.project_id or d.to not in ("hands", "reactor"):
            return
        run = self.store.get_run(run_id)
        if not run or run["project_id"] != self.project_id:
            return
        exists = self.store._x("SELECT 1 FROM event WHERE kind='agent.job_linked' AND run_id=? AND "
                              "json_extract(payload_json,'$.project_id')=? AND "
                              "json_extract(payload_json,'$.step_id') IS ? LIMIT 1",
                              (run_id, self.project_id, d.step)).fetchone()
        if not exists:
            self.store.emit("agent.job_linked", {"project_id": self.project_id, "step_id": d.step,
                            "job_id": run_id, "flow": run["flow"]}, run_id=run_id, agent=d.to)

    def turn_finished(self, ok: bool = True) -> None:
        """End of an orchestrator turn. A turn that ended badly (error, stop,
        spend limit) puts the steps it left open back to pending. A good one
        closes them (after the same check), and the orchestrator's own steps
        are done once everything before them is."""
        for key in list(self._open):
            self.delegation_finished(key, "done" if ok else "pending")
        self._facts.clear()
        if not ok:
            return
        from qumus2.records.reactor_requirements import acceptance_errors

        if acceptance_errors(self.store, self.project_id):
            return
        for s in self._plan():
            if s.get("status") in ("done", "skipped"):
                continue
            if s.get("owner") != ORCHESTRATOR:
                break  # an unfinished step of someone else: later steps wait
            self.store.set_step_status(self.project_id, s["id"], "done", agent=ORCHESTRATOR)

    def _active_delegation_step(self, agent: str | None = None) -> str | None:
        """Use the real open delegation, never a task-text job ID or a guessed step."""
        active = [d for d in self._open.values() if agent is None or d.to == agent]
        if len(active) != 1:
            return None
        delegation = active[0]
        return next((s["id"] for s in self._plan()
                     if s["id"] == delegation.step and s.get("owner") == delegation.to), None)

    async def execute(self, agent: str, tool: str, args: dict[str, Any]) -> ToolResult:
        spec = self.registry.get(tool)
        if spec is None:
            return ToolResult(status="error", summary=f"unknown tool {tool}")
        parsed = spec.args_model.model_validate(args)
        ctx = ToolContext(store=self.store, jobs=self.jobs, agent=agent,
                          project_id=self.project_id, extra={"bench": self.bench, "registry": self.registry,
                              "delegation_step_id": self._active_delegation_step(agent),
                              "stations": self.stations})
        try:
            return await spec.handler(parsed, ctx)
        except QumusError as e:
            return ToolResult(status="error", summary=e.message, data=e.to_dict())
        except (ContractError, ConnectionError, TimeoutError, ValueError, KeyError, RuntimeError) as e:
            # A station refusal, a lost connection or a lapsed lease is an
            # outcome the agent can act on (look at the job, retry later, ask a
            # person); it must not end the whole turn.
            return ToolResult(status="error", summary=f"{tool} failed: {type(e).__name__}: {e}",
                              data={"type": type(e).__name__, "message": str(e)})

    def post(self, agent: str, tool: str, result: ToolResult,
             tool_use_id: str | None = None) -> str | None:
        """Log the result; return user instructions to append for the model.

        The orchestrator gets (and consumes) pending instructions. A subagent
        sees each pending instruction once as a heads-up without consuming it;
        the orchestrator still gets it when the subagent returns (spec 11.4).
        """
        self.store.emit("tool.result", {"tool": short_name(tool), "tool_use_id": tool_use_id,
                                        **result.model_dump()}, agent=agent)
        for d in self._open.values():  # what the subagent did, for its delegation check
            if d.to == agent:
                d.calls.append({"tool": short_name(tool), "status": result.status,
                                "summary": result.summary, "data": result.data or {}})
                data = result.data or {}
                if rid := data.get("job_id") or data.get("run_id"):
                    self._link_job(d, rid)
        if agent == ORCHESTRATOR:
            return self.inbox.format_for_tool_result()
        return self.inbox.format_heads_up(agent)

    def save_agent_status(self, agent: str, last_message: str,
                          session_id: str | None = None) -> None:
        """Stop / SubagentStop hooks: code saves progress, not the AI (spec 6.1)."""
        if not self.project_id:
            return
        proj = self.store.get_project(self.project_id) or {"state": {}}
        last = dict(proj["state"].get("agents_last", {}))
        last[agent] = {"ts": self.store.clock.now(), "message": (last_message or "")[:2000]}
        self.store.update_project_state(self.project_id, agents_last=last)
        if session_id and agent == ORCHESTRATOR:
            self.store.set_session_id(self.project_id, session_id)

    def project_summary(self) -> str:
        """Plain-text project state for the orchestrator after a restart (spec 6.5)."""
        proj = self.store.get_project(self.project_id) if self.project_id else None
        if not proj:
            return "(no project)"
        st = {k: v for k, v in proj["state"].items() if k != "agents_last"}
        lines = [f"project {proj['id']} ({proj['name']}), plan revision {proj['plan_revision']}, plan version {proj['plan_version']}"]
        for s in proj["plan"]:
            lines.append(f"  - [{s.get('status', 'pending')}] {s.get('id')}: {s.get('title')} "
                         f"({s.get('owner')})")
        step_id = self._active_delegation_step()
        if evidence := linked_job_evidence(self.store, self.project_id, step_id):
            lines.append("Current delegated step job records (read-only evidence, not new execution; "
                         "copy exact IDs for status checks and do not repeat completed jobs): "
                         + json.dumps({"project_id": self.project_id, "step_id": step_id,
                                       "known_jobs": evidence}, ensure_ascii=False))
        from qumus2.records.reactor_requirements import requirements_record

        if record := requirements_record(self.store, self.project_id):
            lines.append("Established experiment (independent of plan): " + json.dumps(record, ensure_ascii=False))
        if st:
            lines.append(f"state: {json.dumps(st, ensure_ascii=False, default=str)[:3000]}")
        active = [r for r in self.store.list_runs()
                  if r["project_id"] == self.project_id
                  and r["status"] in ("queued", "running", "parked", "paused", "waiting_human")]
        for r in active:
            lines.append(f"job {r['id']} {r['flow']}: {r['status']}")
        return "\n".join(lines)

    async def call(self, agent: str, tool: str, args: dict[str, Any],
                   tool_use_id: str | None = None) -> tuple[ToolResult, str | None]:
        d = self.pre(agent, tool, args, tool_use_id)
        if not d.allow:
            return ToolResult(status="denied", summary=d.reason, data={"rule": d.rule}), None
        result = await self.execute(agent, tool, args)
        return result, self.post(agent, tool, result, tool_use_id)
