"""Optional non-streamed upstream transport, keeping the SDK's event interface.

Some gateways corrupt streamed parallel tool calls. Read one complete model
response and deliver its distinct output items to the SDK unchanged. No JSON
repair, guessed tool identities, second model call or local HTTP proxy.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from agents import Model
from agents.models.interface import ModelProvider
from agents.usage import _attach_raw_usage_snapshot
from openai.types.responses import Response, ResponseCompletedEvent


class BufferedModel(Model):
    def __init__(self, model: Model, name: str) -> None:
        self.inner, self.name = model, name

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        return await self.inner.get_response(*args, **kwargs)

    async def stream_response(self, *args: Any, **kwargs: Any):
        result = await self.inner.get_response(*args, **kwargs)
        usage = result.usage
        response = Response(
            id=result.response_id or f"buffered-{uuid4().hex}", created_at=0,
            model=self.name, object="response", status="completed", output=result.output,
            parallel_tool_calls=False, tool_choice="auto", tools=[],
            usage={"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                   "total_tokens": usage.total_tokens or usage.input_tokens + usage.output_tokens,
                   "input_tokens_details": {"cached_tokens": usage.input_tokens_details.cached_tokens,
                                            "cache_write_tokens": getattr(usage.input_tokens_details,
                                                                          "cache_write_tokens", 0) or 0},
                   "output_tokens_details": {"reasoning_tokens": usage.output_tokens_details.reasoning_tokens}},
        )
        _attach_raw_usage_snapshot(response, result.raw_usage)
        yield ResponseCompletedEvent(type="response.completed", sequence_number=0, response=response)

    async def close(self) -> None:
        await self.inner.close()


class BufferedProvider(ModelProvider):
    def __init__(self, provider: ModelProvider) -> None:
        self.inner = provider

    def get_model(self, model_name: str | None) -> Model:
        return BufferedModel(self.inner.get_model(model_name), model_name or "configured-model")


class UsageModel(Model):
    """Observe raw usage before buffering or SDK terminal-error handling."""

    def __init__(self, model: Model, callback: Any, failure_callback: Any = None) -> None:
        self.inner, self.callback, self.failure_callback = model, callback, failure_callback

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        try:
            result = await self.inner.get_response(*args, **kwargs)
            self.callback(result.raw_usage)
            return result
        except (Exception, asyncio.CancelledError) as exc:
            if self.failure_callback:
                self.failure_callback(exc)
            raise

    async def stream_response(self, *args: Any, **kwargs: Any):
        stream = self.inner.stream_response(*args, **kwargs)
        try:
            async for event in stream:
                if getattr(event, "type", None) in ("response.incomplete", "response.failed"):
                    # These native Responses events never reach on_llm_end: the
                    # runner raises first. They can still contain real token usage.
                    usage = getattr(getattr(event, "response", None), "usage", None)
                    self.callback(usage.model_dump(exclude_unset=True) if usage is not None else None)
                    if self.failure_callback:
                        self.failure_callback(event)
                yield event
        except (Exception, asyncio.CancelledError) as exc:
            if self.failure_callback:
                self.failure_callback(exc)
            raise
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    async def close(self) -> None:
        await self.inner.close()


class UsageProvider(ModelProvider):
    def __init__(self, provider: ModelProvider, callback: Any, failure_callback: Any = None) -> None:
        self.inner, self.callback, self.failure_callback = provider, callback, failure_callback

    def get_model(self, model_name: str | None) -> Model:
        return UsageModel(self.inner.get_model(model_name), self.callback, self.failure_callback)
