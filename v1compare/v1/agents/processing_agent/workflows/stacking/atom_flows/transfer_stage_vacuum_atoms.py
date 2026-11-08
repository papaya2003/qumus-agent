from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class AtomTransferStageVacuumOn(BaseWorkflow):
    name = "atom_transfer_stage_vacuum_on"
    description = "Turn on the transfer stage vacuum (open solenoid valve) to hold items on the transfer stage."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.vacuum_relay.on()
        return "✅ Transfer stage vacuum ON (Item held)."


class AtomTransferStageVacuumOff(BaseWorkflow):
    name = "atom_transfer_stage_vacuum_off"
    description = "Turn off the transfer stage vacuum (close solenoid valve) to release items on the transfer stage."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.vacuum_relay.off()
        return "✅ Transfer stage vacuum OFF (Item released)."
