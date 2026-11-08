from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.exfoliation.atom_flows import (
    AtomLeftArmHome, AtomExfoliatorPowerOff, AtomHeaterOff, AtomVacuumOff,
    AtomExfoliationHeaterRelayOff,
)

class MoleculeResetSystem(BaseWorkflow):
    name = "molecule_reset_system"
    description = "Reset system to safe idle state: Heater OFF, Vacuum OFF, Printer OFF, Arm Home."
    inputs = {}
    output_type = "string"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.atom_heater_off = AtomHeaterOff(*args, **kwargs)
        self.atom_exfoliation_heater_relay_off = AtomExfoliationHeaterRelayOff(*args, **kwargs)
        self.atom_vacuum_off = AtomVacuumOff(*args, **kwargs)
        self.atom_printer_off = AtomExfoliatorPowerOff(*args, **kwargs)
        self.atom_home = AtomLeftArmHome(*args, **kwargs)

    def forward(self) -> str:
        results = []
        # Safe sequence
        results.append(self.atom_heater_off.forward())
        results.append(self.atom_exfoliation_heater_relay_off.forward())
        results.append(self.atom_vacuum_off.forward())
        results.append(self.atom_printer_off.forward())
        results.append(self.atom_home.forward())
        return "✅ System Reset.\n" + "\n".join(results)
