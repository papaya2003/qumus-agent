import subprocess
import re
import threading
import time
from typing import Mapping, Optional


class TemperatureController:
    """
    Minimal Dracal temperature tool class (smolagent-friendly).
    Provides:
      - read_temperature()
      - wait_until_reach_temperature()   # NEW tool
    """

    def __init__(
        self,
        exe_path: str = r"C:\Users\Qumus_PC\Desktop\DracalView\dracal-usb-get.exe",
        *,
        default_sensor_serial: str = "E30559",
        sensor_serial_by_stage: Optional[Mapping[str, str]] = None,
        default_stage: str = "exfoliation",
        default_index: str = "0",
        unit: str = "C",
    ):
        self.lock = threading.Lock()
        self.exe_path = exe_path
        self.default_sensor_serial = str(default_sensor_serial)
        self.sensor_serial_by_stage = {
            "exfoliation": self.default_sensor_serial,
            "transfer": "E30753",
        }
        if sensor_serial_by_stage:
            self.sensor_serial_by_stage.update({str(k).lower(): str(v) for k, v in sensor_serial_by_stage.items()})
        self.default_stage = str(default_stage).lower()
        self.default_index = str(default_index)
        self.unit = unit

    def _resolve_sensor_serial(
        self,
        *,
        sensor_serial: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> str:
        if sensor_serial:
            return str(sensor_serial)
        if stage:
            key = str(stage).lower()
            if key in self.sensor_serial_by_stage:
                return self.sensor_serial_by_stage[key]
            raise ValueError(f"Unknown temperature stage '{stage}'. Known stages: {sorted(self.sensor_serial_by_stage.keys())}")
        if self.default_stage in self.sensor_serial_by_stage:
            return self.sensor_serial_by_stage[self.default_stage]
        return self.default_sensor_serial

    def read_temperature(
        self,
        index: Optional[str] = None,
        *,
        sensor_serial: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> float:
        """
        Read one temperature sample (Celsius by default).
        """
        idx = self.default_index if index is None else str(index)
        serial = self._resolve_sensor_serial(sensor_serial=sensor_serial, stage=stage)

        with self.lock:
            out = subprocess.check_output(
                [self.exe_path, "-s", serial, "-i", idx, "-T", self.unit, "-x", "2", "-7"],
                stderr=subprocess.STDOUT,
                text=True,
            ).strip()

        m = re.search(r"[-+]?\d+(?:\.\d+)?", out)
        if not m:
            raise RuntimeError(f"Dracal returned non-numeric value: {out!r}")

        return float(m.group())

    def wait_until_reach_temperature(
        self,
        target_T: float,
        *,
        index: Optional[str] = None,
        sensor_serial: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> str:
        """
        Tool: monitor temperature until it reaches/crosses target_T, then stop immediately.

        - If initial temperature >= target_T: returns immediately.
        - Otherwise: loops until last < target_T <= current, then returns.

        Returns a string like: "Reached target 30.0°C (current=30.123°C)".
        """
        idx = self.default_index if index is None else str(index)
        serial = self._resolve_sensor_serial(sensor_serial=sensor_serial, stage=stage)

        interval_s: float = 1.0
        timeout_s: Optional[float] = 3600.0

        # First read (handles "initial already above target")
        t0 = time.time()
        T_start = self.read_temperature(index=idx, sensor_serial=serial)
        if T_start >= target_T:
            return f"✅ Reached target {target_T:.1f}°C (current={T_start:.3f}°C)"

        last_T = T_start

        while True:
            if timeout_s is not None and (time.time() - t0) > timeout_s:
                raise TimeoutError(f"Timeout: did not reach target {target_T:.1f}°C within {timeout_s:.0f}s")

            T = self.read_temperature(index=idx, sensor_serial=serial)
            print(f"Current Temperature: {T} °C")
            # Cross detection 
            if last_T < target_T <= T:
                return f"✅ Reached target {target_T:.1f}°C (current={T:.3f}°C)"

            last_T = T
            time.sleep(interval_s)

    def wait_until_drop_temperature(
        self,
        target_T: float,
        *,
        index: Optional[str] = None,
        sensor_serial: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> str:
        """
        Tool: monitor temperature until it drops to/below target_T, then stop immediately.

        - If initial temperature <= target_T: returns immediately.
        - Otherwise: loops until last > target_T >= current, then returns.

        Returns a string like: "Dropped to target 30.0°C (current=29.876°C)".
        """
        idx = self.default_index if index is None else str(index)
        serial = self._resolve_sensor_serial(sensor_serial=sensor_serial, stage=stage)

        interval_s: float = 1.0
        timeout_s: Optional[float] = 3600.0

        # First read (handles "initial already below target")
        t0 = time.time()
        T_start = self.read_temperature(index=idx, sensor_serial=serial)
        if T_start <= target_T:
            return f"✅ Dropped to target {target_T:.1f}°C (current={T_start:.3f}°C)"

        last_T = T_start

        while True:
            if timeout_s is not None and (time.time() - t0) > timeout_s:
                raise TimeoutError(f"Timeout: did not drop to target {target_T:.1f}°C within {timeout_s:.0f}s")

            T = self.read_temperature(index=idx, sensor_serial=serial)

            # Cross detection 
            if last_T > target_T >= T:
                return f"✅ Dropped to target {target_T:.1f}°C (current={T:.3f}°C)"

            last_T = T
            time.sleep(interval_s)
