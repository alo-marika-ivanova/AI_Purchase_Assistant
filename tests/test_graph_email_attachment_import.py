from __future__ import annotations

import base64
from io import BytesIO

import pytest
import requests
from openpyxl import Workbook

from app.db.database import get_connection
from app.db.repository import PurchasingRepository
from app.integrations import graph_email_adapter
from app.services import email_transport_service
from app.services.case_service import create_case, create_case_from_detected_items


repo = PurchasingRepository()

# Matches the "email" supplier seeded by tests/conftest.py::_insert_test_suppliers.
TEST_SUPPLIER_EMAIL = "supplier.email@example.test"


def _build_prices_xlsx_bytes() -> bytes:
    """A real xlsx binary, matching the RFQ template layout, with two
    clean unhedged rows - what the deterministic multi-item safeguard
    expects to see once extract_text_from_spreadsheet flattens it."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(
        [
            "ALO ID",
            "Description",
            "Needed quantity, pcs",
            "Eleonora IMPORTANT notes",
            "Price USD/ct",
        ]
    )
    worksheet.append(
        ["PKGRPI500", "Garnet pink round regular 5 mm", 12, "3 sets", 44]
    )
    worksheet.append(
        ["PKPE200", "Peridot round regular 2 mm", 100, "matching", 20]
    )

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------
# graph_email_adapter: Graph API request/response handling
# ---------------------------------------------------------------------


class _FakeGraphResponse:
    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)

    def json(self) -> dict:
        return self._json_body


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def test_list_recent_inbox_messages_requests_attachments_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain message-list call never returns attachment content - the
    $expand=attachments query param is what makes Graph include it."""
    monkeypatch.setattr(
        graph_email_adapter, "get_graph_access_token", lambda: "fake-token"
    )

    captured_params = {}

    def fake_get(url, headers, params, timeout):
        captured_params.update(params)
        return _FakeGraphResponse(200, {"value": []})

    monkeypatch.setattr(requests, "get", fake_get)

    graph_email_adapter.list_recent_inbox_messages(user_email="buyer@example.test")

    assert captured_params.get("$expand") == "attachments"
    assert "hasAttachments" in captured_params.get("$select", "")


def test_list_recent_inbox_messages_decodes_file_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graph_email_adapter, "get_graph_access_token", lambda: "fake-token"
    )

    xlsx_bytes = b"fake xlsx bytes"

    def fake_get(url, headers, params, timeout):
        return _FakeGraphResponse(
            200,
            {
                "value": [
                    {
                        "id": "msg-1",
                        "subject": "[CASE-1] RFQ",
                        "from": {"emailAddress": {"name": "Supplier", "address": "s@example.test"}},
                        "receivedDateTime": "2026-08-07T10:00:00Z",
                        "body": {"content": "see the attachment"},
                        "bodyPreview": "see the attachment",
                        "internetMessageId": "<abc@example.test>",
                        "conversationId": "conv-1",
                        "internetMessageHeaders": [],
                        "attachments": [
                            {
                                "@odata.type": "#microsoft.graph.fileAttachment",
                                "name": "prices.xlsx",
                                "contentType": "application/vnd.openxmlformats",
                                "isInline": False,
                                "contentBytes": _b64(xlsx_bytes),
                            },
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(requests, "get", fake_get)

    messages = graph_email_adapter.list_recent_inbox_messages(
        user_email="buyer@example.test"
    )

    assert len(messages) == 1
    attachments = messages[0]["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "prices.xlsx"
    assert attachments[0]["content_bytes"] == xlsx_bytes


def test_list_recent_inbox_messages_skips_inline_and_non_file_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graph_email_adapter, "get_graph_access_token", lambda: "fake-token"
    )

    def fake_get(url, headers, params, timeout):
        return _FakeGraphResponse(
            200,
            {
                "value": [
                    {
                        "id": "msg-1",
                        "subject": "[CASE-1] RFQ",
                        "from": {"emailAddress": {"name": "Supplier", "address": "s@example.test"}},
                        "receivedDateTime": "2026-08-07T10:00:00Z",
                        "body": {"content": "hi"},
                        "bodyPreview": "hi",
                        "internetMessageId": "<abc@example.test>",
                        "conversationId": "conv-1",
                        "internetMessageHeaders": [],
                        "attachments": [
                            {
                                "@odata.type": "#microsoft.graph.fileAttachment",
                                "name": "logo.png",
                                "isInline": True,
                                "contentBytes": _b64(b"inline-logo"),
                            },
                            {
                                "@odata.type": "#microsoft.graph.itemAttachment",
                                "name": "Forwarded email",
                            },
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(requests, "get", fake_get)

    messages = graph_email_adapter.list_recent_inbox_messages(
        user_email="buyer@example.test"
    )

    assert messages[0]["attachments"] == []


# ---------------------------------------------------------------------
# email_transport_service: importing a real inbound email with attachments
# ---------------------------------------------------------------------


def _create_two_subcase_email_case(supplier_id: int) -> int:
    return create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet pink round regular 5 mm",
                "quantity": 12.0,
                "supplier_ids": [supplier_id],
            },
            {
                "item_material": "Peridot round regular 2 mm",
                "quantity": 100.0,
                "supplier_ids": [supplier_id],
            },
        ],
        notes="",
    )


def _fake_graph_email(
    case_number: str,
    sender_email: str,
    body: str,
    attachments: list[dict] | None = None,
    graph_message_id: str = "graph-msg-1",
) -> dict:
    return {
        "graph_message_id": graph_message_id,
        "internet_message_id": f"<{graph_message_id}@example.test>",
        "in_reply_to": None,
        "references": None,
        "graph_conversation_id": "conv-1",
        "subject": f"[{case_number}] RFQ - test",
        "sender_name": "Supplier",
        "sender_email": sender_email,
        "received_at": "2026-08-07T10:00:00Z",
        "body": body,
        "body_preview": body,
        "attachments": attachments or [],
    }


def test_real_inbound_email_with_xlsx_attachment_is_priced_and_saved(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the real bug: a supplier's real email reply with an xlsx
    attachment (body "see the attachment") must have that attachment fed
    into price extraction AND saved/linked so it's visible in the UI - not
    silently dropped."""
    supplier_id = supplier_ids["email"]
    case_id = _create_two_subcase_email_case(supplier_id)

    from app.services import simple_chat_service

    simple_chat_service.start_negotiating_case(case_id)

    case_number = repo.get_case_basic(case_id)["case_number"]
    monkeypatch.setenv("BUYER_EMAIL", "buyer@example.test")

    email = _fake_graph_email(
        case_number=case_number,
        sender_email=TEST_SUPPLIER_EMAIL,
        body="see the attachment",
        attachments=[
            {
                "filename": "prices.xlsx",
                "content_bytes": _build_prices_xlsx_bytes(),
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
        ],
    )

    monkeypatch.setattr(
        email_transport_service,
        "list_recent_inbox_messages",
        lambda user_email, top: [email],
    )

    result = email_transport_service.import_supplier_emails_for_case(case_id)

    assert result["imported_count"] == 1
    assert result["results"][0]["imported"] is True

    items = repo.list_case_items(case_id)
    items_by_name = {item["item_material"]: item for item in items}

    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Garnet pink round regular 5 mm"]["id"], supplier_id
    )
    peridot_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Peridot round regular 2 mm"]["id"], supplier_id
    )
    assert garnet_offer is not None and garnet_offer["unit_price_usd"] == pytest.approx(44.0)
    assert peridot_offer is not None and peridot_offer["unit_price_usd"] == pytest.approx(20.0)

    case_attachments = repo.list_attachments_for_case(case_id)
    assert len(case_attachments) == 1
    assert case_attachments[0]["original_filename"] == "prices.xlsx"
    assert case_attachments[0]["direction"] == "inbound"


def test_quoted_history_in_the_body_does_not_swallow_the_extracted_prices(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces a real bug: most mail clients (including Seznam.cz
    webmail) automatically quote the original message below a reply. If
    that quoted-history marker sits in the email body, and the extracted
    spreadsheet text is appended AFTER the body to build analysis_text,
    record_supplier_message_simple's later quote-stripping pass
    (_extract_supplier_authored_text) would cut off everything from the
    marker onward - discarding the real price data along with the quote,
    even though extraction itself succeeded perfectly. The marker must be
    stripped from the body BEFORE the price text is appended, not after."""
    supplier_id = supplier_ids["email"]
    case_id = _create_two_subcase_email_case(supplier_id)

    from app.services import simple_chat_service

    simple_chat_service.start_negotiating_case(case_id)

    case_number = repo.get_case_basic(case_id)["case_number"]
    monkeypatch.setenv("BUYER_EMAIL", "buyer@example.test")

    body_with_quoted_history = (
        "Please see the attached prices.\n\n"
        "---------- Původní e-mail ----------\n"
        "From: buyer@example.test\n"
        "Subject: RFQ\n"
        "Could you please quote your best unit price in USD?"
    )

    email = _fake_graph_email(
        case_number=case_number,
        sender_email=TEST_SUPPLIER_EMAIL,
        body=body_with_quoted_history,
        attachments=[
            {
                "filename": "prices.xlsx",
                "content_bytes": _build_prices_xlsx_bytes(),
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
        ],
    )

    monkeypatch.setattr(
        email_transport_service,
        "list_recent_inbox_messages",
        lambda user_email, top: [email],
    )

    result = email_transport_service.import_supplier_emails_for_case(case_id)

    assert result["imported_count"] == 1

    items = repo.list_case_items(case_id)
    items_by_name = {item["item_material"]: item for item in items}

    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Garnet pink round regular 5 mm"]["id"], supplier_id
    )
    peridot_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Peridot round regular 2 mm"]["id"], supplier_id
    )
    assert garnet_offer is not None and garnet_offer["unit_price_usd"] == pytest.approx(44.0)
    assert peridot_offer is not None and peridot_offer["unit_price_usd"] == pytest.approx(20.0)


def test_real_inbound_email_with_only_an_attachment_and_empty_body_still_imports(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supplier who attaches a file without typing any body text must not
    be silently skipped as "email body is empty"."""
    supplier_id = supplier_ids["email"]
    case_id = _create_two_subcase_email_case(supplier_id)

    from app.services import simple_chat_service

    simple_chat_service.start_negotiating_case(case_id)

    case_number = repo.get_case_basic(case_id)["case_number"]
    monkeypatch.setenv("BUYER_EMAIL", "buyer@example.test")

    email = _fake_graph_email(
        case_number=case_number,
        sender_email=TEST_SUPPLIER_EMAIL,
        body="",
        attachments=[
            {
                "filename": "prices.xlsx",
                "content_bytes": _build_prices_xlsx_bytes(),
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
        ],
    )

    monkeypatch.setattr(
        email_transport_service,
        "list_recent_inbox_messages",
        lambda user_email, top: [email],
    )

    result = email_transport_service.import_supplier_emails_for_case(case_id)

    assert result["imported_count"] == 1
    messages = repo.list_messages_for_case_supplier(case_id, supplier_id)
    inbound = [m for m in messages if m["direction"] == "inbound"][0]
    assert "prices.xlsx" in inbound["body"]


def test_real_inbound_email_with_non_spreadsheet_attachment_is_saved_but_not_parsed(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF/image attachment can't be read as a price table - it must
    still be saved for the buyer to see, without crashing or being fed into
    extract_text_from_spreadsheet."""
    supplier_id = supplier_ids["email"]
    case_id = create_case(
        item_material="Tanzanite (TAN)",
        quantity=40.0,
        notes="",
        supplier_ids=[supplier_id],
    )

    from app.services import simple_chat_service

    simple_chat_service.start_negotiating_case(case_id)

    case_number = repo.get_case_basic(case_id)["case_number"]
    monkeypatch.setenv("BUYER_EMAIL", "buyer@example.test")

    email = _fake_graph_email(
        case_number=case_number,
        sender_email=TEST_SUPPLIER_EMAIL,
        body="Our formal quote is attached, please see the PDF.",
        attachments=[
            {
                "filename": "quote.pdf",
                "content_bytes": b"%PDF-1.4 fake pdf bytes",
                "mime_type": "application/pdf",
            }
        ],
    )

    monkeypatch.setattr(
        email_transport_service,
        "list_recent_inbox_messages",
        lambda user_email, top: [email],
    )

    result = email_transport_service.import_supplier_emails_for_case(case_id)

    assert result["imported_count"] == 1

    case_attachments = repo.list_attachments_for_case(case_id)
    assert len(case_attachments) == 1
    assert case_attachments[0]["original_filename"] == "quote.pdf"


def test_unparseable_spreadsheet_attachment_is_saved_and_logged_not_silent(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file named like a spreadsheet (e.g. a legacy .xls saved with an
    .xlsx extension, or a genuinely corrupted upload) that openpyxl cannot
    parse must not fail as indistinguishably from "no attachment was ever
    sent" - it's still saved, and the failure is logged so it's
    diagnosable instead of silently disappearing."""
    supplier_id = supplier_ids["email"]
    case_id = create_case(
        item_material="Tanzanite (TAN)",
        quantity=40.0,
        notes="",
        supplier_ids=[supplier_id],
    )

    from app.services import simple_chat_service

    simple_chat_service.start_negotiating_case(case_id)

    case_number = repo.get_case_basic(case_id)["case_number"]
    monkeypatch.setenv("BUYER_EMAIL", "buyer@example.test")

    email = _fake_graph_email(
        case_number=case_number,
        sender_email=TEST_SUPPLIER_EMAIL,
        body="Please see the attached prices.",
        attachments=[
            {
                "filename": "prices.xlsx",
                "content_bytes": b"not a real xlsx file",
                "mime_type": "application/vnd.openxmlformats",
            }
        ],
    )

    monkeypatch.setattr(
        email_transport_service,
        "list_recent_inbox_messages",
        lambda user_email, top: [email],
    )
    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: {
            "success": True,
            "message_category": "UNCLEAR_PRICE",
            "recommended_action": "ASK_PRICE_CLARIFICATION",
            "safe_for_automation": True,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "confidence": "low",
            "unit_price_usd": None,
            "currency": None,
            "price_basis": "NONE",
            "is_price_clear": False,
            "is_currency_clear": False,
            "has_multiple_prices": False,
            "is_conditional": False,
            "reason": "No usable price found in the message text.",
        },
    )

    result = email_transport_service.import_supplier_emails_for_case(case_id)

    assert result["imported_count"] == 1

    case_attachments = repo.list_attachments_for_case(case_id)
    assert len(case_attachments) == 1
    assert case_attachments[0]["original_filename"] == "prices.xlsx"

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT event_type, details FROM negotiation_events
            WHERE case_id = ? AND event_type = 'attachment_extraction_failed'
            """,
            (case_id,),
        ).fetchall()

    assert len(rows) == 1
    assert "prices.xlsx" in rows[0]["details"]


def test_spreadsheet_that_parses_but_has_no_readable_content_is_logged(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces a real bug: an attachment that opens fine in Excel and
    parses without raising in openpyxl, but whose cells all read back as
    None/empty (e.g. cached formula values not preserved through some
    email clients), produced empty extracted text - is_attachment_reply
    still ended up True with nothing useful in analysis_text, so the LLM
    saw only the plain body and concluded no price was ever provided. This
    failure mode must be distinguished from a parse exception and logged,
    not silently treated as if the file were never attached."""
    supplier_id = supplier_ids["email"]
    case_id = create_case(
        item_material="Tanzanite (TAN)",
        quantity=40.0,
        notes="",
        supplier_ids=[supplier_id],
    )

    from app.services import simple_chat_service

    simple_chat_service.start_negotiating_case(case_id)

    case_number = repo.get_case_basic(case_id)["case_number"]
    monkeypatch.setenv("BUYER_EMAIL", "buyer@example.test")

    empty_workbook = Workbook()
    buffer = BytesIO()
    empty_workbook.save(buffer)

    email = _fake_graph_email(
        case_number=case_number,
        sender_email=TEST_SUPPLIER_EMAIL,
        body="Please see the attached prices.",
        attachments=[
            {
                "filename": "prices.xlsx",
                "content_bytes": buffer.getvalue(),
                "mime_type": "application/vnd.openxmlformats",
            }
        ],
    )

    monkeypatch.setattr(
        email_transport_service,
        "list_recent_inbox_messages",
        lambda user_email, top: [email],
    )
    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: {
            "success": True,
            "message_category": "UNCLEAR_PRICE",
            "recommended_action": "ASK_PRICE_CLARIFICATION",
            "safe_for_automation": True,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "confidence": "low",
            "unit_price_usd": None,
            "currency": None,
            "price_basis": "NONE",
            "is_price_clear": False,
            "is_currency_clear": False,
            "has_multiple_prices": False,
            "is_conditional": False,
            "reason": "No usable price found in the message text.",
        },
    )

    result = email_transport_service.import_supplier_emails_for_case(case_id)

    assert result["imported_count"] == 1

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT event_type, details FROM negotiation_events
            WHERE case_id = ? AND event_type = 'attachment_extraction_failed'
            """,
            (case_id,),
        ).fetchall()

    assert len(rows) == 1
    assert "no readable cell content" in rows[0]["details"]


def test_plain_text_only_reply_without_attachments_still_works(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression check: an ordinary attachment-free reply must import
    exactly as before this change."""
    supplier_id = supplier_ids["email"]
    case_id = create_case(
        item_material="Tanzanite (TAN)",
        quantity=40.0,
        notes="",
        supplier_ids=[supplier_id],
    )

    from app.services import simple_chat_service

    simple_chat_service.start_negotiating_case(case_id)

    case_number = repo.get_case_basic(case_id)["case_number"]
    monkeypatch.setenv("BUYER_EMAIL", "buyer@example.test")

    email = _fake_graph_email(
        case_number=case_number,
        sender_email=TEST_SUPPLIER_EMAIL,
        body="We can do 180 usd/ct.",
        attachments=[],
    )

    monkeypatch.setattr(
        email_transport_service,
        "list_recent_inbox_messages",
        lambda user_email, top: [email],
    )
    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: {
            "success": True,
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "safe_for_automation": True,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "confidence": "high",
            "unit_price_usd": 180.0,
            "currency": "USD",
            "price_basis": "UNIT",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "reason": "Single clear price.",
        },
    )

    result = email_transport_service.import_supplier_emails_for_case(case_id)

    assert result["imported_count"] == 1
    offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    assert offer is not None
    assert offer["unit_price_usd"] == pytest.approx(180.0)
    assert repo.list_attachments_for_case(case_id) == []


def test_email_thread_header_keeps_the_real_in_reply_to_and_references(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for a duplicate record_email_message_header call
    that immediately overwrote the correct threading data (in_reply_to,
    reference_chain) written moments earlier with in_reply_to=None and
    reference_chain set to the message's own ID - breaking future
    outbound-reply threading for every imported email."""
    supplier_id = supplier_ids["email"]
    case_id = create_case(
        item_material="Tanzanite (TAN)",
        quantity=40.0,
        notes="",
        supplier_ids=[supplier_id],
    )

    from app.services import simple_chat_service

    simple_chat_service.start_negotiating_case(case_id)

    case_number = repo.get_case_basic(case_id)["case_number"]
    monkeypatch.setenv("BUYER_EMAIL", "buyer@example.test")

    email = _fake_graph_email(
        case_number=case_number,
        sender_email=TEST_SUPPLIER_EMAIL,
        body="We can do 180 usd/ct.",
        attachments=[],
    )
    email["in_reply_to"] = "<rfq-message-id@example.test>"
    email["references"] = "<rfq-message-id@example.test>"

    monkeypatch.setattr(
        email_transport_service,
        "list_recent_inbox_messages",
        lambda user_email, top: [email],
    )
    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: {
            "success": True,
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "safe_for_automation": True,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "confidence": "high",
            "unit_price_usd": 180.0,
            "currency": "USD",
            "price_basis": "UNIT",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "reason": "Single clear price.",
        },
    )

    result = email_transport_service.import_supplier_emails_for_case(case_id)
    assert result["imported_count"] == 1

    header = repo.get_latest_email_thread_header(case_id, supplier_id)
    assert header is not None
    assert header["in_reply_to"] == "<rfq-message-id@example.test>"
    assert header["reference_chain"] == "<rfq-message-id@example.test>"
