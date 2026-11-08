"""Count outgoing model text without retaining another copy of the prompt.

Characters are Unicode code points, not tokens or HTTP bytes. Count textual
input values and compact JSON tool definitions, excluding media and protocol
metadata. The HTTP request hook runs after SDK conversion and input filtering,
once per send attempt (including retries), for either OpenAI API transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from qumus2.agents.context_composition import METHOD, application_context_index, input_composition
from qumus2.agents.token_usage import reported_usage

_METADATA = frozenset({
    "type", "role", "id", "call_id", "tool_call_id", "status",
    "encrypted_content", "signature", "annotations", "logprobs",
})
_IMAGES = frozenset({"input_image", "image_url"})
_OTHER_MEDIA = frozenset({"input_audio", "audio", "input_file", "file", "input_video"})


class PrefixComparison:
    """Compare final wire components locally; never persist content or fingerprints.

    Component order is a diagnostic convention, not a provider's hidden prompt
    rendering or tokenization. Array order and every input value participate in
    the keyed fingerprints. Only categories, indices and counts leave memory.
    """

    MAX_SCOPES, MAX_COMPONENTS, MAX_BYTES = 16, 2048, 32_000_000

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
        self._previous: OrderedDict[tuple[Any, ...], tuple[str, list[tuple[str, bytes]]]] = OrderedDict()

    def clear(self) -> None:
        self._previous.clear()
        self._key = secrets.token_bytes(32)

    def compare(self, payload: dict[str, Any], api: str, identity: dict[str, Any],
                agent: str | None, hints: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = tuple(identity.get(k) for k in ("provider", "gateway", "model", "session_id")) + (api, agent)
        result: dict[str, Any] = {"version": 1, "measurement": "local_wire_components_v1"}
        hints = hints or {}
        actions = hints.get("filter_actions")
        if isinstance(actions, dict):
            result["filter_actions"] = {key: actions[key] for key in (
                "superseded_snapshots", "omitted_images", "duplicate_images", "compacted_catalogues")
                if type(actions.get(key)) is int and 0 <= actions[key] <= 2**53 - 1}
        if any(not isinstance(value, str) or not value for value in scope):
            return {**result, "status": "unavailable", "reason": "unknown_scope"}
        items = payload.get("messages" if api == "chat_completions" else "input", [])
        items = items if isinstance(items, list) else [items]
        snapshot = application_context_index(items, hints)
        components: list[tuple[str, bytes]] = []
        size = 0

        def add(category: str, value: Any) -> None:
            nonlocal size
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
            size += len(encoded)
            if size > self.MAX_BYTES or len(components) >= self.MAX_COMPONENTS:
                raise OverflowError
            components.append((category, hmac.digest(self._key, encoded, hashlib.sha256)))

        try:
            # Request settings are compared separately from content; their server
            # rendering order is unknown. Streaming changes are still observable.
            add("request_settings", {k: v for k, v in payload.items()
                                     if k not in {"input", "messages", "instructions", "tools", "model"}})
            if "instructions" in payload:
                add("system_instructions", payload["instructions"])
            add("tool_definitions", payload.get("tools", []))
            for index, item in enumerate(items):
                kind = item.get("type") if isinstance(item, dict) else None
                role = item.get("role") if isinstance(item, dict) else None
                category = ("lab_snapshot" if index == snapshot else
                            "system_instructions" if role in {"system", "developer"} else
                            "tool_calls" if kind in {"function_call", "custom_tool_call"} else
                            "tool_results" if role == "tool" or kind in {
                                "function_call_output", "custom_tool_call_output"} else
                            "conversation_history")
                add(category, item)
        except OverflowError:
            self._previous.pop(scope, None)
            return {**result, "status": "unavailable", "reason": "comparison_limit"}

        previous = self._previous.pop(scope, None)
        self._previous[scope] = (identity["request_id"], components)
        while len(self._previous) > self.MAX_SCOPES:
            self._previous.popitem(last=False)
        result["component_count"] = len(components)
        if previous is None:
            return {**result, "status": "not_recorded"}
        baseline, old = previous
        matched = 0
        for before, after in zip(old, components, strict=False):
            if before != after:
                break
            matched += 1
        unchanged = matched == len(old) == len(components)
        return {**result, "status": "comparable", "baseline_request_id": baseline,
                "change": "identical" if unchanged else "appended" if matched == len(old) else "changed",
                "matched_components": matched,
                "first_changed_component": None if unchanged else matched,
                "first_changed_category": None if unchanged else (
                    components[matched][0] if matched < len(components) else old[matched][0])}


def _input_size(value: Any) -> tuple[int, int]:
    if isinstance(value, str):
        return len(value), 0
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(kind, str) and kind in _IMAGES:
            return 0, 1
        if isinstance(kind, str) and kind in _OTHER_MEDIA:
            return 0, 0
        values = (v for k, v in value.items() if k not in _METADATA)
    elif isinstance(value, list):
        values = iter(value)
    else:
        return 0, 0
    chars = images = 0
    for item in values:
        n, count = _input_size(item)
        chars += n
        images += count
    return chars, images


def request_text_size(payload: dict[str, Any], api: str) -> dict[str, Any]:
    """Measure the decoded request; do not mutate or persist its contents."""
    instructions, _ = _input_size(payload.get("instructions", ""))
    items = payload.get("messages" if api == "chat_completions" else "input", [])
    if api == "chat_completions" and isinstance(items, list):
        instructions += sum(_input_size(m.get("content"))[0] for m in items
                            if isinstance(m, dict) and m.get("role") in ("system", "developer"))
        items = [m for m in items if not isinstance(m, dict)
                 or m.get("role") not in ("system", "developer")]
    conversation, images = _input_size(items)
    tools = payload.get("tools") or []
    tool_chars = len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))) if tools else 0
    return {
        "text_chars": instructions + conversation + tool_chars,
        "instructions_chars": instructions,
        "conversation_chars": conversation,
        "tools_chars": tool_chars,
        "image_count": images,
        "unit": "unicode_code_points",
        "measurement": "outgoing_text_v1",
    }


class RequestMetrics:
    def __init__(self, store: Any, context: Any, *, track_tokens: bool = False) -> None:
        self.store, self.context = store, context
        self.track_tokens = track_tokens
        self.latest: dict[str, Any] = {}
        self._reported: str | None = None
        self._current_attempt: dict[str, Any] = {}
        self.prefix_comparison = PrefixComparison()

    def record_usage(self, raw_usage: Any) -> None:
        """Match a completed response to the last send of its logical call.

        Retries keep separate request IDs. Earlier unsuccessful sends stay
        unknown; a later response must never fill their gaps or duplicate spend.
        """
        context = self.context()
        if not self.latest or self.latest.get("call_id") != context.get("call_id"):
            return
        request_id = self.latest["request_id"]
        if self._reported == request_id:
            return
        self._reported = request_id
        self.store.emit("agent.request_usage", {
            **{k: self.latest.get(k) for k in (
                "request_id", "call_id", "session_id", "provider", "model", "api", "attempt",
                "context_window_tokens", "max_output_tokens", "context_limit_source", "gateway")},
            **reported_usage(raw_usage),
        }, agent=context.get("agent"))

    def _failure(self, attempt: dict[str, Any], *, kind: str,
                 exception_type: str | None = None, provider_code: str | None = None) -> None:
        if not attempt or attempt.get("failure_recorded"):
            return
        attempt["failure_recorded"] = True
        identity = attempt["identity"]
        # Deliberately exclude exception messages, response bodies, URLs and raw headers.
        self.store.emit("agent.request_failure", {
            **{k: identity.get(k) for k in (
                "request_id", "call_id", "session_id", "provider", "model", "api", "attempt")},
            "failure_kind": kind, "exception_type": exception_type,
            "http_status": attempt.get("http_status"),
            "provider_request_id": attempt.get("provider_request_id"),
            "provider_error_code": self._safe_code(provider_code),
        }, agent=attempt.get("agent"))

    @staticmethod
    def _safe_code(value: Any) -> str | None:
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else None

    @staticmethod
    def _http_failure(status: int) -> str:
        return {400: "invalid_request", 401: "authentication", 403: "access_denied",
                408: "timeout", 409: "conflict", 413: "request_too_large", 429: "rate_limit"}.get(
                    status, "server_error" if status >= 500 else "http_error")

    async def on_response(self, response: Any) -> None:
        attempt = response.request.extensions.get("qumus_request_attempt")
        if not attempt:
            return
        attempt["http_status"] = response.status_code
        attempt["provider_request_id"] = self._safe_code(response.headers.get("x-request-id"))
        if response.status_code >= 400:
            self._failure(attempt, kind=self._http_failure(response.status_code))

    def record_exception(self, exc: BaseException, attempt: dict[str, Any]) -> None:
        names = {cls.__name__ for cls in type(exc).__mro__}
        if "CancelledError" in names:
            kind = "cancelled"
        elif names & {"TimeoutException", "APITimeoutError", "TimeoutError"}:
            kind = "timeout"
        elif names & {"TransportError", "RequestError", "APIConnectionError"}:
            kind = "connection_error"
        elif names & {"ValidationError", "JSONDecodeError", "ModelBehaviorError"}:
            kind = "invalid_response"
        else:
            kind = "response_error"
        status = getattr(exc, "status_code", None)
        if type(status) is int and 400 <= status <= 599:
            attempt["http_status"] = status
            kind = self._http_failure(status)
        self._failure(attempt, kind=kind, exception_type=type(exc).__name__)

    def observe_client(self, client: Any) -> Any:
        """Observe failed sends before the SDK retries; preserve its retry policy.

        Use the client's public send method so the SDK's native and legacy HTTP
        clients both work. Response hooks do not receive connection exceptions.
        """
        send = client.send

        async def observed_send(request: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return await send(request, *args, **kwargs)
            except (Exception, asyncio.CancelledError) as exc:
                attempt = request.extensions.get("qumus_request_attempt")
                if attempt:
                    self.record_exception(exc, attempt)
                raise

        client.send = observed_send
        return client

    def record_model_failure(self, failure: Any) -> None:
        attempt = self._current_attempt
        if not attempt or attempt["identity"].get("call_id") != self.context().get("call_id"):
            return
        if isinstance(failure, BaseException):
            self.record_exception(failure, attempt)
        else:
            kind = getattr(failure, "type", "")
            if kind not in ("response.failed", "response.incomplete"):
                return
            response = getattr(failure, "response", None)
            detail = getattr(response, "error" if kind == "response.failed" else "incomplete_details", None)
            code = getattr(detail, "code" if kind == "response.failed" else "reason", None)
            self._failure(attempt, kind=kind.replace(".", "_"), provider_code=code)

    async def __call__(self, request: Any) -> None:
        path = request.url.path.rstrip("/")
        api = ("chat_completions" if path.endswith("/chat/completions") else
               "responses" if path.endswith("/responses") else None)
        if request.method != "POST" or api is None:
            return
        context = self.context()
        event = {"provider": "openai", "api": api, "request_id": uuid4().hex,
                 **{k: context[k] for k in ("session_id", "call_id") if k in context}}
        parts = urlsplit(str(request.url))
        host = parts.hostname or ""
        host = f"[{host}]" if ":" in host else host
        port = parts.port
        if port and (parts.scheme, port) not in {("https", 443), ("http", 80)}:
            host += f":{port}"
        suffix = "/chat/completions" if api == "chat_completions" else "/responses"
        event["gateway"] = urlunsplit((parts.scheme, host, parts.path.rstrip("/")[:-len(suffix)].rstrip("/"), "", ""))
        # Only the normalized deployment identity, never raw headers/query credentials or pixels.
        retry = request.headers.get("x-stainless-retry-count", "0")
        event["attempt"] = int(retry) + 1 if retry.isdigit() else None
        try:
            payload = json.loads(await request.aread())
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            event.update(request_text_size(payload, api), model=payload.get("model"))
            if self.track_tokens:
                try:
                    event["prefix_comparison"] = self.prefix_comparison.compare(
                        payload, api, event, context.get("agent"), context.get("composition_hints"))
                except Exception:
                    # Diagnostics cannot reject a send or leak the failed payload.
                    event["prefix_comparison"] = {"version": 1, "status": "unavailable"}
                try:
                    event["input_composition"] = input_composition(payload, api, context.get("composition_hints"))
                except Exception:
                    # Diagnostic attribution cannot change execution or invent a measurement.
                    event["input_composition"] = {"version": 1, "method": METHOD, "status": "unavailable"}
        except (ValueError, UnicodeError):
            # Observability must not turn an unfamiliar payload into a failed call.
            event.update(text_chars=None, measurement="unavailable")
        if self.track_tokens:
            event.update(token_measurement="provider_usage_v1",
                         context_window_tokens=context.get("context_window_tokens"),
                         context_limit_source=context.get("context_limit_source"),
                         max_output_tokens=context.get("max_output_tokens"))
            self.latest = event.copy()
            self._current_attempt = {"identity": event.copy(), "agent": context.get("agent")}
            request.extensions["qumus_request_attempt"] = self._current_attempt
        self.store.emit("agent.request", event, agent=context.get("agent"))
