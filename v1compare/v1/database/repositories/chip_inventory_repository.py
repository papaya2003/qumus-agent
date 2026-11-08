from __future__ import annotations

import math
from typing import Any, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from common.chip_slot_policy import is_robot_accessible_chip_slot_label
from common.material_semantics import normalize_material_code
from database import models
from database.db_utils import material_session_scope


CHIP_POSITION_TOLERANCE_MM = 3.0
CHIP_SIZE_TOLERANCE_MM = 3.0
CHIP_SLOT_WIDTH_MM = 20.0
CHIP_SLOT_HEIGHT_MM = 30.0
CHIP_SLOT_SELECTION_TOLERANCE_MM = 1.0


class ChipInventoryRepository:
    """Database accessors for chip, slot, move, and detection tables."""

    def __init__(self, tolerance_mm: float = 40.0):
        self.tolerance_mm = tolerance_mm

    def _ensure_chip(self, session, label: str) -> models.Chip:
        chip = session.query(models.Chip).filter(models.Chip.label == label).first()
        if chip is None:
            chip = models.Chip(label=label)
            session.add(chip)
            session.flush()
        return chip

    def _assign_slot(self, session, chip: models.Chip, slot: Optional[models.ChipSlot]) -> None:
        if chip.current_slot_id and chip.current_slot_id != (slot.id if slot else None):
            prev_slot = session.get(models.ChipSlot, chip.current_slot_id)
            if prev_slot:
                prev_slot.chip = None
                prev_slot.occupied_by = None
        if slot:
            slot.chip = chip
            chip.current_slot_id = slot.id
            slot.occupied_by = chip.id
        else:
            chip.current_slot_id = None

    def _find_slot_by_label(self, session, label: str) -> Optional[models.ChipSlot]:
        query = session.query(models.ChipSlot).filter(models.ChipSlot.label == label)
        return query.first()

    @staticmethod
    def _slot_coords(slot: models.ChipSlot, frame: str) -> tuple[Optional[float], Optional[float]]:
        if "right" in frame.lower():
            x = slot.x_right_mm
            y = slot.y_right_mm
            return x, y
        return slot.x_left_mm, slot.y_left_mm

    def _point_in_slot(self, slot: models.ChipSlot, x_mm: float, y_mm: float, frame: str) -> bool:
        slot_x, slot_y = self._slot_coords(slot, frame)
        if slot_x is None or slot_y is None:
            return False
        half_w = 0.5 * CHIP_SLOT_WIDTH_MM + CHIP_SLOT_SELECTION_TOLERANCE_MM
        half_h = 0.5 * CHIP_SLOT_HEIGHT_MM + CHIP_SLOT_SELECTION_TOLERANCE_MM
        return (
            (slot_x - half_w) <= x_mm <= (slot_x + half_w)
            and (slot_y - half_h) <= y_mm <= (slot_y + half_h)
        )

    def _find_slot_by_xy(self, session, x_mm: float, y_mm: float, frame: str = "left") -> Optional[models.ChipSlot]:
        slots = [
            slot
            for slot in session.query(models.ChipSlot).all()
            if is_robot_accessible_chip_slot_label(slot.label)
        ]
        if not slots:
            return None

        # Prefer rectangular containment over distance tolerance so detections
        # inside a slot footprint always latch to that slot.
        containing_slots: list[models.ChipSlot] = []
        for slot in slots:
            if self._point_in_slot(slot, x_mm, y_mm, frame):
                containing_slots.append(slot)

        if containing_slots:
            # Deterministically choose the closest center if multiple slots overlap.
            containing_slots.sort(
                key=lambda slot: math.hypot(
                    (self._slot_coords(slot, frame)[0] or 0) - x_mm,
                    (self._slot_coords(slot, frame)[1] or 0) - y_mm,
                )
            )
            return containing_slots[0]

        # Fallback: previous distance-based tolerance for noisy detections.
        best = None
        best_dist = None
        for slot in slots:
            slot_x, slot_y = self._slot_coords(slot, frame)
            if slot_x is None or slot_y is None:
                continue
            dist = math.hypot((slot_x - x_mm), (slot_y - y_mm))
            if best is None or dist < best_dist:
                best = slot
                best_dist = dist
        if best is None or best_dist is None:
            return None
        if self.tolerance_mm and best_dist > self.tolerance_mm:
            return None
        return best

    def fetch_slots(self) -> Sequence[models.ChipSlot]:
        with material_session_scope() as session:
            query = session.query(models.ChipSlot)
            # Order by ID (numeric) instead of label (alphabetic) to get proper sequential order
            # This prevents S1, S10, S11, S2, S3... and gives S1, S2, S3... S10, S11...
            slots = query.options(selectinload(models.ChipSlot.chip)).order_by(models.ChipSlot.id.asc()).all()
            for slot in slots:
                session.expunge(slot)
            return slots

    def fetch_slot_by_id(self, slot_id: int) -> Optional[models.ChipSlot]:
        with material_session_scope() as session:
            slot = session.query(models.ChipSlot).options(selectinload(models.ChipSlot.chip)).filter(models.ChipSlot.id == slot_id).one_or_none()
            if slot is not None:
                session.expunge(slot)
            return slot

    def find_slot_by_xy(self, x_mm: float, y_mm: float, frame: str = "left") -> Optional[models.ChipSlot]:
        with material_session_scope() as session:
            slot = self._find_slot_by_xy(session, x_mm, y_mm, frame)
            if slot is not None:
                session.expunge(slot)
            return slot

    def list_chips(
        self,
        *,
        label: Optional[str] = None,
        present: Optional[bool] = None,
        min_x: Optional[float] = None,
        max_x: Optional[float] = None,
        min_y: Optional[float] = None,
        max_y: Optional[float] = None,
    ) -> Sequence[models.Chip]:
        with material_session_scope() as session:
            query = session.query(models.Chip)
            if label is not None:
                query = query.filter(models.Chip.label == label)
            if present is not None:
                query = query.filter(models.Chip.present == present)
            if min_x is not None:
                query = query.filter(models.Chip.stage_position_x >= min_x)
            if max_x is not None:
                query = query.filter(models.Chip.stage_position_x <= max_x)
            if min_y is not None:
                query = query.filter(models.Chip.stage_position_y >= min_y)
            if max_y is not None:
                query = query.filter(models.Chip.stage_position_y <= max_y)
            rows = query.options(selectinload(models.Chip.corner_image)).order_by(models.Chip.id.asc()).all()
            for chip in rows:
                session.expunge(chip)
            return rows

    def get_chip(self, label: str) -> Optional[models.Chip]:
        with material_session_scope() as session:
            query = session.query(models.Chip).options(selectinload(models.Chip.corner_image)).filter(models.Chip.label == label)
            chip = query.one_or_none()
            if chip is not None:
                session.expunge(chip)
            return chip

    def get_chip_by_id(self, chip_id: int) -> Optional[models.Chip]:
        with material_session_scope() as session:
            chip = (
                session.query(models.Chip)
                .options(selectinload(models.Chip.corner_image))
                .filter(models.Chip.id == chip_id)
                .one_or_none()
            )
            if chip is not None:
                session.expunge(chip)
            return chip

    def _upsert_image(self, session, *, path: str, image_type: str, source: str, metadata: Optional[dict] = None) -> models.Image:
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
        return img

    def _find_existing_chip(
        self,
        session,
        *,
        label: str,
        stage_position_x: Optional[float],
        stage_position_y: Optional[float],
        width_mm: Optional[float],
        height_mm: Optional[float],
    ) -> models.Chip | None:

        query = session.query(models.Chip)
        
        if stage_position_x is None or stage_position_y is None:
            return None
            
        # Spatial lookup
        query = query.filter(
            models.Chip.stage_position_x.isnot(None),
            models.Chip.stage_position_y.isnot(None),
            func.abs(models.Chip.stage_position_x - stage_position_x) <= CHIP_POSITION_TOLERANCE_MM,
            func.abs(models.Chip.stage_position_y - stage_position_y) <= CHIP_POSITION_TOLERANCE_MM,
        )
        
        # Optional size check
        if width_mm is not None:
            query = query.filter(
                models.Chip.width_mm.isnot(None),
                func.abs(models.Chip.width_mm - width_mm) <= CHIP_SIZE_TOLERANCE_MM,
            )
        if height_mm is not None:
            query = query.filter(
                models.Chip.height_mm.isnot(None),
                func.abs(models.Chip.height_mm - height_mm) <= CHIP_SIZE_TOLERANCE_MM,
            )
            
        return query.order_by(models.Chip.last_updated_date.desc()).first()

    def upsert_chip(
        self,
        label: str,
        *,
        chip_id: Optional[int] = None,
        stage_position_x: Optional[float],
        stage_position_y: Optional[float],
        corner_location_x: Optional[float] = None,
        corner_location_y: Optional[float] = None,
        corner_image_path: Optional[str] = None,
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
    ) -> models.Chip:
        with material_session_scope() as session:
            chip = None
            if chip_id is not None:
                chip = session.get(models.Chip, chip_id)
            if chip is None:
                chip = self._find_existing_chip(
                    session,
                    label=label,
                    stage_position_x=stage_position_x,
                    stage_position_y=stage_position_y,
                    width_mm=width_mm,
                    height_mm=height_mm,
                )
            if chip is None:
                corner_image_id = None
                if corner_image_path:
                    corner_img = self._upsert_image(
                        session,
                        path=corner_image_path,
                        image_type="chip_corner",
                        source="lab_manager",
                        metadata=metadata,
                    )
                    corner_image_id = corner_img.id
                chip = models.Chip(
                    label=label,
                    stage_position_x=stage_position_x,
                    stage_position_y=stage_position_y,
                    corner_location_x=corner_location_x,
                    corner_location_y=corner_location_y,
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
                    present=True if present is None else bool(present),
                    table_coordinate_left=table_coordinate_left,
                    table_coordinate_right=table_coordinate_right,
                    metadata_json=metadata or {},
                    corner_image_id=corner_image_id,
                )
                session.add(chip)
            else:
                chip.stage_position_x = stage_position_x
                chip.stage_position_y = stage_position_y
                if corner_location_x is not None:
                    chip.corner_location_x = corner_location_x
                if corner_location_y is not None:
                    chip.corner_location_y = corner_location_y
                chip.pickup_left_x_mm = pickup_left_x_mm
                chip.pickup_left_y_mm = pickup_left_y_mm
                chip.pickup_left_angle_deg = pickup_left_angle_deg
                chip.pickup_right_x_mm = pickup_right_x_mm
                chip.pickup_right_y_mm = pickup_right_y_mm
                chip.pickup_right_angle_deg = pickup_right_angle_deg
                chip.angle_deg = angle_deg
                chip.width_mm = width_mm
                chip.height_mm = height_mm
                chip.confidence = confidence
                if present is not None:
                    chip.present = bool(present)
                chip.table_coordinate_left = table_coordinate_left
                chip.table_coordinate_right = table_coordinate_right
                if metadata is not None:
                    chip.metadata_json = metadata
                if corner_image_path:
                    corner_img = self._upsert_image(
                        session,
                        path=corner_image_path,
                        image_type="chip_corner",
                        source="lab_manager",
                        metadata=metadata,
                    )
                    chip.corner_image_id = corner_img.id
            session.flush()
            session.expunge(chip)
            return chip

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
        with material_session_scope() as session:
            if to_slot_label and not is_robot_accessible_chip_slot_label(to_slot_label):
                raise ValueError(
                    f"Chip slot '{to_slot_label}' is reserved to remain open and cannot be used by the robot arm"
                )
            if from_slot_label and not is_robot_accessible_chip_slot_label(from_slot_label):
                raise ValueError(
                    f"Chip slot '{from_slot_label}' is reserved to remain open and cannot be accessed by the robot arm"
                )
            if chip_id is not None:
                chip = session.get(models.Chip, chip_id)
                if chip is None:
                    # Fallback if invalid ID provided
                    chip = self._ensure_chip(session, label)
            else:
                chip = self._ensure_chip(session, label)
            from_slot = self._find_slot_by_label(session, from_slot_label) if from_slot_label else None
            to_slot = self._find_slot_by_label(session, to_slot_label) if to_slot_label else None

            self._assign_slot(session, chip, to_slot)

            session.add(
                models.ChipMove(
                    chip_id=chip.id,
                    from_slot_id=from_slot.id if from_slot else None,
                    to_slot_id=to_slot.id if to_slot else None,
                    source=source,
                    correlation_id=correlation_id,
                    notes=notes,
                )
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
        with material_session_scope() as session:
            slot = None
            if center_x_mm is not None and center_y_mm is not None:
                slot = self._find_slot_by_xy(session, center_x_mm, center_y_mm, frame)
            
            if chip_id is not None:
                chip = session.get(models.Chip, chip_id)
                if chip is None:
                    # Fallback if ID invalid (shouldn't happen if passed from upsert)
                    chip = self._ensure_chip(session, label)
            else:
                chip = self._ensure_chip(session, label)

            if slot:
                self._assign_slot(session, chip, slot)

            session.add(
                models.ChipDetection(
                    chip_id=chip.id,
                    slot_id=slot.id if slot else None,
                    label=label,
                    confidence=confidence,
                    center_x_mm=center_x_mm,
                    center_y_mm=center_y_mm,
                    angle_deg=angle_deg,
                    width_mm=width_mm,
                    height_mm=height_mm,
                    polygon_json=polygon,
                    source=source,
                    session_id=session_id,
                )
            )

    def promote_chip(
        self,
        chip_id: int,
        *,
        new_label: str,
        stage_position_x: Optional[float],
        stage_position_y: Optional[float],
        corner_location_x: Optional[float] = None,
        corner_location_y: Optional[float] = None,
        corner_image_path: Optional[str] = None,
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
    ) -> models.Chip:
        with material_session_scope() as session:
            chip = session.get(models.Chip, chip_id)
            if chip is None:
                raise ValueError(f"Chip id {chip_id} not found")
            chip.label = new_label
            chip.stage_position_x = stage_position_x
            chip.stage_position_y = stage_position_y
            if corner_location_x is not None:
                chip.corner_location_x = corner_location_x
            if corner_location_y is not None:
                chip.corner_location_y = corner_location_y
            chip.pickup_left_x_mm = pickup_left_x_mm
            chip.pickup_left_y_mm = pickup_left_y_mm
            chip.pickup_left_angle_deg = pickup_left_angle_deg
            chip.pickup_right_x_mm = pickup_right_x_mm
            chip.pickup_right_y_mm = pickup_right_y_mm
            chip.pickup_right_angle_deg = pickup_right_angle_deg
            chip.angle_deg = angle_deg
            chip.width_mm = width_mm
            chip.height_mm = height_mm
            chip.confidence = confidence
            if present is not None:
                chip.present = bool(present)

            chip.table_coordinate_left = table_coordinate_left
            chip.table_coordinate_right = table_coordinate_right
            if material_type is not None:
                chip.material_type = normalize_material_code(material_type)
            if carrier_id is not None:
                chip.carrier_id = carrier_id
            if metadata is not None:
                chip.metadata_json = metadata
            if corner_image_path:
                corner_img = self._upsert_image(
                    session,
                    path=corner_image_path,
                    image_type="chip_corner",
                    source="lab_manager",
                    metadata=metadata,
                )
                chip.corner_image_id = corner_img.id
            session.flush()
            session.expunge(chip)
            return chip

    def fetch_present_chip_ids_by_source(self, source: str) -> list[int]:
        normalized_source = (source or "").strip().lower()
        if not normalized_source:
            return []
        with material_session_scope() as session:
            rows = (
                session.query(models.Chip.id, models.Chip.metadata_json)
                .filter(models.Chip.present.is_(True))
                .all()
            )
            chip_ids: list[int] = []
            for chip_id, metadata in rows:
                md = metadata or {}
                if not isinstance(md, dict):
                    continue
                last_detected_by = str(md.get("last_detected_by") or "").strip().lower()
                if last_detected_by == normalized_source:
                    chip_ids.append(int(chip_id))
            return chip_ids

    def set_chip_presence(
        self,
        chip_ids: Sequence[int],
        *,
        present: bool,
        source: Optional[str] = None,
        metadata_updates: Optional[dict[str, Any]] = None,
    ) -> int:
        if not chip_ids:
            return 0
        with material_session_scope() as session:
            chips = session.query(models.Chip).filter(models.Chip.id.in_(chip_ids)).all()
            for chip in chips:
                chip.present = bool(present)
                current = chip.metadata_json or {}
                if not isinstance(current, dict):
                    current = {}
                if source:
                    current["last_presence_updated_by"] = source
                if metadata_updates:
                    current.update(metadata_updates)
                chip.metadata_json = current
            return len(chips)

    def clear_chip_positions(
        self,
        chip_ids: Sequence[int],
        *,
        metadata_updates: Optional[dict[str, Any]] = None,
    ) -> int:
        if not chip_ids:
            return 0
        with material_session_scope() as session:
            chips = session.query(models.Chip).filter(models.Chip.id.in_(chip_ids)).all()
            for chip in chips:
                chip.stage_position_x = None
                chip.stage_position_y = None
                chip.pickup_left_x_mm = None
                chip.pickup_left_y_mm = None
                chip.pickup_left_angle_deg = None
                chip.pickup_right_x_mm = None
                chip.pickup_right_y_mm = None
                chip.pickup_right_angle_deg = None
                chip.angle_deg = None
                if metadata_updates:
                    current = chip.metadata_json or {}
                    chip.metadata_json = {**current, **metadata_updates}
            return len(chips)

    def set_chip_searched(
        self,
        chip_id: int,
        *,
        is_searched: bool = True,
    ) -> bool:
        with material_session_scope() as session:
            chip = session.get(models.Chip, int(chip_id))
            if chip is None:
                return False
            chip.is_searched = bool(is_searched)
            return True
