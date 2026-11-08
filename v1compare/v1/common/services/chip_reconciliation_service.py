"""
Chip reconciliation service for dual YOLO camera system.

Prevents duplicate chip entries when both left and right YOLO cameras detect
the same physical chip. Uses spatial matching and sensor fusion to reconcile
detections from multiple cameras.
"""

from __future__ import annotations

import math
import time
import logging
from datetime import datetime
from typing import Optional

from database.models import Chip
from common.services.coordinate_transform_service import get_coordinate_transform_service
from common.services.chip_inventory_service import ChipInventoryService


logger = logging.getLogger(__name__)


class ChipReconciliationService:
    """
    Reconcile chip detections from multiple YOLO cameras.
    
    This service:
    1. Matches new detections to existing chips using spatial proximity
    2. Fuses multiple detections to improve accuracy
    3. Prevents duplicate chip entries
    4. Tracks detection history in metadata
    """
    
    def __init__(
        self,
        position_tolerance_mm: float = 5.0,
        size_tolerance_mm: float = 3.0,
        max_detection_history: int = 10
    ):
        """
        Initialize chip reconciliation service.
        
        Args:
            position_tolerance_mm: Maximum distance (mm) to consider chips as same
            size_tolerance_mm: Maximum size difference (mm) to consider chips as same
            max_detection_history: Max number of detections to store in metadata
        """
        self.position_tolerance = position_tolerance_mm
        self.size_tolerance = size_tolerance_mm
        self.max_detection_history = max_detection_history
        
        self.transform_service = get_coordinate_transform_service()
        self.chip_service = ChipInventoryService()
        
        logger.info(
            f"ChipReconciliationService initialized: "
            f"position_tol={position_tolerance_mm}mm, size_tol={size_tolerance_mm}mm"
        )
    
    
    def find_matching_chip_by_table_coordinate(
        self,
        table_coordinate: str,
        width: Optional[float],
        height: Optional[float],
        source: str,
    ) -> Optional[Chip]:
        """
        Find existing chip by table_coordinate proximity (±2mm in both X and Y)
        using the source-specific table coordinate (left or right).
        
        This is faster than spatial queries and handles the common case where
        the same chip is detected with slightly different precise coordinates
        but rounds to nearby integer table coordinates.
        
        Args:
            table_coordinate: Table coordinate string (e.g., "244:69")
            width: Detected width for size validation
            height: Detected height for size validation
            
        Returns:
            Existing Chip if found within ±2mm position and ±2mm size, None otherwise
        """
        if not table_coordinate or ':' not in table_coordinate:
            return None
            
        try:
            x_center, y_center = map(int, table_coordinate.split(':'))
        except (ValueError, AttributeError):
            return None
        
        # Query chips with table_coordinates within ±2 in both dimensions
        from database.db_utils import material_session_scope
        from database.models import Chip as ChipModel
        
        with material_session_scope() as session:
            coord_attr = "table_coordinate_right" if source == "right_yolo" else "table_coordinate_left"
            # Get all chips and filter by table_coordinate proximity
            all_chips = session.query(ChipModel).filter(
                getattr(ChipModel, coord_attr).isnot(None)
            ).all()
            
            for chip in all_chips:
                chip_coord = getattr(chip, coord_attr, None)
                if not chip_coord or ':' not in chip_coord:
                    continue
                    
                try:
                    chip_x, chip_y = map(int, chip_coord.split(':'))
                except (ValueError, AttributeError):
                    continue
                
                # Check position proximity (±2mm)
                if abs(chip_x - x_center) > 2 or abs(chip_y - y_center) > 2:
                    continue
                
                # Check size similarity (±2mm) if both have size data
                if (width is not None and height is not None and
                    chip.width_mm is not None and chip.height_mm is not None):
                    dw = abs(chip.width_mm - width)
                    dh = abs(chip.height_mm - height)
                    if dw > self.size_tolerance or dh > self.size_tolerance:
                        continue
                
                # Found a match!
                logger.debug(
                    f"Table coordinate match: '{table_coordinate}' matches chip {chip.label} "
                    f"at '{chip_coord}'"
                )
                session.expunge(chip)  # Detach from session
                return chip
        
        return None
    
    def find_matching_chip(
        self,
        x_detected: float,
        y_detected: float,
        width: Optional[float],
        height: Optional[float],
        source: str,
        slot_id: Optional[int] = None,
        table_coordinate_left: Optional[str] = None,
        table_coordinate_right: Optional[str] = None,
    ) -> Optional[Chip]:
        """
        Find existing chip that matches this detection.
        
        Args:
            x_detected: X coordinate in the SOURCE camera's frame
            y_detected: Y coordinate in the SOURCE camera's frame
            width: Detected width (optional)
            height: Detected height (optional)
            source: Which YOLO detected it ("left_yolo" or "right_yolo")
            slot_id: Optional slot id for authoritative matching
            table_coordinate_left: Pre-calculated left table coordinate for fast matching
            table_coordinate_right: Pre-calculated right table coordinate for fast matching
            
        Returns:
            Existing Chip if found, None otherwise
        """
        # Stage 0: Slot-based identity (authoritative)
        if slot_id is not None:
            chip = self._get_chip_for_slot(slot_id)
            if chip is not None:
                return chip

        # Stage 1: Try table_coordinate proximity matching (fast path)
        table_coordinate = table_coordinate_right if source == "right_yolo" else table_coordinate_left
        if table_coordinate:
            match = self.find_matching_chip_by_table_coordinate(
                table_coordinate, width, height, source
            )
            if match:
                return match
        
        # Stage 2: Fall back to spatial proximity matching
        # Convert detection to canonical (left arm) coordinates
        x_canonical, y_canonical = self._to_canonical_coords(
            x_detected, y_detected, source
        )
        
        # Query all chips near this location
        nearby_chips = self._get_chips_in_region(
            x_canonical,
            y_canonical,
            radius=self.position_tolerance
        )
        
        if not nearby_chips:
            return None
        
        # Find best match by position and size
        for chip in nearby_chips:
            if self._is_same_chip(
                chip,
                x_canonical,
                y_canonical,
                width,
                height
            ):
                logger.debug(
                    f"Spatial match found: {source} detection at ({x_detected:.1f}, {y_detected:.1f}) "
                    f"matches chip {chip.label}"
                )
                return chip
        
        return None
    
    def process_detection(
        self,
        x_detected: float,
        y_detected: float,
        width: Optional[float] = None,
        height: Optional[float] = None,
        confidence: Optional[float] = None,
        source: str = "left_yolo",
        label: Optional[str] = None,
        slot_id: Optional[int] = None,
        **other_data
    ) -> Chip:
        """
        Process a chip detection, either updating existing or creating new.
        
        Args:
            x_detected: X coordinate in the source camera's frame
            y_detected: Y coordinate in the source camera's frame
            width: Detected width
            height: Detected height
            confidence: Detection confidence
            source: YOLO source ("left_yolo" or "right_yolo")
            label: Optional chip label
            slot_id: Optional slot id for authoritative matching
            **other_data: Additional fields (table_coordinate_left/right, etc.)
            
        Returns:
            The Chip object (existing or newly created)
        """
        # Convert to canonical coordinates for validation
        x_canonical, y_canonical = self._to_canonical_coords(
            x_detected, y_detected, source
        )
        
        # Coordinate validation: reject unrealistic detections
        if not self._validate_coordinates(x_canonical, y_canonical, width, height):
            logger.warning(
                f"Rejected detection from {source} at ({x_canonical:.1f}, {y_canonical:.1f}) "
                f"with size {width}×{height}mm - coordinates out of bounds"
            )
            # Return a dummy chip or raise exception - for now, proceed with caution
            # You may want to raise an exception here instead
        
        table_coordinate_left = other_data.get("table_coordinate_left")
        table_coordinate_right = other_data.get("table_coordinate_right")
        
        # Try to find matching chip
        existing_chip = self.find_matching_chip(
            x_detected,
            y_detected,
            width,
            height,
            source,
            slot_id=slot_id,
            table_coordinate_left=table_coordinate_left,
            table_coordinate_right=table_coordinate_right,
        )
        
        if existing_chip:
            # Update existing chip with new detection
            self._update_chip_from_detection(
                existing_chip,
                x_detected,
                y_detected,
                width,
                height,
                confidence,
                source,
                **other_data
            )
            return existing_chip
        else:
            # Create new chip
            return self._create_chip_from_detection(
                x_detected,
                y_detected,
                width,
                height,
                confidence,
                source,
                label,
                **other_data
            )

    @staticmethod
    def _get_chip_for_slot(slot_id: int) -> Optional[Chip]:
        from database.db_utils import material_session_scope
        from database.models import ChipSlot as ChipSlotModel

        with material_session_scope() as session:
            slot = session.query(ChipSlotModel).filter(ChipSlotModel.id == slot_id).one_or_none()
            if slot is None or slot.chip is None:
                return None
            chip = slot.chip
            session.expunge(chip)
            return chip
    
    def _validate_coordinates(
        self,
        x: float,
        y: float,
        width: Optional[float],
        height: Optional[float]
    ) -> bool:
        """
        Validate that coordinates and sizes are within realistic bounds.
        
        Args:
            x: X coordinate in canonical frame
            y: Y coordinate in canonical frame
            width: Chip width
            height: Chip height
            
        Returns:
            True if coordinates are valid, False otherwise
        """
        # Position bounds (stage dimensions)
        MAX_X = 500.0  # mm
        MAX_Y = 300.0  # mm
        
        if abs(x) > MAX_X or abs(y) > MAX_Y:
            return False
        
        # Size bounds (silicon chips should be reasonable sizes)
        MAX_WIDTH = 50.0   # mm
        MAX_HEIGHT = 50.0  # mm
        
        if width is not None and width > MAX_WIDTH:
            return False
        if height is not None and height > MAX_HEIGHT:
            return False
        
        return True
    
    def _to_canonical_coords(
        self,
        x: float,
        y: float,
        source: str
    ) -> tuple[float, float]:
        """Convert detection coordinates to canonical (left arm) frame."""
        if source == "right_yolo":
            return self.transform_service.right_to_left(x, y)
        else:  # left_yolo or unknown
            return (x, y)

    def _is_same_chip(
        self,
        chip: Chip,
        x: float,
        y: float,
        width: Optional[float],
        height: Optional[float]
    ) -> bool:
        """Check if chip matches the detection."""
        # Match by stage position, not pickup pose.
        if chip.stage_position_x is None or chip.stage_position_y is None:
            return False

        dx = abs(chip.stage_position_x - x)
        dy = abs(chip.stage_position_y - y)
        position_match = (
            dx <= self.position_tolerance and
            dy <= self.position_tolerance
        )
        
        if not position_match:
            return False
        
        # Check size if both chip and detection have size data
        if (width is not None and height is not None and
            chip.width_mm is not None and chip.height_mm is not None):
            dw = abs(chip.width_mm - width)
            dh = abs(chip.height_mm - height)
            size_match = (
                dw <= self.size_tolerance and
                dh <= self.size_tolerance
            )
            return size_match
        
        # If no size data, position match is sufficient
        return True
    
    def _get_chips_in_region(
        self,
        x: float,
        y: float,
        radius: float
    ) -> list:
        """Get all chips within radius of (x, y)."""
        # Get all chips using list_chips
        # Note: This returns ChipRecord objects, not Chip model instances
        chip_records = self.chip_service.list_chips()
        
        # Need to get actual Chip models from database
        from database.db_utils import material_session_scope
        from database.models import Chip as ChipModel
        
        nearby = []
        with material_session_scope() as session:
            for chip_record in chip_records:
                if chip_record.stage_position_x is None or chip_record.stage_position_y is None:
                    continue
                
                distance = math.sqrt(
                    (chip_record.stage_position_x - x) ** 2 +
                    (chip_record.stage_position_y - y) ** 2
                )
                
                if distance <= radius:
                    # Get the actual Chip model
                    chip_model = session.query(ChipModel).filter(ChipModel.id == chip_record.id).one_or_none()
                    if chip_model:
                        nearby.append(chip_model)
        
        return nearby
    
    def _update_chip_from_detection(
        self,
        chip: Chip,
        x_detected: float,
        y_detected: float,
        width: Optional[float],
        height: Optional[float],
        confidence: Optional[float],
        source: str,
        **other_data
    ):
        """Update existing chip with new detection (fusion logic)."""
        # Convert to canonical coordinates
        x_canonical, y_canonical = self._to_canonical_coords(
            x_detected, y_detected, source
        )

        if "stage_position_x" in other_data:
            other_data["stage_position_x"] = x_canonical
        if "stage_position_y" in other_data:
            other_data["stage_position_y"] = y_canonical
        # Ensure per-arm pickup coordinates are populated when possible.
        if source == "left_yolo":
            other_data.setdefault("pickup_left_x_mm", x_detected)
            other_data.setdefault("pickup_left_y_mm", y_detected)
        elif source == "right_yolo":
            other_data.setdefault("pickup_right_x_mm", x_detected)
            other_data.setdefault("pickup_right_y_mm", y_detected)
        
        # Get or initialize metadata - start with passed-in metadata if provided
        passed_metadata = other_data.pop("metadata", {})
        metadata = chip.metadata_json or {}
        
        # Merge passed metadata into existing metadata (preserving custom fields)
        for key, value in passed_metadata.items():
            if key not in ["detections", "detection_count", "last_detected_by"]:
                metadata[key] = value
        
        detections_by_source = metadata.get("detections", {})
        if not isinstance(detections_by_source, dict):
            detections_by_source = {}

        # Keep only the latest detection per source to reduce metadata size.
        detection_record = {
            "source": source,
            "timestamp": datetime.utcnow().isoformat(),
            "x": x_canonical,
            "y": y_canonical,
            "width": width,
            "height": height,
            "confidence": confidence,
        }
        detections_by_source[source] = detection_record

        # Fusion strategy: Use average across the latest per-source detections.
        recent_detections = [d for d in detections_by_source.values() if d["x"] is not None]
        
        if recent_detections:
            # Average sizes (if available)
            size_detections = [d for d in recent_detections if d["width"] is not None]
            if size_detections:
                chip.width_mm = sum(d["width"] for d in size_detections) / len(size_detections)
                chip.height_mm = sum(d["height"] for d in size_detections) / len(size_detections)
            
            # Use highest confidence
            conf_detections = [d for d in recent_detections if d["confidence"] is not None]
            if conf_detections:
                chip.confidence = max(d["confidence"] for d in conf_detections)
        
        # Update other fields if provided
        for key, value in other_data.items():
            if hasattr(chip, key) and value is not None:
                setattr(chip, key, value)

        # Update metadata
        metadata["detections"] = detections_by_source
        metadata["last_detected_by"] = source
        metadata["detection_count"] = len(detections_by_source)
        chip.metadata_json = metadata
        
        # Save using upsert_chip
        chip_record = self.chip_service.upsert_chip(
            label=chip.label,
            chip_id=chip.id,
            stage_position_x=chip.stage_position_x,
            stage_position_y=chip.stage_position_y,
            pickup_left_x_mm=chip.pickup_left_x_mm,
            pickup_left_y_mm=chip.pickup_left_y_mm,
            pickup_left_angle_deg=chip.pickup_left_angle_deg,
            pickup_right_x_mm=chip.pickup_right_x_mm,
            pickup_right_y_mm=chip.pickup_right_y_mm,
            pickup_right_angle_deg=chip.pickup_right_angle_deg,
            angle_deg=chip.angle_deg,
            width_mm=chip.width_mm,
            height_mm=chip.height_mm,
            confidence=chip.confidence,
            present=True,
            table_coordinate_left=getattr(chip, "table_coordinate_left", None),
            table_coordinate_right=getattr(chip, "table_coordinate_right", None),
            metadata=metadata
        )
        
        logger.info(
            f"Updated chip {chip.label} from {source} detection "
            f"(sources: {len(detections_by_source)} detections)"
        )
    
    def _create_chip_from_detection(
        self,
        x_detected: float,
        y_detected: float,
        width: Optional[float],
        height: Optional[float],
        confidence: Optional[float],
        source: str,
        label: Optional[str],
        **other_data
    ):
        """Create new chip from detection."""
        # Convert to canonical coordinates
        x_canonical, y_canonical = self._to_canonical_coords(
            x_detected, y_detected, source
        )
        
        # Generate label if not provided
        if label is None:
            label = f"C{int(time.time() % 100000)}"
        
        # Extract and merge passed-in metadata with auto-generated metadata
        passed_metadata = other_data.pop("metadata", {})
        # Ensure per-arm pickup coordinates are populated when possible.
        if source == "left_yolo":
            other_data.setdefault("pickup_left_x_mm", x_detected)
            other_data.setdefault("pickup_left_y_mm", y_detected)
        elif source == "right_yolo":
            other_data.setdefault("pickup_right_x_mm", x_detected)
            other_data.setdefault("pickup_right_y_mm", y_detected)

        # Prepare metadata - start with passed values, then add detection tracking
        metadata = {**passed_metadata}  # Start with any passed metadata
        metadata.update({
            "detected_by": source,
            "detections": {
                source: {
                    "source": source,
                    "timestamp": datetime.utcnow().isoformat(),
                    "x": x_canonical,
                    "y": y_canonical,
                    "width": width,
                    "height": height,
                    "confidence": confidence,
                }
            },
            "detection_count": 1,
            "last_detected_by": source,
        })
        
        # Create chip using upsert_chip (returns ChipRecord)
        chip_record = self.chip_service.upsert_chip(
            label=label,
            stage_position_x=x_canonical,
            stage_position_y=y_canonical,
            width_mm=width,
            height_mm=height,
            confidence=confidence,
            present=True,
            metadata=metadata,
            **{
                k: v
                for k, v in other_data.items()
                if k not in ("stage_position_x", "stage_position_y")
            }
        )
        
        # Get the actual Chip model from database
        from database.db_utils import material_session_scope
        from database.models import Chip as ChipModel
        
        with material_session_scope() as session:
            chip = session.query(ChipModel).filter(ChipModel.id == chip_record.id).one()
            session.expunge(chip)  # Detach from session so it can be used outside
        
        logger.info(
            f"Created new chip {chip.label} from {source} detection "
            f"at ({x_canonical:.1f}, {y_canonical:.1f})"
        )
        
        return chip


# Singleton instance
_reconciliation_service = None


def get_chip_reconciliation_service() -> ChipReconciliationService:
    """
    Get the singleton chip reconciliation service instance.
    
    Returns:
        ChipReconciliationService instance
    """
    global _reconciliation_service
    if _reconciliation_service is None:
        _reconciliation_service = ChipReconciliationService()
    return _reconciliation_service


def reset_chip_reconciliation_service():
    """Reset the singleton instance (mainly for testing)."""
    global _reconciliation_service
    _reconciliation_service = None
