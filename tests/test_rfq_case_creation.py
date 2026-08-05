from __future__ import annotations

import pytest

from app.db.repository import PurchasingRepository
from app.services.case_service import create_case, create_case_from_detected_items


repo = PurchasingRepository()


def test_creates_one_case_holding_all_items(
    supplier_ids: dict[str, int],
) -> None:
    """Item 1 -> supplier A only, item 2 -> both A and B, item 3 -> B only.
    Expect exactly ONE case (the order), with three case_items, each linked
    to its own supplier subset.
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

    details = repo.get_case_details(case_id)
    assert len(details["items"]) == 3

    # The case's overall supplier list is the union of every item's list.
    assert {s["id"] for s in details["suppliers"]} == {supplier_a, supplier_b}

    items_by_name = {item["item_material"]: item for item in details["items"]}
    tan_suppliers = {s["id"] for s in items_by_name["Tanzanite (TAN)"]["suppliers"]}
    sa_suppliers = {s["id"] for s in items_by_name["Blue sapphire (SA)"]["suppliers"]}
    rbn_suppliers = {s["id"] for s in items_by_name["Ruby (RBN)"]["suppliers"]}

    assert tan_suppliers == {supplier_a}
    assert sa_suppliers == {supplier_a, supplier_b}
    assert rbn_suppliers == {supplier_b}


def test_a_supplier_linked_to_two_items_only_gets_those_items(
    supplier_ids: dict[str, int],
) -> None:
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Tanzanite (TAN)",
                "quantity": 40.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Ruby (RBN)",
                "quantity": 10.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    supplier_items = repo.list_case_items_for_supplier(case_id, supplier_a)
    assert {item["item_material"] for item in supplier_items} == {
        "Tanzanite (TAN)",
        "Ruby (RBN)",
    }


def test_case_quantity_is_sum_of_all_items(
    supplier_ids: dict[str, int],
) -> None:
    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Tanzanite (TAN)",
                "quantity": 40.0,
                "supplier_ids": [supplier_ids["email"]],
            },
            {
                "item_material": "Ruby (RBN)",
                "quantity": 10.0,
                "supplier_ids": [supplier_ids["email"]],
            },
        ],
        notes="",
    )

    case = repo.get_case_basic(case_id)
    assert case["quantity"] == 50.0
    assert case["item_material"].startswith("RFQ order (2 items):")


def test_single_item_uses_item_name_directly(
    supplier_ids: dict[str, int],
) -> None:
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

    case = repo.get_case_basic(case_id)
    assert case["item_material"] == "Tanzanite (TAN)"
    assert case["quantity"] == 40.0


def test_applies_shared_settings_to_the_case(
    supplier_ids: dict[str, int],
) -> None:
    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Tanzanite (TAN)",
                "quantity": 1.0,
                "supplier_ids": [supplier_ids["email"]],
            },
        ],
        notes="shared note",
        auto_send_messages=True,
        notify_buyer_on_human_review=True,
    )

    details = repo.get_case_details(case_id)
    assert details["case"]["notes"] == "shared note"
    assert details["case"]["auto_send_messages"] == 1
    assert details["case"]["notify_human_review_email"] == 1


def test_raises_when_no_items(supplier_ids: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        create_case_from_detected_items(items=[], notes="")


def test_raises_when_total_quantity_is_zero(
    supplier_ids: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        create_case_from_detected_items(
            items=[
                {
                    "item_material": "Tanzanite (TAN)",
                    "quantity": 0.0,
                    "supplier_ids": [supplier_ids["email"]],
                },
            ],
            notes="",
        )


def test_raises_when_no_item_has_any_supplier(
    supplier_ids: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        create_case_from_detected_items(
            items=[
                {
                    "item_material": "Tanzanite (TAN)",
                    "quantity": 1.0,
                    "supplier_ids": [],
                },
            ],
            notes="",
        )


def test_item_with_no_suppliers_still_appears_but_has_none(
    supplier_ids: dict[str, int],
) -> None:
    """One item can end up with no suppliers as long as at least one other
    item in the order has some - it should still show up (for visibility)
    rather than silently vanish."""
    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Tanzanite (TAN)",
                "quantity": 40.0,
                "supplier_ids": [supplier_ids["email"]],
            },
            {
                "item_material": "Ruby (RBN)",
                "quantity": 10.0,
                "supplier_ids": [],
            },
        ],
        notes="",
    )

    details = repo.get_case_details(case_id)
    items_by_name = {item["item_material"]: item for item in details["items"]}
    assert items_by_name["Ruby (RBN)"]["suppliers"] == []


def test_manually_created_case_has_no_items(
    supplier_ids: dict[str, int],
) -> None:
    case_id = create_case(
        item_material="Tanzanite (TAN)",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_ids["email"]],
    )

    assert repo.list_case_items(case_id) == []
    assert repo.list_case_items_for_supplier(case_id, supplier_ids["email"]) == []
