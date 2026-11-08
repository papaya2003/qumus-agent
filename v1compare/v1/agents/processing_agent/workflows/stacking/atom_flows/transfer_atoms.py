from __future__ import annotations

from agents.processing_agent.workflows.base_workflow import BaseWorkflow


class _TransferAtom(BaseWorkflow):
    def _require_transfer(self) -> bool:
        if self.transfer is None:
            print("❌ Transfer controller unavailable.")
            return False
        return True

    @staticmethod
    def _normalize_axis(axis: str) -> str:
        axis_key = axis.strip().lower()
        if axis_key not in {"x", "y", "z", "goni", "l"}:
            raise ValueError("Axis must be one of: x, y, z, goni, L.")
        return axis_key

    @staticmethod
    def _axis_units(axis_key: str) -> str:
        return "deg" if axis_key == "goni" else "mm"


class AtomTransferConnect(_TransferAtom):
    name = "atom_transfer_connect"
    description = "Connect the transfer stage controller."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        self.transfer.connect()
        return "✅ Transfer controller connected."


class AtomTransferDisconnect(_TransferAtom):
    name = "atom_transfer_disconnect"
    description = "Disconnect the transfer stage controller."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        self.transfer.disconnect()
        return "✅ Transfer controller disconnected."


class AtomTransferHomeAll(_TransferAtom):
    name = "atom_transfer_home_all"
    description = "Home all transfer axes (x, y, z, goni, L)."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        self.transfer.home_all()
        return "✅ Transfer axes homed."


class AtomTransferHomeAxis(_TransferAtom):
    name = "atom_transfer_home_axis"
    description = "Home a single transfer axis."
    inputs = {
        "axis": {"type": "string", "description": "Axis name: x, y, z, goni, or L."}
    }
    output_type = "string"

    def forward(self, axis: str) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        axis_key = self._normalize_axis(axis)
        self.transfer.home(axis_key if axis_key != "l" else "L")
        return f"✅ Transfer axis {axis_key} homed."


class AtomTransferMoveAxisTo(_TransferAtom):
    name = "atom_transfer_move_axis_to"
    description = "Move a transfer axis to an absolute position (mm or deg)."
    inputs = {
        "axis": {"type": "string", "description": "Axis name: x, y, z, goni, or L."},
        "value": {"type": "number", "description": "Target position (mm for linear, deg for goni)."},
    }
    output_type = "string"

    def forward(self, axis: str, value: float) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        axis_key = self._normalize_axis(axis)
        self.transfer.move_axis_to(axis_key, float(value))
        units = self._axis_units(axis_key)
        return f"✅ Transfer axis {axis_key} moved to {float(value):.4f} {units}."


class AtomTransferMoveAxisBy(_TransferAtom):
    name = "atom_transfer_move_axis_by"
    description = "Move a transfer axis by a relative delta (mm or deg)."
    inputs = {
        "axis": {"type": "string", "description": "Axis name: x, y, z, goni, or L."},
        "delta": {"type": "number", "description": "Relative delta (mm for linear, deg for goni)."},
    }
    output_type = "string"

    def forward(self, axis: str, delta: float) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        axis_key = self._normalize_axis(axis)
        self.transfer.move_axis_by(axis_key, float(delta))
        units = self._axis_units(axis_key)
        return f"✅ Transfer axis {axis_key} moved by {float(delta):.4f} {units}."


class AtomTransferJogAxis(_TransferAtom):
    name = "atom_transfer_jog_axis"
    description = "Jog a transfer axis by a step (mm or deg)."
    inputs = {
        "axis": {"type": "string", "description": "Axis name: x, y, z, goni, or L."},
        "step": {"type": "number", "description": "Jog step (mm for linear, deg for goni)."},
    }
    output_type = "string"

    def forward(self, axis: str, step: float) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        axis_key = self._normalize_axis(axis)
        self.transfer.jog_axis(axis_key, float(step))
        units = self._axis_units(axis_key)
        return f"✅ Transfer axis {axis_key} jogged by {float(step):.4f} {units}."


class AtomTransferGetPositions(_TransferAtom):
    name = "atom_transfer_get_positions"
    description = "Read the current positions of all transfer axes."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        positions = self.transfer.positions()
        summary = ", ".join(f"{k}={v:.4f}" for k, v in positions.items())
        return f"✅ Transfer positions: {summary}."


class AtomTransferSetKSTSpeed(_TransferAtom):
    name = "atom_transfer_set_kst_speed"
    description = "Set the speed profile on a KST axis (x, y, z)."
    inputs = {
        "axis": {"type": "string", "description": "Axis name: x, y, or z."},
        "max_vel": {"type": "number", "description": "Max velocity in mm/s."},
        "accel": {"type": "number", "description": "Acceleration in mm/s^2."},
        "min_vel": {"type": "number", "description": "Min velocity in mm/s.", "nullable": True, "default": 0.0},
    }
    output_type = "string"

    def forward(self, axis: str, max_vel: float, accel: float, min_vel: float = 0.0) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        axis_key = self._normalize_axis(axis)
        self.transfer.set_kst_speed(axis_key, float(max_vel), float(accel), float(min_vel))
        return f"✅ KST axis {axis_key} speed set (max={max_vel}, accel={accel})."


class AtomTransferGetKSTSpeed(_TransferAtom):
    name = "atom_transfer_get_kst_speed"
    description = "Read the speed profile for a KST axis (x, y, z)."
    inputs = {
        "axis": {"type": "string", "description": "Axis name: x, y, or z."},
    }
    output_type = "string"

    def forward(self, axis: str) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        axis_key = self._normalize_axis(axis)
        speed = self.transfer.get_kst_speed(axis_key)
        return f"✅ KST axis {axis_key} speed: {speed}."


class AtomTransferSetPDVSpeed(_TransferAtom):
    name = "atom_transfer_set_pdv_speed"
    description = "Set the PDV L-axis continuous speed."
    inputs = {"speed": {"type": "integer", "description": "Speed value (0-16383)."}}
    output_type = "string"

    def forward(self, speed: int) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        self.transfer.set_pdv_speed(int(speed))
        return f"✅ PDV L-axis speed set to {int(speed)}."


class AtomTransferSetPDVMaxSpeed(_TransferAtom):
    name = "atom_transfer_set_pdv_max_speed"
    description = "Set the PDV L-axis maximum speed."
    inputs = {"max_speed": {"type": "integer", "description": "Max speed value (0-16383)."}}
    output_type = "string"

    def forward(self, max_speed: int) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        self.transfer.set_pdv_max_speed(int(max_speed))
        return f"✅ PDV L-axis max speed set to {int(max_speed)}."


class AtomTransferStopPDV(_TransferAtom):
    name = "atom_transfer_stop_pdv"
    description = "Stop the PDV L-axis immediately."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_transfer():
            return "❌ Transfer controller unavailable."
        self.transfer.stop_pdv()
        return "✅ PDV L-axis stopped."
