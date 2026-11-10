"""The agent's side of the laboratory: a JobService over contract v1 stations.

The agent tools keep calling `ctx.jobs.submit(flow, params)`, `wait`, `status`,
`cancel`, ... (runtime/jobs.py JobService). `ContractJobService` has the same
methods, but every call that changes the lab is a module command:

    submit(flow, params)  -> put_records (the brain's samples/project state), then
                             POST /api/command {"action": flow, ...params, "job": {...}}
                             on the module that offers the flow (stations/catalog.py)
    cancel / retry / ...  -> job_cancel / job_retry / ... on the run's module
    stop_all()            -> POST /api/stop on every module (no lease needed)

It holds each module's control lease with the client's LeaseKeeper from the
first command until `close()`, so a dead brain stops the lab's motion within
the 3 s lease. Run progress (status, steps, results) is read from the brain's
own store, which the record mirror (stations/mirror.py) keeps in step with the
station's /api/records feed.
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
from qumus2.stations.catalog import CATALOG, FlowCatalog

LAB_WIDE_MODULE = "stacking"  # [default] module that serves resume_all / complete_experiment


class ContractJobService:
    def __init__(self, store: Any, clients: dict[str, ContractClient], *,
                 mirror: Any = None, catalog: FlowCatalog | None = None,
                 wall: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 0.1,
                 command_timeout_s: float = 10.0, run_appear_s: float = 10.0) -> None:
        self.store, self.clients = store, clients
        self.mirror = mirror
        self.catalog = catalog or CATALOG
        self.wall, self.sleep, self.poll_s = wall, sleep, poll_s
        self.command_timeout_s = command_timeout_s
        self.run_appear_s = run_appear_s
        self._keepers: dict[str, LeaseKeeper] = {}

    def learn(self) -> None:
        """Read each station's job list (describe) into the catalogue; skip offline ones."""
        for module, client in self.clients.items():
            try:
                self.catalog.learn(module, client.describe(refresh=True).commands)
            except ContractError:
                continue

    def flow_names(self) -> list[str]:
        """Jobs the connected stations offer."""
        return [f for f in self.catalog.flow_names() if self.catalog.module_of(f) in self.clients]

    # -- plumbing --------------------------------------------------------------
    def module_of_flow(self, flow: str) -> str:
        module = self.catalog.module_of(flow)
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
        self.push()
        data = self._command(module, flow, {**params, "job": {
            "project_id": project_id, "requested_by": requested_by}}, lease=True)
        run_id = data.get("run_id")
        if not isinstance(run_id, str):
            raise RuntimeError(f"{module} accepted {flow} but returned no run_id: {data}")
        self._await_run(run_id)
        return run_id

    def push(self) -> None:
        """Send the brain's sample/project records before a command that may read them."""
        if self.mirror is not None:
            try:
                self.mirror.push()
            except ContractError as e:
                raise ValueError(f"the laboratory did not take the brain's records: {e}") from e

    def sync(self) -> None:
        if self.mirror is not None:
            self.mirror.sync()

    def _await_run(self, run_id: str) -> None:
        """The run row arrives through the record feed; tools read it right after submit."""
        if self.mirror is None:
            return
        if not self.mirror.wait_for(lambda: self.store.get_run(run_id) is not None, self.run_appear_s):
            raise RuntimeError(f"job {run_id} was accepted but its record did not arrive "
                               f"from the laboratory within {self.run_appear_s:.0f} s")

    def status(self, run_id: str) -> JobStatus:
        if self.mirror is not None and not self.mirror.running:
            self.mirror.sync()
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
        self.push()
        self._command(self.module_of_run(run_id), "job_retry", {"run_id": run_id}, lease=True)

    def reverify(self, run_id: str) -> None:
        self._command(self.module_of_run(run_id), "job_reverify", {"run_id": run_id}, lease=True)

    def resume(self, run_id: str) -> dict[str, Any]:
        data = self._command(self.module_of_run(run_id), "job_resume", {"run_id": run_id},
                             lease=True)
        return data.get("result") or {}

    def resume_all(self) -> list[dict[str, Any]]:
        self.push()
        return self._command(LAB_WIDE_MODULE, "job_resume_all", {}, lease=True).get("result") or []

    def review_waiting(self, run_id: str, *, project_id: str, failure_event_id: int,
                       reviewer: str, reason: str) -> dict[str, Any]:
        return self._command(self.module_of_run(run_id), "job_review_waiting", {
            "run_id": run_id, "project_id": project_id, "failure_event_id": failure_event_id,
            "reviewer": reviewer, "reason": reason}, lease=False).get("result") or {}

    def complete_experiment(self, project_id: str, summary: str) -> dict[str, Any]:
        self.push()
        return self._command(LAB_WIDE_MODULE, "complete_experiment",
                             {"project_id": project_id, "summary": summary},
                             lease=True).get("result") or {}

    def close(self) -> None:
        if self.mirror is not None:
            self.mirror.close()
        for module, keeper in list(self._keepers.items()):
            keeper.stop()
            try:
                self.clients[module].release()
            except ContractError:
                pass
        self._keepers.clear()


def connect(urls: dict[str, str], store: Any, client_id: str = "qumus-agent", *,
            records_from: str = "stacking", image_dir: Any = None, mirror_poll_s: float = 0.5,
            start_mirror: bool = True, require_all: bool = True, **kwargs: Any) -> ContractJobService:
    """Clients for every station, the record mirror from `records_from`, and the job service.

    `require_all=False` keeps going when a station is offline (it is retried on use)."""
    from qumus2.stations.mirror import RecordMirror

    clients = {m: ContractClient(url, client_id, name=m) for m, url in urls.items()}
    for c in clients.values():
        try:
            c.describe()  # fail early on a wrong address or contract version
        except ContractError:
            if require_all:
                raise
    feed = clients.get(records_from) or next(iter(clients.values()))
    mirror = RecordMirror(store, feed, image_dir=image_dir, poll_s=mirror_poll_s)
    service = ContractJobService(store, clients, mirror=mirror, **kwargs)
    service.learn()
    mirror.sync()
    if start_mirror:
        mirror.start()
    return service
