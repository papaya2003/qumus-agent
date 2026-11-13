"""Jialei 2026-11-06: the same repository must run on the lab's Windows PC
after a plain `git clone`. There is no Windows machine in CI, so these tests
(1) scan the code for the things that break there (text files read with the
locale code page, POSIX-only process calls) and (2) pretend to be Windows to
check that every place that differs takes the Windows path. What only a real
Windows run can confirm is listed in docs/local-start.md; scripts/check_platform.py
checks it on the lab PC in one command."""
from __future__ import annotations

import ast
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qumus2.runtime import oscompat
from scripts import setup_qumus, start_qumus

REPO = Path(__file__).resolve().parents[1]
CODE_ROOTS = ("qumus2", "scripts", "tests")


def _python_files():
    for root in CODE_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith("v1compare/v1/") or "__pycache__" in rel:
                continue   # v1compare/v1 is the byte-identical frozen v1 copy
            yield rel, ast.parse(path.read_text(encoding="utf-8"))


# ----- scans -----------------------------------------------------------------

# where encoding= is (positionally) the n-th argument; Path.open has no file argument
_ENCODING_POSITION = {"read_text": 1, "write_text": 2, "open": 4}
_NOT_FILE_OPEN = {"os", "Image", "gzip", "zipfile", "tarfile", "io", "webbrowser", "sqlite3",
                  "self", "opener", "_opener", "request", "urllib"}


def _missing_encoding(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name) and func.id == "open":
        name, path_method = "open", False
    elif isinstance(func, ast.Attribute) and func.attr in _ENCODING_POSITION:
        name, path_method = func.attr, func.attr == "open"
        owner = func.value.id if isinstance(func.value, ast.Name) else \
            func.value.attr if isinstance(func.value, ast.Attribute) else None
        if path_method and owner in _NOT_FILE_OPEN:
            return False
    else:
        return False
    if any(k.arg in (None, "encoding") for k in call.keywords):
        return False
    if len(call.args) >= _ENCODING_POSITION[name] - path_method:
        return False
    if name == "open":
        mode = next((k.value for k in call.keywords if k.arg == "mode"), None)
        position = 0 if path_method else 1
        if mode is None and len(call.args) > position:
            mode = call.args[position]
        if isinstance(mode, ast.Constant) and "b" in str(mode.value):
            return False
    return True


def test_every_text_file_is_read_and_written_as_utf8():
    """Windows reads text with the locale code page (GBK on a Chinese system):
    bench.yaml's Chinese comment and experience.yaml's "°C" would break.
    ruff's PLW1514 catches only calls on a known Path; this scans every call."""
    found = [f"{rel}:{node.lineno}" for rel, tree in _python_files()
             for node in ast.walk(tree) if isinstance(node, ast.Call) and _missing_encoding(node)]
    assert found == [], "add encoding='utf-8' to: " + ", ".join(found)


# POSIX-only names and where they are allowed (behind a Windows check)
_POSIX_ATTRS = {"SIGKILL", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2", "killpg", "getpgid",
                "setsid", "setpgrp", "fork", "WNOHANG", "pwrite", "getuid", "geteuid"}
_POSIX_MODULES = {"fcntl", "termios", "pwd", "grp", "resource"}
_POSIX_KEYWORDS = {"pass_fds", "preexec_fn"}
_GUARDED = {
    # every use sits behind `if WINDOWS` / hasattr / getattr; tested below
    "scripts/start_qumus.py": {"fcntl", "pass_fds", "WNOHANG", "pwrite"},
    "qumus2/runtime/oscompat.py": {"SIGKILL"},
    "qumus2/cli.py": {"SIGUSR1"},                          # behind hasattr(signal, "SIGUSR1")
    "qumus2/stations/launcher.py": {".venv/bin"},          # tries .venv/Scripts/python.exe too
    "tests/test_start_entry_robust.py": {"SIGKILL", "WNOHANG"},   # runs on POSIX only
    "tests/test_windows_portability.py": {"SIGKILL", ".venv/bin", "libSystem"},   # the scan itself
}


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant):
            ids.add(id(body[0].value))
    return ids


def test_no_new_posix_only_calls():
    found = []
    for rel, tree in _python_files():
        allowed = _GUARDED.get(rel, set())
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            hit = None
            if isinstance(node, ast.Attribute) and node.attr in _POSIX_ATTRS:
                hit = node.attr
            elif isinstance(node, ast.Import | ast.ImportFrom):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                hit = next((n for n in names if n.split(".")[0] in _POSIX_MODULES), None)
            elif isinstance(node, ast.keyword) and node.arg in _POSIX_KEYWORDS:
                hit = node.arg
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs and (".venv/bin" in node.value
                                                  or "/usr/lib/libSystem" in node.value):
                hit = ".venv/bin" if ".venv/bin" in node.value else "libSystem"
            if hit and hit not in allowed and not (hit == "libSystem"
                                                  and rel == "qumus2/ui/recording.py"):
                found.append(f"{rel}:{getattr(node, 'lineno', '?')} {hit}")
    assert found == [], ("POSIX-only (breaks on Windows); use qumus2/runtime/oscompat.py "
                         "or guard it and list it here: " + ", ".join(found))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX locale switch")
def test_qumus_starts_when_the_system_default_encoding_is_not_utf8(tmp_path):
    """The lab PC's default is GBK. A C locale without UTF-8 coercion gives an
    ASCII default here, which fails the same way: before the fix, loading the
    bench config raised UnicodeDecodeError (bench.yaml has Chinese text)."""
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0",
           "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}
    code = ("import locale, sys\n"
            "assert locale.getpreferredencoding(False).lower() not in ('utf-8', 'utf8')\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "from conftest import make_fake_lab\n"
            "from qumus2.app import build_services\n"
            "lab = make_fake_lab()\n"
            "build_services(sys.argv[1], stations=lab.stations())\n"
            "lab.close()\n")
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "qumus.db"),
                             str(REPO / "tests")], env=env,
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr[-2000:]


# ----- pretend Windows: process helpers --------------------------------------

def test_force_kill_without_sigkill_uses_terminate(monkeypatch):
    sent = []
    monkeypatch.delattr(signal, "SIGKILL")
    monkeypatch.setattr(oscompat.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    oscompat.force_kill(123)
    assert sent == [(123, signal.SIGTERM)]   # on Windows: TerminateProcess


def test_interrupt_self_on_windows_raises_in_process(monkeypatch):
    raised, killed = [], []
    monkeypatch.setattr(oscompat, "WINDOWS", True)
    monkeypatch.setattr(oscompat.signal, "raise_signal", raised.append)
    monkeypatch.setattr(oscompat.os, "kill", lambda *a: killed.append(a))
    oscompat.interrupt_self()
    assert raised == [signal.SIGINT] and killed == []   # os.kill would TerminateProcess


def test_stop_signals_include_control_break_where_it_exists(monkeypatch):
    assert set(oscompat.stop_signals()) >= {signal.SIGTERM, signal.SIGINT}
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    assert 21 in oscompat.stop_signals()


def test_pid_alive_never_signals_a_process(monkeypatch):
    calls = []
    monkeypatch.setattr(start_qumus, "WINDOWS", True)
    monkeypatch.setattr(oscompat, "pid_alive", lambda pid: calls.append(pid) or True)
    monkeypatch.setattr(start_qumus.os, "kill", lambda *a: pytest.fail("os.kill on Windows"))
    assert start_qumus.alive(4321) is True and calls == [4321]


def test_windows_stop_falls_back_to_ending_the_process_tree(monkeypatch):
    ran = []
    monkeypatch.setattr(start_qumus, "WINDOWS", True)
    monkeypatch.setattr(start_qumus, "_run", lambda command: ran.append(command) or "")
    monkeypatch.setattr(start_qumus, "_wait_gone", lambda pid, timeout: bool(ran))
    monkeypatch.setattr(start_qumus.os, "kill", lambda *a: pytest.fail("os.kill on Windows"))
    assert start_qumus.stop_pid(77, 1.0) is True
    assert ran == [["taskkill", "/PID", "77", "/T", "/F"]]


# ----- the HTTP stop -----------------------------------------------------------

def test_shutdown_endpoint_needs_the_launcher_token(tmp_path, monkeypatch):
    from qumus2.app import build_services
    from qumus2.ui import app as ui_app

    stopped = []
    monkeypatch.setattr(ui_app, "interrupt_self", lambda: stopped.append(1))
    s = build_services(tmp_path / "qumus.db")
    url, header = "/api/server/shutdown", ui_app.SHUTDOWN_TOKEN_HEADER
    with TestClient(ui_app.create_app(s, start_driver=False)) as client:   # one event loop
        monkeypatch.delenv(ui_app.SHUTDOWN_TOKEN_ENV, raising=False)
        assert client.post(url, headers={header: ""}).status_code == 403   # no token configured
        monkeypatch.setenv(ui_app.SHUTDOWN_TOKEN_ENV, "secret-token")
        assert client.post(url).status_code == 403
        assert client.post(url, headers={header: "wrong"}).status_code == 403
        assert stopped == []
        assert client.post(url, headers={header: "secret-token"}).json() == {"stopping": True}
        for _ in range(20):
            if stopped:
                break
            client.get("/api/health")   # lets the loop run the stop scheduled after the reply
            time.sleep(0.1)
    assert stopped == [1]


FAKE_SERVER = '''
import http.server, json, os, signal, sys, threading
signal.signal(signal.SIGINT, signal.SIG_IGN)      # only the HTTP stop can end it politely
TOKEN = os.environ.get("QUMUS_SHUTDOWN_TOKEN")
class H(http.server.BaseHTTPRequestHandler):
    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self._reply(200, {"ok": True, "pid": os.getpid(), "busy": False})
    def do_POST(self):
        if self.headers.get("X-Qumus-Shutdown-Token") != TOKEN:
            return self._reply(403, {})
        self._reply(200, {"stopping": True})
        threading.Timer(0.2, lambda: os._exit(0)).start()
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    port = _free_port()
    monkeypatch.setattr(start_qumus, "REPO", root)
    monkeypatch.setattr(start_qumus, "PORT", port)
    monkeypatch.setattr(start_qumus, "STOP_TIMEOUT_S", 10.0)
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    pids: list[int] = []
    yield root, port, script, pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("handoff", [False, True], ids=["mac", "windows-handoff"])
def test_background_start_records_the_server_and_stops_it_over_http(repo, monkeypatch, handoff):
    """handoff=True is the Windows way: the launcher lets go of the lock and the
    server process (scripts/start_qumus.py --serve-holding-lock) takes it itself."""
    if sys.platform == "win32" and not handoff:
        pytest.skip("Windows always hands the lock off")
    root, port, script, pids = repo
    monkeypatch.setattr(start_qumus, "LOCK_HANDOFF", handoff)
    fd = start_qumus.acquire_lock()
    state = {"pid": os.getpid(), "port": port, "version": "v1", "started_at": time.time(),
             "phase": "starting"}
    assert start_qumus.start_background([sys.executable, str(script), str(port)], fd, state, port)
    for _ in range(50):   # the handed-off server marks itself running once it answers
        server = start_qumus.find_server()
        if server and not server.starting:
            break
        time.sleep(0.1)
    pids.append(server.pid)
    assert server.pid == start_qumus.health(port)["pid"]   # the record names the server itself
    assert server.token and not server.starting
    assert start_qumus.try_lock() is None                  # and it holds the lock
    t0 = time.monotonic()
    assert start_qumus.stop_server(server) is True         # SIGINT is ignored: HTTP did it
    assert time.monotonic() - t0 < 8.0
    fd = start_qumus.try_lock()
    assert fd is not None
    os.close(fd)


def test_handed_off_server_refuses_to_start_while_the_lock_is_held(repo):
    root, port, script, _ = repo
    fd = start_qumus.acquire_lock()
    try:
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts/start_qumus.py"), "--serve-holding-lock",
             str(root / start_qumus.LOCK), "--", sys.executable, str(script), str(port)],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        os.close(fd)
    assert result.returncode == 1 and "holds the workspace lock" in result.stderr


def test_lock_record_is_readable_while_locked(repo):
    """Windows locks are mandatory: the lock sits on a byte past the record."""
    fd = start_qumus.acquire_lock()
    try:
        start_qumus.write_state(fd, {"pid": 5, "port": 9})
        assert start_qumus.read_state() == {"pid": 5, "port": 9}
    finally:
        os.close(fd)


def test_write_state_without_pwrite(repo, monkeypatch):
    monkeypatch.delattr(start_qumus.os, "pwrite")
    fd = start_qumus.acquire_lock()
    try:
        start_qumus.write_state(fd, {"pid": 123456789, "port": 1})
        start_qumus.write_state(fd, {"pid": 1})   # shorter: no tail of the old record
        assert start_qumus.read_state() == {"pid": 1}
    finally:
        os.close(fd)


# ----- pretend Windows: setup and launchers ------------------------------------

def test_setup_uses_the_windows_venv_layout_and_writes_bat_launchers(tmp_path, monkeypatch):
    repo = tmp_path / "Qumus Lab" / "v2"
    templates = repo / "scripts/templates"
    templates.mkdir(parents=True)
    (repo / ".env.example").write_text("PLACEHOLDER=\n", encoding="utf-8")
    for name in ("Start Qumus.bat", "Stop Qumus.bat"):
        (templates / name).write_bytes((REPO / "scripts/templates" / name).read_bytes())
    monkeypatch.setattr(setup_qumus, "REPO", repo)
    monkeypatch.setattr(setup_qumus, "WINDOWS", True)
    assert setup_qumus.venv_python(repo / ".venv") == repo / ".venv/Scripts/python.exe"
    setup_qumus.prepare_local_files()
    start = (tmp_path / "Qumus Lab" / "Start Qumus.bat").read_bytes()
    stop = (tmp_path / "Qumus Lab" / "Stop Qumus.bat").read_bytes()
    assert b'call "%~dp0v2\\scripts\\qumus_launcher.bat" %*' in start
    assert str(tmp_path).encode() not in start  # no absolute (possibly non-ASCII) path
    assert b"--stop" in stop and b"\r\n" in start and b"\n" not in start.replace(b"\r\n", b"")
    assert not (tmp_path / "Qumus Lab" / "Start Qumus.command").exists()
    monkeypatch.setattr(setup_qumus, "WINDOWS", False)
    assert setup_qumus.venv_python(repo / ".venv") == repo / ".venv/bin/python"


@pytest.mark.parametrize("name", ["Start Qumus.bat", "Stop Qumus.bat", "scripts/qumus_launcher.bat",
                                  "scripts/templates/Start Qumus.bat",
                                  "scripts/templates/Stop Qumus.bat"])
def test_bat_files_have_crlf_and_call_the_shared_launcher(name):
    data = (REPO / name).read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b""), "cmd.exe needs CRLF"
    assert data.startswith(b"@echo off")
    text = data.decode("ascii")
    if name.endswith("qumus_launcher.bat"):
        assert r".venv\Scripts\python.exe" in text and r"scripts\start_qumus.py" in text
        assert 'set "PYTHONUTF8=1"' in text
    else:
        assert r"scripts\qumus_launcher.bat" in text
        assert ("--stop" in text) == name.endswith("Stop Qumus.bat")
    assert "*.bat text eol=crlf" in (REPO / ".gitattributes").read_text(encoding="utf-8")


# ----- exports: the photo "clone" outside macOS --------------------------------

def test_clone_file_falls_back_to_a_link_then_a_copy(tmp_path, monkeypatch):
    from qumus2.ui import recording

    src = tmp_path / "photo.jpg"
    src.write_bytes(b"jpeg")
    monkeypatch.setattr(recording.sys, "platform", "win32")
    assert recording.clone_file(src, tmp_path / "a.jpg") == "link"

    def no_link(*_):
        raise OSError("hard links not supported on this volume (FAT/exFAT, network drive)")

    monkeypatch.setattr(recording.os, "link", no_link)
    assert recording.clone_file(src, tmp_path / "b.jpg") == "copy"
    assert (tmp_path / "b.jpg").read_bytes() == b"jpeg"
