from __future__ import annotations

import re

from app.llm.supplier_message_analysis import add_structured_dimensions
from app.negotiation.common_reply_policy import extract_common_explicit_prices


_TENTATIVE_PATTERN = re.compile(
    r"\b(?:"
    r"almost\s+sure|not\s+(?:yet\s+)?sure|I\s+think|I\s+believe|"
    r"probably|likely|should\s+be|seems?\s+to\s+be|appears?\s+to\s+be|"
    r"tentative(?:ly)?|not\s+final|"
    r"(?:will|need\s+to|must|let\s+me)\s+(?:check|verify|confirm)|"
    r"check\s+with|verify\s+with|confirm\s+with|ask\s+(?:my|our|the)\s+"
    r"(?:supervisor|manager|boss|team)"
    r")\b",
    re.IGNORECASE,
)

_RISK_OR_SCOPE_PATTERN = re.compile(
    r"\b(?:"
    r"deposit|prepayment|pre-payment|payment\s+term|cash\s+payment|"
    r"delivery|lead\s+time|ship(?:ping)?|"
    r"different\s+(?:item|material|stone)|alternative\s+(?:item|material|stone)|"
    r"specification|quality|certificate|return|refund|"
    r"legal|liability|customs|sanction|compliance|"
    r"confidential|exclusive|dispute|claim"
    r")\b",
    re.IGNORECASE,
)

_TOTAL_OR_TIER_PATTERN = re.compile(
    r"\b(?:total|altogether|for\s+all|range|between|"
    r"only\s+if|provided\s+that|subject\s+to|"
    r"above|over|at\s+least|minimum|min\.?|more\s+than)\b",
    re.IGNORECASE,
)

_CONFIRMATION_PATTERN = re.compile(
    r"^(?:yes[,\s.!-]*)?(?:"
    r"confirmed|I\s+confirm|we\s+confirm|that(?:'s|\s+is)\s+confirmed|"
    r"the\s+price\s+is\s+confirmed|correct|yes\s+that(?:'s|\s+is)\s+correct"
    r")[\s.!]*$",
    re.IGNORECASE,
)

_WORD_PATTERN = re.compile(r"[a-zA-Z]+")
_GENERIC_ITEM_WORDS = {
    "diamond", "diamonds", "topaz", "sapphire", "sapphires", "ruby", "rubies",
    "emerald", "emeralds", "gem", "gems", "gemstone", "gemstones", "stone", "stones",
    "gold", "silver", "platinum", "pearl", "pearls",
}


def _requested_item_is_consistent(message_body: str, case_data: dict) -> bool:
    """Reject obvious references to a different item in deterministic mode."""
    item_words = {
        word.lower()
        for word in _WORD_PATTERN.findall(str(case_data.get("item_material") or ""))
        if len(word) >= 3
    }
    message_words = {
        word.lower()
        for word in _WORD_PATTERN.findall(message_body)
    }

    mentioned_item_words = message_words & _GENERIC_ITEM_WORDS
    requested_item_words = item_words & _GENERIC_ITEM_WORDS

    if mentioned_item_words and requested_item_words:
        return bool(mentioned_item_words & requested_item_words)

    # When the supplier names an item, require at least one requested-item word.
    if mentioned_item_words and not (message_words & item_words):
        return False

    return True


def extract_tentative_rfq_unit_price(
    message_body: str,
    case_data: dict,
) -> float | None:
    """Extract one provisional unit price only when uncertainty is explicit."""
    text = (message_body or "").strip()
    if not text or "?" in text:
        return None
    if not _TENTATIVE_PATTERN.search(text):
        return None
    if _RISK_OR_SCOPE_PATTERN.search(text):
        return None
    if _TOTAL_OR_TIER_PATTERN.search(text):
        return None
    if not _requested_item_is_consistent(text, case_data):
        return None

    prices = extract_common_explicit_prices(text)
    if len(prices) != 1:
        return None

    price = float(prices[0])
    return price if price > 0 else None


def is_contextual_provisional_price_confirmation(message_body: str) -> bool:
    text = " ".join((message_body or "").strip().split())
    return bool(text and _CONFIRMATION_PATTERN.fullmatch(text))


def build_deterministic_tentative_rfq_result(price: float) -> dict:
    return add_structured_dimensions(
        {
            "success": True,
            "provider": "deterministic",
            "model": None,
            "message_category": "TENTATIVE_PRICE",
            "recommended_action": "SAVE_PROVISIONAL_OFFER_AND_WAIT",
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
            "supplier_will_reply_later": True,
            "supplier_refused": False,
            "supplier_accepts_target": False,
            "question_can_be_answered_from_case": False,
            "price_certainty": "TENTATIVE",
            "supplier_commitment": "WILL_VERIFY",
            "pending_supplier_action": "Supplier will verify the indicated price.",
            "offer_status": "PROVISIONAL",
            "reason": (
                "One USD unit price was stated, but the supplier explicitly "
                "described it as unconfirmed and indicated that verification will follow."
            ),
            "suggested_clarification_question": None,
            "suggested_buyer_reply": (
                "Thank you for the update. Please confirm the price once you have "
                "verified it internally."
            ),
            "raw_result": None,
            "error": None,
        }
    )


def build_contextual_price_confirmation_result(price: float) -> dict:
    return add_structured_dimensions(
        {
            "success": True,
            "provider": "deterministic_context",
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
            "price_certainty": "CONFIRMED",
            "supplier_commitment": "CONFIRMED",
            "pending_supplier_action": None,
            "offer_status": "CONFIRMED",
            "reason": (
                "The supplier explicitly confirmed the previously recorded "
                "provisional unit price."
            ),
            "suggested_clarification_question": None,
            "suggested_buyer_reply": None,
            "raw_result": None,
            "error": None,
        }
    )
