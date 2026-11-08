from typing import Optional

from agents.processing_agent.workflows.base_workflow import BaseWorkflow, _parse_xy_angle
from agents.processing_agent.workflows.exfoliation.atom_flows import (
    AtomTapeRelease,
    AtomLeftArmPlaceRawCarrier,
    AtomVacuumOn,
    AtomExfoliatorPowerOn,
    AtomTapeApply,
    AtomLeftArmMassage,
    AtomTapePeel,
    AtomVacuumOff,
    AtomLeftArmStoreRawCarrier,
    AtomLeftArmHome,
    AtomExfoliatorPowerOff,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool
from agents.processing_agent.tools.chip_pickup_tool import SelectChipPickupPosition

class MoleculePrepareMotherTape(BaseWorkflow):
    name = "molecule_prepare_mother_tape"
    description = "Prepare mother tape from raw crystal carrier: Release tape, pick carrier, apply tape, massage, peel, return carrier."
    inputs = {
        "raw_carrier_qr_message": {
            "type": "string",
            "description": "Optional QR message to select a specific raw crystal carrier.",
            "nullable": True,
        },
        "num_massages": {
            "type": "integer",
            "description": "Number of massage stroke rounds per row. Default 1.",
            "default": 1,
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cv = SelectChipPickupPosition()
        
        # Tools
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

        # Instantiate Atoms
        self.atom_release = AtomTapeRelease(*args, **kwargs)
        self.atom_home = AtomLeftArmHome(*args, **kwargs)
        self.atom_place_carrier = AtomLeftArmPlaceRawCarrier(*args, **kwargs)
        self.atom_vacuum_on = AtomVacuumOn(*args, **kwargs)
        self.atom_printer_on = AtomExfoliatorPowerOn(*args, **kwargs)
        self.atom_tape_apply = AtomTapeApply(*args, **kwargs)
        self.atom_massage = AtomLeftArmMassage(*args, **kwargs)
        self.atom_tape_peel = AtomTapePeel(*args, **kwargs)
        self.atom_vacuum_off = AtomVacuumOff(*args, **kwargs)
        self.atom_store_carrier = AtomLeftArmStoreRawCarrier(*args, **kwargs)
        self.atom_printer_off = AtomExfoliatorPowerOff(*args, **kwargs)

    PLAN_STEPS = [
        "Release Tape",
        "Home & Find Carrier",
        "Place Carrier & Vac ON",
        "Apply Tape & Massage",
        "Peel Tape",
        "Vac OFF",
        "Return Carrier",
        "Cleanup"
    ]

    def forward(self, raw_carrier_qr_message: Optional[str] = None, num_massages: Optional[int] = 1) -> str:
        massage_rounds = int(num_massages) if num_massages is not None else 1
        results = []
        self.broadcaster_plan.forward(self.PLAN_STEPS)
        
        # 1. Release Tape
        self.broadcaster_step.forward(0)
        results.append(self.atom_release.forward(120.0))
        
        # 2. Find Carrier
        self.broadcaster_step.forward(1)
        results.append(self.atom_home.forward())
        pos_str = self.cv.forward("raw_crystal_carrier", qr_message=raw_carrier_qr_message)
        rx, ry, rangle = _parse_xy_angle(pos_str)

        # 3. Place Carrier
        self.broadcaster_step.forward(2)
        results.append(self.atom_printer_on.forward())
        results.append(self.atom_place_carrier.forward(rx, ry, rangle))
        results.append(self.atom_vacuum_on.forward())

        # 4. Apply & Massage
        self.broadcaster_step.forward(3)
        results.append(self.atom_tape_apply.forward())
        results.append(self.atom_massage.forward(num_massages=massage_rounds))
        
        # 5. Peel
        self.broadcaster_step.forward(4)
        results.append(self.atom_tape_peel.forward())

        # 6. Vac OFF
        self.broadcaster_step.forward(5)
        results.append(self.atom_vacuum_off.forward())

        # 7. Return Carrier
        self.broadcaster_step.forward(6)
        results.append(self.atom_store_carrier.forward(rx, ry))
        
        # 8. Cleanup
        self.broadcaster_step.forward(7)
        results.append(self.atom_home.forward())
        
        return "✅ Mother Tape Prepared.\n" + "\n".join(results)
