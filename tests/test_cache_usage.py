"""Count raw cache evidence without estimating absent usage or joining retries."""

from copy import deepcopy

import pytest

from qumus2.agents.token_usage import cache_usage_summary, reported_usage
from qumus2.runtime.events import Event


def request(request_id="r1", *, agent="orchestrator", **changes):
    return {"kind": "agent.request", "agent": agent, "payload": {
        "request_id": request_id, "call_id": "call", "session_id": "session",
        "model": "openai/exact-model", "provider": "openai", "api": "responses",
        "gateway": "https://example.test/v1", "attempt": 1, **changes}}


def usage(req, inp=100, cache=80, out=10, **changes):
    return {"kind": "agent.request_usage", "agent": req["agent"], "payload": {
        **req["payload"], "measurement": "provider_usage_v1", "input_tokens": inp,
        "cached_input_tokens": cache, "output_tokens": out, **changes}}


def test_weighted_cache_fraction_and_hit_frequency_are_distinct():
    first, second = request("first"), request("second")
    result = cache_usage_summary([first, usage(first, 100, 100),
                                 second, usage(second, 900, 0)])
    totals = result["totals"]
    assert totals["attempts"] == totals["valid_cache_requests"] == 2
    assert totals["input_tokens"] == totals["cache_eligible_input_tokens"] == 1000
    assert totals["cached_input_tokens"] == 100
    assert totals["cached_input_fraction"] == .1
    assert totals["cache_hit_fraction"] == .5
    assert totals["cache_input_coverage"] == 1
    assert totals["output_tokens"] == 20
    assert len(result["groups"]) == 1
    assert result["groups"][0]["cached_input_fraction"] == .1


def test_partial_unknown_zero_and_invalid_preserve_their_denominators():
    events = []
    evidence = [(100, 80, None), (200, None, 10), (0, 0, 0), (50, 51, 5),
                (None, 10, 10), (20, True, 0)]
    for index, values in enumerate(evidence):
        req = request(str(index))
        events.extend([req, usage(req, *values)])
    missing = request("missing")
    events.append(missing)
    before = deepcopy(events)
    totals = cache_usage_summary(events)["totals"]
    assert events == before
    assert totals["attempts"] == 7
    assert totals["usage_requests"] == totals["known_usage_requests"] == 6
    assert totals["complete_usage_requests"] == 4
    assert totals["known_input_requests"] == 5
    assert totals["input_tokens"] == 370
    assert totals["output_tokens"] == 25
    assert totals["valid_cache_requests"] == 2
    assert totals["positive_input_requests"] == totals["cache_hit_requests"] == 1
    assert totals["known_positive_input_requests"] == 4
    assert totals["cached_input_tokens"] == 80
    assert totals["cached_input_fraction"] == .8
    assert totals["cache_input_coverage"] == pytest.approx(100 / 370)
    assert totals["cache_hit_fraction"] == 1
    assert totals["invalid_cache_requests"] == 2
    assert totals["zero_input_requests"] == 1
    assert totals["unknown_input_requests"] == 2
    assert totals["unknown_cache_requests"] == 3
    assert totals["incomplete_output_requests"] == 2
    assert totals["missing_usage_requests"] == 1
    assert totals["duplicate_usage_events"] == 0
    assert all(value is None or value >= 0 for value in totals.values())


def test_empty_and_zero_only_denominators_are_null():
    totals = cache_usage_summary([])["totals"]
    assert totals["cached_input_fraction"] is None
    assert totals["cache_hit_fraction"] is None
    assert totals["cache_input_coverage"] is None
    req = request()
    totals = cache_usage_summary([req, usage(req, 0, 0, 0)])["totals"]
    assert totals["valid_cache_requests"] == 1
    assert totals["positive_input_requests"] == 0
    assert totals["cached_input_fraction"] is None
    assert totals["cache_hit_fraction"] is None
    assert totals["cache_input_coverage"] is None


def test_duplicate_and_conflicting_reports_never_double_count_or_pick_a_winner():
    req = request()
    report = usage(req)
    totals = cache_usage_summary([req, deepcopy(req), report, deepcopy(report)])["totals"]
    assert totals["attempts"] == totals["usage_requests"] == 1
    assert totals["input_tokens"] == 100
    assert totals["duplicate_request_events"] == totals["duplicate_usage_events"] == 1
    conflicting = usage(req, cache=0)
    for reports in ([report, conflicting], [conflicting, report]):
        totals = cache_usage_summary([req, *reports])["totals"]
        assert totals["conflicting_usage_requests"] == 1
        assert totals["known_usage_requests"] == totals["input_tokens"] == 0
        assert totals["cached_input_fraction"] is None
        assert totals["incomplete_output_requests"] == 1
    # Different attempt metadata for the same complete request identity is ambiguous.
    totals = cache_usage_summary([req, request(attempt=2), report])["totals"]
    assert totals["attempts"] == totals["conflicting_request_requests"] == 1
    assert totals["input_tokens"] == 0


@pytest.mark.parametrize("field", ["call_id", "session_id", "model", "role"])
def test_reused_request_id_with_conflicting_core_identity_is_one_ambiguous_attempt(field):
    first = request()
    conflicting = deepcopy(first)
    if field == "role":
        conflicting["agent"] = "hands"
    else:
        conflicting["payload"][field] = "conflicting"
    events = [first, deepcopy(first), usage(first), conflicting, usage(conflicting)]
    original = deepcopy(events)
    for ordered in (events, list(reversed(events))):
        result = cache_usage_summary(ordered)
        totals = result["totals"]
        assert totals["attempts"] == totals["conflicting_request_requests"] == 1
        assert totals["usage_requests"] == totals["known_usage_requests"] == 0
        assert totals["valid_cache_requests"] == totals["input_tokens"] == 0
        assert totals["cached_input_tokens"] == totals["output_tokens"] == 0
        assert totals["cached_input_fraction"] is None
        assert totals["cache_hit_fraction"] is None
        assert len(result["groups"]) == 1
        if field != "call_id":
            assert result["groups"][0][field] is None
    assert events == original


def test_anonymous_requests_remain_separate_but_incomplete_identified_duplicates_count_once():
    anonymous = request()
    anonymous["payload"].pop("request_id")
    incomplete = request("known-request-id")
    incomplete["payload"].pop("call_id")
    result = cache_usage_summary([anonymous, deepcopy(anonymous), incomplete, deepcopy(incomplete)])
    totals = result["totals"]
    assert totals["attempts"] == totals["missing_usage_requests"] == 3
    assert totals["conflicting_request_requests"] == 0
    assert totals["duplicate_request_events"] == 1


@pytest.mark.parametrize("field,value", [
    ("request_id", "another"), ("call_id", "another"), ("session_id", "another"),
    ("model", "another"), ("provider", "another"), ("gateway", "https://other.test/v1"),
    ("api", "chat_completions"), ("attempt", 2), ("gateway", None), ("model", None),
])
def test_exact_recorded_identity_is_required(field, value):
    req = request()
    totals = cache_usage_summary([req, usage(req, **{field: value})])["totals"]
    assert totals["attempts"] == totals["missing_usage_requests"] == 1
    assert totals["unmatched_usage_events"] == 1
    assert totals["input_tokens"] == 0


def test_agent_identity_and_missing_core_fields_do_not_join():
    req = request()
    report = usage(req)
    report["agent"] = "hands"
    totals = cache_usage_summary([req, report])["totals"]
    assert totals["unmatched_usage_events"] == 1
    for key in ("request_id", "call_id", "session_id", "model"):
        incomplete = request()
        incomplete["payload"].pop(key)
        totals = cache_usage_summary([incomplete, usage(incomplete)])["totals"]
        assert totals["attempts"] == totals["missing_usage_requests"] == 1
        assert totals["unmatched_usage_events"] == 1


def test_retry_only_fills_its_own_attempt_and_only_raw_usage_is_eligible():
    first, retry = request("failed"), request("retry", attempt=2)
    normalized = usage(first, measurement="sdk_usage")
    cost = {**usage(first), "kind": "agent.usage"}
    totals = cache_usage_summary([first, retry, normalized, cost, usage(retry)])["totals"]
    assert totals["attempts"] == 2
    assert totals["usage_requests"] == totals["missing_usage_requests"] == 1
    assert totals["input_tokens"] == 100
    assert totals["usage_events"] == 2
    assert totals["ineligible_usage_events"] == 1


def test_groups_keep_every_recorded_dimension_and_historical_unknowns():
    requests = [request("base")]
    for field, value in [("provider", "other"), ("gateway", "https://other.test/v1"),
                         ("model", "exact-other"), ("api", "chat_completions"),
                         ("session_id", "other-session")]:
        requests.append(request(field, **{field: value}))
    requests.append(request("role", agent="hands"))
    historical = request("historical", provider=None, gateway=None, api=None)
    requests.append(historical)
    events = [event for req in requests for event in (req, usage(req))]
    groups = cache_usage_summary(reversed(events))["groups"]
    assert len(groups) == len(requests)
    assert all(group["attempts"] == 1 for group in groups)
    unknown = next(group for group in groups if group["provider"] is None)
    assert unknown["gateway"] is unknown["api"] is None
    assert unknown["cached_input_fraction"] == .8


def test_stored_events_and_payload_privacy():
    req = request()
    req["payload"].update(prompt="private prompt", headers={"Authorization": "private key"},
                          tools=[{"sensitive": "private tool content"}])
    report = usage(req, cache_write_tokens=7)
    objects = [Event(id=index, ts=1, **event) for index, event in enumerate([req, report])]
    result = cache_usage_summary(objects)
    assert "private" not in str(result)
    assert "request_id" not in str(result)
    assert result["totals"]["cache_write_tokens"] == 7
    assert result["totals"]["known_cache_write_requests"] == 1
    req = request(gateway="https://user:private@example.test/v1?key=private#private")
    result = cache_usage_summary([req, usage(req)])
    assert result["groups"][0]["gateway"] is None
    assert "private" not in str(result)


@pytest.mark.parametrize("details", ["input_tokens_details", "prompt_tokens_details"])
def test_cache_write_raw_field_presence_is_additive(details):
    assert reported_usage({details: {}})["cache_write_tokens"] is None
    assert reported_usage({details: {"cache_write_tokens": 0}})["cache_write_tokens"] == 0
    assert reported_usage({details: {"cache_write_tokens": 11}})["cache_write_tokens"] == 11
    assert reported_usage({details: {"cache_write_tokens": True}})["cache_write_tokens"] is None
