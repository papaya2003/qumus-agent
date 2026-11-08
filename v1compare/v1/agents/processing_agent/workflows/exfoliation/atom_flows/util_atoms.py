import time
from typing import Optional
from agents.processing_agent.workflows.base_workflow import BaseWorkflow

class AtomWait(BaseWorkflow):
    name = "atom_wait"
    description = "Wait for a specified duration in seconds."
    inputs = {"seconds": {"type": "number", "description": "Duration to wait.", "nullable": True}}
    output_type = "string"

    def forward(self, seconds: Optional[float]) -> str:
        time.sleep(seconds)
        return f"✅ Waited for {seconds} seconds."
