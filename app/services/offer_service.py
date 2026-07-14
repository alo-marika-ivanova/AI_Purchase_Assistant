from __future__ import annotations

from app.db.repository import PurchasingRepository


repo = PurchasingRepository()


def add_offer(
    case_id: int,
    supplier_id: int,
    unit_price_usd: float,
    quantity: float | None = None,
    message_id: int | None = None,
    extraction_method: str = "manual",
    extraction_confidence: str = "human_verified",
    notes: str | None = None,
) -> int:
    if unit_price_usd <= 0:
        raise ValueError("Unit price must be greater than zero.")

    repo.ensure_supplier_linked_to_case(case_id, supplier_id)

    return repo.add_offer(
        case_id=case_id,
        supplier_id=supplier_id,
        unit_price_usd=unit_price_usd,
        quantity=quantity,
        message_id=message_id,
        extraction_method=extraction_method,
        extraction_confidence=extraction_confidence,
        notes=notes,
    )


def list_offers_for_case(case_id: int) -> list[dict]:
    return repo.list_offers_for_case(case_id)