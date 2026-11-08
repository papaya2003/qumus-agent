from __future__ import annotations

from common.services.processing_checkpoint_service import ProcessingCheckpointService
from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomRightArmPlaceStampOnTransferArm,
    AtomTransferArmVacuumOn,
    AtomTransferConnect,
    AtomTransferMoveAxisTo,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeLoadStamp(BaseWorkflow):
    name = "molecule_load_stamp"
    description = "Move the transfer arm to the loading pose, enable transfer arm vacuum, and place the stamp onto the transfer arm."
    inputs = {
        "checkpoint_path": {
            "type": "string",
            "description": "Parent processing checkpoint path.",
            "nullable": True,
            "default": None,
        },
        "parent_step_index": {
            "type": "integer",
            "description": "Parent workflow step index.",
            "nullable": True,
            "default": None,
        },
    }
    output_type = "string"

    L_TARGET = 0.0
    X_TARGET = 6.0
    Y_TARGET = 7.0
    Z_TARGET = 6.5

    PLAN_STEPS = [
        "Connect Transfer Stage And Move To Stamp Loading Pose",
        "Turn On Transfer Arm Vacuum",
        "Place Stamp On Transfer Arm",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transfer_connect = AtomTransferConnect(*args, **kwargs)
        self.transfer_move_axis_to = AtomTransferMoveAxisTo(*args, **kwargs)
        self.transfer_arm_vacuum_on = AtomTransferArmVacuumOn(*args, **kwargs)
        self.right_arm_place_stamp_on_transfer_arm = AtomRightArmPlaceStampOnTransferArm(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    def forward(
        self,
        checkpoint_path: str | None = None,
        parent_step_index: int | None = None,
    ) -> str:
        if self.transfer is None:
            return "❌ Transfer controller unavailable."
        if self.arm is None:
            return "❌ Right robot arm controller unavailable."
        if self.transfer_arm_vacuum_relay is None:
            return "❌ Transfer arm vacuum relay unavailable."

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []

        self.broadcaster_step.forward(0)
        results.append(self.transfer_connect())

        # Connection itself does not move hardware. Preserve a directly
        # retryable checkpoint until it succeeds; only the first axis move
        # changes the operation to physically in-progress.
        if checkpoint_path:
            ProcessingCheckpointService().update(
                checkpoint_path,
                parent_step_index=parent_step_index,
                operation="load_stamp_motion_in_progress",
                operation_state="in_progress",
                safe_to_resume=False,
                safety_reason="Transfer controller connected and load-stamp axis motion started.",
            )
        results.append(self.transfer_move_axis_to(axis="L", value=self.L_TARGET))
        results.append(self.transfer_move_axis_to(axis="x", value=self.X_TARGET))
        results.append(self.transfer_move_axis_to(axis="y", value=self.Y_TARGET))
        results.append(self.transfer_move_axis_to(axis="z", value=self.Z_TARGET))

        self.broadcaster_step.forward(1)
        results.append(self.transfer_arm_vacuum_on())

        self.broadcaster_step.forward(2)
        results.append(self.right_arm_place_stamp_on_transfer_arm())

        return "\n".join(results)
