from __future__ import annotations

from datetime import datetime
import math
from typing import Sequence

import numpy as np

try:  # pragma: no cover - optional dependency guard
    import cv2  # type: ignore
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

import os

from common.services import ChipInventoryService, get_instrument_status_service
from common.services.chip_reconciliation_service import get_chip_reconciliation_service
from database.db_utils import init_db
from agents.lab_manager_agent.tools.cv.stage_geometry import px_to_mm, is_calibrated, polygon_mm_metrics


class SiliconChipDetector:
    """Detects and records silicon chips from YOLO detections."""

    def __init__(self, repository: ChipInventoryService | None = None):
        init_db()
        self.inventory = repository or ChipInventoryService()
        self.status_service = get_instrument_status_service()
        self.reconciliation = get_chip_reconciliation_service()

    def record(self, detections: Sequence[dict], *, source: str = "live_yolo") -> None:
        normalized_source = self._normalize_source(source)
        previous_scan_detected_chip_ids = set(
            self.inventory.get_present_chip_ids_by_source(normalized_source)
        )
        current_scan_detected_chip_ids: set[int] = set()
        for det in detections:
            if not self._is_silicon_chip(det, source=normalized_source):
                continue
            x_px = float(det.get("x", 0.0))
            y_px = float(det.get("y", 0.0))
            center_x_mm, center_y_mm = px_to_mm(x_px, y_px, source=normalized_source)
            polygon_px = self._polygon_from_detection(det)
            if not polygon_px:
                continue
            pts_mm, width_mm, height_mm = polygon_mm_metrics(polygon_px, source=normalized_source)
            angle_deg = float(det.get("angle_deg", 0.0))
            frame = "right" if "right" in normalized_source else "left"
            pickup_x_mm, pickup_y_mm, rotate_angle = self._compute_pickup_pose_for_stage(
                center_x_mm,
                center_y_mm,
                angle_deg,
                width_mm,
                stage_name="transfer" if frame == "right" else "exfoliation",
                flip_y_offset=frame == "right",
            )
            
            db_label = self._infer_db_label(det, center_x_mm=center_x_mm)
            
            stored_rotate_angle = self._normalize_pickup_angle_for_db(
                rotate_angle,
                frame=frame,
                db_label=db_label,
            )

            if db_label == "electrode_chip" and frame == "right":
                stored_pickup_y_mm = pickup_y_mm + 16.0
            else:
                stored_pickup_y_mm = pickup_y_mm

            metadata = {
                "source": normalized_source,  # Use normalized source
            }
            coord_key = f"{int(round(center_x_mm))}:{int(round(center_y_mm))}"
            slot = self.inventory.find_slot_by_xy(center_x_mm, center_y_mm, frame=frame)
            chip = self.reconciliation.process_detection(
                x_detected=center_x_mm,
                y_detected=center_y_mm,
                width=width_mm,
                height=height_mm,
                confidence=float(det.get("confidence", 0.0)),
                source=normalized_source,
                label=db_label,
                slot_id=slot.id if slot else None,
                pickup_left_x_mm=pickup_x_mm if frame == "left" else None,
                pickup_left_y_mm=pickup_y_mm if frame == "left" else None,
                pickup_left_angle_deg=stored_rotate_angle if frame == "left" else None,
                pickup_right_x_mm=pickup_x_mm if frame == "right" else None,
                pickup_right_y_mm=stored_pickup_y_mm if frame == "right" else None,
                pickup_right_angle_deg=stored_rotate_angle if frame == "right" else None,
                table_coordinate_left=coord_key if frame == "left" else None,
                table_coordinate_right=coord_key if frame == "right" else None,
                angle_deg=angle_deg,
                metadata=metadata,
            )
            self.inventory.record_detection(
                label=db_label,
                center_x_mm=center_x_mm,
                center_y_mm=center_y_mm,
                angle_deg=angle_deg,
                width_mm=width_mm,
                height_mm=height_mm,
                confidence=float(det.get("confidence", 0.0)),
                source=source,
                chip_id=chip.id,
                frame=frame,
            )
            current_scan_detected_chip_ids.add(int(chip.id))

        stale_chip_ids = previous_scan_detected_chip_ids - current_scan_detected_chip_ids
        if stale_chip_ids:
            self.inventory.set_chip_presence(
                list(stale_chip_ids),
                present=False,
                source=normalized_source,
                metadata_updates={
                    "stale_cleared_at": datetime.utcnow().isoformat() + "Z",
                    "stale_reason": "not_detected_in_current_scan",
                },
            )

        if current_scan_detected_chip_ids:
            self.inventory.set_chip_presence(
                list(current_scan_detected_chip_ids),
                present=True,
                source=normalized_source,
            )



    @staticmethod
    def _is_silicon_chip(det: dict, source: str = "left_yolo") -> bool:
        label = str(det.get("class") or det.get("label") or "").lower()
        class_id = det.get("class_id")
        normalized_source = (source or "").lower()
        left_ids = SiliconChipDetector._get_chip_class_ids_for_source(normalized_source, "left")
        right_ids = SiliconChipDetector._get_chip_class_ids_for_source(normalized_source, "right")
        if class_id is not None:
            try:
                cid = int(class_id)
                # Left/right class IDs are configurable via env vars; defaults match the original models.
                
                # Exclude exfoliation stage (left only)
                if cid == 0 and "stage" in label:
                    return False
                
                # Accept configured class IDs for the active side.
                if "right" in normalized_source and cid in right_ids:
                    return True
                
                if "right" not in normalized_source and cid in left_ids:
                    return True
                    
            except (TypeError, ValueError):
                pass
        
        label_lower = label.lower()
        if "exfoliation stage" in label_lower:
            return False

        if "device" in label_lower:
            return True

        return "chip" in label_lower and "carrier" not in label_lower

    @staticmethod
    def _load_chip_class_ids(env_key: str, *, default: set[int]) -> set[int]:
        raw = os.getenv(env_key)
        if not raw:
            return set(default)
        values: set[int] = set()
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                values.add(int(token))
            except ValueError:
                continue
        return values or set(default)

    @staticmethod
    def _get_chip_class_ids_for_source(source: str, side: str) -> set[int]:
        # side is "left" or "right". Use env-configured IDs if present.
        if side == "right":
            return SiliconChipDetector._load_chip_class_ids("YOLO_RIGHT_CHIP_CLASS_IDS", default={3})
        return SiliconChipDetector._load_chip_class_ids("YOLO_LEFT_CHIP_CLASS_IDS", default={4})

    @staticmethod
    def _normalize_source(source: str) -> str:
        lowered = (source or "").strip().lower()
        if "right" in lowered:
            return "right_yolo"
        if "left" in lowered:
            return "left_yolo"
        return "left_yolo"

    @staticmethod
    def _infer_db_label(det: dict, *, center_x_mm: float | None = None) -> str:
        class_name = str(det.get("class", "")).lower()
        if "bare" in class_name and "chip" in class_name:
            return "bare_silicon_chip"
        if "exfoliated" in class_name and "chip" in class_name:
            return "exfoliated_chip"
        if "device" in class_name:
            return "electrode_chip"
        if center_x_mm is not None:
            return "bare_silicon_chip" if center_x_mm < -100 else "exfoliated_chip"
        return "exfoliated_chip"

    @staticmethod
    def _normalize_pickup_angle_for_db(
        rotate_angle_deg: float,
        *,
        frame: str,
        db_label: str,
    ) -> float:
        """Return the hardware pickup angle to store in the database.

        The pickup-pose calculation already returns the right-arm hardware
        rotation convention. Do not invert it here; downstream pickup workflows
        consume the database value directly as the motor command angle.
        """
        return rotate_angle_deg

    @staticmethod
    def _polygon_from_detection(det: dict) -> list[tuple[float, float]] | None:
        coords = det.get("xyxyxyxy")
        if isinstance(coords, (list, tuple)) and len(coords) >= 8:
            pts = np.array(coords, dtype=float).reshape(-1, 2)
            if cv2 is not None:
                rect = cv2.minAreaRect(pts.astype(np.float32))
                box = cv2.boxPoints(rect)
                return [(float(x), float(y)) for x, y in box]
            return [(float(coords[i]), float(coords[i + 1])) for i in range(0, len(coords), 2)][:4]
        x = float(det.get("x", 0.0))
        y = float(det.get("y", 0.0))
        w = float(det.get("w", 0.0))
        h = float(det.get("h", 0.0))
        angle_deg = float(det.get("angle_deg", 0.0))
        theta = math.radians(-angle_deg)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        half_w, half_h = 0.5 * w, 0.5 * h
        rect = [
            (-half_w, -half_h),
            (half_w, -half_h),
            (half_w, half_h),
            (-half_w, half_h),
        ]
        return [(x + dx * cos_t - dy * sin_t, y + dx * sin_t + dy * cos_t) for dx, dy in rect]

    def _get_exfoliation_stage_position(self) -> tuple[float, float]:
        """Return exfoliation stage position (fixed default unless opted into detected)."""
        return self._get_stage_position(
            stage_name="exfoliation",
            default_pos=(0.0, 262.0),
            prefer_detected=False,
        )

    def _get_transfer_stage_position(self) -> tuple[float, float]:
        """Return transfer stage origin used for right-arm chip pickup geometry."""
        return self._get_stage_position(
            stage_name="transfer",
            default_pos=(0.0, 300.0),
            prefer_detected=False,
        )

    def _get_stage_position(
        self,
        stage_name: str,
        default_pos: tuple[float, float],
        *,
        prefer_detected: bool = True,
    ) -> tuple[float, float]:
        if not prefer_detected:
            return default_pos
        try:
            status = self.status_service.get_latest_status(f"{stage_name}_stage")
            if not status:
                return default_pos
            pos = status.get("position") or {}
            x = float(pos.get("x", default_pos[0]))
            y = float(pos.get("y", default_pos[1]))
            return x, y
        except Exception:
            return default_pos

    def _compute_pickup_pose_for_stage(
        self,
        chip_center_x_mm: float,
        chip_center_y_mm: float,
        chip_angle_deg: float,
        chip_width_mm: float,
        *,
        stage_name: str,
        flip_y_offset: bool,
    ) -> tuple[float, float, float]:
        # Port of the exfoliation simulator's bare-chip pickup heuristic.
        if stage_name == "transfer":
            stage_x_mm, stage_y_mm = self._get_transfer_stage_position()
        elif stage_name == "exfoliation":
            stage_x_mm, stage_y_mm = self._get_exfoliation_stage_position()

        if chip_center_x_mm < stage_x_mm:
            reference_x_mm = stage_x_mm + 10.0
        else:
            reference_x_mm = stage_x_mm - 10.0

        reference_vec = (reference_x_mm, stage_y_mm)
        chip_center_norm = math.hypot(chip_center_x_mm, chip_center_y_mm) or 1e-9
        reference_norm = math.hypot(*reference_vec) or 1e-9
        dot = chip_center_x_mm * reference_vec[0] + chip_center_y_mm * reference_vec[1]
        cosv = max(-1.0, min(1.0, dot / (chip_center_norm * reference_norm)))
        reference_angle_deg = math.degrees(math.acos(cosv))
        offset_mm = 0.2 * chip_width_mm if chip_width_mm > 0 else 10.0
        offset_toward_reference = chip_angle_deg < reference_angle_deg

        y_sign = -1.0 if flip_y_offset else 1.0

        if offset_toward_reference:
            pickup_x = chip_center_x_mm - offset_mm * math.sin(math.radians(chip_angle_deg))
            pickup_y = chip_center_y_mm + y_sign * offset_mm * math.cos(math.radians(chip_angle_deg))
            rotate_angle = 90 + chip_angle_deg - reference_angle_deg
        else:
            pickup_x = chip_center_x_mm + offset_mm * math.sin(math.radians(chip_angle_deg))
            pickup_y = chip_center_y_mm - y_sign * offset_mm * math.cos(math.radians(chip_angle_deg))
            rotate_angle = -(90 - (chip_angle_deg - reference_angle_deg))
        return pickup_x, pickup_y, rotate_angle
