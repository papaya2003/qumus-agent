"""Phoenix / OpenTelemetry setup for the agents.

Why this exists: `phoenix.otel.register()` defaults to a **SimpleSpanProcessor**,
which exports every span synchronously on `span.end()` and BLOCKS the calling
thread until the OTLP gRPC call to the collector returns. If the local Phoenix
collector is slow or backed up (which it gets, over a long session), an agent
run freezes mid-step with 0 CPU and no new spans — indistinguishable from a hung
LLM call. Using a BatchSpanProcessor makes export async and non-blocking (it
drops spans under backpressure instead of stalling the run).

Set QUMUS_DISABLE_TRACING=1 to skip instrumentation entirely.
"""

from __future__ import annotations

import os

_DONE = False


def _truthy(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


def setup_tracing() -> None:
    """Register Phoenix tracing with a non-blocking BatchSpanProcessor. Idempotent."""
    global _DONE
    if _DONE:
        return
    _DONE = True

    if _truthy(os.getenv("QUMUS_DISABLE_TRACING", "")):
        return

    try:
        from opentelemetry import trace
        from openinference.instrumentation.smolagents import SmolagentsInstrumentor
        from phoenix.otel import register

        # Only register if nothing else already set a real tracer provider.
        if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            return

        try:
            register(batch=True)  # BatchSpanProcessor: async, drops under backpressure
        except TypeError:
            # older phoenix.otel without the `batch` kwarg
            register()
        SmolagentsInstrumentor().instrument()
    except Exception:
        # phoenix not installed / already registered / collector unreachable — never fatal
        pass
