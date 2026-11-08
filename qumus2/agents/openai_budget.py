"""Persistent estimated spend ledger with an optional stage-total cap.

Reserve a conservative request allowance before sending it. If a connection
dies without usage, retain that reservation: unknown spend is never treated as
free. Rates are configured gateway estimates, not an invoice from the provider.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


class SpendLedger:
    def __init__(self, path: Path, limit: float | None = 200.0) -> None:
        if limit is not None and (not math.isfinite(limit) or limit <= 0):
            raise ValueError("stage spend budget must be positive and finite, or None for unlimited")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.limit = path, limit
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS spend "
                       "(id TEXT PRIMARY KEY, scope TEXT, reserved REAL, actual REAL)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def total(self, scope: str | None = None) -> float:
        with self._connect() as db:
            sql = "SELECT COALESCE(SUM(COALESCE(actual, reserved)), 0) FROM spend"
            return float(db.execute(sql + (" WHERE scope=?" if scope else ""),
                                    (scope,) if scope else ()).fetchone()[0])

    def reserve(self, scope: str, amount: float, test_limit: float) -> str:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            total = float(db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) "
                                     "FROM spend").fetchone()[0])
            test = float(db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) "
                                    "FROM spend WHERE scope=?", (scope,)).fetchone()[0])
            if (self.limit is not None and total + amount > self.limit) or test + amount > test_limit:
                stage_limit = "unlimited" if self.limit is None else f"${self.limit:.2f}"
                raise ValueError("estimated spend budget cannot cover another model request "
                                 f"(phase ${total:.2f}/{stage_limit}; "
                                 f"test ${test:.2f}/${test_limit:.2f}, "
                                 f"request reserve ${amount:.2f})")
            ident = uuid4().hex
            db.execute("INSERT INTO spend VALUES (?,?,?,NULL)", (ident, scope, amount))
            return ident

    def settle(self, ident: str, cost: float) -> None:
        with self._connect() as db:
            db.execute("UPDATE spend SET actual=? WHERE id=?", (cost, ident))

    def increase_reservation(self, ident: str, scope: str, amount: float, test_limit: float,
                             *, check: Callable[[float], None] | None = None) -> float:
        """Admit a serialized first-send bound on its original pending row.

        The callback observes the old row and receives only its proposed delta.
        It must be read-only, so inclusive campaign checks cannot double count
        the already-held allowance or publish a failed admission prematurely.
        """
        if not _nonnegative(amount) or not _nonnegative(test_limit) or test_limit <= 0:
            raise ValueError("Reservation allowance must be finite/nonnegative and scope cap finite/positive")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT scope,reserved,actual FROM spend WHERE id=?", (ident,)).fetchone()
            if row is None or row[0] != scope or row[2] is not None or not _nonnegative(row[1]):
                raise ValueError("Named spend reservation must be owned by this scope and unsettled")
            target = max(row[1], amount)
            delta = target-row[1]
            total = float(db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM spend").fetchone()[0])
            scoped = float(db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM spend WHERE scope=?",
                                      (scope,)).fetchone()[0])
            if (self.limit is not None and total+delta > self.limit) or scoped+delta > test_limit:
                raise ValueError("estimated spend budget cannot cover serialized request allowance")
            if check is not None:
                check(delta)
            if delta:
                db.execute("UPDATE spend SET reserved=? WHERE id=?", (target, ident))
            return target


def _nonnegative(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


_PROVENANCE_FIELDS = frozenset((
    'gateway', 'model', 'source', 'verified_at', 'recorded_at', 'source_sha256', 'basis',
    'input_price', 'output_price', 'cached_input_price', 'cache_read_not_more_than_input',
    'request_id', 'scope', 'currency', 'usd',
))


def _provenance(evidence: dict) -> dict:
    return {key: value for key, value in evidence.items() if key in _PROVENANCE_FIELDS}


def _safe_evidence_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return (parts.scheme in ('https', 'http') and bool(parts.hostname)
                and not (parts.username or parts.password or parts.query or parts.fragment))
    except ValueError:
        return False


def _scoped_evidence(evidence: object, gateway: str, model: str) -> bool:
    from datetime import datetime

    if not isinstance(evidence, dict):
        return False
    if (evidence.get('gateway') != gateway.rstrip('/') or evidence.get('model') != model
            or not isinstance(evidence.get('source'), str) or not evidence['source'].strip()
            or not _safe_evidence_url(evidence['gateway'])
            or ('://' in evidence['source'] and not _safe_evidence_url(evidence['source']))):
        return False
    if ('basis' in evidence and (not isinstance(evidence['basis'], str) or
            evidence['basis'] not in ('administrator_nominal_quote', 'recorded_configured_token_rates',
                                      'dated_route_rates', 'current_configured_token_rates'))):
        return False
    if ('cache_read_not_more_than_input' in evidence and
            type(evidence['cache_read_not_more_than_input']) is not bool):
        return False
    if 'source_sha256' in evidence and (not isinstance(evidence['source_sha256'], str)
            or len(evidence['source_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in evidence['source_sha256'])):
        return False
    try:
        # A date is provenance, not proof of applicability to another route/model.
        date_key = 'recorded_at' if evidence.get('basis') == 'recorded_configured_token_rates' else 'verified_at'
        datetime.fromisoformat(evidence[date_key].replace('Z', '+00:00'))
    except (KeyError, AttributeError, TypeError, ValueError):
        return False
    return True


def nominal_rates(gateway: str, model: str) -> dict | None:
    """Exact owner-supplied nominal quote; zero is a quote, never a missing value.

    The registry already converts micro-USD per million to USD per million
    (200000 micro-USD is 0.20 USD). It contains no cache tariff or account bill.
    """
    from qumus2.config import load_yaml

    catalog = load_yaml('model_context_limits.yaml')
    if gateway.rstrip('/') != catalog['base_url']:
        return None
    quote = catalog.get('quoted_prices_usd_per_million', {}).get(model)
    if not isinstance(quote, dict) or not all(_nonnegative(quote.get(k)) for k in ('input', 'output')):
        return None
    return {'gateway': gateway.rstrip('/'), 'model': model, 'source': catalog['source'],
            'verified_at': catalog['source_date'], 'source_sha256': catalog['source_sha256'],
            'input_price': quote['input'], 'output_price': quote['output'],
            'basis': 'administrator_nominal_quote'}


def request_cost(usage: dict, *, gateway: str, model: str, request_id: str | None = None,
                 rates: dict | None = None, billed: dict | None = None) -> dict:
    """Project one request without turning an estimate into an actual invoice.

    Billed evidence requires scope=request, currency=USD, source, verified_at,
    gateway, model, request_id and usd. Rate evidence requires source, verified_at,
    gateway, model, input_price and output_price; cached_input_price is optional.
    Saved configured rates instead carry basis=recorded_configured_token_rates
    and recorded_at, which dates the configuration without verifying a tariff.
    Evidence applies to this exact route/model only. A missing cache rate never
    earns a discount. `cache_read_not_more_than_input=True` explicitly attests the
    inequality needed to describe gross input pricing as an upper bound.

    Only normalized provider_usage_v1 input/output/cache-read counts are used.
    Cache-write fields are deliberately ignored: no supported provider contract
    currently establishes their tariff or non-overlap with input/cache reads.
    This function never settles a ledger, reprices history or invents usage.
    """
    from qumus2.agents.token_usage import token_count

    result = {'actual_usd': None, 'estimate_usd': None, 'upper_bound_usd': None,
              'gross_input_estimate_usd': None, 'basis': 'unavailable',
              'cache_status': 'unknown', 'unpriced_cache_write_tokens': None, 'provenance': None}
    if (_scoped_evidence(billed, gateway, model) and isinstance(request_id, str) and request_id
            and billed.get('request_id') == request_id and billed.get('scope') == 'request'
            and billed.get('currency') == 'USD' and _nonnegative(billed.get('usd'))):
        result.update(actual_usd=billed['usd'], basis='request_billed_cost', provenance=_provenance(billed))
        return result
    if not (_scoped_evidence(rates, gateway, model)
            and all(_nonnegative(rates.get(k)) for k in ('input_price', 'output_price'))
            and (rates.get('cached_input_price') is None or
                 _nonnegative(rates['cached_input_price']) and
                 rates['cached_input_price'] <= rates['input_price'])):
        rates = nominal_rates(gateway, model)
    if rates is None:
        return result
    result.update(basis=rates.get('basis', 'dated_route_rates'), provenance=_provenance(rates))
    inp, out = token_count(usage.get('input_tokens')), token_count(usage.get('output_tokens'))
    if (usage.get('measurement') != 'provider_usage_v1' or usage.get('usage_status') != 'reported'
            or inp is None or out is None):
        return result
    gross = (inp * rates['input_price'] + out * rates['output_price']) / 1_000_000
    result.update(estimate_usd=gross, gross_input_estimate_usd=gross)
    cached = token_count(usage.get('cached_input_tokens'))
    cached = cached if cached is not None and cached <= inp else None
    cache_price = rates.get('cached_input_price')
    if cached is not None and cache_price is not None:
        estimate = ((inp - cached) * rates['input_price'] + cached * cache_price
                    + out * rates['output_price']) / 1_000_000
        result.update(estimate_usd=estimate, upper_bound_usd=estimate, cache_status='measured_cache_reads')
    elif cached == 0 or inp == 0:
        result.update(upper_bound_usd=gross, cache_status='no_cache_reads')
    else:
        result['cache_status'] = 'unknown_cache_rate' if cached is not None else 'unknown_cache_tokens'
        if cache_price is not None or rates.get('cache_read_not_more_than_input') is True:
            result['upper_bound_usd'] = gross
    writes = token_count(usage.get('cache_write_tokens'))
    if writes:
        result.update(unpriced_cache_write_tokens=writes, upper_bound_usd=None)
    return result
