"""Normalize provider-reported request usage without estimating missing tokens."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit


def token_count(value: Any) -> int | None:
    # Booleans, strings, negative values and SDK defaults are not measurements.
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def reported_usage(raw: Any) -> dict[str, Any]:
    """Accept both OpenAI-compatible protocols; retain unknowns and real zeros."""
    raw = raw if isinstance(raw, dict) else {}
    inp = token_count(raw.get("input_tokens", raw.get("prompt_tokens")))
    out = token_count(raw.get("output_tokens", raw.get("completion_tokens")))
    input_details = raw.get("input_tokens_details", raw.get("prompt_tokens_details")) or {}
    output_details = raw.get("output_tokens_details", raw.get("completion_tokens_details")) or {}
    return {
        "input_tokens": inp, "output_tokens": out,
        "total_tokens": inp + out if inp is not None and out is not None else None,
        "provider_total_tokens": token_count(raw.get("total_tokens")),
        "cached_input_tokens": token_count(input_details.get("cached_tokens"))
        if isinstance(input_details, dict) else None,
        "cache_write_tokens": token_count(input_details.get("cache_write_tokens"))
        if isinstance(input_details, dict) else None,
        "reasoning_output_tokens": token_count(output_details.get("reasoning_tokens"))
        if isinstance(output_details, dict) else None,
        "usage_status": "reported" if inp is not None and out is not None else
                        "partial" if inp is not None or out is not None else "unavailable",
        "measurement": "provider_usage_v1", "unit": "tokens",
    }


_IDENTITY = ("request_id", "call_id", "session_id", "model")
_OPTIONAL_IDENTITY = ("provider", "gateway", "api", "attempt")
_GROUP = ("provider", "gateway", "model", "api", "role", "session_id")
_REPORT_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens")
_COUNTS = (
    "attempts", "usage_requests", "known_usage_requests", "complete_usage_requests",
    "known_input_requests", "known_output_requests", "known_cache_requests",
    "known_positive_input_requests", "valid_cache_requests", "positive_input_requests",
    "cache_hit_requests", "missing_usage_requests", "unknown_input_requests",
    "unknown_cache_requests", "incomplete_output_requests", "zero_input_requests",
    "invalid_cache_requests", "conflicting_request_requests", "conflicting_usage_requests",
    "duplicate_request_events", "duplicate_usage_events", "input_tokens", "output_tokens",
    "cache_eligible_input_tokens", "cached_input_tokens", "cache_write_tokens",
    "known_cache_write_requests",
)


def _event_parts(event: Any) -> tuple[str | None, str | None, Mapping[str, Any]]:
    """Read recording dictionaries and stored Events without copying their payloads."""
    read = event.get if isinstance(event, Mapping) else lambda key: getattr(event, key, None)
    payload = read("payload")
    agent = read("agent")
    return read("kind"), agent if isinstance(agent, str) and agent else None, (
        payload if isinstance(payload, Mapping) else {})


def _identity(payload: Mapping[str, Any], agent: str | None) -> tuple[Any, ...] | None:
    values = tuple(payload.get(key) for key in _IDENTITY) + (agent,)
    return values if all(isinstance(v, str) and v for v in values) else None


def _optional_identity(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(payload.get(key) if payload.get(key) != "" else None
                 for key in _OPTIONAL_IDENTITY)


def _report_signature(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    # Keep malformed values distinct from absent fields; never repair the source.
    return tuple((key in payload, type(payload.get(key)), payload.get(key))
                 for key in _REPORT_FIELDS)


def _group_value(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        return None
    if key == "gateway":
        # Producers record sanitized URLs. Refuse unsafe imported metadata rather
        # than exposing credentials/query values in a count-only projection.
        try:
            parts = urlsplit(value)
            if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                    or parts.password or parts.query or parts.fragment):
                return None
        except ValueError:
            return None
    return value


def _cache_counts() -> dict[str, Any]:
    return dict.fromkeys(_COUNTS, 0)


def _cache_ratios(counts: dict[str, Any]) -> None:
    def ratio(numerator: str, denominator: str) -> float | None:
        return counts[numerator] / counts[denominator] if counts[denominator] else None

    counts["cached_input_fraction"] = ratio("cached_input_tokens", "cache_eligible_input_tokens")
    counts["cache_hit_fraction"] = ratio("cache_hit_requests", "positive_input_requests")
    counts["cache_input_coverage"] = ratio("cache_eligible_input_tokens", "input_tokens")


def cache_usage_summary(events: Iterable[Any]) -> dict[str, Any]:
    """Summarize raw usage for exact HTTP attempts, without prompts or cost estimates.

    Only ``agent.request_usage`` with measurement ``provider_usage_v1`` qualifies.
    Request/call/session/model/agent must all be recorded and match. Optional
    provider/gateway/API/attempt values must match too, including unknowns: a
    recorded identity on one side never fills the other side's historical gap.
    Requests sharing a request ID count once; conflicting core or optional
    identities supply no usage. Reports with different identity are unmatched,
    never retry recovery. Requests without an ID remain separate anonymous sends.

    Cache fractions use valid I/C pairs (0 <= C <= I), independently of output
    coverage. Hit frequency uses only those pairs with I > 0. Token coverage is
    sum(I for valid pairs) / sum(all known I); unknown input remains explicitly
    counted, so coverage cannot establish completeness for those attempts.
    Groups expose only recorded deployment/model/role/session metadata and counts.
    """
    requests: dict[str | tuple[int], list[tuple[str | None, Mapping[str, Any]]]] = {}
    reports: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    diagnostics = {"usage_events": 0, "ineligible_usage_events": 0, "unmatched_usage_events": 0}
    anonymous = 0
    for event in events:
        kind, agent, payload = _event_parts(event)
        if kind not in ("agent.request", "agent.request_usage"):
            continue
        identity = _identity(payload, agent)
        if kind == "agent.request":
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                anonymous += 1
                request_id = (anonymous,)
            requests.setdefault(request_id, []).append((agent, payload))
        else:
            diagnostics["usage_events"] += 1
            if payload.get("measurement") != "provider_usage_v1":
                diagnostics["ineligible_usage_events"] += 1
            elif identity is None:
                diagnostics["unmatched_usage_events"] += 1
            else:
                reports.setdefault(identity, []).append(payload)

    totals = _cache_counts()
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for attempts in requests.values():
        agent, request = attempts[0]
        identity = _identity(request, agent)
        core = tuple(request.get(key) for key in _IDENTITY) + (agent,)
        optional = _optional_identity(request)
        conflicting_request = any(
            tuple(p.get(key) for key in _IDENTITY) + (role,) != core
            or _optional_identity(p) != optional for role, p in attempts[1:])
        # Every conflicting grouping dimension stays unknown instead of choosing one.
        group = tuple((agent if all(role == agent for role, _ in attempts) else None)
                      if key == "role" else
                      _group_value(request, key)
                      if all(p.get(key) == request.get(key) for _, p in attempts) else None
                      for key in _GROUP)
        counts = _cache_counts()
        counts["attempts"] = 1
        counts["conflicting_request_requests"] = int(conflicting_request)
        counts["duplicate_request_events"] = len(attempts) - 1 if not conflicting_request else 0
        candidates = reports.pop(identity, [])
        matched = [p for p in candidates if _optional_identity(p) == optional]
        diagnostics["unmatched_usage_events"] += len(candidates) - len(matched)
        conflicting_usage = bool(matched) and any(
            _report_signature(p) != _report_signature(matched[0]) for p in matched[1:])
        counts["conflicting_usage_requests"] = int(conflicting_usage)
        counts["duplicate_usage_events"] = max(0, len(matched) - 1) if not conflicting_usage else 0
        report = matched[0] if matched and not conflicting_usage and not conflicting_request else {}
        counts["usage_requests"] = int(bool(report))
        counts["missing_usage_requests"] = int(not matched)
        inp, out, cache, write = (token_count(report.get(key)) for key in _REPORT_FIELDS)
        counts["known_input_requests"] = int(inp is not None)
        counts["known_output_requests"] = int(out is not None)
        counts["known_cache_requests"] = int(cache is not None)
        counts["known_cache_write_requests"] = int(write is not None)
        counts["known_usage_requests"] = int(inp is not None or out is not None)
        counts["complete_usage_requests"] = int(inp is not None and out is not None)
        counts["unknown_input_requests"] = int(inp is None)
        counts["unknown_cache_requests"] = int(cache is None)
        counts["incomplete_output_requests"] = int(out is None)
        counts["zero_input_requests"] = int(inp == 0)
        counts["known_positive_input_requests"] = int(inp is not None and inp > 0)
        counts["invalid_cache_requests"] = int(
            (report.get("cached_input_tokens") is not None and cache is None)
            or (inp is not None and cache is not None and cache > inp))
        counts["input_tokens"] = inp or 0
        counts["output_tokens"] = out or 0
        counts["cache_write_tokens"] = write or 0
        if inp is not None and cache is not None and cache <= inp:
            counts["valid_cache_requests"] = 1
            counts["cache_eligible_input_tokens"] = inp
            counts["cached_input_tokens"] = cache
            counts["positive_input_requests"] = int(inp > 0)
            counts["cache_hit_requests"] = int(cache > 0)
        bucket = groups.setdefault(group, _cache_counts())
        for key, value in counts.items():
            totals[key] += value
            bucket[key] += value

    diagnostics["unmatched_usage_events"] += sum(len(items) for items in reports.values())
    _cache_ratios(totals)
    output_groups = []
    for group, counts in sorted(groups.items(), key=lambda item: tuple(v or "" for v in item[0])):
        _cache_ratios(counts)
        output_groups.append({**dict(zip(_GROUP, group, strict=True)), **counts})
    return {"measurement": "provider_usage_v1", "totals": {**totals, **diagnostics},
            "groups": output_groups}
