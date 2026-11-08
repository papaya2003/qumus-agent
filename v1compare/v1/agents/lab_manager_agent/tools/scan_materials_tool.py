from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover
    import cv2  # type: ignore
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

from smolagents import Tool

from agents.lab_manager_agent.tools.cv.pipeline import DetectionPipeline
from agents.lab_manager_agent.tools.cv.recorder import InventoryRecorder
from agents.lab_manager_agent.tools.cv.glass_slide_detector import GlassSlideDetectionConfig
from agents.lab_manager_agent.tools.cv.stage_geometry import px_to_mm, is_calibrated
from common.services import get_instrument_status_service
from common.services.chip_inventory_service import ChipInventoryService


class ScanMaterialsTool(Tool):
    name = "scan_materials"
    description = "Load YOLO detections from disk, persist them to the chip inventory, and return a summary."
    inputs = {
        "detection_path": {
            "type": "string",
            "description": "Path to a JSON detections file (defaults to agents/lab_manager_agent/runtime/latest_yolo_detection_left.json).",
            "nullable": True,
        },
        "frame_path": {
            "type": "string",
            "description": "Optional path to a frame image when carrier processing is desired.",
            "nullable": True,
        },
        "process_carriers": {
            "type": "boolean",
            "description": "Whether to run the carrier polygon processor (requires a frame_path).",
            "default": True,
            "nullable": True,
        },
        "table_side": {
            "type": "string",
            "description": "Camera/table side to use for glass slide detection ('left' or 'right').",
            "nullable": True,
        },
        "persist": {
            "type": "boolean",
            "description": "Whether to persist detections to the database (default False). Set true only if you intend to update the inventory.",
            "default": False,
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(
        self,
        *,
        default_detection_path: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self._default_path = (
            default_detection_path
            or Path("agents") / "lab_manager_agent" / "runtime" / "latest_yolo_detection_left.json"
        )

    def forward(
        self,
        detection_path: Optional[str] = None,
        frame_path: Optional[str] = None,
        process_carriers: Optional[bool] = True,
        table_side: Optional[str] = None,
        persist: Optional[bool] = False,
    ) -> str:
        path = Path(detection_path) if detection_path else self._default_path
        detections = self._load_detections(path)
        frame = None
        frame_used: Optional[Path] = None

        # Handle None explicitly for boolean flag default
        enable_processing = process_carriers if process_carriers is not None else True
        should_persist = persist if persist is not None else False

        if enable_processing and frame_path:
            if cv2 is None:
                raise RuntimeError("OpenCV is required to load frames for carrier processing.")
            frame_used = Path(frame_path)
            frame = cv2.imread(str(frame_used))
            if frame is None:
                raise FileNotFoundError(f"Could not load frame image: {frame_used}")

        pipeline = self._build_pipeline(
            enable_carriers=bool(enable_processing and frame is not None),
            table_side=table_side,
        )
        
        # Determine source: use table_side if provided, otherwise infer from file path
        side = (table_side or "").strip().lower()
        if not side and detection_path:
            # Try to infer from path: check for "right" or "left" in path
            path_str = str(detection_path).lower()
            if "right" in path_str:
                side = "right"
            elif "left" in path_str:
                side = "left"

        if should_persist and side not in ("left", "right"):
            raise ValueError("table_side must be provided ('left' or 'right') when persist=True.")
        
        source = f"{side}_yolo" if side in ("left", "right") else "scan_materials"
        summary = pipeline.process(detections, frame_bgr=frame, source=source, persist=should_persist)
        project_root = Path(__file__).resolve().parents[4]  # agents/lab_manager_agent/tools/cv -> Qumus

        def _relativize(candidate: Optional[Path | str]) -> Optional[str]:
            if not candidate:
                return None
            p = Path(candidate)
            try:
                return str(p.resolve().relative_to(project_root))
            except Exception:
                return str(p.resolve())

        payload = {
            "detections": summary.chip_detections,
            "carrier_slides": summary.carrier_slides,
            "metadata": summary.metadata,
            "detection_file": _relativize(path),
            "frame_file": _relativize(frame_used),
        }
        
        # Add obstruction analysis
        obstruction_data = self._check_obstructions(detections, source=side or "left")
        payload.update(obstruction_data)
        
        # YOLO-based chip slot occupancy verification
        if should_persist:
            # When persisting, also verify chip slot occupancy
            slot_updates = self._verify_chip_slots_from_yolo(
                detections,
                tolerance_mm=20.0,
                frame=side or "left",
                update_db=True  # Update database with verified occupancy
            )
            payload["chip_slot_verification"] = {
                "verified_slots": len(slot_updates),
                "slots": [
                    {
                        "slot_id": u["slot_id"],
                        "slot_label": u["slot_label"],
                        "chip_position": (round(u["chip_x"], 2), round(u["chip_y"], 2)),
                        "distance_mm": round(u["distance_mm"], 2)
                    }
                    for u in slot_updates
                ]
            }
        else:
            # When not persisting, still report what we would verify
            slot_updates = self._verify_chip_slots_from_yolo(
                detections,
                tolerance_mm=20.0,
                frame=side or "left",
                update_db=False  # Don't update, just report
            )
            if slot_updates:
                payload["chip_slot_verification"] = {
                    "verified_slots": len(slot_updates),
                    "note": "Verification not persisted (use persist=True to update database)"
                }

        # Check for latest images to display
        # We look for the "final result" overlay first, then fallback to raw frame
        import time

        # Candidate images in priority order
        side_prefix = (table_side or "left").lower()
        candidates = [
            project_root / "images" / "yolo_captures" / "final_overlays" / f"{side_prefix}_latest_final_result.jpg",
            project_root / "images" / "yolo_captures" / f"{side_prefix}_latest_frame.jpg"
        ]
        
        latest_image = None
        for cand in candidates:
            if cand.exists():
                latest_image = str(cand)
                break
        
        if latest_image:
            payload["scan_image_file"] = _relativize(latest_image)
        
        return json.dumps(payload, ensure_ascii=False)

    def _build_pipeline(self, enable_carriers: bool, table_side: Optional[str]) -> DetectionPipeline:
        glass_config = None
        if enable_carriers:
            side = (table_side or "left").lower()
            if side not in ("left", "right"):
                raise ValueError("table_side must be 'left' or 'right'")
            glass_config = GlassSlideDetectionConfig(table_side=side)
        recorder = InventoryRecorder(glass_config=glass_config)
        return DetectionPipeline(recorder)

    @staticmethod
    def _load_detections(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"No YOLO detections found at {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data  # type: ignore[return-value]
        raise ValueError("YOLO detection file did not contain a list payload")

    def _check_obstructions(self, detections: list[dict[str, Any]], *, source: str = "left") -> dict[str, Any]:
        """
        Check if any non-stage object overlaps with the Exfoliation Stage.
        """
        normalized_source = (source or "left").lower()
        # 1. Determine Stage Position (prefer instrument status, fallback to detection)
        stage_box_mm = None
        
        # Try finding stage in detections first (left: class 0, right: stage labels)
        def _is_stage_detection(det: dict[str, Any]) -> bool:
            label = str(det.get("class") or det.get("label") or "").lower()
            class_id = det.get("class_id")
            if "right" in normalized_source:
                if class_id is not None:
                    try:
                        return int(class_id) in {2, 6}
                    except (TypeError, ValueError):
                        pass
                return "stage" in label
            # left
            if class_id is not None:
                try:
                    if int(class_id) == 0:
                        return True
                except (TypeError, ValueError):
                    pass
            return "exfoliation stage" in label

        stage_det = next((d for d in detections if _is_stage_detection(d)), None)
        
        if stage_det and is_calibrated(normalized_source):
            # Use detected stage box converted to mm
            x_px, y_px = float(stage_det.get("x", 0)), float(stage_det.get("y", 0))
            w_px, h_px = float(stage_det.get("w", 0)), float(stage_det.get("h", 0))
            
            # Simple bounding box approximation
            left_px, right_px = x_px - w_px/2, x_px + w_px/2
            top_px, bottom_px = y_px - h_px/2, y_px + h_px/2
            
            # Convert corners
            tl_mm = px_to_mm(left_px, top_px, source=normalized_source)
            br_mm = px_to_mm(right_px, bottom_px, source=normalized_source)
            
            # Form mm box (min_x, min_y, max_x, max_y)
            stage_box_mm = (
                min(tl_mm[0], br_mm[0]), min(tl_mm[1], br_mm[1]),
                max(tl_mm[0], br_mm[0]), max(tl_mm[1], br_mm[1])
            )
        else:
            # Fallback to stored instrument status
            try:
                status_service = get_instrument_status_service()
                status = status_service.get_latest_status("exfoliation_stage")
                if status and "position" in status:
                    sx = float(status["position"]["x"])
                    sy = float(status["position"]["y"])
                    # Assume generous default size if not detected (e.g. 20mm x 20mm safety zone)
                    stage_box_mm = (sx - 10, sy - 10, sx + 10, sy + 10)
            except Exception:
                pass

        if not stage_box_mm:
            return {"stage_status": "unknown_no_calibration", "obstructions_on_stage": 0, "obstructing_labels": []}

        # 2. Check Overlaps
        obstructions = 0
        obstructing_labels: list[str] = []
        s_min_x, s_min_y, s_max_x, s_max_y = stage_box_mm
        
        for det in detections:
            cid = int(det.get("class_id", -1))
            if _is_stage_detection(det):
                continue  # Skip the stage itself
            
            # object center in px
            cx = float(det.get("x", 0))
            cy = float(det.get("y", 0))
            
            if not is_calibrated(normalized_source):
                 continue

            ox, oy = px_to_mm(cx, cy, source=normalized_source)
            
            # Check if object center is inside stage box
            if s_min_x <= ox <= s_max_x and s_min_y <= oy <= s_max_y:
                obstructions += 1
                label = str(det.get("class") or det.get("label") or f"class_{cid}")
                obstructing_labels.append(label)

        return {
            "stage_status": "occupied" if obstructions > 0 else "clear",
            "obstructions_on_stage": obstructions,
            "obstructing_labels": obstructing_labels,
            "stage_position_mm": {"min_x": round(s_min_x, 2), "max_x": round(s_max_x, 2), 
                                  "min_y": round(s_min_y, 2), "max_y": round(s_max_y, 2)}
        }

    def _verify_chip_slots_from_yolo(
        self,
        detections: list[dict],
        *,
        tolerance_mm: float = 20.0,
        frame: str = "left",
        update_db: bool = False,
    ) -> list[dict]:
        """
        Verify chip slot occupancy based on YOLO detections.

        Args:
            detections: List of YOLO detection dicts with 'class', 'x_mm', 'y_mm' keys
            tolerance_mm: Maximum distance (in mm) for a chip to be considered in a slot
            update_db: If True, update the database with verified occupancy

        Returns:
            List of dicts with slot verification info
        """
        inventory = ChipInventoryService()
        all_slots = inventory.list_robot_accessible_slots()
        
        # Filter for exfoliated chips from detections.
        # NOTE: capture_controller relabels "silicon chip" to "Exfoliated Silicon Chip",
        # so we accept any label that includes both "exfoliated" and "chip".
        exfoliated_chips: list[dict[str, float]] = []
        for det in detections:
            label = str(det.get("class") or det.get("label") or "").lower()
            is_exfoliated = label == "exfoliated_chip" or ("exfoliated" in label and "chip" in label)
            if not is_exfoliated:
                continue

            if "x_mm" in det and "y_mm" in det:
                try:
                    exfoliated_chips.append({
                        "x_mm": float(det["x_mm"]),
                        "y_mm": float(det["y_mm"]),
                    })
                except (TypeError, ValueError):
                    continue
                continue

            # Fallback: compute mm coordinates from px if calibration is active
            if is_calibrated(frame):
                try:
                    x_px = float(det.get("x", 0.0))
                    y_px = float(det.get("y", 0.0))
                except (TypeError, ValueError):
                    continue
                try:
                    x_mm, y_mm = px_to_mm(x_px, y_px, source=frame)
                except Exception:
                    continue
                exfoliated_chips.append({"x_mm": x_mm, "y_mm": y_mm})
        
        # If we can't compute any chip positions, don't clear slots.
        if update_db and not exfoliated_chips:
            return []
        
        updates: list[dict] = []
        occupied_slot_ids: set[int] = set()
        
        # For each detected chip, find the closest slot
        for chip in exfoliated_chips:
            chip_x = float(chip["x_mm"])
            chip_y = float(chip["y_mm"])
            
            closest_slot = None
            closest_distance = float('inf')
            
            use_right = "right" in (frame or "").lower()
            for slot in all_slots:
                slot_x = slot.x_right_mm if use_right else slot.x_left_mm
                slot_y = slot.y_right_mm if use_right else slot.y_left_mm
                if slot_x is None or slot_y is None:
                    continue
                
                # Calculate Euclidean distance
                dx = chip_x - slot_x
                dy = chip_y - slot_y
                distance = (dx**2 + dy**2)**0.5
                
                if distance < closest_distance:
                    closest_distance = distance
                    closest_slot = slot
            
            # If the closest slot is within tolerance, mark it as occupied
            if closest_slot and closest_distance <= tolerance_mm:
                updates.append({
                    "slot_id": closest_slot.id,
                    "slot_label": closest_slot.label,
                    "chip_x": chip_x,
                    "chip_y": chip_y,
                    "distance_mm": closest_distance,
                })
                occupied_slot_ids.add(closest_slot.id)
        
        if update_db:
            self._update_slot_occupancy_from_yolo(occupied_slot_ids, all_slots)
        
        return updates

    @staticmethod
    def _update_slot_occupancy_from_yolo(
        occupied_slot_ids: set[int],
        all_slots: list,
    ) -> None:
        """
        Update the database to reflect YOLO-verified occupancy.
        
        Sets occupied_by=NULL for slots that appear empty in YOLO
        (This is a reconciliation - we trust visual evidence over programmatic moves)
        """
        from database.db_utils import material_session_scope
        from database import models
        
        with material_session_scope() as session:
            for slot_info in all_slots:
                slot_model = session.get(models.ChipSlot, slot_info.id)
                if not slot_model:
                    continue
                
                # If YOLO sees a chip here, keep it marked as occupied
                # If YOLO doesn't see a chip but DB says occupied, clear it
                if slot_info.id not in occupied_slot_ids and slot_model.occupied_by is not None:
                    print(f"⚠️ YOLO Verification: Clearing slot {slot_info.label} "
                          f"(was occupied_by={slot_model.occupied_by}, but no chip detected)")
                    slot_model.occupied_by = None
                    slot_model.chip = None
