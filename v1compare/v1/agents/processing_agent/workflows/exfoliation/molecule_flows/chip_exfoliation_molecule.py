from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.exfoliation.atom_flows import (
    AtomLeftArmHome, AtomLeftArmPlaceSiliconChip, AtomVacuumOn, AtomExfoliatorPowerOn,
    AtomHeaterSet, AtomHeaterOn, AtomHeaterOff,
    AtomExfoliationHeaterRelayOn, AtomExfoliationHeaterRelayOff, AtomTransferHeaterRelayOff,
    AtomTapeApply, AtomLeftArmMassage, AtomTapePeel, AtomTapeRelease, AtomVacuumOff,
    AtomLeftArmStoreSiliconChip, AtomExfoliatorPowerOff, AtomReadTemperature, AtomWaitUntilReachTemperature, AtomWaitUntilDropTemperature,
    AtomExfoliationFanOff, AtomExfoliationFanOn
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool

from common.services import ChipInventoryService, ChipRecord
from common.services.chip_inventory_service import DEFAULT_CHIP_SLOT_ORIGIN
from common.logic.exfoliation_setpoints import (
    validate_dwell_time_s,
    validate_exfoliation_setpoint_pair,
    validate_tape_peel_speed_v,
)

from typing import Optional

import time

class MoleculeExfoliateChip(BaseWorkflow):
    name = "molecule_exfoliate_chip"
    description = "Exfoliate onto a silicon chip: Select chip, place (heat), apply tape, massage, peel, store chip."

    inputs = {
        "target_T": {"type": "number", "description": "Target temperature (°C).", "nullable": True},
        "hold_V":   {"type": "number", "description": "Hold voltage after reaching target_T (V). Must match approved target_T/hold_V pairs.", "default": 0.0, "nullable": True},
        "dwell_time_s": {"type": "number", "description": "Time to hold at target temperature (seconds). Allowed range 10 to 90. Default 10.", "default": 10, "nullable": True},
        "tape_peel_speed_v": {"type": "number", "description": "Tape peel arc feedrate (G-code F value). Allowed range 50 to 500. Default 500.", "default": 500, "nullable": True},
        "num_massages": {"type": "integer", "description": "Number of massage stroke rounds per row. Default 1.", "default": 1, "nullable": True},
        "material_type": {"type": "string", "description": "Material code (e.g. 'Gr', 'hBN'). Optional.", "nullable": True},
    }
    output_type = "string"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inventory = ChipInventoryService()
        
        # Tools
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

        # Instantiate Atoms
        self.atom_home = AtomLeftArmHome(*args, **kwargs)
        self.atom_place_chip = AtomLeftArmPlaceSiliconChip(*args, **kwargs)
        self.atom_vacuum_on = AtomVacuumOn(*args, **kwargs)
        self.atom_printer_on = AtomExfoliatorPowerOn(*args, **kwargs)
        self.atom_heater_set = AtomHeaterSet(*args, **kwargs)
        self.atom_heater_on = AtomHeaterOn(*args, **kwargs)
        self.atom_heater_off = AtomHeaterOff(*args, **kwargs)
        self.atom_exfoliation_heater_relay_on = AtomExfoliationHeaterRelayOn(*args, **kwargs)
        self.atom_exfoliation_heater_relay_off = AtomExfoliationHeaterRelayOff(*args, **kwargs)
        self.atom_transfer_heater_relay_off = AtomTransferHeaterRelayOff(*args, **kwargs)
        self.atom_tape_apply = AtomTapeApply(*args, **kwargs)
        self.atom_massage = AtomLeftArmMassage(*args, **kwargs)
        self.atom_tape_peel = AtomTapePeel(*args, **kwargs)
        self.atom_tape_release = AtomTapeRelease(*args, **kwargs)
        self.atom_vacuum_off = AtomVacuumOff(*args, **kwargs)
        self.atom_store_chip = AtomLeftArmStoreSiliconChip(*args, **kwargs)
        self.atom_read_temp = AtomReadTemperature(*args, **kwargs )
        self.atom_wait_until_reach_temperature = AtomWaitUntilReachTemperature(*args, **kwargs)
        self.atom_wait_until_drop_temperature = AtomWaitUntilDropTemperature(*args, **kwargs)
        self.atom_fan_on = AtomExfoliationFanOn(*args, **kwargs)
        self.atom_fan_off = AtomExfoliationFanOff(*args, **kwargs)

    PLAN_STEPS = [
        "Home & Find Chip",
        "Place Chip & Vac ON",
        "Apply Tape & Massage",
        "Raise Temperature & Hold",
        "Wait Heating &Peel Tape",
        "Vac OFF & Store Chip (Right)",
        "Release Tape & Cleanup"
    ]

    def _select_source_chip(self) -> ChipRecord:
        # STRICT: Only bare silicon chips on the LEFT side (X < 0)
        candidates = [
            chip
            for chip in self.inventory.list_chips(label="bare_silicon_chip")
            if chip.stage_position_x is not None and chip.stage_position_x < 0
        ]
        if not candidates:
            # raise RuntimeError("No bare silicon chips available in inventory.")
             # Fallback for safe testing if DB empty (user requested robusteness but keeping fallback for simulation continuity if needed, 
             # actually user asked for correct material. I will throw if empty to be strict as requested).
             raise RuntimeError("No bare silicon chips available on the LEFT side (X < 0).")
        
        # Sort by last_updated_date descending to get the most recent detection
        candidates.sort(key=lambda c: c.last_updated_date, reverse=True)
        return candidates[0]

    def forward(
        self,
        target_T: Optional[float],
        hold_V: Optional[float] = None,
        dwell_time_s: Optional[float] = 10.0,
        tape_peel_speed_v: Optional[float] = 500.0,
        num_massages: Optional[int] = 1,
        material_type: Optional[str] = None,
    ) -> str:
        if target_T is None:
            raise ValueError("target_T is required.")

        target_T, hold_V = validate_exfoliation_setpoint_pair(target_T, hold_V)
        dwell_time_s = validate_dwell_time_s(dwell_time_s)
        tape_peel_speed_v = validate_tape_peel_speed_v(tape_peel_speed_v)
        massage_rounds = int(num_massages) if num_massages is not None else 1

        results = []
        self.broadcaster_plan.forward(self.PLAN_STEPS)
        
        # 1. Select Chip
        self.broadcaster_step.forward(0)
        results.append(self.atom_home.forward())
        chip_record = self._select_source_chip()
        if chip_record.pickup_left_x_mm is None or chip_record.pickup_left_y_mm is None:
            raise RuntimeError("Selected chip missing pickup_left_x_mm/pickup_left_y_mm.")
        cx = float(chip_record.pickup_left_x_mm)
        cy = float(chip_record.pickup_left_y_mm)
        cangle = chip_record.pickup_left_angle_deg or chip_record.angle_deg or 0.0

        # 2. Prep Hardware
        results.append(self.atom_printer_on.forward())

        # 3. Place Chip & Vacuum
        self.broadcaster_step.forward(1)
        results.append(self.atom_place_chip.forward(cx, cy, cangle))
        results.append(self.atom_vacuum_on.forward())

        # 4. Prepares Heating & Read Temp
        temp_reading = self.atom_read_temp.forward()
        results.append(temp_reading)
        temp_value = float(temp_reading.split(":")[-1].replace("°C", "").strip())

        # 5. Exfoliation
        self.broadcaster_step.forward(2)
        results.append(self.atom_tape_apply.forward())
        results.append(self.atom_massage.forward(num_massages=massage_rounds))

        # 6. Heating if needed
        self.broadcaster_step.forward(3)
        wait_s = dwell_time_s
        if temp_value < float(target_T):
            results.append(self.atom_vacuum_off.forward())
            results.append(self.atom_tape_peel.forward(tape_peel_speed_v=tape_peel_speed_v))
            results.append(self.atom_heater_set.forward(27.0))
            results.append(self.atom_heater_on.forward())
            results.append(self.atom_exfoliation_heater_relay_on.forward())
            results.append(self.atom_transfer_heater_relay_off.forward())
            results.append(self.atom_wait_until_reach_temperature.forward(float(target_T)))
            results.append(self.atom_heater_set.forward(float(hold_V)))
            results.append(self.atom_tape_apply.forward())
            results.append(self.atom_vacuum_on.forward())
            results.append(self.atom_heater_off.forward())
            results.append(self.atom_exfoliation_heater_relay_off.forward())
            results.append(self.atom_fan_on.forward())
            time.sleep(wait_s)
        else:
            # Maintain a consistent dwell stage even if chip is already at/above target.
            time.sleep(wait_s)

        # 7. Peel (Heater OFF first)
        self.broadcaster_step.forward(4)
        results.append(self.atom_wait_until_drop_temperature.forward(60.0))
        results.append(self.atom_fan_off.forward())
        results.append(self.atom_tape_peel.forward(tape_peel_speed_v=tape_peel_speed_v))
        # results.append(self.atom_heater_off.forward())

        # 8. Store Chip (Right Side)
        self.broadcaster_step.forward(5)
        results.append(self.atom_vacuum_off.forward())
        
        # Reserve Slot
        try:
            slot = self.inventory.find_first_free_slot()
            if slot is None:
                raise RuntimeError("No free chip slots available.")
            tx = float(slot.x_left_mm if slot.x_left_mm is not None else DEFAULT_CHIP_SLOT_ORIGIN[0])
            ty = float(slot.y_left_mm if slot.y_left_mm is not None else DEFAULT_CHIP_SLOT_ORIGIN[1])
            slot_label = slot.label or f"slot_{slot.id}"
            
            # Exec Store
            results.append(self.atom_store_chip.forward(tx, ty))
            
            # Commit Slot
            self.inventory.record_move(
                "exfoliated_chip",
                chip_id=chip_record.id,
                from_slot_label=None,
                to_slot_label=slot.label,
                source="exfoliation",
                notes=f"stored from ({cx},{cy})"
            )
            
            # Promote Chip Logic
            self.inventory.promote_chip(
                chip_id=chip_record.id,
                new_label="exfoliated_chip",
                stage_position_x=tx,
                stage_position_y=ty,
                table_coordinate_left=f"{int(tx)}:{int(ty)}",
                material_type=material_type,
            )

        except Exception as e:
            results.append(f"⚠️ Slot reservation error: {e}")
            # Try to store to default right-side origin if failed (assuming default is safe)
            results.append(self.atom_store_chip.forward(DEFAULT_CHIP_SLOT_ORIGIN[0], DEFAULT_CHIP_SLOT_ORIGIN[1]))

        # 9. Release tape before cleanup
        results.append(self.atom_tape_release.forward())

        # 10. Cleanup
        self.broadcaster_step.forward(6)
        results.append(self.atom_home.forward())

        return "✅ Chip Exfoliation Complete.\n" + "\n".join(results)
