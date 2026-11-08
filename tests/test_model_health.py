"""Model status for the UI (agents/openai_health.py, /api/health, /api/continue, WS origin)."""

from __future__ import annotations

import asyncio
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.agents.openai_health import OpenAIHealth
from qumus2.core.http import tls_context
from qumus2.ui.app import create_app

KEY = "sk-test-0123456789abcdefghij"


@pytest.fixture
def gateway(monkeypatch):
    """A local stand-in for the gateway: answers `code` and records headers."""
    for v in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(v, raising=False)
    seen: list[dict[str, str]] = []
    state = {"code": 200}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
            body = b'{"data": []}'
            self.send_response(state["code"])
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", state, seen
    srv.shutdown()


def test_reachable_with_bearer_key(gateway):
    url, _, seen = gateway
    h = OpenAIHealth(OpenAICredentials(KEY, base_url=url + "/v1/", model_id="test"))
    r = h.check()
    assert r["state"] == "ok" and r["checked_at"]
    assert seen[-1]["path"] == "/v1/models"
    assert seen[-1]["authorization"] == f"Bearer {KEY}" and "x-api-key" not in seen[-1]


def test_refused_key_and_server_error(gateway):
    url, state, seen = gateway
    h = OpenAIHealth(OpenAICredentials(KEY, base_url=url + "/v1", model_id="test"))
    state["code"] = 401
    assert h.check()["state"] == "auth_failed"
    assert seen[-1]["authorization"] == f"Bearer {KEY}"
    state["code"] = 503
    assert h.check()["state"] == "error"
    assert KEY not in str(h.state)


def test_unreachable_gateway(monkeypatch):
    for v in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(v, raising=False)
    h = OpenAIHealth(OpenAICredentials(KEY, base_url="http://127.0.0.1:9/v1", model_id="test"), timeout_s=2)
    r = h.check()
    assert r["state"] == "unreachable" and "127.0.0.1:9" in r["detail"]


def test_check_trusts_the_certifi_bundle(monkeypatch):
    """The check trusts what httpx trusts, so a Python without a system CA store
    (python.org on a Mac) does not report a reachable gateway as unreachable."""
    certifi = pytest.importorskip("certifi")
    loaded: list[str] = []
    real = tls_context.__globals__["ssl"].create_default_context

    def spy(*a, **kw):
        loaded.append(kw.get("cafile"))
        return real(*a, **kw)

    monkeypatch.setattr("qumus2.core.http.ssl.create_default_context", spy)
    ctx = tls_context()
    assert loaded == [certifi.where()] and ctx.cert_store_stats()["x509_ca"] > 0
    sent: list = []

    def fake_urlopen(req, timeout, context):
        sent.append(context)
        raise OSError("offline")

    monkeypatch.setattr("qumus2.agents.openai_health.urllib.request.urlopen", fake_urlopen)
    OpenAIHealth(OpenAICredentials(KEY, base_url="https://gateway.example/v1", model_id="test")).check()
    assert isinstance(sent[0], ssl.SSLContext)


def test_health_reports_model_in_sdk_mode(services, gateway):
    url, state, _ = gateway
    s = services

    class Sdk:  # only what the app reads from an SDK runtime
        mode, provider, busy, stopped = "sdk", "openai", False, False
        creds = OpenAICredentials(KEY, base_url=url + "/v1", model_id="test")

        async def close(self):
            pass

    s.runtime = Sdk()
    with TestClient(create_app(s, start_driver=False)) as c:
        assert c.post("/api/model/check").json()["state"] == "ok"
        h = c.get("/api/health").json()
        assert h["mode"] == "sdk" and h["model_status"]["state"] == "ok"
        assert h['models'] == ['test']
        state["code"] = 403
        assert c.post("/api/model/check").json()["state"] == "auth_failed"
        assert c.get("/api/health").json()["model_status"]["state"] == "auth_failed"


def test_continue_endpoint_queues_a_continue(services):
    with TestClient(create_app(services, start_driver=False)) as c:
        iid = c.post("/api/continue").json()["instruction_id"]
    (ins,) = [i for i in services.store.pending_instructions() if i["id"] == iid]
    assert ins["kind"] == "continue"


def test_websocket_refuses_other_sites(services):
    with TestClient(create_app(services, start_driver=False)) as c:
        with c.websocket_connect("/ws/events?after=0",
                                 headers={"origin": "http://testserver"}) as ws:
            assert "kind" in ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/events?after=0",
                                     headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()


def test_scripted_stop_then_continue_end_to_end(services):
    """Stop while the scripted Qumus waits on a job; Continue finishes the turn
    and the job ends where it would have without Stop."""
    s = services
    from tests.test_stop_continue import stop_at

    # the eyes' inspect_bench takes the first 30 device calls (overview camera);
    # Stop lands inside the placement job that follows
    fired = stop_at(s, 30 + 12)

    async def go():
        s.store.add_instruction("Put a chip on the transfer stage", project_id=s.project_id)
        loop = asyncio.get_running_loop()
        end = loop.time() + 30
        while not s.store.list_runs("paused") and loop.time() < end:
            await s.driver.tick()
            await asyncio.sleep(0.02)
        assert fired and s.store.list_runs("paused")
        s.store.add_instruction("STOP", kind="stop", project_id=s.project_id)
        await s.driver.tick()
        assert s.runtime.stopped
        s.store.add_instruction("CONTINUE", kind="continue", project_id=s.project_id)
        await s.driver.wait_idle(timeout=30)

    asyncio.run(go())
    kinds = [e.kind for e in s.store.events_after(0, limit=100_000)]
    assert "system.stopped" in kinds and "system.continued" in kinds
    assert "agent.error" not in kinds
    assert not s.store.list_runs("paused")
    assert s.store.get_sample("chip_001")["location"] == "transfer_stage"
