from __future__ import annotations

import pytest

from app.llm.rfq_multi_item_price_safeguard import (
    build_deterministic_multi_item_rfq_offer_result,
    extract_safe_multi_item_rfq_prices,
)


SUPPLIER_ITEMS = [
    {"id": 1, "item_material": "Garnet pink round regular 5 mm"},
    {"id": 2, "item_material": "Peridot round regular 2 mm"},
    {"id": 3, "item_material": "Peridot round regular 4 mm"},
    {"id": 4, "item_material": "Peridot round regular 5 mm"},
]

CLEAN_ATTACHMENT_TEXT = (
    "QUALITY REQUIREMENTS: TOP quality. Perfect cut, polish, symmetry.\n"
    "ALO ID | Description | Needed quantity, pcs | Eleonora IMPORTANT notes | Price USD/ct\n"
    "PKGRPI500 | Garnet pink round regular 5 mm | 12 | 3 sets (each set by 4 stones) | 44\n"
    "PKPE200 | Peridot round regular 2 mm | 100 | matching | 20\n"
    "PKPE400 | Peridot round regular 4 mm | 24 | 6 sets (each set by 4 stones) | 30\n"
    "PKPE500 | Peridot round regular 5 mm | 16 | 4 sets (each set by 4 stones) | 40"
)


def test_clean_multi_item_table_is_fully_extracted_and_confirmed() -> None:
    item_offers = extract_safe_multi_item_rfq_prices(
        CLEAN_ATTACHMENT_TEXT, SUPPLIER_ITEMS
    )

    assert item_offers is not None
    by_material = {entry["item_material"]: entry for entry in item_offers}
    assert by_material["Garnet pink round regular 5 mm"]["unit_price_usd"] == 44.0
    assert by_material["Peridot round regular 2 mm"]["unit_price_usd"] == 20.0
    assert by_material["Peridot round regular 4 mm"]["unit_price_usd"] == 30.0
    assert by_material["Peridot round regular 5 mm"]["unit_price_usd"] == 40.0
    assert all(
        entry["price_certainty"] == "CONFIRMED" for entry in item_offers
    )


def test_build_result_shape_is_a_confirmed_save_offer() -> None:
    item_offers = extract_safe_multi_item_rfq_prices(
        CLEAN_ATTACHMENT_TEXT, SUPPLIER_ITEMS
    )
    result = build_deterministic_multi_item_rfq_offer_result(item_offers)

    assert result["success"] is True
    assert result["provider"] == "deterministic"
    assert result["message_category"] == "CLEAR_PRICE_OFFER"
    assert result["recommended_action"] == "SAVE_OFFER"
    assert result["requires_human_review"] is False
    assert result["contains_risky_topic"] is False
    assert result["item_offers"] == item_offers


def test_missing_item_bails_to_llm() -> None:
    """The file only covers 3 of 4 requested items - not safe to guess, so
    the whole message must be left to the LLM."""
    text = (
        "ALO ID | Description | Needed quantity, pcs | notes | Price USD/ct\n"
        "PKGRPI500 | Garnet pink round regular 5 mm | 12 | notes | 44\n"
        "PKPE200 | Peridot round regular 2 mm | 100 | notes | 20\n"
        "PKPE400 | Peridot round regular 4 mm | 24 | notes | 30"
    )

    assert extract_safe_multi_item_rfq_prices(text, SUPPLIER_ITEMS) is None


def test_hedge_keyword_anywhere_in_the_text_bails_to_llm() -> None:
    text = CLEAN_ATTACHMENT_TEXT + "\nWe will confirm these prices tomorrow."

    assert extract_safe_multi_item_rfq_prices(text, SUPPLIER_ITEMS) is None


def test_risk_keyword_anywhere_in_the_text_bails_to_llm() -> None:
    text = CLEAN_ATTACHMENT_TEXT + "\nNote: a 30% deposit is required upfront."

    assert extract_safe_multi_item_rfq_prices(text, SUPPLIER_ITEMS) is None


def test_no_header_row_bails_to_llm() -> None:
    text = "Hi, please see attached, all good."

    assert extract_safe_multi_item_rfq_prices(text, SUPPLIER_ITEMS) is None


def test_conflicting_prices_for_the_same_item_bails_to_llm() -> None:
    text = (
        "ALO ID | Description | Needed quantity, pcs | notes | Price USD/ct\n"
        "PKGRPI500 | Garnet pink round regular 5 mm | 12 | notes | 44\n"
        "PKGRPI500 | Garnet pink round regular 5 mm | 12 | notes | 46\n"
        "PKPE200 | Peridot round regular 2 mm | 100 | notes | 20\n"
        "PKPE400 | Peridot round regular 4 mm | 24 | notes | 30\n"
        "PKPE500 | Peridot round regular 5 mm | 16 | notes | 40"
    )

    assert extract_safe_multi_item_rfq_prices(text, SUPPLIER_ITEMS) is None


def test_row_with_a_dropped_cell_shifting_columns_is_skipped_not_misread() -> None:
    """A row missing one cell (e.g. an empty notes field) has fewer
    pipe-separated cells than the header - it must be skipped rather than
    have the wrong cell misread as the price, which then causes a bail
    because the affected item ends up unmatched."""
    text = (
        "ALO ID | Description | Needed quantity, pcs | notes | Price USD/ct\n"
        "PKGRPI500 | Garnet pink round regular 5 mm | 12 | 44\n"  # notes cell missing
        "PKPE200 | Peridot round regular 2 mm | 100 | matching | 20\n"
        "PKPE400 | Peridot round regular 4 mm | 24 | 6 sets | 30\n"
        "PKPE500 | Peridot round regular 5 mm | 16 | 4 sets | 40"
    )

    assert extract_safe_multi_item_rfq_prices(text, SUPPLIER_ITEMS) is None


def test_empty_supplier_items_returns_none() -> None:
    assert extract_safe_multi_item_rfq_prices(CLEAN_ATTACHMENT_TEXT, []) is None


def test_empty_message_returns_none() -> None:
    assert extract_safe_multi_item_rfq_prices("", SUPPLIER_ITEMS) is None
