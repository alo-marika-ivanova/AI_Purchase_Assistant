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
    )


def get_case_details(case_id: int) -> dict | None:
    return repo.get_case_details(case_id)