from __future__ import annotations

from typing import Sequence, Any
from datetime import datetime, timezone

from common.services import get_instrument_status_service
from agents.lab_manager_agent.tools.cv.stage_geometry import px_to_mm

class ScotchTapeCrystalDetector:
    """Detects and updates the position of Scotch Tape Crystals (Class 3) from YOLO detections."""

    def __init__(self):
        self.status_service = get_instrument_status_service()
        self.target_class_id = 3
        # We can also look for label variations if needed
        self.target_label_substrings = ["scotch tape crystal", "tape crystal", "crystal"]

    def process(self, detections: Sequence[dict[str, Any]], frame_count: int, *, source: str = "left_yolo") -> bool:
        """
        Check if Scotch Tape Crystal (Class 3 in LEFT yolo) is detected.
        If so, compute its mm position and update instrument_status.
        
        Note: Right YOLO does not have scotch tape crystals class.
        """
        detected = False
        for det in detections:
            cls_id = det.get("class_id")
            label = str(det.get("class") or "").lower()
            is_crystal = False

            # Class ID 3 is ONLY "scotch tape crystals" in left_yolo
            # In right_yolo_new, class 3 is "silicon chip" (no scotch tape class).
            if cls_id is not None and "left" in source.lower():
                try:
                    if int(cls_id) == self.target_class_id:
                        is_crystal = True
                except (ValueError, TypeError):
                    pass

            # Fallback text check if class ID is missing/mismatched but label is clear
            if not is_crystal:
                for sub in self.target_label_substrings:
                    if sub in label:
                        is_crystal = True
                        break

            if is_crystal:
                detected = True
                # Found the crystal
                x_px = float(det.get("x", 0.0))
                y_px = float(det.get("y", 0.0))
                angle_deg = float(det.get("angle_deg", 0.0))

                # Only update if we have valid coordinates
                try:
                    x_mm, y_mm = px_to_mm(x_px, y_px)
                    if x_mm == 0.0 and y_mm == 0.0:
                        # Likely uncalibrated or origin
                        continue

                    payload = {
                        "scotch_tape_crystal": {
                            "position": {"x": x_mm, "y": y_mm},
                            "angle": angle_deg,
                            "last_seen_frame": frame_count,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                    }
                    # Using "yolo_detection" event to indicate source
                    self.status_service.update_true_instrument(payload, event="yolo_detection")
                except Exception as exc:
                    print(f"[YOLO] Failed to update crystal position: {exc}")
        return detected
