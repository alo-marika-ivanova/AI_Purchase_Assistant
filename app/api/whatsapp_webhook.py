from __future__ import annotations

import hashlib
import hmac
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.services.whatsapp_transport_service import (
    persist_inbound_whatsapp_event,
    process_inbound_whatsapp_message,
    process_pending_whatsapp_events,
)

router = APIRouter()


def _whatsapp_webhook_verify_token() -> str:
    """Return the configured GET-verification token.

    WHATSAPP_WEBHOOK_VERIFY_TOKEN is the current name; WHATSAPP_VERIFY_TOKEN
    is accepted as a fallback so an already-configured Meta app keeps
    working without a forced rename.
    """
    return (
        os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "").strip()
        or os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    )


def _inline_processing_enabled() -> bool:
    """Whether the webhook should process events synchronously (legacy
    behavior) instead of persisting and returning immediately.

    Defaults to true so this rollout does not change behavior until
    explicitly turned off in .env once the staged/async path has been
    validated end to end.
    """
    return os.getenv("WHATSAPP_INLINE_PROCESSING", "true").lower() == "true"


def _verify_meta_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Validate the X-Hub-Signature-256 header using META_APP_SECRET.

    Returns False (rejected) whenever META_APP_SECRET is not configured, the
    header is missing or malformed, or the signature does not match.
    Comparison is constant-time to avoid a timing side channel.
    """
    app_secret = os.getenv("META_APP_SECRET", "").strip()

    if not app_secret:
        return False

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_signature = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    provided_signature = signature_header[len("sha256=") :]

    return hmac.compare_digest(expected_signature, provided_signature)


@router.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(request: Request):
    """
    Meta webhook verification endpoint.
    """

    params = request.query_params

    mode = params.get("hub.mode")
    verify_token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    expected_token = _whatsapp_webhook_verify_token()

    if (
        mode == "subscribe"
        and expected_token
        and verify_token == expected_token
        and challenge
    ):
        return PlainTextResponse(challenge)

    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook/whatsapp")
async def receive_whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Receive WhatsApp webhook events from Meta.

    Every request's signature is validated before the body is parsed as
    JSON. Once validated, this either processes messages synchronously
    (WHATSAPP_INLINE_PROCESSING=true, the original behavior, kept available
    during rollout) or persists each message and returns immediately,
    leaving classification and negotiation processing to the transport
    worker's poll cycle and to a background task scheduled right after this
    response is sent.
    """

    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")

    if not _verify_meta_signature(raw_body, signature_header):
        raise HTTPException(status_code=403, detail="Invalid signature.")

    payload = await request.json()

    results = []
    inline_processing = _inline_processing_enabled()

    try:
        entries = payload.get("entry", [])

        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})

                messages = value.get("messages", [])

                for msg in messages:
                    wa_message_id = msg.get("id")
                    sender_phone = msg.get("from")
                    timestamp = msg.get("timestamp")

                    msg_type = msg.get("type")

                    if msg_type != "text":
                        results.append(
                            {
                                "imported": False,
                                "reason": f"Ignored non-text WhatsApp message type: {msg_type}",
                            }
                        )
                        continue

                    text = msg.get("text", {}).get("body", "")

                    if inline_processing:
                        result = process_inbound_whatsapp_message(
                            wa_message_id=wa_message_id,
                            sender_phone=sender_phone,
                            text=text,
                            received_at=timestamp,
                        )
                    else:
                        result = persist_inbound_whatsapp_event(
                            wa_message_id=wa_message_id,
                            sender_phone=sender_phone,
                            body=text,
                            received_at=timestamp,
                        )

                    results.append(result)

    except Exception as exc:
        # Return 200 so Meta does not retry forever while you debug.
        # The error is included in response and should also appear in server logs.
        print(f"WhatsApp webhook processing error: {exc}")
        results.append(
            {
                "imported": False,
                "error": str(exc),
            }
        )

    if not inline_processing:
        background_tasks.add_task(process_pending_whatsapp_events)

    return {
        "ok": True,
        "results": results,
    }
