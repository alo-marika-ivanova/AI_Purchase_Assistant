from __future__ import annotations

from app.db.database import get_connection
from app.db.repository import PurchasingRepository
from app.services.case_service import create_case


repo = PurchasingRepository()


def _create_case_and_message(supplier_id: int, channel: str) -> tuple[int, int]:
    case_id = create_case(
        item_material="test black diamonds",
        quantity=1.0,
        notes="Transport outbox regression test",
        supplier_ids=[supplier_id],
        auto_send_messages=True,
    )

    message_id = repo.add_message(
        case_id=case_id,
        supplier_id=supplier_id,
        direction="outbound",
        channel=channel,
        body="Test outbound message body.",
        status="queued",
        message_type="rfq",
        approval_required=False,
        approved_by_buyer=True,
    )

    return case_id, message_id


def _backdate_outbox_locked_at(outbox_id: int, minutes: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE transport_outbox
            SET locked_at = datetime('now', ?)
            WHERE id = ?
            """,
            (f"-{minutes} minutes", outbox_id),
        )
        conn.commit()


def _backdate_outbox_next_attempt_at(outbox_id: int, minutes: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE transport_outbox
            SET next_attempt_at = datetime('now', ?)
            WHERE id = ?
            """,
            (f"-{minutes} minutes", outbox_id),
        )
        conn.commit()


def test_create_outbox_job_is_idempotent(supplier_ids: dict[str, int]) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )

    first_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )
    second_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )

    assert first_id == second_id

    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM transport_outbox WHERE message_id = ?",
            (message_id,),
        ).fetchone()["n"]

    assert count == 1


def test_new_job_defaults_to_pending(supplier_ids: dict[str, int]) -> None:
    case_id, message_id = _create_case_and_message(supplier_ids["email"], "email")

    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        channel="email",
    )

    job = repo.get_outbox_status_for_message(message_id)

    assert job is not None
    assert job["id"] == outbox_id
    assert job["status"] == "pending"
    assert job["attempt_count"] == 0
    assert job["channel"] == "email"


def test_claim_next_outbox_job_marks_it_processing(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )
    repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )

    claimed = repo.claim_next_outbox_job(worker_id="test-worker-1")

    assert claimed is not None
    assert claimed["message_id"] == message_id
    assert claimed["status"] == "processing"
    assert claimed["locked_by"] == "test-worker-1"


def test_claim_next_outbox_job_skips_future_next_attempt(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )
    repo.mark_outbox_transient_failure(
        outbox_id=outbox_id,
        error="simulated transient failure",
        next_attempt_at="9999-01-01 00:00:00",
    )

    claimed = repo.claim_next_outbox_job(worker_id="test-worker-1")

    assert claimed is None


def test_claim_next_outbox_job_is_not_returned_twice(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )
    repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )

    first_claim = repo.claim_next_outbox_job(worker_id="test-worker-1")
    second_claim = repo.claim_next_outbox_job(worker_id="test-worker-2")

    assert first_claim is not None
    assert second_claim is None


def test_mark_outbox_sent_stores_provider_id_and_clears_retry_fields(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )
    repo.claim_next_outbox_job(worker_id="test-worker-1")

    repo.mark_outbox_sent(outbox_id=outbox_id, provider_message_id="wamid.TEST123")

    job = repo.get_outbox_status_for_message(message_id)

    assert job["status"] == "sent"
    assert job["provider_message_id"] == "wamid.TEST123"
    assert job["attempt_count"] == 1
    assert job["next_attempt_at"] is None
    assert job["last_error"] is None
    assert job["sent_at"] is not None


def test_mark_outbox_simulated_is_distinct_from_sent(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(supplier_ids["email"], "email")
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        channel="email",
    )
    repo.claim_next_outbox_job(worker_id="test-worker-1")

    repo.mark_outbox_simulated(
        outbox_id=outbox_id, provider_message_id="dry-run-email"
    )

    job = repo.get_outbox_status_for_message(message_id)

    assert job["status"] == "simulated"
    assert job["provider_message_id"] == "dry-run-email"


def test_mark_outbox_transient_failure_sets_retry_fields(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )
    repo.claim_next_outbox_job(worker_id="test-worker-1")

    repo.mark_outbox_transient_failure(
        outbox_id=outbox_id,
        error="Rate limited (429).",
        next_attempt_at="2099-01-01 00:00:00",
    )

    job = repo.get_outbox_status_for_message(message_id)

    assert job["status"] == "transient_failure"
    assert job["attempt_count"] == 1
    assert job["failure_type"] == "transient"
    assert job["next_attempt_at"] == "2099-01-01 00:00:00"
    assert job["last_error"] == "Rate limited (429)."


def test_transient_failure_job_can_be_reclaimed_once_due(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )
    repo.claim_next_outbox_job(worker_id="test-worker-1")
    repo.mark_outbox_transient_failure(
        outbox_id=outbox_id,
        error="simulated transient failure",
        next_attempt_at="2099-01-01 00:00:00",
    )

    assert repo.claim_next_outbox_job(worker_id="test-worker-2") is None

    _backdate_outbox_next_attempt_at(outbox_id, minutes=10)

    claimed = repo.claim_next_outbox_job(worker_id="test-worker-2")

    assert claimed is not None
    assert claimed["id"] == outbox_id
    assert claimed["status"] == "processing"


def test_mark_outbox_permanent_failure_stops_retries(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(supplier_ids["email"], "email")
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        channel="email",
    )
    repo.claim_next_outbox_job(worker_id="test-worker-1")

    repo.mark_outbox_permanent_failure(
        outbox_id=outbox_id,
        error="Invalid recipient address.",
    )

    job = repo.get_outbox_status_for_message(message_id)

    assert job["status"] == "permanent_failure"
    assert job["failure_type"] == "permanent"
    assert job["next_attempt_at"] is None

    assert repo.claim_next_outbox_job(worker_id="test-worker-2") is None


def test_mark_outbox_delivery_unknown_is_not_retryable(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )
    repo.claim_next_outbox_job(worker_id="test-worker-1")

    repo.mark_outbox_delivery_unknown(
        outbox_id=outbox_id,
        error="Connection reset while awaiting the provider response.",
    )

    job = repo.get_outbox_status_for_message(message_id)

    assert job["status"] == "delivery_unknown"
    assert job["failure_type"] == "unknown"
    assert job["next_attempt_at"] is None

    assert repo.claim_next_outbox_job(worker_id="test-worker-2") is None


def test_reclaim_abandoned_outbox_jobs_moves_stale_processing_to_delivery_unknown(
    supplier_ids: dict[str, int],
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )
    outbox_id = repo.create_outbox_job(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        channel="whatsapp",
    )
    claimed = repo.claim_next_outbox_job(worker_id="crashed-worker")
    assert claimed is not None

    reclaimed = repo.reclaim_abandoned_outbox_jobs(lease_seconds=300)
    assert reclaimed == []

    _backdate_outbox_locked_at(outbox_id, minutes=10)

    reclaimed = repo.reclaim_abandoned_outbox_jobs(lease_seconds=300)

    assert len(reclaimed) == 1
    assert reclaimed[0]["id"] == outbox_id

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "delivery_unknown"
    assert job["failure_type"] == "unknown"

    assert repo.claim_next_outbox_job(worker_id="test-worker-2") is None


def test_get_outbox_status_for_message_returns_none_when_no_job_exists(
    supplier_ids: dict[str, int],
) -> None:
    _, message_id = _create_case_and_message(supplier_ids["email"], "email")

    assert repo.get_outbox_status_for_message(message_id) is None
