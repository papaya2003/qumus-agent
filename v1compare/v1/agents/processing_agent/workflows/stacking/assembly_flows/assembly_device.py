from __future__ import annotations

import os
import re
import time
from typing import Optional

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from common.services import DeviceInventoryService
from agents.processing_agent.workflows.search.molecule_flows import (
    MoleculeCornerDetection,
    MoleculeFlakeRefindAccurate,
    MoleculeFlakeRefindRough,
    MoleculePickupChipToTransferStage,
    MoleculeStoreSearchSiliconChip,
)
from agents.processing_agent.workflows.search.atom_flows import (
    AtomCameraStart,
    AtomCameraStop,
    AtomSampleStageMoveLHome,
    AtomSampleStageMoveToMicroscope,
)
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomTransferConnect,
    AtomTransferMoveAxisTo,
)
from agents.processing_agent.workflows.stacking.molecule_flows import (
    MoleculeAdjustTouchingPoint,
    MoleculeDropDown,
    MoleculeInitializeStamp,
    MoleculeLoadStamp,
    MoleculePickUp,
    MoleculeRecordDevice,
    MoleculeRecycleStamp,
    MoleculeRemoveStamp,
    MoleculeSetInitialTemp,
)
from agents.processing_agent.workflows.stacking.molecule_flows.utils import get_stacking_run_dirs
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class AssemblyDevice(BaseWorkflow):
    name = "assembly_device"
    flow_type = "assembly"
    camera_route = "right"
    description = "Produce a whole device by stacking flake A and flake B onto bottom electrode in order."
    inputs = {
        "flake_a_coord": {"type": "array", "description": "Flake A coordinate as [x, y]."},
        "flake_a_image_path": {"type": "string", "description": "Flake A image path.", "nullable": True, "default": None},
        "flake_a_angle": {"type": "number", "description": "Flake A angle.", "nullable": True, "default": 0},
        "shift_x_a": {"type": "number", "description": "Optional design shift_x for flake A accurate refind.", "nullable": True, "default": 0},
        "shift_y_a": {"type": "number", "description": "Optional design shift_y for flake A accurate refind.", "nullable": True, "default": 0},
        "chip_a_corner_coord": {"type": "array", "description": "Chip A reference BL corner coordinate as [x, y]."},
        "chip_a_corner_image_path": {"type": "string", "description": "Chip A reference BL corner image path."},
        "chip_a_id": {"type": "integer", "description": "Chip A id for pickup.", "nullable": True},
        "flake_b_coord": {"type": "array", "description": "Flake B coordinate as [x, y].", "nullable": True},
        "flake_b_image_path": {"type": "string", "description": "Flake B image path.", "nullable": True, "default": None},
        "flake_b_angle": {"type": "number", "description": "Flake B angle.", "nullable": True, "default": 0},
        "shift_x_b": {"type": "number", "description": "Optional design shift_x for flake B accurate refind.", "nullable": True, "default": 0},
        "shift_y_b": {"type": "number", "description": "Optional design shift_y for flake B accurate refind.", "nullable": True, "default": 0},
        "chip_b_corner_coord": {"type": "array", "description": "Chip B reference BL corner coordinate as [x, y].", "nullable": True},
        "chip_b_corner_image_path": {"type": "string", "description": "Chip B reference BL corner image path.", "nullable": True},
        "chip_b_id": {"type": "integer", "description": "Chip B id for pickup.", "nullable": True},
        "bottom_electrode_coord": {"type": "array", "description": "Reserved: Bottom electrode coordinate as [x, y].", "nullable": True},
        "bottom_electrode_image_path": {"type": "string", "description": "Reserved: Bottom electrode image path.", "nullable": True, "default": None},
        "bottom_electrode_angle": {"type": "number", "description": "Bottom electrode angle.", "nullable": True, "default": 0},
        "electrode_chip_corner_coord": {"type": "array", "description": "Reserved: Electrode chip reference BL corner coordinate as [x, y].", "nullable": True},
        "electrode_chip_corner_image_path": {"type": "string", "description": "Reserved: Electrode chip reference BL corner image path.", "nullable": True},
        "electrode_chip_id": {"type": "integer", "description": "Reserved: Electrode chip id for pickup.", "nullable": True},
    }
    output_type = "string"

    PLAN_STEPS = [
        "Start New Stacking Run",
        "Set Initial Transfer Temperature",
        "Load Stamp",
        "Remove Stamp to Safe Position",
        "Pickup Chip A To Transfer Stage",
        "Corner Detection For Chip A",
        "Rough Refind Flake A",
        "Accurate Refind Flake A",
        "Initialize Stamp",
        "Adjust Touching Point For Chip A",
        "Pick Up Flake A",
        "Remove Stamp",
        "Store Chip A",
        "Pickup Chip B To Transfer Stage",
        "Corner Detection For Chip B",
        "Rough Refind Flake B",
        "Accurate Refind Flake B",
        "Initialize Stamp For Chip B",
        "Pick Up Flake B",
        "Remove Stamp For Chip B",
        "Store Chip B",
        "Pickup Electrode Chip To Transfer Stage",
        "Corner Detection For Electrode Chip",
        "Rough Refind Bottom Electrode",
        "Accurate Refind Bottom Electrode",
        "Initialize Stamp For Electrode Chip",
        "Drop Down Bottom Electrode",
        "Remove Stamp For Electrode Chip",
        "Record Device Images",
        "Store Electrode Chip",
        "Recycle Stamp",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.molecule_set_initial_temp = MoleculeSetInitialTemp(*args, **kwargs)
        self.molecule_pickup_chip = MoleculePickupChipToTransferStage(*args, **kwargs)
        self.molecule_store_chip = MoleculeStoreSearchSiliconChip(*args, **kwargs)
        self.molecule_corner_detection = MoleculeCornerDetection(*args, **kwargs)
        self.molecule_flake_refind_accurate = MoleculeFlakeRefindAccurate(*args, **kwargs)
        self.molecule_flake_refind_rough = MoleculeFlakeRefindRough(*args, **kwargs)
        self.molecule_adjust_touching_point = MoleculeAdjustTouchingPoint(*args, **kwargs)
        self.molecule_drop_down = MoleculeDropDown(*args, **kwargs)
        self.molecule_initialize_stamp = MoleculeInitializeStamp(*args, **kwargs)
        self.molecule_load_stamp = MoleculeLoadStamp(*args, **kwargs)
        self.molecule_pick_up = MoleculePickUp(*args, **kwargs)
        self.molecule_record_device = MoleculeRecordDevice(*args, **kwargs)
        self.molecule_recycle_stamp = MoleculeRecycleStamp(*args, **kwargs)
        self.molecule_remove_stamp = MoleculeRemoveStamp(*args, **kwargs)
        self.device_service = DeviceInventoryService()
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.atom_stage_l_home = AtomSampleStageMoveLHome(*args, **kwargs)
        self.atom_stage_to_microscope = AtomSampleStageMoveToMicroscope(*args, **kwargs)
        self.atom_transfer_connect = AtomTransferConnect(*args, **kwargs)
        self.atom_transfer_move_axis_to = AtomTransferMoveAxisTo(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    @staticmethod
    def _parse_xy(value, label: str) -> list[float]:
        if value is None:
            raise ValueError(f"{label} is required.")
        try:
            if len(value) != 2:
                raise ValueError
            return [float(value[0]), float(value[1])]
        except Exception as exc:
            raise ValueError(f"{label} must be [x, y].") from exc

    @staticmethod
    def _parse_saved_corner_path(message: str) -> Optional[str]:
        match = re.search(r"\[CORNER\]\s+BL saved:\s+(.+?)(?:\s+\(score=|\s*$)", message, flags=re.MULTILINE)
        if not match:
            return None
        return match.group(1).strip()

    @staticmethod
    def _parse_corner_pos(message: str) -> Optional[list[float]]:
        patterns = [
            r"\[FINAL-CORNER-POS\]\s+BL:\s+\(([-+0-9.eE]+),\s*([-+0-9.eE]+)\)",
            r"\[CORNER-POS\]\s+BL:\s+\(([-+0-9.eE]+),\s*([-+0-9.eE]+)\)",
        ]
        for pattern in patterns:
            match = re.search(pattern, message)
            if match:
                return [float(match.group(1)), float(match.group(2))]
        return None

    @staticmethod
    def _run_number_from_dir(run_dir: str) -> int:
        name = os.path.basename(run_dir.rstrip("\\/"))
        match = re.fullmatch(r"run_(\d+)", name)
        if not match:
            raise ValueError(f"Unable to parse run number from {run_dir}")
        return int(match.group(1))

    def forward(
        self,
        flake_a_coord,
        chip_a_corner_coord,
        chip_a_corner_image_path: str,
        chip_a_id: Optional[int] = None,
        flake_a_image_path: Optional[str] = None,
        flake_a_angle: Optional[float] = 0,
        shift_x_a: Optional[float] = 0,
        shift_y_a: Optional[float] = 0,
        flake_b_coord=None,
        flake_b_image_path: Optional[str] = None,
        chip_b_corner_coord=None,
        chip_b_corner_image_path: Optional[str] = None,
        chip_b_id: Optional[int] = None,
        flake_b_angle: Optional[float] = 0,
        shift_x_b: Optional[float] = 0,
        shift_y_b: Optional[float] = 0,
        bottom_electrode_coord=None,
        bottom_electrode_image_path: Optional[str] = None,
        electrode_chip_corner_coord=None,
        electrode_chip_corner_image_path: Optional[str] = None,
        electrode_chip_id: Optional[int] = None,
        bottom_electrode_angle: Optional[float] = 0,
    ) -> str:
        del (
            bottom_electrode_angle,
        )

        results: list[str] = []
        self.broadcaster_plan.forward(self.PLAN_STEPS)

        try:
            flake_a_xy = self._parse_xy(flake_a_coord, "flake_a_coord")
            chip_a_corner_xy = self._parse_xy(chip_a_corner_coord, "chip_a_corner_coord")
            flake_b_xy = self._parse_xy(flake_b_coord, "flake_b_coord")
            chip_b_corner_xy = self._parse_xy(chip_b_corner_coord, "chip_b_corner_coord")
            bottom_electrode_xy = self._parse_xy(bottom_electrode_coord, "bottom_electrode_coord")
            electrode_chip_corner_xy = self._parse_xy(electrode_chip_corner_coord, "electrode_chip_corner_coord")
        except Exception as exc:
            return f"❌ {exc}"

        if not chip_a_corner_image_path:
            return "❌ chip_a_corner_image_path is required."
        if not flake_a_image_path:
            return "❌ flake_a_image_path is required."
        if not chip_b_corner_image_path:
            return "❌ chip_b_corner_image_path is required."
        if not flake_b_image_path:
            return "❌ flake_b_image_path is required."
        if not electrode_chip_corner_image_path:
            return "❌ electrode_chip_corner_image_path is required."
        if not bottom_electrode_image_path:
            return "❌ bottom_electrode_image_path is required."
        if not os.path.exists(chip_a_corner_image_path):
            return f"❌ chip_a_corner_image_path does not exist: {chip_a_corner_image_path}"
        if not os.path.exists(flake_a_image_path):
            return f"❌ flake_a_image_path does not exist: {flake_a_image_path}"
        if not os.path.exists(chip_b_corner_image_path):
            return f"❌ chip_b_corner_image_path does not exist: {chip_b_corner_image_path}"
        if not os.path.exists(flake_b_image_path):
            return f"❌ flake_b_image_path does not exist: {flake_b_image_path}"
        if not os.path.exists(electrode_chip_corner_image_path):
            return f"❌ electrode_chip_corner_image_path does not exist: {electrode_chip_corner_image_path}"
        if not os.path.exists(bottom_electrode_image_path):
            return f"❌ bottom_electrode_image_path does not exist: {bottom_electrode_image_path}"

        self.broadcaster_step.forward(0)
        # A new workflow must reserve a new physical run even when this Python
        # process previously handled another stack.  Reuse is allowed only via
        # the explicit checkpoint-resume path.
        run_dirs = get_stacking_run_dirs(create_new=True)
        run_dir = run_dirs["run_dir"]
        run_number = self._run_number_from_dir(run_dir)
        results.append(f"Starting stacking run: {run_dir}")
        try:
            self.broadcaster_step.forward(1)
            res_temp = self.molecule_set_initial_temp.forward()
            results.append(f"Step 1 (Set Initial Temp): {res_temp}")

            self.broadcaster_step.forward(2)
            res_load_stamp = self.molecule_load_stamp.forward()
            results.append(f"Step 2 (Load Stamp): {res_load_stamp}")

            self.broadcaster_step.forward(3)
            res_pre_init_x = self.atom_transfer_move_axis_to.forward(axis="x", value=6.0)
            res_pre_init_y = self.atom_transfer_move_axis_to.forward(axis="y", value=6.5)
            res_cam_live = self.cam_start.forward(open_live_view=True)
            res_move_l_preview = self.atom_transfer_move_axis_to.forward(axis="L", value=120.0)
            time.sleep(90.0)
            res_cam_stop = self.cam_stop.forward()
            res_remove_loaded_stamp = self.atom_transfer_move_axis_to.forward(axis="L", value=160.0)
            results.append(
                "Step 3 (Remove Stamp to Safe Position): "
                f"{res_pre_init_x} | {res_pre_init_y} | {res_cam_live} | "
                f"{res_move_l_preview} | waited 60s | {res_cam_stop} | {res_remove_loaded_stamp}"
            )

            chip_a_dirs = get_stacking_run_dirs(run_number=run_number, sample_label="chip_a")
            chip_b_dirs = get_stacking_run_dirs(run_number=run_number, sample_label="chip_b")
            electrode_dirs = get_stacking_run_dirs(run_number=run_number, sample_label="electrode")

            self.broadcaster_step.forward(4)
            res_stage_home_a = self.atom_stage_l_home.forward()
            res_pick_chip = self.molecule_pickup_chip.forward(
                chip_id=chip_a_id,
            )
            results.append(f"Step 4 (Pickup Chip A): {res_stage_home_a} | {res_pick_chip}")

            self.broadcaster_step.forward(5)
            res_stage_micro_a = self.atom_stage_to_microscope.forward()
            chip_a_corner_save_name = "chip_a_corner_bl.jpg"
            res_corner = self.molecule_corner_detection.forward(
                save_dir=chip_a_dirs["corner_dir"],
                save_name=chip_a_corner_save_name,
            )
            results.append(f"Step 5 (Corner Detection): {res_stage_micro_a} | {res_corner}")

            new_corner_image = self._parse_saved_corner_path(res_corner) or os.path.join(chip_a_dirs["corner_dir"], chip_a_corner_save_name)
            new_corner_coord = self._parse_corner_pos(res_corner)
            if not os.path.exists(new_corner_image):
                raise RuntimeError(f"Corner detection did not produce the expected image: {new_corner_image}")
            if new_corner_coord is None:
                raise RuntimeError("Failed to parse new BL corner coordinates from molecule_corner_detection output.")

            self.broadcaster_step.forward(6)
            res_refind = self.molecule_flake_refind_rough.forward(
                corner_BL_image=chip_a_corner_image_path,
                new_image=new_corner_image,
                corner_BL_coord=chip_a_corner_xy,
                new_corner_coord=new_corner_coord,
                flake_coord=flake_a_xy,
            )
            results.append(f"Step 6 (Rough Refind Flake A): {res_refind}")

            self.broadcaster_step.forward(7)
            res_refind_accurate = self.molecule_flake_refind_accurate.forward(
                image_path=flake_a_image_path,
                alignment_dir=chip_a_dirs["alignment_dir"],
                save_stem="flake_A",
                shift_x=shift_x_a,
                shift_y=shift_y_a,
            )
            results.append(f"Step 7 (Accurate Refind Flake A): {res_refind_accurate}")

            self.broadcaster_step.forward(8)
            res_transfer_connect_a = self.atom_transfer_connect.forward()
            res_pre_init_x = self.atom_transfer_move_axis_to.forward(axis="x", value=6.0)
            res_pre_init_y = self.atom_transfer_move_axis_to.forward(axis="y", value=6.5)
            res_init_stamp = self.molecule_initialize_stamp.forward()
            results.append(
                "Step 8 (Initialize Stamp): "
                f"{res_transfer_connect_a} | {res_pre_init_x} | {res_pre_init_y} | {res_init_stamp}"
            )

            self.broadcaster_step.forward(9)
            res_adjust_touching = self.molecule_adjust_touching_point.forward(run_number=run_number, sample_label="chip_a")
            results.append(f"Step 9 (Adjust Touching Point A): {res_adjust_touching}")

            self.broadcaster_step.forward(10)
            res_pick_up = self.molecule_pick_up.forward(run_number=run_number, sample_label="chip_a")
            results.append(f"Step 10 (Pick Up Flake A): {res_pick_up}")

            self.broadcaster_step.forward(11)
            res_remove_stamp = self.molecule_remove_stamp.forward()
            results.append(f"Step 11 (Remove Stamp): {res_remove_stamp}")

            self.broadcaster_step.forward(12)
            res_stage_home_store_a = self.atom_stage_l_home.forward()
            res_store_chip_a = self.molecule_store_chip.forward(
                chip_id=chip_a_id,
            )
            results.append(f"Step 12 (Store Chip A): {res_stage_home_store_a} | {res_store_chip_a}")

            self.broadcaster_step.forward(13)
            res_stage_home_b = self.atom_stage_l_home.forward()
            res_pick_chip_b = self.molecule_pickup_chip.forward(
                chip_id=chip_b_id,
            )
            results.append(f"Step 13 (Pickup Chip B): {res_stage_home_b} | {res_pick_chip_b}")

            self.broadcaster_step.forward(14)
            res_stage_micro_b = self.atom_stage_to_microscope.forward()
            chip_b_corner_save_name = "chip_b_corner_bl.jpg"
            res_corner_b = self.molecule_corner_detection.forward(
                save_dir=chip_b_dirs["corner_dir"],
                save_name=chip_b_corner_save_name,
            )
            results.append(f"Step 14 (Corner Detection Chip B): {res_stage_micro_b} | {res_corner_b}")

            new_corner_image_b = self._parse_saved_corner_path(res_corner_b) or os.path.join(chip_b_dirs["corner_dir"], chip_b_corner_save_name)
            new_corner_coord_b = self._parse_corner_pos(res_corner_b)
            if not os.path.exists(new_corner_image_b):
                raise RuntimeError(f"Corner detection did not produce the expected image for chip B: {new_corner_image_b}")
            if new_corner_coord_b is None:
                raise RuntimeError("Failed to parse new BL corner coordinates from chip B corner detection output.")

            self.broadcaster_step.forward(15)
            res_refind_b = self.molecule_flake_refind_rough.forward(
                corner_BL_image=chip_b_corner_image_path,
                new_image=new_corner_image_b,
                corner_BL_coord=chip_b_corner_xy,
                new_corner_coord=new_corner_coord_b,
                flake_coord=flake_b_xy,
            )
            results.append(f"Step 15 (Rough Refind Flake B): {res_refind_b}")

            self.broadcaster_step.forward(16)
            res_refind_accurate_b = self.molecule_flake_refind_accurate.forward(
                image_path=flake_b_image_path,
                alignment_dir=chip_b_dirs["alignment_dir"],
                save_stem="flake_B",
                shift_x=shift_x_b,
                shift_y=shift_y_b,
            )
            results.append(f"Step 16 (Accurate Refind Flake B): {res_refind_accurate_b}")

            self.broadcaster_step.forward(17)
            res_init_stamp_b = self.molecule_initialize_stamp.forward()
            results.append(f"Step 17 (Initialize Stamp For Chip B): {res_init_stamp_b}")

            self.broadcaster_step.forward(18)
            res_pick_up_b = self.molecule_pick_up.forward(run_number=run_number, sample_label="chip_b")
            results.append(f"Step 18 (Pick Up Flake B): {res_pick_up_b}")

            self.broadcaster_step.forward(19)
            res_remove_stamp_b = self.molecule_remove_stamp.forward()
            results.append(f"Step 19 (Remove Stamp For Chip B): {res_remove_stamp_b}")

            self.broadcaster_step.forward(20)
            res_stage_home_store_b = self.atom_stage_l_home.forward()
            res_store_chip_b = self.molecule_store_chip.forward(
                chip_id=chip_b_id,
            )
            results.append(f"Step 20 (Store Chip B): {res_stage_home_store_b} | {res_store_chip_b}")

            self.broadcaster_step.forward(21)
            res_stage_home_electrode = self.atom_stage_l_home.forward()
            res_pick_electrode = self.molecule_pickup_chip.forward(
                chip_id=electrode_chip_id,
            )
            results.append(f"Step 21 (Pickup Electrode Chip): {res_stage_home_electrode} | {res_pick_electrode}")

            self.broadcaster_step.forward(22)
            res_stage_micro_electrode = self.atom_stage_to_microscope.forward()
            electrode_corner_save_name = "electrode_chip_corner_bl.jpg"
            res_corner_electrode = self.molecule_corner_detection.forward(
                save_dir=electrode_dirs["corner_dir"],
                save_name=electrode_corner_save_name,
            )
            results.append(
                f"Step 22 (Corner Detection Electrode): "
                f"{res_stage_micro_electrode} | {res_corner_electrode}"
            )

            new_corner_image_electrode = self._parse_saved_corner_path(res_corner_electrode) or os.path.join(
                electrode_dirs["corner_dir"],
                electrode_corner_save_name,
            )
            new_corner_coord_electrode = self._parse_corner_pos(res_corner_electrode)
            if not os.path.exists(new_corner_image_electrode):
                raise RuntimeError(
                    f"Corner detection did not produce the expected image for electrode chip: {new_corner_image_electrode}"
                )
            if new_corner_coord_electrode is None:
                raise RuntimeError("Failed to parse new BL corner coordinates from electrode chip corner detection output.")

            self.broadcaster_step.forward(23)
            res_refind_electrode = self.molecule_flake_refind_rough.forward(
                corner_BL_image=electrode_chip_corner_image_path,
                new_image=new_corner_image_electrode,
                corner_BL_coord=electrode_chip_corner_xy,
                new_corner_coord=new_corner_coord_electrode,
                flake_coord=bottom_electrode_xy,
            )
            results.append(f"Step 23 (Rough Refind Bottom Electrode): {res_refind_electrode}")

            self.broadcaster_step.forward(24)
            res_refind_accurate_electrode = self.molecule_flake_refind_accurate.forward(
                image_path=bottom_electrode_image_path,
                alignment_dir=electrode_dirs["alignment_dir"],
                save_stem="bottom_electrode",
            )
            results.append(
                f"Step 24 (Accurate Refind Bottom Electrode): {res_refind_accurate_electrode}"
            )

            self.broadcaster_step.forward(25)
            res_init_stamp_electrode = self.molecule_initialize_stamp.forward()
            results.append(f"Step 25 (Initialize Stamp For Electrode Chip): {res_init_stamp_electrode}")

            self.broadcaster_step.forward(26)
            res_drop_down = self.molecule_drop_down.forward(run_number=run_number, sample_label="electrode")
            results.append(f"Step 26 (Drop Down Bottom Electrode): {res_drop_down}")

            self.broadcaster_step.forward(27)
            res_remove_stamp_electrode = self.molecule_remove_stamp.forward()
            results.append(f"Step 27 (Remove Stamp For Electrode Chip): {res_remove_stamp_electrode}")

            self.broadcaster_step.forward(28)
            res_record_device = self.molecule_record_device.forward()
            results.append(f"Step 28 (Record Device Images): {res_record_device}")

            device_dirs = get_stacking_run_dirs(run_number=run_number, sample_label="device")
            device_5x_path = os.path.join(device_dirs["sample_dir"], "device_5X.jpg")
            device_10x_path = os.path.join(device_dirs["sample_dir"], "device_10X.jpg")
            device_20x_path = os.path.join(device_dirs["sample_dir"], "device_20X.jpg")
            device_100x_path = os.path.join(device_dirs["sample_dir"], "device_100X.jpg")
            device_record = self.device_service.record_device(
                name="graphene transistor",
                chip_id=int(electrode_chip_id),
                flake_id=None,
                status="fabricated",
                notes=f"Recorded after assembly_device completed in stacking run {run_number}.",
                image_5x_path=device_5x_path if os.path.exists(device_5x_path) else None,
                image_10x_path=device_10x_path if os.path.exists(device_10x_path) else None,
                image_20x_path=device_20x_path if os.path.exists(device_20x_path) else None,
                image_100x_path=device_100x_path if os.path.exists(device_100x_path) else None,
            )
            results.append(f"Device DB record created: id={device_record.id}")

            self.broadcaster_step.forward(29)
            res_stage_home_store_electrode = self.atom_stage_l_home.forward()
            res_store_chip_electrode = self.molecule_store_chip.forward(
                chip_id=electrode_chip_id,
            )
            results.append(
                "Step 29 (Store Electrode Chip): "
                f"{res_stage_home_store_electrode} | {res_store_chip_electrode}"
            )

            self.broadcaster_step.forward(30)
            res_recycle_stamp = self.molecule_recycle_stamp.forward()
            results.append(f"Step 30 (Recycle Stamp): {res_recycle_stamp}")

        except Exception as exc:
            results.append(f"❌ Assembly device failed: {exc}")
            return "\n".join(results)

        results.append(f"Assembly device draft complete. run_number={run_number}")
        return "\n".join(results)
