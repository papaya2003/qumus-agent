from __future__ import annotations

import os
import re
import time

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomCameraAutofocusOff,
    AtomCameraAutofocusOnce,
    AtomCameraGetFocusPosition,
    AtomCameraScanFocusAndTakePhoto,
    AtomCameraSetFocusPosition,
    AtomCameraStart,
    AtomCameraStop,
    AtomCameraWaitFocusAndTakePhoto,
    AtomFocusMoveRelative,
    AtomTurretRotate,
)
from agents.processing_agent.workflows.stacking.molecule_flows.utils import get_stacking_run_dirs
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeRecordDevice(BaseWorkflow):
    name = "molecule_record_device"
    description = "Capture one full device image set in the current stacking run: 5X, then 10X, 20X, and 100X images with the same focus-tuning logic used in autosearch."
    inputs = {
        "open_live_view": {"type": "boolean", "description": "Open live view during capture.", "default": True, "nullable": True},
    }
    output_type = "string"

    A1 = 18.1367
    A2 = 4.4029
    FOCUS_REF_POS = 330
    TURRET_SETTLE_S = 1.0
    BLUR_5X = 7.0
    BLUR_10X = 6.5
    BLUR_20X = 5.5
    BLUR_100X = 5.0
    AUTOFOCUS_WAIT_S = 5.0

    PLAN_STEPS = [
        "Prepare Device Capture Directory And Camera",
        "Initial Focus And 5X Capture",
        "Capture 10X Device Image",
        "Tune Focus And Capture 20X Device Image",
        "Tune Focus And Capture 100X Device Image",
        "Finalize Device Imaging",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.cam_autofocus_once = AtomCameraAutofocusOnce(*args, **kwargs)
        self.cam_autofocus_off = AtomCameraAutofocusOff(*args, **kwargs)
        self.cam_get_focus_pos = AtomCameraGetFocusPosition(*args, **kwargs)
        self.cam_set_focus_pos = AtomCameraSetFocusPosition(*args, **kwargs)
        self.cam_wait_focus_photo = AtomCameraWaitFocusAndTakePhoto(*args, **kwargs)
        self.cam_scan_focus_photo = AtomCameraScanFocusAndTakePhoto(*args, **kwargs)
        self.focus_move = AtomFocusMoveRelative(*args, **kwargs)
        self.turret_rotate = AtomTurretRotate(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    @staticmethod
    def _parse_int_from_message(message: str) -> int | None:
        match = re.search(r"(-?\d+)", str(message))
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return None

    def forward(self, open_live_view: bool = True) -> str:
        if self.camera is None or self.focus is None or self.turret is None:
            return "❌ Required hardware (camera/focus/turret) unavailable."

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []

        device_dirs = get_stacking_run_dirs(sample_label="device")
        device_dir = device_dirs["sample_dir"]

        self.broadcaster_step.forward(0)
        results.append(self.cam_start(open_live_view=open_live_view))

        self.broadcaster_step.forward(1)
        results.append(self.cam_autofocus_once())
        preview_path = os.path.join(device_dir, "device_5X.jpg")
        msg_preview = self.cam_wait_focus_photo(
            photo_dir=device_dir,
            save_path=preview_path,
            min_blur=self.BLUR_5X,
        )
        results.append(f"[5X] {msg_preview}")

        self.broadcaster_step.forward(2)
        results.append(self.turret_rotate(position=2))
        time.sleep(self.TURRET_SETTLE_S)
        results.append(self.cam_autofocus_once())
        time.sleep(self.AUTOFOCUS_WAIT_S)
        save10 = os.path.join(device_dir, "device_10X.jpg")
        msg10 = self.cam_wait_focus_photo(
            photo_dir=device_dir,
            save_path=save10,
            min_blur=self.BLUR_10X,
        )
        results.append(f"[10X] {msg10}")

        self.broadcaster_step.forward(3)
        try:
            s1_msg = self.cam_get_focus_pos()
            s1 = self._parse_int_from_message(s1_msg)
            if s1 is None:
                raise RuntimeError("Failed to parse focus position after 10X capture.")
            results.append(self.cam_set_focus_pos(pos=self.FOCUS_REF_POS))
            dz1 = float(self.A1) * (float(s1) - float(self.FOCUS_REF_POS))
            if 100 <= s1 <= 700:
                results.append(self.focus_move(delta=dz1, wait=True))
                time.sleep(abs(dz1) * 0.005)
            else:
                results.append("[WARN] 10X focus position out of expected range; skipping mapped focus move.")
        except Exception as exc:
            results.append(f"[WARN] 10X focus-position mapping failed: {exc}")

        results.append(self.turret_rotate(position=3))
        time.sleep(self.TURRET_SETTLE_S)
        results.append(self.cam_autofocus_once())
        time.sleep(self.AUTOFOCUS_WAIT_S)
        save20 = os.path.join(device_dir, "device_20X.jpg")
        msg20 = self.cam_wait_focus_photo(
            photo_dir=device_dir,
            save_path=save20,
            min_blur=self.BLUR_20X,
        )
        results.append(f"[20X] {msg20}")

        self.broadcaster_step.forward(4)
        try:
            s2_msg = self.cam_get_focus_pos()
            s2 = self._parse_int_from_message(s2_msg)
            if s2 is None:
                raise RuntimeError("Failed to parse focus position after 20X capture.")
            results.append(self.cam_set_focus_pos(pos=self.FOCUS_REF_POS))
            dz2 = float(self.A2) * (float(s2) - float(self.FOCUS_REF_POS))
            if 100 <= s2 <= 700:
                results.append(self.focus_move(delta=dz2, wait=True))
                time.sleep(abs(dz2) * 0.005)
            else:
                results.append("[WARN] 20X focus position out of expected range; skipping mapped focus move.")
        except Exception as exc:
            results.append(f"[WARN] 20X focus-position mapping failed: {exc}")

        results.append(self.turret_rotate(position=4))
        time.sleep(2.0)
        try:
            results.append(self.cam_autofocus_off())
        except Exception:
            pass
        save100 = os.path.join(device_dir, "device_100X.jpg")
        msg100 = self.cam_scan_focus_photo(
            photo_dir=device_dir,
            save_path=save100,
            min_blur=self.BLUR_100X,
        )
        results.append(f"[100X] {msg100}")

        self.broadcaster_step.forward(5)
        try:
            results.append(self.turret_rotate(position=2))
            time.sleep(self.TURRET_SETTLE_S)
        except Exception as exc:
            results.append(f"[WARN] Failed to return turret to 10X position: {exc}")
        results.append(self.cam_stop())

        results.append(f"Device image set saved under: {device_dir}")
        return "\n".join(results)
