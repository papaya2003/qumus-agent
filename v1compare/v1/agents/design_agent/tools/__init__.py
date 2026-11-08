"""Tooling package for the design agent."""

from __future__ import annotations

_LAYOUT_EXPORTS = {
    "configure_no_electrode_design",
    "export_design_parameters",
    "export_qumus_handoff",
    "generate_design_constraints",
    "get_agent_state",
    "get_all_tools",
    "get_violation_details",
    "load_and_separate_electrodes",
    "load_material_masks",
    "optimize_design",
    "reset_agent_state",
    "set_agent_state",
    "visualize_result",
}

_WORKFLOW_EXPORTS = {
    "build_claude_klayout_handoff",
    "get_workflow_definition",
    "normalize_design_scenario",
    "run_claude_sdk_klayout_design",
    "run_claude_sdk_klayout_design_tool",
    "select_device_design_workflow",
}

_KLAYOUT_EXPORTS = {
    "capture_klayout_screenshot",
    "check_klayout_mcp",
    "create_klayout_layout",
    "execute_klayout_script",
    "export_optimized_masks_to_klayout",
    "run_klayout_mcp_tool",
    "save_klayout_layout",
}

__all__ = sorted(_LAYOUT_EXPORTS | _WORKFLOW_EXPORTS | _KLAYOUT_EXPORTS)


def __getattr__(name: str):
    """Lazily expose tools so tests can import light modules in isolation."""
    if name in _LAYOUT_EXPORTS:
        from . import layout_tool

        return getattr(layout_tool, name)

    if name in _WORKFLOW_EXPORTS:
        from . import design_workflows

        return getattr(design_workflows, name)

    if name in _KLAYOUT_EXPORTS:
        from . import klayout_claw_tool

        return getattr(klayout_claw_tool, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
