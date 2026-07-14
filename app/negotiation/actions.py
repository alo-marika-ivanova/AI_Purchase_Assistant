from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class NegotiationActionType(StrEnum):
    SEND_RFQ = "SEND_RFQ"
    SEND_NO_RESPONSE_FOLLOWUP = "SEND_NO_RESPONSE_FOLLOWUP"
    SEND_CLARIFICATION_REQUEST = "SEND_CLARIFICATION_REQUEST"
    SEND_DISCOUNT_REQUEST = "SEND_DISCOUNT_REQUEST"
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