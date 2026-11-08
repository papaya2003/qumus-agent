"""Controller compatibility and uncertainty tests without hardware connections."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from qumus2.core.clock import SimClock
from qumus2.core.errors import DeviceUnavailable, NeedsHumanError, PreconditionFailed
from qumus2.core.types import Pose
from qumus2.devices.real.reactor import (
    ControllerReactorStage,
    FurnaceServiceGasFlow,
    FurnaceServiceHeater,
    MotorCalibration,
    PositionObservation,
    ReactorMotorBank,
)


class Transport:
    """Firmware reply fixture, including lost ACK and incomplete motion."""
    microstep = 16

    def __init__(self):
        self.states = {k: SimpleNamespace(position=0, remaining=0, mode="idle")
                       for k in ("bottom", "top", "insert")}
        self.commands = []
        self.selected, self.sign = "bottom", 1
        self.bad_ack = self.stalled = False

    def status(self):
        return self.states

    def command(self, command):
        self.commands.append(command)
        if command == "stop all":
            for s in self.states.values():
                s.mode, s.remaining = "idle", 0
            return
        verb, arg = command.split()
        if verb == "motor":
            self.selected = arg
        elif verb == "dir":
            self.sign = 1 if arg == "cw" else -1
        elif verb == "steps":
            s = self.states[self.selected]
            if self.stalled:
                s.mode, s.remaining = "moving", int(arg)
            else:
                s.position += int(arg) * self.sign
            if self.bad_ack:
                raise RuntimeError("ACK lost after motor accepted command")


def motor_fixture():
    transport, clock = Transport(), SimClock()
    calibrations = {m: MotorCalibration(m, 100, "cw", 0, 16, "test-reference", 200)
                    for m in transport.states}
    bank = ReactorMotorBank(transport, clock, calibrations, timeout_s=0.2)
    def observe(device):
        # Test stand-in for a calibrated external observer; never supplied by
        # the production adapter or derived internally from pulse counts.
        p = transport.states
        pose = Pose(x=p["bottom"].position / 100, y=p["top"].position / 100)
        if device == "reactor_insert":
            pose = Pose(x=0, y=p["insert"].position / 100)
        return PositionObservation(pose, clock.now(), "calibrated_camera")
    return bank, transport, clock, observe


def test_three_channels_share_transactions_and_keep_separate_frames():
    bank, t, clock, observer = motor_fixture()
    stage = ControllerReactorStage(bank, observer)
    insert = ControllerReactorStage(bank, observer, insertion=True)
    stage.move_to(Pose(x=1, y=-0.5))
    insert.move_to(Pose(x=0, y=2))
    assert stage.get_pose() == Pose(x=1, y=-0.5)
    assert insert.get_pose() == Pose(x=0, y=2)
    assert t.commands == ["motor bottom", "dir cw", "speed 200", "steps 100",
                          "motor top", "dir ccw", "speed 200", "steps 50",
                          "motor insert", "dir cw", "speed 200", "steps 200"]
    assert clock.now() >= 1_800_000_002  # firmware/controller cooldown retained


def test_calibrated_sign_and_offset_are_applied_without_changing_microstep():
    bank, t, _, observer = motor_fixture()
    bank.calibrations["bottom"] = replace(bank.calibrations["bottom"],
                                          positive_direction="ccw", zero_pulses=500)
    t.states["bottom"].position = 500
    bank.move({"bottom": 1.5})
    assert t.states["bottom"].position == 350
    assert not any(c.startswith("microstep") for c in t.commands)


@pytest.mark.parametrize("change", ["resolution", "busy", "missing_channel", "position_mismatch"])
def test_invalid_or_busy_reference_never_starts_motion(change):
    bank, t, clock, observer = motor_fixture()
    if change == "resolution":
        t.microstep = 1
    elif change == "busy":
        t.states["top"].mode = "moving"
    elif change == "missing_channel":
        t.states.pop("insert")
    else:
        def observer(_):
            return PositionObservation(Pose(x=2, y=0), clock.now(), "encoder")
    stage = ControllerReactorStage(bank, observer)
    with pytest.raises((NeedsHumanError, PreconditionFailed)):
        stage.move_to(Pose(x=1, y=0))
    assert t.commands == []


@pytest.mark.parametrize("failure", ["ack", "timeout", "endpoint"])
def test_uncertain_motion_stops_and_never_retries(failure):
    bank, t, clock, observer = motor_fixture()
    t.bad_ack, t.stalled = failure == "ack", failure == "timeout"
    if failure == "endpoint":
        def observer(_):
            return PositionObservation(Pose(x=0, y=0), clock.now(), "encoder")
    stage = ControllerReactorStage(bank, observer)
    with pytest.raises(NeedsHumanError):
        stage.move_to(Pose(x=1, y=0))
    assert t.commands.count("steps 100") == 1
    assert t.commands[-1] == "stop all"
    with pytest.raises(NeedsHumanError):
        stage.move_to(Pose(x=1, y=0))
    assert t.commands.count("steps 100") == 1


@pytest.mark.parametrize("source, age", [("commanded_pulses", 0), ("encoder", 3), ("encoder", -1)])
def test_position_requires_fresh_independent_observation(source, age):
    bank, t, clock, _ = motor_fixture()
    stage = ControllerReactorStage(bank, lambda _: PositionObservation(
        Pose(x=0, y=0), clock.now() - age, source))
    with pytest.raises(DeviceUnavailable):
        stage.move_to(Pose(x=1, y=0))
    assert t.commands == []


def test_home_is_never_assumed_and_insertion_cannot_move_x():
    bank, t, _, observer = motor_fixture()
    stage = ControllerReactorStage(bank, observer, insertion=True)
    with pytest.raises(NeedsHumanError):
        stage.home()
    with pytest.raises(PreconditionFailed):
        stage.move_to(Pose(x=1, y=1))
    assert t.commands == []


class Furnace:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.snapshot = dict(connected=True, error=None, sample_time=clock.now(),
                             pv=22, sv=800, run_state_code=0)
        self.channel = dict(channel_index=1, available=True, write_only=False,
                            error=None, target_percent=40, actual_percent=5)

    def status(self):
        return self.snapshot

    def apply_setpoint(self, temperature, ramp):
        self.calls.append(("heat", temperature, ramp))

    def stop_furnace(self):
        self.calls.append(("heat_off",))

    def flow_status(self):
        return dict(connected=True, error=None, gas_channels=[self.channel])

    def inspect_gas_flow(self, channel):
        self.calls.append(("inspect", channel))
        return self.flow_status()

    def set_gas_flow(self, channel, percent):
        self.calls.append(("flow", channel, percent))

    def stop_gas_flow(self, channel):
        self.calls.append(("flow_off", channel))
        self.channel["actual_percent"] = 0  # upstream sets this without reading hardware
        self.channel["available"] = False


def test_furnace_keeps_pv_sv_and_stop_independent():
    clock = SimClock()
    source = Furnace(clock)
    h = FurnaceServiceHeater(source, clock, ramp_c_per_min=5)
    h.set_target(810)
    assert h.get_target() == 800 and h.read_temperature() == 22
    assert source.calls == [("heat", 810, 5)]
    clock.sleep(4)
    with pytest.raises(DeviceUnavailable):
        h.read_temperature()
    h.off()  # stale feedback must not prevent sending STOP
    assert source.calls[-1] == ("heat_off",)


def test_gas_conversion_is_explicit_and_stop_does_not_invent_flow():
    source = Furnace(SimClock())
    gas = FurnaceServiceGasFlow(source, channel=1, full_scale_sccm=500, gas_identity="test gas")
    gas.set_flow(200)
    assert source.calls[-1] == ("flow", 1, 40)
    assert gas.get_target() == 200
    assert gas.read_flow() == 25
    gas.set_flow(0)
    with pytest.raises(DeviceUnavailable):
        gas.read_flow()
    assert source.calls[-1] == ("inspect", 1)
    with pytest.raises(PreconditionFailed):
        gas.set_flow(501)


@pytest.mark.parametrize("field,value", [("write_only", True), ("available", False),
                                         ("actual_percent", None), ("actual_percent", float("nan")),
                                         ("error", "MFC timeout")])
def test_unconfirmed_flow_is_unavailable(field, value):
    source = Furnace(SimClock())
    source.channel[field] = value
    gas = FurnaceServiceGasFlow(source, channel=1, full_scale_sccm=500, gas_identity="test gas")
    with pytest.raises(DeviceUnavailable):
        gas.read_flow()
