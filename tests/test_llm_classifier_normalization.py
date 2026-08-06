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


ORDER_CASE_DATA = {
    "case_number": "RFQ-2026-07-31-01",
    "item_material": (
        "RFQ order (3 items): Tanzanite (TAN), Blue sapphire (SA), "
        "Ruby (RBN)."
    ),
    "quantity": 66.0,
    "notes": None,
    "items": [
        {"item_material": "Tanzanite (TAN)", "quantity": 40.0},
        {"item_material": "Blue sapphire (SA)", "quantity": 16.0},
        {"item_material": "Ruby (RBN)", "quantity": 10.0},
    ],
}


def test_multi_item_reply_extracts_a_confirmed_price_per_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A realistic reply quoting all three items in one message must yield
    one item_offers entry per item, each confirmed."""
    _patch_provider(
        monkeypatch,
        {
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "confidence": "high",
            "stated_price_amount": None,
            "unit_price_usd": None,
            "currency": "USD",
            "price_basis": "MULTIPLE",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": True,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "reason": "Supplier quoted all three requested stones.",
            "item_offers": [
                {
                    "item_material": "Tanzanite (TAN)",
                    "unit_price_usd": 180,
                    "price_certainty": "CONFIRMED",
                },
                {
                    "item_material": "Blue sapphire (SA)",
                    "unit_price_usd": 95,
                    "price_certainty": "CONFIRMED",
                },
                {
                    "item_material": "Ruby (RBN)",
                    "unit_price_usd": 60,
                    "price_certainty": "CONFIRMED",
                },
            ],
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body=(
            "Hi, here are our prices: Tanzanite 180 usd/ct, Blue sapphire "
            "95 usd/ct, Ruby 60 usd/ct. Let us know if you need anything else."
        ),
        case_data=ORDER_CASE_DATA,
        supplier={"name": "HC Arnoldi"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
    )

    assert result["success"] is True
    item_offers = result["item_offers"]
    assert item_offers is not None
    assert len(item_offers) == 3
    by_item = {entry["item_material"]: entry for entry in item_offers}
    assert by_item["Tanzanite (TAN)"]["unit_price_usd"] == pytest.approx(180.0)
    assert by_item["Blue sapphire (SA)"]["unit_price_usd"] == pytest.approx(95.0)
    assert by_item["Ruby (RBN)"]["unit_price_usd"] == pytest.approx(60.0)
    assert all(
        entry["price_certainty"] == "CONFIRMED" for entry in item_offers
    )


def test_multi_item_reply_covering_only_some_items_omits_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplier answering about only one of three items must not have a
    price fabricated for the other two - they stay unmentioned/unresolved
    for this round."""
    _patch_provider(
        monkeypatch,
        {
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "confidence": "high",
            "unit_price_usd": None,
            "currency": "USD",
            "price_basis": "UNIT",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "reason": "Supplier only priced the Ruby for now.",
            "item_offers": [
                {
                    "item_material": "Ruby (RBN)",
                    "unit_price_usd": 60,
                    "price_certainty": "CONFIRMED",
                },
            ],
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body=(
            "We can do Ruby at 60 usd/ct. Still checking on the others, "
            "will update you soon."
        ),
        case_data=ORDER_CASE_DATA,
        supplier={"name": "HC Arnoldi"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
    )

    item_offers = result["item_offers"]
    assert item_offers is not None
    assert len(item_offers) == 1
    assert item_offers[0]["item_material"] == "Ruby (RBN)"


def test_multi_item_reply_with_mixed_confirmed_and_tentative_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One item confirmed, another hedged in the same message - each item's
    own certainty must be preserved independently."""
    _patch_provider(
        monkeypatch,
        {
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "confidence": "medium",
            "unit_price_usd": None,
            "currency": "USD",
            "price_basis": "MULTIPLE",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": True,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "reason": "One confirmed price, one still to be verified.",
            "item_offers": [
                {
                    "item_material": "Tanzanite (TAN)",
                    "unit_price_usd": 180,
                    "price_certainty": "CONFIRMED",
                },
                {
                    "item_material": "Blue sapphire (SA)",
                    "unit_price_usd": 95,
                    "price_certainty": "TENTATIVE",
                },
            ],
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body=(
            "Tanzanite is confirmed at 180 usd/ct. For the sapphire I think "
            "it's around 95 usd/ct but let me double check internally."
        ),
        case_data=ORDER_CASE_DATA,
        supplier={"name": "HC Arnoldi"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
    )

    item_offers = result["item_offers"]
    by_item = {entry["item_material"]: entry for entry in item_offers}
    assert by_item["Tanzanite (TAN)"]["price_certainty"] == "CONFIRMED"
    assert by_item["Blue sapphire (SA)"]["price_certainty"] == "TENTATIVE"


def test_multi_item_reply_with_risk_topic_clears_all_item_offers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Risk classification has priority over price classification for the
    whole message - even if the model also returned item_offers, a risky
    reply must not have any of them recorded."""
    _patch_provider(
        monkeypatch,
        {
            "message_category": "DEPOSIT_OR_PREPAYMENT",
            "recommended_action": "PAUSE_FOR_REVIEW",
            "confidence": "high",
            "unit_price_usd": None,
            "currency": "USD",
            "price_basis": "MULTIPLE",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": True,
            "is_conditional": False,
            "requires_human_review": True,
            "contains_risky_topic": True,
            "risk_category": "DEPOSIT_OR_PREPAYMENT",
            "reason": "Supplier requires a deposit before quoting further.",
            "item_offers": [
                {
                    "item_material": "Tanzanite (TAN)",
                    "unit_price_usd": 180,
                    "price_certainty": "CONFIRMED",
                },
            ],
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body=(
            "We can do Tanzanite at 180 usd/ct, but only with a 50% "
            "deposit upfront for the whole order."
        ),
        case_data=ORDER_CASE_DATA,
        supplier={"name": "HC Arnoldi"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
    )

    assert result["contains_risky_topic"] is True
    assert result["requires_human_review"] is True
    assert result["item_offers"] is None


def test_single_item_case_data_never_triggers_multi_item_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy single-item case (no "items", or exactly one) must not
    request/produce item_offers at all - confirms the gate is on item
    count, not merely the presence of the key."""
    single_item_with_list = {
        **CASE_DATA,
        "items": [{"item_material": "Pink Sapphire (PSA)", "quantity": 1.0}],
    }

    _patch_provider(
        monkeypatch,
        {
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "confidence": "high",
            "unit_price_usd": 42,
            "currency": "USD",
            "price_basis": "UNIT",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "reason": "Single clear price.",
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body="We can do 42 usd per unit.",
        case_data=single_item_with_list,
        supplier={"name": "New Goi Gems SRL"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
    )

    # This simple message may be caught by the deterministic RFQ safeguard
    # (which never sets "item_offers" at all) or reach the mocked LLM (which
    # sets it to None) - either way, no item-level extraction should occur.
    assert result.get("item_offers") is None


ATTACHMENT_ORDER_CASE_DATA = {
    "case_number": "RFQ-2026-08-06-01",
    "item_material": "RFQ order (2 items): Garnet Pink, Peridote (PER).",
    "quantity": 152.0,
    "notes": None,
    "items": [
        {"item_material": "Garnet pink round regular 5 mm", "quantity": 12.0},
        {"item_material": "Peridot round regular 2 mm", "quantity": 100.0},
    ],
}

CLEAN_MULTI_ITEM_ATTACHMENT_TEXT = (
    "QUALITY REQUIREMENTS: TOP quality. Perfect cut, polish, symmetry.\n"
    "ALO ID | Description | Needed quantity, pcs | Eleonora IMPORTANT notes | Price USD/ct\n"
    "PKGRPI500 | Garnet pink round regular 5 mm | 12 | 3 sets (each set by 4 stones) | 44\n"
    "PKPE200 | Peridot round regular 2 mm | 100 | matching | 20"
)


def test_attachment_derived_multi_item_reply_is_confirmed_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces a real bug: a supplier's filled-in RFQ spreadsheet, with
    no hedging language at all, was sent to the LLM and came back
    TENTATIVE_PRICE. is_attachment_reply=True must intercept a clean,
    unhedged multi-item table BEFORE the LLM is called at all - the fake
    provider below is configured to return TENTATIVE precisely to prove the
    deterministic shortcut wins regardless of what the LLM would say."""
    _patch_provider(
        monkeypatch,
        {
            "message_category": "TENTATIVE_PRICE",
            "recommended_action": "SAVE_PROVISIONAL_OFFER_AND_WAIT",
            "confidence": "medium",
            "unit_price_usd": None,
            "currency": "USD",
            "price_basis": "MULTIPLE",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": True,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "reason": "This is what the LLM would have said - must not be used.",
            "item_offers": [
                {
                    "item_material": "Garnet pink round regular 5 mm",
                    "unit_price_usd": 44,
                    "price_certainty": "TENTATIVE",
                },
                {
                    "item_material": "Peridot round regular 2 mm",
                    "unit_price_usd": 20,
                    "price_certainty": "TENTATIVE",
                },
            ],
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body=CLEAN_MULTI_ITEM_ATTACHMENT_TEXT,
        case_data=ATTACHMENT_ORDER_CASE_DATA,
        supplier={"name": "HC Arnoldi"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
        is_attachment_reply=True,
    )

    assert result["provider"] == "deterministic"
    assert result["message_category"] == "CLEAR_PRICE_OFFER"
    assert result["recommended_action"] == "SAVE_OFFER"
    item_offers = result["item_offers"]
    by_material = {entry["item_material"]: entry for entry in item_offers}
    assert by_material["Garnet pink round regular 5 mm"]["unit_price_usd"] == pytest.approx(44.0)
    assert by_material["Peridot round regular 2 mm"]["unit_price_usd"] == pytest.approx(20.0)
    assert all(entry["price_certainty"] == "CONFIRMED" for entry in item_offers)


def test_same_text_typed_instead_of_attached_still_reaches_the_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deterministic multi-item shortcut is attachment-only by design -
    a supplier typing the same table as free text must still go through the
    LLM, per is_attachment_reply defaulting to False."""
    _patch_provider(
        monkeypatch,
        {
            "message_category": "TENTATIVE_PRICE",
            "recommended_action": "SAVE_PROVISIONAL_OFFER_AND_WAIT",
            "confidence": "medium",
            "unit_price_usd": None,
            "currency": "USD",
            "price_basis": "MULTIPLE",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": True,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "reason": "The LLM was actually called for this typed reply.",
            "item_offers": [],
        },
    )

    result = analyze_supplier_message_with_ollama(
        message_body=CLEAN_MULTI_ITEM_ATTACHMENT_TEXT,
        case_data=ATTACHMENT_ORDER_CASE_DATA,
        supplier={"name": "HC Arnoldi"},
        message_history=[],
        conversation_stage="RFQ",
        supplier_state="AWAITING_RESPONSE",
        is_attachment_reply=False,
    )

    assert result["message_category"] == "TENTATIVE_PRICE"
    assert result["reason"] == "The LLM was actually called for this typed reply."


class _FlakyThenGoodProvider:
    """Returns malformed output the first N-1 calls, then a valid JSON
    response - simulates a provider that occasionally truncates/garbles a
    response even at temperature=0."""

    name = "fake"
    model = "fake-model"

    def __init__(self, response: dict, bad_calls: int) -> None:
        self._response = response
        self._bad_calls = bad_calls
        self.call_count = 0

    def generate(self, prompt, *, timeout_seconds, temperature=None) -> str:
        self.call_count += 1
        if self.call_count <= self._bad_calls:
            # Truncated mid-object, exactly like a real cut-off response.
            return '{"message_category": "CLEAR_PRICE_OFFER", "unit_pri'
        return json.dumps(self._response)


def test_malformed_response_is_retried_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One malformed/truncated response must not immediately doom the
    reply to human review - a retry with the same prompt gets a second
    chance to produce valid JSON."""
    flaky_provider = _FlakyThenGoodProvider(
        {
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "confidence": "high",
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
            "reason": "Single clear USD unit price.",
        },
        bad_calls=1,
    )
    monkeypatch.setattr(
        classifier_module, "get_llm_provider", lambda: flaky_provider
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

    assert flaky_provider.call_count == 2
    assert result["success"] is True
    assert result["message_category"] == "CLEAR_PRICE_OFFER"
    assert result["unit_price_usd"] == pytest.approx(22.0)
    assert result["requires_human_review"] is False


def test_repeatedly_malformed_response_still_fails_after_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every attempt returns malformed output, the classifier must still
    give up (and not retry forever) and report the same kind of failure
    result as before this safeguard existed."""
    flaky_provider = _FlakyThenGoodProvider(
        {"message_category": "CLEAR_PRICE_OFFER"},
        bad_calls=99,
    )
    monkeypatch.setattr(
        classifier_module, "get_llm_provider", lambda: flaky_provider
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

    assert flaky_provider.call_count == classifier_module.JSON_PARSE_MAX_ATTEMPTS
    assert result["success"] is False
    assert result["requires_human_review"] is True


def test_auth_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider construction failure (e.g. missing API key) fails the
    same way every time - it must not be retried, only a malformed-JSON
    response should be."""

    def _raise_auth_error():
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    monkeypatch.setattr(
        classifier_module, "get_llm_provider", _raise_auth_error
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

    assert result["success"] is False
    assert result["requires_human_review"] is True
