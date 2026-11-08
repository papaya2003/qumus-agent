from __future__ import annotations

from typing import Sequence, Any
import os
from datetime import datetime, timezone

from common.services import get_instrument_status_service
from agents.lab_manager_agent.tools.cv.stage_geometry import px_to_mm


def _normalize_label(label: str) -> str:
    return label.lower().replace("_", " ").strip()


def _label_matches(label: str, *terms: str) -> bool:
    normalized = _normalize_label(label)
    return any(term in normalized for term in terms if term)


def _load_class_ids(env_name: str) -> set[int]:
    raw = os.getenv(env_name, "")
    values: set[int] = set()
    for token in raw.replace(",", " ").split():
        try:
            values.add(int(token))
        except ValueError:
            continue
    return values

class ExfoliationStageDetector:
    """Detects and updates the position of the Exfoliation Stage from YOLO detections."""

    def __init__(self):
        self.status_service = get_instrument_status_service()

    def process(self, detections: Sequence[dict[str, Any]], frame_count: int, *, source: str = "left_yolo") -> None:
        """
        Check if Exfoliation Stage (Class 0 in LEFT yolo) is detected. 
        If so, compute its mm position and update instrument_status.
        
        Note: In right_yolo, class 0 is "glass slide micro qr", NOT the stage!
        """
        for det in detections:
            # Check for Class ID 0 or name "exfoliation stage"
            cls_id = det.get("class_id")
            label = str(det.get("class") or "").lower()
            is_stage = False
            
            # Class ID 0 is ONLY "exfoliation stage" in left_yolo
            # In right_yolo, class 0 is "glass slide micro qr"!
            if cls_id is not None and "left" in source.lower():
                try:
                    if int(cls_id) == 0:
                        is_stage = True
                except (ValueError, TypeError):
                    pass
            
            if not is_stage and "exfoliation stage" in label:
                is_stage = True
            
            if is_stage:
                # Found the stage
                x_px = float(det.get("x", 0.0))
                y_px = float(det.get("y", 0.0))
                
                # Only update if we have calibrations
                try:
                    x_mm, y_mm = px_to_mm(x_px, y_px, source="left")
                    if x_mm == 0.0 and y_mm == 0.0:
                        # Likely uncalibrated or origin
                        continue
                        
                    payload = {
                        "exfoliation_stage": {
                            "position": {"x": x_mm, "y": y_mm},
                            "last_calibrated": "live_yolo",
                            "last_seen_frame": frame_count,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                    }
                    self.status_service.update_true_instrument(payload, event="yolo_calibration")
                except Exception as exc:
                    print(f"[YOLO] Failed to update stage position: {exc}")


class TransferStageDetector:
    """Detects and updates the position of the Transfer Stage from right YOLO detections."""

    def __init__(self):
        self.status_service = get_instrument_status_service()
        self._class_ids = _load_class_ids("YOLO_RIGHT_TRANSFER_STAGE_CLASS_IDS") or {6}

    def process(self, detections: Sequence[dict[str, Any]], frame_count: int, *, source: str = "right_yolo") -> None:
        if "right" not in (source or "").lower():
            return
        for det in detections:
            cls_id = det.get("class_id")
            label = str(det.get("class") or det.get("label") or "")
            is_stage = False

            if cls_id is not None and self._class_ids:
                try:
                    if int(cls_id) in self._class_ids:
                        is_stage = True
                except (ValueError, TypeError):
                    pass

            if not is_stage and _label_matches(label, "transfer stage"):
                is_stage = True

            if is_stage:
                x_px = float(det.get("x", 0.0))
                y_px = float(det.get("y", 0.0))
                try:
                    x_mm, y_mm = px_to_mm(x_px, y_px, source="right")
                    if x_mm == 0.0 and y_mm == 0.0:
                        continue
                    payload = {
                        "transfer_stage": {
                            "position": {"x": x_mm, "y": y_mm},
                            "last_calibrated": "live_yolo",
                            "last_seen_frame": frame_count,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    }
                    self.status_service.update_true_instrument(payload, event="yolo_calibration")
                except Exception as exc:
                    print(f"[YOLO] Failed to update transfer stage position: {exc}")


class StampHolderDetector:
    """Detects and updates the position of the Stamp Holder from right YOLO detections."""

    def __init__(self):
        self.status_service = get_instrument_status_service()
        self._class_ids = _load_class_ids("YOLO_RIGHT_STAMP_HOLDER_CLASS_IDS") or {4}

    def process(self, detections: Sequence[dict[str, Any]], frame_count: int, *, source: str = "right_yolo") -> None:
        if "right" not in (source or "").lower():
            return
        for det in detections:
            cls_id = det.get("class_id")
            label = str(det.get("class") or det.get("label") or "")
            is_holder = False

            if cls_id is not None and self._class_ids:
                try:
                    if int(cls_id) in self._class_ids:
                        is_holder = True
                except (ValueError, TypeError):
                    pass

            if not is_holder and _label_matches(label, "stamp holder"):
                is_holder = True

            if is_holder:
                x_px = float(det.get("x", 0.0))
                y_px = float(det.get("y", 0.0))
                try:
                    x_mm, y_mm = px_to_mm(x_px, y_px, source="right")
                    if x_mm == 0.0 and y_mm == 0.0:
                        continue
                    payload = {
                        "stamp_holder": {
                            "position": {"x": x_mm, "y": y_mm},
                            "last_calibrated": "live_yolo",
                            "last_seen_frame": frame_count,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    }
                    self.status_service.update_true_instrument(payload, event="yolo_calibration")
                except Exception as exc:
                    print(f"[YOLO] Failed to update stamp holder position: {exc}")
