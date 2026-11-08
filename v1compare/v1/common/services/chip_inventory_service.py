from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import inspect as sa_inspect

from common.chip_slot_policy import is_robot_accessible_chip_slot
from database.repositories.chip_inventory_repository import ChipInventoryRepository

# Default fallback coordinates for chip slots when DB lacks precise positions.
DEFAULT_CHIP_SLOT_ORIGIN = (210.0, 150.0)


@dataclass(frozen=True)
class ChipSlotInfo:
    id: int
    label: str

    x_left_mm: Optional[float]
    y_left_mm: Optional[float]
    x_right_mm: Optional[float]
    y_right_mm: Optional[float]
    occupied_by: Optional[int]
    updated_at: Optional[datetime]

    @property
    def is_occupied(self) -> bool:
        return self.occupied_by is not None


@dataclass(frozen=True)
class ChipRecord:
    id: int
    label: str

    table_coordinate_left: Optional[str]
    table_coordinate_right: Optional[str]
    stage_position_x: Optional[float]
    stage_position_y: Optional[float]
    chip_corner_coords: Optional[tuple[float, float]]
    chip_corner_image_path: Optional[str]
    pickup_left_x_mm: Optional[float]
    pickup_left_y_mm: Optional[float]
    pickup_left_angle_deg: Optional[float]
    pickup_right_x_mm: Optional[float]
    pickup_right_y_mm: Optional[float]
    pickup_right_angle_deg: Optional[float]
    angle_deg: Optional[float]
    width_mm: Optional[float]
    height_mm: Optional[float]
    confidence: Optional[float]
    present: bool
    is_searched: bool
    current_slot_id: Optional[int]
    last_updated_date: Optional[datetime]
    metadata: dict[str, Any]
    material_type: Optional[str] = None
    carrier_id: Optional[int] = None


class ChipInventoryService:
    """Service for recording chip moves and detections with slot tracking."""

    def __init__(self, tolerance_mm: float = 40.0, repository: ChipInventoryRepository | None = None):
        self._repository = repository or ChipInventoryRepository(tolerance_mm=tolerance_mm)

    def _to_slot_info(self, slot) -> ChipSlotInfo:
        occupant = slot.occupied_by
        if occupant is None:
            try:
                state = sa_inspect(slot)
                chip_loaded = "chip" not in state.unloaded
            except Exception:
                chip_loaded = False
            if chip_loaded and getattr(slot, "chip", None) is not None:
                occupant = slot.chip.id
        return ChipSlotInfo(
            id=slot.id,
            label=slot.label,

            x_left_mm=slot.x_left_mm,
            y_left_mm=slot.y_left_mm,
            x_right_mm=getattr(slot, "x_right_mm", None),
            y_right_mm=getattr(slot, "y_right_mm", None),
            occupied_by=occupant,
            updated_at=getattr(slot, "updated_at", None),
        )

    def _to_chip_record(self, chip) -> ChipRecord:
        chip_corner_image_path = None
        try:
            chip_state = sa_inspect(chip)
            corner_loaded = "corner_image" not in chip_state.unloaded
        except Exception:
            corner_loaded = False
        if corner_loaded:
            corner_image = getattr(chip, "corner_image", None)
            if corner_image is not None:
                chip_corner_image_path = getattr(corner_image, "path", None)

        return ChipRecord(
            id=chip.id,
            label=chip.label,

            table_coordinate_left=getattr(chip, "table_coordinate_left", None),
            table_coordinate_right=getattr(chip, "table_coordinate_right", None),
            stage_position_x=chip.stage_position_x,
            stage_position_y=chip.stage_position_y,
            chip_corner_coords=(
                (chip.corner_location_x, chip.corner_location_y)
                if chip.corner_location_x is not None and chip.corner_location_y is not None
                else None
            ),
            chip_corner_image_path=chip_corner_image_path,
            pickup_left_x_mm=getattr(chip, "pickup_left_x_mm", None),
            pickup_left_y_mm=getattr(chip, "pickup_left_y_mm", None),
            pickup_left_angle_deg=getattr(chip, "pickup_left_angle_deg", None),
            pickup_right_x_mm=getattr(chip, "pickup_right_x_mm", None),
            pickup_right_y_mm=getattr(chip, "pickup_right_y_mm", None),
            pickup_right_angle_deg=getattr(chip, "pickup_right_angle_deg", None),
            angle_deg=chip.angle_deg,
            width_mm=chip.width_mm,
            height_mm=chip.height_mm,
            confidence=chip.confidence,
            present=getattr(chip, "present", True),
            is_searched=bool(getattr(chip, "is_searched", False)),
            current_slot_id=chip.current_slot_id,
            last_updated_date=chip.last_updated_date,
            metadata=chip.metadata_json or {},
            material_type=getattr(chip, "material_type", None),
            carrier_id=getattr(chip, "carrier_id", None),
        )

    def record_move(
        self,
        label: str,
        *,
        chip_id: Optional[int] = None,
        from_slot_label: Optional[str] = None,
        to_slot_label: Optional[str] = None,
        source: str = "exfoliation",
        correlation_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Log a planned/commanded move and update slot occupancy."""
        self._repository.record_move(
            label,
            chip_id=chip_id,
            from_slot_label=from_slot_label,
            to_slot_label=to_slot_label,
            source=source,
            correlation_id=correlation_id,
            notes=notes,
        )

    def record_detection(
        self,
        label: str,
        *,
        center_x_mm: Optional[float],
        center_y_mm: Optional[float],
        angle_deg: Optional[float] = None,
        width_mm: Optional[float] = None,
        height_mm: Optional[float] = None,
        confidence: Optional[float] = None,
        polygon: Optional[dict] = None,
        source: str = "yolo",
        session_id: Optional[str] = None,
        chip_id: Optional[int] = None,
        frame: str = "left",
    ) -> None:
        """Log an observational detection and update occupancy if a slot matches."""
        self._repository.record_detection(
            label,
            center_x_mm=center_x_mm,
            center_y_mm=center_y_mm,
            angle_deg=angle_deg,
            width_mm=width_mm,
            height_mm=height_mm,
            confidence=confidence,
            polygon=polygon,
            source=source,
            session_id=session_id,
            chip_id=chip_id,
            frame=frame,
        )

    def list_slots(self) -> list[ChipSlotInfo]:
        """Return chip slots (optionally filtered by table) plus occupancy metadata."""
        slots = self._repository.fetch_slots()
        return [self._to_slot_info(slot) for slot in slots]

    def list_robot_accessible_slots(self) -> list[ChipSlotInfo]:
        return [slot for slot in self.list_slots() if is_robot_accessible_chip_slot(slot)]

    def get_slot(self, slot_id: int) -> Optional[ChipSlotInfo]:
        slot = self._repository.fetch_slot_by_id(slot_id)
        if slot is None:
            return None
        return self._to_slot_info(slot)

    def find_slot_by_xy(self, x_mm: float, y_mm: float, frame: str = "left") -> Optional[ChipSlotInfo]:
        slot = self._repository.find_slot_by_xy(x_mm, y_mm, frame)
        if slot is None:
            return None
        return self._to_slot_info(slot)

    def find_first_free_slot(self) -> Optional[ChipSlotInfo]:
        for slot in self.list_robot_accessible_slots():
            if not slot.is_occupied:
                return slot
        return None

    def list_chips(
        self,
        *,
        label: Optional[str] = None,
        present: Optional[bool] = None,
        min_x: Optional[float] = None,
        max_x: Optional[float] = None,
        min_y: Optional[float] = None,
        max_y: Optional[float] = None,
    ) -> list[ChipRecord]:
        rows = self._repository.list_chips(
            label=label,
            present=present,
            min_x=min_x,
            max_x=max_x,
            min_y=min_y,
            max_y=max_y,
        )
        return [self._to_chip_record(row) for row in rows]

    def get_chip(self, label: str) -> Optional[ChipRecord]:
        chip = self._repository.get_chip(label)
        if chip is None:
            return None
        return self._to_chip_record(chip)

    def get_chip_by_id(self, chip_id: int) -> Optional[ChipRecord]:
        chip = self._repository.get_chip_by_id(chip_id)
        if chip is None:
            return None
        return self._to_chip_record(chip)

    def upsert_chip(
        self,
        label: str,
        *,
        chip_id: Optional[int] = None,
        stage_position_x: Optional[float],
        stage_position_y: Optional[float],
        chip_corner_coords: Optional[tuple[float, float]] = None,
        chip_corner_image_path: Optional[str] = None,
        pickup_left_x_mm: Optional[float] = None,
        pickup_left_y_mm: Optional[float] = None,
        pickup_left_angle_deg: Optional[float] = None,
        pickup_right_x_mm: Optional[float] = None,
        pickup_right_y_mm: Optional[float] = None,
        pickup_right_angle_deg: Optional[float] = None,
        angle_deg: Optional[float] = None,
        width_mm: Optional[float] = None,
        height_mm: Optional[float] = None,
        confidence: Optional[float] = None,
        present: Optional[bool] = None,
        table_coordinate_left: Optional[str] = None,
        table_coordinate_right: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ChipRecord:
        chip = self._repository.upsert_chip(
            label=label,
            chip_id=chip_id,
            stage_position_x=stage_position_x,
            stage_position_y=stage_position_y,
            corner_location_x=chip_corner_coords[0] if chip_corner_coords is not None else None,
            corner_location_y=chip_corner_coords[1] if chip_corner_coords is not None else None,
            corner_image_path=chip_corner_image_path,
            pickup_left_x_mm=pickup_left_x_mm,
            pickup_left_y_mm=pickup_left_y_mm,
            pickup_left_angle_deg=pickup_left_angle_deg,
            pickup_right_x_mm=pickup_right_x_mm,
            pickup_right_y_mm=pickup_right_y_mm,
            pickup_right_angle_deg=pickup_right_angle_deg,
            angle_deg=angle_deg,
            width_mm=width_mm,
            height_mm=height_mm,
            confidence=confidence,
            present=present,
            table_coordinate_left=table_coordinate_left,
            table_coordinate_right=table_coordinate_right,
            metadata=metadata,
        )
        return self._to_chip_record(chip)

    def promote_chip(
        self,
        chip_id: int,
        *,
        new_label: str,
        stage_position_x: Optional[float],
        stage_position_y: Optional[float],
        chip_corner_coords: Optional[tuple[float, float]] = None,
        chip_corner_image_path: Optional[str] = None,
        pickup_left_x_mm: Optional[float] = None,
        pickup_left_y_mm: Optional[float] = None,
        pickup_left_angle_deg: Optional[float] = None,
        pickup_right_x_mm: Optional[float] = None,
        pickup_right_y_mm: Optional[float] = None,
        pickup_right_angle_deg: Optional[float] = None,
        angle_deg: Optional[float] = None,
        width_mm: Optional[float] = None,
        height_mm: Optional[float] = None,
        confidence: Optional[float] = None,
        present: Optional[bool] = None,
        table_coordinate_left: Optional[str] = None,
        table_coordinate_right: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        material_type: Optional[str] = None,
        carrier_id: Optional[int] = None,
    ) -> ChipRecord:
        chip = self._repository.promote_chip(
            chip_id,
            new_label=new_label,
            stage_position_x=stage_position_x,
            stage_position_y=stage_position_y,
            corner_location_x=chip_corner_coords[0] if chip_corner_coords is not None else None,
            corner_location_y=chip_corner_coords[1] if chip_corner_coords is not None else None,
            corner_image_path=chip_corner_image_path,
            pickup_left_x_mm=pickup_left_x_mm,
            pickup_left_y_mm=pickup_left_y_mm,
            pickup_left_angle_deg=pickup_left_angle_deg,
            pickup_right_x_mm=pickup_right_x_mm,
            pickup_right_y_mm=pickup_right_y_mm,
            pickup_right_angle_deg=pickup_right_angle_deg,
            angle_deg=angle_deg,
            width_mm=width_mm,
            height_mm=height_mm,
            confidence=confidence,
            present=present,
            table_coordinate_left=table_coordinate_left,
            table_coordinate_right=table_coordinate_right,
            metadata=metadata,
            material_type=material_type,
            carrier_id=carrier_id,
        )
        return self._to_chip_record(chip)

    def clear_chip_positions(
        self,
        chip_ids: list[int],
        *,
        metadata_updates: Optional[dict[str, Any]] = None,
    ) -> int:
        return self._repository.clear_chip_positions(chip_ids, metadata_updates=metadata_updates)

    def get_present_chip_ids_by_source(self, source: str) -> list[int]:
        return self._repository.fetch_present_chip_ids_by_source(source)

    def set_chip_presence(
        self,
        chip_ids: list[int],
        *,
        present: bool,
        source: Optional[str] = None,
        metadata_updates: Optional[dict[str, Any]] = None,
    ) -> int:
        return self._repository.set_chip_presence(
            chip_ids,
            present=present,
            source=source,
            metadata_updates=metadata_updates,
        )

    def set_chip_searched(self, chip_id: int, *, is_searched: bool = True) -> bool:
        return self._repository.set_chip_searched(chip_id, is_searched=is_searched)
