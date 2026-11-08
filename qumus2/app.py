"""Wiring: build the whole system for one mode.

    services = build_services(db_path="runs/checks/manual/qumus.db")    # testbench + scripted or SDK
    services.store / .gate / .jobs / .gateway / .runtime / .driver

    build_services(..., lab_process=True)   # lab side in its own supervised process
    build_lab_side(...)                     # what that process runs (qumus2 lab)

Stage 3 adds mode="lab" with devices/real; everything above devices is unchanged.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from qumus2.agents.driver import ConversationDriver
from qumus2.agents.gateway import ToolGateway
from qumus2.agents.openai_credentials import OpenAICredentials, load_openai_credentials
from qumus2.agents.runtime import AgentRuntime
from qumus2.agents.scripted import ScriptedRuntime, load_scenario
from qumus2.agents.tooling import default_registry
from qumus2.config import load_yaml
from qumus2.core.clock import Clock, SimClock
from qumus2.flows.context import FlowContext
from qumus2.runtime.jobs import ThreadJobService
from qumus2.runtime.store import RunStore
from qumus2.safety.gate import SafetyGate
from qumus2.safety.limits import Limits
from qumus2.testbench.lab import SimLab, build_sim_lab
from qumus2.testbench.persist import load_lab_state


@dataclass
class Services:
    store: RunStore
    clock: Clock
    lab: SimLab
    gate: SafetyGate
    jobs: ThreadJobService
    gateway: ToolGateway
    runtime: AgentRuntime
    driver: ConversationDriver
    project_id: str
    supervisor: Any = None  # runtime.supervisor.Supervisor when the lab is its own process
    # the last event before this session (one serve) wrote anything: the UI's
    # "this run" is everything after it, earlier sessions are its History
    session_start: int = 0
    _local_close_lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    async def close_local(self, timeout_s: float = 30.0) -> dict[str, Any]:
        """Close an owned in-process fixture only after its workers have exited.

        Supervised services retain their existing supervisor lifecycle. On an
        unfinished shutdown the worker service holds this owner, including its
        temporary image directories and open store, for a later explicit close.
        """
        if self.supervisor is not None:
            raise ValueError("close_local is only for an in-process job service")
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("shutdown timeout must be finite and nonnegative")
        if not self._local_close_lock.acquire(blocking=False):
            return {**self._pending_local_close(), "close_pending": True}
        try:
            return await self._close_local_owned(timeout_s)
        finally:
            self._local_close_lock.release()

    def _pending_local_close(self) -> dict[str, Any]:
        stop = self.jobs._shutdown_stop
        return {"version": 1, "closed": False, "safe_to_close_resources": False,
                "manual_execution": not self.jobs._threads, "admission_stopped": self.jobs._closing,
                "unsettled_jobs": sorted(rid for rid, done in list(self.jobs._done.items()) if not done.is_set()),
                "alive_workers": [t.name for t in self.jobs._threads + self.jobs._retired_threads if t.is_alive()],
                "stop_pending": stop is not None and stop.is_alive(), "stop_error": self.jobs._shutdown_stop_error,
                "unconfirmed_energy_jobs": list(self.jobs._shutdown_unconfirmed_energy), "evidence_pending": True}

    async def _close_local_owned(self, timeout_s: float) -> dict[str, Any]:
        if getattr(self, "_local_resources_closed", False):
            return self._local_shutdown_report
        self.jobs._shutdown_owner = self
        deadline = time.monotonic() + timeout_s
        try:
            report = await asyncio.wait_for(asyncio.to_thread(
                lambda: self.jobs.shutdown(max(0., deadline-time.monotonic()))),
                max(0., deadline-time.monotonic()))
        except TimeoutError:
            report = {**self._pending_local_close(), "shutdown_pending": True}
        self._local_shutdown_report = report
        if not report["safe_to_close_resources"]:
            return report
        try:
            await asyncio.wait_for(self.runtime.close(), max(0., deadline-time.monotonic()))
        except Exception as exc:
            report = {**report, "safe_to_close_resources": False,
                      "runtime_close_error": f"{type(exc).__name__}: {exc}"}
            self._local_shutdown_report = report
            return report
        if not self.store._lock.acquire(timeout=max(0., deadline-time.monotonic())):
            report = {**report, "safe_to_close_resources": False, "resource_close_pending": True}
            self._local_shutdown_report = report
            return report
        try:
            self.store.close()
        finally:
            self.store._lock.release()
        for owner in vars(self).values():
            if isinstance(owner, TemporaryDirectory):
                owner.cleanup()
        self._local_resources_closed = True
        self.jobs._shutdown_owner = None
        return report


@dataclass
class LabSide:
    """Everything below the tools: run store, clock, devices, safety, jobs.
    In-process by default; `qumus2 lab` runs it as its own process (spec 5.4)."""

    store: RunStore
    clock: Clock
    lab: SimLab
    gate: SafetyGate
    jobs: ThreadJobService


def build_lab_side(db_path: str | Path = ":memory:", *, seed: int | None = None,
                   testbench_config: dict[str, Any] | None = None,
                   image_dir: str | Path | None = None,
                   state_path: str | Path | None = None) -> LabSide:
    """`image_dir`: where the testbench writes camera pictures; default
    <db dir>/images for a file database, none for ":memory:".
    `state_path`: saved sim world of an earlier lab process (testbench/persist.py);
    when it exists and the database already has a ledger, the world is restored
    from it and the ledger is left as is (a fresh database is always seeded)."""
    material_recognition = (testbench_config or {}).get("material_recognition")
    if material_recognition is not None:
        from qumus2.core.optical_profiles import V1_MATERIAL_CAPTURE_PROFILE

        if material_recognition != {"backend": "v1_lab_rgb", "capture_profile": V1_MATERIAL_CAPTURE_PROFILE}:
            raise ValueError("Unknown explicit material recognition/capture profile")
    clock = SimClock()
    store = RunStore(db_path, clock=clock)
    if image_dir is None and str(db_path) != ":memory:":
        image_dir = Path(db_path).parent / "images"
    restore = (state_path is not None and Path(state_path).exists()
               and bool(store.list_samples()))
    from qumus2.testbench.bench_geometry import (
        PROFILE,
        WORKSPACE_META,
        admit_workspace_geometry,
        limits_for_bench,
    )

    try:
        admit_workspace_geometry(store, testbench_config, restoring=restore)
    except Exception:
        store.close()
        raise
    lab = build_sim_lab(clock, store=None if restore else store, seed=seed,
                        config=testbench_config, image_dir=image_dir, reactor_store=store)
    if restore:
        load_lab_state(lab, clock, state_path)
        lab.restore_optical_references(store)
    if material_recognition is not None:
        from qumus2.perception.observations import RGBMicroscopeAnalyzer
        from qumus2.testbench.render import MicroscopeRenderer

        lab.devices.get("microscope").renderer = MicroscopeRenderer(
            Path(image_dir)/"microscope" if image_dir is not None else None,
            material_capture_profile=material_recognition["capture_profile"])
        lab.analyzer = RGBMicroscopeAnalyzer(lab.analyzer)
    # the person's own 20x photo of each electrode site, taken before the run (v1, #204)
    from qumus2.testbench.user_reference import add_user_references

    add_user_references(lab, store)
    # testbench: a person's new-film report stands for the act (testbench/world.py)
    lab.world.film_reports = lambda sid: len(
        ((store.get_sample(sid) or {}).get("props") or {}).get("film_reports") or [])
    if lab.bench.get("geometry_profile") and not restore:
        store.set_meta(WORKSPACE_META, PROFILE)
    gate = SafetyGate(lab.devices, Limits.load(limits_for_bench(lab.bench, load_yaml("limits.yaml"))),
                      clock, sink=lambda k, p: store.emit(k, p),
                      frames=lab.twin.delta)
    verification = load_yaml("verification.yaml")

    def ctx_factory(run_id: str, params: dict[str, Any], project_id: str | None) -> FlowContext:
        run_gate = SafetyGate(lab.devices, gate.limits, clock,
                              sink=lambda k, p: store.emit(k, p, run_id=run_id),
                              frames=lab.twin.delta)
        # share time-based heater state across runs
        run_gate._heater_on_since = gate._heater_on_since
        run_gate._heater_last_set = gate._heater_last_set
        return FlowContext(run_id=run_id, store=store, gate=run_gate, clock=clock,
                           locator=lab.locator, bench=lab.bench, verification=verification,
                           params=params, project_id=project_id, analyzer=lab.analyzer)

    jobs = ThreadJobService(store, gate, ctx_factory, bench=lab.bench)
    # testbench "crash" fault: the job worker dies; restart and recover like a supervisor
    def recover_after_crash() -> None:
        # A scheduled simulator callback may arrive after its owner disposes the
        # local service. It must not restart workers or touch the closed store.
        with jobs._submit_lock:
            if not jobs._closing:
                jobs.recover()
    lab.faults.on_crash = recover_after_crash
    return LabSide(store, clock, lab, gate, jobs)


def build_services(db_path: str | Path = ":memory:", *,
                   scenario: str | dict[str, Any] = "place_chip_demo",
                   seed: int | None = None,
                   testbench_config: dict[str, Any] | None = None,
                   step_delay_s: float = 0.0,
                   image_dir: str | Path | None = None,
                   lab_process: bool = False, run_budget_usd: float | None = None,
                   use_openai: bool = False,
                   pre_send_admission: Any = None, http_max_retries: int = 2,
                   openai_credentials: OpenAICredentials | None = None,
                   state_path: str | Path | None = None) -> Services:
    """`lab_process=True` runs the lab side (devices, safety, jobs) as a separate
    process under a supervisor (spec 5.4); needs a file database. The returned
    `lab` and `jobs` are then proxies with the same methods.

    `run_budget_usd` caps model spend for this run on top of the per-turn cap.
    OpenAI uses configured token-rate estimates and a shared persistent ledger.

    `scenario`: a scripted scenario name, path or already loaded dict.
    `state_path`: a saved sim world to resume (testbench/checkpoint.py), with
    the run store it was saved with at `db_path` (in-process lab only)."""
    # Fail before creating a fresh bench or starting its supervisor.
    oa_creds = openai_credentials if openai_credentials is not None else (
        load_openai_credentials() if use_openai else None)
    if use_openai and oa_creds is None:
        raise RuntimeError("OpenAI requested: set QUMUS_OPENAI_API_KEY and QUMUS_OPENAI_MODEL in .env")
    supervisor = None
    session_start = _last_event_id(db_path)  # before the bench seeds the ledger
    if lab_process and state_path is not None:
        raise ValueError("state_path resumes the in-process testbench; the lab process keeps its own")
    if lab_process:
        from qumus2.runtime.remote import RemoteJobService, RemoteLab
        from qumus2.runtime.supervisor import Supervisor

        if str(db_path) == ":memory:":
            raise ValueError("a separate lab process needs a database file")
        store = RunStore(db_path)
        supervisor = Supervisor(db_path, store, seed=seed, testbench_config=testbench_config,
                                image_dir=image_dir)
        supervisor.start()
        clock: Clock = store.clock
        lab: Any = RemoteLab(supervisor)
        gate: Any = None
        jobs: Any = RemoteJobService(supervisor)
    else:
        side = build_lab_side(db_path, seed=seed, testbench_config=testbench_config,
                              image_dir=image_dir, state_path=state_path)
        store, clock, lab, gate, jobs = side.store, side.clock, side.lab, side.gate, side.jobs
        # This testbench starts from a fresh world and ledger every time, so
        # jobs the last run left open cannot go on (a real lab, or `serve
        # --lab-process`, keeps its state and recovers them instead).
        if closed := jobs.close_leftovers("the testbench was restarted from scratch"):
            store.emit("notification", {"level": "warn", "text": (
                f"{len(closed)} job(s) left open by the last run were closed "
                f"({', '.join(closed)}): this testbench restarts with a fresh bench, so they "
                "cannot continue. Use serve --lab-process to keep the bench across restarts.")})
    projects = store.list_projects()
    project_id = projects[0]["id"] if projects else store.create_project("demo")
    gateway = ToolGateway(default_registry(), store, jobs, project_id=project_id, bench=lab.bench)

    runtime: AgentRuntime
    if use_openai and oa_creds is not None:
        from qumus2.agents.openai_runtime import OpenAIRuntime

        runtime = OpenAIRuntime(gateway, oa_creds, run_budget_usd=run_budget_usd,
                                pre_send_admission=pre_send_admission,
                                http_max_retries=http_max_retries)
    else:
        runtime = ScriptedRuntime(gateway, scenario if isinstance(scenario, dict) else load_scenario(scenario),
                                  step_delay_s=step_delay_s)
    store.emit("notification", {"level": "info", "text": f"agents: {runtime.mode} mode"})
    driver = ConversationDriver(store, runtime, jobs)
    store.begin_session(session_start)
    return Services(store, clock, lab, gate, jobs, gateway, runtime, driver, project_id,
                    supervisor, session_start)


def _last_event_id(db_path: str | Path) -> int:
    """The newest event id of an existing run store (0: new or in memory)."""
    if str(db_path) == ":memory:" or not Path(db_path).exists():
        return 0
    store = RunStore(db_path)
    try:
        return store.last_event_id()
    finally:
        store.close()
