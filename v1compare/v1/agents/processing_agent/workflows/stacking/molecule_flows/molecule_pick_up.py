from __future__ import annotations

import os
import time

from common.services.processing_checkpoint_service import ProcessingCheckpointService

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomCameraAutofocusOnce,
    AtomCameraStart,
    AtomCameraStop,
    AtomTransferConnect,
    AtomTransferGetPositions,
    AtomTransferMoveAxisBy,
    AtomTransferMoveAxisTo,
    AtomTransferSetKSTSpeed,
    AtomTurretRotate,
)
from agents.processing_agent.workflows.stacking.molecule_flows.utils import (
    get_stacking_run_dirs,
    stake_target_flake,
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool


class MoleculePickUp(BaseWorkflow):
    name = "molecule_pick_up"
    description = "Perform the stacking pickup phase after the touching point has already been adjusted."
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
        "checkpoint_path": {
            "type": "string",
            "description": "Parent processing checkpoint path.",
            "nullable": True,
            "default": None,
        },
        "parent_step_index": {
            "type": "integer",
            "description": "Parent workflow step index.",
            "nullable": True,
            "default": None,
        },
        "resume_operation": {
            "type": "string",
            "description": "Saved pickup sub-operation state.",
            "nullable": True,
            "default": None,
        },
        "saved_safe_z": {
            "type": "number",
            "description": "Saved safe Z used only during checkpoint recovery.",
            "nullable": True,
            "default": None,
        },
    }
    output_type = "string"

    DELTA_Z = 0.2
    SAFE_Z_OFFSET = 0.2
    SAFE_DIST = 240
    FOCUS_WAIT_S = 5.0

    PLAN_STEPS = [
        "Initialize Hardware",
        "Stack Target Flake",
        "Recover Z And Finalize",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transfer_connect = AtomTransferConnect(*args, **kwargs)
        self.transfer_set_kst_speed = AtomTransferSetKSTSpeed(*args, **kwargs)
        self.transfer_move_axis_by = AtomTransferMoveAxisBy(*args, **kwargs)
        self.transfer_move_axis_to = AtomTransferMoveAxisTo(*args, **kwargs)
        self.transfer_get_positions = AtomTransferGetPositions(*args, **kwargs)
        self.cam_start = AtomCameraStart(*args, **kwargs)
        self.cam_stop = AtomCameraStop(*args, **kwargs)
        self.cam_autofocus_once = AtomCameraAutofocusOnce(*args, **kwargs)
        self.turret_rotate = AtomTurretRotate(*args, **kwargs)
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    @staticmethod
    def _parse_positions(message: str) -> dict[str, float]:
        positions: dict[str, float] = {}
        if not message:
            return positions
        if ":" in message:
            message = message.split(":", 1)[1]
        for part in message.strip().split(","):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            try:
                positions[key.strip()] = float(value.strip().rstrip("."))
            except Exception:
                continue
        return positions

    def forward(
        self,
        run_number: int = 1,
        sample_label: str | None = None,
        checkpoint_path: str | None = None,
        parent_step_index: int | None = None,
        resume_operation: str | None = None,
        saved_safe_z: float | None = None,
    ) -> str:
        if self.transfer is None or self.camera is None or self.stage is None or self.turret is None:
            return "❌ Required hardware (transfer/camera/stage/turret) unavailable."

        run_number = int(run_number) if run_number is not None else 1

        run_dirs = get_stacking_run_dirs(run_number=run_number, sample_label=sample_label)
        run_dir = run_dirs["run_dir"]
        sample_dir = run_dirs["sample_dir"]
        output_dir = run_dirs["output_dir"]
        stacking_log = run_dirs["stacking_log_dir"]

        self.broadcaster_plan.forward(self.PLAN_STEPS)
        results: list[str] = []
        target_pt = (1900, 1100)
        sample_name = sample_label or "default"
        checkpoint_service = ProcessingCheckpointService() if checkpoint_path else None

        def update_checkpoint(**updates) -> None:
            if checkpoint_service is not None and checkpoint_path:
                checkpoint_service.update(checkpoint_path, **updates)

        self.broadcaster_step.forward(0)
        print(f"[PICKUP] Start picking up for sample '{sample_name}' in run {run_number}", flush=True)
        print(f"[PICKUP] Sample directory: {sample_dir}", flush=True)
        results.append(self.transfer_connect())
        results.append(self.transfer_set_kst_speed(axis="x", max_vel=0.2, accel=0.5))
        results.append(self.transfer_set_kst_speed(axis="z", max_vel=0.002, accel=0.1))
        results.append(self.cam_start(open_live_view=True))
        results.append(self.turret_rotate(position=1))
        safe_pos_msg = self.transfer_get_positions()
        safe_pos = self._parse_positions(safe_pos_msg)
        safe_z = (
            float(saved_safe_z)
            if saved_safe_z is not None
            else safe_pos.get("z", 0.0) + self.SAFE_Z_OFFSET
        )
        results.append(safe_pos_msg)
        print(f"[PICKUP] Safe transfer position at begining: {safe_pos_msg}", flush=True)

        stack_already_complete = resume_operation == "stack_target_flake_complete"
        update_checkpoint(
            parent_step_index=parent_step_index,
            operation=(
                "stack_target_flake_complete"
                if stack_already_complete
                else "stack_target_flake_not_started"
            ),
            operation_state="complete" if stack_already_complete else "not_started",
            safe_to_resume=True,
            safety_reason=(
                "Flake coverage was already completed; safe-Z recovery remains pending."
                if stack_already_complete
                else "Hardware reconnected; no stacking Z motion has started."
            ),
            safety_metadata={"saved_safe_z": safe_z, "sample_label": sample_name},
        )

        self.broadcaster_step.forward(1)
        if resume_operation != "stack_target_flake_complete":
            print("[PICKUP] Step 3: stack target flake", flush=True)
            time.sleep(float(self.FOCUS_WAIT_S))

            def mark_motion_started() -> None:
                update_checkpoint(
                    operation="stack_target_flake_in_progress",
                    operation_state="in_progress",
                    safe_to_resume=False,
                    safety_reason=(
                        "Stacking Z motion started. Automatic replay is blocked until "
                        "the hardware state is verified."
                    ),
                    safety_metadata={"saved_safe_z": safe_z, "sample_label": sample_name},
                )

            def mark_stack_complete(step_idx: int) -> None:
                update_checkpoint(
                    operation="stack_target_flake_complete",
                    operation_state="complete",
                    safe_to_resume=True,
                    safety_reason="Flake coverage completed; safe-Z recovery is restartable.",
                    safety_metadata={
                        "saved_safe_z": safe_z,
                        "sample_label": sample_name,
                        "stacking_step": int(step_idx),
                    },
                )

            stake_target_flake(
                self.camera,
                self.transfer,
                target_pt,
                self.SAFE_DIST,
                self.DELTA_Z,
                stacking_log,
                os.path.join(output_dir, "stacking_log"),
                on_motion_start=mark_motion_started,
                on_complete=mark_stack_complete,
            )
        else:
            results.append("Stack target flake already complete; skipped replay.")

        self.broadcaster_step.forward(2)
        results.append(self.transfer_set_kst_speed(axis="z", max_vel=0.01, accel=0.1))
        print(f"[PICKUP] Recovering to saved safe Z with slow speed: z={safe_z}", flush=True)
        results.append(self.transfer_move_axis_to(axis="z", value=safe_z))
        results.append(self.transfer_set_kst_speed(axis="z", max_vel=0.1, accel=0.1))
        results.append(self.transfer_move_axis_to(axis="z", value=5.5))

        update_checkpoint(
            operation="pickup_complete",
            operation_state="complete",
            safe_to_resume=True,
            safety_reason="Pickup and safe-Z recovery completed.",
            safety_metadata={"saved_safe_z": safe_z, "sample_label": sample_name},
        )

        results.append(self.cam_stop())
        print("[PICKUP] Picking up procedure completed.", flush=True)
        results.append(f"Stacking sample directory: {sample_dir}")
        results.append(f"Stacking run directory: {run_dir}")
        results.append("Picking up procedure completed.")
        return "\n".join(results)
