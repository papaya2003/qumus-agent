import serial
import time
import threading
from typing import Optional

from common.services.instrument_status_service import get_instrument_status_service

"""
This is a class of function to control the heating system (TP3005P power supply), which connects to two heaters used to heat up the exfoliation stage.
Different voltage represents different temperature of exfoliation stage.
During the exfoliation process, after applying tape on new chips, we set the output voltage (e.g., 5V corresponds to 110°C) and fixed the current=2A.
The heating system is turned on after massage action and turned off before the tape exfoliator performs exfoliation.
In the future the agent may explore appropriate temperature and voltage for different materials.
"""


class TP3005PController:
    def __init__(self, port: str = "COM8", baudrate: int = 9600):
        self.ser = serial.Serial(port, baudrate=baudrate, timeout=1)
        self._lock = threading.Lock()
        time.sleep(0.5)  # wait port to be stable
        print(f"🔗 Power supply serial connected on {port} at {baudrate} baud.")
        self._status = get_instrument_status_service()
        self._true_state = {"voltage": None, "power_on": False}
        self._update_state(event="power_supply_init")

    def send_command(self, command: str):
        """
        Set a correct format of command.
        """
        full_cmd = command + "/n"
        with self._lock:
            self.ser.reset_input_buffer()
            self.ser.write(full_cmd.encode())
            self.ser.flush()
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors="ignore").strip()
                    if line:
                        print(f"[PowerSupply] << {line}")
                        if line.lower().startswith("ok") or line.endswith("ok"):
                            break
                else:
                    time.sleep(0.05)

    def turn_on(self):
        """
        Turn on the heating system output. Call this function after setting the voltage to start heating the exfoliation stage.
        """
        self.send_command("OUTPUT1:")
        self._true_state["power_on"] = True
        self._update_state(event="power_supply_on")

    def turn_off(self):
        """
        Turn off the heating system output. Call this function when we want to stop heating the exfoliation stage.
        """
        self.send_command("OUTPUT0:")
        self._true_state["power_on"] = False
        self._update_state(event="power_supply_off")

    def set_output(self, voltage: Optional[float]):
        """
        Set output voltage for the heating system, fix current to 2.000A. Higher voltage increases temperature, lower voltage decreases temperature.
        """
        if voltage is None:
            raise ValueError("Voltage must be provided")
        # current = 2.0
        # voltage = power / current
        v_cmd = f"VSET1:{voltage:05.2f}"
        #i_cmd = "ISET1:2.000"
        self.send_command(v_cmd)
        #sself.send_command(i_cmd)
        self._true_state["voltage"] = float(voltage)
        self._update_state(event="power_supply_voltage")

    def _update_state(self, event: str):
        self._status.update_true_instrument(
            {"heating": dict(self._true_state)},
            event=event,
        )

    def close(self):
        """
        Close the port connection to the heating system controller.
        """
        self.ser.close()
        self._true_state["connection"] = "closed"
        self._update_state(event="power_supply_connection_closed")
