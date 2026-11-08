from typing import List, Tuple, Dict, Any, Optional
from common.services import ChipInventoryService, RawMaterialCarrierService
from common.services.instrument_status_service import get_instrument_status_service

DEFAULT_EXFOLIATION_STAGE_CENTER = (0.0, 262.0)

def get_exfoliation_stage_bounds(
    padding_mm: float = 10.0,
    status_service = None,
    prefer_detected: bool = False,
) -> Tuple[Dict[str, float], Tuple[float, float], str]:
    """
    Calculates the bounds of the exfoliation stage.
    Returns: (bounds_dict, (center_x, center_y), warning_message)
    """
    if status_service is None:
        status_service = get_instrument_status_service()
        
    sx, sy = DEFAULT_EXFOLIATION_STAGE_CENTER
    warning = "Using default exfoliation stage center (0.0, 262.0)."

    if prefer_detected:
        stage_status = status_service.get_latest_status("exfoliation_stage")
        if stage_status and "position" in stage_status:
            pos = stage_status["position"]
            sx, sy = float(pos.get("x", sx)), float(pos.get("y", sy))
            warning = ""
        else:
            warning = "⚠️ Detection unavailable. Using default exfoliation stage center (0.0, 262.0)."

    STAGE_WIDTH = 50.0 
    STAGE_HEIGHT = 50.0

    bounds = {
        "min_x": sx - (STAGE_WIDTH / 2) - padding_mm,
        "max_x": sx + (STAGE_WIDTH / 2) + padding_mm,
        "min_y": sy - (STAGE_HEIGHT / 2) - padding_mm,
        "max_y": sy + (STAGE_HEIGHT / 2) + padding_mm
    }
    return bounds, (sx, sy), warning

def find_exfoliation_obstructions(padding_mm: float = 10.0) -> Tuple[List[Any], List[Any], str]:
    """
    Finds all silicon chips and raw material carriers located on the Exfoliation Stage.
    
    Args:
        padding_mm: Extra margin around the stage to consider. Default 10.0mm.
        
    Returns:
        Tuple of:
        - List of Chip objects found
        - List of Carrier objects found
        - Description string of what was checked (provenance info)
    """
    
    # 1. Services
    chip_service = ChipInventoryService()
    carrier_service = RawMaterialCarrierService()
    status_service = get_instrument_status_service()
    
    # 2. Determine Stage Position
    bounds, (sx, sy), warning = get_exfoliation_stage_bounds(padding_mm, status_service)
    
    # 3. Find Chips
    chips_on_stage = chip_service.list_chips(
        min_x=bounds["min_x"], max_x=bounds["max_x"],
        min_y=bounds["min_y"], max_y=bounds["max_y"]
    )
    
    # 4. Find Carriers
    all_carriers = carrier_service.list_carriers()
    carriers_on_stage = []
    
    for c in all_carriers:
        cx = c.pickup_center_x_mm
        cy = c.pickup_center_y_mm
        
        # Simple bounds check
        if cx is not None and cy is not None:
            if (bounds["min_x"] <= cx <= bounds["max_x"] and 
                bounds["min_y"] <= cy <= bounds["max_y"]):
                carriers_on_stage.append(c)

    provenance = f"{warning}Checked stage centered at ({sx:.1f}, {sy:.1f}) with {padding_mm}mm padding."
    
    return chips_on_stage, carriers_on_stage, provenance
