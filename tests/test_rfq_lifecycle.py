from __future__ import annotations

import pytest

from app.db.database import get_connection
from app.db.repository import PurchasingRepository
from app.negotiation.states import CaseState, SupplierState
from app.services.case_service import create_case
from app.services import simple_chat_service


repo = PurchasingRepository()


def _create_case(supplier_ids: list[int]) -> int:
    return create_case(
        item_material="test ruby",
        quantity=10.0,
        notes="RFQ lifecycle regression test",
        supplier_ids=supplier_ids,
        auto_send_messages=False,
    )


def _outbound_message_count(
    case_id: int,
    supplier_id: int,
    message_type: str,
) -> int:
    return repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type=message_type,
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


def _clear_offer_analysis(price: float) -> dict:
    return {
        "success": True,
        "provider": "test",
        "model": "deterministic-test",
        "message_category": "CLEAR_PRICE_OFFER",
        "recommended_action": "SAVE_OFFER",
        "safe_for_automation": True,
        "requires_human_review": False,
        "confidence": "high",
        "stated_price_amount": price,
        "unit_price_usd": price,
        "currency": "USD",
        "price_basis": "UNIT",
        "is_price_clear": True,
        "is_currency_clear": True,
        "has_multiple_prices": False,
        "is_conditional": False,
        "condition_summary": None,
        "supplier_will_reply_later": False,
        "supplier_refused": False,
        "supplier_accepts_target": False,
        "question_can_be_answered_from_case": False,
        "reason": "Deterministic test offer.",
        "suggested_clarification_question": None,
        "suggested_buyer_reply": None,
        "raw_result": {},
        "error": None,
    }


def test_start_sends_one_rfq_per_supplier_without_duplicates(
    supplier_ids: dict[str, int],
) -> None:
    selected = [supplier_ids["email"], supplier_ids["whatsapp"]]
    case_id = _create_case(selected)

    first_result = simple_chat_service.start_negotiating_case(case_id)

    assert [
        action["action"] for action in first_result["actions"]
    ].count("SEND_RFQ") == 2

    # Starting/continuing the same case again must not create duplicate RFQs.
    simple_chat_service.start_negotiating_case(case_id)

    for supplier_id in selected:
        assert _outbound_message_count(
            case_id,
            supplier_id,
            "rfq",
        ) == 1


def test_rfq_reminder_is_not_early_and_is_sent_only_once(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id = _create_case([supplier_id])
    simple_chat_service.start_negotiating_case(case_id)

    immediate_result = simple_chat_service.continue_negotiation_for_case(case_id)
    assert "SEND_RFQ_REMINDER" not in {
        action["action"] for action in immediate_result["actions"]
    }

    _age_outbound_messages(case_id, minutes=3)

    reminder_result = simple_chat_service.continue_negotiation_for_case(case_id)
    assert [
        action["action"] for action in reminder_result["actions"]
    ].count("SEND_RFQ_REMINDER") == 1

    second_result = simple_chat_service.continue_negotiation_for_case(case_id)
    assert "SEND_RFQ_REMINDER" not in {
        action["action"] for action in second_result["actions"]
    }
    assert _outbound_message_count(
        case_id,
        supplier_id,
        "rfq_reminder",
    ) == 1


def test_supplier_becomes_no_response_after_testing_deadline(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id = _create_case([supplier_id])
    simple_chat_service.start_negotiating_case(case_id)

    _age_outbound_messages(case_id, minutes=3)
    simple_chat_service.continue_negotiation_for_case(case_id)

    # Make both the RFQ and reminder older than the four-minute RFQ deadline.
    _age_outbound_messages(case_id, minutes=5)
    simple_chat_service.continue_negotiation_for_case(case_id)

    supplier_state = repo.get_supplier_state(case_id, supplier_id)
    case_data = repo.get_case_basic(case_id)

    assert supplier_state is not None
    assert supplier_state["state"] == SupplierState.NO_RESPONSE.value
    assert case_data is not None
    assert case_data["status"] == CaseState.NO_VALID_OFFERS.value


def test_direct_late_response_reopens_supplier_and_saves_offer(
    supplier_ids: dict[str, int],
    monkeypatch,
) -> None:
    supplier_id = supplier_ids["email"]
    case_id = _create_case([supplier_id])
    simple_chat_service.start_negotiating_case(case_id)

    _age_outbound_messages(case_id, minutes=3)
    simple_chat_service.continue_negotiation_for_case(case_id)
    _age_outbound_messages(case_id, minutes=5)
    simple_chat_service.continue_negotiation_for_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _clear_offer_analysis(42.0),
    )

    # Constructing a new repository object simulates a fresh process reading
    # the state persisted by the previous worker process.
    restarted_repo = PurchasingRepository()
    assert restarted_repo.get_supplier_state(
        case_id,
        supplier_id,
    )["state"] == SupplierState.NO_RESPONSE.value

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="Our price is 42 USD per unit.",
    )

    supplier_state = restarted_repo.get_supplier_state(case_id, supplier_id)
    case_data = restarted_repo.get_case_basic(case_id)

    assert result["saved_offer_id"] is not None
    assert supplier_state is not None
    assert supplier_state["state"] == SupplierState.PRICE_EXTRACTED.value
    assert case_data is not None
    assert case_data["status"] == CaseState.COLLECTING_OFFERS.value


def test_worker_keeps_no_valid_offer_case_eligible_for_late_email(
    supplier_ids: dict[str, int],
) -> None:
    """This test exposes the current delayed-email worker bug."""
    supplier_id = supplier_ids["email"]
    case_id = _create_case([supplier_id])
    simple_chat_service.start_negotiating_case(case_id)

    _age_outbound_messages(case_id, minutes=3)
    simple_chat_service.continue_negotiation_for_case(case_id)
    _age_outbound_messages(case_id, minutes=5)
    simple_chat_service.continue_negotiation_for_case(case_id)

    case_data = repo.get_case_basic(case_id)
    assert case_data is not None
    assert case_data["status"] == CaseState.NO_VALID_OFFERS.value

    worker_case_ids = {
        int(case["id"])
        for case in repo.list_cases_for_transport_worker()
    }

    # A restarted worker must still poll this case so a delayed supplier email
    # can be imported and passed to record_supplier_message_simple().
    assert case_id in worker_case_ids


def test_worker_keeps_limited_competition_case_eligible_for_late_email(
    supplier_ids: dict[str, int],
    monkeypatch,
) -> None:
    """A late second offer must still be importable after RFQ timeout."""
    first_supplier_id = supplier_ids["email"]
    second_supplier_id = supplier_ids["whatsapp"]
    case_id = _create_case([first_supplier_id, second_supplier_id])
    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _clear_offer_analysis(42.0),
    )

    simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=first_supplier_id,
        channel="email",
        body="Our price is 42 USD per unit.",
    )

    _age_outbound_messages(case_id, minutes=3)
    simple_chat_service.continue_negotiation_for_case(case_id)
    _age_outbound_messages(case_id, minutes=5)
    simple_chat_service.continue_negotiation_for_case(case_id)

    case_data = repo.get_case_basic(case_id)
    assert case_data is not None
    assert case_data["status"] == CaseState.LIMITED_COMPETITION.value

    worker_case_ids = {
        int(case["id"])
        for case in repo.list_cases_for_transport_worker()
    }
    assert case_id in worker_case_ids


@pytest.mark.parametrize(
    "terminal_status",
    [
        CaseState.WINNER_NOTIFIED.value,
        CaseState.CLOSED.value,
        CaseState.CANCELLED.value,
    ],
)
def test_worker_still_excludes_completed_cases(
    supplier_ids: dict[str, int],
    terminal_status: str,
) -> None:
    case_id = _create_case([supplier_ids["email"]])
    repo.update_case_status_with_event(
        case_id=case_id,
        status=terminal_status,
        event_type="test_terminal_state",
        details="Regression test terminal state.",
    )

    worker_case_ids = {
        int(case["id"])
        for case in repo.list_cases_for_transport_worker()
    }
    assert case_id not in worker_case_ids
