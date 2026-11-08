from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class AtomExfoliationFanOn(BaseWorkflow):
    name = "atom_exfoliation_fan_on"
    description = "Turn on the exfoliation fan."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if self.fan_relay is None:
            raise RuntimeError("Fan relay is not configured for AtomExfoliationFanOn.")
        self.fan_relay.on()
        return "✅ Exfoliation fan ON."


class AtomExfoliationFanOff(BaseWorkflow):
    name = "atom_exfoliation_fan_off"
    description = "Turn off the exfoliation fan."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if self.fan_relay is None:
            raise RuntimeError("Fan relay is not configured for AtomExfoliationFanOff.")
        self.fan_relay.off()
        return "✅ Exfoliation fan OFF."
