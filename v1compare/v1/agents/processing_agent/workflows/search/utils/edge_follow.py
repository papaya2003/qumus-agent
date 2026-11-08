from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

AREA_INVALID_HIGH = 0.80
AREA_INVALID_LOW = 0.15
BOUNDARY_NEAR_BORDER_FRAC = 0.06
TOUCH_TOL_PX = 6
HALF_DOMINANCE_MIN = 0.55
HALF_DOMINANCE_GAP = 0.08
EDGE_BAND_PX = 20
MIN_FIT_PTS = 120
RGB_REGION_MIN_AREA = 400
RGB_BLACK_SUM_MAX = 60.0
RGB_CHIP_SCORE_MIN = 20.0
DEFAULT_DIR = (1.0, 0.0)

DETECT_DOWNSCALE = 0.5
RGB_ROI_MARGIN_PX = 36
MIN_COMPONENT_AREA = 200
MIN_FULLRES_NONZERO = 200


@dataclass
class DetectResult:
    ok: bool
    reason: str
    chip_ratio: float
    chip_mask: np.ndarray
    thresh_vis: np.ndarray
    contour: Optional[np.ndarray]
    bbox: Optional[Tuple[int, int, int, int]]
    motion_vec: Tuple[float, float]
    motion_label: str
    line_point: Optional[Tuple[int, int]]
    line_dir: Optional[Tuple[float, float]]


def _unit(vx: float, vy: float) -> Tuple[float, float]:
    n = float(np.hypot(vx, vy))
    if n < 1e-9:
        return 0.0, 0.0
    return vx / n, vy / n


def _otsu_threshold(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return thr


def _choose_chip_mask(gray: np.ndarray, thr: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    white = thr == 255
    black = ~white
    if white.sum() < 50 or black.sum() < 50:
        return thr.copy(), thr

    mean_white = float(gray[white].mean())
    mean_black = float(gray[black].mean())
    if mean_white < mean_black:
        thr2 = cv2.bitwise_not(thr)
        return thr2.copy(), thr2
    return thr.copy(), thr


def _largest_component(
    mask255: np.ndarray,
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[Tuple[int, int, int, int]]]:
    cnts, _ = cv2.findContours(mask255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, None, None
    cnt = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(cnt) < MIN_COMPONENT_AREA:
        return None, None, None

    filled = np.zeros_like(mask255)
    cv2.drawContours(filled, [cnt], -1, 255, thickness=-1)
    x, y, w, h = cv2.boundingRect(cnt)
    return cnt, filled, (x, y, x + w, y + h)


def _boundary_points(mask255: np.ndarray) -> np.ndarray:
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    grad = cv2.morphologyEx(mask255, cv2.MORPH_GRADIENT, k)
    ys, xs = np.where(grad > 0)
    if len(xs) == 0:
        return np.zeros((0, 2), dtype=np.int32)
    return np.stack([xs, ys], axis=1).astype(np.int32)


def _fit_line(points_xy: np.ndarray) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    if points_xy.shape[0] < MIN_FIT_PTS:
        return None
    pts = points_xy.astype(np.float32).reshape(-1, 1, 2)
    vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01)
    dx = float(np.asarray(vx).reshape(-1)[0])
    dy = float(np.asarray(vy).reshape(-1)[0])
    x0 = float(np.asarray(x0).reshape(-1)[0])
    y0 = float(np.asarray(y0).reshape(-1)[0])
    n = float(np.hypot(dx, dy))
    if n < 1e-9:
        return None
    return ((x0, y0), (dx / n, dy / n))


def _select_edge_points(boundary_xy: np.ndarray, bbox: Tuple[int, int, int, int], edge: str) -> np.ndarray:
    x0, y0, x1, y1 = bbox
    if boundary_xy.shape[0] == 0:
        return boundary_xy
    x = boundary_xy[:, 0]
    y = boundary_xy[:, 1]

    if edge == "top":
        mask = np.abs(y - y0) <= EDGE_BAND_PX
    elif edge == "bottom":
        mask = np.abs(y - y1) <= EDGE_BAND_PX
    elif edge == "left":
        mask = np.abs(x - x0) <= EDGE_BAND_PX
    elif edge == "right":
        mask = np.abs(x - x1) <= EDGE_BAND_PX
    else:
        mask = np.ones_like(x, dtype=bool)
    return boundary_xy[mask]


def _enforce_sign(vec: Tuple[float, float], want: str) -> Tuple[float, float]:
    dx, dy = vec
    if want == "RIGHT" and dx < 0:
        dx, dy = -dx, -dy
    elif want == "LEFT" and dx > 0:
        dx, dy = -dx, -dy
    elif want == "UP" and dy > 0:
        dx, dy = -dx, -dy
    elif want == "DOWN" and dy < 0:
        dx, dy = -dx, -dy
    return _unit(dx, dy)


def _fallback_axis(want: str) -> Tuple[float, float]:
    if want == "RIGHT":
        return 1.0, 0.0
    if want == "LEFT":
        return -1.0, 0.0
    if want == "UP":
        return 0.0, -1.0
    if want == "DOWN":
        return 0.0, 1.0
    return _unit(*DEFAULT_DIR)


def _touch_flags(bbox: Tuple[int, int, int, int], w: int, h: int) -> Dict[str, bool]:
    x0, y0, x1, y1 = bbox
    return {
        "L": x0 <= TOUCH_TOL_PX,
        "R": x1 >= (w - 1 - TOUCH_TOL_PX),
        "T": y0 <= TOUCH_TOL_PX,
        "B": y1 >= (h - 1 - TOUCH_TOL_PX),
    }


def _corner_from_touches(touches: Dict[str, bool]) -> Optional[str]:
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


def _classify_from_chip_mask(
    chip_filled: np.ndarray,
    thresh_vis: np.ndarray,
    cnt: np.ndarray,
    bbox: Tuple[int, int, int, int],
    h: int,
    w: int,
) -> DetectResult:
    chip_ratio = float((chip_filled > 0).mean())
    if chip_ratio > AREA_INVALID_HIGH or chip_ratio < AREA_INVALID_LOW:
        return DetectResult(
            False,
            f"area invalid ({chip_ratio:.3f})",
            chip_ratio,
            chip_filled,
            thresh_vis,
            cnt,
            bbox,
            _unit(*DEFAULT_DIR),
            "DEFAULT",
            None,
            None,
        )

    bpts = _boundary_points(chip_filled)
    if bpts.shape[0] < MIN_FIT_PTS:
        return DetectResult(
            False,
            "too few boundary pts",
            chip_ratio,
            chip_filled,
            thresh_vis,
            cnt,
            bbox,
            _unit(*DEFAULT_DIR),
            "DEFAULT",
            None,
            None,
        )

    bx = float(bpts[:, 0].mean())
    by = float(bpts[:, 1].mean())
    margin_x = BOUNDARY_NEAR_BORDER_FRAC * w
    margin_y = BOUNDARY_NEAR_BORDER_FRAC * h
    if bx < margin_x or bx > (w - margin_x) or by < margin_y or by > (h - margin_y):
        return DetectResult(
            False,
            "boundary near image border",
            chip_ratio,
            chip_filled,
            thresh_vis,
            cnt,
            bbox,
            _unit(*DEFAULT_DIR),
            "DEFAULT",
            (int(bx), int(by)),
            None,
        )

    touches = _touch_flags(bbox, w, h)
    corner_name = _corner_from_touches(touches)
    override_k1 = None
    corner_reason = None

    if corner_name is not None:
        hmid = h // 2
        wmid = w // 2
        fracs = {
            "TL": float((chip_filled[:hmid, :wmid] > 0).mean()),
            "TR": float((chip_filled[:hmid, wmid:] > 0).mean()),
            "BR": float((chip_filled[hmid:, wmid:] > 0).mean()),
            "BL": float((chip_filled[hmid:, :wmid] > 0).mean()),
        }
        dom_min = 0.08
        corner_ok = False
        if corner_name == "TL":
            if fracs["BL"] > 0.5:
                override_k1 = "LEFT"
            elif fracs["TL"] >= dom_min:
                corner_ok = True
                corner_reason = "BR"
        elif corner_name == "TR":
            if fracs["TL"] > 0.5:
                override_k1 = "TOP"
            elif fracs["TR"] >= dom_min:
                corner_ok = True
                corner_reason = "BL"
        elif corner_name == "BR":
            if fracs["TR"] > 0.5:
                override_k1 = "RIGHT"
            elif fracs["BR"] >= dom_min:
                corner_ok = True
                corner_reason = "TL"
        elif corner_name == "BL":
            if fracs["BR"] > 0.5:
                override_k1 = "BOTTOM"
            elif fracs["BL"] >= dom_min:
                corner_ok = True
                corner_reason = "TR"
        if not corner_ok:
            corner_name = None

    if corner_name == "TL":
        want, edge_for_fit = "LEFT", "bottom"
    elif corner_name == "TR":
        want, edge_for_fit = "UP", "left"
    elif corner_name == "BR":
        want, edge_for_fit = "RIGHT", "top"
    elif corner_name == "BL":
        want, edge_for_fit = "DOWN", "right"
    else:
        want, edge_for_fit = None, None

    line_point = None
    line_dir = None
    if want is not None and edge_for_fit is not None:
        pts_edge = _select_edge_points(bpts, bbox, edge_for_fit)
        fit = _fit_line(pts_edge)
        if fit is not None:
            (x0, y0), (dx, dy) = fit
            line_point = (int(x0), int(y0))
            line_dir = (dx, dy)
            motion = _enforce_sign((dx, dy), want)
        else:
            motion = _fallback_axis(want)
        return DetectResult(
            True,
            f"CORNER {corner_reason if corner_reason is not None else corner_name}",
            chip_ratio,
            chip_filled,
            thresh_vis,
            cnt,
            bbox,
            motion,
            want,
            line_point,
            line_dir,
        )

    ys, xs = np.where(chip_filled > 0)
    if len(xs) < 200:
        return DetectResult(
            False,
            "chip too small",
            chip_ratio,
            chip_filled,
            thresh_vis,
            cnt,
            bbox,
            _unit(*DEFAULT_DIR),
            "DEFAULT",
            (int(bx), int(by)),
            None,
        )

    midx = w * 0.5
    midy = h * 0.5
    fracs = {
        "TOP": float((ys < midy).mean()),
        "BOTTOM": float((ys >= midy).mean()),
        "LEFT": float((xs < midx).mean()),
        "RIGHT": float((xs >= midx).mean()),
    }
    order = sorted(fracs.items(), key=lambda kv: kv[1], reverse=True)
    (k1, v1), (_, v2) = order[0], order[1]

    if override_k1 is None and (v1 < HALF_DOMINANCE_MIN or (v1 - v2) < HALF_DOMINANCE_GAP):
        return DetectResult(
            False,
            f"ambiguous halves {fracs}",
            chip_ratio,
            chip_filled,
            thresh_vis,
            cnt,
            bbox,
            _unit(*DEFAULT_DIR),
            "DEFAULT",
            (int(bx), int(by)),
            None,
        )

    if override_k1 is not None:
        k1 = override_k1

    if k1 == "BOTTOM":
        want, edge_for_fit = "RIGHT", "top"
    elif k1 == "TOP":
        want, edge_for_fit = "LEFT", "bottom"
    elif k1 == "RIGHT":
        want, edge_for_fit = "UP", "left"
    else:
        want, edge_for_fit = "DOWN", "right"

    pts_edge = _select_edge_points(bpts, bbox, edge_for_fit)
    fit = _fit_line(pts_edge)
    if fit is None:
        fit = _fit_line(bpts)

    if fit is not None:
        (x0, y0), (dx, dy) = fit
        line_point = (int(x0), int(y0))
        line_dir = (dx, dy)
        motion = _enforce_sign((dx, dy), want)
        if want in ("RIGHT", "LEFT") and abs(motion[0]) < 0.3:
            motion = _fallback_axis(want)
        if want in ("UP", "DOWN") and abs(motion[1]) < 0.3:
            motion = _fallback_axis(want)
    else:
        line_point = None
        line_dir = None
        motion = _fallback_axis(want)

    return DetectResult(
        True,
        f"EDGE {k1}",
        chip_ratio,
        chip_filled,
        thresh_vis,
        cnt,
        bbox,
        motion,
        want,
        line_point,
        line_dir,
    )


def _classify_rgb_region(bgr: np.ndarray, region_mask: np.ndarray) -> str:
    vals = bgr[region_mask]
    if vals.size == 0:
        return "background"
    mean_bgr = vals.mean(axis=0)
    b = float(mean_bgr[0])
    g = float(mean_bgr[1])
    r = float(mean_bgr[2])
    inten = r + g + b
    score = b - 0.5 * (r + g)
    if inten < RGB_BLACK_SUM_MAX:
        return "black"
    if score > RGB_CHIP_SCORE_MIN:
        return "chip"
    return "background"


def _expand_bbox(bbox: Tuple[int, int, int, int], w: int, h: int, margin: int) -> Tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    return (
        max(0, x0 - margin),
        max(0, y0 - margin),
        min(w, x1 + margin),
        min(h, y1 + margin),
    )


def _build_chip_mask_by_rgb_regions_roi(bgr: np.ndarray, thr0: np.ndarray, bbox: Tuple[int, int, int, int]) -> np.ndarray:
    h, w = thr0.shape[:2]
    out = np.zeros((h, w), dtype=np.uint8)
    rx0, ry0, rx1, ry1 = _expand_bbox(bbox, w, h, RGB_ROI_MARGIN_PX)
    bgr_roi = bgr[ry0:ry1, rx0:rx1]
    thr_roi = thr0[ry0:ry1, rx0:rx1]

    for side_val in (0, 255):
        side = np.uint8(thr_roi == side_val)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(side, connectivity=8)
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < RGB_REGION_MIN_AREA:
                continue
            region = labels == i
            label = _classify_rgb_region(bgr_roi, region)
            if label == "chip":
                out_roi = out[ry0:ry1, rx0:rx1]
                out_roi[region] = 255
    return out


def _resize_for_detect(bgr: np.ndarray) -> np.ndarray:
    if DETECT_DOWNSCALE >= 0.999:
        return bgr
    h, w = bgr.shape[:2]
    new_w = max(2, int(round(w * DETECT_DOWNSCALE)))
    new_h = max(2, int(round(h * DETECT_DOWNSCALE)))
    return cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _scale_bbox_to_fullres(
    bbox: Optional[Tuple[int, int, int, int]],
    sx: float,
    sy: float,
    w: int,
    h: int,
) -> Optional[Tuple[int, int, int, int]]:
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    return (
        int(np.clip(round(x0 * sx), 0, w - 1)),
        int(np.clip(round(y0 * sy), 0, h - 1)),
        int(np.clip(round(x1 * sx), 0, w - 1)),
        int(np.clip(round(y1 * sy), 0, h - 1)),
    )


def _scale_contour_to_fullres(cnt: Optional[np.ndarray], sx: float, sy: float) -> Optional[np.ndarray]:
    if cnt is None:
        return None
    out = cnt.astype(np.float32).copy()
    out[:, 0, 0] *= sx
    out[:, 0, 1] *= sy
    return np.rint(out).astype(np.int32)


def _scale_line_point_to_fullres(
    line_point: Optional[Tuple[int, int]],
    sx: float,
    sy: float,
    w: int,
    h: int,
) -> Optional[Tuple[int, int]]:
    if line_point is None:
        return None
    return (
        int(np.clip(round(line_point[0] * sx), 0, w - 1)),
        int(np.clip(round(line_point[1] * sy), 0, h - 1)),
    )


def _detect_on_small_image(bgr_small: np.ndarray) -> DetectResult:
    h, w = bgr_small.shape[:2]
    gray = cv2.cvtColor(bgr_small, cv2.COLOR_BGR2GRAY)
    thr0 = _otsu_threshold(gray)
    chip_mask_gray, thresh_vis_gray = _choose_chip_mask(gray, thr0)

    cnt_gray, chip_filled_gray, bbox_gray = _largest_component(chip_mask_gray)
    if chip_filled_gray is None or bbox_gray is None or cnt_gray is None:
        return DetectResult(
            False,
            "no chip component",
            0.0,
            chip_mask_gray,
            thresh_vis_gray,
            None,
            None,
            _unit(*DEFAULT_DIR),
            "DEFAULT",
            None,
            None,
        )

    res_gray = _classify_from_chip_mask(chip_filled_gray, thresh_vis_gray, cnt_gray, bbox_gray, h, w)
    if not (res_gray.reason.startswith("EDGE ") or res_gray.reason.startswith("CORNER ")):
        return res_gray

    chip_mask_rgb = _build_chip_mask_by_rgb_regions_roi(bgr_small, thr0, bbox_gray)
    cnt_rgb, chip_filled_rgb, bbox_rgb = _largest_component(chip_mask_rgb)
    if chip_filled_rgb is None or bbox_rgb is None or cnt_rgb is None:
        return res_gray
    if np.count_nonzero(chip_filled_rgb) < MIN_COMPONENT_AREA:
        return res_gray

    res_rgb = _classify_from_chip_mask(chip_filled_rgb, thr0, cnt_rgb, bbox_rgb, h, w)
    if res_rgb.reason.startswith("EDGE ") or res_rgb.reason.startswith("CORNER "):
        return res_rgb
    return res_gray


def detect_edge_and_motion(bgr: np.ndarray) -> DetectResult:
    full_h, full_w = bgr.shape[:2]
    bgr_small = _resize_for_detect(bgr)
    small_h, small_w = bgr_small.shape[:2]

    res_small = _detect_on_small_image(bgr_small)
    if (small_h, small_w) == (full_h, full_w):
        return res_small

    sx = float(full_w) / float(small_w)
    sy = float(full_h) / float(small_h)

    chip_mask = cv2.resize(res_small.chip_mask, (full_w, full_h), interpolation=cv2.INTER_NEAREST)
    thresh_vis = cv2.resize(res_small.thresh_vis, (full_w, full_h), interpolation=cv2.INTER_NEAREST)
    bbox = _scale_bbox_to_fullres(res_small.bbox, sx, sy, full_w, full_h)
    contour = _scale_contour_to_fullres(res_small.contour, sx, sy)
    line_point = _scale_line_point_to_fullres(res_small.line_point, sx, sy, full_w, full_h)

    if np.count_nonzero(chip_mask) < MIN_FULLRES_NONZERO:
        return DetectResult(
            False,
            "chip too small after upscale",
            res_small.chip_ratio,
            chip_mask,
            thresh_vis,
            contour,
            bbox,
            _unit(*DEFAULT_DIR),
            "DEFAULT",
            line_point,
            res_small.line_dir,
        )

    return DetectResult(
        ok=res_small.ok,
        reason=res_small.reason,
        chip_ratio=res_small.chip_ratio,
        chip_mask=chip_mask,
        thresh_vis=thresh_vis,
        contour=contour,
        bbox=bbox,
        motion_vec=res_small.motion_vec,
        motion_label=res_small.motion_label,
        line_point=line_point,
        line_dir=res_small.line_dir,
    )
