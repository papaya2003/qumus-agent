"""A fake reactor Lab Console for offline tests of ``ReactorConsole``.

It answers the console agent routes listed in ``console.py`` the way the
reactor docs describe them (docs/agent-cli.en.md). It controls nothing and
simulates nothing physical. No reactor code is used.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .console import DEVICES

_COMMANDS = {
    "robot": ["clear_alarm", "disable", "enable", "execute", "home", "vacuum"],
    "stage": ["goto", "home", "move", "pose", "zero"],
    "thermal": ["connect", "shutter"],
    "vision": ["check_calibration", "detect", "observe_slots", "read_labels", "read_markers"],
    "furnace": ["acknowledge", "apply_setpoint", "set_gas_flow", "start_sequence"],
    "microscope": ["autofocus", "focus_move", "snapshot", "select_objective"],
}


class FakeConsole:
    def __init__(self, base_path: str = "/lab/", *, demo: bool = True) -> None:
        self.base_path = base_path
        self.demo = demo
        self.lock = threading.Lock()
        self.holders: dict[str, str] = {}
        self.states = {d: {"service": d, "demo": demo, "busy": False, "locked": False,
                           "fault": None, "last_command": None} for d in DEVICES}
        self.seq = 0
        self.events: list[dict[str, Any]] = []
        self.posts: list[tuple[str, str | None, Any]] = []  # (path, page, body)
        self.drop_next_command = False
        self.stops: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{self.base_path}"

    def __enter__(self) -> FakeConsole:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _event(self, service: str, message: str) -> None:
        self.seq += 1
        self.events.append({"seq": self.seq, "service": service, "message": message})

    def _handler(self):
        console = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _path(self) -> str | None:
                if not self.path.startswith(console.base_path):
                    return None
                return self.path[len(console.base_path):].split("?", 1)[0]

            def do_GET(self) -> None:  # noqa: N802
                path = self._path()
                page = self.headers.get("X-MG400-Client")
                with console.lock:
                    if path == "api/console/state":
                        control = [{"device": d, "holder": h, "mine": h == page}
                                   for d, h in console.holders.items()]
                        return self._send(200, {"console": {"commit": "fake", "demo": console.demo},
                                                "control": control, "run": None})
                    if path == "api/console/events":
                        return self._send(200, {"events": list(console.events), "errors": {}})
                    for prefix in ("api/console/state/", "api/console/describe/"):
                        if path and path.startswith(prefix):
                            device = path[len(prefix):]
                            if device not in DEVICES:
                                return self._send(404, {"error": "Unknown console device."})
                            if prefix.endswith("describe/"):
                                return self._send(200, {"commands": {
                                    c: {"params": {"type": "object"}} for c in _COMMANDS[device]}})
                            holder = console.holders.get(device)
                            return self._send(200, {"state": dict(console.states[device]),
                                                    "control": {"holder": holder,
                                                                "mine": holder == page}})
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = self._path() or ""
                page = self.headers.get("X-MG400-Client")
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with console.lock:
                    console.posts.append((path, page, body))
                    if page is None:
                        return self._send(400, {"error": "Send the page identity in X-MG400-Client."})
                    if path == "api/console/heartbeat":
                        mine = [{"device": d, "holder": h, "mine": True}
                                for d, h in console.holders.items() if h == page]
                        return self._send(200, {"control": mine})
                    if path == "api/dashboard/motion":
                        console.stops.append("dashboard:motion")
                        return self._send(202, {"operation_id": "op-1"})
                    if path.startswith("api/console/stop/"):
                        device = path.rsplit("/", 1)[1]
                        console.stops.append(device)
                        console._event(device, "stop")
                        return self._send(200, {"outcome": "confirmed"})
                    if path.startswith("api/console/control/"):
                        device, verb = path.split("/")[-2:]
                        if verb == "take":
                            other = console.holders.get(device)
                            if other and other != page:
                                return self._send(409, {"error": "Held by another client."})
                            console.holders[device] = page
                            return self._send(200, {"control": {"device": device, "holder": page}})
                        console.holders.pop(device, None)
                        return self._send(200, {"result": "released"})
                    if path == "api/console/release-page":
                        mine = [d for d, h in console.holders.items() if h == page]
                        for d in mine:
                            del console.holders[d]
                        return self._send(200, {"results": {d: "released" for d in mine}})
                    if path.startswith("api/console/command/"):
                        device = path.rsplit("/", 1)[1]
                        if console.holders.get(device) != page:
                            return self._send(409, {"outcome": "refused",
                                                    "error": "Take control of this device first."})
                        if console.drop_next_command:
                            console.drop_next_command = False
                            self.close_connection = True
                            self.connection.close()
                            return None
                        console.seq += 1
                        cid = str(console.seq)
                        console.states[device]["last_command"] = {
                            "id": cid, "action": body.get("action"), "result": "ok",
                            "client": page, "message": "done"}
                        return self._send(202, {"outcome": "accepted", "command_id": cid})
                self._send(404, {"error": "not found"})

        return Handler
