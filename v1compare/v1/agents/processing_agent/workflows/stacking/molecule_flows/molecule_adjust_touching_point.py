from __future__ import annotations

import os
import time

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomCameraAutofocusOnce,
    AtomCameraStart,
    AtomCameraStop,
    AtomCameraWaitFocusAndTakePhoto,
    AtomTransferConnect,
    AtomTransferGetPositions,
    AtomTransferMoveAxisBy,
    AtomTransferSetKSTSpeed,
    AtomTurretRotate,
)
from agents.processing_agent.workflows.stacking.molecule_flows.utils import (
    convert_dist_unit,
    detect_first_touching_point,
    get_stacking_run_dirs,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculeAdjustTouchingPoint(BaseWorkflow):
    name = "molecule_adjust_touching_point"
    description = "Initialize stacking hardware, detect the first touching point, and move it to the target pickup position."
    inputs = {
        "run_number": {
            "type": "integer",
            "description": "Stacking run number used to place outputs under images/stacking/run_<N>.",
            "default": 1,
            "nullable": True,
        },
        "sample_label": {
            "type": "string",
            "description": "Optional subfolder label inside the stacking run, e.g. chip_a or chip_b.",
            "nullable": True,
        },
    }
    output_type = "string"

    DELTA_Z = 0.2
    TARGET_TP_DIST = 300
    FOCUS_WAIT_S = 5.0
    POST_CAPTURE_BLUR_5X = 5.5

    PLAN_STEPS = [
        "Initialize Hardware",
        "Capture Initial 5X View",
        "Find And Adjust First Touching Point",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transfer_connect = AtomTransferConnect(*args, **kwargs)
        self.transfer_set_kst_speed = AtomTransferSetKSTSpeed(*args, **kwargs)
        self.transfer_move_axis_by = AtomTransferMoveAxisBy(*args, **kwargs)
        self.transfer_get_positions = AtomTransferGetPositions(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.cam_autofocus_once = AtomCameraAutofocusOnce(*args, **kwargs)
        self.cam_wait_focus_photo = AtomCameraWaitFocusAndTakePhoto(*args, **kwargs)
        self.turret_rotate = AtomTurretRotate(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    def forward(self, run_number: int = 1, sample_label: str | None = None) -> str:
        if self.transfer is None or self.camera is None or self.stage is None or self.turret is None:
            return "❌ Required hardware (transfer/camera/stage/turret) unavailable."

        run_number = int(run_number) if run_number is not None else 1
        run_dirs = get_stacking_run_dirs(run_number=run_number, sample_label=sample_label)
        run_dir = run_dirs["run_dir"]
        sample_dir = run_dirs["sample_dir"]
        output_dir = run_dirs["output_dir"]
        tp_path_init = run_dirs["tp_init_dir"]

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []
        target_pt = (1920, 1080)
        sample_name = sample_label or "default"

        self.broadcaster_step.forward(0)
        print(f"[ADJUST-TP] Start touching-point adjustment for sample '{sample_name}' in run {run_number}", flush=True)
        print(f"[ADJUST-TP] Sample directory: {sample_dir}", flush=True)
        results.append(self.transfer_connect())
        results.append(self.transfer_set_kst_speed(axis="x", max_vel=0.2, accel=0.5))
        results.append(self.transfer_set_kst_speed(axis="y", max_vel=0.2, accel=0.5))
        results.append(self.transfer_set_kst_speed(axis="z", max_vel=0.005, accel=0.1))
        results.append(self.cam_start(open_live_view=True))
        results.append(self.turret_rotate(position=1))
        time.sleep(float(self.FOCUS_WAIT_S))

        self.broadcaster_step.forward(1)
        print("[ADJUST-TP] Step 1: capture initial 5X view", flush=True)
        init_fig_path = os.path.join(output_dir, "initial_view_5x.png")
        results.append(
            self.cam_wait_focus_photo(
                photo_dir=output_dir,
                min_blur=float(self.POST_CAPTURE_BLUR_5X),
                save_path=init_fig_path,
            )
        )
        pre_pos_msg = self.transfer_get_positions()
        results.append(pre_pos_msg)
        print(f"[ADJUST-TP] Initial transfer position: {pre_pos_msg}", flush=True)

        self.broadcaster_step.forward(2)
        print("[ADJUST-TP] Step 2: find first touching point and move it to target position", flush=True)
        touching_pt, _ = detect_first_touching_point(
            self.camera,
            self.transfer,
            tp_path_init,
            os.path.join(output_dir, "tp_pos_init.png"),
        )
        print(f"[ADJUST-TP] First touching point: {touching_pt}", flush=True)
        results.append(self.cam_autofocus_once())
        time.sleep(float(self.FOCUS_WAIT_S))
        results.append(self.transfer_set_kst_speed(axis="z", max_vel=0.05, accel=0.1))
        results.append(self.transfer_move_axis_by(axis="z", delta=self.DELTA_Z))

        touching_aim_pt = (target_pt[0], target_pt[1] - self.TARGET_TP_DIST)
        delta_x = -convert_dist_unit(touching_aim_pt[0] - touching_pt[0], view="5x")
        delta_y = -convert_dist_unit(touching_aim_pt[1] - touching_pt[1], view="5x")
        print(f"[ADJUST-TP] Move touching point from {touching_pt} to {touching_aim_pt}", flush=True)
        print(f"[ADJUST-TP] Moving distance: dx={delta_x}, dy={delta_y}", flush=True)
        results.append(self.transfer_move_axis_by(axis="x", delta=delta_x))
        results.append(self.transfer_move_axis_by(axis="y", delta=delta_y))

        results.append(self.cam_stop())
        print("[ADJUST-TP] Touching-point adjustment completed.", flush=True)
        results.append(f"Stacking sample directory: {sample_dir}")
        results.append(f"Stacking run directory: {run_dir}")
        results.append("Touching-point adjustment completed.")
        return "\n".join(results)
