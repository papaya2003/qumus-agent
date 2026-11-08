from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func

from database import models
from database.db_utils import material_session_scope


class StampRepository:
    """Persistence helpers for transfer stamps tracked by the lab manager."""

    COORDINATE_TOLERANCE_MM = 3.0

    def _normalize_qr_message(self, qr_message: Optional[str]) -> Optional[str]:
        if qr_message is None:
            return None
        normalized = qr_message.strip()
        if not normalized or normalized.lower() == "not found":
            return None
        return normalized

    def _find_existing_stamp(
        self,
        session,
        *,
        qr_message: Optional[str],
        qr_center_x: Optional[float],
        qr_center_y: Optional[float],
    ) -> models.Stamp | None:
        if qr_message is None or qr_center_x is None or qr_center_y is None:
            return None
        return (
            session.query(models.Stamp)
            .filter(
                models.Stamp.qr_message == qr_message,
                models.Stamp.qr_center_x_mm.isnot(None),
                models.Stamp.qr_center_y_mm.isnot(None),
                func.abs(models.Stamp.qr_center_x_mm - qr_center_x) <= self.COORDINATE_TOLERANCE_MM,
                func.abs(models.Stamp.qr_center_y_mm - qr_center_y) <= self.COORDINATE_TOLERANCE_MM,
            )
            .order_by(models.Stamp.updated_at.desc())
            .first()
        )

    def create_from_slide(
        self,
        *,
        slide,
        source: Optional[str],
        frame_id: Optional[str],
    ) -> models.Stamp:
        qr_message = self._normalize_qr_message(slide.qr_message)
        qr_center_x, qr_center_y = slide.metric.qr_center_mm
        pickup_center_x, pickup_center_y = slide.metric.pickup_center_mm

        with material_session_scope() as session:
            stamp = self._find_existing_stamp(
                session,
                qr_message=qr_message,
                qr_center_x=qr_center_x,
                qr_center_y=qr_center_y,
            )

            if stamp is None:
                stamp = models.Stamp(label=qr_message or "Stamp")
                session.add(stamp)

            stamp.source = source
            stamp.frame_id = frame_id
            stamp.qr_message = qr_message or slide.qr_message
            stamp.qr_confidence = slide.qr_confidence
            stamp.match_score = slide.match_score
            stamp.alignment_score = slide.alignment
            stamp.qr_orientation_deg = slide.qr_orientation_deg
            stamp.pickup_angle_deg = slide.rotate_angle_deg
            stamp.qr_center_x_mm = qr_center_x
            stamp.qr_center_y_mm = qr_center_y
            stamp.pickup_center_x_mm = pickup_center_x
            stamp.pickup_center_y_mm = pickup_center_y
            stamp.metadata_json = {
                "qr_bbox": slide.qr_bbox,
                "pickup_bbox": slide.pickup_bbox,
                "midpoint_px": slide.midpoint_px,
                "qr_center_px": slide.qr_center_px,
                "pickup_center_px": slide.pickup_center_px,
                "crystal_center_mm": slide.metric.crystal_center_mm,
                "slide_angle_deg": slide.slide_angle_deg,
            }

            session.flush()
            session.expunge(stamp)
            return stamp

    def record_usage(self, stamp_id: int, *, adhesion_rating: Optional[float] = None) -> models.Stamp:
        with material_session_scope() as session:
            stamp = session.get(models.Stamp, stamp_id)
            if stamp is None:
                raise ValueError(f"Stamp {stamp_id} not found")
            stamp.usage_count += 1
            stamp.last_used_at = datetime.utcnow()
            if adhesion_rating is not None:
                stamp.adhesion_rating = adhesion_rating
            session.flush()
            session.expunge(stamp)
            return stamp

    def list_stamps(self, *, limit: Optional[int] = None) -> Sequence[models.Stamp]:
        with material_session_scope() as session:
            query = session.query(models.Stamp).order_by(models.Stamp.updated_at.desc())
            if limit is not None:
                query = query.limit(limit)
            rows = query.all()
            for row in rows:
                session.expunge(row)
            return rows
