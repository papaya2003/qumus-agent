import serial
from typing import Optional

from common.services.instrument_status_service import get_instrument_status_service
import time

"""
This is a class of function that control two motors in Auto Exfoliation system. The two motors are attached to the two rolls of scotch tape used for exfoliation,
so when they rotate, they will also drive the tape to rotate together. The system will use the exposed tape between the two rolls of tape for exfoliation,
so the role of these two motors is：
    1. Rotate motors to adjust the position of materials on the tape, to make sure it aligns well with chips before exfoliation process. When rotate one motor to collect
    tape from one side, this motor is power on, the motor on the other side should be power off, to make sure the tape is tight between two motors. When material is too
    close to motor2, motor1 will rotate CCW; when material is too close to motor1, motor2 will rotate CW.
    2. When moving these two motors by using 3D printer to complete the exfoliation process, two motors should be both power on.
    3. After a exfoliation process finished, motor1 will rotate CCW to collect used tape, motor2 should be power off at the meanwhile, to make sure the tape keep tight.
    All the rotation angle will be determine by computer vision tool.
These class of function is calling the command in Arduino, which already upload the function on Arduino board that control two motors by A4988.
"""


class DualMotorController:
    def __init__(self, port: str, baud: int = 115200):
        """
        Set up the port connection of arduino board.
        """
        self._status = get_instrument_status_service()
        self._state = {
            "motor1_power": True,
            "motor2_power": True,
            "last_action": "init",
        }
        self.ser = serial.Serial(port, baud, timeout=1)
        time.sleep(2)  # wait the port be stable
        self._state["motor1_power"] = False
        self._state["motor2_power"] = False
        self._update_status()
        self.motor1_on()
        self.motor2_on()
        print(f"🔗 Motor serial connected on {port} at {baud} baud.")
        self._update_status()

    def close(self):
        """
        Close the port connection of arduino board.
        """
        self.ser.close()
        print("Serial connection closed.")
        self._state["connection"] = "closed"
        self._update_status()

    def send(self, cmd: str):
        """
        Setting a correct format to send command.
        """
        self.ser.write((cmd + "\n").encode())
        self.ser.flush()
        time.sleep(0.05)
        while self.ser.in_waiting:
            response = self.ser.readline().decode().strip()
            print(f"Arduino response: {response}")
        self._state["last_command_raw"] = cmd
        self._update_status()

    def cmd(self, motor_id: int, cw: bool, angle: float) -> str:
        """
        Rotation Command of two motors, write in the number of motor, and CW or CCW, and the angle to rotate.
        """
        direction = "CW" if cw else "CCW"
        return f"MOTOR{motor_id}_{direction}_{angle}"

    def _update_status(self):
        self._status.update_true_instrument(
            {"motors": {k: v for k, v in self._state.items() if k != "last_command_raw"}},
            event="motors_state_update",
            context={"raw": self._state.get("last_command_raw")},
        )

    def motor1_on(self):
        """
        Motor 1 power on.
        """
        self.send("MOTOR1_ON")
        print("MOTOR1 power on")
        self._state["motor1_power"] = True
        self._state["last_action"] = "motor1_on"
        self._update_status()

    def motor1_off(self):
        """
        Motor 1 power off.
        """
        self.send("MOTOR1_OFF")
        print("MOTOR1 power off")
        self._state["motor1_power"] = False
        self._state["last_action"] = "motor1_off"
        self._update_status()

    def motor2_on(self):
        """
        Motor 2 power on.
        """
        self.send("MOTOR2_ON")
        print("MOTOR2 power on")
        self._state["motor2_power"] = True
        self._state["last_action"] = "motor2_on"
        self._update_status()

    def motor2_off(self):
        """
        Motor 2 power off.
        """
        self.send("MOTOR2_OFF")
        print("MOTOR2 power off")
        self._state["motor2_power"] = False
        self._state["last_action"] = "motor2_off"
        self._update_status()

    def release_new_tape(self, angle: float):
        """
        This function lets the tape motors release new tape and recycle used tape after finishing a exfoliation process and before we start to exfoliate on next silicon chip.
        """
        try:
            angle = float(angle)
        except (TypeError, ValueError):
            raise ValueError("angle must be a number (degrees)")
        if angle <= 0:
            raise ValueError("angle must be positive (degrees)")
        self.motor1_on()
        self.motor2_off()
        command = self.cmd(1, cw=False, angle=angle)
        self.send(command)
        self.motor2_on()
        print("New tape has been released.")
        self._state["last_action"] = "release_new_tape"
        self._state["last_angle"] = angle
        self._update_status()

    def adjust_mother_tape_position(self, angle: float):
        """
        This function lets the tape motors to adjust the position of the crystal on the tape (the mother tape).
        The computer vision tool will give a angle to rotate, if angle>0, motor1 rotate ccw; if angle<0, motor2 rotate cw.
        """
        try:
            angle = float(angle)
        except (TypeError, ValueError):
            raise ValueError("angle must be a number (degrees)")
        if angle > 0:
            self.motor1_on()
            self.motor2_off()
            command = self.cmd(1, cw=False, angle=angle)
            self.send(command)
            print(f"Motor1 CCW rotate {angle}°")
            self.motor2_on()
            self._state["last_action"] = "adjust_motor1"
            self._state["last_angle"] = angle
            self._update_status()
        elif angle < 0:
            self.motor2_on()
            self.motor1_off()
            command = self.cmd(2, cw=True, angle=abs(angle))
            self.send(command)
            print(f"Motor2 CW rotate {abs(angle)}°")
            self.motor1_on()
            self._state["last_action"] = "adjust_motor2"
            self._state["last_angle"] = angle
            self._update_status()
        else:
            print("No adjustion needed.")
