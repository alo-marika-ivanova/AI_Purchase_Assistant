from __future__ import annotations

import pytest

from app.db.repository import PurchasingRepository
from app.negotiation.states import SupplierState
from app.services.case_service import create_case
from app.services.simple_chat_service import record_supplier_message_simple


repo = PurchasingRepository()


@pytest.mark.parametrize(
    "supplier_message",
    [
        "Dear partner, black diamond costs 33 usd",
        "33 usd is the unit price for 1pcs of black diamond",
    ],
)
def test_clear_manual_rfq_response_saves_offer_without_review(
    supplier_ids: dict[str, int],
    supplier_message: str,
) -> None:
    supplier_id = supplier_ids["email"]
    case_id = create_case(
        item_material="Black Diamonds",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_id],
        auto_send_messages=False,
    )

    result = record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="manual",
        body=supplier_message,
    )

    supplier_state = repo.get_supplier_state(case_id, supplier_id)
    offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)

    assert result["saved_offer_id"] is not None
    assert result["review_item_id"] is None
    assert result["extraction"]["unit_price_usd"] == pytest.approx(33.0)
    assert result["extraction"]["method"] == (
        "deterministic_rfq_price_parser"
    )
    assert supplier_state is not None
    assert supplier_state["state"] == SupplierState.PRICE_EXTRACTED.value
    assert offer is not None
    assert float(offer["unit_price_usd"]) == pytest.approx(33.0)
    assert offer["extraction_method"] == (
        "deterministic_rfq_price_parser"
    )
