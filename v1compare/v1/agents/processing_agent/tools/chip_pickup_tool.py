from __future__ import annotations

from typing import Optional

from smolagents import Tool

from common.services.chip_inventory_service import ChipInventoryService
from common.services.raw_material_carrier_service import RawMaterialCarrierService


class SelectChipPickupPosition(Tool):
    name = "SelectChipPickupPosition"
    description = (
        "Select pickup coordinates for a stored chip or raw crystal carrier based on inventory data."
    )
    inputs = {
        "item_type": {
            "type": "string",
            "description": "Inventory item type: bare_silicon_chip, exfoliated_chip, or raw_crystal_carrier.",
        },
        "arm": {
            "type": "string",
            "description": "Robot arm to use for chip pickup coordinates ('left' or 'right'). Required for chips.",
            "nullable": True,
        },
        "qr_message": {
            "type": "string",
            "description": "Optional QR message filter for raw crystal carriers.",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self) -> None:
        super().__init__()
        self._chip_service = ChipInventoryService()
        self._carrier_service = RawMaterialCarrierService()

    def _format_result(self, x: float, y: float, angle: Optional[float]) -> str:
        ang = float(angle) if angle is not None else 0.0
        return f"{float(x)},{float(y)},{ang}"

    def forward(self, item_type: str, qr_message: Optional[str] = None, arm: Optional[str] = None) -> str:
        item = (item_type or "").strip().lower()
        arm_value = (arm or "").strip().lower()

        if item == "bare_silicon_chip":
            if arm_value not in ("left", "right"):
                raise ValueError("arm must be 'left' or 'right' when selecting bare_silicon_chip.")
            if arm_value == "left":
                chips = [
                    c
                    for c in self._chip_service.list_chips()
                    if c.pickup_left_x_mm is not None and c.pickup_left_y_mm is not None
                ]
            else:
                chips = [
                    c
                    for c in self._chip_service.list_chips()
                    if c.pickup_right_x_mm is not None and c.pickup_right_y_mm is not None
                ]
            if not chips:
                raise ValueError("No chips with pickup coordinates available in inventory.")
            chips.sort(key=lambda c: (c.last_updated_date is None, c.last_updated_date), reverse=True)
            chip = chips[0]
            if arm_value == "left":
                return self._format_result(chip.pickup_left_x_mm, chip.pickup_left_y_mm, chip.pickup_left_angle_deg)
            return self._format_result(chip.pickup_right_x_mm, chip.pickup_right_y_mm, chip.pickup_right_angle_deg)

        if item == "exfoliated_chip":
            if arm_value not in ("left", "right"):
                raise ValueError("arm must be 'left' or 'right' when selecting exfoliated_chip.")
            if arm_value == "left":
                chips = [
                    c for c in self._chip_service.list_chips(label="exfoliated_chip")
                    if c.pickup_left_x_mm is not None and c.pickup_left_y_mm is not None
                ]
            else:
                chips = [
                    c for c in self._chip_service.list_chips(label="exfoliated_chip")
                    if c.pickup_right_x_mm is not None and c.pickup_right_y_mm is not None
                ]
            if not chips:
                raise ValueError("No exfoliated chips with pickup coordinates available in inventory.")
            chips.sort(key=lambda c: (c.last_updated_date is None, c.last_updated_date), reverse=True)
            chip = chips[0]
            if arm_value == "left":
                return self._format_result(chip.pickup_left_x_mm, chip.pickup_left_y_mm, chip.pickup_left_angle_deg)
            return self._format_result(chip.pickup_right_x_mm, chip.pickup_right_y_mm, chip.pickup_right_angle_deg)

        if item == "raw_crystal_carrier":
            carriers = self._carrier_service.list_carriers()
            if qr_message:
                carriers = [c for c in carriers if (c.qr_message or "").strip() == qr_message.strip()]
            carriers = [
                c
                for c in carriers
                if c.pickup_center_x_mm is not None and c.pickup_center_y_mm is not None
            ]
            if not carriers:
                raise ValueError("No raw crystal carriers with pickup coordinates available.")
            carrier = carriers[0]
            return self._format_result(
                carrier.pickup_center_x_mm,
                carrier.pickup_center_y_mm,
                carrier.pickup_angle_deg,
            )

        raise ValueError(f"Unsupported item_type '{item_type}'.")
