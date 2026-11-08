from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from smolagents import Tool

from agents.lab_manager_agent.tools.cv.recorder import InventoryRecorder
from agents.lab_manager_agent.tools.cv.silicon_detector import SiliconChipDetector
from agents.lab_manager_agent.tools.cv.stage_geometry import is_calibrated, px_to_mm
from common.logic.exfoliation_logic import find_exfoliation_obstructions
from common.logic.exfoliation_logic import get_exfoliation_stage_bounds
from common.services.chip_inventory_service import ChipInventoryService
from common.services.instrument_status_service import get_instrument_status_service
from common.services.raw_material_carrier_service import RawMaterialCarrierService
from database.repositories.chip_inventory_repository import CHIP_POSITION_TOLERANCE_MM

class CheckStageObstructionTool(Tool):
    name = "check_stage_obstruction"
    description = "Check the database for any chips OR carriers located on the Exfoliation Stage. Returns occupied status and list of obstructions."
    inputs = {
        "padding_mm": {
            "type": "number",
            "description": "Extra padding around stage area to check (default 10.0).",
            "default": 10.0,
            "nullable": True
        },
        "refresh_before_query": {
            "type": "boolean",
            "description": "If true (default), re-import the latest YOLO detections file before checking the DB.",
            "default": True,
            "nullable": True,
        },
        "detection_path": {
            "type": "string",
            "description": "Optional override path for the YOLO detections JSON (defaults to runtime/latest left file).",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, *, detection_path: Optional[Path] = None):
        super().__init__()
        self._inventory_refresher = InventoryRecorder()
        self._chip_inventory = ChipInventoryService()
        self._default_detection_path = (
            detection_path
            or Path("agents") / "lab_manager_agent" / "runtime" / "latest_yolo_detection_left.json"
        )

    def forward(
        self,
        padding_mm: float = 10.0,
        refresh_before_query: Optional[bool] = True,
        detection_path: Optional[str] = None,
    ) -> str:
        # Use valid default if None passed
        if padding_mm is None:
            padding_mm = 10.0

        refresh_flag = refresh_before_query if refresh_before_query is not None else True
        refresh_result = None
        if refresh_flag:
            refresh_result = self._refresh_inventory_from_scan(detection_path, padding_mm)
            
        chips, carriers, provenance = find_exfoliation_obstructions(padding_mm=padding_mm)
        
        # Filter logic: Ensure we don't count "exfoliation stage" label if it exists (though shared logic queries by bounds)
        chip_obstructions = [c for c in chips if "exfoliation stage" not in c.label.lower()]
        carrier_obstructions = carriers
        
        obstructions_count = len(chip_obstructions) + len(carrier_obstructions)
        
        obstructing_items = []
        for c in chip_obstructions:
             obstructing_items.append({
                 "type": "chip", 
                 "id": c.id, 
                 "label": c.label, 
                 "x": c.stage_position_x, 
                 "y": c.stage_position_y
             })
        for c in carrier_obstructions:
             obstructing_items.append({
                 "type": "carrier", 
                 "id": c.id, 
                 "label": getattr(c, "qr_message", "unknown_carrier"),
                 "x": c.pickup_center_x_mm, 
                 "y": c.pickup_center_y_mm
             })

        result = {
            "stage_status": "occupied" if obstructions_count > 0 else "clear",
            "obstructions_count": obstructions_count,
            "obstructing_items": obstructing_items,
            "obstructing_items": obstructing_items,
            "provenance": provenance
        }

        if refresh_result is not None:
            result["refresh"] = refresh_result
        
        return json.dumps(result, ensure_ascii=False)

    def _refresh_inventory_from_scan(self, detection_path: Optional[str], padding_mm: float) -> dict[str, object]:
        """
        Load the latest YOLO detections and persist them so the DB is up-to-date for this query.
        """
        path = Path(detection_path) if detection_path else self._default_detection_path
        if not path.exists():
            return {
                "attempted": True,
                "success": False,
                "message": f"No detection file found at {path}",
            }
        try:
            raw = path.read_text(encoding="utf-8")
            detections = json.loads(raw)
            if not isinstance(detections, list):
                raise ValueError("Detection file payload must be a list.")
        except Exception as exc:
            return {
                "attempted": True,
                "success": False,
                "message": f"Failed to load detections: {exc}",
            }

        try:
            self._inventory_refresher.record_chips(detections, source="check_stage_refresh")
            cleanup = self._prune_stale_stage_chips(detections, padding_mm)
        except Exception as exc:
            return {
                "attempted": True,
                "success": False,
                "message": f"Failed to persist detections: {exc}",
            }

        result = {
            "attempted": True,
            "success": True,
            "message": f"Persisted {len(detections)} detection(s) from {path}",
        }
        if cleanup is not None:
            result["stale_cleanup"] = cleanup
        return result

    def _prune_stale_stage_chips(self, detections: list[dict], padding_mm: float) -> Optional[dict[str, object]]:
        if not detections:
            return {"attempted": False, "message": "No detections available; skipped stale cleanup."}
        if not is_calibrated():
            return {"attempted": False, "message": "Stage calibration missing; skipped stale cleanup."}

        bounds, _, _ = get_exfoliation_stage_bounds(padding_mm)
        detected_positions: list[tuple[float, float]] = []
        for det in detections:
            if not SiliconChipDetector._is_silicon_chip(det):
                continue
            try:
                x_mm, y_mm = px_to_mm(float(det.get("x", 0.0)), float(det.get("y", 0.0)))
            except Exception:
                continue
            if (
                bounds["min_x"] <= x_mm <= bounds["max_x"]
                and bounds["min_y"] <= y_mm <= bounds["max_y"]
            ):
                detected_positions.append((x_mm, y_mm))

        chips_on_stage = self._chip_inventory.list_chips(
            min_x=bounds["min_x"],
            max_x=bounds["max_x"],
            min_y=bounds["min_y"],
            max_y=bounds["max_y"],
        )
        if not chips_on_stage:
            return {"attempted": True, "cleared": 0}

        stale_ids: list[int] = []
        for chip in chips_on_stage:
            cx = chip.stage_position_x
            cy = chip.stage_position_y
            if cx is None or cy is None:
                continue
            if any(
                (abs(cx - dx) <= CHIP_POSITION_TOLERANCE_MM and abs(cy - dy) <= CHIP_POSITION_TOLERANCE_MM)
                for dx, dy in detected_positions
            ):
                continue
            stale_ids.append(chip.id)

        if not stale_ids:
            return {"attempted": True, "cleared": 0}

        cleared = self._chip_inventory.clear_chip_positions(
            stale_ids,
            metadata_updates={
                "stale_cleared_at": datetime.utcnow().isoformat() + "Z",
                "stale_reason": "stage_not_detected",
            },
        )
        return {"attempted": True, "cleared": cleared, "chip_ids": stale_ids}
