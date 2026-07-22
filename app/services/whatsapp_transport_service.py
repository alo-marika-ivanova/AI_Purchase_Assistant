from __future__ import annotations

import os
import re
from typing import Any

from dotenv import load_dotenv

from app.db.repository import PurchasingRepository
from app.services.simple_chat_service import (
    continue_negotiation_for_case,
    record_supplier_message_simple,
)

load_dotenv()

repo = PurchasingRepository()


def _extract_case_number(text: str) -> str | None:
    # Oldest format: CASE-YYYYMMDD-HHMMSS-microseconds-6HEX
    match = re.search(r"\bCASE-\d{8}-\d{6}-\d{6}-[A-Z0-9]{6}\b", text)
    if match:
        return match.group(0)

    # Previous format: CASE-YYYYMMDD-HHMMSS-6HEX
    match = re.search(r"\bCASE-\d{8}-\d{6}-[A-Z0-9]{6}\b", text)
    if match:
        return match.group(0)

    # Current format: ITEMCODE-YYYY-MM-DD-NN (e.g. AMP-2026-07-22-01)
    match = re.search(r"\b[A-Z0-9]{1,12}-\d{4}-\d{2}-\d{2}-\d{2,3}\b", text)
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