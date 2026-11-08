"""The agent's side of the lab windows: a JobService over contract v1 clients.

The agent tools keep calling `ctx.jobs.submit(flow, params)`, `wait`, `status`,
`cancel`, ... (runtime/jobs.py JobService, contract C5). `ContractJobService`
has the same methods, but every call that changes the lab is a module command:

    submit(flow, params)  -> POST /api/command {"action": flow, ...params, "job": {...}}
                             on the module that owns the flow (simlab.modules.FLOW_MODULE)
    cancel / retry / ...  -> job_cancel / job_retry / ... on the run's module
    stop_all()            -> POST /api/stop on every module (no lease needed)

It holds each module's control lease with the client's LeaseKeeper from the
first command until `close()`, so a dead agent stops the lab's motion within
the 3 s lease. Run progress (status, steps, results) is read from the run
store the sim lab writes. [sim lab only] The agent and the sim lab share one
SQLite file; with the real qumus-lab, progress comes from module state and
events instead (adaptation-plan.md step 6).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from qumus2.contract import (
    BadRequest,
    Conflict,
    ContractClient,
    ContractError,
    LeaseKeeper,
    NotFound,
)
from qumus2.runtime.jobs import TERMINAL, JobStatus
from qumus2.simlab.modules import FLOW_MODULE

LAB_WIDE_MODULE = "stacking"  # [default] module that serves resume_all / complete_experiment


class ContractJobService:
    def __init__(self, store: Any, clients: dict[str, ContractClient], *,
                 wall: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 0.1,
                 command_timeout_s: float = 10.0) -> None:
        self.store, self.clients = store, clients
        self.wall, self.sleep, self.poll_s = wall, sleep, poll_s
        self.command_timeout_s = command_timeout_s
        self._keepers: dict[str, LeaseKeeper] = {}

    # -- plumbing --------------------------------------------------------------
    def module_of_flow(self, flow: str) -> str:
        module = FLOW_MODULE.get(flow)
        if module is None or module not in self.clients:
            raise KeyError(f"flow {flow!r} is not offered by any connected lab module")
        return module

    def module_of_run(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown job {run_id}")
        return self.module_of_flow(run["flow"])

    def _lease(self, module: str) -> None:
        keeper = self._keepers.get(module)
        if keeper is not None and keeper.active:
            return
        client = self.clients[module]
        try:
            client.heartbeat()
        except Conflict as e:
            raise ValueError(f"the {module} module is controlled by someone else: {e}") from e
        self._keepers[module] = client.lease_keeper().start()

    def _command(self, module: str, action: str, params: dict[str, Any], *,
                 lease: bool) -> dict[str, Any]:
        """Send one synchronous module command; return its result data."""
        if lease:
            self._lease(module)
        client = self.clients[module]
        try:
            receipt = client.command(action, params)
        except BadRequest as e:  # the module refused the parameters (422)
            raise ValueError(str(e)) from e
        except NotFound as e:
            raise KeyError(str(e)) from e
        except Conflict as e:  # busy / locked / lease
            raise ValueError(f"{module} module refused {action}: {e}") from e
        lc = (receipt.state.raw or {}).get("last_command") or {}
        if receipt.command_id is not None and lc.get("id") == receipt.command_id \
                and lc.get("result") not in (None, "running"):
            if lc.get("result") != "ok":
                raise ValueError(f"{action} on {module}: {lc.get('result')}: {lc.get('message')}")
            return lc.get("data") or {}
        # not in the reply: find this command's end in the events
        done = client.wait_for_command(receipt, timeout_s=self.command_timeout_s)
        return dict(done.raw.get("data") or {})

    # -- JobService (C5) -----------------------------------------------------------
    def submit(self, flow: str, params: dict[str, Any], *, project_id: str | None = None,
               requested_by: str | None = None) -> str:
        module = self.module_of_flow(flow)
        data = self._command(module, flow, {**params, "job": {
            "project_id": project_id, "requested_by": requested_by}}, lease=True)
        run_id = data.get("run_id")
        if not isinstance(run_id, str):
            raise RuntimeError(f"{module} accepted {flow} but returned no run_id: {data}")
        return run_id

    def status(self, run_id: str) -> JobStatus:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown job {run_id}")
        return JobStatus(run_id=run_id, flow=run["flow"], status=run["status"],
                         done=run["status"] in TERMINAL, steps=self.store.get_steps(run_id),
                         result=run["result"], error=run["error"],
                         workflow=run["meta"].get("workflow"))

    def wait(self, run_id: str, timeout_s: float) -> JobStatus:
        deadline = self.wall() + max(0.0, timeout_s)
        while True:
            st = self.status(run_id)
            if st.done or self.wall() >= deadline:
                return st
            self.sleep(min(self.poll_s, max(0.0, deadline - self.wall())))

    def cancel(self, run_id: str) -> None:
        self._command(self.module_of_run(run_id), "job_cancel", {"run_id": run_id}, lease=False)

    def stop_all(self) -> None:
        errors = []
        for module, client in self.clients.items():
            try:
                client.stop(wait_s=1.0)
            except ContractError as e:
                errors.append(f"{module}: {e}")
        if errors:
            raise ConnectionError("stop not confirmed by every module: " + "; ".join(errors))

    def retry(self, run_id: str) -> None:
        self._command(self.module_of_run(run_id), "job_retry", {"run_id": run_id}, lease=True)

    def reverify(self, run_id: str) -> None:
        self._command(self.module_of_run(run_id), "job_reverify", {"run_id": run_id}, lease=True)

    def resume(self, run_id: str) -> dict[str, Any]:
        data = self._command(self.module_of_run(run_id), "job_resume", {"run_id": run_id},
                             lease=True)
        return data.get("result") or {}

    def resume_all(self) -> list[dict[str, Any]]:
        return self._command(LAB_WIDE_MODULE, "job_resume_all", {}, lease=True).get("result") or []

    def review_waiting(self, run_id: str, *, project_id: str, failure_event_id: int,
                       reviewer: str, reason: str) -> dict[str, Any]:
        return self._command(self.module_of_run(run_id), "job_review_waiting", {
            "run_id": run_id, "project_id": project_id, "failure_event_id": failure_event_id,
            "reviewer": reviewer, "reason": reason}, lease=False).get("result") or {}

    def complete_experiment(self, project_id: str, summary: str) -> dict[str, Any]:
        return self._command(LAB_WIDE_MODULE, "complete_experiment",
                             {"project_id": project_id, "summary": summary},
                             lease=True).get("result") or {}

    def close(self) -> None:
        for module, keeper in list(self._keepers.items()):
            keeper.stop()
            try:
                self.clients[module].release()
            except ContractError:
                pass
        self._keepers.clear()


def connect(urls: dict[str, str], store: Any, client_id: str = "qumus-agent",
            **kwargs: Any) -> ContractJobService:
    clients = {m: ContractClient(url, client_id, name=m) for m, url in urls.items()}
    for c in clients.values():
        c.describe()  # fail early on a wrong address or contract version
    return ContractJobService(store, clients, **kwargs)
