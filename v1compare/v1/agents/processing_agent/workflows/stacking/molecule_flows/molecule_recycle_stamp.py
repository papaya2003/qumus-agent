from __future__ import annotations

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomRightArmHome,
    AtomRightArmMoveTo,
    AtomRightArmPickUpItem,
    AtomRightArmPlaceItem,
    AtomTransferArmVacuumOff,
    AtomTransferConnect,
    AtomTransferMoveAxisTo,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeRecycleStamp(BaseWorkflow):
    name = "molecule_recycle_stamp"
    description = "Recycle the stamp by picking it from the transfer arm, releasing transfer arm vacuum, placing it at the recycle pose, and homing the right arm."
    inputs = {}
    output_type = "string"

    TRANSFER_L_TARGET = 0.0
    TRANSFER_X_TARGET = 6.0
    TRANSFER_Y_TARGET = 7.0
    TRANSFER_Z_TARGET = 6.5
    PICK_X = 42.0
    PICK_Y = 276.0
    SAFE_Z = 150.0
    PICK_Z = 134.0
    RECYCLE_X = -250.0
    RECYCLE_Y = 250.0
    RECYCLE_SAFE_Z = 100.0
    RECYCLE_PLACE_Z = 15.0

    PLAN_STEPS = [
        "Move Transfer Arm To Stamp Handoff Pose",
        "Move Right Arm To Stamp Pickup Pose",
        "Lower And Pick Up Stamp",
        "Release Transfer Arm Vacuum",
        "Move To Recycle Pose",
        "Place Stamp",
        "Home Right Arm",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transfer_connect = AtomTransferConnect(*args, **kwargs)
        self.transfer_move_axis_to = AtomTransferMoveAxisTo(*args, **kwargs)
        self.right_arm_move_to = AtomRightArmMoveTo(*args, **kwargs)
        self.right_arm_pick_up_item = AtomRightArmPickUpItem(*args, **kwargs)
        self.right_arm_place_item = AtomRightArmPlaceItem(*args, **kwargs)
        self.right_arm_home = AtomRightArmHome(*args, **kwargs)
        self.transfer_arm_vacuum_off = AtomTransferArmVacuumOff(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    def forward(self) -> str:
        if self.arm is None:
            return "❌ Right robot arm controller unavailable."
        if self.transfer is None:
            return "❌ Transfer controller unavailable."
        if self.transfer_arm_vacuum_relay is None:
            return "❌ Transfer arm vacuum relay unavailable."

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []

        self.broadcaster_step.forward(0)
        results.append(self.transfer_connect())
        results.append(self.transfer_move_axis_to(axis="L", value=self.TRANSFER_L_TARGET))
        results.append(self.transfer_move_axis_to(axis="x", value=self.TRANSFER_X_TARGET))
        results.append(self.transfer_move_axis_to(axis="y", value=self.TRANSFER_Y_TARGET))
        results.append(self.transfer_move_axis_to(axis="z", value=self.TRANSFER_Z_TARGET))

        self.broadcaster_step.forward(1)
        results.append(self.right_arm_move_to(x=self.PICK_X, y=self.PICK_Y, z=self.SAFE_Z))

        self.broadcaster_step.forward(2)
        results.append(self.right_arm_move_to(x=self.PICK_X, y=self.PICK_Y, z=self.PICK_Z))
        results.append(self.right_arm_pick_up_item())

        self.broadcaster_step.forward(3)
        results.append(self.transfer_arm_vacuum_off())
        results.append(self.right_arm_move_to(x=self.PICK_X, y=self.PICK_Y, z=self.SAFE_Z))

        self.broadcaster_step.forward(4)
        results.append(self.right_arm_move_to(x=self.RECYCLE_X, y=self.RECYCLE_Y, z=self.RECYCLE_SAFE_Z))
        results.append(self.right_arm_move_to(x=self.RECYCLE_X, y=self.RECYCLE_Y, z=self.RECYCLE_PLACE_Z))

        self.broadcaster_step.forward(5)
        results.append(self.right_arm_place_item())
        results.append(self.right_arm_move_to(x=self.RECYCLE_X, y=self.RECYCLE_Y, z=self.RECYCLE_SAFE_Z))

        self.broadcaster_step.forward(6)
        results.append(self.right_arm_home())

        return "\n".join(results)
