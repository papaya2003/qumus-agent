import cv2
import numpy as np
import os
import sys
import json
import re

current_dir = os.path.dirname(os.path.abspath(__file__))
seg_dir = os.path.join(os.path.dirname(current_dir), 'segmenter')
sys.path.append(seg_dir)

from segmenter import Segmenter
from material import graphene, hBN, wte2, wse2, rese2
from tqdm import tqdm
from utils import focus_disk, blur
from scipy.ndimage import gaussian_filter
import warnings
import matplotlib.pyplot as plt

warnings.simplefilter('ignore', UserWarning)

colors_by_layer = {
    'monolayer': np.array([0,163,255])/255.0, # Blue
    'bilayer': np.array([29,255,0])/255.0, # Green
    'trilayer': np.array([198,22,22])/255.0, # Red
    'fewlayer': np.array([255,165,0])/255.0, # Orange
    'manylayer': np.array([255,165,0])/255.0, # Orange
    'bluish_layers': np.array([255,165,0])/255.0, # Orange
    'bulk': np.array([152,7,235])/255.0, # Purple
    'dirt': np.array([0, 0, 0])/255.0, # Uncolored
    'more_bluish_layers': np.array([255, 255, 0])/255.0, # Yellow
    'bg': np.array([0, 0, 0])/255.0, # Uncolored
    'black': np.array([0, 0, 0])/255.0, # Uncolored
}

def append_to_line(line, arr):
    arr_str = ' '.join(str(x) for x in arr)
    line = line.strip()
    if line:
        return line + ' ' + arr_str + '\n'
    else:
        return arr_str + '\n'

class LiveSegment():
    def __init__(
        self,
        input_dir,
        result_dir,
        result_txt,
        magnification,
        focus_disks,
        min_area=None,
        max_area=None,
        grow=3,
        material_type="Gr",
        consensus_runs=None,
    ):
        self.input_dir = input_dir
        self.result_dir = result_dir
        self.magnification = magnification 
        self.focus_disks = focus_disks
        if min_area is None:
            if magnification >= 50:
                min_area = 2000
            else:
                min_area = 200
        if max_area is None:
            if magnification >= 50:
                max_area = 10000000
            else:
                max_area = 100000
        self.min_area = min_area
        self.max_area = max_area
        self.grow = grow

        self.result_txt = result_txt
        self.material_type = material_type or "Gr"
        self._material, self._colors = self._resolve_material_config(self.material_type)
        if consensus_runs is None:
            consensus_runs = 3 if self.magnification >= 50 else 1
        self.consensus_runs = max(1, int(consensus_runs))
        self.result_jsonl = self.result_txt + ".jsonl"

    def _resolve_material_config(self, material_type):
        normalized = str(material_type or "").strip().lower()
        if normalized in {"hbn", "boron nitride"}:
            return hBN, {
                '0-10': np.array([0,163,255])/255.0,
                '10-20': np.array([29,255,0])/255.0,
                '20-30': np.array([198,22,22])/255.0,
                '30-40': np.array([255,165,0])/255.0,
                '40+': np.array([152,7,235])/255.0,
                'dirt': np.array([0, 0, 0])/255.0,
                'bg': np.array([0, 0, 0])/255.0,
                'black': np.array([0, 0, 0])/255.0,
            }
        if normalized in {"wte2", "w te2", "w-te2"}:
            return wte2, colors_by_layer
        if normalized in {"wse2", "w se2", "w-se2"}:
            return wse2, colors_by_layer
        if normalized in {"rese2", "re se2", "re-se2"}:
            return rese2, colors_by_layer
        return graphene, colors_by_layer

    def _build_segmenter(self, img_rgb, max_area=None):
        effective_max_area = self.max_area if max_area is None else max_area
        return Segmenter(
            img_rgb,
            self._material,
            colors=self._colors,
            magnification=self.magnification,
            min_area=self.min_area,
            max_area=effective_max_area,
            focus_disks=self.focus_disks,
            k=3,
            surround_bg=True,
        )

    def _collect_candidates(self, segmenter, max_area=None):
        effective_max_area = self.max_area if max_area is None else max_area
        candidates = []
        for idx, mask_id in enumerate(segmenter.mask_ids):
            if mask_id == 0:
                continue
            area = float(segmenter.mask_areas[idx])
            if not (self.min_area < area < effective_max_area):
                continue
            label = segmenter.mask_labels[idx]
            if label in {"dirt", "bg", "black", None, ""}:
                continue
            centroid = segmenter.mask_coords(mask_id)
            if centroid is None:
                continue
            candidates.append(
                {
                    "mask_id": int(mask_id),
                    "label": label,
                    "area": area,
                    "centroid": np.asarray(centroid, dtype=float),
                }
            )
        candidates.sort(key=lambda row: (row["label"], -row["area"], row["centroid"][0], row["centroid"][1]))
        return candidates

    def _candidates_match(self, left, right, centroid_tol_px=35.0, area_rel_tol=0.35):
        if len(left) != len(right):
            return False
        used = [False] * len(right)
        for cand in left:
            match_idx = None
            for idx, other in enumerate(right):
                if used[idx] or cand["label"] != other["label"]:
                    continue
                centroid_dist = float(np.linalg.norm(cand["centroid"] - other["centroid"]))
                if centroid_dist > centroid_tol_px:
                    continue
                denom = max(cand["area"], other["area"], 1.0)
                if abs(cand["area"] - other["area"]) / denom > area_rel_tol:
                    continue
                match_idx = idx
                break
            if match_idx is None:
                return False
            used[match_idx] = True
        return True

    def _run_with_consensus(self, img_rgb, max_area=None):
        runs = []
        for _ in range(self.consensus_runs):
            segmenter = self._build_segmenter(img_rgb, max_area=max_area)
            segmenter.process_frame()
            runs.append(
                (
                    segmenter,
                    self._collect_candidates(segmenter, max_area=max_area),
                )
            )

        base_segmenter, base_candidates = runs[0]
        if self.consensus_runs == 1:
            return base_segmenter, base_candidates, True

        consensus_ok = all(self._candidates_match(base_candidates, candidates) for _, candidates in runs[1:])
        return base_segmenter, base_candidates, consensus_ok

    def _largest_candidates(self, candidates, limit=1):
        if not candidates:
            return []
        ordered = sorted(candidates, key=lambda c: float(c.get("area", 0.0)), reverse=True)
        return ordered[:limit]

    def _center_priority_candidate(self, candidates, frame_size, tie_tolerance_px=50.0):
        """Select a revisit target by center distance, using area only for a near tie.

        ``Segmenter.mask_coords`` returns coordinates in (row, column) order.
        Revisit captures are centered on the selected flake, so an unrelated
        large flake elsewhere in the field must not outrank the centered target.
        """
        if not candidates:
            return None
        frame_center = np.asarray(frame_size, dtype=float) / 2.0
        ranked = [
            (float(np.linalg.norm(cand["centroid"] - frame_center)), cand)
            for cand in candidates
        ]
        nearest_distance = min(distance for distance, _ in ranked)
        if nearest_distance > 0.35 * min(frame_size):
            return None
        # A large revisit plate may be slightly off-center while a narrow
        # fragment or terrace crosses the exact center. Prefer the largest
        # candidate inside a tight central zone before falling back to the
        # nearest-candidate tie rule.
        central_zone_radius = 0.12 * min(frame_size)
        central_candidates = [
            cand for distance, cand in ranked if distance <= central_zone_radius
        ]
        if central_candidates:
            return max(
                central_candidates,
                key=lambda cand: float(cand.get("area", 0.0)),
            )
        near_ties = [
            cand
            for distance, cand in ranked
            if distance <= nearest_distance + float(tie_tolerance_px)
        ]
        return max(near_ties, key=lambda cand: float(cand.get("area", 0.0)))

    def _is_graphene(self):
        normalized = str(self.material_type or "").strip().lower()
        return normalized in {"gr", "graphene"}

    def _center_color_fallback(self, img_rgb, accepted_labels=None):
        """Recover a centered graphene revisit flake by normalized LAB color.

        Native 20X captures use a deterministic residual-color mask. At 100X,
        where the target can fill most of the frame and touch its borders, LAB
        clustering is more reliable. Both paths use side substrate as the
        background reference and retain only the component nearest the center.
        Ordinary graphene revisits recover monolayer/bilayer targets; callers
        can restrict recovery to another calibrated graphene-family label for
        purpose-specific captures such as a many-layer graphite gate.
        """
        if not self._is_graphene():
            return None

        if accepted_labels is None:
            accepted_labels = {"monolayer", "bilayer"}
        else:
            accepted_labels = {
                str(label).strip().lower() for label in accepted_labels
            }

        height, width = img_rgb.shape[:2]
        shrink = 4 if min(height, width) >= 400 else 1
        small_width = max(1, width // shrink)
        small_height = max(1, height // shrink)
        small_rgb = cv2.resize(
            img_rgb,
            (small_width, small_height),
            interpolation=cv2.INTER_AREA,
        )
        lab = cv2.cvtColor(small_rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        lab[:, :, 0] *= 100.0 / 255.0
        lab[:, :, 1:] -= 128.0

        target_bg = np.asarray(
            self._material.target_bg_lab_for_mag(self.magnification),
            dtype=float,
        )
        # The revisit-camera color calibration is shared by the 20X and 100X
        # captures even though their edge profiles must remain different.
        color_profile_mag = 100 if self.magnification == 20 else self.magnification
        reference_rows = []
        for reference_lab, label in self._material.layer_labels_for_mag(
            color_profile_mag
        ).items():
            if label in accepted_labels:
                reference_rows.append((np.asarray(reference_lab, dtype=float), label))
        if not reference_rows:
            return None

        target_label = None
        if self.magnification < 50:
            # Build a row-wise substrate estimate from the two side bands. This
            # removes the strong illumination/color gradient in native 20X
            # captures without sampling the centered flake itself.
            x0, x1 = 0, small_width
            band_width = max(8, int(round(0.12 * small_width)))
            left_bg = np.median(lab[:, :band_width], axis=1)
            right_bg = np.median(lab[:, -band_width:], axis=1)
            smooth_height = min(101, small_height if small_height % 2 else small_height - 1)
            smooth_height = max(1, smooth_height)
            left_bg = cv2.GaussianBlur(
                left_bg[:, None, :], (1, smooth_height), 0
            )[:, 0, :]
            right_bg = cv2.GaussianBlur(
                right_bg[:, None, :], (1, smooth_height), 0
            )[:, 0, :]
            alpha = np.linspace(0.0, 1.0, small_width, dtype=np.float32)[None, :, None]
            local_bg = left_bg[:, None, :] * (1.0 - alpha) + right_bg[:, None, :] * alpha
            residual = lab - local_bg
            reference_deltas = np.stack(
                [reference_lab - target_bg for reference_lab, _ in reference_rows],
                axis=0,
            )
            distances = np.linalg.norm(
                residual[None, :, :, :] - reference_deltas[:, None, None, :],
                axis=3,
            )
            projections = np.stack(
                [
                    np.sum(residual * delta, axis=2) / max(float(np.sum(delta ** 2)), 1e-6)
                    for delta in reference_deltas
                ],
                axis=0,
            )
            binary = (
                (np.min(distances, axis=0) < 4.5)
                & (np.max(projections, axis=0) > 0.30)
            ).astype(np.uint8)
            binary = cv2.morphologyEx(
                binary,
                cv2.MORPH_CLOSE,
                np.ones((7, 7), dtype=np.uint8),
                iterations=2,
            )
            binary = cv2.morphologyEx(
                binary,
                cv2.MORPH_OPEN,
                np.ones((3, 3), dtype=np.uint8),
            )
        else:
            # Side margins normally contain bare substrate even when a large
            # 100X flake reaches the top and bottom of the image.
            x0 = int(round(0.20 * small_width))
            x1 = int(round(0.80 * small_width))
            if x1 - x0 < 8:
                return None
            roi = lab[:, x0:x1]
            samples = np.ascontiguousarray(roi.reshape(-1, 3), dtype=np.float32)
            cluster_count = min(4, len(samples))
            if cluster_count < 2:
                return None
            cv2.setRNGSeed(3701)
            criteria = (
                cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
                50,
                0.05,
            )
            _, labels, centers = cv2.kmeans(
                samples,
                cluster_count,
                None,
                criteria,
                5,
                cv2.KMEANS_PP_CENTERS,
            )
            labels = labels.reshape(roi.shape[:2])
            strip_width = max(2, int(round(0.04 * labels.shape[1])))
            side_labels = np.concatenate(
                (labels[:, :strip_width].ravel(), labels[:, -strip_width:].ravel())
            )
            bg_cluster = int(
                np.bincount(side_labels, minlength=cluster_count).argmax()
            )
            bg_lab = centers[bg_cluster]
            cluster_choices = []
            for cluster_idx, center_lab in enumerate(centers):
                if cluster_idx == bg_cluster:
                    continue
                corrected_lab = center_lab - bg_lab + target_bg
                reference_lab, label = min(
                    reference_rows,
                    key=lambda row: float(np.linalg.norm(corrected_lab - row[0])),
                )
                color_distance = float(np.linalg.norm(corrected_lab - reference_lab))
                contrast = float(np.linalg.norm(center_lab - bg_lab))
                max_distance_by_label = {
                    "monolayer": 6.0,
                    "bilayer": 7.0,
                    # Thick graphite has substantially wider optical contrast
                    # than atomically thin graphene. This still requires a
                    # strong separation from the learned substrate cluster.
                    "manylayer": 18.0,
                }
                max_distance = max_distance_by_label.get(label, 7.0)
                if color_distance <= max_distance and contrast >= 2.5:
                    cluster_choices.append(
                        (color_distance, -contrast, cluster_idx, label)
                    )
            if not cluster_choices:
                return None
            _, _, target_cluster, target_label = min(cluster_choices)
            binary = (labels == target_cluster).astype(np.uint8)
            binary = cv2.morphologyEx(
                binary,
                cv2.MORPH_CLOSE,
                np.ones((3, 3), dtype=np.uint8),
            )

        component_count, component_labels, stats, centroids = (
            cv2.connectedComponentsWithStats(binary, connectivity=8)
        )
        roi_center = np.asarray(
            [small_height / 2.0, small_width / 2.0 - x0],
            dtype=float,
        )
        components = []
        for component_idx in range(1, component_count):
            area_small = int(stats[component_idx, cv2.CC_STAT_AREA])
            area_estimate = float(area_small * shrink * shrink)
            if not (self.min_area < area_estimate < self.max_area):
                continue
            left = int(stats[component_idx, cv2.CC_STAT_LEFT])
            comp_width = int(stats[component_idx, cv2.CC_STAT_WIDTH])
            if left == 0 and left + comp_width >= binary.shape[1]:
                continue
            centroid = np.asarray(
                [centroids[component_idx, 1], centroids[component_idx, 0]],
                dtype=float,
            )
            center_distance = float(np.linalg.norm(centroid - roi_center))
            components.append((center_distance, -area_small, component_idx))
        if not components:
            return None

        center_distance, _, component_idx = min(components)
        if center_distance > 0.35 * min(small_height, small_width):
            return None

        small_mask = np.zeros((small_height, small_width), dtype=np.uint8)
        small_mask[:, x0:x1][component_labels == component_idx] = 1
        mask = cv2.resize(
            small_mask,
            (width, height),
            interpolation=cv2.INTER_NEAREST,
        ).astype(bool)
        area = float(np.count_nonzero(mask))
        if not (self.min_area < area < self.max_area):
            return None
        centroid = np.argwhere(mask).mean(axis=0)
        if target_label is None:
            selected_small = component_labels == component_idx
            mean_residual = np.mean(residual[selected_small], axis=0)
            corrected_lab = mean_residual + target_bg
            _, target_label = min(
                reference_rows,
                key=lambda row: float(np.linalg.norm(corrected_lab - row[0])),
            )
        return mask, {
            "mask_id": -1,
            "label": target_label,
            "area": area,
            "centroid": np.asarray(centroid, dtype=float),
            "source": "center_color_fallback",
        }

    @staticmethod
    def _is_centered_monolayer_recovery(candidate, frame_size, max_center_fraction=0.12):
        """Return whether color recovery found a credible centered monolayer.

        A mixed-thickness graphene revisit can contain a valid thicker model
        candidate as well as the intended monolayer.  Previously, the thicker
        candidate prevented color recovery from running at all.  Restrict the
        override to an actual monolayer component inside a tight central zone
        so graphite-only revisit targets keep their normal model result.
        """
        if candidate is None or candidate.get("label") != "monolayer":
            return False
        centroid = np.asarray(candidate.get("centroid"), dtype=float)
        if centroid.shape != (2,) or not np.all(np.isfinite(centroid)):
            return False
        frame_center = np.asarray(frame_size, dtype=float) / 2.0
        center_distance = float(np.linalg.norm(centroid - frame_center))
        return center_distance <= float(max_center_fraction) * min(frame_size)

    def _is_hbn(self):
        normalized = str(self.material_type or "").strip().lower()
        return normalized in {"hbn", "boron nitride"}

    @staticmethod
    def _is_graphite_revisit_filename(filename):
        """Return whether a capture is the dedicated graphite revisit."""
        basename = os.path.basename(filename)
        return re.match(
            r"^graphite(?:_\d+)?_(?:20x|100x)(?:_|\.|$)",
            basename,
            re.IGNORECASE,
        ) is not None

    @staticmethod
    def _is_revisit_filename(filename):
        """Return whether a filename represents a centered revisit capture."""
        basename = os.path.basename(filename)
        numbered_revisit = re.match(
            r"^(?:flake|graphene|(?:(?:bottom|top)_)?hbn)_\d+(?:_|\.|$)",
            basename,
            re.IGNORECASE,
        )
        return numbered_revisit is not None or LiveSegment._is_graphite_revisit_filename(
            basename
        )

    @staticmethod
    def _revisit_index(filename):
        """Extract the target number from supported revisit filename styles."""
        basename = os.path.basename(filename)
        match = re.match(
            r"^(?:flake|graphene|(?:(?:bottom|top)_)?hbn)_(\d+)(?:_|\.|$)",
            basename,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1))
        graphite_match = re.match(
            r"^graphite(?:_(\d+))?_(?:20x|100x)(?:_|\.|$)",
            basename,
            re.IGNORECASE,
        )
        if graphite_match:
            return int(graphite_match.group(1) or 1)
        return None

    def _effective_max_area(self, filename, frame_size):
        """Allow a centered low-mag revisit target to exceed the scan limit.

        Broad-search frames retain the fixed area ceiling that suppresses large
        substrate regions. A revisit is centered on a previously chosen flake,
        so it may legitimately occupy substantially more of the image.
        """
        if self.magnification >= 50 or not self._is_revisit_filename(filename):
            return self.max_area
        frame_area = int(frame_size[0]) * int(frame_size[1])
        return max(self.max_area, int(round(0.10 * frame_area)))

    @classmethod
    def _requires_nonempty_mask(cls, filename):
        """Only centered revisit captures must contain a flake mask.

        Broad-search ``test_*`` frames are allowed to contain no detected
        flakes.  Their all-black segmentation is a valid negative result and
        must not abort the remainder of AutoSearch.
        """
        return cls._is_revisit_filename(filename)

    def _load_result_lines(self):
        hbn_lines = [
            "0-10_sizes: \n",
            "10-20_sizes: \n",
            "20-30_sizes: \n",
            "30-40_sizes: \n",
            "0-10_frame_nums: \n",
            "10-20_frame_nums: \n",
            "20-30_frame_nums: \n",
            "30-40_frame_nums: \n",
        ]
        if os.path.exists(self.result_txt):
            with open(self.result_txt, 'r') as f:
                lines = f.readlines()
            if self._is_hbn() and self.magnification < 50:
                if len(lines) < 8 or not lines[0].startswith("0-10_sizes:"):
                    return hbn_lines
            return lines
        if self._is_hbn() and self.magnification < 50:
            return hbn_lines
        return ["mono_sizes: \n", "bi_sizes: \n", "mono_frame_nums: \n", "bi_frame_nums: \n", "mono_x: \n", "mono_y: \n", "bi_x: \n", "bi_y: \n"]

    def _write_candidate_sidecar(self, candidates, frame_idx):
        with open(self.result_jsonl, "a", encoding="utf-8") as f:
            for cand in candidates:
                row = {
                    "size_area_px": float(cand["area"]),
                    "frame_num": int(frame_idx),
                    "x": float(np.round(cand["centroid"][0], 2)),
                    "y": float(np.round(cand["centroid"][1], 2)),
                    "thickness_label": str(cand["label"]),
                }
                f.write(json.dumps(row) + "\n")

    def __call__(self, filename):
        try:
            filename = os.path.basename(filename)
            g1 = cv2.imread(os.path.join(self.input_dir, filename))
            if g1 is None:
                raise RuntimeError(f"Unable to read segmentation input image: {filename}")
            g1 = cv2.cvtColor(g1, cv2.COLOR_BGR2RGB)

            effective_max_area = self._effective_max_area(filename, g1.shape[:2])
            segmenter, base_candidates, consensus_ok = self._run_with_consensus(
                g1,
                max_area=effective_max_area,
            )
            agreed_candidates = base_candidates if consensus_ok else []
            
            result = segmenter.prettify()
            revisit_target = None
            fallback_used = False
            if self._is_revisit_filename(filename):
                graphite_revisit = self._is_graphite_revisit_filename(filename)
                source_candidates = agreed_candidates if consensus_ok else base_candidates
                revisit_target = self._center_priority_candidate(
                    source_candidates,
                    frame_size=g1.shape[:2],
                )
                accepted_fallback_labels = {"manylayer"} if graphite_revisit else None
                fallback = (
                    self._center_color_fallback(
                        g1,
                        accepted_labels=accepted_fallback_labels,
                    )
                    if self._is_graphene()
                    else None
                )
                prefer_fallback = fallback is not None and (
                    self._is_centered_monolayer_recovery(
                        fallback[1],
                        frame_size=g1.shape[:2],
                    )
                    or (
                        graphite_revisit
                        and self.magnification >= 50
                        and fallback[1].get("label") == "manylayer"
                    )
                )
                if revisit_target is None or prefer_fallback:
                    if fallback is None:
                        raise RuntimeError(
                            f"No usable {self.material_type} segmentation candidate found in revisit image {filename}"
                        )
                    fallback_mask, revisit_target = fallback
                    fallback_used = True
                    result = np.zeros_like(g1, dtype=float)
                    result[fallback_mask] = self._colors.get(
                        revisit_target["label"],
                        self._colors.get("monolayer", np.array([0, 163, 255]) / 255.0),
                    )
                else:
                    # A revisit mask represents exactly the centered target,
                    # not every detected flake in the wide field of view.
                    result = result.copy()
                    result[segmenter.masks != revisit_target["mask_id"]] = 0
            result = (255 * result).astype(np.uint8)
            result = cv2.cvtColor(result, cv2.COLOR_RGB2BGR)

            print(f"{self.result_dir}/{filename}")
            output_path = f'{self.result_dir}/{filename}'
            if not np.any(result) and self._requires_nonempty_mask(filename):
                raise RuntimeError(f"Segmentation image is empty: {output_path}")
            if not cv2.imwrite(output_path, result):
                raise RuntimeError(f"Failed to write segmentation image: {output_path}")

            result_candidates = (
                [revisit_target]
                if fallback_used and revisit_target is not None
                else agreed_candidates
            )
            if consensus_ok or fallback_used:
                mono = [c for c in result_candidates if c["label"] == "monolayer"]
                bi = [c for c in result_candidates if c["label"] == "bilayer"]
                mono_size = np.array([c["area"] for c in mono], dtype=float)
                mono_locations = np.stack([c["centroid"] for c in mono], axis=0) if mono else None
                bi_size = np.array([c["area"] for c in bi], dtype=float)
                bi_locations = np.stack([c["centroid"] for c in bi], axis=0) if bi else None
            else:
                print(f"[livesegment] consensus failed for {filename}; skipping result write")
                mono_size = np.array([], dtype=float)
                mono_locations = None
                bi_size = np.array([], dtype=float)
                bi_locations = None

            i = None

            if 'test_' in filename:
                i = int(filename[filename.index('_') + 1:filename.index('.')])
            elif 'm100' in filename:
                start = filename.index('_') + 1
                start = filename.index('_', start) + 1
                end = filename.index('.')
                i = int(filename[start:end])
            elif self._is_revisit_filename(filename):
                i = self._revisit_index(filename)

            if i is None:
                print(f"[livesegment] WARNING: cannot parse index i from filename: {filename}. Skip txt write.")
                return segmenter
            mono_frame_nums = i*np.ones_like(mono_size)
            bi_frame_nums = i*np.ones_like(bi_size)

            lines = self._load_result_lines()

            json_candidates = agreed_candidates
            if self._is_revisit_filename(filename):
                # Revisit frames represent the target placed at frame center.
                # Area breaks only near-equal center-distance ties.
                json_candidates = [revisit_target] if revisit_target is not None else []

            write_json_to_txt = self.magnification >= 50

            if self._is_hbn() and self.magnification < 50:
                grouped = {
                    "0-10": [c for c in agreed_candidates if c["label"] == "0-10"],
                    "10-20": [c for c in agreed_candidates if c["label"] == "10-20"],
                    "20-30": [c for c in agreed_candidates if c["label"] == "20-30"],
                    "30-40": [c for c in agreed_candidates if c["label"] == "30-40"],
                }
                labels = ["0-10", "10-20", "20-30", "30-40"]
                for idx, label in enumerate(labels):
                    sizes = np.array([c["area"] for c in grouped[label]], dtype=float)
                    frame_nums = i * np.ones_like(sizes)
                    if sizes.size:
                        lines[idx] = append_to_line(lines[idx], sizes)
                        lines[idx + 4] = append_to_line(lines[idx + 4], frame_nums)
                write_json_to_txt = False
            else:
                if mono_locations is not None:
                    mono_locations = np.round(mono_locations, 2)
                    lines[0] = append_to_line(lines[0], mono_size)
                    lines[2] = append_to_line(lines[2], mono_frame_nums)
                    lines[4] = append_to_line(lines[4], mono_locations[:, 0])
                    lines[5] = append_to_line(lines[5], mono_locations[:, 1])

                if bi_locations is not None:
                    bi_locations = np.round(bi_locations, 2)
                    lines[1] = append_to_line(lines[1], bi_size)
                    lines[3] = append_to_line(lines[3], bi_frame_nums)
                    lines[6] = append_to_line(lines[6], bi_locations[:, 0])
                    lines[7] = append_to_line(lines[7], bi_locations[:, 1])

            # The legacy low-magnification text format only has graphene
            # monolayer/bilayer columns (or four hBN thin bins). Persist every
            # accepted label in JSONL so the manylayer graphite-gate
            # candidates remain available for ranking.
            if self.magnification < 50:
                self._write_candidate_sidecar(json_candidates, i)

            with open(self.result_txt, 'w') as f:
                f.writelines(lines)
                if write_json_to_txt and (consensus_ok or json_candidates):
                    for cand in json_candidates:
                        row = {
                            "size_area_px": float(cand["area"]),
                            "frame_num": int(i),
                            "x": float(np.round(cand["centroid"][0], 2)),
                            "y": float(np.round(cand["centroid"][1], 2)),
                            "thickness_label": str(cand["label"]),
                        }
                        f.write(json.dumps(row) + "\n")

            return segmenter
        except Exception:
            with open("livesegment_errors.txt", "a", encoding="utf-8") as f:
                import traceback
                f.write(f"Segmentation failed for {filename!r}\n")
                f.write(traceback.format_exc() + '\n')
            raise
