"""A fake reactor Lab Console for offline tests of ``ReactorConsole``.

It answers the console agent routes listed in ``console.py`` the way the
reactor docs describe them (docs/agent-cli.en.md, docs/orchestrator.en.md),
including recipe runs, with a toy world: tray slots, one crucible and a
furnace whose sequence ends after a few state reads. It controls nothing.
No reactor code is used.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .console import DEVICES, RECIPE_DEVICES

#: smallest JPEG-looking payload (start and end markers): frames are opaque bytes here
FAKE_JPEG = b"\xff\xd8\xff\xe0fake-frame\xff\xd9"

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
        # toy world
        self.slots: dict[str, str | None] = {"A1": None, "A2": None, "A3": None, "A4": None}
        self.crucible: str | None = None
        self.states["furnace"].update({"pv_c": 25.0, "target_c": 25.0, "control_mode": "STOPPED",
                                       "flow_pct": [0.0, 0.0, 0.0],
                                       "sequence": {"running": False, "phase": "idle"}})
        self.furnace_reads_left = 0
        self.load_max_c = 100.0
        self.hold_recipe: str | None = None  # this recipe's run holds instead of completing
        self.orchestrator_holder: str | None = None
        self.runs: dict[str, dict[str, Any]] = {}
        self.orchestrator = {"last_command": None, "active_run": None}
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
                    if path == "api/state":
                        return self._send(200, json.loads(json.dumps(console.orchestrator)))
                    if path and path.startswith("api/runs/"):
                        run = console.runs.get(path.rsplit("/", 1)[1])
                        return self._send(200, run) if run else self._send(404, {"error": "no run"})
                    if path == "sim/microscope/api/camera/microscope/frame.jpg":
                        data = FAKE_JPEG
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return None
                    for prefix in ("api/console/state/", "api/console/describe/"):
                        if path and path.startswith(prefix):
                            device = path[len(prefix):]
                            if device not in DEVICES:
                                return self._send(404, {"error": "Unknown console device."})
                            if prefix.endswith("describe/"):
                                return self._send(200, {"commands": {
                                    c: {"params": {"type": "object"}} for c in _COMMANDS[device]}})
                            holder = console.holders.get(device)
                            if device == "furnace":
                                console._advance_furnace()
                            return self._send(200, {"state": json.loads(json.dumps(console.states[device])),
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
                    if path == "api/heartbeat":
                        if console.orchestrator_holder not in (None, page):
                            return self._send(409, {"error": "Another client holds the lease."})
                        console.orchestrator_holder = page
                        return self._send(200, {"lease": {"holder": page}})
                    if path == "api/renew":
                        if console.orchestrator_holder != page:
                            return self._send(409, {"error": "The lease expired."})
                        return self._send(200, {"lease": {"holder": page}})
                    if path == "api/release":
                        if console.orchestrator_holder == page:
                            console.orchestrator_holder = None
                        return self._send(200, {"result": "released"})
                    if path == "api/runs":
                        return self._send(*console._start_run(page, body))
                    if path.startswith("api/runs/") and path.endswith("/abort"):
                        run_id = path.split("/")[2]
                        active = console.orchestrator["active_run"] or {}
                        if active.get("run_id") == run_id and not active.get("closed"):
                            active.update(status="aborted", closed=True,
                                          detail=active.get("detail") or "aborted by the agent")
                        return self._send(202, {"command_id": "abort"})
                    if path == "sim/handover":
                        slot = body.get("slot")
                        if slot not in console.slots:
                            return self._send(422, {"error": "unknown slot"})
                        console.slots[slot] = body.get("chip_id")
                        return self._send(200, {"slots": dict(console.slots)})
                    if path == "api/dashboard/motion":
                        console.stops.append("dashboard:motion")
                        return self._send(202, {"operation_id": "op-1"})
                    if path.startswith("api/console/stop/"):
                        device = path.rsplit("/", 1)[1]
                        console.stops.append(device)
                        if device == "furnace":
                            console.states["furnace"]["sequence"] = {"running": False,
                                                                     "phase": "stopped"}
                            console.states["furnace"]["control_mode"] = "STOPPED"
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
                        result, message = console._apply(device, body.get("action"),
                                                         body.get("params") or {})
                        console.states[device]["last_command"] = {
                            "id": cid, "action": body.get("action"), "result": result,
                            "client": page, "message": message}
                        return self._send(202, {"outcome": "accepted", "command_id": cid})
                self._send(404, {"error": "not found"})

        return Handler

    # ------------------------------------------------------------ toy world
    def _apply(self, device: str, action: Any, params: dict[str, Any]) -> tuple[str, str]:
        if device == "furnace" and action == "start_sequence":
            steps = params.get("steps") or []
            if not steps:
                return "rejected", "A sequence needs at least one step."
            furnace = self.states["furnace"]
            furnace.update(control_mode="RUN", target_c=steps[-1]["temperature_c"])
            furnace["sequence"] = {"running": True, "phase": "ramping", "current_index": 0,
                                   "steps": steps, "end_gas": params.get("end_gas"),
                                   "end_gas_until_c": params.get("end_gas_until_c")}
            self.furnace_reads_left = 2
            return "ok", "sequence started"
        return "ok", "done"

    def _advance_furnace(self) -> None:
        furnace = self.states["furnace"]
        sequence = furnace.get("sequence") or {}
        if not sequence.get("running"):
            return
        self.furnace_reads_left -= 1
        steps = sequence.get("steps") or [{}]
        if self.furnace_reads_left > 0:
            furnace["pv_c"] = float(steps[-1].get("temperature_c", 25.0))
            sequence["phase"] = "holding"
            return
        furnace.update(pv_c=25.0, target_c=25.0, control_mode="STOPPED")
        sequence.update(running=False, phase="complete")

    def _start_run(self, page: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.orchestrator_holder != page:
            return 409, {"error": "Acquire the orchestrator lease first."}
        recipe, params = body.get("recipe"), body.get("params") or {}
        devices = RECIPE_DEVICES.get(recipe)
        if devices is None:
            return 422, {"error": f"Unknown recipe {recipe!r}."}
        missing = [d for d in devices if self.holders.get(d) != page]
        if missing:
            return 409, {"error": "Take control of these devices first: " + ", ".join(missing) + "."}
        self.seq += 1
        command_id, run_id = str(self.seq), f"run-{self.seq}"
        error, value = self._recipe(recipe, params)
        held = recipe == self.hold_recipe
        status = "held" if held else ("aborted" if error else "completed")
        self.runs[run_id] = {"run_id": run_id, "recipe": recipe, "steps": [{
            "skill": recipe, "status": "failed" if (error or held) else "succeeded",
            "result_json": json.dumps(value) if value is not None else None,
            "error": error or ("held for an operator" if held else None)}]}
        self.orchestrator = {
            "last_command": {"id": command_id, "action": "start", "result": "ok", "message": ""},
            "active_run": {"run_id": run_id, "status": status, "closed": not held,
                           "detail": error or ("held for an operator" if held else None)}}
        return 202, {"command_id": command_id}

    def _recipe(self, recipe: str, params: dict[str, Any]) -> tuple[str | None, Any]:
        if recipe == "slot-state":
            slot = params.get("slot")
            if slot not in self.slots:
                return f"Unknown slot {slot!r}.", None
            if self.slots[slot] is None:
                return None, {"state": "empty", "center_xy_mm": None, "angle_deg": None,
                              "reason": None}
            return None, {"state": "present", "center_xy_mm": [10.0, 20.0], "angle_deg": 0.0,
                          "reason": None}
        if recipe == "crucible-load":
            slot = params.get("from_slot")
            if self.slots.get(slot) is None:
                return "No chip is present in the requested source slot.", None
            if self.states["furnace"]["pv_c"] >= self.load_max_c or self.crucible is not None:
                return "The furnace is too hot or the crucible is occupied.", None
            self.crucible, self.slots[slot] = self.slots[slot], None
            return None, None
        if recipe == "crucible-unload":
            slot = params.get("to_slot")
            if slot not in self.slots or self.slots[slot] is not None or self.crucible is None:
                return "The target slot is not empty or the crucible is empty.", None
            self.slots[slot], self.crucible = self.crucible, None
            return None, None
        if recipe == "microscope-record":
            return None, {"recording_id": "rec-1", "artifact_id": "art-1", "status": "completed"}
        return f"Recipe {recipe} is not simulated.", None
