from typing import Optional
from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from common.logic.exfoliation_setpoints import validate_tape_peel_speed_v

# Printer/Exfoliator Atoms

class AtomExfoliatorPowerOn(BaseWorkflow):
    name = "atom_exfoliator_power_on"
    description = "Power on the tape exfoliator motors (X, Y, Z)."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.printer.power_on()
        return "✅ Exfoliator powered ON."

class AtomExfoliatorPowerOff(BaseWorkflow):
    name = "atom_exfoliator_power_off"
    description = "Power off the tape exfoliator motors."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.printer.power_off()
        return "✅ Exfoliator powered OFF."

class AtomTapeApply(BaseWorkflow):
    name = "atom_tape_apply"
    description = "Apply tape to the item on the exfoliation stage using the exfoliator."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.printer.apply_tape()
        return "✅ Tape applied."

class AtomTapePeel(BaseWorkflow):
    name = "atom_tape_peel"
    description = "Peel/Exfoliate tape from the item on the exfoliation stage."
    inputs = {
        "tape_peel_speed_v": {
            "type": "number",
            "description": "Tape peel arc feedrate (G-code F value). Allowed range 50 to 500. Default 500.",
            "default": 500.0,
            "nullable": True,
        }
    }
    output_type = "string"

    def forward(self, tape_peel_speed_v: Optional[float] = 500.0) -> str:
        peel_v = validate_tape_peel_speed_v(tape_peel_speed_v)
        self.printer.exfoliate_tape(tape_peel_speed_v=peel_v)
        return f"✅ Tape exfoliated (speed={peel_v:.3f})."

class AtomExfoliatorHome(BaseWorkflow):
    name = "atom_exfoliator_home"
    description = "Move the tape exfoliator nozzle to home position (X=0, Y=0, Z=0). The home position should have been set previously using G92 X0 Y0 Z0."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        self.printer.home()
        return "✅ Exfoliator homed to (0, 0, 0)."

# Motor Atoms

class AtomTapeRelease(BaseWorkflow):
    name = "atom_tape_release"
    description = "Release fresh tape and recycle used tape (Motor 1 CCW, Motor 2 resets)."
    inputs = {"angle": {"type": "number", "description": "Angle to release (degrees).", "default": 3600.0, "nullable": True}}
    output_type = "string"

    def forward(self, angle: Optional[float] = 3600.0) -> str:
        self.motors.release_new_tape(angle)
        return f"✅ Released new tape ({angle}°)."

class AtomTapeAdjust(BaseWorkflow):
    name = "atom_tape_adjust"
    description = "Adjust tape position using motors based on CV feedback."
    inputs = {"angle": {"type": "number", "description": "Signed angle in degrees (>0 Motor1 CCW, <0 Motor2 CW).", "nullable": True}}
    output_type = "string"

    def forward(self, angle: Optional[float]) -> str:
        self.motors.adjust_mother_tape_position(angle)
        return f"✅ Tape position adjusted by {angle}°."
