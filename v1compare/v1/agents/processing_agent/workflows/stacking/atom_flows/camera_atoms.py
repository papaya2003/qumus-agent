from __future__ import annotations

import os

from agents.processing_agent.workflows.base_workflow import BaseWorkflow

# Camera Atoms

class _CameraAtom(BaseWorkflow):
    def _require_camera(self) -> bool:
        if self.camera is None:
            print("❌ Camera controller unavailable.")
            return False
        return True

    def _require_focus(self) -> bool:
        if self.focus is None:
            print("❌ Focus controller unavailable.")
            return False
        return True


class AtomCameraStart(_CameraAtom):
    name = "atom_camera_start"
    description = "Start the UVCHAM camera and optionally open the live view window."
    inputs = {
        "open_live_view": {
            "type": "boolean",
            "description": "Open the OpenCV live view window.",
            "default": True,
            "nullable": True,
        }
    }
    output_type = "string"

    def forward(self, open_live_view: bool = True) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.start()
        if open_live_view:
            self.camera.open_live_view()
            return "✅ Camera started. [UI_LIVE_FEED microscope on]"
        return "✅ Camera started."


class AtomCameraStop(_CameraAtom):
    name = "atom_camera_stop"
    description = "Close the live view window (if open) and stop the camera stream."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        try:
            self.camera.close_live_view()
        except Exception:
            pass
        self.camera.stop()
        return "✅ Camera stopped. [UI_LIVE_FEED microscope off]"


class AtomCameraSetLiveViewSize(_CameraAtom):
    name = "atom_camera_set_live_view_size"
    description = "Set the live view window size in pixels."
    inputs = {
        "width": {"type": "number", "description": "Window width in pixels."},
        "height": {"type": "number", "description": "Window height in pixels."},
    }
    output_type = "string"

    def forward(self, width: float, height: float) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.set_live_view_size(int(width), int(height))
        return f"✅ Live view size set to {int(width)}x{int(height)}."


class AtomCameraOpenLiveView(_CameraAtom):
    name = "atom_camera_open_live_view"
    description = "Open the live view window (non-blocking)."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.open_live_view()
        return "✅ Live view opened. [UI_LIVE_FEED microscope on]"


class AtomCameraCloseLiveView(_CameraAtom):
    name = "atom_camera_close_live_view"
    description = "Close the live view window."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.close_live_view()
        return "✅ Live view closed. [UI_LIVE_FEED microscope off]"


class AtomCameraTakePhoto(_CameraAtom):
    name = "atom_camera_take_photo"
    description = "Capture and save a photo to a file path."
    inputs = {
        "path": {"type": "string", "description": "File path to save the image."},
        "timeout": {"type": "number", "description": "Timeout in seconds.", "default": 2.0, "nullable": True},
    }
    output_type = "string"

    def forward(self, path: str, timeout: float = 2.0) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        abs_path = self.camera.take_photo(path=path, timeout=float(timeout))
        return f"✅ Photo saved: {abs_path}"


class AtomCameraSnap(_CameraAtom):
    name = "atom_camera_snap"
    description = "Capture a single frame without saving to disk."
    inputs = {
        "timeout": {"type": "number", "description": "Timeout in seconds.", "default": 2.0, "nullable": True},
    }
    output_type = "string"

    def forward(self, timeout: float = 2.0) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        frame = self.camera.snap(timeout=float(timeout))
        shape = getattr(frame, "shape", None)
        if shape:
            return f"✅ Frame captured ({shape[1]}x{shape[0]})."
        return "✅ Frame captured."


class AtomCameraBlurScore(_CameraAtom):
    name = "atom_camera_blur_score"
    description = "Compute focus score (Tenengrad) for the latest frame."
    inputs = {
        "timeout": {"type": "number", "description": "Timeout in seconds.", "default": 2.0, "nullable": True},
        "shrink": {"type": "number", "description": "Downsample factor.", "default": 2, "nullable": True},
    }
    output_type = "string"

    def forward(self, timeout: float = 2.0, shrink: float = 2) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        frame = self.camera.snap(timeout=float(timeout))
        score = self.camera.blur_score_bgr(frame, shrink=int(shrink))
        return f"✅ Blur score (Tenengrad): {float(score):.3f}"


class AtomCameraBlurScoreLaplacian(_CameraAtom):
    name = "atom_camera_blur_score_laplacian"
    description = "Compute focus score (Laplacian variance) for the latest frame."
    inputs = {
        "timeout": {"type": "number", "description": "Timeout in seconds.", "default": 2.0, "nullable": True},
        "shrink": {"type": "number", "description": "Downsample factor.", "default": 2, "nullable": True},
    }
    output_type = "string"

    def forward(self, timeout: float = 2.0, shrink: float = 2) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        frame = self.camera.snap(timeout=float(timeout))
        score = self.camera.blur_score_bgr_laplacian(frame, shrink=int(shrink))
        return f"✅ Blur score (Laplacian): {float(score):.3f}"


class AtomCameraWaitFocusAndSnap(_CameraAtom):
    name = "atom_camera_wait_focus_and_snap"
    description = "Wait for focus quality and capture a frame (not saved)."
    inputs = {
        "min_blur": {"type": "number", "description": "Minimum acceptable focus score.", "default": 3.5, "nullable": True},
        "max_checks": {"type": "number", "description": "Maximum focus checks.", "default": 100, "nullable": True},
        "interval_s": {"type": "number", "description": "Interval between checks (s).", "default": 0.02, "nullable": True},
        "timeout_per_snap": {"type": "number", "description": "Per-snap timeout (s).", "default": 2.0, "nullable": True},
        "flush": {"type": "number", "description": "Frames to flush before scoring.", "default": 2, "nullable": True},
        "shrink": {"type": "number", "description": "Downsample factor.", "default": 2, "nullable": True},
    }
    output_type = "string"

    def forward(
        self,
        min_blur: float = 3.5,
        max_checks: float = 100,
        interval_s: float = 0.02,
        timeout_per_snap: float = 2.0,
        flush: float = 2,
        shrink: float = 2,
    ) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        frame, score = self.camera.wait_focus_and_snap(
            min_blur=float(min_blur),
            max_checks=int(max_checks),
            interval_s=float(interval_s),
            timeout_per_snap=float(timeout_per_snap),
            flush=int(flush),
            shrink=int(shrink),
        )
        hit = frame is not None and float(score) >= float(min_blur)
        status = "HIT" if hit else "BEST"
        return f"✅ Focus {status}: score={float(score):.3f}"


class AtomCameraWaitFocusAndTakePhoto(_CameraAtom):
    name = "atom_camera_wait_focus_and_take_photo"
    description = "Wait for focus and save the best frame to disk."
    inputs = {
        "photo_dir": {"type": "string", "description": "Directory to save the capture."},
        "min_blur": {"type": "number", "description": "Minimum acceptable focus score.", "default": 3.5, "nullable": True},
        "max_checks": {"type": "number", "description": "Maximum focus checks.", "default": 100, "nullable": True},
        "interval_s": {"type": "number", "description": "Interval between checks (s).", "default": 0.02, "nullable": True},
        "timeout_per_snap": {"type": "number", "description": "Per-snap timeout (s).", "default": 2.0, "nullable": True},
        "flush": {"type": "number", "description": "Frames to flush before scoring.", "default": 2, "nullable": True},
        "shrink": {"type": "number", "description": "Downsample factor.", "default": 2, "nullable": True},
        "save_path": {"type": "string", "description": "Optional exact save path.", "nullable": True},
    }
    output_type = "string"

    def forward(
        self,
        photo_dir: str,
        min_blur: float = 3.5,
        max_checks: float = 100,
        interval_s: float = 0.02,
        timeout_per_snap: float = 2.0,
        flush: float = 2,
        shrink: float = 2,
        save_path: str | None = None,
    ) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        os.makedirs(photo_dir, exist_ok=True)
        path, score = self.camera.wait_focus_and_take_photo(
            path=photo_dir,
            min_blur=float(min_blur),
            max_checks=int(max_checks),
            interval_s=float(interval_s),
            timeout_per_snap=float(timeout_per_snap),
            flush=int(flush),
            shrink=int(shrink),
            save_path=save_path,
        )
        return f"✅ Captured image: {path} (score={float(score):.2f})"


class AtomCameraManualFocusAndTakePhoto(_CameraAtom):
    name = "atom_camera_manual_focus_and_take_photo"
    description = "Scan focus offsets manually and save the best frame."
    inputs = {
        "photo_dir": {"type": "string", "description": "Directory to save the capture."},
        "min_blur": {"type": "number", "description": "Minimum acceptable focus score.", "default": 4.0, "nullable": True},
        "start_offset_um": {"type": "number", "description": "Start offset in um.", "default": -500, "nullable": True},
        "end_offset_um": {"type": "number", "description": "End offset in um.", "default": 500, "nullable": True},
        "step_um": {"type": "number", "description": "Step size in um.", "default": 20, "nullable": True},
        "timeout": {"type": "number", "description": "Snap timeout (s).", "default": 2.0, "nullable": True},
        "settle_s": {"type": "number", "description": "Settle time per step (s).", "default": 0.05, "nullable": True},
        "shrink": {"type": "number", "description": "Downsample factor.", "default": 2, "nullable": True},
        "wait_factor_s_per_um": {"type": "number", "description": "Extra settle per um.", "nullable": True},
        "save_path": {"type": "string", "description": "Optional exact save path.", "nullable": True},
    }
    output_type = "string"

    def forward(
        self,
        photo_dir: str,
        min_blur: float = 4.0,
        start_offset_um: float = -500,
        end_offset_um: float = 500,
        step_um: float = 20,
        timeout: float = 2.0,
        settle_s: float = 0.05,
        shrink: float = 2,
        wait_factor_s_per_um: float | None = None,
        save_path: str | None = None,
    ) -> str:
        if not self._require_camera() or not self._require_focus():
            return "❌ Camera or focus controller unavailable."
        os.makedirs(photo_dir, exist_ok=True)
        path, score = self.camera.manual_focus_and_take_photo(
            self.focus,
            path=photo_dir,
            min_blur=float(min_blur),
            start_offset_um=int(start_offset_um),
            end_offset_um=int(end_offset_um),
            step_um=int(step_um),
            timeout=float(timeout),
            settle_s=float(settle_s),
            shrink=int(shrink),
            wait_factor_s_per_um=None if wait_factor_s_per_um is None else float(wait_factor_s_per_um),
            save_path=save_path,
        )
        return f"✅ Manual focus capture: {path} (score={float(score):.2f})"


class AtomCameraScanFocusAndTakePhoto(_CameraAtom):
    name = "atom_camera_scan_focus_and_take_photo"
    description = "Scan focus offsets and save the best frame, staying at best focus."
    inputs = {
        "photo_dir": {"type": "string", "description": "Directory to save the capture."},
        "min_blur": {"type": "number", "description": "Minimum acceptable focus score.", "default": 7.0, "nullable": True},
        "start_offset_um": {"type": "number", "description": "Start offset in um.", "default": -300, "nullable": True},
        "end_offset_um": {"type": "number", "description": "End offset in um.", "default": 300, "nullable": True},
        "step_um": {"type": "number", "description": "Step size in um.", "default": 20, "nullable": True},
        "timeout": {"type": "number", "description": "Snap timeout (s).", "default": 2.0, "nullable": True},
        "settle_s": {"type": "number", "description": "Settle time per step (s).", "default": 0.05, "nullable": True},
        "shrink": {"type": "number", "description": "Downsample factor.", "default": 2, "nullable": True},
        "wait_factor_s_per_um": {"type": "number", "description": "Extra settle per um.", "nullable": True},
        "save_path": {"type": "string", "description": "Optional exact save path.", "nullable": True},
    }
    output_type = "string"

    def forward(
        self,
        photo_dir: str,
        min_blur: float = 7.0,
        start_offset_um: float = -300,
        end_offset_um: float = 300,
        step_um: float = 20,
        timeout: float = 2.0,
        settle_s: float = 0.05,
        shrink: float = 2,
        wait_factor_s_per_um: float | None = None,
        save_path: str | None = None,
    ) -> str:
        if not self._require_camera() or not self._require_focus():
            return "❌ Camera or focus controller unavailable."
        os.makedirs(photo_dir, exist_ok=True)
        path, score = self.camera.scan_focus_and_take_photo(
            self.focus,
            path=photo_dir,
            min_blur=float(min_blur),
            start_offset_um=int(start_offset_um),
            end_offset_um=int(end_offset_um),
            step_um=int(step_um),
            timeout=float(timeout),
            settle_s=float(settle_s),
            shrink=int(shrink),
            wait_factor_s_per_um=None if wait_factor_s_per_um is None else float(wait_factor_s_per_um),
            save_path=save_path,
        )
        return f"✅ Scan focus capture: {path} (score={float(score):.2f})"


class AtomCameraAutofocusOn(_CameraAtom):
    name = "atom_camera_autofocus_on"
    description = "Enable continuous autofocus."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.autofocus_on()
        return "✅ Autofocus enabled."


class AtomCameraAutofocusOff(_CameraAtom):
    name = "atom_camera_autofocus_off"
    description = "Disable autofocus (manual focus mode)."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.autofocus_off()
        return "✅ Autofocus disabled."


class AtomCameraAutofocusOnce(_CameraAtom):
    name = "atom_camera_autofocus_once"
    description = "Trigger one-shot autofocus."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.autofocus_once()
        return "✅ Autofocus triggered (one-shot)."


class AtomCameraAutoExposureOn(_CameraAtom):
    name = "atom_camera_auto_exposure_on"
    description = "Enable automatic exposure control."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.auto_exposure_on()
        return "✅ Auto exposure enabled."


class AtomCameraAutoExposureOff(_CameraAtom):
    name = "atom_camera_auto_exposure_off"
    description = "Disable automatic exposure control."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.auto_exposure_off()
        return "✅ Auto exposure disabled."


class AtomCameraGetAfZoneInfo(_CameraAtom):
    name = "atom_camera_get_af_zone_info"
    description = "Get autofocus zone grid info and current zone."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        info = self.camera.get_af_zone_info()
        return f"✅ AF zone info: {info}"


class AtomCameraSetAfZone(_CameraAtom):
    name = "atom_camera_set_af_zone"
    description = "Set autofocus zone by zone index, row/col, or preset location."
    inputs = {
        "zone": {"type": "number", "description": "Zone index.", "nullable": True},
        "row": {"type": "number", "description": "Grid row index.", "nullable": True},
        "col": {"type": "number", "description": "Grid column index.", "nullable": True},
        "where": {"type": "string", "description": "Preset location: center/tl/tr/bl/br.", "nullable": True},
    }
    output_type = "string"

    def forward(
        self,
        zone: float | None = None,
        row: float | None = None,
        col: float | None = None,
        where: str | None = None,
    ) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        result = self.camera.set_af_zone(
            zone=None if zone is None else int(zone),
            row=None if row is None else int(row),
            col=None if col is None else int(col),
            where=where,
        )
        return f"✅ AF zone set: {result}"


class AtomCameraGetAfStatusCode(_CameraAtom):
    name = "atom_camera_get_af_status_code"
    description = "Get autofocus feedback code (raw integer)."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        code = self.camera.get_af_status_code()
        return f"✅ AF status code: {int(code)}"


class AtomCameraGetAfStatus(_CameraAtom):
    name = "atom_camera_get_af_status"
    description = "Get autofocus feedback status."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        status = self.camera.get_af_status()
        return f"✅ AF status: {status}"


class AtomCameraGetFocusPosition(_CameraAtom):
    name = "atom_camera_get_focus_position"
    description = "Get manual focus position."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        pos = self.camera.get_focus_position()
        return f"✅ Focus position: {int(pos)}"


class AtomCameraSetFocusPosition(_CameraAtom):
    name = "atom_camera_set_focus_position"
    description = "Set manual focus position."
    inputs = {
        "pos": {"type": "number", "description": "Focus position value."},
        "clamp": {"type": "boolean", "description": "Clamp to allowed range.", "default": True, "nullable": True},
    }
    output_type = "string"

    def forward(self, pos: float, clamp: bool = True) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        result = self.camera.set_focus_position(int(pos), clamp=bool(clamp))
        return f"✅ Focus position set: {result}"


class AtomCameraWait(_CameraAtom):
    name = "atom_camera_wait"
    description = "Block until interrupted (Ctrl+C)."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        self.camera.wait()
        return "✅ Camera wait complete."


class AtomCameraIsRunning(_CameraAtom):
    name = "atom_camera_is_running"
    description = "Check whether the camera stream is running."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        running = bool(getattr(self.camera, "_running", False))
        return f"✅ Camera running: {running}"


class AtomCameraCapture(_CameraAtom):
    name = "atom_camera_capture"
    description = "Capture a focused frame and save it into the provided directory."
    inputs = {
        "photo_dir": {"type": "string", "description": "Directory to save the capture."},
        "min_blur": {"type": "number", "description": "Minimum blur score for autofocusing.", "default": 3.5, "nullable": True},
    }
    output_type = "string"

    def forward(self, photo_dir: str, min_blur: float = 3.5) -> str:
        if not self._require_camera():
            return "❌ Camera controller unavailable."
        os.makedirs(photo_dir, exist_ok=True)
        path, score = self.camera.wait_focus_and_take_photo(path=photo_dir, min_blur=min_blur)
        return f"✅ Captured image: {path} (score={score:.2f})"
