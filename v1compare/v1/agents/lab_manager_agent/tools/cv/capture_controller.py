from __future__ import annotations

import csv
import json
import math
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np

try:  # pragma: no cover - optional dependency guard
    import cv2  # type: ignore
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    import pyboof as pb  # type: ignore
except ImportError:  # pragma: no cover
    pb = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    from ultralytics import YOLO  # type: ignore
except ImportError:  # pragma: no cover
    YOLO = None  # type: ignore

from database.db_utils import init_db

from .config import LiveYOLOConfig
from .glass_slide_detector import GlassSlideDetectionConfig, GlassSlideDetector
from .silicon_detector import SiliconChipDetector
from .qr_utils import prepare_table_qr_data
from .stage_geometry import px_to_mm
from .stage_detector import ExfoliationStageDetector, TransferStageDetector, StampHolderDetector
from .scotch_tape_crystal_detector import ScotchTapeCrystalDetector
from common.services.instrument_status_service import get_instrument_status_service


def json_safe(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.float32, np.float64)):
        return float(value)
    if isinstance(value, (np.int32, np.int64)):
        return int(value)
    return value


def atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(directory), suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=json_safe)
        shutil.move(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


def write_polygon_label_file(path: Path, detections: list[dict[str, Any]], *, frame_width: int, frame_height: int) -> None:
    lines: list[str] = []
    for det in detections:
        cls_id = det.get("class_id")
        coords = det.get("xyxyxyxy")
        if cls_id is None or not coords:
            continue
        pts = np.array(coords, dtype=float).reshape(-1, 2)
        pts[:, 0] /= float(frame_width)
        pts[:, 1] /= float(frame_height)
        coord_str = " ".join(f"{v:.6f}" for v in pts.flatten())
        lines.append(f"{cls_id} {coord_str}")
    text = "\n".join(lines) + ("\n" if lines else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class LiveYOLOController:
    """Main loop extracted from yolo_cv/live_yolo_test.py as a reusable service."""

    def __init__(
        self,
        config: Optional[LiveYOLOConfig] = None,
        *,
        chip_recorder: Optional[SiliconChipDetector] = None,
        glass_detector: Optional[GlassSlideDetector] = None,
    ):
        self.config = config or LiveYOLOConfig()
        self._chip_recorder = chip_recorder
        self._glass_detector = glass_detector
        self._table_qr_detector = None
        self._last_overlay_path: Path | None = None
        self._last_final_overlay_path: Path | None = None
        self._last_overlay_path: Path | None = None
        self._last_final_overlay_path: Path | None = None
        self._qr_memory: dict[str, dict[str, Any]] = {}
        self._status_service = get_instrument_status_service()
        self._stage_detector = ExfoliationStageDetector()
        self._transfer_stage_detector = TransferStageDetector()
        self._stamp_holder_detector = StampHolderDetector()
        self._scotch_detector = ScotchTapeCrystalDetector()

    def run(
        self,
        *,
        max_frames: Optional[int] = None,
        write_latest: Optional[bool] = None,
        display_window: Optional[bool] = None,
    ) -> dict[str, Any]:
        if YOLO is None or cv2 is None:
            raise RuntimeError("Live YOLO capture requires both ultralytics and OpenCV to be installed.")

        cfg = self.config
        self._last_overlay_path = None
        self._last_final_overlay_path = None
        self._qr_memory = {}
        display = cfg.display_window if display_window is None else display_window
        write_latest_flag = cfg.write_latest if write_latest is None else write_latest

        model = YOLO(str(cfg.model_path))

        # Camera index is already resolved in config.__post_init__
        # Default to 0 if something went wrong and it's still None (shouldn't happen with updated config)
        cam_idx = cfg.camera_index if cfg.camera_index is not None else 0
        print(
            f"[YOLO_CAPTURE] camera_view={cfg.camera_view} camera_index={cam_idx} "
            f"model={cfg.model_path}",
            flush=True,
        )

        def _open_capture() -> "cv2.VideoCapture":
            if os.name == "posix" and os.uname().sysname == "Darwin":
                return cv2.VideoCapture(cam_idx, cv2.CAP_AVFOUNDATION)
            if os.name == "nt":
                # Prefer DirectShow to avoid MSMF grabFrame errors.
                cap_dshow = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
                if cap_dshow.isOpened():
                    return cap_dshow
                return cv2.VideoCapture(cam_idx, cv2.CAP_MSMF)
            return cv2.VideoCapture(cam_idx)

        cap = _open_capture()

        # On Windows, force MJPG for high resolution to avoid bandwidth issues/MSMF errors
        if os.name == "nt":
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            
        if cfg.set_camera_resolution:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_height)
        if not cap.isOpened():
            env_hint = "YOLO_CAM_LEFT_INDEX" if cfg.camera_view == "left" else "YOLO_CAM_RIGHT_INDEX"
            raise RuntimeError(
                f"Failed to open camera index {cam_idx}. "
                f"Ensure the camera is connected or update {env_hint} (current: {cam_idx})."
            )

        # Warm up a few frames before failing hard.
        time.sleep(0.25)
        ret = False
        for _ in range(6):
            ret, _ = cap.read()
            if ret:
                break
            time.sleep(0.1)
        if not ret:
            cap.release()
            raise RuntimeError("Failed to read from webcam.")

        if cfg.camera_config_path:
            atomic_write_json(cfg.camera_config_path, {"width": cfg.frame_width, "height": cfg.frame_height})

        if cfg.log_per_frame and cfg.per_frame_dir:
            Path(cfg.per_frame_dir).mkdir(parents=True, exist_ok=True)
        if cfg.save_frame_images and cfg.frame_image_dir:
            Path(cfg.frame_image_dir).mkdir(parents=True, exist_ok=True)
        if cfg.save_frame_labels and cfg.frame_label_dir:
            Path(cfg.frame_label_dir).mkdir(parents=True, exist_ok=True)

        init_db()
        polygon_processor = self._init_glass_detector()
        chip_recorder = self._chip_recorder or SiliconChipDetector()

        if display:
            try:
                cv2.namedWindow(cfg.window_name, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(cfg.window_name, *cfg.window_size)
            except Exception:
                display = False

        center_line_x = cfg.frame_width // 2
        frame_count = 0
        saved_images = 0
        saved_labels = 0
        polygon_updates = 0
        detections_summary: list[int] = []
        scotch_crystal_detected = False
        start_time = time.time()

        try:
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                if cfg.force_resize:
                    frame = cv2.resize(frame, (cfg.frame_width, cfg.frame_height))

                frame_for_recording = frame.copy() if (cfg.save_frame_images or cfg.save_frame_labels) else None
                results = model.predict(frame, conf=cfg.conf_threshold, verbose=False)
                detections = self._extract_detections(results, center_line_x)
                detections_summary.append(len(detections))

                if detections:
                    # Normalize source for source-aware class ID handling
                    normalized_source = "left_yolo" if cfg.camera_view == "left" else "right_yolo"
                    self._prepare_table_qr(frame, detections, source=normalized_source)
                    self._apply_qr_memory(detections)
                    self._log_table_qr_messages(frame_count, detections)
                    # Pass source to detectors for source-aware class ID handling
                    self._stage_detector.process(detections, frame_count, source=normalized_source)
                    self._transfer_stage_detector.process(detections, frame_count, source=normalized_source)
                    self._stamp_holder_detector.process(detections, frame_count, source=normalized_source)
                    if self._scotch_detector.process(detections, frame_count, source=normalized_source):
                        scotch_crystal_detected = True

                if chip_recorder and detections:
                    # Pass correct source for chip deduplication
                    normalized_source = "left_yolo" if cfg.camera_view == "left" else "right_yolo"
                    try:
                        print(f"[DEBUG] Recording {len(detections)} detections from {normalized_source} (camera_view={cfg.camera_view})")
                        chip_recorder.record(detections, source=normalized_source)
                    except Exception as exc:
                        print(f"[DB] Recording detections failed: {exc}")

                if cfg.enable_polygon_processor and polygon_processor and detections:
                    try:
                        polygon_output = polygon_processor.process_frame(
                            frame.copy(),
                            detections,
                            frame_id=f"{cfg.camera_view}_frame_{frame_count:05}",
                            source="live_yolo",
                        )
                        if polygon_output and polygon_output.slides:
                            polygon_updates += len(polygon_output.slides)
                        if polygon_output and polygon_output.overlay_path:
                            self._handle_final_overlay(polygon_output.overlay_path)
                    except Exception as exc:
                        print(f"[Polygon] Processing failed: {exc}")

                if cfg.log_per_frame and cfg.per_frame_dir:
                    per_frame_path = Path(cfg.per_frame_dir) / f"{cfg.camera_view}_frame_{frame_count:05}.json"
                    atomic_write_json(per_frame_path, detections)

                if cfg.save_frame_images and frame_for_recording is not None and cfg.frame_image_dir:
                    img_path = Path(cfg.frame_image_dir) / f"{cfg.camera_view}_frame_{frame_count:05}.jpg"
                    cv2.imwrite(str(img_path), frame_for_recording)
                    saved_images += 1

                if cfg.save_frame_labels and cfg.frame_label_dir:
                    label_path = Path(cfg.frame_label_dir) / f"{cfg.camera_view}_frame_{frame_count:05}.txt"
                    write_polygon_label_file(
                        label_path,
                        detections,
                        frame_width=cfg.frame_width,
                        frame_height=cfg.frame_height,
                    )
                    saved_labels += 1

                if write_latest_flag and cfg.latest_file:
                    atomic_write_json(cfg.latest_file, detections)

                if cfg.draw_overlays or cfg.overlay_frame_path:
                    target_frame = frame if cfg.draw_overlays else frame.copy()
                    self._draw_overlays(target_frame, detections, center_line_x)
                    if cfg.overlay_frame_path:
                        self._write_overlay_frame(target_frame, frame_count)

                if display:
                    try:
                        win_w = cv2.getWindowImageRect(cfg.window_name)[2]
                        win_h = cv2.getWindowImageRect(cfg.window_name)[3]
                        scale = min(win_w / cfg.frame_width, win_h / cfg.frame_height)
                        resized = cv2.resize(frame, (int(cfg.frame_width * scale), int(cfg.frame_height * scale)))
                        cv2.imshow(cfg.window_name, resized)
                        if cv2.waitKey(1) & 0xFF == ord("q"):
                            break
                    except Exception:
                        display = False

                frame_count += 1
                if max_frames and frame_count >= max_frames:
                    break
        finally:
            cap.release()
            if display:
                try:
                    cv2.destroyWindow(cfg.window_name)
                except Exception:
                    cv2.destroyAllWindows()

        elapsed = time.time() - start_time

        def _path_str(path: Path | None) -> str | None:
            if not path:
                return None
            return str(path).replace("\\", "/")

        scotch_payload = None
        if scotch_crystal_detected:
            scotch_payload = self._status_service.get_latest_status("scotch_tape_crystal")

        return {
            "camera_view": cfg.camera_view,
            "camera_index": cam_idx,
            "frames_processed": frame_count,
            "avg_detections_per_frame": (sum(detections_summary) / len(detections_summary)) if detections_summary else 0.0,
            "total_detections": sum(detections_summary),
            "polygon_slide_updates": polygon_updates,
            "images_saved": saved_images,
            "labels_saved": saved_labels,
            "per_frame_dir": _path_str(cfg.per_frame_dir),
            "latest_file": _path_str(cfg.latest_file),
            "yolo_capture_overlay": _path_str(self._last_overlay_path),
            "processed_overlay": _path_str(self._last_final_overlay_path),
            "duration_seconds": elapsed,
            "scotch_tape_crystal_detected": scotch_crystal_detected,
            "scotch_tape_crystal": scotch_payload,
        }

    def _init_glass_detector(self) -> Optional[GlassSlideDetector]:
        if self._glass_detector is not None:
            return self._glass_detector
        cfg = self.config
        if not cfg.enable_polygon_processor:
            return None
        glass_config = GlassSlideDetectionConfig(
            overlay_output_path=cfg.polygon_overlay_path,
            csv_output_path=cfg.polygon_csv_path,
            frame_interval=max(1, cfg.polygon_frame_interval),
            table_side=cfg.camera_view,
        )
        self._glass_detector = GlassSlideDetector(glass_config)
        return self._glass_detector


    def _extract_detections(self, results: Any, center_line_x: int) -> list[dict[str, Any]]:
        detections: list[dict[str, Any]] = []
        if not results:
            return detections

        def _flatten_array(value: Any) -> Optional[np.ndarray]:
            if value is None:
                return None
            if isinstance(value, np.ndarray):
                arr = value
            else:
                try:
                    arr = value.cpu().numpy()  # type: ignore[attr-defined]
                except AttributeError:
                    try:
                        arr = np.asarray(value)
                    except Exception:
                        return None
            if arr.ndim > 1:
                return arr.reshape(-1)
            return arr

        result = results[0]
        names = getattr(result, "names", None)

        mask_obj = getattr(result, "masks", None)
        masks = getattr(mask_obj, "xy", None)
        if masks:
            probs = _flatten_array(getattr(mask_obj, "scores", None) or getattr(mask_obj, "conf", None))
            cls_vals = _flatten_array(getattr(mask_obj, "cls", None))
            boxes_obj = getattr(result, "boxes", None)
            box_cls_vals = _flatten_array(getattr(boxes_obj, "cls", None)) if boxes_obj is not None else None
            box_conf_vals = _flatten_array(getattr(boxes_obj, "conf", None)) if boxes_obj is not None else None
            for idx, polygon in enumerate(masks):
                pts = np.array(polygon, dtype=float)
                if pts.size == 0:
                    continue
                if cls_vals is not None and idx < len(cls_vals):
                    cls_id = int(cls_vals[idx])
                elif box_cls_vals is not None and idx < len(box_cls_vals):
                    cls_id = int(box_cls_vals[idx])
                else:
                    cls_id = idx
                if probs is not None and idx < len(probs):
                    conf = float(probs[idx])
                elif box_conf_vals is not None and idx < len(box_conf_vals):
                    conf = float(box_conf_vals[idx])
                else:
                    conf = 0.0
                detections.append(self._build_polygon_detection(pts, cls_id, conf, names, center_line_x))
            return detections

        return detections

    def _build_polygon_detection(
        self,
        pts: np.ndarray,
        cls_id: int,
        conf: float,
        names: Any,
        center_line_x: int,
    ) -> dict[str, Any]:
        if pts.ndim != 2 or pts.shape[1] != 2:
            pts = pts.reshape(-1, 2)
        label = self._resolve_label(names, cls_id)
        if self.config.relabel_split:
            centroid_x = float(np.mean(pts[:, 0]))
            label = self._relabel(label, centroid_x, center_line_x, self.config.camera_view)

        angle_deg = 0.0
        w = 0.0
        h = 0.0
        x = float(np.mean(pts[:, 0]))
        y = float(np.mean(pts[:, 1]))
        if cv2 is not None and len(pts) >= 3:
            rect = cv2.minAreaRect(pts.astype(np.float32))
            (cx, cy), (rw, rh), rect_angle = rect
            
            # cv2.minAreaRect behavior is not strictly deterministic regarding which side is w vs h.
            # We force w <= h (portrait aspect for the bounding box stats).
            # If the raw detection comes in as "landscape" (rw > rh), it means the angle is relative to the long side.
            # If we swap w/h, we must shift the angle by 90 degrees to keep the coordinate system consistent.
            
            if rw > rh:
                final_angle = rect_angle + 90
            else:
                final_angle = rect_angle
                
            x = float(cx)
            y = float(cy)
            w = float(min(rw, rh))
            h = float(max(rw, rh))
            
            angle_deg = -float(final_angle)
            if angle_deg > 90:
                angle_deg -= 180
            elif angle_deg < -90:
                angle_deg += 180
        else:
            x_min = float(np.min(pts[:, 0]))
            x_max = float(np.max(pts[:, 0]))
            y_min = float(np.min(pts[:, 1]))
            y_max = float(np.max(pts[:, 1]))
            w = x_max - x_min
            h = y_max - y_min

        det: dict[str, Any] = {
            "class": label,
            "class_id": cls_id,
            "confidence": round(conf, 2),
            "x": round(x, 2),
            "y": round(y, 2),
            "w": round(w, 2),
            "h": round(h, 2),
            "angle_deg": round(angle_deg, 2),
        }
        if self.config.include_polygons:
            det["xyxyxyxy"] = pts.flatten().tolist()
        return det

    def _build_detection(
        self,
        box: np.ndarray,
        cls_id: int,
        conf: float,
        names: Any,
        center_line_x: int,
    ) -> dict[str, Any]:
        x = float(np.mean(box[:, 0]))
        y = float(np.mean(box[:, 1]))
        d01 = float(np.linalg.norm(box[0] - box[1]))
        d12 = float(np.linalg.norm(box[1] - box[2]))
        short_edge = box[1] - box[0] if d01 < d12 else box[2] - box[1]
        angle_rad = math.atan2(float(short_edge[1]), float(short_edge[0]))
        angle_deg = math.degrees(angle_rad)
        if angle_deg > 90:
            angle_deg -= 180
        elif angle_deg < -90:
            angle_deg += 180
        angle_deg = -angle_deg

        w = min(d01, d12)
        h = max(d01, d12)
        label = self._resolve_label(names, cls_id)
        if self.config.relabel_split:
            label = self._relabel(label, x, center_line_x, self.config.camera_view)

        det: dict[str, Any] = {
            "class": label,
            "class_id": cls_id,
            "confidence": round(conf, 2),
            "x": round(x, 2),
            "y": round(y, 2),
            "w": round(w, 2),
            "h": round(h, 2),
            "angle_deg": round(angle_deg, 2),
        }
        if self.config.include_polygons:
            det["xyxyxyxy"] = box.flatten().tolist()
        return det

    @staticmethod
    def _resolve_label(names: Any, cls_id: int) -> str:
        if isinstance(names, dict):
            return str(names.get(cls_id, cls_id))
        if isinstance(names, (list, tuple)) and 0 <= cls_id < len(names):
            return str(names[cls_id])
        return str(cls_id)

    def _write_overlay_frame(self, frame: np.ndarray, frame_idx: int) -> None:
        if cv2 is None:
            return
        path = self.config.overlay_frame_path
        if path is None:
            return
        target = Path(path)
        if target.suffix:
            file_path = target
        else:
            target.mkdir(parents=True, exist_ok=True)
            file_path = target / f"frame_{frame_idx:05}.jpg"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(file_path), frame)
        ui_path = self._export_for_ui(
            file_path, 
            Path("images") / "yolo_captures",
            filename_override=f"latest_frame{file_path.suffix}"
        )
        self._last_overlay_path = ui_path or file_path

    def _handle_final_overlay(self, source: Path | str) -> None:
        src_path = Path(source)
        ui_path = self._export_for_ui(
            src_path, 
            Path("images") / "yolo_captures" / "final_overlays",
            filename_override=f"latest_final_result{src_path.suffix}"
        )
        if ui_path is not None:
            self._last_final_overlay_path = ui_path

    def _export_for_ui(self, source: Path, rel_dir: Path, filename_override: str | None = None) -> Path | None:
        try:
            project_root = Path(__file__).resolve().parents[4]
        except Exception:
            return None
        dest_dir = project_root / rel_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        
        if filename_override:
            # Check if we are doing the special "latest" exports and inject camera view
            # This is a bit of a hack to keep the signature clean but enforce the requirement
            if filename_override.startswith("latest_"):
                filename_override = f"{self.config.camera_view}_{filename_override}"

            dest_name = filename_override
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            suffix = source.suffix
            stem = source.stem if suffix else source.name
            dest_name = f"{stem}_{timestamp}{suffix}" if suffix else f"{stem}_{timestamp}"
            
        dest_path = dest_dir / dest_name
        try:
            shutil.copy2(source, dest_path)
        except Exception:
            return None
        return rel_dir / dest_name

    def _prepare_table_qr(self, frame: np.ndarray, detections: Sequence[dict[str, Any]], *, source: str = "left_yolo") -> None:
        if cv2 is None:
            return
        detector = self._ensure_table_qr_detector()
        prepare_table_qr_data(frame, detections, detector=detector, source=source)

    def _apply_qr_memory(self, detections: Sequence[dict[str, Any]]) -> None:
        MAX_MISSES = 3
        MAX_DISTANCE = 160.0  # pixels; table fiducials are widely spaced
        active_labels: set[str] = set()
        for det in detections:
            if not self._is_table_qr(det):
                continue
            center = (float(det.get("x", 0.0)), float(det.get("y", 0.0)))
            label = self._normalize_qr_label(det.get("qr_message"))
            if label:
                entry = self._qr_memory.setdefault(label, {"center": center, "misses": 0})
                entry["center"] = center
                entry["misses"] = 0
                active_labels.add(label)
                continue
            fallback = self._match_cached_qr(center, max_distance=MAX_DISTANCE)
            if not fallback:
                continue
            entry = self._qr_memory.get(fallback)
            if entry is None:
                continue
            entry["center"] = center
            entry["misses"] = entry.get("misses", 0) + 1
            if entry["misses"] <= MAX_MISSES:
                det["qr_message"] = fallback
                active_labels.add(fallback)
            else:
                # Stop reusing stale labels once misses exceed the threshold
                entry["misses"] = MAX_MISSES
        for label, entry in list(self._qr_memory.items()):
            if label in active_labels:
                continue
            entry["misses"] = entry.get("misses", 0) + 1
            if entry["misses"] >= MAX_MISSES:
                self._qr_memory.pop(label, None)

    def _match_cached_qr(self, center: tuple[float, float], *, max_distance: float) -> str | None:
        if not self._qr_memory:
            return None
        best_label: str | None = None
        best_dist = float("inf")
        for label, entry in self._qr_memory.items():
            cached = entry.get("center")
            if not cached:
                continue
            dist = math.hypot(center[0] - cached[0], center[1] - cached[1])
            if dist < best_dist and dist <= max_distance:
                best_label = label
                best_dist = dist
        return best_label

    def _is_table_qr(self, det: Mapping[str, Any]) -> bool:
        """Check if detection is a table QR marker (source-aware)."""
        label_text = str(det.get("class") or det.get("label") or "").lower()
        class_id = det.get("class_id")
        if "table micro qr" in label_text:
            return True
        if class_id is not None:
            try:
                cid = int(class_id)
                # Left/Right YOLO: class 5 = table micro qr
                return cid == 5
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def _normalize_qr_label(value: Any) -> str:
        if not value:
            return ""
        text = str(value).strip()
        return "".join(ch for ch in text if ch.isalnum()).upper()

    def _log_table_qr_messages(self, frame_idx: int, detections: Sequence[dict[str, Any]]) -> None:
        csv_path = self.config.table_qr_csv_path
        if not csv_path:
            return
        rows: list[dict[str, Any]] = []
        timestamp = datetime.now(timezone.utc).isoformat()
        for det in detections:
            message = det.get("qr_message")
            if not isinstance(message, str) or not message.strip():
                continue
            rows.append(
                {
                    "frame_index": frame_idx,
                    "timestamp": timestamp,
                    "label": str(det.get("class") or det.get("label") or ""),
                    "class_id": det.get("class_id"),
                    "qr_message": message.strip(),
                    "x": det.get("x"),
                    "y": det.get("y"),
                    "w": det.get("w"),
                    "h": det.get("h"),
                    "confidence": det.get("confidence"),
                }
            )
        if not rows:
            return
        path = Path(csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "frame_index",
            "timestamp",
            "label",
            "class_id",
            "qr_message",
            "x",
            "y",
            "w",
            "h",
            "confidence",
        ]
        write_header = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)

    def _ensure_table_qr_detector(self):
        if pb is None:
            return None
        if self._table_qr_detector is None:
            try:
                self._table_qr_detector = pb.FactoryFiducial(np.uint8).microqr()
            except Exception as exc:
                print(f"[Calib] QR detector unavailable: {exc}")
                self._table_qr_detector = None
        return self._table_qr_detector


    @staticmethod
    def _draw_overlays(frame: np.ndarray, detections: list[dict[str, Any]], center_line_x: int) -> None:
        if cv2 is None:
            return
        height = frame.shape[0]
        for det in detections:
            coords = det.get("xyxyxyxy")
            if coords:
                pts = np.array(coords, dtype=float).reshape(-1, 2).astype(int)
                cv2.polylines(frame, [pts], isClosed=True, color=(0, 255, 0), thickness=2)
                text_x = int(np.min(pts[:, 0]))
                text_y = int(np.max(pts[:, 1])) + 14
            else:
                text_x = int(det.get("x", 0) - 0.5 * det.get("w", 0))
                text_y = int(det.get("y", 0) + 0.5 * det.get("h", 0)) + 14
            text_x = max(0, min(frame.shape[1] - 1, text_x))
            text_y = max(0, min(height - 1, text_y))
            text = str(det["class"])
            cv2.putText(frame, text, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 255), 2)
        # cv2.line(frame, (center_line_x, 0), (center_line_x, height), (255, 0, 0), 2)

    @staticmethod
    def _relabel(label: str, x: float, center_line_x: int, camera_view: str) -> str:
        cleaned = label.strip()
        overrides = {
            "orange sqaure": "Orange Square",
        }
        normalized_key = cleaned.lower()
        if normalized_key in overrides:
            cleaned = overrides[normalized_key]
            normalized_key = cleaned.lower()
        normalized = normalized_key
        if normalized == "silicon chip":
            if camera_view == "right":
                # Right YOLO: left side exfoliated, right side device
                return "Exfoliated Silicon Chip" if x < center_line_x else "Device"
            # Left YOLO: left side bare, right side exfoliated
            return "Bare Silicon Chip" if x < center_line_x else "Exfoliated Silicon Chip"
        return cleaned
