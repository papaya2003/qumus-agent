"""The reactor through its Lab Console agent API (reactor ADR 0010, 2026-11-09).

Since ADR 0010 the reactor owner lets agents operate the devices through the
Lab Console instead of the old propose/approve gateway. The console holds each
agent's device leases (renewed every 1 s on the host) and releases an agent
session after 10 minutes without a call. The console is reachable over the
LarePass VPN through the reactor's single-port proxy at
``http://100.64.0.1:8080/lab/``. Qumus reaches it through an SSH tunnel
instead (Jialei, 2026-11-10): ``scripts/reactor_tunnel.sh`` forwards
``127.0.0.1:18772`` to the console on Olares, see ``docs/reactor-ssh.md``.

Written from the reactor repo's public docs (``docs/agent-cli.en.md``,
``docs/olares.en.md``, ADR 0010) and its route list. No reactor code is copied
(the repo is AGPL-3.0).

Routes used (all relative to the console base URL):

* ``GET api/console/state``: dashboard snapshot, control holders, current run;
* ``GET api/console/state/<device>``: one device state plus its holder;
* ``GET api/console/describe/<device>``: offered commands;
* ``GET api/console/events``: merged events of all devices;
* ``POST api/console/heartbeat``: stamp this agent session (keeps its leases);
* ``POST api/console/control/<device>/take|release``, ``release-page``;
* ``POST api/console/command/<device>`` ``{action, params}`` -> 202/409/502;
* ``POST api/console/stop/<device>``: stop needs no control;
* ``POST api/dashboard/motion``: console-wide motion stop (also holds a run).

Recipe runs (the reactor orchestrator, same base; reactor docs/orchestrator.en.md):

* ``POST api/heartbeat`` / ``api/renew`` / ``api/release``: the caller's orchestrator lease;
* ``POST api/runs`` ``{recipe, params, borrow: true}`` -> 202 ``{command_id}``; the run borrows
  the devices this session holds;
* ``GET api/state``: ``last_command`` (admission) and ``active_run`` (``run_id``, ``status``
  running|held|completed|aborted, ``closed``, ``detail``);
* ``GET api/runs/<id>``: the run's steps; ``POST api/runs/<id>/abort``.

Microscope frames are served by the microscope service, not by the console:
``GET <microscope base>/api/camera/microscope/frame.jpg``.

Every POST carries ``X-MG400-Client: agent-<session>``; reads need no header.

Qumus policy (until Jialei explicitly allows more): reads, stops and the
non-moving, non-heating commands below. Taking control, sending other
commands and starting recipes are refused before anything is sent unless the
owner constructs the client with ``allow_actuation=True``. The agent cannot
flip that flag. Commands are sent once and never retried; a lost reply is
``OutcomeUnknown``.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..contract.errors import (
    BadRequest,
    Conflict,
    ContractError,
    NotFound,
    OutcomeUnknown,
    ServerError,
    TransportError,
)
from .adapter import REACTOR_CLIENT_HEADER, ReactorPolicyError

#: Console base URL as seen from a LarePass VPN client (reactor docs/olares.en.md).
VPN_CONSOLE_URL = "http://100.64.0.1:8080/lab/"
#: Console base URL on the reactor host itself (labctl's default).
LOCAL_CONSOLE_URL = "http://127.0.0.1:8772/lab/"
#: Console base URL through the SSH tunnel of scripts/reactor_tunnel.sh (Jialei's choice, 11-10).
TUNNEL_CONSOLE_URL = "http://127.0.0.1:18772/lab/"

#: Devices the console knows (reactor docs/agent-cli.en.md).
DEVICES = ("robot", "stage", "thermal", "vision", "furnace", "microscope")
#: Devices that move; "STOP ALL MOTION" stops these.
MOTION_DEVICES = ("robot", "stage", "microscope")

#: Commands that move nothing, heat nothing and write no calibration.
READ_ONLY_ACTIONS: dict[str, frozenset[str]] = {
    "thermal": frozenset({"connect", "shutter"}),
    "microscope": frozenset({"snapshot"}),
    "vision": frozenset({"detect", "read_labels", "read_markers", "observe_slots",
                         "check_calibration"}),
}

#: Devices each catalog recipe uses (reactor src skill registry, 2137e7a). A recipe run
#: borrows them, so this session must hold every one before starting it.
RECIPE_DEVICES: dict[str, tuple[str, ...]] = {
    "slot-state": ("robot", "vision"),
    "crucible-load": ("robot", "stage", "furnace"),
    "crucible-unload": ("robot", "stage", "furnace", "thermal"),
    "microscope-record": ("microscope",),
}
#: Recipes allowed without allow_actuation. None yet: microscope-record moves and heats
#: nothing, but recording waits for Jialei's OK like every recipe (action map, 11-10).
READ_ONLY_RECIPES: frozenset[str] = frozenset()

#: Command results the console reports as final (same set labctl waits for).
FINAL_RESULTS = frozenset({"ok", "failed", "rejected", "stopped"})

_SESSION = re.compile(r"[A-Za-z0-9_.-]{1,64}")


@dataclass
class CommandResult:
    device: str
    action: str
    command_id: str | None
    outcome: str  # accepted | ok | failed | rejected | stopped | timeout | unknown
    message: str = ""
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in {"accepted", "ok"}


@dataclass
class RecipeResult:
    recipe: str
    run_id: str | None
    outcome: str  # ok | failed | refused | timeout | unknown
    status: str | None = None  # completed | aborted (the run's own end state)
    message: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def result(self) -> Any:
        """The last step's result (e.g. slot-state's ``{state, center_xy_mm, angle_deg}``)."""
        return self.steps[-1].get("result") if self.steps else None

    def to_dict(self) -> dict[str, Any]:
        return {"recipe": self.recipe, "run_id": self.run_id, "outcome": self.outcome,
                "status": self.status, "message": self.message, "steps": self.steps}


class ReactorConsole:
    """The reactor's Lab Console as seen from the Qumus brain."""

    def __init__(self, base_url: str = VPN_CONSOLE_URL, *, session: str = "qumus",
                 timeout_s: float = 10.0, allow_actuation: bool = False,
                 opener: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not _SESSION.fullmatch(session):
            raise ValueError("session: 1 to 64 letters, digits, '_', '.' or '-'")
        self.base_url = base_url.rstrip("/") + "/"
        self.session = session
        #: page identity the console binds this agent's leases to
        self.page = f"agent-{session}"
        self.timeout_s = timeout_s
        self.allow_actuation = allow_actuation
        # No proxies (LarePass is a VPN route, not an HTTP proxy), no redirects.
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())
        self._clock, self._sleep = clock, sleep

    # ------------------------------------------------------------------ HTTP
    def _request(self, path: str, data: dict[str, Any] | None = None,
                 *, command: bool = False) -> tuple[int, dict[str, Any]]:
        url = urllib.parse.urljoin(self.base_url, path)
        headers = {"Accept": "application/json"}
        body = None
        if data is not None:
            headers[REACTOR_CLIENT_HEADER] = self.page
            headers["Content-Type"] = "application/json"
            body = json.dumps(data, allow_nan=False).encode()
        request = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST" if data is not None else "GET")
        try:
            with self._opener.open(request, timeout=self.timeout_s) as reply:
                status, payload = reply.status, reply.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
            exc.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            kind = OutcomeUnknown if command else TransportError
            raise kind(f"console did not answer: {exc}", url=url) from None
        try:
            parsed = json.loads(payload) if payload else {}
        except (ValueError, UnicodeError):
            parsed = {"error": payload[:200].decode("utf-8", "replace")}
        return status, parsed if isinstance(parsed, dict) else {"value": parsed}

    def _get(self, path: str) -> dict[str, Any]:
        status, body = self._request(path)
        _raise_for(status, body, urllib.parse.urljoin(self.base_url, path))
        return body

    def _post(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        status, body = self._request(path, {} if data is None else data)
        _raise_for(status, body, urllib.parse.urljoin(self.base_url, path))
        return body

    @staticmethod
    def _device(device: str) -> str:
        if device not in DEVICES:
            raise ValueError(f"unknown reactor device {device!r}; expected one of {DEVICES}")
        return device

    # ----------------------------------------------------------------- reads
    def overview(self) -> dict[str, Any]:
        """Dashboard snapshot: devices, control holders, current run, console commit/demo."""
        return self._get("api/console/state")

    def device_state(self, device: str) -> dict[str, Any]:
        """``{"state": {...}, "control": {"holder", "mine"}}`` for one device."""
        return self._get(f"api/console/state/{self._device(device)}")

    def describe(self, device: str) -> dict[str, Any]:
        """Offered commands of one device: ``{"commands": {name: schema...}}``."""
        return self._get(f"api/console/describe/{self._device(device)}")

    def events(self, since_ts: float = 0.0, device: str | None = None) -> list[dict[str, Any]]:
        """Console events newer than ``since_ts`` (unix seconds), oldest first.

        The real console numbers ``seq`` per service (robot 1.., runs 1..), so
        events are ordered and filtered by ``ts_unix``, never by ``seq``."""
        rows = self._get("api/console/events").get("events", [])
        rows = [row for row in rows
                if (device is None or row.get("service") == device)
                and isinstance(row.get("ts_unix"), (int, float)) and row["ts_unix"] > since_ts]
        return sorted(rows, key=lambda row: row["ts_unix"])

    def probe(self) -> dict[str, Any]:
        """Read-only connectivity report. Sends GETs only: no session, no control, no command."""
        report: dict[str, Any] = {"url": self.base_url, "reachable": False, "devices": {}}
        try:
            snapshot = self.overview()
        except ContractError as exc:
            report["error"] = str(exc)
            return report
        report["reachable"] = True
        console = snapshot.get("console") or {}
        report["console"] = {"commit": console.get("commit"), "demo": console.get("demo")}
        report["control"] = snapshot.get("control", [])
        report["run"] = snapshot.get("run")
        for device in DEVICES:
            entry: dict[str, Any] = {}
            try:
                body = self.device_state(device)
                state = body.get("state") or {}
                entry.update({key: state.get(key) for key in
                              ("demo", "busy", "locked", "fault", "connected")
                              if key in state})
                entry["holder"] = (body.get("control") or {}).get("holder")
            except ContractError as exc:
                entry["state_error"] = str(exc)
            try:
                entry["commands"] = sorted(self.describe(device).get("commands") or {})
            except ContractError as exc:
                entry["describe_error"] = str(exc)
            report["devices"][device] = entry
        return report

    # ----------------------------------------------------------------- stops
    def stop(self, device: str) -> str:
        """Stop one device once (no control needed). Returns confirmed|pending|unconfirmed|
        not-configured|unreachable. The furnace stop turns heat and gas off."""
        self._device(device)
        try:
            status, body = self._request(f"api/console/stop/{device}", {})
        except TransportError:
            return "unreachable"
        if status == 409:
            return "not-configured"
        outcome = body.get("outcome") if status == 200 else None
        return outcome if outcome in {"confirmed", "pending", "unconfirmed"} else "unconfirmed"

    def stop_all_motion(self) -> dict[str, str]:
        """Console motion stop (also holds a running recipe) plus each moving device's stop."""
        results: dict[str, str] = {}
        try:
            status, _ = self._request("api/dashboard/motion", {})
            results["console"] = "accepted" if status == 202 else f"refused ({status})"
        except TransportError:
            results["console"] = "unreachable"
        for device in MOTION_DEVICES:
            results[device] = self.stop(device)
        return results

    def stop_all(self) -> dict[str, str]:
        """Everything off: the motion stop above plus the furnace stop (heat and gas off).
        Each stop is sent once; none needs control."""
        results = self.stop_all_motion()
        results["furnace"] = self.stop("furnace")
        return results

    def furnace_wait(self, *, timeout_s: float, poll_s: float = 1.0) -> dict[str, Any]:
        """Wait until the furnace sequence no longer runs (it may still be cooling with gas).
        Returns the last furnace state; ``state["_timeout"]`` is True when time ran out."""
        deadline = self._clock() + timeout_s
        beat_at = self._clock() + 1.0
        state: dict[str, Any] = {}
        while True:
            try:
                state = self.device_state("furnace").get("state") or {}
            except ContractError:
                state = {}
            sequence = state.get("sequence") or {}
            if state and not sequence.get("running"):
                return state
            if self._clock() >= deadline:
                return {**state, "_timeout": True}
            if self._clock() >= beat_at:
                beat_at = self._clock() + 1.0
                try:
                    self.heartbeat()
                except ContractError:
                    pass
            self._sleep(poll_s)

    # -------------------------------------------------------------- control
    def _check(self, device: str, action: str | None) -> None:
        if self.allow_actuation:
            return
        if action is not None and action in READ_ONLY_ACTIONS.get(device, frozenset()):
            return
        what = f"{device}.{action}" if action else f"take control of {device}"
        raise ReactorPolicyError(
            f"reactor {what} refused: moves or heats hardware; needs Jialei's explicit OK")

    def heartbeat(self) -> list[dict[str, Any]]:
        """Stamp this session; returns this session's control entries."""
        body = self._post("api/console/heartbeat")
        return [item for item in body.get("control", []) if item.get("mine")]

    def take(self, device: str) -> dict[str, Any]:
        self._device(device)
        if device not in READ_ONLY_ACTIONS:
            self._check(device, None)
        return self._post(f"api/console/control/{device}/take").get("control", {})

    def release(self, device: str) -> str | None:
        return self._post(f"api/console/control/{self._device(device)}/release").get("result")

    def release_session(self) -> dict[str, str]:
        """Release every device this session holds (never other holders')."""
        return self._post("api/console/release-page").get("results", {})

    # ------------------------------------------------------------- commands
    def command(self, device: str, action: str, params: dict[str, Any] | None = None,
                *, wait: bool = True, timeout_s: float = 300.0,
                poll_s: float = 0.25) -> CommandResult:
        """Send one described command once. The session must hold the device."""
        self._device(device)
        self._check(device, action)
        before = (self.device_state(device).get("state") or {}) if wait else {}
        baseline = (before.get("last_command") or {}).get("id")
        status, body = self._request(f"api/console/command/{device}",
                                     {"action": action, "params": params or {}}, command=True)
        url = urllib.parse.urljoin(self.base_url, f"api/console/command/{device}")
        if status >= 500:
            raise OutcomeUnknown(body.get("error", "command outcome unknown"),
                                 status=status, url=url)
        if status != 202:
            _raise_for(status, body, url)
            raise ServerError(f"unexpected status for a command: {body}", status=status, url=url)
        command_id = body.get("command_id")
        result = CommandResult(device, action, command_id, "accepted")
        if not wait:
            return result
        deadline = self._clock() + timeout_s
        beat_at = self._clock() + 1.0
        while True:
            try:
                state = self.device_state(device).get("state") or {}
            except ContractError:
                state = {}
            last = state.get("last_command") or {}
            matched = (last.get("id") == command_id) if command_id is not None else (
                last.get("id") not in (None, baseline))
            outcome = last.get("result")
            if matched and outcome not in (None, "pending", "running", "accepted"):
                result.outcome = outcome if outcome in FINAL_RESULTS else "unknown"
                result.message = str(last.get("message") or "")
                result.state = state
                return result
            if self._clock() >= deadline:
                result.outcome, result.state = "timeout", state
                return result
            if self._clock() >= beat_at:
                beat_at = self._clock() + 1.0
                try:
                    self.heartbeat()
                except ContractError:
                    pass
            self._sleep(poll_s)

    # -------------------------------------------------------------- recipes
    def ensure_control(self, devices: tuple[str, ...] | list[str]) -> list[str]:
        """Take every listed device this session does not hold yet. Returns the ones taken."""
        mine = {item.get("device") for item in self.heartbeat()}
        taken = []
        for device in devices:
            if device not in mine:
                self.take(device)
                taken.append(device)
        return taken

    def run_recipe(self, recipe: str, params: dict[str, Any] | None = None, *,
                   timeout_s: float = 1800.0, poll_s: float = 0.2,
                   abort_wait_s: float = 30.0) -> RecipeResult:
        """Run one catalog recipe with this session's devices (borrowed control), once.

        Takes the recipe's devices, holds the orchestrator lease while the run lasts
        (renewed every second, with the console heartbeat), aborts a run that holds
        (agents get no operator prompt) or outlives ``timeout_s``, and releases the
        orchestrator lease at the end. The devices stay with this session.
        """
        devices = RECIPE_DEVICES.get(recipe)
        if devices is None:
            raise ValueError(f"unknown reactor recipe {recipe!r}; known: {sorted(RECIPE_DEVICES)}")
        if not self.allow_actuation and recipe not in READ_ONLY_RECIPES:
            raise ReactorPolicyError(
                f"reactor recipe {recipe} refused: moves or heats hardware; needs Jialei's explicit OK")
        params = dict(params or {})
        try:
            self.ensure_control(devices)
            status, body = self._request("api/heartbeat", {})
        except Conflict as exc:
            return RecipeResult(recipe, None, "refused", message=str(exc))
        except TransportError as exc:
            return RecipeResult(recipe, None, "refused", message=str(exc))
        if status != 200:
            return RecipeResult(recipe, None, "refused",
                                message=str(body.get("error") or "orchestrator lease refused"))
        try:
            return self._drive(recipe, params, timeout_s, poll_s, abort_wait_s)
        finally:
            try:
                self._request("api/release", {})
            except TransportError:
                pass

    def _drive(self, recipe: str, params: dict[str, Any], timeout_s: float, poll_s: float,
               abort_wait_s: float) -> RecipeResult:
        status, body = self._request("api/runs", {"recipe": recipe, "params": params,
                                                  "borrow": True}, command=True)
        if status >= 500:
            return RecipeResult(recipe, None, "unknown",
                                message=str(body.get("error") or "run admission unknown"))
        if status != 202:
            return RecipeResult(recipe, None, "refused",
                                message=str(body.get("error") or f"run refused ({status})"))
        command_id = body.get("command_id")
        result = RecipeResult(recipe, None, "unknown")
        aborted, lost = False, ""
        deadline = self._clock() + timeout_s
        beat_at = self._clock() + 1.0
        while True:
            try:
                state = self._get("api/state")
            except ContractError:
                state = {}
            last = state.get("last_command") or {}
            active = state.get("active_run") or {}
            if result.run_id is None and command_id is not None and last.get("id") == command_id:
                if last.get("result") != "ok":
                    result.outcome = "failed"
                    result.message = str(last.get("message") or "the run did not start")
                    return result
                result.run_id = active.get("run_id")
            if result.run_id is not None and active.get("run_id") == result.run_id:
                if active.get("status") == "held" and not aborted:
                    self._abort(result.run_id)  # no operator prompt for agents
                    aborted = True
                    result.message = str(active.get("detail") or "the run held; abort sent")
                if active.get("closed") and active.get("status") in {"completed", "aborted"}:
                    result.status = active["status"]
                    result.outcome = "ok" if result.status == "completed" else "failed"
                    if result.status == "aborted":
                        result.message = str(active.get("detail") or result.message
                                             or "the run was aborted")
                    result.steps = self.run_steps(result.run_id)
                    return result
            if lost and not aborted:
                result.outcome, result.message = "unknown", f"orchestrator lease lost: {lost}"
                return result
            if self._clock() >= deadline:
                if result.run_id is not None and not aborted:
                    self._abort(result.run_id)
                    aborted = True
                    deadline = self._clock() + abort_wait_s
                    continue
                result.outcome = "timeout"
                result.message = f"no result within {timeout_s:g} s"
                return result
            if self._clock() >= beat_at:
                beat_at = self._clock() + 1.0
                try:
                    renew, reply = self._request("api/renew", {})
                    if renew != 200:
                        lost = str(reply.get("error") or f"renew answered {renew}")
                    self.heartbeat()
                except ContractError as exc:
                    lost = str(exc)
            self._sleep(poll_s)

    def _abort(self, run_id: str) -> None:
        try:
            self._request(f"api/runs/{urllib.parse.quote(str(run_id), safe='')}/abort", {})
        except TransportError:
            pass

    def run_steps(self, run_id: str) -> list[dict[str, Any]]:
        """The recorded steps of one run: ``[{skill, status, result, error}]``."""
        try:
            body = self._get(f"api/runs/{urllib.parse.quote(str(run_id), safe='')}")
        except ContractError:
            return []
        steps = []
        for step in body.get("steps") or []:
            raw = step.get("result_json")
            try:
                value = json.loads(raw) if isinstance(raw, str) and raw else raw
            except ValueError:
                value = raw
            steps.append({"skill": step.get("skill"), "status": step.get("status"),
                          "result": value, "error": step.get("error")})
        return steps

    def orchestrator_state(self) -> dict[str, Any]:
        return self._get("api/state")

    # ------------------------------------------------------------ microscope
    def microscope_frame(self, microscope_url: str | None = None) -> bytes:
        """The current microscope frame (JPEG bytes). On the real host the microscope service
        serves it (``microscope_url``, e.g. a tunnel to 8770); the simulated console serves it
        under its own base."""
        base = (microscope_url or urllib.parse.urljoin(self.base_url, "sim/microscope/"))
        url = urllib.parse.urljoin(base.rstrip("/") + "/", "api/camera/microscope/frame.jpg")
        request = urllib.request.Request(url, headers={"Accept": "image/jpeg"}, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout_s) as reply:
                status, payload = reply.status, reply.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
            exc.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise TransportError(f"microscope frame not reachable: {exc}", url=url) from None
        if status != 200 or not payload:
            raise ServerError(f"microscope frame: HTTP {status}", status=status, url=url)
        return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _raise_for(status: int, body: dict[str, Any], url: str) -> None:
    if 200 <= status < 300:
        return
    message = str(body.get("error") or body.get("message") or body)
    if status in (400, 413, 415, 422):
        raise BadRequest(message, status=status, url=url)
    if status == 403:
        raise ReactorPolicyError(message, status=status, url=url)
    if status == 404:
        raise NotFound(message, status=status, url=url)
    if status == 409:
        raise Conflict(message, status=status, url=url)
    raise ServerError(message, status=status, url=url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m qumus2.reactor.console",
        description="Read-only check of the reactor Lab Console (GET requests only).")
    parser.add_argument("command", choices=["probe"], help="probe: read state and command lists")
    parser.add_argument("--url", default=VPN_CONSOLE_URL,
                        help=f"console base URL (default {VPN_CONSOLE_URL})")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    report = ReactorConsole(args.url, timeout_s=args.timeout).probe()
    json.dump(report, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0 if report.get("reachable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
