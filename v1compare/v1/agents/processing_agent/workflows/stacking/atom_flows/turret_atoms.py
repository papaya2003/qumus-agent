from __future__ import annotations

from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class _TurretAtom(BaseWorkflow):
    def _require_turret(self) -> bool:
        if self.turret is None:
            print("❌ Turret controller unavailable.")
            return False
        return True


class AtomTurretRotate(_TurretAtom):
    name = "atom_turret_rotate"
    description = "Rotate the microscope turret to the requested position index."
    inputs = {"position": {"type": "integer", "description": "Turret position index."}}
    output_type = "string"

    def forward(self, position: int) -> str:
        if not self._require_turret():
            return "❌ Turret controller unavailable."
        self.turret.rotate_to_position(int(position))
        return f"✅ Turret moved to position {position}."
