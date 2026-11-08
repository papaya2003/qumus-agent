from __future__ import annotations

"""
Seed default chip slots into the database so YOLO detections and workflow moves
can resolve slot labels to rows. Run this once after adding the new chip_slot schema.
"""

from database.db_utils import init_db, material_session_scope
from database import models

# 14 available chip slots with explicit coordinates.
# Format: (x_mm, y_mm)
CHIP_SLOT_COORDINATES_LEFT = [
    (200, 160),  # S1 
    (240, 160),  # S2 
    (280, 160),  # S3 
    (320, 160),  # S4 
    (200, 110),  # S5 
    (240, 110),  # S6 
    (280, 110),  # S7 
    (320, 110),  # S8
    (200, 60),   # S9
    (240, 60),   # S10
    (280, 60),   # S11
    (320, 60),   # S12
    (240, 20),   # S13
    (280, 20),   # S14
]

CHIP_SLOT_COORDINATES_RIGHT = [
    (-320, 160),  # S1 
    (-280, 160),  # S2 
    (-240, 160),  # S3 
    (-200, 160),  # S4 
    (-320, 110),  # S5 
    (-280, 110),  # S6 
    (-240, 110),  # S7 
    (-200, 110),  # S8
    (-320, 60),   # S9
    (-280, 60),   # S10
    (-240, 60),   # S11
    (-200, 60),   # S12
    (-280, 20),   # S13
    (-240, 20),   # S14
]


def seed_default_chip_slots(*, init: bool = True) -> tuple[int, int]:
    """Upsert chip slots with explicit coordinates and visibility metadata."""
    if init:
        init_db()
    created = 0
    updated = 0
    
    if len(CHIP_SLOT_COORDINATES_LEFT) != len(CHIP_SLOT_COORDINATES_RIGHT):
        raise ValueError("Left/right chip slot coordinate lists must be the same length.")

    with material_session_scope() as session:
        for idx, ((x_left, y_left), (x_right, y_right)) in enumerate(
            zip(CHIP_SLOT_COORDINATES_LEFT, CHIP_SLOT_COORDINATES_RIGHT, strict=True)
        ):
            label = f"S{idx + 1}"
            slot = session.query(models.ChipSlot).filter(models.ChipSlot.label == label).one_or_none()
            x_left = float(x_left)
            y_left = float(y_left)
            x_right = float(x_right)
            y_right = float(y_right)

            if slot is None:
                slot = models.ChipSlot(
                    label=label,
                    x_left_mm=x_left,
                    y_left_mm=y_left,
                    x_right_mm=x_right,
                    y_right_mm=y_right,
                )
                session.add(slot)
                created += 1
            else:
                # Update existing slot with new coordinates
                slot.x_left_mm = x_left
                slot.y_left_mm = y_left
                slot.x_right_mm = x_right
                slot.y_right_mm = y_right
                updated += 1
    
    return created, updated


if __name__ == "__main__":
    n_created, n_updated = seed_default_chip_slots()
    print(f"Seeded chip slots: created {n_created}, updated {n_updated}")
