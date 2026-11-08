from __future__ import annotations

import csv
import ctypes
import json
import os
import threading
import time
import argparse
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional, Sequence

import cv2

from .camera_controller import CameraController
from .transfer_control_controller import TransferControl, TransferController


DEFAULT_TRANSFER_AXES = ("x", "y", "z")


@dataclass(frozen=True)
class StampMove:
    """One relative stamp-axis move to run during recording."""

    axis: str
    delta_mm: float
    start_s: float = 0.0


@dataclass(frozen=True)
class RecordingResult:
    video_path: str
    frame_csv_path: str
    position_csv_path: str
    manifest_path: str
    frame_count: int


class StampVideoRecorder:
    """
    Record camera frames while timestamping stamp-axis positions.

    The recorder writes:
    - video_path: recorded video
    - *_frames.csv: one row per video frame with frame timestamp and nearest
      sampled transfer-stage positions in mm
    - *_positions.csv: raw transfer-stage position samples in mm with their own
      timestamps
    - *_manifest.json: metadata needed to reconstruct frame/position alignment

    Timestamps use both epoch seconds and monotonic elapsed seconds. Use
    elapsed_s for synchronization inside one recording.
    """

    def __init__(
        self,
        camera: CameraController,
        transfer: TransferControl,
        *,
        axes: Sequence[str] = DEFAULT_TRANSFER_AXES,
        fps: Optional[float] = None,
        sample_hz: float = 30.0,
    ) -> None:
        self.camera = camera
        self.transfer = transfer
        self.axes = self._normalize_axes(axes)
        self.fps = float(fps or camera.fps)
        self.sample_hz = float(sample_hz)
        if self.fps <= 0:
            raise ValueError("fps must be > 0")
        if self.sample_hz <= 0:
            raise ValueError("sample_hz must be > 0")

        self._samples: list[dict[str, float]] = []
        self._sample_lock = threading.Lock()
        self._axis_io_lock = threading.Lock()
        self._motion_lock = threading.Lock()
        self._motion_threads: list[threading.Thread] = []
        self._axis_motion_threads: dict[str, threading.Thread] = {}
        self._stop_event = threading.Event()
        self._record_start_mono = 0.0
        self._record_start_epoch = 0.0
        self._user32 = ctypes.windll.user32 if os.name == "nt" else None

    def record(
        self,
        video_path: str,
        *,
        duration_s: float,
        moves: Optional[Sequence[StampMove]] = None,
        movement_fn: Optional[Callable[[], None]] = None,
        open_live_view: bool = True,
        codec: str = "mp4v",
    ) -> RecordingResult:
        """
        Start live view, record video, move stamp axes, and save alignment data.

        Args:
            video_path: Output video path, usually ending in .mp4 or .avi.
            duration_s: Recording duration in seconds.
            moves: Optional relative transfer-axis moves scheduled by elapsed time.
            movement_fn: Optional user function launched in a background thread.
                Use this for custom movement sequences.
            open_live_view: If True, opens the camera live-view window.
            codec: FourCC codec for OpenCV VideoWriter.
        """
        if duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        if moves and movement_fn:
            raise ValueError("Use either moves or movement_fn, not both.")

        self._ensure_camera_running()
        if open_live_view:
            self.camera.open_live_view()

        first_frame, _, _ = self._snap_frame_with_timestamp(timeout=3.0)
        height, width = first_frame.shape[:2]

        os.makedirs(os.path.dirname(video_path) or ".", exist_ok=True)
        writer = cv2.VideoWriter(
            video_path,
            cv2.VideoWriter_fourcc(*codec),
            self.fps,
            (width, height),
        )
        if not writer.isOpened():
            raise IOError(f"Failed to open video writer: {video_path}")

        base, _ = os.path.splitext(video_path)
        frame_csv_path = f"{base}_frames.csv"
        position_csv_path = f"{base}_positions.csv"
        manifest_path = f"{base}_manifest.json"

        self._samples = []
        self._motion_threads = []
        self._axis_motion_threads = {}
        self._stop_event.clear()
        self._record_start_mono = time.perf_counter()
        self._record_start_epoch = time.time()

        sampler = threading.Thread(target=self._sample_positions_loop, daemon=True)
        sampler.start()

        mover = self._start_motion_thread(moves=moves, movement_fn=movement_fn)

        frame_rows: list[dict[str, float | int | str]] = []
        frame_count = 0
        frame_period = 1.0 / self.fps
        next_frame_t = self._record_start_mono
        last_frame_timestamp_mono: float | None = None

        try:
            while True:
                now_mono = time.perf_counter()
                elapsed_s = now_mono - self._record_start_mono
                if elapsed_s >= duration_s:
                    break

                if now_mono < next_frame_t:
                    time.sleep(min(next_frame_t - now_mono, 0.005))
                    continue

                frame, frame_timestamp_epoch, frame_timestamp_mono = (
                    self._snap_frame_with_timestamp(
                        timeout=max(1.0, frame_period * 2.0),
                        after_mono=last_frame_timestamp_mono,
                    )
                )
                frame_elapsed_s = frame_timestamp_mono - self._record_start_mono

                nearest = self._nearest_sample(frame_timestamp_mono)
                writer.write(frame)
                frame_rows.append(
                    self._build_frame_row(
                        frame_index=frame_count,
                        timestamp_epoch=frame_timestamp_epoch,
                        elapsed_s=frame_elapsed_s,
                        nearest=nearest,
                    )
                )
                frame_count += 1
                last_frame_timestamp_mono = frame_timestamp_mono
                next_frame_t += frame_period
        finally:
            self._stop_event.set()
            sampler.join(timeout=2.0)
            if mover:
                mover.join(timeout=2.0)
            self._join_motion_threads(timeout=2.0)
            writer.release()

        self._write_position_csv(position_csv_path)
        self._write_frame_csv(frame_csv_path, frame_rows)
        self._write_manifest(
            manifest_path,
            video_path=video_path,
            frame_csv_path=frame_csv_path,
            position_csv_path=position_csv_path,
            frame_count=frame_count,
            width=width,
            height=height,
            duration_s=duration_s,
            codec=codec,
        )

        return RecordingResult(
            video_path=os.path.abspath(video_path),
            frame_csv_path=os.path.abspath(frame_csv_path),
            position_csv_path=os.path.abspath(position_csv_path),
            manifest_path=os.path.abspath(manifest_path),
            frame_count=frame_count,
        )

    def record_keyboard_control(
        self,
        video_path: str,
        *,
        xy_step_mm: float = 0.02,
        z_step_mm: float = 0.05,
        window_name: str = "Stamp Keyboard Recording",
        codec: str = "mp4v",
    ) -> RecordingResult:
        """
        Record until q is pressed while controlling stamp axes from the video window.

        Controls:
            hold a/d: X -/+
            hold s/w: Y -/+
            hold f/r: Z -/+
            q: stop recording
        """
        if xy_step_mm <= 0:
            raise ValueError("xy_step_mm must be > 0")
        if z_step_mm <= 0:
            raise ValueError("z_step_mm must be > 0")

        self._ensure_camera_running()
        first_frame, _, _ = self._snap_frame_with_timestamp(timeout=3.0)
        height, width = first_frame.shape[:2]

        os.makedirs(os.path.dirname(video_path) or ".", exist_ok=True)
        writer = cv2.VideoWriter(
            video_path,
            cv2.VideoWriter_fourcc(*codec),
            self.fps,
            (width, height),
        )
        if not writer.isOpened():
            raise IOError(f"Failed to open video writer: {video_path}")

        base, _ = os.path.splitext(video_path)
        frame_csv_path = f"{base}_frames.csv"
        position_csv_path = f"{base}_positions.csv"
        manifest_path = f"{base}_manifest.json"

        self._samples = []
        self._motion_threads = []
        self._axis_motion_threads = {}
        self._stop_event.clear()
        self._record_start_mono = time.perf_counter()
        self._record_start_epoch = time.time()

        sampler = threading.Thread(target=self._sample_positions_loop, daemon=True)
        sampler.start()

        frame_rows: list[dict[str, float | int | str]] = []
        frame_count = 0
        frame_period = 1.0 / self.fps
        next_frame_t = self._record_start_mono
        last_frame_timestamp_mono: float | None = None

        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 960, 540)

        try:
            while not self._stop_event.is_set():
                now_mono = time.perf_counter()
                if now_mono < next_frame_t:
                    key = cv2.waitKey(1) & 0xFF
                    self._handle_keyboard_key(key)
                    self._poll_keyboard_motion(xy_step_mm, z_step_mm)
                    time.sleep(min(next_frame_t - now_mono, 0.005))
                    continue

                frame, frame_timestamp_epoch, frame_timestamp_mono = (
                    self._snap_frame_with_timestamp(
                        timeout=max(1.0, frame_period * 2.0),
                        after_mono=last_frame_timestamp_mono,
                    )
                )
                frame_elapsed_s = frame_timestamp_mono - self._record_start_mono

                nearest = self._nearest_sample(frame_timestamp_mono)
                writer.write(frame)
                frame_rows.append(
                    self._build_frame_row(
                        frame_index=frame_count,
                        timestamp_epoch=frame_timestamp_epoch,
                        elapsed_s=frame_elapsed_s,
                        nearest=nearest,
                    )
                )
                frame_count += 1
                last_frame_timestamp_mono = frame_timestamp_mono
                next_frame_t += frame_period

                cv2.imshow(window_name, frame)
                key = cv2.waitKey(1) & 0xFF
                self._handle_keyboard_key(key)
                self._poll_keyboard_motion(xy_step_mm, z_step_mm)
        finally:
            self._stop_event.set()
            sampler.join(timeout=2.0)
            self._join_motion_threads(timeout=2.0)
            writer.release()
            try:
                cv2.destroyWindow(window_name)
            except Exception:
                pass

        duration_s = time.perf_counter() - self._record_start_mono
        self._write_position_csv(position_csv_path)
        self._write_frame_csv(frame_csv_path, frame_rows)
        self._write_manifest(
            manifest_path,
            video_path=video_path,
            frame_csv_path=frame_csv_path,
            position_csv_path=position_csv_path,
            frame_count=frame_count,
            width=width,
            height=height,
            duration_s=duration_s,
            codec=codec,
        )

        return RecordingResult(
            video_path=os.path.abspath(video_path),
            frame_csv_path=os.path.abspath(frame_csv_path),
            position_csv_path=os.path.abspath(position_csv_path),
            manifest_path=os.path.abspath(manifest_path),
            frame_count=frame_count,
        )

    def _ensure_camera_running(self) -> None:
        if not getattr(self.camera, "_running", False):
            self.camera.start()

    def _snap_frame_with_timestamp(
        self,
        *,
        timeout: float,
        after_mono: float | None = None,
    ):
        t0 = time.perf_counter()
        while True:
            latest = self.camera.get_latest_frame_with_timestamp()
            if latest is not None:
                frame, timestamp_epoch, timestamp_mono = latest
                if after_mono is None or timestamp_mono > after_mono:
                    return frame, timestamp_epoch, timestamp_mono
            if time.perf_counter() - t0 > timeout:
                raise TimeoutError("No fresh camera frame available.")
            time.sleep(0.002)

    @staticmethod
    def _normalize_axes(axes: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(StampVideoRecorder._position_key(name) for name in axes)
        if not normalized:
            raise ValueError("At least one axis is required.")
        valid = {"x", "y", "z", "goni", "L"}
        invalid = [name for name in normalized if name not in valid]
        if invalid:
            raise ValueError(f"Unknown transfer axes: {invalid}. Use x, y, z, goni, or L.")
        return normalized

    @staticmethod
    def _position_key(axis: str) -> str:
        axis_key = str(axis).strip()
        return "L" if axis_key.lower() == "l" else axis_key.lower()

    def _start_motion_thread(
        self,
        *,
        moves: Optional[Sequence[StampMove]],
        movement_fn: Optional[Callable[[], None]],
    ) -> threading.Thread | None:
        if movement_fn is not None:
            thread = threading.Thread(target=movement_fn, daemon=True)
            thread.start()
            return thread
        if not moves:
            return None

        def _run_moves() -> None:
            for move in sorted(moves, key=lambda item: item.start_s):
                target_time = self._record_start_mono + float(move.start_s)
                while not self._stop_event.is_set():
                    remaining = target_time - time.perf_counter()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 0.01))
                if self._stop_event.is_set():
                    return
                self._move_axis_by(move.axis, move.delta_mm)

        thread = threading.Thread(target=_run_moves, daemon=True)
        thread.start()
        return thread

    def _handle_keyboard_key(self, key: int) -> None:
        if key == 255:
            return
        if key == ord("q"):
            self._stop_event.set()
            return

    def _poll_keyboard_motion(self, xy_step_mm: float, z_step_mm: float) -> None:
        if self._key_is_down("q"):
            self._stop_event.set()
            return

        requested_moves = []
        if self._key_is_down("a") and not self._key_is_down("d"):
            requested_moves.append(("x", -xy_step_mm))
        elif self._key_is_down("d") and not self._key_is_down("a"):
            requested_moves.append(("x", xy_step_mm))

        if self._key_is_down("s") and not self._key_is_down("w"):
            requested_moves.append(("y", -xy_step_mm))
        elif self._key_is_down("w") and not self._key_is_down("s"):
            requested_moves.append(("y", xy_step_mm))

        if self._key_is_down("f") and not self._key_is_down("r"):
            requested_moves.append(("z", -z_step_mm))
        elif self._key_is_down("r") and not self._key_is_down("f"):
            requested_moves.append(("z", z_step_mm))

        for axis_name, delta_mm in requested_moves:
            if self._position_key(axis_name) in self.axes:
                self._move_axis_by_async(axis_name, delta_mm)

    def _key_is_down(self, key: str) -> bool:
        if self._user32 is None:
            return False
        return bool(self._user32.GetAsyncKeyState(ord(key.upper())) & 0x8000)

    def _move_axis_by_async(self, axis: str, delta_mm: float) -> None:
        axis_key = self._position_key(axis)
        existing = self._axis_motion_threads.get(axis_key)
        if existing is not None and existing.is_alive():
            return
        thread = threading.Thread(
            target=self._move_axis_by,
            args=(axis, delta_mm),
            daemon=True,
        )
        thread.start()
        self._axis_motion_threads[axis_key] = thread
        self._motion_threads.append(thread)

    def _move_axis_by(self, axis: str, delta_mm: float) -> None:
        with self._motion_lock:
            self.transfer.move_axis_by(axis, float(delta_mm))

    def _join_motion_threads(self, timeout: float) -> None:
        deadline = time.perf_counter() + timeout
        for thread in list(self._motion_threads):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def _sample_positions_loop(self) -> None:
        period = 1.0 / self.sample_hz
        while not self._stop_event.is_set():
            t_mono = time.perf_counter()
            row: dict[str, float] = {
                "timestamp_epoch": time.time(),
                "timestamp_mono": t_mono,
                "elapsed_s": t_mono - self._record_start_mono,
            }
            with self._axis_io_lock:
                positions = self.transfer.positions()
            for name in self.axes:
                row[f"{name}_mm"] = positions[name]
            with self._sample_lock:
                self._samples.append(row)

            elapsed = time.perf_counter() - t_mono
            time.sleep(max(0.0, period - elapsed))

    def _nearest_sample(self, timestamp_mono: float) -> dict[str, float]:
        with self._sample_lock:
            if not self._samples:
                return {}
            return min(
                self._samples,
                key=lambda sample: abs(sample["timestamp_mono"] - timestamp_mono),
            ).copy()

    def _build_frame_row(
        self,
        *,
        frame_index: int,
        timestamp_epoch: float,
        elapsed_s: float,
        nearest: dict[str, float],
    ) -> dict[str, float | int | str]:
        row: dict[str, float | int | str] = {
            "frame_index": frame_index,
            "timestamp_epoch": timestamp_epoch,
            "elapsed_s": elapsed_s,
        }
        if nearest:
            row["position_elapsed_s"] = nearest["elapsed_s"]
            row["position_time_offset_s"] = elapsed_s - nearest["elapsed_s"]
            for name in self.axes:
                row[f"{name}_mm"] = nearest.get(f"{name}_mm", "")
        return row

    def _write_frame_csv(
        self,
        path: str,
        rows: Iterable[dict[str, float | int | str]],
    ) -> None:
        fieldnames = [
            "frame_index",
            "timestamp_epoch",
            "elapsed_s",
            "position_elapsed_s",
            "position_time_offset_s",
        ]
        for name in self.axes:
            fieldnames.append(f"{name}_mm")
        self._write_csv(path, fieldnames, rows)

    def _write_position_csv(self, path: str) -> None:
        fieldnames = ["timestamp_epoch", "timestamp_mono", "elapsed_s"]
        for name in self.axes:
            fieldnames.append(f"{name}_mm")
        with self._sample_lock:
            rows = list(self._samples)
        self._write_csv(path, fieldnames, rows)

    @staticmethod
    def _write_csv(
        path: str,
        fieldnames: Sequence[str],
        rows: Iterable[Mapping[str, float | int | str]],
    ) -> None:
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _write_manifest(
        self,
        path: str,
        *,
        video_path: str,
        frame_csv_path: str,
        position_csv_path: str,
        frame_count: int,
        width: int,
        height: int,
        duration_s: float,
        codec: str,
    ) -> None:
        data = {
            "video_path": os.path.abspath(video_path),
            "frame_csv_path": os.path.abspath(frame_csv_path),
            "position_csv_path": os.path.abspath(position_csv_path),
            "record_start_epoch": self._record_start_epoch,
            "record_start_mono": self._record_start_mono,
            "fps": self.fps,
            "sample_hz": self.sample_hz,
            "frame_count": frame_count,
            "width": width,
            "height": height,
            "duration_s": duration_s,
            "codec": codec,
            "axes": list(self.axes),
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)


def _parse_move(value: str) -> StampMove:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "Move must be formatted as axis,delta_mm,start_s, for example L,2.0,1.0"
        )
    axis, delta_mm, start_s = parts
    try:
        return StampMove(axis=axis, delta_mm=float(delta_mm), start_s=float(start_s))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_axes(value: str) -> list[str]:
    if "," in value:
        return [part.strip() for part in value.split(",") if part.strip()]
    axes: list[str] = []
    i = 0
    while i < len(value):
        if value[i : i + 4].lower() == "goni":
            axes.append("goni")
            i += 4
        else:
            axes.append(value[i])
            i += 1
    return axes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record camera video while logging transfer-stage positions in mm."
    )
    parser.add_argument("--x-serial", default="26007081", help="Transfer X KST201 serial.")
    parser.add_argument("--y-serial", default="26007120", help="Transfer Y KST201 serial.")
    parser.add_argument("--z-serial", default="26006985", help="Transfer Z KST201 serial.")
    parser.add_argument("--goni-serial", default="27271252", help="Transfer goni KDC101 serial.")
    parser.add_argument("--l-com-port", default="COM19", help="Transfer L-axis PDV COM port.")
    parser.add_argument("--axes", default="xyz", help="Axes to log, for example xyz or x,y,z.")
    parser.add_argument("--camera-index", type=int, default=0, help="Camera index.")
    parser.add_argument("--fps", type=float, default=15.0, help="Video frames per second.")
    parser.add_argument("--sample-hz", type=float, default=30.0, help="Axis position sampling rate.")
    parser.add_argument("--duration", type=float, default=10.0, help="Recording duration in seconds.")
    parser.add_argument("--video", default="stamp_recording.mp4", help="Output video path.")
    parser.add_argument("--xy-step-mm", type=float, default=0.02, help="Keyboard X/Y move chunk in mm while a key is held.")
    parser.add_argument("--z-step-mm", type=float, default=0.05, help="Keyboard Z move chunk in mm while a key is held.")
    parser.add_argument("--x-max-vel", type=float, default=0.2, help="X-axis max velocity in mm/s.")
    parser.add_argument("--x-accel", type=float, default=0.5, help="X-axis acceleration in mm/s^2.")
    parser.add_argument("--y-max-vel", type=float, default=0.2, help="Y-axis max velocity in mm/s.")
    parser.add_argument("--y-accel", type=float, default=0.5, help="Y-axis acceleration in mm/s^2.")
    parser.add_argument("--z-max-vel", type=float, default=0.01, help="Z-axis max velocity in mm/s.")
    parser.add_argument("--z-accel", type=float, default=0.1, help="Z-axis acceleration in mm/s^2.")
    parser.add_argument(
        "--keyboard-control",
        action="store_true",
        help="Control x/y/z with keyboard and stop when q is pressed.",
    )
    parser.add_argument(
        "--move",
        action="append",
        type=_parse_move,
        default=[],
        help="Scheduled relative move as axis,delta_mm,start_s. Can be repeated.",
    )
    parser.add_argument(
        "--no-live-view",
        action="store_true",
        help="Record without opening the OpenCV live-view window.",
    )
    args = parser.parse_args()

    camera = CameraController(index=args.camera_index, fps=int(args.fps))
    transfer = TransferController(
        x_serial=args.x_serial,
        y_serial=args.y_serial,
        z_serial=args.z_serial,
        goni_serial=args.goni_serial,
        L_com_port=args.l_com_port,
        auto_connect=True,
    )
    transfer.set_kst_speed(axis="x", max_vel=args.x_max_vel, accel=args.x_accel)
    transfer.set_kst_speed(axis="y", max_vel=args.y_max_vel, accel=args.y_accel)
    transfer.set_kst_speed(axis="z", max_vel=args.z_max_vel, accel=args.z_accel)

    recorder = StampVideoRecorder(
        camera,
        transfer,
        axes=_parse_axes(args.axes),
        fps=args.fps,
        sample_hz=args.sample_hz,
    )

    try:
        if args.keyboard_control:
            result = recorder.record_keyboard_control(
                args.video,
                xy_step_mm=args.xy_step_mm,
                z_step_mm=args.z_step_mm,
            )
        else:
            result = recorder.record(
                args.video,
                duration_s=args.duration,
                moves=args.move,
                open_live_view=not args.no_live_view,
            )
    finally:
        camera.stop()
        transfer.disconnect()

    print(f"Video: {result.video_path}")
    print(f"Per-frame positions: {result.frame_csv_path}")
    print(f"Raw position samples: {result.position_csv_path}")
    print(f"Manifest: {result.manifest_path}")
    print(f"Frames recorded: {result.frame_count}")


if __name__ == "__main__":
    main()
