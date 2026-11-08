"""Focus motor controller shim for the common hardware namespace."""

from __future__ import annotations
import time
from typing import Optional, Union
import serial

Number = Union[int, float]

class FocusController:
    """
    Minimal, robust serial driver for the Thorlabs MFC1 focus motor.
    (Standalone implementation, migrated from legacy focus.py)
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        *,
        address: int = 1,
        motor_index: int = 0,
        steps_per_um: float = 92.5,   
        max_chunk_um: float = 30000.0,       # max distance per command
        wait_factor_s_per_um: float = 0.006,
        timeout: float = 4.0,
    ):
        self.address = int(address) & 0xFF
        self.motor_index = int(motor_index) & 0xFF
        self.steps_per_um = float(steps_per_um)
        self.max_chunk_um = float(max_chunk_um)
        self.wait_factor_s_per_um = float(wait_factor_s_per_um)

        # Software-side position estimate (in µm)
        self.position_um: float = 0.0

        # Open the serial port
        try:
            self.serial_port = serial.Serial(
                port=port,
                baudrate=baudrate,
                stopbits=serial.STOPBITS_ONE,
                parity=serial.PARITY_NONE,
                timeout=timeout,
            )
        except serial.SerialException as e:
            raise RuntimeError(f"Could not open focus port {port}: {e}")

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _checksum(cmd_bytes: list[int]) -> int:
        """8-bit checksum: sum of all bytes (0..7) modulo 256."""
        return sum(cmd_bytes) & 0xFF

    def _start_frame(self, stop: bool) -> list[int]:
        """Build the 4-byte start of the command frame."""
        if stop:
            instr = 3  # stop
        else:
            instr = 4  # move (relative)
        return [self.address, instr, 1, self.motor_index]

    def _encode_distance_um(self, dist_um: Number) -> list[int]:
        """Convert a relative distance in µm into 4 bytes (big-endian)."""
        dist_um = float(dist_um)
        microsteps = int(abs(dist_um) * self.steps_per_um)

        if dist_um == 0:
            dir_bit = 1
        else:
            dir_bit = 1 if dist_um > 0 else 0

        if abs(dist_um) > self.max_chunk_um:
            return [0, 0, 0, 0]

        if dir_bit == 1:
            val = microsteps
        else:
            val = (2 ** 32 - microsteps)

        return [
            (val >> 24) & 0xFF,
            (val >> 16) & 0xFF,
            (val >> 8) & 0xFF,
            val & 0xFF,
        ]

    def _send_command(self, frame: list[int]) -> Optional[Union[int, str]]:
        """Send one full command frame (9 bytes) and read 9-byte reply."""
        if len(frame) != 9:
            raise ValueError("Command frame must be exactly 9 bytes.")

        if self.serial_port.is_open:
            self.serial_port.write(bytearray(frame))
            answer = self.serial_port.read(9)
        else:
            return "Port closed"

        if len(answer) < 3:
            return "No response"

        status = answer[2]
        if status == 1:
            return "Checksum incorrect"
        elif status == 2:
            return "Invalid Command"
        elif status == 3:
            return "Wrong Type"
        elif status == 4:
            return "Invalid Value"
        elif status == 5:
            return "Configuration EEPROM locked"
        elif status == 6:
            return "Command not available"
        else:
            return status

    # ------------------------------------------------------------------
    # Public movement API
    # ------------------------------------------------------------------

    def move_relative_um(self, dist_um: Number) -> float:
        """Move by a relative distance in micrometers (µm)."""
        value_bytes = self._encode_distance_um(dist_um)
        start = self._start_frame(stop=False)
        cmd = start + value_bytes
        cmd.append(self._checksum(cmd))

        _ = self._send_command(cmd)

        if abs(float(dist_um)) <= self.max_chunk_um:
            self.position_um += float(dist_um)

        return self.position_um

    def move_relative_mm(self, dist_mm: Number) -> float:
        """Move by a relative distance in millimeters."""
        return self.move_relative_um(float(dist_mm) * 1000.0)

    def move_to_um(self, target_um: Number, wait: bool = True) -> float:
        """Move to an absolute position (in µm)."""
        target_um = float(target_um)
        dist = self.position_um - target_um
        tol = 1e-6

        while abs(dist) > tol:
            if abs(dist) <= self.max_chunk_um:
                step = dist
            else:
                margin = 5.0
                step_mag = self.max_chunk_um - margin
                step = step_mag if dist > 0 else -step_mag

            self.move_relative_um(step)

            if wait:
                time.sleep(self.wait_factor_s_per_um * abs(step))

            dist = self.position_um - target_um

        return self.position_um

    def move_to_mm(self, target_mm: Number, wait: bool = True) -> float:
        """Move to an absolute position in millimeters."""
        return self.move_to_um(float(target_mm) * 1000.0, wait=wait)

    # ------------------------------------------------------------------
    # Position / zero / stop
    # ------------------------------------------------------------------

    def get_position_um(self) -> float:
        """Return the software-side position estimate in µm."""
        return self.position_um

    def get_position_mm(self) -> float:
        """Return the software-side position estimate in mm."""
        return self.position_um / 1000.0

    def set_zero(self) -> float:
        """Set the current position as zero (software only)."""
        self.position_um = 0.0
        return self.position_um

    def home(self, wait: bool = True) -> float:
        """Move back to software zero (position 0 µm)."""
        return self.move_to_um(0.0, wait=wait)

    def stop(self) -> Optional[Union[int, str]]:
        """Send a STOP command to the controller."""
        value_bytes = self._encode_distance_um(0.0)
        start = self._start_frame(stop=True)
        cmd = start + value_bytes
        cmd.append(self._checksum(cmd))
        return self._send_command(cmd)

    # ------------------------------------------------------------------
    # Resource management
    # ------------------------------------------------------------------

    def close(self):
        """Close the underlying serial port."""
        if hasattr(self, 'serial_port') and self.serial_port and self.serial_port.is_open:
            self.serial_port.close()

    def kill(self):
        """Alias for close."""
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
