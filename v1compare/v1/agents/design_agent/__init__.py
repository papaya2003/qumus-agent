from __future__ import annotations

_AGENT_EXPORTS = {
    "create_design_agent",
    "reset_design_agent",
}

_TOOL_EXPORTS = {
    "get_all_tools",
    "get_agent_state",
    "reset_agent_state",
    "set_agent_state",
    "load_material_masks",
    "load_and_separate_electrodes",
    "configure_no_electrode_design",
    "generate_design_constraints",
    "optimize_design",
    "visualize_result",
    "get_violation_details",
    "export_design_parameters",
    "export_qumus_handoff",
}

_WORKFLOW_EXPORTS = {
    "build_claude_klayout_handoff",
    "run_claude_sdk_klayout_design",
    "run_claude_sdk_klayout_design_tool",
}

__all__ = sorted(_AGENT_EXPORTS | _TOOL_EXPORTS | _WORKFLOW_EXPORTS)


def __getattr__(name: str):
    """Lazily expose design-agent helpers without importing heavy workflows."""
    if name in _AGENT_EXPORTS:
        from . import design_agent

        return getattr(design_agent, name)

    if name in _TOOL_EXPORTS:
        from .tools import layout_tool

        return getattr(layout_tool, name)

    if name in _WORKFLOW_EXPORTS:
        from .tools import design_workflows

        return getattr(design_workflows, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
