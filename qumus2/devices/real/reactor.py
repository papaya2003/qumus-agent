"""Opt-in adapters for the reviewed WuLab controllers; no I/O on import.

An application must inject already connected, exclusively owned controllers,
calibration and independent position observations, then place these devices
behind SafetyGate. The normal application does not instantiate these adapters.
Pulse counters are controller evidence, never measured millimetres.
"""
from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from qumus2.core.clock import Clock
from qumus2.core.errors import DeviceUnavailable, NeedsHumanError, PreconditionFailed
from qumus2.core.types import Pose
from qumus2.devices.base import GasFlowController, Heater, MotionStage


def finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DeviceUnavailable(f"Missing or invalid {name}")
    return float(value)


@dataclass(frozen=True)
class PositionObservation:
    pose: Pose
    timestamp: float
    source: str  # encoder or calibrated_camera; pulse counter is not an observation


@dataclass(frozen=True)
class MotorCalibration:
    motor: str
    pulses_per_mm: float
    positive_direction: str
    zero_pulses: int
    microstep: int
    reference_id: str
    speed_pulses_s: int

    def __post_init__(self) -> None:
        if (self.motor not in {"bottom", "top", "insert"}
                or self.positive_direction not in {"cw", "ccw"}
                or not self.reference_id.strip()
                or self.microstep not in {1, 2, 4, 8, 16}
                or type(self.zero_pulses) is not int
                or type(self.speed_pulses_s) is not int
                or not 1 <= self.speed_pulses_s <= 1000
                or finite(self.pulses_per_mm, "pulses per mm") <= 0):
            raise ValueError("Explicit motor reference, direction, resolution and speed required")

    def pulses(self, mm: float) -> int:
        sign = 1 if self.positive_direction == "cw" else -1
        return self.zero_pulses + round(finite(mm, "position") * self.pulses_per_mm) * sign


class MotorTransport(Protocol):
    microstep: int | None

    def command(self, command: str) -> None: ...
    def status(self) -> Mapping[str, Any]: ...


class ReactorMotorBank:
    """Shared serial ownership for assembly and insertion device adapters.

    Compatible with Stage-Motor-Controller's Transport. Serial transport creation
    and hardware authorization belong to the operator's composition layer.
    No continuous jog, automatic retry, microstep change or inferred homing.
    """

    def __init__(self, transport: MotorTransport, clock: Clock,
                 calibrations: Mapping[str, MotorCalibration], *, timeout_s: float = 30) -> None:
        if set(calibrations) != {"bottom", "top", "insert"}:
            raise ValueError("All three motor calibrations are required")
        if any(k != c.motor for k, c in calibrations.items()):
            raise ValueError("Calibration motor identity mismatch")
        self.transport, self.clock = transport, clock
        self.calibrations = dict(calibrations)
        self.timeout_s = finite(timeout_s, "motion timeout")
        if self.timeout_s <= 0:
            raise ValueError("Motion timeout must be positive")
        self.lock = threading.RLock()
        self.uncertain = False
        self.ready_at = 0.0

    def status(self) -> Mapping[str, Any]:
        if self.uncertain:
            raise NeedsHumanError("Controller position is uncertain; inspect and re-reference before reuse")
        try:
            status = self.transport.status()
            if set(status) != set(self.calibrations):
                raise ValueError("Incomplete motor status")
            for motor, calibration in self.calibrations.items():
                s = status[motor]
                if (self.transport.microstep != calibration.microstep
                        or type(s.position) is not int or type(s.remaining) is not int
                        or s.mode not in {"idle", "moving", "continuous"}):
                    raise ValueError("Resolution changed or controller status is invalid")
            return status
        except Exception as exc:
            self.uncertain = True
            raise NeedsHumanError("Motor status unavailable; reference no longer trusted") from exc

    def fail_motion(self, exc: Exception) -> None:
        self.uncertain = True
        stopped = False
        try:
            self.transport.command("stop all")
            states = self.transport.status()
            stopped = (set(states) == set(self.calibrations)
                       and all(s.mode == "idle" and s.remaining == 0 for s in states.values()))
        except Exception:
            pass
        raise NeedsHumanError("Motion interrupted; inspect and re-reference. "
                             + ("Controller reports pulse generation stopped."
                                if stopped else "Stop could not be confirmed.")) from exc

    def move(self, targets_mm: Mapping[str, float]) -> None:
        with self.lock:
            states = self.status()
            if any(s.mode != "idle" or s.remaining for s in states.values()):
                raise PreconditionFailed("Controller is busy; no motion was queued")
            planned = []
            for motor, mm in targets_mm.items():
                c = self.calibrations[motor]
                target = c.pulses(mm)
                delta = target - states[motor].position
                if abs(delta) > 100_000:
                    raise PreconditionFailed("Move exceeds reviewed firmware transaction bound")
                if delta:
                    planned.append((motor, c, target, delta))
            try:
                for motor, c, target, delta in planned:
                    self.clock.sleep(max(0.0, self.ready_at - self.clock.now()))
                    for command in (f"motor {motor}", f"dir {'cw' if delta > 0 else 'ccw'}",
                                    f"speed {c.speed_pulses_s}", f"steps {abs(delta)}"):
                        self.transport.command(command)
                    deadline = self.clock.now() + self.timeout_s
                    while True:
                        state = self.status()[motor]
                        if state.mode == "idle":
                            if state.position != target or state.remaining != 0:
                                raise ValueError("Motion stopped before the requested pulse count")
                            break
                        if self.clock.now() >= deadline:
                            raise TimeoutError("Motion did not complete before its deadline")
                        self.clock.sleep(0.05)
                    self.ready_at = self.clock.now() + 1.0
            except Exception as exc:
                self.fail_motion(exc)


class ControllerReactorStage(MotionStage):
    """Existing MotionStage API, with separate assembly and insertion devices."""

    def __init__(self, bank: ReactorMotorBank,
                 observe: Callable[[str], PositionObservation], *, insertion: bool = False,
                 max_age_s: float = 2, tolerance_mm: float = 0.05) -> None:
        super().__init__("reactor_insert" if insertion else "reactor_slot")
        self.bank, self.observe, self.insertion = bank, observe, insertion
        self.max_age_s, self.tolerance_mm = max_age_s, tolerance_mm
        if not (math.isfinite(max_age_s) and max_age_s > 0
                and math.isfinite(tolerance_mm) and tolerance_mm > 0):
            raise ValueError("Freshness and tolerance must be positive and finite")

    def _observation(self, since: float | None = None) -> Pose:
        obs = self.observe(self.device_id)
        age = self.bank.clock.now() - finite(obs.timestamp, "position timestamp")
        if (obs.source not in {"encoder", "calibrated_camera"} or not 0 <= age <= self.max_age_s
                or (since is not None and obs.timestamp < since)):
            raise DeviceUnavailable("Fresh measured position required; pulse counts are not encoders")
        p = obs.pose
        for axis in ("x", "y", "z", "rotation"):
            finite(getattr(p, axis), "measured " + axis)
        if p.z != 0 or p.rotation != 0 or (self.insertion and p.x != 0):
            raise DeviceUnavailable("Position observation is in the wrong coordinate frame")
        return p.model_copy()

    def get_pose(self) -> Pose:
        with self.bank.lock:
            self.bank.status()
            return self._observation()

    def move_to(self, pose: Pose) -> None:
        if pose.z or pose.rotation or (self.insertion and pose.x):
            raise PreconditionFailed("Assembly supports XY; insertion supports only Y")
        axes = {"insert": "y"} if self.insertion else {"bottom": "x", "top": "y"}
        with self.bank.lock:
            current = self.get_pose()
            status = self.bank.status()
            for motor, axis in axes.items():
                c = self.bank.calibrations[motor]
                if abs(c.pulses(getattr(current, axis)) - status[motor].position) > \
                        self.tolerance_mm * c.pulses_per_mm + 1:
                    self.bank.uncertain = True
                    raise NeedsHumanError("Measured position disagrees with calibration; re-reference")
            self.bank.move({m: getattr(pose, axis) for m, axis in axes.items()})
            completed = self.bank.clock.now()
            try:
                measured = self._observation(since=completed)
                if math.hypot(measured.x - pose.x, measured.y - pose.y) > self.tolerance_mm:
                    raise ValueError("Measured endpoint does not match the requested position")
            except Exception as exc:
                self.bank.fail_motion(exc)

    def home(self) -> None:
        raise NeedsHumanError("No verified homing protocol: establish a calibrated reference explicitly")


class FurnaceServiceHeater(Heater):
    """FurnaceService adapter: process value and request are separate."""

    def __init__(self, service: Any, clock: Clock, *, ramp_c_per_min: float,
                 max_age_s: float = 3) -> None:
        super().__init__("reactor_heater")
        self.service, self.clock = service, clock
        self.ramp, self.max_age_s = ramp_c_per_min, max_age_s
        if finite(self.ramp, "ramp") <= 0 or finite(max_age_s, "freshness") <= 0:
            raise ValueError("Positive ramp and freshness required")

    def _status(self) -> dict[str, Any]:
        s = self.service.status()
        age = self.clock.now() - finite(s.get("sample_time"), "temperature timestamp")
        if not s.get("connected") or s.get("error") or not 0 <= age <= self.max_age_s:
            raise DeviceUnavailable("Furnace temperature feedback is unavailable or stale")
        return s

    def set_target(self, celsius: float) -> None:
        self._status()
        self.service.apply_setpoint(finite(celsius, "temperature target"), self.ramp)

    def get_target(self) -> float | None:
        s = self._status()
        return None if s.get("run_state_code") == 1 else finite(s.get("sv"), "temperature setpoint")

    def read_temperature(self) -> float:
        return finite(self._status().get("pv"), "temperature process value")

    def off(self) -> None:
        self.service.stop_furnace()


class FurnaceServiceGasFlow(GasFlowController):
    """One calibrated MFC channel. Write-only output never becomes measured flow."""

    def __init__(self, service: Any, *, channel: int, full_scale_sccm: float,
                 gas_identity: str) -> None:
        super().__init__("reactor_flow")
        if type(channel) is not int or channel not in (0, 1, 2) or not gas_identity.strip():
            raise ValueError("Explicit channel and gas identity required")
        if finite(full_scale_sccm, "MFC full scale") <= 0:
            raise ValueError("MFC full scale must be calibrated")
        self.service, self.channel = service, channel
        self.full_scale, self.gas_identity = full_scale_sccm, gas_identity

    def _channel(self, status: dict[str, Any]) -> dict[str, Any]:
        if not status.get("connected") or status.get("error"):
            raise DeviceUnavailable("MFC connection unavailable")
        matches = [c for c in status.get("gas_channels", []) if c.get("channel_index") == self.channel]
        if len(matches) != 1:
            raise DeviceUnavailable("MFC channel identity missing or ambiguous")
        return matches[0]

    def set_flow(self, sccm: float) -> None:
        percent = finite(sccm, "flow target") / self.full_scale * 100
        if not 0 <= percent <= 100:
            raise PreconditionFailed("Requested flow exceeds calibrated MFC full scale")
        if percent == 0:
            self.service.stop_gas_flow(self.channel)
        else:
            self.service.set_gas_flow(self.channel, percent)

    def get_target(self) -> float:
        c = self._channel(self.service.flow_status())
        return finite(c.get("target_percent"), "requested MFC percentage") * self.full_scale / 100

    def read_flow(self) -> float:
        # This performs a fresh, read-only hardware inspection. Never use the
        # service's cached zero after stop_gas_flow as a flow observation.
        c = self._channel(self.service.inspect_gas_flow(self.channel))
        if not c.get("available") or c.get("write_only") or c.get("error"):
            raise DeviceUnavailable("MFC has no confirmed measured flow")
        percent = finite(c.get("actual_percent"), "measured MFC percentage")
        if not 0 <= percent <= 100:
            raise DeviceUnavailable("Invalid measured MFC percentage")
        return percent * self.full_scale / 100

    def safe_state(self) -> None:
        self.service.stop_gas_flow(self.channel)
