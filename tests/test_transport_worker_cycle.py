from __future__ import annotations

import pytest

from app.db.database import get_connection
from app.db.repository import PurchasingRepository
from app.negotiation.states import CaseState, SupplierState
from app.services import simple_chat_service, transport_delivery_service
from app.services import transport_worker_service
from app.services.case_service import create_case
from app.services.offer_service import add_offer


repo = PurchasingRepository()


def _create_case_and_message(supplier_id: int, channel: str) -> tuple[int, int]:
    case_id = create_case(
        item_material="test black diamonds",
        quantity=1.0,
        notes="Transport worker cycle regression test",
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


def _backdate_outbox_next_attempt_at(outbox_id: int, minutes: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE transport_outbox SET next_attempt_at = datetime('now', ?) WHERE id = ?",
            (f"-{minutes} minutes", outbox_id),
        )
        conn.commit()


def _backdate_outbox_locked_at(outbox_id: int, minutes: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE transport_outbox SET locked_at = datetime('now', ?) WHERE id = ?",
            (f"-{minutes} minutes", outbox_id),
        )
        conn.commit()


def test_send_message_by_whatsapp_updates_messages_table_on_success(
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
            "provider_message_id": "wamid.REAL1",
        },
    )

    simple_chat_service._send_message_by_whatsapp(message_id)

    message = repo.get_message_by_id(message_id)
    assert message["status"] == "sent_whatsapp"


def test_send_message_by_email_updates_messages_table_and_thread_header(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(supplier_ids["email"], "email")

    monkeypatch.setattr(
        transport_delivery_service,
        "send_email_message",
        lambda **kwargs: {
            "success": True,
            "delivery_outcome": "sent",
            "provider_message_id": "smtp:supplier@example.test",
            "internet_message_id": "<real1@purchasing-ai.local>",
        },
    )

    simple_chat_service._send_message_by_email(message_id)

    message = repo.get_message_by_id(message_id)
    assert message["status"] == "sent_email"

    header = repo.get_latest_email_thread_header(
        case_id=case_id, supplier_id=supplier_ids["email"]
    )
    assert header is not None
    assert header["internet_message_id"] == "<real1@purchasing-ai.local>"


def test_flush_outbox_retries_delivers_a_due_whatsapp_job(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(
        supplier_ids["whatsapp"], "whatsapp"
    )

    call_outcomes = iter(
        [
            {
                "success": False,
                "delivery_outcome": "transient",
                "error": "simulated transient failure",
            },
            {
                "success": True,
                "delivery_outcome": "sent",
                "provider_message_id": "wamid.RETRY1",
            },
        ]
    )

    monkeypatch.setattr(
        transport_delivery_service,
        "send_whatsapp_text",
        lambda to_number, body: next(call_outcomes),
    )

    simple_chat_service._send_message_by_whatsapp(message_id)

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "transient_failure"

    flushed_while_not_due = transport_worker_service.flush_outbox_retries()
    assert flushed_while_not_due == 0

    _backdate_outbox_next_attempt_at(int(job["id"]), minutes=10)

    flushed = transport_worker_service.flush_outbox_retries()
    assert flushed == 1

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "sent"
    assert job["provider_message_id"] == "wamid.RETRY1"
    assert job["attempt_count"] == 2

    message = repo.get_message_by_id(message_id)
    assert message["status"] == "sent_whatsapp"


def test_flush_outbox_retries_delivers_a_due_email_job_with_correct_threading(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, message_id = _create_case_and_message(supplier_ids["email"], "email")

    call_outcomes = iter(
        [
            {
                "success": False,
                "delivery_outcome": "transient",
                "error": "simulated SMTP connection failure",
            },
            {
                "success": True,
                "delivery_outcome": "sent",
                "provider_message_id": "smtp:supplier@example.test",
                "internet_message_id": "<retry1@purchasing-ai.local>",
            },
        ]
    )

    monkeypatch.setattr(
        transport_delivery_service,
        "send_email_message",
        lambda **kwargs: next(call_outcomes),
    )

    simple_chat_service._send_message_by_email(message_id)

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "transient_failure"

    _backdate_outbox_next_attempt_at(int(job["id"]), minutes=10)

    flushed = transport_worker_service.flush_outbox_retries()
    assert flushed == 1

    message = repo.get_message_by_id(message_id)
    assert message["status"] == "sent_email"

    header = repo.get_latest_email_thread_header(
        case_id=case_id, supplier_id=supplier_ids["email"]
    )
    assert header is not None
    assert header["internet_message_id"] == "<retry1@purchasing-ai.local>"


def test_reclaim_abandoned_outbox_jobs_surfaces_human_review(
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

    _backdate_outbox_locked_at(outbox_id, minutes=10)

    reclaimed = transport_worker_service.reclaim_abandoned_outbox_jobs()

    assert len(reclaimed) == 1
    assert _human_review_item_count(message_id) == 1

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "delivery_unknown"


def test_run_transport_cycle_reclaims_before_flushing(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
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
    repo.claim_next_outbox_job(worker_id="crashed-worker")
    _backdate_outbox_locked_at(outbox_id, minutes=10)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "A reclaimed (crashed) job must not be auto-retried."
        )

    monkeypatch.setattr(
        transport_delivery_service, "send_whatsapp_text", _fail_if_called
    )

    transport_worker_service.run_transport_cycle()

    job = repo.get_outbox_status_for_message(message_id)
    assert job["status"] == "delivery_unknown"


def test_run_email_worker_cycle_is_an_alias_for_run_transport_cycle() -> None:
    results = transport_worker_service.run_email_worker_cycle()
    assert results == transport_worker_service.run_transport_cycle()


def _set_up_negotiating_case_with_discount_request(
    supplier_id: int,
    channel: str,
) -> tuple[int, int]:
    """Build a case in NEGOTIATING state, with the supplier PRICE_EXTRACTED
    and one queued price_reduction_request message, matching the state a
    real negotiation planner run would have produced.
    """
    case_id = create_case(
        item_material="test black diamonds",
        quantity=1.0,
        notes="Deferred delivery reconciliation regression test",
        supplier_ids=[supplier_id],
        auto_send_messages=True,
    )

    offer_id = add_offer(
        case_id=case_id,
        supplier_id=supplier_id,
        unit_price_usd=100.0,
        extraction_method="manual",
        extraction_confidence="human_verified",
    )

    repo.upsert_case_negotiation_context(
        case_id=case_id,
        initial_best_offer_usd=100.0,
        target_price_usd=90.0,
        best_supplier_id=supplier_id,
        best_offer_id=offer_id,
        valid_offer_count=1,
        target_discount_percent=10.0,
        ranking_json="[]",
    )

    repo.set_supplier_policy_state(
        case_id=case_id,
        supplier_id=supplier_id,
        state=SupplierState.PRICE_EXTRACTED.value,
        best_offer_usd=100.0,
        target_price_usd=90.0,
    )

    repo.update_case_status_with_event(
        case_id=case_id,
        status=CaseState.NEGOTIATING.value,
        event_type="test_setup",
        details="Test setup for deferred delivery reconciliation.",
    )

    message_id = repo.add_message(
        case_id=case_id,
        supplier_id=supplier_id,
        direction="outbound",
        channel=channel,
        body="Could you reach USD 90.00 per unit?",
        status="queued",
        message_type="price_reduction_request",
        approval_required=False,
        approved_by_buyer=True,
    )

    return case_id, message_id


def test_deferred_retry_reconciles_negotiation_state_and_attempt_count(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplier_id = supplier_ids["whatsapp"]
    case_id, message_id = _set_up_negotiating_case_with_discount_request(
        supplier_id, "whatsapp"
    )

    call_outcomes = iter(
        [
            {
                "success": False,
                "delivery_outcome": "transient",
                "error": "simulated transient failure",
            },
            {
                "success": True,
                "delivery_outcome": "sent",
                "provider_message_id": "wamid.NEGOTIATE1",
            },
        ]
    )

    monkeypatch.setattr(
        transport_delivery_service,
        "send_whatsapp_text",
        lambda to_number, body: next(call_outcomes),
    )

    simple_chat_service._send_message_by_whatsapp(message_id)

    state_after_failure = repo.get_supplier_policy_state(
        case_id=case_id, supplier_id=supplier_id
    )
    assert state_after_failure["state"] == SupplierState.PRICE_EXTRACTED.value
    assert state_after_failure["negotiation_attempts"] == 0

    job = repo.get_outbox_status_for_message(message_id)
    _backdate_outbox_next_attempt_at(int(job["id"]), minutes=10)

    flushed = transport_worker_service.flush_outbox_retries()
    assert flushed == 1

    state_after_retry = repo.get_supplier_policy_state(
        case_id=case_id, supplier_id=supplier_id
    )
    assert state_after_retry["state"] == SupplierState.DISCOUNT_REQUEST_SENT.value
    assert state_after_retry["negotiation_attempts"] == 1
