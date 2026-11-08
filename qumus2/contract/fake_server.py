"""A tiny in-process contract v1 service for tests and offline demos.

It follows ADR 0001 of the reactor repo plus the Qumus rules, with simulated
commands that just take time. It controls no device. Use it to test clients,
the agent's tools and the UI without the lab PC or the reactor host.

    with FakeService("stacking", commands={"read_all": FakeCommand()}) as svc:
        client = ContractClient(svc.base_url, "test")
        client.run("read_all")

Knobs that model the differences seen between services:

* ``client_header``: header the service requires (Qumus ``X-Qumus-Client``,
  reactor ``X-MG400-Client``); ``required_headers`` adds fixed ones
  (reactor robot ``X-MG400-Panel: 1``).
* ``dialect``: ``"adr"`` (commands map, flat body) or ``"qumus"`` (actions
  list, nested params, command_id/status names).
* ``reply_style``: ``"202"`` (``{command_id, state}``) or ``"200-state"``
  (reactor robot: 200 with the bare state and no command id).
* ``drop_next_command_reply()``: accept the next command, then hang up without
  a reply. The service locks itself, as contract v1 requires for an unknown
  outcome.
"""

from __future__ import annotations

import ipaddress
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


@dataclass
class FakeCommand:
    params: dict[str, Any] = field(default_factory=lambda: {
        "type": "object", "properties": {}, "additionalProperties": False})
    any_client: bool = False
    motion: bool | None = False
    local_only: bool = False
    ui_only: bool = False
    duration_s: float = 0.05
    #: result reported when the simulated work ends: ok or failed
    result: str = "ok"
    message: str = ""
    #: optional hook(params, service) -> dict of top-level/device field updates
    effect: Any = None


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "object": dict, "array": list, "null": type(None)}


def _check_params(schema: dict[str, Any], params: dict[str, Any]) -> str | None:
    props = schema.get("properties", {}) or {}
    for name in schema.get("required", []) or []:
        if name not in params:
            return f"missing parameter {name!r}"
    if schema.get("additionalProperties") is False:
        extra = sorted(set(params) - set(props))
        if extra:
            return f"unknown parameters {extra}"
    for name, value in params.items():
        spec = props.get(name, {})
        kind = spec.get("type")
        if kind in _TYPES:
            ok = isinstance(value, _TYPES[kind])
            if kind in {"integer", "number"} and isinstance(value, bool):
                ok = False
            if not ok:
                return f"parameter {name!r} must be {kind}"
        if "enum" in spec and value not in spec["enum"]:
            return f"parameter {name!r} must be one of {spec['enum']}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                return f"parameter {name!r} below minimum {spec['minimum']}"
            if "maximum" in spec and value > spec["maximum"]:
                return f"parameter {name!r} above maximum {spec['maximum']}"
    return None


class FakeService:
    def __init__(
        self,
        service: str,
        *,
        commands: dict[str, FakeCommand] | None = None,
        lease_seconds: float = 3.0,
        client_header: str = "X-Qumus-Client",
        advertise_client_header: bool = True,
        required_headers: dict[str, str] | None = None,
        base_path: str = "/",
        dialect: str = "adr",
        reply_style: str = "202",
        renew_route: bool = True,
        demo: bool = True,
        device_fields: dict[str, Any] | None = None,
        on_lease_expiry: str = "stop motion",
        host: str = "127.0.0.1",
        port: int = 0,
        treat_clients_as_remote: bool = False,
        heartbeat_acquires: bool = True,
        extra_get: dict[str, Any] | None = None,
    ) -> None:
        self.service = service
        self.commands = dict(commands or {})
        self.lease_seconds = lease_seconds
        self.client_header = client_header
        self.advertise_client_header = advertise_client_header
        self.required_headers = dict(required_headers or {})
        self.base_path = "/" + base_path.strip("/") + ("/" if base_path.strip("/") else "")
        self.dialect = dialect
        self.reply_style = reply_style
        self.renew_route = renew_route
        self.demo = demo
        self.device_fields = dict(device_fields or {})
        self.on_lease_expiry = on_lease_expiry
        self.treat_clients_as_remote = treat_clients_as_remote
        #: False models the reactor robot: a command (start_sync) takes control and
        #: api/heartbeat only renews it.
        self.heartbeat_acquires = heartbeat_acquires
        #: extra read-only GET routes (path below base_path -> JSON payload)
        self.extra_get = dict(extra_get or {})
        self.lock = threading.RLock()
        self.seq = 0
        self.busy = False
        self.locked = False
        self.fault: dict[str, Any] | None = None
        self.holder: str | None = None
        self.deadline = 0.0
        self.last_command: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self._drop_next = False
        self._work: threading.Timer | None = None
        self._current: dict[str, Any] | None = None
        self._server = ThreadingHTTPServer((host, port), self._handler_class())
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None
        self._ticker_stop = threading.Event()

    # ----------------------------------------------------------- lifecycle
    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.base_path}"

    def start(self) -> FakeService:
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()
        threading.Thread(target=self._tick, daemon=True).start()
        return self

    def close(self) -> None:
        self._ticker_stop.set()
        if self._work is not None:
            self._work.cancel()
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeService:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def drop_next_command_reply(self) -> None:
        self._drop_next = True

    def _tick(self) -> None:
        while not self._ticker_stop.wait(0.05):
            self.poll_lease()

    # -------------------------------------------------------------- state
    def event(self, level: str, message: str, **fields: Any) -> None:
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "ts_unix": time.time(), "level": level,
                                "message": message, "fields": fields})

    def poll_lease(self) -> None:
        with self.lock:
            if self.holder is not None and time.monotonic() >= self.deadline:
                holder, self.holder = self.holder, None
                self.event("warning", "lease expired", client=holder)
                if self.busy and self._current is not None and self._current.get("motion"):
                    self._finish("stopped", "lease expired during motion")

    def lease_snapshot(self) -> dict[str, Any]:
        remaining = max(0.0, self.deadline - time.monotonic()) if self.holder else 0.0
        if self.dialect == "qumus":
            return {"holder": self.holder,
                    "expires_unix": time.time() + remaining if self.holder else None}
        return {"holder": self.holder, "remaining_s": remaining}

    def last_command_view(self) -> dict[str, Any] | None:
        lc = self.last_command
        if lc is None or self.dialect != "qumus":
            return lc
        return {"command_id": lc["id"], "action": lc["action"], "client": lc["client"],
                "status": {"ok": "done"}.get(lc["result"], lc["result"]) if lc["result"]
                else "started", "error": lc["message"], "finished_at": lc["finished_at"]}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            state = {
                "service": self.service, "seq": self.seq, "ts_unix": time.time(),
                "demo": self.demo, "connected": True, "busy": self.busy,
                "locked": self.locked, "fault": self.fault, "lease": self.lease_snapshot(),
                "last_command": self.last_command_view(),
            }
            if self.dialect == "qumus":
                state["devices"] = json.loads(json.dumps(self.device_fields))
            else:
                state.update(json.loads(json.dumps(self.device_fields)))
            return state

    def describe(self) -> dict[str, Any]:
        cmds = {
            name: {"params": c.params, "any_client": c.any_client,
                   **({"motion": c.motion} if c.motion is not None else {}),
                   **({"local_only": True} if c.local_only else {}),
                   **({"ui_only": True} if c.ui_only else {})}
            for name, c in self.commands.items()
        }
        body: dict[str, Any] = {
            "service": self.service, "version": "fake", "contract": 1, "demo": self.demo,
            "zones": [], "lease_seconds": self.lease_seconds,
            "on_lease_expiry": self.on_lease_expiry,
        }
        if self.renew_route:
            body["renew_only_heartbeat"] = True
        if self.advertise_client_header:
            body["client_header"] = self.client_header
        if self.dialect == "qumus":
            body["contract"] = "1"
            body["actions"] = [{"name": n, **spec} for n, spec in cmds.items()]
        else:
            body["commands"] = cmds
        return body

    # ----------------------------------------------------------- commands
    def _finish(self, result: str, message: str) -> None:
        with self.lock:
            if self._current is None:
                return
            if self._work is not None:
                self._work.cancel()
                self._work = None
            lc = dict(self._current["record"])
            lc.update(result=result, message=message, finished_at=time.time())
            self.last_command = lc
            self.busy = False
            self.event("info" if result == "ok" else "warning", f"command {result}",
                       id=lc["id"], action=lc["action"])
            self._current = None

    def _complete(self, cmd: FakeCommand, params: dict[str, Any]) -> None:
        with self.lock:
            if self._current is None:
                return
            if cmd.effect is not None and cmd.result == "ok":
                updates = cmd.effect(params, self) or {}
                self.device_fields.update(updates)
            self._finish(cmd.result, cmd.message)

    def start_command(self, client: str, action: str, params: dict[str, Any]) -> str:
        cmd = self.commands[action]
        ident = uuid.uuid4().hex
        with self.lock:
            self.busy = True
            record = {"id": ident, "action": action, "client": client, "result": None,
                      "message": "", "finished_at": None}
            self.last_command = dict(record)
            self._current = {"record": record, "motion": bool(cmd.motion)}
            self.event("info", "command accepted", id=ident, action=action, client=client)
            self._work = threading.Timer(cmd.duration_s, self._complete, args=(cmd, params))
            self._work.daemon = True
            self._work.start()
        return ident

    # -------------------------------------------------------------- HTTP
    def _handler_class(self):  # noqa: C901 - one small router
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence test output
                pass

            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _route(self) -> tuple[str, dict[str, list[str]]] | None:
                parts = urlsplit(self.path)
                if not parts.path.startswith(service.base_path):
                    return None
                return "/" + parts.path[len(service.base_path):], parse_qs(parts.query)

            def do_GET(self) -> None:  # noqa: N802
                routed = self._route()
                service.requests.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}, None))
                if routed is None:
                    return self._send(404, {"error": "not found"})
                path, query = routed
                service.poll_lease()
                if path == "/api/describe":
                    return self._send(200, service.describe())
                if path == "/api/state":
                    return self._send(200, service.snapshot())
                if path == "/api/events":
                    raw = query.get("since", ["0"])[0]
                    if not raw.isdecimal():
                        return self._send(400, {"error": "since must be a nonnegative integer"})
                    since = int(raw)
                    with service.lock:
                        items = [e for e in service.events if e["seq"] > since]
                        if service.dialect == "qumus":
                            return self._send(200, items)
                        return self._send(200, {"events": items, "next": service.seq})
                if path in service.extra_get:
                    return self._send(200, service.extra_get[path])
                return self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                routed = self._route()
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                service.requests.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, raw))
                if routed is None:
                    return self._send(404, {"error": "not found"})
                path, _ = routed
                if self.headers.get_content_type() != "application/json":
                    return self._send(415, {"error": "Expected Content-Type: application/json."})
                for name, value in service.required_headers.items():
                    if self.headers.get(name) != value:
                        return self._send(403, {"error": f"missing header {name}"})
                client = self.headers.get(service.client_header)
                if not client or len(client) > 128:
                    return self._send(400, {"error": f"missing {service.client_header}"})
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
                if not isinstance(body, dict):
                    return self._send(400, {"error": "Expected a JSON object."})
                service.poll_lease()
                with service.lock:
                    return self._post(path, client, body)

            def _post(self, path: str, client: str, body: dict[str, Any]) -> None:
                s = service
                if path == "/api/heartbeat":
                    if not s.heartbeat_acquires and s.holder != client:
                        return self._send(409, {"error": "Take control first; heartbeat only renews."})
                    if s.holder not in (None, client):
                        return self._send(409, {"error": "Another client holds the control lease."})
                    if s.holder is None:
                        s.event("info", "lease acquired", client=client)
                    s.holder, s.deadline = client, time.monotonic() + s.lease_seconds
                    return self._send(200, {"lease": s.lease_snapshot()})
                if path == "/api/renew" and s.renew_route:
                    if s.holder != client:
                        return self._send(409, {"error": "No lease to renew."})
                    s.deadline = time.monotonic() + s.lease_seconds
                    return self._send(200, {"lease": s.lease_snapshot()})
                if path == "/api/release":
                    if s.holder not in (None, client):
                        return self._send(409, {"error": "Another client holds the control lease."})
                    if s.holder is not None:
                        s.event("info", "lease released", client=client)
                    s.holder = None
                    return self._send(200, {"lease": s.lease_snapshot()})
                if path == "/api/stop":
                    wait_s = body.get("wait_s", 0)
                    if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) \
                            or not 0 <= wait_s <= 15:
                        return self._send(400, {"error": "wait_s must be 0..15"})
                    if s.busy:
                        s._finish("stopped", f"stopped by {client}")
                    s.event("warning", "stop", client=client)
                    word = "unconfirmed" if s.locked else "confirmed"
                    return self._send(200, {"stop": word, "state": s.snapshot()})
                if path == "/api/command":
                    return self._command(client, body)
                return self._send(404, {"error": "not found"})

            def _command(self, client: str, body: dict[str, Any]) -> None:
                s = service
                bad = 422 if s.dialect == "qumus" else 400
                body = dict(body)
                action = body.pop("action", None)
                if s.dialect == "qumus":
                    params = body.get("params", {})
                    if not isinstance(params, dict):
                        return self._send(bad, {"error": "params must be an object"})
                else:
                    params = body
                if not isinstance(action, str) or action not in s.commands:
                    return self._send(bad, {"error": "Unknown command action."})
                cmd = s.commands[action]
                if cmd.local_only and (s.treat_clients_as_remote or not ipaddress.ip_address(
                        self.client_address[0]).is_loopback):
                    return self._send(403, {"error": "local_only action"})
                problem = _check_params(cmd.params, params)
                if problem:
                    return self._send(bad, {"error": problem})
                if s.locked:
                    return self._send(409, {"error": "The service is locked. Only stop is allowed."})
                if not cmd.any_client and s.holder != client:
                    return self._send(409, {"error": "Acquire the control lease before sending a command."})
                if s.busy:
                    return self._send(409, {"error": "busy"})
                ident = s.start_command(client, action, params)
                if s._drop_next:
                    s._drop_next = False
                    s.locked = True
                    s.fault = {"code": "unknown_outcome", "message": "reply lost; check device"}
                    s.event("error", "unknown command outcome; locked", id=ident)
                    self.close_connection = True
                    # Hang up without an answer.
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    return None
                if s.reply_style == "200-state":
                    return self._send(200, s.snapshot())
                return self._send(202, {"command_id": ident, "state": s.snapshot()})

        return Handler
