from __future__ import annotations

from app.negotiation.common_reply_policy import (
    decide_common_negotiation_reply,
)


def test_llm_result_is_authoritative_despite_two_numbers_in_text() -> None:
    """Regression test for a real production case: the supplier refused
    the target price (27) and countered with a clear new price (28.50) in
    the same message. The old regex layer counted two numbers in the raw
    text and paused for review even when the LLM classified it correctly.
    The LLM result must now be trusted as-is."""
    analysis = {
        "success": True,
        "message_category": "IMPROVED_PRICE_OFFER",
        "recommended_action": "SAVE_OFFER",
        "unit_price_usd": 28.50,
        "requires_human_review": False,
        "safe_for_automation": True,
        "has_multiple_prices": False,
        "is_conditional": False,
    }

    decision = decide_common_negotiation_reply(
        supplier_text=(
            "Absolutely impossible to go to 27 usd. We could go to "
            "28.50 usd, but that's the lowest we can get. Hope you "
            "understan"
        ),
        analysis=analysis,
        previous_best_price_usd=30.0,
        target_price_usd=27.0,
    )

    assert decision.action == "USE_CLASSIFIER_RESULT"


def test_llm_risky_classification_is_trusted_without_a_regex_recheck() -> None:
    """The deposit/payment/legal-topic regex was removed. A successful LLM
    classification -- including its own PAUSE_FOR_REVIEW verdict for risky
    topics -- is used as-is; this function must not re-scan the text."""
    analysis = {
        "success": True,
        "message_category": "DEPOSIT_OR_PREPAYMENT",
        "recommended_action": "PAUSE_FOR_REVIEW",
        "unit_price_usd": None,
        "requires_human_review": True,
        "safe_for_automation": False,
    }

    decision = decide_common_negotiation_reply(
        supplier_text="We can do 36 USD, but only with a 50 percent deposit.",
        analysis=analysis,
        previous_best_price_usd=42.0,
        target_price_usd=36.0,
    )

    assert decision.action == "USE_CLASSIFIER_RESULT"


def test_failed_llm_call_falls_back_to_a_single_extracted_price() -> None:
    """When the LLM call itself failed, the classifier's analysis carries
    no usable price. The deterministic fallback should recover a single
    unambiguous price from the raw text rather than escalate blindly."""
    analysis = {
        "success": False,
        "message_category": "UNKNOWN",
        "recommended_action": "PAUSE_FOR_REVIEW",
        "unit_price_usd": None,
        "requires_human_review": True,
    }

    decision = decide_common_negotiation_reply(
        supplier_text="We can do 38 USD.",
        analysis=analysis,
        previous_best_price_usd=42.0,
        target_price_usd=36.0,
    )

    assert decision.action == "SAVE_OFFER"
    assert decision.unit_price_usd == 38.0


def test_failed_llm_call_with_ambiguous_text_still_pauses() -> None:
    """If the fallback extraction itself can't find exactly one price,
    human review is still the safe outcome."""
    analysis = {
        "success": False,
        "message_category": "UNKNOWN",
        "recommended_action": "PAUSE_FOR_REVIEW",
        "unit_price_usd": None,
        "requires_human_review": True,
    }

    decision = decide_common_negotiation_reply(
        supplier_text="We can do 40 USD, or 38 USD above 100 pieces.",
        analysis=analysis,
        previous_best_price_usd=42.0,
        target_price_usd=36.0,
    )

    assert decision.action == "PAUSE_FOR_REVIEW"
    assert decision.unit_price_usd is None


def test_save_offer_action_missing_price_triggers_fallback_extraction() -> None:
    """A successful LLM call that recommends SAVE_OFFER but omits a usable
    unit_price_usd is not usable on its own; the fallback should try to
    recover the price from the raw text."""
    analysis = {
        "success": True,
        "message_category": "CLEAR_PRICE_OFFER",
        "recommended_action": "SAVE_OFFER",
        "unit_price_usd": None,
        "requires_human_review": False,
        "safe_for_automation": True,
    }

    decision = decide_common_negotiation_reply(
        supplier_text="We can reduce the price to 38 per unit.",
        analysis=analysis,
        previous_best_price_usd=42.0,
        target_price_usd=36.0,
    )

    assert decision.action == "SAVE_OFFER"
    assert decision.unit_price_usd == 38.0
