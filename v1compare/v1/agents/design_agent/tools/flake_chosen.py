"""
Flake selection tool for choosing the best hBN mask for a graphene mask.

Rule:
1) Candidate hBN mask must fully cover graphene mask.
2) If multiple candidates satisfy (1), choose the one with the largest area.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List

import cv2
import numpy as np
from smolagents import tool


def _largest_connected_component(mask01: np.ndarray) -> np.ndarray:
    """Keep only the largest connected component from a binary {0,1} mask."""
    if np.count_nonzero(mask01) == 0:
        return mask01

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask01, connectivity=8)
    if num_labels <= 1:
        return np.zeros_like(mask01, dtype=np.uint8)

    # Skip background index 0
    largest_idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (labels == largest_idx).astype(np.uint8)


def _load_binary_mask(path: str) -> np.ndarray:
    """Load a mask from disk, binarize it, and keep only largest connected component."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")

    mask = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if mask is None:
        raise ValueError(f"Could not read image: {path}")

    # Any non-zero pixel is treated as foreground.
    binary = (mask > 0).astype(np.uint8)
    return _largest_connected_component(binary)


def _mask_centroid(mask01: np.ndarray) -> tuple[int, int]:
    ys, xs = np.where(mask01 > 0)
    if len(xs) == 0:
        return (0, 0)
    return (int(np.mean(xs)), int(np.mean(ys)))


def _principal_axis_angle(mask01: np.ndarray) -> float:
    """Estimate principal-axis angle in degrees via PCA."""
    ys, xs = np.where(mask01 > 0)
    if len(xs) < 2:
        return 0.0
    coords = np.column_stack((xs, ys)).astype(np.float32)
    mean, eigenvectors, _ = cv2.PCACompute2(coords, mean=np.array([]))
    v = eigenvectors[0]
    return float(np.degrees(np.arctan2(v[1], v[0])))


def _rotate_mask(mask01: np.ndarray, angle_deg: float, center: tuple[int, int]) -> np.ndarray:
    h, w = mask01.shape
    M = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    rot = cv2.warpAffine((mask01 * 255).astype(np.uint8), M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    return (rot > 0).astype(np.uint8)


def _translate_mask(mask01: np.ndarray, dx: int, dy: int) -> np.ndarray:
    h, w = mask01.shape
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    shifted = cv2.warpAffine((mask01 * 255).astype(np.uint8), M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    return (shifted > 0).astype(np.uint8)


def _search_cover_transform(
    graphene01: np.ndarray,
    hbn01: np.ndarray,
    max_uncovered_ratio: float = 0.30,
) -> Dict:
    """
    Fast search rotation + translation for hBN to cover graphene.
    Coverage pass criterion: uncovered_ratio <= max_uncovered_ratio.

    Returns:
        dict with fields:
            covers_enough, covers_all, uncovered_pixels, uncovered_ratio, angle_deg, dx, dy, transformed_hbn_area
    """
    graphene_area = int(np.count_nonzero(graphene01))
    if graphene_area == 0:
        return {
            "covers_enough": True,
            "covers_all": True,
            "uncovered_pixels": 0,
            "uncovered_ratio": 0.0,
            "angle_deg": 0.0,
            "dx": 0,
            "dy": 0,
            "transformed_hbn_area": int(np.count_nonzero(hbn01)),
        }

    cgx, cgy = _mask_centroid(graphene01)
    hcx, hcy = _mask_centroid(hbn01)

    # Use principal-axis alignment to reduce angle search from full 360 sweep.
    angle_g = _principal_axis_angle(graphene01)
    angle_h = _principal_axis_angle(hbn01)
    base = angle_g - angle_h
    coarse_angles = [base + d for d in (-12, -6, 0, 6, 12)]
    coarse_angles += [base + 180 + d for d in (-12, -6, 0, 6, 12)]
    coarse_offsets = (-24, -12, -6, 0, 6, 12, 24)

    best = {
        "covers_enough": False,
        "covers_all": False,
        "uncovered_pixels": graphene_area,
        "uncovered_ratio": 1.0,
        "angle_deg": 0.0,
        "dx": 0,
        "dy": 0,
        "transformed_hbn_area": int(np.count_nonzero(hbn01)),
    }

    # Stage 1: coarse local search around principal-axis alignment.
    for angle in coarse_angles:
        rot = _rotate_mask(hbn01, float(angle), (hcx, hcy))
        rcx, rcy = _mask_centroid(rot)
        base_dx = cgx - rcx
        base_dy = cgy - rcy

        for ox in coarse_offsets:
            for oy in coarse_offsets:
                dx = int(base_dx + ox)
                dy = int(base_dy + oy)
                moved = _translate_mask(rot, dx, dy)
                uncovered = int(np.count_nonzero(graphene01 & (1 - moved)))
                if uncovered < best["uncovered_pixels"]:
                    best = {
                        "covers_enough": (uncovered / graphene_area) <= max_uncovered_ratio,
                        "covers_all": uncovered == 0,
                        "uncovered_pixels": uncovered,
                        "uncovered_ratio": float(uncovered / graphene_area),
                        "angle_deg": float(angle),
                        "dx": dx,
                        "dy": dy,
                        "transformed_hbn_area": int(np.count_nonzero(moved)),
                    }
                if uncovered == 0:
                    break
            if best["covers_all"]:
                break
        if best["covers_all"]:
            break

    # Stage 2: small refinement around current best.
    seed_angle = float(best["angle_deg"])
    seed_dx = int(best["dx"])
    seed_dy = int(best["dy"])
    for angle in np.arange(seed_angle - 4.0, seed_angle + 4.1, 2.0):
        rot = _rotate_mask(hbn01, float(angle), (hcx, hcy))
        for dx in range(seed_dx - 6, seed_dx + 7, 3):
            for dy in range(seed_dy - 6, seed_dy + 7, 3):
                moved = _translate_mask(rot, int(dx), int(dy))
                uncovered = int(np.count_nonzero(graphene01 & (1 - moved)))
                if uncovered < best["uncovered_pixels"]:
                    best = {
                        "covers_enough": (uncovered / graphene_area) <= max_uncovered_ratio,
                        "covers_all": uncovered == 0,
                        "uncovered_pixels": uncovered,
                        "uncovered_ratio": float(uncovered / graphene_area),
                        "angle_deg": float(angle),
                        "dx": int(dx),
                        "dy": int(dy),
                        "transformed_hbn_area": int(np.count_nonzero(moved)),
                    }

    best["covers_all"] = best["uncovered_pixels"] == 0
    best["covers_enough"] = best["uncovered_ratio"] <= max_uncovered_ratio
    return best


def _parse_hbn_paths(raw_paths: str) -> List[str]:
    """
    Parse hBN paths from:
    - JSON array string: '["a.png", "b.png"]'
    - Comma-separated string: 'a.png,b.png'
    - Newline-separated string
    """
    text = raw_paths.strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(p).strip() for p in parsed if str(p).strip()]
        except json.JSONDecodeError:
            pass

    if "\n" in text:
        return [p.strip() for p in text.splitlines() if p.strip()]
    return [p.strip() for p in text.split(",") if p.strip()]


def choose_best_covering_hbn(
    graphene_path: str,
    hbn_paths: List[str],
    max_uncovered_ratio: float = 0.30,
) -> Dict:
    """
    Choose the hBN mask that best covers graphene (allowing up to 30% uncovered by default),
    and among valid candidates choose the largest hBN area.

    Args:
        graphene_path: Path to graphene mask image.
        hbn_paths: Candidate hBN mask paths.

    Returns:
        A dictionary with chosen mask info and candidates summary.
    """
    if not hbn_paths:
        return {
            "success": False,
            "message": "No hBN candidate paths provided.",
            "graphene_path": graphene_path,
            "best_hbn_path": None,
            "best_hbn_area": 0,
            "valid_candidates": [],
        }

    graphene = _load_binary_mask(graphene_path)
    gh, gw = graphene.shape
    graphene_area = int(np.count_nonzero(graphene))

    valid_candidates = []
    all_candidate_scores = []
    for path in hbn_paths:
        hbn = _load_binary_mask(path)
        if hbn.shape != graphene.shape:
            raise ValueError(
                f"Shape mismatch: graphene={graphene.shape}, hBN({path})={hbn.shape}. "
                "All masks must have the same size."
            )

        # Search rotation + translation for cover check.
        transform = _search_cover_transform(graphene, hbn, max_uncovered_ratio=max_uncovered_ratio)
        area = int(np.count_nonzero(hbn))
        candidate_info = {
            "path": path,
            "area": area,
            "height": gh,
            "width": gw,
            "cover_ratio": float(1.0 - transform["uncovered_ratio"]),
            "best_transform": {
                "rotation_angle_deg": float(transform["angle_deg"]),
                "translation": [int(transform["dx"]), int(transform["dy"])],
                "uncovered_pixels": int(transform["uncovered_pixels"]),
                "uncovered_ratio": float(transform["uncovered_ratio"]),
            },
        }
        all_candidate_scores.append(candidate_info)
        if transform["covers_enough"]:
            valid_candidates.append(candidate_info)

    if not valid_candidates:
        best_effort = min(all_candidate_scores, key=lambda x: x["best_transform"]["uncovered_pixels"]) if all_candidate_scores else None

        return {
            "success": False,
            "message": f"No hBN mask meets cover threshold (>= {int((1 - max_uncovered_ratio) * 100)}% graphene coverage).",
            "graphene_path": graphene_path,
            "graphene_area": graphene_area,
            "best_hbn_path": None,
            "best_hbn_area": 0,
            "valid_candidates": [],
            "cover_threshold": float(1.0 - max_uncovered_ratio),
            "best_effort_candidate": best_effort,
        }

    best = max(valid_candidates, key=lambda x: x["area"])
    return {
        "success": True,
        "message": f"Found hBN mask(s) meeting coverage threshold (>= {int((1 - max_uncovered_ratio) * 100)}%).",
        "graphene_path": graphene_path,
        "graphene_area": graphene_area,
        "cover_threshold": float(1.0 - max_uncovered_ratio),
        "best_hbn_path": best["path"],
        "best_hbn_area": best["area"],
        "best_cover_ratio": best["cover_ratio"],
        "num_valid_candidates": len(valid_candidates),
        "valid_candidates": valid_candidates,
    }


@tool
def choose_hbn_for_graphene(graphene_path: str, hbn_paths: str) -> str:
    """
    Choose the best hBN image path for graphene.

    Args:
        graphene_path: Path to the graphene mask image.
        hbn_paths: Candidate hBN paths in one of these formats:
            1) Comma-separated string: "a.png,b.png,c.png"
            2) JSON list string: '["a.png","b.png","c.png"]'
            3) Newline-separated text

    Rule:
    - Keep only hBN masks that can fully cover graphene.
    - If multiple satisfy this, choose the one with larger hBN area.

    Returns:
        JSON string with best path and candidate details.
    """
    try:
        candidates = _parse_hbn_paths(hbn_paths)
        result = choose_best_covering_hbn(graphene_path, candidates)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        error = {
            "success": False,
            "message": f"Failed to choose hBN mask: {exc}",
            "graphene_path": graphene_path,
            "best_hbn_path": None,
            "best_hbn_area": 0,
        }
        return json.dumps(error, ensure_ascii=False, indent=2)
