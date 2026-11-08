from typing import Optional
from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.exfoliation.molecule_flows import (
    MoleculePrepareMotherTape,
    MoleculeExfoliateChip,
    MoleculeResetSystem,
    MoleculeClearStage
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool
from common.services.instrument_status_service import get_instrument_status_service
from common.logic.exfoliation_setpoints import (
    validate_dwell_time_s,
    validate_exfoliation_setpoint_pair,
    validate_tape_peel_speed_v,
)

class AssemblyExfoliation(BaseWorkflow):
    name = "assembly_exfoliation"
    camera_route = "left"
    description = (
        "Complete Exfoliation Process (Assembly). "
        "conditionally runs Mother Tape Prep, then Exfoliated Chip sequences."
    )
    inputs = {
        "prepare_mother_tape": {
            "type": "boolean",
            "description": "Force run Mother Tape preparation? Default False (assumes tape ready unless specified).",
            "default": False,
            "nullable": True
        },
        "raw_carrier_qr_message": {
            "type": "string",
            "description": "Optional QR message to select a specific raw crystal carrier.",
            "nullable": True
        },
        "num_chips": {
            "type": "integer",
            "description": "Number of chips to exfoliate.",
            "default": 1,
            "nullable": True
        },
        "target_T": {
            "type": "number",
            "description": "Target exfoliation temperature (°C). Default 22.0.",
            "default": 22.0,
            "nullable": True
        },
        "hold_V": {
            "type": "number",
            "description": "Hold voltage after target temperature is reached (V). Must match approved target_T/hold_V pairs.",
            "default": 0.0,
            "nullable": True
        },
        "dwell_time_s": {
            "type": "number",
            "description": "Time to hold at target temperature (seconds). Allowed range 10 to 90. Default 10.",
            "default": 10.0,
            "nullable": True
        },
        "num_massages": {
            "type": "integer",
            "description": "Number of massage stroke rounds per row during tape adhesion. Default 1.",
            "default": 1,
            "nullable": True
        },
        "tape_peel_speed_v": {
            "type": "number",
            "description": "Tape peel arc feedrate (G-code F value). Allowed range 50 to 500. Default 500.",
            "default": 500.0,
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.molecule_clear_stage = MoleculeClearStage(*args, **kwargs)
        self.molecule_prep = MoleculePrepareMotherTape(*args, **kwargs)
        self.molecule_exfoliate = MoleculeExfoliateChip(*args, **kwargs)
        self.molecule_reset = MoleculeResetSystem(*args, **kwargs)
        
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    def forward(
        self,
        prepare_mother_tape: Optional[bool] = False,
        raw_carrier_qr_message: Optional[str] = None,
        num_chips: Optional[int] = 1,
        target_T: Optional[float] = 22.0,
        hold_V: Optional[float] = 0.0,
        dwell_time_s: Optional[float] = 10.0,
        num_massages: Optional[int] = 1,
        tape_peel_speed_v: Optional[float] = 500.0,
    ) -> str:
        results = []
        
        # Build Plan description for UI
        plan_desc = []

        # Adaptive Logic: Check for Scotch Tape Crystal
        status_service = get_instrument_status_service()
        instrument_state = status_service.read_true_instrument_state() or {}
        crystal_info = instrument_state.get("scotch_tape_crystal")

        auto_prep_triggered = False
        if not prepare_mother_tape:
            # If not forced, check if we need to auto-prepare
            if not crystal_info:
                # No crystal found -> We MUST prepare one
                prepare_mother_tape = True
                auto_prep_triggered = True
                plan_desc.append("⚠️ Auto-enabling Tape Prep (No Crystal Detected)")
            else:
                # Crystal found -> We can skip
                plan_desc.append(f"✅ Crystal Detected at ({crystal_info.get('position',{}).get('x',0):.1f}, {crystal_info.get('position',{}).get('y',0):.1f})mm")

        plan_desc.append("Clear Stage")
        if prepare_mother_tape:
            if auto_prep_triggered:
                 plan_desc.append("Prepare Mother Tape (Auto: No Crystal)")
            else:
                 plan_desc.append("Prepare Mother Tape")
        
        count = num_chips if num_chips is not None else 1
        exfoliation_target_T = float(target_T) if target_T is not None else 22.0
        exfoliation_target_T, exfoliation_hold_V = validate_exfoliation_setpoint_pair(
            exfoliation_target_T,
            hold_V,
        )
        exfoliation_dwell_time_s = validate_dwell_time_s(dwell_time_s)
        exfoliation_num_massages = int(num_massages) if num_massages is not None else 1
        exfoliation_tape_peel_speed_v = validate_tape_peel_speed_v(tape_peel_speed_v)
        if count > 0:
            plan_desc.append(f"Exfoliate {count} Chip(s)")
        
        self.broadcaster_plan.forward(plan_desc)

        try:
            step_idx = 0
            
            # 0. Clear Stage
            self.broadcaster_step.forward(step_idx)
            results.append("--- Phase 0: Clearing Stage ---")
            results.append(self.molecule_clear_stage.forward())
            # MoleculeClearStage doesn't broadcast a plan, but we resync just in case
            self.broadcaster_plan.forward(plan_desc)
            self.broadcaster_step.forward(step_idx)
            step_idx += 1

            # 1. Prepare Mother Tape
            if prepare_mother_tape:
                self.broadcaster_step.forward(step_idx)
                results.append(f"--- Phase 1: Preparing Mother Tape ---")
                results.append(
                    self.molecule_prep.forward(
                        raw_carrier_qr_message=raw_carrier_qr_message,
                        num_massages=exfoliation_num_massages,
                    )
                )
                # Restore parent plan
                self.broadcaster_plan.forward(plan_desc)
                self.broadcaster_step.forward(step_idx)
                step_idx += 1
            else:
                results.append("ℹ️ Skipped Mother Tape Preparation (Assumed Ready).")
            
            # Use the raw_carrier_qr_message directly as the material_label
            # because the lab encodes materials exactly as the QR code (e.g. 'Gr', 'hBN')
            material_label = raw_carrier_qr_message.strip() if raw_carrier_qr_message else "unknown"
            
            # 2. Exfoliate Chips
            if count > 0:
                self.broadcaster_step.forward(step_idx)
                results.append(f"--- Phase 2: Exfoliating {count} Chip(s) ---")
                for i in range(count):
                    results.append(f"--- Chip {i+1}/{count} ---")
                    results.append(
                        self.molecule_exfoliate.forward(
                            target_T=exfoliation_target_T,
                            hold_V=exfoliation_hold_V,
                            dwell_time_s=exfoliation_dwell_time_s,
                            num_massages=exfoliation_num_massages,
                            tape_peel_speed_v=exfoliation_tape_peel_speed_v,
                            material_type=material_label,
                        )
                    )
                    # Restore parent plan after EACH chip
                    self.broadcaster_plan.forward(plan_desc)
                    self.broadcaster_step.forward(step_idx)
                step_idx += 1

            if not prepare_mother_tape and count <= 0:
                return "⚠️ No actions selected."
        
        except Exception as e:
            results.append(f"❌ Error during Exfoliation Assembly: {e}")
            results.append("Attempting safety reset...")
            try:
                results.append(self.molecule_reset.forward())
            except Exception as reset_error:
                results.append(f"❌ Reset failed: {reset_error}")
            return "\n".join(results)

        return "✅ Assembly Exfoliation Cycle Complete.\n" + "\n".join(results)
