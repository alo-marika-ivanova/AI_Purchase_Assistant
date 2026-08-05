from __future__ import annotations

from typing import Iterable

from app.db.repository import PurchasingRepository
from app.services.supplier_catalog_service import (
    material_catalog_is_available,
    material_exists,
)
from app.services.supplier_catalog_service import (
    material_catalog_is_available,
    material_exists,
)

repo = PurchasingRepository()


def list_active_suppliers() -> list[dict]:
    return repo.list_active_suppliers()


def list_cases() -> list[dict]:
    return repo.list_cases()


def create_case(
    item_material: str,
    quantity: float,
    notes: str,
    supplier_ids: Iterable[int],
    auto_send_messages: bool = False,
    notify_buyer_on_human_review: bool = False,
) -> int:
    clean_item = item_material.strip()



    if not clean_item:
        raise ValueError("Item/material is required.")

    if material_catalog_is_available() and not material_exists(clean_item):
        raise ValueError(
            "Item/material must be selected from the imported supplier-material database."
        )

    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero.")

    supplier_ids = list(dict.fromkeys(int(sid) for sid in supplier_ids))

    if not supplier_ids:
        raise ValueError("Select at least one supplier.")

    return repo.create_case(
        item_material=clean_item,
        quantity=quantity,
        notes=notes,
        supplier_ids=supplier_ids,
        auto_send_messages=auto_send_messages,
        notify_buyer_on_human_review=notify_buyer_on_human_review,
    )


def get_case_details(case_id: int) -> dict | None:
    return repo.get_case_details(case_id)


def _build_order_item_summary(item_names: list[str]) -> str:
    if len(item_names) == 1:
        return item_names[0]

    # The trailing period matters: without it, a label ending in the last
    # item's own "(CODE)" would make _slugify_item_code pick that code as
    # the whole order's case-number prefix.
    return f"RFQ order ({len(item_names)} items): " + ", ".join(item_names) + "."


def create_case_from_detected_items(
    items: Iterable[dict],
    notes: str,
    auto_send_messages: bool = False,
    notify_buyer_on_human_review: bool = False,
) -> int:
    """Create one case for an uploaded RFQ order, holding one item
    ("subcase") per detected stone or brilliant size/color bucket.

    Each item dict must have "item_material", "quantity", and "supplier_ids"
    - the suppliers selected to quote that specific item. Different items
    can have different, overlapping supplier sets (see case_item_suppliers).
    The case's own supplier list is the union of all of them, so every
    involved supplier gets exactly one combined outbound message listing
    only the items they were linked to (see communication_writer.py) - not
    one message per item.

    Each item still negotiates independently once replies come in (its own
    target price, rounds, and winner, compared across whichever suppliers
    are linked to that specific item), so different items in the same
    order can end up awarded to different suppliers even though a shared
    supplier discussed several of them in one conversation.
    """
    items = list(items)

    if not items:
        raise ValueError("At least one item is required.")

    item_names = [item["item_material"] for item in items]
    total_quantity = sum(float(item["quantity"]) for item in items)

    if total_quantity <= 0:
        raise ValueError("Total quantity must be greater than zero.")

    all_supplier_ids = list(
        dict.fromkeys(
            int(supplier_id)
            for item in items
            for supplier_id in item["supplier_ids"]
        )
    )

    if not all_supplier_ids:
        raise ValueError("Select at least one supplier for at least one item.")

    # Bypasses case_service.create_case's catalog-membership check: the
    # summary label is synthesized from multiple items and will never
    # match a single catalog goods_name.
    case_id = repo.create_case(
        item_material=_build_order_item_summary(item_names),
        quantity=total_quantity,
        notes=notes,
        supplier_ids=all_supplier_ids,
        auto_send_messages=auto_send_messages,
        notify_buyer_on_human_review=notify_buyer_on_human_review,
    )

    for item in items:
        case_item_id = repo.add_case_item(
            case_id=case_id,
            item_material=item["item_material"],
            quantity=item["quantity"],
            source_description=item.get("description"),
        )

        for supplier_id in item["supplier_ids"]:
            repo.add_case_item_supplier(case_item_id, int(supplier_id))

    return case_id