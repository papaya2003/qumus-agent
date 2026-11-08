from __future__ import annotations

from typing import Optional, Sequence

from common.services import ChipInventoryService

from .glass_slide_detector import GlassSlideDetectionConfig, GlassSlideDetectionOutput, GlassSlideDetector
from .silicon_detector import SiliconChipDetector
from .qr_utils import prepare_table_qr_data


class InventoryRecorder:
    """High-level helper that persists detections via ChipInventoryService."""

    def __init__(
        self,
        *,
        inventory: ChipInventoryService | None = None,
        glass_config: GlassSlideDetectionConfig | None = None,
    ):
        self.inventory = inventory or ChipInventoryService()
        self.chip_detector = SiliconChipDetector(self.inventory)
        self.glass_detector: GlassSlideDetector | None = None
        if glass_config is not None:
            self.glass_detector = GlassSlideDetector(glass_config, chip_repository=self.inventory)

    def record_chips(self, detections: Sequence[dict], *, source: str = "scan_materials") -> None:
        self.chip_detector.record(detections, source=source)

    def record_carriers(
        self,
        frame_bgr,
        detections: Sequence[dict],
        *,
        frame_id: str | None = None,
        source: str | None = None,
    ) -> GlassSlideDetectionOutput | None:
        if not self.glass_detector:
            return None
        if frame_bgr is not None:
            prepare_table_qr_data(frame_bgr, detections, source=source)
        return self.glass_detector.process_frame(frame_bgr, detections, frame_id=frame_id, source=source)
