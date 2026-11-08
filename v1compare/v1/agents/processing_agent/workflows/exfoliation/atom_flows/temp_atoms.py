from typing import Optional
from agents.processing_agent.workflows.base_workflow import BaseWorkflow

class AtomReadTemperature(BaseWorkflow):
    name = "atom_read_temperature"
    description = "Read the temperature of exfoliation stage."
    inputs = {
        "stage": {
            "type": "string",
            "description": "Stage sensor to read: 'exfoliation' or 'transfer'. Defaults to 'exfoliation'.",
            "default": "exfoliation",
            "nullable": True
        },
        "index": {
            "type": "string",
            "description": "Dracal sensor index (string).",
            "default": "0",
            "nullable": True
        }
    }
    output_type = "string"

    def forward(self, stage: Optional[str] = "exfoliation", index: Optional[str] = "0") -> str:
        if self.temp is None:
            raise RuntimeError("Temperature controller is not initialized.")
        stage_name = (stage or "exfoliation").lower()
        t = self.temp.read_temperature(index=index, stage=stage_name)
        return f"🌡️ Temperature (stage={stage_name}, index={index}): {t:.3f} °C"

class AtomWaitUntilReachTemperature(BaseWorkflow):
    name = "atom_wait_until_reach_temperature"
    description = "Monitor exfoliation stage temperature until it reaches/crosses the target, then stop immediately."
    inputs = {
        "stage": {
            "type": "string",
            "description": "Stage sensor to monitor: 'exfoliation' or 'transfer'. Defaults to 'exfoliation'.",
            "default": "exfoliation",
            "nullable": True
        },
        "target_T": {
            "type": "number",
            "description": "Target temperature in °C. If initial temp >= target or later crosses target, stop and return.",
            "nullable": True
        }
    }
    output_type = "string"

    def forward(self, target_T: Optional[float], stage: Optional[str] = "exfoliation") -> str:
        if target_T is None:
            raise ValueError("target_T is required.")
        if self.temp is None:
            raise RuntimeError("Temperature controller is not initialized.")
        stage_name = (stage or "exfoliation").lower()
        return self.temp.wait_until_reach_temperature(float(target_T), stage=stage_name)

class AtomWaitUntilDropTemperature(BaseWorkflow):
    name = "atom_wait_until_drop_temperature"
    description = "Monitor exfoliation stage temperature until it drops to/below the target, then stop immediately."
    inputs = {
        "stage": {
            "type": "string",
            "description": "Stage sensor to monitor: 'exfoliation' or 'transfer'. Defaults to 'exfoliation'.",
            "default": "exfoliation",
            "nullable": True
        },
        "target_T": {
            "type": "number",
            "description": "Target temperature in °C. If initial temp <= target or later drops to/below target, stop and return.",
            "nullable": True
        }
    }
    output_type = "string"

    def forward(self, target_T: Optional[float], stage: Optional[str] = "exfoliation") -> str:
        if target_T is None:
            raise ValueError("target_T is required.")
        if self.temp is None:
            raise RuntimeError("Temperature controller is not initialized.")
        stage_name = (stage or "exfoliation").lower()
        return self.temp.wait_until_drop_temperature(float(target_T), stage=stage_name)
