from __future__ import annotations

import json


def extract_json_object(raw_text: str) -> dict:
    """Parse one JSON object out of raw LLM text output.

    Tolerates markdown code fences and leading/trailing prose, since not
    every provider enforces strict JSON-only output the way Ollama's
    format="json" option does.
    """
    clean_text = (raw_text or "").strip()

    if clean_text.startswith("```"):
        clean_text = clean_text.replace("```json", "", 1)
        clean_text = clean_text.replace("```", "").strip()

    try:
        parsed = json.loads(clean_text)
    except json.JSONDecodeError:
        first_brace = clean_text.find("{")
        last_brace = clean_text.rfind("}")
        if first_brace < 0 or last_brace <= first_brace:
            raise ValueError("LLM returned no valid JSON object.")
        parsed = json.loads(clean_text[first_brace:last_brace + 1])

    if not isinstance(parsed, dict):
        raise ValueError("LLM output is not a JSON object.")

    return parsed
