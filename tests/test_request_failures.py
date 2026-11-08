"""Attempt failures are observable without altering retries or retaining secrets."""

import asyncio
from types import SimpleNamespace

import pytest

from qumus2.agents.request_metrics import RequestMetrics


class Store:
    def __init__(self):
        self.events = []

    def emit(self, kind, payload, **kwargs):
        self.events.append((kind, payload, kwargs))


def observer():
    store = Store()
    metrics = RequestMetrics(
        store,
        lambda: {"agent": "hands", "call_id": "call", "session_id": "session"},
        track_tokens=True,
    )
    return store, metrics


@pytest.mark.parametrize("client_module", ["httpx", "httpx2"])
@pytest.mark.parametrize(
    "status,category",
    [
        (400, "invalid_request"),
        (401, "authentication"),
        (403, "access_denied"),
        (408, "timeout"),
        (409, "conflict"),
        (413, "request_too_large"),
        (429, "rate_limit"),
        (503, "server_error"),
    ],
)
def test_http_failure_is_recorded_before_retry_and_preserves_response(
    client_module, status, category
):
    http = pytest.importorskip(client_module)
    store, metrics = observer()
    sent = []

    async def respond(request):
        sent.append(request)
        if len(sent) == 1:
            return http.Response(
                status,
                headers={"x-request-id": "upstream-id", "Retry-After": "1"},
                json={"error": {"message": "secret key and private prompt"}},
            )
        return http.Response(200, json={"result": "ok"})

    async def go():
        client = metrics.observe_client(
            http.AsyncClient(
                transport=http.MockTransport(respond),
                event_hooks={"request": [metrics], "response": [metrics.on_response]},
            )
        )
        async with client:
            first = await client.post(
                "https://example.invalid/v1/responses",
                json={"model": "m", "input": "private prompt"},
            )
            assert first.status_code == status
            assert first.json()["error"]["message"] == "secret key and private prompt"
            assert first.headers["Retry-After"] == "1"
            failure = [e for e in store.events if e[0] == "agent.request_failure"]
            assert len(failure) == 1  # observable before another attempt starts
            second = await client.post(
                "https://example.invalid/v1/responses",
                json={"model": "m"},
                headers={"x-stainless-retry-count": "1"},
            )
            assert second.status_code == 200
            metrics.record_usage({"input_tokens": 12, "output_tokens": 3})

    asyncio.run(go())
    requests = [e[1] for e in store.events if e[0] == "agent.request"]
    failure = [e[1] for e in store.events if e[0] == "agent.request_failure"][0]
    usage = [e[1] for e in store.events if e[0] == "agent.request_usage"][0]
    assert failure["request_id"] == requests[0]["request_id"] != usage["request_id"]
    assert failure["http_status"] == status and failure["failure_kind"] == category
    assert failure["provider_request_id"] == "upstream-id"
    assert "secret key" not in str(store.events) and "private prompt" not in str(store.events)
    assert requests[0]['gateway'] == usage['gateway'] == 'https://example.invalid/v1'
    assert '/?key=' not in str(store.events)
    assert all("cost_usd" not in e[1] for e in store.events)


@pytest.mark.parametrize("client_module", ["httpx", "httpx2"])
@pytest.mark.parametrize(
    "exception_name,category", [("ReadTimeout", "timeout"), ("ConnectError", "connection_error")]
)
def test_network_exception_retains_identity_and_is_rethrown_unchanged(
    client_module, exception_name, category
):
    http = pytest.importorskip(client_module)
    store, metrics = observer()
    original = getattr(http, exception_name)(
        "secret transport error https://example.invalid/?key=secret"
    )

    async def respond(request):
        raise original

    async def go():
        client = metrics.observe_client(
            http.AsyncClient(
                transport=http.MockTransport(respond),
                event_hooks={"request": [metrics], "response": [metrics.on_response]},
            )
        )
        async with client:
            with pytest.raises(type(original)) as raised:
                await client.post(
                    "https://example.invalid/v1/chat/completions", json={"model": "m"}
                )
            assert raised.value is original
            metrics.record_model_failure(
                original
            )  # model wrapper must not duplicate transport evidence

    asyncio.run(go())
    failures = [e for e in store.events if e[0] == "agent.request_failure"]
    assert len(failures) == 1
    assert failures[0][1]["failure_kind"] == category
    assert failures[0][1]["exception_type"] == exception_name
    assert failures[0][2]["agent"] == "hands"
    assert "secret" not in str(store.events)


def test_cancelled_attempt_and_native_terminal_failure_preserve_usage_and_unknowns():
    http = pytest.importorskip("httpx")
    store, metrics = observer()

    async def go():
        await metrics(
            http.Request("POST", "https://example.invalid/v1/responses", json={"model": "m"})
        )
        metrics.record_model_failure(asyncio.CancelledError("private"))
        await metrics(
            http.Request(
                "POST",
                "https://example.invalid/v1/responses",
                json={"model": "m"},
                headers={"x-stainless-retry-count": "1"},
            )
        )
        metrics.record_usage({"input_tokens": 10, "output_tokens": 2})
        metrics.record_model_failure(
            SimpleNamespace(
                type="response.incomplete",
                response=SimpleNamespace(
                    incomplete_details=SimpleNamespace(reason="max_output_tokens")
                ),
            )
        )

    asyncio.run(go())
    failures = [e[1] for e in store.events if e[0] == "agent.request_failure"]
    assert [f["failure_kind"] for f in failures] == ["cancelled", "response_incomplete"]
    assert failures[1]["provider_error_code"] == "max_output_tokens"
    assert len([e for e in store.events if e[0] == "agent.request_usage"]) == 1
    assert "private" not in str(store.events)


def test_non_model_requests_and_untrusted_header_codes_do_not_leak():
    http = pytest.importorskip("httpx")
    store, metrics = observer()

    async def go():
        other = http.Request("GET", "https://example.invalid/v1/models")
        await metrics(other)
        await metrics.on_response(http.Response(500, request=other))
        assert not store.events
        request = http.Request("POST", "https://example.invalid/v1/responses", json={"model": "m"})
        await metrics(request)
        await metrics.on_response(
            http.Response(500, request=request, headers={"x-request-id": "Bearer secret-value"})
        )

    asyncio.run(go())
    assert store.events[-1][1]["provider_request_id"] is None
    assert "secret-value" not in str(store.events)


@pytest.mark.parametrize("client_module", ["httpx", "httpx2"])
@pytest.mark.parametrize("first_failure", ["timeout", "server_error"])
def test_openai_sdk_still_retries_once_and_assigns_usage_only_to_recovery(
    client_module, first_failure
):
    http = pytest.importorskip(client_module)
    openai = pytest.importorskip("openai")
    store, metrics = observer()
    sent = []

    async def respond(request):
        sent.append(request)
        if len(sent) == 1:
            if first_failure == "timeout":
                raise http.ReadTimeout("sensitive transport message", request=request)
            return http.Response(
                503, headers={"retry-after-ms": "1"}, json={"error": {"message": "private"}}
            )
        return http.Response(
            200,
            json={
                "id": "response-1",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "m",
                "output": [],
                "usage": {"input_tokens": 123, "output_tokens": 4, "total_tokens": 127},
            },
        )

    async def go():
        http_client = metrics.observe_client(
            http.AsyncClient(
                transport=http.MockTransport(respond),
                event_hooks={"request": [metrics], "response": [metrics.on_response]},
            )
        )
        async with openai.AsyncOpenAI(
            api_key="offline-only",
            base_url="https://example.invalid/v1",
            max_retries=2,
            http_client=http_client,
        ) as client:
            result = await client.responses.create(model="m", input="private prompt")
            metrics.record_usage(result.usage.model_dump(exclude_unset=True))

    asyncio.run(go())
    requests = [e[1] for e in store.events if e[0] == "agent.request"]
    failures = [e[1] for e in store.events if e[0] == "agent.request_failure"]
    usage = [e[1] for e in store.events if e[0] == "agent.request_usage"]
    assert len(sent) == len(requests) == 2
    assert [r["attempt"] for r in requests] == [1, 2]
    assert len(failures) == len(usage) == 1
    assert failures[0]["failure_kind"] == first_failure
    assert failures[0]["request_id"] == requests[0]["request_id"]
    assert usage[0]["request_id"] == requests[1]["request_id"]
    assert usage[0]["total_tokens"] == 127
    assert "private" not in str(store.events) and "sensitive" not in str(store.events)
