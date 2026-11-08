"""Counts-only attribution of actual wire input, with explicit rough text estimates.

This is deliberately not a model tokenizer. UTF-8 bytes / 4 is a portable text
proxy for comparing prompt components. It neither replaces reported usage nor
estimates pixels, opaque reasoning or server-side message framing.
"""

from __future__ import annotations

import json
from typing import Any

METHOD = "utf8_bytes_div4_v1"
LAB_MARKER = "\nCurrent laboratory record (authoritative; do not repeat completed jobs):\n"
CATEGORIES = (
    "system_instructions", "reference_skills", "lab_snapshot", "current_task",
    "conversation_history", "tool_definitions", "tool_calls", "tool_results",
    "media", "protocol_unassigned",
)
_MEDIA = frozenset({"input_image", "image_url", "input_audio", "audio", "input_file",
                    "file", "input_video", "video_url", "file_url"})
_METADATA = frozenset({"type", "role", "id", "call_id", "tool_call_id", "status",
                       "encrypted_content", "signature", "annotations", "logprobs"})


def instruction_sections(text: Any, layout: dict[str, Any] | None) -> list[dict[str, str]] | None:
    """Use assembly offsets, never semantic guesses based on prompt headings."""
    if not isinstance(text, str) or not layout:
        return None
    prefix = layout["prefix"]
    if not text.startswith(prefix) or (text != prefix and not text[len(prefix):].startswith(LAB_MARKER)):
        return None
    sections, pos = [], 0
    for start, end in layout["references"]:
        sections.extend([{"category": "system_instructions", "text": prefix[pos:start]},
                         {"category": "reference_skills", "text": prefix[start:end]}])
        pos = end
    sections.extend([{"category": "system_instructions", "text": prefix[pos:]},
                     {"category": "lab_snapshot", "text": text[len(prefix):]}])
    return sections


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_plain_text(v) for v in value)
    if isinstance(value, dict):
        return value.get("text", "") if isinstance(value.get("text"), str) else ""
    return ""


def application_context_index(items: list[Any], hints: dict[str, Any]) -> int | None:
    """Validate the ephemeral last-item assembly hint against actual wire text.

    Headings and equal user messages are not provenance. Only the context item
    appended by the runtime, at its recorded position and role, can be attributed.
    """
    context = hints.get("application_context")
    if not isinstance(context, dict) or not isinstance(context.get("text"), str) or not items:
        return None
    item = items[-1]
    content = item.get("content") if isinstance(item, dict) else None
    text_only = (isinstance(content, str) or isinstance(content, list)
                 and len(content) == 1 and isinstance(content[0], dict)
                 and content[0].get("type") in {"text", "input_text"}
                 and set(content[0]) == {"type", "text"})
    if (context.get("role") == "developer" and isinstance(item, dict)
            and item.get("role") == "developer"
            and text_only and _plain_text(content) == context["text"]):
        return len(items) - 1
    return None


def input_composition(payload: dict[str, Any], api: str,
                      hints: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inspect post-conversion input; persist only counts and fixed identifiers."""
    hints = hints or {}
    sizes = dict.fromkeys(CATEGORIES, 0)
    items = dict.fromkeys(CATEGORIES, 0)
    uncertain: set[str] = set()
    opaque = 0

    def add(category: str, text: str) -> None:
        if text:
            sizes[category] += len(text.encode("utf-8", errors="replace"))
            items[category] += 1

    def content(value: Any, category: str) -> None:
        nonlocal opaque
        if isinstance(value, str):
            add(category, value)
        elif isinstance(value, list):
            for part in value:
                content(part, category)
        elif isinstance(value, dict):
            if value.get("type") in _MEDIA:
                items["media"] += 1
                return  # Never tokenize URLs, base64, pixels or file identifiers.
            if "encrypted_content" in value or "signature" in value:
                opaque += 1
            for key, part in value.items():
                if key not in _METADATA:
                    content(part, category)

    wire_items = payload.get("messages" if api == "chat_completions" else "input", [])
    if isinstance(wire_items, str):
        wire_items = [{"role": "user", "content": wire_items}]
    if not isinstance(wire_items, list):
        wire_items = [wire_items]
    snapshot_index = application_context_index(wire_items, hints)
    instructions = payload.get("instructions") or ""
    if api == "chat_completions":
        instructions = "".join(_plain_text(m.get("content")) for i, m in enumerate(wire_items)
                               if i != snapshot_index and isinstance(m, dict)
                               and m.get("role") in {"system", "developer"})
    sections = hints.get("instruction_sections")
    valid_sections = isinstance(sections, list) and all(
        isinstance(s, dict) and s.get("category") in CATEGORIES[:3]
        and isinstance(s.get("text"), str) for s in sections)
    if valid_sections and "".join(s["text"] for s in sections) == instructions:
        for section in sections:
            add(section["category"], section["text"])
    else:
        content(instructions, "protocol_unassigned")
        uncertain.update(CATEGORIES[:3])

    task = hints.get("current_task")
    candidates = [i for i, m in enumerate(wire_items) if isinstance(task, str)
                  and isinstance(m, dict) and m.get("role") == "user"
                  and _plain_text(m.get("content")) == task]
    task_index = candidates[-1] if candidates else None
    if task_index is None:
        uncertain.add("current_task")
    evidence = hints.get("tool_evidence_texts", [])
    evidence = set(evidence) if isinstance(evidence, list) else set()
    tools = payload.get("tools") or []
    if tools:
        add("tool_definitions", json.dumps(tools, ensure_ascii=False, separators=(",", ":")))

    for i, item in enumerate(wire_items):
        if not isinstance(item, dict):
            content(item, "protocol_unassigned")
            continue
        role, kind = item.get("role"), item.get("type")
        if i == snapshot_index:
            content(item.get("content"), "lab_snapshot")
            continue
        if role in {"system", "developer"}:
            continue
        if kind in {"function_call", "custom_tool_call"}:
            for key in ("name", "arguments", "input"):
                content(item.get(key), "tool_calls")
        elif kind in {"function_call_output", "custom_tool_call_output"} or role == "tool":
            content(item.get("output", item.get("content")), "tool_results")
        elif role in {"user", "assistant"}:
            if role == "assistant":
                category = "conversation_history"
            elif i == task_index:
                category = "current_task"
            elif _plain_text(item.get("content")) in evidence:
                category = "tool_results"
            elif task_index is not None:
                category = "conversation_history"
            else:
                category = "protocol_unassigned"
                uncertain.add("conversation_history")
            content(item.get("content"), category)
            content(item.get("refusal"), category)
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict):
                    content(call.get("function", {}).get("name"), "tool_calls")
                    content(call.get("function", {}).get("arguments"), "tool_calls")
            if item.get("function_call"):
                content(item["function_call"], "tool_calls")
        else:
            content(item, "protocol_unassigned")

    estimates = {key: (size + 3) // 4 for key, size in sizes.items()}
    components = []
    for key in CATEGORIES:
        status = ("unknown" if key == "protocol_unassigned" or (key == "media" and items[key])
                  else "partial" if key in uncertain and sizes[key]
                  else "unknown" if key in uncertain
                  else "estimated" if sizes[key] else "not_present")
        components.append({"category": key, "status": status, "text_bytes": sizes[key],
                           "item_count": items[key], "text_estimated_tokens": estimates[key],
                           "estimated_tokens": None if status == "unknown" else estimates[key]})
    return {"version": 1, "method": METHOD, "status": "partial" if uncertain else "available",
            "text_estimated_tokens": sum(estimates.values()), "components": components,
            "media_count": items["media"], "opaque_count": opaque}
