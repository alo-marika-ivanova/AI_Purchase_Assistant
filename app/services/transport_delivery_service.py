from __future__ import annotations

import os
import random
from datetime import datetime, timedelta

from app.db.repository import PurchasingRepository
from app.integrations.email_adapter import send_email_message
from app.integrations.whatsapp_adapter import send_whatsapp_text
from app.services.human_review_notification_service import (
    create_human_review_item_with_notification,
)


repo = PurchasingRepository()

# First attempt is immediate (attempt 1). Attempts 2-5 follow this backoff,
# counted in minutes since the previous attempt.
MAX_ATTEMPTS = 5
_BACKOFF_MINUTES_BY_ATTEMPT = {2: 1, 3: 5, 4: 15, 5: 60}
_JITTER_FRACTION = 0.15

_WORKER_ID = f"inline:{os.getpid()}"


def _next_attempt_at(
    next_attempt_number: int,
    retry_after_seconds: int | None,
) -> str | None:
    """Return the timestamp for the next attempt, or None once exhausted.

    ``next_attempt_number`` is the attempt about to be scheduled (2..5). A
    provider-supplied Retry-After is respected as a floor on top of the
    normal backoff schedule.
    """
    if next_attempt_number > MAX_ATTEMPTS:
        return None

    base_minutes = _BACKOFF_MINUTES_BY_ATTEMPT[next_attempt_number]
    jitter_minutes = base_minutes * _JITTER_FRACTION * random.uniform(-1, 1)
    delay_seconds = max(1.0, (base_minutes + jitter_minutes) * 60)

    if retry_after_seconds is not None:
        delay_seconds = max(delay_seconds, float(retry_after_seconds))

    next_attempt_time = datetime.utcnow() + timedelta(seconds=delay_seconds)
    return next_attempt_time.strftime("%Y-%m-%d %H:%M:%S")


def _surface_for_review(
    case_id: int,
    supplier_id: int | None,
    message_id: int,
    review_type: str,
    reason: str,
) -> None:
    create_human_review_item_with_notification(
        case_id=case_id,
        supplier_id=supplier_id,
        message_id=message_id,
        review_type=review_type,
        reason=reason,
    )


def _record_outcome(
    outbox_id: int,
    case_id: int,
    supplier_id: int | None,
    message_id: int,
    attempt_count_before: int,
    result: dict,
) -> None:
    outcome = result.get("delivery_outcome")
    error = result.get("error")

    if outcome == "sent":
        repo.mark_outbox_sent(
            outbox_id=outbox_id,
            provider_message_id=result.get("provider_message_id"),
        )
        return

    if outcome == "dry_run":
        repo.mark_outbox_simulated(
            outbox_id=outbox_id,
            provider_message_id=result.get("provider_message_id"),
        )
        return

    if outcome == "transient":
        next_attempt_number = attempt_count_before + 2
        next_attempt_at = _next_attempt_at(
            next_attempt_number=next_attempt_number,
            retry_after_seconds=result.get("retry_after_seconds"),
        )

        if next_attempt_at is None:
            repo.mark_outbox_permanent_failure(
                outbox_id=outbox_id,
                error=(
                    f"Retries exhausted after {MAX_ATTEMPTS} attempts. "
                    f"Last error: {error}"
                ),
            )
            _surface_for_review(
                case_id=case_id,
                supplier_id=supplier_id,
                message_id=message_id,
                review_type="outbound_delivery_retries_exhausted",
                reason=(
                    f"Automatic delivery failed {MAX_ATTEMPTS} times and "
                    f"will not be retried further. Last error: {error}"
                ),
            )
            return

        repo.mark_outbox_transient_failure(
            outbox_id=outbox_id,
            error=error or "Transient delivery failure.",
            next_attempt_at=next_attempt_at,
        )
        return

    if outcome == "permanent":
        repo.mark_outbox_permanent_failure(
            outbox_id=outbox_id,
            error=error or "Permanent delivery failure.",
        )
        _surface_for_review(
            case_id=case_id,
            supplier_id=supplier_id,
            message_id=message_id,
            review_type="outbound_delivery_permanent_failure",
            reason=(
                error
                or "The provider rejected this message outright; it will "
                "not be retried."
            ),
        )
        return

    # "unknown" and any unrecognized outcome are treated the same, safest
    # way: do not guess, surface for a human to resolve.
    repo.mark_outbox_delivery_unknown(
        outbox_id=outbox_id,
        error=error or "Delivery outcome could not be determined.",
    )
    _surface_for_review(
        case_id=case_id,
        supplier_id=supplier_id,
        message_id=message_id,
        review_type="outbound_delivery_unknown",
        reason=(
            error
            or "A timeout or connection loss made it unclear whether the "
            "provider received this message. It was not retried "
            "automatically to avoid a duplicate send."
        ),
    )


def _idempotent_short_circuit(message_id: int) -> dict | None:
    """Return a result without sending, if this message is already resolved
    or already being handled by another concurrent caller.
    """
    existing = repo.get_outbox_status_for_message(message_id)

    if existing is None:
        return None

    status = existing["status"]

    if status == "sent":
        return {
            "success": True,
            "delivery_outcome": "sent",
            "provider_message_id": existing.get("provider_message_id"),
            "error": None,
            "outbox_status": status,
        }

    if status == "simulated":
        return {
            "success": True,
            "delivery_outcome": "dry_run",
            "provider_message_id": existing.get("provider_message_id"),
            "error": None,
            "outbox_status": status,
        }

    return {
        "success": False,
        "delivery_outcome": "unknown",
        "provider_message_id": None,
        "error": (
            "Delivery already in progress or already resolved for this "
            "message."
        ),
        "outbox_status": status,
    }


def deliver_claimed_email_job(
    job: dict,
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> dict:
    """Perform one delivery attempt for an outbox job the caller has
    already claimed (via claim_outbox_job_by_id or claim_next_outbox_job),
    and update the outbox, the messages table, and the email thread-header
    record accordingly.

    Shared by the immediate first-attempt path (attempt_email_delivery) and
    the transport worker's later retry of the same job, so both paths apply
    the exact same bookkeeping regardless of which attempt succeeds.
    """
    message_id = int(job["message_id"])
    case_id = int(job["case_id"])
    supplier_id = job.get("supplier_id")
    supplier_id = int(supplier_id) if supplier_id is not None else None

    result = send_email_message(
        to_email=to_email,
        subject=subject,
        body=body,
        in_reply_to=in_reply_to,
        references=references,
    )

    _record_outcome(
        outbox_id=int(job["id"]),
        case_id=case_id,
        supplier_id=supplier_id,
        message_id=message_id,
        attempt_count_before=int(job["attempt_count"]),
        result=result,
    )

    if result.get("success"):
        repo.mark_message_sent_email(
            message_id=message_id,
            provider_message_id=result.get("provider_message_id"),
        )

        if hasattr(repo, "record_email_message_header"):
            repo.record_email_message_header(
                message_id=message_id,
                case_id=case_id,
                supplier_id=supplier_id,
                subject=subject,
                internet_message_id=result.get("internet_message_id"),
                in_reply_to=in_reply_to,
                reference_chain=references,
                graph_conversation_id=None,
            )
    else:
        repo.mark_message_send_failed(
            message_id=message_id,
            error=result.get("error") or "Unknown email send error.",
        )

    return result


def deliver_claimed_whatsapp_job(
    job: dict,
    to_number: str,
    body: str,
) -> dict:
    """Perform one delivery attempt for an outbox job the caller has
    already claimed, and update the outbox and the messages table
    accordingly. See deliver_claimed_email_job for the shared design.
    """
    message_id = int(job["message_id"])
    case_id = int(job["case_id"])
    supplier_id = job.get("supplier_id")
    supplier_id = int(supplier_id) if supplier_id is not None else None

    result = send_whatsapp_text(
        to_number=to_number,
        body=body,
    )

    _record_outcome(
        outbox_id=int(job["id"]),
        case_id=case_id,
        supplier_id=supplier_id,
        message_id=message_id,
        attempt_count_before=int(job["attempt_count"]),
        result=result,
    )

    if result.get("success"):
        repo.mark_message_sent_whatsapp(
            message_id=message_id,
            provider_message_id=result.get("provider_message_id"),
        )
    else:
        repo.mark_message_send_failed(
            message_id=message_id,
            error=result.get("error") or "Unknown WhatsApp send error.",
        )

    return result


def attempt_email_delivery(
    message_id: int,
    case_id: int,
    supplier_id: int | None,
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> dict:
    """Enqueue (idempotently) and make one immediate delivery attempt.

    This is the synchronous, message-creation-time send path used by both
    the automatic negotiation planners and buyer-triggered Streamlit
    actions. A later, poll-driven retry of a transient failure is handled
    separately by the transport worker, against the same outbox row (see
    deliver_claimed_email_job).
    """
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
    )

    claimed = repo.claim_outbox_job_by_id(
        outbox_id=outbox_id,
        worker_id=_WORKER_ID,
    )

    if claimed is None:
        short_circuit = _idempotent_short_circuit(message_id)
        if short_circuit is not None:
            return short_circuit

        return {
            "success": False,
            "delivery_outcome": "unknown",
            "provider_message_id": None,
            "error": "Could not claim the outbox job for this message.",
        }

    return deliver_claimed_email_job(
        job=claimed,
        to_email=to_email,
        subject=subject,
        body=body,
        in_reply_to=in_reply_to,
        references=references,
    )


def attempt_whatsapp_delivery(
    message_id: int,
    case_id: int,
    supplier_id: int | None,
    to_number: str,
    body: str,
) -> dict:
    """Enqueue (idempotently) and make one immediate delivery attempt.

    See attempt_email_delivery for the shared enqueue/claim/retry design.
    """
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_id,
        channel="whatsapp",
    )

    claimed = repo.claim_outbox_job_by_id(
        outbox_id=outbox_id,
        worker_id=_WORKER_ID,
    )

    if claimed is None:
        short_circuit = _idempotent_short_circuit(message_id)
        if short_circuit is not None:
            return short_circuit

        return {
            "success": False,
            "delivery_outcome": "unknown",
            "provider_message_id": None,
            "error": "Could not claim the outbox job for this message.",
        }

    return deliver_claimed_whatsapp_job(
        job=claimed,
        to_number=to_number,
        body=body,
    )
