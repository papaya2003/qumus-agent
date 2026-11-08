"""
Coordinate transformation service for dual robot arm system.

Manages transformations between left and right robot arm coordinate frames.
The database stores all coordinates in the canonical left arm frame, and this
service converts to/from right arm coordinates as needed.
"""

from typing import Tuple
import logging

logger = logging.getLogger(__name__)


class CoordinateTransformService:
    """
    Handles coordinate transformations between left and right robot arm frames.
    
    Convention:
    - Left arm: positive X values (e.g., X=+280mm)
    - Right arm: negative X values for same physical location (e.g., X=-280mm)
    - Database always stores in left arm frame (canonical)
    - Y coordinates typically remain the same (parallel arms)
    """
    
    def __init__(
        self,
        x_mirror_axis: float = 0.0,
        y_offset: float = 0.0,
        calibration_metadata: dict = None
    ):
        """
        Initialize coordinate transformation service.
        
        Args:
            x_mirror_axis: X coordinate of the mirror/symmetry plane between arms
            y_offset: Any Y-axis offset between left and right arm coordinate systems
            calibration_metadata: Optional dict with calibration parameters
        """
        self.x_mirror_axis = x_mirror_axis
        self.y_offset = y_offset
        self.calibration_metadata = calibration_metadata or {}
        
        logger.info(
            f"CoordinateTransformService initialized: "
            f"mirror_axis={x_mirror_axis}, y_offset={y_offset}"
        )
    
    def left_to_right(self, x_left: float, y_left: float) -> Tuple[float, float]:
        """
        Convert coordinates from left arm frame to right arm frame.
        
        Args:
            x_left: X coordinate in left arm frame
            y_left: Y coordinate in left arm frame
            
        Returns:
            (x_right, y_right) coordinates in right arm frame
            
        Example:
            >>> transform.left_to_right(280.0, 160.0)
            (-280.0, 160.0)
        """
        # Mirror across the X axis (simple negation if mirror_axis is 0)
        x_right = -(x_left - self.x_mirror_axis) + self.x_mirror_axis
        y_right = y_left + self.y_offset
        
        logger.debug(
            f"Left→Right: ({x_left:.2f}, {y_left:.2f}) → ({x_right:.2f}, {y_right:.2f})"
        )
        
        return (x_right, y_right)
    
    def right_to_left(self, x_right: float, y_right: float) -> Tuple[float, float]:
        """
        Convert coordinates from right arm frame to left arm frame.
        
        Args:
            x_right: X coordinate in right arm frame
            y_right: Y coordinate in right arm frame
            
        Returns:
            (x_left, y_left) coordinates in left arm frame
            
        Example:
            >>> transform.right_to_left(-280.0, 160.0)
            (280.0, 160.0)
        """
        # Mirror across the X axis (simple negation if mirror_axis is 0)
        x_left = -(x_right - self.x_mirror_axis) + self.x_mirror_axis
        y_left = y_right - self.y_offset
        
        logger.debug(
            f"Right→Left: ({x_right:.2f}, {y_right:.2f}) → ({x_left:.2f}, {y_left:.2f})"
        )
        
        return (x_left, y_left)
    
    def get_coordinates_for_arm(
        self,
        x_stored: float,
        y_stored: float,
        target_arm: str
    ) -> Tuple[float, float]:
        """
        Get coordinates for a specific arm from stored (canonical left frame) values.
        
        Args:
            x_stored: X coordinate as stored in database (left arm perspective)
            y_stored: Y coordinate as stored in database (left arm perspective)
            target_arm: Either "left" or "right"
            
        Returns:
            (x, y) coordinates for the target arm
            
        Raises:
            ValueError: If target_arm is not "left" or "right"
        """
        if target_arm == "left":
            return (x_stored, y_stored)
        elif target_arm == "right":
            return self.left_to_right(x_stored, y_stored)
        else:
            raise ValueError(f"Unknown arm: {target_arm}. Must be 'left' or 'right'.")
    
    def update_calibration(self, x_mirror_axis: float = None, y_offset: float = None):
        """
        Update calibration parameters.
        
        Args:
            x_mirror_axis: New mirror axis X coordinate (optional)
            y_offset: New Y offset (optional)
        """
        if x_mirror_axis is not None:
            old_mirror = self.x_mirror_axis
            self.x_mirror_axis = x_mirror_axis
            logger.info(f"Updated mirror axis: {old_mirror} → {x_mirror_axis}")
        
        if y_offset is not None:
            old_offset = self.y_offset
            self.y_offset = y_offset
            logger.info(f"Updated Y offset: {old_offset} → {y_offset}")


# Singleton instance
_coordinate_transform_service = None


def get_coordinate_transform_service() -> CoordinateTransformService:
    """
    Get the singleton coordinate transform service instance.
    
    Returns:
        CoordinateTransformService instance
    """
    global _coordinate_transform_service
    if _coordinate_transform_service is None:
        _coordinate_transform_service = CoordinateTransformService()
    return _coordinate_transform_service


def reset_coordinate_transform_service():
    """Reset the singleton instance (mainly for testing)."""
    global _coordinate_transform_service
    _coordinate_transform_service = None
