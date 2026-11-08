from __future__ import annotations

import argparse
import os
import sys

# The processing agent is also launched with stdout connected to the Qumus
# SSE pipe on Windows.  Keep hardware/controller diagnostics UTF-8 safe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass

from smolagents import CodeAgent, LiteLLMModel, tool
import litellm
litellm.drop_params = True  # Drop unsupported params for providers that reject OpenAI-style extras.
from agents.processing_agent.workflows import create_all_workflow_tools
from agents.processing_agent.tools.chip_pickup_tool import SelectChipPickupPosition
from dotenv import load_dotenv
from common.llm_config import DEFAULT_MODEL_ID, get_api_base_for_model, get_api_key_for_model, resolve_model_id

load_dotenv()


_STATUS_ONLY_MARKERS = (
    "status-only",
    "status check",
    "check status",
    "instrument status",
    "heater status",
    "vacuum status",
    "current temperature",
    "read temperature",
    "what is the temperature",
    "is the heater on",
    "is the vacuum on",
)

_PHYSICAL_ACTION_MARKERS = (
    "exfoliate",
    "search chip",
    "autosearch",
    "stack",
    "assemble",
    "move ",
    "pick up",
    "pickup",
    "place ",
    "clear stage",
    "prepare tape",
    "home ",
    "reset ",
    "turn on",
    "turn off",
    "heater on",
    "heater off",
    "set voltage",
    "heat to",
    "drop down",
    "remove stamp",
    "resume_processing_checkpoint",
    "resume from",
)

_STATUS_ACTION_NEGATIONS = (
    "no need to turn off",
    "do not turn off",
    "don't turn off",
    "must not turn off",
    "without turning off",
    "do not change",
    "don't change",
    "without changing",
)


def _is_read_only_status_request(task: str) -> bool:
    """Identify observational requests that must never initialize hardware."""
    normalized = " ".join(str(task or "").casefold().split())
    if "status-only" in normalized:
        return True
    has_status_intent = any(marker in normalized for marker in _STATUS_ONLY_MARKERS)
    if has_status_intent and any(marker in normalized for marker in _STATUS_ACTION_NEGATIONS):
        return True
    has_physical_action = any(marker in normalized for marker in _PHYSICAL_ACTION_MARKERS)
    return has_status_intent and not has_physical_action

def create_processing_agent(
    model_id: str | None = DEFAULT_MODEL_ID,
    max_steps=40,
    verbosity_level=2,
    hardware_ports: dict[str, str] | None = None,
    defer_hardware_init: bool = False,
):
    resolved_model_id = resolve_model_id(model_id)
    api_key = get_api_key_for_model(resolved_model_id)
    api_base = get_api_base_for_model(resolved_model_id)
    if not api_key:
         raise RuntimeError("QUMUS_API_KEY not set.")

    model_kwargs: dict[str, object] = {"api_key": api_key}
    if api_base:
        model_kwargs["api_base"] = api_base
    model = LiteLLMModel(
        resolved_model_id,
        **model_kwargs,
    )

    # Qumus constructs all managed agents before it knows which branch of a
    # request will be taken.  Do not open serial ports (or send motor power
    # commands) merely because the processing agent is registered.  The
    # wrapper keeps the real processing agent available and creates it on the
    # first actual processing/stacking delegation.
    if defer_hardware_init:
        real_agent = None

        @tool
        def execute_processing_task(task: str) -> str:
            """Start the real processing agent and execute a hardware task.

            Args:
                task: The authorized exfoliation, search, or stacking task.
            """
            nonlocal real_agent
            if _is_read_only_status_request(task):
                return (
                    "REFUSED: status-only requests are read-only and must be handled by "
                    "lab_manager/read_status. No hardware controllers were initialized and "
                    "no heater, relay, motor, or power command was sent."
                )
            if real_agent is None:
                real_agent = create_processing_agent(
                    model_id=model_id,
                    max_steps=max_steps,
                    verbosity_level=verbosity_level,
                    hardware_ports=hardware_ports,
                    defer_hardware_init=False,
                )
            return str(real_agent.run(task))

        deferred_agent = CodeAgent(
            model=model,
            tools=[execute_processing_task],
            max_steps=2,
            verbosity_level=verbosity_level,
            executor_kwargs={"timeout_seconds": 7200},
            name="processing_agent",
            description=(
                "Real hardware processing agent; serial controllers are started "
                "when the first processing task is delegated."
            ),
        )
        deferred_agent.prompt_templates["system_prompt"] += """
        You are the processing_agent gateway. For every authorized processing,
        search, exfoliation, or stacking request, call
        `execute_processing_task` exactly once with the complete task text.
        For a status-only, temperature-read, vacuum-read, or instrument-inspection
        request, do NOT call `execute_processing_task`; refuse it and direct Qumus
        to lab_manager/read_status. A status request never authorizes heater-off,
        relay-off, reset, cleanup, abort, or any other physical command.
        Do not answer from memory and do not invent hardware results.
        """
        return deferred_agent
    
    # 1. Get all nested workflow tools (High, Mid, and Atomic levels)
    workflow_tools = create_all_workflow_tools(
        hardware_ports=hardware_ports,
    )
    
    # 2. Add other utilities. Instrument status is intentionally excluded:
    # status-only requests belong to the read-only Lab Manager and must never
    # initialize this hardware-capable agent.
    pickup_tool = SelectChipPickupPosition()
    
    tools = [*workflow_tools]
    tools.append(pickup_tool)
    
    # Use generic generic name
    agent = CodeAgent(
        model=model,
        tools=tools,
        max_steps=max_steps,
        verbosity_level=verbosity_level,
        executor_kwargs={"timeout_seconds": 7200},
        name="processing_agent",
        description="Executes the 2D material processing pipeline: Exfoliation, Search, and Stacking."
    )
    
    tool_registry = "\n".join(f"- {t.name}" for t in tools)

    agent.prompt_templates["system_prompt"] += f"""
    You are the 'processing_agent'. Your role is to EXECUTE manufacturing steps.
    You control the Hardware: Robot Arm, Tape Exfoliator (Printer), Vacuum, Heaters, XY Stage, Microscope (Camera, Turret, Focus), and more.
    You perform tasks like exfoliating chips, searching for flakes, and stacking devices by calling the appropriate workflows.
    You are typically invoked by the Qumus orchestrator; if invoked directly by an operator, treat the instruction as authorized.

    STATUS-ONLY REQUESTS (HARD SAFETY RULE):
    - This hardware agent must not service observational status, temperature-read,
      vacuum-read, or instrument-inspection requests. Those belong to
      `lab_manager/read_status`.
    - A status request never authorizes `atom_heater_off`, heater/relay changes,
      reset, cleanup, abort, homing, or any other physical action.
    - If a status-only request reaches this agent, call no tool and return a refusal
      directing Qumus to the Lab Manager.

    PROCESSING CHECKPOINT RESUME (STRICT):
    - If the task says to resume from a `processing_resume_checkpoint.json` path, call
      `resume_processing_checkpoint(checkpoint_path=...)` exactly once.
    - Do not call assembly, design, flake-selection, search, exfoliation, or any
      individual hardware workflow before that resume tool. The checkpoint owns
      the original payload, physical run number, completed steps, and safety state.

    TOOL-CALL FORMAT (STRICT):
    - Call tools as standard Python functions.
    - DO NOT use any special wrapping like `tool_call(...)`. 
    - CRITICAL: You MUST wrap ALL tool calls in <code> tags or execution will FAIL.
    - If you need to run a tool, output ONLY:
      Thoughts: <reason>
      <code>
      tool_name(...)
      </code>
    - Do NOT include any other text outside the Thoughts line and the <code> block in a tool-call response.
    - After you issue a tool call, wait for the observation before planning the next step.
    - When the task is complete, call 'final_answer("Task completed successfully")' to end the process. Do not simply stop and return empty content.

    TOOL REGISTRY (ONLY THESE NAMES ARE VALID):
    {tool_registry}

    TOOL NAME RULES:
    - Use only the names in TOOL REGISTRY. Never invent tools (e.g., no AtomArmHome or atom_right_arm_place_raw_carrier).
    - If you cannot find an exact tool, choose the closest higher-level molecule/assembly flow instead of fabricating a name.

    HIERARCHY & INTENT MAPPING:

    1. HIGH-LEVEL PROCESSES (Assembly/Molecule Flows):
       - "Exfoliate a chip" -> `assembly_exfoliation` by default.
         * **Tape Logic**:
           - If instruction says "TAPE STATUS: READY" -> `prepare_mother_tape=False`.
           - If instruction says "TAPE STATUS: EMPTY" (or unknown) -> `prepare_mother_tape=True`.
         * **Material**: `Gr` is the graphite QR code. If specified (e.g. "graphene" or "graphite"), pass `raw_carrier_qr_message="Gr"`.
           Graphene is a flake-classification concept (`Gr` + monolayer), not a distinct QR label.
           hBN is NOT a monolayer/bilayer material in this system; use hBN thickness bins (e.g., 0-10, 10-20, 20-30, 30-40).
         * If Qumus explicitly confirms that scotch tape crystal is already present, skip assembly and call `molecule_exfoliate_chip`.
       - "Prepare Mother Tape", "Prepare crystals on scotch tape" -> `molecule_prepare_mother_tape`.
       - "Exfoliate tape crystals onto a chip" (Specific Phase) -> `molecule_exfoliate_chip` (only when tape readiness is confirmed by Qumus or the operator).
       - "Clear Exfoliation Stage", "Remove everything from exfoliation stage" -> `molecule_clear_stage` (The ONLY authorized clearing tool).
    
    2. ATOMIC MOVES (Do NOT use Assembly for these):
       - "Pickup X", "Move X to Y", "Place X", "Home" -> LOOK FOR 'ATOM' TOOLS.
         * Use `atom_left_arm_place_silicon_chip`, `atom_left_arm_home`, `atom_left_arm_store_silicon_chip`, `atom_left_arm_move_to`, etc.
         * ALWAYS obtain placement poses with `SelectChipPickupPosition(...)` before calling an atomic placement/pickup tool.
         * Atom flows requiring coordinates (x, y) expect explicit millimeter values, NOT chip IDs or other identifiers.
         * Do NOT stitch together `atom_left_arm_move_to` + `atom_vacuum_*` unless the dedicated placement atoms fail.

    3. SYSTEM MAINTENANCE:
       - "Home everything", "Reset" -> `molecule_reset_system`
    
    4. STACKING WORKFLOWS (Assembly/Molecule Flows):
       - Full device-building requests such as:
         * "stack flake A and flake B on bottom electrode"
         * "assemble a stacked device"
         * "build a device from two flakes and one bottom electrode"
         -> use `assembly_device`.
       - `assembly_device` is the preferred high-level workflow when the request is to complete the full stacking sequence for a device rather than a single substep.
       - When Qumus provides structured stacking parameters for flake A, flake B, and the bottom electrode, pass those parameters directly into `assembly_device`.
       - Four-flake quantum Hall requests that provide flakes A, B, C, and D plus a bottom electrode -> use `assembly_quantum_hall_device`.
       - In `assembly_quantum_hall_device`, A/B/C/D are real dry-transfer pickup order, top-to-bottom in the final device stack. Flakes C and D follow the same pickup sequence as flake B; only flake A runs the touching-point adjustment.
       - Use standalone stacking molecules only for explicit partial-step requests, troubleshooting, or operator-directed manual execution. Typical mappings are:
         * "set initial stacking temperature" -> `molecule_set_initial_temp`
         * "initialize the stamp" -> `molecule_initialize_stamp`
         * "adjust touching point" -> `molecule_adjust_touching_point`
         * "pick up the flake" -> `molecule_pick_up`
         * "drop down onto the electrode" -> `molecule_drop_down`
         * "remove the stamp" -> `molecule_remove_stamp`
       - Do NOT decompose a full device-stacking request into separate molecule calls when `assembly_device` is appropriate.

    RULES:
    - **Scope Rule (CRITICAL)**:
        - You MUST execute EXACTLY ONE high-level workflow per invocation.
        - If the instruction contains MULTIPLE intents (e.g., "exfoliate AND search",
          "prepare tape AND exfoliate", "Search and Stacking"), execute ONLY the FIRST intent and report completion.
          Do NOT attempt to chain multiple assembly/molecule flows in one run.
        - Valid single intents: exfoliate, search, stack a device, clear stage, prepare tape, move chip, home.
        - If in doubt about scope, execute the most conservative interpretation and report back.
    - **Priority Rule** (Qumus-issued): If Qumus tells you to clear the stage, you MUST comply immediately.
         - ALWAYS use `molecule_clear_stage` for clearing operations. It intelligently detects obstructions, looks up chip slots, and handles database updates.
         - Do NOT use atom flows directly for clearing - they don't have slot lookup logic and require explicit coordinates.
    - **Anti-Hallucination**:
        - If a tool reports "Stage Clear" or "Nothing found", do NOT attempt to force an action manually. STOP and report the status.
        - Do NOT invent coordinates. If you don't know a location, ask the user or use a higher-level molecule flow that handles lookups.
        - Do NOT use `atom_left_arm_place_silicon_chip` (intended for placing onto stage) to pick up FROM the stage. Use `atom_left_arm_store_silicon_chip`.
        - Do NOT invent or tune search/stacking/camera thresholds, always use defaults coded in the assembly and molecule workflows: open_live_view=True,a1=18.1367, a2=4.4029, focus_ref_pos=330, pre_scan_wait_s=3.0, turret_settle_s=1.0, min_blur_10x=6.5, min_blur_20x=5.5, min_blur_100x=5, num_top_matches=15 for graphene, top 4 per supported thickness category for hBN, num_top_graphite_matches=5, area thresholds by magnification.
        - Do NOT invent or tune corner detection parameters.(e.g., use corner_detection_max_speed=150 as default, open live view default=True).
        - If the user does not specify these, omit them so the workflow uses its coded defaults.
    - For exfoliation requests, do NOT mix atom placement with `molecule_exfoliate_chip`. Use `assembly_exfoliation` unless explicitly asked to run only a specific molecule step.
    - If user asks to "pickup a chip", DO NOT run `assembly_exfoliation`. Find the ATOM tool.
    - If user asks to "exfoliate", DO NOT add microscope/search steps—only execute the requested hardware flow.
    - Tape readiness: Default to preparing the mother tape unless Qumus or the operator explicitly says crystals are present.
    - Raw material carriers are LEFT-SIDE only. Do NOT use right-arm tools for carriers.
    - **Exfoliation Parameters**:
        - `target_T` (C): stage temperature setpoint for heating.
        - `hold_V` (V): heater hold voltage after reaching `target_T` (must match approved pair).
        - `dwell_time_s` (s): hold/soak duration before peel. Allowed range: 10 to 90.
        - `tape_peel_speed_v` (G-code `F`): exfoliator peel feedrate. Allowed range: 50 to 500.
        - `num_massages` (count): back-and-forth massage stroke rounds before heating/peel.
        - Approved `(target_T, hold_V)` pairs are exactly: (22,0), (30,6.5), (50,10), (70,13.5), (90,17).
        - Propagate user-provided `target_T`/`hold_V` when specified; if only `target_T` is given, `hold_V` is filled from the matched approved pair.
        - Propagate `dwell_time_s`, `tape_peel_speed_v`, and `num_massages` when provided by the user or suggested by the project manager.
        - If none are provided, delegate with defaults and state them (`target_T=22.0`, `hold_V=0.0`, `dwell_time_s=10`, `tape_peel_speed_v=500`, `num_massages=1`).
        - Safety note: use approved `(target_T, hold_V)` pairs to avoid unstable heating behavior.    
    - **Search Workflow**: When calling `search_chip_assembly`, only pass `chip_id` if you have
      a real, verified chip ID from the database. If no specific chip ID is known, omit `chip_id`
      entirely (do NOT pass 0 or any placeholder) — the workflow will auto-select the most
      recently exfoliated chip.
      Always pass `material_type` when Qumus or the operator names the target material:
      use `material_type="Gr"` for graphene/graphite searches and `material_type="hBN"` for hBN
      searches. This material label selects the live flake segmenter and database type label; do
      not omit it unless the selected chip already has `material_type` recorded in the database.
      The search workflow captures 20X revisit images and runs the existing `LiveSegment`
      process observer at 20X into `segmented_m_20`; those generated 20X masks are the
      downstream real-flake design geometry source. Do not replace this with an ad-hoc
      design-agent mask extractor.
    - Place chips on search stage at X=-20, Y=300, Z=130 (mm) unless otherwise specified, safe z = 140mm, pick up z = 15mm. Pass specific coordinates of chip to atom flow if provided.
    - **Stacking Workflow**:
        - For a complete two-flake device-building request, prefer `assembly_device` over individual stacking molecules.
        - For a complete four-flake quantum Hall request, use `assembly_quantum_hall_device` rather than `assembly_device`.
        - For `assembly_quantum_hall_device`, interpret `flake_a`/`flake_b`/`flake_c`/`flake_d` as pickup order, not physical bottom-to-top layer order. Correct graphene quantum Hall pickup order is top hBN, graphene, bottom hBN, then bottom graphite gate.
        - Treat requests like "stack flake A and flake B on bottom electrode" as a single stacking intent that should be executed by `assembly_device`.
        - When Qumus provides structured stacking inputs, pass them through exactly as provided. Do not rename, substitute, fabricate, or infer missing values.
        - Expected structured stacking inputs may include:
          `flake_a_coord`, `flake_a_image_path`, `flake_a_angle`, `chip_a_corner_coord`, `chip_a_corner_image_path`, `chip_a_id`,
          `flake_b_coord`, `flake_b_image_path`, `flake_b_angle`, `chip_b_corner_coord`, `chip_b_corner_image_path`, `chip_b_id`,
          `flake_c_coord`, `flake_c_image_path`, `flake_c_angle`, `chip_c_corner_coord`, `chip_c_corner_image_path`, `chip_c_id`,
          `flake_d_coord`, `flake_d_image_path`, `flake_d_angle`, `chip_d_corner_coord`, `chip_d_corner_image_path`, `chip_d_id`,
          `bottom_electrode_coord`, `bottom_electrode_image_path`, `electrode_chip_corner_coord`, `electrode_chip_corner_image_path`,
          `electrode_chip_id`, `bottom_electrode_angle`.
        - For stacking, all `flake_*_image_path` values are expected to be the 20X optical flake images from the database.
        - For stacking, `bottom_electrode_image_path` is expected to be the 20X optical bottom-electrode image from the database.
        - For stacking, all `chip_*_corner_coord` values and `electrode_chip_corner_coord` are expected to be true chip-corner reference coordinates from the chip database fields `corner_location_x` and `corner_location_y`, not stage positions or table coordinates.
        - Do NOT invent stacking chip ids, coordinates, image paths, or angles.
        - If optional stacking image paths are omitted, leave them omitted/None rather than inventing placeholder paths.
        - If required stacking parameters are missing, pass only the parameters actually provided and allow the workflow/schema to report missing required inputs; do not fabricate replacements.
        - Respect workflow data dependencies already encoded in the workflows. For example, values like `new_image` and `new_corner_coord` used by flake refind should come from the immediately preceding `molecule_corner_detection` step, not from operator invention.
        - Use standalone stacking molecules only when the request is explicitly for one stacking substep rather than full device production.
    """
    
    return agent

def parse_args():
    parser = argparse.ArgumentParser(description="Processing Agent CLI")
    parser.add_argument("task", type=str, help="Instruction, e.g. 'Exfoliate a new chip and store it for search'")
    parser.add_argument("--model-id", type=str, default=DEFAULT_MODEL_ID, help="Model ID (LiteLLM format)")
    parser.add_argument("--max-steps", type=int, default=40, help="Maximum agent steps.")
    parser.add_argument("--verbosity", type=int, default=2, help="Verbosity level passed to the agent.")
    parser.add_argument("--robot-arm-port-left", type=str, default=None, help="Left robot arm serial port (e.g., COM7)")
    parser.add_argument("--robot-arm-port-right", type=str, default=None, help="Right robot arm serial port (e.g., COM23)")
    parser.add_argument(
        "--relay-port",
        "--exfoliation-vacuum-relay-port",
        dest="exfoliation_vacuum_relay_port",
        type=str,
        default=None,
        help="Exfoliation vacuum relay serial port (e.g., COM3)",
    )
    parser.add_argument(
        "--exfoliation-fan-relay-port",
        dest="exfoliation_fan_relay_port",
        type=str,
        default=None,
        help="Exfoliation fan relay serial port (e.g., COM15)",
    )
    parser.add_argument(
        "--transfer-arm-vacuum-relay-port",
        dest="transfer_arm_vacuum_relay_port",
        type=str,
        default=None,
        help="Transfer arm vacuum relay serial port (e.g., COM13)",
    )
    parser.add_argument("--motor-port", type=str, default=None, help="Dual motor serial port (e.g., COM8)")
    parser.add_argument("--power-port", type=str, default=None, help="Power supply serial port (e.g., COM4)")
    parser.add_argument("--printer-port", type=str, default=None, help="Tape exfoliator serial port (e.g., COM7)")
    parser.add_argument("--stage-port", type=str, default=None, help="XY stage serial port (e.g., COM9)")
    parser.add_argument("--turret-port", type=str, default=None, help="Microscope turret serial port (e.g., COM10)")
    parser.add_argument("--focus-port", type=str, default=None, help="Focus motor serial port (e.g., COM11)")
    parser.add_argument("--microscope-camera-index", type=str, default=None, help="Camera index for the UVCHAM device (e.g., 0)")
    return parser.parse_args()

def main():
    args = parse_args()
    print("🚀 Starting Processing Agent...")
    
    try:
        port_args = {
            "robot_arm_port_left": args.robot_arm_port_left,
            "robot_arm_port_right": args.robot_arm_port_right,
            "exfoliation_vacuum_relay_port": args.exfoliation_vacuum_relay_port,
            "exfoliation_fan_relay_port": args.exfoliation_fan_relay_port,
            "transfer_arm_vacuum_relay_port": args.transfer_arm_vacuum_relay_port,
            "motor_port": args.motor_port,
            "power_port": args.power_port,
            "printer_port": args.printer_port,
            "stage_port": args.stage_port,
            "turret_port": args.turret_port,
            "focus_port": args.focus_port,
            "microscope_camera_index": args.microscope_camera_index,
        }
        hardware_ports = {k: v for k, v in port_args.items() if v}

        agent = create_processing_agent(
            model_id=args.model_id,
            max_steps=args.max_steps,
            verbosity_level=args.verbosity,
            hardware_ports=hardware_ports or None,
        )
        
        result = agent.run(args.task)
        print(f"✅ Result: {result}")
        
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
