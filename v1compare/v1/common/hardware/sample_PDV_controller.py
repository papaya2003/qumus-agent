"""Sample PDV stage controller for the Qumus processing hardware.

This module provides a stage controller implementation built on the vendor
kz100_pdv_api, plus high-level snake scan utilities.
"""

from __future__ import annotations

import math
import time
from typing import Optional, Sequence

import numpy as np
from tqdm import tqdm

from .drivers.kz100_pdv_api import (  # noqa: F401
    GetDisplay,
    GetStatus,
    SetDistance,
    SetMaxSpeed,
    SetSpeed,
    Stop,
)

class SamplePDVController:
    """Sample-stage controller for multi-axis motor control with snake scanning.
    
    This class consolidates:
    - Low-level motion control via kz100_pdv_api
    - High-level stage movement and positioning
    - Snake scanning pattern generation and execution
    """

    _ROW_SPACING_STEPS = {
        5: 20000,
        10: 10000,
        20: 5000,
        50: 2000,
        100: 1000,
    }

    def __init__(
        self,
        port: str,
        *,
        magnification: int = 20,
        x_axis: str = "X",
        y_axis: str = "Y",
        z_axis: str = "Z",
        l_axis: str = "L",
        x_y_steps_per_mm: float = 12800.0,
        l_steps_per_mm: float = 3280.0,
        
        x_pos_min_mm: Optional[float] = None,
        x_pos_max_mm: Optional[float] = None,
        y_pos_min_mm: Optional[float] = None,
        y_pos_max_mm: Optional[float] = None,
        z_pos_min_mm: Optional[float] = None,
        z_pos_max_mm: Optional[float] = None,
        l_pos_min_mm: Optional[float] = 0.0,
        l_pos_max_mm: Optional[float] = 120.0,
    ) -> None:
        """Initialize the stage controller.
        
        Args:
            port: Serial port name (e.g., 'COM9' on Windows, '/dev/ttyUSB0' on Linux)
            magnification: Microscope magnification (5, 10, 20, 50, or 100)
            x_axis: Controller channel for X-axis movement (default: "X")
            y_axis: Controller channel for Y-axis movement (default: "Y")
            z_axis: Controller channel for Z-axis movement (default: "Z")
            l_axis: Controller channel for L-axis movement (default: "L")
            x_y_steps_per_mm: Motor steps per millimeter for X/Y (default: 12800.0)
            l_steps_per_mm: Motor steps per millimeter for L (default: 3280.0)
        """
        if magnification not in self._ROW_SPACING_STEPS:
            raise ValueError(f"Unsupported magnification: {magnification}")

        # Basic configuration
        self.com_port = port
        self.mag = magnification

        # Channel mapping (sample coordinate system -> controller channels)
        self.x_axis = x_axis.upper()
        self.y_axis = y_axis.upper()
        self.z_axis = z_axis.upper()
        self.l_axis = l_axis.upper()

        # Unit conversion
        self.x_y_steps_per_mm = float(x_y_steps_per_mm)
        self.steps_per_inch = self.x_y_steps_per_mm * 25.4  # 1 inch = 25.4 mm
        self.l_steps_per_mm = float(l_steps_per_mm)

        # Magnification-dependent step sizes
        self.short_edge_step = self._ROW_SPACING_STEPS[magnification]
        self.long_edge_step = self.short_edge_step * 1.8

        # Optional axis limits (mm). If not set, no limit check is applied.
        self._axis_pos_min_mm = {
            self.x_axis: x_pos_min_mm,
            self.y_axis: y_pos_min_mm,
            self.z_axis: z_pos_min_mm,
            self.l_axis: l_pos_min_mm,
        }
        self._axis_pos_max_mm = {
            self.x_axis: x_pos_max_mm,
            self.y_axis: y_pos_max_mm,
            self.z_axis: z_pos_max_mm,
            self.l_axis: l_pos_max_mm,
        }
        self.l_pos_min_mm = l_pos_min_mm
        self.l_pos_max_mm = l_pos_max_mm

        # Scanning geometry state
        self.home_location = np.zeros(2, dtype=float)  # [x, y]
        self.rotation_matrix = np.eye(2, dtype=float)  # 2x2 rotation matrix
        self.long_edge = None  # Set via set_chip_dims
        self.short_edge = None
        self.coords = []  # Snake path coordinates
        self.n_rows = 0
        self.n_cols = 0
        self.current_pos = np.array([0.0, 0.0], dtype=float)  # Current position [x, y]

    # ========== Hardware control methods ========== #

    def set_speed(self, channel: str, speed: int) -> None:
        """Run the motor at a continuous speed.
        
        Args:
            channel: Axis identifier ('X', 'Y', 'Z', 'L')
            speed: Speed value (-16383 to 16383)
        """
        SetSpeed(self.com_port, channel, int(speed))

    def set_max_speed(self, channel: str, max_speed: int) -> None:
        """Set the maximum speed for an axis.
        
        Args:
            channel: Axis identifier ('X', 'Y', 'Z', 'L')
            max_speed: Maximum speed (0 to 16383)
        """
        SetMaxSpeed(self.com_port, channel, int(max_speed))

    def set_scan_max_speed(self, max_speed_x: int, max_speed_y: int) -> None:
        """Set max speed for the scan axes."""
        self.set_max_speed(self.x_axis, int(max_speed_x))
        self.set_max_speed(self.y_axis, int(max_speed_y))

    def set_distance(self, channel: str, distance: int) -> None:
        """Move the motor by a specified distance.
        
        Args:
            channel: Axis identifier ('X', 'Y', 'Z', 'L')
            distance: Distance in steps (-2^30 to 2^30)
        """
        SetDistance(self.com_port, channel, int(distance))

    def get_display(self, channel: str) -> int:
        """Read the displayed position for an axis.
        
        Args:
            channel: Axis identifier ('X', 'Y', 'Z', 'L')
            
        Returns:
            Current position in steps
        """
        return int(GetDisplay(self.com_port, channel))

    def get_status(self, content: str) -> str:
        """Read controller status.
        
        Args:
            content: 'S' for limit status, 'M' for motion mode, 'H' for home status
            
        Returns:
            Status character
        """
        return str(GetStatus(self.com_port, content))

    def stop(self) -> None:
        """Emergency stop for all axes."""
        Stop(self.com_port)

    def _require_axis(self, axis: str) -> str:
        axis_key = str(axis).upper()
        valid = {
            self.x_axis,
            self.y_axis,
            self.z_axis,
            self.l_axis,
        }
        if axis_key not in valid:
            raise ValueError(
                f"Unknown axis '{axis_key}'. Use one of {sorted(valid)}."
            )
        return axis_key

    def _check_pos_mm(self, axis: str, target_mm: float) -> bool:
        axis_key = self._require_axis(axis)
        min_mm = self._axis_pos_min_mm.get(axis_key)
        max_mm = self._axis_pos_max_mm.get(axis_key)
        if min_mm is None or max_mm is None:
            return True
        if target_mm < min_mm or target_mm > max_mm:
            print(f"Out of range: {axis_key} target {target_mm} mm not in [{min_mm}, {max_mm}]")
            return False
        return True

    def _check_l_pos_mm(self, target_mm: float) -> Optional[str]:
        if self.l_pos_min_mm is None or self.l_pos_max_mm is None:
            return None
        if target_mm < self.l_pos_min_mm or target_mm > self.l_pos_max_mm:
            return (
                f"Out of range: {self.l_axis} target {target_mm} mm not in "
                f"[{self.l_pos_min_mm}, {self.l_pos_max_mm}]"
            )
        return None

    def move_L_axis_to_mm(self, value_mm: float, wait: bool = False) -> Optional[str]:
        target_mm = float(value_mm)
        error = self._check_l_pos_mm(target_mm)
        if error:
            return error
        current_steps = self.get_display(self.l_axis)
        target_steps = int(round(target_mm * self.l_steps_per_mm))
        delta = target_steps - int(round(current_steps))
        if delta != 0:
            self.set_distance(self.l_axis, delta)
        if wait:
            self.wait_until_stop()
        return None

    def move_L_axis_by_mm(self, delta_mm: float, wait: bool = False) -> Optional[str]:
        current_steps = self.get_display(self.l_axis)
        target_mm = (current_steps / self.l_steps_per_mm) + float(delta_mm)
        error = self._check_l_pos_mm(target_mm)
        if error:
            return error
        delta_steps = int(round(float(delta_mm) * self.l_steps_per_mm))
        if delta_steps != 0:
            self.set_distance(self.l_axis, delta_steps)
        if wait:
            self.wait_until_stop()
        return None

    def move_axis_to(self, axis: str, value: float, wait: bool = False) -> None:
        axis_key = self._require_axis(axis)
        if axis_key == self.l_axis:
            self.move_L_axis_to_mm(float(value), wait=wait)
            return
        if not self._check_pos_mm(axis_key, float(value)):
            return
        current_steps = self.get_display(axis_key)
        target_steps = int(round(float(value) * self.x_y_steps_per_mm))
        delta = target_steps - int(round(current_steps))
        if delta != 0:
            self.set_distance(axis_key, delta)
        if wait:
            self.wait_until_stop()

    def move_axis_by(self, axis: str, delta: float, wait: bool = False) -> None:
        axis_key = self._require_axis(axis)
        if axis_key == self.l_axis:
            self.move_L_axis_by_mm(float(delta), wait=wait)
            return
        current_steps = self.get_display(axis_key)
        target_steps = current_steps + int(round(float(delta) * self.x_y_steps_per_mm))
        target_mm = target_steps / self.x_y_steps_per_mm
        if not self._check_pos_mm(axis_key, target_mm):
            return
        delta_steps = int(round(float(delta) * self.x_y_steps_per_mm))
        if delta_steps != 0:
            self.set_distance(axis_key, delta_steps)
        if wait:
            self.wait_until_stop()

    # ========== High-level stage control methods ========== #

    def set_home(self) -> None:
        """Set the current position as the home location (chip reference corner)."""
        x_pos = self.get_display(self.x_axis)
        y_pos = self.get_display(self.y_axis)
        
        self.home_location = np.array([x_pos, y_pos], dtype=float)
        self.current_pos = self.home_location.copy()

    def set_direction(self, angle_deg: float) -> None:
        """Set the snake scan fast-axis direction.
        
        Args:
            angle_deg: Angle in degrees (0 = +X, 90 = +Y, etc.)
        """
        theta = angle_deg * np.pi / 180.0
        self.rotation_matrix = np.array([
            [np.cos(theta), -np.sin(theta)],
            [np.sin(theta), np.cos(theta)],
        ], dtype=float)

    def set_chip_dims(self, long_edge_inch: float, short_edge_inch: float) -> None:
        """Set the chip dimensions for scanning.
        
        Args:
            long_edge_inch: Chip length along fast-scan direction (inches)
            short_edge_inch: Chip width along slow-scan direction (inches)
        """
        self.long_edge = float(long_edge_inch) * self.steps_per_inch
        self.short_edge = float(short_edge_inch) * self.steps_per_inch

    def move_relative(self, dx: float, dy: float, wait: bool = False) -> None:
        """Move relative to current position.
        
        Args:
            dx: X displacement in steps
            dy: Y displacement in steps
            wait: If True, wait for movement to complete
        """
        # Limit checks in mm (if configured)
        x_target_mm = (self.get_display(self.x_axis) + float(dx)) / self.x_y_steps_per_mm
        y_target_mm = (self.get_display(self.y_axis) + float(dy)) / self.x_y_steps_per_mm
        if not self._check_pos_mm(self.x_axis, x_target_mm):
            return
        if not self._check_pos_mm(self.y_axis, y_target_mm):
            return
        # Round to nearest 1000 steps (controller quantization)
        dx_i = int(round(float(dx) / 1000.0) * 1000)
        dy_i = int(round(float(dy) / 1000.0) * 1000)
        
        # Execute X movement
        if dx_i != 0:
            self.set_distance(self.x_axis, dx_i)
        
        # Execute Y movement in chunks (Z channel has 1000 step max)
        if dy_i != 0:
            self.set_distance(self.y_axis, dy_i)
            
        if wait:
            self.wait_until_stop()

    def jogging(self, speed_x: int, speed_y: int, wait: bool = False) -> None:
        """Jog the scan axes at continuous speeds."""
        self.set_speed(self.x_axis, int(speed_x))
        self.set_speed(self.y_axis, int(speed_y))
        if wait:
            self.wait_until_stop()

    def jog_in_direction(self, angle_deg: float, step: float, wait: bool = False) -> None:
        """Jog the stage in a specific direction.
        
        Args:
            angle_deg: Direction angle in degrees
            step: Step size
            wait: If True, wait for movement to complete
        """
        theta = math.radians(float(angle_deg))
        dx = float(step) * math.cos(theta)
        dy = float(step) * math.sin(theta)
        self.move_relative(dx, dy)
        if wait:
            self.wait_until_stop()

    def get_pos(self) -> np.ndarray:
        """Get the current position.
        
        Returns:
            Current position as [x, y] array
        """
        x = self.get_display(self.x_axis)
        y = self.get_display(self.y_axis)
        return np.array([x, y], dtype=float)

    def move_to(self, location: Sequence[float], wait: bool = False) -> None:
        """Move to an absolute position.
        
        Args:
            location: Target position as [x, y]
            wait: If True, wait for movement to complete
        """
        x_target = float(location[0])
        y_target = float(location[1])
        if not self._check_pos_mm(self.x_axis, x_target / self.x_y_steps_per_mm):
            return
        if not self._check_pos_mm(self.y_axis, y_target / self.x_y_steps_per_mm):
            return
        # Sync current position with hardware
        x_now = self.get_display(self.x_axis)
        y_now = self.get_display(self.y_axis)
        self.current_pos = np.array([x_now, y_now], dtype=float)

        # Calculate relative displacement
        dx = int(round(x_target - self.current_pos[0]))
        dy = int(round(y_target - self.current_pos[1]))

        # Execute movement
        if dx != 0:
            self.set_distance(self.x_axis, dx)
        if dy != 0:
            self.set_distance(self.y_axis, dy)

        # Update internal position
        self.current_pos[0] += dx
        self.current_pos[1] += dy

        if wait:
            self.wait_until_stop()

    def move_home(self, wait: bool = False) -> None:
        """Move to the home location."""
        self.move_to(self.home_location, wait=wait)
        self.wait_until_stop()

    def wait_until_stop(self, tol_steps: int = 2, timeout: float = 5.0) -> None:
        """Wait until the stage stops moving.
        
        Args:
            tol_steps: Position tolerance in steps
            timeout: Maximum wait time in seconds
        """
        t0 = time.time()
        x_prev = self.get_display(self.x_axis)
        y_prev = self.get_display(self.y_axis)
        l_prev = None
        if getattr(self, "l_axis", None):
            try:
                l_prev = self.get_display(self.l_axis)
            except Exception:
                l_prev = None

        while True:
            time.sleep(0.05)
            x_now = self.get_display(self.x_axis)
            y_now = self.get_display(self.y_axis)
            l_now = None
            if l_prev is not None:
                try:
                    l_now = self.get_display(self.l_axis)
                except Exception:
                    l_now = None

            l_stopped = True
            if l_prev is not None and l_now is not None:
                l_stopped = abs(l_now - l_prev) <= tol_steps

            if abs(x_now - x_prev) <= tol_steps and abs(y_now - y_prev) <= tol_steps and l_stopped:
                break  # Stage has stopped

            x_prev, y_prev = x_now, y_now
            if l_prev is not None and l_now is not None:
                l_prev = l_now

            if time.time() - t0 > timeout:
                print("wait_until_stop: timeout reached")
                break

    # ========== Snake scanning methods ========== #

    def get_snake(self) -> list[np.ndarray]:
        """Generate snake scan path based on current configuration.
        
        Returns:
            List of [x, y] coordinates for the snake path
        """
        if self.long_edge is None or self.short_edge is None:
            raise RuntimeError("Call set_chip_dims() before get_snake().")

        coords = []

        # Step sizes
        step_x = float(self.long_edge_step)   # Along long edge
        step_y = float(self.short_edge_step)  # Along short edge

        # Calculate grid dimensions
        n_rows = max(1, math.ceil(self.short_edge / step_y))
        n_cols = max(1, math.ceil(self.long_edge / step_x))

        self.n_rows = n_rows
        self.n_cols = n_cols

        c = float(self.rotation_matrix[0, 0])
        s = float(self.rotation_matrix[1, 0])

        home_x = float(self.home_location[0])
        home_y = float(self.home_location[1])
        # Generate grid in chip local coordinates, then transform to stage coordinates
        for i in range(n_rows):
            y_local = i * step_y
            row_points = []

            for j in range(n_cols):
                x_local = j * step_x
                # rotate in chip/view coords
                x_rot = c * x_local - s * y_local
                y_rot = s * x_local + c * y_local

                # map to stage deltas (your correct reasoning)
                dX_stage = -y_rot
                dY_stage = +x_rot
                # Rotate and translate to stage coordinate system
                p_world = np.array([home_x + dX_stage, home_y + dY_stage], dtype=float)
                row_points.append(p_world)

            # Snake pattern: reverse every other row
            if i % 2 == 1:
                row_points.reverse()

            coords.extend(row_points)

        self.coords = coords
        return coords

    def start_snake(self, sf: Optional[callable] = None, wf: Optional[callable] = None, recompute_path: bool = False) -> None:
        """Execute snake scan pattern.
        
        Args:
            sf: Optional callback function to call at each scan point
            wf: Optional callback function to call at each scan point
            recompute_path: If True, regenerate the scan path
        """
        # Ensure scan path exists
        if recompute_path or not self.coords:
            self.get_snake()
        # steps = len(self.coords)
        current_step = 0
        try:
            # Move through snake path
            for idx, coord in enumerate(tqdm(self.coords, desc="Snake scan")):
                current_step += 1

                self.move_to(coord, wait=True)
                # Call callback at each point (e.g., for focus and capture)
                if wf is not None:
                    wf()
                    print("wf here")
                # if current_step % 10 == 0:
                    sf()
                    print("sf here")

        except KeyboardInterrupt:
            print("Snake scan interrupted by user.")

if __name__ == "__main__":
    # Example usage
    stage = SamplePDVController(port="COM9", magnification=10)
    stage.set_home()
    stage.set_chip_dims(long_edge_inch=0.3, short_edge_inch=0.1)
    snake_path = stage.start_snake()
    print(f"Generated snake path with {len(snake_path)} points.")
