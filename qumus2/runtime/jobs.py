"""Job interface the agent tools use (spec 5.3, 6.4).

CONTRACT. Hardware tools never run a flow inside the tool call. They submit a
job and return its id at once; the agent then calls wait_job(job_id, timeout).

    JobService.submit(flow, params, project_id=, requested_by=) -> run_id
    JobService.wait(run_id, timeout_s) -> JobStatus      (returns early when done)
    JobService.status(run_id) -> JobStatus
    JobService.cancel(run_id)                            (stop at next safe point)
    JobService.stop_all()                                (UI Stop button: every job pauses)
    JobService.resume(run_id) / resume_all()             (UI Continue button)
    JobService.retry(run_id) / reverify(run_id)          (the agent's recovery menu)

The brain never runs a job itself. The only implementation is
qumus2.stations.jobs.ContractJobService: every call becomes a contract v1
command on the station (simulated or real) that owns the flow, and the run and
step rows come back through the station's record feed (qumus2.stations.mirror).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from qumus2.runtime.store import RunStore

TERMINAL = {"succeeded", "failed", "cancelled", "parked", "waiting_human", "paused"}
#: step statuses a resume goes past: done, or skipped by a changed plan
FINISHED_STEP = ("done", "skipped")


class JobRecordLate(RuntimeError):
    """submit: the station accepted the job (it runs) but its run record has not
    reached the brain yet. Callers keep `run_id` so nobody submits it twice."""

    def __init__(self, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id


@dataclass
class JobStatus:
    run_id: str
    flow: str
    status: str
    done: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    result: Any = None
    error: Any = None
    workflow: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class JobService(Protocol):
    def submit(self, flow: str, params: dict[str, Any], *, project_id: str | None = None,
               requested_by: str | None = None) -> str: ...

    def wait(self, run_id: str, timeout_s: float) -> JobStatus: ...

    def status(self, run_id: str) -> JobStatus: ...

    def cancel(self, run_id: str) -> None: ...

    def stop_all(self) -> None: ...

    def resume(self, run_id: str) -> dict[str, Any]: ...

    def resume_all(self) -> list[dict[str, Any]]: ...

    def retry(self, run_id: str) -> None: ...

    def reverify(self, run_id: str) -> None: ...

    def review_waiting(self, run_id: str, *, project_id: str, failure_event_id: int,
                       reviewer: str, reason: str) -> dict[str, Any]: ...

    def complete_experiment(self, project_id: str, summary: str) -> dict[str, Any]: ...


#: statuses from which the agent's recovery menu may retry or re-verify a job
#: (retrying a paused job continues it, like the Continue button)
RECOVERABLE = {"parked", "paused"}


def waiting_human_failure(store: RunStore, run_id: str) -> dict[str, Any] | None:
    """The current hold's journal identity, for a human's per-job review form."""
    run = store.get_run(run_id)
    if not run or run["status"] != "waiting_human":
        return None
    row = store._x("SELECT id FROM event WHERE run_id=? AND kind='job.status' ORDER BY id DESC LIMIT 1",
                   (run_id,)).fetchone()
    if not row:
        return None
    event = store.events_after(row["id"]-1, limit=1)[0]
    if event.payload.get("status") != "waiting_human":
        return None
    # the laboratory's own event id: the review goes back to the station that wrote it
    return {"failure_event_id": int(event.payload.get("lab_event_id", event.id)),
            "message": event.payload.get("message", "")}


def first_unfinished(store: RunStore, run_id: str) -> int:
    """Sequence number of the first step that is not done (resume point)."""
    done = {s["seq"] for s in store.get_steps(run_id) if s["status"] in FINISHED_STEP}
    seq = 0
    while seq in done:
        seq += 1
    return seq
