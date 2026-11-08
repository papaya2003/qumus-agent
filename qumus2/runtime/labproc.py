"""The lab process (spec 5.3, 5.4): devices, safety layer and job service in a
process of their own, so neither a crashed agent/UI process cuts a hardware
flow in half nor a crashed job process takes the UI down.

    qumus2 lab --db runs/checks/manual/qumus.db       (started by runtime/supervisor.py)

Start-up: restore the sim world a dead predecessor left (testbench/persist.py);
if that predecessor did not stop cleanly, put every device in its safe state
first; then `jobs.recover()` resumes or parks the runs it left (spec 8.2).
Afterwards it serves the JobService and a read-only lab view over local RPC
(runtime/rpc.py) and writes a heartbeat to the run store every `heartbeat_s`.
"""

from __future__ import annotations

import json
import os
import signal
import threading
from pathlib import Path
from typing import Any

from qumus2.core.clock import RealClock
from qumus2.testbench.persist import save_lab_state

HEARTBEAT = "lab"
#: read-only device methods the UI's lab panel may call (never a command)
DEVICE_READS = ("get_pose", "vacuum_on", "read_temperature", "read_flow", "get_target", "get",
                "get_positions", "world_pose", "is_homed", "remaining_mm", "focus_plane",
                "get_objective", "get_focus")


def device_states(lab: Any) -> list[dict[str, Any]]:
    out = []
    for dev in lab.devices.devices.values():
        attrs = {"device_id": dev.device_id, "offline": bool(getattr(dev, "offline", False)),
                 "stuck_at": getattr(dev, "stuck_at", None)}
        reads: dict[str, Any] = {}
        for m in DEVICE_READS:
            if callable(getattr(dev, m, None)):
                if m in ("read_temperature", "read_flow") and attrs["offline"]:
                    reads[m] = ("err", f"{dev.device_id} offline")
                    continue
                try:
                    reads[m] = ("ok", getattr(dev, m)())
                except Exception as e:  # noqa: BLE001 - a status read never breaks the page
                    reads[m] = ("err", str(e))
        out.append({"class": type(dev).__name__, "attrs": attrs, "reads": reads})
    return out


def rpc_methods(side: Any) -> dict[str, Any]:
    jobs, lab = side.jobs, side.lab
    return {
        "ping": lambda: {"pid": os.getpid()},
        "jobs.submit": jobs.submit, "jobs.wait": jobs.wait, "jobs.status": jobs.status,
        "jobs.cancel": jobs.cancel, "jobs.stop_all": jobs.stop_all,
        "jobs.retry": jobs.retry, "jobs.reverify": jobs.reverify,
        "jobs.review_waiting": jobs.review_waiting,
        "jobs.resume": jobs.resume, "jobs.resume_all": jobs.resume_all,
        "jobs.complete_experiment": jobs.complete_experiment,
        "lab.bench": lambda: lab.bench,
        "lab.world_snapshot": lab.world.snapshot,
        "lab.evaluation_snapshot": lab.world.evaluation_snapshot,
        "lab.stamp_snapshot": lab.world.stamp_snapshot,
        "lab.tape_snapshot": lab.world.tape_snapshot,
        "lab.world_faults": lambda: dict(vars(lab.world.faults)),
        "lab.device_states": lambda: device_states(lab),
        "lab.twin_state": lab.twin.state,
        "lab.preview": lab.preview,
        "faults.inject": lab.faults.inject, "faults.describe": lab.faults.describe,
    }


def run_lab_process(db_path: str | Path, *, authkey: bytes, seed: int | None = None,
                    testbench_config: dict[str, Any] | None = None,
                    image_dir: str | Path | None = None, heartbeat_s: float = 0.5) -> None:
    from qumus2.app import build_lab_side
    from qumus2.runtime.rpc import RpcServer

    wall = RealClock()
    state_path = Path(db_path).with_suffix(".testbench.pkl")  # one per database
    side = build_lab_side(db_path, seed=seed, testbench_config=testbench_config,
                          image_dir=image_dir, state_path=state_path)
    store, lab = side.store, side.lab
    prev = store.get_heartbeat(HEARTBEAT)
    if prev is not None and prev["status"] != "stopped":
        failed = lab.devices.all_safe_state()
        if lab.world.model.get("process", {}).get("restart_loses_arm_position", False):
            # the arm controllers restarted too: they must home before moving
            for d in lab.devices.devices.values():
                if hasattr(d, "lose_position"):
                    d.lose_position()
        store.emit("notification", {
            "level": "warn", "text": "lab process restarted after it stopped unexpectedly: "
            "all devices put in their safe state, recovering runs",
            "safe_state_failed": failed})
    lab.faults.hard_kill = True
    lab.faults.on_crash = None
    lab.faults.persist = lambda: save_lab_state(lab, side.clock, state_path)
    save_lab_state(lab, side.clock, state_path)
    decisions = side.jobs.recover()

    server = RpcServer(rpc_methods(side), authkey)
    server.start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    info = {"address": list(server.address), "recovered": len(decisions)}
    while not stop.is_set():
        if not lab.faults.hung.is_set():  # a frozen process writes no heartbeat
            store.beat(HEARTBEAT, pid=os.getpid(), ts=wall.now(), info=info)
        stop.wait(heartbeat_s)
    # clean stop: devices safe, world saved, heartbeat says so
    lab.devices.all_safe_state()
    save_lab_state(lab, side.clock, state_path)
    store.beat(HEARTBEAT, pid=os.getpid(), ts=wall.now(), status="stopped", info=info)
    server.close()


def main(args: Any) -> None:
    key = os.environ.get("QUMUS2_LAB_AUTHKEY")
    if not key:
        raise SystemExit("QUMUS2_LAB_AUTHKEY is not set (the supervisor starts this process)")
    cfg = os.environ.get("QUMUS2_TESTBENCH_CONFIG_JSON")
    run_lab_process(args.db, authkey=bytes.fromhex(key), seed=args.seed,
                    testbench_config=json.loads(cfg) if cfg else None,
                    image_dir=args.image_dir, heartbeat_s=args.heartbeat)
