from __future__ import annotations

import pytest

from app.db.repository import PurchasingRepository
from app.negotiation.states import CaseState
from app.services import (
    negotiation_reply_service,
    simple_chat_service,
    transport_delivery_service,
)
from app.services.attachment_service import (
    extract_text_from_spreadsheet,
    save_case_attachment,
    send_case_attachment,
)
from app.services.case_service import create_case
from app.services.offer_service import add_offer
from app.services.simple_chat_service import (
    record_supplier_message_simple,
    start_negotiating_case,
)


repo = PurchasingRepository()


def _create_case(supplier_ids: list[int], auto_send_messages: bool = False) -> int:
    return create_case(
        item_material="test sapphire",
        quantity=5.0,
        notes="Attachment sending regression test",
        supplier_ids=supplier_ids,
        auto_send_messages=auto_send_messages,
    )


# ---------------------------------------------------------------------
# send_or_display_outbound_message(attachment_ids=...)
# ---------------------------------------------------------------------

def test_send_or_display_outbound_message_links_attachment_to_new_message(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_case([supplier_ids["email"]])
    template = save_case_attachment(
        case_id=case_id,
        original_filename="price_request.xlsx",
        file_bytes=b"fake xlsx bytes",
    )

    result = simple_chat_service.send_or_display_outbound_message(
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        body="Please quote using the attached template.",
        message_type="attachment_share",
        attachment_ids=[template["id"]],
    )

    linked = repo.list_attachments_for_message(result["message_id"])
    assert len(linked) == 1
    assert linked[0]["original_filename"] == "price_request.xlsx"
    assert linked[0]["stored_path"] == template["stored_path"]
    assert linked[0]["supplier_id"] == supplier_ids["email"]

    # The original template attachment is untouched (still unlinked), so it
    # can be reused (e.g. sent to another supplier, or attached to a later
    # RFQ) without being consumed by this one send.
    original = repo.get_attachment_by_id(template["id"])
    assert original["message_id"] is None


def test_attachment_ids_ignored_when_none_given(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_case([supplier_ids["email"]])

    result = simple_chat_service.send_or_display_outbound_message(
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        body="Plain text message, no attachment.",
        message_type="manual_note",
    )

    assert result["attachment_ids"] == []
    assert repo.list_attachments_for_message(result["message_id"]) == []


# ---------------------------------------------------------------------
# attachment_service.send_case_attachment
# ---------------------------------------------------------------------

def test_send_case_attachment_creates_message_and_links_file(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_case([supplier_ids["email"]])
    attachment = save_case_attachment(
        case_id=case_id,
        original_filename="catalog.pdf",
        file_bytes=b"%PDF-1.4 fake",
    )

    result = send_case_attachment(
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        attachment_id=attachment["id"],
    )

    message = repo.get_message_by_id(result["message_id"])
    assert message["direction"] == "outbound"
    assert "catalog.pdf" in message["body"]

    linked = repo.list_attachments_for_message(result["message_id"])
    assert len(linked) == 1
    assert linked[0]["original_filename"] == "catalog.pdf"


def test_send_case_attachment_uses_custom_caption(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_case([supplier_ids["email"]])
    attachment = save_case_attachment(
        case_id=case_id,
        original_filename="catalog.pdf",
        file_bytes=b"%PDF-1.4 fake",
    )

    result = send_case_attachment(
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        attachment_id=attachment["id"],
        caption="Here is our updated catalog.",
    )

    message = repo.get_message_by_id(result["message_id"])
    assert message["body"] == "Here is our updated catalog."


def test_send_case_attachment_rejects_attachment_from_another_case(
    supplier_ids: dict[str, int],
) -> None:
    case_id_1 = _create_case([supplier_ids["email"]])
    case_id_2 = _create_case([supplier_ids["email"]])

    attachment = save_case_attachment(
        case_id=case_id_1,
        original_filename="only-in-1.csv",
        file_bytes=b"x",
    )

    with pytest.raises(ValueError):
        send_case_attachment(
            case_id=case_id_2,
            supplier_id=supplier_ids["email"],
            attachment_id=attachment["id"],
        )


# ---------------------------------------------------------------------
# RFQ auto-attach: execute_rfq_rule_action(SEND_RFQ) attaches template files
# ---------------------------------------------------------------------

def test_starting_negotiation_attaches_uploaded_file_to_each_rfq(
    supplier_ids: dict[str, int],
) -> None:
    selected = [supplier_ids["email"], supplier_ids["whatsapp"]]
    case_id = _create_case(selected)

    template = save_case_attachment(
        case_id=case_id,
        original_filename="rfq_template.xlsx",
        file_bytes=b"fake workbook bytes",
    )

    simple_chat_service.start_negotiating_case(case_id)

    for supplier_id in selected:
        rfq_message = repo.get_latest_supplier_outbound_message(
            case_id=case_id,
            supplier_id=supplier_id,
        )
        assert rfq_message is not None

        linked = repo.list_attachments_for_message(int(rfq_message["id"]))
        assert len(linked) == 1
        assert linked[0]["original_filename"] == "rfq_template.xlsx"
        assert linked[0]["stored_path"] == template["stored_path"]

    # The original template stays unlinked - it is not "consumed" by being
    # cloned onto each supplier's own RFQ message.
    original = repo.get_attachment_by_id(template["id"])
    assert original["message_id"] is None


def test_case_without_uploaded_file_sends_rfq_without_attachment(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_case([supplier_ids["email"]])

    simple_chat_service.start_negotiating_case(case_id)

    rfq_message = repo.get_latest_supplier_outbound_message(
        case_id=case_id,
        supplier_id=supplier_ids["email"],
    )
    assert rfq_message is not None
    assert repo.list_attachments_for_message(int(rfq_message["id"])) == []


# ---------------------------------------------------------------------
# transport_delivery_service reads attachments from the DB at send time
# ---------------------------------------------------------------------

def test_deliver_claimed_email_job_attaches_linked_files(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = _create_case([supplier_ids["email"]], auto_send_messages=True)

    message_id = repo.add_message(
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        direction="outbound",
        channel="email",
        body="Please see the attached file.",
        status="queued",
        message_type="attachment_share",
        approval_required=False,
        approved_by_buyer=True,
    )

    attachment = save_case_attachment(
        case_id=case_id,
        original_filename="offer_request.xlsx",
        file_bytes=b"fake workbook content",
        supplier_id=supplier_ids["email"],
        message_id=message_id,
        channel="email",
        direction="outbound",
    )

    captured = {}

    def fake_send_email_message(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "delivery_outcome": "dry_run",
            "provider_message_id": "dry-run-email",
            "internet_message_id": "<abc@purchasing-ai.local>",
        }

    monkeypatch.setattr(
        transport_delivery_service, "send_email_message", fake_send_email_message
    )

    result = transport_delivery_service.attempt_email_delivery(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        to_email="supplier@example.test",
        subject="Attachment",
        body="Please see the attached file.",
    )

    assert result["success"] is True
    assert captured["attachments"] is not None
    assert len(captured["attachments"]) == 1
    assert captured["attachments"][0]["filename"] == "offer_request.xlsx"
    assert captured["attachments"][0]["content"] == b"fake workbook content"


def test_deliver_claimed_email_job_passes_none_when_no_attachment(
    supplier_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = _create_case([supplier_ids["email"]], auto_send_messages=True)

    message_id = repo.add_message(
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        direction="outbound",
        channel="email",
        body="Plain message.",
        status="queued",
        message_type="rfq",
        approval_required=False,
        approved_by_buyer=True,
    )

    captured = {}

    def fake_send_email_message(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "delivery_outcome": "dry_run",
            "provider_message_id": "dry-run-email",
            "internet_message_id": "<abc@purchasing-ai.local>",
        }

    monkeypatch.setattr(
        transport_delivery_service, "send_email_message", fake_send_email_message
    )

    transport_delivery_service.attempt_email_delivery(
        message_id=message_id,
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        to_email="supplier@example.test",
        subject="RFQ",
        body="Plain message.",
    )

    assert captured["attachments"] is None


# ---------------------------------------------------------------------
# extract_text_from_spreadsheet
# ---------------------------------------------------------------------

def test_extract_text_from_csv() -> None:
    csv_bytes = b"Item,Unit price USD\nAmethyst,12.5\nPeridote,9\n"

    text = extract_text_from_spreadsheet(csv_bytes, "prices.csv")

    assert "Item | Unit price USD" in text
    assert "Amethyst | 12.5" in text
    assert "Peridote | 9" in text


def test_extract_text_from_xlsx() -> None:
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["Item", "Unit price USD"])
    worksheet.append(["Amethyst", 12.5])

    buffer = BytesIO()
    workbook.save(buffer)

    text = extract_text_from_spreadsheet(buffer.getvalue(), "prices.xlsx")

    assert "Item | Unit price USD" in text
    assert "Amethyst | 12.5" in text


def test_extract_text_from_spreadsheet_rejects_unsupported_extension() -> None:
    with pytest.raises(ValueError):
        extract_text_from_spreadsheet(b"not a spreadsheet", "notes.txt")


# ---------------------------------------------------------------------
# analysis_text: the classifier reads the file's extracted content while
# the stored/displayed message body stays a short, human-facing note.
# ---------------------------------------------------------------------

def test_rfq_stage_reply_stores_clean_body_but_analyzes_the_full_text(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _create_case([supplier_ids["email"]])
    start_negotiating_case(case_id)

    seen_message_bodies = []

    def fake_analyze(**kwargs):
        seen_message_bodies.append(kwargs["message_body"])
        return {
            "success": True,
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "unit_price_usd": 33.0,
            "currency": "USD",
            "price_basis": "UNIT",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "confidence": "high",
            "reason": "Single clear price.",
        }

    monkeypatch.setattr(
        simple_chat_service, "analyze_supplier_message_with_ollama", fake_analyze
    )

    result = record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        channel="manual",
        body="see the attached file",
        analysis_text=(
            "see the attached file\n\n"
            "ALO ID | Description | Price USD/ct\n"
            "PK500 | test sapphire | 33"
        ),
    )

    # The classifier saw the full extracted content...
    assert len(seen_message_bodies) == 1
    assert "Price USD/ct" in seen_message_bodies[0]
    assert "PK500" in seen_message_bodies[0]

    # ...but the stored/displayed message body stays the short note, not a
    # dump of the spreadsheet content.
    stored_message = repo.get_message_by_id(result["inbound_message_id"])
    assert stored_message["body"] == "see the attached file"
    assert "Price USD/ct" not in stored_message["body"]

    assert result["saved_offer_id"] is not None


def test_negotiation_stage_reply_stores_clean_body_but_analyzes_the_full_text(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    supplier_a = supplier_ids["email"]
    case_id = create_case(
        item_material="test sapphire",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_a],
    )

    saved_offer_id = add_offer(
        case_id=case_id,
        supplier_id=supplier_a,
        unit_price_usd=50.0,
        extraction_method="manual",
        extraction_confidence="human_verified",
        notes="Initial offer for test setup.",
    )
    repo.upsert_case_negotiation_context(
        case_id=case_id,
        initial_best_offer_usd=50.0,
        target_price_usd=45.0,
        best_supplier_id=supplier_a,
        best_offer_id=saved_offer_id,
        valid_offer_count=1,
        target_discount_percent=10.0,
        ranking_json="[]",
    )
    repo.update_case_status_with_event(
        case_id=case_id,
        status=CaseState.NEGOTIATING.value,
        event_type="test_setup_negotiating",
        details="Test fixture.",
    )
    repo.add_message(
        case_id=case_id,
        supplier_id=supplier_a,
        direction="outbound",
        channel="manual",
        body="Could you please confirm whether you can reach USD 45.00?",
        status="sent_simulated",
        message_type="price_reduction_request",
        approval_required=False,
        approved_by_buyer=True,
    )

    seen_message_bodies = []

    def fake_analyze(**kwargs):
        seen_message_bodies.append(kwargs["message_body"])
        return {
            "success": True,
            "message_category": "IMPROVED_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "unit_price_usd": 46.0,
            "requires_human_review": False,
            "safe_for_automation": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "confidence": "high",
            "reason": "Supplier improved the price.",
        }

    monkeypatch.setattr(
        negotiation_reply_service, "analyze_supplier_message_with_ollama", fake_analyze
    )

    result = record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="see the attached file",
        analysis_text=(
            "see the attached file\n\nPrice USD/ct: 46"
        ),
    )

    assert len(seen_message_bodies) == 1
    assert "Price USD/ct: 46" in seen_message_bodies[0]

    stored_message = repo.get_message_by_id(result["inbound_message_id"])
    assert stored_message["body"] == "see the attached file"
    assert "Price USD/ct" not in stored_message["body"]
