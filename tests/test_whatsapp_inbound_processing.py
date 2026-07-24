from __future__ import annotations

from app.db.database import get_connection
from app.db.repository import PurchasingRepository
from app.services import transport_worker_service, whatsapp_transport_service
from app.services.case_service import create_case


repo = PurchasingRepository()

WHATSAPP_SENDER = "+420700000001"


def _create_whatsapp_case(supplier_id: int) -> int:
    return create_case(
        item_material="test black diamonds",
        quantity=1.0,
        notes="WhatsApp inbound staging regression test",
        supplier_ids=[supplier_id],
        auto_send_messages=False,
    )


def _backdate_event_locked_at(event_id: int, minutes: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE whatsapp_inbound_events SET locked_at = datetime('now', ?) WHERE id = ?",
            (f"-{minutes} minutes", event_id),
        )
        conn.commit()


def _event_status(wa_message_id: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM whatsapp_inbound_events WHERE wa_message_id = ?",
            (wa_message_id,),
        ).fetchone()

    return dict(row) if row else None


def test_persist_inbound_whatsapp_event_is_idempotent() -> None:
    first = whatsapp_transport_service.persist_inbound_whatsapp_event(
        wa_message_id="wamid.IDEMP1",
        sender_phone=WHATSAPP_SENDER,
        body="hello",
    )
    second = whatsapp_transport_service.persist_inbound_whatsapp_event(
        wa_message_id="wamid.IDEMP1",
        sender_phone=WHATSAPP_SENDER,
        body="hello",
    )

    assert first["persisted"] is True
    assert second["persisted"] is False

    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM whatsapp_inbound_events WHERE wa_message_id = ?",
            ("wamid.IDEMP1",),
        ).fetchone()["n"]

    assert count == 1


def test_process_pending_whatsapp_events_processes_a_staged_event(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["whatsapp"]
    case_id = _create_whatsapp_case(supplier_id)

    whatsapp_transport_service.persist_inbound_whatsapp_event(
        wa_message_id="wamid.STAGED1",
        sender_phone=WHATSAPP_SENDER,
        body="Our price is 42 USD per unit.",
    )

    processed_count = whatsapp_transport_service.process_pending_whatsapp_events()

    assert processed_count == 1

    event = _event_status("wamid.STAGED1")
    assert event["status"] == "processed"
    assert event["case_id"] == case_id
    assert event["supplier_id"] == supplier_id
    assert event["message_id"] is not None

    assert repo.whatsapp_import_exists("wamid.STAGED1")

    messages = repo.list_messages_for_case_supplier(
        case_id=case_id, supplier_id=supplier_id
    )
    inbound_bodies = [
        m["body"] for m in messages if m["direction"] == "inbound"
    ]
    assert "Our price is 42 USD per unit." in inbound_bodies


def test_unmatched_supplier_is_marked_failed_without_stopping_the_batch(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["whatsapp"]
    _create_whatsapp_case(supplier_id)

    whatsapp_transport_service.persist_inbound_whatsapp_event(
        wa_message_id="wamid.UNMATCHED1",
        sender_phone="+420799999999",
        body="hello from nobody",
    )
    whatsapp_transport_service.persist_inbound_whatsapp_event(
        wa_message_id="wamid.STAGED2",
        sender_phone=WHATSAPP_SENDER,
        body="Our price is 55 USD per unit.",
    )

    processed_count = whatsapp_transport_service.process_pending_whatsapp_events()

    assert processed_count == 2

    unmatched_event = _event_status("wamid.UNMATCHED1")
    assert unmatched_event["status"] == "failed"
    assert unmatched_event["error"]

    matched_event = _event_status("wamid.STAGED2")
    assert matched_event["status"] == "processed"


def test_reclaim_abandoned_whatsapp_inbound_event_is_not_reprocessed(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["whatsapp"]
    _create_whatsapp_case(supplier_id)

    whatsapp_transport_service.persist_inbound_whatsapp_event(
        wa_message_id="wamid.CRASH1",
        sender_phone=WHATSAPP_SENDER,
        body="Our price is 61 USD per unit.",
    )

    claimed = repo.claim_next_whatsapp_inbound_event(worker_id="crashed-worker")
    assert claimed is not None

    _backdate_event_locked_at(int(claimed["id"]), minutes=10)

    reclaimed = transport_worker_service.reclaim_abandoned_whatsapp_inbound_events()

    assert len(reclaimed) == 1

    event = _event_status("wamid.CRASH1")
    assert event["status"] == "failed"
    assert "interrupted" in event["error"]

    # A failed/reclaimed event must not be picked up again automatically.
    assert repo.claim_next_whatsapp_inbound_event(worker_id="another-worker") is None

    # No inbound message should have been recorded for the abandoned event.
    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE body = 'Our price is 61 USD per unit.'"
        ).fetchone()["n"]
    assert count == 0


def test_run_transport_cycle_processes_pending_whatsapp_events(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["whatsapp"]
    _create_whatsapp_case(supplier_id)

    whatsapp_transport_service.persist_inbound_whatsapp_event(
        wa_message_id="wamid.CYCLE1",
        sender_phone=WHATSAPP_SENDER,
        body="Our price is 77 USD per unit.",
    )

    transport_worker_service.run_transport_cycle()

    event = _event_status("wamid.CYCLE1")
    assert event["status"] == "processed"
