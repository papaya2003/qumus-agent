from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import func

from database import models
from database.db_utils import material_session_scope


COORDINATE_TOLERANCE_MM = 3.0


class RawMaterialCarrierRepository:
    """Persistence helpers for raw material carrier entries."""

    def _normalize_qr_message(self, qr_message: Optional[str]) -> Optional[str]:
        if qr_message is None:
            return None
        normalized = qr_message.strip()
        if not normalized or normalized.lower() == "not found":
            return None
        return normalized

    def _find_existing_carrier(
        self,
        session,
        *,
        qr_message: Optional[str],
        crystal_center_x: Optional[float],
        crystal_center_y: Optional[float],
    ) -> models.RawMaterialCarrier | None:
        if qr_message is None or crystal_center_x is None or crystal_center_y is None:
            return None
        return (
            session.query(models.RawMaterialCarrier)
            .filter(
                models.RawMaterialCarrier.qr_message == qr_message,
                models.RawMaterialCarrier.crystal_center_x_mm.isnot(None),
                models.RawMaterialCarrier.crystal_center_y_mm.isnot(None),
                func.abs(models.RawMaterialCarrier.crystal_center_x_mm - crystal_center_x) <= COORDINATE_TOLERANCE_MM,
                func.abs(models.RawMaterialCarrier.crystal_center_y_mm - crystal_center_y) <= COORDINATE_TOLERANCE_MM,
            )
            .order_by(models.RawMaterialCarrier.updated_at.desc())
            .first()
        )

    def create_from_slide(
        self,
        *,
        slide,
        source: Optional[str],
        frame_id: Optional[str],
        table_coordinate: Optional[str],
    ) -> models.RawMaterialCarrier:
        qr_message = self._normalize_qr_message(slide.qr_message)
        crystal_center_x, crystal_center_y = slide.metric.crystal_center_mm
        qr_center_x_mm, qr_center_y_mm = slide.metric.qr_center_mm
        pickup_center_x_mm, pickup_center_y_mm = slide.metric.pickup_center_mm

        with material_session_scope() as session:
            carrier = self._find_existing_carrier(
                session,
                qr_message=qr_message,
                crystal_center_x=crystal_center_x,
                crystal_center_y=crystal_center_y,
            )

            if carrier is None:
                carrier = models.RawMaterialCarrier()
                session.add(carrier)

            carrier.source = source
            carrier.frame_id = frame_id
            carrier.qr_message = qr_message or slide.qr_message
            carrier.qr_confidence = slide.qr_confidence
            carrier.match_score = slide.match_score
            carrier.alignment_score = slide.alignment
            carrier.qr_orientation_deg = slide.qr_orientation_deg
            carrier.pickup_angle_deg = slide.rotate_angle_deg
            carrier.slide_angle_deg = slide.slide_angle_deg
            carrier.qr_center_x_mm = qr_center_x_mm
            carrier.qr_center_y_mm = qr_center_y_mm
            carrier.pickup_center_x_mm = pickup_center_x_mm
            carrier.pickup_center_y_mm = pickup_center_y_mm
            carrier.crystal_center_x_mm = crystal_center_x
            carrier.crystal_center_y_mm = crystal_center_y
            carrier.qr_center_x_px = float(slide.qr_center_px[0])
            carrier.qr_center_y_px = float(slide.qr_center_px[1])
            carrier.pickup_center_x_px = float(slide.pickup_center_px[0])
            carrier.pickup_center_y_px = float(slide.pickup_center_px[1])
            carrier.match_distance_px = slide.match_distance_px
            carrier.expected_error_px = slide.expected_error_px
            carrier.table_coordinate = table_coordinate
            carrier.metadata_json = {
                "qr_bbox": slide.qr_bbox,
                "pickup_bbox": slide.pickup_bbox,
                "midpoint_px": slide.midpoint_px,
            }

            session.flush()
            session.expunge(carrier)
            return carrier

    def list_carriers(self, *, limit: Optional[int] = None) -> Sequence[models.RawMaterialCarrier]:
        with material_session_scope() as session:
            query = session.query(models.RawMaterialCarrier).order_by(models.RawMaterialCarrier.updated_at.desc())
            if limit is not None:
                query = query.limit(limit)
            rows = query.all()
            for carrier in rows:
                session.expunge(carrier)
            return rows
