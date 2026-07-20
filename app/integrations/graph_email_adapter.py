from __future__ import annotations

import os
import re

import msal
import requests
from dotenv import load_dotenv


load_dotenv()

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


def strip_html(value: str | None) -> str:
    """
    Simple HTML stripper for email body content.
    """
    if not value:
        return ""

    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def get_graph_access_token() -> str:
    """
    Get Microsoft Graph application token using client credentials.
    """
    client_id = os.getenv("MS_GRAPH_CLIENT_ID")
    client_secret = os.getenv("MS_GRAPH_CLIENT_SECRET")
    tenant_id = os.getenv("MS_GRAPH_TENANT_ID")

    if not client_id or not client_secret or not tenant_id:
        raise ValueError("Missing Microsoft Graph credentials in .env.")

    authority = f"https://login.microsoftonline.com/{tenant_id}"

    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )

    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )

    if "access_token" not in result:
        raise RuntimeError(f"Could not get Microsoft Graph token: {result}")

    return result["access_token"]


def _get_header_value(
    internet_message_headers: list[dict],
    header_name: str,
) -> str | None:
    """
    Extract one RFC email header value from Microsoft Graph internetMessageHeaders.
    Header names are case-insensitive.
    """
    wanted = header_name.lower()

    for header in internet_message_headers or []:
        name = (header.get("name") or "").lower()
        value = header.get("value")

        if name == wanted and value:
            return value

    return None


def list_recent_inbox_messages(user_email: str, top: int = 25) -> list[dict]:
    """
    Read recent mailbox messages for the buyer.

    This does not modify or delete emails.

    Returned threading fields:
    - internet_message_id: Message-ID of the inbound email.
    - in_reply_to: RFC In-Reply-To header, if available.
    - references: RFC References header, if available.
    - graph_conversation_id: Microsoft Graph conversation ID.
    """
    token = get_graph_access_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    params = {
        "$top": str(top),
        "$orderby": "receivedDateTime desc",
        "$select": (
            "id,subject,from,receivedDateTime,body,bodyPreview,"
            "internetMessageId,conversationId,internetMessageHeaders"
        ),
    }

    response = requests.get(
        f"{GRAPH_BASE_URL}/users/{user_email}/messages",
        headers=headers,
        params=params,
        timeout=30,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Graph message read failed: {response.status_code} {response.text}"
        )

    raw_messages = response.json().get("value", [])

    messages: list[dict] = []

    for msg in raw_messages:
        sender = (
            msg.get("from", {})
            .get("emailAddress", {})
        )

        body = msg.get("body") or {}
        body_content = body.get("content") or msg.get("bodyPreview") or ""

        internet_message_headers = msg.get("internetMessageHeaders") or []

        in_reply_to = _get_header_value(
            internet_message_headers=internet_message_headers,
            header_name="In-Reply-To",
        )

        references = _get_header_value(
            internet_message_headers=internet_message_headers,
            header_name="References",
        )

        messages.append(
            {
                "graph_message_id": msg.get("id"),
                "internet_message_id": msg.get("internetMessageId"),
                "in_reply_to": in_reply_to,
                "references": references,
                "graph_conversation_id": msg.get("conversationId"),
                "subject": msg.get("subject") or "",
                "sender_name": sender.get("name") or "",
                "sender_email": (sender.get("address") or "").lower(),
                "received_at": msg.get("receivedDateTime") or "",
                "body": strip_html(body_content),
                "body_preview": msg.get("bodyPreview") or "",
            }
        )

    return messages