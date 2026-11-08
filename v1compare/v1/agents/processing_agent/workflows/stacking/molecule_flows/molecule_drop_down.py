from __future__ import annotations

import time

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.exfoliation.atom_flows import AtomExfoliationHeaterRelayOff
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomCameraAutofocusOnce,
    AtomCameraStart,
    AtomCameraStop,
    AtomHeaterOff,
    AtomHeaterOn,
    AtomHeaterSet,
    AtomReadTemperature,
    AtomTransferConnect,
    AtomTransferHeaterRelayOn,
    AtomTransferHeaterRelayOff,
    AtomTransferMoveAxisBy,
    AtomTransferMoveAxisTo,
    AtomTransferSetKSTSpeed,
    AtomTurretRotate,
)
from agents.processing_agent.workflows.stacking.molecule_flows.utils import get_stacking_run_dirs
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeDropDown(BaseWorkflow):
    name = "molecule_drop_down"
    description = "Drop the picked-up flake onto the target and melt the PC film by raising the transfer-stage temperature."
    inputs = {
        "run_number": {
            "type": "integer",
            "description": "Stacking run number used to place outputs under images/stacking/run_<N>.",
            "default": 1,
            "nullable": True,
        },
        "sample_label": {
            "type": "string",
            "description": "Optional subfolder label inside the stacking run, e.g. chip_a or chip_b.",
            "nullable": True,
        },
        "melt_temp_c": {
            "type": "number",
            "description": "Transfer-stage temperature threshold that must be exceeded before lifting the stamp.",
            "default": 170.0,
            "nullable": True,
        },
    }
    output_type = "string"

    SAFE_DIST = 200
    DELTA_Z = 0.0
    TARGET_PT = (1920, 1080)
    START_VOLTAGE = 27.0
    HOLD_VOLTAGE = 16.0
    TEMP_CHECK_PERIOD_S = 10.0
    INITIAL_FOCUS_WAIT_S = 5.0
    DROP_DOWN_TEMP_C = 155.0
    MELT_LIFT_DELTA_Z = 5.0
    DROP_DOWN_Z = 4.9

    PLAN_STEPS = [
        "Initialize Hardware",
        "Heat Transfer Stage To Drop-Down Temperature",
        "Drop Down To Target And Continue Heating",
        "Lift Stamp After Melting",
        "Finalize",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transfer_connect = AtomTransferConnect(*args, **kwargs)
        self.transfer_set_kst_speed = AtomTransferSetKSTSpeed(*args, **kwargs)
        self.transfer_move_axis_by = AtomTransferMoveAxisBy(*args, **kwargs)
        self.transfer_move_axis_to = AtomTransferMoveAxisTo(*args, **kwargs)
        self.transfer_heater_relay_on = AtomTransferHeaterRelayOn(*args, **kwargs)
        self.transfer_heater_relay_off = AtomTransferHeaterRelayOff(*args, **kwargs)
        self.exfoliation_heater_relay_off = AtomExfoliationHeaterRelayOff(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.cam_autofocus_once = AtomCameraAutofocusOnce(*args, **kwargs)
        self.turret_rotate = AtomTurretRotate(*args, **kwargs)
        self.heater_set = AtomHeaterSet(*args, **kwargs)
        self.heater_on = AtomHeaterOn(*args, **kwargs)
        self.heater_off = AtomHeaterOff(*args, **kwargs)
        self.read_temp = AtomReadTemperature(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    def forward(self, run_number: int = 1, sample_label: str | None = None, melt_temp_c: float = 170.0) -> str:
        if self.transfer is None or self.camera is None or self.turret is None or self.power is None or self.temp is None:
            return "❌ Required hardware (transfer/camera/turret/power/temp) unavailable."

        run_number = int(run_number) if run_number is not None else 1
        melt_temp = float(melt_temp_c) if melt_temp_c is not None else 170.0

        run_dirs = get_stacking_run_dirs(run_number=run_number, sample_label=sample_label)
        run_dir = run_dirs["run_dir"]
        sample_dir = run_dirs["sample_dir"]

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []

        self.broadcaster_step.forward(0)
        results.append(self.transfer_connect())
        results.append(self.cam_start(open_live_view=True))
        results.append(self.transfer_set_kst_speed(axis="x", max_vel=0.2, accel=0.5))
        results.append(self.transfer_set_kst_speed(axis="y", max_vel=0.2, accel=0.5))
        results.append(self.transfer_set_kst_speed(axis="z", max_vel=0.02, accel=0.1))
        results.append(self.turret_rotate(position=1))
        results.append(self.cam_autofocus_once())
        time.sleep(float(self.INITIAL_FOCUS_WAIT_S))

        self.broadcaster_step.forward(1)
        results.append(self.exfoliation_heater_relay_off())
        results.append(self.transfer_heater_relay_on())
        results.append(self.heater_set(voltage=self.START_VOLTAGE))
        results.append(self.heater_on())

        while True:
            results.append(self.cam_autofocus_once())
            temp_value = float(self.temp.read_temperature(stage="transfer"))
            results.append(f"Transfer-stage temperature: {temp_value:.2f} C")
            if temp_value >= self.DROP_DOWN_TEMP_C:
                results.append("Reached drop-down temperature.")
                break
            time.sleep(float(self.TEMP_CHECK_PERIOD_S))

        self.broadcaster_step.forward(2)
        results.append(self.transfer_move_axis_to(axis="z", value=self.DROP_DOWN_Z))
        results.append("The flake has been dropped down onto the target.")

        while True:
            temp_value = float(self.temp.read_temperature(stage="transfer"))
            results.append(f"Transfer-stage temperature: {temp_value:.2f} C")
            if temp_value > melt_temp:
                results.append("Exceeded melting temperature.")
                break
            time.sleep(float(self.TEMP_CHECK_PERIOD_S))

        results.append(self.heater_set(voltage=self.HOLD_VOLTAGE))
        self.broadcaster_step.forward(3)
        results.append(self.transfer_set_kst_speed(axis="z", max_vel=0.1, accel=1.0))
        results.append(self.transfer_move_axis_by(axis="z", delta=self.MELT_LIFT_DELTA_Z))

        self.broadcaster_step.forward(4)
        results.append(self.heater_off())
        results.append(self.transfer_heater_relay_off())
        results.append(self.cam_stop())
        results.append(f"Stacking sample directory: {sample_dir}")
        results.append(f"Stacking run directory: {run_dir}")
        results.append("Finish melting the PC film.")
        return "\n".join(results)
