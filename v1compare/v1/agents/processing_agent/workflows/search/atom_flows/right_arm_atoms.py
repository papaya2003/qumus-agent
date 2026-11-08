from __future__ import annotations

from typing import Optional

from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class _RightArmAtom(BaseWorkflow):
    def _require_arm(self) -> bool:
        if self.arm is None:
            print("❌ Robot arm controller unavailable.")
            return False
        return True


class AtomRightArmHome(_RightArmAtom):
    name = "atom_right_arm_home"
    description = "Move the right robot arm to a safe home position."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_arm():
            return "❌ Robot arm controller unavailable."
        self.arm.home_to_search_stage()
        return "✅ Right robot arm homed."


class AtomRightArmMoveTo(_RightArmAtom):
    name = "atom_right_arm_move_to"
    description = "Move the right robot arm to a specific (X, Y, Z) coordinate."
    inputs = {
        "x": {"type": "number", "description": "Target X coordinate.", "nullable": True},
        "y": {"type": "number", "description": "Target Y coordinate.", "nullable": True},
        "z": {"type": "number", "description": "Target Z coordinate.", "nullable": True},
        "speed": {"type": "integer", "description": "Movement speed (default 5000).", "default": 5000, "nullable": True},
    }
    output_type = "string"

    def forward(self, x: Optional[float], y: Optional[float], z: Optional[float], speed: Optional[int] = 5000) -> str:
        if not self._require_arm():
            return "❌ Robot arm controller unavailable."
        if x is None or y is None or z is None:
            return "❌ Error: X, Y, and Z coordinates are required."
        self.arm.move_to(x, y, z, speed=speed)
        return f"✅ Right robot arm moved to ({x}, {y}, {z}) at speed {speed}."


class AtomRightArmVacuumOn(_RightArmAtom):
    name = "atom_right_arm_vacuum_on"
    description = "Turn ON the right robot arm vacuum (suction cup) to pick up items."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_arm():
            return "❌ Robot arm controller unavailable."
        self.arm.vacuum_on()
        return "✅ Right robot arm vacuum ON (suction active)."


class AtomRightArmVacuumOff(_RightArmAtom):
    name = "atom_right_arm_vacuum_off"
    description = "Turn OFF the right robot arm vacuum (suction cup) to release items."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_arm():
            return "❌ Robot arm controller unavailable."
        self.arm.vacuum_off()
        return "✅ Right robot arm vacuum OFF (items released)."


class AtomRightArmPlaceSiliconChipOnTransferStage(_RightArmAtom):
    name = "atom_right_arm_place_silicon_chip_on_transfer_stage"
    description = "Pick up a silicon chip and place it on the transfer stage using the right robot arm."
    inputs = {
        "pickup_x": {"type": "number", "description": "Pickup X coordinate (mm)."},
        "pickup_y": {"type": "number", "description": "Pickup Y coordinate (mm)."},
        "rotate_angle": {"type": "number", "description": "Optional nose rotation (deg).", "default": 0.0, "nullable": True},
    }
    output_type = "string"

    def forward(
        self,
        pickup_x: float,
        pickup_y: float,
        rotate_angle: Optional[float] = 0.0,
    ) -> str:
        if not self._require_arm():
            return "❌ Robot arm controller unavailable."
        px = float(pickup_x)
        py = float(pickup_y)
        pang = float(rotate_angle) if rotate_angle is not None else 0.0
        self.arm.place_silicon_chip_on_transfer_stage(px, py, rotate_angle=pang)
        return "✅ Silicon chip placed on transfer stage by right arm."


class AtomRightArmStoreSearchSiliconChip(_RightArmAtom):
    name = "atom_right_arm_store_search_silicon_chip"
    description = "Pick up silicon chip from transfer/search stage and store it to target location."
    inputs = {
        "x": {"type": "number", "description": "Target X coordinate.", "nullable": True},
        "y": {"type": "number", "description": "Target Y coordinate.", "nullable": True},
        "rotate_angle": {"type": "number", "description": "Optional nose rotation (deg).", "default": None, "nullable": True},
    }
    output_type = "string"

    def forward(self, x: Optional[float], y: Optional[float], rotate_angle: Optional[float] = None) -> str:
        if not self._require_arm():
            return "❌ Robot arm controller unavailable."
        if x is None or y is None:
            return "❌ Error: X and Y coordinates are required."
        angle = float(rotate_angle) if rotate_angle is not None else None
        self.arm.store_search_silicon_chip(float(x), float(y), rotate_angle=angle)
        return f"✅ Search silicon chip stored to ({x}, {y}) by right arm."
