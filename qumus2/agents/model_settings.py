"""Read-only gateway model discovery; no inference calls or credential export."""

from __future__ import annotations

import json
import math
import time
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.config import load_yaml
from qumus2.core.http import tls_context
from qumus2.core.scrub import scrub_text

TOKEN_STATION = 'physics.bytefuture.ai'


def agent_model_choices(value: Any) -> dict[str, dict[str, Any]]:
    """Validate the closed specialist override map before changing runtime state."""
    from qumus2.agents.roster import SUBAGENTS

    if not isinstance(value, dict) or set(value) - set(SUBAGENTS):
        raise ValueError('Choose models only for known specialist agents.')
    result = {}
    for role, choice in value.items():
        if not isinstance(choice, dict) or set(choice) != {'model', 'input_price', 'output_price'}:
            raise ValueError('Each specialist model needs its ID and both budget rates.')
        if not isinstance(choice['model'], str) or not choice['model'].strip():
            raise ValueError('Choose a model for each specialist override.')
        if any(type(choice[key]) not in (int, float) or not math.isfinite(choice[key]) or choice[key] < 0
               for key in ('input_price', 'output_price')):
            raise ValueError('Every specialist model needs non-negative finite budget rates.')
        result[role] = choice.copy()
    # A model has one price throughout a team, regardless of who calls it.
    prices = {}
    for choice in result.values():
        pair = (choice['input_price'], choice['output_price'])
        if choice['model'] in prices and prices[choice['model']] != pair:
            raise ValueError('Use the same budget rates for the same model across agents.')
        prices[choice['model']] = pair
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward this project's credential to another origin


class ModelCatalog:
    def __init__(self, creds: OpenAICredentials) -> None:
        self.creds = creds
        self._rows: list[dict[str, Any]] | None = None
        self._at = 0.0

    def list(self) -> list[dict[str, Any]]:
        if self._rows is not None and time.monotonic() - self._at < 300:
            return self._rows
        c = self.creds
        station = urlsplit(c.base_url).hostname == TOKEN_STATION
        url = c.base_url.removesuffix('/v1') + '/api/models' if station else c.base_url + '/models'
        request = urllib.request.Request(url, headers={'Authorization': f'Bearer {c.api_key}',
                                                       'Accept': 'application/json'})
        opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=tls_context()))
        try:
            with opener.open(request, timeout=10) as response:
                data = json.load(response)
        except Exception as exc:
            raise ValueError('Model catalog unavailable: ' + scrub_text(str(exc), [c.api_key])) from None
        rows = data if station else data.get('data', [])
        if not isinstance(rows, list):
            raise ValueError('Model catalog returned an unexpected format.')
        self._rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get('name' if station else 'id')
            modalities = row.get('modalities', [])
            modalities = [m for m in modalities if isinstance(m, str)] if isinstance(modalities, list) else []
            if not isinstance(name, str) or not name.strip():
                continue
            # Featured gateway models report zero prices but is_free=false.
            # Unknown rates must not turn into free requests or a false bill.
            inp, out = row.get('input_price_per_million'), row.get('output_price_per_million')
            known = station and type(inp) in (int, float) and math.isfinite(inp) and inp >= 0 and (
                row.get('is_free') is True or inp > 0)
            self._rows.append({'id': name, 'vision': 'multimodal' in modalities if station and modalities else None,
                               'modalities': modalities,
                               'conversational': bool({'text', 'multimodal'} & set(modalities)) if station and modalities else None,
                               'input_price': inp / 1_000_000 if known else None,
                               'output_price': out / 1_000_000 if known and type(out) in (int, float) and math.isfinite(out) and out >= 0 else None})
        if station:
            # The account-facing list is distinct from the website registry.
            account_ids = None
            try:
                request = urllib.request.Request(c.base_url.rstrip('/') + '/models',
                    headers={'Authorization': f'Bearer {c.api_key}', 'Accept': 'application/json'})
                with opener.open(request, timeout=10) as response:
                    account = json.load(response)
                if isinstance(account, dict) and isinstance(account.get('data'), list):
                    account_ids = {r['id'] for r in account['data']
                                   if isinstance(r, dict) and isinstance(r.get('id'), str)}
            except Exception:
                pass  # No account-access claim when discovery failed.
            catalog = load_yaml('model_context_limits.yaml')
            scoped = c.base_url.rstrip('/') == catalog['base_url']
            limits = catalog['models'] if scoped else {}
            prices = catalog.get('quoted_prices_usd_per_million', {}) if scoped else {}
            by_id = {r['id']: r for r in self._rows}
            website_ids = set(by_id)
            for name in set(limits) | (account_ids or set()):
                by_id.setdefault(name, {'id': name, 'vision': None, 'modalities': [],
                    'conversational': None, 'input_price': None, 'output_price': None})
            for name, row in by_id.items():
                capacity = limits.get(name)
                row.update(catalog_checked_at=time.time(), gateway=c.base_url.rstrip('/'),
                    account_scope='current_configured_key', registered=name in limits, website_listed=name in website_ids,
                    account_listed=name in account_ids if account_ids is not None else None,
                    access_verified=False, context_window_tokens=capacity or None,
                    context_reason='administrator_table' if capacity else
                        'administrator_zero' if name in limits else 'exact_id_not_in_administrator_table',
                    administrator_quote=prices.get(name), quote_currency='USD',
                    quote_unit='per_million_tokens', quote_source_date=catalog['source_date'] if name in prices else None,
                    quote_source_sha256=catalog['source_sha256'] if name in prices else None,
                    quote_account_applicability='unverified', quote_effective_at=None,
                    cache_input_price=None, account_multiplier=None)
            self._rows = list(by_id.values())
        self._rows.sort(key=lambda r: r['id'])
        self._at = time.monotonic()
        return self._rows
