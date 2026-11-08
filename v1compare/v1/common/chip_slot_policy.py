from __future__ import annotations

from typing import Any


ROBOT_ACCESSIBLE_CHIP_SLOT_IDS = frozenset({1, 2, 5, 6, 9, 10, 13})
ROBOT_BLOCKED_CHIP_SLOT_IDS = frozenset({3, 4, 7, 8, 11, 12, 14})


def chip_slot_label_to_id(label: str | None) -> int | None:
    if not label:
        return None
    normalized = str(label).strip().upper()
    if not normalized.startswith("S"):
        return None
    try:
        return int(normalized[1:])
    except ValueError:
        return None


def get_chip_slot_id(slot: Any) -> int | None:
    slot_id = getattr(slot, "id", None)
    if slot_id is not None:
        try:
            return int(slot_id)
        except (TypeError, ValueError):
            return None
    return chip_slot_label_to_id(getattr(slot, "label", None))


def is_robot_accessible_chip_slot(slot: Any) -> bool:
    slot_id = get_chip_slot_id(slot)
    return slot_id in ROBOT_ACCESSIBLE_CHIP_SLOT_IDS


def is_robot_accessible_chip_slot_label(label: str | None) -> bool:
    slot_id = chip_slot_label_to_id(label)
    return slot_id in ROBOT_ACCESSIBLE_CHIP_SLOT_IDS
