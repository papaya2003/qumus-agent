"""One-command self-check that the brain and qumus-simlab work on this computer
(macOS or Windows).

    macOS:    .venv/bin/python scripts/check_platform.py
    Windows:  .venv\\Scripts\\python.exe scripts\\check_platform.py

Free: scripted agents only, no model calls, no real instruments. It never
touches the everyday workspace (runs/sim-lab) or a running Qumus: everything
happens in a temporary folder on a free port (the sim lab's own ports 8781-8783
must be free, so stop a running Qumus first), with the same launcher code
(scripts/start_qumus.py) that Start Qumus uses. Stages:

  install   Python version, packages, the launcher files for this system
  encoding  every tracked text file the program reads decodes as UTF-8
  start     start a server in the background (lock, detached process); it starts
            qumus-simlab from the checkout beside this one (../qumus-simlab)
  run       a short scripted simulation: put a chip on the transfer stage
  stop      stop it the way Stop Qumus does; qumus-simlab stops with it
  export    turn the run into a recording (as History shows them)

Prints PASS/FAIL per stage and exits 0 only when all pass. --keep leaves the
temporary folder for inspection.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import start_qumus  # noqa: E402

WINDOWS = os.name == "nt"
# The server is on this computer: never send its requests through a system proxy.
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PACKAGES = ("qumus2", "numpy", "shapely", "PIL", "yaml", "pydantic", "fastapi", "uvicorn",
            "imageio_ffmpeg")
OPTIONAL = ("cv2", "scipy", "skimage", "klayout", "agents")
RUN_TIMEOUT_S = 300.0
SIMLAB_PORTS = (8781, 8782, 8783)
SIMLAB_DIR = REPO.parent / "qumus-simlab"


def _port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port: int, path: str) -> dict:
    with LOCAL_HTTP.open(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
        return json.loads(response.read())


def _post(port: int, path: str, body: dict) -> dict:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with LOCAL_HTTP.open(request, timeout=10) as response:
        return json.loads(response.read())


class Check:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.port = _free_port()
        # relative to the folder the server runs in, as Start Qumus passes it
        self.db_arg = "runs/self-check/qumus.db"
        self.db = work / self.db_arg
        self.server: start_qumus.Server | None = None

    # ----- stages ------------------------------------------------------------

    def install(self) -> str:
        if sys.version_info < (3, 11):  # noqa: UP036 - the venv may be wrong
            raise AssertionError(f"Python {platform.python_version()} is older than 3.11")
        for name in PACKAGES:
            importlib.import_module(name)
        missing = []
        for name in OPTIONAL:
            try:
                importlib.import_module(name)
            except ImportError:
                missing.append(name)
        names = (("Start Qumus.bat", "Stop Qumus.bat", "scripts/qumus_launcher.bat") if WINDOWS
                 else ("Start Qumus.command", "Stop Qumus.command", "scripts/qumus_launcher.zsh"))
        absent = [n for n in names if not (REPO / n).is_file()]
        if absent:
            raise AssertionError("launcher files missing: " + ", ".join(absent))
        venv = REPO / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")
        note = f"Python {platform.python_version()} on {platform.system()} {platform.release()}"
        if not venv.exists():
            note += "; .venv not found (run scripts/setup_qumus.py before using the launcher)"
        if missing:
            note += "; optional packages missing: " + ", ".join(missing)
        return note

    def encoding(self) -> str:
        files = subprocess.run(["git", "-C", str(REPO), "ls-files", "qumus2", "scripts"],
                               capture_output=True, text=True, encoding="utf-8", check=True)
        bad, count = [], 0
        for name in files.stdout.splitlines():
            if Path(name).suffix not in (".py", ".yaml", ".yml", ".json", ".md", ".html",
                                         ".js", ".css", ".txt"):
                continue
            count += 1
            try:
                (REPO / name).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                bad.append(name)
        if bad:
            raise AssertionError("not UTF-8: " + ", ".join(bad))
        return f"{count} files read as UTF-8 (system default: {sys.getfilesystemencoding()}, " \
               f"locale {__import__('locale').getpreferredencoding(False)})"

    def start(self) -> str:
        if not (SIMLAB_DIR / "qumus_simlab").is_dir():
            raise AssertionError(f"no qumus-simlab checkout at {SIMLAB_DIR}; clone it beside "
                                 "qumus-agent and install it (its own .venv)")
        busy = [p for p in SIMLAB_PORTS if _port_in_use(p)]
        if busy:
            raise AssertionError(f"port(s) {busy} already in use: stop the running Qumus or "
                                 "qumus-simlab first (this check starts its own)")
        start_qumus.REPO = self.work           # its lock, logs and cwd: the temporary folder
        start_qumus.PORT = self.port
        fd = start_qumus.acquire_lock()
        state = {"pid": os.getpid(), "port": self.port, "version": "self-check",
                 "started_at": time.time(), "phase": "starting", "repository": str(REPO)}
        self.db.parent.mkdir(parents=True, exist_ok=True)
        # the packaged stations with the simlab checkout as an absolute path (cwd is temporary)
        stations = (REPO / "qumus2" / "config" / "stations.yaml").read_text(encoding="utf-8")
        stations = stations.replace("simlab_dir: ../qumus-simlab",
                                    f"simlab_dir: {json.dumps(SIMLAB_DIR.as_posix())}")
        (self.work / "stations.yaml").write_text(stations, encoding="utf-8")
        command = [sys.executable, "-m", "qumus2", "serve", "--db", self.db_arg,
                   "--stations", "stations.yaml", "--seed", "3",
                   "--host", "127.0.0.1", "--port", str(self.port), "--no-browser"]
        log = start_qumus.start_background(command, fd, state, self.port)
        if log is None:
            raise AssertionError("the server did not come up (its log is printed above)")
        self.server = start_qumus.find_server()
        if self.server is None or not start_qumus.alive(self.server.pid):
            raise AssertionError("started, but the lock record does not name a running server")
        if start_qumus.try_lock() is not None:
            raise AssertionError("the server does not hold the workspace lock")
        health = _get(self.port, "/api/health")
        if health.get("pid") != self.server.pid:
            raise AssertionError(f"lock names pid {self.server.pid}, server is {health.get('pid')}")
        return f"pid {self.server.pid} on port {self.port}, mode {health.get('mode')}"

    def run(self) -> str:
        # The scripted team goes on towards a whole Hall bar (hours); the check
        # passes as soon as its first laboratory job has succeeded.
        _post(self.port, "/api/messages", {"text": "Put a chip on the transfer stage"})
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while time.monotonic() < deadline:
            runs = _get(self.port, "/api/snapshot").get("runs") or []
            statuses = [str(r.get("status")) for r in runs]
            bad = sorted({x for x in statuses if x in ("failed", "aborted", "locked")})
            if bad:
                raise AssertionError("a lab job ended as " + ", ".join(bad))
            if "succeeded" in statuses:
                return f"{statuses.count('succeeded')} lab job(s) succeeded through qumus-simlab"
            time.sleep(1.0)
        raise AssertionError(f"no lab job succeeded within {RUN_TIMEOUT_S:.0f} s")

    def stop(self) -> str:
        assert self.server is not None, "start failed"
        if not self.server.token:
            raise AssertionError("the lock record has no stop token")
        if not start_qumus.stop_server(self.server):
            raise AssertionError("the server did not stop")
        if start_qumus.alive(self.server.pid):
            raise AssertionError("the server process still runs")
        deadline = time.monotonic() + 20.0
        while any(_port_in_use(p) for p in SIMLAB_PORTS) and time.monotonic() < deadline:
            time.sleep(0.5)
        still = [p for p in SIMLAB_PORTS if _port_in_use(p)]
        if still:
            raise AssertionError(f"qumus-simlab still answers on {still} after the server stopped")
        fd = start_qumus.try_lock()
        if fd is None:
            raise AssertionError("the workspace lock was not released")
        os.close(fd)
        return "stopped over HTTP; qumus-simlab stopped with it; lock released"

    def export(self) -> str:
        out = self.work / "recording"
        subprocess.run([sys.executable, "-m", "qumus2", "export", "--db", self.db_arg,
                        "--out", str(out), "--title", "platform self-check"],
                       cwd=self.work, check=True, capture_output=True)
        files = [p for p in out.rglob("*") if p.is_file()]
        if not (out / "recording.json").is_file() and not files:
            raise AssertionError("the recording folder is empty")
        return f"{len(files)} files in the recording"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder")
    parser.add_argument("--handoff", action="store_true",
                        help="use the Windows way of starting (the server takes the lock itself)")
    args = parser.parse_args()
    if args.handoff:
        start_qumus.LOCK_HANDOFF = True
    work = Path(tempfile.mkdtemp(prefix="qumus-self-check-"))
    check = Check(work)
    stages: list[tuple[str, Callable[[], str]]] = [
        ("install", check.install), ("encoding", check.encoding), ("start", check.start),
        ("run", check.run), ("stop", check.stop), ("export", check.export)]
    failed = False
    print(f"Qumus self-check in {work}", flush=True)
    try:
        for name, stage in stages:
            if failed and name != "stop":
                print(f"SKIP  {name}", flush=True)
                continue
            if failed and check.server is None:
                continue
            try:
                note = stage()
            except Exception as error:  # noqa: BLE001 - report every stage
                failed = True
                print(f"FAIL  {name}: {error}", flush=True)
                if os.environ.get("QUMUS_SELF_CHECK_TRACE"):
                    traceback.print_exc()
            else:
                print(f"PASS  {name}: {note}", flush=True)
    finally:
        if check.server is not None and start_qumus.alive(check.server.pid):
            start_qumus.stop_pid(check.server.pid, 10.0, port=check.port, token=check.server.token)
        logs = sorted((work / start_qumus.LOGS).glob("*.log"))
        if failed and logs:
            print(f"Server log: {logs[-1]}")
        if args.keep or failed:
            print(f"Kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print("ALL PASS" if not failed else "SOME STAGES FAILED", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
