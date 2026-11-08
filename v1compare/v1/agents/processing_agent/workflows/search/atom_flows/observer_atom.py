from __future__ import annotations

import time

from smolagents import Tool

from agents.processing_agent.tools.observer import start_observer, stop_observer


class AtomStartObserver(Tool):
    name = "atom_start_observer"
    description = "Start the observer recording pipeline."
    inputs = {
        "mag": {"type": "number", "description": "Magnification (10, 20, or 100)."},
        "photo_dir": {"type": "string", "description": "Directory for photos."},
        "result_dir": {"type": "string", "description": "Directory for segmentation results."},
        "result_txt": {"type": "string", "description": "Output results file path."},
        "grow": {"type": "number", "description": "Optional scale factor (default 0.5).", "nullable": True},
        "material_type": {"type": "string", "description": "Material code/name (e.g., Gr, hBN).", "nullable": True},
    }
    output_type = "string"

    def forward(
        self,
        mag: int,
        photo_dir: str,
        result_dir: str,
        result_txt: str,
        grow: float = 0.5,
        material_type: str = "Gr",
    ) -> str:
        start_observer(
            mag=mag,
            photo_dir=photo_dir,
            result_dir=result_dir,
            result_txt=result_txt,
            grow=grow,
            material_type=material_type,
        )
        time.sleep(0.5)
        return "✅ Observer started."


class AtomStopObserver(Tool):
    name = "atom_stop_observer"
    description = "Stop the observer recording pipeline."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        stop_observer()
        time.sleep(0.5)
        return "✅ Observer stopped."
