"""QUMUS_OPENAI_MAX_OUTPUT_TOKENS overrides the agents.yaml output cap."""
from __future__ import annotations

import pytest

from qumus2.agents.openai_runtime import max_output_tokens


def test_default_comes_from_agents_yaml(monkeypatch):
    monkeypatch.delenv("QUMUS_OPENAI_MAX_OUTPUT_TOKENS", raising=False)
    assert max_output_tokens({"openai": {"max_output_tokens": 4096}}) == 4096
    assert max_output_tokens({}) == 4096


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("QUMUS_OPENAI_MAX_OUTPUT_TOKENS", "16384")
    assert max_output_tokens({"openai": {"max_output_tokens": 4096}}) == 16384


def test_env_must_be_positive(monkeypatch):
    monkeypatch.setenv("QUMUS_OPENAI_MAX_OUTPUT_TOKENS", "0")
    with pytest.raises(ValueError):
        max_output_tokens({})


def test_http_timeout_env(monkeypatch):
    from qumus2.agents.openai_runtime import http_timeout_s

    monkeypatch.delenv("QUMUS_OPENAI_HTTP_TIMEOUT_S", raising=False)
    assert http_timeout_s() == 120.0
    monkeypatch.setenv("QUMUS_OPENAI_HTTP_TIMEOUT_S", "600")
    assert http_timeout_s() == 600.0
    monkeypatch.setenv("QUMUS_OPENAI_HTTP_TIMEOUT_S", "-1")
    with pytest.raises(ValueError):
        http_timeout_s()
