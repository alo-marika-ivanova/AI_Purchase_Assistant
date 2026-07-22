from __future__ import annotations

import json

import pytest

import app.llm.supplier_message_classifier as classifier_module
from app.llm.supplier_message_classifier import analyze_supplier_message_with_ollama


CASE_DATA = {
    "case_number": "CASE-CLASSIFIER-NORMALIZATION",
    "item_material": "Pink Sapphire (PSA)",
    "quantity": 1.0,
    "notes": None,
}


class _FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, response: dict) -> None:
        self._response = response

    def generate(self, prompt, *, timeout_seconds, temperature=None) -> str:
        return json.dumps(self._response)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, response: dict) -> None:
    monkeypatch.setattr(
        classifier_module,
        "get_llm_provider",
        lambda: _FakeProvider(response),
    )


def test_casual_clear_price_reaches_llm_and_is_saved_as_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single clear price with a casual, non-hedging tone must not be
    downgraded to a provisional/tentative offer. Neither deterministic
    safeguard can handle this message (informal vocabulary), so it must
    reach the LLM; this checks the plumbing/normalization once the LLM
    responds per the updated prompt instructions."""
    _patch_provider(
        monkeypatch,
        {
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "confidence": "high",
            "stated_price_amount": 22,
            "unit_price_usd": 22,
            "currency": "USD",
            "price_basis": "UNIT",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "reason": "Single clear USD unit price despite casual tone.",
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body=(
            "Hi, mate, you are lucky, we just got new items. "
            "The price for one unit is 22 usd."
        ),
        case_data=CASE_DATA,
        supplier={"name": "New Goi Gems SRL"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
    )

    assert result["success"] is True
    assert result["message_category"] == "CLEAR_PRICE_OFFER"
    assert result["recommended_action"] == "SAVE_OFFER"
    assert result["unit_price_usd"] == pytest.approx(22.0)
    assert result["requires_human_review"] is False
    assert result["safe_for_automation"] is True


def test_plain_language_confirmation_supersedes_stored_provisional_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'this is a confirmed price.' does not fullmatch the deterministic
    bare-word confirmation regex, so it reaches the LLM. Per the updated
    prompt, the LLM is expected to recognize a plain-language confirmation
    of a stored provisional price without repeating the number; the
    classifier must then fill in the stored provisional price itself."""
    _patch_provider(
        monkeypatch,
        {
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "confidence": "high",
            "stated_price_amount": None,
            "unit_price_usd": None,
            "currency": None,
            "price_basis": None,
            "is_price_clear": False,
            "is_currency_clear": False,
            "has_multiple_prices": False,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "reason": "Supplier plainly confirmed the previously stated price.",
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body="this is a confirmed price.",
        case_data=CASE_DATA,
        supplier={"name": "New Goi Gems SRL"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_PRICE_CONFIRMATION",
        provisional_price_usd=22.0,
    )

    assert result["success"] is True
    assert result["message_category"] == "CLEAR_PRICE_OFFER"
    assert result["recommended_action"] == "SAVE_OFFER"
    assert result["unit_price_usd"] == pytest.approx(22.0)
    assert result["currency"] == "USD"
    assert result["price_basis"] == "UNIT"
    assert result["requires_human_review"] is False
    assert result["safe_for_automation"] is True


def test_clear_price_offer_without_provisional_price_is_not_fabricated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provisional-confirmation substitution must not fire when there is
    no stored provisional price -- an LLM response missing a price should
    still fall back to asking for clarification rather than inventing one."""
    _patch_provider(
        monkeypatch,
        {
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "confidence": "low",
            "stated_price_amount": None,
            "unit_price_usd": None,
            "currency": None,
            "price_basis": None,
            "is_price_clear": False,
            "is_currency_clear": False,
            "has_multiple_prices": False,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "reason": "Model claimed a clear offer but supplied no price.",
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body="Sounds good.",
        case_data=CASE_DATA,
        supplier={"name": "New Goi Gems SRL"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
    )

    assert result["unit_price_usd"] is None
    assert result["recommended_action"] == "ASK_PRICE_CLARIFICATION"
