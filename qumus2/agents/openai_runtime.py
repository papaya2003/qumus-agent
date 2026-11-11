"""OpenAI Agents SDK adapter for Qumus' existing AgentRuntime contract.

The SDK owns model/tool iteration. Qumus owns policy, the sample ledger, jobs,
safe stopping and recovery. Every domain tool goes through ToolGateway.call;
observational SDK hooks are never used as the only permission boundary.
Specialists are agents-as-tools, with sequential execution and checked reports.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from qumus2.agents.context_composition import instruction_sections
from qumus2.agents.model_context import UNSET, context_defaults, context_overrides
from qumus2.agents.model_settings import agent_model_choices
from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.agents.roster import ORCHESTRATOR, SUBAGENTS, load_roster
from qumus2.agents.skills import skill_text
from qumus2.agents.token_usage import reported_usage
from qumus2.config import load_yaml
from qumus2.core.run_paths import original_run_path, resolve_run_path
from qumus2.core.scrub import scrub_text


class BudgetExceeded(RuntimeError):
    """Stop the model loop before another call when its configured budget is spent."""


DEFAULT_CONTEXT_IMAGES = 24


def context_image_limit() -> int:
    """How many earlier images one request carries (QUMUS_OPENAI_MAX_CONTEXT_IMAGES)."""
    raw = os.environ.get("QUMUS_OPENAI_MAX_CONTEXT_IMAGES", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_CONTEXT_IMAGES
    except ValueError:
        return DEFAULT_CONTEXT_IMAGES


def unique_image_input(data: Any, max_images: int | None = None, *, diagnostic_observer: Any = None) -> Any:
    """Bound visual/catalogue context while preserving every tool identity.

    Old pixels can be requested again through the sample tools. Superseded
    complete project snapshots are replaced by references to the latest one;
    raw historical flake payloads use the same catalogue view as current tools.
    Omissions are explicit and never modify the persisted history.
    """
    from agents.run_config import ModelInputData

    seen: set[str] = set()
    images: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "input_image" and value.get("image_url"):
                images.append(json.dumps(value, sort_keys=True))
            else:
                for v in value.values():
                    collect(v)
        elif isinstance(value, list):
            for v in value:
                collect(v)

    items = data.model_data.input
    catalogue_calls = {item.get('call_id') for item in items if isinstance(item, dict)
                       and item.get('type') == 'function_call'
                       and item.get('name') == 'list_samples'}
    compacted_catalogues = 0

    def catalogue_text(value: str) -> str:
        nonlocal compacted_catalogues
        from qumus2.agents.tools.inventory import flake_catalogue_view

        try:
            payload = json.loads(value)
        except (ValueError, TypeError):
            return value
        body = payload.get('data') if isinstance(payload, dict) else None
        rows = body.get('samples') if isinstance(body, dict) else None
        if not isinstance(rows, list) or not any(
                isinstance(row, dict) and row.get('kind') == 'flake'
                and not row.get('record_reference') for row in rows):
            return value
        body['samples'] = [flake_catalogue_view(row) if isinstance(row, dict)
                           and row.get('kind') == 'flake' else row for row in rows]
        body['historical_catalogue_projection'] = (
            'Observed catalogue metadata only; full contours, masks and measurement '
            'receipts remain in the original sample records and journal. Historical '
            'availability is not reassessed. Picker and Designer validate full records.')
        compacted_catalogues += 1
        return json.dumps(payload, ensure_ascii=False)

    def catalogue_item(item: Any) -> Any:
        if not (isinstance(item, dict) and item.get('type') == 'function_call_output'
                and item.get('call_id') in catalogue_calls):
            return item
        output = item.get('output')
        if isinstance(output, str):
            return {**item, 'output': catalogue_text(output)}
        if isinstance(output, list):
            return {**item, 'output': [
                {**part, 'text': catalogue_text(part['text'])}
                if isinstance(part, dict) and part.get('type') == 'input_text'
                and isinstance(part.get('text'), str) else part for part in output]}
        return item

    items = [catalogue_item(item) for item in items]
    snapshots = {item.get('call_id') for item in items if isinstance(item, dict)
                 and item.get('type') == 'function_call' and item.get('name') == 'get_project_context'}
    results = [item.get('call_id') for item in items if isinstance(item, dict)
               and item.get('type') == 'function_call_output' and item.get('call_id') in snapshots]
    if len(results) > 1:
        old = set(results[:-1])
        items = [{**item, 'output': '[Earlier full project snapshot superseded by the latest '
                  'get_project_context result below. The original is retained in the run record.]'}
                 if isinstance(item, dict) and item.get('type') == 'function_call_output'
                 and item.get('call_id') in old else item for item in items]
    # Images the latest tool results just returned are always shown: a picker that
    # retrieves 6 views to compare must see all 6 (bounding them made it retrieve
    # again and again). Older images are kept whole per tool result, newest first,
    # up to the bound, so a picker comparing three flakes keeps all three sets
    # instead of losing the previous flake each time it views the next one.
    if max_images is None:
        max_images = context_image_limit()
    tail = len(items)
    while tail > 0 and isinstance(items[tail - 1], dict) \
            and items[tail - 1].get('type') == 'function_call_output':
        tail -= 1
    collect(items[tail:])
    retained = set(images)
    for item in reversed(items[:tail]):
        images.clear()
        collect(item)
        group = [i for i in dict.fromkeys(reversed(images)) if i not in retained]
        room = max(1, max_images) - len(retained)
        if len(group) > room:
            retained.update(group[:max(0, room)])  # newest first; the bound ends here
            break
        retained.update(group)
    actions = {"superseded_snapshots": max(0, len(results) - 1),
               "omitted_images": 0, "duplicate_images": 0}
    if compacted_catalogues:
        actions['compacted_catalogues'] = compacted_catalogues

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            if value.get("type") == "input_image" and value.get("image_url"):
                key = json.dumps(value, sort_keys=True)
                if key not in retained:
                    actions["omitted_images"] += 1
                    return {"type": "input_text", "text": (
                        "[Earlier image omitted from this request to bound visual context. "
                        "Your earlier view of it still counts and its text record is unchanged. "
                        "Retrieve the sample's images again only if a direct comparison needs it.]")}
                if key in seen:
                    actions["duplicate_images"] += 1
                    return {"type": "input_text", "text": "[Same image attached earlier in this request.]"}
                seen.add(key)
            return {k: visit(v) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        return value

    filtered = ModelInputData(input=visit(items), instructions=data.model_data.instructions)
    if diagnostic_observer is not None:
        try:
            diagnostic_observer(actions)
        except Exception:
            pass  # Optional diagnostic attribution cannot reject valid input.
    return filtered


def chat_session_input(history: list[Any], new_input: list[Any]) -> list[Any]:
    """Reject ambiguous saved tool identities before SDK session deduplication."""
    combined = [*history, *new_input]
    seen: set[tuple[str, str]] = set()
    for item in combined:
        if not isinstance(item, dict) or item.get("type") not in ("function_call", "function_call_output"):
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            continue  # The transport adapter reports missing/unpaired identities.
        key = (item["type"], call_id)
        if key in seen:
            raise ValueError("Cannot associate Chat Completions tool history: duplicate call ID.")
        seen.add(key)
    return combined


def transport_model_input(data: Any, api: str, *, evidence_observer: Any = None,
                          diagnostic_observer: Any = None) -> Any:
    """Adapt retained tool images without changing saved SDK session history.

    Chat Completions tool messages accept text only. Put the images in user
    content after *all* replies to an assistant's tool-call group, labelled as
    tool evidence. The SDK then converts those images through its public input
    types; no SDK internals or HTTP bodies are patched. Responses stays native.
    """
    from agents.run_config import ModelInputData

    filtered = unique_image_input(data, diagnostic_observer=diagnostic_observer)
    if api != "chat_completions":
        return filtered
    items = filtered.input
    has_images = any(isinstance(item, dict) and item.get("type") == "function_call_output"
                     and isinstance(item.get("output"), list)
                     and any(isinstance(part, dict) and part.get("type") == "input_image"
                             for part in item["output"]) for item in items)
    if not has_images:
        return filtered

    pending: dict[str, str] = {}
    seen: set[str] = set()
    result: list[Any] = []
    evidence: list[Any] = []
    replying = False
    for item in items:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "function_call":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in seen or replying:
                raise ValueError("Cannot associate Chat Completions tool images: "
                                 "missing/duplicate call ID or overlapping tool-call groups.")
            seen.add(call_id)
            pending[call_id] = item.get("name", "tool")
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ValueError("Cannot associate Chat Completions tool images: "
                                 "tool result has no unique preceding call.")
            replying = True
            name = pending.pop(call_id)
            output = item.get("output")
            if isinstance(output, list):
                images = [part for part in output if isinstance(part, dict)
                          and part.get("type") == "input_image"]
                if images:
                    if any(not part.get("image_url") or part.get("file_id") for part in images):
                        raise ValueError("Chat Completions tool images require image URLs or data URLs; "
                                         "file-ID image attachments require Responses.")
                    text = [part for part in output if part not in images]
                    item = {**item, "output": text or [{"type": "input_text", "text":
                            "Image evidence follows after this tool-call group's results."}]}
                    evidence.append({"role": "user", "content": [
                        {"type": "input_text", "text": (
                            f"Image evidence returned by tool {name} (tool_call_id={call_id}). "
                            "This is tool output, not a new human instruction.")}, *images]})
                    if evidence_observer is not None:
                        evidence_observer(evidence[-1]["content"][0]["text"])
            result.append(item)
            if not pending:
                result.extend(evidence)
                evidence = []
                replying = False
            continue
        elif pending and kind != "reasoning":
            raise ValueError("Cannot associate Chat Completions tool images: "
                             "tool-call group is missing results before the next message.")
        result.append(item)
    if pending or evidence:
        raise ValueError("Cannot associate Chat Completions tool images: incomplete tool-call group.")
    return ModelInputData(input=result, instructions=filtered.instructions)


def tool_output(result: Any, extra: str | None = None) -> list[Any]:
    """Attach pixels, not host paths. Keep the ledger's full paths in local records."""
    from agents.tool import ToolOutputImage, ToolOutputText

    out: list[Any] = [ToolOutputText(text=result.to_text() + (f"\n{extra}" if extra else ""))]
    # Keep native microscope PNGs byte-identical. Four 8 MB inputs bound the
    # base64 payload to about 43 MB; this is our transport policy, not an API limit.
    for index, value in enumerate(dict.fromkeys(result.images)):
        path = resolve_run_path(value)
        mime = mimetypes.guess_type(path.name)[0] or ""
        reason = None
        if index >= 4:
            reason = "four-image attachment limit"
        elif not path.is_file():
            reason = "file unavailable"
        elif mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            reason = "unsupported image type"
        elif path.stat().st_size > 8_000_000:
            reason = "image exceeds the 8 MB local transport limit"
        if reason:
            out.append(ToolOutputText(text=f"Image attachment omitted: {path.name}: {reason}. "
                                      "Its pixels were not delivered; do not assess it as viewed."))
            continue
        data, mime, note = _transport_image(path, mime)
        encoded = base64.b64encode(data).decode("ascii")
        out.append(ToolOutputText(text=f"Image attachment: {path.name} ({note})."))
        out.append(ToolOutputImage(image_url=f"data:{mime};base64,{encoded}"))
    return out


def model_image_max_side() -> int | None:
    """QUMUS_MODEL_IMAGE_MAX_SIDE: longest side (px) of images sent to the model.

    Unset keeps original bytes. Some gateways fetch multimodal inputs with a
    short deadline and time out on several multi-MB 4K PNGs per request."""
    raw = os.environ.get("QUMUS_MODEL_IMAGE_MAX_SIDE", "").strip()
    if not raw:
        return None
    value = int(raw)
    if value < 256:
        raise ValueError("QUMUS_MODEL_IMAGE_MAX_SIDE must be at least 256")
    return value


def _transport_image(path: Path, mime: str) -> tuple[bytes, str, str]:
    """Bytes sent to the model. Local files and records always keep the original."""
    data = path.read_bytes()
    limit = model_image_max_side()
    if limit is None:
        return data, mime, "original bytes"
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if max(width, height) <= limit:
            return data, mime, "original bytes"
        scale = limit / max(width, height)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        small = image.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    buffer = BytesIO()
    small.save(buffer, format="JPEG", quality=90)
    return (buffer.getvalue(), "image/jpeg",
            f"downscaled for transport from {width}x{height} to {size[0]}x{size[1]} JPEG; "
            "local analysis used the full-resolution original")


def max_output_tokens(cfg: dict[str, Any]) -> int:
    """Per-request output cap: QUMUS_OPENAI_MAX_OUTPUT_TOKENS, else agents.yaml.

    Reasoning models that count their thinking as output (e.g. qwen3.8-max)
    need more room than the 4096 default for long orchestrator turns."""
    raw = os.environ.get("QUMUS_OPENAI_MAX_OUTPUT_TOKENS", "").strip()
    value = int(raw) if raw else int(cfg.get("openai", {}).get("max_output_tokens", 4096))
    if value <= 0:
        raise ValueError("max output tokens must be a positive integer")
    return value


def http_timeout_s() -> float:
    """Per-request HTTP timeout: QUMUS_OPENAI_HTTP_TIMEOUT_S, else 120 s.

    Slow reasoning models can take longer than 120 s to answer one request."""
    raw = os.environ.get("QUMUS_OPENAI_HTTP_TIMEOUT_S", "").strip()
    value = float(raw) if raw else 120.0
    if not 0 < value < float("inf"):
        raise ValueError("HTTP timeout must be a positive number of seconds")
    return value


#: HTTP replies that mean the provider did not process the request.
REFUSED_UNPROCESSED = frozenset({429, 503})
#: HTTP replies after which the model service is worth asking again later
#: (overloaded, rate limited, gateway/upstream failure).
OUTAGE_STATUS = frozenset({429, 500, 502, 503, 504})


def http_max_retries_from_env(default: int) -> int:
    """QUMUS_OPENAI_HTTP_RETRIES overrides the per-request HTTP retry count."""
    raw = os.environ.get("QUMUS_OPENAI_HTTP_RETRIES", "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < 0:
        raise ValueError("QUMUS_OPENAI_HTTP_RETRIES must be a nonnegative integer")
    return value


_RETRY_AFTER_TEXT = re.compile(r'retry-after(-ms)?\\?"?\s*[:=]\s*\\?"?(\d+(?:\.\d+)?)', re.I)


def _retry_after_s(exc: BaseException) -> float | None:
    """Seconds the provider asked us to wait: headers first, then the error text
    (the gateway forwards the upstream headers inside its JSON message)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        if headers.get("retry-after-ms"):
            return float(headers["retry-after-ms"]) / 1000.0
        if headers.get("retry-after"):
            return float(headers["retry-after"])
    except (TypeError, ValueError):
        pass
    found = [(m.group(1), float(m.group(2))) for m in _RETRY_AFTER_TEXT.finditer(str(exc))]
    for ms, value in found:
        if not ms:
            return value
    return found[0][1] / 1000.0 if found else None


def model_outage(exc: BaseException) -> dict[str, Any] | None:
    """{"status", "retry_after_s"} when `exc` (or what it wraps) means the model
    service was unavailable rather than our request being wrong; else None."""
    try:
        import openai
    except ImportError:  # pragma: no cover - the SDK runtime needs openai anyway
        return None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, openai.APIStatusError):
            if current.status_code in OUTAGE_STATUS:
                return {"status": current.status_code, "retry_after_s": _retry_after_s(current)}
            return None
        if isinstance(current, openai.APIConnectionError):  # includes APITimeoutError
            return {"status": None, "retry_after_s": None}
        current = current.__cause__ or current.__context__
    return None

class OpenAIRuntime:
    # Keep the existing UI/event contract; provider identifies which SDK backs it.
    mode, provider = "sdk", "openai"

    def __init__(self, gateway: Any, creds: OpenAICredentials, *,
                 run_budget_usd: float | None = None, model_provider: Any = None,
                 pre_send_admission: Any = None, http_max_retries: int = 2,
                 config: dict[str, Any] | None = None) -> None:
        if type(http_max_retries) is not int or http_max_retries < 0:
            raise ValueError("http_max_retries must be a nonnegative integer")
        self._pre_send_admission = pre_send_admission
        self._http_max_retries = http_max_retries_from_env(http_max_retries)
        self.gateway, self.store, self.creds = gateway, gateway.store, creds
        state = (self._project() or {}).get('state', {})
        self._context_defaults = context_defaults(creds)
        self._context_overrides = context_overrides(state, creds)
        self._context_limits = self._context_defaults | self._context_overrides
        self._context_default_sources = {model: 'administrator_table' if limit else None
                                         for model, limit in self._context_defaults.items()}
        if creds.context_window_tokens:
            self._context_default_sources[creds.model_id] = 'configured'
        saved = state.get('openai_model_config') or {}
        self._agent_models = agent_model_choices(saved.get('agent_models', {})) if (
            saved.get('base_url') == creds.base_url) else {}
        if saved.get('base_url') == creds.base_url:
            # Legacy settings cannot borrow a cache quote from another model or tariff.
            same_tariff = (saved.get('model'), saved.get('input_price'), saved.get('output_price')) == (
                creds.model_id, creds.input_price, creds.output_price)
            self.creds = replace(creds, model_id=saved['model'], input_price=saved['input_price'],
                                 output_price=saved['output_price'],
                                 cached_input_price=saved.get('cached_input_price',
                                     creds.cached_input_price if same_tariff else None),
                                 context_window_tokens=self._context_limits.get(saved['model']))
        else:
            self.creds = replace(creds, context_window_tokens=self._context_limits.get(creds.model_id))
        self.cfg = config if config is not None else load_yaml("agents.yaml")
        self.run_budget_usd = run_budget_usd
        self._model_provider = model_provider  # test injection, real SDK loop in both cases
        self._client: Any = None
        self._agents: dict[str, Any] = {}
        self._config: Any = None
        self._hooks: Any = None
        self._session: Any = None
        self._active: Any = None
        self._busy = False
        self._interrupted = False
        self._done = asyncio.Event()
        self._done.set()
        self._turn_cost = 0.0
        self._turn_tokens = 0
        self._turn_requests = 0
        self._budget_hit: str | None = None
        self._exhausted_told = False
        self.last_result: Any = None
        self._ledger: Any = None
        self._reservations: dict[str, str] = {}
        # Sends per model call still without a definite "not processed" reply
        # (HTTP 429/503). Only a call whose every send was refused that way
        # may release its spend reservation.
        self._unanswered: dict[str, int] = {}
        self._budget_scope = str(original_run_path(self.store.path))
        self._last_messages: dict[str, str] = {}
        self._next_model_call = 0.0
        self._pacing_wait: asyncio.Task[None] | None = None
        self._request_context: dict[str, Any] = {}
        self._composition_layout: dict[str, Any] = {}
        self._composition_tasks: dict[str, str] = {}
        self._composition_evidence: list[str] = []
        self._composition_snapshot: str = ""
        self._composition_filter_actions: dict[str, int] = {}
        self._metrics: Any = None
        proj = self._project()
        self.session_id = ((proj or {}).get("state", {}).get("openai_session_id")
                           or f"openai-{uuid4().hex}")
        if gateway.project_id:
            self.store.update_project_state(gateway.project_id, openai_session_id=self.session_id)

    def _project(self) -> dict[str, Any] | None:
        return self.store.get_project(self.gateway.project_id) if self.gateway.project_id else None

    def _context_limit_source(self, model: str) -> str | None:
        if not self._context_limits.get(model):
            return None
        return 'configured' if model in self._context_overrides else self._context_default_sources.get(model)

    def model_switch_blocker(self) -> str | None:
        """Why a model cannot be chosen now (the same rule select_model enforces), or None."""
        if self.busy or self.store.pending_instructions() or any(
                r['status'] in ('queued', 'running', 'paused', 'waiting_human', 'parked')
                for r in self.store.list_runs()):
            return 'Finish the current work and pending instructions before switching models.'
        if any(step.get('status') not in ('done', 'skipped')
               for step in (self._project() or {}).get('plan', [])):
            return 'Finish or resolve the current Task Plan before switching models.'
        return None

    def model_settings(self) -> dict[str, Any]:
        c = self.creds
        return {'model': c.model_id, 'input_price': c.input_price, 'output_price': c.output_price,
                'cached_input_price': c.cached_input_price,
                'context_window_tokens': c.context_window_tokens, 'context_limits': self._context_limits.copy(),
                'context_default_limits': self._context_defaults.copy(),
                'context_default_sources': self._context_default_sources.copy(),
                'context_limit_source': self._context_limit_source(c.model_id),
                'context_limit_sources': {model: self._context_limit_source(model) for model in self._context_limits},
                'agent_models': {role: value.copy() for role, value in self._agent_models.items()},
                'effective_models': {role: self.agent_model(role) for role in (ORCHESTRATOR, *SUBAGENTS)},
                'api': c.api, 'busy': self.busy, 'budget_usd': self.run_budget_usd or c.test_budget_usd,
                'switch_blocked': self.model_switch_blocker()}

    def agent_model(self, role: str) -> dict[str, Any]:
        """Resolve one role's model, rates and deployment capacity together."""
        choice = self._agent_models.get(role) or {
            'model': self.creds.model_id, 'input_price': self.creds.input_price,
            'output_price': self.creds.output_price}
        # Overrides carry no cached-input tariff; only the default model's applies.
        cached = self.creds.cached_input_price if choice['model'] == self.creds.model_id else None
        return {**choice, 'cached_input_price': cached, 'inherits_default': role not in self._agent_models,
                'context_window_tokens': self._context_limits.get(choice['model']),
                'context_limit_source': self._context_limit_source(choice['model'])}

    async def select_model(self, model: str, input_price: float, output_price: float, *,
                           context_window_tokens: Any = UNSET, cached_input_price: Any = UNSET,
                           agent_models: Any = UNSET) -> None:
        """Change the next turn only; retain all spend, lab state and saved events."""
        blocker = self.model_switch_blocker()
        if blocker:
            raise ValueError(blocker)
        limit = (self._context_limits.get(model) if context_window_tokens is UNSET else context_window_tokens)
        if cached_input_price is UNSET:
            same_tariff = (model, input_price, output_price) == (
                self.creds.model_id, self.creds.input_price, self.creds.output_price)
            cached_input_price = self.creds.cached_input_price if same_tariff else None
        updated = replace(self.creds, model_id=model, input_price=input_price, output_price=output_price,
                          cached_input_price=cached_input_price, context_window_tokens=limit)
        assignments = agent_model_choices(self._agent_models if agent_models is UNSET else agent_models)
        if any(choice['model'] == model and (choice['input_price'], choice['output_price']) != (
                input_price, output_price) for choice in assignments.values()):
            raise ValueError('Use the default budget rates for overrides using the default model.')
        self._busy = True  # keep the driver out during asynchronous client cleanup
        self._done.clear()
        try:
            if self._client is not None:
                await self._client.close()
                self._client = None
            if self._session is not None:
                self._session.close()
                self._session = None
            previous = self.creds.model_id
            self.creds = updated
            self._agent_models = assignments
            self._context_limits[model] = limit
            if context_window_tokens is not UNSET or model not in self._context_defaults:
                if model in self._context_defaults and limit == self._context_defaults[model]:
                    self._context_overrides.pop(model, None)
                else:
                    self._context_overrides[model] = limit
            if self._metrics is not None:
                self._metrics.prefix_comparison.clear()
            self._metrics = None
            self._request_context = {}
            self._agents.clear()
            self._config = self._hooks = self.last_result = None
            self._last_messages.clear()
            # Different models must not inherit provider-specific reasoning items.
            self.session_id = f'openai-{uuid4().hex}'
            if self.gateway.project_id:
                limits = dict((self._project() or {}).get('state', {}).get('openai_context_limits', {}))
                # Persist overrides only, so future catalog updates can refresh defaults.
                limits[updated.base_url] = self._context_overrides.copy()
                self.store.update_project_state(self.gateway.project_id, openai_session_id=self.session_id,
                    openai_context_limits=limits,
                    openai_model_config={'model': model, 'input_price': input_price,
                                         'cached_input_price': cached_input_price,
                                         'output_price': output_price, 'base_url': updated.base_url,
                                         'agent_models': assignments})
            self.store.emit('model.changed', {'previous': previous, 'model': model,
                            'session_id': self.session_id, 'input_price': input_price,
                            'cached_input_price': cached_input_price,
                            'context_window_tokens': limit,
                            'context_limit_source': self._context_limit_source(model),
                            'output_price': output_price, 'cost_basis': 'configured_token_rates'})
            self.store.emit('model.routing_changed', {'agent_models': self.model_settings()['effective_models'],
                            'session_id': self.session_id})
        finally:
            self._busy = False
            self._done.set()

    @property
    def busy(self) -> bool:
        return self._busy

    def spent_usd(self) -> float:
        return self.store.spent_usd()

    @property
    def budget_exhausted(self) -> bool:
        return self.run_budget_usd is not None and self.spent_usd() >= self.run_budget_usd

    def _check_budget(self, role: str = ORCHESTRATOR) -> None:
        if self._interrupted:
            raise asyncio.CancelledError
        if self.budget_exhausted:
            raise self._budget_error("run budget exhausted")
        cap = float(os.environ.get("QUMUS_MAX_BUDGET_USD") or self.cfg.get("max_budget_usd", 40))
        if cap <= 0 or self._turn_cost >= cap:
            raise self._budget_error("turn budget exhausted")
        token_cap = int(os.environ.get("QUMUS_OPENAI_MAX_TOTAL_TOKENS")
                        or self.cfg.get("openai", {}).get("max_total_tokens", 2_000_000))
        if self._turn_tokens >= token_cap:
            raise self._budget_error("turn token budget exhausted")
        # A USD cap with unknown gateway prices would be a false guarantee.
        choice = self.agent_model(role)
        if choice['input_price'] is None or choice['output_price'] is None:
            raise ValueError("Set QUMUS_OPENAI_INPUT_USD_PER_MILLION and "
                             "QUMUS_OPENAI_OUTPUT_USD_PER_MILLION to the gateway's token "
                             "prices before a paid run. No request was sent.")

    def _budget_error(self, reason: str) -> BudgetExceeded:
        self._budget_hit = reason
        return BudgetExceeded(reason)

    async def _admit_http(self, request: Any) -> None:
        """Opt-in recorder admission, before metrics and before any network send.

        A rejected first attempt is known unsent: release its existing reservation
        by settling to zero. A rejected retry cannot erase an earlier unknown send.
        """
        if self._pre_send_admission is None:
            return
        try:
            await self._pre_send_admission(request, self)
        except (Exception, asyncio.CancelledError) as exc:
            latest = self._metrics.latest if self._metrics else {}
            sent = latest.get("call_id") == self._request_context.get("call_id") and bool(latest)
            if not sent:
                reservation = self._reservations.pop(self._request_context.get("agent"), None)
                if reservation and self._ledger is not None:
                    self._ledger.settle(reservation, 0.0)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise self._budget_error("pre-send admission rejected: " + scrub_text(str(exc),
                                     [self.creds.api_key])) from exc

    async def _note_send(self, request: Any) -> None:
        call = self._request_context.get("call_id")
        if call:
            self._unanswered[call] = self._unanswered.get(call, 0) + 1

    async def _note_refusal(self, response: Any) -> None:
        call = self._request_context.get("call_id")
        if call and response.status_code in REFUSED_UNPROCESSED and self._unanswered.get(call):
            self._unanswered[call] -= 1

    def _model_failure(self, failure: Any) -> None:
        """Record the failure; release the reservation of a call the provider refused unprocessed.

        A 429 or 503 reply means the gateway did not run the request. When every
        send of this call got such a reply, no tokens can have been billed, so
        the reservation settles to zero instead of counting as spend. Any send
        without a reply (timeout, dropped connection) keeps the full reservation.
        """
        if self._metrics is not None:
            self._metrics.record_model_failure(failure)
        status = getattr(failure, "status_code", None)
        call = self._request_context.get("call_id")
        if status not in REFUSED_UNPROCESSED or not call or self._unanswered.pop(call, 1):
            return
        agent = self._request_context.get("agent")
        reservation = self._reservations.pop(agent, None)
        if reservation and self._ledger is not None:
            self._ledger.settle(reservation, 0.0)
            self.store.emit("agent.reservation_released", {
                "provider": "openai", "call_id": call, "status": status,
                "reason": "every send refused unprocessed"}, agent=agent)

    def _ensure(self) -> None:
        if self._agents:
            return
        from agents import Agent, ModelSettings, RunConfig, RunHooks, SQLiteSession, WebSearchTool
        from agents.models.openai_provider import OpenAIProvider
        from agents.run_config import ToolExecutionConfig

        rt = self
        if self._model_provider is None:
            from qumus2.agents.openai_budget import SpendLedger

            self._ledger = SpendLedger(Path(self.creds.budget_path), self.creds.stage_budget_usd)

        class Hooks(RunHooks):
            async def on_llm_start(self, context, agent, system_prompt, input_items):
                rt._check_budget(agent.name)
                if rt._ledger is not None or rt.run_budget_usd is not None:
                    allowance = rt._request_allowance(agent, system_prompt, input_items)
                    # the run budget also holds back what this request may cost: a
                    # check of what was spent alone let the last request overshoot
                    if rt.run_budget_usd is not None \
                            and rt.spent_usd() + allowance > rt.run_budget_usd:
                        raise rt._budget_error(
                            f"run budget cannot cover another model request (spent "
                            f"${rt.spent_usd():.2f} of ${rt.run_budget_usd:.2f}, request "
                            f"reserve ${allowance:.2f})")
                if rt._ledger is not None:
                    cap = min(rt.creds.test_budget_usd, rt.run_budget_usd or rt.creds.test_budget_usd)
                    try:
                        rt._reservations[agent.name] = rt._ledger.reserve(rt._budget_scope, allowance, cap)
                    except ValueError as exc:
                        raise rt._budget_error(str(exc)) from None
                # Calls are sequential, including nested specialists. Keep the
                # identity through HTTP retries; the hook counts each send.
                choice = rt.agent_model(agent.name)
                rt._request_context = {"agent": agent.name, "model": choice['model'], "session_id": rt.session_id,
                                       "call_id": uuid4().hex,
                                       "context_window_tokens": choice['context_window_tokens'],
                                       "context_limit_source": choice['context_limit_source'],
                                       "max_output_tokens": agent.model_settings.max_tokens,
                                       "composition_hints": {
                                           "instruction_sections": instruction_sections(
                                               system_prompt, rt._composition_layout.get(agent.name)),
                                           "current_task": rt._composition_tasks.get(agent.name),
                                           "application_context": {
                                               "role": "developer", "text": rt._composition_snapshot},
                                           "filter_actions": rt._composition_filter_actions.copy(),
                                           "tool_evidence_texts": rt._composition_evidence.copy()}}

            async def on_llm_end(self, context, agent, response):
                if rt._metrics is not None:
                    rt._metrics.record_usage(response.raw_usage)
                usage = response.usage
                rt._turn_requests += 1
                rt._turn_tokens += usage.total_tokens or usage.input_tokens + usage.output_tokens
                choice = rt.agent_model(agent.name)
                cost, pricing = rt._response_cost(usage, response.raw_usage, agent.name)
                rt._turn_cost += cost
                reservation = rt._reservations.pop(agent.name, None)
                rt._unanswered.pop(rt._request_context.get("call_id"), None)
                # Missing usage keeps the full reservation, including across restarts.
                if reservation and (usage.input_tokens or usage.output_tokens):
                    rt._ledger.settle(reservation, cost)
                # Persist after each response, including responses preceding a crash.
                rt.store.emit("agent.usage", {"provider": "openai", "session_id": rt.session_id,
                              "call_id": rt._request_context.get("call_id"),
                              "request_id": rt._metrics.latest.get("request_id") if rt._metrics else None,
                              "gateway": rt._metrics.latest.get("gateway") if rt._metrics else None,
                              "api": rt._metrics.latest.get("api") if rt._metrics else None,
                              "attempt": rt._metrics.latest.get("attempt") if rt._metrics else None,
                              "token_measurement": "provider_usage_v1",
                              "model": choice['model'],
                              "input_price": choice['input_price'], "output_price": choice['output_price'],
                              "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                              "cost_usd": cost, "cost_basis": "configured_token_rates",
                              "pricing": pricing}, agent=agent.name)

        provider = self._model_provider
        if provider is None:
            from openai import AsyncOpenAI, DefaultAsyncHttpxClient

            from qumus2.agents.request_metrics import RequestMetrics

            self._metrics = RequestMetrics(self.store, lambda: self._request_context, track_tokens=True)
            # Explicit client: never inherits another app's global OpenAI key or base URL.
            self._client = AsyncOpenAI(api_key=self.creds.api_key, base_url=self.creds.base_url,
                                       max_retries=self._http_max_retries, timeout=http_timeout_s(),
                                       http_client=self._metrics.observe_client(DefaultAsyncHttpxClient(event_hooks={
                                           "request": [self._admit_http, self._note_send, self._metrics],
                                           "response": [self._metrics.on_response, self._note_refusal]})))
            provider = OpenAIProvider(openai_client=self._client,
                                      use_responses=self.creds.api == "responses")
        if self._metrics is not None:
            from qumus2.agents.openai_models import UsageProvider

            provider = UsageProvider(provider, self._metrics.record_usage, self._model_failure)
        if not self.creds.stream:
            from qumus2.agents.openai_models import BufferedProvider

            provider = BufferedProvider(provider)
        self._hooks = Hooks()
        async def prepare_input(data):
            from agents.run_config import ModelInputData

            # The SDK runs its input filter before on_llm_start. Wait before
            # reading state, so a job/plan change during pacing is included in
            # this request. The final hook still checks budget and reserves the
            # fully assembled input; nested specialists share this same clock.
            self._check_budget()
            loop = asyncio.get_running_loop()
            delay = self._next_model_call - loop.time()
            if delay > 0:
                # SDK cancellation does not interrupt an awaited input filter.
                # Keep only this wait cancellable by the normal Stop path.
                self._pacing_wait = asyncio.create_task(asyncio.sleep(delay))
                try:
                    await self._pacing_wait
                finally:
                    self._pacing_wait = None
            self._check_budget()  # Stop may have arrived while pacing.
            self._next_model_call = loop.time() + float(self.cfg.get("openai", {}).get(
                "min_request_interval_s", 0))
            self._composition_evidence = []
            self._composition_filter_actions = {}
            filtered = transport_model_input(data, self.creds.api,
                                             evidence_observer=self._composition_evidence.append,
                                             diagnostic_observer=self._composition_filter_actions.update)
            # One fresh trusted application context at the tail keeps changing
            # state behind reusable rules/history. The SDK persists its original
            # input, not this ephemeral filtered list. Never accumulate snapshots.
            self._composition_snapshot = (
                "Current laboratory record (application state, not a human instruction; "
                "authoritative over older state; do not repeat completed jobs):\n"
                + self.gateway.project_summary())
            return ModelInputData(input=[*filtered.input, {
                "role": "developer", "content": self._composition_snapshot}],
                instructions=filtered.instructions)

        self._config = RunConfig(model_provider=provider, tracing_disabled=True,
                                 trace_include_sensitive_data=False,
                                 session_input_callback=(chat_session_input
                                                         if self.creds.api == "chat_completions" else None),
                                 call_model_input_filter=prepare_input,
                                 workflow_name="Qumus", group_id=self.gateway.project_id,
                                 tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1))
        roster = load_roster()
        settings = ModelSettings(parallel_tool_calls=False,
                                 include_usage=True, preserve_raw_usage=True,
                                 max_tokens=max_output_tokens(self.cfg))
        for name, spec in roster.items():
            instructions = spec.prompt
            reference_ranges = []
            if self.store.bench == "sim":
                instructions += ("\nThe connected lab is a TESTBENCH. All samples, photos, "
                                 "actions and outcomes in this run are simulated. Label "
                                 "experimental results as simulated in your reports.\n")
            for skill in self.cfg["agents"].get(name, {}).get("skills", []):
                start = len(instructions)
                instructions += f"\n\nREFERENCE SKILL ({skill}):\n{skill_text(skill)}"
                reference_ranges.append((start, len(instructions)))
            if name == ORCHESTRATOR:
                instructions += ("\nOpenAI runtime: call delegate_eyes / delegate_advisor / "
                                 "delegate_hands / delegate_picker / delegate_designer / delegate_reactor. Each "
                                 "takes task input. Task input is {input: 'sN: task, ids, parameters and context'}. "
                                 "Only tools actually in your list are yours. Each task "
                                 "should state only its positive objective; unrelated "
                                 "workflow names can confuse the intent checks.\n")
            if name == "advisor" and not self.creds.web_search:
                instructions += ("\nWeb tools are unavailable in this connection. Cite local "
                                 "records or supplied literature; explicitly state when "
                                 "external evidence has not been retrieved.\n")
            instructions += ("\nThe final application-state context is a current laboratory "
                             "record, not a new human request or a new optical observation. "
                             "Use it over older state; follow recorded human constraints and "
                             "check current evidence through your permitted tools.\n")
            self._composition_layout[name] = {"prefix": instructions, "references": reference_ranges}
            tools: list[Any] = [self._domain_tool(name, t)
                                for t in self.gateway.registry.for_agent(name) if t.name != "set_reactor_pair_goal"]
            if name == "advisor" and self.creds.web_search:
                tools.append(WebSearchTool())
            self._agents[name] = Agent(name=name, instructions=instructions, tools=tools,
                                        model=self.agent_model(name)['model'], model_settings=settings)
        for name in SUBAGENTS:
            self._agents[ORCHESTRATOR].tools.append(self._delegate_tool(name))
        db_path = ":memory:" if self.store.path == ":memory:" else str(
            Path(self.store.path).with_suffix(".openai-sessions.db"))
        self._session = SQLiteSession(self.session_id, db_path=db_path)

    def _response_cost(self, usage: Any, raw_usage: Any, role: str = ORCHESTRATOR) -> tuple[float, dict[str, Any]]:
        """Credit only measured cache reads; SDK defaults and cache writes are not evidence."""
        choice = self.agent_model(role)
        measured = reported_usage(raw_usage)
        cached = measured['cached_input_tokens']
        valid = (cached is not None and measured['input_tokens'] == usage.input_tokens
                 and measured['output_tokens'] == usage.output_tokens
                 and 0 <= cached <= usage.input_tokens)
        applied = cached if valid and choice['cached_input_price'] is not None else 0
        cost = ((usage.input_tokens - applied) * float(choice['input_price'] or 0)
                + applied * float(choice['cached_input_price'] or 0)
                + usage.output_tokens * float(choice['output_price'] or 0)) / 1_000_000
        return cost, {
            'input_usd_per_million': choice['input_price'],
            'output_usd_per_million': choice['output_price'],
            'cached_input_usd_per_million': choice['cached_input_price'],
            'measured_cached_input_tokens': cached if valid else None,
            'discounted_cached_input_tokens': applied,
            'basis': ('measured_cache_reads' if valid and choice['cached_input_price'] is not None
                      else 'full_input_rate'),
        }

    def _request_allowance(self, agent: Any, instructions: str | None, items: Any) -> float:
        # UTF-8 byte count overestimates ordinary text tokens. Images reserve
        # 32K tokens each; this is a planning allowance, not provider tokenization.
        images = 0

        def text_only(value: Any) -> Any:
            nonlocal images
            if isinstance(value, dict):
                return {k: text_only(v) for k, v in value.items()}
            if isinstance(value, list):
                return [text_only(v) for v in value]
            if isinstance(value, str) and value.startswith("data:image/"):
                images += 1
                return "[image]"
            return value

        schemas = [getattr(t, "params_json_schema", {}) for t in agent.tools]
        text = json.dumps([instructions, text_only(items), schemas], default=str).encode("utf-8")
        input_bound = len(text) + images * 32768 + 4096
        output_bound = max_output_tokens(self.cfg)
        choice = self.agent_model(agent.name)
        return (input_bound * float(choice['input_price'] or 0)
                + output_bound * float(choice['output_price'] or 0)) / 1_000_000

    def _domain_tool(self, role: str, spec: Any) -> Any:
        from agents import FunctionTool

        from qumus2.agents.tooling import ToolResult

        async def invoke(ctx: Any, raw: str) -> list[Any]:
            if self._interrupted:
                raise asyncio.CancelledError
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be an object")
            except (ValueError, TypeError):
                self.store.emit("tool.denied", {"tool": spec.name, "args": {},
                                "reason": "invalid JSON object", "rule": "schema"}, agent=role)
                return tool_output(ToolResult(status="denied", summary="invalid JSON object"))
            result, extra = await self.gateway.call(role, spec.name, args, ctx.tool_call_id)
            return tool_output(result, extra)

        return FunctionTool(name=spec.name, description=spec.description,
                            params_json_schema=spec.json_schema(), on_invoke_tool=invoke,
                            strict_json_schema=False)

    def _delegate_tool(self, role: str) -> Any:
        async def stream(data: Any) -> None:
            self._stream_event(data["event"])

        tool = self._agents[role].as_tool(
            tool_name=f"delegate_{role}", tool_description=load_roster()[role].description,
            run_config=self._config, hooks=self._hooks,
            max_turns=int(self.cfg["agents"][role]["max_turns"]), on_stream=stream,
            failure_error_function=None,
        )
        original = tool.on_invoke_tool

        async def invoke(ctx: Any, raw: str) -> str:
            if self._interrupted:
                raise asyncio.CancelledError
            payload = json.loads(raw)
            task = payload.get("input", "")
            decision = self.gateway.delegate(role, ctx.tool_call_id, task, require_sample_outcomes=True,
                                             objective=payload.get("objective"))
            if not decision.allow:
                return json.dumps({"status": "denied", "reason": decision.reason, "details": decision.args})
            key = ctx.tool_call_id
            self.gateway.delegation_started(key, role, task)
            self._composition_tasks[role] = task
            # Input preparation reads state after this reopened delegation;
            # the specialist's reusable instructions never contain stale state.
            self.store.emit("agent.delegate", {"to": role, "task": task,
                                               "tool_use_id": key}, agent=ORCHESTRATOR)
            completed = False
            try:
                output = await original(ctx, json.dumps({"input": task}))
                completed = True
                facts = self.gateway.delegation_finished(key)
                self.store.emit("agent.done", {"summary": str(output)}, agent=role)
                self.gateway.save_agent_status(role, str(output))
                pending = self.gateway.inbox.format_for_tool_result()
                return str(output) + "\n" + (facts or "") + (f"\n{pending}" if pending else "")
            finally:
                self._composition_tasks.pop(role, None)
                if not completed:
                    self.gateway.delegation_finished(key, "pending")

        return replace(tool, on_invoke_tool=invoke)

    def _stream_event(self, event: Any) -> None:
        from agents import ItemHelpers

        if event.type == "run_item_stream_event" and event.name == "message_output_created":
            text = ItemHelpers.text_message_output(event.item)
            if text.strip():
                role = event.item.agent.name
                self._last_messages[role] = text
                self.store.emit("agent.message", {"text": text}, agent=role)
        elif event.type == "run_item_stream_event" and event.name == "tool_called":
            raw = event.item.raw_item
            if getattr(raw, "type", None) == "web_search_call":
                self.store.emit("agent.web", {"tool": "web_search", "phase": "call",
                                "tool_use_id": getattr(raw, "id", None)}, agent=event.item.agent.name)

    async def send_input(self, items: list[dict[str, Any]]) -> None:
        """Structured image/text input through the same instrumented send loop."""
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Structured model input must be a list of message objects")
        await self.send(items)

    async def send(self, text: str | list[dict[str, Any]]) -> None:
        from agents import Runner

        if self._busy:
            raise RuntimeError("an agent turn is already running")
        if self.budget_exhausted:
            self._tell_exhausted()
            return
        self._busy, self._interrupted = True, False
        self._done.clear()
        self._turn_cost, self._turn_tokens, self._turn_requests = 0.0, 0, 0
        self._budget_hit = None
        ok, reason = False, "error"
        try:
            self._ensure()
            self._composition_tasks[ORCHESTRATOR] = text if isinstance(text, str) else "\n".join(
                part["text"] for item in text for part in item.get("content", [])
                if isinstance(part, dict) and part.get("type") == "input_text" and isinstance(part.get("text"), str))
            # Input preparation refreshes authoritative state on every model
            # request, including follow-ups, nested specialists and restart.
            self._active = Runner.run_streamed(
                self._agents[ORCHESTRATOR], text, context=self, hooks=self._hooks,
                run_config=self._config, session=self._session,
                max_turns=int(self.cfg["agents"][ORCHESTRATOR]["max_turns"]),
            )
            async for event in self._active.stream_events():
                self._stream_event(event)
            self.last_result = self._active
            ok = not self._interrupted
            reason = "success" if ok else "interrupted"
        except BudgetExceeded as exc:
            reason = "budget_exhausted"
            self.store.emit("notification", {"level": "danger", "text": str(exc), "reason": "budget_exhausted",
                                               "session_id": self.session_id, "project_id": self.gateway.project_id})
        except asyncio.CancelledError:
            reason = "interrupted"
            if not self._interrupted:
                raise
        except Exception as exc:
            if self._budget_hit:
                # The SDK can wrap an exception raised inside an agent-as-tool.
                reason = "budget_exhausted"
                self.store.emit("notification", {"level": "danger", "text": self._budget_hit, "reason": "budget_exhausted",
                                                   "session_id": self.session_id, "project_id": self.gateway.project_id})
            else:
                from qumus2.agents.runtime import ReportedAgentError

                message = scrub_text(str(exc), [self.creds.api_key])
                outage = model_outage(exc)
                event_id = self.store.emit("agent.error", {"provider": "openai", "type": type(exc).__name__,
                                "session_id": self.session_id, "project_id": self.gateway.project_id,
                                "error": message, **({"outage": outage} if outage else {})},
                                agent=ORCHESTRATOR)
                raise ReportedAgentError(message, event_id, outage=outage) from None
        finally:
            self._composition_tasks.pop(ORCHESTRATOR, None)
            self.gateway.turn_finished(ok=ok)
            for role, message in self._last_messages.items():
                self.gateway.save_agent_status(role, message)
            self.store.emit("agent.turn_end", {"provider": "openai", "session_id": self.session_id,
                            "project_id": self.gateway.project_id, "model": self.creds.model_id,
                            "is_error": not ok, "num_turns": self._turn_requests,
                            "input_output_tokens": self._turn_tokens, "cost_usd": self._turn_cost,
                            "cost_accounted_by_usage": True, "cost_basis": "configured_token_rates",
                            "reason": reason}, agent=ORCHESTRATOR)
            self._turn_cost = 0.0
            self._busy, self._active = False, None
            self._done.set()
            if self.budget_exhausted:
                self._tell_exhausted()

    def _tell_exhausted(self) -> None:
        if not self._exhausted_told:
            self._exhausted_told = True
            self.store.emit("notification", {"level": "danger", "reason": "budget_exhausted",
                "session_id": self.session_id, "project_id": self.gateway.project_id, "text": (
                "Run budget exhausted. No more model requests will be sent. "
                "Existing hardware jobs remain managed by the job service.")})

    async def continue_turn(self, text: str) -> None:
        await self._done.wait()
        await self.send(text)

    async def interrupt(self) -> None:
        self._interrupted = True
        if self._pacing_wait is not None:
            self._pacing_wait.cancel()
        if self._active is not None:
            self._active.cancel(mode="immediate")
        self.store.emit("agent.interrupted", {"reason": "user stop"}, agent=ORCHESTRATOR)
        if self._busy:
            await self._done.wait()

    async def close(self) -> None:
        if self._busy:
            await self.interrupt()
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._session is not None:
            self._session.close()
        if self._metrics is not None:
            self._metrics.prefix_comparison.clear()
        self._composition_snapshot = ""
