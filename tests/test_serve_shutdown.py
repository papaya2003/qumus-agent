"""Ctrl+C stops `qumus2 serve` while a page is open (owner's report 2026-10-23:
the events socket only sent, never listened, so the server waited for it)."""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

websockets_sync = pytest.importorskip("websockets.sync.client")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT")
def test_ctrl_c_stops_serve_with_a_page_open(tmp_path):
    from conftest import make_fake_lab

    lab = make_fake_lab()
    stations = tmp_path / "stations.yaml"
    stations.write_text("modules:\n" + "".join(f"  {m}: {{url: '{u}', kind: sim}}\n" for m, u in lab.urls.items()))
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    env.pop("QUMUS_ANTHROPIC_API_KEY", None)
    p = subprocess.Popen([sys.executable, "-m", "qumus2", "serve", "--no-browser", "--port", str(port),
                          "--db", str(tmp_path / "qumus.db"), "--stations", str(stations), "--no-simlab"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(150):
            try:
                urllib.request.urlopen(url + "/api/health", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        with websockets_sync.connect(f"ws://127.0.0.1:{port}/ws/events?after=0",
                                     origin=url, open_timeout=10) as ws:
            ws.recv(timeout=10)  # the page is live
            p.send_signal(signal.SIGINT)
            p.wait(timeout=15)
        assert p.returncode == 0
    finally:
        if p.poll() is None:
            p.kill()
        out = p.stdout.read().decode()
        lab.close()
    assert "Exception in ASGI application" not in out
