from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional

import numpy as np

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.search.atom_flows import (
    AtomCameraStart,
    AtomCameraStop,
    AtomCameraAutofocusOn,
    AtomCameraAutofocusOff,
    AtomCameraAutofocusOnce,
    AtomCameraGetFocusPosition,
    AtomCameraSetFocusPosition,
    AtomCameraWaitFocusAndTakePhoto,
    AtomCameraScanFocusAndTakePhoto,
    AtomCameraIsRunning,
    AtomFocusMoveRelative,
    AtomSampleStageGetPosition,
    AtomSampleStageMoveHome,
    AtomSampleStageMoveTo,
    AtomSampleStageSetChipDims,
    AtomSampleStageSetDirection,
    AtomSampleStageSetHome,
    AtomTurretRotate,
)
from agents.processing_agent.tools.observer import (
    start_observer,
    stop_observer,
    wait_for_observer_file,
    wait_for_observer_files,
)
from agents.processing_agent.segmenter.search_utils import clear_results, sort_results
from database.repositories.chip_inventory_repository import ChipInventoryRepository
from common.services.flake_inventory_service import FlakeInventoryService
from common.material_semantics import normalize_material_code
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeAutoSearch(BaseWorkflow):
    name = "molecule_autosearch"
    description = "Run a 10X snake scan, revisit graphene candidates followed by graphite-gate candidates at 20X and 100X, and segment the revisit captures."
    camera_route: str | None = "none"
    inputs = {
        "angle": {"type": "number", "description": "Scan angle (default 0).", "default": 0, "nullable": True},
        "run_dir": {"type": "string", "description": "Optional existing run directory under images/autosearch/run_<N>.", "nullable": True},
        # Freedom mode (currently disabled):
        # "chip_dims_inch": {
        #     "type": "array",
        #     "description": "Chip dimensions [long, short] in inches (from DB width_mm/height_mm conversion).",
        #     "default": [0.5, 0.5],
        #     "nullable": True,
        # },
        "open_live_view": {"type": "boolean", "description": "Open live view during scan/revisit.", "nullable": True},
        "a1": {"type": "number", "description": "10X -> focus motor correction coefficient.", "default": 18.1367, "nullable": True},
        "a2": {"type": "number", "description": "20X -> focus motor correction coefficient.", "default": 4.4029, "nullable": True},
        "focus_ref_pos": {"type": "integer", "description": "Reference AFPOSITION to reset to.", "default": 330, "nullable": True},
        "pre_scan_wait_s": {"type": "number", "description": "Seconds to wait after autofocus_once before reading AFPOSITION.", "default": 3.0, "nullable": True},
        "turret_settle_s": {"type": "number", "description": "Turret settle time (s).", "default": 1.0, "nullable": True},
        "min_blur_10x": {"type": "number", "description": "Min blur for 10X captures.", "default": 6.5, "nullable": True},
        "min_blur_20x": {"type": "number", "description": "Min blur for 20X captures.", "default": 5.5, "nullable": True},
        "min_blur_100x": {"type": "number", "description": "Min blur for 100X captures.", "default": 5.0, "nullable": True},
        "num_top_matches": {"type": "integer", "description": "Number of primary graphene candidates to revisit. hBN searches instead revisit the top four candidates in each supported thickness category.", "default": 15, "nullable": True},
        "num_top_graphite_matches": {"type": "integer", "description": "For graphene searches, number of additional manylayer graphite-gate candidates to revisit after the primary candidates.", "default": 5, "nullable": True},
        "max_area": {"type": "integer", "description": "Maximum area for candidate selection (pixels). Defaults to 100000 px at 10X and 10000000 px at 100X.", "nullable": True},
        "min_area": {"type": "integer", "description": "Minimum area for candidate selection (pixels). Defaults to 200 px at 10X and 2000 px at 100X.", "nullable": True},
        "chip_id": {"type": "integer", "description": "Chip id for flake DB persistence.", "nullable": True},
        "material_type": {"type": "string", "description": "Material code for detected flakes (e.g., Gr, hBN). Graphene uses Gr + monolayer; hBN uses thickness bins like 0-10/10-20/20-30/30-40 (not monolayer/bilayer).", "nullable": True},
    }
    output_type = "string"

    PIXEL_SIZE_UM_MAP = {
        10: 0.365,
        20: 0.182,
        100: 0.036,
    }
    DEFAULT_MIN_AREA_PX_BY_MAG = {
        10: 200,
        20: 200,
        100: 2000,
    }
    DEFAULT_MAX_AREA_PX_BY_MAG = {
        10: 100000,
        20: 100000,
        100: 10000000,
    }
    STEPS_PER_MM = 12800.0
    CENTER_X = 1780
    CENTER_Y = 1180
    DEFAULT_CHIP_DIMS_INCH = [0.5, 0.5]
    GRAPHITE_TARGET_LABELS = ("manylayer",)
    HBN_TARGET_LABELS = ("0-10", "10-20", "20-30", "30-40")
    HBN_TARGETS_PER_LABEL = 4

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stage_set_dir = AtomSampleStageSetDirection(*args, **kwargs)
        self.stage_set_home = AtomSampleStageSetHome(*args, **kwargs)
        self.stage_set_dims = AtomSampleStageSetChipDims(*args, **kwargs)
        self.stage_get_pos = AtomSampleStageGetPosition(*args, **kwargs)
        self.stage_move_home = AtomSampleStageMoveHome(*args, **kwargs)
        self.stage_move_to = AtomSampleStageMoveTo(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.cam_autofocus_on = AtomCameraAutofocusOn(*args, **kwargs)
        self.cam_autofocus_off = AtomCameraAutofocusOff(*args, **kwargs)
        self.cam_autofocus_once = AtomCameraAutofocusOnce(*args, **kwargs)
        self.cam_get_focus_pos = AtomCameraGetFocusPosition(*args, **kwargs)
        self.cam_set_focus_pos = AtomCameraSetFocusPosition(*args, **kwargs)
        self.cam_wait_focus_photo = AtomCameraWaitFocusAndTakePhoto(*args, **kwargs)
        self.cam_scan_focus_photo = AtomCameraScanFocusAndTakePhoto(*args, **kwargs)
        self.cam_is_running = AtomCameraIsRunning(*args, **kwargs)
        self.turret_rotate = AtomTurretRotate(*args, **kwargs)
        self.focus_move = AtomFocusMoveRelative(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    PLAN_STEPS = [
        "Initialize Setup & Observers",
        "Snake Scan (10X)",
        "Process & Analyze Targets",
        "Revisit Graphene then Graphite Targets",
        "Save to Database"
    ]

    def _pixel_to_stage(self, frame_center_xy, flake_px, magnification: int = 10):
        x_pix, y_pix = flake_px
        dx_pix = y_pix - self.CENTER_X
        dy_pix = x_pix - self.CENTER_Y
        px_size_um = self.PIXEL_SIZE_UM_MAP.get(magnification, self.PIXEL_SIZE_UM_MAP[10])
        mm_per_pixel = px_size_um * 1e-3
        steps_per_pixel = mm_per_pixel * self.STEPS_PER_MM
        d_x = steps_per_pixel * dy_pix
        d_y = steps_per_pixel * dx_pix
        return np.array([frame_center_xy[0] + d_x, frame_center_xy[1] + d_y], dtype=float)

    def _min_area_for_mag(self, magnification: int, override: Optional[int]) -> int:
        if override is not None:
            return int(override)
        return self.DEFAULT_MIN_AREA_PX_BY_MAG.get(magnification, 200)

    def _max_area_for_mag(self, magnification: int, override: Optional[int]) -> int:
        if override is not None:
            return int(override)
        return self.DEFAULT_MAX_AREA_PX_BY_MAG.get(magnification, 100000)

    def _preferred_labels(self, normalized_material: str, purpose: str) -> List[str]:
        if normalized_material == "hBN":
            return list(self.HBN_TARGET_LABELS)
        if purpose == "graphite_target":
            return list(self.GRAPHITE_TARGET_LABELS) if normalized_material == "Gr" else []
        if purpose == "db" and normalized_material == "Gr":
            return ["monolayer", "bilayer", *self.GRAPHITE_TARGET_LABELS]
        if purpose in {"target", "db"}:
            return ["monolayer"]
        return ["monolayer", "bilayer"]

    def _keep_largest_per_frame(self, rows: List[dict]) -> List[dict]:
        best_by_frame: dict[int, dict] = {}
        for row in rows:
            try:
                frame_idx = int(row.get("frame_num", -1))
            except Exception:
                continue
            if frame_idx < 0:
                continue
            current = best_by_frame.get(frame_idx)
            if current is None or float(row.get("size_area_px", 0.0)) > float(current.get("size_area_px", 0.0)):
                best_by_frame[frame_idx] = row
        return sorted(
            best_by_frame.values(),
            key=lambda r: float(r.get("size_area_px", 0.0)),
            reverse=True,
        )

    def _load_result_candidates(self, result_txt: str) -> List[dict]:
        sidecar_path = result_txt + ".jsonl"
        if os.path.exists(sidecar_path):
            candidates = []
            with open(sidecar_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if "size_area_px" not in row or "frame_num" not in row:
                        continue
                    candidates.append(row)
            if candidates:
                return candidates

        if not os.path.exists(result_txt):
            return []
        with open(result_txt, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        if not lines:
            return []

        candidates = []
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if "size_area_px" not in row or "frame_num" not in row:
                continue
            candidates.append(row)
        if candidates:
            return candidates

        # Backward compatibility: fall back to the legacy 8-line results.txt format.
        def _split_numbers(s: str) -> List[str]:
            return [x for x in s.split(" ") if x]

        clean_lines = []
        for line in lines:
            if ":" in line:
                clean_lines.append(line.split(":", 1)[1].strip())
            else:
                clean_lines.append(line.strip())

        if len(clean_lines) < 8:
            return []

        try:
            mono_areas = [float(e) for e in _split_numbers(clean_lines[0])]
            bi_areas = [float(e) for e in _split_numbers(clean_lines[1])]
            mono_frame_nums = [int(float(e)) for e in _split_numbers(clean_lines[2])]
            bi_frame_nums = [int(float(e)) for e in _split_numbers(clean_lines[3])]
            mono_x = [float(e) for e in _split_numbers(clean_lines[4])]
            mono_y = [float(e) for e in _split_numbers(clean_lines[5])]
            bi_x = [float(e) for e in _split_numbers(clean_lines[6])]
            bi_y = [float(e) for e in _split_numbers(clean_lines[7])]
        except Exception:
            return []

        def _zip_legacy(
            areas: List[float],
            frame_nums: List[int],
            xs: List[float],
            ys: List[float],
            label: str,
        ) -> None:
            n = min(len(areas), len(frame_nums), len(xs), len(ys))
            for idx in range(n):
                candidates.append(
                    {
                        "size_area_px": float(areas[idx]),
                        "frame_num": int(frame_nums[idx]),
                        "x": float(xs[idx]),
                        "y": float(ys[idx]),
                        "thickness_label": label,
                    }
                )

        _zip_legacy(mono_areas, mono_frame_nums, mono_x, mono_y, "monolayer")
        _zip_legacy(bi_areas, bi_frame_nums, bi_x, bi_y, "bilayer")
        return candidates

    def _parse_results(
        self,
        result_txt: str,
        max_area: int,
        min_area: int,
        num_top: int,
        normalized_material: str,
        purpose: str = "target",
        candidate_group: Optional[str] = None,
        allowed_labels: Optional[List[str]] = None,
    ) -> List[dict]:
        candidates = self._load_result_candidates(result_txt)
        if not candidates:
            return []
        allowed = set(
            allowed_labels
            if allowed_labels is not None
            else self._preferred_labels(normalized_material, purpose=purpose)
        )

        filtered = []
        for row in candidates:
            try:
                area = float(row.get("size_area_px", 0.0))
                label = str(row.get("thickness_label", ""))
                if label not in allowed:
                    continue
                if not (area < max_area and area >= min_area):
                    continue
                filtered.append(row)
            except Exception:
                continue
        filtered.sort(key=lambda r: float(r.get("size_area_px", 0.0)), reverse=True)
        filtered = filtered[:num_top]

        if not hasattr(self.stage, "coords") or not self.stage.coords:
            return []
        stage_coords = np.array(self.stage.coords, dtype=float)
        targets = []
        for row in filtered:
            frame_idx = int(row["frame_num"])
            if frame_idx < 0 or frame_idx >= len(stage_coords):
                continue
            frame_center_xy = stage_coords[frame_idx, :2]
            flake_xy = self._pixel_to_stage(
                frame_center_xy,
                (float(row.get("x", 0.0)), float(row.get("y", 0.0))),
                magnification=10,
            )
            targets.append({
                "x": float(flake_xy[0]),
                "y": float(flake_xy[1]),
                "frame_num": int(frame_idx),
                "thickness_label": str(row.get("thickness_label", "")),
                "size_area_px": float(row.get("size_area_px", 0.0)),
                "candidate_group": candidate_group or purpose,
            })
        return targets

    def _select_revisit_targets(
        self,
        result_txt: str,
        max_area: int,
        min_area: int,
        num_primary: int,
        num_graphite: int,
        normalized_material: str,
    ) -> tuple[List[dict], List[dict]]:
        primary_group = "graphene" if normalized_material == "Gr" else normalized_material
        if normalized_material == "hBN":
            # Keep the hBN revisit set balanced across thickness categories.
            # Each category is independently ranked by segmented area so a
            # plentiful category cannot consume the slots of another one.
            primary_targets = []
            for thickness_label in self.HBN_TARGET_LABELS:
                primary_targets.extend(
                    self._parse_results(
                        result_txt,
                        max_area=max_area,
                        min_area=min_area,
                        num_top=self.HBN_TARGETS_PER_LABEL,
                        normalized_material=normalized_material,
                        purpose="target",
                        candidate_group=primary_group,
                        allowed_labels=[thickness_label],
                    )
                )
        else:
            primary_targets = self._parse_results(
                result_txt,
                max_area=max_area,
                min_area=min_area,
                num_top=max(0, int(num_primary)),
                normalized_material=normalized_material,
                purpose="target",
                candidate_group=primary_group,
            )
        graphite_targets: List[dict] = []
        if normalized_material == "Gr" and int(num_graphite) > 0:
            graphite_targets = self._parse_results(
                result_txt,
                max_area=max_area,
                min_area=min_area,
                num_top=int(num_graphite),
                normalized_material=normalized_material,
                purpose="graphite_target",
                candidate_group="graphite",
            )
        return primary_targets, graphite_targets

    def _parse_results_detailed(
        self,
        result_txt: str,
        max_area: int,
        min_area: int,
        num_top: int,
        normalized_material: str,
        magnification: int = 10,
        frame_centers: Optional[List[List[float]]] = None,
    ) -> List[dict]:
        """Like _parse_results but returns rich dicts with size and type info.

        Each dict contains:
            x, y: stage coordinates (steps)
            size_area_px: pixel area
            thickness_label: segmenter thickness label (material-dependent)
        """
        candidates = self._load_result_candidates(result_txt)
        if not candidates:
            return []
        allowed = set(self._preferred_labels(normalized_material, purpose="db"))
        filtered = []
        for row in candidates:
            try:
                area = float(row.get("size_area_px", 0.0))
                label = str(row.get("thickness_label", ""))
                if label not in allowed:
                    continue
                if not (area < max_area and area >= min_area):
                    continue
                filtered.append(row)
            except Exception:
                continue
        filtered = self._keep_largest_per_frame(filtered)
        filtered = filtered[:num_top]

        if frame_centers is not None:
            stage_coords = np.array(frame_centers, dtype=float)
        else:
            if not hasattr(self.stage, "coords") or not self.stage.coords:
                return []
            stage_coords = np.array(self.stage.coords, dtype=float)
        detailed = []
        recorded_positions = self._load_recorded_stage_positions(result_txt)
        for row in filtered:
            frame_idx = int(row["frame_num"])
            if frame_idx < 0 or frame_idx >= len(stage_coords):
                continue
            frame_center_xy = stage_coords[frame_idx, :2]
            flake_xy = self._pixel_to_stage(
                frame_center_xy,
                (float(row.get("x", 0.0)), float(row.get("y", 0.0))),
                magnification=magnification,
            )
            stage_x = float(flake_xy[0])
            stage_y = float(flake_xy[1])
            if frame_idx in recorded_positions:
                stage_x, stage_y = recorded_positions[frame_idx]
            detailed.append({
                "x": stage_x,
                "y": stage_y,
                "size_area_px": float(row.get("size_area_px", 0.0)),
                "thickness_label": str(row.get("thickness_label", "")),
                "frame_num": int(frame_idx),
            })

        return detailed

    def _parse_capture_result(self, message: str) -> tuple[Optional[str], Optional[float]]:
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
        elif "capture:" in message:
            try:
                after = message.split("capture:", 1)[1]
                path = after.split("(score=", 1)[0].strip().strip(".")
            except Exception:
                path = None
        if "score=" in message:
            try:
                score_part = message.split("score=", 1)[1]
                score_text = score_part.split(")", 1)[0]
                score = float(score_text)
            except Exception:
                score = None
        return path, score

    def _parse_stage_position_message(self, message: str) -> tuple[Optional[float], Optional[float]]:
        if not message:
            return None, None
        match_x = re.search(r"X=([-+]?\d+(?:\.\d+)?)", message)
        match_y = re.search(r"Y=([-+]?\d+(?:\.\d+)?)", message)
        if not match_x or not match_y:
            return None, None
        try:
            return float(match_x.group(1)), float(match_y.group(1))
        except Exception:
            return None, None

    def _append_refind_stage_position(
        self,
        result_txt: str,
        frame_num: int,
        revisit_index: int,
        stage_x: float,
        stage_y: float,
        source_frame_num: Optional[int] = None,
        candidate_group: Optional[str] = None,
        source_thickness_label: Optional[str] = None,
    ) -> None:
        payload = {
            "record_type": "refind_stage_position",
            "frame_num": int(frame_num),
            "revisit_index": int(revisit_index),
            "stage_x": float(stage_x),
            "stage_y": float(stage_y),
        }
        if source_frame_num is not None:
            payload["source_frame_num"] = int(source_frame_num)
        if candidate_group:
            payload["candidate_group"] = str(candidate_group)
        if source_thickness_label:
            payload["source_thickness_label"] = str(source_thickness_label)
        with open(result_txt, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    def _load_recorded_stage_positions(self, result_txt: str) -> dict[int, tuple[float, float]]:
        if not os.path.exists(result_txt):
            return {}
        positions: dict[int, tuple[float, float]] = {}
        with open(result_txt, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict) or row.get("record_type") != "refind_stage_position":
                    continue
                try:
                    frame_num = int(row["frame_num"])
                    stage_x = float(row["stage_x"])
                    stage_y = float(row["stage_y"])
                except Exception:
                    continue
                positions[frame_num] = (stage_x, stage_y)
        return positions

    def _parse_int_from_message(self, message: str) -> Optional[int]:
        if not message:
            return None
        try:
            import re
            match = re.search(r"-?\d+", message)
            if match:
                return int(match.group(0))
        except Exception:
            return None
        return None

    def _parse_bool_from_message(self, message: str) -> Optional[bool]:
        if not message:
            return None
        lower = message.lower()
        if "true" in lower:
            return True
        if "false" in lower:
            return False
        return None

    # Freedom mode (currently disabled):
    # def _normalize_chip_dims(self, chip_dims_inch: Optional[list[float]]) -> list[float]:
    #     # Expected source: assembly flow converts DB width_mm/height_mm -> inches.
    #     if not chip_dims_inch:
    #         return self.DEFAULT_CHIP_DIMS_INCH.copy()
    #     try:
    #         long_edge = float(chip_dims_inch[0])
    #         short_edge = float(chip_dims_inch[1])
    #     except (TypeError, ValueError, IndexError):
    #         return self.DEFAULT_CHIP_DIMS_INCH.copy()
    #     if long_edge <= 0 or short_edge <= 0:
    #         return self.DEFAULT_CHIP_DIMS_INCH.copy()
    #     return [long_edge, short_edge]

    def forward(
        self,
        angle: float = 0,
        run_dir: Optional[str] = None,
        # Freedom mode (currently disabled):
        # chip_dims_inch: Optional[list[float]] = None,
        open_live_view: bool = True,
        a1: float = 18.1367,
        a2: float = 4.4029,
        focus_ref_pos: int = 330,
        pre_scan_wait_s: float = 3.0,
        turret_settle_s: float = 1.0,
        min_blur_10x: float = 6.5,
        min_blur_20x: float = 5.5,
        min_blur_100x: float = 5.0,
        num_top_matches: int = 15,
        num_top_graphite_matches: int = 5,
        max_area: Optional[int] = None,
        min_area: Optional[int] = None,
        chip_id: Optional[int] = None,
        material_type: Optional[str] = None,
    ) -> str:
        if self.stage is None or self.camera is None or self.turret is None or self.focus is None:
            return "❌ Required hardware (stage/turret/focus/camera) unavailable."

        chip_dims_inch = self.DEFAULT_CHIP_DIMS_INCH.copy()
        # Freedom mode (currently disabled):
        # chip_dims_inch = self._normalize_chip_dims(chip_dims_inch)

        current = os.getcwd()
        base_photo_dir = os.path.join(current, "images", "autosearch")
        os.makedirs(base_photo_dir, exist_ok=True)
        if run_dir:
            photo_dir = os.path.abspath(run_dir)
        else:
            existing = []
            for name in os.listdir(base_photo_dir):
                match = re.fullmatch(r"run_(\d+)", name)
                if match:
                    existing.append(int(match.group(1)))
            next_run = max(existing, default=0) + 1
            photo_dir = os.path.join(base_photo_dir, f"run_{next_run}")

        long_edge, short_edge = float(chip_dims_inch[0]), float(chip_dims_inch[1])
        result_txt = os.path.join(photo_dir, f"results_{long_edge}_{short_edge}.txt")
        result_txt_20 = os.path.join(photo_dir, f"results_{long_edge}_{short_edge}_20x.txt")
        result_txt_100 = os.path.join(photo_dir, f"results_{long_edge}_{short_edge}_100x.txt")
        m10_dir = os.path.join(photo_dir, "m_10")
        result_dir = os.path.join(photo_dir, "segmented_m_10")
        m10_results_dir = os.path.join(photo_dir, "m_10_results")
        m20_results_dir = os.path.join(photo_dir, "m_20_results")
        m100_results_dir = os.path.join(photo_dir, "m_100_results")
        m100_segmented_dir = os.path.join(photo_dir, "segmented_m_100")
        m20_segmented_dir = os.path.join(photo_dir, "segmented_m_20")
        os.makedirs(photo_dir, exist_ok=True)
        os.makedirs(m10_dir, exist_ok=True)
        os.makedirs(result_dir, exist_ok=True)
        os.makedirs(m10_results_dir, exist_ok=True)
        os.makedirs(m20_results_dir, exist_ok=True)
        os.makedirs(m100_results_dir, exist_ok=True)
        os.makedirs(m100_segmented_dir, exist_ok=True)
        os.makedirs(m20_segmented_dir, exist_ok=True)
        clear_results(result_txt)
        clear_results(result_txt_20)
        clear_results(result_txt_100)

        results = []
        results.append(f"Starting Molecule AutoSearch in {photo_dir}")
        # Ensure frame indices in result files are local to this scan run.
        # LiveSegment parses indices from filenames like test_<counter>.jpg.
        try:
            if hasattr(self.camera, "counter"):
                self.camera.counter = 0
                results.append("Reset camera frame counter for this scan run.")
        except Exception as exc:
            results.append(f"⚠️ Failed to reset camera frame counter: {exc}")
        self.broadcaster_plan.forward(self.PLAN_STEPS)
        
        self.broadcaster_step.forward(0)

        def _coerce_min_blur(value: Optional[float], default: float) -> float:
            if value is None:
                return default
            try:
                v = float(value)
            except Exception:
                return default
            if not np.isfinite(v) or v <= 0:
                return default
            return v

        a1_val = float(a1) if a1 is not None else 18.1367
        a2_val = float(a2) if a2 is not None else 4.4029
        ref_pos = int(focus_ref_pos) if focus_ref_pos is not None else 330
        # Resolve material used by live segmenter.
        resolved_material_type = material_type
        if not resolved_material_type and chip_id is not None:
            try:
                chip_record = ChipInventoryRepository().get_chip_by_id(int(chip_id))
                if chip_record and chip_record.material_type:
                    resolved_material_type = chip_record.material_type
            except Exception:
                pass
        normalized_material = normalize_material_code(resolved_material_type)
        if not normalized_material:
            return (
                "❌ AutoSearch requires an explicit material type. "
                f"chip_id={chip_id} did not resolve to a valid material_type, and no material_type was provided."
            )
        blur_10x = _coerce_min_blur(min_blur_10x, 6.5)
        blur_20x = _coerce_min_blur(min_blur_20x, 5.5)
        blur_100x = _coerce_min_blur(min_blur_100x, 5.0)
        settle_s = float(turret_settle_s) if turret_settle_s is not None else 1.0
        top_n = int(num_top_matches) if num_top_matches is not None else 15
        graphite_top_n = (
            int(num_top_graphite_matches)
            if num_top_graphite_matches is not None
            else 5
        )
        top_n = max(0, top_n)
        graphite_top_n = max(0, graphite_top_n)
        min_area_10x = self._min_area_for_mag(10, min_area)
        min_area_100x = self._min_area_for_mag(100, min_area)
        max_area_10x = self._max_area_for_mag(10, max_area)
        max_area_100x = self._max_area_for_mag(100, max_area)

        try:
            start_observer(
                mag=10,
                photo_dir=m10_dir,
                result_dir=result_dir,
                result_txt=result_txt,
                material_type=normalized_material,
                key="m10",
            )
            start_observer(
                mag=100,
                photo_dir=m100_results_dir,
                result_dir=m100_segmented_dir,
                result_txt=result_txt_100,
                material_type=normalized_material,
                key="m100",
            )
            # The 20X revisit is the real-flake design mask source. Use its
            # native edge profile; the 100X profile can merge faint 20X flakes
            # into the substrate because its smaller edge-method set does not
            # close their boundaries.
            start_observer(
                mag=20,
                photo_dir=m20_results_dir,
                result_dir=m20_segmented_dir,
                result_txt=result_txt_20,
                material_type=normalized_material,
                key="m20",
            )
        except Exception as exc:
            results.append(f"⚠️ observer startup failed: {exc}")

        try:
            results.append(self.turret_rotate(position=2))
        except Exception:
            pass

        try:
            if hasattr(self.stage, "set_scan_max_speed"):
                self.stage.set_scan_max_speed(5000, 5000)
            else:
                self.stage.set_max_speed(5000, 5000)
        except Exception:
            pass

        results.append(self.stage_set_dir(0))
        results.append(self.stage_set_home())
        results.append(self.stage_set_dims(long_edge, short_edge))
        results.append(self.cam_start(open_live_view=open_live_view))

        if a1 is not None:
            try:
                results.append(self.cam_autofocus_off())
                try:
                    results.append(self.cam_autofocus_once())
                except Exception as exc:
                    results.append(f"⚠️ autofocus_once failed: {exc}")
                time.sleep(float(pre_scan_wait_s) if pre_scan_wait_s is not None else 3.0)
                s0_msg = self.cam_get_focus_pos()
                s0 = self._parse_int_from_message(s0_msg)
                print(f"DEBUG: pre-scan autofocus position: {s0}")
                if s0 is None:
                    raise RuntimeError("Failed to parse focus position.")
                # results.append(self.cam_set_focus_pos(pos=ref_pos))
                # dz0 = float(a1_val) * (float(s0) - float(ref_pos))
                # time.sleep(abs(dz0) * 0.005)
                # if abs(dz0) > 0:
                #     results.append(self.focus_move(delta=dz0, wait=True))
                # s0_msg = self.cam_get_focus_pos()
                # s0_after = self._parse_int_from_message(s0_msg)
                # print(f"DEBUG: post-mapping autofocus position: {s0_after}")
            except Exception as exc:
                results.append(f"⚠️ pre-scan focus mapping failed: {exc}")

        try:
            results.append(self.cam_autofocus_on())
        except Exception as exc:
            results.append(f"⚠️ autofocus_on failed: {exc}")

        def _capture():
            results.append(self.cam_autofocus_on())
            msg = self.cam_wait_focus_photo(photo_dir=m10_dir, min_blur=blur_10x)
            path, score = self._parse_capture_result(msg)
            if path is None:
                results.append(f"[10X] capture: {msg}")
                return
            if score is None:
                results.append(f"[10X] capture: {path}")
                return
            results.append(f"[10X] capture: {path} (score={score:.2f})")
        
        def _setfocus():
            results.append(self.cam_set_focus_pos(pos=330))

        try:
            self.broadcaster_step.forward(1)
            self.stage.start_snake(sf=_setfocus, wf=_capture, recompute_path=True)
        except KeyboardInterrupt:
            results.append("[10X] Snake scan interrupted by user.")

        results.append(self.stage_move_home())
        results.append(self.cam_stop())

        scan_filenames = [
            name for name in os.listdir(m10_dir)
            if os.path.isfile(os.path.join(m10_dir, name))
        ]
        try:
            scan_segmentation_failures = wait_for_observer_files(
                "m10",
                scan_filenames,
                timeout=300.0,
                continue_on_error=True,
            )
            completed_count = len(scan_filenames) - len(scan_segmentation_failures)
            results.append(
                f"[10X] Segmentation complete for {completed_count}/{len(scan_filenames)} scan images."
            )
            for failure in scan_segmentation_failures:
                results.append(
                    f"⚠️ [10X] Skipping failed segmentation {failure['filename']}: {failure['error']}"
                )
        except Exception as exc:
            results.append(f"❌ 10X segmentation did not complete: {exc}")
            for observer_key in ("m10", "m20", "m100"):
                try:
                    stop_observer(observer_key)
                except Exception:
                    pass
            return "\n".join(results)

        self.broadcaster_step.forward(2)
        try:
            sort_results(result_txt)
        except Exception:
            pass
        # Parse results with filtering
        primary_targets, graphite_targets = self._select_revisit_targets(
            result_txt,
            max_area=max_area_10x,
            min_area=min_area_10x,
            num_primary=top_n,
            num_graphite=graphite_top_n,
            normalized_material=normalized_material,
        )
        targets = primary_targets + graphite_targets
        if normalized_material == "Gr":
            results.append(
                f"Found {len(primary_targets)} graphene targets and "
                f"{len(graphite_targets)} graphite targets."
            )
        elif normalized_material == "hBN":
            category_counts = {
                label: sum(
                    target.get("thickness_label") == label
                    for target in primary_targets
                )
                for label in self.HBN_TARGET_LABELS
            }
            category_summary = ", ".join(
                f"{label}={category_counts[label]}"
                for label in self.HBN_TARGET_LABELS
            )
            results.append(
                f"Found {len(primary_targets)} hBN targets "
                f"(top {self.HBN_TARGETS_PER_LABEL} per category: {category_summary})."
            )
        else:
            results.append(f"Found {len(primary_targets)} targets.")
        target_by_revisit_index = {
            index + 1: target for index, target in enumerate(targets)
        }

        if not targets:
            results.append("❌ No flakes found. AutoSearch finish.")
            if chip_id is not None:
                try:
                    ChipInventoryRepository().set_chip_searched(int(chip_id), is_searched=True)
                    results.append(f"📝 Marked chip {chip_id} as searched.")
                except Exception as exc:
                    results.append(f"⚠️ Failed to mark chip as searched: {exc}")
            try:
                stop_observer("m10")
                stop_observer("m20")
                stop_observer("m100")
            except Exception:
                pass
            return "\n".join(results)

        running_msg = self.cam_is_running()
        camera_was_running = bool(self._parse_bool_from_message(running_msg))
        if not camera_was_running:
            results.append(self.cam_start(open_live_view=open_live_view))

        try:
            segmentation_failures = []
            try:
                results.append(self.cam_autofocus_off())
            except Exception:
                pass

            self.broadcaster_step.forward(3)
            for i, target_info in enumerate(targets):
                x, y, f_idx = target_info["x"], target_info["y"], target_info["frame_num"]
                revisit_idx = i + 1
                candidate_group = str(target_info.get("candidate_group", "target"))
                source_thickness_label = str(target_info.get("thickness_label", ""))
                results.append(self.stage_move_to(x=float(x), y=float(y), wait=True))

                try:
                    results.append(self.turret_rotate(position=2))
                    time.sleep(settle_s)
                except Exception as exc:
                    results.append(f"[WARN] Turret rotate_to_position(2) failed: {exc}")

                try:
                    results.append(self.cam_autofocus_once())
                except Exception as exc:
                    results.append(f"[WARN] camera.autofocus_once() failed at 10X: {exc}")
                time.sleep(3.0)

                save10 = os.path.join(m10_results_dir, f"flake_{revisit_idx}_10.jpg")
                msg10 = self.cam_wait_focus_photo(
                    photo_dir=m10_results_dir,
                    save_path=save10,
                    min_blur=blur_10x,
                )
                abs10, _score10 = self._parse_capture_result(msg10)
                results.append(f"[10X] {candidate_group} target {i} (frame {f_idx}, revisit {revisit_idx}): {abs10 or msg10}")

                try:
                    s1_msg = self.cam_get_focus_pos()
                    s1 = self._parse_int_from_message(s1_msg)
                    if s1 is None:
                        raise RuntimeError("Failed to parse focus position.")
                    results.append(self.cam_set_focus_pos(pos=ref_pos))
                    dz1 = float(a1_val) * (float(s1) - float(ref_pos))
                    if s1 >=100 and s1 <=700:
                        results.append(self.focus_move(delta=dz1, wait=True))
                        time.sleep(abs(dz1) * 0.005)
                    else:
                        results.append("out of focus, skipping focus move")
                except Exception as exc:
                    results.append(f"[WARN] 10X focus-position mapping failed: {exc}")

                try:
                    results.append(self.turret_rotate(position=3))
                    time.sleep(settle_s)
                except Exception as exc:
                    results.append(f"[WARN] Turret rotate_to_position(3) failed: {exc}")

                try:
                    results.append(self.cam_autofocus_once())
                except Exception as exc:
                    results.append(f"[WARN] camera.autofocus_once() failed at 20X: {exc}")
                time.sleep(3.0)

                save20 = os.path.join(m20_results_dir, f"flake_{revisit_idx}_20.jpg")
                msg20 = self.cam_wait_focus_photo(
                    photo_dir=m20_results_dir,
                    save_path=save20,
                    min_blur=blur_20x,
                )
                abs20, _score20 = self._parse_capture_result(msg20)
                results.append(f"[20X] Target {i} (frame {f_idx}, revisit {revisit_idx}): {abs20 or msg20}")
                try:
                    wait_for_observer_file("m20", os.path.basename(save20), timeout=180.0)
                    results.append(f"[20X] Segmentation complete: {os.path.join(m20_segmented_dir, os.path.basename(save20))}")
                except Exception as exc:
                    failure = f"20X flake {revisit_idx}: {exc}"
                    segmentation_failures.append(failure)
                    results.append(f"⚠️ {failure}")

                try:
                    s2_msg = self.cam_get_focus_pos()
                    s2 = self._parse_int_from_message(s2_msg)
                    if s2 is None:
                        raise RuntimeError("Failed to parse focus position.")
                    results.append(self.cam_set_focus_pos(pos=ref_pos))
                    dz2 = float(a2_val) * (float(s2) - float(ref_pos))
                    if s2 >=100 and s2 <=700:
                        results.append(self.focus_move(delta=dz2, wait=True))
                        time.sleep(abs(dz1) * 0.005)
                    else:
                        results.append("out of focus, skipping focus move")
                except Exception as exc:
                    results.append(f"[WARN] 20X focus-position mapping failed: {exc}")

                try:
                    results.append(self.turret_rotate(position=4))
                    time.sleep(2.0)
                except Exception as exc:
                    results.append(f"[WARN] Turret rotate_to_position(4) failed: {exc}")

                try:
                    results.append(self.cam_autofocus_off())
                except Exception:
                    pass

                save100 = os.path.join(m100_results_dir, f"flake_{revisit_idx}_100.jpg")
                msg100 = self.cam_scan_focus_photo(
                    photo_dir=m100_results_dir,
                    save_path=save100,
                    min_blur=blur_100x,
                )
                abs100, _score100 = self._parse_capture_result(msg100)
                results.append(f"[100X] Target {i} (frame {f_idx}, revisit {revisit_idx}): {abs100 or msg100}")
                try:
                    wait_for_observer_file("m100", os.path.basename(save100), timeout=180.0)
                    results.append(f"[100X] Segmentation complete: {os.path.join(m100_segmented_dir, os.path.basename(save100))}")
                except Exception as exc:
                    failure = f"100X flake {revisit_idx}: {exc}"
                    segmentation_failures.append(failure)
                    results.append(f"⚠️ {failure}")
                try:
                    stage_pos_msg = self.stage_get_pos()
                    stage_x, stage_y = self._parse_stage_position_message(stage_pos_msg)
                    if stage_x is None or stage_y is None:
                        raise RuntimeError(stage_pos_msg)
                    self._append_refind_stage_position(
                        result_txt_100,
                        frame_num=revisit_idx,
                        revisit_index=revisit_idx,
                        stage_x=stage_x,
                        stage_y=stage_y,
                        source_frame_num=f_idx,
                        candidate_group=candidate_group,
                        source_thickness_label=source_thickness_label,
                    )
                    results.append(
                        f"[100X] Recorded final stage position for flake {revisit_idx}: X={stage_x:.3f}, Y={stage_y:.3f}"
                    )
                except Exception as exc:
                    results.append(f"[WARN] Failed to record final stage position for flake {revisit_idx}: {exc}")

                try:
                    results.append(self.turret_rotate(position=2))
                    time.sleep(settle_s)
                except Exception as exc:
                    results.append(f"[WARN] Turret rotate_to_position(2) failed: {exc}")

                results.append(
                    f"[DONE] flake #{revisit_idx} (source frame {f_idx}) saved: 10X={abs10}, 20X={abs20}, 100X={abs100}"
                )

            if segmentation_failures:
                results.append(
                    f"⚠️ Segmentation completed with {len(segmentation_failures)} failed revisit image(s)."
                )

        finally:
            try:
                stop_observer("m10")
                stop_observer("m20")
                stop_observer("m100")
            except Exception:
                pass
            try:
                results.append(self.turret_rotate(position=2))
            except Exception:
                pass
            try:
                results.append(self.stage_move_home())
            except Exception:
                pass
            try:
                results.append(self.cam_autofocus_once())
            except Exception:
                pass
            try:
                s3_msg = self.cam_get_focus_pos()
                s3 = self._parse_int_from_message(s3_msg)
                if s3 is None:
                    raise RuntimeError("Failed to parse focus position.")
                results.append(self.cam_set_focus_pos(pos=ref_pos))
                dz3 = float(a1_val) * (float(s3) - float(ref_pos))
                if abs(dz3) > 0:
                    results.append(self.focus_move(delta=dz3, wait=True))
                    time.sleep(abs(dz3) * 0.005)
            except Exception as exc:
                results.append(f"[WARN] 10X focus-position mapping failed: {exc}")            
            if not camera_was_running:
                try:
                    results.append(self.cam_stop())
                except Exception:
                    pass

        results.append(f"AutoSearch complete. results={result_txt} targets={len(targets)}")

        # ----- Persist detected flakes to the database -----
        self.broadcaster_step.forward(4)
        if chip_id is not None:
            try:
                flake_service = FlakeInventoryService()
                
                # Resolve material_type from chip if not provided
                type_label = resolved_material_type
                if not type_label:
                    chip_repo = ChipInventoryRepository()
                    chip_record = chip_repo.get_chip_by_id(chip_id)
                    if chip_record and chip_record.material_type:
                        type_label = chip_record.material_type
                    else:
                        type_label = "unknown"

                # Re-parse with detailed info (sizes + thickness labels)
                detailed = self._parse_results_detailed(
                    result_txt_100,
                    max_area=max_area_100x,
                    min_area=min_area_100x,
                    num_top=len(targets),
                    normalized_material=normalized_material,
                    magnification=100,
                )

                saved_count = 0
                db_allowed_labels = set(self._preferred_labels(normalized_material, purpose="db"))
                for j, flake_info in enumerate(detailed):
                    try:
                        if flake_info["thickness_label"] not in db_allowed_labels:
                            results.append(
                                f"ℹ️ Skipping flake #{j} (thickness={flake_info['thickness_label']}) - not in allowed labels."
                            )
                            continue

                        # Revisit captures are named by sequential flake index, which is
                        # carried through the 100X segmenter results as frame_num.
                        f_idx = flake_info["frame_num"]
                        img_100 = os.path.join(m100_results_dir, f"flake_{f_idx}_100.jpg")
                        img_20 = os.path.join(m20_results_dir, f"flake_{f_idx}_20.jpg")
                        img_10 = os.path.join(m10_results_dir, f"flake_{f_idx}_10.jpg")
                        mask_100 = os.path.join(m100_segmented_dir, f"flake_{f_idx}_100.jpg")
                        mask_20 = os.path.join(m20_segmented_dir, f"flake_{f_idx}_20.jpg")
                        image_path = img_100 if os.path.exists(img_100) else (
                            img_10 if os.path.exists(img_10) else None
                        )
                        image_20x_path = img_20 if os.path.exists(img_20) else None
                        mask_path = mask_100 if os.path.exists(mask_100) else None
                        mask_20x_path = mask_20 if os.path.exists(mask_20) else None
                        segmenter_thickness_label = flake_info["thickness_label"]
                        source_target = target_by_revisit_index.get(f_idx, {})
                        saved_type_label = "Gr" if normalized_material == "Gr" else type_label
                        saved_thickness_label = (
                            "monolayer"
                            if normalized_material == "Gr" and segmenter_thickness_label == "monolayer"
                            else segmenter_thickness_label
                        )

                        flake_service.record_flake(
                            chip_id=chip_id,
                            type_label=saved_type_label,
                            thickness_label=saved_thickness_label,
                            location_x=flake_info["x"],
                            location_y=flake_info["y"],
                            size_area_px=flake_info["size_area_px"],
                            magnification=100,
                            image_path=image_path,
                            image_20x_path=image_20x_path,
                            mask_path=mask_path,
                            mask_20x_path=mask_20x_path,
                            metadata={
                                "run_dir": photo_dir,
                                "result_txt": result_txt_100,
                                "flake_index": j,
                                "frame_num": f_idx,
                                "segmenter_thickness_label": segmenter_thickness_label,
                                "candidate_group": source_target.get("candidate_group"),
                                "source_10x_thickness_label": source_target.get("thickness_label"),
                            },
                        )
                        saved_count += 1
                    except Exception as exc:
                        results.append(f"⚠️ Failed to save flake #{j}: {exc}")

                results.append(f"💾 Saved {saved_count}/{len(detailed)} flakes to database.")
            except Exception as exc:
                results.append(f"⚠️ Flake DB write failed: {exc}")
            try:
                ChipInventoryRepository().set_chip_searched(int(chip_id), is_searched=True)
                results.append(f"📝 Marked chip {chip_id} as searched.")
            except Exception as exc:
                results.append(f"⚠️ Failed to mark chip as searched: {exc}")
        else:
            results.append("ℹ️ chip_id not provided — skipping flake DB write.")

        return "\n".join(results)
