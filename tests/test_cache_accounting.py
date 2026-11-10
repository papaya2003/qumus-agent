"""Exact quote units, scoped cost evidence and honest cache-cost uncertainty."""

from __future__ import annotations

import json
import sqlite3

import pytest

from qumus2.agents.openai_budget import SpendLedger, nominal_rates, request_cost
from qumus2.agents.token_usage import reported_usage
from qumus2.ui.app import ModelChoice
from qumus2.ui.billing import request_estimate

GATEWAY = 'https://physics.bytefuture.ai/v1'
ASTRA = 'openai/gpt-6-astra'


def usage(inp=1_000_000, out=100_000, cached=250_000):
    return reported_usage({'input_tokens': inp, 'output_tokens': out,
                           'input_tokens_details': {'cached_tokens': cached}})


def rates(model=ASTRA, **extra):
    return {'gateway': GATEWAY, 'model': model, 'source': 'dated_fixture',
            'verified_at': '2026-10-29T12:00:00Z', 'input_price': 10, 'output_price': 50,
            **extra}


@pytest.mark.parametrize('model,micro_input,micro_output,expected', [
    ('atria/atria-dawn', 200000, 1200000, .32),
    ('qwen-token-plan/deepseek-v4.1-flash', 300000, 1200000, .42),
    ('qwen-token-plan/qwen3.8-max', 2000000, 6000000, 2.6),
    ('olares-qumusphusis/qwen-3.8-27b', 0, 0, 0),
])
def test_exact_nominal_micro_usd_quotes_are_not_inflated(model, micro_input, micro_output, expected):
    quote = nominal_rates(GATEWAY, model)
    assert quote['input_price'] == micro_input / 1_000_000
    assert quote['output_price'] == micro_output / 1_000_000
    cost = request_cost(usage(), gateway=GATEWAY, model=model)
    assert cost['estimate_usd'] == pytest.approx(expected)
    assert cost['gross_input_estimate_usd'] == pytest.approx(expected)
    assert cost['actual_usd'] is None and cost['upper_bound_usd'] is None
    assert cost['basis'] == 'administrator_nominal_quote'
    assert cost['cache_status'] == 'unknown_cache_rate'
    assert cost['provenance']['source_sha256'] == quote['source_sha256']


def test_exact_dated_astra_cache_rate_is_not_shared_with_another_route_or_model():
    tariff = rates(cached_input_price=1)
    cost = request_cost(usage(), gateway=GATEWAY, model=ASTRA, rates=tariff)
    assert cost['estimate_usd'] == pytest.approx(12.75)
    assert cost['actual_usd'] is None
    assert cost['cache_status'] == 'measured_cache_reads'
    other = request_cost(usage(), gateway=GATEWAY, model='atria/atria-dawn', rates=tariff)
    assert other['estimate_usd'] == pytest.approx(.32)
    assert other['provenance'].get('cached_input_price') is None
    assert request_cost(usage(), gateway='https://other.example/v1', model=ASTRA,
                        rates=tariff)['estimate_usd'] is None


@pytest.mark.parametrize('extra,expected,status', [
    ({'cached_input_price': 1}, 12.75, 'measured_cache_reads'),
    ({'cached_input_price': 0}, 12.5, 'measured_cache_reads'),
    ({'cache_read_not_more_than_input': True}, 15, 'unknown_cache_rate'),
    ({}, 15, 'unknown_cache_rate'),
])
def test_missing_cache_price_does_not_invent_discount_or_bound(extra, expected, status):
    cost = request_cost(usage(), gateway=GATEWAY, model=ASTRA, rates=rates(**extra))
    assert cost['estimate_usd'] == pytest.approx(expected)
    assert cost['cache_status'] == status
    assert cost['upper_bound_usd'] == (None if not extra else pytest.approx(expected))


@pytest.mark.parametrize('cached', [None, True, '250000', -1, 1_000_001])
def test_unknown_or_invalid_cache_measurement_never_gets_discount(cached):
    cost = request_cost(usage(cached=cached), gateway=GATEWAY, model=ASTRA,
                        rates=rates(cached_input_price=1))
    assert cost['estimate_usd'] == 15
    assert cost['upper_bound_usd'] == 15
    assert cost['cache_status'] == 'unknown_cache_tokens'


def test_real_zero_cache_and_zero_input_remain_known_without_cache_tariff():
    for report in [usage(cached=0), usage(inp=0, cached=None)]:
        cost = request_cost(report, gateway=GATEWAY, model=ASTRA, rates=rates())
        assert cost['upper_bound_usd'] == cost['estimate_usd']
        assert cost['cache_status'] == 'no_cache_reads'


def test_partial_input_output_cannot_turn_into_zero_cost():
    cost = request_cost(reported_usage({'input_tokens': 100}), gateway=GATEWAY, model=ASTRA)
    assert cost['estimate_usd'] is None and cost['actual_usd'] is None


def bill(**extra):
    return {'gateway': GATEWAY, 'model': ASTRA, 'source': 'signed_in_request_bill',
            'verified_at': '2026-10-29T12:00:00Z', 'request_id': 'r1', 'scope': 'request',
            'currency': 'USD', 'usd': .123, **extra}


def test_request_billed_cost_has_priority_even_when_provider_usage_is_unavailable():
    cost = request_cost({}, gateway=GATEWAY, model=ASTRA, request_id='r1',
                        rates=rates(cached_input_price=1), billed=bill())
    assert cost['actual_usd'] == .123 and cost['estimate_usd'] is None
    assert cost['basis'] == 'request_billed_cost'


@pytest.mark.parametrize('extra', [
    {'request_id': 'retry'}, {'model': 'other'}, {'gateway': 'https://other.example/v1'},
    {'scope': 'account'}, {'currency': 'EUR'}, {'verified_at': ''}, {'source': ''},
    {'usd': -1}, {'usd': True}, {'usd': float('nan')},
])
def test_mismatched_or_malformed_bill_is_never_an_actual_request_charge(extra):
    cost = request_cost(usage(), gateway=GATEWAY, model=ASTRA, request_id='r1', billed=bill(**extra))
    assert cost['actual_usd'] is None
    assert cost['basis'] == 'administrator_nominal_quote'


@pytest.mark.parametrize('extra', [
    {'input_price': True}, {'output_price': float('inf')}, {'cached_input_price': 11},
    {'cached_input_price': -1}, {'verified_at': None}, {'source': ''}, {'model': 'other'},
])
def test_invalid_rate_evidence_falls_back_to_exact_nominal(extra):
    cost = request_cost(usage(), gateway=GATEWAY, model=ASTRA, rates=rates(**extra))
    assert cost['basis'] == 'administrator_nominal_quote'
    assert cost['estimate_usd'] == 15


def test_undocumented_cache_write_fields_are_not_discounted_or_double_charged():
    report = usage()
    report['cache_write_tokens'] = 100
    cost = request_cost(report, gateway=GATEWAY, model=ASTRA, rates=rates(cached_input_price=1))
    assert cost['estimate_usd'] == 12.75
    assert cost['unpriced_cache_write_tokens'] == 100
    assert cost['upper_bound_usd'] is None


def test_zero_price_model_schema_keeps_budget_limits_and_unknown_ledger_history(tmp_path):
    assert ModelChoice(model='free-nominal', input_price=0, output_price=0).input_price == 0
    ledger = SpendLedger(tmp_path / 'budget.db', limit=1)
    prior = ledger.reserve('workspace', .5, 1)
    zero = ledger.reserve('workspace', 0, 1)
    ledger.settle(zero, 0)
    assert ledger.total() == .5
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT actual FROM spend WHERE id=?', (prior,)).fetchone() == (None,)


def emit_request(store, rid, model='atria/atria-dawn', cached=250_000):
    identity = {'request_id': rid, 'provider': 'openai', 'gateway': GATEWAY,
                'model': model, 'call_id': f'call-{rid}', 'session_id': 'session',
                'api': 'responses', 'attempt': 1}
    store.emit('agent.request', identity, agent='orchestrator')
    store.emit('agent.request_usage', {**identity, **usage(cached=cached)}, agent='orchestrator')
    return identity


def test_projection_retries_duplicates_unknowns_and_cache_summary_share_existing_history(services):
    store = services.store
    identity = emit_request(store, 'success')
    # Duplicate response is one charge, failed retry stays an unknown attempt.
    report = {**identity, **usage()}
    store.emit('agent.request_usage', report, agent='orchestrator')
    store.emit('agent.request', {**identity, 'request_id': 'unknown-retry'}, agent='orchestrator')
    before = [tuple(row) for row in store._x('SELECT * FROM event').fetchall()]
    projected = request_estimate(store, gateway=GATEWAY)
    assert projected['requests'] == 1 and projected['unpriced_requests'] == 1
    assert projected['estimate_usd'] == pytest.approx(.32)
    assert projected['actual_usd'] is projected['upper_bound_usd'] is None
    totals = projected['cache_usage']['totals']
    assert totals['attempts'] == 2 and totals['cache_hit_requests'] == 1
    assert totals['cached_input_fraction'] == .25 and totals['unknown_cache_requests'] == 1
    assert [tuple(row) for row in store._x('SELECT * FROM event').fetchall()] == before


@pytest.mark.parametrize('missing', ['call_id', 'session_id', 'agent'])
def test_token_projection_cannot_join_two_missing_core_identities(services, missing):
    identity = {'request_id': 'incomplete', 'provider': 'openai', 'gateway': GATEWAY,
                'model': 'atria/atria-dawn', 'call_id': 'call', 'session_id': 'session',
                'api': 'responses', 'attempt': 1}
    if missing != 'agent':
        identity.pop(missing)
    role = None if missing == 'agent' else 'orchestrator'
    services.store.emit('agent.request', identity, agent=role)
    services.store.emit('agent.request_usage', {**identity, **usage()}, agent=role)
    result = request_estimate(services.store, gateway=GATEWAY)
    assert result['requests'] == 0 and result['unpriced_requests'] == 1
    assert result['estimate_usd'] is None
    assert result['cache_usage']['totals']['known_usage_requests'] == 0


def test_saved_configured_rates_are_estimates_and_do_not_reprice_nominal_other_model(services):
    store = services.store
    identity = emit_request(store, 'r1', model=ASTRA)
    store.emit('agent.usage', {**identity, **usage(), 'cost_basis': 'configured_token_rates',
               'pricing': {'input_usd_per_million': 10, 'output_usd_per_million': 50,
                           'cached_input_usd_per_million': 1}}, agent='orchestrator')
    emit_request(store, 'r2')
    result = request_estimate(store, gateway=GATEWAY)
    assert result['estimate_usd'] == pytest.approx(12.75 + .32)
    assert result['actual_usd'] is None
    assert result['bases'] == {'recorded_configured_token_rates': 1, 'administrator_nominal_quote': 1}
    assert request_estimate(store, gateway=GATEWAY, billed_requests=[bill()])['estimate_usd'] == pytest.approx(.123 + .32)


def test_conflicting_usage_identity_stays_unknown(services):
    identity = emit_request(services.store, 'conflict')
    services.store.emit('agent.request_usage', {**identity, **usage(out=999)}, agent='orchestrator')
    result = request_estimate(services.store, gateway=GATEWAY)
    assert result['requests'] == 0 and result['unpriced_requests'] == 1
    assert result['estimate_usd'] is None


def test_billing_api_has_request_projection_without_private_account_snapshot(services):
    from fastapi.testclient import TestClient

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.ui.app import create_app

    services.runtime.creds = OpenAICredentials('offline', model_id=ASTRA, base_url=GATEWAY)
    emit_request(services.store, 'r1')
    with TestClient(create_app(services)) as client:
        response = client.get('/api/billing')
    assert response.status_code == 200
    assert response.json()['request_estimate']['estimate_usd'] == pytest.approx(.32)
    assert json.loads(response.content)['status'] == 'unavailable'


@pytest.mark.parametrize('cached,token_hit,token_miss', [
    (250_000, '25.0%', '75.0%'), (0, '0.0%', '100.0%'), (None, 'Unknown', 'Unknown'),
])
def test_settings_saves_source_nominal_zero_and_shows_measured_cache_coverage(
        tmp_path, monkeypatch, cached, token_hit, token_miss):
    pw = pytest.importorskip('playwright.sync_api')
    from test_ui_browser import _chromium, _page, _Server

    from qumus2.agents.model_settings import ModelCatalog
    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.agents.openai_runtime import OpenAIRuntime
    from qumus2.app import build_services
    from qumus2.ui.app import create_app

    zero_model = 'olares-qumusphusis/qwen-3.8-27b'
    monkeypatch.setattr(ModelCatalog, 'list', lambda self: [
        {'id': ASTRA, 'input_price': 10, 'output_price': 50},
        {'id': zero_model, 'input_price': None, 'output_price': None,
         'administrator_quote': {'input': 0, 'output': 0}, 'quote_source_date': '2026-10-27'}])
    services = build_services(tmp_path / 'run.db', seed=1)
    services.runtime = services.driver.runtime = OpenAIRuntime(services.gateway, OpenAICredentials(
        'offline', base_url=GATEWAY, model_id=ASTRA, input_price=10, output_price=50))
    identity = emit_request(services.store, 'cached', cached=cached)
    services.store.emit('agent.request', {**identity, 'request_id': 'unknown-attempt'}, agent='orchestrator')
    with pw.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=_chromium())
        except Exception as exc:
            pytest.skip(f'no Chromium for Playwright: {exc}')
        try:
            with _Server(create_app(services, start_driver=False)) as url:
                context, page, errors = _page(browser)
                page.route('**/api/health', lambda route: route.fulfill(json={
                    'mode': 'sdk', 'provider': 'openai', 'model': ASTRA,
                    'model_status': {'state': 'ok'}}))
                try:
                    page.goto(url)
                    page.click('.ab[data-page="settings"]')
                    page.wait_for_function("document.querySelector('#billing-settings')?.textContent.includes('Input-token cache hit:')")
                    billing = page.inner_text('#billing-settings')
                    assert f'Input-token cache hit: {token_hit} · miss: {token_miss}' in billing
                    assert 'These complementary shares use the same' in billing
                    assert 'input tokens with valid input/cache measurements in this workspace' in billing
                    assert 'Request cache hit:' in billing
                    assert 'Unknown attempts are excluded from these rates, not counted as misses.' in billing
                    expected_coverage = '0.0%' if cached is None else '100.0%'
                    assert f'Coverage of known input tokens: {expected_coverage}' in billing
                    assert 'with unknown input counts: 1' in billing
                    assert 'administrator nominal quote' in page.inner_text('#billing-settings')
                    page.select_option('#model-choice', zero_model)
                    assert page.input_value('#model-input-rate') == '0'
                    assert page.input_value('#model-output-rate') == '0'
                    page.click('#model-apply')
                    page.wait_for_function("document.querySelector('.model-status')?.textContent.startsWith('Saved:')")
                    assert services.runtime.creds.model_id == zero_model
                    assert services.runtime.creds.input_price == services.runtime.creds.output_price == 0
                    assert services.runtime.creds.cached_input_price is None
                    assert not errors, errors
                finally:
                    context.close()
        finally:
            browser.close()


@pytest.mark.parametrize('extra', [
    {'scope': 'account'}, {'gateway': 'https://other.example/v1'},
    {'source': 'https://user:secret@physics.bytefuture.ai/dashboard/usage'},
    {'currency': 'EUR'}, {'verified_at': 'unknown'}, {'verified_at': '2026-10-29'},
    {'usd': True}, {'api_key': 'not-allowed'},
])
def test_private_request_bill_schema_rejects_unknown_scope_urls_dates_and_secrets(extra):
    from pydantic import ValidationError

    from qumus2.ui.app import BillingRequest

    with pytest.raises(ValidationError):
        BillingRequest.model_validate(bill(source='https://physics.bytefuture.ai/dashboard/usage', **extra)
                                      if 'source' not in extra else bill(**extra))


def test_billing_snapshot_request_evidence_has_real_api_caller_and_preserves_account_average(tmp_path):
    from fastapi.testclient import TestClient

    from qumus2.agents.openai_credentials import OpenAICredentials
    from qumus2.app import build_services
    from qumus2.ui.app import create_app

    services = build_services(tmp_path / 'run.db', seed=1)
    services.runtime.creds = OpenAICredentials('offline', model_id=ASTRA, base_url=GATEWAY)
    emit_request(services.store, 'r1', model=ASTRA)
    emit_request(services.store, 'nominal')
    snapshot = {'source_url': 'https://physics.bytefuture.ai/dashboard/usage',
                'verified_at': '2026-10-29T12:00:00Z', 'period': 'Past 30 days', 'scope': 'Account',
                'total_usd': 100, 'by_model': [{'model': ASTRA, 'usd': 100, 'tokens': 1_000_000,
                                              'requests': 100}],
                'by_request': [bill(source='https://physics.bytefuture.ai/dashboard/usage')]}
    (tmp_path / 'gateway-billing.json').write_text(json.dumps(snapshot), encoding="utf-8")
    with TestClient(create_app(services, start_driver=False)) as client:
        result = client.get('/api/billing').json()
    assert result['status'] == 'snapshot'
    assert result['estimate']['basis'] == 'gateway_billed_average'
    assert result['estimate']['usd'] == 110
    projected = result['request_estimate']
    assert projected['billed_requests'] == 1 and projected['identified_requests'] == 2
    assert projected['estimate_usd'] == pytest.approx(.123 + .32)
    assert projected['actual_usd'] is None  # only one of two requests has an actual bill
    assert result['snapshot']['total_usd'] == 100
    assert 'by_request' not in result['snapshot']


def test_conflicting_duplicate_bills_never_choose_the_last_price(services):
    emit_request(services.store, 'r1', model=ASTRA)
    projected = request_estimate(services.store, gateway=GATEWAY,
                                 billed_requests=[bill(), bill(usd=99)])
    assert projected['actual_usd'] is None and projected['billed_requests'] == 0
    assert projected['estimate_usd'] == 15


@pytest.mark.parametrize('kind', ['agent.request_usage', 'agent.usage'])
def test_other_role_with_same_request_id_never_supplies_usage_or_saved_rates(services, kind):
    identity = emit_request(services.store, 'r1', model=ASTRA)
    payload = {**identity, **usage(), 'cost_basis': 'configured_token_rates',
               'pricing': {'input_usd_per_million': 10, 'output_usd_per_million': 50,
                           'cached_input_usd_per_million': 1}}
    services.store.emit(kind, payload, agent='advisor')
    result = request_estimate(services.store, gateway=GATEWAY)
    if kind == 'agent.request_usage':
        assert result['estimate_usd'] is None and result['unpriced_requests'] == 1
    else:
        assert result['estimate_usd'] == 15
        assert result['bases'] == {'administrator_nominal_quote': 1}


@pytest.mark.parametrize('missing', ['call_id', 'session_id', 'model', 'provider', 'gateway', None])
def test_historical_saved_cache_rates_need_mandatory_identity_but_not_optional_api_attempt(services, missing):
    identity = emit_request(services.store, 'r1', model=ASTRA)
    identity.pop('api')
    identity.pop('attempt')
    if missing:
        identity.pop(missing)
    services.store.emit('agent.usage', {**identity, **usage(), 'cost_basis': 'configured_token_rates',
                        'pricing': {'input_usd_per_million': 10, 'output_usd_per_million': 50,
                                    'cached_input_usd_per_million': 1}}, agent='orchestrator')
    result = request_estimate(services.store, gateway=GATEWAY)
    assert result['estimate_usd'] == (15 if missing else 12.75)


def test_saved_rate_sources_group_identical_rates_over_date_range(services):
    store = services.store
    for n in range(220):
        identity = emit_request(store, f'r{n}', model=ASTRA)
        store.emit('agent.usage', {**identity, **usage(), 'cost_basis': 'configured_token_rates',
                   'pricing': {'input_usd_per_million': 10, 'output_usd_per_million': 50,
                               'cached_input_usd_per_million': 1}}, agent='orchestrator')
    result = request_estimate(store, gateway=GATEWAY)
    assert result['requests'] == 220
    assert len(result['price_sources']) == 1
    source = result['price_sources'][0]
    assert source['requests'] == 220 and source['first_at'] <= source['last_at']


@pytest.mark.parametrize('url', ['https://user:secret@example.com/v1', 'https://example.com/v1?key=secret'])
def test_evidence_credentialed_and_query_urls_are_rejected(url):
    for key in ['source', 'gateway']:
        evidence = rates(**{key: url})
        cost = request_cost(usage(), gateway=evidence['gateway'], model=ASTRA, rates=evidence)
        assert cost['basis'] != 'dated_route_rates'
        assert 'secret' not in str(cost['provenance'])


def test_cost_provenance_never_copies_unknown_or_secret_fields():
    cost = request_cost(usage(), gateway=GATEWAY, model=ASTRA,
                        rates=rates(api_key='private', arbitrary={'secret': 'private'}))
    assert cost['estimate_usd'] == 15
    assert 'private' not in str(cost['provenance'])
    billed = request_cost({}, gateway=GATEWAY, model=ASTRA, request_id='r1',
                          billed=bill(api_key='private'))
    assert billed['actual_usd'] == .123 and 'private' not in str(billed['provenance'])
