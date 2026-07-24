from __future__ import annotations

import os
import re
import socket
from typing import Any

from dotenv import load_dotenv

from app.db.repository import PurchasingRepository
from app.services.simple_chat_service import (
    continue_negotiation_for_case,
    record_supplier_message_simple,
)

load_dotenv()

repo = PurchasingRepository()

_WORKER_ID = f"whatsapp-inbound:{socket.gethostname()}:{os.getpid()}"
WHATSAPP_INBOUND_FLUSH_BATCH_LIMIT = 50


def _extract_case_number(text: str) -> str | None:
    match = re.search(r"\bCASE-\d{8}-\d{6}-\d{6}-[A-Z0-9]{6}\b", text)
    if match:
        return match.group(0)

    match = re.search(r"\bCASE-\d{8}-\d{6}-[A-Z0-9]{6}\b", text)
    if match:
        return match.group(0)

    return None


def _determine_case_for_whatsapp_message(
    supplier_id: int,
    text: str,
) -> dict:
    """
    Determine which case an inbound WhatsApp message belongs to.

    Strategy:
    1. If message contains a case number, use it.
    2. Otherwise, if supplier has exactly one open case, use it.
    3. Otherwise fail safely.
    """

    case_number = _extract_case_number(text)

    if case_number:
        case_data = repo.find_case_by_case_number(case_number)
        if case_data is None:
            raise ValueError(f"Case number {case_number} was found but not recognized.")

        return case_data

    open_cases = repo.list_open_cases_for_supplier(supplier_id)

    if len(open_cases) == 1:
        return open_cases[0]

    if not open_cases:
        raise ValueError(
            "Could not match WhatsApp message to a case. Supplier has no open cases."
        )

    raise ValueError(
        "Could not match WhatsApp message to a case. Supplier has multiple open cases. "
        "Ask supplier to include the case number in the message."
    )


def process_inbound_whatsapp_message(
    wa_message_id: str,
    sender_phone: str,
    text: str,
    received_at: str | None = None,
) -> dict[str, Any]:
    """
    Store inbound WhatsApp message, extract price, and optionally continue negotiation.
    """

    if not wa_message_id:
        raise ValueError("WhatsApp message ID is missing.")

    if repo.whatsapp_import_exists(wa_message_id):
        return {
            "imported": False,
            "reason": "WhatsApp message already imported.",
        }

    supplier = repo.find_supplier_by_whatsapp_number(sender_phone)

    if supplier is None:
        raise ValueError(f"No active supplier found for WhatsApp number {sender_phone}.")

    supplier_id = int(supplier["id"])

    case_data = _determine_case_for_whatsapp_message(
        supplier_id=supplier_id,
        text=text,
    )

    case_id = int(case_data["id"])

    result = record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="whatsapp",
        body=text,
    )

    repo.record_whatsapp_import(
        wa_message_id=wa_message_id,
        case_id=case_id,
        supplier_id=supplier_id,
        message_id=int(result["inbound_message_id"]),
        sender_phone=sender_phone,
        received_at=received_at,
    )

    auto_continue = (
        os.getenv("WHATSAPP_AUTO_CONTINUE_NEGOTIATION", "true").lower() == "true"
    )

    negotiation_result = {
        "actions": [],
    }

    if auto_continue:
        negotiation_result = continue_negotiation_for_case(
            case_id=case_id,
        )

    return {
        "imported": True,
        "case_id": case_id,
        "supplier_id": supplier_id,
        "supplier_name": supplier["name"],
        "record_result": result,
        "negotiation_result": negotiation_result,
    }


def persist_inbound_whatsapp_event(
    wa_message_id: str,
    sender_phone: str,
    body: str,
    received_at: str | None = None,
) -> dict[str, Any]:
    """
    Persist one inbound WhatsApp webhook event quickly, without any
    classification or negotiation processing.

    This is the fast path the webhook uses so it can respond immediately.
    Safe to call more than once for the same wa_message_id (e.g. a Meta
    redelivery): the unique constraint on wa_message_id makes a duplicate
    call a no-op.
    """
    if not wa_message_id:
        raise ValueError("WhatsApp message ID is missing.")

    persisted = repo.create_whatsapp_inbound_event(
        wa_message_id=wa_message_id,
        sender_phone=sender_phone,
        body=body,
        received_at=received_at,
    )

    return {
        "persisted": persisted,
        "wa_message_id": wa_message_id,
    }


def _process_whatsapp_inbound_event_row(event: dict) -> dict[str, Any]:
    """
    Classify and route one already-staged inbound WhatsApp event.

    Shares the same supplier/case resolution and negotiation-continuation
    logic as process_inbound_whatsapp_message, applied to a row read from
    whatsapp_inbound_events instead of raw webhook arguments.
    """
    sender_phone = event["sender_phone"]
    text = event["body"]

    supplier = repo.find_supplier_by_whatsapp_number(sender_phone)

    if supplier is None:
        raise ValueError(f"No active supplier found for WhatsApp number {sender_phone}.")

    supplier_id = int(supplier["id"])

    case_data = _determine_case_for_whatsapp_message(
        supplier_id=supplier_id,
        text=text,
    )

    case_id = int(case_data["id"])

    result = record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="whatsapp",
        body=text,
    )

    repo.record_whatsapp_import(
        wa_message_id=event["wa_message_id"],
        case_id=case_id,
        supplier_id=supplier_id,
        message_id=int(result["inbound_message_id"]),
        sender_phone=sender_phone,
        received_at=event.get("received_at"),
    )

    auto_continue = (
        os.getenv("WHATSAPP_AUTO_CONTINUE_NEGOTIATION", "true").lower() == "true"
    )

    negotiation_result = {
        "actions": [],
    }

    if auto_continue:
        negotiation_result = continue_negotiation_for_case(
            case_id=case_id,
        )

    return {
        "case_id": case_id,
        "supplier_id": supplier_id,
        "supplier_name": supplier["name"],
        "record_result": result,
        "negotiation_result": negotiation_result,
    }


def _process_one_pending_whatsapp_event() -> bool:
    """Claim and process exactly one pending inbound WhatsApp event.

    Returns True if an event was claimed (regardless of outcome), False if
    nothing is currently pending. A per-event failure is recorded on that
    event and does not stop the caller from processing the rest.
    """
    event = repo.claim_next_whatsapp_inbound_event(worker_id=_WORKER_ID)

    if event is None:
        return False

    event_id = int(event["id"])

    try:
        result = _process_whatsapp_inbound_event_row(event)

        repo.mark_whatsapp_inbound_event_processed(
            event_id=event_id,
            case_id=result.get("case_id"),
            supplier_id=result.get("supplier_id"),
            message_id=int(result["record_result"]["inbound_message_id"]),
        )
    except Exception as exc:
        repo.mark_whatsapp_inbound_event_failed(
            event_id=event_id,
            error=str(exc),
        )

    return True


def process_pending_whatsapp_events(
    max_events: int = WHATSAPP_INBOUND_FLUSH_BATCH_LIMIT,
) -> int:
    """Process every currently-pending inbound WhatsApp event, up to
    max_events per call.

    Called both by the transport worker's regular poll cycle (the durable
    backstop) and, as a fast path, by a background task the webhook
    schedules right after responding. Both callers race safely: claiming is
    atomic, so the same event is never processed twice.
    """
    processed = 0

    while processed < max_events:
        if not _process_one_pending_whatsapp_event():
            break
        processed += 1

    return processed