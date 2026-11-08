"""Reactor services (labmate's dobot_mg400 repo) as the agent's third lab window.

Everything here is written from the reactor repo's public docs (ADR 0001
contract v1, ADR 0003 ports, ADR 0008 agent approvals, docs/*.md). No reactor
code is copied (the repo is AGPL-3.0). See docs/reactor-gaps.md for what
does not connect yet.

The reactor runs one small HTTP service per device on one Linux host
(Olares). Each service listens on loopback; the VPN is the access boundary.
``ReactorLab`` gives the agent:

* ``discover()``: describe + state of every service, or the reason it failed;
* ``client(service)``: a ``ContractClient`` with the reactor's header rules and
  the Qumus agent policy guard;
* ``stop_all()``: contract stop on every service (any client, no lease);
* ``capability_table()``: each offered command, whether the agent may send it
  tonight, and which Qumus action it corresponds to.

Agent policy (Qumus side, conservative default until Jialei and the reactor
owner decide otherwise): the agent may only send commands that move nothing
and heat nothing. Raw coordinates are never sent (Qumus rule: actions, not
coordinates), and the reactor's own ADR 0008 also refuses raw stage moves from
agents.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..contract import (
    ContractClient,
    ContractError,
    Forbidden,
    ServiceDescription,
    ServiceState,
    StopResult,
)
from ..contract.models import CommandSpec

#: Header the reactor's labkit services read the client id from (ADR 0001).
REACTOR_CLIENT_HEADER = "X-MG400-Client"
DEFAULT_CLIENT_ID = "qumus-agent"


class ReactorPolicyError(Forbidden):
    """The Qumus agent policy refuses this reactor command (nothing was sent)."""


@dataclass(frozen=True)
class ReactorService:
    """Static facts about one reactor service, from the reactor docs."""

    id: str
    port: int
    #: base path of the contract routes; stage uses /contract/, the installed
    #: orchestrator unit uses /lab/ (its --demo default is /).
    base_path: str
    role: str
    #: commands the Qumus agent may send under tonight's policy
    agent_actions: frozenset[str] = frozenset()
    #: commands that exist but are refused, with the reason (for the gap table)
    refused: dict[str, str] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: Qumus contract action names this service covers (for the capability table)
    qumus_equivalents: dict[str, str] = field(default_factory=dict)
    evidence: str = ""


RAW_COORDS = "raw coordinates/steps: Qumus agent sends named actions only; reactor ADR 0008 also refuses raw stage moves"
NEEDS_OWNER = "moves or heats hardware; needs Jialei + reactor owner decision before agent use"

REACTOR_SERVICES: dict[str, ReactorService] = {
    "robot": ReactorService(
        id="robot", port=8766, base_path="/", role="Dobot MG400 arm + gripper + suction cup",
        refused={
            "execute": RAW_COORDS,
            "start_sync": NEEDS_OWNER, "enable": NEEDS_OWNER, "home": NEEDS_OWNER,
            "grip": NEEDS_OWNER, "initialize_gripper": NEEDS_OWNER, "vacuum": NEEDS_OWNER,
            "keyboard": "ui_only (interactive pages only)",
            "clear_alarm": "human recovery step, never automatic",
            "disable": "allowed only as part of stop handling by a person",
            "stop_sync": NEEDS_OWNER,
        },
        # Robot HTTP routes also require this fixed header (docs/development.md).
        extra_headers={"X-MG400-Panel": "1"},
        qumus_equivalents={"vacuum": "set_switch", "execute": "(go_to_point uses named stations)",
                           "home": "home_query is query-only in Qumus"},
        evidence="docs/development.md 'Robot service interface'; apps/control_panel/server.py",
    ),
    "stage": ReactorService(
        id="stage", port=8767, base_path="/contract/", role="three-motor stage (crucible insert)",
        refused={"move": RAW_COORDS, "hold": RAW_COORDS, "keepalive": RAW_COORDS,
                 "release_hold": RAW_COORDS, "set_microstep": RAW_COORDS,
                 "stop_all": "use the contract stop endpoint instead"},
        qumus_equivalents={"move": "jog (local_only in Qumus)"},
        evidence="docs/development.md 'Stage automation contract'",
    ),
    "thermal": ReactorService(
        id="thermal", port=8768, base_path="/", role="P1 thermal camera (crucible temperature)",
        agent_actions=frozenset({"connect", "shutter"}),
        qumus_equivalents={"connect": "read_all"},
        evidence="docs/thermal.md 'Contract'",
    ),
    "furnace": ReactorService(
        id="furnace", port=8769, base_path="/", role="tube furnace + 3 gas MFCs",
        refused={
            "apply_setpoint": NEEDS_OWNER + "; also no timeout_s parameter (Qumus heat rule)",
            "set_sequence": NEEDS_OWNER, "start_sequence": NEEDS_OWNER,
            "set_gas_flow": NEEDS_OWNER, "set_gas_valve": NEEDS_OWNER,
            "connect": NEEDS_OWNER, "connect_flow": NEEDS_OWNER,
            "inspect_gas_flow": NEEDS_OWNER,
        },
        qumus_equivalents={"apply_setpoint": "heat (no timeout_s)", "start_sequence": "heat",
                           "(api/stop)": "heat_off"},
        evidence="docs/furnace.md 'API and troubleshooting'; ADR 0005",
    ),
    "microscope": ReactorService(
        id="microscope", port=8770, base_path="/", role="furnace-window microscope (focus, turret, camera)",
        agent_actions=frozenset({"snapshot"}),
        refused={"focus_move": RAW_COORDS, "objective_nudge": RAW_COORDS,
                 "select_objective": NEEDS_OWNER, "autofocus": NEEDS_OWNER,
                 "home_focus": NEEDS_OWNER,
                 "start_recording": NEEDS_OWNER, "stop_recording": NEEDS_OWNER,
                 "record_with_sequence": NEEDS_OWNER, "set_stabilization": NEEDS_OWNER},
        qumus_equivalents={"snapshot": "capture"},
        evidence="docs/microscope.md 'Contract routes'",
    ),
    "vision": ReactorService(
        id="vision", port=8771, base_path="/", role="tray cameras: chip detection, QR labels, slot state",
        agent_actions=frozenset({"detect", "read_labels", "read_markers", "observe_slots",
                                 "check_calibration"}),
        refused={"calibrate_camera": "writes calibration; operator step"},
        qumus_equivalents={"detect": "capture + recognition", "observe_slots": "read_all (tray)"},
        evidence="docs/vision.md 'Service routes'",
    ),
    "orchestrator": ReactorService(
        id="orchestrator", port=8772, base_path="/", role="recipes, runs, zones, Lab Console",
        refused={"*": "runs start only through a person (ADR 0008); agent reads recipes/runs only"},
        evidence="docs/orchestrator.md 'HTTP service'; ADR 0008",
    ),
}

#: Services that implement contract v1 (the MCP agent gateway on 8773 does not).
CONTRACT_SERVICES = tuple(REACTOR_SERVICES)


def agent_guard(service: ReactorService):
    """Build the ContractClient guard that enforces the agent policy for one service."""

    def guard(action: str, params: dict[str, Any], spec: CommandSpec) -> None:
        if spec.ui_only:
            raise ReactorPolicyError(f"{service.id}.{action} is ui_only")
        if action not in service.agent_actions:
            reason = service.refused.get(action) or service.refused.get("*") or (
                "not on the agent allow-list for this service")
            raise ReactorPolicyError(f"reactor {service.id}.{action} refused: {reason}")

    return guard


@dataclass
class ServiceReport:
    id: str
    url: str
    reachable: bool
    description: ServiceDescription | None = None
    state: ServiceState | None = None
    error: str = ""

    @property
    def demo(self) -> bool | None:
        return self.description.demo if self.description else None


class ReactorLab:
    """The reactor as seen from the Qumus agent.

    ``host`` is where the reactor services answer: ``127.0.0.1`` on the reactor
    host itself or through an SSH/VPN tunnel (the services bind loopback only).
    ``endpoints`` overrides individual base URLs (tests, tunnels with other ports).
    """

    def __init__(self, host: str = "127.0.0.1", *, client_id: str = DEFAULT_CLIENT_ID,
                 endpoints: dict[str, str] | None = None, services: tuple[str, ...] | None = None,
                 timeout_s: float = 5.0, enforce_policy: bool = True) -> None:
        self.host = host
        self.client_id = client_id
        self.endpoints = dict(endpoints or {})
        self.services = tuple(services or CONTRACT_SERVICES)
        unknown = set(self.services) - set(REACTOR_SERVICES)
        if unknown:
            raise ValueError(f"unknown reactor services: {sorted(unknown)}")
        self.timeout_s = timeout_s
        self.enforce_policy = enforce_policy
        self._clients: dict[str, ContractClient] = {}

    def base_url(self, service_id: str) -> str:
        if service_id in self.endpoints:
            return self.endpoints[service_id]
        info = REACTOR_SERVICES[service_id]
        return f"http://{self.host}:{info.port}{info.base_path}"

    def client(self, service_id: str) -> ContractClient:
        if service_id not in self._clients:
            info = REACTOR_SERVICES[service_id]
            self._clients[service_id] = ContractClient(
                self.base_url(service_id), self.client_id,
                client_header=REACTOR_CLIENT_HEADER,
                extra_headers=info.extra_headers,
                timeout_s=self.timeout_s,
                body_style="flat",
                guard=agent_guard(info) if self.enforce_policy else None,
                name=f"reactor:{service_id}",
            )
        return self._clients[service_id]

    # ------------------------------------------------------------- reads
    def discover(self) -> dict[str, ServiceReport]:
        reports: dict[str, ServiceReport] = {}
        for sid in self.services:
            c = self.client(sid)
            report = ServiceReport(id=sid, url=c.base_url, reachable=False)
            try:
                report.description = c.describe(refresh=True)
                report.state = c.state()
                report.reachable = True
            except ContractError as exc:
                report.error = str(exc)
            reports[sid] = report
        return reports

    def stop_all(self, wait_s: float = 0.0) -> dict[str, StopResult | ContractError]:
        """Contract stop on every service. Never raises; returns each outcome."""
        out: dict[str, StopResult | ContractError] = {}
        for sid in self.services:
            try:
                out[sid] = self.client(sid).stop(wait_s=wait_s)
            except ContractError as exc:
                out[sid] = exc
        return out

    def capability_table(self, reports: dict[str, ServiceReport] | None = None) -> list[dict[str, Any]]:
        """One row per command offered by a reachable service, with the agent verdict."""
        reports = reports if reports is not None else self.discover()
        rows: list[dict[str, Any]] = []
        for sid, report in reports.items():
            info = REACTOR_SERVICES[sid]
            if report.description is None:
                rows.append({"service": sid, "action": None, "offered": False,
                             "agent_allowed": False, "reason": report.error,
                             "qumus_action": None})
                continue
            for name, spec in sorted(report.description.commands.items()):
                allowed = name in info.agent_actions and not spec.ui_only
                rows.append({
                    "service": sid, "action": name, "offered": True,
                    "agent_allowed": allowed,
                    "any_client": spec.any_client,
                    "reason": "" if allowed else (info.refused.get(name)
                                                  or info.refused.get("*")
                                                  or "not on the agent allow-list"),
                    "qumus_action": info.qumus_equivalents.get(name),
                    "params": spec.param_names,
                })
        return rows

    def orchestrator_read(self, path: str) -> Any:
        """GET a read-only orchestrator route (``api/recipes``, ``api/runs``, ``api/runs/<id>``)."""
        clean = path.lstrip("/")
        if not (clean in {"api/recipes", "api/runs", "api/state", "api/describe"}
                or clean.startswith("api/runs/")) or "/../" in f"/{clean}/":
            raise ReactorPolicyError(f"orchestrator route not allowed for the agent: {path}")
        url = urllib.parse.urljoin(self.base_url("orchestrator"), clean)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(urllib.request.Request(url, method="GET"),
                             timeout=self.timeout_s) as reply:
                return json.loads(reply.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise ContractError(f"orchestrator {clean}: {exc.reason}", status=exc.code,
                                url=url) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ContractError(f"orchestrator {clean}: {exc}", url=url) from None
