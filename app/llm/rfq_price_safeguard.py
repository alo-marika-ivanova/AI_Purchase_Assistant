from __future__ import annotations

import re
from typing import Any

from app.negotiation.common_reply_policy import (
    extract_common_explicit_prices,
)


_RISK_PATTERN = re.compile(
    r"\b(?:"
    r"deposit|prepayment|pre-payment|payment\s+term|"
    r"pay\s+in\s+advance|advance\s+payment|cash\s+payment|"
    r"delivery|lead\s+time|ship(?:ping)?|"
    r"different|alternative|instead|substitute|change(?:d)?|"
    r"specification|specifications|quality|certificate|certification|"
    r"return|refund|reject(?:ion|ed)?|"
    r"legal|liability|contract|penalty|"
    r"customs|sanction|compliance|"
    r"confidential|confidentiality|exclusive|exclusivity|"
    r"dispute|claim|"
    r"call\s+me|phone\s+me|telephone|video\s+call"
    r")\b",
    re.IGNORECASE,
)

_CONDITION_PATTERN = re.compile(
    r"\b(?:total|altogether|for\s+all|range|between|"
    r"only\s+if|provided\s+that|subject\s+to|"
    r"above|over|at\s+least|minimum|min\.?|more\s+than)\b",
    re.IGNORECASE,
)

_ALLOWED_WORDS = {
    "a", "about", "an", "approximately", "are", "at", "available",
    "best", "can", "carat", "carats", "cost", "costs", "ct",
    "dear", "dollar", "dollars", "do", "each", "final", "for",
    "hello", "hi", "is", "it", "of", "offer", "offered", "our",
    "partner", "pc", "pcs", "per", "piece", "pieces", "price",
    "quote", "quotation", "stone", "stones", "thank", "thanks",
    "the", "this", "unit", "us", "usd", "we",
}

_NUMBER_PATTERN = re.compile(r"\b\d+(?:[.,]\d{1,4})?\b")
_WORD_PATTERN = re.compile(r"[a-zA-Z]+")


def _singular_variants(word: str) -> set[str]:
    variants = {word}

    if len(word) > 3 and word.endswith("ies"):
        variants.add(word[:-3] + "y")
    if len(word) > 3 and word.endswith("es"):
        variants.add(word[:-2])
    if len(word) > 2 and word.endswith("s"):
        variants.add(word[:-1])

    return variants


def _safe_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def extract_safe_simple_rfq_unit_price(
    message_body: str,
    case_data: dict,
) -> float | None:
    """Extract only a single objectively safe RFQ unit price.

    This is intentionally conservative. Messages containing questions,
    conditions, multiple prices, totals, changed specifications or other
    commercial topics are left to the LLM and human-review workflow.
    """
    text = (message_body or "").strip()

    if not text or "?" in text:
        return None
    if _RISK_PATTERN.search(text):
        return None
    if _CONDITION_PATTERN.search(text):
        return None

    prices = extract_common_explicit_prices(text)
    if len(prices) != 1:
        return None

    price = float(prices[0])
    if price <= 0:
        return None

    requested_quantity = _safe_float(case_data.get("quantity"))
    item_text = str(case_data.get("item_material") or "").lower()

    allowed_words = set(_ALLOWED_WORDS)
    for item_word in _WORD_PATTERN.findall(item_text):
        allowed_words.update(_singular_variants(item_word.lower()))

    message_words = {
        word.lower()
        for word in _WORD_PATTERN.findall(text)
    }
    if not message_words.issubset(allowed_words):
        return None

    item_numbers = {
        number.replace(",", ".")
        for number in _NUMBER_PATTERN.findall(item_text)
    }

    for raw_number in _NUMBER_PATTERN.findall(text):
        value = _safe_float(raw_number)
        if value is None:
            return None
        if abs(value - price) <= 0.005:
            continue
        if raw_number.replace(",", ".") in item_numbers:
            continue
        if (
            requested_quantity is not None
            and abs(value - requested_quantity) <= 0.005
        ):
            continue
        return None

    return price


def build_deterministic_rfq_offer_result(price: float) -> dict:
    """Return the same result shape as the Ollama classifier."""
    return {
        "success": True,
        "provider": "deterministic",
        "model": None,
        "message_category": "CLEAR_PRICE_OFFER",
        "recommended_action": "SAVE_OFFER",
        "safe_for_automation": True,
        "requires_human_review": False,
        "contains_risky_topic": False,
        "risk_category": "NONE",
        "confidence": "high",
        "stated_price_amount": price,
        "unit_price_usd": price,
        "currency": "USD",
        "price_basis": "UNIT",
        "is_price_clear": True,
        "is_currency_clear": True,
        "has_multiple_prices": False,
        "is_conditional": False,
        "condition_summary": None,
        "supplier_will_reply_later": False,
        "supplier_refused": False,
        "supplier_accepts_target": False,
        "question_can_be_answered_from_case": False,
        "reason": (
            "One simple, unambiguous RFQ unit price was verified by the "
            "deterministic safety parser."
        ),
        "suggested_clarification_question": None,
        "suggested_buyer_reply": None,
        "raw_result": None,
        "error": None,
    }
