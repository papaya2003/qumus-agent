"""Project-scoped OpenAI/Token Station configuration. Secrets never enter prompts.

No fallback to OPENAI_API_KEY or the old Anthropic credentials: Qumus must not
silently charge another application or send its key to a different endpoint.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass(frozen=True)
class OpenAICredentials:
    api_key: str = field(repr=False)
    base_url: str = "https://api.openai.com/v1"
    api: str = "responses"
    model_id: str = ""
    input_price: float | None = None  # USD per million tokens; gateway's rate
    output_price: float | None = None
    cached_input_price: float | None = None  # Optional measured cache-read rate, for this model only.
    web_search: bool = False  # opt-in: gateway must support Responses web search
    stage_budget_usd: float | None = 200.0  # None disables only the stage-total gate.
    test_budget_usd: float = 40.0
    budget_path: str = "runs/openai-budget.db"
    stream: bool = True
    context_window_tokens: int | None = None  # actual deployment's input + output limit

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("QUMUS_OPENAI_BASE_URL must be an HTTP(S) API root ending in /v1")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("put credentials in QUMUS_OPENAI_API_KEY, never in the API URL")
        if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("remote model endpoints must use HTTPS")
        if self.api not in ("responses", "chat_completions"):
            raise ValueError("QUMUS_OPENAI_API must be responses or chat_completions")
        if self.base_url.rstrip("/").endswith(("/responses", "/chat/completions")):
            raise ValueError("base URL must end at /v1, without a model-call endpoint")
        if not self.api_key.strip() or not self.model_id.strip():
            raise ValueError("set both QUMUS_OPENAI_API_KEY and QUMUS_OPENAI_MODEL")
        if self.context_window_tokens is not None and (
                type(self.context_window_tokens) is not int or
                not 0 < self.context_window_tokens <= 2**53 - 1):
            raise ValueError("context window must be a positive integer token count, or unknown")
        for rate in (self.input_price, self.output_price, self.cached_input_price):
            if rate is not None and (not math.isfinite(rate) or rate < 0):
                raise ValueError("token prices must be finite non-negative USD per million")
        if (self.input_price is None) != (self.output_price is None):
            raise ValueError("set both input and output token prices, or leave both blank")
        if self.cached_input_price is not None and (
                isinstance(self.cached_input_price, bool) or self.input_price is None
                or self.cached_input_price > self.input_price):
            raise ValueError("cached input price requires input/output prices and cannot exceed input price")
        if self.web_search and self.api != "responses":
            raise ValueError("hosted web search requires the Responses API")
        budgets = (self.test_budget_usd,) if self.stage_budget_usd is None else (
            self.stage_budget_usd, self.test_budget_usd)
        for amount in budgets:
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError("spend budgets must be positive and finite")


def load_openai_credentials(dotenv: Path | None = Path(".env")) -> OpenAICredentials | None:
    values = _load_dotenv(dotenv) if dotenv else {}

    def get(name: str, default: str = "") -> str:
        return (os.environ.get(name) or values.get(name) or default).strip()

    key = get("QUMUS_OPENAI_API_KEY")
    if not key:
        return None

    def rate(name: str) -> float | None:
        value = get(name)
        return float(value) if value else None

    base = get("QUMUS_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    stage_budget = get("QUMUS_OPENAI_STAGE_BUDGET_USD", "200")
    return OpenAICredentials(
        api_key=key, base_url=get("QUMUS_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        api=get("QUMUS_OPENAI_API", "responses"), model_id=get("QUMUS_OPENAI_MODEL"),
        input_price=rate("QUMUS_OPENAI_INPUT_USD_PER_MILLION"),
        output_price=rate("QUMUS_OPENAI_OUTPUT_USD_PER_MILLION"),
        cached_input_price=rate("QUMUS_OPENAI_CACHED_INPUT_USD_PER_MILLION"),
        web_search=get("QUMUS_OPENAI_WEB_SEARCH", "0").lower() in ("1", "true"),
        stage_budget_usd=None if stage_budget.lower() == "unlimited" else float(stage_budget),
        test_budget_usd=float(get("QUMUS_OPENAI_TEST_BUDGET_USD", "40")),
        budget_path=get("QUMUS_OPENAI_BUDGET_DB", "runs/openai-budget.db"),
        stream=get("QUMUS_OPENAI_STREAM", "1" if base == "https://api.openai.com/v1" else "0").lower()
               in ("1", "true"),
        context_window_tokens=int(get("QUMUS_OPENAI_CONTEXT_WINDOW_TOKENS"))
                              if get("QUMUS_OPENAI_CONTEXT_WINDOW_TOKENS") else None,
    )
