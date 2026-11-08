"""Hands' recovery menu (spec 4.3 L1).

When a job comes back `needs_attention` (a step's L0 corrections ran out, or its
observation was unclear), hands looks at the photos and measurements and picks
ONE of these tools. It cannot invent another hardware action. Every pick is
counted in the project state and limited by `recovery:` in config/agents.yaml
(policy rule `recovery_budget`), so a confused agent cannot loop forever; when
the budget is spent the only way on is request_human_help.

    reverify_job    re-shoot: run only the failed step's verify() again (no motion)
    retry_job       re-place: run the job again from its failed step
    skip_chip       mark a chip unusable for this project
    replace_chip    run the same flow again on another chip
    request_human_help  (hardware.py) ask a person
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.agents.tools import hardware
from qumus2.agents.tools.hardware import job_result, recoveries, submit
from qumus2.config import load_yaml
from qumus2.runtime.jobs import RECOVERABLE

#: a job that failed outright may still be redone on another chip
REPLACEABLE = RECOVERABLE | {"failed"}
#: the agent-facing parameters of each flow (resolved fields like home_slot are
#: recomputed when the new job is submitted)
FLOW_ARGS: dict[str, type[BaseModel]] = {
    "place_chip_on_transfer_stage": hardware.PlaceChipArgs,
    "search_chip": hardware.SearchChipArgs,
}


class RecoverArgs(BaseModel):
    job_id: str
    reason: str = Field(description="What you saw in the photos/measurements and why this "
                        "option fits")


class SkipChipArgs(BaseModel):
    chip_id: str
    reason: str


class ReplaceChipArgs(BaseModel):
    job_id: str = Field(description="The job that failed on its chip")
    new_chip_id: str
    reason: str


def budget() -> dict[str, int]:
    cfg = load_yaml("agents.yaml").get("recovery", {})
    return {"per_job": int(cfg.get("per_job", 2)), "per_project": int(cfg.get("per_project", 20))}


def _counts(ctx: ToolContext) -> dict[str, Any]:
    proj = ctx.store.get_project(ctx.project_id) if ctx.project_id else None
    return dict((proj or {"state": {}})["state"].get("recovery", {"total": 0, "jobs": {}}))


def _count(ctx: ToolContext, job_id: str | None, action: str, reason: str) -> dict[str, Any]:
    c = _counts(ctx)
    jobs = dict(c.get("jobs", {}))
    if job_id:
        jobs[job_id] = jobs.get(job_id, 0) + 1
    c = {**c, "total": c.get("total", 0) + 1, "jobs": jobs}
    if ctx.project_id:
        ctx.store.update_project_state(ctx.project_id, recovery=c)
    ctx.store.emit("notification", {"level": "info", "text": f"recovery: {action} ({reason})",
                                    "action": action, "job_id": job_id}, run_id=job_id,
                   agent=ctx.agent)
    return c


def root_job(ctx: ToolContext, job_id: str) -> str:
    """Retries of a retry count against the original job."""
    return _counts(ctx).get("links", {}).get(job_id, job_id)


def _link(ctx: ToolContext, new_job: str, root: str) -> None:
    c = _counts(ctx)
    c["links"] = {**c.get("links", {}), new_job: root}
    if ctx.project_id:
        ctx.store.update_project_state(ctx.project_id, recovery=c)


def budget_reason(ctx: ToolContext, job_id: str | None) -> str | None:
    b, c = budget(), _counts(ctx)
    if c.get("total", 0) >= b["per_project"]:
        return (f"recovery budget for this project is spent ({b['per_project']}); "
                "call request_human_help")
    if job_id is not None:
        used = c.get("jobs", {}).get(root_job(ctx, job_id), 0)
        if used >= b["per_job"]:
            return (f"job {job_id} already used its {b['per_job']} recovery attempts; "
                    "call request_human_help")
    return None


def _recoverable(ctx: ToolContext, job_id: str, ok: set[str] = RECOVERABLE) -> str | None:
    run = ctx.store.get_run(job_id)
    if run is None:
        return f"unknown job {job_id}"
    if run["status"] not in ok:
        return f"job {job_id} is {run['status']}; only {sorted(ok)} jobs can be recovered"
    err = run.get("error") or {}
    if isinstance(err, dict) and err.get("category") == "dangerous":
        return f"job {job_id} stopped for safety; only a human may continue it"
    return None


@qumus_tool("reverify_job", "Re-shoot: run the failed step's check again without moving "
            "anything (e.g. the photo was unclear). If it now passes the job continues.",
            agents={"hands"}, kind="recovery", requires=("recovery_budget",))
async def reverify_job(args: RecoverArgs, ctx: ToolContext) -> ToolResult:
    if (why := _recoverable(ctx, args.job_id)) is not None:
        return ToolResult(status="error", summary=why)
    if not hasattr(ctx.jobs, "reverify"):
        return ToolResult(status="error", summary="re-shoot is not available in this job "
                          "service yet")
    _count(ctx, root_job(ctx, args.job_id), "reverify", args.reason)
    ctx.jobs.reverify(args.job_id)  # type: ignore[attr-defined]
    return job_result(ctx.jobs.status(args.job_id), recoveries(ctx.store, args.job_id),
                      store=ctx.store)


@qumus_tool("retry_job", "Re-place / re-do: run the job again from the step that failed "
            "(its own checks and corrections run again). Then call wait_job.",
            agents={"hands"}, kind="recovery", requires=("recovery_budget",))
async def retry_job(args: RecoverArgs, ctx: ToolContext) -> ToolResult:
    if (why := _recoverable(ctx, args.job_id)) is not None:
        return ToolResult(status="error", summary=why)
    if not hasattr(ctx.jobs, "retry"):
        return ToolResult(status="error", summary="retry is not available in this job "
                          "service yet")
    _count(ctx, root_job(ctx, args.job_id), "retry", args.reason)
    rid = ctx.jobs.retry(args.job_id) or args.job_id  # type: ignore[attr-defined]
    return ToolResult(status="pending", summary=f"job {rid} restarted from its failed step; "
                      "call wait_job", data={"job_id": rid})


@qumus_tool("skip_chip", "Mark a chip as unusable for this project (e.g. broken or nothing "
            "transferred). It will not be used again.",
            agents={"hands"}, kind="recovery", refs={"chip_id": "chip"},
            requires=("recovery_budget",))
async def skip_chip(args: SkipChipArgs, ctx: ToolContext) -> ToolResult:
    _count(ctx, None, "skip_chip", args.reason)
    ctx.store.upsert_sample(args.chip_id, "chip", status="skipped",
                            props={"skip_reason": args.reason, "skipped_by": ctx.agent})
    return ToolResult(status="ok", summary=f"{args.chip_id} marked skipped",
                      data={"chip_id": args.chip_id})


@qumus_tool("replace_chip", "Use another chip: run the failed job's flow again with "
            "new_chip_id instead of its chip. Then call wait_job.",
            agents={"hands"}, kind="recovery", refs={"new_chip_id": "chip"},
            requires=("recovery_budget",))
async def replace_chip(args: ReplaceChipArgs, ctx: ToolContext) -> ToolResult:
    if (why := _recoverable(ctx, args.job_id, REPLACEABLE)) is not None:
        return ToolResult(status="error", summary=why)
    run = ctx.store.get_run(args.job_id) or {}
    model = FLOW_ARGS.get(run.get("flow", ""))
    if model is None:
        return ToolResult(status="error", summary=f"flow {run.get('flow')} does not run on "
                          "one chip; replace_chip does not apply")
    params = {k: v for k, v in (run.get("params") or {}).items() if k in model.model_fields}
    new = ctx.store.get_sample(args.new_chip_id) or {}
    if new.get("status") == "skipped":
        return ToolResult(status="error", summary=f"{args.new_chip_id} was skipped earlier")
    root = root_job(ctx, args.job_id)
    _count(ctx, root, "replace_chip", args.reason)
    params["chip_id"] = args.new_chip_id
    out = submit(ctx, run["flow"], params)
    if out.status == "pending":
        _link(ctx, out.data["job_id"], root)
        ctx.store.emit("notification", {"level": "info", "text":
                       f"{out.data['job_id']} replaces {args.job_id}"}, run_id=args.job_id)
    return out
