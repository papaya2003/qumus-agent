from __future__ import annotations

import os

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.stacking.assembly_flows.assembly_quantum_hall_device import (
    AssemblyQuantumHallDevice,
)
from common.services.processing_checkpoint_service import ProcessingCheckpointService


class ResumeProcessingCheckpoint(BaseWorkflow):
    """Resume one session-bound physical workflow without replaying prior steps."""

    name = "resume_processing_checkpoint"
    flow_type = "assembly"
    camera_route = "right"
    description = (
        "Resume an interrupted physical processing workflow from its durable checkpoint. "
        "Use only when the task provides a processing_resume_checkpoint.json path."
    )
    inputs = {
        "checkpoint_path": {
            "type": "string",
            "description": "Absolute path to processing_resume_checkpoint.json.",
        }
    }
    output_type = "string"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.assembly_quantum_hall_device = AssemblyQuantumHallDevice(*args, **kwargs)
        self.checkpoints = ProcessingCheckpointService()

    def forward(self, checkpoint_path: str) -> str:
        checkpoint = self.checkpoints.validate_for_resume(
            checkpoint_path,
            workflow_name=AssemblyQuantumHallDevice.name,
            current_session_key=str(os.getenv("QUMUS_UI_SESSION_ID") or ""),
            current_project_id=str(os.getenv("QUMUS_PROJECT_ID") or ""),
        )
        payload = dict(checkpoint["payload"])

        return self.assembly_quantum_hall_device.forward(
            **payload,
            resume_run_number=int(checkpoint["run_number"]),
            resume_from_step=int(checkpoint.get("next_step_index", 0)),
            resume_checkpoint_path=str(checkpoint_path),
        )
