from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class ProcessingCheckpointService:
    """Durable, session-bound checkpoints for physical processing workflows."""

    FILE_NAME = "processing_resume_checkpoint.json"
    TERMINAL_STATUSES = frozenset({"complete", "cancelled", "abandoned"})
    DIRECT_RESUME_ONCE_KEY = "launch_processing_agent_directly_once"
    DIRECT_RESUME_CONSUMED_KEY = "launch_processing_agent_directly_once_consumed"

    def __init__(self, stacking_root: str | Path | None = None) -> None:
        self.stacking_root = (
            Path(stacking_root)
            if stacking_root is not None
            else _repo_root() / "images" / "stacking"
        )

    def path_for_run(self, run_number: int) -> Path:
        return self.stacking_root / f"run_{int(run_number)}" / self.FILE_NAME

    @staticmethod
    def payload_fingerprint(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def read(self, path: str | Path) -> dict[str, Any]:
        checkpoint_path = Path(path).resolve()
        root = self.stacking_root.resolve()
        if root != checkpoint_path and root not in checkpoint_path.parents:
            raise ValueError("Processing checkpoint must be inside images/stacking.")
        with checkpoint_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("Processing checkpoint must contain a JSON object.")
        return value

    def create(
        self,
        *,
        run_number: int,
        workflow_name: str,
        payload: dict[str, Any],
        plan_steps: list[str],
        session_key: str = "",
        project_id: str = "",
        original_request: str = "",
        next_step_index: int = 0,
        operation: str = "workflow_not_started",
        safe_to_resume: bool = True,
        runtime_context: dict[str, Any] | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        path = self.path_for_run(run_number).resolve()
        checkpoint = {
            "schema_version": 1,
            "checkpoint_type": "processing",
            "workflow_name": workflow_name,
            "agent_type": "processing",
            "session_key": session_key,
            "project_id": project_id,
            "original_request": original_request,
            "run_number": int(run_number),
            "payload": payload,
            "payload_sha256": self.payload_fingerprint(payload),
            "workflow_steps": list(plan_steps),
            "workflow_active_index": int(next_step_index),
            "next_step_index": int(next_step_index),
            "parent_step_index": int(next_step_index),
            "parent_step_name": plan_steps[next_step_index] if next_step_index < len(plan_steps) else "Complete",
            "operation_state": "not_started",
            "operation": operation,
            "completed_parent_steps": list(range(max(0, int(next_step_index)))),
            "runtime_context": dict(runtime_context or {}),
            "safe_to_resume": bool(safe_to_resume),
            "safety_reason": "Saved at a restartable workflow boundary." if safe_to_resume else "Physical action state requires verification.",
            "status": "resume_pending",
            "resume_prompt": (
                "Resume the interrupted processing workflow. Call "
                f"resume_processing_checkpoint(checkpoint_path={path.as_posix()!r}) exactly once. "
                "Do not call any other workflow first."
            ),
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }
        self._atomic_write(path, checkpoint)
        return path, checkpoint

    def update(self, path: str | Path, **updates: Any) -> dict[str, Any]:
        checkpoint_path = Path(path).resolve()
        checkpoint = self.read(checkpoint_path)
        checkpoint.update(updates)
        checkpoint["updated_at"] = _utc_now()
        self._atomic_write(checkpoint_path, checkpoint)
        return checkpoint

    def validate_for_resume(
        self,
        path: str | Path,
        *,
        workflow_name: str = "",
        current_session_key: str = "",
        current_project_id: str = "",
        require_identity: bool = True,
    ) -> dict[str, Any]:
        """Validate an exact checkpoint before any physical workflow is called."""
        checkpoint_path = Path(path).resolve()
        checkpoint = self.read(checkpoint_path)
        if checkpoint_path.name != self.FILE_NAME:
            raise ValueError("The supplied path is not a processing checkpoint file.")
        if checkpoint.get("checkpoint_type") != "processing":
            raise ValueError("The supplied file is not a processing checkpoint.")
        if checkpoint.get("status") in self.TERMINAL_STATUSES:
            raise ValueError("The processing checkpoint is terminal and cannot be resumed.")
        if checkpoint.get("status") != "resume_pending":
            raise ValueError("The processing checkpoint is not pending resume.")
        if not checkpoint.get("safe_to_resume"):
            raise ValueError(
                "The interrupted physical operation is not at a verified restart boundary."
            )

        try:
            run_number = int(checkpoint["run_number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("The processing checkpoint has no valid run number.") from exc
        if checkpoint_path != self.path_for_run(run_number).resolve():
            raise ValueError("The checkpoint path does not match its recorded run number.")

        stored_workflow = str(checkpoint.get("workflow_name") or "")
        if workflow_name and stored_workflow != workflow_name:
            raise ValueError("The processing checkpoint belongs to a different workflow.")
        payload = checkpoint.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Processing checkpoint is missing its original workflow payload.")
        expected_fingerprint = str(checkpoint.get("payload_sha256") or "")
        if not expected_fingerprint:
            raise ValueError(
                "Legacy processing checkpoint has no payload fingerprint; start a new run."
            )
        if expected_fingerprint != self.payload_fingerprint(payload):
            raise ValueError("Processing checkpoint payload fingerprint mismatch.")

        steps = checkpoint.get("workflow_steps")
        try:
            next_step = int(checkpoint.get("next_step_index", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Processing checkpoint has an invalid next step.") from exc
        if not isinstance(steps, list) or not steps or not 0 <= next_step < len(steps):
            raise ValueError("Processing checkpoint next step is outside its workflow plan.")

        if require_identity:
            checkpoint_session = str(checkpoint.get("session_key") or "").strip()
            current_session = str(current_session_key or "").strip()
            if not checkpoint_session or not current_session or checkpoint_session != current_session:
                raise ValueError("The processing checkpoint does not belong to this UI session.")
            checkpoint_project = str(checkpoint.get("project_id") or "").strip()
            current_project = str(current_project_id or "").strip()
            if (checkpoint_project or current_project) and checkpoint_project != current_project:
                raise ValueError("The processing checkpoint belongs to a different project.")
        return checkpoint

    @classmethod
    def direct_resume_once_is_armed(cls, checkpoint: dict[str, Any]) -> bool:
        options = dict(checkpoint.get("resume_options") or {})
        return bool(options.get(cls.DIRECT_RESUME_ONCE_KEY)) and not bool(
            options.get(cls.DIRECT_RESUME_CONSUMED_KEY)
        )

    @classmethod
    def direct_resume_once_is_consumed(cls, checkpoint: dict[str, Any]) -> bool:
        options = dict(checkpoint.get("resume_options") or {})
        return bool(options.get(cls.DIRECT_RESUME_CONSUMED_KEY))

    def arm_direct_resume_once(self, path: str | Path) -> dict[str, Any]:
        """Allow exactly one implicit/direct Processing-agent continuation."""
        checkpoint = self.validate_for_resume(path, require_identity=False)
        options = dict(checkpoint.get("resume_options") or {})
        options[self.DIRECT_RESUME_ONCE_KEY] = True
        options[self.DIRECT_RESUME_CONSUMED_KEY] = False
        options.pop("launch_processing_agent_directly_once_consumed_at", None)
        return self.update(path, resume_options=options)

    def consume_direct_resume_once(
        self,
        path: str | Path,
    ) -> tuple[dict[str, Any], bool]:
        """Consume a one-shot direct continuation before its process starts."""
        checkpoint = self.read(path)
        if not self.direct_resume_once_is_armed(checkpoint):
            return checkpoint, False
        options = dict(checkpoint.get("resume_options") or {})
        options[self.DIRECT_RESUME_ONCE_KEY] = False
        options[self.DIRECT_RESUME_CONSUMED_KEY] = True
        options["launch_processing_agent_directly_once_consumed_at"] = _utc_now()
        return self.update(path, resume_options=options), True

    def begin_step(
        self,
        path: str | Path,
        *,
        step_index: int,
        step_name: str,
        operation: str,
        safe_to_resume: bool = False,
    ) -> dict[str, Any]:
        return self.update(
            path,
            workflow_active_index=int(step_index),
            next_step_index=int(step_index),
            parent_step_index=int(step_index),
            parent_step_name=step_name,
            operation=operation,
            operation_state="in_progress",
            safe_to_resume=bool(safe_to_resume),
            safety_reason=(
                "Saved before any irreversible motion at a restartable boundary."
                if safe_to_resume
                else "A physical workflow step is in progress; verify hardware state before resuming."
            ),
            status="resume_pending",
        )

    def complete_step(
        self,
        path: str | Path,
        *,
        step_index: int,
        plan_steps: list[str],
        runtime_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checkpoint = self.read(path)
        completed = {int(value) for value in checkpoint.get("completed_parent_steps", [])}
        completed.add(int(step_index))
        next_step = int(step_index) + 1
        updates: dict[str, Any] = {
            "completed_parent_steps": sorted(completed),
            "workflow_active_index": min(next_step, max(0, len(plan_steps) - 1)),
            "next_step_index": next_step,
            "parent_step_index": next_step,
            "parent_step_name": plan_steps[next_step] if next_step < len(plan_steps) else "Complete",
            "operation": "workflow_complete" if next_step >= len(plan_steps) else "next_step_not_started",
            "operation_state": "complete" if next_step >= len(plan_steps) else "not_started",
            "safe_to_resume": next_step < len(plan_steps),
            "safety_reason": (
                "Workflow completed."
                if next_step >= len(plan_steps)
                else "Previous physical step completed; next step has not started."
            ),
            "status": "complete" if next_step >= len(plan_steps) else "resume_pending",
        }
        if runtime_context is not None:
            updates["runtime_context"] = dict(runtime_context)
        return self.update(path, **updates)

    def find_for_session(self, session_key: str) -> tuple[Path | None, dict[str, Any] | None]:
        session_key = str(session_key or "").strip()
        if not session_key:
            return None, None
        matches: list[tuple[Path, dict[str, Any]]] = []
        for candidate in self.stacking_root.glob(f"run_*/{self.FILE_NAME}"):
            try:
                value = self.read(candidate)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if (
                value.get("session_key") == session_key
                and value.get("status") not in self.TERMINAL_STATUSES
            ):
                matches.append((candidate.resolve(), value))
        if not matches:
            return None, None
        return max(matches, key=lambda item: str(item[1].get("updated_at") or ""))
