from __future__ import annotations

from typing import Literal, TypedDict


PriceCertainty = Literal["NONE", "TENTATIVE", "CONFIRMED"]
SupplierCommitment = Literal["NONE", "WILL_VERIFY", "CONFIRMED"]
OfferStatus = Literal["NONE", "PROVISIONAL", "CONFIRMED"]


class StructuredSupplierMessageAnalysis(TypedDict, total=False):
    """Stable semantic fields shared by LLM and deterministic analyzers.

    The classifier may return many diagnostic fields, but these dimensions are
    the business-facing contract consumed by the workflow policy.
    """

    price_certainty: PriceCertainty
    supplier_commitment: SupplierCommitment
    pending_supplier_action: str | None
    offer_status: OfferStatus


def add_structured_dimensions(result: dict) -> dict:
    """Return a copy with normalized business-facing semantic dimensions."""
    normalized = dict(result)

    action = str(normalized.get("recommended_action") or "").upper()
    category = str(normalized.get("message_category") or "").upper()

    raw_certainty = str(normalized.get("price_certainty") or "").upper()
    if raw_certainty not in {"NONE", "TENTATIVE", "CONFIRMED"}:
        if action == "SAVE_PROVISIONAL_OFFER_AND_WAIT" or category == "TENTATIVE_PRICE":
            raw_certainty = "TENTATIVE"
        elif action == "SAVE_OFFER":
            raw_certainty = "CONFIRMED"
        else:
            raw_certainty = "NONE"

    raw_commitment = str(normalized.get("supplier_commitment") or "").upper()
    if raw_commitment not in {"NONE", "WILL_VERIFY", "CONFIRMED"}:
        if action == "SAVE_PROVISIONAL_OFFER_AND_WAIT" or category == "TENTATIVE_PRICE":
            raw_commitment = "WILL_VERIFY"
        elif action == "SAVE_OFFER":
            raw_commitment = "CONFIRMED"
        else:
            raw_commitment = "NONE"

    raw_offer_status = str(normalized.get("offer_status") or "").upper()
    if raw_offer_status not in {"NONE", "PROVISIONAL", "CONFIRMED"}:
        if action == "SAVE_PROVISIONAL_OFFER_AND_WAIT":
            raw_offer_status = "PROVISIONAL"
        elif action == "SAVE_OFFER":
            raw_offer_status = "CONFIRMED"
        else:
            raw_offer_status = "NONE"

    pending_action = normalized.get("pending_supplier_action")
    if pending_action is not None:
        pending_action = str(pending_action).strip() or None

    if raw_commitment == "WILL_VERIFY" and not pending_action:
        pending_action = "Supplier will verify and confirm the price."

    normalized.update(
        {
            "price_certainty": raw_certainty,
            "supplier_commitment": raw_commitment,
            "pending_supplier_action": pending_action,
            "offer_status": raw_offer_status,
        }
    )
    return normalized
