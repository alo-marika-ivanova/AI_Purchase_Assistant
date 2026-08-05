from __future__ import annotations

from app.db.repository import PurchasingRepository
from app.services import simple_chat_service
from app.services.case_service import create_case_from_detected_items


repo = PurchasingRepository()


def test_each_supplier_gets_exactly_one_rfq_scoped_to_their_own_items(
    supplier_ids: dict[str, int],
) -> None:
    """Item 1 -> supplier A only, item 2 -> both A and B, item 3 -> B only.
    Starting negotiation for the order must send exactly one RFQ to A
    (mentioning items 1+2, not 3) and exactly one RFQ to B (mentioning
    items 2+3, not 1) - not one RFQ per item.
    """
    supplier_a = supplier_ids["email"]
    supplier_b = supplier_ids["whatsapp"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Tanzanite (TAN)",
                "quantity": 40.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Blue sapphire (SA)",
                "quantity": 16.0,
                "supplier_ids": [supplier_a, supplier_b],
            },
            {
                "item_material": "Ruby (RBN)",
                "quantity": 10.0,
                "supplier_ids": [supplier_b],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    a_messages = repo.list_messages_for_case_supplier(case_id, supplier_a)
    b_messages = repo.list_messages_for_case_supplier(case_id, supplier_b)

    a_rfqs = [m for m in a_messages if m["message_type"] == "rfq"]
    b_rfqs = [m for m in b_messages if m["message_type"] == "rfq"]

    assert len(a_rfqs) == 1
    assert len(b_rfqs) == 1

    a_body = a_rfqs[0]["body"]
    b_body = b_rfqs[0]["body"]

    assert "Tanzanite" in a_body
    assert "Blue sapphire" in a_body
    assert "Ruby" not in a_body

    assert "Blue sapphire" in b_body
    assert "Ruby" in b_body
    assert "Tanzanite" not in b_body


def test_single_supplier_single_item_rfq_still_works(
    supplier_ids: dict[str, int],
) -> None:
    """An order that resolves to one item/one supplier must not accidentally
    trip the multi-item wording path."""
    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Tanzanite (TAN)",
                "quantity": 40.0,
                "supplier_ids": [supplier_ids["email"]],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    messages = repo.list_messages_for_case_supplier(
        case_id, supplier_ids["email"]
    )
    rfqs = [m for m in messages if m["message_type"] == "rfq"]

    assert len(rfqs) == 1
    assert "Tanzanite" in rfqs[0]["body"]
