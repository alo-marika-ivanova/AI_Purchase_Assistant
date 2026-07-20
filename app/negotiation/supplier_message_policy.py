from __future__ import annotations

from dataclasses import dataclass


_ALLOWED_ACTIONS = {
    "SAVE_OFFER",
    "SAVE_PROVISIONAL_OFFER_AND_WAIT",
    "ASK_PRICE_CLARIFICATION",
    "WAIT_FOR_SUPPLIER",
    "ANSWER_FROM_CASE_AND_REPEAT_REQUEST",
    "MARK_REJECTED",
    "RECORD_PRICE_REFUSAL",
    "PAUSE_FOR_REVIEW",
}


@dataclass(frozen=True)
class SupplierMessagePolicyDecision:
    """Deterministic business decision based on structured interpretation."""

    action: str
    reason: str


def _has_usable_unit_price(analysis: dict) -> bool:
    try:
        price = float(analysis.get("unit_price_usd"))
    except (TypeError, ValueError):
        return False

    return (
        price > 0
        and str(analysis.get("currency") or "").upper() == "USD"
        and str(analysis.get("price_basis") or "").upper() == "UNIT"
        and bool(analysis.get("is_price_clear"))
        and bool(analysis.get("is_currency_clear"))
        and not bool(analysis.get("has_multiple_prices"))
        and not bool(analysis.get("is_conditional"))
    )


def decide_supplier_message_policy(
    analysis: dict,
) -> SupplierMessagePolicyDecision:
    """Convert semantic facts into an allowed workflow action.

    The LLM or deterministic analyzer interprets language. This policy function
    owns the business authority and therefore cannot be bypassed by an LLM's
    recommended action.
    """
    if (
        bool(analysis.get("requires_human_review"))
        or bool(analysis.get("contains_risky_topic"))
    ):
        return SupplierMessagePolicyDecision(
            action="PAUSE_FOR_REVIEW",
            reason="Risk or explicit human-review requirement has priority.",
        )

    offer_status = str(analysis.get("offer_status") or "NONE").upper()
    price_certainty = str(
        analysis.get("price_certainty") or "NONE"
    ).upper()
    usable_price = _has_usable_unit_price(analysis)

    if offer_status == "PROVISIONAL" or price_certainty == "TENTATIVE":
        if usable_price:
            return SupplierMessagePolicyDecision(
                action="SAVE_PROVISIONAL_OFFER_AND_WAIT",
                reason=(
                    "A usable but explicitly tentative unit price may be stored "
                    "as provisional and must remain excluded from comparison."
                ),
            )
        return SupplierMessagePolicyDecision(
            action="ASK_PRICE_CLARIFICATION",
            reason="Tentative price information is not structurally usable.",
        )

    analyzer_action = str(
        analysis.get("recommended_action") or ""
    ).upper()

    if analyzer_action == "SAVE_OFFER":
        if usable_price:
            return SupplierMessagePolicyDecision(
                action="SAVE_OFFER",
                reason="The analyzer returned a structurally valid confirmed offer.",
            )
        return SupplierMessagePolicyDecision(
            action="ASK_PRICE_CLARIFICATION",
            reason="A confirmed offer was suggested without a usable unit price.",
        )

    if analyzer_action in _ALLOWED_ACTIONS:
        return SupplierMessagePolicyDecision(
            action=analyzer_action,
            reason="The analyzer action passed deterministic policy validation.",
        )

    return SupplierMessagePolicyDecision(
        action="PAUSE_FOR_REVIEW",
        reason="No safe workflow action could be determined.",
    )
