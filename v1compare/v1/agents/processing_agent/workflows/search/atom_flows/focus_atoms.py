from __future__ import annotations

from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class _FocusAtom(BaseWorkflow):
    def _require_focus(self) -> bool:
        if self.focus is None:
            print("❌ Focus controller unavailable.")
            return False
        return True


class AtomFocusSetZero(_FocusAtom):
    name = "atom_focus_set_zero"
    description = "Define the current focus position as the zero reference."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_focus():
            return "❌ Focus controller unavailable."
        self.focus.set_zero()
        return "✅ Focus zeroed."


class AtomFocusMoveRelative(_FocusAtom):
    name = "atom_focus_move_relative"
    description = "Move the focus relative to the current position."
    inputs = {
        "delta": {"type": "number", "description": "Relative focus steps."},
        "wait": {"type": "boolean", "description": "Wait for focus to stop.", "default": True, "nullable": True},
    }
    output_type = "string"

    def forward(self, delta: float, wait: bool = True) -> str:
        if not self._require_focus():
            return "❌ Focus controller unavailable."
        if hasattr(self.focus, "move_relative_um"):
            self.focus.move_relative_um(float(delta))
            if wait:
                factor = float(getattr(self.focus, "wait_factor_s_per_um", 0.0))
                if factor > 0:
                    import time
                    time.sleep(abs(float(delta)) * factor)
            return f"✅ Focus moved by {float(delta):.3f} um."
        self.focus.move_relative(int(round(delta)), wait=wait)
        return f"✅ Focus moved by {int(round(delta))} steps."
