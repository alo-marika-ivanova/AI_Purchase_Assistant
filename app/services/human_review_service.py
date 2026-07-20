from __future__ import annotations

import re
from typing import Any

from app.db.repository import PurchasingRepository
from app.negotiation.states import SupplierState
from app.services.simple_chat_service import send_or_display_outbound_message


repo = PurchasingRepository()


def _format_quantity(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value or "").strip() or "the requested quantity"


def _extract_price_hint(text: str) -> str | None:
    """
    Small UI helper only. This does not save offers or make decisions.
    It only helps phrase suggested human replies.
    """
    if not text:
        return None

    match = re.search(
        r"(?i)(?:usd|\$)?\s*(\d+(?:[.,]\d+)?)\s*(?:usd|\$)?",
        text,
    )

    if not match:
        return None

    value = match.group(1).replace(",", ".")
    return value


def build_human_review_suggestions(
    review_item: dict,
    case_data: dict,
) -> list[dict]:
    """
    Return buyer-safe suggested replies.

    These are intentionally conservative. They do not accept changed terms,
    changed quantity, delivery/payment changes, or conditional offers.
    """
    item_material = case_data.get("item_material") or review_item.get("item_material") or "the requested item"
    quantity = _format_quantity(case_data.get("quantity") or review_item.get("quantity"))
    supplier_message = review_item.get("message_body") or ""
    price_hint = _extract_price_hint(supplier_message)

    price_text = (
        f" at USD {price_hint} per unit"
        if price_hint
        else ""
    )

    return [
        {
            "title": "Ask for requested quantity only",
            "body": (
                f"Thank you. For this request, please quote only for {quantity} pcs "
                f"of {item_material}. Can you confirm your best unit price in USD "
                f"for this quantity?"
            ),
        },
        {
            "title": "Reject the quantity condition",
            "body": (
                f"Thank you for the information. We cannot confirm a different "
                f"minimum quantity for this request at this stage. Please confirm "
                f"your best possible unit price in USD for {quantity} pcs of "
                f"{item_material}."
            ),
        },
        {
            "title": "Clarify whether the offer applies",
            "body": (
                f"Thank you. The quantity condition changes the offer. Please confirm "
                f"whether your price{price_text} is valid for {quantity} pcs of "
                f"{item_material}. If not, please send your best unit price in USD "
                f"for the requested quantity."
            ),
        },
    ]


def resolve_human_review_with_reply(
    review_item_id: int,
    body: str,
) -> dict:
    clean_body = (body or "").strip()

    if not clean_body:
        raise ValueError("Human review reply body is required.")

    item = repo.get_open_human_review_item(review_item_id)

    if item is None:
        raise ValueError("Open human review item not found.")

    supplier_id = item.get("supplier_id")

    if supplier_id is None:
        raise ValueError(
            "This review item is case-level and is not linked to a supplier."
        )

    case_id = int(item["case_id"])
    supplier_id = int(supplier_id)

    result = send_or_display_outbound_message(
        case_id=case_id,
        supplier_id=supplier_id,
        body=clean_body,
        message_type="human_review_response",
    )

    send_result = result.get("send_result")

    if send_result is not None and not send_result.get("success"):
        return {
            "success": False,
            "resolved": False,
            "message_id": result.get("message_id"),
            "error": send_result.get("error") or "Message sending failed.",
            "send_result": send_result,
        }

    repo.resolve_human_review_item(review_item_id)

    repo.set_supplier_policy_state(
        case_id=case_id,
        supplier_id=supplier_id,
        state=SupplierState.AWAITING_RESPONSE.value,
    )

    repo.log_worker_event(
        case_id=case_id,
        event_type="human_review_reply_sent",
        details=(
            f"Human review item {review_item_id} was resolved by buyer reply. "
            f"Outbound message ID: {result.get('message_id')}."
        ),
    )

    return {
        "success": True,
        "resolved": True,
        "message_id": result.get("message_id"),
        "send_result": send_result,
    }


def resolve_human_review_without_reply(
    review_item_id: int,
    note: str = "",
) -> dict:
    """
    Use when the buyer handled the issue outside the system or decides the
    item no longer requires action.
    """
    item = repo.get_open_human_review_item(review_item_id)

    if item is None:
        raise ValueError("Open human review item not found.")

    case_id = int(item["case_id"])
    supplier_id = item.get("supplier_id")

    repo.resolve_human_review_item(review_item_id)

    if supplier_id is not None:
        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=int(supplier_id),
            state=SupplierState.AWAITING_RESPONSE.value,
        )

    repo.log_worker_event(
        case_id=case_id,
        event_type="human_review_resolved_without_system_reply",
        details=(
            f"Human review item {review_item_id} was marked resolved by the buyer. "
            f"Note: {(note or '').strip() or 'No note provided.'}"
        ),
    )

    return {
        "success": True,
        "resolved": True,
    }