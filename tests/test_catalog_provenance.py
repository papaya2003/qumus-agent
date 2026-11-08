"""Registry metadata never grants account access or invents alias prices."""
import io
import json

from fastapi.testclient import TestClient

from qumus2.agents.model_settings import ModelCatalog
from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.agents.openai_runtime import OpenAIRuntime
from qumus2.ui.app import create_app


def test_registry_account_and_prices_are_distinct(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if request.full_url.endswith('/v1/models'):
                return io.StringIO(json.dumps({'data': [{'id': 'anthropic/claude-opus-5-5'}]}))
            return io.StringIO(json.dumps([{'name': 'anthropic/claude-opus-5-5',
                'input_price_per_million': 0, 'output_price_per_million': 0, 'is_free': False}]))

    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    catalog = ModelCatalog(OpenAICredentials('offline', base_url='https://physics.bytefuture.ai/v1',
                                            model_id='anthropic/claude-opus-5-5'))
    rows = {r['id']: r for r in catalog.list()}
    alias = rows['anthropic/claude-opus-5-5']
    assert alias['account_listed'] is True and alias['access_verified'] is False
    assert alias['context_window_tokens'] is None and alias['administrator_quote'] is None
    assert alias['conversational'] is None and alias['input_price'] is None
    qwen = rows['olares-qumusphusis/qwen-3.8-27b']
    assert qwen['registered'] and qwen['account_listed'] is False
    assert qwen['context_window_tokens'] == 131072
    assert qwen['administrator_quote'] == {'input': 0, 'output': 0}
    assert qwen['input_price'] is None
    astra = rows['openai/gpt-6-astra']
    assert astra['administrator_quote'] == {'input': 10, 'output': 50}
    assert astra['quote_account_applicability'] == 'unverified'
    catalog.list()
    assert len(calls) == 2


def test_registry_only_cannot_be_saved(services, monkeypatch):
    monkeypatch.setattr(ModelCatalog, 'list', lambda self: [{'id': 'registry/model',
        'registered': True, 'website_listed': False, 'account_listed': None}])
    services.runtime = OpenAIRuntime(services.gateway,
        OpenAICredentials('offline', model_id='current', input_price=1, output_price=2))
    client = TestClient(create_app(services, start_driver=False))
    response = client.post('/api/model', json={'model': 'registry/model',
                                             'input_price': 1, 'output_price': 2})
    assert response.status_code == 400
    assert not services.runtime._client
