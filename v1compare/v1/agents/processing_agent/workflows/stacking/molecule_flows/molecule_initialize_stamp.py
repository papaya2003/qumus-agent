from __future__ import annotations

import time

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomCameraAutofocusOnce,
    AtomCameraStart,
    AtomCameraStop,
    AtomFocusMoveRelative,
    AtomTransferConnect,
    AtomTransferMoveAxisTo,
    AtomTransferSetKSTSpeed,
    AtomTurretRotate,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeInitializeStamp(BaseWorkflow):
    name = "molecule_initialize_stamp"
    description = "Initialize and set up the transfer stamp position and tune focus control tofocus on the target. Be readty to pick up the flake after this initialization."
    inputs = {}
    output_type = "string"

    X_MAX_VEL = 0.2
    Y_MAX_VEL = 0.2
    Z_MAX_VEL = 0.1
    XY_ACCEL = 0.5
    Z_ACCEL = 0.5
    L_TARGET = 120.0
    Z_TARGET = 5.4
    FOCUS_DELTA_UM = 25000.0
    FOCUS_SETTLE_S = 90.0
    AUTOFOCUS_WAIT_S = 3.0
    OPEN_LIVE_VIEW = True

    PLAN_STEPS = [
        "Rotate Turret To Position 1",
        "Connect Transfer Stage",
        "Configure Transfer Speeds",
        "Move Stamp To Initialization Position",
        "Adjust Focus",
        "Stop Camera",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transfer_connect = AtomTransferConnect(*args, **kwargs)
        self.transfer_set_kst_speed = AtomTransferSetKSTSpeed(*args, **kwargs)
        self.transfer_move_axis_to = AtomTransferMoveAxisTo(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.cam_autofocus_once = AtomCameraAutofocusOnce(*args, **kwargs)
        self.focus_move_relative = AtomFocusMoveRelative(*args, **kwargs)
        self.turret_rotate = AtomTurretRotate(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    def forward(self) -> str:
        if self.transfer is None or self.camera is None or self.focus is None:
            return "❌ Required hardware (transfer/camera/focus) unavailable."

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []

        self.broadcaster_step.forward(0)
        results.append(self.turret_rotate(position=1))

        self.broadcaster_step.forward(1)
        results.append(self.transfer_connect())

        self.broadcaster_step.forward(2)
        results.append(self.transfer_set_kst_speed(axis="x", max_vel=self.X_MAX_VEL, accel=self.XY_ACCEL))
        results.append(self.transfer_set_kst_speed(axis="y", max_vel=self.Y_MAX_VEL, accel=self.XY_ACCEL))
        results.append(self.transfer_set_kst_speed(axis="z", max_vel=self.Z_MAX_VEL, accel=self.Z_ACCEL))

        self.broadcaster_step.forward(3)
        results.append(self.cam_start(open_live_view=self.OPEN_LIVE_VIEW))
        results.append(self.transfer_move_axis_to(axis="L", value=self.L_TARGET))
        results.append(self.transfer_move_axis_to(axis="z", value=self.Z_TARGET))

        self.broadcaster_step.forward(4)
        results.append(self.focus_move_relative(delta=self.FOCUS_DELTA_UM, wait=True))
        # time.sleep(float(self.FOCUS_SETTLE_S))

        self.broadcaster_step.forward(5)
        results.append(self.cam_stop())

        return "\n".join(results)
