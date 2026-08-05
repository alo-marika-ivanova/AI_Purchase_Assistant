from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class NegotiationActionType(StrEnum):
    SEND_RFQ = "SEND_RFQ"
    SEND_NO_RESPONSE_FOLLOWUP = "SEND_NO_RESPONSE_FOLLOWUP"
    SEND_CLARIFICATION_REQUEST = "SEND_CLARIFICATION_REQUEST"
    SEND_DISCOUNT_REQUEST = "SEND_DISCOUNT_REQUEST"
    SEND_NEGOTIATION_NO_RESPONSE_REMINDER = (
        "SEND_NEGOTIATION_NO_RESPONSE_REMINDER"
    )
    MOVE_CASE_TO_BUYER_REVIEW = "MOVE_CASE_TO_BUYER_REVIEW"
    NO_ACTION = "NO_ACTION"


@dataclass(frozen=True)
class NegotiationAction:
    action_type: NegotiationActionType
    case_id: int
    supplier_id: int | None = None
    message_type: str | None = None
    llm_intent: str | None = None
    target_price_usd: float | None = None
    supplier_best_price_usd: float | None = None
    reason: str = ""
    # Per-item target/best-price breakdown for a supplier linked to more
    # than one case_item. When set, this - not the single scalar fields
    # above - is authoritative for message wording; the scalars are kept
    # populated (as a representative value) for logging and action-lock
    # keying only. Each entry: {case_item_id, item_material,
    # best_price_usd, target_price_usd}.
    item_targets: list[dict] | None = None