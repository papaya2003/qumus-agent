from __future__ import annotations

import os
import time
from typing import Optional

from agents.processing_agent.workflows.stacking.assembly_flows.assembly_device import AssemblyDevice
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomHeaterOff,
    AtomTransferHeaterRelayOff,
)
from agents.processing_agent.workflows.stacking.molecule_flows.utils import get_stacking_run_dirs
from common.services.processing_checkpoint_service import ProcessingCheckpointService


class AssemblyQuantumHallDevice(AssemblyDevice):
    """Build a four-flake stack by pickup order A -> B -> C -> D."""

    name = "assembly_quantum_hall_device"
    flow_type = "assembly"
    camera_route = "right"
    description = (
        "Produce a quantum Hall device by sequentially picking up flakes A, B, C, and D, "
        "then depositing the four-flake stack onto a bottom electrode. A/B/C/D are "
        "real dry-transfer pickup order, so A should be the final top layer and D "
        "should be the final bottom layer."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.heater_off = AtomHeaterOff(*args, **kwargs)
        self.transfer_heater_relay_off = AtomTransferHeaterRelayOff(*args, **kwargs)

    inputs = {
        # Flake A: first pickup. This one also runs touching-point adjustment.
        "flake_a_coord": {"type": "array", "description": "Flake A coordinate as [x, y]."},
        "flake_a_image_path": {
            "type": "string",
            "description": "Flake A 20X image path.",
            "nullable": True,
            "default": None,
        },
        "flake_a_angle": {
            "type": "number",
            "description": "Reserved flake A angle.",
            "nullable": True,
            "default": 0,
        },
        "shift_x_a": {
            "type": "number",
            "description": "Optional design shift_x for flake A accurate refind.",
            "nullable": True,
            "default": 0,
        },
        "shift_y_a": {
            "type": "number",
            "description": "Optional design shift_y for flake A accurate refind.",
            "nullable": True,
            "default": 0,
        },
        "chip_a_corner_coord": {
            "type": "array",
            "description": "Chip A reference BL corner coordinate as [x, y].",
        },
        "chip_a_corner_image_path": {
            "type": "string",
            "description": "Chip A reference BL corner image path.",
        },
        "chip_a_id": {"type": "integer", "description": "Chip A id for pickup.", "nullable": True},
        # Flake B: second pickup.
        "flake_b_coord": {
            "type": "array",
            "description": "Flake B coordinate as [x, y].",
            "nullable": True,
        },
        "flake_b_image_path": {
            "type": "string",
            "description": "Flake B 20X image path.",
            "nullable": True,
            "default": None,
        },
        "flake_b_angle": {
            "type": "number",
            "description": "Reserved flake B angle.",
            "nullable": True,
            "default": 0,
        },
        "shift_x_b": {
            "type": "number",
            "description": "Optional design shift_x for flake B accurate refind.",
            "nullable": True,
            "default": 0,
        },
        "shift_y_b": {
            "type": "number",
            "description": "Optional design shift_y for flake B accurate refind.",
            "nullable": True,
            "default": 0,
        },
        "chip_b_corner_coord": {
            "type": "array",
            "description": "Chip B reference BL corner coordinate as [x, y].",
            "nullable": True,
        },
        "chip_b_corner_image_path": {
            "type": "string",
            "description": "Chip B reference BL corner image path.",
            "nullable": True,
        },
        "chip_b_id": {"type": "integer", "description": "Chip B id for pickup.", "nullable": True},
        # Flake C: third pickup.
        "flake_c_coord": {
            "type": "array",
            "description": "Flake C coordinate as [x, y].",
            "nullable": True,
        },
        "flake_c_image_path": {
            "type": "string",
            "description": "Flake C 20X image path.",
            "nullable": True,
        },
        "flake_c_angle": {
            "type": "number",
            "description": "Reserved flake C angle.",
            "nullable": True,
            "default": 0,
        },
        "shift_x_c": {
            "type": "number",
            "description": "Optional design shift_x for flake C accurate refind.",
            "nullable": True,
            "default": 0,
        },
        "shift_y_c": {
            "type": "number",
            "description": "Optional design shift_y for flake C accurate refind.",
            "nullable": True,
            "default": 0,
        },
        "chip_c_corner_coord": {
            "type": "array",
            "description": "Chip C reference BL corner coordinate as [x, y].",
            "nullable": True,
        },
        "chip_c_corner_image_path": {
            "type": "string",
            "description": "Chip C reference BL corner image path.",
            "nullable": True,
        },
        "chip_c_id": {"type": "integer", "description": "Chip C id for pickup.", "nullable": True},
        # Flake D: fourth pickup.
        "flake_d_coord": {
            "type": "array",
            "description": "Flake D coordinate as [x, y].",
            "nullable": True,
        },
        "flake_d_image_path": {
            "type": "string",
            "description": "Flake D 20X image path.",
            "nullable": True,
        },
        "flake_d_angle": {
            "type": "number",
            "description": "Reserved flake D angle.",
            "nullable": True,
            "default": 0,
        },
        "shift_x_d": {
            "type": "number",
            "description": "Optional design shift_x for flake D accurate refind.",
            "nullable": True,
            "default": 0,
        },
        "shift_y_d": {
            "type": "number",
            "description": "Optional design shift_y for flake D accurate refind.",
            "nullable": True,
            "default": 0,
        },
        "chip_d_corner_coord": {
            "type": "array",
            "description": "Chip D reference BL corner coordinate as [x, y].",
            "nullable": True,
        },
        "chip_d_corner_image_path": {
            "type": "string",
            "description": "Chip D reference BL corner image path.",
            "nullable": True,
        },
        "chip_d_id": {"type": "integer", "description": "Chip D id for pickup.", "nullable": True},
        # Bottom electrode target for final dropdown.
        "bottom_electrode_coord": {
            "type": "array",
            "description": "Bottom electrode coordinate as [x, y].",
            "nullable": True,
        },
        "bottom_electrode_image_path": {
            "type": "string",
            "description": "Bottom electrode image path.",
            "nullable": True,
            "default": None,
        },
        "bottom_electrode_angle": {
            "type": "number",
            "description": "Reserved bottom electrode angle.",
            "nullable": True,
            "default": 0,
        },
        "electrode_chip_corner_coord": {
            "type": "array",
            "description": "Electrode chip reference BL corner coordinate as [x, y].",
            "nullable": True,
        },
        "electrode_chip_corner_image_path": {
            "type": "string",
            "description": "Electrode chip reference BL corner image path.",
            "nullable": True,
        },
        "electrode_chip_id": {
            "type": "integer",
            "description": "Electrode chip id for pickup.",
            "nullable": True,
        },
        "resume_run_number": {
            "type": "integer",
            "description": "Existing physical run number; only valid with resume_checkpoint_path.",
            "nullable": True,
            "default": None,
        },
        "resume_from_step": {
            "type": "integer",
            "description": "Saved parent-step index; only valid with resume_checkpoint_path.",
            "nullable": True,
            "default": None,
        },
        "resume_checkpoint_path": {
            "type": "string",
            "description": "Validated processing checkpoint path for an interrupted run.",
            "nullable": True,
            "default": None,
        },
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
        "Pickup Chip C To Transfer Stage",
        "Corner Detection For Chip C",
        "Rough Refind Flake C",
        "Accurate Refind Flake C",
        "Initialize Stamp For Chip C",
        "Pick Up Flake C",
        "Remove Stamp For Chip C",
        "Store Chip C",
        "Pickup Chip D To Transfer Stage",
        "Corner Detection For Chip D",
        "Rough Refind Flake D",
        "Accurate Refind Flake D",
        "Initialize Stamp For Chip D",
        "Pick Up Flake D",
        "Remove Stamp For Chip D",
        "Store Chip D",
        "Pickup Electrode Chip To Transfer Stage",
        "Corner Detection For Electrode Chip",
        "Rough Refind Bottom Electrode",
        "Initialize Stamp For Electrode Chip",
        "Drop Down Bottom Electrode",
        "Remove Stamp For Electrode Chip",
        "Record Device Images",
        "Store Electrode Chip",
        "Recycle Stamp",
    ]

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
        flake_c_coord=None,
        flake_c_image_path: Optional[str] = None,
        chip_c_corner_coord=None,
        chip_c_corner_image_path: Optional[str] = None,
        chip_c_id: Optional[int] = None,
        flake_c_angle: Optional[float] = 0,
        shift_x_c: Optional[float] = 0,
        shift_y_c: Optional[float] = 0,
        flake_d_coord=None,
        flake_d_image_path: Optional[str] = None,
        chip_d_corner_coord=None,
        chip_d_corner_image_path: Optional[str] = None,
        chip_d_id: Optional[int] = None,
        flake_d_angle: Optional[float] = 0,
        shift_x_d: Optional[float] = 0,
        shift_y_d: Optional[float] = 0,
        bottom_electrode_coord=None,
        bottom_electrode_image_path: Optional[str] = None,
        electrode_chip_corner_coord=None,
        electrode_chip_corner_image_path: Optional[str] = None,
        electrode_chip_id: Optional[int] = None,
        bottom_electrode_angle: Optional[float] = 0,
        resume_run_number: Optional[int] = None,
        resume_from_step: Optional[int] = None,
        resume_checkpoint_path: Optional[str] = None,
    ) -> str:
        supplied_values = dict(locals())
        original_payload = {
            key: supplied_values.get(key)
            for key in self.inputs
            if not key.startswith("resume_")
        }
        # Rotation inputs remain reserved, matching assembly_device behavior.
        del flake_a_angle, flake_b_angle, flake_c_angle, flake_d_angle, bottom_electrode_angle

        results: list[str] = []
        self.broadcaster_plan.forward(self.PLAN_STEPS)

        assembly_failed = False
        try:
            flake_coords = {
                "a": self._parse_xy(flake_a_coord, "flake_a_coord"),
                "b": self._parse_xy(flake_b_coord, "flake_b_coord"),
                "c": self._parse_xy(flake_c_coord, "flake_c_coord"),
                "d": self._parse_xy(flake_d_coord, "flake_d_coord"),
            }
            corner_coords = {
                "a": self._parse_xy(chip_a_corner_coord, "chip_a_corner_coord"),
                "b": self._parse_xy(chip_b_corner_coord, "chip_b_corner_coord"),
                "c": self._parse_xy(chip_c_corner_coord, "chip_c_corner_coord"),
                "d": self._parse_xy(chip_d_corner_coord, "chip_d_corner_coord"),
            }
            bottom_electrode_xy = self._parse_xy(bottom_electrode_coord, "bottom_electrode_coord")
            electrode_corner_xy = self._parse_xy(
                electrode_chip_corner_coord,
                "electrode_chip_corner_coord",
            )
        except Exception as exc:
            return f"❌ {exc}"

        flake_images = {
            "a": flake_a_image_path,
            "b": flake_b_image_path,
            "c": flake_c_image_path,
            "d": flake_d_image_path,
        }
        corner_images = {
            "a": chip_a_corner_image_path,
            "b": chip_b_corner_image_path,
            "c": chip_c_corner_image_path,
            "d": chip_d_corner_image_path,
        }
        chip_ids = {"a": chip_a_id, "b": chip_b_id, "c": chip_c_id, "d": chip_d_id}
        shifts = {
            "a": (shift_x_a, shift_y_a),
            "b": (shift_x_b, shift_y_b),
            "c": (shift_x_c, shift_y_c),
            "d": (shift_x_d, shift_y_d),
        }

        required_paths = {
            **{f"flake_{label}_image_path": flake_images[label] for label in "abcd"},
            **{f"chip_{label}_corner_image_path": corner_images[label] for label in "abcd"},
            "bottom_electrode_image_path": bottom_electrode_image_path,
            "electrode_chip_corner_image_path": electrode_chip_corner_image_path,
        }
        for field, path in required_paths.items():
            if not path:
                return f"❌ {field} is required."
            if not os.path.exists(path):
                return f"❌ {field} does not exist: {path}"

        for field, chip_id in {
            **{f"chip_{label}_id": chip_ids[label] for label in "abcd"},
            "electrode_chip_id": electrode_chip_id,
        }.items():
            if isinstance(chip_id, bool) or not isinstance(chip_id, int) or chip_id <= 0:
                return f"❌ {field} must be a positive integer."

        checkpoint_service = ProcessingCheckpointService()
        resume_checkpoint: dict = {}
        if (resume_run_number is not None or resume_from_step is not None) and not resume_checkpoint_path:
            return "❌ Resume run/step values require a validated processing checkpoint path."
        if resume_checkpoint_path:
            try:
                resume_checkpoint = checkpoint_service.validate_for_resume(
                    resume_checkpoint_path,
                    workflow_name=self.name,
                    current_session_key=str(os.getenv("QUMUS_UI_SESSION_ID") or ""),
                    current_project_id=str(os.getenv("QUMUS_PROJECT_ID") or ""),
                )
            except ValueError as exc:
                return f"❌ {exc}"
            resume_run_number = int(resume_checkpoint["run_number"])
            resume_from_step = int(resume_checkpoint.get("next_step_index", 0))
            original_payload = dict(resume_checkpoint.get("payload") or original_payload)

        self.broadcaster_step.forward(0)
        run_dirs = (
            get_stacking_run_dirs(run_number=int(resume_run_number))
            if resume_run_number is not None
            else get_stacking_run_dirs(create_new=True)
        )
        run_dir = run_dirs["run_dir"]
        run_number = self._run_number_from_dir(run_dir)
        start_step = max(0, int(resume_from_step or 0))
        if resume_checkpoint_path:
            checkpoint_path = str(resume_checkpoint_path)
            results.append(
                f"Resuming quantum Hall stacking run {run_number} at parent step {start_step}: "
                f"{self.PLAN_STEPS[start_step] if start_step < len(self.PLAN_STEPS) else 'Complete'}"
            )
        else:
            checkpoint_file, resume_checkpoint = checkpoint_service.create(
                run_number=run_number,
                workflow_name=self.name,
                payload=original_payload,
                plan_steps=self.PLAN_STEPS,
                session_key=str(os.getenv("QUMUS_UI_SESSION_ID") or ""),
                project_id=str(os.getenv("QUMUS_PROJECT_ID") or ""),
                original_request=str(os.getenv("QUMUS_ORIGINAL_REQUEST") or ""),
            )
            checkpoint_path = str(checkpoint_file)
            results.append(f"Starting quantum Hall stacking run: {run_dir}")

        runtime_context = dict(resume_checkpoint.get("runtime_context") or {})
        resume_options = dict(resume_checkpoint.get("resume_options") or {})
        skip_stage_home = bool(resume_options.get("skip_stage_home"))
        # The temperature step powers the transfer heater before any motion.
        # Treat resumed runs after that step as potentially heated so an
        # exception cannot leave power applied.
        heater_may_be_on = start_step >= 1

        def safe_heater_shutdown() -> None:
            """Best-effort heater and relay shutdown on every exit path."""
            if not heater_may_be_on:
                return
            try:
                if self.power is not None:
                    results.append(f"Safety cleanup: {self.heater_off.forward()}")
            except Exception as cleanup_exc:
                results.append(f"⚠️ Safety cleanup could not turn heater off: {cleanup_exc}")
            try:
                if self.transfer_heater_relay is not None:
                    results.append(
                        f"Safety cleanup: {self.transfer_heater_relay_off.forward()}"
                    )
            except Exception as cleanup_exc:
                results.append(
                    "⚠️ Safety cleanup could not turn transfer heater relay off: "
                    f"{cleanup_exc}"
                )

        def stage_home_or_skip() -> str:
            if skip_stage_home:
                return "Stage home skipped by explicit operator restart option."
            return self.atom_stage_l_home.forward()

        def begin_step(step_index: int, operation: str, *, safely_restartable: bool = False) -> None:
            self.broadcaster_step.forward(step_index)
            checkpoint_service.begin_step(
                checkpoint_path,
                step_index=step_index,
                step_name=self.PLAN_STEPS[step_index],
                operation=operation,
                safe_to_resume=safely_restartable,
            )

        def finish_step(step_index: int) -> None:
            checkpoint_service.complete_step(
                checkpoint_path,
                step_index=step_index,
                plan_steps=self.PLAN_STEPS,
                runtime_context=runtime_context,
            )

        # Step 0 only allocates/reuses the run directory. It has no hardware
        # side effect and is already complete by the time the checkpoint exists.
        if start_step <= 0:
            finish_step(0)
            start_step = 1

        sample_dirs = {
            label: get_stacking_run_dirs(run_number=run_number, sample_label=f"chip_{label}")
            for label in "abcd"
        }
        electrode_dirs = get_stacking_run_dirs(run_number=run_number, sample_label="electrode")

        def process_flake(label: str, base_step: int, *, adjust_touching: bool) -> None:
            upper = label.upper()
            dirs = sample_dirs[label]

            if start_step <= base_step:
                begin_step(base_step, f"pickup_chip_{label}")
                res_home = stage_home_or_skip()
                res_pick = self.molecule_pickup_chip.forward(chip_id=chip_ids[label])
                results.append(f"Pickup Chip {upper}: {res_home} | {res_pick}")
                finish_step(base_step)

            corner_step = base_step + 1
            corner_key = f"chip_{label}_corner"
            if start_step <= corner_step:
                begin_step(corner_step, f"corner_detection_chip_{label}")
                res_micro = self.atom_stage_to_microscope.forward()
                corner_name = f"chip_{label}_corner_bl.jpg"
                res_corner = self.molecule_corner_detection.forward(
                    save_dir=dirs["corner_dir"],
                    save_name=corner_name,
                )
                results.append(f"Corner Detection Chip {upper}: {res_micro} | {res_corner}")
                new_corner_image = self._parse_saved_corner_path(res_corner) or os.path.join(
                    dirs["corner_dir"], corner_name
                )
                new_corner_coord = self._parse_corner_pos(res_corner)
                if not os.path.exists(new_corner_image):
                    raise RuntimeError(
                        f"Corner detection did not produce the expected image for chip {upper}: "
                        f"{new_corner_image}"
                    )
                if new_corner_coord is None:
                    raise RuntimeError(
                        f"Failed to parse new BL corner coordinates from chip {upper} corner detection output."
                    )
                runtime_context[corner_key] = {
                    "image_path": new_corner_image,
                    "coord": list(new_corner_coord),
                }
                finish_step(corner_step)
            else:
                saved_corner = runtime_context.get(corner_key) or {}
                new_corner_image = saved_corner.get("image_path")
                new_corner_coord = saved_corner.get("coord")

            rough_step = base_step + 2
            if start_step <= rough_step:
                if not new_corner_image or new_corner_coord is None:
                    raise RuntimeError(f"Resume checkpoint is missing detected corner data for chip {upper}.")
                begin_step(rough_step, f"rough_refind_flake_{label}")
                res_rough = self.molecule_flake_refind_rough.forward(
                    corner_BL_image=corner_images[label],
                    new_image=new_corner_image,
                    corner_BL_coord=corner_coords[label],
                    new_corner_coord=new_corner_coord,
                    flake_coord=flake_coords[label],
                )
                results.append(f"Rough Refind Flake {upper}: {res_rough}")
                finish_step(rough_step)

            accurate_step = base_step + 3
            if start_step <= accurate_step:
                begin_step(accurate_step, f"accurate_refind_flake_{label}")
                shift_x, shift_y = shifts[label]
                res_accurate = self.molecule_flake_refind_accurate.forward(
                    image_path=flake_images[label],
                    alignment_dir=dirs["alignment_dir"],
                    save_stem=f"flake_{upper}",
                    shift_x=shift_x,
                    shift_y=shift_y,
                )
                results.append(f"Accurate Refind Flake {upper}: {res_accurate}")
                finish_step(accurate_step)

            initialize_step = base_step + 4
            if start_step <= initialize_step:
                begin_step(initialize_step, f"initialize_stamp_chip_{label}")
                if label == "a":
                    res_connect = self.atom_transfer_connect.forward()
                    res_x = self.atom_transfer_move_axis_to.forward(axis="x", value=6.0)
                    res_y = self.atom_transfer_move_axis_to.forward(axis="y", value=6.5)
                    res_init = self.molecule_initialize_stamp.forward()
                    results.append(
                        f"Initialize Stamp For Chip {upper}: {res_connect} | {res_x} | {res_y} | {res_init}"
                    )
                else:
                    res_init = self.molecule_initialize_stamp.forward()
                    results.append(f"Initialize Stamp For Chip {upper}: {res_init}")
                finish_step(initialize_step)

            pickup_step = base_step + 5
            if adjust_touching:
                if start_step <= pickup_step:
                    begin_step(pickup_step, f"adjust_touching_point_chip_{label}")
                    res_adjust = self.molecule_adjust_touching_point.forward(
                        run_number=run_number,
                        sample_label=f"chip_{label}",
                    )
                    results.append(f"Adjust Touching Point Chip {upper}: {res_adjust}")
                    finish_step(pickup_step)
                pickup_step += 1

            if start_step <= pickup_step:
                current_checkpoint = checkpoint_service.read(checkpoint_path)
                resume_operation = (
                    str(current_checkpoint.get("operation") or "")
                    if int(current_checkpoint.get("parent_step_index", -1)) == pickup_step
                    else ""
                )
                safety_metadata = current_checkpoint.get("safety_metadata") or {}
                if resume_operation != "stack_target_flake_complete":
                    begin_step(
                        pickup_step,
                        "stack_target_flake_not_started",
                        safely_restartable=True,
                    )
                    resume_operation = "stack_target_flake_not_started"
                else:
                    self.broadcaster_step.forward(pickup_step)
                res_pickup = self.molecule_pick_up.forward(
                    run_number=run_number,
                    sample_label=f"chip_{label}",
                    checkpoint_path=checkpoint_path,
                    parent_step_index=pickup_step,
                    resume_operation=resume_operation,
                    saved_safe_z=safety_metadata.get("saved_safe_z"),
                )
                results.append(f"Pick Up Flake {upper}: {res_pickup}")
                finish_step(pickup_step)

            remove_step = pickup_step + 1
            if start_step <= remove_step:
                begin_step(remove_step, f"remove_stamp_chip_{label}")
                res_remove = self.molecule_remove_stamp.forward()
                results.append(f"Remove Stamp For Chip {upper}: {res_remove}")
                finish_step(remove_step)

            store_step = pickup_step + 2
            if start_step <= store_step:
                begin_step(store_step, f"store_chip_{label}")
                res_home_store = stage_home_or_skip()
                res_store = self.molecule_store_chip.forward(chip_id=chip_ids[label])
                results.append(f"Store Chip {upper}: {res_home_store} | {res_store}")
                finish_step(store_step)

        try:
            if start_step <= 1:
                begin_step(1, "set_initial_transfer_temperature")
                results.append(f"Set Initial Temp: {self.molecule_set_initial_temp.forward()}")
                finish_step(1)

            if start_step <= 2:
                begin_step(2, "load_stamp_connect_not_started", safely_restartable=True)
                results.append(
                    "Load Stamp: "
                    f"{self.molecule_load_stamp.forward(checkpoint_path=checkpoint_path, parent_step_index=2)}"
                )
                finish_step(2)

            if start_step <= 3:
                begin_step(3, "remove_stamp_to_safe_position")
                res_x = self.atom_transfer_move_axis_to.forward(axis="x", value=6.0)
                res_y = self.atom_transfer_move_axis_to.forward(axis="y", value=6.5)
                res_camera = self.cam_start.forward(open_live_view=True)
                res_preview = self.atom_transfer_move_axis_to.forward(axis="L", value=120.0)
                time.sleep(90.0)
                res_camera_stop = self.cam_stop.forward()
                res_safe = self.atom_transfer_move_axis_to.forward(axis="L", value=160.0)
                results.append(
                    "Remove Stamp to Safe Position: "
                    f"{res_x} | {res_y} | {res_camera} | {res_preview} | waited 90s | "
                    f"{res_camera_stop} | {res_safe}"
                )
                finish_step(3)

            process_flake("a", 4, adjust_touching=True)
            process_flake("b", 13, adjust_touching=False)
            process_flake("c", 21, adjust_touching=False)
            process_flake("d", 29, adjust_touching=False)

            if start_step <= 37:
                begin_step(37, "pickup_electrode_chip")
                res_home_electrode = stage_home_or_skip()
                res_pick_electrode = self.molecule_pickup_chip.forward(chip_id=electrode_chip_id)
                results.append(f"Pickup Electrode Chip: {res_home_electrode} | {res_pick_electrode}")
                finish_step(37)

            if start_step <= 38:
                begin_step(38, "corner_detection_electrode")
                res_micro_electrode = self.atom_stage_to_microscope.forward()
                electrode_corner_name = "electrode_chip_corner_bl.jpg"
                res_corner_electrode = self.molecule_corner_detection.forward(
                    save_dir=electrode_dirs["corner_dir"],
                    save_name=electrode_corner_name,
                )
                results.append(
                    f"Corner Detection Electrode: {res_micro_electrode} | {res_corner_electrode}"
                )
                new_electrode_corner_image = self._parse_saved_corner_path(res_corner_electrode) or os.path.join(
                    electrode_dirs["corner_dir"], electrode_corner_name
                )
                new_electrode_corner_coord = self._parse_corner_pos(res_corner_electrode)
                if not os.path.exists(new_electrode_corner_image):
                    raise RuntimeError(
                        "Corner detection did not produce the expected electrode image: "
                        f"{new_electrode_corner_image}"
                    )
                if new_electrode_corner_coord is None:
                    raise RuntimeError("Failed to parse new BL coordinates from electrode corner detection.")
                runtime_context["electrode_corner"] = {
                    "image_path": new_electrode_corner_image,
                    "coord": list(new_electrode_corner_coord),
                }
                finish_step(38)
            else:
                saved_electrode_corner = runtime_context.get("electrode_corner") or {}
                new_electrode_corner_image = saved_electrode_corner.get("image_path")
                new_electrode_corner_coord = saved_electrode_corner.get("coord")

            if start_step <= 39:
                if not new_electrode_corner_image or new_electrode_corner_coord is None:
                    raise RuntimeError("Resume checkpoint is missing detected electrode corner data.")
                begin_step(39, "rough_refind_bottom_electrode")
                res_rough_electrode = self.molecule_flake_refind_rough.forward(
                    corner_BL_image=electrode_chip_corner_image_path,
                    new_image=new_electrode_corner_image,
                    corner_BL_coord=electrode_corner_xy,
                    new_corner_coord=new_electrode_corner_coord,
                    flake_coord=bottom_electrode_xy,
                )
                results.append(f"Rough Refind Bottom Electrode: {res_rough_electrode}")
                finish_step(39)

            if start_step <= 40:
                begin_step(40, "initialize_stamp_electrode")
                results.append(
                    f"Initialize Stamp For Electrode: {self.molecule_initialize_stamp.forward()}"
                )
                finish_step(40)

            if start_step <= 41:
                begin_step(41, "drop_down_bottom_electrode")
                results.append(
                    "Drop Down Bottom Electrode: "
                    f"{self.molecule_drop_down.forward(run_number=run_number, sample_label='electrode')}"
                )
                finish_step(41)

            if start_step <= 42:
                begin_step(42, "remove_stamp_electrode")
                results.append(f"Remove Stamp For Electrode: {self.molecule_remove_stamp.forward()}")
                finish_step(42)

            if start_step <= 43:
                begin_step(43, "record_device_images")
                results.append(f"Record Device Images: {self.molecule_record_device.forward()}")
                device_dirs = get_stacking_run_dirs(run_number=run_number, sample_label="device")
                device_paths = {
                    mag: os.path.join(device_dirs["sample_dir"], f"device_{mag}X.jpg")
                    for mag in (5, 10, 20, 100)
                }
                device_record = self.device_service.record_device(
                    name="quantum Hall device",
                    chip_id=int(electrode_chip_id),
                    flake_id=None,
                    status="fabricated",
                    notes=(
                        "Recorded after assembly_quantum_hall_device completed "
                        f"in stacking run {run_number}."
                    ),
                    image_5x_path=device_paths[5] if os.path.exists(device_paths[5]) else None,
                    image_10x_path=device_paths[10] if os.path.exists(device_paths[10]) else None,
                    image_20x_path=device_paths[20] if os.path.exists(device_paths[20]) else None,
                    image_100x_path=device_paths[100] if os.path.exists(device_paths[100]) else None,
                )
                results.append(f"Device DB record created: id={device_record.id}")
                finish_step(43)

            if start_step <= 44:
                begin_step(44, "store_electrode_chip")
                res_home_store = stage_home_or_skip()
                res_store_electrode = self.molecule_store_chip.forward(chip_id=electrode_chip_id)
                results.append(f"Store Electrode Chip: {res_home_store} | {res_store_electrode}")
                finish_step(44)

            if start_step <= 45:
                begin_step(45, "recycle_stamp")
                results.append(f"Recycle Stamp: {self.molecule_recycle_stamp.forward()}")
                finish_step(45)
        except Exception as exc:
            results.append(f"❌ Quantum Hall device assembly failed: {exc}")
            assembly_failed = True
        finally:
            safe_heater_shutdown()

        if assembly_failed:
            return "\n".join(results)
        results.append(f"Quantum Hall device assembly complete. run_number={run_number}")
        return "\n".join(results)
