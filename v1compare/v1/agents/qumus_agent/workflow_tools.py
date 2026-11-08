from __future__ import annotations

import json
import sys
from typing import Iterable, List

from smolagents import Tool


def _emit(tag: str, payload: str) -> None:
    """Low-level helper that writes plan events to stderr for SSE parsing."""
    # Use __stderr__ to bypass any capturing by smolagents/Test/etc.
    sys.__stderr__.write(f"{tag} {payload}\n")
    sys.__stderr__.flush()


def _normalize_steps(steps: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    for step in steps:
        text = str(step).strip()
        if not text:
            continue
        normalized.append(text)
    if not normalized:
        raise ValueError("At least one non-empty workflow step is required.")
    if len(normalized) > 8:
        normalized = normalized[:8]
    return normalized


class BroadcastWorkflowPlanTool(Tool):
    name = "broadcast_workflow_plan"
    description = (
        "Send the UI a short ordered list of workflow steps so the operator can track progress in the side panel."
        " Call this before executing a multi-step plan."
    )
    inputs = {
        "steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ordered step descriptions (3-8 items) that summarize the upcoming workflow.",
        }
    }
    output_type = "string"

    def forward(self, steps: list[str]) -> str:
        normalized = _normalize_steps(steps)
        _emit("[PLAN_INIT]", json.dumps(normalized, ensure_ascii=True))
        return f"Broadcasted workflow plan with {len(normalized)} steps."


class BroadcastWorkflowStepTool(Tool):
    name = "broadcast_workflow_step"
    description = (
        "Highlight the currently active workflow step in the UI by providing its zero-based index."
        " Call this each time you start or finish a major phase."
    )
    inputs = {
        "index": {
            "type": "integer",
            "description": "Zero-based index of the workflow step currently in progress.",
        }
    }
    output_type = "string"

    def forward(self, index: int) -> str:
        if index is None:
            raise ValueError("index is required.")
        try:
            idx = int(index)
        except Exception as exc:
            raise ValueError("index must be an integer.") from exc
        if idx < 0:
            raise ValueError("index must be zero or positive.")
        _emit("[PLAN_STEP]", str(idx))
        return f"Workflow progress broadcast at step {idx}."
