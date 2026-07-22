import os
import sys

from anthropic import Anthropic
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

    if not api_key:
        print("ANTHROPIC_API_KEY is missing from .env")
        return 1

    try:
        client = Anthropic(
            api_key=api_key,
            timeout=30.0,
            max_retries=2,
        )

        response = client.messages.create(
            model=model,
            max_tokens=80,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Reply with exactly: Claude API connection successful"
                    ),
                }
            ],
        )

        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )

        print("Model:", response.model)
        print("Response:", text)
        print("Input tokens:", response.usage.input_tokens)
        print("Output tokens:", response.usage.output_tokens)
        print("Request ID:", getattr(response, "_request_id", None))
        return 0

    except Exception as exc:
        print(f"Claude API test failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())