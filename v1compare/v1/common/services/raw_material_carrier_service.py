from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from database.repositories.raw_material_carrier_repository import RawMaterialCarrierRepository


@dataclass(frozen=True)
class RawMaterialCarrierRecord:
    id: int
    table_coordinate: Optional[str]
    qr_message: Optional[str]
    qr_confidence: Optional[float]
    match_score: Optional[float]
    alignment_score: Optional[float]
    pickup_angle_deg: Optional[float]
    slide_angle_deg: Optional[float]
    pickup_center_x_mm: Optional[float]
    pickup_center_y_mm: Optional[float]
    qr_center_x_mm: Optional[float]
    qr_center_y_mm: Optional[float]


class RawMaterialCarrierService:
    """High-level interface for persisting and querying raw material carriers."""

    def __init__(self, repository: RawMaterialCarrierRepository | None = None) -> None:
        self._repository = repository or RawMaterialCarrierRepository()

    def _to_record(self, carrier) -> RawMaterialCarrierRecord:
        return RawMaterialCarrierRecord(
            id=carrier.id,
            table_coordinate=carrier.table_coordinate,
            qr_message=carrier.qr_message,
            qr_confidence=carrier.qr_confidence,
            match_score=carrier.match_score,
            alignment_score=carrier.alignment_score,
            pickup_angle_deg=carrier.pickup_angle_deg,
            slide_angle_deg=carrier.slide_angle_deg,
            pickup_center_x_mm=carrier.pickup_center_x_mm,
            pickup_center_y_mm=carrier.pickup_center_y_mm,
            qr_center_x_mm=carrier.qr_center_x_mm,
            qr_center_y_mm=carrier.qr_center_y_mm,
        )

    def record_from_slide(
        self,
        slide,
        *,
        source: Optional[str],
        frame_id: Optional[str],
        table_coordinate: Optional[str],
    ) -> RawMaterialCarrierRecord:
        row = self._repository.create_from_slide(
            slide=slide,
            source=source,
            frame_id=frame_id,
            table_coordinate=table_coordinate,
        )
        return self._to_record(row)

    def list_carriers(self, *, limit: Optional[int] = None) -> list[RawMaterialCarrierRecord]:
        rows = self._repository.list_carriers(limit=limit)
        return [self._to_record(row) for row in rows]
