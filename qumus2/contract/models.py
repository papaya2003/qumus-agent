"""Typed views of contract v1 replies.

The reactor services (ADR 0001 of the reactor repo) and the Qumus lab modules
(notes/night-20261108/contract-v1-qumus.md) speak the same contract, but the
Qumus spec file uses a few different names. These classes accept both spellings
so callers never look at raw field names:

| Meaning              | ADR 0001 name                | Qumus spec-file alias          |
|----------------------|------------------------------|--------------------------------|
| command list         | describe.commands {name: ..} | describe.actions [..] or {..}  |
| command receipt id   | last_command.id              | last_command.command_id        |
| command outcome      | last_command.result          | last_command.status            |
| outcome text         | last_command.message         | last_command.error             |
| lease time left      | lease.remaining_s            | lease.expires_unix             |

The original reply is always kept in ``raw``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: last_command results that mean the command has ended.
FINAL_RESULTS = frozenset({"ok", "failed", "rejected", "stopped"})

# Qumus spec-file status words mapped onto ADR 0001 results. Words that mean
# "still running" map to None.
_STATUS_ALIASES: dict[str, str | None] = {
    "ok": "ok",
    "done": "ok",
    "succeeded": "ok",
    "success": "ok",
    "failed": "failed",
    "error": "failed",
    "rejected": "rejected",
    "stopped": "stopped",
    "accepted": None,
    "started": None,
    "running": None,
    "pending": None,
}

STATE_KEYS = frozenset({
    "service", "seq", "ts_unix", "demo", "connected", "busy", "locked", "fault",
    "lease", "last_command",
})


def _as_bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


@dataclass(frozen=True)
class CommandSpec:
    """One command a service offers, as listed by describe."""

    name: str
    params: dict[str, Any]
    any_client: bool = False
    #: Qumus extension. None when the service does not say (the reactor never does).
    motion: bool | None = None
    #: Qumus extension: only accepted from the lab PC itself (debug page, probe scripts).
    local_only: bool = False
    #: Reactor extension: meant for interactive pages only (robot "keyboard").
    ui_only: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def param_names(self) -> list[str]:
        return list((self.params or {}).get("properties", {}))

    @property
    def required_params(self) -> list[str]:
        return list((self.params or {}).get("required", []))

    @classmethod
    def from_raw(cls, name: str, raw: dict[str, Any]) -> CommandSpec:
        raw = raw if isinstance(raw, dict) else {}
        params = raw.get("params")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}, "additionalProperties": False}
        return cls(
            name=name,
            params=params,
            any_client=_as_bool(raw.get("any_client")),
            motion=raw.get("motion") if isinstance(raw.get("motion"), bool) else None,
            local_only=_as_bool(raw.get("local_only")),
            ui_only=_as_bool(raw.get("ui_only")),
            raw=raw,
        )


@dataclass(frozen=True)
class ServiceDescription:
    """GET api/describe, normalised."""

    service: str
    contract: int
    demo: bool
    commands: dict[str, CommandSpec]
    lease_seconds: float = 3.0
    on_lease_expiry: str = ""
    renew_only_heartbeat: bool = False
    client_header: str | None = None
    version: str = ""
    zones: tuple[str, ...] = ()
    #: True when the service listed its commands under "actions" (Qumus spec-file style).
    uses_actions_key: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> ServiceDescription:
        uses_actions = "commands" not in raw and "actions" in raw
        listed = raw.get("commands") if not uses_actions else raw.get("actions")
        commands: dict[str, CommandSpec] = {}
        if isinstance(listed, dict):
            for name, spec in listed.items():
                commands[str(name)] = CommandSpec.from_raw(str(name), spec)
        elif isinstance(listed, list):
            for spec in listed:
                if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                    commands[spec["name"]] = CommandSpec.from_raw(spec["name"], spec)
        contract = raw.get("contract")
        try:
            contract_number = int(contract)
        except (TypeError, ValueError):
            contract_number = -1
        # the Qumus sim lab names it lease_s; the reactor/ADR dialect lease_seconds
        lease = raw.get("lease_seconds", raw.get("lease_s", 3))
        lease_seconds = float(lease) if isinstance(lease, (int, float)) and lease > 0 else 3.0
        zones = raw.get("zones") if isinstance(raw.get("zones"), list) else []
        header = raw.get("client_header")
        return cls(
            service=str(raw.get("service", "")),
            contract=contract_number,
            demo=_as_bool(raw.get("demo")),
            commands=commands,
            lease_seconds=lease_seconds,
            on_lease_expiry=str(raw.get("on_lease_expiry", "")),
            renew_only_heartbeat=_as_bool(raw.get("renew_only_heartbeat")),
            client_header=header if isinstance(header, str) and header else None,
            version=str(raw.get("version", "")),
            zones=tuple(str(z) for z in zones),
            uses_actions_key=uses_actions,
            raw=raw,
        )


@dataclass(frozen=True)
class LastCommand:
    """state.last_command, normalised. ``result`` is None while the command runs."""

    id: str | None
    action: str
    client: str
    result: str | None
    message: str
    finished_at: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def finished(self) -> bool:
        return self.result in FINAL_RESULTS

    @property
    def ok(self) -> bool:
        return self.result == "ok"

    @classmethod
    def from_raw(cls, raw: Any) -> LastCommand | None:
        if not isinstance(raw, dict):
            return None
        ident = raw.get("id", raw.get("command_id"))
        word = raw.get("result", raw.get("status"))
        result = _STATUS_ALIASES.get(word, word) if isinstance(word, str) else None
        message = raw.get("message")
        if message is None:
            message = raw.get("error") or ""
        finished = raw.get("finished_at")
        return cls(
            id=str(ident) if ident is not None else None,
            action=str(raw.get("action", "")),
            client=str(raw.get("client", "")),
            result=result,
            message=str(message),
            finished_at=float(finished) if isinstance(finished, (int, float)) else None,
            raw=raw,
        )


@dataclass(frozen=True)
class Lease:
    holder: str | None
    remaining_s: float

    @classmethod
    def from_raw(cls, raw: Any, *, now: float | None = None) -> Lease:
        if not isinstance(raw, dict):
            return cls(holder=None, remaining_s=0.0)
        holder = raw.get("holder")
        remaining = raw.get("remaining_s")
        if not isinstance(remaining, (int, float)):
            expires = raw.get("expires_unix")
            if isinstance(expires, (int, float)):
                remaining = max(0.0, expires - (time.time() if now is None else now))
            else:
                remaining = 0.0
        return cls(holder=holder if isinstance(holder, str) else None,
                   remaining_s=float(remaining))


@dataclass(frozen=True)
class ServiceState:
    """GET api/state, normalised. Device fields stay in ``devices`` / ``extras``."""

    service: str
    seq: int
    ts_unix: float
    demo: bool
    connected: bool
    busy: bool
    locked: bool
    fault: dict[str, Any] | None
    lease: Lease
    last_command: LastCommand | None
    #: Qumus modules put devices under "devices"; reactor services put device
    #: fields at the top level (they end up in ``extras``).
    devices: dict[str, Any]
    extras: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_raw(cls, raw: Any) -> ServiceState:
        raw = raw if isinstance(raw, dict) else {}
        fault = raw.get("fault")
        seq = raw.get("seq")
        ts = raw.get("ts_unix")
        devices = raw.get("devices") if isinstance(raw.get("devices"), dict) else {}
        extras = {k: v for k, v in raw.items() if k not in STATE_KEYS and k != "devices"}
        return cls(
            service=str(raw.get("service", "")),
            seq=int(seq) if isinstance(seq, int) and not isinstance(seq, bool) else 0,
            ts_unix=float(ts) if isinstance(ts, (int, float)) else 0.0,
            demo=_as_bool(raw.get("demo")),
            # Older reactor routes (thermal, vision) omit "connected"; treat missing as True
            # only when there is no fault, so a silent service is not taken as healthy.
            connected=_as_bool(raw.get("connected"), default=fault is None),
            busy=_as_bool(raw.get("busy")),
            locked=_as_bool(raw.get("locked")),
            fault=fault if isinstance(fault, dict) else None,
            lease=Lease.from_raw(raw.get("lease")),
            last_command=LastCommand.from_raw(raw.get("last_command")),
            devices=devices,
            extras=extras,
            raw=raw,
        )


@dataclass(frozen=True)
class Event:
    seq: int
    ts_unix: float
    level: str
    message: str
    fields: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Event:
        fields_ = raw.get("fields")
        if not isinstance(fields_, dict):
            # Qumus spec-file style: kind/action/... at the top level.
            fields_ = {k: v for k, v in raw.items()
                       if k not in {"seq", "ts_unix", "level", "message"}}
        message = raw.get("message")
        if not isinstance(message, str):
            message = str(raw.get("kind") or raw.get("type") or "")
        seq = raw.get("seq")
        ts = raw.get("ts_unix")
        return cls(
            seq=int(seq) if isinstance(seq, int) else 0,
            ts_unix=float(ts) if isinstance(ts, (int, float)) else 0.0,
            level=str(raw.get("level", "info")),
            message=message,
            fields=fields_,
            raw=raw,
        )


@dataclass(frozen=True)
class EventPage:
    events: list[Event]
    #: Pass this as ``since`` next time.
    next: int
    #: True when the service's sequence went backwards (it restarted; reactor
    #: services renumber from 0). Read again from 0 and treat state as new.
    restarted: bool = False


@dataclass(frozen=True)
class CommandReceipt:
    """The 202 answer of POST api/command: accepted, not finished."""

    action: str
    command_id: str | None
    state: ServiceState
    sent_at: float


@dataclass(frozen=True)
class StopResult:
    """POST api/stop. ``stop`` is confirmed, pending or unconfirmed."""

    stop: str
    state: ServiceState | None

    @property
    def confirmed(self) -> bool:
        return self.stop == "confirmed"
