from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.services.whatsapp_transport_service import process_inbound_whatsapp_message

router = APIRouter()


@router.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(request: Request):
    """
    Meta webhook verification endpoint.
    """

    params = request.query_params

    mode = params.get("hub.mode")
    verify_token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    expected_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "")

    if mode == "subscribe" and verify_token == expected_token and challenge:
        return PlainTextResponse(challenge)

    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook/whatsapp")
async def receive_whatsapp_webhook(request: Request):
    """
    Receive WhatsApp webhook events from Meta.

    This implementation handles inbound text messages.
    Status events and non-text messages are acknowledged but ignored.
    """

    payload = await request.json()

    results = []

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

                    result = process_inbound_whatsapp_message(
                        wa_message_id=wa_message_id,
                        sender_phone=sender_phone,
                        text=text,
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

    return {
        "ok": True,
        "results": results,
    }