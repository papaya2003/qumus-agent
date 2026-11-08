from __future__ import annotations

import math
import os
import time
from typing import Optional

import cv2
import numpy as np

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.search.atom_flows import (
    AtomCameraAutofocusOnce,
    AtomCameraGetFocusPosition,
    AtomCameraStart,
    AtomCameraStop,
    AtomCameraSetFocusPosition,
    AtomFocusMoveRelative,
    AtomSampleStageMoveTo,
    AtomTurretRotate,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeFlakeRefindRough(BaseWorkflow):
    name = "molecule_flake_refind_rough"
    description = "Refind a flake by matching a new BL corner image against a reference BL corner image."
    inputs = {
        "corner_BL_image": {
            "type": "string",
            "description": "Reference BL corner image path.",
        },
        "new_image": {
            "type": "string",
            "description": "New BL corner image path.",
        },
        "corner_BL_coord": {
            "type": "array",
            "description": "Reference BL corner stage coordinate as [x, y].",
        },
        "new_corner_coord": {
            "type": "array",
            "description": "New BL corner stage coordinate as [x, y].",
        },
        "flake_coord": {
            "type": "array",
            "description": "Reference flake stage coordinate as [x, y].",
        },
    }
    output_type = "string"

    ORB_NFEATURES = 1000
    RATIO_TEST = 0.75
    RANSAC_REPROJ_THRESH = 3.0
    MIN_GOOD_MATCHES = 10
    MIN_INLIERS = 5
    MIN_KEYPOINTS = 12
    AFFINE_RANSAC_REPROJ_THRESH = 2.0
    AFFINE_MAX_ITERS = 5000
    AFFINE_CONFIDENCE = 0.99
    AUTOFOCUS_WAIT_S = 3.0

    PLAN_STEPS = [
        "Load Images And Coordinates",
        "Match BL Corner",
        "Move To Refound Flake",
        "Autofocus",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stage_move_to = AtomSampleStageMoveTo(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.cam_autofocus_once = AtomCameraAutofocusOnce(*args, **kwargs)
        self.cam_set_focus_pos = AtomCameraSetFocusPosition(*args, **kwargs)
        self.cam_get_focus_pos = AtomCameraGetFocusPosition(*args, **kwargs)
        self.focus_move = AtomFocusMoveRelative(*args, **kwargs)
        self.turret_rotate = AtomTurretRotate(*args, **kwargs)
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
    def _parse_xy(value, label: str) -> np.ndarray:
        try:
            arr = np.asarray(value, dtype=float).reshape(-1)
        except Exception as exc:
            raise ValueError(f"{label} must be a numeric [x, y] array.") from exc
        if arr.size != 2:
            raise ValueError(f"{label} must contain exactly two values [x, y].")
        return arr.astype(float)

    @staticmethod
    def _gray_variants(img_bgr: np.ndarray) -> list[np.ndarray]:
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        gray_clahe = clahe.apply(gray)
        gray_eq = cv2.equalizeHist(gray)
        return [gray, gray_clahe, gray_eq]

    @staticmethod
    def _parse_int_from_message(message: str) -> Optional[int]:
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

    def _extract_features(self, img_bgr: np.ndarray, detector) -> tuple[list, Optional[np.ndarray]]:
        best_kp = []
        best_des = None
        for gray in self._gray_variants(img_bgr):
            kp, des = detector.detectAndCompute(gray, None)
            if des is not None and len(kp) > len(best_kp):
                best_kp = kp
                best_des = des
        return best_kp, best_des

    @staticmethod
    def _match_features(des_query, des_train, matcher, ratio: float) -> list:
        knn = matcher.knnMatch(des_query, des_train, k=2)
        good = []
        for pair in knn:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < ratio * n.distance:
                good.append(m)
        return good

    def _match_corner_bl_only(
        self,
        new_img_bgr: np.ndarray,
        corner_bl_img_bgr: np.ndarray,
    ) -> tuple[int, int, np.ndarray, np.ndarray, np.ndarray]:
        detector = cv2.ORB_create(
            nfeatures=self.ORB_NFEATURES * 2,
            fastThreshold=5,
            edgeThreshold=15,
            patchSize=31,
        )
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

        kp_new, des_new = self._extract_features(new_img_bgr, detector)
        kp_ref, des_ref = self._extract_features(corner_bl_img_bgr, detector)

        if des_new is None or des_ref is None or len(kp_new) < self.MIN_KEYPOINTS or len(kp_ref) < self.MIN_KEYPOINTS:
            raise RuntimeError(
                "ORB failed: too few keypoints/descriptors "
                f"(new={len(kp_new)}, ref={len(kp_ref)})"
            )

        good = self._match_features(des_new, des_ref, matcher, ratio=self.RATIO_TEST)
        good_count = len(good)
        if good_count < self.MIN_GOOD_MATCHES:
            raise RuntimeError(
                f"Match too weak: good_matches={good_count} < min_good_matches={self.MIN_GOOD_MATCHES}"
            )

        pts_new = np.float32([kp_new[m.queryIdx].pt for m in good]).reshape(-1, 2)
        pts_ref = np.float32([kp_ref[m.trainIdx].pt for m in good]).reshape(-1, 2)

        H, mask = cv2.findHomography(
            pts_ref.reshape(-1, 1, 2),
            pts_new.reshape(-1, 1, 2),
            cv2.RANSAC,
            ransacReprojThreshold=self.RANSAC_REPROJ_THRESH,
        )
        if H is None or mask is None:
            raise RuntimeError("findHomography failed")

        inliers = int(mask.sum())
        if inliers < self.MIN_INLIERS:
            raise RuntimeError(
                f"Match too weak: inliers={inliers} < min_inliers={self.MIN_INLIERS}"
            )

        in_mask = mask.ravel().astype(bool)
        pts_ref_in = pts_ref[in_mask]
        pts_new_in = pts_new[in_mask]
        return inliers, good_count, H, pts_ref_in, pts_new_in

    def _theta_cw_from_inliers_affine(self, pts_ref_in: np.ndarray, pts_new_in: np.ndarray) -> float:
        if pts_ref_in is None or pts_new_in is None or len(pts_ref_in) < 6:
            raise RuntimeError("Not enough inlier points for affine angle estimation")

        M, _ = cv2.estimateAffinePartial2D(
            pts_ref_in,
            pts_new_in,
            method=cv2.RANSAC,
            ransacReprojThreshold=self.AFFINE_RANSAC_REPROJ_THRESH,
            maxIters=self.AFFINE_MAX_ITERS,
            confidence=self.AFFINE_CONFIDENCE,
        )
        if M is None:
            raise RuntimeError("estimateAffinePartial2D failed")

        a = float(M[0, 0])
        c = float(M[1, 0])
        return math.atan2(c, a)

    @staticmethod
    def _rot_cw(theta_rad: float) -> np.ndarray:
        c = math.cos(theta_rad)
        s = math.sin(theta_rad)
        return np.array([[c, s], [-s, c]], dtype=float)

    def forward(
        self,
        corner_BL_image: str,
        new_image: str,
        corner_BL_coord,
        new_corner_coord,
        flake_coord,
    ) -> str:
        if self.stage is None or self.camera is None:
            return "❌ Stage or camera controller unavailable."

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []
        results.append(self.cam_start(open_live_view=True))
        self.broadcaster_step.forward(0)
        ref_corner_xy = self._parse_xy(corner_BL_coord, "corner_BL_coord")
        new_corner_xy = self._parse_xy(new_corner_coord, "new_corner_coord")
        flake_xy = self._parse_xy(flake_coord, "flake_coord")
        ref_corner_img = self._load_image(corner_BL_image, "corner_BL_image")
        new_corner_img = self._load_image(new_image, "new_image")
        results.append(f"Loaded reference image: {os.path.abspath(corner_BL_image)}")
        results.append(f"Loaded new image: {os.path.abspath(new_image)}")

        self.broadcaster_step.forward(1)
        inliers, good_count, _H, pts_ref_in, pts_new_in = self._match_corner_bl_only(
            new_corner_img,
            ref_corner_img,
        )
        theta_cw = self._theta_cw_from_inliers_affine(pts_ref_in, pts_new_in)
        v = flake_xy - ref_corner_xy
        v_rot = self._rot_cw(theta_cw) @ v
        flake_new_xy = new_corner_xy + v_rot
        results.append(f"good_matches: {good_count}")
        results.append(f"inliers: {inliers}")
        results.append(f"theta_cw_rad: {theta_cw:.10f}")
        results.append(f"target_xy: ({float(flake_new_xy[0]):.6f}, {float(flake_new_xy[1]):.6f})")

        self.broadcaster_step.forward(2)
        results.append(
            self.stage_move_to(
                x=float(flake_new_xy[0]),
                y=float(flake_new_xy[1]),
                wait=True,
            )
        )

        self.broadcaster_step.forward(3)
        try:
            results.append(self.turret_rotate(position=3))
            results.append(self.cam_autofocus_once())
            time.sleep(5.0)
        except Exception as exc:
            results.append(f"⚠️ Autofocus failed: {exc}")
        try:
            s1_msg = self.cam_get_focus_pos()
            s1 = self._parse_int_from_message(s1_msg)
            if s1 is None:
                raise RuntimeError("Failed to parse focus position.")
            results.append(self.cam_set_focus_pos(pos=330))
            dz1 = float(4.4029) * (float(s1) - float(330))
            if s1 >=100 and s1 <=800:
                results.append(self.focus_move(delta=dz1, wait=True))
                time.sleep(abs(dz1) * 0.005)
            else:
                results.append("out of focus, skipping focus move")
        except Exception as exc:
            results.append(f"[WARN] 20X focus-position mapping failed: {exc}")
        results.append(self.cam_stop())
        return "\n".join(results)
