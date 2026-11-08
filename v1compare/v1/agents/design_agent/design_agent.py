"""
The agent can:
- Load and process electrode masks
- Generate design constraints from natural language
- Optimize material placement
- Provide interactive adjustments
- Visualize results

All tools are defined under agents.design_agent.tools.layout_tool for better code organization.
"""

import argparse
import json
import os
import sys
from typing import Dict, List, Optional
from pathlib import Path
from datetime import datetime

# Claude/KLayout SDK output can contain Unicode tool and geometry diagnostics.
# Make direct SDK subprocess output safe when captured by the Windows UI server.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from smolagents import CodeAgent, LiteLLMModel
import litellm
litellm.drop_params = True  # Drop unsupported params for providers that reject OpenAI-style extras.

from dotenv import load_dotenv
from common.llm_config import DEFAULT_MODEL_ID, get_api_base_for_model, get_api_key_for_model, resolve_model_id
from common.logic.stacking_run_dirs import get_stacking_run_dirs
from common.services.design_candidate_payload_service import build_design_candidate_payload

load_dotenv()

# Import all tools from design tools package
from agents.design_agent.tools.layout_tool import (
    get_all_tools,
    get_agent_state,
    reset_agent_state,
    set_agent_state,
    load_material_masks,
    load_and_separate_electrodes,
    configure_no_electrode_design,
    generate_design_constraints,
    optimize_design,
    visualize_result,
    get_violation_details,
    export_design_parameters,
    export_qumus_handoff
)
from agents.design_agent.tools.flake_chosen import choose_hbn_for_graphene, choose_best_covering_hbn
from agents.design_agent.tools.design_workflows import (
    LEGACY_MASK_OPTIMIZER,
    normalize_design_scenario,
    run_claude_sdk_klayout_design,
)

# OpenTelemetry Instrumentation for Phoenix
try:
    from phoenix.otel import register
    from openinference.instrumentation.smolagents import SmolagentsInstrumentor
    from opentelemetry import trace

    # Only register if a tracer provider hasn't been set yet
    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        register()
        SmolagentsInstrumentor().instrument()
except (ImportError, Exception):
    # Ignore if phoenix is not installed or if registration fails
    pass


# ============================================================================
# Agent Factory Function
# ============================================================================

def _default_qumus_design_output_dir() -> str:
    """Reserve/use the current stacking run's design folder for Qumus-driven design output."""
    run_dirs = get_stacking_run_dirs(create_new=False, sample_label="design")
    return run_dirs["sample_dir"]


def _merge_auto_candidate_payload(extra_context_json: str, *, enabled: bool, limit_per_role: int = 20) -> str:
    if not enabled:
        return extra_context_json
    if extra_context_json.strip():
        extra_context = json.loads(extra_context_json)
        if not isinstance(extra_context, dict):
            raise ValueError("--extra-context-json must decode to a JSON object")
    else:
        extra_context = {}

    guidance = extra_context.get("approved_device_guidance") or extra_context.get("device_guidance")
    guidance_dict = guidance if isinstance(guidance, dict) else {}
    payload = build_design_candidate_payload(
        approved_device_guidance=guidance_dict,
        artifact_paths_by_role=extra_context.get("artifact_paths_by_role"),
        artifacts_by_role=extra_context.get("artifacts_by_role"),
        limit_per_role=limit_per_role,
    )
    for key in (
        "candidate_mask_paths_by_role",
        "candidate_image_paths_by_role",
        "candidate_flakes_by_role",
        "artifact_paths_by_role",
        "artifact_records_by_role",
        "roles_satisfied_by_artifacts",
        "pixel_size_um_per_px",
        "coordinate_frame_notes",
    ):
        if key in payload and key not in extra_context:
            extra_context[key] = payload[key]
    extra_context["design_candidate_payload"] = payload
    candidates = payload.get("candidate_mask_paths_by_role")
    has_candidates = isinstance(candidates, dict) and any(
        isinstance(paths, list) and paths for paths in candidates.values()
    )
    if has_candidates:
        extra_context.setdefault("design_stage", "real_flake_adaptation")
    return json.dumps(extra_context, ensure_ascii=False)

def create_design_agent(
    model_id: str | None = DEFAULT_MODEL_ID,
    max_steps: int = 20,
    verbosity_level: int = 2,
    planning_interval: int = 3,
):
    """
    Create a quantum device design agent.
    
    Args:
        model_id: Model ID (must be provided via argument or QUMUS_DEFAULT_MODEL_ID)
        max_steps: Maximum steps for agent execution (default: 10)
        verbosity_level: Verbosity level for agent output (0=quiet, 1=normal, 2=verbose)
        planning_interval: Planning interval for the agent (default: 3)
    
    Returns:
        CodeAgent: Configured design agent instance
    
    Example:
        >>> agent = create_design_agent()
        >>> response = agent.run("Design a dual-gate Hall bar device")
        
    """
    # Initialize model
    resolved_model_id = resolve_model_id(model_id)
    api_key = get_api_key_for_model(resolved_model_id)
    api_base = get_api_base_for_model(resolved_model_id)
    if not api_key:
        raise RuntimeError("QUMUS_API_KEY not set. Please export it in your environment (e.g., export QUMUS_API_KEY=...).")
    model_kwargs: dict[str, object] = {"api_key": api_key}
    if api_base:
        model_kwargs["api_base"] = api_base
    model = LiteLLMModel(
        resolved_model_id,
        **model_kwargs,
    )
    
    # Get all available tools and include hBN selection tool
    tools = get_all_tools() + [choose_hbn_for_graphene]
    
    # Create the design agent
    design_agent = CodeAgent(
        model=model,
        tools=tools,
        max_steps=max_steps,
        verbosity_level=verbosity_level,
        executor_kwargs={"timeout_seconds": 3600},
        planning_interval=planning_interval,
        additional_authorized_imports=["os", "cv2", "numpy", "json"],
        name="design_agent",
        description=(
            "The design agent is an expert quantum device design assistant specialized in "
            "creating KLayout/GDS-ready 2D quantum device layouts from approved guidance, "
            "device-family skills, imported templates, material masks, and routing rules. It packages "
            "Claude Agent SDK/KlayoutClaw execution artifacts and machine-readable metadata for Qumus "
            "to continue autonomously through downstream workflow stages."
        )
    )
    
    # Customize system prompt
    design_agent.prompt_templates["system_prompt"] += """

You are the Design Agent for quantum device optimization.
Your primary responsibility is to help users design 2D material heterostructures for quantum devices.

System Components:
- Material masks: Binary images defining measured or candidate material shapes when real-flake adaptation is requested.
- External templates and references: Imported GDS assets, device-family profiles, and user-provided artifacts.
- Constraint rules: Geometry, connectivity, routing, clean-room, and device-stack constraints from approved guidance and skills.
- KLayout integration: KlayoutClaw-compatible MCP tools can create layouts, execute `pya`
  scripts, save GDS/OASIS, capture screenshots, and run routing/evaluation.
- Claude Agent SDK execution: `run_claude_sdk_klayout_design_tool` is the only
  permitted direct KLayout design backend for normal Qumus workflows.
- Device guidance: Qumus/project_manager may provide `approved_device_guidance` with stack roles,
  material thickness/size targets, contact/gate/activation constraints, lab-history evidence,
  literature evidence, and capability annotations. Preserve it in Claude handoff context.
- Reference assets live in `agents/design_agent/example/`. Use repo-relative paths such as
  `agents/design_agent/example/WTe2.png`, `agents/design_agent/example/hBN.png`, and
  `agents/design_agent/example/electrodes.png` when loading materials and electrodes.

For every device workflow:
- `approved_device_guidance` is the source of truth when present. If guidance
  is missing a clear workflow, synthesize a task-specific plan from approved
  physics constraints, available assets, and KLayout needs.
- Use project skills for device-family details instead of embedding those
  details in the system prompt:
  - Graphene QH / edge-contact graphene devices:
    `agents/design_agent/skills/graphene-qh-device/SKILL.md`
  - ReSe2 / 2D dual-gate / bottom-contact transistor devices:
    `agents/design_agent/skills/2d-dual-gate-transistor/SKILL.md`
  - Shared project routing, marker, pad, visual, and verification conventions:
    `agents/design_agent/skills/qumus-project-routing/SKILL.md`
- Explicit human/project-manager values override skill defaults. Do not force
  unrelated requests into graphene/ReSe2 recipes.
- For a graphene QH edge-contact request that uses the standard defaults, reuse
  the verified run_187 ideal package declared by the graphene-QH skill instead
  of executing a fresh ideal-design SDK/KLayout run. A new ideal design is
  required only when the human explicitly changes the ideal geometry or sets
  `force_ideal_redesign=true` / `reuse_default_ideal_design=false`.

- Use outline/transparent display styles where possible. Draw only
  physical/electrical circuit geometry in KLayout/GDS outputs unless the human
  explicitly requests documentation geometry. Put layer meanings, clean-room
  order, checks, and palette information in result JSON/run notes.
- Before saving GDS or running verification, merge same-layer polygonal
  geometry and run explicit checks for unintended overlaps between distinct
  nets, imported markers/pads, contacts, gates, routes, and material flakes.
  Different electrical wires/nets must keep positive clearance unless they are
  intentionally connected. Intentional contact/handoff overlaps to pre-drawn
  geometry, bottom contacts, pads, or another metal layer must be at least
  `1.5 um x 1.5 um` unless approved process or device-family guidance gives a
  different value.

1) Call `select_device_design_workflow` only to turn the approved/free-form plan
   into an ordered handoff scaffold. Pass `approved_device_guidance` through
   `extra_context_json` so the workflow comes from the guidance, not from a
   material-name lookup.
2) Do not constrain the design plan to a single clean-room visit unless the
   human explicitly asks for that. Instead, identify every clean-room operation
   such as contact/electrode lithography, metallization, etch, passivation, and
   mask/alignment work. In KLayout outputs, mark clean-room order on the real
   physical geometry layers and metadata only. Do not create extra
   lithography/deposition/etch/process-mask geometry layers unless the human
   explicitly asks for fabrication mask deliverables.
   Prefer jagged quadrilateral outlines only for virtual material flakes unless
   guidance or human feedback explicitly overrides that preference; do not draw
   separate review/device boundary frames by default.
   Do not create legend, annotation, inset, scale-bar, layer-map, substrate-frame,
   or other review-only geometry in the GDS by default. Verification must run on
   physical/electrical circuit geometry only.
3) For KLayout/GDS design tasks, use the Claude Agent SDK backend only. In
   SDK/direct Claude backend mode, Claude executes the KLayout stage through
   KlayoutClaw; no LiteLLM/OpenAI model is required for the design stage itself.
   If running inside this smolagents design agent, call
   `run_claude_sdk_klayout_design_tool`. If SDK execution is unavailable, stop
   and report that SDK/KlayoutClaw must be configured.
4) Every design output must include enough machine-readable metadata for Qumus
   to continue autonomously to the next workflow stage. After a KLayout
   execution package, ideal layout result, real-flake-adapted layout, final GDS,
   screenshot, or result JSON is produced, return it to Qumus with
   `autonomous_continuation: true` when the result is successful and unblocked;
   do not insert a human approval step for completed valid outputs. If required
   net mapping, pad assignment, process approval, or geometry constraints remain
   unresolved, return `status: blocked`, list `open_issues`, and set
   `human_review_required: true`.
5) Only use direct KlayoutClaw MCP tools (`create_klayout_layout`, `execute_klayout_script`,
   `run_klayout_mcp_tool`, etc.) if the user explicitly asks Qumus/design_agent
   to drive KLayout directly without the Claude Agent SDK backend.
6) Legacy mask optimization tools are compatibility-only. Use them only when
   the task or scenario explicitly requests the legacy electrode-backed or
   electrode-free mask optimizer. Do not use legacy material-name recipes,
   pixel-overlap constraint defaults, Interactive Design page instructions, or
   optimized-mask exports for normal Qumus KLayout/GDS workflows.

Key Rules:
- For every new request, derive the design order from `approved_device_guidance.workflow_plan`
  or from a freshly synthesized task-specific plan. Do not infer a fixed recipe from material names.
- For Qumus-driven device design:
  - candidate material paths should be grouped by approved material role using `candidate_mask_paths_by_role`
  - human/external artifact paths should be grouped by role using `artifact_paths_by_role`
  - `approved_device_guidance` from project_manager/autonomous Qumus workflow is the source of material/contact/gate/activation
    constraints and workflow plan; include it unchanged in `extra_context_json`. Treat any legacy
    `cleanroom_minimization_plan` as advisory context, not as a one-visit design constraint.
- The design-agent inputs may mix mask and optical sources:
  - for real-flake design, use only the segmented 20X path in `mask_20x_path`
    (or the already-normalized 20X value in `candidate_mask_paths_by_role`);
    never use the legacy 100X `mask_path` as design geometry
  - use optical 20X images as visual references rather than substitutes for masks
  - future 20X masks must be produced by the existing process segmentation
    code (`scripts/prepare_graphene_real_device_context.py` or the integrated
    processing-agent 20X segmentation path); do not invent a second mask
    extraction algorithm in the design agent
- Material order matters in any stack representation: define whether the list is top-to-bottom or bottom-to-top and keep it consistent with approved guidance.
- For KLayout/Claude Agent SDK workflows, report the execution package paths:
  `CLAUDE_START_HERE.md`, `claude_design_contract.json`, `ideal_device_layout.py`,
  and expected ideal GDS/screenshot/result paths.
- For KLayout/Claude Agent SDK workflows, apply the shared routing skill and
  any matching device-family skill; avoid repeating or restating skill defaults
  in generated task-specific prompts unless approved guidance overrides a value.
- For KLayout/Claude Agent SDK workflows, clean-room process sessions are
  metadata on existing real geometry layer records. Do not add
  standalone CR/session/process mask layers such as 903/90 or 904/90 unless the
  human explicitly requests fabrication mask deliverables. Effective device
  layer counts must exclude process-order-only metadata.
- For KLayout/Claude Agent SDK workflows, keep execution bounded: prefer one
  monolithic KLayout build script plus at most one verification/refinement pass,
  do not rely on state persisting across `execute_script` calls, and stop as
  soon as the required GDS, screenshot, and result JSON exist and checks pass.
  Do not continue with post-output beautification, alternate screenshots, or
  palette experiments after the review artifacts are ready.
- Before any KLayout/Claude Agent SDK review screenshot, make the review/top
  design cell active and expand hierarchy. If a layout uses child-cell
  instances for a core device, inset, or repeated geometry, either flatten/copy
  the real geometry into the screenshot review cell or set
  `_layout_view.active_cellview().cell = review_cell` and call
  `_layout_view.max_hier()` before the KlayoutClaw `screenshot` call. A
  screenshot that shows only child-cell names, instance boxes, or labels is not
  a valid review artifact even when recursive geometry checks pass.
- Keep legend, annotation, inset, scale-bar, and other review-only geometry out
  of GDS outputs unless explicitly requested, and always out of device
  verification/routing extraction. Record review metadata in JSON/run notes
  instead of drawing extra layer boxes.
- For every design output, explicitly state whether Qumus can continue
  autonomously to inventory, another design stage, processing, stacking, or
  clean-room execution. Default to autonomous continuation when required
  artifacts and movement fields are present.
- Keep every generated artifact inside the active stacking run design folder (`images/stacking/run_<N>/design/`).
- Do not create ad hoc directories such as `images/stacking/temp*`, `test_run`, or other scratch folders outside the active run folder.

Be conversational, explain what you're doing step by step, and provide helpful feedback on design quality.
"""
    
    # Initialize agent state with model ID
    set_agent_state({
        'model_id': resolved_model_id,
        'output_dir': _default_qumus_design_output_dir(),
    })

    return design_agent


def quick_design(
    task: str,
    electrode_path: Optional[str],
    graphene_path: str,
    hbn_paths: List[str],
    requirements: Optional[str] = None,
    output_dir: str = "",
    max_iter: int = 30,
    model_id: str | None = DEFAULT_MODEL_ID,
    no_electrodes: bool = False,
    scenario: str = "",
) -> Dict:
    """
    Quick design workflow without agent interaction.
    
    This function automates the entire design process by calling all tools in sequence.
    No agent or LLM inference is needed - it's a pure programmatic workflow.
    
    Args:
        task: Device task description
        electrode_path: Path to electrode mask. Optional when no_electrodes=True.
        graphene_path: Path to graphene mask
        hbn_paths: Candidate hBN mask paths
        requirements: Optional design requirements text
        output_dir: Output directory for results
        max_iter: Maximum optimization iterations
        scenario: Optional free-form workflow label. Use "legacy" for the
            original electrode-backed/electrode-free mask optimizer; general
            KLayout device design should use the Claude Agent SDK backend with
            `approved_device_guidance`.
    
    Returns:
        Results dictionary with optimized masks and transformations
    
    Example:
        >>> results = quick_design(
        ...     task="Design graphene-hBN device",
        ...     electrode_path="mask_electrodes.png",
        ...     graphene_path="mask_graphene.png",
        ...     hbn_paths=["hbn_1.png", "hbn_2.png", "hbn_3.png"],
        ...     max_iter=100
        ... )
    """
    if not output_dir:
        output_dir = _default_qumus_design_output_dir()
    resolved_model_id = resolve_model_id(model_id)
    scenario_id = normalize_design_scenario(scenario, task) if scenario else LEGACY_MASK_OPTIMIZER

    # Reset state
    reset_agent_state()
    set_agent_state({
        'model_id': resolved_model_id,
        'output_dir': output_dir,
        'design_scenario': scenario_id,
    })

    # Step 1: Choose best hBN candidate for graphene
    print("Step 1: Choosing best hBN candidate...")
    selection = choose_best_covering_hbn(graphene_path, hbn_paths)
    if not selection.get("success"):
        raise ValueError(selection.get("message", "Failed to select hBN candidate."))
    selected_hbn_path = selection["best_hbn_path"]
    print(f"[OK] Selected hBN: {selected_hbn_path}")

    materials = ["graphene", "hBN"]
    material_paths = [graphene_path, selected_hbn_path]

    # Step 2: Load materials (graphene + selected hBN)
    print("\nStep 2: Loading materials...")
    load_material_masks(",".join(materials), ",".join(material_paths))

    # Step 3: Load electrodes or explicitly configure electrode-free mode
    if no_electrodes or not electrode_path:
        print("\nStep 3: Configuring electrode-free design...")
        configure_no_electrode_design()
    else:
        print("\nStep 3: Processing electrodes...")
        load_and_separate_electrodes(electrode_path, output_dir)

    # Step 4: Generate constraints
    print("\nStep 4: Generating constraints...")
    if requirements is None:
        if no_electrodes or not electrode_path:
            requirements = (
                "hBN should fully cover graphene and act as an insulating layer. "
                "No electrode is present, so only material-material layout constraints apply."
            )
        else:
            requirements = (
                "hBN should fully cover graphene and act as an insulating layer. "
                "graphene should establish required contact with contact_electrode while avoiding unwanted electrode contacts."
            )
    generate_design_constraints(task, requirements)

    # Step 5: Optimize
    print("\nStep 5: Optimizing design...")
    optimize_design(max_iterations=max_iter, debug=True)

    # Step 6: Visualize
    print("\nStep 6: Creating visualization...")
    vis_path = os.path.join(output_dir, "design_result.png")
    visualize_result(vis_path)

    # Step 7: Export
    print("\nStep 7: Exporting parameters...")
    param_path = os.path.join(output_dir, "design_parameters.json")
    export_design_parameters(param_path)

    # Step 8: Build qumus handoff payload
    print("\nStep 8: Building qumus handoff payload...")
    handoff_json = export_qumus_handoff()

    state = get_agent_state()
    state["selected_hbn"] = selection
    state["qumus_handoff"] = handoff_json
    state["design_scenario"] = scenario_id
    return state


def reset_design_agent():
    """
    Reset the design agent state.
    
    Call this function to clear all loaded masks, constraints, and results.
    Useful when starting a new design project.
    """
    reset_agent_state()
    print("[OK] Design agent state reset")


# ============================================================================
# Main Execution
# ============================================================================

def _setup_logging(log_path: Optional[str]) -> Optional[Path]:
    """Setup logging to file and console (tee)."""
    if log_path:
        target = Path(log_path)
    else:
        root_dir = Path(__file__).resolve().parents[2]
        logs_dir = root_dir / "logs" / "design_agent"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = logs_dir / f"run_{ts}.log"

    class Tee:
        def __init__(self, file_path: Path):
            self._file = open(file_path, "w", encoding="utf-8")
            self._stdout = sys.stdout
            self._stderr = sys.stderr

        def write(self, data: str) -> None:
            self._stdout.write(data)
            self._file.write(data)

        def flush(self) -> None:
            self._stdout.flush()
            self._file.flush()

        def close(self) -> None:
            try:
                self._file.close()
            except Exception:
                pass

    tee = Tee(target)
    sys.stdout = tee  # type: ignore
    sys.stderr = tee  # type: ignore
    print(f"[log] tee to {target}")
    return target


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Quantum Device Design Agent")
    parser.add_argument("task", type=str, nargs="?", default=None,
                       help="Design task description for the Claude Agent SDK backend")
    parser.add_argument(
        "--mode",
        choices=["claude-sdk"],
        default="claude-sdk",
        help=(
            "Run mode; only 'claude-sdk' is supported for direct KLayout execution."
        ),
    )
    parser.add_argument(
        "--scenario",
        default="",
        help="Optional free-form workflow label; leave empty for AI-designed Claude/KLayout workflows",
    )
    parser.add_argument("--handoff-dir", default="",
                       help="Output directory for Claude Agent SDK/KLayout artifacts")
    parser.add_argument("--extra-context-json", default="",
                       help="Optional JSON object to include in the Claude Agent SDK KLayout execution package")
    parser.add_argument("--auto-candidate-payload", action="store_true",
                       help="Query material_base.db and merge role-grouped flake candidates into extra context before Claude/KLayout execution")
    parser.add_argument("--candidate-limit", type=int, default=20,
                       help="Maximum flake candidates per material role when --auto-candidate-payload is used")
    parser.add_argument("--klayout-mcp-url", default=os.getenv("KLAYOUT_MCP_URL", "http://127.0.0.1:8765/mcp"),
                       help="KlayoutClaw MCP URL for Claude Agent SDK execution")
    parser.add_argument("--claude-sdk-timeout", type=int, default=int(os.getenv("QUMUS_CLAUDE_SDK_TIMEOUT", "3600")),
                       help="Timeout in seconds for --mode claude-sdk (default 1 hour; smaller values are raised to the floor)")
    parser.add_argument("--claude-sdk-permission-mode", default=os.getenv("QUMUS_CLAUDE_SDK_PERMISSION_MODE", "acceptEdits"),
                       help="Claude Agent SDK permission mode for --mode claude-sdk")
    parser.add_argument("--claude-sdk-model", default=os.getenv("QUMUS_CLAUDE_SDK_MODEL", "claude-opus-5"),
                       help="Claude Agent SDK model for --mode claude-sdk; defaults to QUMUS_CLAUDE_SDK_MODEL or claude-opus-5")
    parser.add_argument("--claude-sdk-extra-allowed-tools", default=os.getenv("QUMUS_CLAUDE_SDK_EXTRA_ALLOWED_TOOLS", ""),
                       help="Comma-separated extra Claude Agent SDK tools to auto-approve")
    parser.add_argument("--claude-sdk-resume-session-id", default=os.getenv("QUMUS_CLAUDE_SDK_RESUME_SESSION_ID", ""),
                       help="Resume a previous Claude Agent SDK session for revision runs")
    parser.add_argument("--claude-sdk-resume-from-run-result", default=os.getenv("QUMUS_CLAUDE_SDK_RESUME_FROM_RUN_RESULT", ""),
                       help="Path to a previous claude_sdk_run_result.json; its session id will be used for resume")
    parser.add_argument("--allow-missing-ideal-artifacts", action="store_true",
                       help="For SDK debugging only: do not fail when ideal GDS/screenshot/result JSON are missing")
    parser.add_argument("--allow-incomplete-guidance", action="store_true",
                       help="For Claude/KLayout debugging only: allow placeholder layout execution even when approved guidance is incomplete")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        resolved_extra_context_json = _merge_auto_candidate_payload(
            args.extra_context_json,
            enabled=args.auto_candidate_payload,
            limit_per_role=args.candidate_limit,
        )
    except Exception as exc:
        print(f"\n[ERROR] Error building candidate payload: {exc}")
        sys.exit(1)

    task = args.task or "Run Claude Agent SDK as the direct KLayout design backend"
    payload = run_claude_sdk_klayout_design(
        task=task,
        scenario=args.scenario,
        handoff_dir=args.handoff_dir,
        extra_context_json=resolved_extra_context_json,
        klayout_mcp_url=args.klayout_mcp_url,
        timeout_seconds=args.claude_sdk_timeout,
        permission_mode=args.claude_sdk_permission_mode,
        claude_sdk_model=args.claude_sdk_model,
        extra_allowed_tools=args.claude_sdk_extra_allowed_tools,
        resume_session_id=args.claude_sdk_resume_session_id,
        resume_from_run_result=args.claude_sdk_resume_from_run_result,
        require_ideal_artifacts=not args.allow_missing_ideal_artifacts,
        allow_incomplete_guidance=args.allow_incomplete_guidance,
    )
    print("\n" + "="*70)
    print("[OK] Claude Agent SDK Design Backend Finished")
    print("="*70)
    print(f"Status: {payload['status']}")
    print(f"Handoff dir: {payload['handoff_dir']}")
    print(f"Start here: {payload['start_here']}")
    print(f"Contract: {payload['contract_path']}")
    print(f"KLayout script: {payload['ideal_klayout_script']}")
    print(f"Run result: {payload['claude_sdk_run_result']}")
    if payload.get("claude_sdk_session_id"):
        print(f"Claude SDK session: {payload['claude_sdk_session_id']}")
        print("Resume revisions with --claude-sdk-resume-session-id or --claude-sdk-resume-from-run-result")
    if payload.get("review_gate"):
        print(f"Review status: {payload['review_gate']}")
    if payload.get("next_qmus_action"):
        print(f"Next Qumus action: {payload['next_qmus_action']}")
    artifacts = payload.get("artifacts", {})
    if isinstance(artifacts, dict):
        print("Artifacts:")
        for name, info in artifacts.items():
            if isinstance(info, dict):
                print(f"  - {name}: {info.get('path')} exists={info.get('exists')}")
    if payload["status"] not in {"ok"}:
        print("\nClaude Agent SDK message:")
        print(payload.get("message") or payload.get("stderr") or "")
        sys.exit(1)
