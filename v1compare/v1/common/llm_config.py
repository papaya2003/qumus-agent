from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency in some runtimes
    load_dotenv = None  # type: ignore[assignment]

# Ensure .env values are loaded before reading default model configuration.
if load_dotenv is not None:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env", override=False)

# Optional env-configured default model for the whole stack.
DEFAULT_MODEL_ID = os.getenv("QUMUS_DEFAULT_MODEL_ID", "").strip()

# Hard bounds on every LLM call so a stalled request / connection can never block
# a run forever. Applies process-wide to litellm (smolagents LiteLLMModel and the
# tools that call litellm.completion directly). Override via env if needed.
LLM_REQUEST_TIMEOUT = float(os.getenv("QUMUS_LLM_TIMEOUT", "240"))
LLM_NUM_RETRIES = int(os.getenv("QUMUS_LLM_RETRIES", "1"))


def configure_litellm() -> None:
    """Idempotent global litellm setup. Safe to call many times / from any entry point."""
    try:
        import litellm
    except Exception:  # pragma: no cover - litellm optional in some tooling contexts
        return
    litellm.drop_params = True
    # per-request wall-clock cap; litellm raises Timeout which callers/agents can handle
    litellm.request_timeout = LLM_REQUEST_TIMEOUT
    litellm.num_retries = LLM_NUM_RETRIES


configure_litellm()


def resolve_model_id(model_id: str | None = None) -> str:
    resolved = (model_id or DEFAULT_MODEL_ID).strip()
    if resolved:
        return resolved
    raise RuntimeError("model_id is required. Pass --model-id or set QUMUS_DEFAULT_MODEL_ID.")


def get_api_key_for_model(model_id: str | None = None) -> str | None:
    _ = model_id
    return os.getenv("QUMUS_API_KEY")


def get_api_base_for_model(model_id: str | None = None) -> str | None:
    resolved = (model_id or "").strip().lower()
    explicit_base = (os.getenv("QUMUS_API_BASE") or "").strip()
    if explicit_base:
        return explicit_base

    # DashScope/Qwen commonly requires region-specific routing.
    if resolved.startswith("dashscope/"):
        return (os.getenv("DASHSCOPE_BASE_URL") or "").strip() or None
    return None
