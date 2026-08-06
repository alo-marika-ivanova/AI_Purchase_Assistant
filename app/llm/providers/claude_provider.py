from __future__ import annotations

import os

from anthropic import Anthropic
from dotenv import load_dotenv


load_dotenv()


class ClaudeProvider:
    """Hosted Anthropic Claude provider."""

    name = "claude"

    def __init__(self) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to .env or switch "
                "LLM_PROVIDER back to 'ollama'."
            )

        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
        # A multi-item order's classification response includes one
        # item_offers entry per item on top of the usual fields; 1024 was
        # enough for a single-item reply but silently truncated mid-JSON for
        # a several-item order, which then fails to parse and is reported as
        # an unclassifiable message with no price extracted at all.
        self.max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4096"))
        self._client = Anthropic(api_key=api_key, max_retries=2)

    def generate(
        self,
        prompt: str,
        *,
        timeout_seconds: int,
        temperature: float | None = None,
    ) -> str:
        # `temperature` is accepted for interface parity with other providers
        # but not forwarded: claude-sonnet-5 rejects it with a 400
        # invalid_request_error ("temperature is deprecated for this model").
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            timeout=float(timeout_seconds),
            messages=[{"role": "user", "content": prompt}],
        )

        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
