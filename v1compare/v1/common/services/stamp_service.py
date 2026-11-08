from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from database.repositories.stamp_repository import StampRepository


@dataclass(frozen=True)
class StampRecord:
    id: int
    label: Optional[str]
    status: str
    usage_count: int
    last_used_at: Optional[datetime]
    adhesion_rating: Optional[float]
    notes: Optional[str]
    metadata: dict


class StampService:
    """Service wrapper for interacting with StampRepository."""

    def __init__(self, repository: StampRepository | None = None) -> None:
        self._repository = repository or StampRepository()

    def record_usage(self, stamp_id: int, *, adhesion_rating: Optional[float] = None) -> StampRecord:
        row = self._repository.record_usage(stamp_id, adhesion_rating=adhesion_rating)
        return self._to_record(row)

    def list_stamps(self, *, limit: Optional[int] = None) -> list[StampRecord]:
        rows = self._repository.list_stamps(limit=limit)
        return [self._to_record(row) for row in rows]

    def record_from_slide(
        self,
        slide,
        *,
        source: Optional[str],
        frame_id: Optional[str],
    ) -> StampRecord:
        row = self._repository.create_from_slide(
            slide=slide,
            source=source,
            frame_id=frame_id,
        )
        return self._to_record(row)

    def _to_record(self, row) -> StampRecord:
        return StampRecord(
            id=row.id,
            label=row.label,
            status=row.status,
            usage_count=row.usage_count,
            last_used_at=row.last_used_at,
            adhesion_rating=row.adhesion_rating,
            notes=row.notes,
            metadata=row.metadata_json or {},
        )
