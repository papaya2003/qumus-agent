"""Context limits belong to an exact model and endpoint, never a model family."""

from __future__ import annotations

from typing import Any

from qumus2.agents.token_usage import token_count
from qumus2.config import load_yaml

UNSET = object()


def context_defaults(creds: Any) -> dict[str, int | None]:
    """Owner-approved catalog defaults, scoped to this exact gateway and model ID."""
    catalog = load_yaml("model_context_limits.yaml")
    limits = {}
    if creds.base_url.rstrip("/") == catalog["base_url"]:
        limits = {model: token_count(value) or None for model, value in catalog["models"].items()}
    # A positive environment override belongs only to its accompanying model ID.
    if creds.context_window_tokens is not None:
        limits[creds.model_id] = creds.context_window_tokens
    else:
        limits.setdefault(creds.model_id, None)
    return limits


def context_overrides(state: dict[str, Any], creds: Any) -> dict[str, int | None]:
    saved = state.get("openai_context_limits", {}).get(creds.base_url, {})
    return {model: value for model, value in saved.items()
            if isinstance(model, str) and (value is None or (token_count(value) or 0) > 0)}


def context_limits(state: dict[str, Any], creds: Any) -> dict[str, int | None]:
    return context_defaults(creds) | context_overrides(state, creds)
