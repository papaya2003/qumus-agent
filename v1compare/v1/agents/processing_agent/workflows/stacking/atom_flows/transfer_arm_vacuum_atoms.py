from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class AtomTransferArmVacuumOn(BaseWorkflow):
    name = "atom_transfer_arm_vacuum_on"
    description = "Turn on the transfer arm vacuum to hold items on the transfer arm."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if self.transfer_arm_vacuum_relay is None:
            return "❌ Transfer arm vacuum relay unavailable."
        self.transfer_arm_vacuum_relay.on()
        return "✅ Transfer arm vacuum ON (Item held)."


class AtomTransferArmVacuumOff(BaseWorkflow):
    name = "atom_transfer_arm_vacuum_off"
    description = "Turn off the transfer arm vacuum to release items on the transfer arm."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if self.transfer_arm_vacuum_relay is None:
            return "❌ Transfer arm vacuum relay unavailable."
        self.transfer_arm_vacuum_relay.off()
        return "✅ Transfer arm vacuum OFF (Item released)."
