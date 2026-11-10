"""The brain's vdW reactor tools, in the reactor's own vocabulary (Jialei, 2026-11-10).

No translator: the reactor station (stations.yaml ``adapter: console``) is the
reactor's Lab Console, simulated by qumus-simlab 8783 under ``/lab/`` or real on
Olares through the SSH tunnel. Spec: notes/vdw-reactor-20261110/reactor-native-spec.md.

    reactor_status                         read the console and every device
    reactor_handover_in  chip_id, slot     a person puts the chip into tray `slot`
    reactor_load         chip_id, from_slot  recipe crucible-load
    reactor_heat         chip_id, steps    furnace start_sequence (+ microscope recording)
    reactor_snapshot     chip_id           microscope snapshot + frame
    reactor_record       chip_id, seconds  recipe microscope-record
    reactor_unload       chip_id, to_slot  recipe crucible-unload (waits until cold)
    reactor_handover_out chip_id, slot     a person takes the chip out of tray `slot`
    reactor_stop                           motion stop + furnace stop (heat and gas off)

Each call is one run in the brain's store (flow ``reactor_<tool>``, its steps and
events), sent once and never retried; an unknown outcome is reported and the state
read. The chip's place is kept in the sample ledger: ``reactor:tray:<slot>``,
``reactor:crucible``, ``person`` (``reactor:unknown`` after an unclear outcome).

Chips are handed over by a person for now. In sim the handover places or removes
the chip directly (sim-only ``POST /lab/sim/handover``). On the real reactor the
tool asks the person (a needs_human notification) and returns ``pending``; it is
called again with ``confirmed: true`` after the person has written back.

Gas is percent of each MFC's full scale (the reactor's unit). Temperature and ramp
limits are the reactor host's own; the brain adds none. On the real reactor,
motion and heating are refused before sending unless stations.yaml sets
``allow_actuation: true`` (Jialei's call).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.contract.errors import ContractError
from qumus2.reactor.adapter import ReactorPolicyError
from qumus2.reactor.console import RECIPE_DEVICES, ReactorConsole, RecipeResult

TRAY = "reactor:tray:"
CRUCIBLE = "reactor:crucible"
PERSON = "person"
UNKNOWN = "reactor:unknown"
#: [default] tray slot names of the simulated reactor (the real names come from the owner)
SIM_SLOTS = ("A1", "A2", "A3", "A4")
#: [default] furnace temperature below which loading is sent (the host checks its own load_max_c)
LOAD_BELOW_C = 100.0
_CHIP = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"
_SLOT = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"


@dataclass
class ReactorStation:
    """What the tools need about the configured reactor station."""

    console: ReactorConsole
    kind: str = "sim"  # sim | real
    microscope_url: str | None = None
    image_dir: Path | None = None
    slots: tuple[str, ...] | None = None  # known tray slot names (None: not checked)
    furnace_timeout_s: float = 6 * 3600.0
    unload_timeout_s: float = 4 * 3600.0

    @property
    def sim(self) -> bool:
        return self.kind == "sim"

    @classmethod
    def from_station(cls, station: Any, *, image_dir: str | Path | None = None) -> ReactorStation:
        """From a stations.yaml entry (stations.config.Station with adapter: console)."""
        extra = dict(station.extra)
        sim = station.kind == "sim"
        console = ReactorConsole(station.url, session=str(extra.get("session") or "qumus"),
                                 allow_actuation=sim or extra.get("allow_actuation") is True)
        slots = extra.get("slots")
        return cls(console, station.kind, extra.get("microscope_url"),
                   Path(image_dir) if image_dir else None,
                   tuple(slots) if slots else (SIM_SLOTS if sim else None))


def station_of(ctx: ToolContext) -> ReactorStation | None:
    station = (ctx.extra.get("stations") or {}).get("reactor")
    return station if isinstance(station, ReactorStation) else None


# --------------------------------------------------------------------- arguments
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NoArgs(_Args):
    pass


class ChipArgs(_Args):
    chip_id: str = Field(pattern=_CHIP, description="The chip's ID in the sample ledger")


class HandoverArgs(ChipArgs):
    slot: str = Field(pattern=_SLOT, description="Reactor tray slot, e.g. A1")
    confirmed: bool = Field(False, description="Real reactor only: true after the person wrote "
                            "back that the handover is done")


class LoadArgs(ChipArgs):
    from_slot: str = Field(pattern=_SLOT, description="Tray slot the chip is in")


class UnloadArgs(ChipArgs):
    to_slot: str = Field(pattern=_SLOT, description="Empty tray slot to put the chip into")


GasPercent = dict[Literal["0", "1", "2"], float]


def _check_gas(value: GasPercent | None) -> GasPercent | None:
    for channel, percent in (value or {}).items():
        if not 0 <= percent <= 100:
            raise ValueError(f"gas channel {channel}: percent of full scale must be 0..100")
    return value


class HeatStep(_Args):
    temperature_c: float = Field(ge=0, le=1000, description="Step target temperature, deg C (furnace max 1000)")
    ramp_rate_c_per_min: float = Field(gt=0, le=100, description="Ramp rate to the target, deg C/min (max 100)")
    hold_minutes: float = Field(ge=0, description="Hold at the target, minutes")
    gas: GasPercent | None = Field(None, description="MFC channel '0'..'2' -> percent of full scale")

    _gas = field_validator("gas")(_check_gas)


class HeatArgs(ChipArgs):
    steps: list[HeatStep] = Field(min_length=1, max_length=20,
                                  description="Temperature program; it always ends")
    end_gas: GasPercent | None = Field(None, description="Gas kept during cooling (needed with step gas)")
    end_gas_until_c: float | None = Field(None, ge=0, description="Cooling gas stays on until "
                                          "the furnace is below this temperature")
    record: bool = Field(False, description="Also record the microscope while the program runs")

    _end_gas = field_validator("end_gas")(_check_gas)

    @model_validator(mode="after")
    def _gas_needs_an_end(self) -> HeatArgs:
        if any(s.gas for s in self.steps) and (self.end_gas is None or self.end_gas_until_c is None):
            raise ValueError("a program with gas needs end_gas and end_gas_until_c (reactor rule)")
        return self


class RecordArgs(ChipArgs):
    seconds: float = Field(gt=0, le=3600)
    mode: Literal["time_lapse", "live"] = "time_lapse"


# ------------------------------------------------------------------------ helpers
class _Run:
    """One reactor tool call as a brain run with its steps."""

    def __init__(self, ctx: ToolContext, tool: str, params: dict[str, Any]) -> None:
        self.ctx, self.seq = ctx, 0
        self.id = ctx.store.create_run(f"reactor_{tool}", params, project_id=ctx.project_id,
                                       requested_by=ctx.agent)
        ctx.store.set_run_status(self.id, "running")

    def step(self, name: str, status: str = "done", measurements: Any = None, note: str = "") -> None:
        self.seq += 1
        self.ctx.store.begin_step(self.id, self.seq, name)
        self.ctx.store.finish_step(self.id, self.seq, status, measurements=measurements, note=note)

    def end(self, status: str, *, result: Any = None, error: Any = None, message: str = "") -> None:
        self.ctx.store.set_run_status(self.id, status, result=result, error=error, message=message)


def _place(ctx: ToolContext, chip_id: str, location: str, **props: Any) -> None:
    ctx.store.upsert_sample(chip_id, "chip", location=location, props=props or None)


def _location(ctx: ToolContext, chip_id: str) -> str | None:
    row = ctx.store.get_sample(chip_id)
    return row["location"] if row else None


def _no_station() -> ToolResult:
    return ToolResult(status="error", summary="No reactor station speaks the Lab Console here "
                      "(stations.yaml reactor needs adapter: console).")


def _denied(exc: ReactorPolicyError) -> ToolResult:
    return ToolResult(status="denied", summary=str(exc), data={"rule": "reactor_actuation"})


def _slot_error(station: ReactorStation, *slots: str) -> str | None:
    if station.slots is None:
        return None
    bad = [s for s in slots if s not in station.slots]
    return f"unknown tray slot {', '.join(bad)}; known: {', '.join(station.slots)}" if bad else None


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


#: devices each brain action needs (recipes take theirs from RECIPE_DEVICES)
ACTION_DEVICES: dict[str, tuple[str, ...]] = {
    "load": RECIPE_DEVICES["crucible-load"], "unload": RECIPE_DEVICES["crucible-unload"],
    "slot_check": RECIPE_DEVICES["slot-state"], "heat": ("furnace",), "snapshot": ("microscope",),
    "record": RECIPE_DEVICES["microscope-record"]}


def readiness(console: ReactorConsole, devices: tuple[str, ...] | list[str], *,
              gas: bool = False) -> list[str]:
    """Why these devices cannot do their job now, from read-only console state.

    Empty list = nothing known to block. Only facts the console states are used
    (a missing field never blocks): a service row with ``connected: false`` or a
    fault, stage automation not configured, vision without a calibrated camera,
    the microscope camera off, and for gas the flow controller not connected."""
    reasons: list[str] = []
    try:
        services = {row.get("id"): row for row in console.overview().get("services") or []
                    if isinstance(row, dict)}
    except ContractError as exc:
        return [f"reactor console not reachable: {exc}"]
    for device in devices:
        row = services.get(device) or {}
        if row.get("connected") is False:
            reasons.append(f"{device} is not connected")
        if row.get("fault"):
            reasons.append(f"{device} fault: {row['fault']}")
        try:
            state = console.device_state(device).get("state") or {}
        except ContractError as exc:
            reasons.append(f"{device} state unreadable: {exc}")
            continue
        if state.get("fault") and not row.get("fault"):
            reasons.append(f"{device} fault: {state['fault']}")
        automation = state.get("automation") if device == "stage" else None
        if isinstance(automation, dict) and automation.get("configured") is False:
            reasons.append("stage automation is not configured"
                           + (f" ({automation['error']})" if automation.get("error") else ""))
        if device == "vision" and state.get("calibrated_cameras") == []:
            reasons.append("vision has no calibrated camera")
        cameras = state.get("cameras") if device == "microscope" else None
        if isinstance(cameras, dict) and cameras.get("microscope") is False:
            reasons.append("the microscope camera is not connected")
        flow = state.get("flow") if device == "furnace" else None
        if gas and isinstance(flow, dict) and flow.get("connected") is False:
            reasons.append("the gas flow controller is not connected")
    return reasons


def _not_ready(action: str, reasons: list[str]) -> ToolResult:
    return ToolResult(status="error", summary=f"reactor not ready to {action}: " + "; ".join(reasons),
                      data={"not_ready": reasons})


def _furnace_summary(state: dict[str, Any]) -> dict[str, Any]:
    sequence = state.get("sequence") or {}
    return {"pv_c": state.get("pv_c", state.get("pv")), "target_c": state.get("target_c", state.get("sv")),
            "control_mode": state.get("control_mode"), "gas_pct": state.get("flow_pct"),
            "sequence_running": sequence.get("running"), "phase": sequence.get("phase"),
            "step_index": sequence.get("current_index"),
            "hold_remaining_s": sequence.get("hold_remaining_seconds"),
            "busy": state.get("busy"), "fault": state.get("fault"),
            "run_state": state.get("run_state"),
            "gas_controller_connected": (state.get("flow") or {}).get("connected")}


def _furnace_busy(state: dict[str, Any]) -> str | None:
    summary = _furnace_summary(state)
    if summary["sequence_running"]:
        return f"the furnace program is running (phase {summary['phase']})"
    pv = summary["pv_c"]
    if isinstance(pv, (int, float)) and pv >= LOAD_BELOW_C:
        return f"the furnace is at {pv:.0f} C (load below {LOAD_BELOW_C:.0f} C)"
    return None


def _recipe_result(run: _Run, recipe: RecipeResult) -> None:
    run.step(recipe.recipe, "done" if recipe.ok else "failed", measurements=recipe.to_dict(),
             note=recipe.message)


def _program_minutes(start_c: float, steps: list[HeatStep]) -> float:
    minutes, current = 0.0, start_c
    for step in steps:
        minutes += abs(step.temperature_c - current) / step.ramp_rate_c_per_min + step.hold_minutes
        current = step.temperature_c
    return minutes


def _chip_ledger(ctx: ToolContext) -> list[dict[str, Any]]:
    return [{"chip_id": s["id"], "location": s["location"]}
            for s in ctx.store.list_samples(kind="chip")
            if (s["location"] or "").startswith("reactor:") or s["props"].get("reactor_handover")]


# -------------------------------------------------------------------------- tools
@qumus_tool("reactor_status", "Read the vdW reactor: console, control holders, furnace "
            "(temperature, target, mode, gas %, program phase), stage, thermal, microscope, "
            "vision, robot, the current recipe run, and where the brain's reactor chips are. "
            "Moves and heats nothing.", agents={"reactor", "orchestrator"}, kind="query")
async def reactor_status(args: NoArgs, ctx: ToolContext) -> ToolResult:
    station = station_of(ctx)
    if station is None:
        return _no_station()
    console = station.console
    try:
        overview = await _call(console.overview)
    except ContractError as exc:
        return ToolResult(status="error", summary=f"reactor console not reachable: {exc}")
    services = {row.get("id"): row for row in overview.get("services") or [] if isinstance(row, dict)}
    devices: dict[str, Any] = {}
    for device in ("furnace", "stage", "thermal", "microscope", "vision", "robot"):
        try:
            body = await _call(console.device_state, device)
        except ContractError as exc:
            devices[device] = {"error": str(exc)}
            continue
        state = body.get("state") or {}
        entry = {k: state.get(k) for k in ("busy", "locked", "fault", "connected") if k in state}
        entry["holder"] = (body.get("control") or {}).get("holder")
        row = services.get(device) or {}
        if "connected" in row:
            entry["connected"] = row["connected"]
        if device == "furnace":
            entry.update(_furnace_summary(state))
        elif device == "stage":
            entry["automation"] = state.get("automation")
        elif device == "thermal":
            entry.update(stream=state.get("state"), fps=state.get("fps"))
        elif device == "robot":
            entry["enabled"] = state.get("enabled")
        elif device == "vision":
            entry["calibrated_cameras"] = state.get("calibrated_cameras")
        elif device == "microscope":
            entry.update(cameras=state.get("cameras"), objective_id=state.get("objective_id"),
                         recording=(state.get("recording") or {}).get("status"))
        devices[device] = entry
    ready = {action: await _call(readiness, console, needs) for action, needs in ACTION_DEVICES.items()}
    ready["heat_with_gas"] = await _call(readiness, console, ("furnace",), gas=True)
    furnace = devices.get("furnace", {})
    blocked = sorted(action for action, reasons in ready.items() if reasons)
    summary = (f"reactor {station.kind}: furnace {furnace.get('pv_c')} C, "
               f"program {'running' if furnace.get('sequence_running') else 'not running'}"
               f"; run {'active' if overview.get('run') else 'none'}"
               + (f"; not ready: {', '.join(blocked)}" if blocked else ""))
    return ToolResult(status="ok", summary=summary, data={
        "kind": station.kind, "actuation_allowed": console.allow_actuation,
        "console": overview.get("console"), "orchestrator": overview.get("orchestrator"),
        "control": overview.get("control"), "run": overview.get("run"), "devices": devices,
        "ready": ready, "slots": list(station.slots or []), "chips": _chip_ledger(ctx)})


async def _slot_state(station: ReactorStation, slot: str) -> tuple[str | None, RecipeResult | None]:
    """present | empty | None (not checked: motion not allowed or the check failed)."""
    if not station.console.allow_actuation:
        return None, None
    recipe = await _call(station.console.run_recipe, "slot-state", {"slot": slot})
    state = recipe.result.get("state") if recipe.ok and isinstance(recipe.result, dict) else None
    return state, recipe


async def _handover(args: HandoverArgs, ctx: ToolContext, direction: str) -> ToolResult:
    station = station_of(ctx)
    if station is None:
        return _no_station()
    if err := _slot_error(station, args.slot):
        return ToolResult(status="error", summary=err)
    tool = f"handover_{direction}"
    here = _location(ctx, args.chip_id)
    if direction == "out" and here != TRAY + args.slot:
        return ToolResult(status="error", summary=f"{args.chip_id} is at {here}, not in tray slot "
                          f"{args.slot}; unload it to that slot first")
    if direction == "in" and here is not None and here.startswith("reactor:") and here != UNKNOWN \
            and not args.confirmed:
        return ToolResult(status="error", summary=f"{args.chip_id} is already at {here}")
    want = "present" if direction == "in" else "empty"
    if station.sim:
        run = _Run(ctx, tool, args.model_dump())
        body = {"slot": args.slot, "chip_id": args.chip_id if direction == "in" else None}
        try:
            await _call(station.console._post, "sim/handover", body)
        except ContractError as exc:
            run.end("failed", error={"message": str(exc)})
            return ToolResult(status="error", summary=f"sim handover failed: {exc}",
                              data={"run_id": run.id})
        run.step("sim_handover", measurements=body)
        return await _finish_handover(ctx, station, run, args, direction, want)
    # real reactor: a person does it; the open request is kept with the chip (survives a restart)
    row = ctx.store.get_sample(args.chip_id)
    request = (row or {}).get("props", {}).get("reactor_handover_request") or None
    if request and (request.get("direction"), request.get("slot")) != (direction, args.slot):
        request = None
    if not args.confirmed or request is None:
        if args.confirmed:
            return ToolResult(status="error", summary="No open handover request for this chip and "
                              "slot: call without confirmed first so a person is asked.")
        run = _Run(ctx, tool, args.model_dump())
        text = (f"请把芯片 {args.chip_id} 放进 reactor 托盘 {args.slot}，放好后回复“好了”。"
                f" / Please put chip {args.chip_id} into reactor tray slot {args.slot} and reply when done."
                if direction == "in" else
                f"请从 reactor 托盘 {args.slot} 取走芯片 {args.chip_id}，取走后回复“好了”。"
                f" / Please take chip {args.chip_id} out of reactor tray slot {args.slot} and reply when done.")
        event_id = ctx.store.emit("notification", {"level": "needs_human", "text": text},
                                  run_id=run.id, agent=ctx.agent)
        ctx.store.upsert_sample(args.chip_id, "chip", props={"reactor_handover_request": {
            "direction": direction, "slot": args.slot, "run_id": run.id, "after_event": event_id}})
        run.step("ask_person", measurements={"text": text})
        ctx.store.set_run_status(run.id, "waiting_human", message="waiting for the person")
        return ToolResult(status="pending", summary="A person was asked to do the handover. End "
                          "your turn; when the person writes back that it is done, call this tool "
                          "again with confirmed: true.", data={"run_id": run.id, "asked": text})
    reply = _person_reply(ctx, int(request["after_event"]))
    if reply is None:
        return ToolResult(status="pending", summary="The person has not written back yet; wait "
                          "for their message before confirming.", data={"run_id": request["run_id"]})
    ctx.store.upsert_sample(args.chip_id, "chip", props={"reactor_handover_request": None})
    run = _RunRef(ctx, request["run_id"])
    ctx.store.set_run_status(run.id, "running")
    run.step("person_confirmed", measurements={"message": reply})
    return await _finish_handover(ctx, station, run, args, direction, want)


def _person_reply(ctx: ToolContext, after_event: int) -> str | None:
    """The person's last message after the handover request (None: no message yet)."""
    text, cursor = None, after_event
    while events := ctx.store.events_after(cursor, limit=500):
        for event in events:
            if event.kind == "user.message":
                text = str(event.payload.get("text") or "")
        cursor = events[-1].id
    return text


class _RunRef(_Run):
    """An existing run (a handover waiting for its person), continued."""

    def __init__(self, ctx: ToolContext, run_id: str) -> None:  # noqa: D107 - no new run
        self.ctx, self.id = ctx, run_id
        self.seq = len(ctx.store.get_steps(run_id))


async def _finish_handover(ctx: ToolContext, station: ReactorStation, run: _Run,
                           args: HandoverArgs, direction: str, want: str) -> ToolResult:
    try:
        seen, recipe = await _slot_state(station, args.slot)
    except ReactorPolicyError:
        seen, recipe = None, None
    if recipe is not None:
        _recipe_result(run, recipe)
    if seen is not None and seen != want:
        run.end("failed", error={"slot": args.slot, "seen": seen, "expected": want})
        return ToolResult(status="needs_attention", summary=f"tray slot {args.slot} looks {seen}, "
                          f"expected {want}; the ledger was not changed", data={"run_id": run.id})
    location = TRAY + args.slot if direction == "in" else PERSON
    _place(ctx, args.chip_id, location, reactor_handover=direction)
    checked = "checked by vision" if seen is not None else "not checked by vision (reactor motion not allowed)"
    run.end("succeeded", result={"location": location, "slot_state": seen})
    return ToolResult(status="ok", summary=f"{args.chip_id} is now at {location}; slot {checked}",
                      data={"run_id": run.id, "location": location, "slot_state": seen})


@qumus_tool("reactor_handover_in", "A person puts the chip into a reactor tray slot (chips are "
            "handed over by hand for now). Sim: placed directly. Real: asks the person, returns "
            "pending; call again with confirmed: true after the person wrote back. Then the tray "
            "camera checks the slot when reactor motion is allowed.",
            agents={"reactor"}, kind="hardware")
async def reactor_handover_in(args: HandoverArgs, ctx: ToolContext) -> ToolResult:
    return await _handover(args, ctx, "in")


@qumus_tool("reactor_handover_out", "A person takes the chip out of a reactor tray slot. Sim: "
            "removed directly. Real: asks the person, returns pending; call again with "
            "confirmed: true after the person wrote back.", agents={"reactor"}, kind="hardware")
async def reactor_handover_out(args: HandoverArgs, ctx: ToolContext) -> ToolResult:
    return await _handover(args, ctx, "out")


async def _recipe_tool(ctx: ToolContext, tool: str, params: dict[str, Any], recipe: str,
                       recipe_params: dict[str, Any], *, chip_id: str, from_location: str,
                       to_location: str, timeout_s: float) -> ToolResult:
    station = station_of(ctx)
    if station is None:
        return _no_station()
    here = _location(ctx, chip_id)
    if here != from_location:
        return ToolResult(status="error", summary=f"{chip_id} is at {here}, not {from_location}")
    console = station.console
    try:
        furnace = (await _call(console.device_state, "furnace")).get("state") or {}
    except ContractError as exc:
        return ToolResult(status="error", summary=f"reactor console not reachable: {exc}")
    if (busy := _furnace_busy(furnace)) and recipe == "crucible-load":
        return ToolResult(status="error", summary=f"not loading: {busy}")
    if _furnace_summary(furnace)["sequence_running"]:
        return ToolResult(status="error", summary="the furnace program is still running")
    if reasons := await _call(readiness, console, RECIPE_DEVICES[recipe]):
        return _not_ready(tool, reasons)
    run = _Run(ctx, tool, params)
    run.step("furnace_check", measurements=_furnace_summary(furnace))
    try:
        result = await _call(console.run_recipe, recipe, recipe_params, timeout_s=timeout_s)
    except ReactorPolicyError as exc:
        run.end("cancelled", error={"message": str(exc)})
        return _denied(exc)
    _recipe_result(run, result)
    if result.ok:
        _place(ctx, chip_id, to_location)
        run.end("succeeded", result=result.to_dict())
        return ToolResult(status="ok", summary=f"{recipe} completed; {chip_id} is at {to_location}",
                          data={"run_id": run.id, "location": to_location, "recipe": result.to_dict()})
    if result.outcome == "refused":  # nothing started: the chip is where it was
        run.end("failed", error=result.to_dict())
        return ToolResult(status="error", summary=f"{recipe} refused: {result.message}",
                          data={"run_id": run.id, "recipe": result.to_dict()})
    _place(ctx, chip_id, UNKNOWN, reactor_state_uncertain=True)
    run.end("failed", error=result.to_dict())
    return ToolResult(status="needs_attention", summary=f"{recipe} {result.outcome}: {result.message}. "
                      f"The chip's place is unknown; read reactor_status and ask a person before "
                      f"anything else moves.", data={"run_id": run.id, "recipe": result.to_dict()})


@qumus_tool("reactor_load", "Load the chip from its tray slot into the furnace crucible "
            "(reactor recipe crucible-load: robot and stage). The furnace must be off and cool.",
            agents={"reactor"}, kind="hardware")
async def reactor_load(args: LoadArgs, ctx: ToolContext) -> ToolResult:
    station = station_of(ctx)
    if station is not None and (err := _slot_error(station, args.from_slot)):
        return ToolResult(status="error", summary=err)
    return await _recipe_tool(ctx, "load", args.model_dump(), "crucible-load",
                              {"from_slot": args.from_slot}, chip_id=args.chip_id,
                              from_location=TRAY + args.from_slot, to_location=CRUCIBLE,
                              timeout_s=600.0)


@qumus_tool("reactor_unload", "Take the chip out of the crucible back into an empty tray slot "
            "(reactor recipe crucible-unload). The reactor waits until its thermal camera sees "
            "the crucible cold, so this can take long.", agents={"reactor"}, kind="hardware")
async def reactor_unload(args: UnloadArgs, ctx: ToolContext) -> ToolResult:
    station = station_of(ctx)
    if station is not None and (err := _slot_error(station, args.to_slot)):
        return ToolResult(status="error", summary=err)
    return await _recipe_tool(ctx, "unload", args.model_dump(), "crucible-unload",
                              {"to_slot": args.to_slot}, chip_id=args.chip_id,
                              from_location=CRUCIBLE, to_location=TRAY + args.to_slot,
                              timeout_s=station.unload_timeout_s if station else 3600.0)


@qumus_tool("reactor_heat", "Run a temperature program on the chip in the crucible: steps of "
            "target temperature, ramp rate and hold, each with optional gas (percent of each "
            "MFC's full scale; gas needs end_gas and end_gas_until_c). Waits until the program "
            "ends. Optional microscope recording. Limits are the reactor's own.",
            agents={"reactor"}, kind="hardware")
async def reactor_heat(args: HeatArgs, ctx: ToolContext) -> ToolResult:
    station = station_of(ctx)
    if station is None:
        return _no_station()
    here = _location(ctx, args.chip_id)
    if here != CRUCIBLE:
        return ToolResult(status="error", summary=f"{args.chip_id} is at {here}, not in the crucible")
    console = station.console
    steps = [s.model_dump(exclude_none=True) for s in args.steps]
    params: dict[str, Any] = {"steps": steps}
    if args.end_gas is not None:
        params["end_gas"] = args.end_gas
        params["end_gas_until_c"] = args.end_gas_until_c
    try:
        furnace = (await _call(console.device_state, "furnace")).get("state") or {}
    except ContractError as exc:
        return ToolResult(status="error", summary=f"reactor console not reachable: {exc}")
    if _furnace_summary(furnace)["sequence_running"]:
        return ToolResult(status="error", summary="a furnace program is already running")
    gas = args.end_gas is not None or any(step.gas for step in args.steps)
    needs = ("furnace", "microscope") if args.record else ("furnace",)
    if reasons := await _call(readiness, console, needs, gas=gas):
        return _not_ready("heat", reasons)
    start = _furnace_summary(furnace)["pv_c"]
    minutes = _program_minutes(start if isinstance(start, (int, float)) else 25.0, args.steps)
    run = _Run(ctx, "heat", args.model_dump(exclude_none=True))
    try:
        await _call(console.ensure_control, ["furnace"])
        started = await _call(console.command, "furnace", "start_sequence", params, timeout_s=60.0)
    except ReactorPolicyError as exc:
        run.end("cancelled", error={"message": str(exc)})
        return _denied(exc)
    except ContractError as exc:  # refused (409/422) or unknown: nothing more is sent
        run.end("failed", error={"message": str(exc), "type": type(exc).__name__})
        state = await _call(_safe_furnace, console)
        return ToolResult(status="needs_attention" if "Unknown" in type(exc).__name__ else "error",
                          summary=f"start_sequence: {exc}", data={"run_id": run.id, "furnace": state})
    run.step("start_sequence", "done" if started.outcome == "ok" else "failed",
             measurements={"outcome": started.outcome, "message": started.message,
                           "planned_minutes": round(minutes, 1)})
    if started.outcome != "ok":
        run.end("failed", error={"outcome": started.outcome, "message": started.message})
        return ToolResult(status="error", summary=f"furnace start_sequence {started.outcome}: "
                          f"{started.message}", data={"run_id": run.id})
    recording = None
    if args.record:
        seconds = min(3600.0, max(1.0, math.ceil(minutes * 60)))
        try:
            recording = await _call(console.run_recipe, "microscope-record",
                                    {"chip_id": args.chip_id, "seconds": seconds, "mode": "time_lapse"},
                                    timeout_s=seconds + 600)
            _recipe_result(run, recording)
        except ReactorPolicyError as exc:
            run.step("microscope-record", "skipped", note=str(exc))
    final = await _call(console.furnace_wait, timeout_s=station.furnace_timeout_s,
                        poll_s=0.05 if station.sim else 5.0)
    summary = _furnace_summary(final)
    run.step("program_end", "failed" if final.get("_timeout") else "done", measurements=summary)
    if final.get("_timeout"):
        run.end("failed", error={"message": "program did not end in time", "furnace": summary})
        return ToolResult(status="needs_attention", summary="The furnace program did not end in "
                          "time; it may still be heating. Read reactor_status; reactor_stop turns "
                          "heat and gas off.", data={"run_id": run.id, "furnace": summary})
    _place(ctx, args.chip_id, CRUCIBLE, reactor_last_program=steps)
    result = {"furnace": summary, "planned_minutes": round(minutes, 1),
              "recording": recording.to_dict() if recording else None}
    run.end("succeeded", result=result)
    return ToolResult(status="ok", summary=f"program ended (phase {summary['phase']}, furnace "
                      f"{summary['pv_c']} C); the chip stays in the crucible",
                      data={"run_id": run.id, **result})


def _safe_furnace(console: ReactorConsole) -> dict[str, Any]:
    try:
        return _furnace_summary(console.device_state("furnace").get("state") or {})
    except ContractError as exc:
        return {"error": str(exc)}


@qumus_tool("reactor_snapshot", "Take one microscope picture through the furnace window and "
            "return it. Moves and heats nothing.", agents={"reactor"}, kind="hardware")
async def reactor_snapshot(args: ChipArgs, ctx: ToolContext) -> ToolResult:
    station = station_of(ctx)
    if station is None:
        return _no_station()
    console = station.console
    if reasons := await _call(readiness, console, ACTION_DEVICES["snapshot"]):
        return _not_ready("take a picture", reasons)
    run = _Run(ctx, "snapshot", args.model_dump())
    try:
        await _call(console.ensure_control, ["microscope"])
        shot = await _call(console.command, "microscope", "snapshot", {}, timeout_s=60.0)
        run.step("snapshot", "done" if shot.outcome == "ok" else "failed",
                 measurements={"outcome": shot.outcome, "message": shot.message})
        frame = await _call(console.microscope_frame, station.microscope_url)
    except (ContractError, ReactorPolicyError) as exc:
        run.end("failed", error={"message": str(exc)})
        return ToolResult(status="error", summary=f"snapshot failed: {exc}", data={"run_id": run.id})
    folder = (station.image_dir or Path("runs/images")) / "reactor"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{args.chip_id.replace(':', '_')}-{time.strftime('%Y%m%d-%H%M%S')}-{run.id[-6:]}.jpg"
    path.write_bytes(frame)
    run.step("frame", measurements={"path": str(path), "bytes": len(frame)})
    run.end("succeeded", result={"image": str(path)})
    return ToolResult(status="ok", summary=f"microscope picture saved ({len(frame)} bytes)",
                      data={"run_id": run.id, "image": str(path)}, images=[str(path)])


@qumus_tool("reactor_record", "Record the microscope for some seconds (reactor recipe "
            "microscope-record). Moves and heats nothing.", agents={"reactor"}, kind="hardware")
async def reactor_record(args: RecordArgs, ctx: ToolContext) -> ToolResult:
    station = station_of(ctx)
    if station is None:
        return _no_station()
    if reasons := await _call(readiness, station.console, ACTION_DEVICES["record"]):
        return _not_ready("record", reasons)
    run = _Run(ctx, "record", args.model_dump())
    try:
        result = await _call(station.console.run_recipe, "microscope-record",
                             {"chip_id": args.chip_id, "seconds": args.seconds, "mode": args.mode},
                             timeout_s=args.seconds + 600)
    except ReactorPolicyError as exc:
        run.end("cancelled", error={"message": str(exc)})
        return _denied(exc)
    _recipe_result(run, result)
    run.end("succeeded" if result.ok else "failed", result=result.to_dict() if result.ok else None,
            error=None if result.ok else result.to_dict())
    return ToolResult(status="ok" if result.ok else "error",
                      summary=f"microscope-record {result.outcome} {result.message}".strip(),
                      data={"run_id": run.id, "recipe": result.to_dict()})


@qumus_tool("reactor_stop", "Stop the reactor now: console motion stop (also holds a recipe "
            "run), stop of robot, stage and microscope, and furnace stop (heat and gas off). "
            "Needs no control; each stop is sent once.", agents={"reactor", "orchestrator"},
            kind="hardware")
async def reactor_stop(args: NoArgs, ctx: ToolContext) -> ToolResult:
    station = station_of(ctx)
    if station is None:
        return _no_station()
    run = _Run(ctx, "stop", {})
    results = await _call(station.console.stop_all)
    run.step("stop_all", measurements=results)
    bad = {k: v for k, v in results.items() if v not in ("confirmed", "pending", "accepted")}
    run.end("succeeded" if not bad else "failed", result=results)
    return ToolResult(status="ok" if not bad else "needs_attention",
                      summary="reactor stopped" if not bad else f"stop not confirmed for {bad}",
                      data={"run_id": run.id, "results": results})


REACTOR_TOOLS = ("reactor_status", "reactor_handover_in", "reactor_load", "reactor_heat",
                 "reactor_snapshot", "reactor_record", "reactor_unload", "reactor_handover_out",
                 "reactor_stop")
