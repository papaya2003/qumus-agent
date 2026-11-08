from __future__ import annotations

from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class _SampleStageAtom(BaseWorkflow):
    def _require_stage(self) -> bool:
        if self.stage is None:
            print("❌ Stage controller unavailable.")
            return False
        return True


class AtomSampleStageSetDirection(_SampleStageAtom):
    name = "atom_sample_stage_set_direction"
    description = "Set the fast scan direction (in degrees) for the XY stage."
    inputs = {"angle": {"type": "number", "description": "Direction angle in degrees."}}
    output_type = "string"

    def forward(self, angle: float) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."
        self.stage.set_direction(float(angle))
        return f"✅ Stage direction set to {angle:.1f}°."


class AtomSampleStageSetHome(_SampleStageAtom):
    name = "atom_sample_stage_set_home"
    description = "Record the current XY location as the home reference."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."
        self.stage.set_home()
        positions: dict[str, float] = {}
        if hasattr(self.stage, "get_display"):
            for ch in ("X", "Y", "Z", "L"):
                try:
                    positions[ch] = float(self.stage.get_display(ch))
                except Exception:
                    continue
        elif hasattr(self.stage, "get_pos"):
            try:
                pos = self.stage.get_pos()
                positions["X"] = float(pos[0])
                positions["Y"] = float(pos[1])
            except Exception:
                pass

        if positions:
            self._update_stage({"stage": {"home": positions}}, event="stage_set_home")
            summary = ", ".join(f"{k}={int(v)}" for k, v in positions.items())
            return f"✅ Stage home updated ({summary})."

        return "✅ Stage home updated."


class AtomSampleStageSetChipDims(_SampleStageAtom):
    name = "atom_sample_stage_set_chip_dims"
    description = "Configure the chip dimensions (in inches) for snake scanning."
    inputs = {
        "long_edge_inch": {"type": "number", "description": "Length along the fast scan axis."},
        "short_edge_inch": {"type": "number", "description": "Width along the slow axis."},
    }
    output_type = "string"

    def forward(self, long_edge_inch: float, short_edge_inch: float) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."
        self.stage.set_chip_dims(float(long_edge_inch), float(short_edge_inch))
        return f"✅ Stage chip dims set to {long_edge_inch}in × {short_edge_inch}in."


class AtomSampleStageMoveTo(_SampleStageAtom):
    name = "atom_sample_stage_move_to"
    description = "Move the stage to an absolute XY location (in controller steps)."
    inputs = {
        "x": {"type": "number", "description": "Target X steps."},
        "y": {"type": "number", "description": "Target Y steps."},
        "wait": {"type": "boolean", "description": "Wait until stop.", "default": True, "nullable": True},
    }
    output_type = "string"

    def forward(self, x: float, y: float, wait: bool = True) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."
        self.stage.move_to([int(round(x)), int(round(y))], wait=wait)
        return f"✅ Stage moved to ({int(round(x))}, {int(round(y))})."


class AtomSampleStageMoveRelative(_SampleStageAtom):
    name = "atom_sample_stage_move_relative"
    description = "Move the stage relative to the current position (in steps)."
    inputs = {
        "dx": {"type": "number", "description": "Relative X steps."},
        "dy": {"type": "number", "description": "Relative Y steps."},
        "wait": {"type": "boolean", "description": "Wait until stop.", "default": False, "nullable": True},
    }
    output_type = "string"

    def forward(self, dx: float, dy: float, wait: bool = False) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."
        self.stage.move_relative(int(round(dx)), int(round(dy)), wait=wait)
        return f"✅ Stage moved relative ({int(round(dx))}, {int(round(dy))})."


class AtomSampleStageMoveHome(_SampleStageAtom):
    name = "atom_sample_stage_move_home"
    description = "Return the stage to the stored home position."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."
        self.stage.move_home(wait=True)
        return "✅ Stage returned home."


class AtomSampleStageMoveLHome(_SampleStageAtom):
    name = "atom_sample_stage_move_l_home"
    description = "Move the sample stage x,y,L axis to 0 mm."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."
        if not hasattr(self.stage, "move_L_axis_to_mm"):
            return "❌ Stage controller missing L-axis mm control."
        self.stage.move_L_axis_to_mm(0.0, wait=True)
        self.stage.move_axis_to("x", 0, wait=True)
        self.stage.move_axis_to("y", 0, wait=True)
        return "✅ Sample stage L moved to home position."


class AtomSampleStageMoveToMicroscope(_SampleStageAtom):
    name = "atom_sample_stage_move_to_microscope"
    description = "Move the sample stage L axis to 80 mm, stage X axis to -15 mm (microscope position)."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."
        if not hasattr(self.stage, "move_L_axis_to_mm"):
            return "❌ Stage controller missing L-axis mm control."
        self.stage.move_L_axis_to_mm(80.0, wait=True)
        self.stage.move_axis_to("x", -12, wait=True) 
        self.stage.move_axis_to("y", 8, wait=True)
        return "✅ Sample stage L moved to microscope position."


class AtomSampleStageWaitUntilStop(_SampleStageAtom):
    name = "atom_sample_stage_wait_until_stop"
    description = "Block until the stage reports it has stopped moving."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."
        self.stage.wait_until_stop()
        return "✅ Stage motion complete."


class AtomSampleStageGetPosition(_SampleStageAtom):
    name = "atom_sample_stage_get_position"
    description = "Read the current XY stage position from the controller."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_stage():
            return "❌ Stage controller unavailable."

        x = None
        y = None
        if hasattr(self.stage, "get_display"):
            try:
                x = float(self.stage.get_display("X"))
                y = float(self.stage.get_display("Y"))
            except Exception:
                x = None
                y = None
        if (x is None or y is None) and hasattr(self.stage, "get_pos"):
            try:
                pos = self.stage.get_pos()
                x = float(pos[0])
                y = float(pos[1])
            except Exception:
                x = None
                y = None

        if x is None or y is None:
            return "❌ Failed to read stage position."

        return f"✅ Stage position: X={x:.3f}, Y={y:.3f}."
