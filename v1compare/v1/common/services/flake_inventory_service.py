from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import inspect as sa_inspect

from common.material_semantics import normalize_material_code
from database.repositories.flake_repository import FlakeRepository


# Thickness mapping: segmenter key → numeric layer count
THICKNESS_MAP = {
    "monolayer": 1.0,
    "bilayer": 2.0,
    "trilayer": 3.0,
    "fewlayer": 4.0,
    "manylayer": 10.0,
    # hBN thickness bins (stored as representative bin centers)
    "0-10": 5.0,
    "10-20": 15.0,
    "20-30": 25.0,
    "30-40": 35.0,
    "40+": 40.0,
}


@dataclass(frozen=True)
class FlakeRecord:
    """Immutable snapshot of a Flake row."""
    id: int
    chip_id: int
    type_label: str
    type_index: int
    thickness_layers: Optional[float]
    thickness_label: Optional[str]
    location_x: Optional[float]
    location_y: Optional[float]
    size_area_um: Optional[float]
    image_id: Optional[int]
    image_20x_id: Optional[int]
    mask_path: Optional[str]
    mask_20x_path: Optional[str]
    image_100x_path: Optional[str]
    image_20x_path: Optional[str]


class FlakeInventoryService:
    """High-level interface for recording and querying flakes."""

    def __init__(self, repository: FlakeRepository | None = None) -> None:
        self._repo = repository or FlakeRepository()

    @staticmethod
    def _safe_image_path(flake, relation_name: str) -> Optional[str]:
        try:
            state = sa_inspect(flake)
            if relation_name in state.unloaded:
                return None
        except Exception:
            pass

        relation = getattr(flake, "__dict__", {}).get(relation_name)
        if relation is None:
            return None
        return getattr(relation, "path", None)

    @staticmethod
    def _to_record(flake) -> FlakeRecord:
        return FlakeRecord(
            id=flake.id,
            chip_id=flake.chip_id,
            type_label=flake.type_label,
            type_index=flake.type_index,
            thickness_layers=flake.thickness_layers,
            thickness_label=flake.thickness_label,
            location_x=flake.location_x,
            location_y=flake.location_y,
            size_area_um=flake.size_area_um,
            image_id=flake.image_id,
            image_20x_id=getattr(flake, "image_20x_id", None),
            mask_path=getattr(flake, "mask_path", None),
            mask_20x_path=getattr(flake, "mask_20x_path", None),
            image_100x_path=FlakeInventoryService._safe_image_path(flake, "image"),
            image_20x_path=FlakeInventoryService._safe_image_path(flake, "image_20x"),
        )

    def record_flake(
        self,
        *,
        chip_id: int,
        type_label: str,
        thickness_label: str,
        location_x: Optional[float] = None,
        location_y: Optional[float] = None,
        size_area_px: Optional[float] = None,
        magnification: int = 10,
        image_path: Optional[str] = None,
        image_20x_path: Optional[str] = None,
        mask_path: Optional[str] = None,
        mask_20x_path: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> FlakeRecord:
        """Record a single flake detected during autosearch.

        Args:
            chip_id: The chip being searched.
            type_label: Material code (e.g. "Gr", "hBN", "ReSe2"). Note: "Gr" means graphite; graphene is "Gr" + monolayer.
            thickness_label: Segmenter key (e.g. "monolayer", "bilayer", "10-20", "20-30").
            location_x/y: Stage coordinates (mm).
            size_area_px: Flake size in pixel area.
            magnification: Microscope magnification (10, 20, 100).
            image_path: Path to the 100X flake image (creates/links Image row).
            image_20x_path: Optional path to the paired 20X flake image.
            mask_path: Optional path to the segmented 100X mask image for the same flake.
            mask_20x_path: Optional path to the segmented 20X mask image for the same flake.
            metadata: Extra JSON metadata.
        """
        # Normalize material naming to canonical DB codes.
        normalized_type_label = normalize_material_code(type_label) or type_label

        # Resolve thickness_layers from label
        thickness_layers = THICKNESS_MAP.get(thickness_label, None)

        # Auto-assign type_index globally for this type_label
        type_index = self._repo.get_next_type_index(normalized_type_label)

        # Create Image row if path provided
        image_id = None
        if image_path:
            img = self._repo.upsert_image(
                path=image_path,
                image_type="autosearch",
                source="segmenter",
                metadata={"magnification": magnification},
            )
            image_id = img.id
        image_20x_id = None
        if image_20x_path:
            img_20x = self._repo.upsert_image(
                path=image_20x_path,
                image_type="autosearch_20x",
                source="segmenter",
                metadata={"magnification": 20},
            )
            image_20x_id = img_20x.id

        flake = self._repo.upsert_flake(
            chip_id=chip_id,
            type_label=normalized_type_label,
            type_index=type_index,
            thickness_layers=thickness_layers,
            thickness_label=thickness_label,
            location_x=location_x,
            location_y=location_y,
            size_area_px=size_area_px,
            magnification=magnification,
            image_id=image_id,
            image_20x_id=image_20x_id,
            mask_path=mask_path,
            mask_20x_path=mask_20x_path,
            metadata=metadata,
        )
        return self._to_record(flake)

    def list_flakes(
        self,
        *,
        chip_id: Optional[int] = None,
        type_label: Optional[str] = None,
        min_area_um: Optional[float] = None,
    ) -> list[FlakeRecord]:
        """Query flakes with optional filters."""
        rows = self._repo.list_flakes(
            chip_id=chip_id, type_label=type_label, min_area_um=min_area_um
        )
        return [self._to_record(f) for f in rows]
