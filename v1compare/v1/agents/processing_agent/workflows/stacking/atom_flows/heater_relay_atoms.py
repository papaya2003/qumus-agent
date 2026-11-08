from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class AtomTransferHeaterRelayOn(BaseWorkflow):
    name = "atom_transfer_heater_relay_on"
    description = "Turn on the transfer heater relay."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if self.transfer_heater_relay is None:
            raise RuntimeError("Transfer heater relay is not configured.")
        self.transfer_heater_relay.on()
        return "✅ Transfer heater relay ON."


class AtomTransferHeaterRelayOff(BaseWorkflow):
    name = "atom_transfer_heater_relay_off"
    description = "Turn off the transfer heater relay."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if self.transfer_heater_relay is None:
            raise RuntimeError("Transfer heater relay is not configured.")
        self.transfer_heater_relay.off()
        return "✅ Transfer heater relay OFF."
