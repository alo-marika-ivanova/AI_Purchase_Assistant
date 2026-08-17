from __future__ import annotations

import pytest

from app.llm.provider import get_llm_provider
from app.llm.providers.claude_provider import ClaudeProvider
from app.llm.providers.ollama_provider import OllamaProvider


def test_defaults_to_claude_when_llm_provider_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    provider = get_llm_provider()

    assert isinstance(provider, ClaudeProvider)
    assert provider.name == "claude"


def test_selects_ollama_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1")

    provider = get_llm_provider()

    assert isinstance(provider, OllamaProvider)
    assert provider.name == "ollama"
    assert provider.model == "llama3.1"


def test_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "bogus")

    with pytest.raises(ValueError):
        get_llm_provider()


def test_claude_provider_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        get_llm_provider()


def test_claude_provider_default_max_tokens_is_large_enough_for_multi_item_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-item order reply's JSON response carries one item_offers
    entry per item on top of the usual fields; 1024 tokens was enough for a
    single-item reply but silently truncated a several-item order's
    response mid-JSON, which then failed to parse and was reported as an
    unclassifiable message with no price extracted at all."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_MAX_TOKENS", raising=False)

    provider = ClaudeProvider()

    assert provider.max_tokens >= 4096


def test_claude_provider_max_tokens_still_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "2048")

    provider = ClaudeProvider()

    assert provider.max_tokens == 2048
