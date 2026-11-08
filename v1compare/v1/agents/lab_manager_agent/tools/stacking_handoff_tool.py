from __future__ import annotations

import json
from typing import Any

from smolagents import Tool

from common.services.stacking_handoff_payload_service import StackingHandoffPayloadService


class ResolveStackingHandoffTool(Tool):
    name = "resolve_stacking_handoff"
    description = (
        "Resolve a design-agent real-flake stacking_handoff.json into the exact "
        "processing-agent additional_args for assembly_device or assembly_quantum_hall_device. "
        "It matches selected flakes by flake_id or the effective 20X mask path and fills chip corner references "
        "and 20X image paths from material_base.db."
    )
    inputs = {
        "workflow_name": {
            "type": "string",
            "description": "Processing workflow name: assembly_device or assembly_quantum_hall_device.",
        },
        "stacking_handoff_path": {
            "type": "string",
            "description": "Path to design-agent stacking_handoff.json.",
            "nullable": True,
            "default": "",
        },
        "stacking_handoff_json": {
            "type": "string",
            "description": "Raw stacking_handoff JSON object as a string; used when no path is available.",
            "nullable": True,
            "default": "",
        },
        "bottom_electrode_id": {
            "type": "integer",
            "description": (
                "Optional bottom_electrode.id to use. If omitted, Qumus deterministically selects "
                "the lowest available bottom-electrode id."
            ),
            "nullable": True,
            "default": None,
        },
        "role_to_slot_json": {
            "type": "string",
            "description": "Optional JSON mapping from design roles to processing slots, e.g. {\"top_hbn\":\"a\"}. If omitted, order decides A/B/C/D.",
            "nullable": True,
            "default": "",
        },
    }
    output_type = "string"

    def __init__(self, service: StackingHandoffPayloadService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service or StackingHandoffPayloadService()

    def forward(
        self,
        workflow_name: str,
        stacking_handoff_path: str = "",
        stacking_handoff_json: str = "",
        bottom_electrode_id: int | None = None,
        role_to_slot_json: str = "",
    ) -> str:
        role_to_slot = {}
        if role_to_slot_json.strip():
            role_to_slot = json.loads(role_to_slot_json)
            if not isinstance(role_to_slot, dict):
                raise ValueError("role_to_slot_json must be a JSON object.")
        result = self._service.resolve(
            workflow_name=workflow_name,
            stacking_handoff_path=stacking_handoff_path,
            stacking_handoff_json=stacking_handoff_json,
            bottom_electrode_id=bottom_electrode_id,
            role_to_slot={str(k): str(v) for k, v in role_to_slot.items()},
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
