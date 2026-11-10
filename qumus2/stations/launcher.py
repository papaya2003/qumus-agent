"""Start the simulated laboratory (qumus-simlab) when the stations are simulated.

    ensure_simlab(stations, seed=3, data_dir="runs/sim-lab/...")

If every `kind: sim` station already answers describe(), nothing is started (a
sim lab someone else runs is used as is). Otherwise the checkout at
stations.yaml `simlab_dir` is started as its own process:

    <simlab python> -m qumus_simlab serve --seed <seed> --db <data_dir>/simlab/lab-<time>.db

with that checkout's own virtual environment when it has one (.venv), else this
Python. The sim lab keeps its own store and pictures; the brain reaches it only
over HTTP. The process is stopped when the brain closes it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qumus2.contract import ContractClient, ContractError
from qumus2.stations.config import Stations


@dataclass
class SimLabProcess:
    process: subprocess.Popen[bytes] | None
    db_path: Path | None
    log_path: Path | None

    def close(self, timeout_s: float = 10.0) -> None:
        p = self.process
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=timeout_s)


def _answers(url: str, adapter: str = "contract") -> bool:
    if adapter == "console":  # reactor Lab Console: a GET of its state, nothing else
        from qumus2.reactor.console import ReactorConsole

        try:
            ReactorConsole(url, timeout_s=1.0).overview()
            return True
        except (ContractError, ValueError):
            return False
    try:
        ContractClient(url, "qumus-agent-probe", timeout_s=1.0).describe()
        return True
    except (ContractError, ValueError):
        return False


def _is_local(url: str) -> bool:
    import ipaddress
    import urllib.parse

    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def sim_offline(stations: Stations) -> list[str]:
    return [m for m, s in stations.modules.items()
            if s.kind == "sim" and not _answers(s.url, s.adapter)]


def simlab_python(simlab_dir: Path) -> str:
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        p = simlab_dir / rel
        if p.exists():
            return str(p)
    return sys.executable


def ensure_simlab(stations: Stations, *, seed: int | None = None, data_dir: str | Path = "runs",
                  wait_s: float = 120.0, extra_args: list[str] | None = None,
                  env: dict[str, str] | None = None) -> SimLabProcess:
    """Make sure the simulated stations answer; start qumus-simlab if needed."""
    offline = sim_offline(stations)
    if not offline:
        return SimLabProcess(None, None, None)
    # Only a sim lab on this computer can be started from here. A station on
    # another computer (LAN address, or a tunnel the other side opened) is
    # started there; the brain then just connects (same protocol either way).
    remote = [m for m in offline if not _is_local(stations.modules[m].url)]
    if remote:
        raise RuntimeError(
            f"station(s) {', '.join(remote)} at "
            f"{', '.join(stations.modules[m].url for m in remote)} do not answer; they run on another "
            "computer, so start qumus-simlab there (or open the SSH tunnel) and try again.")
    simlab_dir = stations.simlab_dir
    if simlab_dir is None or not (simlab_dir / "qumus_simlab").is_dir():
        raise RuntimeError(
            f"simulated station(s) {', '.join(offline)} do not answer and no qumus-simlab checkout was "
            f"found at {simlab_dir} (stations.yaml simlab_dir). Start it yourself with "
            "`python -m qumus_simlab serve --seed 3 --db <path>` or fix the path.")
    root = Path(data_dir).resolve() / "simlab"
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = root / f"lab-{stamp}.log"
    db = root / f"lab-{stamp}.db"
    if "--resume" in (extra_args or []):
        # qumus-simlab restores a save point only into <new folder>/qumus.db
        db = root / f"lab-{stamp}" / "qumus.db"
        db.parent.mkdir(parents=True, exist_ok=False)
    cmd = [simlab_python(simlab_dir), "-m", "qumus_simlab", "serve", "--db", str(db)]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    cmd += list(extra_args or [])
    run_env = {**os.environ, **(env or {})}
    with log.open("ab") as out:
        proc = subprocess.Popen(cmd, cwd=simlab_dir, stdout=out, stderr=subprocess.STDOUT, env=run_env)
    handle = SimLabProcess(proc, db, log)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"qumus-simlab exited with code {proc.returncode}; see {log}")
        if not sim_offline(stations):
            return handle
        time.sleep(0.5)
    handle.close()
    raise RuntimeError(f"qumus-simlab did not answer within {wait_s:.0f} s; see {log}")


def describe_stations(stations: Stations) -> list[dict[str, Any]]:
    """Online/offline and sim/real of every station (the page's lab panel)."""
    return [{**s.to_dict(), "online": _answers(s.url, s.adapter)} for s in stations.modules.values()]
