"""Canonical hardware port defaults shared across agents and UI."""

from __future__ import annotations

from typing import Dict

DEFAULT_HARDWARE_PORTS: Dict[str, str] = {
    "robot_arm_port_left": "COM7",   # Left robot arm (exfoliation)
    "robot_arm_port_right": "COM23",  # Right robot arm (search/stacking)
    "exfoliation_vacuum_relay_port": "COM4",   # Exfoliation vacuum relay
    "exfoliation_fan_relay_port": "COM15",  # Exfoliation fan relay
    "sample_stage_vacuum_relay_port": "COM14",  # Sample stage vacuum relay
    "transfer_arm_vacuum_relay_port": "COM13",  # Transfer arm vacuum relay
    "transfer_heater_relay_port": "COM17",  # Transfer-stage heater relay
    "exfoliation_heater_relay_port": "COM18",  # Exfoliation-stage heater relay
    "motor_port": "COM3",    # Dual tape motors (Arduino)
    "power_port": "COM8",    # TP3005P power supply
    "printer_port": "COM22",  # Tape exfoliator printer
    "stage_port": "COM9",    # XY stage
    "turret_port": "COM10",  # Microscope turret
    "focus_port": "COM11",   # Thorlabs focus motor
    "microscope_camera_index": "0",     # UVCHAM camera index
    "transfer_x_serial": "26007081",
    "transfer_y_serial": "26007120",
    "transfer_z_serial": "26006985",
    "transfer_goni_serial": "27271252",
    "transfer_L_port": "COM19",
    "temp_exe_path": r"C:\Users\Qumus_PC\Desktop\DracalView\dracal-usb-get.exe",  # Dracal temperature tool
    "temp_sensor_exfoliation_serial": "E30559",
    "temp_sensor_transfer_serial": "E30753",
}


def get_default_hardware_ports() -> Dict[str, str]:
    """Return a copy of the default hardware port mapping."""

    return dict(DEFAULT_HARDWARE_PORTS)
