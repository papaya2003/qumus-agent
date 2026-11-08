from __future__ import annotations

from typing import Optional, Sequence

from database import models
from database.db_utils import material_session_scope
from sqlalchemy.orm import joinedload


class BottomElectrodeRepository:
    """Persistence helpers for bottom-electrode entries in the material database."""

    def upsert_image(
        self,
        *,
        path: str,
        image_type: str,
        source: str,
        metadata: Optional[dict] = None,
    ) -> models.Image:
        with material_session_scope() as session:
            img = session.query(models.Image).filter(models.Image.path == path).first()
            if img is None:
                img = models.Image(
                    path=path,
                    type=image_type,
                    source=source,
                    metadata_json=metadata,
                )
                session.add(img)
                session.flush()
            session.expunge(img)
            return img

    def upsert_bottom_electrode(
        self,
        *,
        chip_id: int,
        location_x: Optional[float] = None,
        location_y: Optional[float] = None,
        image_id: Optional[int] = None,
        image_20x_id: Optional[int] = None,
    ) -> models.BottomElectrode:
        with material_session_scope() as session:
            query = session.query(models.BottomElectrode).filter(models.BottomElectrode.chip_id == chip_id)
            electrode = query.first()

            if electrode is None:
                electrode = models.BottomElectrode(
                    chip_id=chip_id,
                )
                session.add(electrode)

            electrode.location_x = location_x
            electrode.location_y = location_y
            electrode.image_id = image_id
            electrode.image_20x_id = image_20x_id

            session.flush()
            session.expunge(electrode)
            return electrode

    def get_bottom_electrode_by_id(self, bottom_electrode_id: int) -> Optional[models.BottomElectrode]:
        with material_session_scope() as session:
            row = (
                session.query(models.BottomElectrode)
                .options(
                    joinedload(models.BottomElectrode.image),
                    joinedload(models.BottomElectrode.image_20x),
                )
                .filter(models.BottomElectrode.id == bottom_electrode_id)
                .first()
            )
            if row is not None:
                session.expunge(row)
            return row

    def list_bottom_electrodes(
        self,
        *,
        chip_id: Optional[int] = None,
    ) -> Sequence[models.BottomElectrode]:
        with material_session_scope() as session:
            query = session.query(models.BottomElectrode).options(
                joinedload(models.BottomElectrode.image),
                joinedload(models.BottomElectrode.image_20x),
            )
            if chip_id is not None:
                query = query.filter(models.BottomElectrode.chip_id == chip_id)
            rows = query.order_by(models.BottomElectrode.id).all()
            for row in rows:
                session.expunge(row)
            return rows
