from __future__ import annotations

import json
from typing import Any

from smolagents import Tool

from common.services.project_state_service import ProjectStateService


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


class StartOrGetProjectTool(Tool):
    name = "start_or_get_project"
    description = (
        "Create a resumable Qumus project, reuse the newest matching unfinished project automatically, "
        "or update/get an existing project when project_id is supplied. Use this for multi-stage device "
        "work that may pause for human/external artifacts. If the user says new device, start fresh, or "
        "do not resume, set force_new_project=true; it overrides project_id and matching history."
    )
    inputs = {
        "title": {"type": "string", "description": "Short project title."},
        "user_goal": {"type": "string", "description": "Original or current user goal.", "nullable": True, "default": ""},
        "project_id": {"type": "string", "description": "Existing project id to reuse/update, if known.", "nullable": True, "default": ""},
        "material_system": {"type": "string", "description": "Material system label, e.g. ReSe2, graphene, WSe2.", "nullable": True, "default": ""},
        "device_family": {"type": "string", "description": "Device family label, e.g. transistor, quantum_hall, photodetector.", "nullable": True, "default": ""},
        "status": {"type": "string", "description": "Project status such as active, paused, to_be_continued, complete.", "nullable": True, "default": "active"},
        "force_new_project": {"type": "boolean", "description": "Create a separate project even if a matching unfinished project exists. Use only when the user explicitly asks for a new/separate device.", "nullable": True, "default": False},
        "extra_context_json": {"type": "string", "description": "Optional JSON object. May include metadata, approved_device_guidance, workflow_plan.", "nullable": True, "default": ""},
    }
    output_type = "string"

    def __init__(self, service: ProjectStateService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service or ProjectStateService()

    def forward(
        self,
        title: str,
        user_goal: str = "",
        project_id: str = "",
        material_system: str = "",
        device_family: str = "",
        status: str = "active",
        force_new_project: bool = False,
        extra_context_json: str = "",
    ) -> str:
        context = json.loads(extra_context_json) if extra_context_json.strip() else {}
        if not isinstance(context, dict):
            raise ValueError("extra_context_json must be a JSON object.")
        if force_new_project:
            # A stale project_id must never defeat an explicit fresh-start request.
            project_id = ""
        project = self._service.create_or_get_project(
            title=title,
            user_goal=user_goal,
            project_id=project_id,
            material_system=material_system,
            device_family=device_family,
            status=status,
            metadata=context.get("metadata"),
            approved_device_guidance=context.get("approved_device_guidance"),
            workflow_plan=context.get("workflow_plan"),
            resume_matching=not force_new_project,
        )
        return _json_dumps(
            {
                "status": "ok",
                "fresh_project": bool(force_new_project),
                "project": project,
                "resume_context": self._service.build_resume_context(project["project_id"]),
            }
        )


class SaveProjectStatusTool(Tool):
    name = "save_project_status"
    description = (
        "Save or pause a Qumus project's current stage, next action, guidance, workflow, and summary. "
        "Use this whenever the user says save, pause, to be continued, or before waiting for human/external work."
    )
    inputs = {
        "project_id": {"type": "string", "description": "Project id returned by start_or_get_project."},
        "status": {"type": "string", "description": "Project status such as active, paused, to_be_continued, blocked, complete.", "nullable": True, "default": ""},
        "current_stage": {"type": "string", "description": "Current project stage.", "nullable": True, "default": ""},
        "next_action": {"type": "string", "description": "Next action Qumus should take when resumed.", "nullable": True, "default": ""},
        "summary": {"type": "string", "description": "Short saved status summary.", "nullable": True, "default": ""},
        "extra_context_json": {"type": "string", "description": "Optional JSON object. May include metadata, approved_device_guidance, workflow_plan.", "nullable": True, "default": ""},
    }
    output_type = "string"

    def __init__(self, service: ProjectStateService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service or ProjectStateService()

    def forward(
        self,
        project_id: str,
        status: str = "",
        current_stage: str = "",
        next_action: str = "",
        summary: str = "",
        extra_context_json: str = "",
    ) -> str:
        context = json.loads(extra_context_json) if extra_context_json.strip() else {}
        if not isinstance(context, dict):
            raise ValueError("extra_context_json must be a JSON object.")
        project = self._service.save_status(
            project_id=project_id,
            status=status,
            current_stage=current_stage,
            next_action=next_action,
            summary=summary,
            metadata=context.get("metadata"),
            approved_device_guidance=context.get("approved_device_guidance"),
            workflow_plan=context.get("workflow_plan"),
        )
        return _json_dumps(
            {
                "status": "ok",
                "project": project,
                "resume_context": self._service.build_resume_context(project_id),
            }
        )


class RegisterProjectArtifactTool(Tool):
    name = "register_project_artifact"
    description = (
        "Register a Qumus project artifact from any source, including human-prepared electrodes, contacts, "
        "external-fab files, design outputs, masks, images, flakes, substrates, or cleanroom-ready pieces."
    )
    inputs = {
        "project_id": {"type": "string", "description": "Project id returned by start_or_get_project."},
        "artifact_type": {"type": "string", "description": "General artifact type, e.g. gate_electrode, contact, mask, gds, flake, substrate."},
        "role": {"type": "string", "description": "Role in the device/workflow, e.g. bottom_gate, source_drain_contacts, top_hBN.", "nullable": True, "default": ""},
        "path": {"type": "string", "description": "Optional file/image/GDS/mask path or external reference.", "nullable": True, "default": ""},
        "source": {"type": "string", "description": "Source label, e.g. human_provided, external_fab, qumus_generated, lab_manager, design_agent.", "nullable": True, "default": "human_provided"},
        "status": {"type": "string", "description": "Artifact status, e.g. available, prepared, pending, blocked.", "nullable": True, "default": "available"},
        "description": {"type": "string", "description": "Short artifact description.", "nullable": True, "default": ""},
        "metadata_json": {"type": "string", "description": "Optional JSON object with dimensions, chip_id, coordinates, process notes, material, thickness, etc.", "nullable": True, "default": ""},
    }
    output_type = "string"

    def __init__(self, service: ProjectStateService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service or ProjectStateService()

    def forward(
        self,
        project_id: str,
        artifact_type: str,
        role: str = "",
        path: str = "",
        source: str = "human_provided",
        status: str = "available",
        description: str = "",
        metadata_json: str = "",
    ) -> str:
        metadata = json.loads(metadata_json) if metadata_json.strip() else {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata_json must be a JSON object.")
        result = self._service.register_artifact(
            project_id=project_id,
            artifact_type=artifact_type,
            role=role,
            path=path,
            source=source,
            status=status,
            description=description,
            metadata=metadata,
        )
        return _json_dumps({"status": "ok", **result})


class UpdateProjectArtifactTool(Tool):
    name = "update_project_artifact"
    description = (
        "Update an already-registered Qumus project artifact. Use this when a pending human-prepared "
        "or external-fab item (electrodes, contacts, gates, masks, chips, cleanroom outputs) becomes "
        "ready: flip its status and attach the real path/metadata so downstream design or stacking can use it."
    )
    inputs = {
        "project_id": {"type": "string", "description": "Project id returned by start_or_get_project."},
        "artifact_id": {"type": "string", "description": "Exact artifact id, if known.", "nullable": True, "default": ""},
        "role": {"type": "string", "description": "Artifact role to match when artifact_id is unknown, e.g. bottom_gate, source_drain_contacts.", "nullable": True, "default": ""},
        "artifact_type": {"type": "string", "description": "Artifact type to match when neither id nor role is known.", "nullable": True, "default": ""},
        "path": {"type": "string", "description": "New file/image/GDS/mask path or external reference.", "nullable": True, "default": ""},
        "source": {"type": "string", "description": "Updated source label.", "nullable": True, "default": ""},
        "status": {"type": "string", "description": "Updated status, e.g. available, prepared, ready, pending, blocked.", "nullable": True, "default": ""},
        "description": {"type": "string", "description": "Updated short description.", "nullable": True, "default": ""},
        "metadata_json": {"type": "string", "description": "Optional JSON object merged into existing artifact metadata.", "nullable": True, "default": ""},
    }
    output_type = "string"

    def __init__(self, service: ProjectStateService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service or ProjectStateService()

    def forward(
        self,
        project_id: str,
        artifact_id: str = "",
        role: str = "",
        artifact_type: str = "",
        path: str = "",
        source: str = "",
        status: str = "",
        description: str = "",
        metadata_json: str = "",
    ) -> str:
        result = self._service.update_artifact(
            project_id=project_id,
            artifact_id=artifact_id,
            role=role,
            artifact_type=artifact_type,
            path=path if path.strip() else None,
            source=source if source.strip() else None,
            status=status if status.strip() else None,
            description=description if description.strip() else None,
            metadata=metadata_json if metadata_json.strip() else None,
        )
        return _json_dumps({"status": "ok", **result})


class GetProjectDesignContextTool(Tool):
    name = "get_project_design_context"
    description = (
        "Load a saved Qumus project and return an extra_context-ready payload for a design or stacking "
        "handoff: approved_device_guidance, workflow_plan, human/external artifacts grouped by role, "
        "pending (not-yet-ready) artifacts, and best-effort live inventory flake candidates."
    )
    inputs = {
        "project_id_or_title": {"type": "string", "description": "Project id or exact title."},
        "include_inventory_candidates": {"type": "boolean", "description": "If true, also merge live material_base.db flake candidates by role.", "nullable": True, "default": True},
    }
    output_type = "string"

    def __init__(self, service: ProjectStateService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service or ProjectStateService()

    def forward(self, project_id_or_title: str, include_inventory_candidates: bool = True) -> str:
        context = self._service.build_design_context(
            project_id_or_title,
            include_inventory_candidates=include_inventory_candidates,
        )
        return _json_dumps({"status": "ok", "design_context": context})


class GetProjectContextTool(Tool):
    name = "get_project_context"
    description = (
        "Load a saved Qumus project and return resume_context for continuing design, inventory, "
        "human-artifact integration, or stacking from the saved state."
    )
    inputs = {
        "project_id_or_title": {"type": "string", "description": "Project id or exact title."},
        "include_full_project": {"type": "boolean", "description": "If true, include the full saved project JSON.", "nullable": True, "default": False},
    }
    output_type = "string"

    def __init__(self, service: ProjectStateService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service or ProjectStateService()

    def forward(self, project_id_or_title: str, include_full_project: bool = False) -> str:
        resume_context = self._service.build_resume_context(project_id_or_title)
        payload: dict[str, Any] = {"status": "ok", "resume_context": resume_context}
        if include_full_project:
            payload["project"] = self._service.get_project(project_id_or_title)
        return _json_dumps(payload)


class ListProjectsTool(Tool):
    name = "list_projects"
    description = "List saved Qumus projects, optionally filtered by status."
    inputs = {
        "status": {"type": "string", "description": "Optional status filter.", "nullable": True, "default": ""},
        "limit": {"type": "integer", "description": "Maximum number of projects to return.", "nullable": True, "default": 20},
    }
    output_type = "string"

    def __init__(self, service: ProjectStateService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service or ProjectStateService()

    def forward(self, status: str = "", limit: int = 20) -> str:
        return _json_dumps({"status": "ok", "projects": self._service.list_projects(status=status, limit=limit)})


def create_project_state_tools() -> list[Tool]:
    service = ProjectStateService()
    return [
        StartOrGetProjectTool(service=service),
        SaveProjectStatusTool(service=service),
        RegisterProjectArtifactTool(service=service),
        UpdateProjectArtifactTool(service=service),
        GetProjectContextTool(service=service),
        GetProjectDesignContextTool(service=service),
        ListProjectsTool(service=service),
    ]
