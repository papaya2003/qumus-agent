from __future__ import annotations

import json
import threading
from typing import Optional

from smolagents import Tool

from agents.lab_manager_agent.tools.cv.capture_controller import LiveYOLOController
from agents.lab_manager_agent.tools.cv.config import LiveYOLOConfig

_LIVE_YOLO_CAPTURE_LOCK = threading.Lock()


class LiveYOLOTool(Tool):
    name = "live_yolo_capture"
    description = "Run the live YOLO capture loop with overlays/logging (mirrors yolo_cv/live_yolo_test.py)."
    inputs = {
        "camera_view": {
            "type": "string",
            "description": "Camera selection strategy: 'both' (default, runs left then right sequentially), 'left', or 'right'. Use 'both' for full lab scans. Use 'left' for exfoliation-stage checks, raw material carriers, and bare silicon chips. Use 'right' for search/stacking-stage checks, exfoliated chip storage, stamps, and electrode chips.",
            "nullable": True,
            "enum": ["left", "right", "both"],
        },
        "max_frames": {
            "type": "integer",
            "description": "Number of frames to process. Defaults to 10 if omitted.",
            "nullable": True,
        },
        "write_latest": {
            "type": "boolean",
            "description": "Whether to update the latest_yolo_detection.json file with the latest detections.",
            "nullable": True,
        },
        "display_window": {
            "type": "boolean",
            "description": "Show the OpenCV preview window (set false for headless execution).",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, config: Optional[LiveYOLOConfig] = None):
        super().__init__()
        self._default_config = config or LiveYOLOConfig()

    def forward(
        self,
        camera_view: Optional[str] = "both",
        max_frames: Optional[int] = None,
        write_latest: Optional[bool] = None,
        display_window: Optional[bool] = None,
    ) -> str:
        view = camera_view if camera_view else "both"
        with _LIVE_YOLO_CAPTURE_LOCK:
            if view == "both":
                # Run Left
                summary_left = self._run_once("left", max_frames, write_latest, display_window)
                # Run Right
                summary_right = self._run_once("right", max_frames, write_latest, display_window)

                # Combine summaries
                combined = {
                    "left_camera": summary_left,
                    "right_camera": summary_right,
                    "note": "Executed sequential scan (Left -> Right)."
                }
                return json.dumps(combined, ensure_ascii=False)
            else:
                summary = self._run_once(view, max_frames, write_latest, display_window)
                return json.dumps(summary, ensure_ascii=False)

    def _run_once(
        self,
        view: str,
        max_frames: Optional[int],
        write_latest: Optional[bool],
        display_window: Optional[bool],
    ) -> dict:
        config_args = {
            "camera_view": view,
            "root": self._default_config.root,
        }
        
        # Create config for this specific run
        current_config = LiveYOLOConfig(
            camera_view=view,
            root=self._default_config.root
        )
        
        controller = LiveYOLOController(current_config)
        effective_max_frames = max_frames if max_frames and max_frames > 0 else 10
        
        result = controller.run(
            max_frames=effective_max_frames,
            write_latest=write_latest,
            display_window=display_window,
        )

        # If a processed overlay exists (from the glass slide detector), suppress the raw YOLO overlay
        # so the UI only displays the final processed result.
        if result.get("processed_overlay"):
            result["yolo_capture_overlay"] = None
            
        return result
