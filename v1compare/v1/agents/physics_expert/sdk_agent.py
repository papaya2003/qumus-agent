"""Claude Agent SDK backend for the physics expert.

This gives the physics expert real internet access (WebSearch / WebFetch) so it
can ground material and device-stack recommendations in current literature.

It is a drop-in augmentation: `ProjectManagerPhysicsExpertTool` and the standalone
physics expert call `run_claude_sdk_physics_expert` when the Claude Agent SDK and
Anthropic auth are available, and fall back to the plain LiteLLM `call_model`
path otherwise. No KLayout / MCP wiring here — this backend only reads and
searches.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any

DEFAULT_PHYSICS_SDK_MODEL = (
    os.getenv("QUMUS_PHYSICS_SDK_MODEL")
    or os.getenv("QUMUS_CLAUDE_SDK_MODEL")
    or "claude-opus-5"
).strip() or "claude-opus-5"

_ALLOWED_TOOLS = ["WebSearch", "WebFetch", "Read", "Glob", "Grep"]

_SEARCH_GUIDANCE = """
When the question involves material choice, current state-of-the-art, record
device metrics, or any claim that benefits from recent literature, you MUST use
WebSearch (and WebFetch on promising results) before answering. Run several
distinct searches, prefer 2023-2025 sources and open-access papers (arXiv, PMC),
and cite bare URLs. If a value is not found in a source, write "not reported" —
never guess a number.
""".strip()


def claude_sdk_available() -> bool:
    # Hard kill switch: set QUMUS_DISABLE_PHYSICS_SDK=1 to force every physics-expert
    # call onto the plain chat model (no `claude` subprocess, no multi-minute web search).
    if os.getenv("QUMUS_DISABLE_PHYSICS_SDK", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if not (
        os.getenv("ANTHROPIC_API_KEY")
        or (os.getenv("ANTHROPIC_BASE_URL") and os.getenv("ANTHROPIC_AUTH_TOKEN"))
    ):
        return False
    try:  # pragma: no cover - import guard
        import claude_agent_sdk  # noqa: F401
    except Exception:
        return False
    return True


def _resolve_model(model: str) -> str:
    resolved = (model or DEFAULT_PHYSICS_SDK_MODEL).strip() or DEFAULT_PHYSICS_SDK_MODEL
    base_url = (os.getenv("ANTHROPIC_BASE_URL") or "").lower()
    if "bytefuture" in base_url and resolved.startswith("claude-"):
        return f"anthropic/{resolved}"
    return resolved


def _message_text(message: object) -> str:
    parts: list[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
    result = getattr(message, "result", None)
    if isinstance(result, str) and result.strip():
        parts.append(result.strip())
    return "\n".join(parts).strip()


def _stream_sdk_message(message: object, text: str = "") -> None:
    if os.getenv("QUMUS_STREAM_CLAUDE_SDK_LOGS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    prefix = "[CLAUDE_SDK][physics]"
    content = getattr(message, "content", None)
    if isinstance(content, list):
        tool_names: list[str] = []
        for block in content:
            name = getattr(block, "name", None)
            if name:
                tool_names.append(str(name))
        if tool_names:
            print(f"{prefix}[tool] {', '.join(tool_names)}", flush=True)
    if text:
        max_chars = 2000
        shown = text if len(text) <= max_chars else text[:max_chars].rstrip() + " ... [truncated]"
        print(f"{prefix}[text] {shown}", flush=True)
    else:
        result = getattr(message, "result", None)
        if isinstance(result, str) and result.strip():
            shown = result.strip()
            if len(shown) > 2000:
                shown = shown[:2000].rstrip() + " ... [truncated]"
            print(f"{prefix}[result] {shown}", flush=True)


async def _run_async(
    *,
    prompt: str,
    system_prompt: str,
    model: str,
    timeout_seconds: int,
) -> str:
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = ClaudeAgentOptions(
        model=_resolve_model(model),
        allowed_tools=list(_ALLOWED_TOOLS),
        permission_mode="acceptEdits",
        disallowed_tools=["Bash", "Write", "Edit"],
        system_prompt=system_prompt,
    )

    chunks: list[str] = []

    async def collect() -> None:
        stream = query(prompt=prompt, options=options)
        if inspect.isawaitable(stream):
            stream = await stream
        async for message in stream:
            text = _message_text(message)
            _stream_sdk_message(message, text)
            if text:
                chunks.append(text)

    await asyncio.wait_for(collect(), timeout=max(1, int(timeout_seconds)))
    # The last substantive chunk is the assistant's final answer.
    return chunks[-1] if chunks else ""


def run_claude_sdk_physics_expert(
    query_text: str,
    *,
    system_prompt: str,
    setup_context: str = "",
    conversation_context: str = "",
    model: str = DEFAULT_PHYSICS_SDK_MODEL,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Answer a physics/materials question with internet access.

    Returns {"status": "ok"|"unavailable"|"error", "text": str, ...}.
    Callers should fall back to the LiteLLM path on any non-"ok" status.
    """
    if not claude_sdk_available():
        return {
            "status": "unavailable",
            "text": "",
            "reason": "claude_agent_sdk not installed or ANTHROPIC auth not configured",
        }

    sections = [system_prompt.strip(), "", _SEARCH_GUIDANCE]
    if setup_context.strip():
        sections += ["", "CURRENT LAB SETUP (ground your advice in this):", setup_context.strip()]
    full_system = "\n".join(sections)

    prompt_parts: list[str] = []
    if conversation_context.strip():
        prompt_parts.append("Conversation so far:\n" + conversation_context.strip())
    prompt_parts.append(query_text.strip())
    prompt = "\n\n".join(prompt_parts)

    try:
        from common.async_run import run_sdk_coro

        text = run_sdk_coro(
            _run_async(
                prompt=prompt,
                system_prompt=full_system,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        )
    except Exception as exc:  # pragma: no cover - network/SDK failure
        return {"status": "error", "text": "", "reason": str(exc)}

    if not text:
        return {"status": "error", "text": "", "reason": "empty SDK response"}
    return {"status": "ok", "text": text, "backend": "claude_agent_sdk", "model": _resolve_model(model)}
