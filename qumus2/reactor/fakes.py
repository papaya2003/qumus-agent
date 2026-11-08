"""Doc-faithful fakes of the reactor services, for offline adapter tests.

Command names, parameters, ports, base paths and header rules are taken from
the reactor repo's public docs (docs/development.md, furnace.md, microscope.md,
thermal.md, vision.md, orchestrator.md; ADR 0001). No reactor code is used.
These fakes simulate nothing physical; they only answer like the services do.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager

from qumus2.contract.fake_server import FakeCommand, FakeService

H = "X-MG400-Client"


def obj(required=(), **props):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


INT = {"type": "integer"}
NUM = {"type": "number"}
STR = {"type": "string"}
BOOL = {"type": "boolean"}


def _cmds(**specs):
    return {name: spec if isinstance(spec, FakeCommand) else FakeCommand(params=spec)
            for name, spec in specs.items()}


def reactor_fakes() -> dict[str, FakeService]:
    common = {"client_header": H, "advertise_client_header": False, "demo": True}
    return {
        "robot": FakeService(
            "robot", required_headers={"X-MG400-Panel": "1"}, reply_style="200-state",
            renew_route=False, heartbeat_acquires=False,
            on_lease_expiry="stop motion and lock control",
            device_fields={"mode": 4, "vacuum": {"suck": False, "blow": False, "held": None}},
            commands=_cmds(
                start_sync=obj(), stop_sync=obj(), enable=obj(),
                disable=FakeCommand(any_client=True), stop=FakeCommand(any_client=True),
                home=obj(),
                execute=obj(["targets"], targets={"type": "object"}, speed=INT),
                grip=obj(["position"], position={"type": "integer", "minimum": 0, "maximum": 1000}),
                initialize_gripper=obj(),
                vacuum=obj(["operation"], operation={"type": "string",
                                                     "enum": ["suck", "release", "off"]}),
                keyboard=FakeCommand(params={"type": "object"}, ui_only=True),
                clear_alarm=obj(),
            ), **common),
        "stage": FakeService(
            "stage", base_path="/contract/", on_lease_expiry="stop_all",
            device_fields={"motors": {"bottom": {"moving": False}, "top": {"moving": False},
                                      "insert": {"moving": False}}},
            commands=_cmds(
                move=obj(["motor", "direction", "speed", "steps"], motor=STR, direction=STR,
                         speed=INT, steps=INT),
                hold=obj(["motor", "direction", "speed", "hold_id", "sampled_at"], motor=STR,
                         direction=STR, speed=INT, hold_id=STR, sampled_at=NUM),
                keepalive=obj(["motor", "hold_id"], motor=STR, hold_id=STR),
                release_hold=obj(["motor", "hold_id"], motor=STR, hold_id=STR),
                stop_all=FakeCommand(any_client=True),
                set_microstep=obj(["microstep"], microstep={"type": "integer",
                                                            "enum": [1, 2, 4, 8, 16]}),
            ), **common),
        "thermal": FakeService(
            "thermal", on_lease_expiry="continue_acquisition",
            device_fields={"camera": "streaming", "frame_sequence": 12, "frame_age_s": 0.1},
            commands=_cmds(connect=FakeCommand(any_client=True),
                           shutter=FakeCommand(any_client=True)), **common),
        "furnace": FakeService(
            "furnace", on_lease_expiry="sequence continues; open setpoint -> 15 min orphan grace",
            device_fields={"furnace": {"pv_c": 23.0, "sv_c": None, "mode": "STOP"},
                           "gas": {"0": {"flow_pct": 0.0}}},
            commands=_cmds(
                connect=obj(), connect_flow=obj(),
                apply_setpoint=obj(["target_c", "ramp_c_per_min"], target_c=NUM,
                                   ramp_c_per_min=NUM),
                set_sequence=obj(["steps"], steps={"type": "array"}),
                start_sequence=obj(["steps"], steps={"type": "array"}),
                set_gas_flow=obj(["channel_index", "flow_pct"], channel_index=INT, flow_pct=NUM),
                set_gas_valve=obj(["channel_index", "open"], channel_index=INT, open=BOOL),
                inspect_gas_flow=obj(),
            ), **common),
        "microscope": FakeService(
            "microscope",
            on_lease_expiry="Cancel work, STOP ALL, disable both motors, and lose focus zero.",
            device_fields={"focus_steps": 0, "objective_id": 1},
            commands=_cmds(
                select_objective=obj(["id"], id=INT),
                objective_nudge=obj(["steps"], steps=INT),
                autofocus=obj(["mode"], mode={"type": "string", "enum": ["full", "local"]}),
                focus_move=obj(["steps"], steps=INT),
                home_focus=obj(),
                snapshot=obj([], path={"type": "string", "minLength": 1}),
                start_recording=obj(["recording_id"], recording_id=STR),
                stop_recording=obj(["recording_id"], recording_id=STR),
                record_with_sequence=obj(["recording_id", "enabled"], recording_id=STR,
                                         enabled=BOOL),
                set_stabilization=obj(["enabled"], enabled=BOOL),
            ), **common),
        "vision": FakeService(
            "vision", on_lease_expiry="cancel the controlling client's inference",
            device_fields={"model": "demo", "device": "cpu"},
            commands=_cmds(
                detect=obj(["camera"], camera=INT),
                read_labels=obj(["camera"], camera=INT, region={"type": "array"}),
                read_markers=obj(["camera"], camera=INT),
                calibrate_camera=obj(["camera"], camera=INT),
                check_calibration=obj(["camera"], camera=INT, limit_px=NUM),
                observe_slots=obj(["camera"], camera=INT, slots={"type": "array"}),
            ), **common),
        "orchestrator": FakeService(
            "orchestrator", on_lease_expiry="Hold the active run and stop motion",
            commands={},
            extra_get={"/api/recipes": {"recipes": [{"id": "example-rehearsal",
                                                     "name": "Example", "params": {}}]},
                       "/api/runs": {"runs": []}},
            **common),
    }


@contextmanager
def running_reactor_fakes():
    fakes = reactor_fakes()
    with ExitStack() as stack:
        for fake in fakes.values():
            stack.enter_context(fake)
        yield fakes
