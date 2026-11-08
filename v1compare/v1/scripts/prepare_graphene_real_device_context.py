#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = PROJECT_ROOT / "real_device"
DEFAULT_BASE_CONTEXT = PROJECT_ROOT / "approved_context_graphene_qh_device.json"
DEFAULT_INITIAL_DIR = PROJECT_ROOT / "agents" / "design_agent" / "output" / "graphene_edge_contact_run" / "initial_design"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "agents" / "design_agent" / "output" / "graphene_real_device_20x_run"
PIXEL_SIZE_20X_UM_PER_PX = 0.1830
MAIN_FLAKE_MERGE_GAP_PX = 8
MAIN_FLAKE_MERGE_MIN_AREA_PX = 250
CENTER_TIE_TOLERANCE_PX = 50.0


ROLE_SOURCES = {
    "channel": {
        "material": "monolayer graphene",
        "filename_pattern": re.compile(r"graphene", re.I),
        "legacy_mask_name": "mask_graphene.png",
        "legacy_debug_name": "aligned_graphene_debug.png",
        "aliases": ["graphene"],
        "stack_order": 3,
    },
    "top_encapsulation": {
        "material": "hBN",
        "filename_pattern": re.compile(r"top[-_ ]?hbn", re.I),
        "legacy_mask_name": "mask_hBN_top.png",
        "legacy_debug_name": "aligned_top_hBN_debug.png",
        "aliases": ["top_hbn"],
        "stack_order": 4,
    },
    "bottom_encapsulation": {
        "material": "hBN",
        "filename_pattern": re.compile(r"(^|/)hbn[-_ ]?20|bottom[-_ ]?hbn|(?<!top[-_ ])hbn", re.I),
        "legacy_mask_name": "mask_hBN.png",
        "legacy_debug_name": "aligned_hBN_debug.png",
        "aliases": ["bottom_hbn"],
        "stack_order": 2,
    },
    "bottom_gate": {
        "material": "graphite",
        "filename_pattern": re.compile(r"graphite", re.I),
        "legacy_mask_name": "mask_graphite.png",
        "legacy_debug_name": "aligned_graphite_debug.png",
        "aliases": ["graphite_gate", "graphite"],
        "stack_order": 1,
    },
}


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _source_for_role(input_dir: Path, role: str) -> Path:
    spec = ROLE_SOURCES[role]
    matches = [p for p in input_dir.iterdir() if p.is_file() and spec["filename_pattern"].search(p.name)]
    if role == "bottom_encapsulation":
        matches = [p for p in matches if "top" not in p.name.lower()]
    if not matches:
        raise FileNotFoundError(f"No segmented image found for role {role!r} in {input_dir}")
    return sorted(matches, key=lambda p: p.name.lower())[0]


def _min_distance_to_mask(component_mask: np.ndarray, reference_mask: np.ndarray) -> float:
    if not component_mask.any() or not reference_mask.any():
        return float("inf")
    distance = cv2.distanceTransform((reference_mask == 0).astype(np.uint8), cv2.DIST_L2, 5)
    return float(distance[component_mask > 0].min())


def _merge_adjacent_main_flake_components(
    labels: np.ndarray,
    components: list[dict[str, Any]],
    selected: dict[str, Any],
) -> tuple[np.ndarray, dict[str, Any]]:
    """Merge segmentation fragments that are separated from the main flake only by a tiny gap.

    The segmented real-device images can split one physical flake into multiple color regions
    separated by a black boundary line. For center-flake selection, keep the primary component
    selected by area/centrality, then merge only nearby fragments. This avoids pulling in
    unrelated flakes elsewhere in the field of view.
    """

    merged_labels = {int(selected["label"])}
    merged_mask = (labels == int(selected["label"])).astype(np.uint8)
    merge_events: list[dict[str, Any]] = []

    changed = True
    while changed:
        changed = False
        for component in components:
            label = int(component["label"])
            if label in merged_labels:
                continue
            area = int(component["area_px"])
            if area < MAIN_FLAKE_MERGE_MIN_AREA_PX:
                continue
            component_mask = (labels == label).astype(np.uint8)
            min_distance = _min_distance_to_mask(component_mask, merged_mask)
            if min_distance > MAIN_FLAKE_MERGE_GAP_PX:
                continue
            merged_labels.add(label)
            merged_mask = np.logical_or(merged_mask > 0, component_mask > 0).astype(np.uint8)
            merge_events.append(
                {
                    "label": label,
                    "area_px": area,
                    "min_distance_to_main_px": round(min_distance, 3),
                    "bbox_px": component["bbox_px"],
                    "centroid_px": component["centroid_px"],
                }
            )
            changed = True

    selected = dict(selected)
    selected["primary_label"] = int(selected["label"])
    selected["label"] = sorted(merged_labels)
    selected["merged_component_labels"] = sorted(merged_labels)
    selected["merged_by_adjacency"] = merge_events
    selected["merged_by_adjacency_count"] = len(merge_events)
    selected["merge_rule"] = (
        f"After selecting the center main flake, merge any foreground component with area >= "
        f"{MAIN_FLAKE_MERGE_MIN_AREA_PX} px whose nearest pixel is <= {MAIN_FLAKE_MERGE_GAP_PX} px "
        "from the current merged main flake. This treats black segmentation seams between adjacent "
        "colored regions as one physical flake while rejecting distant flakes."
    )
    selected["rejected_component_count"] = max(0, len(components) - len(merged_labels))

    ys, xs = np.where(merged_mask > 0)
    m = cv2.moments(merged_mask, binaryImage=True)
    if len(xs) and m["m00"]:
        selected["area_px"] = int(merged_mask.sum())
        selected["bbox_px"] = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        selected["centroid_px"] = [float(m["m10"] / m["m00"]), float(m["m01"] / m["m00"])]

    return merged_mask.astype(np.uint8) * 255, selected


def _select_main_component(mask01: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    num, labels, stats, centroids = cv2.connectedComponentsWithStats(mask01.astype(np.uint8), 8)
    h, w = mask01.shape
    image_center = (w / 2.0, h / 2.0)
    components: list[dict[str, Any]] = []
    max_area = 0
    for idx in range(1, num):
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < 25:
            continue
        max_area = max(max_area, area)
        x = int(stats[idx, cv2.CC_STAT_LEFT])
        y = int(stats[idx, cv2.CC_STAT_TOP])
        bw = int(stats[idx, cv2.CC_STAT_WIDTH])
        bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
        cx, cy = (float(centroids[idx][0]), float(centroids[idx][1]))
        components.append(
            {
                "label": idx,
                "area_px": area,
                "bbox_px": [x, y, x + bw - 1, y + bh - 1],
                "centroid_px": [cx, cy],
                "distance_to_image_center_px": math.hypot(cx - image_center[0], cy - image_center[1]),
            }
        )
    if not components:
        raise RuntimeError("No foreground connected component found")

    # Position is authoritative. Area is consulted only for components whose
    # centroid distances are extremely close to the nearest one. Do not use an
    # area floor relative to the largest region: that can discard the intended
    # small center flake before centrality is evaluated.
    nearest_distance = min(c["distance_to_image_center_px"] for c in components)
    center_tied = [
        c
        for c in components
        if c["distance_to_image_center_px"] <= nearest_distance + CENTER_TIE_TOLERANCE_PX
    ]
    selected = max(center_tied, key=lambda c: c["area_px"])
    selected["selection_rule"] = (
        f"nearest centroid to image center first; area is the tie-breaker only within "
        f"{CENTER_TIE_TOLERANCE_PX:g} px of the nearest center distance"
    )
    selected["nearest_center_distance_px"] = nearest_distance
    selected["center_tie_tolerance_px"] = CENTER_TIE_TOLERANCE_PX
    selected["image_center_px"] = [image_center[0], image_center[1]]
    selected["component_count"] = len(components)
    return _merge_adjacent_main_flake_components(labels, components, selected)


def extract_center_main_component(source_path: Path | str) -> tuple[np.ndarray, dict[str, Any], dict[str, Any]]:
    """Extract one center-priority component from an existing segmented mask."""
    rgb = np.asarray(Image.open(source_path).convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask01 = (gray > 10).astype(np.uint8)
    kernel = np.ones((3, 3), np.uint8)
    mask01 = cv2.morphologyEx(mask01, cv2.MORPH_OPEN, kernel)
    selected_mask, selected_component = _select_main_component(mask01)
    return selected_mask, selected_component, _mask_metrics(selected_mask, PIXEL_SIZE_20X_UM_PER_PX)


def _mask_metrics(mask: np.ndarray, pixel_size: float) -> dict[str, Any]:
    mask01 = (mask > 0).astype(np.uint8)
    ys, xs = np.where(mask01 > 0)
    if len(xs) == 0:
        raise RuntimeError("Selected mask is empty")
    m = cv2.moments(mask01, binaryImage=True)
    cx = float(m["m10"] / m["m00"])
    cy = float(m["m01"] / m["m00"])
    h = int(mask01.shape[0])
    contours, _ = cv2.findContours(mask01, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contour = max(contours, key=cv2.contourArea)
    epsilon = max(1.0, 0.0025 * cv2.arcLength(contour, True))
    approx = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
    contour_um = []
    for x_px, y_px in approx:
        contour_um.append([
            round((float(x_px) - cx) * pixel_size, 4),
            round((cy - float(y_px)) * pixel_size, 4),
        ])
    return {
        "area_px": int(mask01.sum()),
        "area_um2": round(float(mask01.sum()) * pixel_size * pixel_size, 4),
        "bbox_px": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
        "bbox_um_size": [
            round(float(xs.max() - xs.min() + 1) * pixel_size, 4),
            round(float(ys.max() - ys.min() + 1) * pixel_size, 4),
        ],
        "centroid_px": [round(cx, 4), round(cy, 4)],
        "centroid_um_from_image_bottom_left": [
            round(cx * pixel_size, 4),
            round((h - cy) * pixel_size, 4),
        ],
        "local_contour_um_centered": contour_um,
        "contour_point_count": len(contour_um),
    }


def _debug_overlay(rgb: np.ndarray, selected_mask: np.ndarray, selected: dict[str, Any]) -> np.ndarray:
    out = rgb.copy()
    out[selected_mask > 0] = (0.35 * out[selected_mask > 0] + np.array([255, 80, 20]) * 0.65).astype(np.uint8)
    contours, _ = cv2.findContours((selected_mask > 0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(out, contours, -1, (255, 255, 255), 3)
    cx, cy = selected["centroid_px"]
    cv2.drawMarker(out, (int(round(cx)), int(round(cy))), (0, 255, 255), markerType=cv2.MARKER_CROSS, markerSize=40, thickness=3)
    return out


def prepare_context(input_dir: Path, output_dir: Path, base_context_path: Path, initial_dir: Path) -> dict[str, Any]:
    masks_dir = output_dir / "prepared_masks"
    legacy_dir = output_dir / "lihan_graphene_compat"
    masks_dir.mkdir(parents=True, exist_ok=True)
    legacy_dir.mkdir(parents=True, exist_ok=True)

    base_context = json.loads(base_context_path.read_text(encoding="utf-8"))
    candidate_masks: dict[str, list[str]] = {}
    candidate_images: dict[str, list[str]] = {}
    candidate_flakes: dict[str, list[dict[str, Any]]] = {}
    selected_flakes: dict[str, dict[str, Any]] = {}
    legacy_transformations: dict[str, dict[str, Any]] = {}

    for role, spec in ROLE_SOURCES.items():
        src = _source_for_role(input_dir, role)
        rgb = np.asarray(Image.open(src).convert("RGB"))
        selected_mask, selected_component, metrics = extract_center_main_component(src)

        mask_path = masks_dir / f"{role}_main_mask.png"
        debug_path = masks_dir / f"{role}_main_debug.png"
        legacy_mask_path = legacy_dir / str(spec["legacy_mask_name"])
        legacy_debug_path = legacy_dir / str(spec["legacy_debug_name"])
        Image.fromarray(selected_mask).save(mask_path)
        Image.fromarray(selected_mask).save(legacy_mask_path)
        overlay = _debug_overlay(rgb, selected_mask, selected_component)
        Image.fromarray(overlay).save(debug_path)
        Image.fromarray(overlay).save(legacy_debug_path)

        for alias in [role, *spec["aliases"]]:
            candidate_masks[alias] = [str(mask_path)]
            candidate_images[alias] = [str(src)]

        flake = {
            "flake_id": f"real_device_20x_{role}_main",
            "role": role,
            "material": spec["material"],
            "mask_path": str(mask_path),
            "image_path": str(src),
            "preferred_image_path": str(src),
            "source_magnification": "20x",
            "pixel_size_um_per_px": PIXEL_SIZE_20X_UM_PER_PX,
            "rotation_allowed": False,
            "rotation_deg": 0,
            "translation_only": True,
            "selection": selected_component,
            "metrics": metrics,
            "source_center": metrics["centroid_um_from_image_bottom_left"],
        }
        for alias in [role, *spec["aliases"]]:
            candidate_flakes[alias] = [flake]
        selected_flakes[role] = flake
        legacy_transformations[role] = {
            "original_centroid": metrics["centroid_px"],
            "total_rotation": 0.0,
            "total_translation": [0.0, 0.0],
            "rotation_allowed": False,
            "source_mask": str(mask_path),
        }

    ideal_result_path = initial_dir / "ideal_device_result.json"
    ideal_gds_path = initial_dir / "ideal_device.gds"
    ideal_screenshot_path = initial_dir / "ideal_device_screenshot.png"

    base_context["design_stage"] = "real_flake_adaptation"
    base_context["pixel_size_um_per_px"] = PIXEL_SIZE_20X_UM_PER_PX
    base_context["candidate_mask_paths_by_role"] = candidate_masks
    base_context["candidate_image_paths_by_role"] = candidate_images
    base_context["candidate_flakes_by_role"] = candidate_flakes
    base_context["selected_real_device_flakes_by_role"] = selected_flakes
    base_context["real_device_input"] = {
        "source_dir": str(input_dir),
        "source_magnification": "20x",
        "pixel_size_um_per_px": PIXEL_SIZE_20X_UM_PER_PX,
        "calibration_source": "docs/OPTICAL_CALIBRATION.md",
        "calibration_note": (
            "Repo-local docs/OPTICAL_CALIBRATION.md gives 20x = 0.1830 um/px for the current microscope/camera "
            "stack. KlayoutClaw/docs/tools.md has a generic 20x example of 0.25 um/px; that was not used."
        ),
        "mask_semantics": "Input JPG files are already segmented masks. Foreground pixels are gray > 10 after RGB-to-gray conversion.",
        "selection_policy": "Use the center main flake from each segmented image; no rotation is allowed, only translation.",
        "image_size_px": [3840, 2160],
    }
    base_context["real_flake_constraints"] = {
        "rotation_allowed": False,
        "rotation_deg": 0,
        "translation_only": True,
        "can_modify_routing_or_local_design": True,
        "must_use_each_center_main_flake": True,
        "roles_required": list(ROLE_SOURCES.keys()),
    }
    base_context["approved_ideal_design"] = {
        "source_run": str(initial_dir),
        "ideal_device_result_json": str(ideal_result_path),
        "ideal_gds": str(ideal_gds_path),
        "ideal_screenshot": str(ideal_screenshot_path),
    }
    base_context["legacy_output_compatibility"] = {
        "reference": "git branch lihan/graphene:agents/design_agent/output",
        "output_dir": str(legacy_dir),
        "required_extra_files": [
            "design_parameters.json",
            "design_result.png",
            "initial_align_all_masks_debug.png",
            "final_violations_visualization.png",
            "initial_violations_visualization.png",
            "mask_graphene.png",
            "mask_hBN.png",
            "mask_graphite.png",
            "aligned_graphene_debug.png",
            "aligned_hBN_debug.png",
            "aligned_graphite_debug.png",
        ],
        "note": (
            "In addition to KLayout outputs, write lihan/graphene-style mask/debug/result artifacts here. "
            "Use top_encapsulation and bottom_encapsulation separately in JSON; keep mask_hBN.png as the bottom hBN "
            "compatibility alias."
        ),
    }
    base_context["coordinate_frame_notes"] = (
        "All four real_device segmented JPG masks are 3840x2160 20x captures. Convert mask pixels to microns with "
        "pixel_size_um_per_px=0.1830 from docs/OPTICAL_CALIBRATION.md. For KLayout drawing, use local flake "
        "coordinates centered at the selected main-component centroid, invert image y so KLayout y points up, and "
        "translate each flake to the target stack position. Do not rotate masks or output nonzero rotations."
    )

    output_context_path = output_dir / "approved_context_real_device.json"
    _write_json(output_context_path, base_context)
    _write_json(output_dir / "real_device_flake_manifest.json", selected_flakes)
    _write_json(
        legacy_dir / "design_parameters.json",
        {
            "task": "Graphene quantum Hall real-device adaptation",
            "materials": ["graphene", "top_hBN", "bottom_hBN", "graphite"],
            "pixel_size_um_per_px": PIXEL_SIZE_20X_UM_PER_PX,
            "calibration_source": "docs/OPTICAL_CALIBRATION.md",
            "transformations": legacy_transformations,
            "input_masks": {role: flake["mask_path"] for role, flake in selected_flakes.items()},
            "results": {
                "status": "prepared_for_design_agent",
                "rotation_allowed": False,
                "translation_only": True,
                "selected_center_main_flake_count": len(selected_flakes),
            },
        },
    )

    # Placeholder compatibility canvases that make the legacy output set complete before Claude/KLayout overwrites
    # or augments them with final design images.
    h, w = 2160, 3840
    blank = Image.fromarray(np.zeros((h, w, 3), dtype=np.uint8))
    for name in ["design_result.png", "initial_align_all_masks_debug.png", "initial_violations_visualization.png", "final_violations_visualization.png"]:
        blank.save(legacy_dir / name)

    return {
        "status": "ok",
        "approved_context_real_device": str(output_context_path),
        "manifest": str(output_dir / "real_device_flake_manifest.json"),
        "legacy_output_dir": str(legacy_dir),
        "pixel_size_um_per_px": PIXEL_SIZE_20X_UM_PER_PX,
        "selected_roles": list(selected_flakes.keys()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare graphene real-device 20x flake masks for design_agent.")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--base-context", default=str(DEFAULT_BASE_CONTEXT))
    parser.add_argument("--initial-dir", default=str(DEFAULT_INITIAL_DIR))
    args = parser.parse_args()
    result = prepare_context(
        input_dir=Path(args.input_dir).resolve(),
        output_dir=Path(args.output_dir).resolve(),
        base_context_path=Path(args.base_context).resolve(),
        initial_dir=Path(args.initial_dir).resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
