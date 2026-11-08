"""
Material Design Optimization Implementation
============================================
This module provides functions for optimizing material placement on electrodes
based on contact/overlap constraints and spatial relationships.

Core functionality:
- Binary map encoding for multi-layer configurations
- Constraint evaluation with completion degrees
- Differential Evolution optimization
- Transformation tracking and inverse recovery

Note: This file is the deterministic layout optimization library used by the
design agent. The actual smolagents tools live in layout_tool.py; keep those
wrappers focused on agent state orchestration and leave the core math here.
"""

import json
import os
import re
from typing import Dict, List, Tuple

import cv2
import litellm
import numpy as np
from scipy.optimize import differential_evolution

from common.llm_config import DEFAULT_MODEL_ID, get_api_base_for_model, get_api_key_for_model, resolve_model_id

try:
    import matplotlib.cm as cm
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    print("[WARNING] matplotlib not available, some visualization features will be disabled")


# ============================================================================
# Utility Functions - Basic Operations
# ============================================================================

def down_sample(masks: Dict[str, np.ndarray],
                downsample_size: Tuple[int, int] = (192, 256)) -> Dict[str, np.ndarray]:
    """
    Resize all masks to a standard size.
    
    Args:
        masks: Dictionary of mask arrays
        downsample_size: Target size (height, width)
    
    Returns:
        resized_masks: Dictionary of resized masks
    """
    h, w = downsample_size
    resized_masks = {}
    for name, mask in masks.items():
        if mask is None:
            print(f"[Warning] Mask '{name}' not found or invalid.")
            continue
        resized = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
        resized_masks[name] = resized
    return resized_masks


def _ensure_grayscale(image: np.ndarray) -> np.ndarray:
    """Normalize input into a 2D grayscale image for OpenCV preprocessing."""
    if image is None:
        raise ValueError("Input image is None")

    if image.ndim == 2:
        return image

    if image.ndim == 3:
        channels = image.shape[2]
        if channels == 1:
            return image[:, :, 0]
        if channels == 3:
            return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if channels == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)

    raise ValueError(f"Unsupported image shape for grayscale conversion: {image.shape}")


def preprocess_image(image: np.ndarray) -> np.ndarray:
    """
    Preprocess a raw image to create a clean binary mask.
    Applies Gaussian blur, Otsu's thresholding, and morphological opening.
    
    Args:
        image: Input image (grayscale or color)
    
    Returns:
        binary_mask: Clean binary mask (0 and 255)
    """
    # Convert to grayscale if needed
    gray = _ensure_grayscale(image)

    # 1. Apply Gaussian Blur to reduce noise
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # 2. Otsu's Thresholding
    # This automatically finds the optimal threshold value
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 3. Morphological Opening (Erosion followed by Dilation)
    # Removing small noise specks
    kernel = np.ones((3, 3), np.uint8)
    opening = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    
    # 4. Automatic Inversion Check
    # Electrodes should be white on black background.
    # If mask is mostly white (>0.5 coverage), it's likely inverted (dark electrodes on bright background)
    white_ratio = np.sum(opening == 255) / opening.size
    if white_ratio > 0.5:
        opening = cv2.bitwise_not(opening)
        
    return opening


def preprocess_electrodes(image: np.ndarray) -> np.ndarray:
    """
    Preprocess a raw electrode image to a clean binary mask.
    Applies Gaussian blur, Otsu thresholding, and morphological opening.

    Args:
        image: Input image (grayscale or color)

    Returns:
        binary_mask: Clean binary mask (0 and 255)
    """
    # Convert to grayscale if needed
    gray = _ensure_grayscale(image)

    # 1. Apply Gaussian Blur to reduce noise
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # 2. Otsu's Thresholding
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 3. Morphological Opening (Erosion followed by Dilation)
    kernel = np.ones((3, 3), np.uint8)
    opening = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)

    # 4. Automatic Inversion Check
    # Electrodes should be white on black background.
    white_ratio = np.sum(opening == 255) / opening.size
    if white_ratio > 0.5:
        opening = cv2.bitwise_not(opening)

    return opening


def find_closest_to_center(mask: np.ndarray) -> Tuple[int, int]:
    """
    Find the pixel closest to image center within the mask.
    
    Args:
        mask: Binary mask array
    
    Returns:
        closest_point: (x, y) coordinates of closest pixel
    """
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        raise ValueError("No nonzero pixels found in mask")
    h, w = mask.shape
    cx, cy = w / 2, h / 2
    dists = np.sqrt((xs - cx)**2 + (ys - cy)**2)
    idx = np.argmin(dists)
    closest_point = (int(xs[idx]), int(ys[idx]))
    return closest_point


def find_electrode_midpoint(masks: Dict[str, np.ndarray], 
                           electrodes: List[str]) -> Tuple[int, int]:
    """
    Find the center point of the contact_electrode region.
    If contact_electrode is not present, falls back to calculating 
    the average of all electrode positions.
    
    Args:
        masks: Dictionary containing electrode masks
        electrodes: List of electrode names (should include "contact_electrode")
    
    Returns:
        mid_pt: (x, y) coordinates of contact electrode center or electrode midpoint
    """
    if not electrodes:
        raise ValueError("No electrodes were provided")

    # Prioritize contact_electrode if it exists
    if "contact_electrode" in masks:
        contact_mask = masks["contact_electrode"]
        # Check if mask has any non-zero pixels
        if np.count_nonzero(contact_mask) > 0:
            # Use centroid of contact_electrode
            M = cv2.moments(contact_mask)
            if M["m00"] > 0:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
                return (cx, cy)
            else:
                # If moments calculation fails, use closest point to center
                pt = find_closest_to_center(contact_mask)
                return pt
        else:
            print(f"[WARNING] contact_electrode mask is empty!")
    else:
        print(f"[WARNING] contact_electrode not found in masks. Available keys: {list(masks.keys())}")
    
    # Fallback: calculate average of all electrode positions
    end_pts = []
    for electrode in electrodes:
        if electrode in masks:
            point = find_closest_to_center(masks[electrode])
            end_pts.append(point)
            print(f"  {electrode}: {point}")
    
    if len(end_pts) == 0:
        raise ValueError("No valid electrodes found in masks")
    
    mid_pt = np.mean(end_pts, axis=0)
    mid_pt_tuple = tuple(int(x) for x in mid_pt)
    print(f"[DEBUG] Calculated average midpoint: {mid_pt_tuple}")
    return mid_pt_tuple


def find_design_reference_midpoint(masks: Dict[str, np.ndarray],
                                   materials: List[str],
                                   electrodes: List[str]) -> Tuple[int, int]:
    """
    Return the layout reference point for initial alignment.

    Electrode-backed designs use the contact electrode center. Electrode-free
    designs use the first material centroid, preserving the bottom layer as the
    anchor while aligning the rest of the stack to it.
    """
    if electrodes:
        return find_electrode_midpoint(masks, electrodes)

    if not materials:
        raise ValueError("No materials were provided")

    anchor = materials[0]
    if anchor not in masks:
        raise ValueError(f"Anchor material {anchor!r} not found in masks")

    return get_centroid(masks[anchor])


def _classify_electrode_points(electrode_mask, distance_ratio=1/3):
    """
    Helper function: Classify electrode components into inner and outer groups.
    
    Args:
        electrode_mask: Binary mask of electrodes
        distance_ratio: Threshold ratio for classification (default: 1/3)
    
    Returns:
        num: Number of components (including background)
        labels: Component labels array
        inner_points: Array of inner electrode center points
        outer_points: Array of outer electrode center points
        inner_indices: Component indices of inner electrodes
        outer_indices: Component indices of outer electrodes
    """
    H, W = electrode_mask.shape
    center = np.array([W / 2, H / 2], dtype=np.float32)

    # Find all electrode components
    num, labels, stats, centroids = cv2.connectedComponentsWithStats(electrode_mask)
    if num <= 1:
        empty = np.array([], dtype=np.int32)
        empty_pts = np.empty((0, 2), dtype=np.int32)
        return num, labels, empty_pts, empty_pts, empty, empty

    component_points = []
    nearest_dists = []
    centroid_dists = []
    comp_indices = []

    # Compute robust per-component features
    for i in range(1, num):
        component_mask = (labels == i).astype(np.uint8) * 255
        if np.count_nonzero(component_mask) == 0:
            continue

        nearest_pt = find_closest_to_center(component_mask)
        component_points.append(nearest_pt)
        comp_indices.append(i)

        nearest_dist = np.linalg.norm(np.array(nearest_pt, dtype=np.float32) - center)
        centroid = centroids[i]
        centroid_dist = np.linalg.norm(centroid.astype(np.float32) - center)

        nearest_dists.append(float(nearest_dist))
        centroid_dists.append(float(centroid_dist))

    if len(comp_indices) == 0:
        empty = np.array([], dtype=np.int32)
        empty_pts = np.empty((0, 2), dtype=np.int32)
        return num, labels, empty_pts, empty_pts, empty, empty

    points = np.array(component_points, dtype=np.int32)
    nearest_dists = np.array(nearest_dists, dtype=np.float32)
    centroid_dists = np.array(centroid_dists, dtype=np.float32)
    comp_indices = np.array(comp_indices, dtype=np.int32)

    # Blend centroid and nearest-point distances to reduce false inner detections
    scores = 0.7 * centroid_dists + 0.3 * nearest_dists

    # Adaptive split by largest gap in sorted scores.
    # Fallback to legacy ratio threshold when the gap is not informative.
    if len(scores) >= 2:
        order = np.argsort(scores)
        sorted_scores = scores[order]
        gaps = np.diff(sorted_scores)
        max_gap_idx = int(np.argmax(gaps)) if len(gaps) > 0 else -1

        if len(gaps) > 0 and gaps[max_gap_idx] > 1e-6:
            threshold = 0.5 * (sorted_scores[max_gap_idx] + sorted_scores[max_gap_idx + 1])
        else:
            threshold = float(np.max(scores) * distance_ratio)
    else:
        threshold = float(scores[0])

    inner_mask = scores <= threshold

    # Keep at least one inner component to preserve downstream behavior.
    if not np.any(inner_mask):
        inner_mask[np.argmin(scores)] = True

    inner_indices = comp_indices[inner_mask]
    outer_indices = comp_indices[~inner_mask]
    inner_points = points[inner_mask]
    outer_points = points[~inner_mask]

    return num, labels, inner_points, outer_points, inner_indices, outer_indices


def separate_electrode_masks(electrode_mask, output_dir="example_2", distance_ratio=1/3, contact_electrode_mask=None):
    """
    Separate outer and inner electrodes and save as individual/combined masks.

    When ``contact_electrode_mask`` is provided, E_others is defined as the union
    of all electrode components that have any pixel overlap with the
    contact_electrode region.  Components with no overlap become E1, E2, …

    When ``contact_electrode_mask`` is None the legacy distance-based
    inner/outer classification is used as a fallback.
    
    Args:
        electrode_mask: Binary mask of all electrodes
        output_dir: Directory for output files (default: "example_2")
        distance_ratio: Threshold ratio for fallback classification (default: 1/3)
        contact_electrode_mask: Optional mask of the contact_electrode region.
            If given, determines E_others by intersection instead of distance.
    
    Returns:
        outer_masks: List of individual outer electrode masks
        inner_combined_mask: Combined mask of all inner electrodes (E_others)
        outer_indices: Component indices of outer electrodes
        inner_indices: Component indices of inner electrodes
    """
    H, W = electrode_mask.shape

    if contact_electrode_mask is not None:
        # New: classify each connected component by intersection with contact_electrode
        num, labels = cv2.connectedComponents(electrode_mask)
        inner_indices_list = []
        outer_indices_list = []
        contact_bin = contact_electrode_mask > 0
        for i in range(1, num):
            comp_pixels = labels == i
            if np.any(comp_pixels & contact_bin):
                inner_indices_list.append(i)
            else:
                outer_indices_list.append(i)
        inner_indices = np.array(inner_indices_list, dtype=np.int32)
        outer_indices = np.array(outer_indices_list, dtype=np.int32)
    else:
        # Legacy: distance-based classification
        num, labels, _, _, inner_indices, outer_indices = _classify_electrode_points(
            electrode_mask, distance_ratio
        )

    print(f"Found {num - 1} electrodes total:")
    print(f"  - Outer electrodes (E1, E2, ...): {len(outer_indices)}")
    print(f"  - Inner electrodes / E_others: {len(inner_indices)}")
    
    # Save outer electrodes as individual masks
    outer_masks = []
    for idx, component_id in enumerate(outer_indices, start=1):
        outer_mask = (labels == component_id).astype(np.uint8) * 255
        outer_masks.append(outer_mask)
        output_path = f"{output_dir}/mask_E{idx}.png"
        cv2.imwrite(output_path, outer_mask)
        print(f"✓ Saved outer electrode {idx} to '{output_path}'")
    
    # Combine intersecting components into E_others mask
    inner_combined_mask = np.zeros((H, W), dtype=np.uint8)
    for component_id in inner_indices:
        inner_combined_mask[labels == component_id] = 255

    output_path = f"{output_dir}/mask_E_others.png"
    cv2.imwrite(output_path, inner_combined_mask)
    print(f"✓ Saved E_others (electrodes intersecting contact_electrode) to '{output_path}'")
    
    return outer_masks, inner_combined_mask, outer_indices, inner_indices


def process_electrode_mask(electrode_mask, output_prefix="example_2/mask_contact_electrodes", expand_pixels=10, distance_ratio=1/3):
    """
    Create expanded rotated rectangle mask for inner electrode region.
    
    Args:
        electrode_mask: Binary mask of electrodes
        output_prefix: Prefix for output files (default: "example_2/mask_inner_rect")
        expand_pixels: Expansion size in pixels (default: 10)
        distance_ratio: Threshold ratio for classification (default: 1/3)
    
    Returns:
        expanded_mask: Binary mask of expanded rectangle
        rotated_rect: ((cx, cy), (w, h), angle) of original rotated rectangle
    """
    H, W = electrode_mask.shape
    
    # Classify electrodes using helper function
    num, labels, inner_pts, outer_pts, inner_indices, outer_indices = _classify_electrode_points(
        electrode_mask, distance_ratio
    )
    
    # Build a robust inner-region mask from classified inner components.
    inner_region_mask = np.zeros((H, W), dtype=np.uint8)
    for component_id in inner_indices:
        inner_region_mask[labels == component_id] = 255

    if np.count_nonzero(inner_region_mask) == 0:
        raise ValueError("Failed to extract inner electrode region for contact_electrode")

    # Keep only the center neighborhood to avoid long leads dominating the fit.
    # This targets the compact inner contact area (the intended contact_electrode region).
    cx_img, cy_img = W // 2, H // 2
    if len(inner_pts) > 0:
        spread_x = float(np.max(np.abs(inner_pts[:, 0] - cx_img)))
        spread_y = float(np.max(np.abs(inner_pts[:, 1] - cy_img)))
        roi_half_w = int(max(12, min(W * 0.20, spread_x * 1.8 + expand_pixels * 2)))
        roi_half_h = int(max(12, min(H * 0.20, spread_y * 1.8 + expand_pixels * 2)))
    else:
        roi_half_w = int(max(12, W * 0.08))
        roi_half_h = int(max(12, H * 0.08))

    x1 = max(0, cx_img - roi_half_w)
    x2 = min(W, cx_img + roi_half_w + 1)
    y1 = max(0, cy_img - roi_half_h)
    y2 = min(H, cy_img + roi_half_h + 1)

    # General robust seed construction (no side-specific heuristic):
    # - Keep a center horizontal band (captures left/right contact lines)
    # - Restrict to a finite center-x window (prevents far-right/left long leads)
    # - Add limited vertical support near center-x (captures top/bottom local contact)
    electrode_bin = (electrode_mask > 0).astype(np.uint8)
    center_seed = np.zeros((H, W), dtype=np.uint8)

    x_half_window = int(max(roi_half_w * 3.2, min(W * 0.24, roi_half_w * 6.0)))
    xw1 = max(0, cx_img - x_half_window)
    xw2 = min(W, cx_img + x_half_window + 1)

    band_half_h = int(max(10, min(H * 0.07, roi_half_h * 1.3)))
    hy1 = max(0, cy_img - band_half_h)
    hy2 = min(H, cy_img + band_half_h + 1)

    horizontal_seed = np.zeros((H, W), dtype=np.uint8)
    horizontal_seed[hy1:hy2, xw1:xw2] = electrode_bin[hy1:hy2, xw1:xw2]
    center_seed = np.maximum(center_seed, horizontal_seed * 255)

    # Local vertical support around center-x to include upper contact, but clipped
    # in both x and y so long vertical leads do not dominate.
    x_strip_half = int(max(8, min(W * 0.035, roi_half_w * 0.9)))
    xs1 = max(0, cx_img - x_strip_half)
    xs2 = min(W, cx_img + x_strip_half + 1)
    y_up = int(max(roi_half_h * 2.2, H * 0.12))
    y_down = int(max(roi_half_h * 1.2, H * 0.07))
    ys1 = max(0, cy_img - y_up)
    ys2 = min(H, cy_img + y_down + 1)

    vertical_seed = np.zeros((H, W), dtype=np.uint8)
    vertical_seed[ys1:ys2, xs1:xs2] = electrode_bin[ys1:ys2, xs1:xs2]
    center_seed = np.maximum(center_seed, vertical_seed * 255)

    # Union with inner-region local crop for robustness.
    inner_crop = np.zeros((H, W), dtype=np.uint8)
    inner_crop[hy1:hy2, xw1:xw2] = inner_region_mask[hy1:hy2, xw1:xw2]
    center_seed = np.maximum(center_seed, inner_crop)

    # If too sparse, fallback to compact radial crop around center.
    if np.count_nonzero(center_seed) < 30:
        yy, xx = np.indices((H, W))
        radius = max(15, int(min(H, W) * 0.14))
        radial = ((xx - cx_img) ** 2 + (yy - cy_img) ** 2) <= radius * radius
        center_seed = np.where(radial, electrode_mask, 0).astype(np.uint8)

    # Final fallback.
    if np.count_nonzero(center_seed) < 10:
        center_seed = inner_region_mask.copy()

    # Fill small gaps inside the center contact cluster before fitting geometry.
    morph_k = max(3, int(round(min(H, W) * 0.005)))
    if morph_k % 2 == 0:
        morph_k += 1
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (morph_k, morph_k))
    refined_inner = cv2.morphologyEx(center_seed, cv2.MORPH_CLOSE, close_kernel, iterations=1)

    # Fit AXIS-ALIGNED rectangle (no rotation).
    # Use robust percentiles to reduce sensitivity to outliers/noise.
    ys_ref, xs_ref = np.where(refined_inner > 0)
    if len(xs_ref) < 10:
        raise ValueError("No contours found in inner electrode region")

    min_side = int(max(12, int(round(min(H, W) * 0.02))))
    x_lo, x_hi = np.percentile(xs_ref, [7, 93])
    y_lo, y_hi = np.percentile(ys_ref, [7, 93])

    x_lo = int(max(0, np.floor(x_lo)))
    x_hi = int(min(W - 1, np.ceil(x_hi)))
    y_lo = int(max(0, np.floor(y_lo)))
    y_hi = int(min(H - 1, np.ceil(y_hi)))

    if (x_hi - x_lo + 1) < min_side:
        c = (x_lo + x_hi) // 2
        half = min_side // 2
        x_lo = max(0, c - half)
        x_hi = min(W - 1, c + half)
    if (y_hi - y_lo + 1) < min_side:
        c = (y_lo + y_hi) // 2
        half = min_side // 2
        y_lo = max(0, c - half)
        y_hi = min(H - 1, c + half)

    cx = float((x_lo + x_hi) / 2.0)
    cy = float((y_lo + y_hi) / 2.0)
    w = float(max(min_side, x_hi - x_lo + 1))
    h = float(max(min_side, y_hi - y_lo + 1))
    angle = 0.0
    rotated_rect = ((cx, cy), (w, h), angle)
    
    # Create expanded rectangle
    expanded_rect = ((cx, cy), (w + 2 * expand_pixels, h + 2 * expand_pixels), angle)
    expanded_box_pts = cv2.boxPoints(expanded_rect).astype(int)
    expanded_mask = np.zeros((H, W), dtype=np.uint8)
    cv2.fillPoly(expanded_mask, [expanded_box_pts], 255)

    
    # Save expanded mask
    cv2.imwrite(f"{output_prefix}.png", expanded_mask)
    print(f"✓ Saved expanded rectangle mask to '{output_prefix}.png'")
    
    # Create visualization
    vis = np.zeros((H, W, 3), dtype=np.uint8)
    vis[electrode_mask > 0] = [100, 100, 100]
    
    # Draw original and expanded rectangles
    original_box_pts = cv2.boxPoints(rotated_rect).astype(int)
    cv2.drawContours(vis, [original_box_pts], 0, (255, 0, 255), 2)  # Magenta: original
    cv2.drawContours(vis, [expanded_box_pts], 0, (255, 255, 0), 3)  # Cyan: expanded
    
    # Add green overlay for expanded area
    green_overlay = vis.copy()
    green_overlay[expanded_mask > 0] = [0, 255, 0]
    vis = cv2.addWeighted(vis, 0.7, green_overlay, 0.3, 0)
    
    # Draw inner points
    for pt in inner_pts:
        cv2.circle(vis, tuple(pt.astype(int)), 5, (0, 255, 0), -1)
    
    cv2.imwrite(f"{output_prefix}_visualization.png", vis)
    print(f"✓ Saved visualization to '{output_prefix}_visualization.png'")
    
    return expanded_mask, rotated_rect


def get_centroid(mask: np.ndarray) -> Tuple[int, int]:
    """
    Calculate the centroid of a mask using image moments.
    
    Args:
        mask: Binary mask array
    
    Returns:
        centroid: (x, y) coordinates of centroid
    """
    M = cv2.moments(mask)
    if M["m00"] == 0:
        raise ValueError("Empty mask (no nonzero pixels)")
    cx = int(M["m10"] / M["m00"])
    cy = int(M["m01"] / M["m00"])
    return (cx, cy)


def get_principal_axis(mask: np.ndarray, debug: bool = False) -> float:
    """
    Compute the principal axis angle of a mask using PCA.
    
    Args:
        mask: Binary mask array
        debug: If True, save visualization
    
    Returns:
        angle: Principal axis angle in degrees
    """
    ys, xs = np.where(mask > 0)
    if len(xs) < 2:
        raise ValueError("Not enough pixels to compute orientation")

    coords = np.column_stack((xs, ys)).astype(np.float32)
    mean, eigenvectors, eigenvalues = cv2.PCACompute2(coords, mean=np.array([]))

    cx, cy = mean[0]
    principal_vec = eigenvectors[0]

    diffs = coords - mean
    proj = diffs @ principal_vec
    farthest_idx = np.argmax(np.abs(proj))
    farthest_proj = proj[farthest_idx]
    if farthest_proj < 0:
        principal_vec = -principal_vec
    
    angle = np.arctan2(principal_vec[1], principal_vec[0]) * 180 / np.pi
    
    if debug:
        vis = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        p1 = (int(cx), int(cy))
        length = 100
        p2 = (int(cx + length * principal_vec[0]), int(cy + length * principal_vec[1]))
        cv2.circle(vis, p1, 3, (0, 0, 255), -1)
        cv2.line(vis, p1, p2, (0, 255, 0), 2)
        cv2.imwrite("principal_axis_debug.png", vis)
    
    return angle


def translate_mask(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """
    Translate a mask by (dx, dy) pixels.
    
    Args:
        mask: Input mask
        dx: Horizontal translation
        dy: Vertical translation
    
    Returns:
        shifted: Translated mask
    """
    h, w = mask.shape[:2]
    M = np.float32([[1, 0, dx],
                    [0, 1, dy]])
    shifted = cv2.warpAffine(mask, M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    return shifted


def rotate_mask(mask: np.ndarray, center: Tuple[int, int], angle: float) -> np.ndarray:
    """
    Rotate a mask around a center point.
    
    Args:
        mask: Input mask
        center: (x, y) rotation center
        angle: Rotation angle in degrees
    
    Returns:
        rotated: Rotated mask
    """
    h, w = mask.shape[:2]
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(mask, M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    return rotated


# ============================================================================
# Binary Map Encoding
# ============================================================================

def build_binary_map(masks: Dict[str, np.ndarray]) -> Tuple[np.ndarray, List[str]]:
    """
    Build a binary map where each pixel encodes which layers are present.
    Uses bitwise encoding: bit i represents layer i.
    
    Args:
        masks: Dictionary of mask arrays
    
    Returns:
        binary_map: 2D array with encoded layer presence
        layer_names: List of layer names (ordered by bit position)
    """
    layer_names = list(masks.keys())
    
    # Get reference shape from first mask
    ref_mask = next(iter(masks.values()))
    H, W = ref_mask.shape
    
    # Initialize binary map
    binary_map = np.zeros((H, W), dtype=np.uint32)
    
    # Build the encoded map: each layer gets a bit position
    for i, layer_name in enumerate(layer_names):
        mask = masks[layer_name]
        # Add to binary map: bit i is set where mask > 0
        binary_map += ((mask > 0).astype(np.uint32) << i)
    
    return binary_map, layer_names


def evaluate_rules(binary_map: np.ndarray, 
                  layer_names: List[str], 
                  output_rules: Dict,
                  materials: List[str] = None,
                  electrodes: List[str] = None) -> Dict:
    """
    Evaluate output rules on the binary map.
    Supports "contact" (direct contact, no layers in between) and "overlap" (layers with material between).
    
    IMPORTANT: Physical stacking model:
    - Electrodes: All on the same substrate plane (bottom level, layer 0)
    - Materials: Stack vertically on top of electrodes (layer 1, 2, 3, ...)
    
    Material-Electrode contact logic:
    - For a material to contact an electrode, NO materials should be BELOW it (between it and electrode plane)
    - Example: graphite can only contact electrode if hBN and WTe2 are not below it at that pixel
    
    Material-Material contact logic:
    - Direct contact requires no materials in between (vertical stacking)
    - Example: hBN contacts WTe2 if they overlap and no other materials are between them
    
    Args:
        binary_map: 2D array with encoded layer presence
        layer_names: List of layer names (ordered by bit position)
        output_rules: Rules dictionary
        materials: List of material names
        electrodes: List of electrode names
    
    Returns:
        results: Dictionary with rule evaluation results
    """
    # Create index lookup for layer positions
    layer_idx = {name: i for i, name in enumerate(layer_names)}
    results = {}
    
    for obj_a, rules in output_rules.items():
        if obj_a not in layer_idx:
            continue
        bit_a = layer_idx[obj_a]
        
        for obj_b, rule in rules.items():
            if obj_b not in layer_idx:
                continue
            
            relation, _ = rule
            if relation == "doesn't matter":
                continue
            
            bit_b = layer_idx[obj_b]
            
            # Determine if this is a material-electrode relation
            is_material_electrode = (electrodes and obj_b in electrodes) or (electrodes and obj_a in electrodes)
            
            # Ensure bit_a > bit_b for the logic below
            if bit_a < bit_b:
                bit_a, bit_b = bit_b, bit_a
            
            # Create mask for pixels where both objects are present
            mask_a = (binary_map >> bit_a) & 1
            mask_b = (binary_map >> bit_b) & 1
            both_present = mask_a & mask_b
            
            # Check if this is material-electrode relation (electrodes on same plane)
            is_material_electrode = False
            if materials and electrodes:
                is_material_electrode = (obj_a in materials and obj_b in electrodes) or \
                                       (obj_a in electrodes and obj_b in materials)
            
            if relation == "contact":
                if is_material_electrode:
                    # Material-Electrode: Check if there are OTHER MATERIALS in between
                    # Electrodes are at substrate level (bottom), materials stack on top
                    # For material to contact electrode, NO materials should be BELOW it
                    
                    # Find which is material and which is electrode
                    if obj_a in materials:
                        mat_name = obj_a
                        mat_bit = bit_a
                        elec_bit = bit_b
                    else:
                        mat_name = obj_b
                        mat_bit = bit_b
                        elec_bit = bit_a
                    
                    # Check if any materials BELOW this material are blocking contact with electrode
                    # Materials stack vertically: WTe2 (bottom) → hBN → graphite (top)
                    # Lower index in materials list = closer to electrode
                    mat_index_in_materials = materials.index(mat_name)
                    
                    # Build mask for materials below this one (using their actual bit positions)
                    below_material_mask = 0
                    for i in range(0, mat_index_in_materials):
                        other_mat = materials[i]
                        # Get the actual bit position of this material in layer_names
                        if other_mat in layer_idx:
                            below_material_mask |= (1 << layer_idx[other_mat])
                    
                    if below_material_mask > 0:
                        # Check if any materials below are blocking
                        below_bits = (binary_map & below_material_mask)
                        # Contact only if no materials below are present
                        contact_pixels = both_present & (below_bits == 0)
                    else:
                        # Bottom material (e.g., WTe2), no materials below to check
                        contact_pixels = both_present
                else:
                    # Material-Material: check no layers in between (vertical stacking)
                    if bit_a - bit_b > 1:
                        # Non-adjacent layers: check no layers in between
                        between_mask = ((1 << (bit_a - bit_b - 1)) - 1) << (bit_b + 1)
                        between_bits = (binary_map & between_mask)
                        contact_pixels = both_present & (between_bits == 0)
                    else:
                        # Adjacent layers: direct contact
                        contact_pixels = both_present
                count = np.count_nonzero(contact_pixels)
                
            elif relation == "overlap":
                if is_material_electrode:
                    # Material-Electrode: doesn't make sense (use "doesn't matter" instead)
                    overlap_pixels = np.zeros_like(both_present)
                else:
                    # Material-Material: check layers in between (vertical stacking)
                    if bit_a - bit_b > 1:
                        # Non-adjacent layers: check for layers in between
                        between_mask = ((1 << (bit_a - bit_b - 1)) - 1) << (bit_b + 1)
                        between_bits = (binary_map & between_mask)
                        overlap_pixels = both_present & (between_bits > 0)
                    else:
                        # Adjacent layers can't have overlap (no room for layers in between)
                        overlap_pixels = np.zeros_like(both_present)
                count = np.count_nonzero(overlap_pixels)
            else:
                count = 0

            key = f"{obj_a}-{obj_b}"
            results[key] = {
                "relation": relation,
                "count": count,
                "obj_a": obj_a,
                "obj_b": obj_b
            }
    
    return results


# ============================================================================
# Constraint Checking and Scoring
# ============================================================================

def check_constraint(count: int, 
                    obj_a_pixels: int, 
                    obj_b_pixels: int, 
                    degree: str) -> Tuple[bool, float]:
    """
    Check if pixel count satisfies the degree constraint.
    
    Args:
        count: Number of pixels satisfying the relation
        obj_a_pixels: Total pixels in object A
        obj_b_pixels: Total pixels in object B
        degree: Constraint degree (not complete, complete, none, max, min)
    
    Returns:
        satisfied: Whether constraint is satisfied
        completion: Completion percentage (0-1)
    """
    smaller = min(obj_a_pixels, obj_b_pixels)
    
    if degree == "not complete":
        satisfied = count >= 40
        completion = min(count / 40.0, 1.0) if satisfied else count / 40.0
        
    elif degree == "complete":
        target = 0.70 * smaller
        satisfied = count >= target
        completion = min(count / target, 1.0) if target > 0 else 0
        
    elif degree == "none":
        target = 0.02 * smaller
        satisfied = count <= target
        completion = max(1.0 - count / (target + 1), 0) if satisfied else max(1.0 - count / (target + 1), 0) * 0.5
        
    elif degree == "max":
        lower = 0.70 * smaller
        upper = 0.90 * smaller
        satisfied = lower <= count <= upper
        if satisfied:
            center = 0.80 * smaller
            completion = 1.0 - abs(count - center) / (upper - lower)
        else:
            if count < lower:
                completion = max(count / lower * 0.5, 0)
            else:
                completion = max((2 * upper - count) / upper * 0.5, 0)
                
    elif degree == "min":
        lower = 0.10 * smaller
        upper = 0.30 * smaller
        satisfied = lower <= count <= upper
        if satisfied:
            center = 0.2 * smaller
            completion = 1.0 - abs(count - center) / (upper - lower)
        else:
            if count < lower:
                completion = max(count / lower * 0.5, 0)
            else:
                completion = max((2 * upper - count) / upper * 0.5, 0)
    else:
        satisfied = False
        completion = 0
    
    return satisfied, completion


def score_configuration(binary_map: np.ndarray,
                       layer_names: List[str],
                       output_rules: Dict,
                       masks: Dict[str, np.ndarray],
                       materials: List[str] = None,
                       electrodes: List[str] = None,
                       mid_pt: Tuple[int, int] = None) -> Tuple[float, int, List[Dict]]:
    """
    Score how well current configuration satisfies output rules.
    Includes completion degrees for all constraint types.
    
    IMPORTANT: Physical stacking model:
    - Electrodes: All on the same substrate plane (bottom level, layer 0)
    - Materials: Stack vertically on top of electrodes (layer 1, 2, 3, ...)
    
    Material-Electrode contact logic:
    - For a material to contact an electrode, NO materials should be BELOW it (between it and electrode plane)
    - Example: graphite can only contact electrode if hBN and WTe2 are not below it at that pixel
    
    Material-Material contact logic:
    - Direct contact requires no materials in between (vertical stacking)
    
    Args:
        binary_map: Encoded layer presence map
        layer_names: List of layer names
        output_rules: Constraint rules
        masks: Dictionary of current masks
        materials: List of material names
        electrodes: List of electrode names
        mid_pt: Midpoint of electrodes (optional)
    
    Returns:
        total_score: Weighted score with completion degrees
        satisfied_count: Number of fully satisfied constraints
        violations: List of unsatisfied constraints
    """
    layer_idx = {name: i for i, name in enumerate(layer_names)}
    total_score = 0.0
    satisfied_count = 0
    violations = []
    
    # Track electrode relations for each material
    material_electrode_relations = {}
    
    for obj_a, rules in output_rules.items():
        if obj_a not in layer_idx:
            continue
        bit_a = layer_idx[obj_a]
        obj_a_pixels = np.count_nonzero(masks[obj_a] > 0)
        
        for obj_b, rule in rules.items():
            if obj_b not in layer_idx:
                continue
            
            relation, degree = rule
            if relation == "doesn't matter":
                continue
            
            bit_b = layer_idx[obj_b]
            obj_b_pixels = np.count_nonzero(masks[obj_b] > 0)
            
            # Track material-electrode relations
            if electrodes and obj_b in electrodes:
                if obj_a not in material_electrode_relations:
                    material_electrode_relations[obj_a] = []
                material_electrode_relations[obj_a].append({
                    'electrode': obj_b,
                    'relation': relation,
                    'degree': degree
                })
            
            # Swap if needed to get higher bit first
            bit_high, bit_low = (bit_a, bit_b) if bit_a > bit_b else (bit_b, bit_a)
            
            # Count pixels satisfying relation
            mask_high = (binary_map >> bit_high) & 1
            mask_low = (binary_map >> bit_low) & 1
            both_present = mask_high & mask_low
            
            # Check if this is material-electrode relation (electrodes on same plane)
            is_material_electrode = False
            if materials and electrodes:
                is_material_electrode = (obj_a in materials and obj_b in electrodes) or \
                                       (obj_a in electrodes and obj_b in materials)
            
            if relation == "contact":
                if is_material_electrode:
                    # Material-Electrode: Check if there are OTHER MATERIALS in between
                    # Electrodes are at substrate level (bottom), materials stack on top
                    # For material to contact electrode, NO materials should be BELOW it
                    
                    # Find which is material and which is electrode
                    if obj_a in materials:
                        mat_name = obj_a
                        mat_bit = bit_a
                        elec_bit = bit_b
                    else:
                        mat_name = obj_b
                        mat_bit = bit_b
                        elec_bit = bit_a
                    
                    # Check if any materials BELOW this material are blocking contact with electrode
                    # Materials stack vertically: WTe2 (bottom) → hBN → graphite (top)
                    # Lower index in materials list = closer to electrode
                    mat_index_in_materials = materials.index(mat_name)
                    
                    # Build mask for materials below this one (using their actual bit positions)
                    below_material_mask = 0
                    for i in range(0, mat_index_in_materials):
                        other_mat = materials[i]
                        # Get the actual bit position of this material in layer_names
                        if other_mat in layer_idx:
                            below_material_mask |= (1 << layer_idx[other_mat])
                    
                    if below_material_mask > 0:
                        # Check if any materials below are blocking
                        below_bits = (binary_map & below_material_mask)
                        # Contact only if no materials below are present
                        valid_pixels = both_present & (below_bits == 0)
                    else:
                        # Bottom material (e.g., WTe2), no materials below to check
                        valid_pixels = both_present
                else:
                    # Material-Material: check no layers in between (vertical stacking)
                    if bit_high - bit_low > 1:
                        # Non-adjacent layers: check no layers in between
                        between_mask = ((1 << (bit_high - bit_low - 1)) - 1) << (bit_low + 1)
                        between_bits = (binary_map & between_mask)
                        valid_pixels = both_present & (between_bits == 0)
                    else:
                        # Adjacent layers: direct contact
                        valid_pixels = both_present
                count = np.count_nonzero(valid_pixels)
                
            elif relation == "overlap":
                if is_material_electrode:
                    # Material-Electrode: doesn't make sense (use "doesn't matter" instead)
                    valid_pixels = np.zeros_like(both_present)
                else:
                    # Material-Material: check layers in between (vertical stacking)
                    if bit_high - bit_low > 1:
                        # Non-adjacent layers: check for layers in between
                        between_mask = ((1 << (bit_high - bit_low - 1)) - 1) << (bit_low + 1)
                        between_bits = (binary_map & between_mask)
                        valid_pixels = both_present & (between_bits > 0)
                    else:
                        # Adjacent layers can't have overlap (no room for layers in between)
                        valid_pixels = np.zeros_like(both_present)
                count = np.count_nonzero(valid_pixels)
            else:
                count = 0
            
            # Check constraint with completion degree
            satisfied, completion = check_constraint(count, obj_a_pixels, obj_b_pixels, degree)
            
            # Add completion score (0 to 1)
            total_score += completion
            
            if satisfied:
                satisfied_count += 1
            else:
                smaller = min(obj_a_pixels, obj_b_pixels)
                violations.append({
                    "rule": f"{obj_a}->{obj_b}",
                    "relation": relation,
                    "degree": degree,
                    "count": count,
                    "smaller": smaller,
                    "completion": completion,
                    "satisfied": False
                })
    
    return total_score, satisfied_count, violations


# ============================================================================
# Initial Alignment
# ============================================================================

def initial_align(masks: Dict[str, np.ndarray],
                  materials: List[str],
                  electrodes: List[str],
                  output_rules: Dict,
                  debug: bool = False,
                  output_dir: str = ".") -> Tuple[Dict[str, np.ndarray], Dict]:
    """
    Perform initial alignment of materials to electrodes.
    All rotations are around each material's own centroid.
    
    Args:
        masks: Dictionary of all masks (materials + electrodes)
        materials: List of material names
        electrodes: List of electrode names
        output_rules: Constraint rules
        debug: If True, save debug images
    
    Returns:
        masks: Updated masks with aligned materials
        transformations: Transformation records for each material
    """
    mid_pt = find_design_reference_midpoint(masks, materials, electrodes)
    if electrodes:
        print(f"Midpoint of electrodes: {mid_pt}")
    else:
        print(f"Electrode-free reference midpoint: {mid_pt}")
    
    transformations = {}

    for material in materials:
        mask = masks[material]
        original_centroid = get_centroid(mask)
        
        # Initialize transformation record
        transformations[material] = {
            'original_centroid': original_centroid,
            'total_rotation': 0.0,
            'total_translation': np.array([0.0, 0.0])
        }
        
        # Step 1: Translate to mid_pt
        dx1 = mid_pt[0] - original_centroid[0]
        dy1 = mid_pt[1] - original_centroid[1]
        mask = translate_mask(mask, dx1, dy1)
        transformations[material]['total_translation'] += np.array([dx1, dy1])

        for electrode in electrodes:
            if electrode not in output_rules.get(material, {}):
                continue
            electrode_end_pt = find_closest_to_center(masks[electrode])
            if output_rules[material][electrode][0] == "contact" and \
               output_rules[material][electrode][1] == "not complete":

                # Step 2: Rotate around current centroid
                angle_flake = get_principal_axis(mask)
                dx = electrode_end_pt[0] - mid_pt[0]
                dy = electrode_end_pt[1] - mid_pt[1]
                angle_to_center = np.degrees(np.arctan2(dy, dx))

                delta_angle = (-angle_to_center + angle_flake)
                
                current_centroid = get_centroid(mask)
                mask = rotate_mask(mask, center=current_centroid, angle=delta_angle)
                transformations[material]['total_rotation'] += delta_angle
                ori_mask = mask.copy()

                # Step 3: Move toward electrode to establish contact
                electrode_mask = masks[electrode]
                overlap = cv2.bitwise_and(mask, electrode_mask)
                overlap_pixels = np.count_nonzero(overlap)

                if overlap_pixels < 20:
                    print(f"Moving {material} toward electrode {electrode}...")
                    
                    # Calculate distance to electrode for adaptive step size
                    material_centroid = get_centroid(mask)
                    dist_to_electrode = np.linalg.norm(
                        np.array(material_centroid) - np.array(electrode_end_pt)
                    )
                    
                    # Adaptive step size: larger steps for larger distances
                    if dist_to_electrode > 100:
                        step_size = 4
                        max_steps = 500
                    elif dist_to_electrode > 50:
                        step_size = 3
                        max_steps = 400
                    else:
                        step_size = 2
                        max_steps = 300
                    
                    dx_step = np.cos(np.radians(angle_to_center))
                    dy_step = np.sin(np.radians(angle_to_center))
                    
                    total_dx3 = 0
                    total_dy3 = 0
                    best_mask = mask.copy()
                    best_overlap = overlap_pixels
                    best_dx = 0
                    best_dy = 0

                    for step in range(max_steps):
                        mask = translate_mask(mask, int(step_size * dx_step), int(step_size * dy_step))
                        total_dx3 += int(step_size * dx_step)
                        total_dy3 += int(step_size * dy_step)
                        overlap = cv2.bitwise_and(mask, electrode_mask)
                        overlap_pixels = np.count_nonzero(overlap)
                        
                        # Track best overlap achieved
                        if overlap_pixels > best_overlap:
                            best_overlap = overlap_pixels
                            best_mask = mask.copy()
                            best_dx = total_dx3
                            best_dy = total_dy3

                        if overlap_pixels >= 40:
                            print(f"Contact established: {overlap_pixels} pixels (step {step+1}/{max_steps})")
                            transformations[material]['total_translation'] += np.array([total_dx3, total_dy3])
                            break
                    else:
                        # Use best overlap achieved if target not reached
                        if best_overlap > overlap_pixels or best_overlap >= 20:
                            print(f"Max steps reached, using best overlap: {best_overlap} pixels")
                            mask = best_mask
                            transformations[material]['total_translation'] += np.array([best_dx, best_dy])
                        else:
                            print(f"Max steps reached, insufficient overlap ({best_overlap} pixels), using rotation result")
                            mask = ori_mask
                        
        if debug:
            cv2.imwrite(os.path.join(output_dir, f"aligned_{material}_debug.png"), mask)
        masks[material] = mask
    
    # Save debug visualization of all masks together
    if debug:
        print("\n[DEBUG] Creating visualization of all aligned masks...")
        H, W = next(iter(masks.values())).shape
        
        # Generate distinct colors for visualization
        all_layers = materials + electrodes
        n_layers = len(all_layers)
        
        # Use HSV colormap for distinct colors
        if MATPLOTLIB_AVAILABLE:
            if n_layers <= 10:
                cmap = cm.get_cmap('tab10')
                colors = [cmap(i)[:3] for i in range(n_layers)]
            else:
                cmap = cm.get_cmap('hsv')
                colors = [cmap(i / n_layers)[:3] for i in range(n_layers)]
            
            # Convert RGB (0-1) to BGR (0-255) for OpenCV
            colors_bgr = [(int(b*255), int(g*255), int(r*255)) for r, g, b in colors]
        else:
            # Fallback: use simple color palette
            base_colors = [
                (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
                (255, 0, 255), (0, 255, 255), (128, 0, 0), (0, 128, 0),
                (0, 0, 128), (128, 128, 0)
            ]
            colors_bgr = [base_colors[i % len(base_colors)] for i in range(n_layers)]
        
        # Create merged visualization
        merged_vis = np.zeros((H, W, 3), dtype=np.float32)
        
        # Add electrode layers (bottom, semi-transparent)
        for i, electrode in enumerate(electrodes):
            idx = materials.index(electrode) if electrode in materials else len(materials) + electrodes.index(electrode)
            color = np.array(colors_bgr[idx], dtype=np.float32)
            layer = np.zeros_like(merged_vis)
            layer[masks[electrode] > 0] = color
            merged_vis = cv2.addWeighted(merged_vis, 1.0, layer, 0.4, 0)
        
        # Add material layers (top, more opaque)
        for i, material in enumerate(materials):
            color = np.array(colors_bgr[i], dtype=np.float32)
            layer = np.zeros_like(merged_vis)
            layer[masks[material] > 0] = color
            merged_vis = cv2.addWeighted(merged_vis, 1.0, layer, 0.6, 0)
        
        merged_vis = np.clip(merged_vis, 0, 255).astype(np.uint8)
        
        # Add text legend
        y_offset = 20
        for i, layer in enumerate(all_layers):
            color = colors_bgr[i]
            text = f"{layer}"
            cv2.putText(merged_vis, text, (10, y_offset), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
            y_offset += 15
        
        # Draw midpoint marker
        cv2.circle(merged_vis, mid_pt, 5, (0, 255, 255), -1)  # Yellow circle
        cv2.circle(merged_vis, mid_pt, 6, (0, 0, 0), 1)      # Black outline
        cv2.putText(merged_vis, "MID", (mid_pt[0] + 8, mid_pt[1] + 5),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1, cv2.LINE_AA)
        
        output_path = os.path.join(output_dir, "initial_align_all_masks_debug.png")
        cv2.imwrite(output_path, merged_vis)
        print(f"✓ Saved visualization: {output_path}")
    
    return masks, transformations


def visualize_violations(masks: Dict[str, np.ndarray],
                        violations: List[Dict],
                        materials: List[str],
                        electrodes: List[str],
                        output_filename: str = "violations_visualization.png",
                        base_image: np.ndarray = None) -> None:
    """
    Visualize violation pixels on the mask configuration.
    
    Args:
        masks: Dictionary of current masks
        violations: List of violation dictionaries
        materials: List of material names
        electrodes: List of electrode names
        output_filename: Output filename for visualization
        base_image: Optional base image to overlay violations on
    """
    if len(violations) == 0:
        print(f"✓ No violations to visualize")
        return
    
    H, W = next(iter(masks.values())).shape
    
    # Create base visualization if not provided
    if base_image is None:
        base_vis = np.zeros((H, W, 3), dtype=np.uint8)
        base_vis[:] = (50, 50, 50)  # Dark gray background
        
        # Generate colors for layers
        all_layers = materials + electrodes
        n_layers = len(all_layers)
        if MATPLOTLIB_AVAILABLE and n_layers <= 10:
            cmap = cm.get_cmap('tab10')
            colors_bgr = [(int(b*255), int(g*255), int(r*255)) 
                         for r, g, b in [cmap(i)[:3] for i in range(n_layers)]]
        else:
            # Fallback colors
            colors_bgr = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
                         (255, 0, 255), (0, 255, 255)] * (n_layers // 6 + 1)
        
        # Add semi-transparent electrode layers (bottom layer)
        for i, electrode in enumerate(electrodes):
            mask = masks[electrode]
            color_idx = len(materials) + i
            color = np.array(colors_bgr[color_idx], dtype=np.float32)
            layer = np.zeros((H, W, 3), dtype=np.float32)
            layer[mask > 0] = color
            base_vis = cv2.addWeighted(base_vis.astype(np.float32), 1.0, layer, 0.3, 0).astype(np.uint8)
        
        # Add semi-transparent material layers (top layer)
        for i, material in enumerate(materials):
            mask = masks[material]
            color = np.array(colors_bgr[i], dtype=np.float32)
            layer = np.zeros((H, W, 3), dtype=np.float32)
            layer[mask > 0] = color
            base_vis = cv2.addWeighted(base_vis.astype(np.float32), 1.0, layer, 0.4, 0).astype(np.uint8)
        
        vis = base_vis.copy()
    else:
        vis = base_image.copy()
    
    # Build binary map for checking layers in between
    # Use the layer_names returned by build_binary_map to ensure consistency
    binary_map, layer_names = build_binary_map(masks)
    layer_idx = {name: i for i, name in enumerate(layer_names)}
    
    print(f"DEBUG: layer_names order = {layer_names}")
    print(f"DEBUG: materials = {materials}")
    print(f"DEBUG: electrodes = {electrodes}")
    
    # Violation colors
    violation_colors = [
        (0, 0, 255),    # Red
        (0, 165, 255),  # Orange
        (0, 255, 255),  # Yellow
        (255, 0, 255),  # Magenta
        (255, 0, 0),    # Blue
        (255, 255, 0),  # Cyan
    ]
    
    print(f"\nVisualizing {len(violations)} violations...")
    
    for i, v in enumerate(violations, 1):
        rule_name = v['rule']
        relation = v['relation']
        degree = v['degree']
        
        parts = rule_name.split('->')
        if len(parts) != 2:
            continue
        
        obj_a, obj_b = parts[0], parts[1]
        
        if obj_a not in masks or obj_b not in masks:
            continue
        
        mask_a = masks[obj_a]
        mask_b = masks[obj_b]
        
        # Get bit positions
        bit_a = layer_idx[obj_a]
        bit_b = layer_idx[obj_b]
        bit_high, bit_low = (bit_a, bit_b) if bit_a > bit_b else (bit_b, bit_a)
        
        print(f"\nDEBUG violation {i}: {rule_name}")
        print(f"  obj_a={obj_a} (bit {bit_a}), obj_b={obj_b} (bit {bit_b})")
        print(f"  mask_a pixels: {np.count_nonzero(mask_a > 0)}")
        print(f"  mask_b pixels: {np.count_nonzero(mask_b > 0)}")
        print(f"  direct overlap: {np.count_nonzero((mask_a > 0) & (mask_b > 0))}")
        
        # Check if this is material-electrode relation (electrodes on same plane)
        is_material_electrode = (obj_a in materials and obj_b in electrodes) or \
                               (obj_a in electrodes and obj_b in materials)
        
        print(f"  is_material_electrode: {is_material_electrode}")
        
        # Calculate violation pixels using DIRECT mask comparison for debugging
        direct_overlap = (mask_a > 0) & (mask_b > 0)
        
        # Calculate violation pixels using binary_map
        mask_high = (binary_map >> bit_high) & 1
        mask_low = (binary_map >> bit_low) & 1
        both_present = mask_high & mask_low
        
        print(f"  both_present (binary_map): {np.count_nonzero(both_present)}")
        print(f"  direct_overlap (masks): {np.count_nonzero(direct_overlap)}")
        
        if relation == "contact":
            if is_material_electrode:
                # Material-Electrode: Check if there are OTHER MATERIALS in between
                # Electrodes are at substrate level (bottom), materials stack on top
                # For material to contact electrode, NO materials should be BELOW it
                
                # Find which is material and which is electrode
                if obj_a in materials:
                    mat_name = obj_a
                    mat_bit = bit_a
                    elec_bit = bit_b
                else:
                    mat_name = obj_b
                    mat_bit = bit_b
                    elec_bit = bit_a
                
                # Check if any materials BELOW this material are blocking contact with electrode
                # Materials stack vertically: WTe2 (bottom) → hBN → graphite (top)
                # Lower index in materials list = closer to electrode
                mat_index_in_materials = materials.index(mat_name)
                
                # Build mask for materials below this one (using their actual bit positions)
                below_material_mask = 0
                for i in range(0, mat_index_in_materials):
                    other_mat = materials[i]
                    # Get the actual bit position of this material in layer_names
                    if other_mat in layer_idx:
                        below_material_mask |= (1 << layer_idx[other_mat])
                
                print(f"  mat_name: {mat_name}, mat_index_in_materials: {mat_index_in_materials}")
                print(f"  below_material_mask: {bin(below_material_mask)} (actual bit positions in layer_names)")
                
                if below_material_mask > 0:
                    # Check if any materials below are blocking
                    below_bits = (binary_map & below_material_mask)
                    # Contact only if no materials below are present
                    contact_pixels = both_present & (below_bits == 0)
                else:
                    # Bottom material (e.g., WTe2), no materials below to check
                    contact_pixels = both_present
                
                print(f"  contact_pixels (after checking below materials): {np.count_nonzero(contact_pixels)}")
            else:
                # Material-Material: check for direct contact (no layers in between)
                if bit_high - bit_low > 1:
                    between_mask = ((1 << (bit_high - bit_low - 1)) - 1) << (bit_low + 1)
                    between_bits = (binary_map & between_mask)
                    contact_pixels = both_present & (between_bits == 0)
                else:
                    contact_pixels = both_present
            
            if degree == "not complete":
                if v['count'] < 40:
                    violation_pixels = contact_pixels
                else:
                    violation_pixels = np.zeros_like(contact_pixels, dtype=bool)
            elif degree == "none":
                # For "contact none", mark actual contact pixels (should not exist)
                violation_pixels = contact_pixels
            elif degree == "complete":
                smaller_mask = mask_a if v['smaller'] == np.count_nonzero(mask_a > 0) else mask_b
                violation_pixels = (smaller_mask > 0) & (contact_pixels == 0)
            else:
                violation_pixels = contact_pixels
        
        elif relation == "overlap":
            if is_material_electrode:
                # Material-Electrode: doesn't make sense (use "doesn't matter" instead)
                overlap_pixels = np.zeros_like(both_present, dtype=bool)
            else:
                # Material-Material: check for layers in between (vertical stacking)
                if bit_high - bit_low > 1:
                    between_mask = ((1 << (bit_high - bit_low - 1)) - 1) << (bit_low + 1)
                    between_bits = (binary_map & between_mask)
                    overlap_pixels = both_present & (between_bits > 0)
                else:
                    overlap_pixels = np.zeros_like(both_present, dtype=bool)
            
            violation_pixels = overlap_pixels
        else:
            continue
        
        # Highlight violations
        color = violation_colors[(i-1) % len(violation_colors)]
        overlay = vis.copy()
        overlay[violation_pixels] = color
        vis = cv2.addWeighted(vis, 0.6, overlay, 0.4, 0)
        
        violation_count = np.count_nonzero(violation_pixels)
        print(f"  {i}. {rule_name}: {violation_count} pixels ({relation} {degree})")
    
    # Add legend panel on the right side
    legend_width = 500
    legend_height = H
    legend_panel = np.ones((legend_height, legend_width, 3), dtype=np.uint8) * 255  # White background
    
    # Add title
    cv2.putText(legend_panel, "VIOLATIONS", (10, 30), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
    cv2.line(legend_panel, (10, 40), (legend_width-10, 40), (0, 0, 0), 2)
    
    # Add each violation to legend
    y_offset = 70
    for i, v in enumerate(violations, 1):
        if i > len(violation_colors):
            break
        
        color = violation_colors[(i-1) % len(violation_colors)]
        rule_name = v['rule']
        relation = v['relation']
        degree = v['degree']
        count = v.get('count', 0)
        completion = v.get('completion', 0)
        
        # Draw color box
        cv2.rectangle(legend_panel, (15, y_offset-15), (45, y_offset+5), color, -1)
        cv2.rectangle(legend_panel, (15, y_offset-15), (45, y_offset+5), (0, 0, 0), 1)
        
        # Add text
        text = f"{i}. {rule_name}"
        cv2.putText(legend_panel, text, (55, y_offset), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
        
        # Add details
        detail = f"   {relation} {degree}"
        cv2.putText(legend_panel, detail, (55, y_offset+18), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (100, 100, 100), 1)
        
        # Add counts
        stats = f"   pixels: {count} ({completion*100:.0f}%)"
        cv2.putText(legend_panel, stats, (55, y_offset+35), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (100, 100, 100), 1)
        
        y_offset += 60
    
    # Add explanation at bottom
    y_offset += 20
    cv2.line(legend_panel, (10, y_offset), (legend_width-10, y_offset), (200, 200, 200), 1)
    y_offset += 25
    cv2.putText(legend_panel, "LEGEND:", (10, y_offset), 
               cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
    y_offset += 25
    
    explanations = [
        "contact = layers touch directly",
        "overlap = layers overlap (with",
        "           layers in between)",
        "",
        "complete = 70%+ coverage",
        "not complete = 40+ pixels",
        "none = <2% coverage",
        "max = 70-90% coverage",
        "min = 10-30% coverage"
    ]
    
    for exp in explanations:
        cv2.putText(legend_panel, exp, (15, y_offset), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.35, (80, 80, 80), 1)
        y_offset += 18
    
    # Combine visualization with legend
    final_vis = np.hstack([vis, legend_panel])
    
    cv2.imwrite(output_filename, final_vis)
    print(f"✓ Saved: {output_filename}\n")


# ============================================================================
# Optimization with Differential Evolution
# ============================================================================

def objective_function(params: np.ndarray,
                      original_materials: Dict[str, np.ndarray],
                      materials: List[str],
                      electrodes: List[str],
                      masks: Dict[str, np.ndarray],
                      output_rules: Dict) -> float:
    """
    Objective function for optimization.
    Returns negative score for minimization.
    """
    current_masks = {k: v.copy() for k, v in masks.items()}
    
    for i, material in enumerate(materials):
        dx = params[i * 3]
        dy = params[i * 3 + 1]
        angle = params[i * 3 + 2]
        
        mask = original_materials[material].copy()
        
        # Rotate around material's current centroid
        if abs(angle) > 0.01:
            centroid = get_centroid(mask)
            mask = rotate_mask(mask, centroid, angle)
        
        # Then translate
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            mask = translate_mask(mask, int(dx), int(dy))
        
        current_masks[material] = mask
    
    # Build binary map and score
    layer_names = list(current_masks.keys())
    binary_map, _ = build_binary_map(current_masks)
    mid_pt = find_design_reference_midpoint(current_masks, materials, electrodes)
    
    total_score, satisfied_count, violations = score_configuration(
        binary_map, layer_names, output_rules, current_masks, materials, electrodes, mid_pt
    )
    
    return -(total_score + satisfied_count * 2)


def optimize_with_de(masks: Dict[str, np.ndarray],
                    materials: List[str],
                    electrodes: List[str],
                    output_rules: Dict,
                    transformations: Dict,
                    max_iter: int = 100) -> Tuple[Dict[str, np.ndarray], float, int, List[Dict], Dict]:
    """
    Optimize material placement using Differential Evolution.
    
    Args:
        masks: Current masks (from initial_align)
        materials: List of material names
        electrodes: List of electrode names
        output_rules: Constraint rules
        transformations: Transformation records from initial_align
        max_iter: Maximum iterations
    
    Returns:
        optimized_masks: Optimized material masks
        total_score: Final score
        satisfied_count: Number of satisfied constraints
        violations: List of remaining violations
        transformations: Updated transformation records
    """
    original_materials = {m: masks[m].copy() for m in materials}
    
    # Define bounds: [dx, dy, angle] for each material
    bounds = []
    for _ in materials:
        bounds.extend([(-30, 30), (-30, 30), (0, 0)])
    
    print(f"Optimizing {len(materials)} materials with Differential Evolution...")
    print(f"Total parameters: {len(bounds)}")
    
    # Custom callback to print progress every 50 iterations
    class ProgressCallback:
        def __init__(self):
            self.counter = 0
            self.printed = set()
        
        def __call__(self, xk, convergence):
            if self.counter % 20 == 0 and self.counter not in self.printed:
                print(f"Iteration {self.counter}: convergence = {convergence:.6f}")
                self.printed.add(self.counter)
            self.counter += 1
            return False
    
    progress = ProgressCallback()
    
    result = differential_evolution(
        objective_function,
        bounds,
        args=(original_materials, materials, electrodes, masks, output_rules),
        maxiter=max_iter,
        popsize=15,
        mutation=(0.5, 1.5),
        recombination=0.7,
        seed=42,
        workers=1,
        updating='deferred',
        disp=False,  
        callback=progress
    )
    
    # Print optimization summary
    print(f"\n=== Differential Evolution Summary ===")
    print(f"Success: {result.success}")
    print(f"Message: {result.message}")
    print(f"Total function evaluations: {result.nfev}")
    print(f"Total iterations: {result.nit}")
    print(f"Best objective value: {result.fun:.6f}")
    print(f"  (Note: objective is negated, so actual score = {-result.fun:.6f})")
    
    # Apply best solution
    best_params = result.x
    optimized_masks = {k: v.copy() for k, v in masks.items()}
    
    print("\n=== Optimization Results ===")
    for i, material in enumerate(materials):
        dx = best_params[i * 3]
        dy = best_params[i * 3 + 1]
        angle = best_params[i * 3 + 2]
        
        mask = original_materials[material].copy()
        
        if abs(angle) > 0.01:
            centroid = get_centroid(mask)
            mask = rotate_mask(mask, centroid, angle)
            transformations[material]['total_rotation'] += angle
        
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            mask = translate_mask(mask, int(dx), int(dy))
            transformations[material]['total_translation'] += np.array([int(dx), int(dy)])
        
        optimized_masks[material] = mask
        print(f"{material}: dx={dx:.1f}, dy={dy:.1f}, angle={angle:.1f}°")
    
    # Final evaluation
    layer_names = list(optimized_masks.keys())
    binary_map, _ = build_binary_map(optimized_masks)
    mid_pt = find_design_reference_midpoint(optimized_masks, materials, electrodes)
    total_score, satisfied_count, violations = score_configuration(
        binary_map, layer_names, output_rules, optimized_masks, materials, electrodes, mid_pt
    )
    
    print(f"\n✓ Optimization complete!")
    print(f"Total score: {total_score:.2f}")
    print(f"Satisfied rules: {satisfied_count}")
    print(f"Remaining violations: {len(violations)}")
    
    return optimized_masks, total_score, satisfied_count, violations, transformations


# ============================================================================
# Transformation Application and Inverse Recovery
# ============================================================================

def apply_transformation(mask: np.ndarray, transformation: Dict) -> np.ndarray:
    """
    Apply recorded transformation to mask.
    
    Args:
        mask: Original mask
        transformation: Dict with 'original_centroid', 'total_rotation', 'total_translation'
    
    Returns:
        transformed_mask: Transformed mask
    """
    result = mask.copy()
    
    # 1. Rotate around original centroid
    if abs(transformation['total_rotation']) > 0.01:
        result = rotate_mask(
            result,
            transformation['original_centroid'],
            transformation['total_rotation']
        )
    
    # 2. Translate
    dx = int(transformation['total_translation'][0])
    dy = int(transformation['total_translation'][1])
    if abs(dx) > 0 or abs(dy) > 0:
        result = translate_mask(result, dx, dy)
    
    return result


def inverse_transformation(mask: np.ndarray, transformation: Dict) -> np.ndarray:
    """
    Apply inverse transformation to recover original mask.
    
    Args:
        mask: Transformed mask
        transformation: Dict with 'original_centroid', 'total_rotation', 'total_translation'
    
    Returns:
        original_mask: Recovered original mask
    """
    result = mask.copy()
    
    # 1. Reverse translation
    dx = -int(transformation['total_translation'][0])
    dy = -int(transformation['total_translation'][1])
    if abs(dx) > 0 or abs(dy) > 0:
        result = translate_mask(result, dx, dy)
    
    # 2. Reverse rotation around original centroid
    if abs(transformation['total_rotation']) > 0.01:
        result = rotate_mask(
            result,
            transformation['original_centroid'],
            -transformation['total_rotation']
        )
    
    return result


# ============================================================================
# High-Level API
# ============================================================================

def optimize_material_design(masks: Dict[str, np.ndarray],
                            materials: List[str],
                            electrodes: List[str],
                            output_rules: Dict,
                            downsample_size: Tuple[int, int] = (192, 256),
                            max_iter: int = 100,
                            debug: bool = False,
                            output_dir: str = ".") -> Tuple[Dict[str, np.ndarray], Dict, Dict]:
    """
    Complete optimization pipeline for 2D material device design.
    
    WORKFLOW OVERVIEW:
    ==================
    This function optimizes the placement of 2D materials on electrodes to satisfy
    geometric constraints for quantum device fabrication. The process has 3 main phases:
    
    PHASE 1: Preprocessing (Downsampling)
    --------------------------------------
    - All masks are resized to a standard size (default: 192x256 pixels)
    - This normalization enables consistent optimization across different image sizes
    - Uses nearest-neighbor interpolation to preserve binary mask integrity
    
    PHASE 2: Initial Alignment
    ---------------------------
    - Calculates reference midpoint: centroid of contact_electrode region
    - For each material:
      1. Translates material centroid to the reference midpoint
      2. If material has ["contact", "not complete"] rule with an electrode:
         a. Rotates material to align its principal axis toward that electrode
         b. Incrementally moves material toward electrode until contact is established
            (adaptive step size: 2-4 pixels, max: 300-500 steps depending on distance)
         c. Tracks best overlap position if target contact cannot be reached
    - Saves debug visualization if enabled (initial_align_all_masks_debug.png)
    
    PHASE 3: Differential Evolution Optimization
    ---------------------------------------------
    - Uses scipy's Differential Evolution algorithm to refine material positions
    - For each material, optimizes 3 parameters:
      * dx: horizontal translation (-30 to +30 pixels)
      * dy: vertical translation (-30 to +30 pixels)
      * angle: rotation (-15° to +15°)
    - Population size: 15 candidate solutions per generation
    - Mutation rate: 0.5-1.5 (exploration vs exploitation balance)
    - Recombination: 0.7 (crossover probability)
    - Maximum iterations: configurable (default: 100)
    
    SCORING MECHANISM:
    ==================
    The objective function minimizes: -(total_score + satisfied_count * 2)
    
    Score Components:
    -----------------
    
    1. PIXEL-LEVEL CONSTRAINT SATISFACTION (completion scores)
       For each rule in output_rules (e.g., "WTe2" -> "contact_electrode": ["contact", "complete"]):
       
       a) Count pixels satisfying the relation:
          - "contact" between material-electrode: Simple pixel overlap (both present)
          - "contact" between materials: Adjacent layers OR no layers in between
          - "overlap" between materials: Both present AND layers exist in between
       
       b) Calculate completion score (0 to 1) based on degree:
          
          - "complete": 
            * target = 0.70 × min(obj_a_pixels, obj_b_pixels)
            * completion = min(count / target, 1.0)
            * satisfied if count >= target
            * Example: WTe2 must cover 70% of contact_electrode
          
          - "not complete":
            * target = 80 pixels (absolute threshold)
            * completion = min(count / 80.0, 1.0)
            * satisfied if count >= 80
            * Example: graphite should touch E2 but not fully cover it
          
          - "none":
            * target = 0.02 × min(obj_a_pixels, obj_b_pixels)
            * completion = max(1.0 - count / (target + 1), 0)
            * satisfied if count <= target (max 2% overlap allowed)
            * Example: WTe2 should NOT touch E1, E2, E3
          
          - "max":
            * lower = 0.70 × smaller, upper = 0.90 × smaller
            * optimal = 0.80 × smaller (center point)
            * completion = 1.0 - abs(count - optimal) / (upper - lower)
            * satisfied if lower <= count <= upper
            * Example: graphite should cover 70-90% of WTe2
          
          - "min":
            * lower = 0.10 × smaller, upper = 0.30 × smaller
            * optimal = 0.20 × smaller
            * Same calculation as "max"
            * satisfied if lower <= count <= upper
    
    2. SATISFIED CONSTRAINT COUNT
       - Each fully satisfied constraint adds +1 to satisfied_count
       - This count is weighted by 2 in the objective function
       - Encourages meeting binary thresholds, not just partial completion
    
    3. DISTANCE PENALTIES (Currently DISABLED)
       - Originally included penalties for materials too close to wrong electrodes
       - Now relies purely on pixel-level constraint satisfaction
       - Can be re-enabled by uncommenting lines in score_configuration()
    
    TOTAL SCORE CALCULATION:
    ------------------------
    total_score = Σ(completion_i) for all constraints
    objective = -(total_score + satisfied_count × 2)
    
    Higher total_score = better configuration
    Objective is negated because Differential Evolution minimizes
    
    BINARY MAP ENCODING:
    ====================
    To efficiently check vertical stacking relationships:
    - Each layer gets a unique bit position (0 to N-1)
    - At each pixel, encode which layers are present using bitwise OR
    - Example: If WTe2 (bit 0) and hBN (bit 1) both present:
      binary_map[y, x] = (1 << 0) | (1 << 1) = 0b11 = 3
    - To check "contact" between hBN (bit 2) and WTe2 (bit 0):
      * Both present: (binary_map >> 2) & 1 AND (binary_map >> 0) & 1
      * No layers between: Check bits 1 are all zero
    
    ELECTRODE HIERARCHY:
    ====================
    - Electrodes (contact_electrode, E_others, E1, E2, E3) are at SUBSTRATE LEVEL
    - Materials (WTe2, hBN, graphite) stack VERTICALLY on top
    - Material-electrode relations: simple overlap (no "in-between" logic)
    - Material-material relations: full vertical stack logic applies
    
    Args:
        masks: Dictionary of material and electrode masks (binary numpy arrays)
        materials: List of material names to be optimized (e.g., ["WTe2", "hBN", "graphite"])
        electrodes: List of electrode names (e.g., ["E1", "E2", "E3", "contact_electrode", "E_others"])
        output_rules: Constraint rules dictionary defining desired geometric relationships
                     Format: {material: {target: [relation, degree]}}
                     Example: {"WTe2": {"contact_electrode": ["contact", "complete"]}}
        downsample_size: Target size for mask normalization (height, width)
        max_iter: Maximum optimization iterations for Differential Evolution
        debug: If True, saves visualization images at each phase
    
    Returns:
        optimized_masks: Dictionary of optimized material masks (materials moved, electrodes unchanged)
        transformations: Transformation records for each material (rotation, translation)
        results: Dictionary containing:
                - 'total_score': Final completion score (higher is better)
                - 'satisfied_count': Number of fully satisfied constraints
                - 'violations': List of unsatisfied constraints with details
                - 'transformations': Same as second return value
    
    Example Usage:
        output_rules = {
            "WTe2": {
                "contact_electrode": ["contact", "complete"],  # Must fully cover contact region
                "E1": ["contact", "none"],                     # Must NOT touch E1
                "E2": ["contact", "none"],                     # Must NOT touch E2
                "E3": ["contact", "none"]                      # Must NOT touch E3
            },
            "hBN": {
                "WTe2": ["contact", "complete"],               # Must fully cover WTe2 (insulator)
                "contact_electrode": ["doesn't matter", "none"]
            },
            "graphite": {
                "WTe2": ["overlap", "max"],                    # Should cover 70-90% of WTe2
                "E2": ["contact", "not complete"]              # Should touch E2 (measurement electrode)
            }
        }
        
        optimized_masks, transformations, results = optimize_material_design(
            masks, materials, electrodes, output_rules, debug=True
        )
        
        print(f"Final score: {results['total_score']:.2f}")
        print(f"Satisfied: {results['satisfied_count']}/{len(output_rules)} constraints")
    """
    # Step 1: Downsample
    print("=== Step 1: Downsampling masks ===")
    masks = down_sample(masks, downsample_size)
    
    # Step 2: Initial alignment
    print("\n=== Step 2: Initial alignment ===")
    masks, transformations = initial_align(masks, materials, electrodes, output_rules, debug, output_dir)
    
    # Evaluate initial alignment score
    print("\n=== Initial Alignment Evaluation ===")
    layer_names = list(masks.keys())
    binary_map, _ = build_binary_map(masks)
    mid_pt = find_design_reference_midpoint(masks, materials, electrodes)
    initial_score, initial_satisfied, initial_violations = score_configuration(
        binary_map, layer_names, output_rules, masks, materials, electrodes, mid_pt
    )
    initial_objective = initial_score + initial_satisfied * 2
    print(f"Initial completion score: {initial_score:.4f}")
    print(f"Initial satisfied count: {initial_satisfied}")
    print(f"Initial objective value: {initial_objective:.4f}")
    print(f"Initial violations: {len(initial_violations)}")
    
    # Print detailed violation information
    if len(initial_violations) > 0:
        print("\n--- Detailed Initial Violations ---")
        for i, v in enumerate(initial_violations, 1):
            rule_name = v['rule']
            relation = v['relation']
            degree = v['degree']
            count = v.get('count', 0)
            completion = v.get('completion', 0)
            
            # Parse rule name
            parts = rule_name.split('->')
            if len(parts) == 2:
                obj_a, obj_b = parts[0], parts[1]
                
                # Describe the violation
                if relation == "contact" and degree == "none":
                    desc = f"Should NOT touch, but has {count} pixels of contact ({completion*100:.1f}%)"
                elif relation == "contact" and degree == "complete":
                    desc = f"Should FULLY cover, but only {completion*100:.1f}% covered (needs ≥70%)"
                elif relation == "contact" and degree == "not complete":
                    desc = f"Should PARTIALLY touch, but only has {count} pixels of contact (needs ≥40)"
                elif relation == "overlap" and degree == "max":
                    desc = f"Should have 70-90% overlap, but actual is {completion*100:.1f}%"
                elif relation == "overlap" and degree == "min":
                    desc = f"Should have 10-30% overlap, but actual is {completion*100:.1f}%"
                else:
                    desc = f"{relation} {degree}: {count} pixels ({completion*100:.1f}%)"
                
                print(f"  {i}. {obj_a} -> {obj_b}")
                print(f"     {desc}")
    else:
        print("\n✓ No violations in initial alignment!")
    
    # Visualize initial violations
    if debug and len(initial_violations) > 0:
        print("\n=== Generating Initial Violations Visualization ===")
        visualize_violations(masks, initial_violations, materials, electrodes, 
                           os.path.join(output_dir, "initial_violations_visualization.png"))
        print(f"Saved: {os.path.join(output_dir, 'initial_violations_visualization.png')}")
    
    # Step 3: Optimization
    print("\n=== Step 3: Differential Evolution optimization ===")
    optimized_masks, total_score, satisfied_count, violations, transformations = optimize_with_de(
        masks, materials, electrodes, output_rules, transformations, max_iter
    )
    
    # Print detailed final violation information
    print(f"\nFinal violations: {len(violations)}")
    if len(violations) > 0:
        print("\n--- Detailed Final Violations ---")
        for i, v in enumerate(violations, 1):
            rule_name = v['rule']
            relation = v['relation']
            degree = v['degree']
            count = v.get('count', 0)
            completion = v.get('completion', 0)
            
            # Parse rule name
            parts = rule_name.split('->')
            if len(parts) == 2:
                obj_a, obj_b = parts[0], parts[1]
                
                # Describe the violation
                if relation == "contact" and degree == "none":
                    desc = f"Should NOT touch, but has {count} pixels of contact ({completion*100:.1f}%)"
                elif relation == "contact" and degree == "complete":
                    desc = f"Should FULLY cover, but only {completion*100:.1f}% covered (needs ≥70%)"
                elif relation == "contact" and degree == "not complete":
                    desc = f"Should PARTIALLY touch, but only has {count} pixels of contact (needs ≥40)"
                elif relation == "overlap" and degree == "max":
                    desc = f"Should have 70-90% overlap, but actual is {completion*100:.1f}%"
                elif relation == "overlap" and degree == "min":
                    desc = f"Should have 10-30% overlap, but actual is {completion*100:.1f}%"
                else:
                    desc = f"{relation} {degree}: {count} pixels ({completion*100:.1f}%)"
                
                print(f"  {i}. {obj_a} -> {obj_b}")
                print(f"     {desc}")
    else:
        print("\n✓ All constraints satisfied!")
    
    # Visualize final violations
    if debug and len(violations) > 0:
        print("\n=== Generating Final Violations Visualization ===")
        visualize_violations(optimized_masks, violations, materials, electrodes, 
                           os.path.join(output_dir, "final_violations_visualization.png"))
        print(f"Saved: {os.path.join(output_dir, 'final_violations_visualization.png')}")
    
    # Prepare results
    results = {
        'total_score': total_score,
        'satisfied_count': satisfied_count,
        'violations': violations,
        'transformations': transformations,
        'initial_score': initial_score,
        'initial_satisfied': initial_satisfied,
        'initial_objective': initial_objective,
        'initial_violations': initial_violations,
        'improvement': (total_score + satisfied_count * 2) - initial_objective
    }
    
    # Print improvement summary
    print("\n" + "="*70)
    print("=== OPTIMIZATION SUMMARY ===")
    print("="*70)
    print(f"Initial state:")
    print(f"  Objective: {initial_objective:.4f} (score: {initial_score:.4f}, satisfied: {initial_satisfied})")
    print(f"\nFinal state:")
    final_objective = total_score + satisfied_count * 2
    print(f"  Objective: {final_objective:.4f} (score: {total_score:.4f}, satisfied: {satisfied_count})")
    print(f"\nImprovement: {results['improvement']:.4f} ({results['improvement']/initial_objective*100:.1f}%)")
    print("="*70)
    
    return optimized_masks, transformations, results



def generate_relation_matrix(
    stack_structure,
    num_electrodes,
    task,
    requirements,
    model_id: str | None = DEFAULT_MODEL_ID,
    include_electrodes: bool = True,
):
    resolved_model_id = resolve_model_id(model_id or DEFAULT_MODEL_ID)
    api_key = get_api_key_for_model(resolved_model_id)
    api_base = get_api_base_for_model(resolved_model_id)
    if not api_key:
        raise RuntimeError("QUMUS_API_KEY not set.")

    if include_electrodes:
        prompt = f"""
You are a quantum materials design assistant.
Your task is to generate a **geometric relation matrix** for a stack of 2D materials and electrodes,
ensuring the result respects both **device physics** and **layer order constraints**.

### Stack Hierarchy
(bottom → top): {stack_structure}

### Electrode Types (ALWAYS PRESENT)
The system ALWAYS includes these special electrodes:
1. **contact_electrode**: A rectangular region covering inner contact electrodes (for material contact)
2. **E_others**: Combined inner electrodes (usually should not contact materials)
3. **E1, E2, E3, ...**: Individual outer electrodes (numbered, count = {num_electrodes})

### Relation Semantics
- "contact" = direct physical or electrical contact
- "overlap" = indirect spatial coverage (only for non-adjacent layers)
- "doesn't matter" = geometrically irrelevant (but still need to specify degree)
- Degree options: ["not complete", "complete", "none", "max", "min"]

### Critical Rules for Electrode Relations

**Bottom Material (first in stack):**
- MUST have ["contact", "complete"] with contact_electrode
- MUST have ["doesn't matter", "none"] with E_others
- MUST have ["contact", "none"] with all individual electrodes (E1, E2, E3, ...)

**Middle Material (e.g., hBN - insulator):**
- MUST have ["contact", "complete"] with material below it
- MUST have ["doesn't matter", "none"] with ALL electrodes (contact_electrode, E_others, E1, E2, E3, ...)

**Top Material (last in stack):**
- Should have ["overlap", "max"] with bottom material (if not adjacent)
- Should have ["doesn't matter", "none"] with hBN (if hBN is middle layer)
- Should have ["doesn't matter", "none"] with contact_electrode
- Should have ["contact", "none"] with E_others
- MUST contact ONLY ONE individual electrode with ["contact", "not complete"],
- All other individual electrodes should be ["contact", "none"]

### Material-to-Material Rules
- hBN/insulator should fully cover the material below: ["contact", "complete"]
- Top material should maximize overlap with bottom material: ["overlap", "max"]
- Only non-adjacent layers can have "overlap" relation
- Adjacent layers use "contact"

### Output Format
Return ONLY valid JSON (no explanation, no markdown).

Example for a 3-layer stack (WTe2, hBN, graphite) with 3 outer electrodes:
{{
  "WTe2": {{
    "contact_electrode": ["contact", "complete"],
    "E_others": ["doesn't matter", "none"],
    "E1": ["contact", "none"],
    "E2": ["contact", "none"],
    "E3": ["contact", "none"]
  }},
  "hBN": {{
    "WTe2": ["contact", "complete"],
    "contact_electrode": ["doesn't matter", "none"],
    "E_others": ["doesn't matter", "none"],
    "E1": ["doesn't matter", "none"],
    "E2": ["doesn't matter", "none"],
    "E3": ["doesn't matter", "none"]
  }},
  "graphite": {{
    "WTe2": ["overlap", "max"],
    "hBN": ["doesn't matter", "none"],
    "contact_electrode": ["doesn't matter", "none"],
    "E_others": ["doesn't matter", "none"],
    "E1": ["contact", "none"],
    "E2": ["contact", "not complete"],
    "E3": ["contact", "none"]
  }}
}}

### Input
Stack (bottom → top): {stack_structure}
Number of Individual Outer Electrodes: {num_electrodes}
Task: {task}
Requirements: {requirements}

Generate the JSON relation matrix following the exact format above.
REMEMBER: Always include contact_electrode, E_others, and E1 through E{num_electrodes} in the output.
"""
    else:
        prompt = f"""
You are a quantum materials design assistant.
Your task is to generate a **geometric relation matrix** for a stack of 2D materials with **no electrodes present**,
ensuring the result respects both **device physics** and **layer order constraints**.

### Stack Hierarchy
(bottom → top): {stack_structure}

### Electrode-Free Design Mode
- There are NO electrodes in this design.
- Do NOT include contact_electrode, E_others, E1, E2, or any other electrode key.
- Keep all material-to-material layout requirements consistent with the electrode-backed mode.

### Relation Semantics
- "contact" = direct physical contact between adjacent material layers
- "overlap" = indirect spatial coverage between non-adjacent layers when another layer exists between them
- "doesn't matter" = geometrically irrelevant (but still need to specify degree)
- Degree options: ["not complete", "complete", "none", "max", "min"]

### Material-to-Material Rules
- Adjacent layers should use "contact".
- Non-adjacent layers should use "overlap".
- hBN/insulator layers should fully cover the material below: ["contact", "complete"].
- Top functional/gate materials should maximize overlap with the bottom functional material when non-adjacent: ["overlap", "max"].
- Do not create any electrode-related constraints.

### Output Format
Return ONLY valid JSON (no explanation, no markdown).

Example for a 3-layer stack (graphene, hBN, graphite) with no electrodes:
{{
  "graphene": {{}},
  "hBN": {{
    "graphene": ["contact", "complete"]
  }},
  "graphite": {{
    "graphene": ["overlap", "max"],
    "hBN": ["doesn't matter", "none"]
  }}
}}

### Input
Stack (bottom → top): {stack_structure}
Task: {task}
Requirements: {requirements}

Generate the JSON relation matrix following the exact format above.
REMEMBER: Do not include any electrode keys or electrode constraints.
"""

    kwargs: dict[str, object] = {
        "model": resolved_model_id,
        "api_key": api_key,
        "messages": [
            {"role": "system", "content": "You are an expert in 2D material device design. Return ONLY valid JSON, no explanation."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    if api_base:
        kwargs["api_base"] = api_base

    response = litellm.completion(**kwargs)
    raw_output = response.choices[0].message.content
    # print("Raw output:", raw_output)
    clean_text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", raw_output.strip(), flags=re.MULTILINE)
    clean_text = clean_text.strip()

    try:
        data = json.loads(clean_text)
    except json.JSONDecodeError:
        print("JSON decode failed — trying fallback regex extraction...")
        match = re.search(r"\{.*\}", clean_text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
        else:
            raise ValueError("Could not find JSON in model output.")
    return data
