"""Shared entry point for the microscope turret controller."""

from __future__ import annotations

import serial
import time

class TurretController:
    """
    Microscope Turret Controller (Standalone).
    Controls the motorized nosepiece to switch objectives.
    """

    def __init__(self, port, baudrate=9600, timeout=3, terminator='\r'):
        self.comport = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.terminator = terminator
        self.position = 1

        try:
            self.serial_port = serial.Serial(
                port=self.comport,
                baudrate=self.baudrate,
                timeout=self.timeout
            )
        except serial.SerialException as e:
            raise RuntimeError(f"Could not open turret port {port}: {e}")

    def rotate_to_position(self, pos: int, wait: bool = False) -> bool:
        """
        Rotate the turret to the specified position index (1-based).
        """
        cmd = f'cRDC{pos}{self.terminator}'
        if self.serial_port.is_open:
            self.serial_port.write(cmd.encode())
        
        if wait:
            msg = self.response()
            # 'oRDC' is likely the success acknowledgement
            return msg == 'oRDC'
        else:
            return True

    def response(self) -> str:
        """Read a line from the serial port."""
        if self.serial_port.is_open:
            response = self.serial_port.readline().decode().strip()
            return response
        return ""

    def kill(self):
        """Close the connection."""
        if hasattr(self, 'serial_port') and self.serial_port.is_open:
            self.serial_port.close()
    
    def close(self):
        """Standardized alias for kill."""
        self.kill()
