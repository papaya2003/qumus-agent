from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy.orm import joinedload

from database import models
from database.db_utils import material_session_scope


class DeviceRepository:
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

    def create_device(
        self,
        *,
        name: str,
        chip_id: int,
        flake_id: Optional[int] = None,
        status: str = "fabricated",
        notes: Optional[str] = None,
        image_5x_id: Optional[int] = None,
        image_10x_id: Optional[int] = None,
        image_20x_id: Optional[int] = None,
        image_100x_id: Optional[int] = None,
    ) -> models.Device:
        with material_session_scope() as session:
            device = models.Device(
                name=name,
                chip_id=chip_id,
                flake_id=flake_id,
                status=status,
                notes=notes,
                image_5x_id=image_5x_id,
                image_10x_id=image_10x_id,
                image_20x_id=image_20x_id,
                image_100x_id=image_100x_id,
            )
            session.add(device)
            session.flush()
            row = (
                session.query(models.Device)
                .options(
                    joinedload(models.Device.image_5x),
                    joinedload(models.Device.image_10x),
                    joinedload(models.Device.image_20x),
                    joinedload(models.Device.image_100x),
                )
                .filter(models.Device.id == device.id)
                .first()
            )
            if row is None:
                raise RuntimeError(f"Failed to reload device {device.id} after creation.")
            session.expunge(row)
            return row

    def get_device_by_id(self, device_id: int) -> Optional[models.Device]:
        with material_session_scope() as session:
            row = (
                session.query(models.Device)
                .options(
                    joinedload(models.Device.image_5x),
                    joinedload(models.Device.image_10x),
                    joinedload(models.Device.image_20x),
                    joinedload(models.Device.image_100x),
                )
                .filter(models.Device.id == device_id)
                .first()
            )
            if row is not None:
                session.expunge(row)
            return row

    def list_devices(self, *, chip_id: Optional[int] = None) -> Sequence[models.Device]:
        with material_session_scope() as session:
            query = session.query(models.Device).options(
                joinedload(models.Device.image_5x),
                joinedload(models.Device.image_10x),
                joinedload(models.Device.image_20x),
                joinedload(models.Device.image_100x),
            )
            if chip_id is not None:
                query = query.filter(models.Device.chip_id == chip_id)
            rows = query.order_by(models.Device.id).all()
            for row in rows:
                session.expunge(row)
            return rows
