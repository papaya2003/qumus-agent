"""App-process proxies for a separate lab process (runtime/labproc.py).

RemoteJobService has the JobService methods (C5); RemoteLab has the read-only
lab view and the fault menu the UI uses (`world.snapshot()`, `faults.inject()`,
`devices.devices`, `bench`). Both call through the Supervisor, so they keep
working across lab-process restarts: `wait` rides out a restart, other calls
wait a few seconds for the new process and then raise ConnectionError.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from qumus2.core.clock import Clock, RealClock
from qumus2.runtime.jobs import TERMINAL, JobStatus

#: how long a call waits for a restarting lab process before giving up
READY_TIMEOUT_S = 15.0


class RemoteJobService:
    def __init__(self, supervisor: Any, wall: Clock | None = None, chunk_s: float = 2.0) -> None:
        self.sup, self.store = supervisor, supervisor.store
        self.wall = wall or RealClock()
        self.chunk_s = chunk_s

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self.sup.call(name, *args, ready_timeout_s=READY_TIMEOUT_S, **kwargs)

    def submit(self, flow: str, params: dict[str, Any], *, project_id: str | None = None,
               requested_by: str | None = None) -> str:
        return self._call("jobs.submit", flow, params, project_id=project_id,
                          requested_by=requested_by)

    def status(self, run_id: str) -> JobStatus:
        """Straight from the shared run store: answers even while the lab restarts."""
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown job {run_id}")
        return JobStatus(run_id=run_id, flow=run["flow"], status=run["status"],
                         done=run["status"] in TERMINAL, steps=self.store.get_steps(run_id),
                         result=run["result"], error=run["error"],
                         workflow=run["meta"].get("workflow"))

    def wait(self, run_id: str, timeout_s: float) -> JobStatus:
        deadline = self.wall.now() + timeout_s
        while True:
            left = deadline - self.wall.now()
            if left <= 0:
                return self.status(run_id)
            try:
                st = self.sup.call("jobs.wait", run_id, min(left, self.chunk_s),
                                   ready_timeout_s=min(left, self.chunk_s))
            except ConnectionError:  # restarting: the run is recovered by the new process
                st = self.status(run_id)
                if not st.done:
                    self.wall.sleep(min(0.2, max(0.0, deadline - self.wall.now())))
                    continue
            if st.done:
                return st

    def cancel(self, run_id: str) -> None:
        self._call("jobs.cancel", run_id)

    def stop_all(self) -> None:
        self._call("jobs.stop_all")

    def resume(self, run_id: str) -> dict[str, Any]:
        return self._call("jobs.resume", run_id)

    def resume_all(self) -> list[dict[str, Any]]:
        return self._call("jobs.resume_all")

    def retry(self, run_id: str) -> None:
        self._call("jobs.retry", run_id)

    def reverify(self, run_id: str) -> None:
        self._call("jobs.reverify", run_id)

    def review_waiting(self, run_id: str, *, project_id: str, failure_event_id: int,
                       reviewer: str, reason: str) -> dict[str, Any]:
        return self._call("jobs.review_waiting", run_id, project_id=project_id,
                          failure_event_id=failure_event_id, reviewer=reviewer, reason=reason)

    def complete_experiment(self, project_id: str, summary: str) -> dict[str, Any]:
        return self._call("jobs.complete_experiment", project_id, summary)


class _World:
    def __init__(self, sup: Any) -> None:
        self._sup = sup

    def snapshot(self) -> list[dict[str, Any]]:
        return self._sup.call("lab.world_snapshot", ready_timeout_s=READY_TIMEOUT_S)

    def evaluation_snapshot(self) -> dict[str, Any]:
        """Private scorer truth copy, never an agent tool or measured evidence."""
        return self._sup.call("lab.evaluation_snapshot", ready_timeout_s=READY_TIMEOUT_S)

    def stamp_snapshot(self) -> dict[str, Any]:
        return self._sup.call("lab.stamp_snapshot", ready_timeout_s=READY_TIMEOUT_S)

    def tape_snapshot(self) -> dict[str, Any]:
        return self._sup.call("lab.tape_snapshot", ready_timeout_s=READY_TIMEOUT_S)

    @property
    def faults(self) -> SimpleNamespace:
        """A copy of the world's fault settings (read-only; inject through `faults`)."""
        return SimpleNamespace(**self._sup.call("lab.world_faults",
                                                ready_timeout_s=READY_TIMEOUT_S))


class _Faults:
    def __init__(self, sup: Any) -> None:
        self._sup = sup

    def inject(self, fault: str, params: dict[str, Any] | None = None) -> None:
        self._sup.call("faults.inject", fault, params, ready_timeout_s=READY_TIMEOUT_S)

    def describe(self) -> list[dict[str, Any]]:
        return self._sup.call("faults.describe", ready_timeout_s=READY_TIMEOUT_S)


def _device_view(state: dict[str, Any]) -> Any:
    """An object with the device's class name, plain attributes and read-only
    methods returning the values read in the lab process (no commands)."""
    def reader(result: tuple[str, Any]) -> Any:
        def read(_self: Any) -> Any:
            if result[0] == "err":
                raise RuntimeError(result[1])
            return result[1]
        return read
    methods = {m: reader(r) for m, r in state["reads"].items()}
    view = type(state["class"], (), methods)()
    vars(view).update(state["attrs"])
    return view


class _Devices:
    def __init__(self, sup: Any) -> None:
        self._sup = sup

    @property
    def devices(self) -> dict[str, Any]:
        states = self._sup.call("lab.device_states", ready_timeout_s=READY_TIMEOUT_S)
        return {s["attrs"]["device_id"]: _device_view(s) for s in states}


class _Twin:
    def __init__(self, sup: Any) -> None:
        self._sup = sup

    def state(self) -> dict[str, Any]:
        return self._sup.call("lab.twin_state", ready_timeout_s=READY_TIMEOUT_S)


class RemoteLab:
    def __init__(self, supervisor: Any) -> None:
        self._sup = supervisor
        self.world = _World(supervisor)
        self.faults = _Faults(supervisor)
        self.devices = _Devices(supervisor)
        self.twin = _Twin(supervisor)
        self._bench: dict[str, Any] | None = None

    def preview(self, camera_id: str) -> dict[str, Any] | None:
        return self._sup.call("lab.preview", camera_id, ready_timeout_s=READY_TIMEOUT_S)

    @property
    def bench(self) -> dict[str, Any]:
        if self._bench is None:  # static layout: fetch once
            self._bench = self._sup.call("lab.bench", ready_timeout_s=READY_TIMEOUT_S)
        return self._bench
