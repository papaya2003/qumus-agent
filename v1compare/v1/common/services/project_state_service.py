from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import uuid
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Artifact status vocabulary. Kept general so any device family (ReSe2, graphene,
# WSe2, future stacks) can describe human/external/Qumus-generated artifacts the
# same way.
_AVAILABLE_ARTIFACT_STATUSES = {"available", "ready", "prepared", "ok", "done", "complete"}
_PENDING_ARTIFACT_STATUSES = {
    "pending",
    "blocked",
    "waiting",
    "requested",
    "in_progress",
    "to_be_prepared",
    "needs_human",
    "needs_external_fab",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _slug(text: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", text.strip().lower()).strip("-")
    return normalized[:48] or "qumus-project"


def _parse_json_object(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw).strip()
    if not text:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object.")
    return parsed


@dataclass(frozen=True)
class ProjectArtifact:
    artifact_id: str
    artifact_type: str
    role: str
    path: str
    source: str
    status: str
    description: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class ProjectStateService:
    """File-backed project state for resumable Qumus device workflows.

    The store is intentionally general: a project can be ReSe2, graphene, or any
    future device family. Human/external artifacts are represented the same way
    as Qumus-generated artifacts, with source/status metadata deciding how Qumus
    may use them.
    """

    def __init__(
        self,
        root_dir: str | Path | None = None,
        human_gate_service: Any | None = None,
    ) -> None:
        self.root_dir = Path(root_dir) if root_dir else _repo_root() / "projects" / "qumus_projects"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._human_gate_service = human_gate_service

    def _approved_flake_context_for_project(
        self,
        project: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        metadata = project.get("metadata") if isinstance(project.get("metadata"), dict) else {}
        gate_id = str(metadata.get("flake_selection_gate_id") or "")
        if not gate_id:
            return {}, ""
        try:
            service = self._human_gate_service
            if service is None:
                from common.services.human_gate_service import HumanGateService

                service = HumanGateService()
            context = service.get_approved_candidate_context(
                gate_id,
                project_id=str(project.get("project_id") or ""),
            )
            return context, ""
        except Exception as exc:
            return {}, str(exc)

    def create_or_get_project(
        self,
        *,
        title: str,
        user_goal: str = "",
        project_id: str = "",
        material_system: str = "",
        device_family: str = "",
        status: str = "active",
        metadata: dict[str, Any] | str | None = None,
        approved_device_guidance: dict[str, Any] | str | None = None,
        workflow_plan: list[Any] | None = None,
        resume_matching: bool = True,
    ) -> dict[str, Any]:
        if project_id:
            existing = self.get_project(project_id, required=False)
            if existing:
                updates: dict[str, Any] = {
                    "title": title or existing.get("title", ""),
                    "user_goal": user_goal or existing.get("user_goal", ""),
                    "status": status or existing.get("status", "active"),
                }
                if material_system:
                    updates["material_system"] = material_system
                if device_family:
                    updates["device_family"] = device_family
                if metadata:
                    merged = dict(existing.get("metadata") or {})
                    merged.update(_parse_json_object(metadata))
                    updates["metadata"] = merged
                if approved_device_guidance:
                    updates["approved_device_guidance"] = _parse_json_object(approved_device_guidance)
                if workflow_plan is not None:
                    updates["workflow_plan"] = workflow_plan
                return self._update_project(existing, updates)

        if resume_matching and not project_id:
            resolved_title_key = _slug(title)
            resumable_statuses = {"active", "paused", "to_be_continued", "blocked"}
            matches: list[dict[str, Any]] = []
            for path in self.root_dir.glob("*.json"):
                try:
                    candidate = self._read_project(path)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                if str(candidate.get("status") or "").strip().lower() not in resumable_statuses:
                    continue
                if _slug(str(candidate.get("title") or "")) != resolved_title_key:
                    continue
                candidate_material = str(candidate.get("material_system") or "").strip().lower()
                candidate_family = str(candidate.get("device_family") or "").strip().lower()
                if material_system and candidate_material and candidate_material != material_system.strip().lower():
                    continue
                if device_family and candidate_family and candidate_family != device_family.strip().lower():
                    continue
                matches.append(candidate)
            if matches:
                existing = max(matches, key=lambda item: str(item.get("updated_at") or ""))
                updates = {
                    "user_goal": user_goal or existing.get("user_goal", ""),
                    "status": existing.get("status", "active"),
                }
                if metadata:
                    merged = dict(existing.get("metadata") or {})
                    merged.update(_parse_json_object(metadata))
                    updates["metadata"] = merged
                if approved_device_guidance:
                    updates["approved_device_guidance"] = _parse_json_object(approved_device_guidance)
                if workflow_plan is not None:
                    updates["workflow_plan"] = workflow_plan
                return self._update_project(existing, updates)

        resolved_title = title.strip() or "Qumus device project"
        resolved_id = project_id.strip() or f"{_slug(resolved_title)}-{uuid.uuid4().hex[:8]}"
        now = _utc_now()
        guidance = _parse_json_object(approved_device_guidance)
        plan = workflow_plan if workflow_plan is not None else guidance.get("workflow_plan", [])
        project = {
            "project_id": resolved_id,
            "title": resolved_title,
            "user_goal": user_goal,
            "status": status or "active",
            "material_system": material_system,
            "device_family": device_family,
            "current_stage": "initiated",
            "next_action": "",
            "created_at": now,
            "updated_at": now,
            "approved_device_guidance": guidance,
            "workflow_plan": plan if isinstance(plan, list) else [],
            "artifacts": [],
            "messages": [],
            "runs": [],
            "notes": [],
            "tags": [],
            "metadata": _parse_json_object(metadata),
        }
        self._write_project(project)
        return self.get_project(resolved_id)

    def save_status(
        self,
        *,
        project_id: str,
        status: str = "",
        current_stage: str = "",
        next_action: str = "",
        summary: str = "",
        metadata: dict[str, Any] | str | None = None,
        approved_device_guidance: dict[str, Any] | str | None = None,
        workflow_plan: list[Any] | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        updates: dict[str, Any] = {}
        if status:
            updates["status"] = status
        if current_stage:
            updates["current_stage"] = current_stage
        if next_action:
            updates["next_action"] = next_action
        if metadata:
            merged = dict(project.get("metadata") or {})
            merged.update(_parse_json_object(metadata))
            updates["metadata"] = merged
        if approved_device_guidance:
            updates["approved_device_guidance"] = _parse_json_object(approved_device_guidance)
        if workflow_plan is not None:
            updates["workflow_plan"] = workflow_plan
        if summary:
            notes = list(project.get("notes") or [])
            notes.append({"created_at": _utc_now(), "kind": "status_summary", "text": summary})
            updates["notes"] = notes
        return self._update_project(project, updates)

    def register_artifact(
        self,
        *,
        project_id: str,
        artifact_type: str,
        role: str = "",
        path: str = "",
        source: str = "human_provided",
        status: str = "available",
        description: str = "",
        metadata: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        now = _utc_now()
        artifact = {
            "artifact_id": f"art_{uuid.uuid4().hex[:10]}",
            "artifact_type": artifact_type.strip() or "artifact",
            "role": role.strip(),
            "path": path.strip(),
            "source": source.strip() or "human_provided",
            "status": status.strip() or "available",
            "description": description.strip(),
            "metadata": _parse_json_object(metadata),
            "created_at": now,
            "updated_at": now,
        }
        artifacts = list(project.get("artifacts") or [])
        artifacts.append(artifact)
        updated = self._update_project(project, {"artifacts": artifacts})
        return {
            "project": self._compact_project(updated),
            "artifact": artifact,
            "resume_context": self.build_resume_context(updated["project_id"]),
        }

    def update_artifact(
        self,
        *,
        project_id: str,
        artifact_id: str = "",
        role: str = "",
        artifact_type: str = "",
        path: str | None = None,
        source: str | None = None,
        status: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | str | None = None,
        merge_metadata: bool = True,
    ) -> dict[str, Any]:
        """Update a previously registered artifact.

        Typical use: an artifact was registered as ``pending`` before pausing for
        human/external work (e.g. hand-prepared electrodes, external-fab contacts,
        a cleanroom trip). When the operator reports it is done, flip it to a
        ready status and attach the real path/metadata so downstream design or
        stacking can consume it. This is intentionally material-agnostic.
        """
        project = self.get_project(project_id)
        artifacts = list(project.get("artifacts") or [])
        target = self._find_artifact(
            artifacts, artifact_id=artifact_id, role=role, artifact_type=artifact_type
        )
        if target is None:
            raise ValueError(
                "No matching artifact found. Provide a known artifact_id, or a role/artifact_type "
                "that matches a registered artifact."
            )
        if path is not None:
            target["path"] = str(path).strip()
        if source is not None and source.strip():
            target["source"] = source.strip()
        if status is not None and status.strip():
            target["status"] = status.strip()
        if description is not None:
            target["description"] = description.strip()
        if metadata:
            parsed = _parse_json_object(metadata)
            if merge_metadata:
                merged = dict(target.get("metadata") or {})
                merged.update(parsed)
                target["metadata"] = merged
            else:
                target["metadata"] = parsed
        target["updated_at"] = _utc_now()
        updated = self._update_project(project, {"artifacts": artifacts})
        return {
            "project": self._compact_project(updated),
            "artifact": target,
            "resume_context": self.build_resume_context(updated["project_id"]),
        }

    def list_pending_artifacts(self, project_id_or_title: str) -> list[dict[str, Any]]:
        project = self.get_project(project_id_or_title)
        return [
            artifact
            for artifact in (project.get("artifacts") or [])
            if str(artifact.get("status", "")).strip().lower() in _PENDING_ARTIFACT_STATUSES
        ]

    def _find_artifact(
        self,
        artifacts: list[dict[str, Any]],
        *,
        artifact_id: str = "",
        role: str = "",
        artifact_type: str = "",
    ) -> dict[str, Any] | None:
        artifact_id = (artifact_id or "").strip()
        if artifact_id:
            for artifact in artifacts:
                if str(artifact.get("artifact_id", "")) == artifact_id:
                    return artifact
            return None

        def _match(field: str, value: str) -> dict[str, Any] | None:
            value = value.strip().lower()
            if not value:
                return None
            hits = [a for a in artifacts if str(a.get(field, "")).strip().lower() == value]
            if not hits:
                return None
            # Most recently updated wins when several share a role/type.
            return sorted(hits, key=lambda a: str(a.get("updated_at") or a.get("created_at") or ""))[-1]

        return _match("role", role) or _match("artifact_type", artifact_type)

    def build_design_context(
        self,
        project_id_or_title: str,
        *,
        include_inventory_candidates: bool = True,
        limit_per_role: int = 20,
    ) -> dict[str, Any]:
        """Assemble an ``extra_context``-ready payload for a design/stacking handoff.

        Combines the saved ``approved_device_guidance`` with role-grouped
        human/external artifacts and (best effort) live inventory flake
        candidates, so a resumed project can be handed straight to the design
        agent / Claude SDK KLayout backend without re-reading the whole chat.
        """
        resume = self.build_resume_context(project_id_or_title)
        context: dict[str, Any] = {
            "project_id": resume["project_id"],
            "current_stage": resume.get("current_stage", ""),
            "next_action": resume.get("next_action", ""),
            "approved_device_guidance": resume.get("approved_device_guidance") or {},
            "workflow_plan": resume.get("workflow_plan") or [],
            "artifact_paths_by_role": resume.get("artifact_paths_by_role") or {},
            "artifacts_by_role": resume.get("artifacts_by_role") or {},
            "pending_artifacts": resume.get("pending_artifacts") or [],
        }
        if include_inventory_candidates:
            try:
                from common.services.design_candidate_payload_service import (
                    build_design_candidate_payload,
                )

                payload = build_design_candidate_payload(
                    approved_device_guidance=context["approved_device_guidance"],
                    artifact_paths_by_role=context["artifact_paths_by_role"],
                    artifacts_by_role=context["artifacts_by_role"],
                    limit_per_role=limit_per_role,
                )
                context["design_candidate_payload"] = payload
                for key in (
                    "candidate_mask_paths_by_role",
                    "candidate_image_paths_by_role",
                    "candidate_image_20x_paths_by_role",
                    "candidate_flakes_by_role",
                    "pixel_size_um_per_px",
                    "coordinate_frame_notes",
                ):
                    if key in payload:
                        context[key] = payload[key]
            except Exception as exc:  # pragma: no cover - inventory DB is optional here
                context["design_candidate_payload_error"] = str(exc)
        approved_candidate_context = resume.get("approved_candidate_context")
        if isinstance(approved_candidate_context, dict) and approved_candidate_context:
            # The verified gate selection is authoritative and must replace the
            # wider live-inventory candidate maps on resume.
            context.update(approved_candidate_context)
            context["approved_candidate_context"] = approved_candidate_context
        if resume.get("flake_selection_approval_error"):
            context["flake_selection_approval_error"] = resume["flake_selection_approval_error"]
        return context

    def append_message(
        self,
        *,
        project_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        messages = list(project.get("messages") or [])
        messages.append(
            {
                "message_id": f"msg_{uuid.uuid4().hex[:10]}",
                "role": role.strip() or "note",
                "content": content,
                "metadata": _parse_json_object(metadata),
                "created_at": _utc_now(),
            }
        )
        return self._update_project(project, {"messages": messages})

    def record_run(
        self,
        *,
        project_id: str,
        run_type: str,
        status: str,
        result_summary: str = "",
        result: dict[str, Any] | str | None = None,
        parameters: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        runs = list(project.get("runs") or [])
        runs.append(
            {
                "run_id": f"run_{uuid.uuid4().hex[:10]}",
                "run_type": run_type,
                "status": status,
                "result_summary": result_summary,
                "result": _parse_json_object(result),
                "parameters": _parse_json_object(parameters),
                "created_at": _utc_now(),
            }
        )
        return self._update_project(project, {"runs": runs})

    def get_project(self, project_id_or_title: str, *, required: bool = True) -> dict[str, Any] | None:
        key = project_id_or_title.strip()
        if not key:
            if required:
                raise ValueError("project_id_or_title is required.")
            return None
        path = self._path_for_id(key)
        if path.exists():
            return self._read_project(path)

        matches = [
            project
            for project in self.list_projects(limit=200)
            if str(project.get("title", "")).strip().lower() == key.lower()
        ]
        if matches:
            return self._read_project(self._path_for_id(matches[0]["project_id"]))
        if required:
            raise ValueError(f"Project not found: {project_id_or_title}")
        return None

    def list_projects(self, *, status: str = "", limit: int = 20) -> list[dict[str, Any]]:
        projects = []
        for path in sorted(self.root_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            project = self._read_project(path)
            if status and str(project.get("status", "")).lower() != status.lower():
                continue
            projects.append(self._compact_project(project))
            if len(projects) >= max(1, int(limit)):
                break
        return projects

    def build_resume_context(self, project_id_or_title: str) -> dict[str, Any]:
        project = self.get_project(project_id_or_title)
        assert project is not None
        approved_candidate_context, approval_error = self._approved_flake_context_for_project(project)
        project_metadata = project.get("metadata") if isinstance(project.get("metadata"), dict) else {}
        artifacts = list(project.get("artifacts") or [])
        available = [
            a for a in artifacts
            if str(a.get("status", "")).strip().lower() in _AVAILABLE_ARTIFACT_STATUSES
        ]
        pending_artifacts = [
            a for a in artifacts
            if str(a.get("status", "")).strip().lower() in _PENDING_ARTIFACT_STATUSES
        ]
        artifact_paths_by_role: dict[str, list[str]] = {}
        artifacts_by_role: dict[str, list[dict[str, Any]]] = {}
        artifacts_by_type: dict[str, list[dict[str, Any]]] = {}
        for artifact in available:
            role = str(artifact.get("role") or artifact.get("artifact_type") or "artifact")
            artifact_type = str(artifact.get("artifact_type") or "artifact")
            artifacts_by_role.setdefault(role, []).append(artifact)
            artifacts_by_type.setdefault(artifact_type, []).append(artifact)
            path = str(artifact.get("path") or "")
            if path:
                artifact_paths_by_role.setdefault(role, []).append(path)

        resume_context = {
            "project_id": project["project_id"],
            "title": project.get("title", ""),
            "status": project.get("status", ""),
            "material_system": project.get("material_system", ""),
            "device_family": project.get("device_family", ""),
            "current_stage": project.get("current_stage", ""),
            "next_action": project.get("next_action", ""),
            "approved_device_guidance": project.get("approved_device_guidance") or {},
            "workflow_plan": project.get("workflow_plan") or [],
            "artifact_paths_by_role": artifact_paths_by_role,
            "artifacts_by_role": artifacts_by_role,
            "artifacts_by_type": artifacts_by_type,
            "pending_artifacts": pending_artifacts,
            "blocked_on_human_or_external": bool(pending_artifacts),
            "flake_selection_gate_id": (
                approved_candidate_context.get("flake_selection_gate_id")
                if approved_candidate_context
                else str(project_metadata.get("flake_selection_gate_id") or "")
            ),
            "flake_selection_human_approved": bool(approved_candidate_context),
            "approved_candidate_context": approved_candidate_context,
            "recent_runs": list(project.get("runs") or [])[-5:],
            "recent_notes": list(project.get("notes") or [])[-5:],
            "continuation_instruction": (
                "Use this resume_context as Qumus project state. Preserve approved_device_guidance, "
                "pass artifact_paths_by_role/artifacts_by_role to design_agent or lab_manager when preparing "
                "real-flake/artifact-specific design inputs, and continue from current_stage/next_action. "
                "If pending_artifacts is non-empty, those human/external items are not ready yet: keep the "
                "project to_be_continued and ask the operator to prepare and register them before that stage."
            ),
        }
        if approval_error:
            resume_context["flake_selection_approval_error"] = approval_error
        if approved_candidate_context:
            if approved_candidate_context.get("design_mask_preparation_required"):
                next_approved_step = (
                    "segment or relink only the approved flakes' existing 20X images, then continue to "
                    "real-flake design with the same physical set"
                )
            else:
                next_approved_step = "continue directly to real-flake design"
            resume_context["continuation_instruction"] = (
                "The persisted flake-selection gate is approved and authoritative. Do not rerun flake "
                "selection, inventory acquisition, or exfoliation. Call get_project_design_context, merge "
                "approved_candidate_context at the root of extra_context_json with the saved "
                f"approved_device_guidance and prior ideal-design paths, and {next_approved_step}."
            )
        return resume_context

    def _compact_project(self, project: dict[str, Any]) -> dict[str, Any]:
        return {
            "project_id": project.get("project_id"),
            "title": project.get("title"),
            "status": project.get("status"),
            "material_system": project.get("material_system"),
            "device_family": project.get("device_family"),
            "current_stage": project.get("current_stage"),
            "next_action": project.get("next_action"),
            "updated_at": project.get("updated_at"),
            "artifact_count": len(project.get("artifacts") or []),
            "run_count": len(project.get("runs") or []),
        }

    def _update_project(self, project: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        project.update(updates)
        project["updated_at"] = _utc_now()
        self._write_project(project)
        return self.get_project(project["project_id"])

    def _path_for_id(self, project_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_id.strip())
        return self.root_dir / f"{safe}.json"

    def _read_project(self, path: Path) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            project = json.load(f)
        if not isinstance(project, dict):
            raise ValueError(f"Invalid project file: {path}")
        return project

    def _write_project(self, project: dict[str, Any]) -> None:
        path = self._path_for_id(str(project["project_id"]))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(project, f, ensure_ascii=False, indent=2)
