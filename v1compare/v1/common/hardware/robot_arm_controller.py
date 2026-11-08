import math
import os
import time
import serial
import re
import threading
from typing import Optional, Tuple

from common.services.instrument_status_service import get_instrument_status_service


class RobotArmController:
    OK_PATTERN = re.compile(r"\bok\b", re.IGNORECASE)
    ERR_PATTERN = re.compile(r"(?:^|[\r\n])(?:error|alarm|unknown command|!!)", re.IGNORECASE)
    NO_OK_CMDS = "M888"

    def __init__(self, port="COM7", baudrate=115200, instrument_key: str = "robot_arm_left"):
        self.lock = threading.Lock()
        self.ser = serial.Serial(port, baudrate, timeout=0.1, write_timeout=2)
        time.sleep(2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        print(f"🔗 Robot arm serial connected on {port} at {baudrate} baud.")
        self.send_gcode("G21")  # use millimeter unit
        self.send_gcode("G90")  # absolute coordinate
        self.send_gcode("M17")  # power on X,Y,Z
        self._status = get_instrument_status_service()
        self._instrument_key = instrument_key
        self._last_pose = {"x": None, "y": None, "z": None}
        self._nose_angle = 0.0
        self._update_position(event="robot_arm_init")

    def _read_until_ok(self, timeout: float = 10.0) -> str:
        """
        Wait for returning ok. Ensure all the motions complete correctly.
        """
        t0 = time.time()
        buf = ""
        while True:
            n = self.ser.in_waiting
            if n:
                chunk = self.ser.read(n).decode(errors="ignore")
                buf += chunk
                # renew timing
                t0 = time.time()
                if "busy" in buf.lower():
                    pass
                if self.ERR_PATTERN.search(buf):
                    raise RuntimeError(f"Firmware error: {buf.strip()}")
                if self.OK_PATTERN.search(buf):
                    return buf
            else:
                if time.time() - t0 > timeout:
                    raise TimeoutError("Timeout waiting for 'ok'")
                time.sleep(0.003)

    def send_gcode(self, cmd: str, *, expect_ok: bool | None = None, timeout: float = 3.0, interline_delay: float = 0.0):
        """
        This function is sending G-code to robot arm, which is a communicatable language.
        """
        if expect_ok is None:
            prefix = cmd.strip().split()[0].upper() if cmd.strip() else ""
            expect_ok = prefix not in self.NO_OK_CMDS
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write((cmd.strip() + "\r\n").encode())
            print(f"Sent: {cmd}")
            if expect_ok:
                _ = self._read_until_ok(timeout=timeout)
            if interline_delay > 0:
                time.sleep(interline_delay)

    def _query_position(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        with self.lock:
            try:
                self.ser.reset_input_buffer()
                self.ser.write(b"M114\r\n")
                deadline = time.time() + 1.0
                x = y = z = None
                while time.time() < deadline:
                    if self.ser.in_waiting:
                        line = self.ser.readline().decode(errors="ignore").strip()
                        if not line:
                            continue
                        match = re.search(r"X:([\-\d\.]+)\s+Y:([\-\d\.]+)\s+Z:([\-\d\.]+)", line)
                        if match:
                            x = float(match.group(1))
                            y = float(match.group(2))
                            z = float(match.group(3))
                        if "ok" in line.lower():
                            break
                    else:
                        time.sleep(0.01)
                return x, y, z
            except Exception:
                return None, None, None

    def _update_position(self, *, event: str) -> None:
        px, py, pz = self._query_position()
        if px is not None:
            self._last_pose = {"x": px, "y": py, "z": pz}
        payload = {
            self._instrument_key: {
                "position": self._last_pose,
                "nose_angle": self._nose_angle,
            }
        }
        self._status.update_true_instrument(payload, event=event)

    def get_exfoliation_stage_position(self) -> Tuple[float, float]:
        """
        Get the current best estimate of the Exfoliation Stage center (X, Y) in mm.
        Uses fixed default (0, 262) unless opt-in dynamic stage targeting is enabled.
        """
        x_target, y_target = 0.0, 256.0
        use_detected = os.getenv("USE_DETECTED_EXFOLIATION_STAGE", "").strip().lower() in {"1", "true", "yes", "on"}
        if not use_detected:
            return x_target, y_target
        try:
            status = self._status.get_latest_status("exfoliation_stage")
            if status and "position" in status:
                pos = status["position"]
                tx = float(pos.get("x", 0.0))
                ty = float(pos.get("y", 0.0))
                # Validate range to avoid wild motions
                if -20 <= tx <= 20 and 200 <= ty <= 320:
                    x_target, y_target = tx, ty
                    # print(f"Using dynamic Exfoliation Stage position: ({x_target:.1f}, {y_target:.1f})")
                else:
                    print(f"Dynamic Exfoliation Stage ({tx}, {ty}) out of safe range. Using default ({x_target}, {y_target}).")
        except Exception as exc:
            print(f"Failed to read dynamic stage position: {exc}. Using default.")
        
        return x_target, y_target
    
    def get_transfer_stage_position(self) -> Tuple[float, float]:
        """
        Get the current best estimate of the Transfer/Search Stage center (X, Y) in mm.
        Uses fixed default (0, 308) unless opt-in dynamic stage targeting is enabled.
        """
        x_target, y_target = 10, 300.0
        use_detected = os.getenv("USE_DETECTED_TRANSFER_STAGE", "").strip().lower() in {"1", "true", "yes", "on"}
        if not use_detected:
            return x_target, y_target
        try:
            status = self._status.get_latest_status("transfer_stage")
            if status and "position" in status:
                pos = status["position"]
                tx = float(pos.get("x", x_target))
                ty = float(pos.get("y", y_target))
                if tx == 0.0 and ty == 0.0:
                    return x_target, y_target
                x_target, y_target = tx, ty
        except Exception as exc:
            print(f"Failed to read transfer stage position: {exc}. Using default.")
        return x_target, y_target
    def move_to(self, x: float, y: float, z: float, speed: int = 5000):
        """
        Move the robot arm to a specific (X, Y, Z) coordinate.
        """
        self.send_gcode(f"G0 X{x} Y{y} Z{z} F{speed}")
        self.send_gcode("M400")
        print(f"Moved to ({x}, {y}, {z})")
        self._update_position(event="robot_arm_moved")

    def home_to_exfoliation_stage(self):
        """
        This function lets the robot arm home to a safe "parking" position (X0, Y220, Z95).
        This position is chosen to not obstruct the camera view of the stage.
        """
        self.send_gcode(f"G0 X0 Y220 Z95 F5000")
        self.send_gcode("M400")
        print(f"Homed to safe position (0,220,95)")
        self._update_position(event="robot_arm_homed")

    def home_to_search_stage(self):
        """
        This function lets the robot arm home to a safe "parking" position (X0, Y270, Z140).
        This position is chosen to not obstruct the camera view of the stage.
        """
        self.send_gcode(f"G0 X0 Y260 Z140 F5000")
        self.send_gcode("M400")
        print(f"Homed to safe position (0,260,140)")
        self._update_position(event="robot_arm_homed")

    def pick_up_item(self):
        self.send_gcode("M888 P2")
        self.send_gcode("M1000")
        self.send_gcode("G4 P300")
        self.send_gcode("M400")
        print("Item has been picked up")
        self._update_position(event="robot_arm_pick")

    def place_item(self):
        self.send_gcode("M888 P2")
        self.send_gcode("M1002")
        self.send_gcode("M1001")
        self.send_gcode("G4 P300")
        self.send_gcode("M1003")
        self.send_gcode("M400")
        print("Item has been placed.")
        self._update_position(event="robot_arm_place")

    def place_silicon_chip_on_exfoliation_stage(self, x: float, y: float, rotate_angle: float = 0.0):
        # Go to dynamic stage position for placement
        sx, sy = self.get_exfoliation_stage_position()
        
        self.send_gcode(f"G0 X{x} Y{y} Z30 F5000")
        self.send_gcode("G0 Z15 F5000")
        self.pick_up_item()
        self.send_gcode("G0 Z30 F5000")
        
        self.home_to_exfoliation_stage()
        
        self.rotate_nose(angle_deg=rotate_angle)
        
        self.send_gcode(f"G0 X{sx + 10} Y{sy} Z80 F5000") # dynamic placement on exfoliation stage
        self.place_item()
        self.send_gcode("G0 Z95 F1000")
        self.home_to_exfoliation_stage()
        self.send_gcode("M400")
        print(f"Moved target item onto exfoliation stage ({sx},{sy}) with rotation {rotate_angle}°.")
        self._update_position(event="robot_arm_place_silicon_chip")

    def place_silicon_chip_on_transfer_stage(self, x: float, y: float, rotate_angle: float = 0.0):
        # Go to dynamic stage position for placement
        sx, sy = self.get_transfer_stage_position()
        
        self.send_gcode(f"G0 X{x} Y{y} Z30 F5000")
        self.send_gcode("G0 Z15 F5000")
        self.pick_up_item()
        self.send_gcode("G0 Z30 F5000")
        
        self.home_to_search_stage()
        
        self.rotate_nose(angle_deg=rotate_angle)
        
        self.send_gcode(f"G0 X{sx - 10} Y{sy} Z132 F5000") # dynamic placement on transfer stage
        self.place_item()
        self.send_gcode("G0 Z140 F1000")
        self.home_to_search_stage()
        self.send_gcode("M400")
        print(f"Moved target item onto transfer stage ({sx},{sy}) with rotation {rotate_angle}°.")
        self._update_position(event="robot_arm_place_silicon_chip")
        
    def place_raw_crystal_carrier_on_exfoliation_stage(self, x: float, y: float, rotate_angle: float = 0.0):
        sx, sy = self.get_exfoliation_stage_position()
        
        self.send_gcode(f"G0 X{x} Y{y} Z30 F5000")
        self.send_gcode("G0 Z15 F5000")
        self.pick_up_item()
        self.send_gcode("G0 Z30 F5000")
        
        self.home_to_exfoliation_stage()
        
        self.rotate_nose(angle_deg=rotate_angle)
        # Dynamic placement
        self.send_gcode(f"G0 X{sx} Y{sy - 27} Z85 F5000") # Approach offset based on original logic 
        self.place_item()
        self.send_gcode("G0 Z95 F1000")
        self.home_to_exfoliation_stage()
        self.send_gcode("M400")
        print(f"Moved target item onto exfoliation stage ({sx},{sy}) with rotation {rotate_angle}°.")
        self._update_position(event="robot_arm_place_raw_carrier")

    def place_stamp_on_transfer_arm(self):
        self.send_gcode(f"G0 X254 Y190 Z150 F5000")
        self.send_gcode("G0 Z16 F5000")
        self.pick_up_item()
        self.send_gcode("G0 Z150 F5000")
        self.send_gcode("M400")
        self.send_gcode("G0 X200 Y275 Z150 F5000")
        self.send_gcode("M400")
        # self.home_to_search_stage()
        self.rotate_nose(angle_deg=50)
        self.send_gcode("G0 X42 Y275 Z150 F5000")
        self.send_gcode("G0 Z134 F5000")
        self.place_item()
        self.send_gcode("M400")
        self.send_gcode("G0 Z150 F5000")
        self.home_to_search_stage()
        self.send_gcode("M400")
        print(f"Loaded stamp onto transfer arm.")

    def store_exfoliation_silicon_chip(self, x: float, y: float):
        self.home_to_exfoliation_stage()
        sx, sy = self.get_exfoliation_stage_position()
        v1x, v1y = (x + 10), (y - 10)
        v2x, v2y = (sx + 12), (sy)

        n1 = math.hypot(v1x, v1y)
        n2 = math.hypot(v2x, v2y)
        if n1 == 0 or n2 == 0:
            raise RuntimeError(f"Cannot compute nose rotation: zero-length vector (v1=({v1x},{v1y}), v2=({v2x},{v2y})).")

        cosang = (v1x * v2x + v1y * v2y) / (n1 * n2)
        cosang = max(-1.0, min(1.0, cosang))  # clamp for numerical stability
        angle_deg = math.degrees(math.acos(cosang))   # angle between v1 and v2
        complement_pos = abs(90.0 - angle_deg)
        # Move to pick up from stage (dynamic)
        self.send_gcode(f"G0 X{sx + 8} Y{sy} Z95 F5000")
        self.send_gcode("G0 Z80 F1000")
        self.pick_up_item()
        self.home_to_exfoliation_stage()
        self.send_gcode(f"G0 X{x + 10} Y{y - 10} Z30 F5000")
        self.rotate_nose(angle_deg=complement_pos)
        self.send_gcode("G0 Z15 F5000")
        self.place_item()
        self.send_gcode("G0 Z30 F5000")
        self.home_to_exfoliation_stage()
        self.send_gcode("M400")
        print("Stored silicon chip.")
        self._update_position(event="robot_arm_store_silicon")

    def store_search_silicon_chip(self, x: float, y: float, rotate_angle: Optional[float] = None):
        self.home_to_search_stage()
        sx, sy = self.get_transfer_stage_position()
        if rotate_angle is None:
            v1x, v1y = (x + 5), (y - 10)
            v2x, v2y = (sx - 10), (sy)

            n1 = math.hypot(v1x, v1y)
            n2 = math.hypot(v2x, v2y)
            if n1 == 0 or n2 == 0:
                raise RuntimeError(
                    f"Cannot compute nose rotation: zero-length vector (v1=({v1x},{v1y}), v2=({v2x},{v2y}))."
                )

            cosang = (v1x * v2x + v1y * v2y) / (n1 * n2)
            cosang = max(-1.0, min(1.0, cosang))  # clamp for numerical stability
            angle_deg = math.degrees(math.acos(cosang))   # angle between v1 and v2
            complement_pos = abs(90.0 - angle_deg)
        else:
            complement_pos = rotate_angle
        # Move to pick up from stage (dynamic)
        self.send_gcode(f"G0 X{sx - 10} Y{sy} Z140 F5000")
        self.send_gcode("G0 Z130 F1000")
        self.pick_up_item()
        self.home_to_search_stage()
        self.send_gcode(f"G0 X{x} Y{y} Z30 F5000")
        self.rotate_nose(angle_deg=-complement_pos)
        self.send_gcode("G0 Z14 F5000")
        self.place_item()
        self.send_gcode("G0 Z30 F5000")
        self.home_to_search_stage()
        self.send_gcode("M400")
        print("Stored search silicon chip.")
        self._update_position(event="robot_arm_store_silicon")

    def store_raw_crystal_carrier(self, x: float, y: float):
        sx, sy = self.get_exfoliation_stage_position()
        v1x, v1y = x, y
        v2x, v2y = sx, (sy - 27)

        n1 = math.hypot(v1x, v1y)
        n2 = math.hypot(v2x, v2y)
        if n1 == 0 or n2 == 0:
            raise RuntimeError(f"Cannot compute nose rotation: zero-length vector (v1=({v1x},{v1y}), v2=({v2x},{v2y})).")

        cosang = (v1x * v2x + v1y * v2y) / (n1 * n2)
        cosang = max(-1.0, min(1.0, cosang))  # clamp for numerical stability
        angle_deg = math.degrees(math.acos(cosang))   # angle between v1 and v2
        complement_pos = abs(90.0 - angle_deg)
        # Move to pick up from stage (dynamic)
        self.send_gcode(f"G0 X{sx} Y{sy - 27} Z95 F5000")
        self.send_gcode("G0 Z82 F1000")
        self.pick_up_item()
        self.home_to_exfoliation_stage()
        self.send_gcode(f"G0 X{x} Y{y} Z30 F5000")
        self.rotate_nose(angle_deg=-complement_pos)
        self.send_gcode("G0 Z15 F5000")
        self.place_item()
        self.send_gcode("G0 Z30 F5000")
        self.send_gcode("M400")
        print("Stored raw crystal carrier.")
        self._update_position(event="robot_arm_store_raw_carrier")

    def rotate_nose(self, angle_deg: float):
        self.send_gcode("M888 P6")
        self.send_gcode("M2100")
        self.send_gcode(f"M2101 R{int(angle_deg)}")
        print(f"Rotated nose by {angle_deg} degrees")
        try:
            self._nose_angle = float(angle_deg)
        except (TypeError, ValueError):
            pass
        self._update_position(event="robot_arm_nose_rotate")

    def massage(self, num_massages: int = 1):
        self.home_to_exfoliation_stage()
        sx, sy = self.get_exfoliation_stage_position()
        
        # Massage centered on dynamic stage position (+32mm Y offset logic preserved from 275->307)
        mx, my = sx, sy + 42
        rounds = int(num_massages)
        if rounds <= 0:
            raise ValueError("num_massages must be a positive integer.")
        
        self.send_gcode(f"G0 X{mx} Y{my} Z95 F5000")
        self.send_gcode(f"G0 X{mx - 13} Y{my} Z74 F5000")

        for idx, y_offset in enumerate(range(0, 16, 3)):
            massage_y = my + y_offset
            for _ in range(rounds):
                    self.send_gcode(f"G1 X{mx + 15} Y{massage_y} F500")
                    self.send_gcode(f"G1 X{mx - 13} Y{massage_y} F500")

        self.send_gcode(f"G0 X{mx} Y{my} Z95 F5000")
        self.home_to_exfoliation_stage()
        self.send_gcode("M400")
        print("Massage process finished")
        self._update_position(event="robot_arm_massage")

    def vacuum_on(self):
        """Turn ON the robot arm vacuum (pick)."""
        self.send_gcode("M888 P2")
        self.send_gcode("M1000")
        print("Robot arm vacuum turned ON.")
        self._update_position(event="robot_arm_vacuum_on")

    def vacuum_off(self):
        """Turn OFF the robot arm vacuum (release)."""
        self.send_gcode("M888 P2")
        self.send_gcode("M1003")
        print("Robot arm vacuum turned OFF.")
        self._update_position(event="robot_arm_vacuum_off")

    def close(self):
        self.ser.close()
        print("Serial connection closed.")
        self._status.update_true_instrument({self._instrument_key: {"connection": "closed"}}, event="robot_arm_connection_closed")
