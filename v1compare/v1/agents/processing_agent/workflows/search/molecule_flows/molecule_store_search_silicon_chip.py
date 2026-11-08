from __future__ import annotations

from typing import Optional

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.search.atom_flows import AtomRightArmStoreSearchSiliconChip
from agents.processing_agent.workflows.search.atom_flows.transfer_stage_vacuum_atoms import (
    AtomTransferStageVacuumOff,
)
from common.chip_slot_policy import is_robot_accessible_chip_slot, is_robot_accessible_chip_slot_label
from common.services.chip_inventory_service import ChipInventoryService
from common.utils.dual_arm_utils import get_slot_coordinates_for_arm
from database.db_utils import material_session_scope
from database.models import ChipSlot
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeStoreSearchSiliconChip(BaseWorkflow):
    """
    Return a chip from the transfer/search stage to its original storage slot.

    Strategy:
    1. If chip_id provided, use stored metadata search_origin when available
    2. If metadata missing, fallback to first free chip slot with right-arm coords
    3. If slot_label provided, return to that slot (and use it for fallback when no metadata)
    """

    name = "molecule_store_search_silicon_chip"
    description = "Return a chip from the transfer/search stage to its original slot using the right arm."

    inputs = {
        "chip_id": {
            "type": "integer",
            "description": "Numeric chip id to return. Uses stored pickup metadata when available.",
            "nullable": True,
        },
        "slot_label": {
            "type": "string",
            "description": "Explicit chip slot to return to if metadata is missing.",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chip_service = ChipInventoryService()
        self.arm_store_search = AtomRightArmStoreSearchSiliconChip(*args, **kwargs)
        self.stage_vac_off = AtomTransferStageVacuumOff(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    PLAN_STEPS = [
        "Disable Transfer Stage Vacuum",
        "Return Chip to Storage"
    ]

    def _first_free_right_slot(self) -> ChipSlot:
        for slot in self.chip_service.list_robot_accessible_slots():
            if slot.is_occupied:
                continue
            if slot.x_right_mm is None or slot.y_right_mm is None:
                continue
            with material_session_scope() as session:
                db_slot = session.query(ChipSlot).filter(ChipSlot.id == slot.id).one_or_none()
                if db_slot and is_robot_accessible_chip_slot(db_slot):
                    return db_slot
        raise ValueError("No free chip slots with right-arm coordinates available.")

    def _resolve_target(
        self,
        chip_id: Optional[int],
        slot_label: Optional[str],
    ) -> tuple[float, float, Optional[str], Optional[int], Optional[str]]:
        """
        Returns:
            (x_right, y_right, target_slot_label, resolved_chip_id, resolved_chip_label)
        """
        if chip_id is not None:
            chip_record = self.chip_service.get_chip_by_id(int(chip_id))
            if not chip_record:
                raise ValueError(f"Chip id '{chip_id}' not found in database")

            origin = chip_record.metadata.get("search_origin") if isinstance(chip_record.metadata, dict) else None
            if isinstance(origin, dict):
                ox = origin.get("pickup_right_x_mm")
                oy = origin.get("pickup_right_y_mm")
                if ox is not None and oy is not None:
                    return float(ox), float(oy), origin.get("slot_label"), chip_record.id, chip_record.label

                origin_slot_label = origin.get("slot_label")
                if origin_slot_label:
                    if not is_robot_accessible_chip_slot_label(origin_slot_label):
                        raise ValueError(f"Chip slot '{origin_slot_label}' is reserved to remain open")
                    with material_session_scope() as session:
                        slot = session.query(ChipSlot).filter(ChipSlot.label == origin_slot_label).one_or_none()
                        if slot:
                            x_right, y_right = get_slot_coordinates_for_arm(slot, target_arm="right")
                            return float(x_right), float(y_right), slot.label, chip_record.id, chip_record.label

            if slot_label:
                if not is_robot_accessible_chip_slot_label(slot_label):
                    raise ValueError(f"Chip slot '{slot_label}' is reserved to remain open")
                with material_session_scope() as session:
                    slot = session.query(ChipSlot).filter(ChipSlot.label == slot_label).one_or_none()
                    if not slot:
                        raise ValueError(f"Chip slot '{slot_label}' not found in database")
                    x_right, y_right = get_slot_coordinates_for_arm(slot, target_arm="right")
                    return float(x_right), float(y_right), slot.label, chip_record.id, chip_record.label

            slot = self._first_free_right_slot()
            x_right, y_right = get_slot_coordinates_for_arm(slot, target_arm="right")
            return float(x_right), float(y_right), slot.label, chip_record.id, chip_record.label

        if slot_label:
            if not is_robot_accessible_chip_slot_label(slot_label):
                raise ValueError(f"Chip slot '{slot_label}' is reserved to remain open")
            with material_session_scope() as session:
                slot = session.query(ChipSlot).filter(ChipSlot.label == slot_label).one_or_none()
                if not slot:
                    raise ValueError(f"Chip slot '{slot_label}' not found in database")
                x_right, y_right = get_slot_coordinates_for_arm(slot, target_arm="right")
                slot_chip_id = slot.chip.id if slot.chip is not None else None
                slot_chip_label = slot.chip.label if slot.chip is not None else None
                return float(x_right), float(y_right), slot.label, slot_chip_id, slot_chip_label

        slot = self._first_free_right_slot()
        x_right, y_right = get_slot_coordinates_for_arm(slot, target_arm="right")
        return float(x_right), float(y_right), slot.label, None, None

    def forward(
        self,
        chip_id: Optional[int] = None,
        slot_label: Optional[str] = None,
    ) -> str:
        try:
            target_x, target_y, target_slot_label, resolved_chip_id, resolved_chip_label = self._resolve_target(
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
        rotate_angle = chip_record.pickup_right_angle_deg if chip_record else None

        self.broadcaster_plan.forward(self.PLAN_STEPS)

        self.broadcaster_step.forward(0)
        print("⚡ Disabling transfer stage vacuum...")
        self.stage_vac_off.forward()

        self.broadcaster_step.forward(1)
        print(f"🤖 Right arm returning chip to ({target_x:.1f}, {target_y:.1f})...")
        self.arm_store_search.forward(target_x, target_y, rotate_angle=rotate_angle)

        if chip_record is None:
            if resolved_chip_id is not None:
                chip_record = self.chip_service.get_chip_by_id(resolved_chip_id)
            elif resolved_chip_label:
                chip_record = self.chip_service.get_chip(resolved_chip_label)

        if chip_record:
            resolved_label = resolved_chip_label or chip_record.label
            metadata = dict(chip_record.metadata or {})
            metadata.pop("search_origin", None)
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
            if target_slot_label:
                self.chip_service.record_move(
                    resolved_label,
                    chip_id=chip_record.id,
                    from_slot_label=None,
                    to_slot_label=target_slot_label,
                    source="search_return",
                    notes="molecule_store_search_silicon_chip",
                )

        return f"✅ Chip stored to ({target_x:.1f}, {target_y:.1f})."
