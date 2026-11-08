from __future__ import annotations

import json
from typing import Any

from smolagents import Tool

from common.services.lab_setup_service import LabSetupService


class LabSetupTool(Tool):
    name = "get_lab_setup"
    description = (
        "Return a snapshot of the CURRENT LAB SETUP: raw crystals staged for exfoliation, "
        "flake inventory by material, chip-slot / instrument state, and the fixed capability "
        "envelope (what Qumus hardware executes vs. what needs a human/external fab). Call this "
        "before recommending materials or a device stack so the advice fits this bench."
    )
    inputs = {
        "as_json": {
            "type": "boolean",
            "description": "If true, return the full structured snapshot as JSON instead of compact text.",
            "nullable": True,
            "default": False,
        },
        "capability_only": {
            "type": "boolean",
            "description": (
                "If true, return ONLY the capability envelope (Qumus-executable vs. "
                "human/external steps) with no staged-crystal, flake-inventory, chip-slot, "
                "or material-code details. Use for 'what materials could we explore' surveys "
                "where inventory/DB state must not bias the answer."
            ),
            "nullable": True,
            "default": False,
        },
    }
    output_type = "string"

    def __init__(self, service: LabSetupService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service or LabSetupService()

    def forward(self, as_json: bool = False, capability_only: bool = False) -> str:
        if as_json and not capability_only:
            return json.dumps(self._service.build_snapshot().to_dict(), ensure_ascii=False, indent=2)
        return self._service.build_setup_text(capability_only=capability_only)
