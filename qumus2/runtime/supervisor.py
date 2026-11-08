"""Supervisor of the lab process (spec 5.4).

Starts `qumus2 lab` as a child process and watches it: the process exiting or
its heartbeat going stale (frozen) gets it killed and started again. The new
process puts the devices in their safe state and recovers the runs itself
(runtime/labproc.py), because only it can talk to the devices. Too many
restarts in a short window stop the restarts and ask for a human.

Liveness is wall-clock time (RealClock), whatever clock the flows use.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from qumus2.core.clock import Clock, RealClock
from qumus2.runtime.labproc import HEARTBEAT
from qumus2.runtime.rpc import RpcClient
from qumus2.runtime.store import RunStore

log = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, db_path: str | Path, store: RunStore, *, seed: int | None = None,
                 testbench_config: dict[str, Any] | None = None,
                 image_dir: str | Path | None = None, heartbeat_s: float = 0.5,
                 stale_s: float = 5.0, start_timeout_s: float = 60.0, max_restarts: int = 5,
                 restart_window_s: float = 600.0, wall: Clock | None = None) -> None:
        self.db_path, self.store = str(db_path), store
        self.seed, self.testbench_config, self.image_dir = seed, testbench_config, image_dir
        self.heartbeat_s, self.stale_s, self.start_timeout_s = heartbeat_s, stale_s, start_timeout_s
        self.max_restarts, self.restart_window_s = max_restarts, restart_window_s
        self.wall = wall or RealClock()
        self._authkey = secrets.token_bytes(32)
        self.proc: subprocess.Popen[bytes] | None = None
        self.restarts: list[float] = []
        self.gave_up = False
        self._client: RpcClient | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._lock = threading.RLock()
        self._monitor: threading.Thread | None = None

    # ----- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._stop_orphan()
        self._spawn()
        if not self._wait_ready():
            self.stop()
            raise RuntimeError("the lab process did not start (see its output above)")
        self._monitor = threading.Thread(target=self._watch, daemon=True, name="supervisor")
        self._monitor.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def _stop_orphan(self) -> None:
        """A lab process left running by an app process that was killed would
        drive the same devices as the new one: stop it first. Only a fresh
        heartbeat counts, so the pid is surely that process, not a reused one."""
        hb = self.store.get_heartbeat(HEARTBEAT)
        if not hb or hb["status"] != "running" or self.wall.now() - hb["ts"] > self.stale_s:
            return
        try:
            os.kill(hb["pid"], signal.SIGTERM)
        except OSError:
            return
        log.warning("stopping a lab process left running (pid %s)", hb["pid"])
        t0 = self.wall.now()
        while self.wall.now() - t0 < 10.0:
            cur = self.store.get_heartbeat(HEARTBEAT)
            if cur and cur["status"] == "stopped":
                return
            self._stopping.wait(0.1)
        try:
            os.kill(hb["pid"], signal.SIGKILL)
        except OSError:
            pass

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def _spawn(self) -> None:
        env = {**os.environ, "QUMUS2_LAB_AUTHKEY": self._authkey.hex()}
        if self.testbench_config is not None:
            env["QUMUS2_TESTBENCH_CONFIG_JSON"] = json.dumps(self.testbench_config)
        cmd = [sys.executable, "-m", "qumus2", "lab", "--db", self.db_path,
               "--heartbeat", str(self.heartbeat_s)]
        if self.seed is not None:
            cmd += ["--seed", str(self.seed)]
        if self.image_dir is not None:
            cmd += ["--image-dir", str(self.image_dir)]
        with self._lock:
            self._ready.clear()
            self._client = None
            self.proc = subprocess.Popen(cmd, env=env)
        log.info("lab process started, pid %s", self.proc.pid)

    def _wait_ready(self) -> bool:
        """Until the new process writes its first heartbeat (start-up recovery done)."""
        t0 = self.wall.now()
        while self.wall.now() - t0 < self.start_timeout_s and not self._stopping.is_set():
            assert self.proc is not None
            if self.proc.poll() is not None:
                return False
            hb = self.store.get_heartbeat(HEARTBEAT)
            if hb and hb["pid"] == self.proc.pid and hb["status"] == "running":
                with self._lock:
                    self._client = RpcClient(tuple(hb["info"]["address"]), self._authkey)
                self._ready.set()
                return True
            self._stopping.wait(0.05)
        return False

    # ----- watching --------------------------------------------------------

    def problem(self) -> str | None:
        """Why the lab process needs a restart, or None if it is healthy."""
        assert self.proc is not None
        rc = self.proc.poll()
        if rc is not None:
            return f"exited with code {rc}"
        hb = self.store.get_heartbeat(HEARTBEAT)
        if hb is None or hb["pid"] != self.proc.pid:
            return "has no heartbeat"
        age = self.wall.now() - hb["ts"]
        if age > self.stale_s:
            return f"stopped responding (no heartbeat for {age:.1f} s)"
        return None

    def _watch(self) -> None:
        while not self._stopping.wait(self.heartbeat_s):
            why = self.problem()
            if why is not None and not self._stopping.is_set():
                self._restart(why)
                if self.gave_up:
                    return

    def _restart(self, why: str) -> None:
        now = self.wall.now()
        self.restarts = [t for t in self.restarts if now - t < self.restart_window_s]
        if len(self.restarts) >= self.max_restarts:
            self.gave_up = True
            with self._lock:
                self._ready.clear()
                self._client = None
            self.store.emit("notification", {
                "level": "danger", "reason": "lab_restart_exhausted", "text": f"lab process {why}; it was restarted "
                f"{len(self.restarts)} times in {self.restart_window_s / 60:.0f} min, so it is "
                "unavailable for commands and automatic restarts are stopped. A human must check the bench."})
            return
        self.restarts.append(now)
        restart_id = uuid4().hex
        self.store.emit("notification", {"level": "warn", "reason": "lab_restarting",
                                         "restart_id": restart_id, "text": f"lab process {why}; "
                                         "restarting it"})
        log.warning("lab process %s; restarting", why)
        assert self.proc is not None
        if self.proc.poll() is None:
            self.proc.kill()  # frozen: kill -9, the new process cleans up
        self.proc.wait()
        self._spawn()
        if self._wait_ready():
            self.store.emit("notification", {"level": "info", "reason": "lab_restarted",
                            "restart_id": restart_id, "text":
                            f"lab process restarted (pid {self.proc.pid})"})

    # ----- calls -----------------------------------------------------------

    def call(self, name: str, *args: Any, ready_timeout_s: float = 0.0, **kwargs: Any) -> Any:
        """RPC into the lab process. While it restarts, wait up to
        `ready_timeout_s` for it, then raise ConnectionError."""
        if not self._ready.wait(ready_timeout_s):
            raise ConnectionError("the lab process is restarting")
        with self._lock:
            client = self._client
        if client is None:
            raise ConnectionError("the lab process is restarting")
        return client.call(name, *args, **kwargs)
