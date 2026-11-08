"""
General device-workflow helpers for the design agent.

The design agent should not force device requests into built-in material
recipes. Project-manager guidance and human edits define the stack, fabrication
order, and constraints; this module turns that guidance into a Claude Agent SDK /
KLayout execution package.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
import asyncio
import inspect
import dataclasses
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Dict, Iterable, List

from smolagents import tool

from common.services.human_gate_service import HumanGateService


AI_DESIGNED_WORKFLOW = "ai_designed_workflow"
LEGACY_MASK_OPTIMIZER = "legacy_mask_optimizer"
PROJECT_ALIGNMENT_MARKER_GDS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "output", "alignmarker2.GDS")
)
PROJECT_GRAPHENE_ELECTRODE_TEMPLATE_GDS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "output", "new_electrodes_500um_samplearea.GDS")
)
PROJECT_GRAPHENE_TEMPLATE_PROFILE_JSON = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "skills",
        "graphene-qh-device",
        "references",
        "graphene-qh-template-profile.json",
    )
)
PROJECT_ROUTING_SKILL_MD = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "skills", "qumus-project-routing", "SKILL.md")
)
PROJECT_GRAPHENE_QH_SKILL_MD = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "skills", "graphene-qh-device", "SKILL.md")
)
PROJECT_GRAPHENE_QH_DEFAULT_IDEAL_JSON = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "skills",
        "graphene-qh-device",
        "references",
        "default-ideal-design.json",
    )
)
PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "skills", "2d-dual-gate-transistor", "SKILL.md")
)
PROJECT_RESE2_BOTTOM_CONTACT_PROFILE_JSON = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "skills",
        "2d-dual-gate-transistor",
        "references",
        "rese2-bottom-contact-profile.json",
    )
)
PROJECT_BOND_PAD_SIZE_UM = 250.0
DEFAULT_CLAUDE_SDK_MODEL = os.getenv("QUMUS_CLAUDE_SDK_MODEL", "claude-opus-5").strip() or "claude-opus-5"


def _default_claude_sdk_timeout() -> int:
    """Wall-clock cap (seconds) for one Claude Agent SDK KLayout design run.

    Full KLayout device design can legitimately spend a long time in Claude Code
    planning and verification. Keep one hour as the default and as a floor so a
    caller cannot accidentally cut off a run before useful artifacts are written.
    """
    try:
        return max(3600, int(os.getenv("QUMUS_CLAUDE_SDK_TIMEOUT", "3600")))
    except ValueError:
        return 3600


DEFAULT_CLAUDE_SDK_TIMEOUT = _default_claude_sdk_timeout()

_GUIDANCE_FIELD_KEYS = (
    "device_name",
    "material_roles",
    "stack_roles",
    "workflow_plan",
    "device_geometry",
    "contact_geometry",
    "gate_geometry",
    "cleanroom_sequence",
    "fabrication_plan",
    "physics_constraints",
    "material_constraints",
    "evidence",
    "lab_history_evidence",
    "literature_evidence",
    "capability_annotations",
)


def _has_claude_sdk_auth() -> bool:
    return bool(
        os.getenv("ANTHROPIC_API_KEY")
        or (os.getenv("ANTHROPIC_BASE_URL") and os.getenv("ANTHROPIC_AUTH_TOKEN"))
    )


def _claude_sdk_auth_mode() -> str:
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic_api_key"
    if os.getenv("ANTHROPIC_BASE_URL") and os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic_gateway_auth_token"
    return "missing"


def _resolve_claude_model_for_gateway(model: str) -> str:
    resolved = (model or DEFAULT_CLAUDE_SDK_MODEL).strip() or DEFAULT_CLAUDE_SDK_MODEL
    base_url = (os.getenv("ANTHROPIC_BASE_URL") or "").lower()
    if "bytefuture" in base_url and resolved.startswith("claude-"):
        return f"anthropic/{resolved}"
    return resolved


LEGACY_WORKFLOW_DEFINITION: Dict[str, object] = {
    "id": LEGACY_MASK_OPTIMIZER,
    "name": "Legacy mask optimizer",
    "material_family": ["graphene", "hBN"],
    "design_order": [
        "Load provided material masks and any explicitly provided electrode mask.",
        "Generate geometric constraints from the user requirements.",
        "Run the image-mask optimizer.",
        "Export visualization, parameters, and Qumus handoff JSON.",
    ],
    "quick_design_mode": "mask_optimizer",
    "klayout_role": "optional_review_or_export",
    "required_inputs": [
        "task",
        "material mask paths",
        "electrode mask path only when the user supplied one",
        "pixel_size_um_per_px when KLayout export is requested",
    ],
    "handoff_outputs": [
        "design_parameters.json",
        "design_result.png",
        "Qumus handoff JSON",
    ],
    "cleanroom_minimization": (
        "No clean-room step is implied by the optimizer itself. If later human "
        "fabrication is needed, batch those steps according to approved guidance."
    ),
}

WORKFLOW_DEFINITIONS: Dict[str, Dict[str, object]] = {
    LEGACY_MASK_OPTIMIZER: LEGACY_WORKFLOW_DEFINITION,
}


def _slugify_workflow_id(raw: str | None) -> str:
    compact = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    compact = re.sub(r"[^a-z0-9_]+", "_", compact).strip("_")
    return compact or AI_DESIGNED_WORKFLOW


def normalize_design_scenario(raw_scenario: str | None, task: str = "") -> str:
    """Return a workflow label without inferring a fixed recipe from material text."""
    compact = _slugify_workflow_id(raw_scenario)
    if compact in {"legacy", "legacy_graphene", "legacy_graphene_electrode_backed", "electrode_backed"}:
        return LEGACY_MASK_OPTIMIZER
    return compact


def _default_output_dir() -> str:
    from common.logic.stacking_run_dirs import get_stacking_run_dirs

    return get_stacking_run_dirs(create_new=False, sample_label="design")["sample_dir"]


def _resolve_symbolic_run_handoff(handoff_dir: str, design_stage: str) -> str:
    """Resolve a model-emitted ``run_new`` path to one persistent numeric run."""
    requested = Path(handoff_dir).resolve()
    parts = list(requested.parts)
    try:
        symbolic_index = next(i for i, part in enumerate(parts) if part.lower() == "run_new")
    except StopIteration:
        return str(requested)

    stacking_root = Path(*parts[:symbolic_index])
    suffix = parts[symbolic_index + 1 :]
    alias_path = stacking_root / ".run_new_alias.json"

    target_run: Path | None = None
    if design_stage == "real_flake_adaptation" and alias_path.is_file():
        try:
            alias = json.loads(alias_path.read_text(encoding="utf-8"))
            candidate = Path(str(alias.get("run_dir") or "")).resolve()
            if candidate.parent == stacking_root and re.fullmatch(r"run_\d+", candidate.name):
                target_run = candidate
        except (OSError, ValueError):
            target_run = None

    if target_run is None:
        from common.logic.stacking_run_dirs import get_stacking_run_dirs

        target_run = Path(
            get_stacking_run_dirs(create_new=True)["run_dir"]
        ).resolve()
        alias_path.write_text(
            json.dumps({"run_dir": str(target_run)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return str(target_run.joinpath(*suffix))


def _replace_symbolic_run_references(value: object, numeric_run_name: str) -> object:
    """Rewrite embedded run_new paths after the handoff directory is resolved."""
    if isinstance(value, str):
        return re.sub(
            r"(?i)(?P<separator>[\\/])run_new(?=(?:[\\/]|$))",
            lambda match: f"{match.group('separator')}{numeric_run_name}",
            value,
        )
    if isinstance(value, dict):
        return {
            key: _replace_symbolic_run_references(item, numeric_run_name)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_symbolic_run_references(item, numeric_run_name) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_symbolic_run_references(item, numeric_run_name) for item in value)
    return value


def _parse_extra_context(extra_context_json: str) -> Dict[str, object]:
    if not extra_context_json.strip():
        return {}
    try:
        parsed = json.loads(extra_context_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"extra_context_json must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("extra_context_json must decode to a JSON object")
    return parsed


def _guidance_from_context(extra_context: Dict[str, object]) -> Dict[str, object]:
    guidance = extra_context.get("approved_device_guidance") or extra_context.get("device_guidance")
    if isinstance(guidance, dict):
        return guidance
    flattened = {key: extra_context[key] for key in _GUIDANCE_FIELD_KEYS if key in extra_context}
    return flattened


def _markdown_section(text: str, heading: str) -> str:
    """Return a markdown section body without consuming the next peer heading."""
    match = re.search(
        rf"(?ims)^##+\s*{re.escape(heading)}\s*:?\s*$\s*(.*?)(?=^##+\s|\Z)",
        text or "",
    )
    return match.group(1).strip() if match else ""


def _embedded_real_flake_context(task: str) -> Dict[str, object]:
    """Recover structured context when an agent put the handoff JSON in task prose."""
    # Concrete signal only: the recovery below needs literal ``- mask_20x_path:``
    # lines, so gate on that marker instead of scanning the prose for the phrase
    # "real-flake" (which an ideal-stage task legitimately mentions in passing).
    if not task or "mask_20x_path" not in task:
        return {}

    masks: Dict[str, List[str]] = {}
    candidates: Dict[str, List[Dict[str, object]]] = {}
    role_matches = list(re.finditer(r"(?im)^###\s+Role\s+\d+\s*:\s*([^\r\n(]+)", task))
    for index, match in enumerate(role_matches):
        role = match.group(1).strip()
        end = role_matches[index + 1].start() if index + 1 < len(role_matches) else len(task)
        block = task[match.end():end]
        mask_match = re.search(r"(?im)^-\s*mask_20x_path\s*:\s*(.+?)\s*$", block)
        if not mask_match:
            continue
        mask_path = mask_match.group(1).strip().strip("`\"'")
        record: Dict[str, object] = {"role": role, "mask_20x_path": mask_path, "mask_path": mask_path}
        for field in ("flake_id", "chip_id"):
            value_match = re.search(rf"(?im)^-\s*{field}\s*:\s*(\d+)", block)
            if value_match:
                record[field] = int(value_match.group(1))
        for field in ("location_x", "location_y", "area_um2"):
            value_match = re.search(rf"(?im)^-\s*{field}\s*:\s*(-?[\d.]+)", block)
            if value_match:
                record[field] = float(value_match.group(1))
        masks.setdefault(role, []).append(mask_path)
        candidates.setdefault(role, []).append(record)

    if not masks:
        return {}

    recovered: Dict[str, object] = {
        "design_stage": "real_flake_adaptation",
        "candidate_mask_paths_by_role": masks,
        "candidate_flakes_by_role": candidates,
    }
    scale_match = re.search(r"(?i)(?:scale|pixel_size_um_per_px)\s*[=:]?\s*([\d.]+)\s*(?:um|µm)\s*/\s*pixel", task)
    if scale_match:
        recovered["pixel_size_um_per_px"] = float(scale_match.group(1))

    approved = _markdown_section(task, "APPROVED DEVICE GUIDANCE")
    if approved:
        device_match = re.search(r"(?im)^\*\*Device:\*\*\s*(.+)$", approved)
        structure_match = re.search(r"(?im)^\*\*Overall Structure:\*\*\s*(.+)$", approved)
        stack_section = _markdown_section(task, "Layer Stack (ordered bottom to top)")
        layer_stack: List[Dict[str, object]] = []
        for layer_match in re.finditer(
            r"(?im)^\d+\.\s*([^|\r\n]+)\|\s*([^|\r\n]+)(?:\|\s*GDS layer\s*([^|\r\n]+))?",
            stack_section,
        ):
            layer_stack.append({
                "role": layer_match.group(1).strip(),
                "material": layer_match.group(2).strip(),
                "gds_layer": (layer_match.group(3) or "").strip(),
            })
        recovered["approved_device_guidance"] = {
            "device_name": device_match.group(1).strip() if device_match else "Real-flake device",
            "overall_structure": structure_match.group(1).strip() if structure_match else approved,
            "layer_stack": layer_stack,
            "contact_constraints": _markdown_section(task, "Contact/Electrode Constraints (layer 10/0)"),
            "gate_constraints": _markdown_section(task, "Gate Constraints (layer 4/0)"),
        }
    return recovered


def _merge_embedded_task_context(task: str, extra_context: Dict[str, object]) -> Dict[str, object]:
    recovered = _embedded_real_flake_context(task)
    merged = dict(recovered)
    # Explicit structured values always win over compatibility recovery.
    merged.update(extra_context)
    gate_match = re.search(r"\bflake_[0-9a-f]{24}\b", task or "", re.I)
    if gate_match is None:
        gate_match = re.search(
            r"(?i)\b(?:human[- ]approved\s+)?gate(?:_id)?\s*[:=]?\s*([0-9a-f]{24})\b",
            task or "",
        )
    if gate_match and not merged.get("flake_selection_gate_id"):
        # A prose gate reference is only a lookup key. It never proves
        # approval; _merge_verified_approved_context checks the persisted gate.
        raw_gate_id = gate_match.group(1) if gate_match.lastindex else gate_match.group(0)
        merged["flake_selection_gate_id"] = HumanGateService.canonical_gate_id(raw_gate_id)
    return merged


def _merge_verified_approved_context(extra_context: Dict[str, object]) -> Dict[str, object]:
    """Replace caller-built approval shapes with the canonical persisted gate context."""
    normalized = dict(extra_context)
    nested = normalized.get("approved_candidate_context")
    supplied_selected: Dict[str, object] | None = None
    if isinstance(nested, dict):
        root_selected = normalized.get("selected_flakes_by_role")
        nested_selected = nested.get("selected_flakes_by_role")
        if isinstance(root_selected, dict):
            supplied_selected = root_selected
        elif isinstance(nested_selected, dict):
            supplied_selected = nested_selected
        # The selector's approved_candidate_context is the canonical public
        # handoff shape. Merge it at the JSON root for the backend contract.
        normalized.update(nested)
    elif normalized.get("flake_selection_human_approved") is True:
        root_selected = normalized.get("selected_flakes_by_role")
        if isinstance(root_selected, dict):
            supplied_selected = root_selected

    gate_id = str(normalized.get("flake_selection_gate_id") or "").strip()
    if not gate_id:
        if normalized.get("flake_selection_human_approved") is True:
            normalized["_flake_selection_approval_error"] = (
                "flake_selection_human_approved=true was supplied without a persisted gate id"
            )
        return normalized

    try:
        gate_id = HumanGateService.canonical_gate_id(gate_id)
        normalized["flake_selection_gate_id"] = gate_id
        approved = HumanGateService().get_approved_candidate_context(
            gate_id,
            project_id=str(normalized.get("project_id") or ""),
            selected_flakes_by_role=supplied_selected,
        )
    except Exception as exc:
        normalized["_flake_selection_approval_error"] = str(exc)
        return normalized

    normalized.update(approved)
    normalized["approved_candidate_context"] = approved
    normalized["_flake_selection_approval_verified"] = True
    normalized.pop("_flake_selection_approval_error", None)
    return normalized


def _normalize_real_flake_context(extra_context: Dict[str, object]) -> Dict[str, object]:
    """Normalize the managed-agent ``flake_candidates`` shape into the SDK contract."""
    extra_context = _merge_verified_approved_context(extra_context)
    if extra_context.get("_flake_selection_approval_verified") is True:
        # Never let a wider pre-approval flake_candidates list overwrite the
        # exact reduced set reconstructed from the approved gate.
        return extra_context
    records = extra_context.get("flake_candidates")
    if not isinstance(records, list):
        role_records = extra_context.get("real_flake_candidates")
        if isinstance(role_records, dict):
            records = []
            for role, value in role_records.items():
                values = value if isinstance(value, list) else [value]
                for record in values:
                    if isinstance(record, dict):
                        normalized_record = dict(record)
                        normalized_record.setdefault("role", str(role))
                        records.append(normalized_record)
    if not isinstance(records, list):
        return extra_context

    masks: Dict[str, List[str]] = {}
    candidates: Dict[str, List[Dict[str, object]]] = {}
    for value in records:
        if not isinstance(value, dict):
            continue
        role = str(value.get("role") or "").strip()
        mask_path = str(value.get("mask_20x_path") or "").strip()
        if not role or not mask_path:
            continue
        record = dict(value)
        # Some managed-agent handoffs historically mislabeled the database's
        # stage-coordinate values as ``location_*_nm``.  Preserve the numeric
        # values exactly (the dataset is authoritative) while fixing only the
        # boundary schema and its unit annotation.
        if record.get("location_x") is None and record.get("location_x_nm") is not None:
            record["location_x"] = record.pop("location_x_nm")
        if record.get("location_y") is None and record.get("location_y_nm") is not None:
            record["location_y"] = record.pop("location_y_nm")
        if record.get("location_x") is not None or record.get("location_y") is not None:
            record["location_units"] = "stage_um"
        # Downstream compatibility fields must point to the selected 20X mask,
        # never to a legacy 100X mask.
        record["mask_20x_path"] = mask_path
        record["mask_path"] = mask_path
        masks.setdefault(role, []).append(mask_path)
        candidates.setdefault(role, []).append(record)

    if not masks:
        return extra_context
    normalized = dict(extra_context)
    # These are derived canonical views. Always replace stale caller-provided
    # copies so a corrected unit schema cannot be shadowed by old *_nm keys.
    normalized["candidate_mask_paths_by_role"] = masks
    normalized["candidate_flakes_by_role"] = candidates
    normalized["flake_candidates"] = [
        record for role_records in candidates.values() for record in role_records
    ]
    normalized["real_flake_candidates"] = {
        role: role_records[0] if len(role_records) == 1 else role_records
        for role, role_records in candidates.items()
    }
    normalized.setdefault("design_stage", "real_flake_adaptation")
    run_metadata = normalized.get("run_metadata")
    calibration = normalized.get("pixel_calibration_um_per_pixel")
    if calibration is None and isinstance(run_metadata, dict):
        calibration = run_metadata.get("pixel_calibration_um_per_pixel")
    if calibration is not None:
        normalized.setdefault("pixel_size_um_per_px", calibration)
    return normalized


def _auto_resolve_real_flake_context(extra_context: Dict[str, object]) -> Dict[str, object]:
    """Fill missing real-flake inputs from the live inventory and project artifacts."""
    from common.services.design_candidate_payload_service import build_design_candidate_payload

    resolved = dict(extra_context)
    project_id = str(resolved.get("project_id") or "").strip()
    if project_id:
        try:
            from common.services.project_state_service import ProjectStateService

            project_context = ProjectStateService().build_design_context(
                project_id,
                include_inventory_candidates=False,
            )
            # The approved flake-selection handoff intentionally contains
            # selection data only.  Its sibling project context owns the
            # approved device guidance used to classify the design and decide
            # whether the verified ideal default applies.  Recover both parts
            # together when the caller supplied only a project id.
            for key in (
                "approved_device_guidance",
                "workflow_plan",
                "artifact_paths_by_role",
                "artifacts_by_role",
            ):
                if not resolved.get(key) and project_context.get(key):
                    resolved[key] = project_context[key]
            approved = project_context.get("approved_candidate_context")
            if (
                isinstance(approved, dict)
                and approved
                and not (
                    resolved.get("flake_selection_gate_id")
                    and resolved.get("_flake_selection_approval_error")
                )
            ):
                resolved["approved_candidate_context"] = approved
                resolved["flake_selection_gate_id"] = approved.get("flake_selection_gate_id")
        except Exception:
            # The normal candidate resolver below still provides its existing
            # diagnostics when the optional project record is unavailable.
            pass
    payload = build_design_candidate_payload(
        approved_device_guidance=_guidance_from_context(resolved),
        artifact_paths_by_role=resolved.get("artifact_paths_by_role"),
        artifacts_by_role=resolved.get("artifacts_by_role"),
        limit_per_role=20,
    )
    resolved["design_candidate_payload"] = payload
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
        current = resolved.get(key)
        if not current and payload.get(key):
            resolved[key] = payload[key]
    return resolved


def _load_project_json(path: str) -> Dict[str, object] | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _lower_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True).lower()
    except TypeError:
        return str(value).lower()


def _has_named_section(guidance: Dict[str, object], names: Iterable[str]) -> bool:
    for name in names:
        value = guidance.get(name)
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, list) and value:
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def validate_device_guidance_for_layout(
    guidance: Dict[str, object],
    task: str = "",
) -> Dict[str, object]:
    """Check whether approved guidance is specific enough for real KLayout execution.

    This intentionally validates general design semantics rather than enforcing
    material recipes. Claude/KLayout can be creative about geometry, but it
    should not invent the device stack, contacts, or gate physics.
    """
    missing: List[str] = []
    warnings: List[str] = []
    layer_stack = guidance.get("layer_stack")
    roles = _roles_from_guidance(guidance)
    layers = _guidance_layers(guidance)
    text = " ".join(
        [
            task.lower(),
            _lower_json(guidance),
            " ".join(roles).lower(),
        ]
    )

    if not guidance:
        missing.append("approved_device_guidance")

    if not _stringify(guidance.get("device_name")):
        missing.append("device_name")

    has_explicit_stack = isinstance(layer_stack, list) and len(layer_stack) >= 2
    has_stack_roles = _has_named_section(guidance, ["stack_roles", "material_requirements"])
    if not (has_explicit_stack or has_stack_roles):
        missing.append("layer_stack or stack_roles with material roles")
    elif not has_explicit_stack:
        warnings.append(
            "No ordered layer_stack was supplied; KLayout can draft role geometry, but vertical stack order may be ambiguous."
        )

    transistor_like = any(word in text for word in ("transistor", "fet", "source", "drain"))
    electrode_like = any(word in text for word in ("contact", "electrode", "source", "drain", "metal"))
    gate_like = "gate" in text

    layer_role_text = " ".join(
        f"{_layer_role(item)} {_layer_name(item)} {_layer_material(item)}".lower()
        for item in layers
        if isinstance(item, dict)
    )
    has_contact_layers = any(word in layer_role_text for word in ("contact", "electrode", "source", "drain", "metal"))
    has_gate_layers = "gate" in layer_role_text

    if (transistor_like or electrode_like) and not (
        _has_named_section(guidance, ["contact_scheme", "contact_constraints"]) or has_contact_layers
    ):
        missing.append("contact_scheme/contact_constraints or contact/electrode layer roles")

    if (transistor_like or gate_like) and not (
        _has_named_section(guidance, ["gate_scheme", "gate_constraints"]) or has_gate_layers
    ):
        missing.append("gate_scheme/gate_constraints or gate layer roles")

    if has_explicit_stack:
        weak_layers = []
        for index, layer in enumerate(layer_stack, start=1):  # type: ignore[arg-type]
            if not isinstance(layer, dict):
                continue
            role = _layer_role(layer)
            material = _layer_material(layer)
            if not role or not material:
                weak_layers.append(index)
        if weak_layers:
            missing.append(f"role/material for layer_stack entries {weak_layers}")

    return {
        "complete": not missing,
        "missing": missing,
        "warnings": warnings,
        "checks": {
            "has_explicit_stack": has_explicit_stack,
            "has_stack_roles": has_stack_roles,
            "transistor_like": transistor_like,
            "electrode_like": electrode_like,
            "gate_like": gate_like,
            "has_contact_layers": has_contact_layers,
            "has_gate_layers": has_gate_layers,
        },
    }


def _unique_strings(values: Iterable[object]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _stringify(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip() or fallback
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _layer_name(layer: Dict[str, object], fallback: str = "") -> str:
    return _stringify(
        layer.get("name") or layer.get("layer") or layer.get("role") or layer.get("material"),
        fallback,
    )


def _layer_role(layer: Dict[str, object], fallback: str = "") -> str:
    return _stringify(layer.get("role") or layer.get("layer") or layer.get("name"), fallback)


def _layer_material(layer: Dict[str, object], fallback: str = "") -> str:
    material = _stringify(layer.get("material"), "")
    if material:
        return material
    text = f"{layer.get('name') or ''} {layer.get('layer') or ''} {layer.get('role') or ''}".lower()
    if "rese" in text:
        return "ReSe2"
    if "hbn" in text or "h-bn" in text:
        return "hBN"
    if "graphite" in text:
        return "Graphite"
    if "graphene" in text:
        return "Graphene"
    if "source" in text and "drain" in text:
        return "source/drain contact material from approved guidance"
    if "metal" in text or "contact" in text or "electrode" in text or "gate" in text:
        return "metal/gate material from approved guidance"
    return fallback


def _materials_from_guidance(guidance: Dict[str, object]) -> List[str]:
    materials: List[object] = []
    layer_stack = guidance.get("layer_stack")
    if isinstance(layer_stack, list):
        for layer in layer_stack:
            if isinstance(layer, dict):
                material = _layer_material(layer)
                if material:
                    materials.append(material)

    material_requirements = guidance.get("material_requirements")
    if isinstance(material_requirements, dict):
        for value in material_requirements.values():
            if isinstance(value, dict):
                material = value.get("material") or value.get("name")
                if material:
                    materials.append(material)
            elif isinstance(value, str):
                materials.append(value)
    elif isinstance(material_requirements, list):
        for value in material_requirements:
            if isinstance(value, dict):
                material = value.get("material") or value.get("name")
                if material:
                    materials.append(material)
            elif isinstance(value, str):
                materials.append(value)

    return _unique_strings(materials)


def _roles_from_guidance(guidance: Dict[str, object]) -> List[str]:
    roles: List[object] = []
    stack_roles = guidance.get("stack_roles")
    if isinstance(stack_roles, dict):
        roles.extend(stack_roles.keys())
    elif isinstance(stack_roles, list):
        for item in stack_roles:
            if isinstance(item, dict):
                role = item.get("role") or item.get("name")
                if role:
                    roles.append(role)
            elif isinstance(item, str):
                roles.append(item)

    layer_stack = guidance.get("layer_stack")
    if isinstance(layer_stack, list):
        for layer in layer_stack:
            if isinstance(layer, dict):
                role = layer.get("role") or layer.get("layer") or layer.get("name")
                if role:
                    roles.append(role)
    return _unique_strings(roles)


def _guidance_layers(guidance: Dict[str, object]) -> List[Dict[str, object]]:
    layer_stack = guidance.get("layer_stack")
    if isinstance(layer_stack, list) and layer_stack:
        normalized = []
        for index, layer in enumerate(layer_stack, start=1):
            if isinstance(layer, dict):
                item = dict(layer)
                item["name"] = _layer_name(item, f"layer_{index}")
                item["role"] = _layer_role(item, item["name"])
                item["material"] = _layer_material(item, "approved material")
                item["order"] = item.get("order") or item.get("position") or item.get("index") or index
                if "target_thickness" not in item:
                    item["target_thickness"] = (
                        item.get("thickness")
                        or item.get("thickness_nm")
                        or item.get("thickness_layers")
                    )
                normalized.append(item)
            else:
                normalized.append({"order": index, "role": f"layer_{index}", "material": str(layer)})
        return normalized

    roles = _roles_from_guidance(guidance)
    if roles:
        return [{"order": index, "role": role, "material": "see approved guidance"} for index, role in enumerate(roles, start=1)]
    return [{"order": 1, "role": "active_region", "material": "approved materials"}]


def _format_stage(stage: object, index: int) -> str:
    if isinstance(stage, str):
        return stage.strip() or f"Stage {index}: define goals and outputs."
    if not isinstance(stage, dict):
        return f"Stage {index}: {stage}"

    title = stage.get("stage") or stage.get("name") or stage.get("goal") or f"Stage {index}"
    actor = stage.get("actor")
    goal = stage.get("goal")
    inputs = stage.get("inputs_needed") or stage.get("inputs")
    outputs = stage.get("outputs_expected") or stage.get("outputs")
    dependencies = stage.get("dependencies")
    execution_tag = stage.get("execution_tag")
    cleanroom_session = stage.get("cleanroom_session_id")

    parts = [str(title)]
    if actor:
        parts.append(f"actor={actor}")
    if goal and goal != title:
        parts.append(f"goal={goal}")
    if inputs:
        parts.append(f"inputs={inputs}")
    if outputs:
        parts.append(f"outputs={outputs}")
    if dependencies:
        parts.append(f"dependencies={dependencies}")
    if execution_tag:
        parts.append(f"tag={execution_tag}")
    if cleanroom_session:
        parts.append(f"cleanroom_session={cleanroom_session}")
    return "; ".join(str(part) for part in parts)


def _extract_plan_steps(guidance: Dict[str, object]) -> List[str]:
    plan = guidance.get("workflow_plan")
    if isinstance(plan, dict):
        for key in ("stages", "steps", "plan", "workflow_plan"):
            value = plan.get(key)
            if isinstance(value, list):
                return [_format_stage(stage, idx) for idx, stage in enumerate(value, start=1)]
        return [_format_stage(plan, 1)]
    if isinstance(plan, list):
        return [_format_stage(stage, idx) for idx, stage in enumerate(plan, start=1)]
    if isinstance(plan, str) and plan.strip():
        return [plan.strip()]

    return [
        "Review approved_device_guidance, evidence, material roles, and human edits before drawing geometry.",
        "Create the ideal KLayout layout first using the Claude Agent SDK and KlayoutClaw before adapting to real flakes.",
        "Identify any clean-room fabrication operations such as contacts, electrodes, lithography, deposition, etch, and alignment masks; mark those operations first in the KLayout output.",
        "Resolve available masks, optical references, and human/external artifacts by material role.",
        "Update the KLayout geometry from the approved stack, contact, gate, channel, and alignment constraints.",
        "Export GDS, screenshots, selected material paths, offsets, assumptions, open issues, and Qumus handoff data.",
    ]


def _required_inputs_from_guidance(guidance: Dict[str, object]) -> List[str]:
    inputs = [
        "approved_device_guidance from project_manager/autonomous Qumus workflow",
        "20X candidate_mask_paths_by_role for all Qumus-searchable material roles, when real-flake adaptation starts",
        "20X candidate_image_paths_by_role for optical review when available",
        "artifact_paths_by_role for human-provided or external-fab contacts, gates, substrates, masks, or chips",
        "20X pixel_size_um_per_px and coordinate-frame notes for every real-flake raster/image source",
    ]
    roles = _roles_from_guidance(guidance)
    if roles:
        inputs.extend(f"role `{role}` resolved from approved guidance" for role in roles)
    return inputs


def _cleanroom_text(guidance: Dict[str, object]) -> str:
    explicit_plan = guidance.get("cleanroom_plan") or guidance.get("cleanroom_process_plan")
    if isinstance(explicit_plan, (dict, list)):
        return json.dumps(explicit_plan, ensure_ascii=False)
    if isinstance(explicit_plan, str) and explicit_plan.strip():
        return explicit_plan.strip()
    plan = guidance.get("cleanroom_minimization_plan")
    if isinstance(plan, (dict, list)):
        return (
            "Legacy cleanroom_minimization_plan is advisory context only; do not constrain "
            "the design to one clean-room visit. Identify and mark clean-room operations "
            f"explicitly instead. Source guidance: {json.dumps(plan, ensure_ascii=False)}"
        )
    if isinstance(plan, str) and plan.strip():
        return (
            "Legacy cleanroom_minimization_plan is advisory context only; do not constrain "
            f"the design to one clean-room visit. Source guidance: {plan.strip()}"
        )
    return (
        "Do not constrain the design to a single clean-room visit. Identify each "
        "clean-room fabrication operation explicitly, especially contacts/electrodes, "
        "lithography, metal deposition, etch, and alignment-mask work."
    )


_REAL_FLAKE_DESIGN_STAGES = {
    "real_flake_adaptation",
    "real_flake",
    "real_flake_adapted_layout",
    "real_flake_design",
    "final_layout",
    "inventory_specific",
}


def _canonical_design_stage(value: object) -> str | None:
    """Return the canonical design stage for a stage/status-like value.

    Handoffs can contain both a top-level stage and a nested output contract.
    Older handoffs also use aliases such as ``real_flake`` or
    ``final_layout``.  Keeping this normalization in one place prevents a
    stale nested value from silently selecting the ideal-layout branch.
    """
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return None
    if normalized in _REAL_FLAKE_DESIGN_STAGES:
        return "real_flake_adaptation"
    if normalized in {
        "ideal",
        "initial_design",
        "ideal_layout",
        "ideal_klayout_layout",
    }:
        return "ideal_klayout_layout"
    return None


def _design_stage_from_payload(payload: Dict[str, object]) -> str:
    """Resolve one authoritative stage for all SDK prompt/artifact logic.

    The input context is the source of truth for a resumed or older handoff;
    the nested output contract is retained for compatibility.  A real-flake
    signal wins over an ideal signal so an inconsistent legacy handoff cannot
    downgrade a real-flake run into synthetic placeholder generation.
    """
    output_contract = payload.get("output_contract")
    input_context = payload.get("input_context")
    context_stage = input_context.get("design_stage") if isinstance(input_context, dict) else None

    explicit_values = [
        payload.get("design_stage"),
        context_stage,
        output_contract.get("design_stage") if isinstance(output_contract, dict) else None,
    ]
    canonical_values = [_canonical_design_stage(value) for value in explicit_values]
    if "real_flake_adaptation" in canonical_values:
        return "real_flake_adaptation"
    # A resumed design context can carry real candidate records while an old
    # nested output contract still says ideal.  Real candidates must win so
    # stale compatibility metadata cannot silently restore synthetic flakes.
    if isinstance(input_context, dict) and _has_real_flake_candidate_inputs(input_context):
        return "real_flake_adaptation"
    if "ideal_klayout_layout" in canonical_values:
        return "ideal_klayout_layout"

    if _labels_request_real_flake(
        str(payload.get("scenario") or ""),
        str(payload.get("workflow_id") or ""),
    ):
        return "real_flake_adaptation"
    return "ideal_klayout_layout"


def _role_mapping_has_mask_paths(mapping: object) -> bool:
    """True when a `*_mask_paths_by_role` mapping carries at least one usable path.

    Callers legitimately pass the per-role value as a single path string,
    a list/tuple/set of paths, or a nested dict (e.g. {"100x": "..."}); only
    an empty container or a blank string counts as "no candidate".
    """
    if not isinstance(mapping, dict):
        return False

    def _value_has_path(value: object) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(_value_has_path(v) for v in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(_value_has_path(v) for v in value)
        return False

    return any(_value_has_path(v) for v in mapping.values())


def _labels_request_real_flake(*labels: str) -> bool:
    """True when a structured stage label names the real-flake stage.

    Only machine labels are inspected -- the ``scenario`` argument, the derived
    ``workflow_id`` slug, or an explicit ``design_stage`` value. Free-form task
    prose is deliberately never scanned here: an ideal-stage request routinely
    *mentions* the later real-flake step ("real-flake adaptation comes in a
    later stage"), and a substring match on that prose would wrongly force the
    run into real-flake mode. The design stage is decided from explicit signals
    only -- see ``build_claude_klayout_handoff`` and ``_design_stage_from_payload``.
    """
    blob = " ".join(t for t in labels if t).lower().replace("-", "_").replace(" ", "_")
    if any(stage in blob for stage in _REAL_FLAKE_DESIGN_STAGES):
        return True
    return "real_flake_adaptation" in blob or "real_flake_design" in blob


def _has_real_flake_candidate_inputs(extra_context: Dict[str, object]) -> bool:
    if str(extra_context.get("design_stage") or "").strip().lower() in _REAL_FLAKE_DESIGN_STAGES:
        return True
    # Resumed project context normally exposes full candidate records under
    # candidate_flakes_by_role, while older handoffs expose only mask paths.
    # Both forms are real-flake inputs when a record contains a usable mask_path.
    for key in ("candidate_mask_paths_by_role", "candidate_flakes_by_role"):
        if _role_mapping_has_mask_paths(extra_context.get(key)):
            return True
    payload = extra_context.get("design_candidate_payload")
    if isinstance(payload, dict):
        for key in ("candidate_mask_paths_by_role", "candidate_flakes_by_role"):
            if _role_mapping_has_mask_paths(payload.get(key)):
                return True
    return False


def _device_material_family(task: str, guidance: Dict[str, object]) -> str:
    """Resolve exactly one active material family without cross-family precedence."""
    structured_text = " ".join(
        _stringify(guidance.get(key))
        for key in (
            "device_name",
            "device_family",
            "material_system",
            "research_goal",
            "overall_structure",
            "layer_stack",
            "stack_roles",
            "contact_scheme",
            "gate_scheme",
            "layout_constraints",
        )
    ).lower()

    def _families(text: str) -> set[str]:
        found: set[str] = set()
        if re.search(r"\bgraphene\b", text):
            found.add("graphene")
        # Word boundaries deliberately exclude identifiers such as
        # ``rese2_channel`` that often occur in negative guard instructions.
        if re.search(r"\brese2\b", text):
            found.add("rese2")
        return found

    structured_families = _families(structured_text)
    if len(structured_families) == 1:
        return next(iter(structured_families))
    if len(structured_families) > 1:
        return ""

    # Free text is only a fallback. Remove explicit negative clauses before
    # looking for a material name so "no ReSe2" cannot activate that family.
    fallback = re.sub(
        r"(?i)\b(?:no|not|never|without|do\s+not)\b[^.\n;]{0,100}\b(?:graphene|rese2)\b[^.\n;]*",
        "",
        task or "",
    ).lower()
    task_families = _families(fallback)
    return next(iter(task_families)) if len(task_families) == 1 else ""


def _filter_candidate_roles_for_family(
    extra_context: Dict[str, object], family: str
) -> Dict[str, object]:
    """Remove only channel roles that contradict the resolved device family.

    Inventory recovery deliberately has a broad unknown-family fallback. Once
    the request resolves to one family, the fallback must not leak the other
    family's channel into real-flake validation. Encapsulation and gate roles
    remain device-agnostic.
    """
    if extra_context.get("_flake_selection_approval_verified") is True:
        # A human-approved reduced set is immutable at this boundary.
        return extra_context
    if family not in {"graphene", "rese2"}:
        return extra_context

    def role_allowed(role: object) -> bool:
        normalized = str(role or "").strip().lower().replace("-", "_").replace(" ", "_")
        if family == "graphene":
            return normalized != "rese2_channel"
        return normalized != "graphene_channel"

    def filtered_mapping(value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {key: item for key, item in value.items() if role_allowed(key)}

    filtered = dict(extra_context)
    mapping_keys = (
        "candidate_mask_paths_by_role",
        "candidate_image_paths_by_role",
        "candidate_flakes_by_role",
        "real_flake_candidates",
    )
    for key in mapping_keys:
        if key in filtered:
            filtered[key] = filtered_mapping(filtered[key])

    records = filtered.get("flake_candidates")
    if isinstance(records, list):
        filtered["flake_candidates"] = [
            record
            for record in records
            if not isinstance(record, dict) or role_allowed(record.get("role"))
        ]

    payload = filtered.get("design_candidate_payload")
    if isinstance(payload, dict):
        clean_payload = dict(payload)
        for key in mapping_keys:
            if key in clean_payload:
                clean_payload[key] = filtered_mapping(clean_payload[key])
        missing = clean_payload.get("missing_candidate_roles")
        if isinstance(missing, list):
            clean_payload["missing_candidate_roles"] = [
                entry
                for entry in missing
                if not isinstance(entry, dict) or role_allowed(entry.get("role"))
            ]
        filtered["design_candidate_payload"] = clean_payload
    return filtered


def _prepare_center_priority_geometry_masks(
    extra_context: Dict[str, object], output_dir: str
) -> Dict[str, object]:
    """Prepare one deterministic center-first geometry mask per candidate.

    The original ``mask_20x_path`` remains the database identity/provenance
    path. ``geometry_mask_path`` is a binary, single-target mask produced by
    the repository's established preparation code and is the only mask the
    layout backend should convert to polygons.
    """
    role_records = extra_context.get("candidate_flakes_by_role")
    if not isinstance(role_records, dict):
        return extra_context

    from PIL import Image
    from scripts.prepare_graphene_real_device_context import extract_center_main_component

    prepared_dir = Path(output_dir) / "prepared_candidate_masks"
    prepared_dir.mkdir(parents=True, exist_ok=True)
    prepared_by_source: Dict[str, tuple[str, Dict[str, object], Dict[str, object]]] = {}
    normalized_roles: Dict[str, List[Dict[str, object]]] = {}
    geometry_paths: Dict[str, List[str]] = {}

    for role, value in role_records.items():
        values = value if isinstance(value, list) else [value]
        normalized: List[Dict[str, object]] = []
        paths: List[str] = []
        for index, candidate in enumerate(values):
            if not isinstance(candidate, dict):
                continue
            source = str(candidate.get("mask_20x_path") or "").strip()
            if not source or not os.path.isfile(source):
                continue
            cached = prepared_by_source.get(os.path.normcase(os.path.abspath(source)))
            if cached is None:
                digest = hashlib.sha1(os.path.abspath(source).encode("utf-8")).hexdigest()[:12]
                prepared_path = prepared_dir / f"center_target_{digest}.png"
                selected_mask, selection, metrics = extract_center_main_component(source)
                Image.fromarray(selected_mask).save(prepared_path)
                cached = (str(prepared_path.resolve()), dict(selection), dict(metrics))
                prepared_by_source[os.path.normcase(os.path.abspath(source))] = cached
            geometry_path, selection, metrics = cached
            record = dict(candidate)
            record["geometry_mask_path"] = geometry_path
            record["geometry_mask_source"] = "scripts.prepare_graphene_real_device_context"
            record["geometry_mask_selection"] = selection
            record["geometry_mask_metrics"] = metrics
            normalized.append(record)
            paths.append(geometry_path)
        normalized_roles[str(role)] = normalized
        geometry_paths[str(role)] = paths

    prepared = dict(extra_context)
    prepared["candidate_flakes_by_role"] = normalized_roles
    prepared["candidate_geometry_mask_paths_by_role"] = geometry_paths
    prepared["geometry_mask_policy"] = (
        "Use geometry_mask_path only for polygon extraction. It contains one center-priority target. "
        "Keep mask_20x_path unchanged for database identity and stacking provenance. Candidate eligibility "
        "thresholds apply to the database size_area_um value, not to the segmented color-component pixel area."
    )
    prepared["flake_candidates"] = [
        record for records in normalized_roles.values() for record in records
    ]
    prepared["real_flake_candidates"] = {
        role: records[0] if len(records) == 1 else records
        for role, records in normalized_roles.items()
    }
    payload = prepared.get("design_candidate_payload")
    if isinstance(payload, dict):
        nested = dict(payload)
        nested["candidate_flakes_by_role"] = normalized_roles
        nested["candidate_geometry_mask_paths_by_role"] = geometry_paths
        prepared["design_candidate_payload"] = nested
    return prepared


def _is_graphene_quantum_hall_design(task: str, guidance: Dict[str, object]) -> bool:
    text = " ".join(
        [task, _stringify(guidance)]
    ).lower()
    return _device_material_family(task, guidance) == "graphene" and (
        "quantum hall" in text
        or "hall bar" in text
        or "hallbar" in text
        or "4-terminal" in text
        or "4 terminal" in text
        or "4 pairs" in text
        or "4-pair" in text
        or "edge contact" in text
        or "edge-contact" in text
    )


def _is_graphene_device_design(task: str, guidance: Dict[str, object]) -> bool:
    text = " ".join(
        [
            task,
            _stringify(guidance.get("device_name")),
            _stringify(guidance.get("research_goal")),
            _stringify(guidance.get("overall_structure")),
            _stringify(guidance.get("layer_stack")),
            _stringify(guidance.get("stack_roles")),
            _stringify(guidance.get("contact_scheme")),
            _stringify(guidance.get("gate_scheme")),
            _stringify(guidance.get("layout_constraints")),
        ]
    ).lower()
    return "graphene" in text and (
        _is_graphene_quantum_hall_design(task, guidance)
        or "device" in text
        or "transistor" in text
        or "heterostructure" in text
        or "stack" in text
    )


def _repo_relative_path(raw: object) -> Path:
    path = Path(str(raw or ""))
    if path.is_absolute():
        return path.resolve()
    return (Path(__file__).resolve().parents[3] / path).resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_graphene_qh_ideal_eligibility(
    task: str,
    guidance: Dict[str, object],
    extra_context: Dict[str, object],
) -> Dict[str, object]:
    """Decide whether the verified run_187 ideal layout matches this request."""
    manifest = _load_project_json(PROJECT_GRAPHENE_QH_DEFAULT_IDEAL_JSON)
    if not manifest or manifest.get("enabled") is not True:
        return {"eligible": False, "reason": "default ideal-design manifest is unavailable or disabled"}
    if not _is_graphene_quantum_hall_design(task, guidance):
        return {"eligible": False, "reason": "request is not a graphene quantum Hall/Hall-bar design"}
    if (
        extra_context.get("reuse_default_ideal_design") is False
        or extra_context.get("disable_default_ideal_design") is True
        or guidance.get("reuse_default_ideal_design") is False
        or guidance.get("disable_default_ideal_design") is True
    ):
        return {"eligible": False, "reason": "default reuse was explicitly disabled"}
    if extra_context.get("force_ideal_redesign") is True or guidance.get(
        "force_ideal_redesign"
    ) is True:
        return {"eligible": False, "reason": "an explicit ideal redesign was requested"}

    request_text = str(task or "").lower()
    explicit_redesign_phrases = (
        "redesign the ideal",
        "new ideal design",
        "custom ideal design",
        "do not reuse the default",
        "don't reuse the default",
        "replace the default design",
    )
    if any(phrase in request_text for phrase in explicit_redesign_phrases):
        return {"eligible": False, "reason": "task explicitly requests a different ideal design"}
    if re.search(r"\b(custom|nonstandard|modified|alternate|different)\b", request_text):
        return {"eligible": False, "reason": "task requests non-default geometry"}

    explicit_terminal_counts = {
        int(value)
        for value in re.findall(r"\b(\d+)\s*[- ]?terminals?\b", request_text)
    }
    contact_scheme = guidance.get("contact_scheme")
    if isinstance(contact_scheme, dict):
        raw_terminals = contact_scheme.get("terminals") or contact_scheme.get("terminal_count")
        try:
            if raw_terminals is not None:
                explicit_terminal_counts.add(int(raw_terminals))
        except (TypeError, ValueError):
            leading_count = re.match(r"\s*(\d+)", str(raw_terminals or ""))
            if leading_count:
                explicit_terminal_counts.add(int(leading_count.group(1)))
        contact_type = str(contact_scheme.get("type") or "").lower()
        if contact_type and "edge" not in contact_type:
            return {"eligible": False, "reason": "contact style differs from the edge-contact default"}
        contact_metal = str(contact_scheme.get("metal") or "").lower()
        if contact_metal and not ("cr" in contact_metal and "au" in contact_metal):
            return {"eligible": False, "reason": "contact metal differs from the Cr/Au default"}
    if any(count not in {10, 11} for count in explicit_terminal_counts):
        return {"eligible": False, "reason": "terminal topology differs from the 10+1 default"}

    dimension_text = request_text
    for container_name in ("layout_constraints", "device_geometry"):
        container = guidance.get(container_name)
        if isinstance(container, dict):
            dimension_text += " " + " ".join(
                _stringify(value).lower()
                for key, value in container.items()
                if "hall" in str(key).lower() or "channel" in str(key).lower()
            )
    requested_dimensions = {
        (float(width), float(length))
        for width, length in re.findall(
            r"(\d+(?:\.\d+)?)\s*(?:um|µm)?\s*[x×]\s*(\d+(?:\.\d+)?)\s*(?:um|µm)?",
            dimension_text,
        )
    }
    default_dimensions = {(10.0, 20.0), (20.0, 10.0), (5.0, 10.0), (10.0, 5.0)}
    if requested_dimensions - default_dimensions:
        return {"eligible": False, "reason": "Hall-channel dimensions differ from the defaults"}

    checked_paths: List[str] = []
    for section_name in ("artifacts", "dependencies"):
        section = manifest.get(section_name)
        if not isinstance(section, dict):
            return {"eligible": False, "reason": f"manifest has no {section_name} mapping"}
        for name, raw_spec in section.items():
            if not isinstance(raw_spec, dict):
                return {"eligible": False, "reason": f"manifest entry {section_name}.{name} is invalid"}
            path = _repo_relative_path(raw_spec.get("path"))
            if not path.is_file():
                return {"eligible": False, "reason": f"default artifact is missing: {path}"}
            expected_hash = str(raw_spec.get("sha256") or "").strip().lower()
            if not expected_hash or _sha256_file(path).lower() != expected_hash:
                return {"eligible": False, "reason": f"default artifact hash mismatch: {path}"}
            checked_paths.append(str(path))

    artifacts = manifest.get("artifacts")
    result_spec = artifacts.get("ideal_result") if isinstance(artifacts, dict) else None
    source_result = _load_project_json(
        str(_repo_relative_path(result_spec.get("path"))) if isinstance(result_spec, dict) else ""
    )
    checks = source_result.get("checks") if isinstance(source_result, dict) else None
    if (
        not source_result
        or source_result.get("status") != "ok"
        or source_result.get("failed_checks") not in (None, [])
        or not isinstance(checks, dict)
        or not checks
        or any(not isinstance(check, dict) or check.get("pass") is not True for check in checks.values())
    ):
        return {"eligible": False, "reason": "source ideal result is not fully verified"}
    return {
        "eligible": True,
        "reason": "verified run_187 default matches graphene QH edge-contact defaults",
        "manifest": manifest,
        "verified_paths": checked_paths,
        "source_result": source_result,
    }


def _materialize_default_graphene_qh_ideal(
    *,
    task: str,
    guidance: Dict[str, object],
    extra_context: Dict[str, object],
    output_dir: str,
) -> Dict[str, object] | None:
    eligibility = _default_graphene_qh_ideal_eligibility(task, guidance, extra_context)
    if eligibility.get("eligible") is not True:
        return None
    manifest = eligibility["manifest"]
    artifacts = manifest["artifacts"]  # type: ignore[index]
    target_dir = Path(output_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        "ideal_gds": target_dir / "ideal_device.gds",
        "ideal_screenshot": target_dir / "ideal_device_screenshot.png",
        "ideal_result": target_dir / "ideal_device_result.json",
    }
    source_paths = {
        name: _repo_relative_path(spec.get("path"))
        for name, spec in artifacts.items()  # type: ignore[union-attr]
        if isinstance(spec, dict)
    }
    for name in ("ideal_gds", "ideal_screenshot"):
        source = source_paths[name]
        target = targets[name]
        if source != target:
            shutil.copy2(source, target)

    source_result = deepcopy(eligibility["source_result"])
    source_result.update(
        {
            "ideal_gds": str(targets["ideal_gds"]),
            "ideal_screenshot": str(targets["ideal_screenshot"]),
            "ideal_layout_result_json": str(targets["ideal_result"]),
            "ideal_klayout_script": str(source_paths["verified_build_script"]),
            "ideal_klayout_script_execution_required": False,
            "reused_default_ideal_design": True,
            "default_ideal_template_id": manifest.get("template_id"),  # type: ignore[union-attr]
            "default_ideal_source_run": manifest.get("source_run"),  # type: ignore[union-attr]
            "default_ideal_manifest": PROJECT_GRAPHENE_QH_DEFAULT_IDEAL_JSON,
        }
    )
    if source_paths["ideal_result"] != targets["ideal_result"]:
        targets["ideal_result"].write_text(
            json.dumps(source_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    provenance = {
        "status": "ok",
        "mode": "verified_default_ideal_design_reuse",
        "template_id": manifest.get("template_id"),  # type: ignore[union-attr]
        "source_run": manifest.get("source_run"),  # type: ignore[union-attr]
        "manifest_path": PROJECT_GRAPHENE_QH_DEFAULT_IDEAL_JSON,
        "claude_sdk_executed": False,
        "target_artifacts": {name: str(path) for name, path in targets.items()},
        "verified_source_build_script": str(source_paths["verified_build_script"]),
    }
    provenance_path = target_dir / "ideal_design_reuse.json"
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**provenance, "provenance_path": str(provenance_path)}


def _is_bottom_contact_geometry_design(task: str, guidance: Dict[str, object]) -> bool:
    text = " ".join(
        [
            task,
            _stringify(guidance.get("device_name")),
            _stringify(guidance.get("research_goal")),
            _stringify(guidance.get("overall_structure")),
            _stringify(guidance.get("layer_stack")),
            _stringify(guidance.get("contact_scheme")),
            _stringify(guidance.get("gate_scheme")),
            _stringify(guidance.get("layout_constraints")),
        ]
    ).lower()
    return _device_material_family(task, guidance) == "rese2" and (
        re.search(r"\brese2\b", text) is not None
        or "bottom contact" in text
        or "split gate" in text
        or "split-gate" in text
    )


def _safe_cell_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", raw.strip())[:48].strip("_")
    return name or "IDEAL_DEVICE"


def _build_ai_workflow(task: str, workflow_id: str, extra_context: Dict[str, object]) -> Dict[str, object]:
    guidance = _guidance_from_context(extra_context)
    device_name = guidance.get("device_name") if isinstance(guidance.get("device_name"), str) else ""
    strategy = guidance.get("workflow_strategy") if isinstance(guidance.get("workflow_strategy"), str) else ""
    name = device_name or strategy or "AI-designed device workflow"

    workflow = {
        "id": workflow_id or AI_DESIGNED_WORKFLOW,
        "name": name,
        "material_family": _materials_from_guidance(guidance),
        "design_order": _extract_plan_steps(guidance),
        "quick_design_mode": "claude_agent_sdk_klayout_execution",
        "klayout_role": "claude_agent_sdk_executes_klayout_from_approved_guidance",
        "required_inputs": _required_inputs_from_guidance(guidance),
        "handoff_outputs": [
            "CLAUDE_START_HERE.md",
            "claude_design_contract.json",
            "ideal_device_layout.py",
            "ideal_device.gds from Claude Agent SDK/KLayout",
            "ideal_device_screenshot.png from Claude Agent SDK/KLayout",
            "ideal_device_result.json from Claude Agent SDK",
            "autonomous continuation to inventory/real-flake preparation after ideal KLayout design",
            "final_device.gds from Claude Agent SDK after real flake adaptation",
            "autonomous continuation to stacking after real-flake/final design",
            "claude_run_notes.md from Claude Agent SDK",
        ],
        "cleanroom_minimization": _cleanroom_text(guidance),
        "workflow_source": "approved_device_guidance.workflow_plan when present; otherwise generic Qumus design handoff scaffold",
    }
    if strategy:
        workflow["workflow_strategy"] = strategy
    return workflow


def get_workflow_definition(
    scenario_id: str,
    task: str = "",
    extra_context: Dict[str, object] | None = None,
) -> Dict[str, object]:
    """Return a workflow definition without applying material-specific recipes."""
    normalized = normalize_design_scenario(scenario_id, task)
    if normalized == LEGACY_MASK_OPTIMIZER:
        return deepcopy(LEGACY_WORKFLOW_DEFINITION)
    return _build_ai_workflow(task, normalized, extra_context or {})


def _build_ideal_klayout_script(
    *,
    task: str,
    guidance: Dict[str, object],
    workflow: Dict[str, object],
    ideal_gds_path: str,
    result_json_path: str,
) -> str:
    """Build pya code the Claude Agent SDK backend should execute inside KLayout via KlayoutClaw."""
    device_name = _stringify(guidance.get("device_name"), "Ideal device")
    top_cell_name = _safe_cell_name(device_name)
    layers = _guidance_layers(guidance)
    graphene_qh_design = _is_graphene_quantum_hall_design(task, guidance)
    graphene_device_automation = _is_graphene_device_design(task, guidance)
    bottom_contact_design = _is_bottom_contact_geometry_design(task, guidance)
    graphene_template_profile = (
        _load_project_json(PROJECT_GRAPHENE_TEMPLATE_PROFILE_JSON) if graphene_qh_design else None
    )
    rese2_bottom_contact_profile = (
        _load_project_json(PROJECT_RESE2_BOTTOM_CONTACT_PROFILE_JSON) if bottom_contact_design else None
    )
    payload = {
        "task": task,
        "device_name": device_name,
        "research_goal": _stringify(guidance.get("research_goal"), task),
        "device_family": (
            "bottom_contact_rese2_style" if bottom_contact_design
            else "edge_contact_graphene_style" if graphene_qh_design
            else None
        ),
        "device_topology": guidance.get("device_topology"),
        "unit_interpretation": guidance.get("unit_interpretation"),
        "forbidden_default_layouts": guidance.get("forbidden_default_layouts"),
        "contact_scheme": guidance.get("contact_scheme"),
        "gate_scheme": guidance.get("gate_scheme"),
        "layout_constraints": guidance.get("layout_constraints"),
        "cleanroom_minimization_plan": guidance.get("cleanroom_minimization_plan"),
        "workflow_plan": guidance.get("workflow_plan"),
        "layers": layers,
        "workflow": workflow,
        "graphene_device_full_automation": graphene_device_automation,
        "design_output_full_automation": True,
        "human_review_required": False,
        "review_gate": None,
        "next_qmus_action_after_design": "continue_to_inventory_or_real_flake_preparation",
        "ideal_gds_path": ideal_gds_path,
        "result_json_path": result_json_path,
        "alignment_marker_gds": PROJECT_GRAPHENE_ELECTRODE_TEMPLATE_GDS if graphene_qh_design else PROJECT_ALIGNMENT_MARKER_GDS,
        "graphene_qh_electrode_template_gds": PROJECT_GRAPHENE_ELECTRODE_TEMPLATE_GDS if graphene_qh_design else None,
        "graphene_qh_template_profile_json": PROJECT_GRAPHENE_TEMPLATE_PROFILE_JSON if graphene_qh_design else None,
        "graphene_qh_template_profile": graphene_template_profile,
        "rese2_bottom_contact_profile_json": PROJECT_RESE2_BOTTOM_CONTACT_PROFILE_JSON if bottom_contact_design else None,
        "rese2_bottom_contact_profile": rese2_bottom_contact_profile,
        "graphene_qh_electrode_template_policy": (
            f"Read `{PROJECT_GRAPHENE_QH_SKILL_MD}` and its pre-extracted profile before applying graphene QH "
            "template, endpoint, terminal-map, pad, marker, or peripheral-electrode behavior. Do not duplicate "
            "graphene device-family defaults from this workflow payload."
            if graphene_qh_design
            else None
        ),
        "project_routing_rules": {
            "fallback_bond_pad_size_um": [PROJECT_BOND_PAD_SIZE_UM, PROJECT_BOND_PAD_SIZE_UM],
            "bond_pad_source_policy": "read the active device-family skill/profile for imported pad/template source paths, layers, and imported pad count",
            "bond_pad_fallback_policy": "generate fallback 250 um x 250 um square bond pads only when no imported pad/template source is provided or available",
            "generate_fallback_bond_pads_when_no_imported_source": True,
            "display_fill_policy": "unfilled_or_transparent_outline_only",
            "annotation_text_policy": "use_black_or_dark_text_do_not_use_white_text",
            "review_palette_policy": {
                "alignment_marker": "saturated pink/magenta outline, matching the imported alignmarker visual reference while staying distinct from contact metal",
                "bond_pad": "light blue outline/hatch-style display, matching the imported bond-pad visual reference",
                "outer_routing": "light blue outline/hatch-style display for outer routing metal, matching the bond-pad visual reference",
                "other_device_layers": "use a soft scientific-plot palette inspired by blue, amber, orange, pale teal, and green series; keep text black or dark gray",
                "fill_policy": "do not use opaque filled-color geometry; prefer transparent/outline display styles",
            },
            "bond_pad_touching_landing_width_um": 50.0,
            "bond_pad_touching_landing_scope": (
                "Only the short outer-routing landing/neck segment that directly touches or overlaps a bond pad "
                "is required to be 50 um wide. Other outer-routing segments should use "
                "net-, clearance-, and congestion-appropriate widths; do not force the entire outer route to 50 um. "
                "The pad-touching neck must extend at least 50 um into the pad, or twice the previous shallow depth, "
                "whichever is larger, and must terminate as a complete orthogonal rectangle/polygon with no missing "
                "corner, notch, chamfer, or diagonal bite."
            ),
            "synthetic_flake_shape_policy": (
                "Initial-design virtual/synthetic flakes must start from a quadrilateral base silhouette with "
                "irregular jagged/perturbed edges. Do not use ellipses, noisy circles, simple rectangles, or "
                "regular polygons as flake silhouettes."
            ),
            "feature_non_overlap_rule": (
                "Never let functionally distinct geometry unintentionally overlap. Newly drawn electrodes, gates, "
                "contacts, and routing metal must not overlap or intrude into the imported alignment-marker "
                "geometry declared by the active device-family skill or into the imported bond-pad rectangles outside their "
                "intended landing necks. Metal belonging to different, non-connected nets must not touch or "
                "overlap on the same layer, and unrelated wires must keep the minimum clearance using process, "
                "approved guidance, or active device-family spacing rules when provided. If no numeric "
                "route-to-route clearance is supplied, use 1.0 um as the provisional minimum for fan-out/routing "
                "wires and record it in result JSON; keep explicitly specified 500 nm blank lateral gaps for the "
                "ReSe2 core bottom-metal strip array. Overlap between geometry that is meant to make electrical/physical "
                "contact (e.g. a stack layer covering the material below it, a designed contact/gate overlap, or "
                "an intentional handoff stitch) is expected and not a violation; only unintended collisions are "
                "errors. Before saving or verifying, run an explicit overlap check (a pya.Region boolean AND, or "
                "equivalent) between new device geometry and the imported alignment-marker/bond-pad regions, and "
                "between same-layer geometry on different nets; also run clearance checks by sizing unrelated-net "
                "regions by half the required clearance and rejecting any intersection. Fix every violation before "
                "finishing."
            ),
            "minimum_intended_contact_area_rule": (
                "When a route/contact/gate must intentionally connect to pre-drawn geometry, bottom contacts, pads, "
                "or another metal layer, make the contact area at least 1.5 um x 1.5 um unless explicit process "
                "guidance requires a larger overlap or the active device-family skill provides a different approved value."
            ),
            "shared_project_routing_skill": PROJECT_ROUTING_SKILL_MD,
            "device_family_skill": (
                PROJECT_GRAPHENE_QH_SKILL_MD
                if graphene_qh_design
                else PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD
                if bottom_contact_design
                else None
            ),
            "device_family_profile_json": (
                PROJECT_GRAPHENE_TEMPLATE_PROFILE_JSON
                if graphene_qh_design
                else PROJECT_RESE2_BOTTOM_CONTACT_PROFILE_JSON
                if bottom_contact_design
                else None
            ),
            "device_family_profile": (
                graphene_template_profile
                if graphene_qh_design
                else rese2_bottom_contact_profile
                if bottom_contact_design
                else None
            ),
            "device_family_skill_policy": (
                "Read the matching device-family skill before filling omitted stack, thickness, terminal, gate, "
                "contact, template, or geometry defaults. Keep those device-family parameters in the skill rather "
                "than duplicating them in generated system or handoff prompts."
            ),
            "metal_layer_grouping_policy": {
                "source": "read the active device-family skill for device-specific layer grouping, routing zones, endpoint rules, and handoff geometry",
                "fallback": "if no device-family skill applies, derive contact, gate, routing, zone, and handoff layers from approved_device_guidance",
                "do_not_duplicate_device_defaults_here": True,
                "same_layer_merge_rule": "merge same-layer polygonal geometry before saving/verifying using pya.Region(...).merged() or equivalent",
                "minimum_intended_contact_area_um": [1.5, 1.5],
            },
            "review_geometry_policy": {
                "drawn_gds_policy": "Do not draw legend boxes, layer-map frames, annotation-only labels, substrate/chip frames, inset frames, scale bars, or process-session placeholder layers unless explicitly requested.",
                "metadata_policy": "Put layer meanings, clean-room order, checks, and palette information in result JSON/run notes instead of extra GDS geometry.",
                "verification": "Run checks on physical/electrical circuit geometry only.",
            },
            "execution_policy": {
                "script_shape": "prefer one monolithic KLayout build script plus at most one bounded verification/refinement pass",
                "stop_condition": "after required GDS, screenshot, and result JSON exist and checks pass, return final answer immediately",
                "no_post_output_beautification": True,
            },
            "graphene_qh_template_context": (
                {
                    "outer_electrode_template_gds": PROJECT_GRAPHENE_ELECTRODE_TEMPLATE_GDS,
                    "template_profile_json": PROJECT_GRAPHENE_TEMPLATE_PROFILE_JSON,
                    "template_profile": graphene_template_profile,
                    "source_skill": PROJECT_GRAPHENE_QH_SKILL_MD,
                }
                if graphene_qh_design
                else None
            ),
        },
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    payload_literal = json.dumps(payload_json)
    return f'''# Generated by Qumus design_agent.
# The Claude Agent SDK backend should execute this script inside KLayout via KlayoutClaw `execute_script`.
# It creates an idealized KLayout layout from approved_device_guidance before
# adapting geometry to real exfoliated flake masks/images.

import json
import math
import os
import pya

payload = json.loads({payload_literal})

layout = _layout
view = _layout_view
top = _top_cell
if layout is None or view is None or top is None:
    view, layout, top = _get_or_create_view()
layout.dbu = 0.001
top.name = {json.dumps(top_cell_name)}
_top_cell = top
if view is not None:
    try:
        view.active_cellview().cell = top
    except Exception:
        pass
dbu = layout.dbu

def um(value):
    return int(round(float(value) / dbu))

def box_um(x1, y1, x2, y2):
    return pya.Box(um(x1), um(y1), um(x2), um(y2))

def polygon_um(points):
    return pya.Polygon([pya.Point(um(x), um(y)) for x, y in points])

def layer(number, datatype=0):
    return layout.layer(int(number), int(datatype))

VISUAL_PALETTE = {{
    "alignment_marker": {{"color": 0xFF2DAA, "frame": 0xFF2DAA, "hex": "#FF2DAA", "group": "reference_marker_magenta", "transparent": True, "priority": 10}},
    "device_outline": {{"color": 0x111111, "frame": 0x111111, "hex": "#111111", "group": "review_boundary_dark", "transparent": True, "priority": 20}},
    "annotation": {{"color": 0x111111, "frame": 0x111111, "hex": "#111111", "group": "annotation_dark", "transparent": True, "priority": 90}},
    "substrate": {{"color": 0x6B7280, "frame": 0x6B7280, "hex": "#6B7280", "group": "substrate_neutral", "transparent": True, "priority": 30}},
    "dielectric": {{"color": 0xA7D4DD, "frame": 0xA7D4DD, "hex": "#A7D4DD", "group": "soft_teal_dielectric", "transparent": True, "priority": 45}},
    "channel": {{"color": 0x79BEA1, "frame": 0x79BEA1, "hex": "#79BEA1", "group": "soft_green_active_material", "transparent": True, "priority": 55}},
    "gate": {{"color": 0xFFC55D, "frame": 0xFFC55D, "hex": "#FFC55D", "group": "soft_amber_gate", "transparent": True, "priority": 60}},
    "contact": {{"color": 0xF97316, "frame": 0xF97316, "hex": "#F97316", "group": "distinct_orange_contact", "transparent": True, "priority": 70}},
    "bond_pad": {{"color": 0x6FA8FF, "frame": 0x6FA8FF, "hex": "#6FA8FF", "group": "bond_pad_light_blue", "transparent": True, "priority": 75}},
    "route": {{"color": 0x8B74FF, "frame": 0x8B74FF, "hex": "#8B74FF", "group": "inner_route_soft_violet", "transparent": True, "priority": 72}},
    "outer_routing": {{"color": 0x6FA8FF, "frame": 0x6FA8FF, "hex": "#6FA8FF", "group": "outer_routing_light_blue", "transparent": True, "priority": 74}},
    "fallback": {{"color": 0x88A6C8, "frame": 0x88A6C8, "hex": "#88A6C8", "group": "soft_blue_other", "transparent": True, "priority": 40}},
}}
REGISTERED_LAYER_STYLES = {{}}

def layer_key(number, datatype=0):
    return "%d/%d" % (int(number), int(datatype))

def apply_layer_style(number, datatype, style_key, name):
    style = VISUAL_PALETTE.get(style_key, VISUAL_PALETTE["fallback"])
    REGISTERED_LAYER_STYLES[layer_key(number, datatype)] = {{
        "style_key": style_key,
        "name": name,
        "color_hex": style["hex"],
        "frame_color_hex": "#" + ("%06X" % (style["frame"] & 0xFFFFFF)),
        "group": style["group"],
        "transparent": bool(style.get("transparent", True)),
        "priority": style.get("priority"),
    }}
    if view is None:
        return False
    try:
        lp_iter = view.begin_layers()
        while not lp_iter.at_end():
            lp = lp_iter.current()
            if lp.source_layer == int(number) and lp.source_datatype == int(datatype):
                lp.fill_color = int(style["color"])
                lp.frame_color = int(style["frame"])
                lp.transparent = bool(style.get("transparent", True))
                lp.visible = True
                try:
                    lp.name = str(name)
                except Exception:
                    pass
                view.set_layer_properties(lp_iter, lp)
                return True
            lp_iter.next()
        lp = pya.LayerProperties()
        lp.source_layer = int(number)
        lp.source_datatype = int(datatype)
        lp.fill_color = int(style["color"])
        lp.frame_color = int(style["frame"])
        lp.transparent = bool(style.get("transparent", True))
        lp.visible = True
        try:
            lp.name = str(name)
        except Exception:
            pass
        view.insert_layer(lp_iter, lp)
        return True
    except Exception:
        return False

def styled_layer(number, datatype, style_key, name):
    li = layer(number, datatype)
    apply_layer_style(number, datatype, style_key, name)
    return li

def add_box(layer_index, x1, y1, x2, y2):
    top.shapes(layer_index).insert(box_um(x1, y1, x2, y2))

def add_polygon(layer_index, points):
    top.shapes(layer_index).insert(polygon_um(points))

alignment_marker_source = payload.get("alignment_marker_gds")
graphene_template_profile = payload.get("graphene_qh_template_profile") or {{}}
alignment_marker_source_name = (
    "new_electrodes_500um_samplearea.GDS"
    if payload.get("graphene_qh_electrode_template_gds")
    else "alignmarker2.GDS"
)
alignment_marker_imported = False
alignment_marker_error = ""

def import_alignment_marker():
    global alignment_marker_imported, alignment_marker_error
    if not alignment_marker_source:
        alignment_marker_error = "No alignment marker GDS path supplied."
        return
    if not os.path.exists(alignment_marker_source):
        alignment_marker_error = "Alignment marker GDS not found: " + str(alignment_marker_source)
        return
    try:
        marker_layout = pya.Layout()
        marker_layout.read(alignment_marker_source)
        if abs(float(marker_layout.dbu) - float(layout.dbu)) > 1e-12:
            alignment_marker_error = "Alignment marker dbu mismatch; expected " + str(layout.dbu) + " got " + str(marker_layout.dbu)
            return
        src_top = marker_layout.top_cell()
        for src_li in marker_layout.layer_indices():
            info = marker_layout.get_info(src_li)
            dst_li = layout.layer(info.layer, info.datatype)
            for shape in src_top.shapes(src_li).each():
                if shape.is_box():
                    top.shapes(dst_li).insert(shape.box)
                elif shape.is_polygon():
                    top.shapes(dst_li).insert(shape.polygon)
                elif shape.is_path():
                    top.shapes(dst_li).insert(shape.path)
                elif shape.is_text():
                    top.shapes(dst_li).insert(shape.text)
        alignment_marker_imported = True
    except Exception as exc:
        alignment_marker_error = str(exc)

import_alignment_marker()

def add_zigzag_boundary(layer_index, x1, y1, x2, y2, tooth=4.0, depth=1.2):
    pts = []
    x = x1
    toggle = False
    while x <= x2:
        pts.append((x, y2 + (depth if toggle else 0.0)))
        x += tooth
        toggle = not toggle
    y = y2
    toggle = False
    while y >= y1:
        pts.append((x2 + (depth if toggle else 0.0), y))
        y -= tooth
        toggle = not toggle
    x = x2
    toggle = False
    while x >= x1:
        pts.append((x, y1 - (depth if toggle else 0.0)))
        x -= tooth
        toggle = not toggle
    y = y1
    toggle = False
    while y <= y2:
        pts.append((x1 - (depth if toggle else 0.0), y))
        y += tooth
        toggle = not toggle
    add_polygon(layer_index, pts)

def flake_like_polygon(cx, cy, width, height, variant=0):
    templates = [
        [(-0.50, -0.28), (-0.30, 0.46), (0.48, 0.32), (0.34, -0.44)],
        [(-0.47, -0.36), (-0.42, 0.34), (0.36, 0.48), (0.50, -0.18)],
        [(-0.52, -0.10), (-0.22, 0.50), (0.50, 0.22), (0.24, -0.48)],
        [(-0.44, -0.42), (-0.50, 0.18), (0.28, 0.46), (0.52, -0.24)],
    ]
    base = templates[variant % len(templates)]
    angle = (variant % 5 - 2) * 0.09
    ca = math.cos(angle)
    sa = math.sin(angle)
    pts = []
    teeth_per_edge = 4
    for i, (x0, y0) in enumerate(base):
        x1, y1 = base[(i + 1) % len(base)]
        edge_x = x1 - x0
        edge_y = y1 - y0
        norm = math.hypot(edge_x, edge_y) or 1.0
        nx = -edge_y / norm
        ny = edge_x / norm
        for j in range(teeth_per_edge):
            t = j / float(teeth_per_edge)
            x = x0 + edge_x * t
            y = y0 + edge_y * t
            amp = (0.018 + 0.008 * ((variant + i + j) % 3)) * (1 if (i + j + variant) % 2 else -1)
            x += nx * amp
            y += ny * amp
            rx = x * ca - y * sa
            ry = x * sa + y * ca
            pts.append((cx + rx * width, cy + ry * height))
    return pts

def add_flake_like_polygon(layer_index, cx, cy, width, height, variant=0):
    add_polygon(layer_index, flake_like_polygon(cx, cy, width, height, variant))

def add_label(layer_index, text, x, y, size=2.0):
    try:
        trans = pya.Trans(pya.Point(um(x), um(y)))
        label = pya.Text(str(text), trans)
        label.size = um(size)
        top.shapes(layer_index).insert(label)
    except Exception:
        pass

def add_icon(layer_index, icon, x, y):
    if icon == "contact_bar":
        add_box(layer_index, x, y + 0.8, x + 8.0, y + 2.2)
    elif icon == "cleanroom_contact_bar":
        add_box(layer_index, x, y + 0.6, x + 8.0, y + 2.4)
        add_box(layer_index, x + 1.0, y, x + 2.0, y + 3.0)
        add_box(layer_index, x + 6.0, y, x + 7.0, y + 3.0)
    elif icon == "gate_strip":
        add_box(layer_index, x, y + 1.0, x + 8.0, y + 2.0)
        add_box(layer_index, x + 3.2, y, x + 4.8, y + 3.0)
    elif icon == "zigzag_boundary":
        add_zigzag_boundary(layer_index, x, y, x + 8.0, y + 3.0, tooth=1.2, depth=0.45)
    elif icon == "dielectric_frame":
        add_box(layer_index, x, y, x + 8.0, y + 0.5)
        add_box(layer_index, x, y + 2.5, x + 8.0, y + 3.0)
        add_box(layer_index, x, y, x + 0.5, y + 3.0)
        add_box(layer_index, x + 7.5, y, x + 8.0, y + 3.0)
    elif icon == "channel_rect":
        add_box(layer_index, x + 1.0, y, x + 7.0, y + 3.0)
    elif icon == "substrate_frame":
        add_box(layer_index, x, y, x + 8.0, y + 3.0)
    else:
        add_box(layer_index, x, y, x + 8.0, y + 3.0)

def is_cleanroom_process(role, material):
    text = f"{{role}} {{material}}".lower()
    markers = (
        "contact", "electrode", "source", "drain", "metal", "metallization",
        "deposition", "lithography", "ebeam", "e-beam", "etch",
        "alignment mask", "alignment_marker", "alignment mark", "bond pad", "pad"
    )
    return any(marker in text for marker in markers)

def role_layer(role, material, index):
    text = f"{{role}} {{material}}".lower()
    if is_cleanroom_process(role, material):
        return 900 + index
    if "gate" in text:
        return 40 + index
    if "hbn" in text or "dielectric" in text or "insulator" in text or "encapsulation" in text:
        return 60 + index
    if "channel" in text or "active_region" in text or "active region" in text or "graphene" in text or "rese2" in text or "sample" in text:
        return 80 + index
    return 100 + index

def role_icon(role, material):
    text = f"{{role}} {{material}}".lower()
    if is_cleanroom_process(role, material):
        return "cleanroom_contact_bar"
    if "gate" in text:
        return "gate_strip"
    if "hbn" in text or "dielectric" in text or "insulator" in text or "encapsulation" in text:
        return "dielectric_frame"
    if "channel" in text or "active_region" in text or "active region" in text or "graphene" in text or "rese2" in text or "sample" in text:
        return "channel_rect"
    if "substrate" in text or "chip" in text:
        return "substrate_frame"
    return "swatch"

def role_style(role, material):
    text = f"{{role}} {{material}}".lower()
    if "bond pad" in text or "bond_pad" in text or text.strip() == "pad":
        return "bond_pad"
    if "outer" in text and ("route" in text or "lead" in text or "wire" in text):
        return "outer_routing"
    if "route" in text or "lead" in text or "wire" in text:
        return "route"
    if is_cleanroom_process(role, material):
        return "contact"
    if "gate" in text:
        return "gate"
    if "hbn" in text or "dielectric" in text or "insulator" in text or "encapsulation" in text:
        return "dielectric"
    if "channel" in text or "active_region" in text or "active region" in text or "graphene" in text or "rese2" in text or "sample" in text:
        return "channel"
    if "substrate" in text or "chip" in text:
        return "substrate"
    return "fallback"

def legend_entry(layer_name, role, material, icon, style_key, legend_only=False, **extra):
    style = VISUAL_PALETTE.get(style_key, VISUAL_PALETTE["fallback"])
    entry = {{
        "layer": layer_name,
        "role": role,
        "material": material,
        "icon": icon,
        "style_key": style_key,
        "color_hex": style["hex"],
        "frame_color_hex": "#" + ("%06X" % (style["frame"] & 0xFFFFFF)),
        "style_group": style["group"],
        "legend_only": legend_only,
    }}
    entry.update(extra)
    return entry

alignment_marker_layer = styled_layer(1, 0, "alignment_marker", "alignment marker / imported reference")

visual_legend = [
    legend_entry(
        "1/0", "alignment_marker", "imported alignment marker geometry", "imported_gds", "alignment_marker",
        legend_only=False, actual_circuit_layer=True, meaning="project-required imported alignment/pad/electrode geometry from " + alignment_marker_source_name,
        source=alignment_marker_source,
        imported=alignment_marker_imported,
        import_error=alignment_marker_error,
    ),
]

layers = payload.get("layers") or []
material_layers = []
cleanroom_layers = []
for idx, item in enumerate(layers, start=1):
    role = str(item.get("role") or f"layer_{{idx}}")
    material = str(item.get("material") or "unspecified")
    (cleanroom_layers if is_cleanroom_process(role, material) else material_layers).append((idx, item))
width = 110.0
height = 36.0
ideal_flake_shapes = []
for idx, item in material_layers + cleanroom_layers:
    role = str(item.get("role") or f"layer_{{idx}}")
    material = str(item.get("material") or "unspecified")
    layer_number = role_layer(role, material, idx)
    cleanroom_first = is_cleanroom_process(role, material)
    datatype = 0
    style_key = role_style(role, material)
    li = styled_layer(layer_number, datatype, style_key, f"{{role}} / {{material}}")
    icon = role_icon(role, material)
    shrink = min(42.0, idx * 4.0)
    cy = idx * 1.2
    flake_width = max(26.0, (width - shrink) * 2.0)
    flake_height = max(12.0, height + idx * 2.2)
    cx = ((idx % 3) - 1) * 2.8
    y1 = cy - flake_height / 2.0
    y2 = cy + flake_height / 2.0
    if cleanroom_first and ("contact" in (role + " " + material).lower() or "electrode" in (role + " " + material).lower() or "gate" in (role + " " + material).lower()):
        add_box(li, -width + shrink, y1, width - shrink, y2)
        geometry_shape = "actual_circuit_metal_placeholder"
    else:
        points = flake_like_polygon(cx, cy, flake_width, flake_height, idx)
        add_polygon(li, points)
        geometry_shape = "quadrilateral_based_jagged_synthetic_flake"
        ideal_flake_shapes.append({{
            "layer": f"{{layer_number}}/{{datatype}}",
            "role": role,
            "material": material,
            "synthetic": True,
            "shape": geometry_shape,
            "source": "deterministic ideal placeholder; replace with candidate_mask_paths_by_role during real-flake adaptation",
            "bbox_um": [cx - flake_width / 2.0, cy - flake_height / 2.0, cx + flake_width / 2.0, cy + flake_height / 2.0],
            "point_count": len(points),
        }})
    target = item.get("target_thickness")
    visual_legend.append(legend_entry(
        f"{{layer_number}}/{{datatype}}", role, material, icon, style_key,
        cleanroom_first=cleanroom_first,
        process_priority="front_cleanroom_operation" if cleanroom_first else "standard_device_geometry",
        target_thickness=target,
        legend_only=False,
        actual_circuit_layer=True,
        synthetic_flake_placeholder=not cleanroom_first,
        geometry_shape=geometry_shape,
        meaning=str(item.get("layout_intent") or item.get("coverage") or "approved guidance layer"),
    ))

summary = {{
    "status": "ok",
    "mode": "ideal_klayout_layout",
    "device_name": payload.get("device_name"),
    "ideal_gds": payload.get("ideal_gds_path"),
    "layers_drawn": len(layers),
        "requires_human_review": False,
        "human_review_required": False,
        "autonomous_continuation": True,
        "next_qmus_action": "continue_to_inventory_or_real_flake_preparation",
    "requires_real_flake_adaptation": True,
    "visual_legend": visual_legend,
    "ideal_flake_geometry": {{
    "mode": "quadrilateral_based_jagged_synthetic_flake_polygons",
        "real_flake_source": "candidate_mask_paths_by_role during real-flake adaptation",
        "warning": "Initial-design flake polygons are visual/geometry placeholders and must not be treated as measured real flakes.",
        "shapes": ideal_flake_shapes,
    }},
    "visual_style": {{
        "name": "qumus_review_v1",
        "purpose": "Stable high-contrast role colors for human design review screenshots.",
        "palette": VISUAL_PALETTE,
        "layer_styles": REGISTERED_LAYER_STYLES,
        "rules": [
            "Use saturated pink/magenta for imported alignment markers.",
            "Use light blue for imported bond pads and outer routing metal.",
            "Use a soft scientific-plot palette for other layers: blue/amber/orange/pale-teal/green families with black or dark-gray annotation text.",
            "Only the short outer-routing landing/neck segment that directly touches a bond pad is required to be 50 um wide; other outer-routing segments use geometry-appropriate widths.",
            "Do not draw legend-only swatches, layer-map boxes, annotation labels, frames, insets, or scale bars in GDS unless explicitly requested; keep this information in JSON/run notes.",
        ],
    }},
    "legend_region_um": None,
    "legend_policy": "No GDS legend/icon-map geometry is drawn by default; layer meanings and palette are JSON metadata.",
    "device_boundary_preference": "do_not_draw_review_boundary_unless_physical_or_explicitly_requested",
    "alignment_marker_gds": alignment_marker_source,
    "alignment_marker_imported": alignment_marker_imported,
    "alignment_marker_error": alignment_marker_error,
    "project_routing_rules": payload.get("project_routing_rules"),
    "cleanroom_front_layers": "Clean-room fabrication order is recorded as metadata on the real physical layers only. Do not create extra lithography/deposition/etch/process-mask geometry layers unless the human explicitly asks for mask deliverables.",
}}

ideal_gds = payload.get("ideal_gds_path")
if ideal_gds:
    os.makedirs(os.path.dirname(ideal_gds), exist_ok=True)
    layout.write(ideal_gds)

result_json = payload.get("result_json_path")
if result_json:
    os.makedirs(os.path.dirname(result_json), exist_ok=True)
    with open(result_json, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

_refresh_view()
result = summary
'''


def _write_ideal_klayout_script(
    *,
    script_path: str,
    task: str,
    guidance: Dict[str, object],
    workflow: Dict[str, object],
    ideal_gds_path: str,
    result_json_path: str,
) -> None:
    script = _build_ideal_klayout_script(
        task=task,
        guidance=guidance,
        workflow=workflow,
        ideal_gds_path=ideal_gds_path,
        result_json_path=result_json_path,
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)


def _write_real_flake_klayout_scaffold(
    *, script_path: str, ideal_gds_path: str, extra_context: Dict[str, object]
) -> None:
    """Write a fail-closed real-stage loader containing no synthetic geometry."""
    candidates = extra_context.get("candidate_flakes_by_role")
    script = f'''# Real-flake adaptation scaffold. Synthetic flake generation is forbidden.
import os

IDEAL_GDS = {ideal_gds_path!r}
CANDIDATES = {candidates!r}
if not os.path.exists(IDEAL_GDS):
    raise RuntimeError("Verified ideal GDS is missing; real-flake adaptation cannot start")
if not isinstance(CANDIDATES, dict) or not CANDIDATES:
    raise RuntimeError("Structured 20X real-flake candidates are missing")
for role, records in CANDIDATES.items():
    if not records or not all(str(record.get("mask_20x_path") or "").strip() for record in records):
        raise RuntimeError("Role %s lacks a mask_20x_path" % role)
_layout.read(IDEAL_GDS)
'''
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)


def _brief_lines(
    workflow: Dict[str, object],
    task: str,
    extra_context: Dict[str, object],
    klayout_mcp_url: str,
    output_contract: Dict[str, object] | None = None,
) -> List[str]:
    workflow_id = str(workflow["id"])
    required_outputs = output_contract.get("required_outputs", {}) if output_contract else {}
    ideal_script = required_outputs.get("ideal_klayout_script", "")
    design_stage = _canonical_design_stage(
        output_contract.get("design_stage") if output_contract else None
    ) or "ideal_klayout_layout"
    real_flake_stage = design_stage == "real_flake_adaptation"
    guidance = _guidance_from_context(extra_context)
    graphene_qh_design = _is_graphene_quantum_hall_design(task, guidance)
    bottom_contact_design = _is_bottom_contact_geometry_design(task, guidance)
    artifact_paths_by_role = extra_context.get("artifact_paths_by_role")
    artifacts_by_role = extra_context.get("artifacts_by_role")
    legacy_output_compatibility = extra_context.get("legacy_output_compatibility")
    legacy_output_dir = ""
    if isinstance(legacy_output_compatibility, dict):
        legacy_output_dir = str(legacy_output_compatibility.get("output_dir") or "")
    artifact_path_lines: List[str] = []
    if isinstance(artifact_paths_by_role, dict):
        for role, paths in artifact_paths_by_role.items():
            if isinstance(paths, list):
                for path in paths:
                    artifact_path_lines.append(f"- {role}: {path}")
            elif paths:
                artifact_path_lines.append(f"- {role}: {paths}")
    lines = [
        "# Qumus Claude Agent SDK / KLayout Execution Brief",
        "",
        f"Workflow: {workflow['name']} (`{workflow_id}`)",
        "",
        "Task:",
        task.strip() or "(No task text supplied.)",
        "",
        "KLayoutClaw connection:",
        f"- MCP URL: `{klayout_mcp_url}`",
        f"- Python path for KlayoutClaw subprocess tools: `{sys.executable}`",
        "- Coordinates are in microns. Keep pixel-size conversions explicit.",
        "- Prefer KLayout layers in `layer/datatype` form such as `1/0`.",
        "- Treat `approved_device_guidance` as the source of physics/fabrication constraints when present.",
        "- Do not apply fixed device templates by implication. If approved guidance explicitly names a project-provided template GDS, import and preserve that named template exactly and treat it as approved input.",
        "",
        "Required first execution:",
        "- For each build attempt, call `create_layout` exactly once before executing geometry-writing code; a corrected attempt starts with another new clean layout.",
        (
            "- Claude Agent SDK must connect to KlayoutClaw, call `create_layout` for a clean layout tab, execute the generated KLayout scaffold script, then adapt the layout to the provided real flake candidates."
            if real_flake_stage
            else "- Claude Agent SDK must connect to KlayoutClaw, call `create_layout` for a clean layout tab, and execute the generated KLayout script before inventory-specific design."
        ),
        f"- Script to execute in KLayout: `{ideal_script}`" if ideal_script else "- Script path is listed in the output contract.",
        (
            "- Use only the human-approved `selected_flakes_by_role` / `candidate_mask_paths_by_role` as the real flake geometry source. Flake selection is already complete: do not rank, choose, substitute, or drop flakes. Place the complete approved set and save final_device.gds, final_device_screenshot.png, claude_design_result.json, and stacking_handoff.json for autonomous stacking handoff."
            if real_flake_stage
            else "- Save the resulting ideal GDS and screenshot so Qumus can continue to real-flake preparation."
        ),
        *(
            [
                f"- Also write lihan/graphene-compatible auxiliary outputs in `{legacy_output_dir}`: design_parameters.json, design_result.png, initial_align_all_masks_debug.png, final_violations_visualization.png, initial_violations_visualization.png, mask/debug images for graphene, hBN, top hBN, and graphite. Keep these auxiliary files consistent with the KLayout result JSON."
            ]
            if real_flake_stage and legacy_output_dir
            else []
        ),
        (
            "- Real-flake adaptation must run a bounded but complete translation-only feasibility check over the approved set. Do not stop after one failed placement. If any placement satisfies every hard approved coverage/contact/gate constraint, write the final artifacts even when it is not aesthetically optimal. Only declare layout impossible after all bounded permitted placements fail."
            if real_flake_stage
            else "- Keep this ideal stage focused on the approved stack/contact/gate draft."
        ),
        (
            "- Do not write an ad-hoc mask selector. Qumus has already prepared each candidate's single-target `geometry_mask_path` with `scripts/prepare_graphene_real_device_context.py`; use that binary mask for geometry and keep `mask_20x_path` only as database provenance."
            if real_flake_stage
            else "- Do not perform real-flake mask extraction in this stage."
        ),
        (
            "- Return the real-flake-adapted layout output to Qumus with stacking_handoff.json so Qumus can continue to stacking when the result is successful and unblocked."
            if real_flake_stage
            else "- Return the ideal layout output to Qumus so Qumus can continue to real-flake preparation when the result is successful and unblocked."
        ),
        (
            "- Stop after the real-flake-adapted layout output exists and checks pass."
            if real_flake_stage
            else "- Stop after the ideal layout output exists and checks pass."
        ),
        *(
            [
                "",
                "Registered human/external artifacts:",
                json.dumps(
                    {
                        "artifact_paths_by_role": artifact_paths_by_role or {},
                        "artifacts_by_role": artifacts_by_role or {},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                *artifact_path_lines,
            ]
            if artifact_paths_by_role or artifacts_by_role
            else []
        ),
        "- Treat the ideal KLayout layout as the target geometry before adapting to real flake shapes or inventory constraints." if not real_flake_stage else "- Treat any prior ideal layout/result paths in context as the design intent, but fit geometry to the selected real flake masks.",
        (
            "- Design output full automation: no human approval gate is required for any successful unblocked design; write `human_review_required: false`, `review_gate: null`, `human_review_gates: []`, and return enough metadata for Qumus to continue."
            if output_contract and output_contract.get("design_output_full_automation")
            else "- Follow the output_contract / Qumus orchestration policy for continuation."
        ),
        "- In initial design, draw material flakes as deterministic synthetic flake polygons derived from a quadrilateral base outline with irregular jagged/perturbed edges. Do not use ellipses, noisy circles, simple rectangles, or regular polygons as the flake silhouette; mark these flakes as synthetic placeholders. Real flake shapes only come from `candidate_mask_paths_by_role` during real-flake adaptation.",
        f"- Read the shared project routing skill before drawing leads or pads: `{PROJECT_ROUTING_SKILL_MD}`.",
        (
            f"- This is a graphene QH / edge-contact graphene design. Read `{PROJECT_GRAPHENE_QH_SKILL_MD}` before filling omitted stack, terminal, template, contact, or geometry defaults."
            if graphene_qh_design
            else f"- If a future request names graphene QH / edge-contact graphene, read `{PROJECT_GRAPHENE_QH_SKILL_MD}` before applying that family."
        ),
        (
            f"- This is a ReSe2 / 2D dual-gate / bottom-contact transistor design. Read `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` before filling omitted stack, gate, contact, spacing, or geometry defaults."
            if bottom_contact_design
            else f"- If a future request names ReSe2 / 2D dual-gate / bottom-contact transistor, read `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` before applying that family."
        ),
        (
            "- ReSe2 outer electrode width rule: beyond the fixed inner-zone boundary, electrode/routing metal should widen with tapered or stepped transitions; use a default outer-route width of at least 2 um where space allows, while preserving same-layer different-net clearance and preventing unintended contact, overlap, or too-close spacing."
            if bottom_contact_design
            else "- Do not apply the ReSe2 outer electrode widening rule unless the 2D dual-gate transistor skill applies."
        ),
        "- Feature non-overlap rule: never let functionally distinct geometry unintentionally overlap. Newly drawn electrodes, gates, contacts, and routing metal must not overlap or intrude into imported alignment-marker geometry or imported bond-pad rectangles outside intended landing necks. Metal on different, non-connected nets must not touch or overlap on the same layer, and unrelated wires must keep positive clearance using process, approved guidance, or active device-family spacing rules when provided. Intentional contact/handoff overlaps to pre-drawn geometry, bottom contacts, pads, or another metal layer must be at least 1.5 um x 1.5 um unless approved process or device-family guidance gives a different value. Before saving or verifying, run explicit overlap checks between new device geometry and imported marker/pad regions and between unrelated electrical nets.",
        "- Default to one physical device per initial design. If approved guidance mentions arrays of gates, contacts, spacings, candidate pairs, or geometry choices, implement them inside that one device. Do not create copied full-device blocks unless approved guidance explicitly gives `physical_device_count` greater than 1 or the human requests repeated devices.",
        (
            f"- Follow `{PROJECT_GRAPHENE_QH_SKILL_MD}` for the graphene template-backed peripheral electrode policy."
            if graphene_qh_design
            else (
                f"- Follow `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for ReSe2 alignment-marker and imported pad/template source rules."
                if bottom_contact_design
                else "- Use only alignment-marker, pad, or template sources explicitly supplied by approved_device_guidance."
            )
        ),
        (
            f"- Follow `{PROJECT_GRAPHENE_QH_SKILL_MD}` for graphene local template endpoint routing."
            if graphene_qh_design
            else (
                f"- Follow `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for ReSe2 pad reuse and fallback pad rules."
                if bottom_contact_design
                else "- Follow the shared routing skill for fallback pad behavior when no imported pad/template source is supplied."
            )
        ),
        "- Use unfilled or transparent/outline display styles for layout content; do not use filled colors for device/review geometry. Use black or dark-gray annotation text and do not use white text.",
        "- Draw only actual physical/electrical circuit geometry in GDS: imported alignment markers and pads, material flakes, contact metal, gate metal, and routing metal. Do not draw layer-map boxes, review frames, substrate/chip frames, scale bars, inset frames, legend swatches, explanatory labels, annotation-only layers, or process-session placeholder layers unless the human explicitly requests documentation geometry.",
        (
            f"- Follow `{PROJECT_GRAPHENE_QH_SKILL_MD}` for graphene template-backed local routing scope."
            if graphene_qh_design
            else (
                f"- Follow `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for the ReSe2 fixed inner-zone layer grouping, inner-contact layer, inner-bottom-gate layer, shared outer-contact layer, and handoff rules."
                if bottom_contact_design
                else "- Derive metal layer grouping, routing zones, and layer handoffs from approved_device_guidance; do not borrow graphene or ReSe2 layer defaults."
            )
        ),
        *(
            [f"- Follow `{PROJECT_GRAPHENE_QH_SKILL_MD}` for graphene-only electrode layer rules."]
            if graphene_qh_design
            else []
        ),
        *(
            [f"- Follow `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for ReSe2 / 2D dual-gate transistor active-region overhang rules."]
            if bottom_contact_design
            else []
        ),
        (
            f"- Follow `{PROJECT_GRAPHENE_QH_SKILL_MD}` for whether outer-contact geometry is imported or newly drawn."
            if graphene_qh_design
            else (
                f"- Follow `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for ReSe2 shared outer-contact layer usage outside the fixed inner-zone rectangle."
                if bottom_contact_design
                else "- Derive outer-contact routing scope from approved_device_guidance; do not borrow ReSe2 fixed inner-zone defaults."
            )
        ),
        (
            f"- Follow `{PROJECT_GRAPHENE_QH_SKILL_MD}` for local metal neck behavior."
            if graphene_qh_design
            else "- Only the short outer-routing landing/neck segment that directly touches or overlaps an imported bond pad must be 50 um wide unless the human explicitly overrides this value. Other outer-routing segments are not globally constrained to 50 um. The pad-touching neck must extend at least 50 um into the pad, or twice the previous shallow depth, whichever is larger, and must terminate as a complete orthogonal rectangle/polygon with no missing corner, notch, chamfer, or diagonal bite."
        ),
        (
            f"- Merge same-layer polygonal geometry before saving or verification; follow `{PROJECT_GRAPHENE_QH_SKILL_MD}` for documenting local template endpoint overlaps."
            if graphene_qh_design
            else (
                f"- Merge same-layer polygonal geometry before saving or verification; follow `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for ReSe2 inner-contact / inner-bottom-gate to shared outer-contact stitch overlaps."
                if bottom_contact_design
                else "- Merge same-layer polygonal geometry before saving or verification. For any layer-to-layer electrical handoff required by approved_device_guidance, create and document an intentional stitch overlap."
            )
        ),
        "- Do not draw a separate device/review boundary unless it is a real physical keep-out, mesa, etch boundary, or explicitly requested documentation geometry.",
        "- Mark clean-room fabrication order on the real physical geometry layers only. Contacts, electrodes, source/drain metal, bond pads, routed metal, bottom gates, and imported alignment pads may be labelled CLEANROOM-FIRST, but do not create extra lithography/deposition/etch/process-mask layers unless the human explicitly asks for fabrication mask deliverables.",
        "- Effective device layers must exclude process-order-only metadata. For this project, CR-01/CR-02 session records belong in `cleanroom_front_layers` and run notes tied to existing physical layer records; they must not add standalone layers such as 903/90 or 904/90.",
        "- Review screenshots should show the actual device geometry only. Put layer/datatype meanings, color/palette mappings, clean-room order, and checks in `visual_legend`, `visual_style`, `cleanroom_front_layers`, and run notes, not as drawn GDS legend/frame geometry.",
        "- Before capturing any review screenshot, make the review/top design cell active and expand hierarchy so child-cell device geometry is visible. If using `CORE_DEVICE`, inset, or other child-cell instances, either flatten/copy their real geometry into the review cell before screenshot or call safe view setup such as `_layout_view.active_cellview().cell = review_cell` followed by `_layout_view.max_hier()` before the KlayoutClaw `screenshot` call. A screenshot showing only child-cell names, instance boxes, or labels is invalid even if recursive geometry checks pass.",
        "- Do not create legend, annotation, inset, scale-bar, layer-map, substrate-frame, or other review-only GDS geometry by default. Verification must use physical/electrical circuit geometry only.",
        "- Apply the stable Qumus review visual system: saturated pink/magenta imported align markers, orange contact metal, light-blue imported bond pads and outer routing metal, soft scientific-plot colors for other device layers, transparent/outline display, and black or dark-gray annotation text.",
        "- Every result JSON must include `visual_legend` entries with layer/datatype, role, material, color_hex/style_group, whether it is an actual circuit layer, plus a `visual_style` object describing the palette.",
        "- Keep execution bounded: prefer one monolithic KLayout build script plus at most one verification/refinement pass. Do not split work into many refinement files unless a single script is impossible.",
        "- Hard stop condition: once the required GDS, screenshot, and result JSON exist and verification checks pass, immediately return the final answer. Do not continue with post-output beautification, alternate screenshots, extra insets, or palette experiments.",
        (
            "- Real-flake geometry must use only the Qumus-prepared binary `geometry_mask_path` attached to each selected candidate. It is derived from the candidate's segmented 20X `mask_20x_path` by `scripts/prepare_graphene_real_device_context.py` using center-first component selection. Keep the original `mask_20x_path` unchanged in selected-flake and stacking records as database provenance; never use the legacy 100X mask and never create a second ad-hoc selector."
            if real_flake_stage
            else "- If real-flake inputs are added later, their masks must be prepared from the existing process 20X segmentation path before real design; do not use legacy 100X masks."
        ),
        (
            "- Real-flake design must also write `stacking_handoff.json` with only flake stacking movement information: one entry per selected flake, arbitrary flake count, mask/image paths, source/target centers, and translation only. Do not rotate real flake masks; all real-flake movement rotations must be 0."
            if real_flake_stage
            else "- Real-flake `stacking_handoff.json` is not required until candidate flakes have been selected."
        ),
        "- Start every geometry-writing build attempt in a newly created clean layout tab. If a build script needs correction after it inserted any geometry, call `create_layout` again before rerunning it; never append a revised build onto the prior attempt's live layout.",
        "- Use the MCP-provided `_layout`, `_layout_view`, and `_top_cell` handles directly. Do not call `_get_or_create_view()` or `layout.top_cell()` after adding or importing cells; those APIs fail when the layout has multiple top cells.",
        "- If refinement requires a fresh target, draw on `_top_cell` or create a uniquely named child cell and insert it into `_top_cell`. Do not leave refinement cells as independent top cells.",
        "- For long KLayout refinements, write the Python script to a file in the handoff directory and call `execute_script` with a short loader such as `exec(open(path).read())`. Do not send very large inline scripts in a single MCP tool call.",
        "- KLayoutClaw safe API rule: do not call `top.clear()`, `Shapes.erase`, `Shapes.clear`, `Shape.delete`, or `LayerPropertiesIterator.set_current`; these are not safe in the MCP execution context.",
        "- KLayout safe object rule: do not call geometry objects as functions. In particular, if a value is a `pya.DBox`/`DBox`, use its properties/methods directly and never write `some_dbox()`.",
        "- For layer display colors/names, use the stable Qumus review visual system. Safe `pya.LayerProperties` updates through `insert_layer` or `set_layer_properties` are allowed; do not use `LayerPropertiesIterator.set_current`.",
        "",
        "Clean-room process marking:",
        f"- {workflow.get('cleanroom_minimization')}",
        "- Do not optimize the design around a single clean-room visit unless human guidance explicitly asks for it.",
        "- Any operation requiring clean-room work must be listed before non-clean-room execution notes in run notes/result JSON and tied to existing real geometry layer records as metadata only. Do not add labels, annotations, or process-only mask geometry layers for clean-room session bookkeeping.",
        "",
        "KlayoutClaw setup:",
        "```bash",
        "/plugin marketplace add caidish/KlayoutClaw",
        "/plugin install klayoutclaw@klayoutclaw",
        f"claude mcp add --transport http klayoutclaw {klayout_mcp_url}",
        "```",
        "",
        "Claude Agent SDK KLayout execution mode:",
        "- Qumus/design_agent prepared this execution package and does not draw the final geometry itself.",
        "- The Claude Agent SDK is the active KLayout executor: read this brief, connect to KlayoutClaw, run `ideal_device_layout.py`, inspect the KLayout geometry, then refine/save outputs.",
        "- Use Qumus context as constraints, but inspect the repo and generated artifacts directly before editing layout geometry.",
        "- Do not ignore history/literature evidence in `approved_device_guidance`; surface conflicts as open issues.",
        "- Every design output must be returned to Qumus with machine-readable metadata so Qumus can continue autonomously to the next design, inventory, lab, processing, stacking, or clean-room stage.",
        "",
        "Workflow order:",
    ]
    for idx, step in enumerate(workflow["design_order"], start=1):
        lines.append(f"{idx}. {step}")

    lines.extend([
        "",
        "Required inputs to verify:",
    ])
    for item in workflow["required_inputs"]:
        lines.append(f"- {item}")

    lines.extend([
        "",
        "Expected outputs:",
    ])
    if real_flake_stage:
        expected_outputs = [
            "final_device.gds",
            "final_device_screenshot.png",
            "claude_design_result.json",
            "stacking_handoff.json",
            "autonomous stacking handoff",
        ]
    else:
        expected_outputs = [
            "ideal_device.gds",
            "ideal_device_screenshot.png",
            "ideal_device_result.json",
            "autonomous real-flake preparation",
        ]
    for item in expected_outputs:
        lines.append(f"- {item}")

    if extra_context:
        lines.extend([
            "",
            "Qumus context:",
            "- Full approved_device_guidance is in `claude_design_contract.json` under `input_context`.",
            "- This brief intentionally avoids duplicating the full JSON contract; use the contract as the single machine-readable source.",
        ])

    if output_contract:
        lines.extend([
            "",
            "Output contract:",
            "```json",
            json.dumps(output_contract, ensure_ascii=False, indent=2),
            "```",
            "",
            "When finished:",
            (
                "- Write `claude_design_result.json` and `stacking_handoff.json` after the real-flake-adapted KLayout layout is created."
                if real_flake_stage
                else "- Write `ideal_device_result.json` after the ideal KLayout layout is created."
            ),
            "- Save the real-flake-adapted layout as GDS2." if real_flake_stage else "- Save the ideal layout as GDS2.",
            "- Capture at least one KLayout screenshot for review.",
            "- Do not draw layer-map boxes, legend swatches, annotation-only labels, frames, insets, or scale bars in the GDS unless explicitly requested; include layer meanings in `visual_legend` plus `visual_style` in the result JSON.",
            "- Include `device_boundary_preference: jagged_zigzag` unless explicitly overridden.",
            "- Include `cleanroom_front_layers` or equivalent JSON metadata listing every CLEANROOM-FIRST real physical layer/datatype and operation. Do not list process-order-only annotation/session records as effective device layers.",
            "- Mark successful, unblocked design results with `status: ok`, `autonomous_continuation: true`, `human_review_required: false`, `review_gate: null`, and `human_review_gates: []`. If required net mapping, pad assignment, process approval, or geometry constraints are unresolved, mark `status: blocked`, list the blocking `open_issues`, and set `autonomous_continuation: false`; do not create a human review gate.",
            "- Return control to Qumus with enough metadata to continue to the next planned stage.",
            (
                "- Copy `selected_flakes` exactly from the human-approved selection, keyed by approved material role as lists. Record placement translations, but do not make a new flake choice."
                if real_flake_stage
                else "- Later, write `claude_design_result.json` after real-flake adaptation is complete, then return control to Qumus according to the output contract."
            ),
            (
                "- In `stacking_handoff.json`, set `machine_ready=false` and list missing movement fields if any selected flake lacks source center, target center, or translation data. Do not include chip-corner, pickup, or workflow-specific payload fields in this design output."
                if real_flake_stage
                else "- Later, write `stacking_handoff.json` after real-flake adaptation if stacking execution will follow."
            ),
            "- Leave a short run note describing assumptions, clean-room process layers, and unresolved issues.",
        ])

    lines.extend([
        "",
        "Recommended KLayoutClaw tools:",
        "- `create_layout` for a clean design tab.",
        (
            "- `execute_script` to load the verified ideal GDS through `real_flake_layout.py`, then adapt it using only structured 20X masks."
            if real_flake_stage
            else "- `execute_script` to run `ideal_device_layout.py` or refined pya code."
        ),
        (
            f"- For this graphene QH template-backed run, read `{PROJECT_GRAPHENE_QH_SKILL_MD}` and use its template profile before probing the template GDS."
            if graphene_qh_design
            else "- For project-specific routing, read `agents/design_agent/skills/qumus-project-routing/SKILL.md` before drawing leads."
        ),
        (
            f"- For this graphene QH template-backed run, follow `{PROJECT_GRAPHENE_QH_SKILL_MD}` for imported peripheral circuit and auto-routing policy."
            if graphene_qh_design
            else "- For graphene QH runs with the provided electrode template, reuse/import that template as the outer circuit rather than planning new peripheral fan-out."
        ),
        (
            "- Avoid `auto_route` and `route_inspect` for this graphene template-backed initial design unless the imported template is missing or explicitly overridden."
            if graphene_qh_design
            else "- `auto_route` and `route_inspect` for contact-to-pad routing."
        ),
        f"- `evaluate_design` for DRC-like scoring only if called with `python_path: {sys.executable}`.",
        "- `screenshot` and `save_layout` for review artifacts. After required artifacts are written and checks pass, stop immediately.",
    ])
    return lines


def build_claude_klayout_handoff(
    task: str,
    scenario: str = "",
    handoff_dir: str = "",
    extra_context_json: str = "",
    klayout_mcp_url: str = "http://127.0.0.1:8765/mcp",
) -> Dict[str, object]:
    """Create a Claude Agent SDK execution package for KLayout design."""
    extra_context = _normalize_real_flake_context(
        _merge_embedded_task_context(task, _parse_extra_context(extra_context_json))
    )
    guidance = _guidance_from_context(extra_context)
    device_material_family = _device_material_family(task, guidance)
    extra_context = _filter_candidate_roles_for_family(extra_context, device_material_family)
    guidance = _guidance_from_context(extra_context)
    graphene_qh_design = _is_graphene_quantum_hall_design(task, guidance)
    graphene_device_automation = _is_graphene_device_design(task, guidance)
    bottom_contact_design = _is_bottom_contact_geometry_design(task, guidance)
    workflow_id = normalize_design_scenario(scenario, task)
    workflow = get_workflow_definition(workflow_id, task=task, extra_context=extra_context)
    # The design stage is authoritative and comes from structured signals only:
    #   * an explicit real-flake `scenario` / `workflow_id` slug,
    #   * an explicit `design_stage` in extra_context, or
    #   * real 20X candidate records attached to extra_context (mask paths /
    #     flake records / a resolved design_candidate_payload).
    # Free-form task prose is never consulted, so an ideal-stage request that
    # merely names the later real-flake step ("real-flake adaptation comes in a
    # later stage") stays in the ideal branch.
    real_flake_requested = (
        _labels_request_real_flake(scenario, workflow_id)
        or _canonical_design_stage(extra_context.get("design_stage")) == "real_flake_adaptation"
        or _has_real_flake_candidate_inputs(extra_context)
    )
    if real_flake_requested and (
        extra_context.get("_flake_selection_approval_verified") is not True
        or not (
            _role_mapping_has_mask_paths(extra_context.get("candidate_mask_paths_by_role"))
            and _role_mapping_has_mask_paths(extra_context.get("candidate_flakes_by_role"))
        )
        or not _guidance_from_context(extra_context)
    ):
        extra_context = _normalize_real_flake_context(
            _auto_resolve_real_flake_context(extra_context)
        )
        # Recovery can add the approved project guidance that identifies the
        # device family.  Do not keep the classification calculated from the
        # sparse pre-recovery request (for example, a request containing only
        # a project/gate id and ``real_flake_adaptation``).  A stale empty
        # family here used to skip the verified graphene-QH default and fail
        # with missing ideal_device artifacts until the user retried with more
        # descriptive task text.
        guidance = _guidance_from_context(extra_context)
        device_material_family = _device_material_family(task, guidance)
        extra_context = _filter_candidate_roles_for_family(
            extra_context, device_material_family
        )
        guidance = _guidance_from_context(extra_context)
        graphene_qh_design = _is_graphene_quantum_hall_design(task, guidance)
        graphene_device_automation = _is_graphene_device_design(task, guidance)
        bottom_contact_design = _is_bottom_contact_geometry_design(task, guidance)
        workflow = get_workflow_definition(workflow_id, task=task, extra_context=extra_context)
    has_real_flakes = _role_mapping_has_mask_paths(extra_context.get("candidate_mask_paths_by_role")) and (
        _role_mapping_has_mask_paths(extra_context.get("candidate_flakes_by_role"))
    )
    if real_flake_requested and not has_real_flakes:
        raise ValueError(
            "Real-flake adaptation was requested, but no usable 20X candidates were found after "
            "recovering task/additional_args context and querying project artifacts plus the live material database. "
            "Refusing to fall back to ideal/synthetic flake generation."
        )
    if real_flake_requested:
        missing_mask_files: List[str] = []
        role_map = extra_context.get("candidate_mask_paths_by_role")
        if isinstance(role_map, dict):
            for role, values in role_map.items():
                paths = values if isinstance(values, list) else [values]
                usable = [str(path) for path in paths if path and os.path.isfile(str(path))]
                if not usable:
                    missing_mask_files.append(str(role))
        if missing_mask_files:
            raise FileNotFoundError(
                "Real-flake roles have no readable 20X mask file after automatic recovery: "
                + ", ".join(sorted(missing_mask_files))
            )
    is_real_flake_stage = real_flake_requested or has_real_flakes
    design_stage = "real_flake_adaptation" if is_real_flake_stage else "ideal_klayout_layout"
    workflow = dict(workflow)
    workflow["handoff_outputs"] = (
        [
            "final_device.gds from Claude Agent SDK after real flake adaptation",
            "final_device_screenshot.png from Claude Agent SDK after real flake adaptation",
            "claude_design_result.json from Claude Agent SDK",
            "stacking_handoff.json from Claude Agent SDK",
            "autonomous continuation to stacking after real-flake/final design",
            "claude_run_notes.md from Claude Agent SDK",
        ]
        if design_stage == "real_flake_adaptation"
        else [
            "CLAUDE_START_HERE.md",
            "claude_design_contract.json",
            "ideal_device_layout.py",
            "ideal_device.gds from Claude Agent SDK/KLayout",
            "ideal_device_screenshot.png from Claude Agent SDK/KLayout",
            "ideal_device_result.json from Claude Agent SDK",
            "autonomous continuation to inventory/real-flake preparation after ideal KLayout design",
        ]
    )

    if handoff_dir:
        output_dir = _resolve_symbolic_run_handoff(handoff_dir, design_stage)
    else:
        output_dir = os.path.join(_default_output_dir(), "claude_klayout_handoff")
    numeric_run_name = next(
        (part for part in Path(output_dir).parts if re.fullmatch(r"run_\d+", part, re.IGNORECASE)),
        "",
    )
    if numeric_run_name:
        task = str(_replace_symbolic_run_references(task, numeric_run_name))
        extra_context = dict(_replace_symbolic_run_references(extra_context, numeric_run_name))
        guidance = _guidance_from_context(extra_context)
    os.makedirs(output_dir, exist_ok=True)
    if design_stage == "real_flake_adaptation":
        if (
            extra_context.get("flake_selection_human_approved") is not True
            or extra_context.get("_flake_selection_approval_verified") is not True
        ):
            detail = str(extra_context.get("_flake_selection_approval_error") or "").strip()
            raise ValueError(
                "Real-flake design requires a verified persisted human-approved flake selection "
                "context at the root of extra_context_json."
                + (f" Approval verification failed: {detail}." if detail else "")
            )
        selected_by_role = extra_context.get("selected_flakes_by_role")
        if not isinstance(selected_by_role, dict) or not selected_by_role:
            raise ValueError("Real-flake design received no approved selected_flakes_by_role set.")
        if any(not isinstance(records, list) or not records for records in selected_by_role.values()):
            raise ValueError("Every approved flake role must contain a non-empty list.")
        extra_context = _prepare_center_priority_geometry_masks(extra_context, output_dir)
        guidance = _guidance_from_context(extra_context)
    guidance_validation = validate_device_guidance_for_layout(guidance, task=task)

    script_path = os.path.join(
        output_dir,
        "real_flake_layout.py" if design_stage == "real_flake_adaptation" else "ideal_device_layout.py",
    )
    ideal_gds_path = os.path.join(output_dir, "ideal_device.gds")
    ideal_screenshot_path = os.path.join(output_dir, "ideal_device_screenshot.png")
    ideal_result_path = os.path.join(output_dir, "ideal_device_result.json")
    final_gds_path = os.path.join(output_dir, "final_device.gds")
    final_screenshot_path = os.path.join(output_dir, "final_device_screenshot.png")
    result_json_path = os.path.join(output_dir, "claude_design_result.json")
    stacking_handoff_path = os.path.join(output_dir, "stacking_handoff.json")
    run_notes_path = os.path.join(output_dir, "claude_run_notes.md")
    if design_stage == "real_flake_adaptation":
        if not os.path.exists(ideal_gds_path) or not os.path.exists(ideal_result_path):
            # The materializer performs its own strict QH/default-geometry and
            # artifact-hash eligibility checks.  Calling it whenever the real
            # stage lacks its local ideal inputs makes this boundary
            # self-healing and avoids relying on an earlier cached classifier.
            _materialize_default_graphene_qh_ideal(
                task=task,
                guidance=guidance,
                extra_context=extra_context,
                output_dir=output_dir,
            )
        if not os.path.exists(ideal_gds_path) or not os.path.exists(ideal_result_path):
            raise FileNotFoundError(
                "Verified ideal_device.gds and ideal_device_result.json are required before real-flake adaptation."
            )
        required_outputs = {
            "real_flake_klayout_script": script_path,
            "final_gds": final_gds_path,
            "review_screenshot": final_screenshot_path,
            "result_json": result_json_path,
            "stacking_handoff": stacking_handoff_path,
            "run_notes": run_notes_path,
        }
        result_json_schema = {
            "status": "ok | blocked",
            "workflow_id": workflow["id"],
            "final_gds": "absolute path to final GDS after real flake/artifact adaptation",
            "review_screenshot": "absolute path to final screenshot",
            "run_notes": "absolute path to notes",
            "autonomous_continuation": "true only when status is ok and required artifacts/movement fields are ready for the next Qumus stage; false when blocked",
            "human_review_required": "false",
            "selected_flakes": "exact human-approved selected_flakes_by_role mapping; each role maps to a list and design must not choose or substitute flakes",
            "flake_fit_failure_report": "required object only when bounded placement proves layout_impossible=true; identifies failed roles, violated hard constraints, tested bounds, best achieved margins, and replacement requirements",
            "stacking_handoff": "absolute path to stacking_handoff.json when real flakes were selected",
            "flake_stacking_movements": (
                "list of one movement record per selected real flake, arbitrary flake count, translation only; "
                "no rotation is allowed during real-flake adaptation"
            ),
            "cleanroom_session_count": "integer count or null if no clean-room work is required",
            "visual_legend": "list of objects mapping actual GDS layer/datatype to role, material, color/style, actual_circuit_layer status, and clean-room metadata",
            "visual_style": "object containing the stable Qumus review palette, role-to-color system, and applied layer_styles",
            "legend_region_um": "null unless the human explicitly requested drawn documentation geometry",
            "device_boundary_preference": "jagged_zigzag unless explicitly overridden by human guidance",
            "cleanroom_front_layers": "list or description of CLEANROOM-FIRST real physical layer/datatype pairs plus operation metadata; process-order-only lithography/deposition/etch session notes must not create extra effective device layers",
            "assumptions": "list of assumptions",
            "open_issues": "list of unresolved issues",
        }
    else:
        required_outputs = {
            "ideal_klayout_script": script_path,
            "ideal_gds": ideal_gds_path,
            "ideal_screenshot": ideal_screenshot_path,
            "ideal_layout_result_json": ideal_result_path,
        }
        result_json_schema = {
            "status": "ok | blocked",
            "workflow_id": workflow["id"],
            "ideal_gds": "absolute path to ideal GDS generated in KLayout",
            "ideal_screenshot": "absolute path to ideal KLayout screenshot",
            "autonomous_continuation": "true only when status is ok and required artifacts are ready for the next Qumus stage; false when blocked",
            "human_review_required": "false",
            "visual_legend": "list of objects mapping actual GDS layer/datatype to role, material, color/style, actual_circuit_layer status, and clean-room metadata",
            "visual_style": "object containing the stable Qumus review palette, role-to-color system, and applied layer_styles",
            "ideal_flake_geometry": "object describing quadrilateral-based jagged synthetic flake polygons used in initial design and warning that real flake geometry comes from candidate masks",
            "legend_region_um": "null unless the human explicitly requested drawn documentation geometry",
            "device_boundary_preference": "jagged_zigzag unless explicitly overridden by human guidance",
            "cleanroom_front_layers": "list or description of CLEANROOM-FIRST real physical layer/datatype pairs plus operation metadata; process-order-only lithography/deposition/etch session notes must not create extra effective device layers",
            "assumptions": "list of assumptions",
            "open_issues": "list of unresolved issues",
        }

    device_profile_context = None
    if graphene_qh_design:
        device_profile_context = {
            "outer_electrode_template_gds": PROJECT_GRAPHENE_ELECTRODE_TEMPLATE_GDS,
            "template_profile_json": PROJECT_GRAPHENE_TEMPLATE_PROFILE_JSON,
            "template_profile": _load_project_json(PROJECT_GRAPHENE_TEMPLATE_PROFILE_JSON),
            "default_ideal_design_manifest": PROJECT_GRAPHENE_QH_DEFAULT_IDEAL_JSON,
            "default_ideal_design": _load_project_json(PROJECT_GRAPHENE_QH_DEFAULT_IDEAL_JSON),
            "device_family_skill": PROJECT_GRAPHENE_QH_SKILL_MD,
            "usage_policy": (
                "This profile is a graphene QH skill reference. Read and apply the device-family skill before "
                "using profile fields; do not treat workflow code as the owner of graphene template rules."
            ),
        }
    elif bottom_contact_design:
        device_profile_context = {
            "profile_json": PROJECT_RESE2_BOTTOM_CONTACT_PROFILE_JSON,
            "profile": _load_project_json(PROJECT_RESE2_BOTTOM_CONTACT_PROFILE_JSON),
            "device_family_skill": PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD,
            "usage_policy": (
                "Use this pre-extracted ReSe2 / 2D dual-gate bottom-contact profile for canonical layer map, "
                "alignment-marker/pad source, material bboxes, gate/contact dimensions, routing zones, and "
                "verification checks before consulting historical generation records."
            ),
        }

    if design_stage == "real_flake_adaptation":
        _write_real_flake_klayout_scaffold(
            script_path=script_path,
            ideal_gds_path=ideal_gds_path,
            extra_context=extra_context,
        )
    else:
        _write_ideal_klayout_script(
            script_path=script_path,
            task=task,
            guidance=guidance,
            workflow=workflow,
            ideal_gds_path=ideal_gds_path,
            result_json_path=ideal_result_path,
        )

    human_review_gates: List[object] = []

    output_contract = {
        "mode": "claude_agent_sdk_klayout_execution",
        "design_stage": design_stage,
        "workflow_id": workflow["id"],
        "scenario": workflow["id"],
        "guidance_validation": guidance_validation,
        "required_outputs": required_outputs,
        "graphene_device_full_automation": graphene_device_automation,
        "verified_default_ideal_design_supported": graphene_qh_design,
        "default_ideal_design_manifest": (
            PROJECT_GRAPHENE_QH_DEFAULT_IDEAL_JSON if graphene_qh_design else None
        ),
        "design_output_full_automation": True,
        "human_review_required": False,
        "review_gate": None,
        "design_review_gate": None,
        "human_review_gates": human_review_gates,
        "design_review_policy": "design_output_full_automation_no_human_gate",
        "next_qmus_action_after_design": "continue_to_inventory_or_real_flake_preparation",
        "autonomous_continuation_stages": [
            {
                "stage": "ideal_klayout_layout",
                "trigger": "after the Claude Agent SDK backend returns ideal_device_result.json / ideal_device.gds / ideal_device_screenshot.png",
                "required": True,
                "qmus_action": "continue to inventory/material acquisition or real-flake preparation",
            },
            {
                "stage": "real_flake_or_final_layout",
                "trigger": "after the Claude Agent SDK backend returns claude_design_result.json, final GDS, or any intermediate design-stage result",
                "required": True,
                "qmus_action": "continue to stacking-payload resolution and Qumus-supported stacking",
            },
        ],
        "result_json_schema": result_json_schema,
        "stacking_handoff_json_schema": {
            "status": "ready_for_stacking | blocked",
            "device_name": "device name from approved guidance",
            "design_stage": "real_flake_adaptation",
            "final_gds": "absolute path to final_device.gds",
            "review_screenshot": "absolute path to final_device_screenshot.png",
            "flake_stacking_movements": [
                {
                    "order": "integer physical device layer order. If stack_order_direction is bottom_to_top, Qumus will reverse it for real dry-transfer pickup.",
                    "role": "approved material role, e.g. channel/top_encapsulation/bottom_gate",
                    "material": "material label",
                    "flake_id": "database flake id or null",
                    "chip_id": "database chip id or null",
                    "mask_path": "absolute path to selected mask",
                    "image_path": "absolute path to selected optical image when available",
                    "source_center": "[x, y] unscaled database flake.location_x/location_y stage coordinates in um",
                    "source_center_frame": "stage_coordinates_um (flake.location_x / location_y)",
                    "target_center_um": "[x, y] target center in KLayout/device um",
                    "translation_um": "[dx, dy] planned KLayout/device-frame translation from nominal/source placement",
                    "translation_px": "[dx, dy] optional mask/image-pixel translation if computed",
                    "rotation_deg": 0,
                    "rotation_allowed": False,
                    "selection_reason": "short reason this flake was chosen",
                }
            ],
            "stack_order_direction": "bottom_to_top for physical device layer order, or top_to_bottom only when flake_stacking_movements is already written in real pickup order",
            "coordinate_frames": {
                "mask_pixel_size_um_per_px": "numeric pixel size used for mask conversion",
                "klayout_units": "um",
                "stage_units": "um",
                "transform_notes": "how mask/image pixels and KLayout um translations relate; real-flake rotation is forbidden and must remain 0",
            },
            "machine_ready": "true only if every selected flake has source_center, target_center_um, and translation_um",
            "missing_for_stacking": "list of movement fields still missing; do not list or require chip-corner/pickup fields in this design output",
            "autonomous_continuation": "true only when status is ok, machine_ready is true, and no blocking open_issues remain",
            "human_review_required": False,
            "review_gate": None,
        },
    }

    contract = {
        "status": "ready_for_claude_agent_sdk_klayout_execution",
        "mode": "claude_agent_sdk_klayout_execution",
        "workflow_id": workflow["id"],
        "scenario": workflow["id"],
        # Keep the stage available at the handoff root as well as inside the
        # output contract.  This makes the stage unambiguous for resumed or
        # older handoffs whose nested contract may be stale.
        "design_stage": design_stage,
        "graphene_device_full_automation": graphene_device_automation,
        "verified_default_ideal_design_supported": graphene_qh_design,
        "default_ideal_design_manifest": (
            PROJECT_GRAPHENE_QH_DEFAULT_IDEAL_JSON if graphene_qh_design else None
        ),
        "design_output_full_automation": True,
        "task": task,
        "repo_root": os.getcwd(),
        "handoff_dir": output_dir,
        "klayout_mcp_url": klayout_mcp_url,
        "python_path": sys.executable,
        "ideal_klayout_script": script_path,
        "project_template_context": device_profile_context,
        "device_profile_context": device_profile_context,
        "workflow": workflow,
        "input_context": extra_context,
        "guidance_validation": guidance_validation,
        "output_contract": output_contract,
        "operator_steps": [
            "Open this repository in the configured Claude Agent SDK environment.",
            "Install/enable KlayoutClaw if needed.",
            f"Connect the Claude Agent SDK backend to KlayoutClaw MCP at {klayout_mcp_url}.",
            f"Run the SDK backend so it reads CLAUDE_START_HERE.md and executes {script_path} in KLayout through KlayoutClaw.",
            (
                "After Claude Agent SDK creates final_device.gds, final_device_screenshot.png, claude_design_result.json, and stacking_handoff.json, return the result to Qumus for autonomous stacking-payload resolution."
                if design_stage == "real_flake_adaptation"
                else "After Claude Agent SDK creates ideal_device.gds, ideal_device_screenshot.png, and ideal_device_result.json, return the result to Qumus for real-flake preparation."
            ),
        ],
    }

    contract_path = os.path.join(output_dir, "claude_design_contract.json")
    start_path = os.path.join(output_dir, "CLAUDE_START_HERE.md")

    lines = _brief_lines(workflow, task, extra_context, klayout_mcp_url, output_contract=output_contract)
    with open(start_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    with open(contract_path, "w", encoding="utf-8") as f:
        json.dump(contract, f, ensure_ascii=False, indent=2)

    contract["start_here"] = start_path
    contract["contract_path"] = contract_path
    return contract


def _claude_sdk_prompt(payload: Dict[str, object]) -> str:
    """Build the prompt passed to the Claude Agent SDK backend for direct design execution."""
    output_contract = payload.get("output_contract")
    design_stage = _design_stage_from_payload(payload)
    real_flake_stage = design_stage == "real_flake_adaptation"
    input_context = payload.get("input_context")
    guidance = {}
    legacy_output_dir = ""
    if isinstance(input_context, dict):
        maybe_guidance = input_context.get("approved_device_guidance") or input_context.get("device_guidance")
        if isinstance(maybe_guidance, dict):
            guidance = maybe_guidance
        legacy_output_compatibility = input_context.get("legacy_output_compatibility")
        if isinstance(legacy_output_compatibility, dict):
            legacy_output_dir = str(legacy_output_compatibility.get("output_dir") or "")
    graphene_qh_design = _is_graphene_quantum_hall_design(str(payload.get("task") or ""), guidance)
    bottom_contact_design = _is_bottom_contact_geometry_design(str(payload.get("task") or ""), guidance)
    device_skill_step = f" Read the shared routing skill `{PROJECT_ROUTING_SKILL_MD}` before drawing leads or pads."
    mandatory_profile_read_step = ""
    device_profile_context_note = "Not applicable for this run."
    if graphene_qh_design:
        device_skill_step += f" Read the graphene QH device skill `{PROJECT_GRAPHENE_QH_SKILL_MD}` before filling omitted stack, terminal, template, contact, or geometry defaults."
        mandatory_profile_read_step = (
            f" Before any `create_layout`, `execute_script`, generated KLayout code execution, or layout file write, "
            f"if the contract, skill, or device profile context names a template/profile reference file, call the Claude Code `Read` tool on that exact file path and use it as the authoritative source instead of re-inspecting prior generated outputs. "
            f"For this graphene QH run, the expected template profile path is `{PROJECT_GRAPHENE_TEMPLATE_PROFILE_JSON}`. If no reference file is named or available for a future device family, inspect the supplied template/artifacts directly and record that no profile reference was provided."
        )
        device_profile_context_note = (
            f"For graphene QH template-backed runs, read the graphene QH skill. If it or the contract names a template profile JSON, directly read "
            f"`{PROJECT_GRAPHENE_TEMPLATE_PROFILE_JSON}` and use it before applying template behavior; only inspect raw template artifacts when no profile reference is provided."
        )
    if bottom_contact_design:
        device_skill_step += f" Read the 2D dual-gate transistor skill `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` before filling omitted stack, gate, contact, spacing, or geometry defaults."
        mandatory_profile_read_step = (
            f" Before any `create_layout`, `execute_script`, generated KLayout code execution, or layout file write, "
            f"if the contract, skill, or device profile context names a template/profile reference file, call the Claude Code `Read` tool on that exact file path and use it as the authoritative source instead of re-inspecting prior generated outputs. "
            f"For this ReSe2 / 2D dual-gate bottom-contact run, the expected profile path is `{PROJECT_RESE2_BOTTOM_CONTACT_PROFILE_JSON}`. "
            f"Do not rely only on embedded `device_profile_context.profile` or `project_template_context.profile`. If no reference file is named or available for a future device family, inspect the supplied template/artifacts directly and record that no profile reference was provided."
        )
        device_profile_context_note = (
            f"For ReSe2 / 2D dual-gate transistor runs, read the 2D dual-gate transistor skill. If it or the contract names a device profile JSON, directly read "
            f"`{PROJECT_RESE2_BOTTOM_CONTACT_PROFILE_JSON}` and use it before applying device-family behavior; only inspect raw template artifacts when no profile reference is provided."
        )
    feature_overlap_step = (
        " Feature non-overlap and clearance rule: never let functionally distinct geometry unintentionally overlap, touch, coincide, or come closer than the minimum clearance. Newly drawn electrodes, gates, contacts, and routing metal must not overlap or intrude into the imported alignment-marker geometry or the imported bond-pad rectangles outside their intended landing necks, and metal on different, non-connected nets must not touch or overlap on the same layer. Unrelated wires must keep the clearance from process, approved guidance, or active device-family spacing rules when provided. If no numeric route-to-route clearance is supplied, use 1.0 um as the provisional minimum for fan-out/routing wires and record it in result JSON; keep explicitly specified 500 nm blank lateral gaps for the ReSe2 core bottom-metal strip array. Overlap that is part of the intended stack (a layer covering the material below it, a designed contact/gate overlap, or an intentional handoff stitch) is expected and not a violation; intentional contact/handoff overlaps to pre-drawn geometry, bottom contacts, pads, or another metal layer must be at least 1.5 um x 1.5 um unless approved process or device-family guidance gives a different value. Run explicit pya.Region overlap checks and clearance checks by sizing unrelated-net regions by half the required clearance and rejecting intersections before saving or verifying. Fix every unintended overlap or clearance violation."
    )
    graphene_layer_merge_step = f" Follow `{PROJECT_GRAPHENE_QH_SKILL_MD}` for graphene-specific electrode layer policy." if graphene_qh_design else ""
    stage_title = (
        "Run the real-flake-adapted KLayout design stage through the Claude Agent SDK backend."
        if real_flake_stage
        else "Run the first KLayout design stage through the Claude Agent SDK backend."
    )
    stage_steps = (
        f"""3. Adapt the approved ideal design intent to the selected real flake masks. Complete all required skill/profile reads before touching KLayout, then execute the generated KLayout scaffold script once. The input must have `flake_selection_human_approved=true`. Use the complete lists in `selected_flakes_by_role` exactly as approved; never rank, choose, substitute, omit, or add a flake.{device_skill_step}{mandatory_profile_read_step}{feature_overlap_step}{graphene_layer_merge_step}
4. Adapt the approved ideal design intent using every approved flake's Qumus-prepared binary `geometry_mask_path`. It contains exactly one center-priority component. Keep the original segmented 20X `mask_20x_path` only for database identity/provenance and stacking records. Optical quality was judged upstream and is not a design-agent selection task. Never use a legacy 100X path or re-extract colour classes yourself.
5. Run a bounded translation-only feasibility search for the approved set. Check every hard coverage, contact, gate, channel, encapsulation, landing, clearance, and fabrication constraint. A failed first placement, lower score, small safety margin that still passes, or aesthetic concern is not layout impossibility.
6. Qumus prepares `geometry_mask_path` before this stage with the existing `scripts/prepare_graphene_real_device_context.py` center-first selector. Do not create or run another segmentation, colour-class, luminance, database-area, or mask-component selection script in the design agent.
7. Do not create or run open-ended standalone optimization loops in /tmp. Mask polygon conversion must consume the prepared single-target geometry mask directly.
8. If you produce an intermediate placement file such as placement.json, immediately write final_device.gds, final_device_screenshot.png, claude_design_result.json, stacking_handoff.json, and claude_run_notes.md in the handoff directory before doing any more exploration.
9. Once coverage/contact/gate checks pass, your next assistant action must be a KlayoutClaw tool call that draws, saves, or screenshots the final layout. Do not spend another turn explaining that you are about to draw.
10. Keep the final drawing script compact. Prefer one monolithic KLayout build script plus at most one bounded verification/refinement pass. Draw only actual physical/electrical circuit geometry: selected flake masks/materials, contacts, gates, routes, imported markers, imported pads, and required keep-outs.
11. Do not draw layer-map boxes, review frames, substrate/chip frames, scale bars, inset frames, legend swatches, explanatory labels, annotation-only layers, or process-session placeholder layers unless the human explicitly requests documentation geometry.
12. Put all clean-room ordering information in metadata/run notes and on existing physical layer records only. Do not add standalone lithography/deposition/etch/process-mask geometry layers unless the human explicitly requests mask deliverables.
13. Ensure the final review screenshot contains the adapted device geometry only; preserve the stable Qumus review palette with saturated pink/magenta align markers, orange contact metal, light-blue bond pads/outer routing, soft scientific-plot colors for other device layers, no filled geometry, and no white text. Include layer meanings and palette in visual_style/visual_legend JSON, not as drawn GDS legend geometry.
14. Before capturing the final review screenshot, make the review/top design cell active and expand hierarchy. If the design uses child-cell instances for the core device, insets, or repeated geometry, either flatten/copy their real geometry into the screenshot review cell or call safe view setup such as _layout_view.active_cellview().cell = review_cell followed by _layout_view.max_hier() before KlayoutClaw screenshot. A screenshot showing only child-cell names, instance boxes, or labels is invalid even if recursive geometry checks pass.
15. Save final_device.gds, final_device_screenshot.png, claude_design_result.json, and stacking_handoff.json paths listed in the contract.
16. If the design is successful and unblocked, mark the result with status=ok, autonomous_continuation=true, human_review_required=false, review_gate=null, and human_review_gates=[]. If the exact approved flake set cannot satisfy the hard layout constraints, declare this only after the bounded feasibility search is exhausted. Then write the same structured `flake_fit_failure_report` object into `claude_design_result.json` and `{os.path.join(str(payload.get('handoff_dir') or '.'), 'flake_fit_failure_report.json')}` with `layout_impossible=true`, `retry_selection_required=true`, `exfoliation_required=false`; `attempt_summary={{"bounded_search_exhausted": true, "tested_translation_count": positive integer, "translation_bounds_um": ..., "constraints_checked": [...]}}`; and `failed_roles` entries containing role, non-empty `failed_flakes` records with flake_id and mask_20x_path, `violated_hard_constraints`, best achieved coverage/margins, why translation-only placement cannot fit, and `replacement_requirements`. Set status=blocked and autonomous_continuation=false. Do not request exfoliation yourself: Qumus must first ask the selector to try remaining inventory. Runtime/tool/net/pad failures are design issues and must not set layout_impossible or trigger flake replacement.
17. Real-flake adaptation may translate selected masks only. Do not rotate any real flake mask or output any nonzero rotation. Every movement record must have rotation_deg=0 and rotation_allowed=false.
18. Copy selected_flakes from the approved `selected_flakes_by_role` without making a choice. Keep each role as a list and preserve the approved 20X mask/image path, flake_id, chip_id, source center, quality assessment, and selection reason; add translation fields only.
19. Include `flake_stacking_movements` in both claude_design_result.json and stacking_handoff.json. It must be a list with one entry per selected flake, for arbitrary flake count, not just flake_a/flake_b. Each entry must contain order, role, material, flake_id, chip_id, the selected 20X mask path, image_path, source_center, source_center_frame="stage_coordinates_um (flake.location_x / location_y)", target_center_um, translation_um, optional translation_px, rotation_deg=0, rotation_allowed=false, and selection_reason. Copy source_center values without scaling from candidate location_x/location_y. Never emit source_center_nm, translation_nm, location_x_nm, or location_y_nm. Use `stack_order_direction="bottom_to_top"` when the list is physical device layer order. Real dry-transfer pickup is top-to-bottom; Qumus/lab_manager reverses bottom-to-top layer order before assigning processing slots A/B/C/D.
20. {f"Also write lihan/graphene-compatible auxiliary outputs in `{legacy_output_dir}`: design_parameters.json, design_result.png, initial_align_all_masks_debug.png, initial_violations_visualization.png, final_violations_visualization.png, mask_graphene.png, mask_hBN.png, mask_hBN_top.png, mask_graphite.png, aligned_graphene_debug.png, aligned_hBN_debug.png, aligned_top_hBN_debug.png, and aligned_graphite_debug.png. Keep the transformations in design_parameters.json consistent with stacking_handoff.json and force all rotations to 0." if legacy_output_dir else "No legacy auxiliary output directory was provided."}
21. Do not include workflow-specific `assembly_device_payload`, legacy flake_a/flake_b handoff objects, chip-corner coordinates, pickup coordinates, bottom-electrode coordinates, or other stacking-tool-specific payloads in design outputs. Qumus/lab_manager can resolve those later from selected 20X mask paths if needed.
22. Include visual_legend in claude_design_result.json with layer/datatype, role, material, color_hex/style_group, actual_circuit_layer status, and CLEANROOM-FIRST metadata. Do not count clean-room session metadata as effective device layers.
23. Write stacking_handoff.json with flake_stacking_movements, stack_order_direction, coordinate_frames, selected flake ids/chip ids/20X mask/image paths, source centers, target centers, translation_um, machine_ready, missing_for_stacking, autonomous_continuation set by status, and human_review_required set by status.
24. If source center, target center, or translation is missing for any selected flake, set machine_ready=false and list those movement fields instead of inventing coordinates.
25. Stop immediately after the real-flake-adapted design output exists and checks pass. Do not continue to processing, stacking, clean-room work, extra beautification, alternate screenshots, or palette experiments."""
        if real_flake_stage
        else f"""3. Complete all required skill/profile reads before touching KLayout, then execute the generated ideal KLayout script and refine the initial layout only if needed to satisfy approved guidance. Prefer one monolithic KLayout build script plus at most one bounded verification/refinement pass.{device_skill_step}{mandatory_profile_read_step}{feature_overlap_step}{graphene_layer_merge_step}
4. Do not draw a separate device/review outline unless it is a real physical keep-out, mesa, etch boundary, or human-requested documentation geometry.
5. Keep initial material flakes as synthetic flake polygons derived from a quadrilateral base outline with irregular jagged/perturbed edges; do not use ellipses, noisy circles, simple rectangles, or regular polygons as the flake silhouette. Record that they are placeholders until real candidate masks are supplied.
6. {"Follow the graphene QH device skill for template-profile and peripheral-electrode policy." if graphene_qh_design else f"Follow the 2D dual-gate transistor skill `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for ReSe2 alignment-marker and imported pad/template source rules." if bottom_contact_design else "Use only alignment-marker, pad, or template sources explicitly supplied by approved_device_guidance."}
7. {"Follow the graphene QH device skill for local endpoint routing and pad/template reuse rules." if graphene_qh_design else f"Follow the 2D dual-gate transistor skill `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for ReSe2 pad reuse and fallback pad rules." if bottom_contact_design else "Follow the shared routing skill for fallback pad behavior when no imported pad/template source is supplied."}
8. Use unfilled or transparent/outline display styles for all layout content; do not use filled colors for device/review geometry. Use black or dark-gray annotation text and do not use white text.
9. {"Follow the graphene QH device skill for local routing scope and electrode-layer policy." if graphene_qh_design else f"Follow the 2D dual-gate transistor skill `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for the ReSe2 fixed inner-zone layer grouping, inner-contact layer, inner-bottom-gate layer, shared outer-contact layer, and handoff rules." if bottom_contact_design else "Derive metal layer grouping, routing zones, and layer handoffs from approved_device_guidance; do not borrow graphene or ReSe2 layer defaults."}
10. {"Follow the 2D dual-gate transistor skill for ReSe2 / bottom-contact active-region overhang rules." if bottom_contact_design else "Do not apply ReSe2-only bottom-gate/source-drain overhang rules unless the matching device skill applies."}
11. {"Follow the 2D dual-gate transistor skill for graphite pickup landing placement." if bottom_contact_design else "Do not add the ReSe2 left graphite contact landing unless the matching device skill applies."}
12. {"Follow the 2D dual-gate transistor skill for graphite pickup and top-hBN access/notch behavior." if bottom_contact_design else "Do not apply the ReSe2 graphite pickup/top-hBN notch rule unless the matching device skill applies."}
13. {"Follow the graphene QH device skill for local metal neck behavior." if graphene_qh_design else "For ReSe2 / bottom-contact designs, make electrode/routing metal wider in the outer part beyond the fixed inner-zone boundary than the central active-region fingers/gates, using tapered or stepped widening with a default outer-route width of at least 2 um where space allows; do not widen in a way that creates same-layer different-net contact, overlap, or clearance violations. Make only the short outer-routing landing/neck segment that directly touches or overlaps an imported bond pad 50 um wide unless the human explicitly overrides this value. Other outer-routing segments are not globally constrained to 50 um. The pad-touching neck must extend at least 50 um into the pad, or twice the previous shallow depth, whichever is larger, and must terminate as a complete orthogonal rectangle/polygon with no missing corner, notch, chamfer, or diagonal bite."}
14. {"Merge same-layer polygonal geometry before saving or verification; follow the graphene QH device skill for documenting local template endpoint overlaps." if graphene_qh_design else f"Merge same-layer polygonal geometry before saving or verification; follow the 2D dual-gate transistor skill `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for ReSe2 inner-contact / inner-bottom-gate to shared outer-contact stitch overlaps." if bottom_contact_design else "Merge same-layer polygonal geometry before saving or verification. For any layer-to-layer electrical handoff required by approved_device_guidance, create and document an intentional stitch overlap."}
15. Put all clean-room ordering information in metadata/run notes and on existing real physical layer records only. Do not create standalone lithography/deposition/etch/process-mask geometry layers unless the human explicitly requests mask deliverables.
16. Draw only actual physical/electrical circuit geometry in GDS: imported alignment markers and pads, material flakes, contact metal, gate metal, routing metal, and explicitly required real keep-outs. Do not draw layer-map boxes, review frames, substrate/chip frames, scale bars, inset frames, legend swatches, explanatory labels, annotation-only layers, or process-session placeholder layers unless explicitly requested.
17. Before capturing the ideal review screenshot, make the review/top design cell active and expand hierarchy. If the design uses child-cell instances for the core device or repeated geometry, either flatten/copy their real geometry into the screenshot review cell or call safe view setup such as _layout_view.active_cellview().cell = review_cell followed by _layout_view.max_hier() before KlayoutClaw screenshot. A screenshot showing only child-cell names, instance boxes, or labels is invalid even if recursive geometry checks pass.
18. Save the ideal GDS, screenshot, and ideal_device_result.json paths listed in the contract.
19. If the design is successful and unblocked, mark the result with status=ok, autonomous_continuation=true, human_review_required=false, review_gate=null, and human_review_gates=[]. If required net mapping, pad assignment, process approval, or geometry constraints are unresolved, mark status=blocked, list the blocking open_issues, and set autonomous_continuation=false; do not create a human review gate.
20. Include visual_legend in ideal_device_result.json with layer/datatype, role, material, color_hex/style_group, actual_circuit_layer status, and CLEANROOM-FIRST metadata. Do not count clean-room session metadata as effective device layers.
21. Stop immediately after the ideal GDS, screenshot, and ideal_device_result.json exist and checks pass. Do not continue to inventory, exfoliation, flake adaptation, processing, stacking, clean-room work, extra beautification, alternate screenshots, or palette experiments."""
    )
    return f"""You are the Qumus Design Agent backend.

{stage_title}

Repository root:
{payload.get("repo_root")}

Read this execution brief first:
{payload.get("start_here")}

Design contract:
{payload.get("contract_path")}

Device profile context:
{device_profile_context_note}

Generated KLayout script to execute through KlayoutClaw:
{payload.get("ideal_klayout_script")}

KlayoutClaw MCP URL:
{payload.get("klayout_mcp_url")}

Python path for KlayoutClaw subprocess tools:
{payload.get("python_path")}

Required behavior:
1. Inspect the brief and contract.
2. If the contract, skill, or Device profile context names a template/profile reference file, perform that profile JSON `Read` tool call before connecting to KlayoutClaw/KLayout or executing generated code. If no such reference is named or available, inspect the supplied template/artifacts directly and record that no profile reference was provided.
{stage_steps}

KlayoutClaw tool notes:
- Call create_layout before execute_script so the generated script draws into a clean layout tab.
- Use the KlayoutClaw MCP tools directly. Do not use Bash/curl/helper scripts to probe or call the MCP server.
- Start every geometry-writing build attempt with create_layout on a new clean layout tab. If a script is edited after inserting geometry, create another clean layout before retrying; never accumulate old and revised real-flake geometry in one layout.
- Prefer one monolithic KLayout build script plus at most one bounded verification/refinement pass. Avoid multi-part refinement files and cross-execute_script shared state.
- Hard stop condition: once the required design-stage GDS, screenshot, and result JSON exist and checks pass, immediately return the final answer. Do not keep polishing display style or generating extra review furniture.
- Default to one physical device per initial design. If approved guidance mentions arrays of gates, contacts, spacings, candidate pairs, or geometry choices, implement them inside that one device. Do not create copied full-device blocks unless approved guidance explicitly gives physical_device_count greater than 1 or the human requests repeated devices.
- Use the MCP-provided _layout, _layout_view, and _top_cell handles directly. Do not call _get_or_create_view() or layout.top_cell() after adding or importing cells; those APIs fail when the layout has multiple top cells.
- If refinement requires a fresh target, draw on _top_cell or create a uniquely named child cell and insert it into _top_cell. Do not leave refinement cells as independent top cells.
- For long KLayout refinements, write the Python script to a file in the handoff directory and call execute_script with a short loader such as exec(open(path).read()). Do not send very large inline scripts in a single MCP tool call.
- Do not call top.clear(), Shapes.erase, Shapes.clear, Shape.delete, or LayerPropertiesIterator.set_current; these commonly fail in KlayoutClaw/KLayout MCP execution.
- Do not call geometry objects as functions. In particular, if a value is a pya.DBox/DBox, use its properties/methods directly and never write some_dbox().
- Avoid GUI layer-property mutation during scripted layout generation. Put layer meaning in labels, layer/datatype numbers, result JSON, and run notes.
- Do not draw legend/icon maps, layer frames, substrate frames, annotation-only text, inset frames, scale bars, or process placeholder layers in GDS unless explicitly requested. The reviewer should get layer meanings from visual_legend/visual_style JSON and run notes.
- Before calling KlayoutClaw screenshot, make the review/top design cell active and expand hierarchy. If the design uses child-cell instances for the core device, insets, or repeated geometry, either flatten/copy their real geometry into the screenshot review cell or call safe view setup such as _layout_view.active_cellview().cell = review_cell followed by _layout_view.max_hier(). A screenshot showing only child-cell names, instance boxes, or labels is invalid even if recursive geometry checks pass.
- {"Real-flake stage: synthetic, placeholder, idealized, or procedurally generated flake outlines are forbidden. Every material-flake polygon must be derived from a selected mask_20x_path in candidate_flakes_by_role." if real_flake_stage else "In initial design, use synthetic flake polygons derived from a quadrilateral base outline with irregular jagged/perturbed edges; do not use ellipses, noisy circles, simple rectangles, or regular polygons as the flake silhouette. Record ideal_flake_geometry and do not present those shapes as measured flakes."}
- {"Read and apply the 2D dual-gate transistor skill for ReSe2 / bottom-contact graphite pickup rules." if bottom_contact_design else "Ignore ReSe2-only graphite pickup defaults unless the 2D dual-gate transistor skill applies."}
- Preserve the stable Qumus review visual style using outlines/transparent layer display rather than filled-color geometry. Use saturated pink/magenta for imported align markers, orange for contact metal, light blue for imported bond pads and outer routing metal, and soft scientific-plot colors for other device layers. Use black or dark-gray annotation text and do not use white text. Safe LayerProperties updates with insert_layer/set_layer_properties are allowed; do not use LayerPropertiesIterator.set_current.
- Draw only actual physical/electrical circuit geometry in GDS. Put layer meanings, clean-room order, checks, and palette information in JSON/run notes, not extra review-only GDS geometry. Verification must use physical/electrical circuit geometry only.
- {"Read and apply the graphene QH device skill for graphene template-backed routing/template policy." if graphene_qh_design else "For project-specific metal lead routing, read agents/design_agent/skills/qumus-project-routing/SKILL.md before drawing leads."}
- {"Follow the graphene QH skill for graphene template/pad behavior." if graphene_qh_design else f"Follow the 2D dual-gate transistor skill `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for ReSe2 alignment-marker, imported pad/template source, pad reuse, and fallback pad behavior." if bottom_contact_design else "Use only alignment-marker, pad, or template sources explicitly supplied by approved_device_guidance; follow the shared routing skill for fallback pad behavior when no imported pad/template source is supplied."}
- {"Follow the graphene QH skill for local sample-area connection rules." if graphene_qh_design else f"Follow the 2D dual-gate transistor skill `{PROJECT_2D_DUAL_GATE_TRANSISTOR_SKILL_MD}` for the ReSe2 fixed inner-zone layer grouping, inner-contact layer, inner-bottom-gate layer, shared outer-contact layer, pad-neck behavior, and handoff stitch rules." if bottom_contact_design else "Derive metal layer grouping, routing zones, pad-neck behavior, and layer handoffs from approved_device_guidance; do not borrow graphene or ReSe2 layer defaults."}
- Do not draw a separate device/review outline unless it is a real physical keep-out, mesa, etch boundary, or explicitly requested documentation geometry.
- Put clean-room process order at the front of the metadata/run notes, use visually distinct CLEANROOM-FIRST labels on existing real geometry layers, and record those physical layer/datatype pairs in result JSON. Do not add process-only mask/session geometry layers such as CR-01/CR-02 field masks unless explicitly requested.
- Do not call evaluate_design unless you pass python_path exactly as listed above; otherwise KlayoutClaw may fail while searching for conda.sh.
"""


def _required_artifact_keys_for_stage(payload: Dict[str, object]) -> List[str]:
    if _design_stage_from_payload(payload) == "real_flake_adaptation":
        return ["final_gds", "review_screenshot", "result_json", "stacking_handoff"]
    return ["ideal_gds", "ideal_screenshot", "ideal_layout_result_json"]


def _incomplete_guidance_result(
    *,
    payload: Dict[str, object],
    result_path: str,
    mode: str,
    prompt_path: str,
    klayout_mcp_url: str,
    extra: Dict[str, object] | None = None,
) -> Dict[str, object]:
    validation = payload.get("guidance_validation")
    required_outputs = payload["output_contract"]["required_outputs"]  # type: ignore[index]
    output_contract = payload.get("output_contract")
    graphene_device_automation = bool(
        output_contract.get("graphene_device_full_automation")
        if isinstance(output_contract, dict)
        else payload.get("graphene_device_full_automation")
    )
    run_result: Dict[str, object] = {
        "status": "blocked",
        "mode": mode,
        "workflow_id": payload["workflow_id"],
        "handoff_dir": payload["handoff_dir"],
        "start_here": payload["start_here"],
        "contract_path": payload["contract_path"],
        "ideal_klayout_script": payload["ideal_klayout_script"],
        "prompt_path": prompt_path,
        "klayout_mcp_url": klayout_mcp_url,
        "human_review_required": False,
        "autonomous_continuation": False,
        "review_gate": None,
        "human_review_gates": [],
        "next_qmus_action": "regenerate_complete_device_guidance_before_layout",
        "design_review_policy": "no_human_review_gate_blocked_for_missing_guidance",
        "guidance_validation": validation,
        "message": (
            "Approved device guidance is incomplete for real Claude/KLayout execution. "
            "Provide an ordered stack/material role definition plus contact/gate constraints, "
            "or rerun with allow_incomplete_guidance only for a placeholder debug draft."
        ),
        "artifacts": {
            key: {
                "path": path,
                "exists": os.path.exists(str(path)),
            }
            for key, path in required_outputs.items()
        },
    }
    if extra:
        run_result.update(extra)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(run_result, f, ensure_ascii=False, indent=2)
    return run_result


def _result_json_is_reviewable(path: str, handoff_payload: Dict[str, object] | None = None) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            result_payload = json.load(f)
    except Exception:
        return False
    if not isinstance(result_payload, dict):
        return False
    status = str(result_payload.get("status") or "").strip().lower()
    if status in {"blocked", "error", "failed", "timeout", "incomplete"}:
        return False
    if handoff_payload is not None and _design_stage_from_payload(handoff_payload) == "real_flake_adaptation":
        result_stage = _canonical_design_stage(
            result_payload.get("design_stage") or result_payload.get("stage")
        )
        if result_stage != "real_flake_adaptation":
            return False
        if result_payload.get("ideal_flake_geometry"):
            return False
        if bool(result_payload.get("synthetic_fallback")):
            return False
        selected = result_payload.get("selected_flakes")
        movements = result_payload.get("flake_stacking_movements")
        if not selected or not isinstance(movements, list) or not movements:
            return False
        input_context = handoff_payload.get("input_context")
        candidate_map = (
            input_context.get("candidate_mask_paths_by_role")
            if isinstance(input_context, dict)
            else None
        )
        if not isinstance(candidate_map, dict) or not candidate_map:
            return False
        allowed_by_role = {
            str(role): {
                os.path.normcase(os.path.abspath(str(candidate_path)))
                for candidate_path in (paths if isinstance(paths, list) else [paths])
                if candidate_path
            }
            for role, paths in candidate_map.items()
        }
        expected_role_masks = {
            (role, candidate_path)
            for role, paths in allowed_by_role.items()
            for candidate_path in paths
        }
        movement_role_masks: set[tuple[str, str]] = set()
        for movement in movements:
            if not isinstance(movement, dict):
                return False
            if any(key in movement for key in ("source_center_nm", "translation_nm")):
                return False
            if movement.get("source_center_frame") != "stage_coordinates_um (flake.location_x / location_y)":
                return False
            if not isinstance(movement.get("source_center"), (list, tuple)):
                return False
            role = str(movement.get("role") or "")
            # ``mask_path`` is the established stacking-handoff field and is
            # valid only when it exactly equals one of the approved 20X mask
            # paths.  Prefer the explicit alias when present, but do not reject
            # an otherwise identical machine-ready handoff for using the
            # compatibility field name.
            mask_path = str(
                movement.get("mask_20x_path") or movement.get("mask_path") or ""
            )
            normalized_mask = os.path.normcase(os.path.abspath(mask_path)) if mask_path else ""
            if (
                not role
                or role not in allowed_by_role
                or normalized_mask not in allowed_by_role[role]
                or not os.path.isfile(mask_path)
            ):
                return False
            if float(movement.get("rotation_deg") or 0) != 0:
                return False
            movement_role_masks.add((role, normalized_mask))
        if len(movements) != len(expected_role_masks) or movement_role_masks != expected_role_masks:
            return False
        if not isinstance(selected, dict) or set(str(role) for role in selected) != set(allowed_by_role):
            return False
        for role, selected_records in selected.items():
            if not isinstance(selected_records, list):
                return False
            selected_masks = {
                os.path.normcase(
                    os.path.abspath(str(record.get("mask_20x_path") or record.get("mask_path") or ""))
                )
                for record in selected_records
                if isinstance(record, dict)
            }
            if selected_masks != allowed_by_role.get(str(role), set()):
                return False
            if any(
                not isinstance(record, dict) or bool(record.get("synthetic_fallback"))
                for record in selected_records
            ):
                return False
    return True


def _required_artifacts_ready(
    payload: Dict[str, object],
    required_outputs: Dict[str, object],
    since_ts: float = 0.0,
) -> bool:
    if _design_stage_from_payload(payload) == "real_flake_adaptation":
        result_path = str(required_outputs.get("result_json") or "")
        if result_path and os.path.exists(result_path):
            if not since_ts or os.path.getmtime(result_path) >= since_ts:
                failure = _validated_flake_fit_failure(result_path)
                report_path = os.path.join(os.path.dirname(result_path), "flake_fit_failure_report.json")
                if failure is not None and _failure_report_file_matches(report_path, failure):
                    return True
    required_keys = _required_artifact_keys_for_stage(payload)
    for key in required_keys:
        path = str(required_outputs.get(key) or "")
        if not path or not os.path.exists(path):
            return False
        # Revision/resume runs start with previous artifacts still on disk. Only
        # treat artifacts as ready when this SDK run has rewritten them.
        if since_ts and os.path.getmtime(path) < since_ts:
            return False
    result_key = "result_json" if "result_json" in required_keys else "ideal_layout_result_json"
    result_path = str(required_outputs.get(result_key) or "")
    return _result_json_is_reviewable(result_path, payload)


def _existing_successful_real_flake_design(
    payload: Dict[str, object],
    required_outputs: Dict[str, object],
) -> Dict[str, object] | None:
    """Reuse a matching, machine-ready real-flake result without another SDK run.

    This is deliberately stricter than checking file existence: the current
    persisted human approval must be verified, result candidates must exactly
    match that approved set, and the stacking handoff must already be marked
    machine-ready with no missing fields.
    """
    if _design_stage_from_payload(payload) != "real_flake_adaptation":
        return None
    input_context = payload.get("input_context")
    if not isinstance(input_context, dict):
        return None
    if input_context.get("_flake_selection_approval_verified") is not True:
        return None
    if input_context.get("force_real_flake_redesign") is True or input_context.get(
        "force_design_rerun"
    ) is True:
        return None
    if not _required_artifacts_ready(payload, required_outputs):
        return None

    result_path = str(required_outputs.get("result_json") or "")
    handoff_path = str(required_outputs.get("stacking_handoff") or "")
    try:
        with open(result_path, "r", encoding="utf-8") as handle:
            design_result = json.load(handle)
        with open(handoff_path, "r", encoding="utf-8") as handle:
            stacking_handoff = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(design_result, dict) or not isinstance(stacking_handoff, dict):
        return None
    if str(design_result.get("status") or "").lower() != "ok":
        return None
    if design_result.get("autonomous_continuation") is not True:
        return None
    if design_result.get("human_review_required") is not False:
        return None
    if str(stacking_handoff.get("status") or "").lower() != "ready_for_stacking":
        return None
    if stacking_handoff.get("machine_ready") is not True:
        return None
    if stacking_handoff.get("missing_for_stacking") not in (None, []):
        return None
    if stacking_handoff.get("human_review_required") is not False:
        return None

    expected_gate = str(input_context.get("flake_selection_gate_id") or "")
    result_gate = str(
        design_result.get("flake_selection_gate_id")
        or design_result.get("flake_selection_gate")
        or ""
    )
    if result_gate:
        try:
            if HumanGateService.canonical_gate_id(result_gate) != HumanGateService.canonical_gate_id(
                expected_gate
            ):
                return None
        except ValueError:
            return None

    return {
        "status": "ok",
        "mode": "verified_existing_real_flake_design_reuse",
        "message": (
            "A verified machine-ready design already matches the persisted human-approved "
            "flake set; skipped duplicate Claude Agent SDK execution."
        ),
        "workflow_id": payload.get("workflow_id"),
        "handoff_dir": payload.get("handoff_dir"),
        "reused_existing_real_flake_design": True,
        "claude_sdk_executed": False,
        "source_design_result": result_path,
        "source_stacking_handoff": handoff_path,
        "artifacts": {
            key: {"path": path, "exists": os.path.exists(str(path))}
            for key, path in required_outputs.items()
        },
        "human_review_required": False,
        "review_gate": None,
        "human_review_gates": [],
        "design_review_policy": "verified_existing_result_no_human_gate",
        "autonomous_continuation": True,
        "next_qmus_action": "continue_to_stacking_payload_resolution_and_stacking",
        "elapsed_seconds": 0.0,
    }


def _validated_flake_fit_failure(result_path: str) -> Dict[str, object] | None:
    """Return only a design-proven hard layout impossibility, never a soft preference."""
    try:
        with open(result_path, "r", encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(result, dict) or str(result.get("status") or "").lower() != "blocked":
        return None
    report = result.get("flake_fit_failure_report")
    if not isinstance(report, dict):
        return None
    attempts = report.get("attempt_summary")
    failed_roles = report.get("failed_roles")
    if (
        report.get("layout_impossible") is not True
        or report.get("retry_selection_required") is not True
        or report.get("exfoliation_required") is not False
        or not isinstance(failed_roles, list)
        or not failed_roles
        or not isinstance(attempts, dict)
        or attempts.get("bounded_search_exhausted") is not True
        or not isinstance(attempts.get("tested_translation_count"), int)
        or isinstance(attempts.get("tested_translation_count"), bool)
        or int(attempts["tested_translation_count"]) <= 0
    ):
        return None
    for failure in failed_roles:
        if not isinstance(failure, dict):
            return None
        if (
            not failure.get("role")
            or not isinstance(failure.get("failed_flakes"), list)
            or not failure.get("failed_flakes")
            or not isinstance(failure.get("violated_hard_constraints"), list)
            or not failure.get("violated_hard_constraints")
        ):
            return None
        if any(
            not isinstance(flake, dict)
            or (
                flake.get("flake_id") is None
                and not (flake.get("mask_20x_path") or flake.get("mask_path"))
            )
            for flake in failure["failed_flakes"]
        ):
            return None
        if (
            not isinstance(failure.get("replacement_requirements"), list)
            or not failure.get("replacement_requirements")
        ):
            return None
    return report


def _failure_report_file_matches(report_path: str, expected: Dict[str, object]) -> bool:
    try:
        with open(report_path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(value, dict) and value == expected


def _missing_required_artifacts(payload: Dict[str, object], required_outputs: Dict[str, object]) -> List[str]:
    return [
        key
        for key in _required_artifact_keys_for_stage(payload)
        if not os.path.exists(str(required_outputs.get(key) or ""))
    ]


def _message_to_record(message: object) -> Dict[str, object]:
    """Serialize a Claude Agent SDK message without depending on exact SDK classes."""
    def _trim(value: object, limit: int = 12000) -> object:
        if isinstance(value, str) and len(value) > limit:
            return value[:limit] + f"... [truncated {len(value) - limit} chars]"
        if isinstance(value, list):
            return [_trim(item, limit) for item in value]
        if isinstance(value, dict):
            return {str(key): _trim(item, limit) for key, item in value.items()}
        return value

    record: Dict[str, object] = {
        "type": message.__class__.__name__,
        "repr": _trim(repr(message), 12000),
    }
    if dataclasses.is_dataclass(message):
        try:
            data = dataclasses.asdict(message)
            record["data"] = _trim(data, 12000)
        except Exception:
            pass

    for attr in ("result", "subtype", "session_id", "total_cost_usd", "data"):
        if hasattr(message, attr):
            try:
                record[attr] = _trim(getattr(message, attr), 12000)
            except Exception:
                pass

    text_parts: List[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if text:
                text_parts.append(str(text))
            name = getattr(block, "name", None)
            if name:
                record.setdefault("tool_calls", []).append(str(name))  # type: ignore[union-attr]
    if text_parts:
        record["text"] = "\n".join(text_parts)
    return record


def _stream_claude_sdk_record(record: Dict[str, object]) -> None:
    if os.getenv("QUMUS_STREAM_CLAUDE_SDK_LOGS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    prefix = "[CLAUDE_SDK][design]"
    record_type = str(record.get("type") or "message")
    if record.get("tool_calls"):
        print(f"{prefix}[tool] {', '.join(str(item) for item in record['tool_calls'])}", flush=True)
    text = str(record.get("text") or record.get("result") or "").strip()
    if text:
        max_chars = 2000
        shown = text if len(text) <= max_chars else text[:max_chars].rstrip() + " ... [truncated]"
        print(f"{prefix}[{record_type}] {shown}", flush=True)
    elif record_type not in {"AssistantMessage"}:
        subtype = record.get("subtype")
        suffix = f" subtype={subtype}" if subtype else ""
        print(f"{prefix}[{record_type}]{suffix}", flush=True)


def _claude_sdk_allowed_tools(extra_allowed_tools: str = "") -> List[str]:
    tools = [
        "Read",
        "Write",
        "Glob",
        "Grep",
        "mcp__klayoutclaw__ping",
        "mcp__klayoutclaw__create_layout",
        "mcp__klayoutclaw__execute_script",
        "mcp__klayoutclaw__get_layout_info",
        "mcp__klayoutclaw__save_layout",
        "mcp__klayoutclaw__screenshot",
        "mcp__klayoutclaw__auto_route",
        "mcp__klayoutclaw__route_inspect",
        "mcp__klayoutclaw__close_layout_view",
        "mcp__klayoutclaw__validate_pixel_size",
    ]
    for item in (extra_allowed_tools or "").split(","):
        text = item.strip()
        if text and text not in tools:
            tools.append(text)
    return tools


def _extract_latest_session_id(messages: List[Dict[str, object]]) -> str:
    session_id = ""
    for message in messages:
        candidate = message.get("session_id")
        if candidate:
            session_id = str(candidate)
            continue
        data = message.get("data")
        if isinstance(data, dict) and data.get("session_id"):
            session_id = str(data["session_id"])
    return session_id


def _extract_latest_session_id_from_stream_log(stream_log_path: str) -> str:
    if not stream_log_path or not os.path.exists(stream_log_path):
        return ""
    session_id = ""
    try:
        with open(stream_log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                candidate = _extract_latest_session_id([record])
                if candidate:
                    session_id = candidate
    except OSError:
        return ""
    return session_id


def _resolve_claude_sdk_resume_id(resume_session_id: str = "", resume_from_run_result: str = "") -> str:
    explicit = (resume_session_id or "").strip()
    if explicit:
        return explicit
    path_text = (resume_from_run_result or "").strip()
    if not path_text:
        return ""
    with open(path_text, "r", encoding="utf-8") as f:
        previous = json.load(f)
    for key in ("claude_sdk_session_id", "session_id", "resume_session_id"):
        value = previous.get(key)
        if value:
            return str(value)
    messages = previous.get("messages")
    if isinstance(messages, list):
        return _extract_latest_session_id([m for m in messages if isinstance(m, dict)])
    return ""


def _resume_context_from_run_result(resume_from_run_result: str) -> str:
    """Recover prior handoff inputs for a direct Claude SDK continuation."""
    if not resume_from_run_result:
        return ""
    resume_path = Path(resume_from_run_result)
    # A clean stage restart intentionally has no prior design contract. Its
    # checkpoint is therefore the authoritative structured input source.
    try:
        with resume_path.open("r", encoding="utf-8") as handle:
            resume_payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        resume_payload = {}
    if isinstance(resume_payload, dict):
        checkpoint_context = resume_payload.get("input_context")
        if isinstance(checkpoint_context, dict):
            recovered = dict(checkpoint_context)
            recovered.setdefault("design_stage", resume_payload.get("design_stage"))
            recovered.setdefault("workflow_plan_stage", resume_payload.get("design_stage"))
            return json.dumps(recovered, ensure_ascii=False)

    contract_path = resume_path.parent / "claude_design_contract.json"
    if not contract_path.exists():
        return ""
    try:
        with contract_path.open("r", encoding="utf-8") as handle:
            contract = json.load(handle)
    except (OSError, ValueError, TypeError):
        return ""
    context = contract.get("input_context")
    if not isinstance(context, dict):
        return ""
    recovered = dict(context)
    recovered.setdefault("design_stage", contract.get("design_stage"))
    recovered.setdefault("workflow_plan_stage", contract.get("design_stage"))
    return json.dumps(recovered, ensure_ascii=False)


async def _run_claude_agent_sdk_async(
    *,
    prompt: str,
    klayout_mcp_url: str,
    allowed_tools: List[str],
    permission_mode: str,
    model: str,
    timeout_seconds: int,
    resume_session_id: str = "",
    stream_log_path: str = "",
    stop_artifact_payload: Dict[str, object] | None = None,
    stop_artifact_outputs: Dict[str, object] | None = None,
) -> Dict[str, object]:
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = ClaudeAgentOptions(
        model=(model or DEFAULT_CLAUDE_SDK_MODEL),
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        resume=resume_session_id or None,
        mcp_servers={
            "klayoutclaw": {
                "type": "http",
                "url": klayout_mcp_url,
            }
        },
        strict_mcp_config=True,
        disallowed_tools=[
            "Bash",
            "ListAgents",
            "SendMessage",
            "Task",
            "TaskOutput",
            "TaskStop",
            "Skill",
            "WebSearch",
            "WebFetch",
            "Monitor",
            "ScheduleWakeup",
            "CronCreate",
            "CronDelete",
            "CronList",
            "PushNotification",
            "EnterWorktree",
            "ExitWorktree",
            "DesignSync",
            "ToolSearch",
        ],
        system_prompt=(
            "You are the Qumus design backend. You operate KLayout through "
            "KlayoutClaw MCP and produce device-layout artifacts with enough "
            "machine-readable metadata for Qumus to continue autonomously. "
            "Use one monolithic KLayout build script plus at most one bounded "
            "verification/refinement pass when possible. Do not rely on state "
            "persisting across execute_script calls. Once the required GDS, "
            "screenshot, and result JSON exist and checks pass, return the final "
            "answer immediately; do not keep polishing display style or generating "
            "extra review furniture. Before review screenshots, activate the review "
            "cell and expand hierarchy or flatten child-cell geometry so the image "
            "shows real device shapes, not just instance labels or boxes. Draw only "
            "actual physical/electrical circuit geometry in GDS; put layer legends, "
            "process notes, and checks in JSON/run notes instead of drawing review "
            "frames, labels, insets, scale bars, or legend swatches. Use SDK tools "
            "only."
        ),
    )

    records: List[Dict[str, object]] = []
    stopped_after_artifacts = False
    run_start_ts = time.time()

    async def collect() -> None:
        nonlocal stopped_after_artifacts
        stream = query(prompt=prompt, options=options)
        if inspect.isawaitable(stream):
            stream = await stream
        async for message in stream:
            record = _message_to_record(message)
            records.append(record)
            if stream_log_path:
                with open(stream_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            _stream_claude_sdk_record(record)
            if (
                stop_artifact_payload is not None
                and stop_artifact_outputs is not None
                and _required_artifacts_ready(
                    stop_artifact_payload, stop_artifact_outputs, since_ts=run_start_ts
                )
            ):
                stopped_after_artifacts = True
                stop_record = {
                    "type": "QumusEarlyStop",
                    "reason": "required_design_artifacts_ready",
                    "message": (
                        "Required design-stage GDS, screenshot, and result JSON are present; "
                        "Qumus stopped the SDK stream so orchestration can continue autonomously."
                    ),
                }
                records.append(stop_record)
                if stream_log_path:
                    with open(stream_log_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(stop_record, ensure_ascii=False) + "\n")
                _stream_claude_sdk_record(stop_record)
                break

    await asyncio.wait_for(collect(), timeout=max(1, int(timeout_seconds)))
    result: Dict[str, object] = {"messages": records}
    if stopped_after_artifacts:
        result["early_stop_reason"] = "required_design_artifacts_ready"
    return result


def run_claude_sdk_klayout_design(
    task: str,
    scenario: str = "",
    handoff_dir: str = "",
    extra_context_json: str = "",
    klayout_mcp_url: str = "http://127.0.0.1:8765/mcp",
    timeout_seconds: int = DEFAULT_CLAUDE_SDK_TIMEOUT,
    permission_mode: str = "acceptEdits",
    claude_sdk_model: str = DEFAULT_CLAUDE_SDK_MODEL,
    extra_allowed_tools: str = "",
    resume_session_id: str = "",
    resume_from_run_result: str = "",
    require_ideal_artifacts: bool = True,
    allow_incomplete_guidance: bool = False,
) -> Dict[str, object]:
    """Run the Python Claude Agent SDK as the direct KLayout design backend."""
    # A resumed CLI invocation normally supplies only the saved run-result
    # path. Rehydrate the sibling contract before rebuilding the handoff so
    # the real-flake candidate and approved-guidance payload are preserved.
    if resume_from_run_result and not (extra_context_json or "").strip():
        extra_context_json = _resume_context_from_run_result(resume_from_run_result)
    try:
        timeout_seconds = max(int(timeout_seconds or 0), DEFAULT_CLAUDE_SDK_TIMEOUT)
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_CLAUDE_SDK_TIMEOUT
    payload = build_claude_klayout_handoff(
        task=task,
        scenario=scenario,
        handoff_dir=handoff_dir,
        extra_context_json=extra_context_json,
        klayout_mcp_url=klayout_mcp_url,
    )
    if _design_stage_from_payload(payload) == "ideal_klayout_layout":
        input_context = payload.get("input_context")
        reuse_context = input_context if isinstance(input_context, dict) else {}
        reuse_guidance = _guidance_from_context(reuse_context)
        reused = _materialize_default_graphene_qh_ideal(
            task=task,
            guidance=reuse_guidance,
            extra_context=reuse_context,
            output_dir=str(payload["handoff_dir"]),
        )
        if reused is not None:
            output_contract = payload.get("output_contract")
            required_outputs = (
                output_contract.get("required_outputs")
                if isinstance(output_contract, dict)
                else {}
            )
            result_path = os.path.join(
                str(payload["handoff_dir"]), "claude_sdk_run_result.json"
            )
            run_result: Dict[str, object] = {
                "status": "ok",
                "mode": "verified_default_ideal_design_reuse",
                "message": (
                    "Reused the fully verified run_187 graphene quantum Hall ideal design; "
                    "Claude Agent SDK/KLayout ideal redesign was skipped."
                ),
                "workflow_id": payload["workflow_id"],
                "handoff_dir": payload["handoff_dir"],
                "start_here": payload["start_here"],
                "contract_path": payload["contract_path"],
                "ideal_klayout_script": payload["ideal_klayout_script"],
                "ideal_klayout_script_execution_required": False,
                "verified_source_build_script": reused["verified_source_build_script"],
                "reused_default_ideal_design": True,
                "default_ideal_template_id": reused["template_id"],
                "default_ideal_source_run": reused["source_run"],
                "default_ideal_manifest": reused["manifest_path"],
                "ideal_design_reuse_provenance": reused["provenance_path"],
                "claude_sdk_executed": False,
                "artifacts": {
                    key: {"path": path, "exists": os.path.exists(str(path))}
                    for key, path in required_outputs.items()  # type: ignore[union-attr]
                },
                "human_review_required": False,
                "review_gate": None,
                "human_review_gates": [],
                "design_review_policy": "verified_default_reuse_no_human_gate",
                "autonomous_continuation": True,
                "next_qmus_action": "continue_to_inventory_and_flake_selection",
                "elapsed_seconds": 0.0,
            }
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(run_result, f, ensure_ascii=False, indent=2)
            run_result["claude_sdk_run_result"] = result_path
            return run_result
    result_path = os.path.join(str(payload["handoff_dir"]), "claude_sdk_run_result.json")
    stream_log_path = os.path.join(str(payload["handoff_dir"]), "claude_sdk_stream.jsonl")
    required_outputs = payload["output_contract"]["required_outputs"]  # type: ignore[index]
    existing_result = _existing_successful_real_flake_design(
        payload,
        required_outputs,  # type: ignore[arg-type]
    )
    if existing_result is not None:
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(existing_result, f, ensure_ascii=False, indent=2)
        existing_result["claude_sdk_run_result"] = result_path
        return existing_result

    prompt = _claude_sdk_prompt(payload)
    prompt_path = os.path.join(str(payload["handoff_dir"]), "claude_sdk_prompt.md")
    resolved_claude_sdk_model = _resolve_claude_model_for_gateway(claude_sdk_model)
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    try:
        resolved_resume_session_id = _resolve_claude_sdk_resume_id(
            resume_session_id=resume_session_id,
            resume_from_run_result=resume_from_run_result,
        )
    except Exception as exc:
        resolved_resume_session_id = ""
        resume_resolution_error = str(exc)
    else:
        resume_resolution_error = ""

    output_contract = payload.get("output_contract")
    graphene_device_automation = bool(
        output_contract.get("graphene_device_full_automation")
        if isinstance(output_contract, dict)
        else payload.get("graphene_device_full_automation")
    )
    validation = payload.get("guidance_validation")
    if (
        not allow_incomplete_guidance
        and isinstance(validation, dict)
        and not validation.get("complete", False)
    ):
        blocked = _incomplete_guidance_result(
            payload=payload,
            result_path=result_path,
            mode="claude_agent_sdk_design_backend",
            prompt_path=prompt_path,
            klayout_mcp_url=klayout_mcp_url,
            extra={
                "permission_mode": permission_mode,
                "claude_sdk_model": resolved_claude_sdk_model,
                "allowed_tools": _claude_sdk_allowed_tools(extra_allowed_tools),
                "resume_session_id": resolved_resume_session_id,
                "resume_from_run_result": resume_from_run_result,
                "not_executed": True,
            },
        )
        blocked["claude_sdk_run_result"] = result_path
        return blocked

    protected_ideal_files: Dict[str, bytes] = {}
    if _design_stage_from_payload(payload) == "real_flake_adaptation":
        for protected_path in (
            os.path.join(str(payload["handoff_dir"]), "ideal_device.gds"),
            os.path.join(str(payload["handoff_dir"]), "ideal_device_result.json"),
        ):
            with open(protected_path, "rb") as protected_file:
                protected_ideal_files[protected_path] = protected_file.read()

    started_at = time.time()
    run_result: Dict[str, object] = {
        "status": "running",
        "mode": "claude_agent_sdk_design_backend",
        "workflow_id": payload["workflow_id"],
        "handoff_dir": payload["handoff_dir"],
        "start_here": payload["start_here"],
        "contract_path": payload["contract_path"],
        "ideal_klayout_script": payload["ideal_klayout_script"],
        "prompt_path": prompt_path,
        "stream_log_path": stream_log_path,
        "klayout_mcp_url": klayout_mcp_url,
        "permission_mode": permission_mode,
        "claude_sdk_model": resolved_claude_sdk_model,
        "claude_sdk_auth_mode": _claude_sdk_auth_mode(),
        "anthropic_base_url": os.getenv("ANTHROPIC_BASE_URL", ""),
        "allowed_tools": _claude_sdk_allowed_tools(extra_allowed_tools),
        "resume_session_id": resolved_resume_session_id,
        "resume_from_run_result": resume_from_run_result,
        "claude_sdk_continuation_mode": "resume" if resolved_resume_session_id else "new_session",
        "human_review_required": False,
        "review_gate": None,
        "human_review_gates": [],
        "design_review_policy": "design_output_full_automation_no_human_gate",
        "autonomous_continuation": True,
    }
    if resume_resolution_error:
        run_result.update(
            {
                "status": "error",
                "message": f"Could not resolve Claude SDK resume session id: {resume_resolution_error}",
                "error_type": "ClaudeSDKResumeResolutionError",
            }
        )

    if not _has_claude_sdk_auth():
        run_result.update(
            {
                "status": "unavailable",
                "message": (
                    "Claude Agent SDK auth is not set. Provide ANTHROPIC_API_KEY for "
                    "Anthropic, or ANTHROPIC_BASE_URL plus ANTHROPIC_AUTH_TOKEN for "
                    "an Anthropic-compatible gateway such as ByteFuture Token Station."
                ),
            }
        )
    elif "claude_agent_sdk" not in sys.modules:
        try:
            __import__("claude_agent_sdk")
        except ImportError as exc:
            run_result.update(
                {
                    "status": "unavailable",
                    "message": "claude-agent-sdk is not installed. Install it with `pip install claude-agent-sdk`.",
                    "stderr": str(exc),
                }
            )

    if run_result["status"] == "running":
        try:
            from common.async_run import run_sdk_coro

            sdk_result = run_sdk_coro(
                _run_claude_agent_sdk_async(
                    prompt=prompt,
                    klayout_mcp_url=klayout_mcp_url,
                    allowed_tools=run_result["allowed_tools"],  # type: ignore[arg-type]
                    permission_mode=permission_mode,
                    model=resolved_claude_sdk_model,
                    timeout_seconds=timeout_seconds,
                    resume_session_id=resolved_resume_session_id,
                    stream_log_path=stream_log_path,
                    stop_artifact_payload=payload,
                    stop_artifact_outputs=required_outputs,  # type: ignore[arg-type]
                )
            )
            run_result.update({"status": "ok", **sdk_result})
            if sdk_result.get("early_stop_reason") == "required_design_artifacts_ready":
                run_result["message"] = (
                    "Claude Agent SDK stream stopped as soon as required design artifacts were ready."
                )
            messages = sdk_result.get("messages")
            if isinstance(messages, list):
                session_id = _extract_latest_session_id([m for m in messages if isinstance(m, dict)])
                if session_id:
                    run_result["claude_sdk_session_id"] = session_id
                    run_result["resume_hint"] = (
                        "Pass this value as --claude-sdk-resume-session-id on a revision run, "
                        "or pass this run result path as --claude-sdk-resume-from-run-result."
                    )
        except asyncio.TimeoutError:
            run_result.update(
                {
                    "status": "timeout",
                    "message": f"Claude Agent SDK did not finish within {timeout_seconds} seconds.",
                }
            )
        except Exception as exc:
            run_result.update(
                {
                    "status": "error",
                    "message": str(exc),
                    "error_type": exc.__class__.__name__,
                }
            )

    run_result["elapsed_seconds"] = round(time.time() - started_at, 3)
    run_result["artifacts"] = {
        key: {
            "path": path,
            "exists": os.path.exists(str(path)),
        }
        for key, path in required_outputs.items()
    }
    if not run_result.get("claude_sdk_session_id"):
        session_id = _extract_latest_session_id_from_stream_log(stream_log_path)
        if session_id:
            run_result["claude_sdk_session_id"] = session_id
            run_result["resume_hint"] = (
                "Pass this value as --claude-sdk-resume-session-id on a revision run, "
                "or pass this run result path as --claude-sdk-resume-from-run-result."
            )

    overwritten_ideal_files: List[str] = []
    for protected_path, original_content in protected_ideal_files.items():
        try:
            current_content = Path(protected_path).read_bytes()
        except OSError:
            current_content = b""
        if current_content != original_content:
            Path(protected_path).write_bytes(original_content)
            overwritten_ideal_files.append(protected_path)
    if overwritten_ideal_files:
        run_result["status"] = "blocked"
        run_result["protected_ideal_files_restored"] = overwritten_ideal_files
        run_result["message"] = (
            "The real-flake SDK attempted to overwrite the verified ideal baseline. "
            "Qumus restored the baseline and rejected this run."
        )

    permission_denials: List[object] = []
    for message in run_result.get("messages", []):
        if not isinstance(message, dict):
            continue
        data = message.get("data")
        if isinstance(data, dict):
            denials = data.get("permission_denials")
            if isinstance(denials, list):
                permission_denials.extend(denials)
        text = str(message.get("text") or message.get("result") or "")
        if "permission denied" in text.lower() or "permission_denials" in text.lower():
            run_result.setdefault("backend_warnings", []).append(text[:1000])  # type: ignore[union-attr]

    if permission_denials:
        run_result["status"] = "blocked"
        run_result["permission_denials"] = permission_denials
        run_result["message"] = "Claude Agent SDK was blocked by missing tool permissions."
    elif _design_stage_from_payload(payload) == "real_flake_adaptation":
        design_result_path = str(required_outputs.get("result_json") or "")
        proven_failure = _validated_flake_fit_failure(design_result_path)
        failure_report_path = os.path.join(
            os.path.dirname(design_result_path), "flake_fit_failure_report.json"
        )
        if proven_failure is not None and _failure_report_file_matches(
            failure_report_path, proven_failure
        ):
            run_result["status"] = "blocked"
            run_result["layout_impossible"] = True
            run_result["retry_flake_selection_required"] = True
            run_result["exfoliation_required"] = False
            run_result["flake_fit_failure_report"] = proven_failure
            run_result["flake_fit_failure_report_path"] = failure_report_path
            run_result["message"] = (
                "Bounded translation-only checks proved the approved flake set cannot satisfy "
                "the hard layout constraints. Rerun selection with this report before considering exfoliation."
            )
        elif require_ideal_artifacts and run_result.get("status") in {"ok", "timeout"}:
            missing_required = _missing_required_artifacts(payload, required_outputs)  # type: ignore[arg-type]
            if missing_required:
                if run_result.get("status") == "ok":
                    run_result["status"] = "incomplete"
                run_result["missing_required_artifacts"] = missing_required
                if run_result.get("status") == "incomplete":
                    run_result["message"] = (
                        "Claude Agent SDK completed but did not produce all required design-stage artifacts."
                    )
            elif run_result.get("status") == "timeout" and _required_artifacts_ready(payload, required_outputs):  # type: ignore[arg-type]
                run_result["status"] = "artifacts_available"
                run_result["message"] = (
                    "Claude Agent SDK timed out after producing all required design-stage artifacts. "
                    "Continue orchestration from the available artifacts instead of rerunning automatically."
                )
    elif require_ideal_artifacts and run_result.get("status") in {"ok", "timeout"}:
        missing_required = _missing_required_artifacts(payload, required_outputs)  # type: ignore[arg-type]
        if missing_required:
            if run_result.get("status") == "ok":
                run_result["status"] = "incomplete"
            run_result["missing_required_artifacts"] = missing_required
            if run_result.get("status") == "incomplete":
                run_result["message"] = (
                    "Claude Agent SDK completed but did not produce all required design-stage artifacts."
                )
        elif run_result.get("status") == "timeout" and _required_artifacts_ready(payload, required_outputs):  # type: ignore[arg-type]
            run_result["status"] = "artifacts_available"
            run_result["message"] = (
                "Claude Agent SDK timed out after producing all required design-stage artifacts. "
                "Continue orchestration from the available artifacts instead of rerunning automatically."
            )
    continuation_ready = run_result.get("status") in {"ok", "artifacts_available"}
    run_result["autonomous_continuation"] = bool(continuation_ready)
    run_result["human_review_required"] = False
    run_result["review_gate"] = None
    run_result["human_review_gates"] = []
    run_result["design_review_policy"] = (
        "design_output_full_automation_no_human_gate"
        if continuation_ready
        else run_result.get("design_review_policy", "no_human_review_gate_blocked_for_design_issue")
    )
    run_result["next_qmus_action"] = (
        "continue_to_next_workflow_stage"
        if continuation_ready
        else (
            "rerun_flake_selection_with_fit_failure_report_before_any_exfoliation"
            if run_result.get("retry_flake_selection_required") is True
            else "resolve_blocking_design_or_execution_issue_before_continuation"
        )
    )

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(run_result, f, ensure_ascii=False, indent=2)
    run_result["claude_sdk_run_result"] = result_path
    return run_result


@tool
def select_device_design_workflow(task: str, scenario: str = "", extra_context_json: str = "") -> str:
    """
    Build the high-level device design workflow before running layout tools.

    Args:
        task: User's design request or device description.
        scenario: Optional free-form workflow label. Use "legacy" only for the
            old image-mask optimizer; otherwise leave empty and let approved
            guidance define the workflow.
        extra_context_json: Optional JSON object containing approved_device_guidance
            with workflow_plan and cleanroom_minimization_plan.

    Returns:
        JSON workflow definition with ordered process steps and input contract.
    """
    extra_context = _parse_extra_context(extra_context_json)
    workflow_id = normalize_design_scenario(scenario, task)
    return json.dumps(
        get_workflow_definition(workflow_id, task=task, extra_context=extra_context),
        ensure_ascii=False,
        indent=2,
    )


def _compact_design_tool_result(run_result: dict) -> dict:
    """Keep verbose SDK messages on disk without returning them through QUMUS."""
    compact = dict(run_result)
    messages = compact.pop("messages", None)
    if isinstance(messages, list):
        compact["message_count"] = len(messages)
        compact["messages_omitted_from_tool_response"] = True
    if compact.get("claude_sdk_run_result"):
        compact["full_result_path"] = compact["claude_sdk_run_result"]
    return compact


@tool
def run_claude_sdk_klayout_design_tool(
    task: str,
    scenario: str = "",
    handoff_dir: str = "",
    extra_context_json: str = "",
    klayout_mcp_url: str = "http://127.0.0.1:8765/mcp",
    timeout_seconds: int = DEFAULT_CLAUDE_SDK_TIMEOUT,
    permission_mode: str = "acceptEdits",
    claude_sdk_model: str = DEFAULT_CLAUDE_SDK_MODEL,
    extra_allowed_tools: str = "",
    resume_session_id: str = "",
    resume_from_run_result: str = "",
    require_ideal_artifacts: bool = True,
    allow_incomplete_guidance: bool = False,
) -> str:
    """
    Run the Python Claude Agent SDK as the design backend for KLayout.

    Args:
        task: User's design request or approved device-design task.
        scenario: Optional free-form workflow label.
        handoff_dir: Optional output directory for SDK/KLayout artifacts.
        extra_context_json: Optional JSON object containing approved guidance.
        klayout_mcp_url: KlayoutClaw MCP endpoint.
        timeout_seconds: Maximum seconds to wait for the SDK run. Defaults to 1 hour
            (QUMUS_CLAUDE_SDK_TIMEOUT), and smaller values are raised to that floor
            to avoid cutting off full device design runs.
        permission_mode: Claude Agent SDK permission mode, usually acceptEdits.
        claude_sdk_model: Claude Agent SDK model id; defaults to QUMUS_CLAUDE_SDK_MODEL
            or claude-opus-5.
        extra_allowed_tools: Comma-separated additional SDK tool names to auto-approve.
        resume_session_id: Previous Claude SDK session id for revision runs.
        resume_from_run_result: Path to a previous claude_sdk_run_result.json
            whose saved session id should be resumed.
        require_ideal_artifacts: If true, mark the run incomplete unless ideal
            GDS, screenshot, and result JSON exist after SDK completion.
        allow_incomplete_guidance: Debug override. If false, block before
            Claude/KLayout execution when approved guidance lacks stack/contact/gate semantics.

    Returns:
        JSON payload describing the SDK run, artifacts, continuation status, and any blocking open issues.
    """
    run_result = run_claude_sdk_klayout_design(
            task=task,
            scenario=scenario,
            handoff_dir=handoff_dir,
            extra_context_json=extra_context_json,
            klayout_mcp_url=klayout_mcp_url,
            timeout_seconds=timeout_seconds,
            permission_mode=permission_mode,
            claude_sdk_model=claude_sdk_model,
            extra_allowed_tools=extra_allowed_tools,
            resume_session_id=resume_session_id,
            resume_from_run_result=resume_from_run_result,
            require_ideal_artifacts=require_ideal_artifacts,
            allow_incomplete_guidance=allow_incomplete_guidance,
        )
    return json.dumps(
        _compact_design_tool_result(run_result),
        ensure_ascii=False,
        indent=2,
    )


__all__ = [
    "AI_DESIGNED_WORKFLOW",
    "LEGACY_MASK_OPTIMIZER",
    "WORKFLOW_DEFINITIONS",
    "normalize_design_scenario",
    "get_workflow_definition",
    "build_claude_klayout_handoff",
    "run_claude_sdk_klayout_design",
    "_compact_design_tool_result",
    "run_claude_sdk_klayout_design_tool",
    "select_device_design_workflow",
]
