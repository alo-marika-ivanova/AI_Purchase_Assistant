from __future__ import annotations

import os
from typing import Protocol

from dotenv import load_dotenv


load_dotenv()


class LLMProvider(Protocol):
    """Provider-independent interface for generating text from a prompt.

    Callers send a plain-text prompt and get back the raw model output as
    text. JSON parsing, validation, and all business/safety logic stay in
    app/llm/*.py and do not depend on which provider is active.
    """

    name: str
    model: str

    def generate(self, prompt: str, *, timeout_seconds: int) -> str:
        ...


def get_llm_provider() -> LLMProvider:
    """Return the LLM provider configured via the LLM_PROVIDER env var.

    Defaults to "claude". A fresh provider instance is constructed on every
    call so env var overrides (including in tests) take effect immediately.
    """
    provider_name = os.getenv("LLM_PROVIDER", "claude").strip().lower()

    if provider_name == "claude":
        from app.llm.providers.claude_provider import ClaudeProvider

        return ClaudeProvider()

    if provider_name == "ollama":
        from app.llm.providers.ollama_provider import OllamaProvider

        return OllamaProvider()

    raise ValueError(f"Unknown LLM_PROVIDER: {provider_name!r}")
