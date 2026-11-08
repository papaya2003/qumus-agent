from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, List, Literal, Sequence, Tuple

import cv2
import numpy as np
import pyboof as pb

from common.services import ChipInventoryService, RawMaterialCarrierService, StampService
from database.db_utils import init_db
from .stage_geometry import SlideMetric, compute_slide_metric
from .qr_utils import read_micro_qr_exhaustive


@dataclass(slots=True)
class GlassSlideDetectionConfig:
    table_side: Literal["left", "right"] = "left"
    expected_distance: float = 146.5
    min_distance: float = 120.0
    max_distance: float = 160.0
    angle_tolerance_deg: float = 60.0
    conf_penalty_scale: float = 40.0
    mirror_expected_direction: bool = False
    qr_labels: tuple[str, ...] = (
        "qr",
        "qr_code",
        "qr_marker",
        "glass slide micro qr",
        "micro qr",
    )
    pickup_labels: tuple[str, ...] = (
        "pickup",
        "pickup_location",
        "pickup_marker",
        "orange square",
        "orange sqaure",
    )
    exclude_labels: tuple[str, ...] = ("ignore", "background")
    overlay_output_path: Path | None = None
    csv_output_path: Path | None = None
    enable_qr_reading: bool = True
    update_database: bool = True
    frame_interval: int = 1
    qr_class_ids: tuple[int, ...] = (1,)
    pickup_class_ids: tuple[int, ...] = (2,)
    exclude_class_ids: tuple[int, ...] = (0, 3, 4, 5)
    rotation_offset_deg: float = 0.0
    pickup_offset_along_mm: float = 0.0
    pickup_offset_cross_mm: float = 0.0
    slide_to_pickup_offset_deg: float = 90.0
    record_raw_material_carriers: bool = True
    raw_material_source_label: str = "live_yolo"
    record_stamps: bool = True
    draw_excluded_detections: bool = True

    def __post_init__(self) -> None:
        side = (self.table_side or "left").lower()
        if side not in ("left", "right"):
            raise ValueError("table_side must be 'left' or 'right'")
        if side == "right":
            if self.qr_class_ids == (1,):
                self.qr_class_ids = (0,)
            if self.pickup_class_ids == (2,):
                self.pickup_class_ids = (1,)
            if self.exclude_class_ids == (0, 3, 4, 5):
                self.exclude_class_ids = (2, 3, 4, 5, 6)

    @classmethod
    def for_side(cls, side: str, **overrides: Any) -> "GlassSlideDetectionConfig":
        """Convenience helper for selecting left/right defaults."""
        normalized = side.lower()
        if normalized not in ("left", "right"):
            raise ValueError("side must be 'left' or 'right'")
        return cls(table_side=normalized, **overrides)


@dataclass(slots=True)
class GlassSlideObservation:
    label: str
    class_id: int | None
    polygon: np.ndarray
    center: tuple[int, int]
    bbox: tuple[int, int, int, int]
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SlideResult:
    slide_index: int
    qr_center_px: tuple[int, int]
    pickup_center_px: tuple[int, int]
    qr_bbox: tuple[int, int, int, int]
    pickup_bbox: tuple[int, int, int, int]
    midpoint_px: tuple[int, int]
    slide_angle_deg: float
    rotate_angle_deg: float
    qr_message: str
    match_score: float
    qr_confidence: float
    match_distance_px: float
    expected_error_px: float
    match_angle_diff_deg: float
    qr_orientation_deg: float
    alignment: float
    metric: SlideMetric

    @property
    def qr_center_mm(self) -> tuple[float, float]:
        return self.metric.qr_center_mm

    @property
    def pickup_center_mm(self) -> tuple[float, float]:
        return self.metric.pickup_center_mm


@dataclass(slots=True)
class GlassSlideDetectionOutput:
    slides: list[SlideResult] = field(default_factory=list)
    overlay_path: Path | None = None
    csv_path: Path | None = None


class GlassSlideDetector:
    def __init__(
        self,
        config: GlassSlideDetectionConfig | None = None,
        *,
        chip_repository: ChipInventoryService | None = None,
        raw_material_service: RawMaterialCarrierService | None = None,
        stamp_service: StampService | None = None,
    ):
        self.config = config or GlassSlideDetectionConfig()
        if self.config.update_database or self.config.record_raw_material_carriers or self.config.record_stamps:
            init_db()
        if self.config.record_raw_material_carriers:
            self.raw_carrier_service = raw_material_service or RawMaterialCarrierService()
        else:
            self.raw_carrier_service = None
        if self.config.record_stamps:
            self.stamp_service = stamp_service or StampService()
        else:
            self.stamp_service = None
        self._qr_detector: pb.fiducial.MicroQR | None = None
        self._frame_counter = 0
        self._qr_label_set = {label.lower() for label in self.config.qr_labels}
        self._pickup_label_set = {label.lower() for label in self.config.pickup_labels}
        self._exclude_label_set = {label.lower() for label in self.config.exclude_labels}
        self._qr_class_set = set(self.config.qr_class_ids)
        self._pickup_class_set = set(self.config.pickup_class_ids)
        self._exclude_class_set = set(self.config.exclude_class_ids)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def process_frame(
        self,
        frame_bgr: np.ndarray,
        detections: Sequence[dict],
        *,
        frame_id: str | None = None,
        source: str | None = None,
    ) -> GlassSlideDetectionOutput | None:
        """Run glass slide pairing + QR decoding on a frame + detection list."""
        self._frame_counter += 1
        if self._frame_counter % max(1, self.config.frame_interval) != 0:
            return None

        if frame_bgr is None or not len(detections):
            return GlassSlideDetectionOutput()

        img_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

        observations = self._build_observations(detections)
        qr_obs = [o for o in observations if self._is_qr(o.label, o.class_id)]
        pickup_obs = [o for o in observations if self._is_pickup(o.label, o.class_id)]
        if not qr_obs or not pickup_obs:
            return GlassSlideDetectionOutput()

        matches = self._match(qr_obs, pickup_obs, img_gray)
        if not matches:
            return GlassSlideDetectionOutput()

        overlay = frame_bgr.copy()
        if self.config.draw_excluded_detections:
            self._draw_excluded_detections(overlay, detections)
        slides: list[SlideResult] = []
        for idx, match in enumerate(matches, 1):
            qr_obs_item = qr_obs[match["class0_idx"]]
            pickup_item = pickup_obs[match["class2_idx"]]
            qr_msg = "Not found"
            if self.config.enable_qr_reading:
                qr_msg = self._read_micro_qr(img_gray, qr_obs_item.bbox, idx)
            if qr_msg and qr_msg != "Not found":
                qr_obs_item.metadata["qr_message"] = qr_msg
            midpoint_px = (
                int(round((qr_obs_item.center[0] + pickup_item.center[0]) * 0.5)),
                int(round((qr_obs_item.center[1] + pickup_item.center[1]) * 0.5)),
            )
            metric = self._apply_metric_offsets(
                compute_slide_metric(
                    qr_obs_item.center,
                    pickup_item.center,
                    qr_obs_item.polygon,
                    pickup_item.polygon,
                    source=self.config.table_side,
                )
            )
            slide_angle_deg = self._compute_slide_angle(metric)
            rotate_angle_deg = self._compute_rotate_angle(metric, slide_angle_deg)
            slide = SlideResult(
                slide_index=idx,
                qr_center_px=qr_obs_item.center,
                pickup_center_px=pickup_item.center,
                qr_bbox=qr_obs_item.bbox,
                pickup_bbox=pickup_item.bbox,
                midpoint_px=midpoint_px,
                slide_angle_deg=round(slide_angle_deg, 2),
                rotate_angle_deg=round(rotate_angle_deg, 2),
                qr_message=qr_msg,
                match_score=match["score"],
                qr_confidence=match["qr_conf"],
                 match_distance_px=match["actual_distance"],
                 expected_error_px=match["expected_error_px"],
                    match_angle_diff_deg=match["angle_diff"],
                    qr_orientation_deg=match["qr_angle"],
                    alignment=match["alignment"],
                metric=metric,
            )
            slides.append(slide)

        self._draw_detection_annotations(overlay, observations)

        for slide in slides:
            cv2.line(overlay, slide.qr_center_px, slide.pickup_center_px, (0, 255, 255), 2)
            cv2.circle(overlay, slide.midpoint_px, 4, (0, 0, 255), -1)
            if slide.qr_message and slide.qr_message != "Not found":
                label = f"QR: {slide.qr_message}"[:32]
            else:
                label = f"Slide {slide.slide_index}"
            text_pos = (slide.qr_center_px[0], max(12, slide.qr_center_px[1] - 10))
            cv2.putText(overlay, label, text_pos, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)

        overlay_path = self._save_overlay(overlay)
        csv_path = self._save_csv(slides) if slides else None
        if self.config.update_database:
            self._update_database(slides, frame_id=frame_id, source=source)

        return GlassSlideDetectionOutput(slides=slides, overlay_path=overlay_path, csv_path=csv_path)

    # ------------------------------------------------------------------
    # Observation helpers
    # ------------------------------------------------------------------
    def _build_observations(self, detections: Sequence[dict]) -> list[GlassSlideObservation]:
        observations: list[GlassSlideObservation] = []
        for det in detections:
            label = str(det.get("class") or det.get("label") or "").strip()
            if not label:
                continue
            label_lower = label.lower()
            class_id = det.get("class_id")
            class_id_int = int(class_id) if class_id is not None else None
            if self._is_excluded(label_lower, class_id_int):
                continue
            polygon = self._polygon_from_detection(det)
            if polygon is None or len(polygon) < 4:
                continue
            polygon = np.array(polygon, dtype=np.int32)
            cx, cy = self._compute_better_center(polygon)
            x_min = int(np.min(polygon[:, 0]))
            y_min = int(np.min(polygon[:, 1]))
            x_max = int(np.max(polygon[:, 0]))
            y_max = int(np.max(polygon[:, 1]))
            observations.append(
                GlassSlideObservation(
                    label=label,
                    class_id=class_id_int,
                    polygon=polygon,
                    center=(cx, cy),
                    bbox=(x_min, y_min, x_max, y_max),
                    confidence=float(det.get("confidence", 1.0)),
                    metadata={},
                )
            )
        return observations

    def _draw_detection_annotations(self, overlay: np.ndarray, observations: Sequence[GlassSlideObservation]) -> None:
        height, width = overlay.shape[:2]
        center_x = width // 2
        for obs in observations:
            pts = obs.polygon
            if pts is None or len(pts) == 0:
                continue
            pts_int = pts.astype(int)
            cv2.polylines(overlay, [pts_int], isClosed=True, color=(0, 255, 0), thickness=2)
            text = self._annotation_text(obs)
            if not text:
                continue
            text_x = int(np.min(pts_int[:, 0]))
            text_y = int(np.max(pts_int[:, 1])) + 14
            text_x = max(0, min(width - 1, text_x))
            text_y = max(0, min(height - 1, text_y))
            cv2.putText(overlay, text, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 255), 2)
        # cv2.line(overlay, (center_x, 0), (center_x, height), (255, 0, 0), 2)

    def _draw_excluded_detections(self, overlay: np.ndarray, detections: Sequence[dict]) -> None:
        height, width = overlay.shape[:2]
        for det in detections:
            label = str(det.get("class") or det.get("label") or "").strip()
            if not label:
                continue
            class_id = det.get("class_id")
            class_id_int = int(class_id) if class_id is not None else None
            if not self._is_excluded(label.lower(), class_id_int):
                continue
            polygon = self._polygon_from_detection(det)
            if polygon is None or len(polygon) < 4:
                continue
            pts_int = np.array(polygon, dtype=int)
            cv2.polylines(overlay, [pts_int], isClosed=True, color=(0, 180, 0), thickness=2)
            text = self._extra_annotation_text(det)
            if not text:
                continue
            text_x = int(np.min(pts_int[:, 0]))
            text_y = int(np.max(pts_int[:, 1])) + 14
            text_x = max(0, min(width - 1, text_x))
            text_y = max(0, min(height - 1, text_y))
            cv2.putText(overlay, text, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 160, 255), 2)

    @staticmethod
    def _annotation_text(obs: GlassSlideObservation) -> str:
        qr_msg = obs.metadata.get("qr_message") if obs.metadata else None
        if qr_msg and qr_msg != "Not found":
            return f"QR: {qr_msg}"
        return obs.label

    @staticmethod
    def _extra_annotation_text(det: dict) -> str:
        qr_msg = det.get("qr_message")
        if isinstance(qr_msg, str):
            qr_msg = qr_msg.strip()
            if qr_msg and qr_msg.lower() != "not found":
                return f"QR: {qr_msg}"
        label = str(det.get("class") or det.get("label") or "").strip()
        return label

    def _polygon_from_detection(self, det: dict) -> np.ndarray | None:
        if "xyxyxyxy" in det and det["xyxyxyxy"]:
            coords = np.array(det["xyxyxyxy"], dtype=float).reshape(-1, 2)
            return coords
        x = float(det.get("x", 0.0))
        y = float(det.get("y", 0.0))
        w = float(det.get("w", 0.0))
        h = float(det.get("h", 0.0))
        angle_deg = float(det.get("angle_deg", 0.0))
        theta = math.radians(-angle_deg)
        half_w, half_h = 0.5 * w, 0.5 * h
        rect = np.array([[-half_w, -half_h], [half_w, -half_h], [half_w, half_h], [-half_w, half_h]], dtype=float)
        R = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]], dtype=float)
        pts = rect @ R.T + np.array([x, y])
        return pts

    @staticmethod
    def _compute_better_center(pts: np.ndarray) -> tuple[int, int]:
        M = cv2.moments(pts)
        if M["m00"] != 0:
            cx_moment = M["m10"] / M["m00"]
            cy_moment = M["m01"] / M["m00"]
        else:
            cx_moment = np.mean(pts[:, 0])
            cy_moment = np.mean(pts[:, 1])
        cx_mean = np.mean(pts[:, 0])
        cy_mean = np.mean(pts[:, 1])
        x_min, y_min = np.min(pts[:, 0]), np.min(pts[:, 1])
        x_max, y_max = np.max(pts[:, 0]), np.max(pts[:, 1])
        cx_bbox = (x_min + x_max) / 2.0
        cy_bbox = (y_min + y_max) / 2.0
        cx = (cx_moment + cx_mean + cx_bbox) / 3.0
        cy = (cy_moment + cy_mean + cy_bbox) / 3.0
        return int(round(cx)), int(round(cy))

    # ------------------------------------------------------------------
    # Matching + QR orientation
    # ------------------------------------------------------------------
    def _match(self, qr_obs: Sequence[GlassSlideObservation], pickup_obs: Sequence[GlassSlideObservation], img_gray: np.ndarray) -> list[dict]:
        qr_data = []
        for idx, obs in enumerate(qr_obs):
            angle, conf = self._detect_qr_orientation(obs.polygon, img_gray)
            if angle is None:
                angle = 0.0
                conf = 0.05
            rad = math.radians(angle)
            exp_x = obs.center[0] + self.config.expected_distance * math.cos(rad)
            exp_y = obs.center[1] + self.config.expected_distance * math.sin(rad)
            qr_data.append(
                {
                    "idx": idx,
                    "center": obs.center,
                    "angle": angle,
                    "conf": conf,
                    "expected": (exp_x, exp_y),
                }
            )

        n_qr = len(qr_obs)
        n_pickup = len(pickup_obs)
        if n_qr == 0 or n_pickup == 0:
            return []
        cost_matrix = np.full((n_qr, n_pickup), float("inf"))

        def dist(a: tuple[int, int], b: tuple[int, int]) -> float:
            return math.hypot(a[0] - b[0], a[1] - b[1])

        for i, qr in enumerate(qr_data):
            for j, pickup in enumerate(pickup_obs):
                actual_dist = dist(qr["center"], pickup.center)
                if actual_dist > self.config.max_distance or actual_dist < self.config.min_distance:
                    continue
                d_to_expected = dist(pickup.center, qr["expected"])
                dir_angle = math.degrees(math.atan2(pickup.center[1] - qr["center"][1], pickup.center[0] - qr["center"][0])) % 360.0
                angle_diff = abs((dir_angle - qr["angle"] + 180) % 360 - 180)
                orientation_penalty = (1.0 - qr["conf"]) * self.config.conf_penalty_scale
                score = d_to_expected + 1.5 * angle_diff + orientation_penalty
                cost_matrix[i, j] = score

        candidates: list[tuple[float, int, int]] = []
        for i in range(n_qr):
            for j in range(n_pickup):
                if cost_matrix[i, j] < float("inf"):
                    candidates.append((cost_matrix[i, j], i, j))
        candidates.sort()

        used_qrs: set[int] = set()
        used_pickups: set[int] = set()
        assigned: list[dict] = []

        def _build_match_record(qr_idx: int, pickup_idx: int, *, score: float | None = None) -> dict:
            qr = qr_data[qr_idx]
            pickup_obs_item = pickup_obs[pickup_idx]
            actual_dist = dist(qr["center"], pickup_obs_item.center)
            d_to_expected = dist(pickup_obs_item.center, qr["expected"])
            dir_angle = math.degrees(math.atan2(pickup_obs_item.center[1] - qr["center"][1], pickup_obs_item.center[0] - qr["center"][0])) % 360.0
            angle_diff = abs((dir_angle - qr["angle"] + 180) % 360 - 180)
            alignment = max(-1.0, min(1.0, math.cos(math.radians(angle_diff))))
            return {
                "class0_idx": qr_idx,
                "class2_idx": pickup_idx,
                "score": score if score is not None else d_to_expected,
                "actual_distance": actual_dist,
                "expected_error_px": d_to_expected,
                "angle_diff": angle_diff,
                "qr_conf": qr["conf"],
                "qr_angle": qr["angle"],
                "alignment": alignment,
            }

        for cost, i, j in candidates:
            if i in used_qrs or j in used_pickups:
                continue
            match_record = _build_match_record(i, j, score=cost)
            assigned.append(match_record)
            used_qrs.add(i)
            used_pickups.add(j)

        for i in range(n_qr):
            if i in used_qrs:
                continue
            best_candidate: dict | None = None
            for j in range(n_pickup):
                if j in used_pickups:
                    continue
                candidate = _build_match_record(i, j, score=None)
                if best_candidate is None or candidate["actual_distance"] < best_candidate["actual_distance"]:
                    best_candidate = candidate
            if best_candidate is not None:
                best_candidate["score"] = best_candidate["actual_distance"]
                assigned.append(best_candidate)
                used_pickups.add(best_candidate["class2_idx"])

        assigned.sort(key=lambda item: item["class0_idx"])
        return assigned

    @staticmethod
    def _wrap_signed_angle(angle: float) -> float:
        wrapped = (angle + 180.0) % 360.0 - 180.0
        return wrapped

    def _compute_slide_angle(self, metric: SlideMetric) -> float:
        dx = metric.pickup_center_mm[0] - metric.qr_center_mm[0]
        dy = metric.pickup_center_mm[1] - metric.qr_center_mm[1]
        angle = math.degrees(math.atan2(dy, dx))
        return self._wrap_signed_angle(angle)

    def _compute_rotate_angle(
        self,
        metric: SlideMetric,
        slide_angle_deg: float,
    ) -> float:
        center_x, center_y = metric.crystal_center_mm
        reference_angle = self._wrap_signed_angle(slide_angle_deg + self.config.slide_to_pickup_offset_deg)
        angle_theta = math.degrees(math.atan2(-center_x, center_y))
        if reference_angle >= 0:
            rotate = reference_angle - angle_theta
        else:
            rotate = 180.0 + reference_angle - angle_theta
        rotate += self.config.rotation_offset_deg
        return self._wrap_signed_angle(rotate)

    def _apply_metric_offsets(self, metric: SlideMetric) -> SlideMetric:
        along = float(self.config.pickup_offset_along_mm)
        cross = float(self.config.pickup_offset_cross_mm)
        if abs(along) < 1e-6 and abs(cross) < 1e-6:
            return metric
        dx = metric.pickup_center_mm[0] - metric.qr_center_mm[0]
        dy = metric.pickup_center_mm[1] - metric.qr_center_mm[1]
        length = math.hypot(dx, dy) or 1.0
        ux = dx / length
        uy = dy / length
        vx = -uy
        vy = ux
        pickup_adjusted = (
            metric.pickup_center_mm[0] + ux * along + vx * cross,
            metric.pickup_center_mm[1] + uy * along + vy * cross,
        )
        crystal_adjusted = (
            (pickup_adjusted[0] + metric.qr_center_mm[0]) * 0.5,
            (pickup_adjusted[1] + metric.qr_center_mm[1]) * 0.5,
        )
        return replace(
            metric,
            pickup_center_mm=pickup_adjusted,
            crystal_center_mm=crystal_adjusted,
        )

    def _detect_qr_orientation(self, polygon: np.ndarray, img_gray: np.ndarray) -> tuple[float | None, float]:
        quad = self._poly_to_quad(polygon)
        if quad is None or quad.shape[0] != 4:
            return None, 0.0

        quad = quad.astype(np.float32)
        c = np.mean(quad, axis=0)
        angs = np.arctan2(quad[:, 1] - c[1], quad[:, 0] - c[0])
        order = np.argsort(angs)
        quad = quad[order]
        top_idx = int(np.argmin(quad[:, 1] * 10000 + quad[:, 0]))
        if top_idx != 0:
            quad = np.roll(quad, -top_idx, axis=0)

        side = int(
            round(
                max(
                    np.linalg.norm(quad[0] - quad[1]),
                    np.linalg.norm(quad[1] - quad[2]),
                    np.linalg.norm(quad[2] - quad[3]),
                    np.linalg.norm(quad[3] - quad[0]),
                )
            )
        )
        if side < 12:
            return None, 0.0

        dst = np.array(
            [[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]],
            dtype=np.float32,
        )
        M = cv2.getPerspectiveTransform(quad, dst)
        Minv = cv2.getPerspectiveTransform(dst, quad)
        qr_warp = cv2.warpPerspective(img_gray, M, (side, side), flags=cv2.INTER_LINEAR, borderValue=255)

        blur = cv2.GaussianBlur(qr_warp, (3, 3), 0)
        _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        inv = 255 - th
        cnts, _ = cv2.findContours(inv, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        candidates = []
        for ctn in cnts:
            a = cv2.contourArea(ctn)
            if a < 0.002 * side * side:
                continue
            peri = cv2.arcLength(ctn, True)
            approx = cv2.approxPolyDP(ctn, 0.05 * peri, True)
            if len(approx) >= 4:
                x, y, wc, hc = cv2.boundingRect(approx)
                ar = wc / float(hc) if hc > 0 else 0
                if 0.45 <= ar <= 1.8:
                    mnts = cv2.moments(approx)
                    if mnts.get("m00", 0) != 0:
                        cx = float(mnts["m10"] / mnts["m00"])
                        cy = float(mnts["m01"] / mnts["m00"])
                        candidates.append({"area": a, "cx": cx, "cy": cy})
        candidates = sorted(candidates, key=lambda x: -x["area"])
        if len(candidates) < 3:
            canon_center = np.array([[[(side - 1) / 2.0, (side - 1) / 2.0]]], dtype=np.float32)
            canon_right = np.array([[[side - 1 - 1e-3, (side - 1) / 2.0]]], dtype=np.float32)
            img_ctr = cv2.perspectiveTransform(canon_center, Minv)[0, 0]
            img_right = cv2.perspectiveTransform(canon_right, Minv)[0, 0]
            vx = img_right[0] - img_ctr[0]
            vy = img_right[1] - img_ctr[1]
            if np.hypot(vx, vy) < 1e-3:
                return None, 0.0
            angle = math.degrees(math.atan2(vy, vx)) % 360.0
            return float(angle), 0.12

        found_centers = np.array([[c["cx"], c["cy"]] for c in candidates[:4]], dtype=np.float32)
        expected_positions = np.array(
            [
                [0.08 * side, 0.08 * side],
                [0.92 * side, 0.08 * side],
                [0.92 * side, 0.92 * side],
                [0.08 * side, 0.92 * side],
            ],
            dtype=np.float32,
        )

        assigned = {}
        used_expected: set[int] = set()
        for fi, fc in enumerate(found_centers):
            dists = np.linalg.norm(expected_positions - fc[None, :], axis=1)
            for ei in np.argsort(dists):
                if ei not in used_expected:
                    assigned[fi] = ei
                    used_expected.add(ei)
                    break

        assigned_dists = [np.linalg.norm(found_centers[fi] - expected_positions[ei]) for fi, ei in assigned.items()]
        mean_assigned = float(np.mean(assigned_dists)) if assigned_dists else 0.5 * side
        conf = 1.0 - np.clip(mean_assigned / (0.4 * side), 0.0, 1.0)
        conf = float(conf)

        expected_idx = set(assigned.values())
        missing_expected = [i for i in range(4) if i not in expected_idx]

        canon_pts = np.array([[[fc[0], fc[1]]] for fc in found_centers], dtype=np.float32)
        img_pts = cv2.perspectiveTransform(canon_pts, Minv).reshape(-1, 2)

        if len(missing_expected) == 1:
            ref_idx = 0
            ref_c = found_centers[ref_idx]
            best_dx = -1e9
            neigh_idx = None
            for k in range(len(found_centers)):
                if k == ref_idx:
                    continue
                dx = found_centers[k][0] - ref_c[0]
                if dx > best_dx:
                    best_dx = dx
                    neigh_idx = k
            if neigh_idx is None:
                neigh_idx = 1 if len(found_centers) > 1 else 0

            img_ref = img_pts[ref_idx]
            img_neigh = img_pts[neigh_idx]
            vx = img_neigh[0] - img_ref[0]
            vy = img_neigh[1] - img_ref[1]
            if np.hypot(vx, vy) < 1e-3:
                return None, 0.0
            angle = math.degrees(math.atan2(vy, vx)) % 360.0
            largest_canonical = found_centers[0]
            dist_to_br = np.linalg.norm(largest_canonical - expected_positions[2])
            if dist_to_br < 0.18 * side:
                conf = max(conf, 0.6)
            return float(angle), float(conf)

        canon_center = np.array([[[(side - 1) / 2.0, (side - 1) / 2.0]]], dtype=np.float32)
        canon_right = np.array([[[side - 1 - 1e-3, (side - 1) / 2.0]]], dtype=np.float32)
        img_ctr = cv2.perspectiveTransform(canon_center, Minv)[0, 0]
        img_right = cv2.perspectiveTransform(canon_right, Minv)[0, 0]
        vx = img_right[0] - img_ctr[0]
        vy = img_right[1] - img_ctr[1]
        if np.hypot(vx, vy) < 1e-3:
            return None, 0.0
        angle = math.degrees(math.atan2(vy, vx)) % 360.0
        return float(angle), float(conf)

    @staticmethod
    def _poly_to_quad(poly: np.ndarray) -> np.ndarray | None:
        pts = np.array(poly, dtype=np.float32).reshape(-1, 2)
        if pts.shape[0] < 4:
            return None
        hull = cv2.convexHull(pts).reshape(-1, 2)
        peri = cv2.arcLength(hull.astype(np.float32), True)
        eps = max(2.0, 0.02 * peri)
        approx = cv2.approxPolyDP(hull.astype(np.float32), eps, True)
        if approx is not None and len(approx) == 4:
            return approx.reshape(4, 2).astype(np.float32)
        rect = cv2.minAreaRect(pts)
        box = cv2.boxPoints(rect).astype(np.float32)
        return box

    # ------------------------------------------------------------------
    # QR reading
    # ------------------------------------------------------------------
    def _ensure_detector(self) -> pb.fiducial.MicroQR:
        if self._qr_detector is None:
            self._qr_detector = pb.FactoryFiducial(np.uint8).microqr()
        return self._qr_detector

    def _read_micro_qr(self, img_gray: np.ndarray, bbox: tuple[int, int, int, int], idx: int) -> str:
        try:
            detector = self._ensure_detector()
        except Exception as exc:
            print(f"Failed to init micro QR detector: {exc}")
            return "Not found"
        return read_micro_qr_exhaustive(img_gray, bbox, idx, detector)

    # ------------------------------------------------------------------
    # Persistence + DB updates
    # ------------------------------------------------------------------
    def _save_overlay(self, overlay: np.ndarray) -> Path | None:
        if self.config.overlay_output_path is None:
            return None
        path = self.config.overlay_output_path
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), overlay)
        return path

    def _save_csv(self, slides: Sequence[SlideResult]) -> Path | None:
        if self.config.csv_output_path is None:
            return None
        path = self.config.csv_output_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "Slide",
            "Pickup location",
            "Crystal location",
            "Slide angle (deg)",
            "Rotate angle (deg)",
            "Message",
            "Distance error (px)",
            "QR orientation (deg)",
            "Alignment score",
        ]
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for slide in slides:
                writer.writerow(
                    {
                        "Slide": f"Slide {slide.slide_index}",
                        "Pickup location": slide.pickup_center_px,
                        "Crystal location": slide.metric.crystal_center_mm,
                        "Slide angle (deg)": slide.slide_angle_deg,
                        "Rotate angle (deg)": slide.rotate_angle_deg,
                        "Message": slide.qr_message,
                        "Distance error (px)": round(slide.expected_error_px, 2),
                        "QR orientation (deg)": round(slide.qr_orientation_deg, 2),
                        "Alignment score": round(slide.alignment, 3),
                    }
                )
        return path

    def _update_database(self, slides: Sequence[SlideResult], *, frame_id: str | None, source: str | None) -> None:
        if not self.config.update_database or self.raw_carrier_service is None:
            return
        for slide in slides:
            self._record_raw_carrier_slide(slide, frame_id=frame_id, source=source)

    def _coordinate_key(self, x_mm: float, y_mm: float) -> str:
        return f"{int(round(x_mm))}:{int(round(y_mm))}"

    def _record_raw_carrier_slide(self, slide: SlideResult, *, frame_id: str | None, source: str | None) -> None:
        if self.raw_carrier_service is None and self.stamp_service is None:
            return
        qr_msg = (slide.qr_message or "").strip()
        if self.stamp_service is not None and qr_msg and "stamp" in qr_msg.lower():
            self.stamp_service.record_from_slide(
                slide,
                source=source or self.config.raw_material_source_label,
                frame_id=frame_id,
            )
            return

        if self.raw_carrier_service is None:
            return
        table_coord = self._coordinate_key(slide.metric.crystal_center_mm[0], slide.metric.crystal_center_mm[1])
        self.raw_carrier_service.record_from_slide(
            slide,
            source=source or self.config.raw_material_source_label,
            frame_id=frame_id,
            table_coordinate=table_coord,
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _is_qr(self, label: str, class_id: int | None) -> bool:
        return label.lower() in self._qr_label_set or (class_id is not None and class_id in self._qr_class_set)

    def _is_pickup(self, label: str, class_id: int | None) -> bool:
        return label.lower() in self._pickup_label_set or (class_id is not None and class_id in self._pickup_class_set)

    def _is_excluded(self, label_lower: str, class_id: int | None) -> bool:
        return label_lower in self._exclude_label_set or (class_id is not None and class_id in self._exclude_class_set)


# ----------------------------------------------------------------------
# Helper for standalone scripts
# ----------------------------------------------------------------------
def load_label_file(txt_path: Path, image_shape: tuple[int, int, int], label_map: dict[int, str] | None = None) -> list[dict]:
    h, w = image_shape[:2]
    detections: list[dict] = []
    with open(txt_path, "r") as f:
        for line in f:
            parts = list(map(float, line.strip().split()))
            if not parts:
                continue
            cls_id = int(parts[0])
            coords = parts[1:]
            if len(coords) % 2 != 0:
                coords = coords[:-1]
            pts = np.array(coords).reshape(-1, 2)
            pts[:, 0] *= w
            pts[:, 1] *= h
            label = label_map.get(cls_id, f"class{cls_id}") if label_map else f"class{cls_id}"
            detections.append(
                {
                    "class": label,
                    "class_id": cls_id,
                    "xyxyxyxy": pts.flatten().tolist(),
                    "confidence": 1.0,
                }
            )
    return detections
