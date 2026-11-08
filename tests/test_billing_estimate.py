"""Billing calibration is a display projection; recorded spending stays intact."""

from qumus2.ui.billing import calibrated_estimate

GATEWAY = 'https://physics.bytefuture.ai/v1'


def test_calibration_uses_each_models_bill_and_skips_unknowns(services):
    store = services.store
    snapshot = {'verified_at': '2026-10-28T03:38:47Z', 'by_model': [
        {'model': 'model/a', 'usd': 4, 'tokens': 1_000_000},
        {'model': 'model/b', 'usd': 10, 'tokens': 1_000_000},
        {'model': 'model/unknown', 'usd': 1}]}
    assert calibrated_estimate(store, snapshot, gateway=GATEWAY)['usd'] == 0
    store.emit('agent.request', {'request_id': 'no-usage'})
    estimate = calibrated_estimate(store, snapshot, gateway=GATEWAY)
    assert estimate['usd'] is None and estimate['unpriced_requests'] == 1
    for n, (model, inp, out) in enumerate([('model/a', 900, 100), ('model/b', 1000, 1000),
                            ('model/unknown', 100, 100), ('model/a', 0, 0),
                            ('model/a', None, 100)]):
        report = {'request_id': f'r{n}', 'provider': 'openai', 'model': model, 'gateway': GATEWAY,
                   'input_tokens': inp, 'output_tokens': out, 'cost_usd': 1,
                   'token_measurement': 'provider_usage_v1'}
        store.emit('agent.usage', report)
        store.emit('agent.request_usage', {**report, 'measurement': 'provider_usage_v1',
                   'usage_status': 'reported' if inp is not None else 'partial'})
    store.emit('agent.request_usage', {'model': 'model/a', 'input_tokens': 900, 'output_tokens': 100})
    store.emit('agent.turn_end', {'cost_usd': 5, 'cost_accounted_by_usage': True})
    before = store.spent_usd()
    result = calibrated_estimate(store, snapshot, gateway=GATEWAY)
    assert abs(result['usd'] - .024) < 1e-10
    assert result['requests'] == 3 and result['unpriced_requests'] == 3
    assert store.spent_usd() == before == 5
    assert calibrated_estimate(store, {'by_model': []}, gateway=GATEWAY) is None


def test_gateway_provenance_failed_retries_and_duplicate_response(services):
    store = services.store
    snapshot = {'verified_at': '2026-10-28T03:38:47Z', 'by_model': [
        {'model': 'same-name', 'usd': 4, 'tokens': 1_000_000}]}
    for rid in ['failed-attempt', 'successful-retry']:
        store.emit('agent.request', {'request_id': rid, 'gateway': GATEWAY})
    response = {'provider': 'openai', 'model': 'same-name',
                'input_tokens': 900, 'output_tokens': 100,
                'token_measurement': 'provider_usage_v1'}
    store.emit('agent.usage', {**response, 'request_id': 'successful-retry'})
    store.emit('agent.usage', {**response, 'request_id': 'successful-retry'})
    store.emit('agent.request_usage', {**response, 'request_id': 'successful-retry',
               'measurement': 'provider_usage_v1', 'usage_status': 'reported'})
    # Same model at another gateway, and old records with no gateway identity,
    # cannot be relabelled using the currently selected deployment.
    store.emit('agent.usage', {**response, 'gateway': 'https://other.example/v1'})
    store.emit('agent.usage', response)
    estimate = calibrated_estimate(store, snapshot, gateway=GATEWAY + '/')
    assert estimate['usd'] == .004
    assert estimate['requests'] == 1 and estimate['unpriced_requests'] == 3
    store.emit('agent.request', {'request_id': 'conflict', 'gateway': 'https://other.example/v1'})
    store.emit('agent.usage', {**response, 'request_id': 'conflict', 'gateway': GATEWAY})
    store.emit('agent.request_usage', {**response, 'request_id': 'conflict', 'gateway': GATEWAY,
               'measurement': 'provider_usage_v1', 'usage_status': 'reported'})
    assert calibrated_estimate(store, snapshot, gateway=GATEWAY)['unpriced_requests'] == 4


def test_sdk_default_zero_cannot_complete_partial_usage_and_failed_report_can_be_priced(services):
    store = services.store
    snapshot = {'verified_at': '2026-10-28T03:38:47Z', 'by_model': [
        {'model': 'm', 'usd': 4, 'tokens': 1_000_000}]}
    partial = {'request_id': 'partial', 'provider': 'openai', 'model': 'm', 'gateway': GATEWAY,
               'input_tokens': 1234, 'output_tokens': None}
    store.emit('agent.request_usage', {**partial, 'usage_status': 'partial', 'measurement': 'provider_usage_v1'})
    store.emit('agent.usage', {**partial, 'output_tokens': 0, 'token_measurement': 'provider_usage_v1'})
    result = calibrated_estimate(store, snapshot, gateway=GATEWAY)
    assert result['usd'] is None and result['unpriced_requests'] == 1
    # Terminal failure can report usage even when on_llm_end emits no cost event.
    for rid, inp, out in [('failed-with-usage', 100, 10), ('real-zero', 0, 0)]:
        store.emit('agent.request_usage', {**partial, 'request_id': rid,
                   'input_tokens': inp, 'output_tokens': out,
                   'usage_status': 'reported', 'measurement': 'provider_usage_v1'})
    result = calibrated_estimate(store, snapshot, gateway=GATEWAY)
    assert abs(result['usd'] - .00044) < 1e-12
    assert result['requests'] == 2 and result['unpriced_requests'] == 1


def test_legacy_turn_only_activity_is_unpriced_rather_than_no_calls(services):
    store = services.store
    snapshot = {'verified_at': '2026-10-28T03:38:47Z', 'by_model': [
        {'model': 'm', 'usd': 4, 'tokens': 1_000_000}]}
    store.emit('agent.turn_end', {'provider': 'claude', 'cost_usd': .12, 'num_turns': 3})
    store.emit('agent.turn_end', {'provider': 'openai', 'cost_usd': .25, 'cost_accounted_by_usage': True})
    estimate = calibrated_estimate(store, snapshot, gateway=GATEWAY)
    assert estimate['usd'] is None
    assert estimate['requests'] == estimate['unpriced_requests'] == 0
    assert estimate['unpriced_turns'] == 1
    assert store.spent_usd() == .12
