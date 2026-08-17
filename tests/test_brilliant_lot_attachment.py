from __future__ import annotations

from pathlib import Path

import pytest

from app.db.database import get_connection
from app.db.repository import PurchasingRepository
from app.services import email_transport_service, simple_chat_service
from app.services.attachment_service import extract_supplier_reply_text
from app.services.case_service import create_case_from_detected_items
from app.services.rfq_detection_service import detect_rfq_selection

repo = PurchasingRepository()

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "rfq_samples"

# Matches the "email" supplier seeded by tests/conftest.py::_insert_test_suppliers.
TEST_SUPPLIER_EMAIL = "supplier.email@example.test"

# Prices baked into tests/fixtures/rfq_samples/brilianty_filled.xlsx, one per
# lot in lot order (see the merged Price USD/ct cells at rows 5/17/28/39/45/49/54).
EXPECTED_LOT_PRICES = [42.5, 55.0, 61.25, 70.0, 95.5, 110.25, 130.75]


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _seed_supplier_goods(
    supplier_id: int, goods_group: str, goods_names: list[str]
) -> None:
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO supplier_goods (supplier_id, goods_name, goods_group)
            VALUES (?, ?, ?)
            """,
            [(supplier_id, goods_name, goods_group) for goods_name in goods_names],
        )
        conn.commit()


def _create_brilliant_case(supplier_id: int, filename: str = "brilianty.xlsx"):
    """Mirrors the Streamlit auto-detected-RFQ case-creation path: build
    pending_items from DetectedRfqItem (including description) and create
    one case_item per lot."""
    result = detect_rfq_selection(_read_fixture(filename), filename)
    assert result.recognized is True

    items = [
        {
            "item_material": item.display_name,
            "description": item.description,
            "quantity": item.quantity,
            "supplier_ids": [supplier_id],
        }
        for item in result.items
    ]
    case_id = create_case_from_detected_items(items=items, notes="")
    return case_id, result.items


def _case_item_id(case_id: int, item_material: str) -> int:
    for case_item in repo.list_case_items(case_id):
        if case_item["item_material"] == item_material:
            return int(case_item["id"])
    raise AssertionError(f"No case_item named {item_material!r} in case {case_id}")


def test_case_items_get_one_per_lot_with_source_description_populated(
    supplier_ids: dict[str, int],
) -> None:
    """Regression for the Streamlit gap where pending_items never carried
    description through to create_case_from_detected_items, leaving
    case_items.source_description NULL even though the field exists and is
    populated by add_case_item."""
    supplier_id = supplier_ids["email"]
    _seed_supplier_goods(supplier_id, "Diamonds", ["up to 1ct", "1ct and up"])

    case_id, detected_items = _create_brilliant_case(supplier_id)

    case_items = repo.list_case_items(case_id)
    assert len(case_items) == 7

    by_material = {ci["item_material"]: ci for ci in case_items}
    for item in detected_items:
        stored = by_material[item.display_name]
        assert stored["source_description"] == item.description
        assert stored["source_description"] is not None
        assert stored["quantity"] == pytest.approx(item.quantity)


def test_manual_reply_with_filled_brilliant_attachment_prices_all_lots_deterministically(
    supplier_ids: dict[str, int],
) -> None:
    """The Streamlit "simulate a supplier reply" upload path: a clean,
    fully filled-in copy of the brilliant-lot template must be priced by
    the deterministic multi-item safeguard, not the LLM."""
    supplier_id = supplier_ids["email"]
    _seed_supplier_goods(supplier_id, "Diamonds", ["up to 1ct", "1ct and up"])

    case_id, detected_items = _create_brilliant_case(supplier_id)
    simple_chat_service.start_negotiating_case(case_id)

    analysis_text = extract_supplier_reply_text(
        _read_fixture("brilianty_filled.xlsx"), "brilianty_filled.xlsx"
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="manual",
        body="(Replied with an attached file: brilianty_filled.xlsx)",
        analysis_text=analysis_text,
    )

    assert result["extraction"]["method"] == "deterministic_rfq_price_parser"
    assert result["extraction"]["needs_review"] is False
    assert len(result["extraction"]["item_offers"]) == 7

    for item, expected_price in zip(detected_items, EXPECTED_LOT_PRICES):
        offer = repo.get_best_offer_for_case_item_supplier(
            _case_item_id(case_id, item.display_name), supplier_id
        )
        assert offer is not None
        assert offer["unit_price_usd"] == pytest.approx(expected_price)


def test_email_attachment_path_matches_manual_attachment_path(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real inbound email with the filled brilliant-lot workbook attached
    must be priced identically to the manual Streamlit upload path - both
    go through extract_supplier_reply_text and the same deterministic
    safeguard."""
    supplier_id = supplier_ids["email"]
    _seed_supplier_goods(supplier_id, "Diamonds", ["up to 1ct", "1ct and up"])

    case_id, detected_items = _create_brilliant_case(supplier_id)
    simple_chat_service.start_negotiating_case(case_id)

    case_number = repo.get_case_basic(case_id)["case_number"]
    monkeypatch.setenv("BUYER_EMAIL", "buyer@example.test")

    email = {
        "graph_message_id": "graph-msg-1",
        "internet_message_id": "<graph-msg-1@example.test>",
        "in_reply_to": None,
        "references": None,
        "graph_conversation_id": "conv-1",
        "subject": f"[{case_number}] RFQ - test",
        "sender_name": "Supplier",
        "sender_email": TEST_SUPPLIER_EMAIL,
        "received_at": "2026-08-07T10:00:00Z",
        "body": "Please see the attached prices.",
        "body_preview": "Please see the attached prices.",
        "attachments": [
            {
                "filename": "brilianty_filled.xlsx",
                "content_bytes": _read_fixture("brilianty_filled.xlsx"),
                "mime_type": (
                    "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"
                ),
            }
        ],
    }

    monkeypatch.setattr(
        email_transport_service,
        "list_recent_inbox_messages",
        lambda user_email, top: [email],
    )

    result = email_transport_service.import_supplier_emails_for_case(case_id)

    assert result["imported_count"] == 1
    assert result["results"][0]["imported"] is True

    for item, expected_price in zip(detected_items, EXPECTED_LOT_PRICES):
        offer = repo.get_best_offer_for_case_item_supplier(
            _case_item_id(case_id, item.display_name), supplier_id
        )
        assert offer is not None
        assert offer["unit_price_usd"] == pytest.approx(expected_price)

    case_attachments = repo.list_attachments_for_case(case_id)
    assert len(case_attachments) == 1
    assert case_attachments[0]["original_filename"] == "brilianty_filled.xlsx"
    assert case_attachments[0]["direction"] == "inbound"
