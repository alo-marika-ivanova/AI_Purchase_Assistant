from __future__ import annotations

import pytest

from app.db.database import get_connection
from app.db.repository import PurchasingRepository
from app.services import transport_delivery_service
from app.services.case_service import create_case


repo = PurchasingRepository()


def _create_case_and_message(supplier_id: int, channel: str) -> tuple[int, int]:
    case_id = create_case(
        item_material="test black diamonds",
        quantity=1.0,
        notes="Transport delivery service regression test",
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


def _human_review_item_count(message_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM human_review_items WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    return int(row["n"])


def test_successful_whatsapp_delivery_marks_outbox_sent(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )

    monkeypatch.setattr(
        transport_delivery_service,
        "send_whatsapp_text",
        lambda to_number, body: {
            "success": True,
            "delivery_outcome": "sent",
            "provider_message_id": "wamid.TEST1",
        },
    )

    result = transport_delivery_service.attempt_whatsapp_delivery(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        to_number="+420700000001",
        body="hello",
    )

    assert result["success"] is True

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "sent"
    assert job["provider_message_id"] == "wamid.TEST1"
    assert job["attempt_count"] == 1


def test_dry_run_delivery_marks_outbox_simulated(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(supplier_ids["email"], "email")

    monkeypatch.setattr(
        transport_delivery_service,
        "send_email_message",
        lambda **kwargs: {
            "success": True,
            "delivery_outcome": "dry_run",
            "provider_message_id": "dry-run-email",
            "internet_message_id": "<abc@purchasing-ai.local>",
        },
    )

    result = transport_delivery_service.attempt_email_delivery(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        to_email="supplier@example.test",
        subject="RFQ",
        body="hello",
    )

    assert result["success"] is True

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "simulated"


def test_transient_failure_schedules_a_retry_and_does_not_review(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )

    monkeypatch.setattr(
        transport_delivery_service,
        "send_whatsapp_text",
        lambda to_number, body: {
            "success": False,
            "delivery_outcome": "transient",
            "error": "Rate limited (429).",
            "retry_after_seconds": None,
        },
    )

    result = transport_delivery_service.attempt_whatsapp_delivery(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        to_number="+420700000001",
        body="hello",
    )

    assert result["success"] is False

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "transient_failure"
    assert job["attempt_count"] == 1
    assert job["next_attempt_at"] is not None
    assert _human_review_item_count(message_id) == 0


def test_permanent_failure_stops_retries_and_creates_review_item(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(supplier_ids["email"], "email")

    monkeypatch.setattr(
        transport_delivery_service,
        "send_email_message",
        lambda **kwargs: {
            "success": False,
            "delivery_outcome": "permanent",
            "error": "550 No such user.",
        },
    )

    result = transport_delivery_service.attempt_email_delivery(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        to_email="supplier@example.test",
        subject="RFQ",
        body="hello",
    )

    assert result["success"] is False

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "permanent_failure"
    assert job["next_attempt_at"] is None
    assert _human_review_item_count(message_id) == 1


def test_unknown_outcome_is_not_retryable_and_creates_review_item(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )

    monkeypatch.setattr(
        transport_delivery_service,
        "send_whatsapp_text",
        lambda to_number, body: {
            "success": False,
            "delivery_outcome": "unknown",
            "error": "Read timeout while awaiting the provider response.",
        },
    )

    result = transport_delivery_service.attempt_whatsapp_delivery(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        to_number="+420700000001",
        body="hello",
    )

    assert result["success"] is False

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "delivery_unknown"
    assert job["next_attempt_at"] is None
    assert _human_review_item_count(message_id) == 1


def test_repeated_attempt_after_success_does_not_resend(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )

    call_count = {"n": 0}

    def fake_send(to_number, body):
        call_count["n"] += 1
        return {
            "success": True,
            "delivery_outcome": "sent",
            "provider_message_id": "wamid.ONLYONCE",
        }

    monkeypatch.setattr(transport_delivery_service, "send_whatsapp_text", fake_send)

    transport_delivery_service.attempt_whatsapp_delivery(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        to_number="+420700000001",
        body="hello",
    )

    second_result = transport_delivery_service.attempt_whatsapp_delivery(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["whatsapp"],
        to_number="+420700000001",
        body="hello",
    )

    assert call_count["n"] == 1
    assert second_result["success"] is True
    assert second_result["provider_message_id"] == "wamid.ONLYONCE"


def test_retries_are_exhausted_after_max_attempts(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )

    monkeypatch.setattr(
        transport_delivery_service,
        "send_whatsapp_text",
        lambda to_number, body: {
            "success": False,
            "delivery_outcome": "transient",
            "error": "simulated persistent transient failure",
        },
    )

    # transient_failure is claimable regardless of next_attempt_at when
    # claimed by id (unlike the due-queue scan used by the poll loop), so
    # calling attempt_whatsapp_delivery repeatedly exercises the same
    # attempt-counting/exhaustion logic the real retry loop will hit later.
    for _ in range(transport_delivery_service.MAX_ATTEMPTS):
        transport_delivery_service.attempt_whatsapp_delivery(
            message_id=message_id,
            case_id=case_id,
            supplier_id=supplier_ids["whatsapp"],
            to_number="+420700000001",
            body="hello",
        )

    job = repo.get_outbox_status_for_message(message_id)

    assert job["status"] == "permanent_failure"
    assert job["attempt_count"] == transport_delivery_service.MAX_ATTEMPTS
    assert _human_review_item_count(message_id) == 1
