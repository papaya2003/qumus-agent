from __future__ import annotations

import os
import time
from typing import Optional

import cv2
import numpy as np

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.search.atom_flows import (
    AtomCameraAutoExposureOff,
    AtomCameraAutoExposureOn,
    AtomCameraAutofocusOff,
    AtomCameraAutofocusOnce,
    AtomCameraGetFocusPosition,
    AtomCameraSetFocusPosition,
    AtomCameraStart,
    AtomCameraStop,
    AtomCameraWaitFocusAndTakePhoto,
    AtomSampleStageMoveHome,
    AtomSampleStageMoveRelative,
    AtomSampleStageWaitUntilStop,
    AtomTurretRotate,
)
from agents.processing_agent.workflows.search.utils.corner_coordinate_adjustment import (
    compute_corner_xy_from_detector,
)
from agents.processing_agent.workflows.search.utils.edge_follow import detect_edge_and_motion
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


def _unit(vx: float, vy: float) -> tuple[float, float]:
    norm = float(np.hypot(vx, vy))
    if norm < 1e-9:
        return 0.0, 0.0
    return vx / norm, vy / norm


def _motion_to_stage_speed(vx_img: float, vy_img: float, max_speed: int) -> tuple[int, int]:
    vx_img, vy_img = _unit(vx_img, vy_img)
    sx = int(round(vy_img * max_speed))
    sy = int(round(vx_img * max_speed))
    return sx, sy


def _draw_overlay(
    bgr: np.ndarray,
    result,
    sx: Optional[int] = None,
    sy: Optional[int] = None,
    max_speed: int = 150,
) -> np.ndarray:
    output = bgr.copy()
    height, width = output.shape[:2]
    cx, cy = width // 2, height // 2

    if result is not None:
        vx, vy = result.motion_vec
        vx, vy = _unit(vx, vy)
        length = int(min(height, width) * 0.25)
        x2 = int(cx - vx * length)
        y2 = int(cy - vy * length)
        cv2.arrowedLine(output, (cx, cy), (x2, y2), (0, 255, 255), 8, tipLength=0.25)

        if result.bbox is not None:
            x0, y0, x1, y1 = result.bbox
            cv2.rectangle(output, (x0, y0), (x1, y1), (0, 0, 255), 2)

        text1 = f"{result.reason} | ratio={result.chip_ratio:.3f} | label={result.motion_label}"
        cv2.putText(output, text1, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)

    if sx is not None and sy is not None:
        text2 = f"jog sx={sx}  sy={sy}  (MAX={max_speed})"
        cv2.putText(output, text2, (10, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)

    cv2.putText(output, "q: quit", (10, height - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
    return output


class MoleculeCornerDetection(BaseWorkflow):
    name = "molecule_corner_detection"
    description = "Continuous edge-follow corner finding for search with coordinate adjustment and corner capture."
    inputs = {
        "save_dir": {"type": "string", "description": "Directory to save corner photos.", "nullable": True},
        "save_name": {"type": "string", "description": "Optional filename for the saved corner image.", "nullable": True},
    }
    output_type = "string"

    MAX_SPEED = 150
    DETECT_PERIOD = 0.5
    DISPLAY_PERIOD = 0.05
    MIN_CORNER_INTERVAL = 1.0
    CORNER_LEAVE_REQUIRED = True
    SHOW_WINDOWS = True
    OPEN_LIVE_VIEW = True
    TURRET_POS = 1
    CENTER_X = 1790
    CENTER_Y = 1220
    PIXEL_SIZE_5X_UM = 0.730
    STEPS_PER_MM = 12800.0
    FOCUS_WAIT_S = 3.0
    ADJUST_SETTLE_S = 1.0
    INITIAL_FOCUS_MIN_BLUR = 6.0
    CAPTURE_MIN_BLUR = 6.0
    STOP_AFTER_CORNER = "BL"
    MAX_SECONDS = 120.0
    RETURN_HOME = False

    PLAN_STEPS = [
        "Initialize Corner Detection",
        "Continuous Edge Following",
        "Adjust And Capture Corner",
        "Finalize",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.cam_auto_exposure_on = AtomCameraAutoExposureOn(*args, **kwargs)
        self.cam_auto_exposure_off = AtomCameraAutoExposureOff(*args, **kwargs)
        self.cam_set_focus_pos = AtomCameraSetFocusPosition(*args, **kwargs)
        self.cam_autofocus_off = AtomCameraAutofocusOff(*args, **kwargs)
        self.cam_autofocus_once = AtomCameraAutofocusOnce(*args, **kwargs)
        self.cam_wait_focus_photo = AtomCameraWaitFocusAndTakePhoto(*args, **kwargs)
        self.stage_wait_until_stop = AtomSampleStageWaitUntilStop(*args, **kwargs)
        self.stage_move_relative = AtomSampleStageMoveRelative(*args, **kwargs)
        self.stage_move_home = AtomSampleStageMoveHome(*args, **kwargs)
        self.turret_rotate = AtomTurretRotate(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    @staticmethod
    def _parse_reason(reason: str) -> tuple[Optional[str], Optional[str]]:
        if not reason:
            return None, None
        parts = reason.strip().split()
        if len(parts) >= 2 and parts[0] in ("CORNER", "EDGE"):
            return parts[0], parts[1]
        return None, None

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
    def _normalize_save_name(save_name: Optional[str], default_name: str) -> str:
        final_name = (save_name or default_name).strip()
        if not final_name:
            final_name = default_name
        root, ext = os.path.splitext(final_name)
        if not ext:
            final_name = f"{final_name}.jpg"
        return final_name

    def _stop_stage_motion(self) -> None:
        if self.stage is None:
            return
        try:
            self.stage.jogging(0, 0, wait=False)
        except Exception:
            pass
        try:
            self.stage_wait_until_stop()
        except Exception:
            pass

    def forward(self, save_dir: Optional[str] = None, save_name: Optional[str] = None) -> str:
        if self.stage is None or self.camera is None:
            return "❌ Stage or camera controller unavailable."

        max_speed = self.MAX_SPEED
        detect_period = self.DETECT_PERIOD
        display_period = self.DISPLAY_PERIOD
        min_corner_interval = self.MIN_CORNER_INTERVAL
        corner_leave_required = self.CORNER_LEAVE_REQUIRED
        show_windows = self.SHOW_WINDOWS
        open_live_view = self.OPEN_LIVE_VIEW
        turret_pos = self.TURRET_POS
        center_x = self.CENTER_X
        center_y = self.CENTER_Y
        pixel_size_5x_um = self.PIXEL_SIZE_5X_UM
        steps_per_mm = self.STEPS_PER_MM
        focus_wait_s = self.FOCUS_WAIT_S
        adjust_settle_s = self.ADJUST_SETTLE_S
        initial_focus_min_blur = self.INITIAL_FOCUS_MIN_BLUR
        capture_min_blur = self.CAPTURE_MIN_BLUR
        stop_corner = self.STOP_AFTER_CORNER.upper() if self.STOP_AFTER_CORNER else None
        max_seconds = self.MAX_SECONDS
        return_home = self.RETURN_HOME
        steps_per_um = float(steps_per_mm) / 1000.0
        results: list[str] = []

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        self.broadcaster_step.forward(0)

        if save_dir is None:
            save_dir = os.path.join(os.getcwd(), "images", "search", "corner")
        os.makedirs(save_dir, exist_ok=True)
        results.append(f"Starting Molecule Corner Detection in {save_dir}")

        if self.turret is not None:
            try:
                results.append(self.turret_rotate(position=1))
            except Exception as exc:
                results.append(f"⚠️ Turret rotate failed: {exc}")

        results.append(self.cam_start(open_live_view=open_live_view))

        try:
            # results.append(self.cam_auto_exposure_on())
            time.sleep(1.0)
            results.append(self.cam_auto_exposure_off())
        except Exception as exc:
            results.append(f"⚠️ Auto exposure setup failed: {exc}")

        try:
            results.append(self.cam_set_focus_pos(pos=330))
            if hasattr(self.camera, "wait_focus_and_snap"):
                self.camera.wait_focus_and_snap(min_blur=float(initial_focus_min_blur))
            results.append(self.cam_autofocus_off())
        except Exception as exc:
            results.append(f"⚠️ Initial focus preparation failed: {exc}")

        if show_windows:
            for name in ("LIVE", "THRESH", "CHIP_MASK"):
                cv2.namedWindow(name, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(name, 1000, 650)

        last_detect_t = 0.0
        last_display_t = 0.0
        last_result = None

        last_corner_time: dict[str, float] = {}
        corners_done: set[str] = set()
        corner_pos: dict[str, np.ndarray] = {}
        corner_ignore_until = 0.0
        corner_lock = False
        current_sx, current_sy = 0, 0
        start_t = time.time()

        try:
            self.stage.jogging(0, 0, wait=False)
        except Exception:
            pass

        try:
            self.broadcaster_step.forward(1)
            while True:
                now = time.time()
                if max_seconds and (now - start_t) > float(max_seconds):
                    results.append(f"Reached max runtime: {float(max_seconds):.1f}s")
                    break

                bgr = self.camera.snap(timeout=2.0)

                if now - last_detect_t >= float(detect_period):
                    result = detect_edge_and_motion(bgr)
                    last_result = result

                    kind, name = self._parse_reason(result.reason)
                    if kind != "CORNER":
                        corner_lock = False

                    if kind == "CORNER" and name is not None:
                        can_save = True
                        if corner_leave_required and corner_lock:
                            can_save = False
                        elif (name in corners_done) or (now < corner_ignore_until):
                            can_save = False
                        elif now - last_corner_time.get(name, 0.0) < float(min_corner_interval):
                            can_save = False

                        if can_save:
                            self.broadcaster_step.forward(2)
                            self._stop_stage_motion()

                            try:
                                bgr_stop = self.camera.snap(timeout=2.0)
                                result_stop = detect_edge_and_motion(bgr_stop)
                            except Exception:
                                bgr_stop = bgr
                                result_stop = result

                            time.sleep(1.0)
                            results.append(f"[CORNER] Detected corner {name}, adjusting coordinates...")
                            adjustment = compute_corner_xy_from_detector(result_stop, bgr_stop)
                            if adjustment.ok and adjustment.corner_xy is not None:
                                px, py = adjustment.corner_xy
                                move_x_px = int(center_x) - int(px)
                                move_y_px = int(center_y) - int(py)
                                move_x_step = -float(move_y_px) * float(pixel_size_5x_um) * steps_per_um
                                move_y_step = -float(move_x_px) * float(pixel_size_5x_um) * steps_per_um
                                results.append(f"[CORNER-XY] {name}: {adjustment.corner_xy}  float={adjustment.corner_xy_float}")
                                results.append(self.stage_move_relative(dx=move_x_step, dy=move_y_step, wait=True))
                                time.sleep(float(adjust_settle_s))
                            else:
                                results.append(f"[CORNER-XY] {name}: failed ({adjustment.reason})")

                            try:
                                results.append(self.cam_autofocus_once())
                                time.sleep(float(focus_wait_s))
                            except Exception as exc:
                                results.append(f"⚠️ Corner autofocus failed: {exc}")

                            final_save_name = self._normalize_save_name(
                                save_name,
                                f"CORNER_{name}_2.jpg",
                            )
                            save_path = os.path.join(save_dir, final_save_name)
                            capture_msg = self.cam_wait_focus_photo(
                                photo_dir=save_dir,
                                min_blur=float(capture_min_blur),
                                save_path=save_path,
                            )
                            saved_abs, score = self._parse_capture_result(capture_msg)

                            pos_xy = self.stage.get_pos()
                            corner_pos[name] = np.array(pos_xy, dtype=float)
                            corners_done.add(name)
                            last_corner_time[name] = now
                            corner_ignore_until = now + 1.0
                            corner_lock = True

                            if score is None:
                                results.append(f"[CORNER] {name} saved: {saved_abs or capture_msg}")
                            else:
                                results.append(f"[CORNER] {name} saved: {saved_abs} (score={score:.2f})")
                            results.append(
                                f"[CORNER-POS] {name}: ({float(pos_xy[0]):.6f}, {float(pos_xy[1]):.6f})"
                            )

                            if stop_corner is not None and name.upper() == stop_corner:
                                results.append(f"[DONE] CORNER {stop_corner} saved.")
                                break

                    if result.ok:
                        vx, vy = result.motion_vec
                        sx, sy = _motion_to_stage_speed(vx, vy, int(max_speed))
                    else:
                        sx, sy = _motion_to_stage_speed(0.0, 1.0, int(max_speed))

                    current_sx, current_sy = int(sx), int(sy)
                    self.stage.jogging(current_sx, current_sy, wait=False)
                    last_detect_t = now

                if show_windows and (now - last_display_t >= float(display_period)):
                    overlay = _draw_overlay(bgr, last_result, current_sx, current_sy, int(max_speed))
                    cv2.imshow("LIVE", overlay)

                    if last_result is not None:
                        cv2.imshow("THRESH", last_result.thresh_vis)
                        cv2.imshow("CHIP_MASK", last_result.chip_mask)

                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("q"), 27):
                        results.append("[QUIT] user requested")
                        break

                    last_display_t = now

        finally:
            self._stop_stage_motion()
            try:
                results.append(self.cam_auto_exposure_on())
            except Exception:
                pass
            try:
                results.append(self.cam_stop())
            except Exception as exc:
                results.append(f"⚠️ Camera stop failed: {exc}")
            if show_windows:
                try:
                    cv2.destroyAllWindows()
                except Exception:
                    pass
            self.broadcaster_step.forward(3)

        if return_home:
            try:
                results.append(self.stage_move_home())
            except Exception as exc:
                results.append(f"⚠️ Stage home move failed: {exc}")

        found = ", ".join(sorted(corners_done)) if corners_done else "none"
        if corner_pos:
            for corner_name in sorted(corner_pos):
                pos_xy = corner_pos[corner_name]
                results.append(
                    f"[FINAL-CORNER-POS] {corner_name}: ({float(pos_xy[0]):.6f}, {float(pos_xy[1]):.6f})"
                )
        results.append(f"Corner detection complete. Corners saved: {found}.")
        return "\n".join(results)
