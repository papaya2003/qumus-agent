from __future__ import annotations

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomCameraStart,
    AtomCameraStop,
    AtomFocusMoveRelative,
    AtomTransferConnect,
    AtomTransferMoveAxisTo,
    AtomTransferSetKSTSpeed,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeRemoveStamp(BaseWorkflow):
    name = "molecule_remove_stamp"
    description = "Move the transfer stamp away from the transfer stage after flake pickup or drop-down."
    inputs = {}
    output_type = "string"

    Z_MAX_VEL = 0.05
    Z_ACCEL = 0.5
    Z_TARGET = 6.5
    L_TARGET = 160.0
    FOCUS_DELTA_UM = -25000.0
    OPEN_LIVE_VIEW = True

    PLAN_STEPS = [
        "Connect Transfer Stage And Start Camera",
        "Configure Z Speed",
        "Move Stamp Away",
        "Refocus And Stop Camera",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.focus_move_relative = AtomFocusMoveRelative(*args, **kwargs)
        self.transfer_connect = AtomTransferConnect(*args, **kwargs)
        self.transfer_set_kst_speed = AtomTransferSetKSTSpeed(*args, **kwargs)
        self.transfer_move_axis_to = AtomTransferMoveAxisTo(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    def forward(self) -> str:
        if self.transfer is None or self.camera is None or self.focus is None:
            return "❌ Required hardware (transfer/camera/focus) unavailable."

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []

        self.broadcaster_step.forward(0)
        results.append(self.transfer_connect())
        results.append(self.cam_start(open_live_view=self.OPEN_LIVE_VIEW))

        self.broadcaster_step.forward(1)
        results.append(self.transfer_set_kst_speed(axis="z", max_vel=self.Z_MAX_VEL, accel=self.Z_ACCEL))

        self.broadcaster_step.forward(2)
        results.append(self.transfer_move_axis_to(axis="z", value=self.Z_TARGET))
        results.append(self.transfer_move_axis_to(axis="L", value=self.L_TARGET))

        self.broadcaster_step.forward(3)
        results.append(self.focus_move_relative(delta=self.FOCUS_DELTA_UM, wait=True))
        results.append(self.cam_stop())

        return "\n".join(results)
