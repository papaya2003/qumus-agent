from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from database.repositories.bottom_electrode_repository import BottomElectrodeRepository


@dataclass(frozen=True)
class BottomElectrodeRecord:
    id: int
    chip_id: int
    location_x: Optional[float]
    location_y: Optional[float]
    image_id: Optional[int]
    image_20x_id: Optional[int]
    image_100x_path: Optional[str]
    image_20x_path: Optional[str]


class BottomElectrodeService:
    """High-level interface for recording and querying bottom electrodes."""

    def __init__(self, repository: BottomElectrodeRepository | None = None) -> None:
        self._repo = repository or BottomElectrodeRepository()

    @staticmethod
    def _to_record(row) -> BottomElectrodeRecord:
        return BottomElectrodeRecord(
            id=row.id,
            chip_id=row.chip_id,
            location_x=row.location_x,
            location_y=row.location_y,
            image_id=row.image_id,
            image_20x_id=getattr(row, "image_20x_id", None),
            image_100x_path=row.image.path if getattr(row, "image", None) is not None else None,
            image_20x_path=row.image_20x.path if getattr(row, "image_20x", None) is not None else None,
        )

    def record_bottom_electrode(
        self,
        *,
        chip_id: int,
        location_x: Optional[float] = None,
        location_y: Optional[float] = None,
        image_100x_path: Optional[str] = None,
        image_20x_path: Optional[str] = None,
    ) -> BottomElectrodeRecord:
        image_id = None
        if image_100x_path:
            img = self._repo.upsert_image(
                path=image_100x_path,
                image_type="bottom_electrode",
                source="lab_manager",
                metadata=None,
            )
            image_id = img.id
        image_20x_id = None
        if image_20x_path:
            img_20 = self._repo.upsert_image(
                path=image_20x_path,
                image_type="bottom_electrode_20x",
                source="lab_manager",
                metadata=None,
            )
            image_20x_id = img_20.id

        row = self._repo.upsert_bottom_electrode(
            chip_id=chip_id,
            location_x=location_x,
            location_y=location_y,
            image_id=image_id,
            image_20x_id=image_20x_id,
        )
        return self._to_record(row)

    def get_bottom_electrode_by_id(self, bottom_electrode_id: int) -> Optional[BottomElectrodeRecord]:
        row = self._repo.get_bottom_electrode_by_id(bottom_electrode_id)
        return None if row is None else self._to_record(row)

    def list_bottom_electrodes(
        self,
        *,
        chip_id: Optional[int] = None,
    ) -> list[BottomElectrodeRecord]:
        rows = self._repo.list_bottom_electrodes(chip_id=chip_id)
        return [self._to_record(row) for row in rows]
