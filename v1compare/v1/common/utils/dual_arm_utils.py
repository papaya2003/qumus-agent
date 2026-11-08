"""Helper utilities for dual-arm robot system coordinate conversion."""

from typing import Literal

from database.models import ChipSlot, Chip

ArmSelection = Literal["left", "right"]

def get_chip_coordinates_for_arm(
    chip: Chip,
    target_arm: ArmSelection
) -> tuple[float, float]:
    """
    Get chip coordinates transformed for the target robot arm.
    
    Args:
        chip: Chip object with per-arm pickup coordinates
        target_arm: Which arm will use these coordinates ("left" or "right")
        
    Returns:
        (x, y) coordinates for the target arm
        
    Raises:
        ValueError: If chip doesn't have pickup coordinates
    """
    if target_arm == "left":
        if chip.pickup_left_x_mm is not None and chip.pickup_left_y_mm is not None:
            return (chip.pickup_left_x_mm, chip.pickup_left_y_mm)
    if target_arm == "right":
        if chip.pickup_right_x_mm is not None and chip.pickup_right_y_mm is not None:
            return (chip.pickup_right_x_mm, chip.pickup_right_y_mm)

    raise ValueError(f"Chip {chip.label} has no pickup coordinates for {target_arm} arm")


def get_slot_coordinates_for_arm(
    chip_slot: ChipSlot,
    target_arm: ArmSelection
) -> tuple[float, float]:
    """
    Get chip slot coordinates transformed for the target robot arm.
    
    Args:
        chip_slot: ChipSlot object with x_mm and y_mm (in canonical left frame)
        target_arm: Which arm will use these coordinates ("left" or "right")
        
    Returns:
        (x, y) coordinates for the target arm
        
    Raises:
        ValueError: If slot doesn't have coordinates
    """
    if target_arm == "right":
        if chip_slot.x_right_mm is None or chip_slot.y_right_mm is None:
            raise ValueError(f"ChipSlot {chip_slot.label} has no right-arm coordinates")
        return (chip_slot.x_right_mm, chip_slot.y_right_mm)

    if chip_slot.x_left_mm is None or chip_slot.y_left_mm is None:
        raise ValueError(f"ChipSlot {chip_slot.label} has no left-arm coordinates")
    return (chip_slot.x_left_mm, chip_slot.y_left_mm)
