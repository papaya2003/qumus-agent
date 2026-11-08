from agents.processing_agent.workflows.base_workflow import BaseWorkflow

class AtomVacuumOn(BaseWorkflow):
    name = "atom_vacuum_on"
    description = "Turn on the vacuum (open solenoid valve) to hold items on the exfoliation stage."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.vacuum_relay.on()
        return "✅ Vacuum ON (Item held)."

class AtomVacuumOff(BaseWorkflow):
    name = "atom_vacuum_off"
    description = "Turn off the vacuum (close solenoid valve) to release items on the exfoliation stage."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.vacuum_relay.off()
        return "✅ Vacuum OFF (Item released)."
