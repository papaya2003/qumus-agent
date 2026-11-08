import serial
import time
from typing import Optional

from common.services.instrument_status_service import get_instrument_status_service

"""
This is a class of function to control a Relay in Auto Exfoliation System. This relay controls a solenoid valve that connect a pump and vacuum chamber of exfoliation
stage. After robot arm put chips or mother tapes on exfoliation stage and before the exfoliation process start, we need the relay on, which means the solenoid valve is
open, the vacuum can attach the chips or solenoid valve to exfoliation stage. When we need to use robot arm to pick up chips or mother tapes on exfoliation stage and move them, we need 
the relay off, which means the solenoid valve is closed, the chips or mother tapes on exfoliation stage won't get sucked on the exfoliation stage.
"""


class RelayController:
    def __init__(self, port="COM4", baud=9600):
        """Set the port connection."""
        self.ser = serial.Serial(port, baudrate=baud, timeout=1)
        time.sleep(0.5)  # wait for stable
        print(f"🔗 Vacuum serial connected on {port} at {baud} baud.")
        self._status = get_instrument_status_service()
        self._update_state("unknown", confirmed=False, raw=None)

    def _read_response(self, timeout: float = 0.5) -> Optional[str]:
        deadline = time.time() + timeout
        buf = []
        while time.time() < deadline:
            if self.ser.in_waiting:
                line = self.ser.readline().decode(errors="ignore").strip()
                if line:
                    buf.append(line)
                    if line.lower().startswith("ok") or line.lower().endswith("ok"):
                        break
            else:
                time.sleep(0.02)
        return "\n".join(buf) if buf else None

    def _update_state(self, state: str, confirmed: bool, raw: Optional[str]) -> None:
        payload = {
            "vacuum": {
                "state": state,
                "confirmed": confirmed,
            }
        }
        if raw:
            payload["vacuum"]["raw_reply"] = raw
        self._status.update_true_instrument(payload, event="vacuum_state_update")

    def on(self):
        """This function lets the relay on, which means solenoid valve is open, and the silicon chips or raw crystal carriers will be held firmly on exfoliation stage by vacuum suction."""
        self.ser.write(b"AT+CH1=1\r\n")
        print("relay on (solenoid valve on)")
        time.sleep(0.1)
        reply = self._read_response()
        confirmed = reply is not None and ("CH1=1" in reply or "on" in reply.lower())
        self._update_state("on", confirmed, reply)

    def off(self):
        """This function lets the relay off, which means solenoid valve is closed, and the silicon chips or raw crystal carriers won't be held by vacuum suction on exfoliation stage. Can be picked up by robot arm."""
        self.ser.write(b"AT+CH1=0\r\n")
        print("relay off (solenoid valve off)")
        time.sleep(0.1)
        reply = self._read_response()
        confirmed = reply is not None and ("CH1=0" in reply or "off" in reply.lower())
        self._update_state("off", confirmed, reply)

    def close(self):
        """This function close the port connection."""
        self.ser.close()
        self._update_state("closed", confirmed=True, raw=None)
