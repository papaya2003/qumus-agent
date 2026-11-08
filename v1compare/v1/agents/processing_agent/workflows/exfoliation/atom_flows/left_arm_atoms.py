from typing import Optional

from agents.processing_agent.workflows.base_workflow import BaseWorkflow, _parse_xy_angle
from agents.processing_agent.tools.chip_pickup_tool import SelectChipPickupPosition

class AtomLeftArmHome(BaseWorkflow):
    name = "atom_left_arm_home"
    description = "Move the robot arm to a safe home above the exfoliation stage (0, 180, 95)."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.arm.home_to_exfoliation_stage()
        return "✅ Robot arm homed."

class AtomLeftArmPlaceSiliconChip(BaseWorkflow):
    name = "atom_left_arm_place_silicon_chip"
    description = "Pick up a silicon chip from storage and place it on the exfoliation stage."
    inputs = {
        "x": {"type": "number", "description": "Source X coordinate (auto-selected if omitted).", "nullable": True},
        "y": {"type": "number", "description": "Source Y coordinate (auto-selected if omitted).", "nullable": True},
        "rotate_angle": {"type": "number", "description": "Placement rotation angle (defaults to DB value).", "nullable": True}
    }
    output_type = "string"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pickup_selector = SelectChipPickupPosition()

    def _resolve_pickup_values(
        self,
        x: Optional[float],
        y: Optional[float],
        rotate_angle: Optional[float],
    ) -> tuple[float, float, float]:
        px = float(x) if x is not None else None
        py = float(y) if y is not None else None
        pang = float(rotate_angle) if rotate_angle is not None else None

        if px is None or py is None or pang is None:
            coords = self._pickup_selector.forward("bare_silicon_chip", arm="left")
            auto_x, auto_y, auto_angle = _parse_xy_angle(coords)
            if px is None:
                px = auto_x
            if py is None:
                py = auto_y
            if pang is None:
                pang = auto_angle

        if px is None or py is None:
            raise RuntimeError("Chip pickup coordinates unavailable. Provide x/y or ensure inventory data exists.")
        if pang is None:
            pang = 0.0

        return float(px), float(py), float(pang)

    def forward(self, x: Optional[float], y: Optional[float], rotate_angle: Optional[float] = 0.0) -> str:
        px, py, pang = self._resolve_pickup_values(x, y, rotate_angle)
        self.arm.place_silicon_chip_on_exfoliation_stage(px, py, pang)
        return f"✅ Silicon chip placed on stage from ({px}, {py}) at {pang}°."

class AtomLeftArmPlaceRawCarrier(BaseWorkflow):
    name = "atom_left_arm_place_raw_carrier"
    description = "Pick up a raw crystal carrier from storage and place it on the exfoliation stage."
    inputs = {
        "x": {"type": "number", "description": "Source X coordinate (auto-selected if omitted).", "nullable": True},
        "y": {"type": "number", "description": "Source Y coordinate (auto-selected if omitted).", "nullable": True},
        "rotate_angle": {"type": "number", "description": "Placement rotation angle (defaults to DB value).", "nullable": True},
        "qr_message": {"type": "string", "description": "Optional QR message filter for selecting a raw carrier.", "nullable": True},
    }
    output_type = "string"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pickup_selector = SelectChipPickupPosition()

    def _resolve_pickup_values(
        self,
        x: Optional[float],
        y: Optional[float],
        rotate_angle: Optional[float],
        qr_message: Optional[str],
    ) -> tuple[float, float, float]:
        px = float(x) if x is not None else None
        py = float(y) if y is not None else None
        pang = float(rotate_angle) if rotate_angle is not None else None

        if px is None or py is None or pang is None:
            coords = self._pickup_selector.forward("raw_crystal_carrier", qr_message=qr_message)
            auto_x, auto_y, auto_angle = _parse_xy_angle(coords)
            if px is None:
                px = auto_x
            if py is None:
                py = auto_y
            if pang is None:
                pang = auto_angle

        if px is None or py is None:
            raise RuntimeError("Carrier pickup coordinates unavailable. Provide x/y or ensure carrier inventory exists.")
        if pang is None:
            pang = 0.0

        return float(px), float(py), float(pang)

    def forward(
        self,
        x: Optional[float],
        y: Optional[float],
        rotate_angle: Optional[float] = 0.0,
        qr_message: Optional[str] = None,
    ) -> str:
        px, py, pang = self._resolve_pickup_values(x, y, rotate_angle, qr_message)
        self.arm.place_raw_crystal_carrier_on_exfoliation_stage(px, py, pang)
        return f"✅ Raw carrier placed on stage from ({px}, {py}) at {pang}°."

class AtomLeftArmStoreSiliconChip(BaseWorkflow):
    name = "atom_left_arm_store_silicon_chip"
    description = "Pick up silicon chip from exfoliation stage and store it to target location."
    inputs = {
        "x": {"type": "number", "description": "Target X coordinate.", "nullable": True},
        "y": {"type": "number", "description": "Target Y coordinate.", "nullable": True}
    }
    output_type = "string"

    def forward(self, x: Optional[float], y: Optional[float]) -> str:
        self.arm.store_exfoliation_silicon_chip(x, y)
        return f"✅ Silicon chip stored to ({x}, {y})."

class AtomLeftArmStoreRawCarrier(BaseWorkflow):
    name = "atom_left_arm_store_raw_carrier"
    description = "Pick up raw crystal carrier from exfoliation stage and store it to target location."
    inputs = {
        "x": {"type": "number", "description": "Target X coordinate.", "nullable": True},
        "y": {"type": "number", "description": "Target Y coordinate.", "nullable": True}
    }
    output_type = "string"

    def forward(self, x: Optional[float], y: Optional[float]) -> str:
        self.arm.store_raw_crystal_carrier(x, y)
        return f"✅ Raw carrier stored to ({x}, {y})."

class AtomLeftArmMassage(BaseWorkflow):
    name = "atom_left_arm_massage"
    description = "Perform massage action on the exfoliation stage to ensure tape adhesion."
    inputs = {
        "num_massages": {
            "type": "integer",
            "description": "Number of massage stroke rounds per row. Default 1.",
            "default": 1,
            "nullable": True,
        }
    }
    output_type = "string"

    def forward(self, num_massages: Optional[int] = 1) -> str:
        rounds = int(num_massages) if num_massages is not None else 1
        self.arm.massage(num_massages=rounds)
        return f"✅ Massage completed (rounds={rounds})."

class AtomLeftArmRotateNose(BaseWorkflow):
    name = "atom_left_arm_rotate_nose"
    description = "Rotate the robot arm nose."
    inputs = {"angle": {"type": "number", "description": "Angle in degrees.", "nullable": True}}
    output_type = "string"

    def forward(self, angle: Optional[float]) -> str:
        self.arm.rotate_nose(angle)
        return f"✅ Nose rotated by {angle}°."

class AtomLeftArmMoveTo(BaseWorkflow):
    name = "atom_left_arm_move_to"
    description = "Move the robot arm to a specific (X, Y, Z) coordinate."
    inputs = {
        "x": {"type": "number", "description": "Target X coordinate.", "nullable": True},
        "y": {"type": "number", "description": "Target Y coordinate.", "nullable": True},
        "z": {"type": "number", "description": "Target Z coordinate.", "nullable": True},
        "speed": {"type": "integer", "description": "Movement speed (default 5000).", "default": 5000, "nullable": True}
    }
    output_type = "string"

    def forward(self, x: Optional[float], y: Optional[float], z: Optional[float], speed: Optional[int] = 5000) -> str:
        if x is None or y is None or z is None:
             return "❌ Error: X, Y, and Z coordinates are required."
        self.arm.move_to(x, y, z, speed=speed)
        return f"✅ Robot arm moved to ({x}, {y}, {z}) at speed {speed}."

class AtomLeftArmVacuumOn(BaseWorkflow):
    name = "atom_left_arm_vacuum_on"
    description = "Turn ON the robot arm vacuum (suction cup) to pick up items."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.arm.vacuum_on()
        return "✅ Robot Arm Vacuum ON (Suction Active)."

class AtomLeftArmVacuumOff(BaseWorkflow):
    name = "atom_left_arm_vacuum_off"
    description = "Turn OFF the robot arm vacuum (suction cup) to release items."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.arm.vacuum_off()
        return "✅ Robot Arm Vacuum OFF (Items Released)."
