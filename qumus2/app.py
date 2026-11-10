"""Wiring: build the brain for one run.

    services = build_services("runs/sim-lab/manual/qumus.db")      # stations.yaml, scripted agents
    services.store / .jobs / .gateway / .runtime / .driver / .lab

The brain never contains the laboratory. Every module (exfoliation, stacking,
reactor) is a contract v1 station at the address stations.yaml gives, simulated
(qumus-simlab) or real. `jobs` sends whole jobs to the stations, the record
mirror copies the stations' runs, steps, samples and events into the brain's
own store, and `lab` is the web page's read-only view of the stations.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qumus2.agents.driver import ConversationDriver
from qumus2.agents.gateway import ToolGateway
from qumus2.agents.openai_credentials import OpenAICredentials, load_openai_credentials
from qumus2.agents.runtime import AgentRuntime
from qumus2.agents.scripted import ScriptedRuntime, load_scenario
from qumus2.agents.tooling import default_registry
from qumus2.core.clock import Clock
from qumus2.runtime.store import RunStore
from qumus2.stations import config as stations_config
from qumus2.stations.config import Stations
from qumus2.stations.jobs import ContractJobService, connect
from qumus2.stations.simview import StationLab


@dataclass
class Services:
    store: RunStore
    clock: Clock
    lab: StationLab
    jobs: ContractJobService
    gateway: ToolGateway
    runtime: AgentRuntime
    driver: ConversationDriver
    project_id: str
    stations: Stations
    # the last event before this session (one serve) wrote anything: the UI's
    # "this run" is everything after it, earlier sessions are its History
    session_start: int = 0
    simlab: Any = None  # stations.launcher.SimLabProcess when this brain started the sim lab
    _closed: bool = field(default=False, init=False, repr=False)
    _close_lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def mirror(self) -> Any:
        return self.jobs.mirror

    async def close(self) -> None:
        """Release the stations' leases, stop the mirror, close the store (and a sim lab we started)."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            await self.runtime.close()
        finally:
            self.jobs.close()
            if self.simlab is not None:
                self.simlab.close()
            self.store.close()


def build_services(db_path: str | Path = ":memory:", *,
                   stations: Stations | str | Path | None = None,
                   scenario: str | dict[str, Any] = "hallbar_device",
                   step_delay_s: float = 0.0,
                   image_dir: str | Path | None = None,
                   run_budget_usd: float | None = None,
                   use_openai: bool = False,
                   pre_send_admission: Any = None, http_max_retries: int = 8,
                   openai_credentials: OpenAICredentials | None = None,
                   start_simlab: bool = False, simlab_seed: int | None = None,
                   simlab_args: list[str] | None = None,
                   client_id: str = "qumus-agent", mirror_poll_s: float = 0.5) -> Services:
    """`stations`: a Stations, a stations.yaml path, or None (search order in stations/config.py).

    `start_simlab=True` starts qumus-simlab (stations.yaml simlab_dir) when the
    simulated stations do not answer. `image_dir`: where pictures fetched from the
    stations are kept; default <db dir>/images.
    `run_budget_usd` caps model spend for this run on top of the per-turn cap.
    `scenario`: a scripted scenario name, path or already loaded dict."""
    oa_creds = openai_credentials if openai_credentials is not None else (
        load_openai_credentials() if use_openai else None)
    if use_openai and oa_creds is None:
        raise RuntimeError("OpenAI requested: set QUMUS_OPENAI_API_KEY and QUMUS_OPENAI_MODEL in .env")
    st = stations if isinstance(stations, Stations) else stations_config.load(stations)
    if image_dir is None and str(db_path) != ":memory:":
        image_dir = Path(db_path).parent / "images"
    simlab = None
    if start_simlab and st.any_sim:
        from qumus2.stations.launcher import ensure_simlab

        data_dir = Path(db_path).parent if str(db_path) != ":memory:" else Path("runs")
        simlab = ensure_simlab(st, seed=simlab_seed, data_dir=data_dir, extra_args=simlab_args)
    session_start = _last_event_id(db_path)
    store = RunStore(db_path, bench=st.bench)
    try:
        jobs = connect(st.urls, store, client_id, records_from=list(st.feeds), image_dir=image_dir,
                       mirror_poll_s=mirror_poll_s)
    except Exception:
        store.close()
        if simlab is not None:
            simlab.close()
        raise
    view_client = jobs.clients.get(st.records_from)
    lab = StationLab(store, view_client, simulated=st.modules[st.records_from].kind == "sim",
                     images=jobs.mirror.images)
    lab.refresh_bench()
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
    store.emit("stations.connected", st.to_dict())
    driver = ConversationDriver(store, runtime, jobs)
    store.begin_session(session_start)
    return Services(store, store.clock, lab, jobs, gateway, runtime, driver, project_id, st,
                    session_start, simlab)


def _last_event_id(db_path: str | Path) -> int:
    """The newest event id of an existing run store (0: new or in memory)."""
    if str(db_path) == ":memory:" or not Path(db_path).exists():
        return 0
    store = RunStore(db_path)
    try:
        return store.last_event_id()
    finally:
        store.close()
