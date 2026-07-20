from __future__ import annotations

import os

from app.db.repository import PurchasingRepository
from app.integrations.email_adapter import send_internal_email_message


repo = PurchasingRepository()


def _buyer_notification_email() -> str:
    return (
        os.getenv("BUYER_REVIEW_NOTIFICATION_EMAIL", "").strip()
        or os.getenv("BUYER_EMAIL", "").strip()
    )


def _build_notification_message(data: dict) -> tuple[str, str]:
    supplier_name = data.get("supplier_name") or "Case-level review"
    supplier_code = data.get("supplier_code")
    supplier_label = (
        f"{supplier_name} ({supplier_code})"
        if supplier_code
        else supplier_name
    )

    subject = (
        f"Human review required: {data['case_number']} - "
        f"{supplier_name}"
    )

    supplier_message = (data.get("supplier_message") or "").strip()
    message_section = supplier_message or "No supplier message was linked to this item."

    body = (
        "A new human-review item was created in AI Purchase Assistant.\n\n"
        f"Case: {data['case_number']}\n"
        f"Item/material: {data['item_material']}\n"
        f"Quantity: {data['quantity']}\n"
        f"Supplier: {supplier_label}\n"
        f"Review type: {data['review_type']}\n"
        f"Reason: {data['reason']}\n\n"
        "Supplier message:\n"
        f"{message_section}\n\n"
        "Open AI Purchase Assistant to review the item. "
        "No purchase commitment is made by this notification."
    )

    return subject, body


def notify_buyer_about_human_review(review_item_id: int) -> dict:
    """Send at most one internal email for an opted-in review item."""
    data = repo.get_human_review_email_notification_data(review_item_id)

    if data is None:
        return {
            "sent": False,
            "reason": "review_item_not_found",
        }

    if not bool(data.get("notify_human_review_email")):
        return {
            "sent": False,
            "reason": "case_notification_disabled",
        }

    recipient_email = _buyer_notification_email()

    if not repo.claim_human_review_email_notification(
        review_item_id=review_item_id,
        recipient_email=recipient_email or None,
    ):
        return {
            "sent": False,
            "reason": "notification_already_claimed",
        }

    if not recipient_email:
        error = (
            "BUYER_REVIEW_NOTIFICATION_EMAIL and BUYER_EMAIL are both empty."
        )
        repo.complete_human_review_email_notification(
            review_item_id,
            success=False,
            error=error,
        )
        repo.log_worker_event(
            case_id=int(data["case_id"]),
            event_type="human_review_email_notification_failed",
            details=(
                f"Review item {review_item_id}: {error}"
            ),
        )
        return {
            "sent": False,
            "reason": "missing_buyer_email",
            "error": error,
        }

    subject, body = _build_notification_message(data)

    try:
        result = send_internal_email_message(
            to_email=recipient_email,
            subject=subject,
            body=body,
        )
    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
        }

    success = bool(result.get("success"))
    error = result.get("error")

    repo.complete_human_review_email_notification(
        review_item_id,
        success=success,
        error=error,
    )

    repo.log_worker_event(
        case_id=int(data["case_id"]),
        event_type=(
            "human_review_email_notification_sent"
            if success
            else "human_review_email_notification_failed"
        ),
        details=(
            f"Review item {review_item_id}; recipient {recipient_email}; "
            f"result {'sent' if success else 'failed'}"
            + (f"; error: {error}" if error else "")
        ),
    )

    return {
        "sent": success,
        "reason": "sent" if success else "send_failed",
        "error": error,
    }


def create_human_review_item_with_notification(
    case_id: int,
    supplier_id: int | None,
    message_id: int | None,
    review_type: str,
    reason: str,
) -> int:
    """Create/deduplicate a review item and apply its email preference."""
    review_item_id = repo.create_human_review_item(
        case_id=case_id,
        supplier_id=supplier_id,
        message_id=message_id,
        review_type=review_type,
        reason=reason,
    )

    notify_buyer_about_human_review(review_item_id)
    return review_item_id
