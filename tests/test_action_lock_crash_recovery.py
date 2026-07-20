from __future__ import annotations

from app.db.database import get_connection
from app.db.repository import PurchasingRepository
from app.services.case_service import create_case
from app.services import simple_chat_service


repo = PurchasingRepository()


def _create_case(supplier_ids: list[int]) -> int:
    return create_case(
        item_material="test black diamonds",
        quantity=1.0,
        notes="Action-lock crash recovery regression test",
        supplier_ids=supplier_ids,
        auto_send_messages=False,
    )


def _age_outbound_messages(case_id: int, minutes: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE messages
            SET created_at = datetime('now', ?)
            WHERE case_id = ?
              AND direction = 'outbound'
            """,
            (f"-{minutes} minutes", case_id),
        )
        conn.commit()


def _backdate_action_lock(
    case_id: int,
    supplier_id: int,
    action_key: str,
    minutes: int,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE negotiation_action_locks
            SET created_at = datetime('now', ?)
            WHERE case_id = ?
              AND supplier_id = ?
              AND action_key = ?
            """,
            (
                f"-{minutes} minutes",
                case_id,
                supplier_id,
                action_key,
            ),
        )
        conn.commit()


def _reminder_count(case_id: int, supplier_id: int) -> int:
    return repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="rfq_reminder",
    )


def test_abandoned_rfq_reminder_lock_is_recovered_after_restart(
    supplier_ids: dict[str, int],
) -> None:
    first_supplier_id = supplier_ids["email"]
    second_supplier_id = supplier_ids["whatsapp"]
    case_id = _create_case([first_supplier_id, second_supplier_id])

    simple_chat_service.start_negotiating_case(case_id)
    _age_outbound_messages(case_id, minutes=3)

    abandoned_action_key = (
        f"SEND_RFQ_REMINDER:{first_supplier_id}:1"
    )

    # Simulate the crash window: the first process committed the lock but the
    # computer stopped before it stored the reminder message.
    assert repo.acquire_action_lock(
        case_id=case_id,
        supplier_id=first_supplier_id,
        action_key=abandoned_action_key,
        action_type="SEND_RFQ_REMINDER",
    )

    # A fresh lock must still protect against concurrent duplicate execution.
    simple_chat_service.continue_negotiation_for_case(case_id)
    assert _reminder_count(case_id, first_supplier_id) == 0
    assert _reminder_count(case_id, second_supplier_id) == 1

    # Once the abandoned lock lease expires, a restarted worker may reclaim it.
    _backdate_action_lock(
        case_id=case_id,
        supplier_id=first_supplier_id,
        action_key=abandoned_action_key,
        minutes=10,
    )

    simple_chat_service.continue_negotiation_for_case(case_id)

    assert _reminder_count(case_id, first_supplier_id) == 1
    assert _reminder_count(case_id, second_supplier_id) == 1
