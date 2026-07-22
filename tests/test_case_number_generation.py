from __future__ import annotations

from app.db.repository import PurchasingRepository, _slugify_item_code
from app.services.whatsapp_transport_service import _extract_case_number


repo = PurchasingRepository()


def test_slugify_prefers_parenthetical_catalog_code() -> None:
    assert _slugify_item_code("Amethyst Pink (AMP)") == "AMP"
    assert _slugify_item_code("Pink Sapphire (PSA)") == "PSA"


def test_slugify_falls_back_to_first_word_without_parenthetical_code() -> None:
    assert _slugify_item_code("Blue Sapphires") == "BLUE"
    assert _slugify_item_code("  ") == "ITEM"
    assert _slugify_item_code("") == "ITEM"


def test_create_case_number_has_readable_item_date_sequence_shape(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id = repo.create_case(
        item_material="Amethyst Pink (AMP)",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_id],
    )

    case_data = repo.get_case_basic(case_id)
    assert case_data is not None
    case_number = case_data["case_number"]

    prefix, rest = case_number.split("-", 1)
    assert prefix == "AMP"
    assert _extract_case_number(f"Please reference {case_number} in replies.") == (
        case_number
    )


def test_case_number_sequence_increments_within_the_same_day(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]

    first_id = repo.create_case(
        item_material="Amethyst Pink (AMP)",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_id],
    )
    second_id = repo.create_case(
        item_material="Pink Sapphire (PSA)",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_id],
    )

    first_number = repo.get_case_basic(first_id)["case_number"]
    second_number = repo.get_case_basic(second_id)["case_number"]

    first_sequence = int(first_number.rsplit("-", 1)[-1])
    second_sequence = int(second_number.rsplit("-", 1)[-1])

    assert second_sequence == first_sequence + 1


def test_extract_case_number_recognizes_legacy_formats() -> None:
    assert _extract_case_number(
        "Re: CASE-20260722-195215-124366-CBF2B9 offer"
    ) == "CASE-20260722-195215-124366-CBF2B9"
    assert _extract_case_number(
        "Re: CASE-20260722-195215-CBF2B9 offer"
    ) == "CASE-20260722-195215-CBF2B9"


def test_extract_case_number_returns_none_without_a_match() -> None:
    assert _extract_case_number("Just a regular message with no reference.") is None
