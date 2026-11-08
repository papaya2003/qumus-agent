"""
Design Tools for Quantum Device Optimization
============================================
This module provides tool functions for the Design Agent.
All tools use the @tool decorator from smolagents framework.

Global State:
    _agent_state: Dictionary storing workflow state across tool calls
"""

import os
import cv2
import json
import numpy as np
from typing import Dict, List
from smolagents import tool
from common.llm_config import DEFAULT_MODEL_ID
from common.logic.stacking_run_dirs import get_stacking_run_dirs
from agents.design_agent.tools.layout_optimizer import (
    optimize_material_design,
    down_sample,
    preprocess_electrodes,
    separate_electrode_masks,
    process_electrode_mask,
    generate_relation_matrix,
    visualize_violations,
    build_binary_map,
    score_configuration,
    find_electrode_midpoint,
    preprocess_image
)
import matplotlib.cm as cm


# ============================================================================
# Global State Management
# ============================================================================

# Global state shared across all tools
_agent_state = {}


def _default_output_dir() -> str:
    return get_stacking_run_dirs(create_new=False, sample_label="design")["sample_dir"]


def _resolve_output_dir(preferred: str | None = None) -> str:
    """
    Resolve the active design output directory.

    Qumus-driven design should always stay inside the current stacking run's
    `run_N/design` folder. If the agent passes a broad path such as
    `images/stacking` or loses state mid-run, redirect back to the current run's
    design folder instead of writing elsewhere.
    """
    default_dir = os.path.abspath(_default_output_dir())
    current_run_dir = os.path.abspath(get_stacking_run_dirs(create_new=False)["run_dir"])

    candidate = preferred or _agent_state.get("output_dir")
    if candidate:
        candidate_abs = os.path.abspath(str(candidate))
        if candidate_abs.startswith(current_run_dir):
            resolved = candidate_abs
        else:
            resolved = default_dir
    else:
        resolved = default_dir

    os.makedirs(resolved, exist_ok=True)
    _agent_state["output_dir"] = resolved
    return resolved


def _resolve_output_file(output_filename: str | None, default_basename: str) -> str:
    if not output_filename:
        return os.path.join(_resolve_output_dir(), default_basename)

    candidate_abs = os.path.abspath(str(output_filename))
    current_run_dir = os.path.abspath(get_stacking_run_dirs(create_new=False)["run_dir"])
    if candidate_abs.startswith(current_run_dir):
        os.makedirs(os.path.dirname(candidate_abs), exist_ok=True)
        return candidate_abs

    redirected = os.path.join(_resolve_output_dir(), os.path.basename(candidate_abs) or default_basename)
    os.makedirs(os.path.dirname(redirected), exist_ok=True)
    return redirected


def get_agent_state() -> Dict:
    """Get current agent state."""
    return _agent_state


def reset_agent_state():
    """Reset agent state to empty."""
    global _agent_state
    _agent_state = {}


def set_agent_state(state: Dict):
    """Set agent state from external dictionary."""
    global _agent_state
    _agent_state = state


# ============================================================================
# Tool Definitions
# ============================================================================

@tool
def load_material_masks(material_names: str, material_paths: str) -> str:
    """
    Load material masks from image files.
    
    Args:
        material_names: Comma-separated material names (e.g., "WTe2,hBN,graphite")
        material_paths: Comma-separated file paths (e.g., "path1.png,path2.png,path3.png")
    
    Returns:
        Status message with loaded materials count
    """
    names = [n.strip() for n in material_names.split(',')]
    paths = [p.strip() for p in material_paths.split(',')]
    
    if len(names) != len(paths):
        return f"Error: Number of names ({len(names)}) doesn't match number of paths ({len(paths)})"
    
    masks = {}
    for name, path in zip(names, paths):
        mask = cv2.imread(path, 0)
        if mask is None:
            return f"Error: Could not load {name} from {path}"
            
        # Auto-detect raw images (more than 2 values) and preprocess
        unique_vals = np.unique(mask)
        if len(unique_vals) > 2:
            print(f"[Info] {name}: Detected raw image with {len(unique_vals)} unique values. Applying preprocessing...")
            mask = preprocess_image(mask)

        # Material masks: keep only the largest connected component.
        # (Electrode processing is handled by load_and_separate_electrodes and is unchanged.)
        material_bin = (mask > 0).astype(np.uint8)
        if np.count_nonzero(material_bin) > 0:
            num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(material_bin, connectivity=8)
            if num_labels > 1:
                largest_idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                material_bin = (labels == largest_idx).astype(np.uint8)
        mask = (material_bin * 255).astype(np.uint8)

        masks[name] = mask

        # Save cleaned mask to output directory for UI/Server to pick up
        output_dir = _resolve_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        save_path = f"{output_dir}/mask_{name}.png"
        cv2.imwrite(save_path, mask)
        print(f"✓ Saved processed mask to {save_path}")
    
    # Store in global state
    global _agent_state
    _agent_state['material_masks'] = masks
    _agent_state['material_names'] = names
    _agent_state['material_paths'] = {name: path for name, path in zip(names, paths)}
    
    return f"✓ Loaded {len(names)} material masks and saved processed versions to {output_dir}"


@tool
def load_and_separate_electrodes(
    electrode_path: str,
    output_dir: str = "",
    distance_ratio: float = 1/3,
    expand_pixels: int = 10,
) -> str:
    """
    Load electrode mask and separate into individual electrodes.
    
    Args:
        electrode_path: Path to electrode mask image
        output_dir: Directory to save separated electrode masks
        distance_ratio: Threshold ratio for classifying inner/outer electrodes (default: 1/3)
        expand_pixels: Expansion size in pixels for contact electrode region (default: 10)
    
    Returns:
        Status message with electrode count information
    """
    output_dir = _resolve_output_dir(output_dir)

    electrode_mask_raw = cv2.imread(electrode_path, 0)
    if electrode_mask_raw is None:
        return f"Error: Could not load electrode mask from {electrode_path}"
    
    # Create output directory if it doesn't exist
    if not os.path.exists(output_dir):
        try:
            os.makedirs(output_dir, exist_ok=True)
            print(f"[Info] Created output directory: {output_dir}")
        except Exception as e:
            return f"Error: Could not create output directory {output_dir}: {e}"

    # Auto-detection: Check if image is raw (noisy/grayscale) or binary mask
    # A clean binary mask should only have 0 and 255 (or very few values)
    unique_vals = np.unique(electrode_mask_raw)
    if len(unique_vals) > 2:
        print(f"[Info] Detected raw image with {len(unique_vals)} unique values. Applying preprocessing...")
        electrode_mask_raw = preprocess_electrodes(electrode_mask_raw)
        
        # Save the generated mask for verification/reference
        mask_save_path = f"{output_dir}/mask_electrodes.png"
        cv2.imwrite(mask_save_path, electrode_mask_raw)
        print(f"✓ Saved preprocessed mask to {mask_save_path}")

    # Create contact electrode mask first so it can be used to define E_others
    contact_electrode_mask, rotated_rect = process_electrode_mask(
        electrode_mask_raw,
        output_prefix=f"{output_dir}/mask_contact_electrode",
        expand_pixels=expand_pixels,
        distance_ratio=distance_ratio
    )

    # Separate electrodes: E_others = all components intersecting contact_electrode
    outer_masks, inner_combined_mask, outer_idx, inner_idx = separate_electrode_masks(
        electrode_mask_raw,
        output_dir=output_dir,
        distance_ratio=distance_ratio,
        contact_electrode_mask=contact_electrode_mask
    )
    
    # Load all electrode masks
    masks = {
        "contact_electrode": contact_electrode_mask,
        "E_others": inner_combined_mask,
    }
    
    # Add individual outer electrodes
    for idx in range(len(outer_idx)):
        electrode_name = f"E{idx+1}"
        electrode_file = f"{output_dir}/mask_E{idx+1}.png"
        masks[electrode_name] = cv2.imread(electrode_file, 0)
    
    # Update global state
    global _agent_state
    _agent_state['electrode_masks'] = masks
    _agent_state['electrode_names'] = [f"E{i+1}" for i in range(len(outer_idx))] + ["contact_electrode", "E_others"]
    _agent_state['num_outer_electrodes'] = len(outer_idx)
    _agent_state['no_electrode_design'] = False
    
    return f"✓ Separated {len(outer_idx)} outer electrodes and {len(inner_idx)} inner electrodes in {output_dir}"


@tool
def configure_no_electrode_design() -> str:
    """
    Configure the current design run for an electrode-free layout.

    Use this after loading material masks when the device design has no bottom
    electrode or electrode pattern. All material-material layout constraints are
    kept, but electrode constraints are omitted.

    Returns:
        Status message confirming electrode-free mode
    """
    global _agent_state
    _agent_state['electrode_masks'] = {}
    _agent_state['electrode_names'] = []
    _agent_state['num_outer_electrodes'] = 0
    _agent_state['no_electrode_design'] = True
    return "✓ Configured electrode-free design mode"


@tool
def generate_design_constraints(task: str, requirements: str) -> str:
    """
    Generate geometric constraint rules from natural language description.
    
    Args:
        task: Brief description of the device (e.g., "Build a Dual-Gate Hall Bar Device")
        requirements: Detailed requirements for material relationships
    
    Returns:
        Status message with generated constraints summary
    """
    global _agent_state
    if 'material_names' not in _agent_state:
        return "Error: Please load material masks first using load_material_masks"
    
    if 'num_outer_electrodes' not in _agent_state:
        return (
            "Error: Please either load and separate electrodes using "
            "load_and_separate_electrodes or call configure_no_electrode_design"
        )
    
    materials = _agent_state['material_names']
    num_electrodes = _agent_state['num_outer_electrodes']
    model_id = _agent_state.get('model_id') or DEFAULT_MODEL_ID
    
    print(f"\n🤖 Generating constraints for: {task}")
    print(f"Materials: {materials}")
    include_electrodes = not _agent_state.get('no_electrode_design', False)
    if include_electrodes:
        print(f"Outer electrodes: {num_electrodes}")
    else:
        print("Electrode-free design: no electrode constraints will be generated")

    output_rules = generate_relation_matrix(
        materials,
        num_electrodes,
        task,
        requirements,
        model_id=model_id,
        include_electrodes=include_electrodes,
    )
    
    _agent_state['output_rules'] = output_rules
    _agent_state['task'] = task
    _agent_state['requirements'] = requirements
    
    # Create summary
    rule_count = sum(len(rules) for rules in output_rules.values())
    summary = f"✓ Generated {rule_count} constraint rules for {len(materials)} materials"
    
    return summary


@tool
def optimize_design(max_iterations: int = 100, debug: bool = True) -> str:
    """
    Optimize material placement to satisfy design constraints.
    
    Args:
        max_iterations: Maximum optimization iterations (default: 100)
        debug: Whether to save debug visualizations (default: True)
    
    Returns:
        Optimization results summary
    """
    global _agent_state
    if not _agent_state:
        return "Error: Agent state not initialized"
    
    required_keys = ['material_masks', 'material_names', 'output_rules']
    for key in required_keys:
        if key not in _agent_state:
            return f"Error: Missing required data '{key}'. Please run previous steps first."
    
    # Combine all masks
    all_masks = {}
    all_masks.update(_agent_state['material_masks'])
    all_masks.update(_agent_state.get('electrode_masks', {}))
    
    materials = _agent_state['material_names']
    electrodes = _agent_state.get('electrode_names', [])
    output_rules = _agent_state['output_rules']
    
    print("\n🚀 Starting optimization...")
    print(f"Materials to optimize: {materials}")
    print(f"Electrodes: {electrodes}")
    
    # Calculate aspect ratio conserving downsample size
    # Use the first available mask to determine input aspect ratio
    first_mask = next(iter(all_masks.values()))
    h, w = first_mask.shape
    target_width = 480  # Higher resolution for better quality
    target_height = int(target_width * (h / w))
    downsample_size = (target_height, target_width)
    print(f"Input size: {w}x{h}, Target size: {target_width}x{target_height}")
    
    # Target output directory for debug images
    output_dir = _resolve_output_dir()
    os.makedirs(output_dir, exist_ok=True)

    # Run optimization
    optimized_masks, transformations, results = optimize_material_design(
        all_masks, materials, electrodes, output_rules, 
        downsample_size=downsample_size,
        max_iter=max_iterations,
        debug=debug,
        output_dir=output_dir
    )
    
    # Store results
    _agent_state['optimized_masks'] = optimized_masks
    _agent_state['transformations'] = transformations
    _agent_state['results'] = results
    
    # Generate summary
    total_score = results['total_score']
    satisfied = results['satisfied_count']
    violations = len(results['violations'])
    improvement = results.get('improvement', 0)
    
    summary = f"""✓ Optimization Complete!
    
Final Score: {total_score:.2f}
Satisfied Constraints: {satisfied}
Remaining Violations: {violations}
Improvement: {improvement:.2f} ({improvement/results['initial_objective']*100:.1f}%)

Transformations applied:"""
    
    for material in materials:
        trans = transformations[material]
        summary += f"\n  - {material}: rotation={trans['total_rotation']:.1f}°, translation=({trans['total_translation'][0]:.0f}, {trans['total_translation'][1]:.0f})"
    
    return summary


@tool
def visualize_result(output_filename: str = "") -> str:
    """
    Visualize the optimized design with all materials and electrodes.
    
    Args:
        output_filename: Output image filename. If empty, saves into the current design output directory.
    
    Returns:
        Status message with saved filename
    """
    global _agent_state
    if 'optimized_masks' not in _agent_state:
        return "Error: No optimized design available. Please run optimize_design first."
    
    optimized_masks = _agent_state['optimized_masks']
    materials = _agent_state['material_names']
    electrodes = _agent_state.get('electrode_names', [])
    output_filename = _resolve_output_file(output_filename, "design_result.png")
    
    # Generate colors
    all_layers = materials + electrodes
    n_layers = len(all_layers)
    
    if n_layers <= 10:
        cmap = cm.get_cmap('tab10')
        colors_rgb = [cmap(i)[:3] for i in range(n_layers)]
    else:
        cmap = cm.get_cmap('hsv')
        colors_rgb = [cmap(i / n_layers)[:3] for i in range(n_layers)]
    
    colors_bgr = [(int(b*255), int(g*255), int(r*255)) for r, g, b in colors_rgb]
    
    # Create visualization
    H, W = next(iter(optimized_masks.values())).shape
    merged_vis = np.zeros((H, W, 3), dtype=np.float32)
    
    # Add material layers
    for i, material in enumerate(materials):
        mask = optimized_masks[material]
        color = np.array(colors_bgr[i], dtype=np.float32)
        layer = np.zeros_like(merged_vis)
        layer[mask > 0] = color
        merged_vis = cv2.addWeighted(merged_vis, 1.0, layer, 0.5, 0)
    
    # Add electrode layers
    for i, electrode in enumerate(electrodes):
        mask = optimized_masks[electrode]
        color = np.array(colors_bgr[len(materials) + i], dtype=np.float32)
        layer = np.zeros_like(merged_vis)
        layer[mask > 0] = color
        merged_vis = cv2.addWeighted(merged_vis, 1.0, layer, 0.6, 0)
    
    merged_vis = np.clip(merged_vis, 0, 255).astype(np.uint8)
    cv2.imwrite(output_filename, merged_vis)
    
    return f"✓ Saved visualization to: {output_filename}"


@tool
def get_violation_details() -> str:
    """
    Get detailed information about remaining constraint violations.
    
    Returns:
        Formatted violation details
    """
    global _agent_state
    if 'results' not in _agent_state:
        return "Error: No optimization results available. Please run optimize_design first."
    
    violations = _agent_state['results']['violations']
    
    if len(violations) == 0:
        return "✓ No violations! All constraints are satisfied."
    
    details = f"Found {len(violations)} violations:\n\n"
    
    for i, v in enumerate(violations, 1):
        rule_name = v['rule']
        relation = v['relation']
        degree = v['degree']
        count = v.get('count', 0)
        completion = v.get('completion', 0)
        
        parts = rule_name.split('->')
        if len(parts) == 2:
            obj_a, obj_b = parts[0], parts[1]
            
            # Describe violation
            if relation == "contact" and degree == "none":
                desc = f"Should NOT touch, but has {count} pixels of contact"
            elif relation == "contact" and degree == "complete":
                desc = f"Should FULLY cover, but only {completion*100:.1f}% covered (needs ≥70%)"
            elif relation == "contact" and degree == "not complete":
                desc = f"Should PARTIALLY touch, but only has {count} pixels (needs ≥40)"
            elif relation == "overlap" and degree == "max":
                desc = f"Should have 70-90% overlap, but actual is {completion*100:.1f}%"
            elif relation == "overlap" and degree == "min":
                desc = f"Should have 10-30% overlap, but actual is {completion*100:.1f}%"
            else:
                desc = f"{relation} {degree}: {count} pixels ({completion*100:.1f}%)"
            
            details += f"{i}. {obj_a} → {obj_b}\n   {desc}\n\n"
    
    return details


@tool
def export_design_parameters(output_filename: str = "") -> str:
    """
    Export design parameters and transformations to JSON file.
    
    Args:
        output_filename: Output JSON filename. If empty, saves into the current design output directory.
    
    Returns:
        Status message with saved filename
    """
    global _agent_state
    if 'transformations' not in _agent_state:
        return "Error: No optimization results available. Please run optimize_design first."
    output_filename = _resolve_output_file(output_filename, "design_parameters.json")
    
    materials = _agent_state['material_names']
    transformations = _agent_state['transformations']
    results = _agent_state['results']
    
    # Prepare export data
    export_data = {
        'task': _agent_state.get('task', ''),
        'requirements': _agent_state.get('requirements', ''),
        'materials': materials,
        'electrodes': _agent_state.get('electrode_names', []),
        'output_rules': _agent_state['output_rules'],
        'transformations': {},
        'results': {
            'total_score': float(results['total_score']),
            'satisfied_count': int(results['satisfied_count']),
            'num_violations': len(results['violations']),
            'improvement': float(results.get('improvement', 0))
        }
    }
    
    # Convert transformations to serializable format
    for material in materials:
        trans = transformations[material]
        export_data['transformations'][material] = {
            'original_centroid': [int(trans['original_centroid'][0]), int(trans['original_centroid'][1])],
            'total_rotation': float(trans['total_rotation']),
            'total_translation': [float(trans['total_translation'][0]), float(trans['total_translation'][1])]
        }
    
    with open(output_filename, 'w') as f:
        json.dump(export_data, f, indent=2)
    
    return f"✓ Exported design parameters to: {output_filename}"


@tool
def export_qumus_handoff() -> str:
    """
    Export a compact handoff payload for qumus agent downstream steps.

    Output schema:
    {
      "selected_hbn_path": str,
      "flake_a": {
        "material_name": str,
        "material_path": str,
        "dx": float,
        "dy": float,
        "angle": float
      },
      "flake_b": {
        "material_name": str,
        "material_path": str,
        "dx": float,
        "dy": float,
        "angle": float
      },
      "materials": [
        {
          "material_name": str,
          "material_path": str,
          "original_central_position": [x, y],
          "central_shift": [dx, dy]
        }
      ]
    }
    """
    global _agent_state
    required_keys = ['material_names', 'transformations']
    for key in required_keys:
        if key not in _agent_state:
            return f"Error: Missing required data '{key}'. Please run optimize_design first."

    material_names = _agent_state['material_names']
    material_paths = _agent_state.get('material_paths', {})
    transformations = _agent_state['transformations']

    payload = {"materials": []}
    for material in material_names:
        trans = transformations[material]
        item = {
            "material_name": material,
            "material_path": material_paths.get(material, ""),
            "original_central_position": [
                int(trans['original_centroid'][0]),
                int(trans['original_centroid'][1]),
            ],
            "central_shift": [
                float(trans['total_translation'][0]),
                float(trans['total_translation'][1]),
            ],
        }
        payload["materials"].append(item)

    by_name = {item["material_name"].lower(): item for item in payload["materials"]}
    if "hbn" in by_name:
        payload["selected_hbn_path"] = by_name["hbn"]["material_path"]
        payload["flake_a"] = {
            "material_name": by_name["hbn"]["material_name"],
            "material_path": by_name["hbn"]["material_path"],
            "dx": by_name["hbn"]["central_shift"][0],
            "dy": by_name["hbn"]["central_shift"][1],
            "angle": float(transformations[by_name["hbn"]["material_name"]]["total_rotation"]),
        }
    if "graphene" in by_name:
        payload["flake_b"] = {
            "material_name": by_name["graphene"]["material_name"],
            "material_path": by_name["graphene"]["material_path"],
            "dx": by_name["graphene"]["central_shift"][0],
            "dy": by_name["graphene"]["central_shift"][1],
            "angle": float(transformations[by_name["graphene"]["material_name"]]["total_rotation"]),
        }

    return json.dumps(payload, ensure_ascii=False, indent=2)


# ============================================================================
# Tool List for Agent
# ============================================================================

def get_all_tools() -> List:
    """
    Get list of all available tools for the agent.
    
    Returns:
        List of tool functions
    """
    # Import higher-level workflow and KLayout tools lazily to avoid circular
    # imports: klayout_claw_tool needs access to this module's agent state.
    from agents.design_agent.tools.design_workflows import (
        run_claude_sdk_klayout_design_tool,
        select_device_design_workflow,
    )
    from agents.design_agent.tools.klayout_claw_tool import (
        capture_klayout_screenshot,
        check_klayout_mcp,
        create_klayout_layout,
        execute_klayout_script,
        export_optimized_masks_to_klayout,
        run_klayout_mcp_tool,
        save_klayout_layout,
    )

    return [
        select_device_design_workflow,
        load_material_masks,
        load_and_separate_electrodes,
        configure_no_electrode_design,
        generate_design_constraints,
        optimize_design,
        visualize_result,
        get_violation_details,
        export_design_parameters,
        export_qumus_handoff,
        run_claude_sdk_klayout_design_tool,
        check_klayout_mcp,
        create_klayout_layout,
        execute_klayout_script,
        run_klayout_mcp_tool,
        save_klayout_layout,
        capture_klayout_screenshot,
        export_optimized_masks_to_klayout,
    ]
