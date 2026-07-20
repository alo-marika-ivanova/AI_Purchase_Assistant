from __future__ import annotations

import urllib.request

import pytest

from app.llm.rfq_price_safeguard import (
    extract_safe_simple_rfq_unit_price,
)
from app.llm.supplier_message_classifier import (
    analyze_supplier_message_with_ollama,
)


CASE_DATA = {
    "case_number": "CASE-RFQ-PRICE-GUARD",
    "item_material": "Black Diamonds",
    "quantity": 1.0,
    "notes": None,
}


@pytest.mark.parametrize(
    ("message", "expected_price"),
    [
        ("Dear partner, black diamond costs 33 usd", 33.0),
        ("33 usd is the unit price for 1pcs of black diamond", 33.0),
        ("Our best unit price is 43 USD.", 43.0),
        ("The price is 32.", 32.0),
    ],
)
def test_safe_simple_rfq_prices_are_extracted(
    message: str,
    expected_price: float,
) -> None:
    assert extract_safe_simple_rfq_unit_price(
        message_body=message,
        case_data=CASE_DATA,
    ) == pytest.approx(expected_price)


@pytest.mark.parametrize(
    "message",
    [
        "USD 100 total for 10 pieces.",
        "It is 33 USD, or 29 USD above 100 pieces.",
        "We can do 33 USD, but only with a 50 percent deposit.",
        "Blue sapphires cost 33 USD.",
        "The price is 33 USD for minimum 10 pcs.",
        "Can you confirm whether 33 USD is acceptable?",
    ],
)
def test_ambiguous_or_risky_messages_are_not_overridden(
    message: str,
) -> None:
    assert extract_safe_simple_rfq_unit_price(
        message_body=message,
        case_data=CASE_DATA,
    ) is None


def test_simple_rfq_offer_does_not_call_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "Ollama must not be called for a safe simple RFQ price."
        )

    monkeypatch.setattr(urllib.request, "urlopen", fail_if_called)

    result = analyze_supplier_message_with_ollama(
        message_body="33 usd is the unit price for 1pcs of black diamond",
        case_data=CASE_DATA,
        supplier={"name": "Fine Star HK Ltd"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
    )

    assert result["provider"] == "deterministic"
    assert result["recommended_action"] == "SAVE_OFFER"
    assert result["unit_price_usd"] == pytest.approx(33.0)
    assert result["confidence"] == "high"
    assert result["requires_human_review"] is False
