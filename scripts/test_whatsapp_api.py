from __future__ import annotations

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv


def normalize_phone(value: str) -> str:
    return "".join(
        char for char in value if char.isdigit()
    )


def masked(value: str, visible: int = 4) -> str:
    if not value:
        return "<missing>"

    if len(value) <= visible * 2:
        return "*" * len(value)

    return (
        value[:visible]
        + "..."
        + value[-visible:]
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test Meta WhatsApp Cloud API directly."
    )

    parser.add_argument(
        "--to",
        required=True,
        help=(
            "Recipient number with country code, "
            "for example 420777123456"
        ),
    )

    parser.add_argument(
        "--mode",
        choices=("template", "text"),
        default="template",
        help=(
            "template sends Meta's hello_world template; "
            "text sends a free-form test message"
        ),
    )

    parser.add_argument(
        "--text",
        default="Direct WhatsApp API test from Python.",
    )

    args = parser.parse_args()

    # For this diagnostic script, explicitly reload the current .env.
    load_dotenv(override=True)

    token = (
        os.getenv("WHATSAPP_ACCESS_TOKEN") or ""
    ).strip()

    phone_number_id = (
        os.getenv("WHATSAPP_PHONE_NUMBER_ID") or ""
    ).strip()

    api_version = (
        os.getenv("WHATSAPP_API_VERSION", "v25.0")
        or "v25.0"
    ).strip()

    recipient = normalize_phone(args.to)

    print("CONFIGURATION")
    print(f"API version: {api_version}")
    print(
        "Phone number ID: "
        f"{masked(phone_number_id)}"
    )
    print(
        "Access token: "
        f"{masked(token)} "
        f"(length={len(token)})"
    )
    print(f"Recipient: {recipient}")

    if not token:
        print("ERROR: WHATSAPP_ACCESS_TOKEN is missing.")
        return 2

    if not phone_number_id:
        print(
            "ERROR: WHATSAPP_PHONE_NUMBER_ID is missing."
        )
        return 2

    if not recipient:
        print("ERROR: recipient number is empty.")
        return 2

    url = (
        f"https://graph.facebook.com/"
        f"{api_version}/{phone_number_id}/messages"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    if args.mode == "template":
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": "hello_world",
                "language": {
                    "code": "en_US",
                },
            },
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": args.text,
            },
        }

    print("\nREQUEST")
    print(f"POST {url}")
    print(json.dumps(payload, indent=2))

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=30,
    )

    print("\nRESPONSE")
    print(f"HTTP {response.status_code}")

    try:
        print(
            json.dumps(
                response.json(),
                indent=2,
                ensure_ascii=False,
            )
        )
    except ValueError:
        print(response.text)

    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())
