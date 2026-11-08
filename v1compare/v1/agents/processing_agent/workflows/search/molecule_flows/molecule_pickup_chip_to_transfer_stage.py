from __future__ import annotations

from typing import Optional

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.search.atom_flows import (
    AtomRightArmPlaceSiliconChipOnTransferStage,
    AtomRightArmHome,
)
from agents.processing_agent.workflows.search.atom_flows.transfer_stage_vacuum_atoms import (
    AtomTransferStageVacuumOn,
)
from common.chip_slot_policy import is_robot_accessible_chip_slot_label
from common.services.chip_inventory_service import ChipInventoryService
from common.utils.dual_arm_utils import get_slot_coordinates_for_arm
from database.db_utils import material_session_scope
from database.models import ChipSlot
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool

class MoleculePickupChipToTransferStage(BaseWorkflow):
    """
    Pick up a chip from a storage slot and place it on the transfer stage using the right robot arm.
    
    This molecule flow:
    1. Looks up the chip location in the database (by chip id or slot label)
    2. Moves right arm to pick up the chip
    3. Places chip on the transfer stage
    4. Enables stage vacuum
    """
    
    name = "molecule_pickup_chip_to_transfer_stage"
    description = "Pick up a chip from storage and place it on the transfer stage using the right arm."
    
    inputs = {
        "chip_id": {
            "type": "integer",
            "description": "Numeric chip id to pick up. Takes priority over slot_label.",
            "nullable": True
        },
        "slot_label": {
            "type": "string",
            "description": "Label of the chip slot to pick from (e.g., 'S3'). Used if chip_id not provided.",
            "nullable": True
        },
        "rotate_angle": {
            "type": "number",
            "description": "Optional nose rotation (deg). Uses pickup_right_angle_deg when not provided.",
            "default": None,
            "nullable": True
        }
    }
    output_type = "string"
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chip_service = ChipInventoryService()
        self.arm_home = AtomRightArmHome(*args, **kwargs)
        self.arm_place_on_transfer_stage = AtomRightArmPlaceSiliconChipOnTransferStage(*args, **kwargs)
        self.stage_vac_on = AtomTransferStageVacuumOn(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    PLAN_STEPS = [
        "Home Right Arm",
        "Place Chip on Transfer Stage",
        "Enable Stage Vacuum"
    ]
    
    def _get_chip_pickup_coords(
        self,
        chip_id: Optional[int],
        slot_label: Optional[str]
    ) -> tuple[float, float, Optional[str], Optional[int], Optional[str]]:
        """
        Get chip pickup coordinates in right arm frame.
        
        Strategy:
        1. If chip_id provided, look up chip and use its coordinates
        2. If slot_label provided, look up slot coordinates
        
        Returns:
            (x_right, y_right, origin_slot_label, resolved_chip_id, resolved_chip_label)
            in mm, right arm coordinate frame
        """
        if chip_id is not None:
            chip_record = self.chip_service.get_chip_by_id(int(chip_id))
            if not chip_record:
                raise ValueError(f"Chip id '{chip_id}' not found in database")

            if chip_record.pickup_right_x_mm is None or chip_record.pickup_right_y_mm is None:
                raise ValueError(f"Chip id '{chip_id}' has no right-arm pickup coordinates")

            x_right = float(chip_record.pickup_right_x_mm)
            y_right = float(chip_record.pickup_right_y_mm)
            print(f"📍 Chip id '{chip_id}' pickup coords (right arm): ({x_right:.1f}, {y_right:.1f})")
            origin_slot_label = None
            if chip_record.current_slot_id is not None:
                slot = self.chip_service.get_slot(chip_record.current_slot_id)
                if slot is not None and not is_robot_accessible_chip_slot_label(slot.label):
                    raise ValueError(
                        f"Chip id '{chip_id}' is currently assigned to slot '{slot.label}', which is reserved to remain open"
                    )
                origin_slot_label = slot.label if slot else None
            return (x_right, y_right, origin_slot_label, chip_record.id, chip_record.label)

        if slot_label:
            if not is_robot_accessible_chip_slot_label(slot_label):
                raise ValueError(f"Chip slot '{slot_label}' is reserved to remain open")
            # Get slot from database
            with material_session_scope() as session:
                slot = session.query(ChipSlot).filter(ChipSlot.label == slot_label).one_or_none()
                if not slot:
                    raise ValueError(f"Chip slot '{slot_label}' not found in database")
                
                x_right, y_right = get_slot_coordinates_for_arm(slot, target_arm="right")
                slot_chip_label = slot.chip.label if slot.chip is not None else None

            print(f"📍 Slot '{slot_label}' pickup coords (right arm): ({x_right:.1f}, {y_right:.1f})")
            slot_chip_id = slot.chip.id if slot.chip is not None else None
            return (x_right, y_right, slot.label, slot_chip_id, slot_chip_label)

        # Auto-select a chip if none specified (exfoliated chips only).
        candidates = list(self.chip_service.list_chips(label="exfoliated_chip"))
        candidates.sort(key=lambda c: (c.last_updated_date is None, c.last_updated_date), reverse=True)

        # First try chips with pickup coords.
        for chip in candidates:
            if chip.pickup_right_x_mm is None or chip.pickup_right_y_mm is None:
                continue
            x_right = float(chip.pickup_right_x_mm)
            y_right = float(chip.pickup_right_y_mm)
            print(f"📍 Auto-selected chip '{chip.label}' pickup coords (right arm): ({x_right:.1f}, {y_right:.1f})")
            origin_slot_label = None
            if chip.current_slot_id is not None:
                slot = self.chip_service.get_slot(chip.current_slot_id)
                if slot is not None and not is_robot_accessible_chip_slot_label(slot.label):
                    continue
                origin_slot_label = slot.label if slot else None
            return (x_right, y_right, origin_slot_label, chip.id, chip.label)

        raise ValueError("No available exfoliated chips with pickup coordinates in inventory.")

    def forward(
        self,
        chip_id: Optional[int] = None,
        slot_label: Optional[str] = None,
        rotate_angle: Optional[float] = None
    ) -> str:
        """
        Execute the chip pickup and placement workflow.
        """
        self.broadcaster_plan.forward(self.PLAN_STEPS)

        self.broadcaster_step.forward(0)
        print("🤖 Moving right arm to home position...")
        self.arm_home.forward()
        
        try:
            # Get pickup coordinates (in right arm frame)
            pickup_x, pickup_y, origin_slot_label, resolved_chip_id, resolved_chip_label = self._get_chip_pickup_coords(
                chip_id,
                slot_label,
            )
        except ValueError as e:
            return f"❌ Error: {str(e)}"

        chip_record = None
        if resolved_chip_id is not None:
            chip_record = self.chip_service.get_chip_by_id(resolved_chip_id)
        elif resolved_chip_label:
            chip_record = self.chip_service.get_chip(resolved_chip_label)
        angle_source = chip_record.pickup_right_angle_deg if chip_record else None

        if rotate_angle is None:
            angle = float(angle_source) if angle_source is not None else 0.0
        else:
            angle = float(rotate_angle)

        self.broadcaster_step.forward(1)
        print(f"🤖 Right arm picking up from ({pickup_x:.1f}, {pickup_y:.1f}) and placing on transfer stage...")

        self.arm_place_on_transfer_stage.forward(
            pickup_x=pickup_x,
            pickup_y=pickup_y,
            rotate_angle=angle,
        )

        if chip_record is None:
            if resolved_chip_id is not None:
                chip_record = self.chip_service.get_chip_by_id(resolved_chip_id)
            elif resolved_chip_label:
                chip_record = self.chip_service.get_chip(resolved_chip_label)
        if chip_record:
            resolved_label = resolved_chip_label or chip_record.label
            metadata = dict(chip_record.metadata or {})
            metadata["search_origin"] = {
                "slot_label": origin_slot_label,
                "pickup_right_x_mm": pickup_x,
                "pickup_right_y_mm": pickup_y,
            }
            self.chip_service.upsert_chip(
                resolved_label,
                chip_id=chip_record.id,
                stage_position_x=chip_record.stage_position_x,
                stage_position_y=chip_record.stage_position_y,
                chip_corner_coords=chip_record.chip_corner_coords,
                chip_corner_image_path=chip_record.chip_corner_image_path,
                pickup_left_x_mm=chip_record.pickup_left_x_mm,
                pickup_left_y_mm=chip_record.pickup_left_y_mm,
                pickup_left_angle_deg=chip_record.pickup_left_angle_deg,
                pickup_right_x_mm=chip_record.pickup_right_x_mm,
                pickup_right_y_mm=chip_record.pickup_right_y_mm,
                pickup_right_angle_deg=chip_record.pickup_right_angle_deg,
                angle_deg=chip_record.angle_deg,
                width_mm=chip_record.width_mm,
                height_mm=chip_record.height_mm,
                confidence=chip_record.confidence,
                table_coordinate_left=chip_record.table_coordinate_left,
                table_coordinate_right=chip_record.table_coordinate_right,
                metadata=metadata,
            )
            if origin_slot_label:
                self.chip_service.record_move(
                    resolved_label,
                    chip_id=chip_record.id,
                    from_slot_label=origin_slot_label,
                    to_slot_label=None,
                    source="search_pickup",
                    notes="molecule_pickup_chip_to_transfer_stage",
                )
        
        self.broadcaster_step.forward(2)
        print("⚡ Enabling transfer stage vacuum...")
        self.stage_vac_on.forward()
        
        return (
            f"✅ Chip picked up from ({pickup_x:.1f}, {pickup_y:.1f}) and placed on transfer stage. "
            "Transfer stage vacuum enabled."
        )
