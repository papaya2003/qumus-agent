from __future__ import annotations

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.exfoliation.atom_flows import AtomExfoliationHeaterRelayOff
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomHeaterOn,
    AtomHeaterSet,
    AtomReadTemperature,
    AtomTransferHeaterRelayOn,
    AtomWaitUntilReachTemperature,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeSetInitialTemp(BaseWorkflow):
    name = "molecule_set_initial_temp"
    description = "Switch heater routing to transfer and bring the transfer stage to the initial temperature for better stacking."
    inputs = {}
    output_type = "string"

    START_VOLTAGE = 20.0
    TARGET_TEMP_C = 90.0
    HOLD_VOLTAGE = 10
    PLAN_STEPS = [
        "Switch Heater Relays",
        "Set Initial Heating Power",
        "Wait For Transfer Temperature",
        "Set Hold Power",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exfoliation_heater_relay_off = AtomExfoliationHeaterRelayOff(*args, **kwargs)
        self.transfer_heater_relay_on = AtomTransferHeaterRelayOn(*args, **kwargs)
        self.heater_set = AtomHeaterSet(*args, **kwargs)
        self.heater_on = AtomHeaterOn(*args, **kwargs)
        self.read_temp = AtomReadTemperature(*args, **kwargs)
        self.wait_until_reach_temp = AtomWaitUntilReachTemperature(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    def forward(self) -> str:
        if self.power is None or self.temp is None:
            return "❌ Required hardware (power/temp) unavailable."

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []

        self.broadcaster_step.forward(0)
        results.append(self.exfoliation_heater_relay_off())
        results.append(self.transfer_heater_relay_on())

        self.broadcaster_step.forward(1)
        results.append(self.heater_set(voltage=self.START_VOLTAGE))
        results.append(self.heater_on())
        results.append(self.read_temp(stage="transfer"))

        self.broadcaster_step.forward(2)
        results.append(self.wait_until_reach_temp(target_T=float(self.TARGET_TEMP_C), stage="transfer"))

        self.broadcaster_step.forward(3)
        results.append(self.heater_set(voltage=self.HOLD_VOLTAGE))
        results.append(self.read_temp(stage="transfer"))

        return "\n".join(results)
