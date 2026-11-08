from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from database.repositories.device_repository import DeviceRepository


@dataclass(frozen=True)
class DeviceRecord:
    id: int
    name: str
    chip_id: int
    flake_id: Optional[int]
    status: str
    notes: Optional[str]
    image_5x_id: Optional[int]
    image_10x_id: Optional[int]
    image_20x_id: Optional[int]
    image_100x_id: Optional[int]
    image_5x_path: Optional[str]
    image_10x_path: Optional[str]
    image_20x_path: Optional[str]
    image_100x_path: Optional[str]


class DeviceInventoryService:
    def __init__(self, repository: DeviceRepository | None = None) -> None:
        self._repo = repository or DeviceRepository()

    @staticmethod
    def _to_record(row) -> DeviceRecord:
        return DeviceRecord(
            id=row.id,
            name=row.name,
            chip_id=row.chip_id,
            flake_id=row.flake_id,
            status=row.status,
            notes=row.notes,
            image_5x_id=getattr(row, "image_5x_id", None),
            image_10x_id=getattr(row, "image_10x_id", None),
            image_20x_id=getattr(row, "image_20x_id", None),
            image_100x_id=getattr(row, "image_100x_id", None),
            image_5x_path=row.image_5x.path if getattr(row, "image_5x", None) is not None else None,
            image_10x_path=row.image_10x.path if getattr(row, "image_10x", None) is not None else None,
            image_20x_path=row.image_20x.path if getattr(row, "image_20x", None) is not None else None,
            image_100x_path=row.image_100x.path if getattr(row, "image_100x", None) is not None else None,
        )

    def record_device(
        self,
        *,
        name: str,
        chip_id: int,
        flake_id: Optional[int] = None,
        status: str = "fabricated",
        notes: Optional[str] = None,
        image_5x_path: Optional[str] = None,
        image_10x_path: Optional[str] = None,
        image_20x_path: Optional[str] = None,
        image_100x_path: Optional[str] = None,
    ) -> DeviceRecord:
        image_5x_id = None
        if image_5x_path:
            img = self._repo.upsert_image(
                path=image_5x_path,
                image_type="device_5x",
                source="processing_agent",
                metadata=None,
            )
            image_5x_id = img.id
        image_10x_id = None
        if image_10x_path:
            img10 = self._repo.upsert_image(
                path=image_10x_path,
                image_type="device_10x",
                source="processing_agent",
                metadata=None,
            )
            image_10x_id = img10.id
        image_20x_id = None
        if image_20x_path:
            img20 = self._repo.upsert_image(
                path=image_20x_path,
                image_type="device_20x",
                source="processing_agent",
                metadata=None,
            )
            image_20x_id = img20.id
        image_100x_id = None
        if image_100x_path:
            img100 = self._repo.upsert_image(
                path=image_100x_path,
                image_type="device_100x",
                source="processing_agent",
                metadata=None,
            )
            image_100x_id = img100.id

        row = self._repo.create_device(
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
        return self._to_record(row)
