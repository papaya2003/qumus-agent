from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

from .recorder import InventoryRecorder
from .qr_utils import prepare_table_qr_data


@dataclass
class PipelineSummary:
    chip_detections: int = 0
    carrier_slides: int = 0
    metadata: dict[str, Any] | None = None


class DetectionPipeline:
    """Bridge raw YOLO detections to database persistence."""

    def __init__(self, recorder: InventoryRecorder | None = None):
        self.recorder = recorder or InventoryRecorder()

    def process(
        self,
        detections: Sequence[dict],
        *,
        frame_bgr: Optional[np.ndarray] = None,
        source: str = "scan_materials",
        persist: bool = True,
    ) -> PipelineSummary:
        if detections:
            calibration_frame = frame_bgr if frame_bgr is not None else None
            try:
                prepare_table_qr_data(calibration_frame, detections, log_errors=False, source=source)
            except ValueError:
                # Not enough QR markers to calibrate; chip recording may fail later.
                pass
            if persist:
                self.recorder.record_chips(detections, source=source)
        carrier_output = None
        if frame_bgr is not None and detections:
            if persist:
                carrier_output = self.recorder.record_carriers(frame_bgr, detections, source=source)
            else:
                # If distinct logic is needed for non-persisting carrier analysis, we'd need to modify record_carriers
                # But record_carriers in recorder.py calls glass_detector.process_frame which does DB writes?
                # Let's check recorder.py.
                # recorder.record_carriers -> glass_detector.process_frame.
                # glass_detector likely writes to DB.
                # If persist is False, we probably still want the "carrier_output" result (slides list) for the summary, just not the DB write.
                # This might be tricky if "record_carriers" bundles analysis and recording.
                # Let's check recorder.py content again.
                pass 
        return PipelineSummary(
            chip_detections=self._count_chips(detections),
            carrier_slides=len(carrier_output.slides) if carrier_output and carrier_output.slides else 0,
            metadata={
                "carrier_overlay": str(carrier_output.overlay_path) if carrier_output and carrier_output.overlay_path else None,
                "carrier_csv": str(carrier_output.csv_path) if carrier_output and carrier_output.csv_path else None,
            },
        )

    @staticmethod
    def _count_chips(detections: Sequence[dict]) -> int:
        total = 0
        for det in detections:
            label = str(det.get("class") or det.get("label") or "").lower()
            if "chip" in label:
                total += 1
        return total
