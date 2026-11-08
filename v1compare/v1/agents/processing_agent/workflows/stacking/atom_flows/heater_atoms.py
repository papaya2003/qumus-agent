from typing import Optional
from agents.processing_agent.workflows.base_workflow import BaseWorkflow

class AtomHeaterOn(BaseWorkflow):
    name = "atom_heater_on"
    description = "Turn ON the heating system."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.power.turn_on()
        return "✅ Heater ON."

class AtomHeaterOff(BaseWorkflow):
    name = "atom_heater_off"
    description = "Turn OFF the heating system."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.power.turn_off()
        return "✅ Heater OFF."

class AtomHeaterSet(BaseWorkflow):
    name = "atom_heater_set_voltage"
    description = "Set the heater voltage (controls temperature). Fixed current 2.0A."
    inputs = {"voltage": {"type": "number", "description": "Voltage in Volts.", "nullable": True}}
    output_type = "string"

    def forward(self, voltage: Optional[float]) -> str:
        self.power.set_output(voltage)
        return f"✅ Heater voltage set to {voltage}V."
