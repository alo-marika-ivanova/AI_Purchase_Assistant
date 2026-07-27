from __future__ import annotations

import re

from app.llm.supplier_message_analysis import add_structured_dimensions


# This module is a deterministic safety net for NEGOTIATION-stage price
# refusals, not a replacement for the LLM classifier. Per the negotiation
# policy, the LLM has authority over SOFT_REFUSAL / FIRM_REFUSAL / HARD_STOP /
# SUPPLY_UNAVAILABLE classification; regex is only used in two narrow ways:
#
# 1. Escalate-only validation: if the LLM already returned a refusal category
#    but the raw text unambiguously asks to stop negotiating, escalate to
#    HARD_STOP. This never downgrades or overrides any other LLM judgement
#    (price, risk topic, etc.) -- it can only make the outcome safer.
# 2. Fallback classification: only when the LLM call itself failed, so an
#    obvious refusal during a provider outage does not need to escalate all
#    the way to human review.

_HARD_STOP_PATTERN = re.compile(
    r"\b(?:"
    r"do\s+not\s+ask\s+(?:us\s+)?again|"
    r"don'?t\s+ask\s+(?:us\s+)?again|"
    r"please\s+stop\s+asking|"
    r"stop\s+asking\s+(?:us\s+)?(?:for|about)?|"
    r"do\s+not\s+contact\s+us\s+again|"
    r"don'?t\s+contact\s+us\s+again|"
    r"please\s+do\s+not\s+negotiate\s+(?:this\s+)?(?:price\s+)?(?:any\s*)?"
    r"(?:further|more)|"
    r"no\s+further\s+(?:discussion|negotiation)s?|"
    r"we\s+will\s+not\s+negotiate\s+(?:this\s+)?(?:any\s*)?(?:further|more)|"
    r"consider\s+(?:this|the)\s+(?:matter|discussion)\s+closed|"
    r"this\s+(?:discussion|topic|matter)\s+is\s+closed"
    r")\b",
    re.IGNORECASE,
)

_SUPPLY_UNAVAILABLE_PATTERN = re.compile(
    r"\b(?:"
    r"(?:no\s+longer|not)\s+(?:able|in\s+a\s+position)\s+to\s+supply|"
    r"out\s+of\s+stock|"
    r"no\s+longer\s+available|"
    r"(?:sold|all)\s+out|"
    r"cannot\s+supply\s+this|"
    r"we\s+(?:can\s*not|cannot|can'?t)\s+(?:provide|supply|deliver)\s+"
    r"(?:this|it|the\s+item)"
    r")\b",
    re.IGNORECASE,
)

_FIRM_REFUSAL_PATTERN = re.compile(
    r"\b(?:"
    r"absolutely\s+(?:not|impossible|final)|"
    r"definitely\s+(?:not|final)|"
    r"under\s+no\s+circumstances|"
    r"no\s+margin\s+(?:whatsoever|at\s+all)"
    r")\b",
    re.IGNORECASE,
)

_SOFT_REFUSAL_PATTERN = re.compile(
    r"\b(?:"
    r"final\s+price|no\s+more\s+margin|no\s+margin\b|"
    r"cannot\s+reach|can'?t\s+reach|"
    r"unable\s+to\s+(?:reach|match|reduce)|"
    r"not\s+possible\s+to\s+(?:reduce|reach)|"
    r"impossible\s+to\s+(?:reach|go|match)"
    r")\b",
    re.IGNORECASE,
)


def text_requests_hard_stop(message_body: str) -> bool:
    """Return True only when the text unambiguously asks to stop negotiating.

    Used purely as an escalation check on top of an already-successful LLM
    classification -- never to invent a refusal that the LLM did not find.
    """
    text = (message_body or "").strip()
    return bool(text and _HARD_STOP_PATTERN.search(text))


def classify_refusal_strength_by_regex(message_body: str) -> str | None:
    """Best-effort refusal classification used only when the LLM call failed.

    Returns one of "HARD_STOP", "SUPPLY_UNAVAILABLE", "FIRM_REFUSAL",
    "SOFT_REFUSAL", or None if nothing recognizable was found (in which case
    the caller should fall back to the existing safe human-review pause).
    """
    text = (message_body or "").strip()
    if not text:
        return None

    if _HARD_STOP_PATTERN.search(text):
        return "HARD_STOP"
    if _SUPPLY_UNAVAILABLE_PATTERN.search(text):
        return "SUPPLY_UNAVAILABLE"
    if _FIRM_REFUSAL_PATTERN.search(text):
        return "FIRM_REFUSAL"
    if _SOFT_REFUSAL_PATTERN.search(text):
        return "SOFT_REFUSAL"

    return None


_ACTION_BY_CATEGORY = {
    "HARD_STOP": "STOP_NEGOTIATION_HARD",
    "SUPPLY_UNAVAILABLE": "MARK_REJECTED",
    "FIRM_REFUSAL": "RECORD_PRICE_REFUSAL",
    "SOFT_REFUSAL": "RECORD_PRICE_REFUSAL",
}

_REASON_BY_CATEGORY = {
    "HARD_STOP": (
        "The LLM classifier was unavailable. A deterministic fallback "
        "detected an explicit request to stop price negotiation."
    ),
    "SUPPLY_UNAVAILABLE": (
        "The LLM classifier was unavailable. A deterministic fallback "
        "detected that the supplier cannot supply the item."
    ),
    "FIRM_REFUSAL": (
        "The LLM classifier was unavailable. A deterministic fallback "
        "detected a strongly worded price refusal."
    ),
    "SOFT_REFUSAL": (
        "The LLM classifier was unavailable. A deterministic fallback "
        "detected an ordinary price refusal."
    ),
}


def build_deterministic_refusal_result(category: str) -> dict:
    """Build a classifier-shaped result for a regex-fallback refusal category.

    Only used when the LLM call itself failed during the NEGOTIATION stage;
    see analyze_supplier_message_with_ollama's except-block.
    """
    if category not in _ACTION_BY_CATEGORY:
        raise ValueError(f"Unsupported fallback refusal category: {category!r}")

    return add_structured_dimensions(
        {
            "success": True,
            "provider": "deterministic_refusal_fallback",
            "model": None,
            "message_category": category,
            "recommended_action": _ACTION_BY_CATEGORY[category],
            "safe_for_automation": True,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "confidence": "medium",
            "stated_price_amount": None,
            "unit_price_usd": None,
            "currency": None,
            "price_basis": "NONE",
            "is_price_clear": False,
            "is_currency_clear": False,
            "has_multiple_prices": False,
            "is_conditional": False,
            "condition_summary": None,
            "supplier_will_reply_later": False,
            "supplier_refused": category in {"SOFT_REFUSAL", "FIRM_REFUSAL"},
            "supplier_accepts_target": False,
            "question_can_be_answered_from_case": False,
            "price_certainty": "NONE",
            "supplier_commitment": "NONE",
            "pending_supplier_action": None,
            "offer_status": "NONE",
            "reason": _REASON_BY_CATEGORY[category],
            "suggested_clarification_question": None,
            "suggested_buyer_reply": None,
            "raw_result": None,
            "error": None,
        }
    )
