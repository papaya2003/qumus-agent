"""Jialei 2026-11-06: one robust entry. Qumus was started from different
terminals and folders, the terminal of a running server got lost (it ran for
1.5 days with no terminal, so Control+C was impossible) and a new start only
said "press Control+C in its original terminal". The server now holds a
workspace lock that records where it is; a start opens or restarts it, Stop
works from anywhere, and closing every page stops it."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qumus2.ui.presence import IdleStop
from scripts import start_qumus

FAKE_SERVER = '''
import http.server, json, os, signal, sys
if len(sys.argv) > 2 and sys.argv[2] == "ignore-int":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "pid": os.getpid(), "busy": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
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
    repo = tmp_path / "repo"
    (repo / "runs/workspace").mkdir(parents=True)
    port = _free_port()
    monkeypatch.setattr(start_qumus, "REPO", repo)
    monkeypatch.setattr(start_qumus, "PORT", port)
    monkeypatch.setattr(start_qumus, "STOP_TIMEOUT_S", 5.0)
    monkeypatch.setattr(start_qumus, "code_version", lambda: "new123")
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(start_qumus.webbrowser, "open", opened.append)
    yield repo, port, script, opened
    for pid in _children:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
    _children.clear()


_children: list[int] = []


def _start(script: Path, port: int, version: str = "new123") -> int:
    fd = start_qumus.acquire_lock()
    state = {"pid": os.getpid(), "port": port, "version": version, "started_at": time.time(),
             "phase": "starting"}
    log = start_qumus.start_background([sys.executable, str(script), str(port)], fd, state, port)
    assert log is not None and log.parent == start_qumus.REPO / start_qumus.LOGS
    server = start_qumus.find_server()
    assert server is not None
    _children.append(server.pid)
    return server.pid


def test_a_background_server_keeps_the_lock_and_is_found_from_anywhere(repo, monkeypatch, tmp_path):
    root, port, script, _ = repo
    pid = _start(script, port)
    monkeypatch.chdir(tmp_path)   # another folder, another terminal: the record is the same
    server = start_qumus.find_server()
    assert (server.pid, server.port, server.version, server.registered) == (pid, port, "new123", True)
    assert start_qumus.try_lock() is None          # the launcher let go; the server holds it
    assert start_qumus.print_status() == 0


def test_starting_again_with_the_same_version_just_opens_the_page(repo):
    root, port, script, opened = repo
    _start(script, port)
    args = start_qumus.argparse.Namespace(restart=False, yes=False, no_browser=False)
    assert start_qumus.handle_running(start_qumus.find_server(), args) is False
    assert opened == [f"http://127.0.0.1:{port}"]


def test_an_older_version_is_restarted_after_one_question(repo, capsys):
    root, port, script, opened = repo
    pid = _start(script, port, version="old000")
    args = start_qumus.argparse.Namespace(restart=False, yes=True, no_browser=False)
    assert start_qumus.handle_running(start_qumus.find_server(), args) is True
    out = capsys.readouterr().out
    assert "older code (old000; this checkout is new123)" in out and "Qumus stopped." in out
    assert not start_qumus.alive(pid) and start_qumus.port_free(port)
    fd = start_qumus.try_lock()                    # the kernel released the lock with the process
    assert fd is not None
    os.close(fd)
    assert start_qumus.find_server() is None and opened == []


def test_without_a_terminal_answer_the_running_server_is_kept(repo, monkeypatch):
    root, port, script, opened = repo
    pid = _start(script, port, version="old000")
    monkeypatch.setattr(start_qumus.sys, "stdin", open(os.devnull, encoding="utf-8"))
    args = start_qumus.argparse.Namespace(restart=False, yes=False, no_browser=False)
    assert start_qumus.handle_running(start_qumus.find_server(), args) is False
    assert start_qumus.alive(pid) and opened == [f"http://127.0.0.1:{port}"]


def test_stop_works_without_the_original_terminal(repo):
    root, port, script, _ = repo
    pid = _start(script, port)
    assert start_qumus.stop_command() == 0
    assert not start_qumus.alive(pid) and start_qumus.find_server() is None
    assert start_qumus.stop_command() == 0         # stopping twice is harmless


def test_a_server_that_ignores_control_c_is_terminated(repo):
    root, port, script, _ = repo
    process = subprocess.Popen([sys.executable, str(script), str(port), "ignore-int"])
    _children.append(process.pid)
    time.sleep(0.5)
    assert start_qumus.stop_pid(process.pid, timeout=1.0) is True
    assert process.poll() is not None           # reaped by stop_pid after SIGTERM


def test_a_server_from_before_the_lock_is_recognised_but_others_are_not(repo):
    root, port, script, _ = repo
    legacy = subprocess.Popen([sys.executable, str(script), str(port), "qumus2", "serve",
                               "--db", start_qumus.DATABASE], cwd=root)
    _children.append(legacy.pid)
    for _ in range(50):
        if not start_qumus.port_free(port):
            break
        time.sleep(0.1)
    server = start_qumus.find_server()
    assert server is not None and server.pid == legacy.pid and server.registered is False
    legacy.kill()
    legacy.wait()
    # A different Qumus (another thread's scratch replay) on the port is never ours.
    other = subprocess.Popen([sys.executable, str(script), str(port), "qumus2", "serve",
                              "--db", "/tmp/scratch/replaydb/qumus.db"], cwd=root)
    _children.append(other.pid)
    for _ in range(50):
        if not start_qumus.port_free(port):
            break
        time.sleep(0.1)
    assert start_qumus.find_server() is None


def test_closing_every_page_stops_after_the_wait_but_not_while_working():
    now = [0.0]
    working = [False]
    stops: list[int] = []
    idle = IdleStop(60, lambda: working[0], stop=lambda: stops.append(1), first_page_s=300,
                    clock=lambda: now[0])
    now[0] = 200
    assert idle.check() is False                   # no page yet: the browser may be slow
    idle.opened()
    now[0] = 1000
    assert idle.check() is False                   # a page is open
    idle.closed()
    now[0] = 1030
    assert idle.check() is False                   # a reload comes back within the wait
    idle.opened()
    idle.closed()
    now[0] = 1100
    working[0] = True
    assert idle.check() is False                   # an experiment runs: stay up
    working[0] = False
    assert idle.check() is True and stops == [1]   # it ended and still no page
    assert idle.check() is False and stops == [1]


def test_the_page_socket_and_page_requests_count_as_presence(tmp_path):
    from qumus2.app import build_services
    from qumus2.ui.app import create_app

    s = build_services(tmp_path / "qumus.db")
    app = create_app(s, start_driver=False, stop_when_closed_s=60)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["pid"] == os.getpid()
        presence = app.state.presence
        assert presence.seen is False              # the launcher's probe has no Referer
        with client.websocket_connect("/ws/events", headers={"origin": "http://testserver"}):
            assert presence.open == 1 and presence.seen
        for _ in range(50):
            if presence.open == 0:
                break
            time.sleep(0.05)
        assert presence.open == 0
        presence.last = -1e9
        client.get("/api/health", headers={"referer": "http://testserver/"})
        assert presence.last > 0
