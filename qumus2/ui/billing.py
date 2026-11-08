"""Read-only display estimates calibrated against a private gateway bill snapshot."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def calibrated_estimate(store: Any, snapshot: dict[str, Any], *, gateway: str) -> dict[str, Any] | None:
    """Apply each model's observed blended cost to reported workspace tokens.

    The snapshot is account-scoped, so this is an estimate, never a per-run
    invoice or a replacement for the conservative spending ledger. Unknown
    usage and models without calibration are not priced as free.
    """
    rates = {row['model']: row['usd'] / row['tokens']
             for row in snapshot['by_model'] if (row.get('tokens') or 0) > 0}
    if not rates:
        return None
    usd, requests, unpriced, unpriced_turns = 0.0, 0, 0, 0
    # Request-usage reports retain raw field presence before SDK zero defaults.
    # Cost events identify historical responses but cannot establish full usage.
    rows = store._x("SELECT kind, payload_json FROM event "
                    "WHERE kind IN ('agent.request', 'agent.request_usage', 'agent.usage', 'agent.turn_end') "
                    "ORDER BY id").fetchall()
    attempts, reports, identities = {}, {}, set()
    for row in rows:
        p = json.loads(row['payload_json'])
        if row['kind'] == 'agent.turn_end':
            if not p.get('cost_accounted_by_usage'):
                unpriced_turns += 1
            continue
        request_id = p.get('request_id')
        if not isinstance(request_id, str) or not request_id:
            if row['kind'] == 'agent.usage':
                unpriced += 1
            continue
        identities.add(request_id)
        if row['kind'] == 'agent.request':
            attempts[request_id] = p
        elif row['kind'] == 'agent.request_usage':
            reports[request_id] = p
    for request_id in identities:
        p = reports.get(request_id, {})
        attempt = attempts.get(request_id, {})
        source = p.get('gateway') or attempt.get('gateway')
        counts = (p.get('input_tokens'), p.get('output_tokens'))
        rate = rates.get(p.get('model'))
        if (p.get('provider') != 'openai' or source != gateway.rstrip('/') or
                (attempt.get('gateway') and attempt['gateway'] != source) or rate is None or
                (attempt.get('model') and attempt['model'] != p.get('model')) or
                p.get('measurement') != 'provider_usage_v1' or p.get('usage_status') != 'reported' or
                any(type(n) is not int or not 0 <= n <= 2**53 - 1 for n in counts)):
            unpriced += 1
            continue
        usd += sum(counts) * rate
        requests += 1
    return {'usd': usd if requests or not (unpriced or unpriced_turns) else None,
            'requests': requests, 'unpriced_requests': unpriced,
            'unpriced_turns': unpriced_turns,
            'basis': 'gateway_billed_average', 'verified_at': snapshot['verified_at']}


def request_estimate(store: Any, *, gateway: str, configured_rates: dict | None = None,
                     billed_requests: list[dict] | None = None) -> dict[str, Any]:
    """Read-only request projection; account-wide averages stay separate above.

    Saved response guard rates are estimates with their original event date,
    never verified provider tariffs. Current configured rates, when supplied,
    require exact scope and explicit provenance. Otherwise the dated exact-ID
    administrator quote supplies a nominal estimate, including genuine zeros.
    """
    from qumus2.agents.openai_budget import nominal_rates, request_cost
    from qumus2.agents.token_usage import cache_usage_summary

    rows = store._x("SELECT kind, ts, agent, payload_json FROM event "
                    "WHERE kind IN ('agent.request', 'agent.request_usage', 'agent.usage', 'agent.turn_end') "
                    "ORDER BY id").fetchall()
    attempts, reports, costs, identities = {}, {}, {}, set()
    cache_events, conflicts = [], set()
    attempt_roles, report_roles, cost_roles = {}, {}, {}
    unpriced, unpriced_turns = 0, 0
    for row in rows:
        p = json.loads(row['payload_json'])
        cache_events.append({'kind': row['kind'], 'agent': row['agent'], 'payload': p})
        if row['kind'] == 'agent.turn_end':
            unpriced_turns += not p.get('cost_accounted_by_usage', False)
            continue
        rid = p.get('request_id')
        if not isinstance(rid, str) or not rid:
            if row['kind'] == 'agent.usage':
                unpriced += 1
            continue
        identities.add(rid)
        if row['kind'] == 'agent.request':
            if rid in attempts and (attempts[rid] != p or attempt_roles[rid] != row['agent']):
                conflicts.add(rid)
            attempts[rid] = p
            attempt_roles[rid] = row['agent']
        elif row['kind'] == 'agent.request_usage':
            if rid in reports and (reports[rid] != p or report_roles[rid] != row['agent']):
                conflicts.add(rid)
            reports[rid] = p
            report_roles[rid] = row['agent']
        else:
            if rid in costs and (costs[rid][0] != p or cost_roles[rid] != row['agent']):
                conflicts.add(rid)
            costs[rid] = (p, row['ts'])
            cost_roles[rid] = row['agent']
    bills, conflicting_bills = {}, set()
    for bill in billed_requests or []:
        if not isinstance(bill, dict):
            continue
        rid = bill.get('request_id')
        if rid in bills and bills[rid] != bill:
            conflicting_bills.add(rid)
        bills[rid] = bill
    for rid in conflicting_bills:
        bills.pop(rid, None)
    estimated, actual, upper, priced, billed_count, unknown_bounds = 0.0, 0.0, 0.0, 0, 0, 0
    bases: dict[str, int] = {}
    price_sources: dict[tuple, dict] = {}
    identity_fields = ('request_id', 'call_id', 'session_id', 'model', 'api', 'attempt', 'provider', 'gateway')
    cost_identity = ('request_id', 'call_id', 'session_id', 'model', 'provider', 'gateway')
    quotes: dict[str, dict | None] = {}
    for rid in identities:
        p, attempt = reports.get(rid, {}), attempts.get(rid, {})
        source, model = p.get('gateway') or attempt.get('gateway'), p.get('model') or attempt.get('model')
        if (rid in conflicts or (p.get('provider') or attempt.get('provider')) != 'openai' or source != gateway.rstrip('/') or not model
                or (p and (not attempt or attempt_roles.get(rid) != report_roles.get(rid)
                           or not isinstance(report_roles.get(rid), str) or not report_roles[rid]
                           or any(not isinstance(p.get(k), str) or not p[k]
                                  for k in ('request_id', 'call_id', 'session_id', 'model'))
                           or any(attempt.get(k) != p.get(k) for k in identity_fields)))):
            unpriced += 1
            continue
        evidence = configured_rates
        if rid in costs:
            cost, ts = costs[rid]
            saved = cost.get('pricing')
            if (isinstance(saved, dict) and p and isinstance(cost_roles.get(rid), str)
                    and cost_roles[rid] and cost_roles[rid] == report_roles.get(rid)
                    and all(isinstance(cost.get(k), str) and cost[k] and cost[k] == p.get(k)
                            for k in cost_identity)
                    and all(cost[k] == p.get(k) for k in ('api', 'attempt')
                            if cost.get(k) is not None)
                    and cost.get('cost_basis') == 'configured_token_rates'
                    and all(cost.get(k) == p.get(k) for k in ('input_tokens', 'output_tokens'))):
                evidence = {'gateway': source, 'model': model,
                            'source': 'saved_response_guard_rates',
                            'recorded_at': datetime.fromtimestamp(ts, UTC).isoformat(),
                            'basis': 'recorded_configured_token_rates',
                            'input_price': saved.get('input_usd_per_million'),
                            'output_price': saved.get('output_usd_per_million'),
                            'cached_input_price': saved.get('cached_input_usd_per_million')}
        if evidence is None:
            if model not in quotes:
                quotes[model] = nominal_rates(source, model)
            evidence = quotes[model]
        cost = request_cost(p, gateway=source, model=model, request_id=rid,
                            rates=evidence, billed=bills.get(rid))
        value = cost['actual_usd'] if cost['actual_usd'] is not None else cost['estimate_usd']
        if value is None:
            unpriced += 1
            continue
        priced += 1
        estimated += value
        bases[cost['basis']] = bases.get(cost['basis'], 0) + 1
        provenance = {k: v for k, v in cost['provenance'].items() if k in (
            'gateway', 'model', 'source', 'source_sha256', 'input_price', 'output_price',
            'cached_input_price', 'cache_read_not_more_than_input')}
        provenance['basis'] = cost['basis']
        source_key = tuple(sorted(provenance.items()))
        date = cost['provenance'].get('recorded_at', cost['provenance'].get('verified_at'))
        grouped = price_sources.setdefault(source_key, {**provenance, 'first_at': date,
                                                        'last_at': date, 'requests': 0})
        grouped['first_at'] = min(grouped['first_at'], date)
        grouped['last_at'] = max(grouped['last_at'], date)
        grouped['requests'] += 1
        if cost['actual_usd'] is not None:
            billed_count += 1
            actual += value
            upper += value
        elif cost['upper_bound_usd'] is not None:
            upper += cost['upper_bound_usd']
        else:
            unknown_bounds += 1
    incomplete = unpriced or unpriced_turns
    return {'estimate_usd': estimated if priced or not incomplete else None,
            'actual_usd': actual if priced and billed_count == priced and not incomplete else None,
            'upper_bound_usd': upper if not (incomplete or unknown_bounds) else None,
            'billed_usd': actual if billed_count else None,
            'requests': priced, 'billed_requests': billed_count, 'identified_requests': len(identities),
            'unpriced_requests': unpriced, 'unpriced_turns': unpriced_turns,
            'unknown_bound_requests': unknown_bounds, 'bases': bases,
            'basis': 'per_request_evidence', 'price_sources': list(price_sources.values()), 'cache_usage': cache_usage_summary(cache_events),
            'detail': 'Request bills are actual only with matching request identity. Saved guard rates and '
                      'administrator quotes are token estimates; missing cache tariffs remain unknown.'}
