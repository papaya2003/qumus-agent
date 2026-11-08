from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

import numpy as np


@dataclass
class CornerAdjustmentResult:
    ok: bool
    corner_xy: Optional[Tuple[int, int]] = None
    corner_xy_float: Optional[Tuple[float, float]] = None
    screen_corner: Optional[str] = None
    edges: Optional[Tuple[str, str]] = None
    bbox: Optional[Tuple[int, int, int, int]] = None
    reason: Optional[str] = None


def _touches_from_bbox(bbox, width: int, height: int, tol: int):
    x0, y0, x1, y1 = bbox
    return {
        "L": x0 <= tol,
        "R": x1 >= (width - 1 - tol),
        "T": y0 <= tol,
        "B": y1 >= (height - 1 - tol),
    }


def _screen_corner_from_touches(touches) -> Optional[str]:
    touched = {k for k, v in touches.items() if v}
    if len(touched) != 2:
        return None
    if touched == {"L", "T"}:
        return "TL"
    if touched == {"R", "T"}:
        return "TR"
    if touched == {"R", "B"}:
        return "BR"
    if touched == {"L", "B"}:
        return "BL"
    return None


def _line_intersection(p1, d1, p2, d2) -> Optional[Tuple[float, float]]:
    x1, y1 = p1
    x2, y2 = p2
    dx1, dy1 = d1
    dx2, dy2 = d2

    a = np.array([[dx1, -dx2], [dy1, -dy2]], dtype=np.float64)
    b = np.array([x2 - x1, y2 - y1], dtype=np.float64)

    det_a = float(np.linalg.det(a))
    if abs(det_a) < 1e-6:
        return None

    t, _ = np.linalg.solve(a, b)
    return float(x1 + t * dx1), float(y1 + t * dy1)


def compute_corner_xy_from_detector(
    res: Any,
    bgr: np.ndarray,
    *,
    det_module=None,
    band_px: Optional[int] = None,
    touch_tol_px: Optional[int] = None,
) -> CornerAdjustmentResult:
    if det_module is None:
        from . import edge_follow as det_module

    if res is None or getattr(res, "bbox", None) is None or getattr(res, "chip_mask", None) is None:
        return CornerAdjustmentResult(ok=False, reason="missing_res_bbox_or_mask")

    height, width = bgr.shape[:2]
    bbox = tuple(int(v) for v in res.bbox)

    tol = int(touch_tol_px) if touch_tol_px is not None else int(getattr(det_module, "TOUCH_TOL_PX", 6))
    touches = _touches_from_bbox(bbox, width, height, tol)
    screen_corner = _screen_corner_from_touches(touches)
    if screen_corner is None:
        return CornerAdjustmentResult(ok=False, bbox=bbox, reason="bbox_not_touching_two_adjacent_borders")

    if screen_corner == "TL":
        edge1, edge2 = "bottom", "right"
    elif screen_corner == "TR":
        edge1, edge2 = "bottom", "left"
    elif screen_corner == "BR":
        edge1, edge2 = "top", "left"
    else:
        edge1, edge2 = "top", "right"

    restore_band = None
    if band_px is not None and hasattr(det_module, "EDGE_BAND_PX"):
        restore_band = det_module.EDGE_BAND_PX
        det_module.EDGE_BAND_PX = int(band_px)

    try:
        boundary = det_module._boundary_points(res.chip_mask)
        min_fit = int(getattr(det_module, "MIN_FIT_PTS", 60))
        if boundary is None or len(boundary) < min_fit:
            return CornerAdjustmentResult(ok=False, screen_corner=screen_corner, edges=(edge1, edge2), bbox=bbox, reason="too_few_boundary_points")

        points1 = det_module._select_edge_points(boundary, bbox, edge1)
        points2 = det_module._select_edge_points(boundary, bbox, edge2)

        fit1 = det_module._fit_line(points1)
        fit2 = det_module._fit_line(points2)
        if fit1 is None or fit2 is None:
            return CornerAdjustmentResult(ok=False, screen_corner=screen_corner, edges=(edge1, edge2), bbox=bbox, reason="fit_line_failed")

        p1, d1 = fit1
        p2, d2 = fit2
        intersection = _line_intersection(p1, d1, p2, d2)
        if intersection is None:
            return CornerAdjustmentResult(ok=False, screen_corner=screen_corner, edges=(edge1, edge2), bbox=bbox, reason="lines_parallel_or_bad_condition")

        xf, yf = intersection
        xi = int(np.clip(round(xf), 0, width - 1))
        yi = int(np.clip(round(yf), 0, height - 1))

        return CornerAdjustmentResult(
            ok=True,
            corner_xy=(xi, yi),
            corner_xy_float=(xf, yf),
            screen_corner=screen_corner,
            edges=(edge1, edge2),
            bbox=bbox,
        )
    finally:
        if restore_band is not None:
            det_module.EDGE_BAND_PX = restore_band
