from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class AtomExfoliationHeaterRelayOn(BaseWorkflow):
    name = "atom_exfoliation_heater_relay_on"
    description = "Turn on the exfoliation heater relay."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if self.exfoliation_heater_relay is None:
            raise RuntimeError("Exfoliation heater relay is not configured.")
        self.exfoliation_heater_relay.on()
        return "✅ Exfoliation heater relay ON."


class AtomExfoliationHeaterRelayOff(BaseWorkflow):
    name = "atom_exfoliation_heater_relay_off"
    description = "Turn off the exfoliation heater relay."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if self.exfoliation_heater_relay is None:
            raise RuntimeError("Exfoliation heater relay is not configured.")
        self.exfoliation_heater_relay.off()
        return "✅ Exfoliation heater relay OFF."
