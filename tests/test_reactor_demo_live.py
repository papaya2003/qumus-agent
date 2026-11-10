"""Opt-in: drive the REAL reactor services in --demo mode through our client.

Skipped unless QUMUS_REACTOR_REPO points to a checkout of the reactor repo
(https://github.com/shc0507/dobot_mg400) whose environment is installed
(``uv sync`` in that checkout). Nothing here moves or heats hardware: every
service runs with ``--demo`` (simulated devices) on a free loopback port, and
the agent policy guard stays on.

    QUMUS_REACTOR_REPO=~/src/dobot_mg400 pytest tests/test_reactor_demo_live.py -v

Optional: QUMUS_REACTOR_PYTHON (interpreter of that checkout; default
<repo>/.venv/bin/python or .venv\\Scripts\\python.exe), QUMUS_REACTOR_REPORT
(path of a JSON file that receives the capability table for docs/reactor-gaps.md).
We only run the reactor's code as separate processes; none of it is copied
or imported into Qumus (AGPL-3.0).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from qumus2.contract import ContractClient, ContractError
from qumus2.reactor import REACTOR_SERVICES, ReactorLab, ReactorPolicyError

REPO = os.environ.get("QUMUS_REACTOR_REPO")
pytestmark = pytest.mark.skipif(not REPO, reason="set QUMUS_REACTOR_REPO to run the reactor demo")


def _python(repo: Path) -> str:
    if os.environ.get("QUMUS_REACTOR_PYTHON"):
        return os.environ["QUMUS_REACTOR_PYTHON"]
    for candidate in (repo / ".venv" / "bin" / "python", repo / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    pytest.skip("reactor checkout has no .venv; run `uv sync` there or set QUMUS_REACTOR_PYTHON")
    return sys.executable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _demo_argv(sid: str, port: int, repo: Path) -> list[str]:
    common = ["--bind", "127.0.0.1", "--port", str(port)]
    return {
        "thermal": ["-m", "thermal_cam", "--demo", *common],
        "furnace": ["-m", "furnace", "--demo", *common],
        "microscope": ["-m", "microscope", "--demo", *common],
        "vision": ["-m", "vision", "--demo", *common],
        "stage": ["-m", "stage_controller", "--demo", *common],
        "orchestrator": ["-m", "orchestrator", "serve", "--demo",
                         "--recipes-dir", str(repo / "recipes"), *common],
    }[sid]


DEMO_SERVICES = ("thermal", "furnace", "microscope", "vision", "stage", "orchestrator")


@pytest.fixture(scope="module")
def reactor_demo():
    repo = Path(REPO).expanduser().resolve()
    if not (repo / "docs" / "adr" / "0001-instrument-service-contract.md").exists():
        pytest.skip(f"{repo} does not look like the reactor repo")
    python = _python(repo)
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    for key in [k for k in env if k.startswith("MG400_")]:
        env.pop(key)  # never inherit host settings into a demo
    logs = Path(tempfile.mkdtemp(prefix="reactor-demo-"))
    procs: dict[str, subprocess.Popen] = {}
    endpoints: dict[str, str] = {}
    for sid in DEMO_SERVICES:
        port = _free_port()
        log = open(logs / f"{sid}.log", "wb")  # noqa: SIM115 - closed with the process
        procs[sid] = subprocess.Popen([python, *_demo_argv(sid, port, repo)], cwd=repo, env=env,
                                      stdout=log, stderr=subprocess.STDOUT)
        endpoints[sid] = f"http://127.0.0.1:{port}{REACTOR_SERVICES[sid].base_path}"
    started: dict[str, str] = {}
    deadline = time.monotonic() + 60
    pending = set(DEMO_SERVICES)
    while pending and time.monotonic() < deadline:
        for sid in list(pending):
            if procs[sid].poll() is not None:
                started[sid] = f"exited {procs[sid].returncode}; see {logs / (sid + '.log')}"
                pending.discard(sid)
                continue
            try:
                ContractClient(endpoints[sid], "probe").describe()
                started[sid] = "up"
                pending.discard(sid)
            except ContractError:
                pass
        time.sleep(0.5)
    for sid in pending:
        started[sid] = f"no describe after 60 s; see {logs / (sid + '.log')}"
    yield ReactorLab(endpoints=endpoints, client_id="qumus-agent-demo-test"), started
    for proc in procs.values():
        proc.terminate()
    for proc in procs.values():
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _require(started: dict[str, str], sid: str) -> None:
    if started.get(sid) != "up":
        pytest.skip(f"reactor {sid} demo did not start: {started.get(sid)}")


@pytest.mark.parametrize("sid", DEMO_SERVICES)
def test_describe_state_events(reactor_demo, sid):
    lab, started = reactor_demo
    _require(started, sid)
    client = lab.client(sid)
    description = client.describe(refresh=True)
    assert description.contract == 1 and description.demo is True
    state = client.state()
    assert state.demo is True
    page = client.events(0)
    assert page.next >= 0


@pytest.mark.parametrize("sid", [s for s in DEMO_SERVICES if s != "orchestrator"])
def test_stop_without_lease(reactor_demo, sid):
    lab, started = reactor_demo
    _require(started, sid)
    result = lab.client(sid).stop(wait_s=1)
    assert result.stop in {"confirmed", "pending", "unconfirmed"}


def test_thermal_connect(reactor_demo):
    lab, started = reactor_demo
    _require(started, "thermal")
    lc = lab.client("thermal").run("connect", timeout_s=20)
    assert lc.ok, lc.message  # raises CommandFailed otherwise


def test_microscope_snapshot_with_lease(reactor_demo):
    lab, started = reactor_demo
    _require(started, "microscope")
    microscope = lab.client("microscope")
    with microscope.lease_keeper() as keeper:
        lc = microscope.run("snapshot", timeout_s=30)
        keeper.check()
    assert lc.ok, lc.message


def test_vision_detect_with_lease(reactor_demo):
    lab, started = reactor_demo
    _require(started, "vision")
    vision = lab.client("vision")
    cameras = vision.describe().commands["detect"].raw["params"]["properties"]["camera"]
    camera = (cameras.get("enum") or [1])[0]
    with vision.lease_keeper():
        lc = vision.run("detect", {"camera": camera}, timeout_s=60)
    assert lc.ok, lc.message


def test_policy_blocks_heat_and_raw_motion_before_sending(reactor_demo):
    lab, started = reactor_demo
    _require(started, "furnace")
    with pytest.raises(ReactorPolicyError):
        lab.client("furnace").command("apply_setpoint", {"target_c": 50, "ramp_c_per_min": 5})
    if started.get("stage") == "up":
        with pytest.raises(ReactorPolicyError):
            lab.client("stage").command("move", {"motor": "insert", "direction": "cw",
                                                 "speed": 1, "steps": 10})


def test_orchestrator_recipes_readable(reactor_demo):
    lab, started = reactor_demo
    _require(started, "orchestrator")
    recipes = lab.orchestrator_read("api/recipes")
    assert recipes


def test_capability_report(reactor_demo):
    lab, started = reactor_demo
    reports = lab.discover()
    rows = lab.capability_table(reports)
    target = os.environ.get("QUMUS_REACTOR_REPORT")
    if target:
        Path(target).write_text(json.dumps({"started": started, "rows": rows}, indent=2), encoding="utf-8")
    assert any(r["offered"] for r in rows)
