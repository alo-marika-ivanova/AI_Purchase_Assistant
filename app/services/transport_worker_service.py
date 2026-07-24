from __future__ import annotations

import os
import socket
from dataclasses import dataclass

from dotenv import load_dotenv

from app.db.repository import PurchasingRepository
from app.services import simple_chat_service
from app.services.human_review_notification_service import (
    create_human_review_item_with_notification,
)
from app.services.simple_chat_service import refresh_mailbox_and_continue_case
from app.services.transport_delivery_service import (
    deliver_claimed_email_job,
    deliver_claimed_whatsapp_job,
)
from app.services.whatsapp_transport_service import process_pending_whatsapp_events


load_dotenv()
repo = PurchasingRepository()

_WORKER_ID = f"transport-worker:{socket.gethostname()}:{os.getpid()}"

# Bounds how many outbox jobs one cycle will flush, so a large backlog
# cannot make a single cycle run indefinitely. Any remainder is picked up on
# the next cycle.
OUTBOX_FLUSH_BATCH_LIMIT = 50


@dataclass
class WorkerCaseResult:
    case_id: int
    case_number: str
    communication_mode: str
    imported_count: int = 0
    skipped_count: int = 0
    rule_actions: list | None = None
    import_results: list[dict] | None = None
    error: str | None = None


def _get_worker_case_filter() -> int | None:
    raw_value = os.getenv("EMAIL_WORKER_CASE_ID", "").strip()

    if not raw_value:
        return None

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(
            "EMAIL_WORKER_CASE_ID must be empty or a numeric case ID."
        ) from exc


def get_cases_for_worker() -> list[dict]:
    case_filter = _get_worker_case_filter()
    cases = repo.list_cases_for_transport_worker()

    if case_filter is None:
        return cases

    return [
        case
        for case in cases
        if int(case["id"]) == case_filter
    ]


def process_case_email_transport(case: dict) -> WorkerCaseResult:
    case_id = int(case["id"])
    real_mode = bool(case.get("auto_send_messages"))

    result = WorkerCaseResult(
        case_id=case_id,
        case_number=case["case_number"],
        communication_mode="REAL" if real_mode else "SIMULATION",
        rule_actions=[],
        import_results=[],
    )

    try:
        cycle_result = refresh_mailbox_and_continue_case(case_id=case_id)

        import_result = cycle_result["import_result"]
        negotiation_result = cycle_result["negotiation_result"]

        result.imported_count = int(import_result.get("imported_count", 0))
        result.skipped_count = int(import_result.get("skipped_count", 0))
        result.import_results = import_result.get("results", [])
        result.rule_actions = negotiation_result.get("actions", [])

        return result

    except Exception as exc:
        result.error = str(exc)
        return result


def reclaim_abandoned_outbox_jobs() -> list[dict]:
    """Move abandoned processing jobs to delivery_unknown and surface each
    one for human review.

    A hard process or computer crash can occur after an outbox job is
    claimed but before its delivery outcome is recorded. Such a job must
    never be silently retried (the provider may already have accepted it)
    and must never be silently forgotten either.
    """
    reclaimed = repo.reclaim_abandoned_outbox_jobs()

    for job in reclaimed:
        supplier_id = job.get("supplier_id")

        create_human_review_item_with_notification(
            case_id=int(job["case_id"]),
            supplier_id=int(supplier_id) if supplier_id is not None else None,
            message_id=int(job["message_id"]),
            review_type="outbound_delivery_unknown",
            reason=(
                "A worker or process restart interrupted this delivery "
                "attempt before its outcome could be recorded. It was not "
                "retried automatically to avoid a duplicate send."
            ),
        )

    return reclaimed


def _retry_one_due_outbox_job() -> bool:
    """Claim and attempt exactly one due outbox job, if any is ready.

    Returns True if a job was claimed (regardless of the attempt's
    outcome), False if nothing is currently due.
    """
    job = repo.claim_next_outbox_job(worker_id=_WORKER_ID)

    if job is None:
        return False

    message_id = int(job["message_id"])
    message = repo.get_message_by_id(message_id)

    if message is None:
        repo.mark_outbox_permanent_failure(
            outbox_id=int(job["id"]),
            error="Referenced message no longer exists.",
        )
        return True

    if job["channel"] == "email":
        context = simple_chat_service.build_email_delivery_context(message)

        result = deliver_claimed_email_job(
            job=job,
            to_email=message.get("email"),
            subject=context["subject"],
            body=message["body"],
            in_reply_to=context["in_reply_to"],
            references=context["references"],
        )
    else:
        result = deliver_claimed_whatsapp_job(
            job=job,
            to_number=message.get("whatsapp_number"),
            body=message["body"],
        )

    if result.get("success"):
        # This retry is, by construction, not a first-attempt success (see
        # apply_deferred_delivery_side_effects docstring), so the state
        # transition a synchronous success would have applied inline was
        # skipped when the first attempt failed. Apply it now.
        simple_chat_service.apply_deferred_delivery_side_effects(message_id)

    return True


def flush_outbox_retries(max_jobs: int = OUTBOX_FLUSH_BATCH_LIMIT) -> int:
    """Attempt delivery for every currently-due outbox job, across every
    case. Returns the number of jobs actually attempted.
    """
    flushed = 0

    while flushed < max_jobs:
        if not _retry_one_due_outbox_job():
            break
        flushed += 1

    return flushed


def reclaim_abandoned_whatsapp_inbound_events() -> list[dict]:
    """Move abandoned inbound WhatsApp events to failed.

    See PurchasingRepository.reclaim_abandoned_whatsapp_inbound_events for
    why an event interrupted mid-processing is surfaced for manual review
    rather than silently reprocessed.
    """
    return repo.reclaim_abandoned_whatsapp_inbound_events()


def run_transport_cycle() -> list[WorkerCaseResult]:
    """Run one full unified transport worker cycle.

    Order: reclaim abandoned outbox jobs and abandoned inbound WhatsApp
    events first (so a crash never leaves either silently stuck), then
    process any pending inbound WhatsApp events (classification + their own
    negotiation continuation), then poll/process inbound email and advance
    negotiation for every eligible case exactly as before, then flush any
    outbound email/WhatsApp retries that are now due.
    """
    reclaim_abandoned_outbox_jobs()
    reclaim_abandoned_whatsapp_inbound_events()

    process_pending_whatsapp_events()

    results = [
        process_case_email_transport(case)
        for case in get_cases_for_worker()
    ]

    flush_outbox_retries()

    return results


def run_email_worker_cycle() -> list[WorkerCaseResult]:
    """Deprecated alias for run_transport_cycle(), kept for compatibility
    with any external caller still importing the old name.
    """
    return run_transport_cycle()
