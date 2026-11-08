"""Utilities for converting between pixel coordinates and stage millimeter space."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence, Tuple

import numpy as np

try:  # pragma: no cover - optional dependency guard
    import cv2  # type: ignore
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore

# World coordinates (mm) for the four table fiducials, expressed in the
# left arm's coordinate frame. The ordering matches the logical
# layout: 1A (top-left), 1B (bottom-left), 1C (bottom-right), 1D (top-right).
FIDUCIAL_LEFT_MM = np.array(
    [
        (-320.0, 214.0),  # 1A (top-left)
        (-320.0, 18.0),   # 1B (bottom-left)
        (328.0, 15.0),    # 1C (bottom-right)
        (328.0, 210.0),   # 1D (top-right)
    ],
    dtype=float,
)

# World coordinates (mm) for the four table fiducials on the right side,
# expressed in the right arm's coordinate frame. These must reflect the
# physical 2a/2b/2c/2d layout, not a mirrored copy of left.
FIDUCIAL_RIGHT_MM = np.array(
    [
        (-328.0, 210.0),  # 2A (top-left, middle table)
        (-328.0, 15.0),   # 2B (bottom-left, middle table)
        (326.0, 14.0),    # 2C (bottom-right, right table)
        (326.0, 209.0),   # 2D (top-right, right table)
    ],
    dtype=float,
)

FIDUCIAL_LABEL_MAP = {
    # Left camera markers
    "1a": 0,
    "1b": 1,
    "1c": 2,
    "1d": 3,
    # Right camera markers
    "2a": 0,
    "2b": 1,
    "2c": 2,
    "2d": 3,
}

_ACTIVE_H_LEFT: np.ndarray | None = None
_ACTIVE_H_RIGHT: np.ndarray | None = None


def is_calibrated(source: str = "left") -> bool:
    """Check if the stage geometry has been calibrated for the given camera.
    
    Args:
        source: Camera source ("left" or "right")
    """
    if "right" in source.lower():
        return _ACTIVE_H_RIGHT is not None
    return _ACTIVE_H_LEFT is not None


def px_to_mm(u_px: float, v_px: float, source: str = "left") -> tuple[float, float]:
    """Convert pixel coordinates to millimeters using the active homography.
    
    Args:
        u_px: Pixel X coordinate
        v_px: Pixel Y coordinate
        source: Camera source ("left" or "right")
    """
    if "right" in source.lower():
        H_use = _ACTIVE_H_RIGHT
        if H_use is None:
            raise RuntimeError("px_to_mm requires right camera calibration from QR markers before use.")
    else:
        H_use = _ACTIVE_H_LEFT
        if H_use is None:
            raise RuntimeError("px_to_mm requires left camera calibration from QR markers before use.")
    denom = float(H_use[2, 0] * u_px + H_use[2, 1] * v_px + H_use[2, 2])
    if denom == 0:
        return 0.0, 0.0
    x_mm = (H_use[0, 0] * u_px + H_use[0, 1] * v_px + H_use[0, 2]) / denom
    y_mm = (H_use[1, 0] * u_px + H_use[1, 1] * v_px + H_use[1, 2]) / denom
    return x_mm, y_mm


def set_px_to_mm_homography(matrix: np.ndarray | None, source: str = "left") -> None:
    """Install a custom px→mm homography (or None to fall back to defaults).
    
    Args:
        matrix: 3x3 homography matrix or None
        source: Camera source ("left" or "right")
    """
    global _ACTIVE_H_LEFT, _ACTIVE_H_RIGHT
    
    if matrix is None:
        if "right" in source.lower():
            _ACTIVE_H_RIGHT = None
        else:
            _ACTIVE_H_LEFT = None
        return
        
    matrix = np.asarray(matrix, dtype=float)
    if matrix.shape != (3, 3):
        raise ValueError(f"Expected a 3x3 homography, got shape {matrix.shape}.")
    if matrix[2, 2] == 0:
        raise ValueError("Invalid homography: bottom-right element cannot be zero.")
    
    normalized = matrix / matrix[2, 2]
    if "right" in source.lower():
        _ACTIVE_H_RIGHT = normalized
    else:
        _ACTIVE_H_LEFT = normalized


def _compute_homography(px_pts: np.ndarray, mm_pts: np.ndarray) -> np.ndarray:
    if px_pts.shape[0] < 4 or mm_pts.shape[0] < 4:
        raise ValueError("Need at least four point pairs to solve homography.")
    A = []
    for (u, v), (x, y) in zip(px_pts, mm_pts):
        A.append([-u, -v, -1, 0, 0, 0, u * x, v * x, x])
        A.append([0, 0, 0, -u, -v, -1, u * y, v * y, y])
    A = np.asarray(A, dtype=float)
    _, _, vh = np.linalg.svd(A)
    H = vh[-1].reshape(3, 3)
    if H[2, 2] == 0:
        H /= np.linalg.norm(H)
    return H / H[2, 2]


def calibrate_px_to_mm_from_qr_markers(detections: Sequence[Mapping[str, Any]], source: str = "left") -> np.ndarray:
    """Compute and activate px→mm homography using table micro QR detections.
    
    Args:
        detections: List of detection dictionaries
        source: Camera source ("left" or "right") - auto-detected from QR labels if not specified
    """
    # First pass: detect source if needed
    detected_source = None
    if source == "left": # Only try to detect if default/unknown, otherwise trust caller
        for det in detections:
            qr_msg = _normalize_qr_label(det.get("qr_message"))
            if not qr_msg: 
                continue
            if qr_msg.startswith("2"):
                detected_source = "right"
                break
            elif qr_msg.startswith("1"):
                detected_source = "left"
    
    if detected_source:
        source = detected_source

    # Determine expectations based on source
    if "right" in source.lower():
        target_prefix = "2"
        expected_labels = ["2a", "2b", "2c", "2d"]
        fiducial_mm = FIDUCIAL_RIGHT_MM
    else:
        target_prefix = "1"
        expected_labels = ["1a", "1b", "1c", "1d"]
        fiducial_mm = FIDUCIAL_LEFT_MM

    qr_centers: list[tuple[float, float]] = []
    labeled_pts: dict[str, tuple[float, float]] = {}
    
    for det in detections:
        label = str(det.get("class") or det.get("label") or "").lower()
        class_id = det.get("class_id")
        # Keep consistent class ID checks (table micro qr is class 5 on both cameras) or just check label text
        if "table micro qr" not in label and (class_id is None or int(class_id) != 5):
            continue
            
        qr_msg = _normalize_qr_label(det.get("qr_message"))
        
        # Filter: Only accept markers belonging to the active source
        if qr_msg and not qr_msg.startswith(target_prefix):
             continue

        try:
            center = (float(det.get("x", 0.0)), float(det.get("y", 0.0)))
        except (TypeError, ValueError):
            continue
            
        qr_centers.append(center)
        if qr_msg in FIDUCIAL_LABEL_MAP:
            labeled_pts[qr_msg] = center
                
    if len(qr_centers) < 4:
        # Don't raise error immediately if we have enough labeled points
        if len(labeled_pts) < 4:
             raise ValueError(f"Detected fewer than four table micro QR markers for {source} camera.")
    
    # Check if we have all expected labels
    if all(label in labeled_pts for label in expected_labels):
        ordered = [labeled_pts[label] for label in expected_labels]
        px_pts = np.array(ordered, dtype=float)
    else:
        px_pts = _infer_qr_order(qr_centers)

    H = _compute_homography(px_pts, fiducial_mm)
    set_px_to_mm_homography(H, source=source)
    return H


def _infer_qr_order(points: Sequence[tuple[float, float]]) -> np.ndarray:
    pts = list(points)
    pts.sort(key=lambda pt: pt[0])
    left = pts[:2]
    right = pts[-2:]
    tl = min(left, key=lambda pt: pt[1])
    bl = max(left, key=lambda pt: pt[1])
    br = max(right, key=lambda pt: pt[1])
    tr = min(right, key=lambda pt: pt[1])
    return np.array([tl, bl, br, tr], dtype=float)


def _normalize_qr_label(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip().lower()
    filtered = "".join(ch for ch in text if ch.isalnum())
    return filtered


def polygon_mm_metrics(px_pts_2d: Sequence[Sequence[float]], source: str = "left") -> tuple[list[tuple[float, float]], float, float]:
    """Return polygon points in mm along with consistent width/height using minAreaRect.
    
    Args:
        px_pts_2d: Sequence of (x, y) pixel coordinates
        source: Camera source ("left" or "right")
    """
    mm_pts = [px_to_mm(float(x), float(y), source=source) for (x, y) in px_pts_2d]

    if len(mm_pts) < 4:
        return mm_pts, 0.0, 0.0

    # Use OpenCV's minAreaRect for consistent width/height regardless of point ordering
    if cv2 is not None:
        # Convert to numpy array for cv2
        pts_array = np.array(mm_pts, dtype=np.float32)
        # minAreaRect returns ((center_x, center_y), (width, height), angle)
        # width is always >= height
        rect = cv2.minAreaRect(pts_array)
        (_, _), (width, height), _ = rect
        # Ensure width >= height convention
        if width < height:
            width, height = height, width
        return mm_pts, float(width), float(height)
    else:
        # Fallback: compute from consecutive edge distances
        def dist_mm(a: tuple[float, float], b: tuple[float, float]) -> float:
            return math.hypot(a[0] - b[0], a[1] - b[1])
        
        # Compute all edge lengths
        edges = [dist_mm(mm_pts[i], mm_pts[(i + 1) % len(mm_pts)]) for i in range(len(mm_pts))]
        # Sort to get two longest (width) and two shortest (height)
        edges_sorted = sorted(edges, reverse=True)
        width = 0.5 * (edges_sorted[0] + edges_sorted[1])
        height = 0.5 * (edges_sorted[2] + edges_sorted[3]) if len(edges_sorted) >= 4 else 0.0
        return mm_pts, width, height


@dataclass(slots=True)
class SlideMetric:
    qr_center_mm: tuple[float, float]
    pickup_center_mm: tuple[float, float]
    crystal_center_mm: tuple[float, float]
    qr_size_mm: tuple[float, float]
    pickup_size_mm: tuple[float, float]


def compute_slide_metric(
    qr_center_px: tuple[float, float],
    pickup_center_px: tuple[float, float],
    qr_polygon_px: Sequence[Sequence[float]],
    pickup_polygon_px: Sequence[Sequence[float]],
    *,
    source: str = "left",
) -> SlideMetric:
    """Return millimeter metrics for a slide pairing."""
    qr_pts_mm, qr_w_mm, qr_h_mm = polygon_mm_metrics(qr_polygon_px, source=source)
    pickup_pts_mm, pickup_w_mm, pickup_h_mm = polygon_mm_metrics(pickup_polygon_px, source=source)

    qr_center_mm = px_to_mm(*qr_center_px, source=source)
    pickup_center_mm = px_to_mm(*pickup_center_px, source=source)
    crystal_center_mm = (
        (qr_center_mm[0] + pickup_center_mm[0]) * 0.5,
        (qr_center_mm[1] + pickup_center_mm[1]) * 0.5,
    )

    return SlideMetric(
        qr_center_mm=qr_center_mm,
        pickup_center_mm=pickup_center_mm,
        crystal_center_mm=crystal_center_mm,
        qr_size_mm=(qr_w_mm, qr_h_mm),
        pickup_size_mm=(pickup_w_mm, pickup_h_mm),
    )
