from __future__ import annotations

import pytest

from app.db.database import get_connection
from app.db.repository import PurchasingRepository
from app.llm.rfq_tentative_price_safeguard import (
    extract_tentative_rfq_unit_price,
)
from app.llm.supplier_message_classifier import (
    analyze_supplier_message_with_ollama,
)
from app.negotiation.states import SupplierState
from app.services.case_service import create_case
from app.services.recommendation_service import get_offer_recommendation
from app.services.simple_chat_service import (
    build_supplier_overview,
    continue_negotiation_for_case,
    record_supplier_message_simple,
    start_negotiating_case,
)


repo = PurchasingRepository()


TENTATIVE_MESSAGE = (
    "dear partner. Thank you for reaching and sorry for a delayed response. "
    "I will check with my supervisor, but I am almost sure the topaz sky "
    "costs 20 usd."
)


def _create_topaz_case(supplier_id: int) -> int:
    return create_case(
        item_material="Topaz Sky",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_id],
        auto_send_messages=False,
    )


def _offer_statuses(case_id: int, supplier_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, unit_price_usd, status
            FROM offers
            WHERE case_id = ?
              AND supplier_id = ?
            ORDER BY id
            """,
            (case_id, supplier_id),
        ).fetchall()
    return [dict(row) for row in rows]


def test_tentative_price_is_structured_without_calling_llm() -> None:
    analysis = analyze_supplier_message_with_ollama(
        message_body=TENTATIVE_MESSAGE,
        case_data={"item_material": "Topaz Sky", "quantity": 1.0},
        conversation_stage="RFQ",
        supplier_state=SupplierState.AWAITING_RESPONSE.value,
    )

    assert analysis["provider"] == "deterministic"
    assert analysis["message_category"] == "TENTATIVE_PRICE"
    assert analysis["recommended_action"] == (
        "SAVE_PROVISIONAL_OFFER_AND_WAIT"
    )
    assert analysis["unit_price_usd"] == pytest.approx(20.0)
    assert analysis["price_certainty"] == "TENTATIVE"
    assert analysis["supplier_commitment"] == "WILL_VERIFY"
    assert analysis["offer_status"] == "PROVISIONAL"
    assert analysis["requires_human_review"] is False


def test_tentative_price_is_saved_but_excluded_from_ranking(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id = _create_topaz_case(supplier_id)
    start_negotiating_case(case_id)

    result = record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="manual",
        body=TENTATIVE_MESSAGE,
    )

    state = repo.get_supplier_state(case_id, supplier_id)
    provisional = repo.get_latest_provisional_offer_for_case_supplier(
        case_id,
        supplier_id,
    )

    assert result["saved_offer_id"] is not None
    assert result["review_item_id"] is None
    assert result["extraction"]["offer_status"] == "provisional"
    assert state is not None
    assert state["state"] == SupplierState.AWAITING_PRICE_CONFIRMATION.value
    assert provisional is not None
    assert float(provisional["unit_price_usd"]) == pytest.approx(20.0)

    # Confirmed-offer queries, comparison and winner recommendation must ignore it.
    assert repo.get_best_offer_for_case_supplier(case_id, supplier_id) is None
    assert repo.list_offers_for_case(case_id) == []
    assert get_offer_recommendation(case_id) is None

    overview = build_supplier_overview(case_id)
    assert overview[0]["best_unit_price_usd"] is None
    assert overview[0]["provisional_unit_price_usd"] == pytest.approx(20.0)


def test_provisional_price_is_acknowledged_once_and_then_waits(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id = _create_topaz_case(supplier_id)
    start_negotiating_case(case_id)

    record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="manual",
        body=TENTATIVE_MESSAGE,
    )

    first_cycle = continue_negotiation_for_case(case_id)
    second_cycle = continue_negotiation_for_case(case_id)

    assert [action["action"] for action in first_cycle["actions"]] == [
        "SEND_PROVISIONAL_PRICE_ACKNOWLEDGEMENT"
    ]
    assert second_cycle["actions"] == []
    assert repo.count_supplier_outbound_message_type(
        case_id,
        supplier_id,
        "provisional_price_acknowledgement",
    ) == 1

    messages = repo.list_messages_for_case_supplier(case_id, supplier_id)
    acknowledgement = [
        message
        for message in messages
        if message.get("message_type") == "provisional_price_acknowledgement"
    ][0]
    normalized_body = acknowledgement["body"].lower()
    assert "20" in normalized_body
    assert "confirm" in normalized_body

    state = repo.get_supplier_state(case_id, supplier_id)
    assert state is not None
    assert state["state"] == SupplierState.AWAITING_PRICE_CONFIRMATION.value


def test_contextual_confirmation_creates_confirmed_offer(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id = _create_topaz_case(supplier_id)
    start_negotiating_case(case_id)

    record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="manual",
        body=TENTATIVE_MESSAGE,
    )
    continue_negotiation_for_case(case_id)

    confirmation = record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="manual",
        body="Confirmed.",
    )

    confirmed = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    state = repo.get_supplier_state(case_id, supplier_id)
    statuses = _offer_statuses(case_id, supplier_id)

    assert confirmation["analysis"]["provider"] == "deterministic_context"
    assert confirmation["extraction"]["offer_status"] == "confirmed"
    assert confirmed is not None
    assert float(confirmed["unit_price_usd"]) == pytest.approx(20.0)
    assert state is not None
    assert state["state"] == SupplierState.PRICE_EXTRACTED.value
    assert [row["status"] for row in statuses] == ["superseded", "active"]


def test_later_confirmed_price_supersedes_provisional_value(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id = _create_topaz_case(supplier_id)
    start_negotiating_case(case_id)

    record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="manual",
        body=TENTATIVE_MESSAGE,
    )

    confirmed_result = record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="manual",
        body="22 usd is the unit price for 1pcs of topaz sky",
    )

    confirmed = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    statuses = _offer_statuses(case_id, supplier_id)

    assert confirmed_result["extraction"]["offer_status"] == "confirmed"
    assert confirmed is not None
    assert float(confirmed["unit_price_usd"]) == pytest.approx(22.0)
    assert [row["status"] for row in statuses] == ["superseded", "active"]
    assert [float(row["unit_price_usd"]) for row in statuses] == [20.0, 22.0]


def test_risky_tentative_price_is_not_handled_deterministically() -> None:
    price = extract_tentative_rfq_unit_price(
        message_body=(
            "I think the Topaz Sky price is 20 USD, but only with a 50% deposit."
        ),
        case_data={"item_material": "Topaz Sky", "quantity": 1.0},
    )
    assert price is None


def test_policy_engine_overrides_unsafe_analyzer_recommendation() -> None:
    from app.negotiation.supplier_message_policy import (
        decide_supplier_message_policy,
    )

    decision = decide_supplier_message_policy(
        {
            "recommended_action": "SAVE_PROVISIONAL_OFFER_AND_WAIT",
            "offer_status": "PROVISIONAL",
            "price_certainty": "TENTATIVE",
            "unit_price_usd": 20.0,
            "currency": "USD",
            "price_basis": "UNIT",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "contains_risky_topic": True,
            "requires_human_review": True,
        }
    )

    assert decision.action == "PAUSE_FOR_REVIEW"
