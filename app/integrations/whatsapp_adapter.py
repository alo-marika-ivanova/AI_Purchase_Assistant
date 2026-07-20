from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


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


def send_whatsapp_text(
    to_number: str,
    body: str,
) -> dict[str, Any]:
    """
    Send a WhatsApp text message through Meta WhatsApp Cloud API.
    """

    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    api_version = os.getenv("WHATSAPP_API_VERSION", "v18.0")

    if not access_token:
        return {
            "success": False,
            "error": "WHATSAPP_ACCESS_TOKEN is missing.",
        }

    if not phone_number_id:
        return {
            "success": False,
            "error": "WHATSAPP_PHONE_NUMBER_ID is missing.",
        }

    clean_to_number = _normalize_whatsapp_to_number(to_number)

    if not clean_to_number:
        return {
            "success": False,
            "error": "Recipient WhatsApp number is missing.",
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

        try:
            response_json = response.json()
        except ValueError:
            response_json = {
                "raw_text": response.text,
            }

        if 200 <= response.status_code < 300:
            provider_message_id = None

            try:
                provider_message_id = response_json["messages"][0]["id"]
            except Exception:
                provider_message_id = None

            return {
                "success": True,
                "provider_message_id": provider_message_id,
                "status_code": response.status_code,
                "response": response_json,
            }

        return {
            "success": False,
            "status_code": response.status_code,
            "error": response.text,
            "response": response_json,
        }

    except requests.RequestException as exc:
        return {
            "success": False,
            "error": str(exc),
        }