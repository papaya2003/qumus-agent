from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from database import models
from database.db_utils import material_session_scope


class FlakeRepository:
    """Persistence helpers for flake entries in the material database."""

    # Pixel size in µm per pixel at each magnification
    PIXEL_SIZE_UM_MAP = {
        10: 0.365,
        20: 0.182,
        100: 0.036,
    }

    def get_next_type_index(self, type_label: str) -> int:
        """Return the next available type_index for a given type_label globally."""
        with material_session_scope() as session:
            max_idx = (
                session.query(func.max(models.Flake.type_index))
                .filter(
                    models.Flake.type_label == type_label,
                )
                .scalar()
            )
            return (max_idx or 0) + 1

    def upsert_flake(
        self,
        *,
        chip_id: int,
        type_label: str,
        type_index: int,
        thickness_layers: float,
        thickness_label: str,
        location_x: Optional[float] = None,
        location_y: Optional[float] = None,
        size_area_px: Optional[float] = None,
        magnification: int = 10,
        image_id: Optional[int] = None,
        image_20x_id: Optional[int] = None,
        mask_path: Optional[str] = None,
        mask_20x_path: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> models.Flake:
        """Insert or update a flake row.

        size_area_px is converted to µm² using the pixel size for the given magnification.
        """
        size_area_um = None
        if size_area_px is not None:
            px_size = self.PIXEL_SIZE_UM_MAP.get(magnification, 0.365)
            size_area_um = size_area_px * (px_size ** 2)

        with material_session_scope() as session:
            # Check for existing flake with same chip + type_label + type_index
            flake = (
                session.query(models.Flake)
                .filter(
                    models.Flake.chip_id == chip_id,
                    models.Flake.type_label == type_label,
                    models.Flake.type_index == type_index,
                )
                .first()
            )

            if flake is None:
                flake = models.Flake(
                    chip_id=chip_id,
                    type_label=type_label,
                    type_index=type_index,
                    date_created=datetime.utcnow(),
                    is_used=False,
                )
                session.add(flake)

            flake.thickness_layers = thickness_layers
            flake.thickness_label = thickness_label
            flake.location_x = location_x
            flake.location_y = location_y
            flake.size_area_um = size_area_um
            flake.image_id = image_id
            flake.image_20x_id = image_20x_id
            flake.mask_path = mask_path
            flake.mask_20x_path = mask_20x_path
            flake.metadata_json = metadata

            session.flush()
            session.expunge(flake)
            return flake

    def upsert_image(
        self,
        *,
        path: str,
        image_type: str = "autosearch",
        source: str = "segmenter",
        metadata: Optional[dict] = None,
    ) -> models.Image:
        """Create or retrieve an Image row for a given path."""
        with material_session_scope() as session:
            img = (
                session.query(models.Image)
                .filter(models.Image.path == path)
                .first()
            )
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

    def list_flakes(
        self,
        *,
        chip_id: Optional[int] = None,
        type_label: Optional[str] = None,
        min_area_um: Optional[float] = None,
    ) -> Sequence[models.Flake]:
        """Query flakes with optional filters."""
        with material_session_scope() as session:
            query = session.query(models.Flake).options(
                selectinload(models.Flake.image),
                selectinload(models.Flake.image_20x),
            )
            if chip_id is not None:
                query = query.filter(models.Flake.chip_id == chip_id)
            if type_label is not None:
                query = query.filter(models.Flake.type_label == type_label)
            if min_area_um is not None:
                query = query.filter(models.Flake.size_area_um >= min_area_um)
            query = query.order_by(models.Flake.type_index)
            rows = query.all()
            for f in rows:
                session.expunge(f)
            return rows
