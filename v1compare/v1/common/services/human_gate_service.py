from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any, Callable
import uuid


_GATE_ID_RE = re.compile(r"^flake_[0-9a-f]{24}$")
_BARE_GATE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_WRITE_LOCK = Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class HumanGateService:
    """Persist intentional UI approval gates independently of agent processes."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.root_dir = Path(root_dir) if root_dir else _repo_root() / "images" / "human_gates"
        self.root_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def canonical_gate_id(gate_id: str) -> str:
        """Return the canonical persisted id for an unambiguous flake gate.

        Managed-agent prose can drop the ``flake_`` namespace prefix.  A bare
        24-hex token is still unambiguous and is safe to repair because the
        persisted gate, project id, status, and selected-flake signature are
        validated separately before approval is trusted.
        """
        normalized = str(gate_id or "").strip().lower()
        if _BARE_GATE_ID_RE.fullmatch(normalized):
            normalized = f"flake_{normalized}"
        if not _GATE_ID_RE.fullmatch(normalized):
            raise ValueError("Invalid human gate id.")
        return normalized

    def _path(self, gate_id: str) -> Path:
        canonical = self.canonical_gate_id(gate_id)
        return self.root_dir / f"{canonical}.json"

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def create_flake_selection_gate(
        self,
        *,
        selection_result_path: str,
        selected_flakes_by_role: dict[str, Any],
        session_key: str = "",
        project_id: str = "",
        output_dir: str = "",
    ) -> dict[str, Any]:
        gate_id = f"flake_{uuid.uuid4().hex[:24]}"
        now = _utc_now()
        selection_path = Path(selection_result_path).resolve()
        try:
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            selection = {}
        prior_rejections = (
            selection.get("human_rejection_history")
            if isinstance(selection, dict) and isinstance(selection.get("human_rejection_history"), list)
            else []
        )
        prior_exclusions = (
            selection.get("excluded_flake_identities")
            if isinstance(selection, dict) and isinstance(selection.get("excluded_flake_identities"), list)
            else []
        )
        selected_summary: list[dict[str, Any]] = []
        for role, raw in selected_flakes_by_role.items():
            records = raw if isinstance(raw, list) else [raw]
            for role_index, item in enumerate(records, start=1):
                record = item if isinstance(item, dict) else {}
                assessment = record.get("quality_assessment")
                selected_summary.append(
                    {
                        "role": str(role),
                        "role_index": role_index,
                        "flake_id": record.get("flake_id"),
                        "chip_id": record.get("chip_id"),
                        "material": record.get("type_label") or record.get("material"),
                        "mask_path": record.get("mask_20x_path") or record.get("mask_path"),
                        "image_path": record.get("image_100x_path"),
                        "image_100x_path": record.get("image_100x_path"),
                        "image_20x_path": record.get("image_20x_path"),
                        "size_area_um": record.get("size_area_um"),
                        "quality_assessment": assessment if isinstance(assessment, dict) else {},
                        "selection_reason": record.get("selection_reason") or "",
                    }
                )
        gate = {
            "gate_id": gate_id,
            "gate_type": "flake_selection_approval",
            "status": "awaiting_human_approval",
            "session_key": session_key,
            "project_id": project_id,
            "selection_result_path": str(selection_path),
            "output_dir": str(Path(output_dir).resolve()) if output_dir else "",
            "selected_flakes": selected_summary,
            "created_at": now,
            "updated_at": now,
            "approved_at": None,
            "rejected_at": None,
            "rejection_scope": None,
            "rejection_reason": "",
            "rejected_flakes": [],
            "human_rejection_history": prior_rejections,
            "excluded_flake_identities": prior_exclusions,
        }
        with _WRITE_LOCK:
            self._write_json(self._path(gate_id), gate)
        return gate

    def get(self, gate_id: str) -> dict[str, Any]:
        path = self._path(gate_id)
        if not path.exists():
            raise FileNotFoundError(f"Human gate not found: {gate_id}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Human gate file is invalid.")
        return value

    @staticmethod
    def _selected_flake_signature_from_mapping(
        selected_flakes_by_role: dict[str, Any],
    ) -> Counter[tuple[str, str, str, str]]:
        signatures: Counter[tuple[str, str, str, str]] = Counter()
        for role, raw_records in selected_flakes_by_role.items():
            records = raw_records if isinstance(raw_records, list) else [raw_records]
            for raw_record in records:
                if not isinstance(raw_record, dict):
                    continue
                signatures[
                    (
                        str(role),
                        str(raw_record.get("chip_id")),
                        str(raw_record.get("flake_id")),
                        str(raw_record.get("mask_20x_path") or raw_record.get("mask_path") or ""),
                    )
                ] += 1
        return signatures

    @staticmethod
    def _selected_flake_signature_from_gate(
        gate: dict[str, Any],
    ) -> Counter[tuple[str, str, str, str]]:
        signatures: Counter[tuple[str, str, str, str]] = Counter()
        for raw_record in gate.get("selected_flakes") or []:
            if not isinstance(raw_record, dict):
                continue
            signatures[
                (
                    str(raw_record.get("role") or ""),
                    str(raw_record.get("chip_id")),
                    str(raw_record.get("flake_id")),
                    str(raw_record.get("mask_path") or ""),
                )
            ] += 1
        return signatures

    def _load_and_validate_gate_selection(
        self,
        gate: dict[str, Any],
    ) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        selection_path = Path(str(gate.get("selection_result_path") or ""))
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        chosen = selection.get("selected_flakes_by_role") if isinstance(selection, dict) else None
        if not isinstance(chosen, dict) or not chosen:
            raise ValueError("Approval gate has no valid selected_flakes_by_role payload.")
        gate_signature = self._selected_flake_signature_from_gate(gate)
        selection_signature = self._selected_flake_signature_from_mapping(chosen)
        if not gate_signature or gate_signature != selection_signature:
            raise ValueError(
                "The persisted flake-selection result no longer matches the flakes shown at the human gate."
            )
        return selection_path, selection, chosen

    @staticmethod
    def _approved_candidate_context(
        *,
        gate: dict[str, Any],
        selection_path: Path,
        selection: dict[str, Any],
        chosen: dict[str, Any],
    ) -> dict[str, Any]:
        mask_preparation_required = bool(selection.get("design_mask_preparation_required"))
        run_name = next(
            (part for part in selection_path.resolve().parts if re.fullmatch(r"run_\d+", part, re.I)),
            "",
        )
        design_handoff_dir = (
            _repo_root() / "images" / "stacking" / run_name / "design" / "claude_klayout_handoff"
            if run_name
            else None
        )
        prior_ideal_design_paths: dict[str, str] = {}
        if design_handoff_dir is not None:
            for key, filename in (
                ("ideal_gds", "ideal_device.gds"),
                ("ideal_screenshot", "ideal_device_screenshot.png"),
                ("ideal_result", "ideal_device_result.json"),
                ("design_contract", "claude_design_contract.json"),
            ):
                candidate = design_handoff_dir / filename
                if candidate.is_file():
                    prior_ideal_design_paths[key] = str(candidate.resolve())

        return {
            "project_id": str(gate.get("project_id") or ""),
            "design_stage": (
                "selected_flake_mask_preparation"
                if mask_preparation_required
                else "real_flake_adaptation"
            ),
            "flake_selection_gate_id": str(gate.get("gate_id") or ""),
            "flake_selection_human_approved": True,
            "flake_selection_result_path": str(selection_path.resolve()),
            "flake_selection_output_dir": str(gate.get("output_dir") or ""),
            "design_run": run_name,
            "design_handoff_dir": str(design_handoff_dir.resolve()) if design_handoff_dir else "",
            "prior_ideal_design_paths": prior_ideal_design_paths,
            "selected_flakes_by_role": chosen,
            "selected_flake_set": [
                {"role": role, **record}
                for role, records in chosen.items()
                for record in (records if isinstance(records, list) else [records])
                if isinstance(record, dict)
            ],
            "human_rejection_history": selection.get("human_rejection_history") or [],
            "excluded_flake_identities": selection.get("excluded_flake_identities") or [],
            "candidate_flakes_by_role": {
                role: [
                    record
                    for record in (records if isinstance(records, list) else [records])
                    if isinstance(record, dict)
                ]
                for role, records in chosen.items()
            },
            "candidate_mask_paths_by_role": {
                role: [
                    str(record.get("mask_20x_path") or record.get("mask_path"))
                    for record in (records if isinstance(records, list) else [records])
                    if isinstance(record, dict)
                    and (record.get("mask_20x_path") or record.get("mask_path"))
                ]
                for role, records in chosen.items()
            },
            "candidate_image_paths_by_role": {
                role: [
                    str(record.get("image_100x_path"))
                    for record in (records if isinstance(records, list) else [records])
                    if isinstance(record, dict) and record.get("image_100x_path")
                ]
                for role, records in chosen.items()
            },
            "candidate_image_20x_paths_by_role": {
                role: [
                    str(record.get("image_20x_path"))
                    for record in (records if isinstance(records, list) else [records])
                    if isinstance(record, dict) and record.get("image_20x_path")
                ]
                for role, records in chosen.items()
            },
            "design_mask_preparation_required": mask_preparation_required,
            "selected_flakes_missing_design_masks": (
                selection.get("selected_flakes_missing_design_masks") or []
            ),
        }

    def get_approved_candidate_context(
        self,
        gate_id: str,
        *,
        session_key: str = "",
        project_id: str = "",
        selected_flakes_by_role: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Rehydrate and verify the canonical design context for an approved gate."""
        gate = self.get(gate_id)
        bound_session = str(gate.get("session_key") or "")
        if bound_session and session_key and bound_session != session_key:
            raise PermissionError("This approval gate belongs to another chat session.")
        bound_project = str(gate.get("project_id") or "")
        if bound_project and project_id and bound_project != project_id:
            raise PermissionError("This approval gate belongs to another project.")
        if gate.get("status") != "approved":
            raise ValueError(f"Flake selection gate is not approved: {gate_id}")
        selection_path, selection, chosen = self._load_and_validate_gate_selection(gate)
        if selected_flakes_by_role is not None:
            supplied_signature = self._selected_flake_signature_from_mapping(selected_flakes_by_role)
            approved_signature = self._selected_flake_signature_from_mapping(chosen)
            if supplied_signature != approved_signature:
                raise ValueError(
                    "The supplied selected_flakes_by_role does not match the human-approved gate."
                )
        return self._approved_candidate_context(
            gate=gate,
            selection_path=selection_path,
            selection=selection,
            chosen=chosen,
        )

    def latest_pending_for_session(self, session_key: str) -> dict[str, Any] | None:
        if not session_key:
            return None
        matches: list[dict[str, Any]] = []
        for path in self.root_dir.glob("flake_*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if (
                isinstance(value, dict)
                and value.get("session_key") == session_key
                and value.get("status") == "awaiting_human_approval"
            ):
                matches.append(value)
        if not matches:
            return None
        return max(matches, key=lambda item: str(item.get("created_at") or ""))

    def wait_for_approval(
        self,
        gate_id: str,
        *,
        timeout_seconds: int = 86400,
        poll_interval_seconds: float = 0.25,
        heartbeat_seconds: float = 15.0,
        on_wait: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Block until the UI approves or rejects the persisted gate."""
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        next_heartbeat = time.monotonic() + max(1.0, float(heartbeat_seconds))
        while time.monotonic() < deadline:
            gate = self.get(gate_id)
            status = str(gate.get("status") or "")
            if status == "approved":
                # Reuse the idempotent approval path to reconstruct the exact
                # reduced context for the still-running selector tool call.
                return self.approve(gate_id)
            if status == "rejected":
                return gate
            if status != "awaiting_human_approval":
                raise ValueError(f"Gate left the approvable state with status {status!r}.")
            now = time.monotonic()
            if on_wait is not None and now >= next_heartbeat:
                on_wait()
                next_heartbeat = now + max(1.0, float(heartbeat_seconds))
            time.sleep(max(0.05, float(poll_interval_seconds)))
        raise TimeoutError(f"Human gate approval timed out: {gate_id}")

    @staticmethod
    def _same_selected_flake(candidate: dict[str, Any], requested: dict[str, Any]) -> bool:
        candidate_role = str(candidate.get("role") or "")
        requested_role = str(requested.get("role") or "")
        if requested_role and candidate_role != requested_role:
            return False
        requested_flake_id = requested.get("flake_id")
        if requested_flake_id is not None:
            return (
                candidate.get("flake_id") == requested_flake_id
                and (
                    requested.get("chip_id") is None
                    or candidate.get("chip_id") == requested.get("chip_id")
                )
            )
        requested_mask = str(requested.get("mask_path") or "").strip()
        return bool(requested_mask and str(candidate.get("mask_path") or "") == requested_mask)

    @staticmethod
    def _with_rejection_prompt(gate: dict[str, Any]) -> dict[str, Any]:
        matched = [item for item in gate.get("rejected_flakes") or [] if isinstance(item, dict)]
        rejection_context = {
            "human_rejection_history": gate.get("human_rejection_history") or [],
            "human_rejected_flakes": matched,
            "excluded_flake_identities": gate.get("excluded_flake_identities") or [],
        }
        return {
            **gate,
            "continuation_prompt": (
                "[FLAKE_SELECTION_REJECTED]\n"
                f"Human rejected gate {gate.get('gate_id')}. Restart fallback only: rerun flake "
                "selection, exclude the rejected physical flakes, and present the replacement "
                "complete set at a new human gate.\nFLAKE_REJECTION_CONTEXT_JSON:\n"
                + json.dumps(rejection_context, ensure_ascii=False)
            ),
        }

    def reject(
        self,
        gate_id: str,
        *,
        scope: str,
        rejected_flakes: list[dict[str, Any]] | None = None,
        reason: str = "",
        session_key: str = "",
    ) -> dict[str, Any]:
        """Reject selected flakes or the entire proposed set so the live selector can re-run."""
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"selected_flakes", "combination"}:
            raise ValueError("Rejection scope must be 'selected_flakes' or 'combination'.")
        initial_gate = self.get(gate_id)
        bound_session = str(initial_gate.get("session_key") or "")
        if bound_session and session_key and bound_session != session_key:
            raise PermissionError("This approval gate belongs to another chat session.")
        if initial_gate.get("status") == "rejected":
            return self._with_rejection_prompt(initial_gate)
        if initial_gate.get("status") != "awaiting_human_approval":
            raise ValueError(f"Gate cannot be rejected from status {initial_gate.get('status')!r}.")

        selected = [
            item for item in initial_gate.get("selected_flakes") or [] if isinstance(item, dict)
        ]
        if not selected:
            raise ValueError("Approval gate has no selected flakes to reject.")
        if normalized_scope == "combination":
            matched = selected
        else:
            requested = [item for item in rejected_flakes or [] if isinstance(item, dict)]
            if not requested:
                raise ValueError("Select at least one flake to reject.")
            matched = []
            for item in requested:
                match = next(
                    (candidate for candidate in selected if self._same_selected_flake(candidate, item)),
                    None,
                )
                if match is None:
                    raise ValueError("A requested rejected flake is not part of this gate.")
                if match not in matched:
                    matched.append(match)

        with _WRITE_LOCK:
            gate = self.get(gate_id)
            bound_session = str(gate.get("session_key") or "")
            if bound_session and session_key and bound_session != session_key:
                raise PermissionError("This approval gate belongs to another chat session.")
            if gate.get("status") == "rejected":
                return self._with_rejection_prompt(gate)
            if gate.get("status") != "awaiting_human_approval":
                raise ValueError(f"Gate cannot be rejected from status {gate.get('status')!r}.")
            now = _utc_now()
            gate["status"] = "rejected"
            gate["rejected_at"] = now
            gate["updated_at"] = now
            gate["rejection_scope"] = normalized_scope
            gate["rejection_reason"] = str(reason or "").strip()[:500]
            gate["rejected_flakes"] = matched
            rejection_entry = {
                "gate_id": gate_id,
                "scope": normalized_scope,
                "reason": gate["rejection_reason"],
                "rejected_flakes": matched,
            }
            prior_history = [
                item for item in gate.get("human_rejection_history") or [] if isinstance(item, dict)
            ]
            gate["human_rejection_history"] = [*prior_history, rejection_entry]
            identities = {
                str(value) for value in gate.get("excluded_flake_identities") or [] if value
            }
            identities.update(
                (
                    f"chip:{item.get('chip_id')}:flake:{item.get('flake_id')}"
                    if item.get("flake_id") is not None
                    else f"mask:{item.get('mask_path') or ''}"
                )
                for item in matched
            )
            gate["excluded_flake_identities"] = sorted(
                identity for identity in identities if identity and identity != "mask:"
            )
            self._write_json(self._path(gate_id), gate)

        project_id = str(gate.get("project_id") or "")
        if project_id:
            try:
                from common.services.project_state_service import ProjectStateService

                ProjectStateService().save_status(
                    project_id=project_id,
                    status="active",
                    current_stage="flake_selection_reselection",
                    next_action="Reselect a complete flake set excluding the human-rejected flakes.",
                    summary=f"Human rejected flake selection gate {gate_id}; reselecting in the same run.",
                    metadata={
                        "flake_selection_gate_id": gate_id,
                        "rejection_scope": normalized_scope,
                        "rejected_flakes": matched,
                    },
                )
            except Exception:
                pass

        return self._with_rejection_prompt(gate)

    def approve(self, gate_id: str, *, session_key: str = "") -> dict[str, Any]:
        initial_gate = self.get(gate_id)
        bound_session = str(initial_gate.get("session_key") or "")
        if bound_session and session_key and bound_session != session_key:
            raise PermissionError("This approval gate belongs to another chat session.")
        with _WRITE_LOCK:
            gate = self.get(gate_id)
            bound_session = str(gate.get("session_key") or "")
            if bound_session and session_key and bound_session != session_key:
                raise PermissionError("This approval gate belongs to another chat session.")
            if gate.get("status") not in {"awaiting_human_approval", "approved"}:
                raise ValueError(f"Gate cannot be approved from status {gate.get('status')!r}.")
            selection_path, selection, chosen = self._load_and_validate_gate_selection(gate)
            mask_preparation_required = bool(selection.get("design_mask_preparation_required"))
            gate_changed = False
            if gate.get("status") != "approved":
                now = _utc_now()
                gate["status"] = "approved"
                gate["approved_at"] = now
                gate["updated_at"] = now
                gate_changed = True
            approved_candidate_context = self._approved_candidate_context(
                gate=gate,
                selection_path=selection_path,
                selection=selection,
                chosen=chosen,
            )
            if gate.get("approved_candidate_context") != approved_candidate_context:
                gate["approved_candidate_context"] = approved_candidate_context
                gate_changed = True
            if gate_changed:
                self._write_json(self._path(gate_id), gate)

        project_id = str(gate.get("project_id") or "")
        if project_id:
            try:
                from common.services.project_state_service import ProjectStateService

                ProjectStateService().save_status(
                    project_id=project_id,
                    status="active",
                    current_stage=(
                        "flake_selection_approved_mask_preparation"
                        if mask_preparation_required
                        else "flake_selection_approved"
                    ),
                    next_action=(
                        "Segment or relink missing 20X masks for only the human-approved flakes, then run real-flake design."
                        if mask_preparation_required
                        else "Run real-flake design using only the human-approved selected flakes."
                    ),
                    summary=f"Human approved Claude SDK flake selection gate {gate_id}.",
                    metadata={
                        "flake_selection_gate_id": gate_id,
                        "flake_selection_result_path": str(selection_path.resolve()),
                        "flake_selection_human_approved": True,
                        "approved_candidate_context": approved_candidate_context,
                    },
                )
            except Exception:
                # Approval itself remains valid even when an optional project record is absent.
                pass

        continuation_prompt = (
            "[FLAKE_SELECTION_APPROVED]\n"
            f"Human approval gate {gate_id} was approved with one UI click.\n"
            f"Project id: {project_id or 'not supplied'}\n"
            f"Authoritative selection result: {selection_path.resolve()}\n"
            "The normal live path is already blocked inside the same Qumus process and resumes from "
            "this approval; use this prompt only as a backend-restart fallback. Continue the same "
            "overall Qumus workflow now. Do not rerun candidate acquisition or "
            "flake selection. Pass the exact approved real-flake context below, plus the original "
            "approved_device_guidance and prior ideal-design paths, to the next stage. If "
            "design_mask_preparation_required is true, segment or relink the selected flakes' 20X "
            "masks before invoking real-flake Claude SDK/KLayout design; never exfoliate because a "
            "selected mask is missing. Do not add any other candidate. Preserve the original "
            "approved_device_guidance and project/run directory. After successful real-flake design, "
            "continue through stacking-payload resolution and the remaining authorized workflow.\n"
            "APPROVED_REAL_FLAKE_CONTEXT_JSON:\n"
            + json.dumps(approved_candidate_context, ensure_ascii=False)
        )
        return {
            **gate,
            "approved_candidate_context": approved_candidate_context,
            "continuation_prompt": continuation_prompt,
        }
