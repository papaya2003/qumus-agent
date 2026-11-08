import serial
import time
import math
import re
import threading
from typing import Optional

from common.logic.exfoliation_setpoints import validate_tape_peel_speed_v
from common.services.instrument_status_service import get_instrument_status_service

"""This is a class of function to control tape exfoliator. This tape exfoliator is a part of auto-exfoliation system, where an exfoliation stage and one end
of tape for exfoliation are fixed on the print bed, the extruder nozzle holds another end of tape. We need to control the X and Z motor of 
tape exfoliator to move the nozzle to do complete exfoliation motion. There are three components in exfoliation process: clean tape held by tape exfoliator, 
original material on mother tapes (human will prepare them), new silicon chips. The exfoliation process can be separated into two tasks:
1. Exfoliate material from original material on mother tapes onto clean tapes that held by tape exfoliator. 
First step: Apply clean tape to original material on mother tapes by an arc motion of extruder nozzle.
Second step: Massage. This will be finished by robot arm tool.
Third step: Exfoliate clean tape off, and there will be material left on the tape, the clean tape is not clean anymore. This is a reverse motion of applying tape of extruder nozzle.
2. Exfoliate material from tape with materials onto new silicon chips.
First step: Apply tape with material to new chips by an arc motion of extruder nozzle.
Second step: Heat up. This will be finished by heating kit.
Third step: Massage. This will be finished by robot arm tool.
Fourth step: Exfoliate tape off, and there will be material left on chips, and these chips are called exfoliated chips. This is a reverse motion of applying tape of extruder nozzle.
The motion of apply tape and exfoliate tape is fixed now, more parameters will be open for agent to tune and explore in the future.
"""


# Shared PrinterController Definition
class SharedPrinterController:
    def __init__(self, port="COM22", baudrate=115200):
        """
        When calling the class, enter the actual port, while the baudrate is default number of machine,
        which has no need to be changed. And also let the motors of tape exfoliator power on.
        """
        self.port = port
        self.baudrate = baudrate
        self.ser = serial.Serial(port, baudrate, timeout=2)
        time.sleep(2)
        self._lock = threading.Lock()
        self._status = get_instrument_status_service()
        self.send_gcode("M17 X Y Z")  # power on the motors
        self._update_state(power_on=True, last_action="power_on", busy=False)

    def send_gcode(self, cmd, wait=0.05, expect_ok: bool = True, log: bool = True):
        """
        This function is sending G-code to tape exfoliator, which is a communicatable language.
        """
        with self._lock:
            self.ser.reset_input_buffer()
            self.ser.write((cmd + "\n").encode())
            if log:
                print(f"[Tape Exfoliator] >> {cmd}")
            deadline = time.time() + 5.0
            while True:
                if self.ser.in_waiting:
                    response = self.ser.readline().decode(errors="ignore").strip()
                    if not response:
                        continue
                    if log:
                        print(f"[Tape Exfoliator] << {response}")
                    lower = response.lower()
                    if "error" in lower:
                        raise RuntimeError(f"Printer error: {response}")
                    if "busy" in lower:
                        deadline = time.time() + 5.0
                        continue
                    if not expect_ok:
                        break
                    if lower.startswith("ok") or lower.endswith(" ok") or lower == "ok" or " ok" in lower:
                        break
                else:
                    if time.time() > deadline:
                        raise TimeoutError(f"Timeout waiting for printer ack after {cmd}")
                    time.sleep(0.02)
            time.sleep(wait)

    def _update_state(self, *, power_on: Optional[bool] = None, last_action: Optional[str] = None, busy: Optional[bool] = None) -> None:
        status_payload: dict[str, dict] = {"exfoliator": {}}
        if power_on is not None:
            status_payload["exfoliator"]["power_on"] = power_on
        if last_action is not None:
            status_payload["exfoliator"]["last_action"] = last_action
        if busy is not None:
            status_payload["exfoliator"]["busy"] = busy
        self._status.update_true_instrument(status_payload, event="exfoliator_state_update")

    def power_on(self):
        """
        This function lets the X,Y,Z motors to power on, so the extruders and print bed cannot be moved manually, can avoid hardware error caused
        by human error.
        """
        self.send_gcode("M17 X Y Z")
        print("tape exfoliator power on")
        self._update_state(power_on=True, last_action="power_on", busy=False)

    def get_position(self):
        """
        Query the current position of the tape exfoliator nozzle.
        Returns a dict with 'x', 'y', 'z' keys (in mm), or None if query fails.
        """
        try:
            with self._lock:
                self.ser.reset_input_buffer()
                self.ser.write(b"M114\n")
                time.sleep(0.1)
                response = ""
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if self.ser.in_waiting:
                        line = self.ser.readline().decode(errors="ignore").strip()
                        response += line + " "
                        # Look for pattern like "X:123.45 Y:67.89 Z:10.11"
                        if "X:" in line and "Y:" in line:
                            break
                    time.sleep(0.05)
                
                # Parse the response
                import re
                match = re.search(r'X:([+-]?\d+\.?\d*)\s+Y:([+-]?\d+\.?\d*)\s+Z:([+-]?\d+\.?\d*)', response)
                if match:
                    return {
                        'x': float(match.group(1)),
                        'y': float(match.group(2)),
                        'z': float(match.group(3))
                    }
                else:
                    print(f"Warning: Could not parse position from: {response}")
                    return None
        except Exception as e:
            print(f"Error getting position: {e}")
            return None

    def power_off(self):
        """
        This function lets the X,Y,Z motors to power off, we typically don't take this action except when machine doesn't work correctly,
        we let's it power off and human will come to solve the problem.
        """
        self.send_gcode("M18 X Y Z")
        print("tape exfoliator power off")
        self._update_state(power_on=False, last_action="power_off", busy=False)


    def apply_tape(self):
        """
        This function controls the extruder nozzle to complete a fixed motion (an arc in the X-Z plane) to apply tape on chip or raw crystal carrier.
        This step is explained in general description. It calls the function draw_arc to complete this motion.
        """
        pos = self.get_position()
        if pos is None:
            raise RuntimeError("Cannot apply tape: failed to get printer position (M114 parse failed).")
        if pos["x"] < -50 or pos["z"] < -20:
            raise RuntimeError(
            f"Printer position does not support apply_tape (x={pos['x']:.3f}, z={pos['z']:.3f})."
            )
        self.send_gcode("G91")  # use relative coordinates
        self.send_gcode("G1 X-140 F1000")
        self.send_gcode("M400")  # Don't do other motion before finish
        self._update_state(busy=True, last_action="apply_tape_start")
        cx, cz = 80, -170
        x0, z0 = 0, 0
        x1, z1 = -110, -160
        v= 1000
        self.draw_arc(x0, z0, x1, z1, cx, cz, v, clockwise=True)
        self.send_gcode("M400")
        self.send_gcode("G90")  # return to absolute coordinates for subsequent commands
        print("The tape has been applied.")
        self._update_state(busy=False, last_action="apply_tape_complete")

    def home(self):
        """
        Move the tape exfoliator nozzle to the home position (X=0).
        The home position should be set using G92 X0 when physically positioned correctly.
        """
        self.send_gcode("G90")  # use absolute coordinates
        self.send_gcode("G1 X0 Z0")  # move to origin at 2000 mm/min
        self.send_gcode("M400")  # wait for motion to complete
        print("Tape exfoliator homed")
        self._update_state(last_action="homed")


    def exfoliate_tape(self, tape_peel_speed_v: float):
        """
        This function controls the extruder nozzle to complete a fixed motion to exfoliate the tape that we applied on chip or raw crystal carrier.
        This step is explained in general description. It also calls the function draw_arc to complete this motion.
        """
        self.send_gcode("G91")  # use relative coordinates
        self._update_state(busy=True, last_action="exfoliate_start")
        cx, cz = 190, -10
        x0, z0 = 0, 0
        x1, z1 = 110, 160
        v = validate_tape_peel_speed_v(tape_peel_speed_v)
        self.draw_arc(x0, z0, x1, z1, cx, cz, v, clockwise=False)
        self.send_gcode("M400")
        self.send_gcode("G1 X140 F1000")
        self.send_gcode("M400")
        self.send_gcode("G90")  # return to absolute coordinates for subsequent commands
        print("Exfoliation complete")
        self._update_state(busy=False, last_action="exfoliate_complete")

    def draw_arc(self, x0, z0, x1, z1, cx, cz, v, clockwise=True):
        """
        This function lets the extruder nozzle of tape exfoliator to do an arc motion in X-Z plane. x0,y0 represent original point, x1,z1 represent
        final point, and cx, cz represent center of arc.
        """

        def angle(x, z):
            return math.atan2(z - cz, x - cx)

        theta0 = angle(x0, z0)
        theta1 = angle(x1, z1)
        if clockwise and theta1 < theta0:
            theta1 += 2 * math.pi
        elif not clockwise and theta1 > theta0:
            theta1 -= 2 * math.pi
        segments = 100
        prev_x, prev_z = x0, z0
        r = math.hypot(x0 - cx, z0 - cz)
        print(f"[Tape Exfoliator] Executing arc motion ({segments} segments, F{v}).")
        for i in range(1, segments + 1):
            theta = theta0 + (theta1 - theta0) * i / segments
            x = cx + r * math.cos(theta)
            z = cz + r * math.sin(theta)
            dx = x - prev_x
            dz = z - prev_z
            prev_x, prev_z = x, z
            self.send_gcode(f"G1 X{round(dx, 4)} Z{round(dz, 4)} F{v}", log=False)

    def close(self):
        """
        This function closes the port connection of tape exfoliator.
        """
        self.ser.close()
        self._update_state(power_on=False, last_action="connection_closed", busy=False)
