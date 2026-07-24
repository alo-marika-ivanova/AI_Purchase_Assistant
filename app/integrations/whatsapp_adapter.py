from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

WHATSAPP_DRY_RUN = os.getenv("WHATSAPP_DRY_RUN", "true").lower() == "true"


def _normalize_whatsapp_to_number(value: str) -> str:
    """
    WhatsApp Cloud API accepts recipient phone numbers with country code.
    It normally works without '+', but your existing script used '+'.

    We keep '+' if present. We only remove spaces and separators.
    """
    value = value.strip()
    allowed = []

    for index, char in enumerate(value):
        if char.isdigit():
            allowed.append(char)
        elif char == "+" and index == 0:
            allowed.append(char)

    return "".join(allowed)


def whatsapp_configured() -> bool:
    return bool(os.getenv("WHATSAPP_ACCESS_TOKEN")) and bool(
        os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    )


def _parse_retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None

    try:
        return max(0, int(float(value)))
    except ValueError:
        return None


def _classify_whatsapp_response(
    status_code: int,
    retry_after_header: str | None,
) -> tuple[str, int | None]:
    """Classify a received HTTP response into a delivery outcome.

    429 (rate limited) and 5xx (server error) are retryable. Other 4xx
    responses (bad auth, invalid recipient, rejected template, malformed
    request) are treated as permanent, per WhatsApp Cloud API conventions.
    """
    if 200 <= status_code < 300:
        return "sent", None

    if status_code == 429 or status_code >= 500:
        return "transient", _parse_retry_after_seconds(retry_after_header)

    return "permanent", None


def send_whatsapp_text(
    to_number: str,
    body: str,
) -> dict[str, Any]:
    """
    Send a WhatsApp text message through Meta WhatsApp Cloud API.

    Returns a dict that always includes ``delivery_outcome``, one of:
    - "sent": the provider accepted the message;
    - "dry_run": WHATSAPP_DRY_RUN was on, nothing was actually sent;
    - "transient": a retryable failure (rate limit, server error, or a
      connection that never opened, so nothing was sent);
    - "permanent": the provider rejected the request outright;
    - "unknown": a timeout or connection loss made it unclear whether the
      provider received the request. Callers must not blindly retry this
      case, since a retry could create a duplicate send.
    """

    clean_to_number = _normalize_whatsapp_to_number(to_number)

    if not clean_to_number:
        return {
            "success": False,
            "delivery_outcome": "permanent",
            "provider_message_id": None,
            "error": "Recipient WhatsApp number is missing.",
        }

    if WHATSAPP_DRY_RUN:
        print("WHATSAPP DRY RUN")
        print("TO:", clean_to_number)
        print("BODY:", body)

        return {
            "success": True,
            "delivery_outcome": "dry_run",
            "provider_message_id": "dry-run-whatsapp",
            "error": None,
        }

    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    api_version = os.getenv("WHATSAPP_API_VERSION", "v18.0")

    if not access_token:
        return {
            "success": False,
            "delivery_outcome": "permanent",
            "provider_message_id": None,
            "error": "WHATSAPP_ACCESS_TOKEN is missing.",
        }

    if not phone_number_id:
        return {
            "success": False,
            "delivery_outcome": "permanent",
            "provider_message_id": None,
            "error": "WHATSAPP_PHONE_NUMBER_ID is missing.",
        }

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": clean_to_number,
        "type": "text",
        "text": {
            "body": body,
        },
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30,
        )
    except requests.exceptions.ConnectTimeout as exc:
        # The TCP connection itself never opened, so nothing was sent.
        # Safe to retry.
        return {
            "success": False,
            "delivery_outcome": "transient",
            "provider_message_id": None,
            "error": str(exc),
        }
    except requests.RequestException as exc:
        # Read timeout, connection reset mid-request, or any other failure
        # after a connection may have been established. It is not possible
        # to tell whether Meta received the request, so this must not be
        # auto-retried.
        return {
            "success": False,
            "delivery_outcome": "unknown",
            "provider_message_id": None,
            "error": str(exc),
        }

    try:
        response_json = response.json()
    except ValueError:
        response_json = {
            "raw_text": response.text,
        }

    outcome, retry_after_seconds = _classify_whatsapp_response(
        status_code=response.status_code,
        retry_after_header=response.headers.get("Retry-After"),
    )

    if outcome == "sent":
        provider_message_id = None

        try:
            provider_message_id = response_json["messages"][0]["id"]
        except Exception:
            provider_message_id = None

        return {
            "success": True,
            "delivery_outcome": "sent",
            "provider_message_id": provider_message_id,
            "status_code": response.status_code,
            "response": response_json,
        }

    return {
        "success": False,
        "delivery_outcome": outcome,
        "provider_message_id": None,
        "retry_after_seconds": retry_after_seconds,
        "status_code": response.status_code,
        "error": response.text,
        "response": response_json,
    }
