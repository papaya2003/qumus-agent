"""Claude Agent SDK flake-quality selector with a persisted human approval gate."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from smolagents import tool

from common.logic.stacking_run_dirs import get_stacking_run_dirs
from common.services.design_candidate_payload_service import (
    build_design_candidate_payload,
    hydrate_candidate_payload_paths,
)
from common.services.human_gate_service import HumanGateService


ROOT_DIR = Path(__file__).resolve().parents[2]
SKILL_PATH = Path(__file__).resolve().parent / "skills" / "high-quality-flake-selection" / "SKILL.md"
DEFAULT_MODEL = (os.getenv("QUMUS_FLAKE_SELECTION_SDK_MODEL") or os.getenv("QUMUS_CLAUDE_SDK_MODEL") or "claude-opus-5").strip()


def _console_print(value: object) -> None:
    """Stream SDK/UI markers without crashing on legacy Windows encodings."""
    text = str(value)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        safe = text.encode(encoding, errors="backslashreplace").decode(encoding)
        print(safe, flush=True)


def _default_output_dir() -> Path:
    """Reuse the latest numeric stacking run when process-local run state was lost."""
    stacking_root = ROOT_DIR / "images" / "stacking"
    existing: list[int] = []
    if stacking_root.exists():
        for path in stacking_root.iterdir():
            match = re.fullmatch(r"run_(\d+)", path.name)
            if path.is_dir() and match:
                existing.append(int(match.group(1)))
    if existing:
        dirs = get_stacking_run_dirs(run_number=max(existing), sample_label="design")
    else:
        dirs = get_stacking_run_dirs(create_new=True, sample_label="design")
    return Path(dirs["sample_dir"]) / "flake_selection"


def _resolve_model(model: str) -> str:
    resolved = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if "bytefuture" in (os.getenv("ANTHROPIC_BASE_URL") or "").lower() and resolved.startswith("claude-"):
        return f"anthropic/{resolved}"
    return resolved


def _sdk_available() -> bool:
    if not (os.getenv("ANTHROPIC_API_KEY") or (os.getenv("ANTHROPIC_BASE_URL") and os.getenv("ANTHROPIC_AUTH_TOKEN"))):
        return False
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception:
        return False
    return True


def _json_object(raw: str, name: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must decode to a JSON object.")
    return value


def _candidate_payload(design_context: dict[str, Any]) -> dict[str, Any]:
    nested = design_context.get("design_candidate_payload")
    if isinstance(nested, dict):
        payload = dict(nested)
    elif isinstance(design_context.get("candidate_flakes_by_role"), dict):
        payload = {
            "status": "ok",
            "candidate_flakes_by_role": design_context.get("candidate_flakes_by_role"),
            "candidate_mask_paths_by_role": design_context.get("candidate_mask_paths_by_role") or {},
            "candidate_image_paths_by_role": design_context.get("candidate_image_paths_by_role") or {},
            "missing_candidate_roles": design_context.get("missing_candidate_roles") or [],
            "pixel_size_um_per_px": design_context.get("pixel_size_um_per_px"),
            "coordinate_frame_notes": design_context.get("coordinate_frame_notes"),
        }
    else:
        guidance = design_context.get("approved_device_guidance")
        payload = build_design_candidate_payload(
            approved_device_guidance=guidance if isinstance(guidance, dict) else {},
            artifact_paths_by_role=design_context.get("artifact_paths_by_role"),
            artifacts_by_role=design_context.get("artifacts_by_role"),
        )
    payload = hydrate_candidate_payload_paths(payload)
    payload["required_flake_counts_by_role"] = _required_flake_counts(design_context, payload)
    failure_report = design_context.get("flake_fit_failure_report")
    if isinstance(failure_report, dict):
        payload["flake_fit_failure_report"] = failure_report
    else:
        failure_path = design_context.get("flake_fit_failure_report_path")
        if failure_path and Path(str(failure_path)).is_file():
            try:
                loaded_failure = json.loads(Path(str(failure_path)).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                loaded_failure = None
            if isinstance(loaded_failure, dict):
                payload["flake_fit_failure_report"] = loaded_failure
    excluded = _human_excluded_identities(design_context)
    if excluded:
        payload = _without_rejected_candidates(payload, excluded)
    return payload


def _role_key(raw: object) -> str:
    text = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    return "".join(ch for ch in text if ch.isalnum() or ch == "_")


def _positive_count(raw: object, default: int = 1) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _required_flake_counts(
    design_context: dict[str, Any], candidate_payload: dict[str, Any]
) -> dict[str, int]:
    roles = candidate_payload.get("candidate_flakes_by_role")
    role_names = [str(role) for role in roles] if isinstance(roles, dict) else []
    counts = {role: 1 for role in role_names}
    explicit = design_context.get("required_flake_counts_by_role") or candidate_payload.get(
        "required_flake_counts_by_role"
    )
    if isinstance(explicit, dict):
        for role, raw_count in explicit.items():
            key = _role_key(role)
            match = next((name for name in role_names if _role_key(name) == key), None)
            if match:
                counts[match] = _positive_count(raw_count)

    guidance = design_context.get("approved_device_guidance")
    layers = guidance.get("layer_stack") if isinstance(guidance, dict) else None
    if isinstance(layers, list):
        occurrences: dict[str, int] = {}
        explicit_for_role: dict[str, int] = {}
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            key = _role_key(layer.get("role") or layer.get("name") or layer.get("material"))
            match = next((name for name in role_names if _role_key(name) == key), None)
            if not match:
                continue
            occurrences[match] = occurrences.get(match, 0) + 1
            for field in ("required_count", "quantity", "count"):
                if layer.get(field) is not None:
                    explicit_for_role[match] = max(
                        explicit_for_role.get(match, 1), _positive_count(layer.get(field))
                    )
                    break
        for role in role_names:
            counts[role] = max(counts[role], occurrences.get(role, 0), explicit_for_role.get(role, 0))
    return counts


def _candidate_identity(record: dict[str, Any]) -> str:
    flake_id = record.get("flake_id")
    chip_id = record.get("chip_id")
    if flake_id is not None:
        return f"chip:{chip_id}:flake:{flake_id}"
    return f"mask:{record.get('mask_20x_path') or record.get('mask_path') or ''}"


def _human_excluded_identities(design_context: dict[str, Any]) -> set[str]:
    excluded = {
        str(value)
        for value in design_context.get("excluded_flake_identities") or []
        if value
    }
    rejected_records: list[dict[str, Any]] = []
    direct = design_context.get("human_rejected_flakes")
    if isinstance(direct, list):
        rejected_records.extend(item for item in direct if isinstance(item, dict))
    history = design_context.get("human_rejection_history")
    if isinstance(history, list):
        for entry in history:
            if isinstance(entry, dict) and isinstance(entry.get("rejected_flakes"), list):
                rejected_records.extend(
                    item for item in entry["rejected_flakes"] if isinstance(item, dict)
                )
    excluded.update(_candidate_identity(item) for item in rejected_records)
    return {identity for identity in excluded if identity and not identity.endswith("mask:")}


def _without_rejected_candidates(
    candidate_payload: dict[str, Any], excluded_identities: set[str]
) -> dict[str, Any]:
    """Remove human-rejected physical flakes while preserving role/cardinality contracts."""
    filtered = copy.deepcopy(candidate_payload)
    roles = filtered.get("candidate_flakes_by_role")
    if not isinstance(roles, dict):
        return filtered
    for role, raw_records in list(roles.items()):
        records = [item for item in raw_records if isinstance(item, dict)] if isinstance(raw_records, list) else []
        kept = [item for item in records if _candidate_identity(item) not in excluded_identities]
        roles[role] = kept
        masks = filtered.get("candidate_mask_paths_by_role")
        if isinstance(masks, dict):
            masks[role] = [
                str(item.get("mask_20x_path") or item.get("mask_path"))
                for item in kept
                if item.get("mask_20x_path") or item.get("mask_path")
            ]
        images = filtered.get("candidate_image_paths_by_role")
        if isinstance(images, dict):
            images[role] = [
                str(item.get("image_100x_path")) for item in kept if item.get("image_100x_path")
            ]
        images_20x = filtered.get("candidate_image_20x_paths_by_role")
        if isinstance(images_20x, dict):
            images_20x[role] = [
                str(item.get("image_20x_path")) for item in kept if item.get("image_20x_path")
            ]
    filtered["excluded_flake_identities"] = sorted(excluded_identities)
    return filtered


def _normalized_path(raw: object) -> str:
    if not raw:
        return ""
    try:
        return os.path.normcase(str(Path(str(raw)).resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(str(raw)))


def _retry_request(
    role: str,
    candidates: list[dict[str, Any]],
    reason: str,
    *,
    action: str = "exfoliate_and_search_one_chip",
) -> dict[str, Any]:
    material = next(
        (
            str(item.get("type_label") or item.get("material"))
            for item in candidates
            if item.get("type_label") or item.get("material")
        ),
        "unspecified",
    )
    return {
        "role": role,
        "material": material,
        "reason": reason,
        "evidence": [
            {
                "flake_id": item.get("flake_id"),
                "mask_20x_path": item.get("mask_20x_path"),
                "image_100x_path": item.get("image_100x_path"),
                "image_20x_path": item.get("image_20x_path"),
            }
            for item in candidates
        ],
        "required_improvements": [
            "readable real 100X optical image",
            "readable real 20X optical image",
            "role-compatible continuous geometry and fabrication-quality visible surface",
        ],
        "action": action,
    }


def _blocked_for_roles(
    candidate_payload: dict[str, Any],
    roles: list[tuple[str, str]],
    *,
    action: str = "exfoliate_and_search_one_chip",
    message: str = "No complete image-verified flake set is available for the approved device.",
) -> dict[str, Any]:
    candidates_by_role = candidate_payload.get("candidate_flakes_by_role")
    candidates_by_role = candidates_by_role if isinstance(candidates_by_role, dict) else {}
    requests = [
        _retry_request(
            role,
            [item for item in candidates_by_role.get(role, []) if isinstance(item, dict)],
            reason,
            action=action,
        )
        for role, reason in roles
    ]
    return {
        "status": "blocked",
        "message": message,
        "unusable_roles": [request["role"] for request in requests],
        "retry_requests": requests,
        "human_review_required": False,
        "autonomous_continuation": False,
    }


def validate_selection_result(
    result: dict[str, Any],
    candidate_payload: dict[str, Any],
    *,
    sdk_read_paths: set[str] | None = None,
) -> dict[str, Any]:
    candidate_roles = candidate_payload.get("candidate_flakes_by_role")
    if not isinstance(candidate_roles, dict):
        raise ValueError("Candidate payload has no candidate_flakes_by_role mapping.")
    normalized_reads = {_normalized_path(path) for path in sdk_read_paths or set() if path}
    if sdk_read_paths is not None:
        unread_images = [
            f"{field}={item.get(field)}"
            for records in candidate_roles.values()
            if isinstance(records, list)
            for item in records
            if isinstance(item, dict)
            for field in ("image_100x_path", "image_20x_path", "mask_20x_path")
            if item.get(field)
            and Path(str(item[field])).is_file()
            and _normalized_path(item[field]) not in normalized_reads
        ]
        if unread_images:
            raise ValueError(
                "Claude SDK did not Read every supplied 100X optical image, 20X optical image, and available 20X segmentation reference: "
                + ", ".join(unread_images)
            )
    if result.get("status") != "ok":
        return result
    selected = result.get("selected_flakes_by_role")
    ranked = result.get("ranked_candidates_by_role")
    if not isinstance(selected, dict) or not isinstance(ranked, dict):
        raise ValueError("Selection result must contain selected_flakes_by_role.")

    raw_counts = candidate_payload.get("required_flake_counts_by_role")
    required_counts = {
        str(role): _positive_count(raw_counts.get(role) if isinstance(raw_counts, dict) else 1)
        for role in candidate_roles
    }
    required_roles = [str(role) for role in candidate_roles]
    missing = [role for role in required_roles if role not in selected]
    if missing:
        raise ValueError(f"Selection omitted required roles: {', '.join(missing)}")

    used_identities: set[str] = set()
    validated: dict[str, list[dict[str, Any]]] = {}
    flat_selection: list[dict[str, Any]] = []
    for role in required_roles:
        choices = selected.get(role)
        if not isinstance(choices, list):
            raise ValueError(f"Selected flakes for role {role!r} must be a list.")
        required_count = required_counts[role]
        if len(choices) != required_count:
            raise ValueError(
                f"Role {role!r} requires exactly {required_count} flakes, got {len(choices)}."
            )
        candidates = [item for item in candidate_roles[role] if isinstance(item, dict)]
        ranked_records = ranked.get(role)
        if not isinstance(ranked_records, list):
            raise ValueError(f"Selection result has no ranked audit list for role {role!r}.")
        if any(
            isinstance(item, dict)
            and item.get("feasible") is True
            and item.get("image_read_confirmed") is not True
            for item in ranked_records
        ):
            raise ValueError(f"A feasible ranked candidate for role {role!r} lacks image-read confirmation.")
        ranked_ids = {item.get("flake_id") for item in ranked_records if isinstance(item, dict) and item.get("flake_id") is not None}
        ranked_masks = {
            str(item.get("mask_20x_path") or item.get("mask_path") or "")
            for item in ranked_records
            if isinstance(item, dict)
        }
        unaudited = [
            item
            for item in candidates
            if not (
                (item.get("flake_id") is not None and item.get("flake_id") in ranked_ids)
                or str(item.get("mask_20x_path") or item.get("mask_path") or "") in ranked_masks
            )
        ]
        if unaudited:
            raise ValueError(f"Selection result did not assess every candidate for role {role!r}.")

        role_selection: list[dict[str, Any]] = []
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError(f"Selected flake for role {role!r} is not an object.")
            choice_mask = str(choice.get("mask_20x_path") or choice.get("mask_path") or "")
            choice_id = choice.get("flake_id")
            match = next(
                (
                    item
                    for item in candidates
                    if (choice_id is not None and item.get("flake_id") == choice_id)
                    or (
                        choice_mask
                        and str(item.get("mask_20x_path") or item.get("mask_path") or "")
                        == choice_mask
                    )
                ),
                None,
            )
            if match is None:
                raise ValueError(f"Selection for role {role!r} is not one of its supplied candidates.")
            identity = _candidate_identity(match)
            if identity in used_identities:
                raise ValueError("One physical flake cannot satisfy multiple roles or instances.")
            used_identities.add(identity)

            real_image_path = str(match.get("image_100x_path") or "").strip()
            if not real_image_path or not Path(real_image_path).is_file():
                raise ValueError(f"Selection for role {role!r} has no readable real 100X optical image.")
            real_image_20x_path = str(match.get("image_20x_path") or "").strip()
            if not real_image_20x_path or not Path(real_image_20x_path).is_file():
                raise ValueError(f"Selection for role {role!r} has no readable real 20X optical image.")
            assessment = choice.get("quality_assessment")
            if not isinstance(assessment, dict):
                raise ValueError(f"Selection for role {role!r} has no quality_assessment.")
            if assessment.get("image_read_confirmed") is not True:
                raise ValueError(f"Selection for role {role!r} did not confirm reading the real image.")
            if _normalized_path(assessment.get("real_image_path")) != _normalized_path(real_image_path):
                raise ValueError(f"Selection for role {role!r} cites the wrong real image path.")
            if assessment.get("image_20x_read_confirmed") is not True:
                raise ValueError(f"Selection for role {role!r} did not confirm reading the real 20X optical image.")
            if _normalized_path(assessment.get("real_image_20x_path")) != _normalized_path(real_image_20x_path):
                raise ValueError(f"Selection for role {role!r} cites the wrong real 20X optical image path.")
            if sdk_read_paths is not None and _normalized_path(real_image_path) not in normalized_reads:
                raise ValueError(f"Claude SDK did not Read the selected real image for role {role!r}.")
            if sdk_read_paths is not None and _normalized_path(real_image_20x_path) not in normalized_reads:
                raise ValueError(f"Claude SDK did not Read the selected real 20X optical image for role {role!r}.")
            for score_name in (
                "geometry_fitness",
                "visible_cleanliness",
                "continuity_uniformity",
                "evidence_confidence",
                "inventory_tiebreaker",
                "overall_score",
            ):
                score = assessment.get(score_name)
                if (
                    not isinstance(score, (int, float))
                    or isinstance(score, bool)
                    or not 0 <= float(score) <= 100
                ):
                    raise ValueError(f"Selection for role {role!r} has invalid {score_name} score.")
            selection_reason = str(choice.get("selection_reason") or "").strip()
            if not selection_reason:
                raise ValueError(f"Selection for role {role!r} has no selection_reason.")
            merged = dict(match)
            merged["quality_assessment"] = assessment
            merged["selection_reason"] = selection_reason
            role_selection.append(merged)
            flat_selection.append({"role": role, **merged})
        validated[role] = role_selection
    result["selected_flakes_by_role"] = validated
    result["selected_flake_set"] = flat_selection
    result["required_flake_counts_by_role"] = required_counts
    missing_design_masks = [
        {
            "role": item["role"],
            "flake_id": item.get("flake_id"),
            "chip_id": item.get("chip_id"),
            "required_action": "segment_or_relink_20x_mask_before_real_flake_design",
        }
        for item in flat_selection
        if not item.get("mask_20x_path") or not Path(str(item["mask_20x_path"])).is_file()
    ]
    result["design_mask_preparation_required"] = bool(missing_design_masks)
    result["selected_flakes_missing_design_masks"] = missing_design_masks
    failure_report = candidate_payload.get("flake_fit_failure_report")
    if isinstance(failure_report, dict) and failure_report.get("layout_impossible") is True:
        for failure in failure_report.get("failed_roles") or []:
            if not isinstance(failure, dict):
                continue
            role = str(failure.get("role") or "")
            if role not in validated:
                continue
            failed_ids = {
                value
                for field in ("flake_ids", "failed_flake_ids")
                for value in (failure.get(field) if isinstance(failure.get(field), list) else [])
            }
            failed_masks = {
                _normalized_path(value)
                for field in ("mask_paths", "failed_mask_paths")
                for value in (failure.get(field) if isinstance(failure.get(field), list) else [])
                if value
            }
            for failed in failure.get("failed_flakes") or []:
                if isinstance(failed, dict):
                    if failed.get("flake_id") is not None:
                        failed_ids.add(failed["flake_id"])
                    if failed.get("mask_20x_path") or failed.get("mask_path"):
                        failed_masks.add(
                            _normalized_path(failed.get("mask_20x_path") or failed.get("mask_path"))
                        )
            if failed_ids or failed_masks:
                chosen = validated[role]
                repeats_failed_set = all(
                    item.get("flake_id") in failed_ids
                    or _normalized_path(item.get("mask_20x_path") or item.get("mask_path")) in failed_masks
                    for item in chosen
                )
                if repeats_failed_set:
                    raise ValueError(
                        f"Selection repeated the design-proven impossible flake combination for role {role!r}."
                    )
    return result


async def _run_sdk(prompt: str, model: str, timeout_seconds: int) -> list[dict[str, Any]]:
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = ClaudeAgentOptions(
        model=_resolve_model(model),
        allowed_tools=["Read", "Write", "Glob", "Grep"],
        disallowed_tools=["Bash", "Edit", "WebSearch", "WebFetch"],
        permission_mode="acceptEdits",
        system_prompt=(
            "You are the Qumus flake-selection backend. Evaluate only supplied local candidate masks, "
            "optical images, metadata, approved guidance, and the named editable skill. Do not design "
            "the device or perform hardware actions. Write the required JSON artifact and stop."
        ),
    )
    records: list[dict[str, Any]] = []

    async def collect() -> None:
        stream = query(prompt=prompt, options=options)
        if inspect.isawaitable(stream):
            stream = await stream
        async for message in stream:
            record: dict[str, Any] = {"type": message.__class__.__name__}
            for attr in ("result", "subtype", "session_id", "total_cost_usd"):
                value = getattr(message, attr, None)
                if value is not None:
                    record[attr] = value
            content = getattr(message, "content", None)
            if isinstance(content, list):
                texts = [str(getattr(block, "text")) for block in content if getattr(block, "text", None)]
                tools = [str(getattr(block, "name")) for block in content if getattr(block, "name", None)]
                tool_inputs = [
                    getattr(block, "input")
                    for block in content
                    if getattr(block, "name", None) and isinstance(getattr(block, "input", None), dict)
                ]
                if texts:
                    record["text"] = "\n".join(texts)[:12000]
                if tools:
                    record["tool_calls"] = tools
                if tool_inputs:
                    record["tool_inputs"] = tool_inputs
            records.append(record)
            if os.getenv("QUMUS_STREAM_CLAUDE_SDK_LOGS", "1").lower() not in {"0", "false", "no", "off"}:
                if record.get("tool_calls"):
                    _console_print(f"[CLAUDE_SDK][flake_selection][tool] {', '.join(record['tool_calls'])}")
                elif record.get("text"):
                    _console_print(f"[CLAUDE_SDK][flake_selection][text] {str(record['text'])[:2000]}")

    await asyncio.wait_for(collect(), timeout=max(1, int(timeout_seconds)))
    return records


def _sdk_read_paths(records: list[dict[str, Any]]) -> set[str]:
    paths: set[str] = set()
    for record in records:
        tools = record.get("tool_calls")
        inputs = record.get("tool_inputs")
        if not isinstance(tools, list) or not isinstance(inputs, list):
            continue
        for name, tool_input in zip(tools, inputs):
            if str(name).lower() != "read" or not isinstance(tool_input, dict):
                continue
            path = tool_input.get("file_path") or tool_input.get("path")
            if path:
                paths.add(str(path))
    return paths


def run_claude_sdk_flake_selection(
    *,
    design_context: dict[str, Any],
    project_id: str = "",
    output_dir: str = "",
    session_key: str = "",
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = 900,
    approval_timeout_seconds: int = 86400,
) -> dict[str, Any]:
    candidate_payload = _candidate_payload(design_context)
    human_rejection_history = [
        copy.deepcopy(item)
        for item in design_context.get("human_rejection_history") or []
        if isinstance(item, dict)
    ]
    excluded_identities = {
        str(value)
        for value in candidate_payload.get("excluded_flake_identities") or []
        if value
    }
    roles = candidate_payload.get("candidate_flakes_by_role")
    missing_roles = candidate_payload.get("missing_candidate_roles")
    if isinstance(missing_roles, list) and isinstance(roles, dict):
        missing_roles = [
            entry
            for entry in missing_roles
            if not any(
                _role_key(role)
                == _role_key(entry.get("role") if isinstance(entry, dict) else entry)
                and isinstance(records, list)
                and any(isinstance(record, dict) for record in records)
                for role, records in roles.items()
            )
        ]
        candidate_payload["missing_candidate_roles"] = missing_roles
    if isinstance(missing_roles, list) and missing_roles:
        role_reasons = [
            (
                str(entry.get("role") or "unknown_role") if isinstance(entry, dict) else str(entry),
                "No candidates were supplied for this required flake role.",
            )
            for entry in missing_roles
        ]
        blocked = _blocked_for_roles(candidate_payload, role_reasons)
        for request, entry in zip(blocked["retry_requests"], missing_roles):
            if isinstance(entry, dict) and entry.get("material"):
                request["material"] = str(entry["material"])
        blocked["missing_candidate_roles"] = missing_roles
        blocked["blocker_type"] = "missing_physical_candidates"
        blocked["exfoliation_allowed"] = True
        blocked["human_rejection_history"] = human_rejection_history
        blocked["excluded_flake_identities"] = sorted(excluded_identities)
        return blocked
    if not isinstance(roles, dict) or not any(isinstance(items, list) and items for items in roles.values()):
        empty_role_reasons = (
            [
                (str(role), "No physical candidates were supplied for this required flake role.")
                for role, items in roles.items()
                if not isinstance(items, list) or not items
            ]
            if isinstance(roles, dict)
            else []
        )
        if empty_role_reasons:
            blocked = _blocked_for_roles(
                candidate_payload,
                empty_role_reasons,
                message="No physical flake candidates were supplied for the required role(s).",
            )
            blocked["blocker_type"] = "missing_physical_candidates"
            blocked["exfoliation_allowed"] = True
            blocked["human_rejection_history"] = human_rejection_history
            blocked["excluded_flake_identities"] = sorted(excluded_identities)
            return blocked
        return {
            "status": "blocked",
            "message": "No physical flake candidates were supplied for selection.",
            "blocker_type": "missing_physical_candidates",
            "exfoliation_allowed": True,
            "human_review_required": False,
            "human_rejection_history": human_rejection_history,
            "excluded_flake_identities": sorted(excluded_identities),
        }
    required_counts = candidate_payload.get("required_flake_counts_by_role")
    image_evidence_shortages: list[tuple[str, str]] = []
    for role, raw_candidates in roles.items():
        candidates = [item for item in raw_candidates if isinstance(item, dict)] if isinstance(raw_candidates, list) else []
        usable = [
            item
            for item in candidates
            if item.get("image_100x_path")
            and Path(str(item["image_100x_path"])).is_file()
            and item.get("image_20x_path")
            and Path(str(item["image_20x_path"])).is_file()
        ]
        required_count = _positive_count(required_counts.get(role) if isinstance(required_counts, dict) else 1)
        if len(usable) < required_count:
            image_evidence_shortages.append(
                (
                    str(role),
                    f"Role requires {required_count} unique flakes but only {len(usable)} candidates have readable real 100X and 20X optical images.",
                )
            )
    if image_evidence_shortages:
        blocked = _blocked_for_roles(
            candidate_payload,
            image_evidence_shortages,
            action="relink_or_capture_optical_images",
            message=(
                "Physical flake candidates exist, but their required 100X/20X optical evidence "
                "must be relinked or recaptured before selection. Do not exfoliate for this condition."
            ),
        )
        blocked["blocker_type"] = "image_evidence_repair"
        blocked["exfoliation_allowed"] = False
        blocked["human_rejection_history"] = human_rejection_history
        blocked["excluded_flake_identities"] = sorted(excluded_identities)
        return blocked

    if output_dir:
        run_dir = Path(output_dir).resolve()
    else:
        run_dir = _default_output_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = run_dir / "candidate_payload.json"
    guidance_path = run_dir / "approved_device_guidance.json"
    result_path = run_dir / "flake_selection_result.json"
    stream_path = run_dir / "claude_sdk_stream.json"
    candidate_path.write_text(json.dumps(candidate_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    guidance = design_context.get("approved_device_guidance")
    guidance_path.write_text(json.dumps(guidance if isinstance(guidance, dict) else {}, ensure_ascii=False, indent=2), encoding="utf-8")

    prompt = f"""Read and follow the editable skill at `{SKILL_PATH.resolve()}`.
Read approved guidance at `{guidance_path}` and the existing deterministic candidate payload at `{candidate_path}`.
Use the Claude Code Read tool to open and visually judge every supplied candidate's real `image_100x_path` and real `image_20x_path`. If a readable segmented `mask_20x_path` is supplied, also read it as optional reference evidence; a missing mask does not make a candidate unselectable. The 100X optical image is primary for local surface/thickness quality; the 20X optical image is primary for the complete flake shape and large-scale continuity; segmentation is reference-only for localization and approximate measurement. Never call a mask-only black gap a physical hole, crack, tear, or disconnection. If the optical images show continuous material or thickness contrast where the mask has a gap, record a segmentation disagreement and follow the optical evidence. A path string or metadata alone is not image inspection. Rank quality and role-specific zero-rotation shape fitness according to the skill. The exact number of flakes required for one complete device is in `required_flake_counts_by_role`. If `flake_fit_failure_report` is present, use its proven placement failure and replacement requirements when re-ranking, and do not repeat the failed combination.
Any `excluded_flake_identities` and `human_rejection_history` in the candidate payload are binding human decisions. Never restore or select an excluded physical flake. Select a fresh complete set from the remaining candidates.
Any `operator_selection_pins_by_role` and `required_flake_ids` in `candidate_prefilters_by_role` are binding operator choices. Select those exact flakes for those roles; do not substitute another candidate. If a pinned flake lacks required evidence or is physically unusable, return blocked instead of replacing it.
Apply every role-specific soft target in `candidate_prefilters_by_role`. In particular, for graphene quantum Hall hBN, approximately 1200 um^2 is preferred after hard coverage and shape feasibility; do not assume a larger hBN area is better.
For a graphene quantum Hall active channel, 10 um x 20 um is preferred and 5 um x 10 um is the hard minimum. If 10 um x 20 um does not fit, judge whether a compact Hall channel between those sizes fits the continuous usable graphene at zero rotation before rejecting it. Do not add the downstream 3.0 um gate-metal-to-contact-metal clearance as a graphene-flake margin.

Write exactly one JSON object to `{result_path}` with this structure:
{{
  "status": "ok" or "blocked",
  "selected_flakes_by_role": {{"role": [{{all original candidate fields, "quality_assessment": {{"geometry_fitness": 0-100, "visible_cleanliness": 0-100, "continuity_uniformity": 0-100, "evidence_confidence": 0-100, "inventory_tiebreaker": 0-100, "overall_score": 0-100, "image_read_confirmed": true, "real_image_path": "exact image_100x_path", "image_20x_read_confirmed": true, "real_image_20x_path": "exact image_20x_path", "segmentation_disagreements": [], "observed_strengths": [], "observed_risks": []}}, "selection_reason": "..."}}]}},
  "selected_flake_set": [{{"role": "...", "flake_id": null}}],
  "ranked_candidates_by_role": {{"role": [{{"flake_id": null, "overall_score": 0-100, "feasible": true, "image_read_confirmed": true, "reason": "..."}}]}},
  "rejected_candidates_by_role": {{}},
  "unusable_roles": [],
  "retry_requests": [{{"role": "...", "material": "...", "reason": "...", "evidence": [], "required_improvements": [], "action": "exfoliate_and_search_one_chip"}}],
  "open_issues": [],
  "selection_summary": "..."
}}
When status is ok, select exactly the required count of unique physical flakes for every role and flatten all of them into selected_flake_set; do not return only one representative flake. Every selection must come from that role's supplied candidates and must have both real optical images you actually opened. A missing 20X segmentation mask is not a selection blocker; code will flag it for segmentation/relinking before real-flake design. If optically assessed candidates exist but none is fabrication-quality, write status=blocked with role-specific retry_requests and action `exfoliate_and_search_one_chip`. Do not rotate masks, perform layout design, or ask for approval. The Qumus UI handles approval after code validation.
"""
    prompt_path = run_dir / "claude_sdk_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    if not _sdk_available():
        return {
            "status": "unavailable",
            "message": "Claude Agent SDK or Anthropic authentication is not configured.",
            "output_dir": str(run_dir),
            "human_review_required": False,
        }

    started = time.time()
    try:
        from common.async_run import run_sdk_coro

        records = run_sdk_coro(_run_sdk(prompt, model, timeout_seconds))
        stream_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        if not result_path.exists():
            raise ValueError("Claude SDK did not write flake_selection_result.json.")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("flake_selection_result.json must contain a JSON object.")
        if result.get("status") == "blocked":
            existing_requests = result.get("retry_requests")
            existing_requests = existing_requests if isinstance(existing_requests, list) else []
            blocked_roles = [
                str(item.get("role"))
                for item in existing_requests
                if isinstance(item, dict) and item.get("role")
            ]
            if not blocked_roles:
                raw_unusable = result.get("unusable_roles")
                blocked_roles = [str(role) for role in raw_unusable] if isinstance(raw_unusable, list) else []
            if not blocked_roles:
                blocked_roles = list(roles)
            generated = _blocked_for_roles(
                candidate_payload,
                [
                    (str(role), "Claude found no fabrication-quality feasible choice for this role.")
                    for role in blocked_roles
                ],
            )
            generated_by_role = {item["role"]: item for item in generated["retry_requests"]}
            normalized_requests: list[dict[str, Any]] = []
            for role in blocked_roles:
                base = generated_by_role[str(role)]
                supplied = next(
                    (
                        item
                        for item in existing_requests
                        if isinstance(item, dict) and str(item.get("role")) == str(role)
                    ),
                    {},
                )
                normalized = {**base, **supplied}
                normalized["role"] = str(role)
                normalized["action"] = "exfoliate_and_search_one_chip"
                normalized_requests.append(normalized)
            result["retry_requests"] = normalized_requests
            result["unusable_roles"] = blocked_roles
            result["blocker_type"] = "optically_assessed_candidates_unusable"
            result["exfoliation_allowed"] = True
        read_paths = _sdk_read_paths(records)
        result = validate_selection_result(
            result,
            candidate_payload,
            sdk_read_paths=read_paths,
        )
        result["real_image_inspection_audit"] = {
            "inspection_tool": "Claude Code SDK Read",
            "read_paths": sorted(read_paths),
            "all_supplied_real_100x_images_read": True,
            "all_supplied_real_20x_images_read": True,
            "all_available_20x_masks_read": True,
        }
        result["human_rejection_history"] = human_rejection_history
        result["excluded_flake_identities"] = sorted(excluded_identities)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "error_type": exc.__class__.__name__,
            "output_dir": str(run_dir),
            "elapsed_seconds": round(time.time() - started, 3),
            "human_review_required": False,
        }

    if result.get("status") != "ok":
        return {
            **result,
            "output_dir": str(run_dir),
            "selection_result_path": str(result_path),
            "elapsed_seconds": round(time.time() - started, 3),
            "human_review_required": False,
        }

    gate = HumanGateService().create_flake_selection_gate(
        selection_result_path=str(result_path),
        selected_flakes_by_role=result["selected_flakes_by_role"],
        session_key=session_key or os.getenv("QUMUS_UI_SESSION_ID", ""),
        project_id=project_id or str(design_context.get("project_id") or ""),
        output_dir=str(run_dir),
    )
    effective_project_id = project_id or str(design_context.get("project_id") or "")
    if effective_project_id:
        try:
            from common.services.project_state_service import ProjectStateService

            ProjectStateService().save_status(
                project_id=effective_project_id,
                status="to_be_continued",
                current_stage="waiting_for_flake_selection_approval",
                next_action=f"Approve or reject human gate {gate['gate_id']} in the Qumus UI.",
                summary="Claude SDK selected high-quality real flakes; waiting for temporary human approval or rejection.",
                metadata={
                    "flake_selection_gate_id": gate["gate_id"],
                    "flake_selection_result_path": str(result_path),
                },
            )
        except Exception:
            pass
    event_payload = json.dumps(gate, ensure_ascii=False, separators=(",", ":"))
    _console_print(f"[HUMAN_FLAKE_GATE] {event_payload}")
    try:
        gate_decision = HumanGateService().wait_for_approval(
            gate["gate_id"],
            timeout_seconds=max(1, int(approval_timeout_seconds)),
            on_wait=lambda: _console_print(f"[HUMAN_FLAKE_GATE_WAITING] {gate['gate_id']}"),
        )
    except TimeoutError:
        return {
            "status": "awaiting_human_approval",
            "message": "The approval wait timed out; the persisted gate can be resumed after approval.",
            "output_dir": str(run_dir),
            "selection_result_path": str(result_path),
            "gate": gate,
            "human_review_required": True,
            "autonomous_continuation": False,
            "elapsed_seconds": round(time.time() - started, 3),
        }
    if gate_decision.get("status") == "rejected":
        rejected_flakes = [
            item for item in gate_decision.get("rejected_flakes") or [] if isinstance(item, dict)
        ]
        rejected_identities = {_candidate_identity(item) for item in rejected_flakes}
        rejected_identities = {
            identity for identity in rejected_identities if identity and not identity.endswith("mask:")
        }
        if not rejected_identities:
            return {
                "status": "error",
                "message": "Human rejection did not identify any selected physical flake.",
                "error_type": "InvalidHumanRejection",
                "gate": gate_decision,
                "output_dir": str(run_dir),
                "human_review_required": False,
            }
        rejection_entry = {
            "gate_id": gate_decision.get("gate_id"),
            "scope": gate_decision.get("rejection_scope"),
            "reason": gate_decision.get("rejection_reason") or "",
            "rejected_flakes": rejected_flakes,
        }
        next_history = [*human_rejection_history, rejection_entry]
        next_excluded = excluded_identities | rejected_identities
        _console_print(
            "[HUMAN_FLAKE_GATE_REJECTED] "
            + json.dumps(rejection_entry, ensure_ascii=False, separators=(",", ":"))
        )
        next_context = dict(design_context)
        next_context["design_candidate_payload"] = candidate_payload
        next_context["human_rejection_history"] = next_history
        next_context["human_rejected_flakes"] = rejected_flakes
        next_context["excluded_flake_identities"] = sorted(next_excluded)
        return run_claude_sdk_flake_selection(
            design_context=next_context,
            project_id=project_id,
            output_dir=str(run_dir),
            session_key=session_key,
            model=model,
            timeout_seconds=timeout_seconds,
            approval_timeout_seconds=approval_timeout_seconds,
        )
    approved = gate_decision
    return {
        "status": "ok",
        "message": "Claude SDK selected the complete image-verified flake set and the human approved it; continue this same agent run.",
        "output_dir": str(run_dir),
        "selection_result_path": str(result_path),
        "gate": approved,
        "approved_candidate_context": approved["approved_candidate_context"],
        "selected_flakes_by_role": result["selected_flakes_by_role"],
        "selected_flake_set": result["selected_flake_set"],
        "design_mask_preparation_required": result.get("design_mask_preparation_required", False),
        "selected_flakes_missing_design_masks": result.get("selected_flakes_missing_design_masks") or [],
        "human_review_required": False,
        "human_approval_status": "approved",
        "human_rejection_history": result.get("human_rejection_history") or [],
        "excluded_flake_identities": result.get("excluded_flake_identities") or [],
        "autonomous_continuation": True,
        "elapsed_seconds": round(time.time() - started, 3),
    }


@tool
def run_claude_sdk_flake_selection_tool(
    design_context_json: str,
    project_id: str = "",
    output_dir: str = "",
    timeout_seconds: int = 900,
    approval_timeout_seconds: int = 86400,
) -> str:
    """Select a complete real-image-verified set and pause for approval or in-run reselection.

    Args:
        design_context_json: JSON object returned by get_project_design_context, including the existing role-grouped candidates.
        project_id: Optional resumable Qumus project id.
        output_dir: Optional persistent output directory for selection artifacts.
        timeout_seconds: Claude SDK selection timeout in seconds.
        approval_timeout_seconds: Maximum in-process human-gate wait before restart fallback; rejection reselects in the same call.
    """
    context = _json_object(design_context_json, "design_context_json")
    result = run_claude_sdk_flake_selection(
        design_context=context,
        project_id=project_id,
        output_dir=output_dir,
        timeout_seconds=timeout_seconds,
        approval_timeout_seconds=approval_timeout_seconds,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)
