from __future__ import annotations

import json
import os
import urllib.request

from dotenv import load_dotenv


load_dotenv()


class OllamaProvider:
    """Local Ollama provider. Preserves the exact request shape used by the
    classifier and communication writer before the provider abstraction was
    introduced."""

    name = "ollama"

    def __init__(self) -> None:
        self.url = os.getenv(
            "OLLAMA_URL",
            "http://localhost:11434/api/generate",
        )
        self.model = os.getenv("OLLAMA_MODEL", "llama3.1")

    def generate(
        self,
        prompt: str,
        *,
        timeout_seconds: int,
        temperature: float | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
        if temperature is not None:
            payload["options"] = {"temperature": temperature}

        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(
            request,
            timeout=timeout_seconds,
        ) as response:
            response_data = json.loads(response.read().decode("utf-8"))

        return response_data.get("response", "")
