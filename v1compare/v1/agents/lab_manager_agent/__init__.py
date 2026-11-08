from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from smolagents import Tool

from .lab_manager_agent import (
    StatusManager,
    build_status_summary,
    create_lab_manager_agent,
    compute_cv_consistency,
    get_lab_status_recorder,
    instrument_consistency_callback,
    status_step_callback,
)
from .tools import (
    InstrumentStatusWatchTool,
    LiveYOLOConfig,
    LiveYOLOTool,
    DatabaseQueryTool,
    ScanMaterialsTool,
)

__all__ = [
    "StatusManager",
    "build_status_summary",
    "compute_cv_consistency",
    "create_status_monitor_tools",
    "create_lab_manager_agent",
    "get_lab_status_recorder",
    "instrument_consistency_callback",
    "status_step_callback",
]


def create_status_monitor_tools(
    *,
    model_path: str | Path,
    camera_index: int = 0,
    default_detection_file: Optional[str | Path] = None,
) -> List[Tool]:
    """Return the tools controlled by the status monitor agent."""
    config = LiveYOLOConfig(
        model_path=model_path,
        camera_index=camera_index,
    )
    scan_tool = ScanMaterialsTool(
        default_detection_path=Path(default_detection_file) if default_detection_file else None,
    )
    return [
        scan_tool,
        LiveYOLOTool(config=config),
        DatabaseQueryTool(),
        InstrumentStatusWatchTool(),
    ]
