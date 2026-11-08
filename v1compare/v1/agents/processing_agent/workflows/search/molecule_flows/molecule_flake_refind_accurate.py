from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from typing import Optional

import cv2
import numpy as np

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.search.atom_flows import (
    AtomCameraAutofocusOff,
    AtomCameraAutofocusOnce,
    AtomCameraGetFocusPosition,
    AtomCameraSetFocusPosition,
    AtomCameraStart,
    AtomCameraStop,
    AtomCameraWaitFocusAndTakePhoto,
    AtomSampleStageMoveRelative,
)
from agents.processing_agent.workflows.stacking.molecule_flows.utils.stacking_run_dirs import (
    get_stacking_run_dirs,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool
from scripts.prepare_graphene_real_device_context import extract_center_main_component


class MoleculeFlakeRefindAccurate(BaseWorkflow):
    name = "molecule_flake_refind_accurate"
    description = (
        "Accurately refind a flake from a reference image, segment the newly captured "
        "actual 20X image with the validated material-specific profile, and align the sample stage."
    )
    inputs = {
        "image_path": {
            "type": "string",
            "description": "Reference flake image path, such as flake A/B or bottom electrode image.",
        },
        "alignment_dir": {
            "type": "string",
            "description": "Optional directory to save alignment artifacts.",
            "nullable": True,
        },
        "save_stem": {
            "type": "string",
            "description": "Optional output stem for saved alignment images.",
            "nullable": True,
        },
        "shift_x": {
            "type": "number",
            "description": "Optional design shift in calibrated 20X image pixels along image X.",
            "nullable": True,
            "default": 0,
        },
        "shift_y": {
            "type": "number",
            "description": "Optional design shift in calibrated 20X image pixels along image Y.",
            "nullable": True,
            "default": 0,
        },
        "init_angle_deg": {
            "type": "number",
            "description": "Optional initial angle guess in degrees for rotated template matching.",
            "nullable": True,
            "default": 0,
        },
        "mask_path": {
            "type": "string",
            "description": "Optional segmented 20X flake mask path. If omitted, Qumus resolves it from image_path when possible.",
            "nullable": True,
            "default": None,
        },
    }
    output_type = "string"

    PLAN_STEPS = [
        "Prepare Alignment Capture",
        "Capture Current Image",
        "Match Reference To Current Image",
        "Move Flake To Image Center",
        "Apply Design Shift",
    ]

    CENTER_PATCH_RATIO = 0.2
    MATCH_RATIO = 0.2
    INIT_ANGLE_DEG = 0.0
    SEARCH_RANGE_DEG = 15.0
    DOWNSAMPLE = 0.5
    USE_EDGE = True
    MIN_BLUR = 3.5
    PIXEL_SIZE_UM = 0.183
    STEPS_PER_UM = 12.8
    CENTER_TO_DESIGN_PAUSE_S = 5.0
    # This intentionally selects the segmenter's 100X parameter profile while
    # processing an actual 20X accurate-refind capture.  It must never be used
    # as the optical pixel scale; 20X motion remains calibrated at 0.183 um/px.
    CURRENT_IMAGE_SEGMENTER_PROFILE = 100
    SEGMENTATION_CONSENSUS_RUNS = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.cam_autofocus_off = AtomCameraAutofocusOff(*args, **kwargs)
        self.cam_autofocus_once = AtomCameraAutofocusOnce(*args, **kwargs)
        self.cam_get_focus_pos = AtomCameraGetFocusPosition(*args, **kwargs)
        self.cam_set_focus_pos = AtomCameraSetFocusPosition(*args, **kwargs)
        self.cam_wait_focus_photo = AtomCameraWaitFocusAndTakePhoto(*args, **kwargs)
        self.stage_move_relative = AtomSampleStageMoveRelative(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    @staticmethod
    def _load_image(path: str, label: str) -> np.ndarray:
        if not path:
            raise ValueError(f"{label} is required.")
        img = cv2.imread(path)
        if img is None:
            raise ValueError(f"Failed to read {label}: {path}")
        return img

    @staticmethod
    def _extract_center_patch(img: np.ndarray, ratio: float) -> np.ndarray:
        h, w = img.shape[:2]
        ph, pw = int(h * ratio), int(w * ratio)
        cy, cx = h // 2, w // 2
        return img[cy - ph // 2 : cy + ph // 2, cx - pw // 2 : cx + pw // 2]

    @staticmethod
    def _rotate_image(img: np.ndarray, angle_deg: float) -> np.ndarray:
        h, w = img.shape[:2]
        center = (w // 2, h // 2)
        matrix = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
        return cv2.warpAffine(
            img,
            matrix,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )

    @staticmethod
    def _preprocess(img: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return cv2.Canny(gray, 50, 150)

    @staticmethod
    def _norm_path(path: object) -> str:
        return os.path.normcase(os.path.abspath(str(path or ""))).replace("\\", "/")

    @classmethod
    def _same_path(cls, left: object, right: object) -> bool:
        left_norm = cls._norm_path(left)
        right_norm = cls._norm_path(right)
        return bool(left_norm and right_norm and (left_norm == right_norm))

    @classmethod
    def _resolve_mask_path_for_reference(cls, image_path: str) -> Optional[str]:
        """Resolve a 20X optical flake image to its segmented 20X mask, if known."""
        db_path = os.path.abspath(os.path.join("database", "material_base.db"))
        if not os.path.exists(db_path):
            return None
        try:
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT i20.path, f.mask_20x_path
                    FROM flake f
                    LEFT JOIN image i20 ON i20.id = f.image_20x_id
                    WHERE f.mask_20x_path IS NOT NULL
                    """
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return None

        for ref_path, mask_path in rows:
            if mask_path and cls._same_path(image_path, ref_path):
                return str(mask_path)
        return None

    @classmethod
    def _resolve_flake_metadata_for_reference(cls, image_path: str) -> dict:
        """Resolve database provenance needed for material-specific segmentation."""
        db_path = os.path.abspath(os.path.join("database", "material_base.db"))
        if not os.path.exists(db_path):
            return {}
        try:
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT f.id, f.type_label, f.thickness_label, i20.path, f.mask_20x_path
                    FROM flake f
                    LEFT JOIN image i20 ON i20.id = f.image_20x_id
                    WHERE i20.path IS NOT NULL
                    """
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return {}

        for flake_id, material_type, thickness_label, ref_path, mask_path in rows:
            if cls._same_path(image_path, ref_path):
                return {
                    "flake_id": int(flake_id),
                    "material_type": str(material_type or ""),
                    "thickness_label": str(thickness_label or ""),
                    "mask_20x_path": str(mask_path or ""),
                }
        return {}

    @staticmethod
    def _load_binary_mask(path: str, expected_shape: tuple[int, int]) -> Optional[np.ndarray]:
        if not path or not os.path.exists(path):
            return None
        try:
            # Use exactly the same center-priority, color-class-aware extraction
            # as real-device design.  The database mask may contain several
            # disconnected color-coded flakes, so grayscale thresholding plus
            # largest-component selection can select a different physical flake.
            mask, _selection, _metrics = extract_center_main_component(path)
        except (FileNotFoundError, ValueError):
            return None
        if mask.shape[:2] != expected_shape:
            mask = cv2.resize(
                mask,
                (expected_shape[1], expected_shape[0]),
                interpolation=cv2.INTER_NEAREST,
            )
        return np.where(mask > 0, 255, 0).astype(np.uint8)

    @staticmethod
    def _largest_component_bbox(mask: np.ndarray) -> Optional[tuple[int, int, int, int, float, float]]:
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if num_labels <= 1:
            return None
        component_idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x, y, w, h, area = stats[component_idx]
        if int(area) <= 0:
            return None
        cx, cy = centroids[component_idx]
        return int(x), int(y), int(w), int(h), float(cx), float(cy)

    @staticmethod
    def _rotate_gray(img: np.ndarray, angle_deg: float) -> np.ndarray:
        h, w = img.shape[:2]
        matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle_deg, 1.0)
        return cv2.warpAffine(
            img,
            matrix,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )

    @staticmethod
    def _rotate_mask(mask: np.ndarray, angle_deg: float) -> np.ndarray:
        h, w = mask.shape[:2]
        matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle_deg, 1.0)
        rotated = cv2.warpAffine(
            mask,
            matrix,
            (w, h),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        _, binary = cv2.threshold(rotated, 1, 255, cv2.THRESH_BINARY)
        return binary

    @staticmethod
    def _rotate_point_in_shape(
        point: tuple[float, float],
        shape: tuple[int, int],
        angle_deg: float,
    ) -> tuple[float, float]:
        h, w = shape[:2]
        matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle_deg, 1.0)
        x = matrix[0, 0] * point[0] + matrix[0, 1] * point[1] + matrix[0, 2]
        y = matrix[1, 0] * point[0] + matrix[1, 1] * point[1] + matrix[1, 2]
        return float(x), float(y)

    @staticmethod
    def _resize_gray_and_mask(
        image: np.ndarray,
        mask: np.ndarray,
        scale: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        if scale == 1.0:
            return image, mask
        resized_image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        resized_mask = cv2.resize(mask, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        _, resized_mask = cv2.threshold(resized_mask, 1, 255, cv2.THRESH_BINARY)
        return resized_image, resized_mask

    def _search_once(self, initial: np.ndarray, target: np.ndarray, angles_deg) -> dict:
        best_score = -1.0
        best_result: Optional[dict] = None

        for angle_deg in angles_deg:
            rotated_initial = self._rotate_image(initial, float(angle_deg))
            template = self._extract_center_patch(rotated_initial, ratio=self.CENTER_PATCH_RATIO)
            if template.size == 0:
                continue

            response = cv2.matchTemplate(target, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(response)
            if max_val > best_score:
                best_score = float(max_val)
                best_result = {
                    "angle": float(angle_deg),
                    "location": (int(max_loc[0]), int(max_loc[1])),
                    "score": float(max_val),
                }

        if best_result is None:
            raise RuntimeError("Template matching failed to produce a valid result.")
        return best_result

    def _find_best_masked_match(
        self,
        initial: np.ndarray,
        current: np.ndarray,
        reference_mask: np.ndarray,
    ) -> dict:
        component = self._largest_component_bbox(reference_mask)
        if component is None:
            raise RuntimeError("Reference mask does not contain a flake component.")

        x, y, w, h, mask_cx, mask_cy = component
        pad = max(32, int(round(max(w, h) * 0.15)))
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(initial.shape[1], x + w + pad)
        y1 = min(initial.shape[0], y + h + pad)
        if x1 <= x0 or y1 <= y0:
            raise RuntimeError("Reference mask crop is empty.")

        template_gray = cv2.cvtColor(initial[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        template_mask = reference_mask[y0:y1, x0:x1]
        current_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
        target = (
            cv2.resize(
                current_gray,
                None,
                fx=self.DOWNSAMPLE,
                fy=self.DOWNSAMPLE,
                interpolation=cv2.INTER_AREA,
            )
            if self.DOWNSAMPLE != 1.0
            else current_gray
        )

        centroid_in_crop = (mask_cx - x0, mask_cy - y0)

        def _search(angles_deg) -> dict:
            best_score = -1.0
            best_result: Optional[dict] = None
            for angle_deg in angles_deg:
                rotated_template = self._rotate_gray(template_gray, float(angle_deg))
                rotated_mask = self._rotate_mask(template_mask, float(angle_deg))
                if int(np.count_nonzero(rotated_mask)) < 16:
                    continue

                tpl, tpl_mask = self._resize_gray_and_mask(
                    rotated_template,
                    rotated_mask,
                    float(self.DOWNSAMPLE),
                )
                if tpl.shape[0] >= target.shape[0] or tpl.shape[1] >= target.shape[1]:
                    continue
                response = cv2.matchTemplate(
                    target,
                    tpl,
                    cv2.TM_CCOEFF_NORMED,
                    mask=tpl_mask,
                )
                response = np.nan_to_num(response, nan=-1.0, posinf=-1.0, neginf=-1.0)
                _, max_val, _, max_loc = cv2.minMaxLoc(response)
                score = float(max_val)
                if score > best_score:
                    loc_x = float(max_loc[0]) / float(self.DOWNSAMPLE)
                    loc_y = float(max_loc[1]) / float(self.DOWNSAMPLE)
                    rotated_centroid = self._rotate_point_in_shape(
                        centroid_in_crop,
                        template_gray.shape,
                        float(angle_deg),
                    )
                    best_score = score
                    best_result = {
                        "angle": float(angle_deg),
                        "location": (int(round(loc_x)), int(round(loc_y))),
                        "score": score,
                        "template_size": (int(template_gray.shape[1]), int(template_gray.shape[0])),
                        "match_center": (
                            float(loc_x + rotated_centroid[0]),
                            float(loc_y + rotated_centroid[1]),
                        ),
                        "template_mask": rotated_mask,
                        "match_mode": "segmented_20x_masked_optical_ccoef",
                    }
            if best_result is None:
                raise RuntimeError("Masked template matching failed to produce a valid result.")
            return best_result

        coarse_angles = np.arange(
            self.INIT_ANGLE_DEG - self.SEARCH_RANGE_DEG,
            self.INIT_ANGLE_DEG + self.SEARCH_RANGE_DEG + 1.0,
            3.0,
        )
        coarse_result = _search(coarse_angles)
        fine_angles = np.arange(
            coarse_result["angle"] - 2.0,
            coarse_result["angle"] + 2.1,
            0.5,
        )
        return _search(fine_angles)

    def _find_best_segmented_current_match(
        self,
        current: np.ndarray,
        reference_mask: np.ndarray,
        material_type: str,
        output_dir: str,
        image_stem: str,
        expected_thickness_label: str = "",
    ) -> dict:
        """Locate the reference flake by segmenting the new, actual 20X image.

        The ``magnification=100`` value below is deliberately a segmenter
        parameter profile only.  The captured image and all returned pixel
        coordinates remain in the native 20X frame, so downstream stage motion
        must continue to use ``PIXEL_SIZE_UM == 0.183``.
        """
        from agents.processing_agent.segmenter.livesegment import LiveSegment

        reference_contours, _ = cv2.findContours(
            reference_mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        if not reference_contours:
            raise RuntimeError("Reference mask does not contain a flake contour.")
        reference_contour = max(reference_contours, key=cv2.contourArea)
        reference_area = float(np.count_nonzero(reference_mask))
        if reference_area <= 0:
            raise RuntimeError("Reference mask area is zero.")

        live = LiveSegment(
            input_dir=output_dir,
            result_dir=output_dir,
            result_txt=os.path.join(output_dir, f"{image_stem}_segmentation_unused.txt"),
            # Profile 100 works well on these images. The image is still 20X.
            magnification=int(self.CURRENT_IMAGE_SEGMENTER_PROFILE),
            focus_disks=[],
            material_type=material_type,
            consensus_runs=int(self.SEGMENTATION_CONSENSUS_RUNS),
        )
        segmenter, candidates, consensus_ok = live._run_with_consensus(
            cv2.cvtColor(current, cv2.COLOR_BGR2RGB)
        )
        if not consensus_ok:
            raise RuntimeError(
                f"Material segmentation did not reach {self.SEGMENTATION_CONSENSUS_RUNS}-run consensus."
            )

        height, width = current.shape[:2]
        frame_diagonal = math.hypot(width, height)
        matches: list[dict] = []
        for candidate in candidates:
            candidate_mask = np.where(
                segmenter.masks == int(candidate["mask_id"]), 255, 0
            ).astype(np.uint8)
            contours, _ = cv2.findContours(
                candidate_mask,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE,
            )
            if not contours:
                continue
            contour = max(contours, key=cv2.contourArea)
            area = float(np.count_nonzero(candidate_mask))
            centroid_row, centroid_col = map(float, candidate["centroid"])
            area_ratio = area / reference_area
            area_log_error = abs(math.log(max(area_ratio, 1e-12)))
            shape_distance = float(
                cv2.matchShapes(
                    reference_contour,
                    contour,
                    cv2.CONTOURS_MATCH_I1,
                    0.0,
                )
            )
            center_distance = math.hypot(
                centroid_col - width / 2.0,
                centroid_row - height / 2.0,
            )
            rank_score = (
                2.0 * area_log_error
                + shape_distance
                + 0.25 * center_distance / max(frame_diagonal, 1.0)
            )
            matches.append(
                {
                    "mask_id": int(candidate["mask_id"]),
                    "thickness_label": str(candidate["label"]),
                    "area_px": area,
                    "area_ratio_to_reference": area_ratio,
                    "shape_distance_i1": shape_distance,
                    "centroid_xy_px": [centroid_col, centroid_row],
                    "distance_to_frame_center_px": center_distance,
                    "rank_score": rank_score,
                    "mask": candidate_mask,
                    "contour": contour,
                }
            )
        if not matches:
            raise RuntimeError("Material segmenter found no usable flake candidates.")

        matches.sort(key=lambda row: float(row["rank_score"]))
        best = matches[0]
        # Prevent an obviously unrelated segmentation island from commanding a
        # stage move. Borderline cases safely use the established optical path.
        if not (0.35 <= float(best["area_ratio_to_reference"]) <= 2.85):
            raise RuntimeError(
                "Best segmented candidate failed area-ratio validation "
                f"({best['area_ratio_to_reference']:.3f})."
            )
        if float(best["shape_distance_i1"]) > 1.0:
            raise RuntimeError(
                "Best segmented candidate failed shape validation "
                f"({best['shape_distance_i1']:.3f})."
            )

        best_mask = best["mask"]
        x, y, bbox_w, bbox_h = cv2.boundingRect(best["contour"])
        cropped_mask = best_mask[y : y + bbox_h, x : x + bbox_w]
        best_mask_path = os.path.join(
            output_dir,
            f"{image_stem}_current_segmented_best_mask.png",
        )
        cv2.imwrite(best_mask_path, best_mask)

        serializable_candidates = [
            {key: value for key, value in match.items() if key not in {"mask", "contour"}}
            for match in matches
        ]
        report_path = os.path.join(
            output_dir,
            f"{image_stem}_current_segmentation_match.json",
        )
        report = {
            "capture_magnification": "20X",
            "pixel_size_um_per_px": float(self.PIXEL_SIZE_UM),
            "segmenter_parameter_profile": int(self.CURRENT_IMAGE_SEGMENTER_PROFILE),
            "segmenter_profile_is_not_capture_magnification": True,
            "material_type": material_type,
            "expected_thickness_label": expected_thickness_label,
            "reference_area_px": reference_area,
            "consensus_runs": int(self.SEGMENTATION_CONSENSUS_RUNS),
            "candidates": serializable_candidates,
            "selected_candidate": serializable_candidates[0],
            "best_mask_path": best_mask_path,
        }
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)

        centroid_x, centroid_y = map(float, best["centroid_xy_px"])
        return {
            "angle": 0.0,
            "location": (int(x), int(y)),
            "score": 1.0 / (1.0 + float(best["rank_score"])),
            "template_size": (int(bbox_w), int(bbox_h)),
            "match_center": (centroid_x, centroid_y),
            "template_mask": cropped_mask,
            "match_mode": "material_100_profile_segmentation_on_actual_20x_capture",
            "segmentation_material": material_type,
            "segmentation_label": best["thickness_label"],
            "segmentation_area_px": float(best["area_px"]),
            "reference_area_px": reference_area,
            "area_ratio_to_reference": float(best["area_ratio_to_reference"]),
            "shape_distance_i1": float(best["shape_distance_i1"]),
            "segmentation_mask_path": best_mask_path,
            "segmentation_report_path": report_path,
        }

    def _find_best_match(
        self,
        initial: np.ndarray,
        current: np.ndarray,
        reference_mask: Optional[np.ndarray] = None,
    ) -> dict:
        if reference_mask is not None:
            return self._find_best_masked_match(initial, current, reference_mask)

        if self.DOWNSAMPLE != 1.0:
            initial = cv2.resize(initial, None, fx=self.DOWNSAMPLE, fy=self.DOWNSAMPLE)
            current = cv2.resize(current, None, fx=self.DOWNSAMPLE, fy=self.DOWNSAMPLE)

        if self.USE_EDGE:
            initial = self._preprocess(initial)
            current = self._preprocess(current)

        coarse_angles = np.arange(
            self.INIT_ANGLE_DEG - self.SEARCH_RANGE_DEG,
            self.INIT_ANGLE_DEG + self.SEARCH_RANGE_DEG + 1.0,
            3.0,
        )
        coarse_result = self._search_once(initial, current, coarse_angles)

        fine_angles = np.arange(
            coarse_result["angle"] - 2.0,
            coarse_result["angle"] + 2.1,
            0.5,
        )
        fine_result = self._search_once(initial, current, fine_angles)
        fine_result["location"] = (
            int(fine_result["location"][0] / self.DOWNSAMPLE),
            int(fine_result["location"][1] / self.DOWNSAMPLE),
        )
        template = self._extract_center_patch(initial, ratio=self.MATCH_RATIO)
        template_w = int(template.shape[1] / self.DOWNSAMPLE)
        template_h = int(template.shape[0] / self.DOWNSAMPLE)
        fine_result["template_size"] = (template_w, template_h)
        fine_result["match_center"] = (
            float(fine_result["location"][0]) + template_w / 2.0,
            float(fine_result["location"][1]) + template_h / 2.0,
        )
        fine_result["match_mode"] = "legacy_center_patch"
        return fine_result

    def _visualize_match(
        self,
        initial: np.ndarray,
        current: np.ndarray,
        result: dict,
        save_path: str,
    ) -> str:
        angle_deg = float(result["angle"])
        if result.get("match_mode") == "legacy_center_patch":
            angle_deg = -float(result["angle"])
        x, y = result["location"]
        score = float(result["score"])

        template_size = result.get("template_size")
        if template_size:
            w, h = int(template_size[0]), int(template_size[1])
        else:
            rotated_initial = self._rotate_image(initial, angle_deg)
            template = self._extract_center_patch(rotated_initial, ratio=self.MATCH_RATIO)
            h, w = template.shape[:2]

        corners = np.array(
            [
                [-w / 2.0, -h / 2.0],
                [w / 2.0, -h / 2.0],
                [w / 2.0, h / 2.0],
                [-w / 2.0, h / 2.0],
            ]
        )
        theta = np.deg2rad(angle_deg)
        rot = np.array(
            [
                [np.cos(theta), -np.sin(theta)],
                [np.sin(theta), np.cos(theta)],
            ]
        )
        rotated_corners = corners @ rot.T
        center_x = x + w / 2.0
        center_y = y + h / 2.0
        rotated_corners[:, 0] += center_x
        rotated_corners[:, 1] += center_y

        vis = current.copy()
        pts = rotated_corners.astype(np.int32)
        cv2.polylines(vis, [pts], isClosed=True, color=(0, 0, 255), thickness=3)
        template_mask = result.get("template_mask")
        if isinstance(template_mask, np.ndarray):
            contours, _ = cv2.findContours(template_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            shifted = [contour + np.array([[[int(x), int(y)]]]) for contour in contours]
            cv2.drawContours(vis, shifted, -1, (0, 255, 255), 2)
        match_center = result.get("match_center")
        if isinstance(match_center, (tuple, list)) and len(match_center) >= 2:
            cv2.circle(
                vis,
                (int(round(float(match_center[0]))), int(round(float(match_center[1])))),
                8,
                (0, 255, 255),
                -1,
            )
        cv2.circle(
            vis,
            (int(round(current.shape[1] / 2.0)), int(round(current.shape[0] / 2.0))),
            8,
            (0, 255, 0),
            -1,
        )
        text = f"angle={angle_deg:.2f}, score={score:.3f}"
        cv2.putText(
            vis,
            text,
            (int(x), max(int(y) - 10, 20)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 255),
            2,
        )
        cv2.imwrite(save_path, vis)
        return save_path

    @staticmethod
    def _parse_capture_result(message: str) -> tuple[Optional[str], Optional[float]]:
        if not message:
            return None, None
        path = None
        score = None
        if "Captured image:" in message:
            try:
                after = message.split("Captured image:", 1)[1]
                path = after.split("(score=", 1)[0].strip().strip(".")
            except Exception:
                path = None
        if "score=" in message:
            try:
                score_text = message.split("score=", 1)[1].split(")", 1)[0]
                score = float(score_text)
            except Exception:
                score = None
        return path, score

    @staticmethod
    def _parse_focus_position(message: str) -> Optional[int]:
        if not message:
            return None
        match = re.search(r"Focus position:\s*(-?\d+)", message)
        if match is None:
            return None
        return int(match.group(1))

    @staticmethod
    def _stem_from_image_path(image_path: str) -> str:
        stem = os.path.splitext(os.path.basename(image_path))[0]
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")
        return stem or "alignment"

    def forward(
        self,
        image_path: str,
        alignment_dir: Optional[str] = None,
        save_stem: Optional[str] = None,
        shift_x: Optional[float] = 0,
        shift_y: Optional[float] = 0,
        init_angle_deg: Optional[float] = 0,
        mask_path: Optional[str] = None,
    ) -> str:
        if self.stage is None or self.camera is None:
            return "❌ Stage or camera controller unavailable."

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []

        if not alignment_dir:
            run_dirs = get_stacking_run_dirs()
            alignment_dir = os.path.join(run_dirs["run_dir"], "alignment")
        os.makedirs(alignment_dir, exist_ok=True)

        image_stem = self._stem_from_image_path(save_stem or image_path)
        current_capture_path = os.path.join(alignment_dir, f"{image_stem}_current.jpg")
        match_result_path = os.path.join(alignment_dir, f"{image_stem}_match_result.jpg")

        self.broadcaster_step.forward(0)
        reference = self._load_image(image_path, "image_path")
        flake_metadata = self._resolve_flake_metadata_for_reference(image_path)
        resolved_mask_path = (
            mask_path
            or flake_metadata.get("mask_20x_path")
            or self._resolve_mask_path_for_reference(image_path)
        )
        reference_mask = self._load_binary_mask(resolved_mask_path, reference.shape[:2]) if resolved_mask_path else None
        segmentation_material = str(flake_metadata.get("material_type") or "").strip()
        expected_thickness_label = str(
            flake_metadata.get("thickness_label") or ""
        ).strip()
        results.append(f"Loaded reference image: {os.path.abspath(image_path)}")
        if reference_mask is not None and resolved_mask_path:
            results.append(f"Loaded segmented 20X mask: {os.path.abspath(resolved_mask_path)}")
            if segmentation_material:
                results.append(
                    "Current capture segmentation: actual 20X image, "
                    f"material={segmentation_material}, parameter profile="
                    f"{self.CURRENT_IMAGE_SEGMENTER_PROFILE} (profile only, not magnification)."
                )
            else:
                results.append(
                    "Reference material unavailable; current-image segmentation cannot be selected safely."
                )
        else:
            results.append("Segmented 20X mask unavailable; using legacy center-patch matching.")
        results.append(f"Alignment directory: {alignment_dir}")

        self.broadcaster_step.forward(1)
        results.append(self.cam_start(open_live_view=True))
        try:
            results.append(self.cam_autofocus_once())
        except Exception as exc:
            results.append(f"⚠️ Autofocus trigger failed: {exc}")
        try:
            results.append(self.cam_autofocus_off())
        except Exception as exc:
            results.append(f"⚠️ Autofocus disable failed: {exc}")

        try:
            focus_position_msg = self.cam_get_focus_pos()
            results.append(focus_position_msg)
            focus_position = self._parse_focus_position(focus_position_msg)
            print(f"[REFIND_ACCURATE] Focus position: {focus_position}", flush=True)
            if focus_position is None:
                results.append("⚠️ Could not parse focus position; keeping current focus.")
            elif focus_position < 100 or focus_position > 700:
                results.append(self.cam_set_focus_pos(pos=330))
                results.append(
                    f"Focus position {focus_position} was outside [100, 700]; reset to 330."
                )
            else:
                results.append(
                    f"Focus position {focus_position} is within [100, 700]; keeping it."
                )
        except Exception as exc:
            results.append(f"⚠️ Focus-position range check failed: {exc}")

        time.sleep(30.0)

        capture_msg = self.cam_wait_focus_photo(
            photo_dir=alignment_dir,
            min_blur=float(self.MIN_BLUR),
            save_path=current_capture_path,
        )
        captured_path, score = self._parse_capture_result(capture_msg)
        if not captured_path:
            try:
                results.append(self.cam_stop())
            except Exception:
                pass
            raise RuntimeError(f"Failed to capture current alignment image: {capture_msg}")
        current = self._load_image(captured_path, "captured image B")
        results.append(f"Captured current image: {captured_path}")
        if score is not None:
            results.append(f"Focus score: {score:.3f}")

        self.broadcaster_step.forward(2)
        self.INIT_ANGLE_DEG = float(init_angle_deg or 0.0)
        segmentation_error: Optional[str] = None
        if reference_mask is not None and segmentation_material:
            try:
                result = self._find_best_segmented_current_match(
                    current=current,
                    reference_mask=reference_mask,
                    material_type=segmentation_material,
                    output_dir=alignment_dir,
                    image_stem=image_stem,
                    expected_thickness_label=expected_thickness_label,
                )
            except Exception as exc:
                segmentation_error = str(exc)
                result = self._find_best_match(
                    reference,
                    current,
                    reference_mask=reference_mask,
                )
        else:
            result = self._find_best_match(
                reference,
                current,
                reference_mask=reference_mask,
            )
        saved_match_path = self._visualize_match(reference, current, result, match_result_path)
        results.append(f"Initial angle guess: {self.INIT_ANGLE_DEG:.3f}")
        if segmentation_error:
            results.append(
                "⚠️ Current 20X image segmentation was rejected; "
                f"used optical fallback: {segmentation_error}"
            )
        results.append(f"Match mode: {result.get('match_mode', 'unknown')}")
        if result.get("match_mode") == "material_100_profile_segmentation_on_actual_20x_capture":
            results.append(
                "Capture scale: actual 20X at "
                f"{self.PIXEL_SIZE_UM:.3f} um/px; segmenter parameter profile: "
                f"{self.CURRENT_IMAGE_SEGMENTER_PROFILE}."
            )
            results.append(
                "Segmented candidate: "
                f"material={result['segmentation_material']}, "
                f"label={result['segmentation_label']}, "
                f"area={result['segmentation_area_px']:.0f} px2, "
                f"reference ratio={result['area_ratio_to_reference']:.4f}, "
                f"shape I1={result['shape_distance_i1']:.6f}."
            )
            results.append(f"Saved segmentation mask: {result['segmentation_mask_path']}")
            results.append(f"Saved segmentation report: {result['segmentation_report_path']}")
        results.append(f"Best angle: {result['angle']:.3f}")
        results.append(f"Location: {result['location']}")
        results.append(f"Score: {result['score']:.6f}")
        results.append(f"Saved match result: {saved_match_path}")

        center_x = current.shape[1] / 2.0
        center_y = current.shape[0] / 2.0
        match_center = result.get("match_center")
        if isinstance(match_center, (tuple, list)) and len(match_center) >= 2:
            matched_x = float(match_center[0])
            matched_y = float(match_center[1])
        else:
            template_w, template_h = result.get("template_size", (0, 0))
            matched_x = float(result["location"][0]) + float(template_w) / 2.0
            matched_y = float(result["location"][1]) + float(template_h) / 2.0

        center_delta_x = center_x - matched_x
        center_delta_y = center_y - matched_y
        center_move_x = (
            -float(center_delta_y) * float(self.PIXEL_SIZE_UM) * float(self.STEPS_PER_UM)
        )
        center_move_y = (
            -float(center_delta_x) * float(self.PIXEL_SIZE_UM) * float(self.STEPS_PER_UM)
        )

        self.broadcaster_step.forward(3)
        results.append(f"Center delta x: {center_delta_x:.3f}")
        results.append(f"Center delta y: {center_delta_y:.3f}")
        results.append(
            self.stage_move_relative(dx=center_move_x, dy=center_move_y, wait=True)
        )
        results.append(
            f"Center move stage by ({center_move_x:.3f}, {center_move_y:.3f})"
        )
        alignment_message = "Moved to center, align according to design"
        print(alignment_message, flush=True)
        results.append(alignment_message)
        time.sleep(self.CENTER_TO_DESIGN_PAUSE_S)

        shift_delta_x = float(shift_x)
        shift_delta_y = float(shift_y)
        shift_move_x = (
            -shift_delta_y * float(self.PIXEL_SIZE_UM) * float(self.STEPS_PER_UM)
        )
        shift_move_y = (
            -shift_delta_x * float(self.PIXEL_SIZE_UM) * float(self.STEPS_PER_UM)
        )

        self.broadcaster_step.forward(4)
        results.append(f"Shift x: {float(shift_x):.3f}")
        results.append(f"Shift y: {float(shift_y):.3f}")
        results.append(f"Shift delta x at calibrated 20X: {shift_delta_x:.3f}")
        results.append(f"Shift delta y at calibrated 20X: {shift_delta_y:.3f}")
        if shift_move_x != 0.0 or shift_move_y != 0.0:
            results.append(
                self.stage_move_relative(dx=shift_move_x, dy=shift_move_y, wait=True)
            )
            results.append(
                f"Shift move stage by ({shift_move_x:.3f}, {shift_move_y:.3f})"
            )
        else:
            results.append("Shift move skipped because shift_x and shift_y are both zero.")
        time.sleep(60.0)
        try:
            results.append(self.cam_stop())
        except Exception as exc:
            results.append(f"⚠️ Camera stop failed: {exc}")
        return "\n".join(results)
