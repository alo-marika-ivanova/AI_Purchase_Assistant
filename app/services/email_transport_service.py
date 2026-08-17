from __future__ import annotations

import os
import re

from dotenv import load_dotenv

from app.db.repository import PurchasingRepository
from app.integrations.graph_email_adapter import list_recent_inbox_messages
from app.services.attachment_service import (
    extract_text_from_spreadsheet,
    save_case_attachment,
)
from app.services.simple_chat_service import (
    _extract_supplier_authored_text,
    record_supplier_message_simple,
)

_SPREADSHEET_EXTENSIONS = (".csv", ".xlsx", ".xlsm")

load_dotenv()

repo = PurchasingRepository()

def _normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _system_sender_emails() -> set[str]:
    """
    Emails that belong to our own buyer/system account.

    These must never be imported as supplier replies.
    Otherwise the worker can accidentally read its own sent emails
    and create a send/receive loop.
    """
    values = {
        os.getenv("BUYER_EMAIL"),
        os.getenv("SMTP_USER"),
    }

    extra_ignored = os.getenv("EMAIL_IGNORE_SENDERS", "")
    for item in extra_ignored.split(","):
        if item.strip():
            values.add(item.strip())

    return {
        _normalize_email(value)
        for value in values
        if _normalize_email(value)
    }


def _build_display_body_and_analysis_text(
    body: str, attachments: list[dict]
) -> tuple[str, str | None, list[str]]:
    """Fold any spreadsheet attachment(s) into analysis_text, mirroring the
    Streamlit "simulate a supplier reply" upload path exactly (see
    ui/streamlit_app_clean.py) so a real inbound email with a filled-in
    price file is interpreted the same way as a manually simulated one.

    A non-spreadsheet attachment (e.g. a PDF or image) is not readable as
    text here and is left out of analysis_text - it is still saved via
    save_case_attachment by the caller so it's visible/downloadable, just
    not fed to the price-extraction pipeline.

    A spreadsheet-named attachment that fails to parse (e.g. a legacy .xls
    saved with an .xlsx extension, or a genuinely corrupted file), or one
    that parses without error but yields no readable cell content at all
    (e.g. an openpyxl data_only=True read of a file whose cached formula
    values weren't preserved), is reported back in the third return value
    - either failure mode must be logged by the caller, not be silently
    indistinguishable from "no spreadsheet was attached at all".
    """
    display_body = (body or "").strip()

    # A real reply's body often carries the buyer's own quoted original
    # message below the supplier's actual text (most mail clients insert
    # this automatically on reply). record_supplier_message_simple strips
    # that out of whatever text it's given via _extract_supplier_authored_text
    # before classification - but analysis_text puts the extracted
    # spreadsheet content AFTER the body, so if that stripping ran on the
    # combined string, it would cut off everything from the quote marker
    # onward, discarding the real price data along with the quoted history.
    # Stripping it here, before concatenating, means there is no marker
    # left for that later pass to find - the price data survives intact.
    authored_body = _extract_supplier_authored_text(display_body)

    extracted_blocks = []
    extraction_issues = []
    for attachment in attachments:
        filename = attachment.get("filename") or ""
        if not filename.lower().endswith(_SPREADSHEET_EXTENSIONS):
            continue
        try:
            extracted = extract_text_from_spreadsheet(
                attachment["content_bytes"], filename
            )
        except Exception as exc:
            # A malformed/unreadable spreadsheet must not block importing
            # the email itself - it's still saved for the buyer to inspect
            # manually, just without automatic price extraction.
            extraction_issues.append(
                f"{filename}: failed to parse ({exc})"
            )
            continue

        if not extracted.strip():
            extraction_issues.append(
                f"{filename}: parsed with no error but produced no "
                "readable cell content"
            )
            continue

        extracted_blocks.append(extracted)

    if not extracted_blocks:
        return display_body, None, extraction_issues

    extracted_text = "\n\n".join(extracted_blocks)
    analysis_text = (
        f"{authored_body}\n\n{extracted_text}" if authored_body else extracted_text
    )

    if not display_body:
        attachment_names = ", ".join(
            attachment.get("filename") or "attachment"
            for attachment in attachments
        )
        display_body = f"(Replied with an attached file: {attachment_names})"

    return display_body, analysis_text, extraction_issues


def build_case_email_subject(
    case_number: str,
    item_material: str,
    supplier_code: str | None = None,
) -> str:
    """
    Subject convention used for threading and importing.

    Supplier code is included so replies can be mapped correctly even when
    several test suppliers use the same email address.
    """

    if supplier_code:
        return f"[{case_number}] [SUPPLIER:{supplier_code}] RFQ - {item_material}"

    return f"[{case_number}] RFQ - {item_material}"

def extract_supplier_code_from_subject(subject: str) -> str | None:
    """
    Extract supplier code from subject like:

    [CASE-20260611-130911] [SUPPLIER:BRIGHT] RFQ - para
    """

    match = re.search(r"\[SUPPLIER:([^\]]+)\]", subject)

    if not match:
        return None

    return match.group(1).strip()



def import_supplier_emails_for_case(case_id: int, top: int = 25) -> dict:
    """
    Import supplier replies from the buyer mailbox into the selected case.

    Matching rules:
    1. Subject must contain the case number.
    2. Sender email must match a selected supplier email.
    3. In EMAIL_TEST_MODE, EMAIL_TEST_SUPPLIER_TO can be accepted for one-supplier tests.

    Safety rules:
    - Do not import emails sent by buyer/system account.
    - Do not import the same Graph message twice.
    - Do not import duplicate inbound bodies.
    """

    buyer_email = os.getenv("BUYER_EMAIL", "").strip()
    if not buyer_email:
        raise ValueError("BUYER_EMAIL is missing in .env.")

    email_test_mode = os.getenv("EMAIL_TEST_MODE", "true").lower() == "true"
    test_supplier_email = _normalize_email(os.getenv("EMAIL_TEST_SUPPLIER_TO", ""))

    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    case_number = case_data["case_number"]
    selected_suppliers = repo.list_case_suppliers(case_id)

    recent_messages = list_recent_inbox_messages(
        user_email=buyer_email,
        top=top,
    )

    imported_count = 0
    skipped_count = 0
    results = []

    for email in recent_messages:
        graph_message_id = email.get("graph_message_id")
        subject = email.get("subject") or ""
        sender_email = email.get("sender_email") or ""
        sender_email_normalized = _normalize_email(sender_email)
        body = email.get("body") or ""
        attachments = email.get("attachments") or []
        display_body, analysis_text, extraction_issues = (
            _build_display_body_and_analysis_text(body, attachments)
        )

        basic_result = {
            "subject": subject,
            "sender_email": sender_email,
            "graph_message_id": graph_message_id,
        }

        if not graph_message_id:
            skipped_count += 1
            results.append(
                {
                    **basic_result,
                    "imported": False,
                    "reason": "Skipped: missing Graph message ID.",
                }
            )
            continue

        if sender_email_normalized in _system_sender_emails():
            skipped_count += 1
            results.append(
                {
                    **basic_result,
                    "imported": False,
                    "reason": "Skipped: email was sent by buyer/system account.",
                }
            )
            continue

        if repo.email_import_exists(graph_message_id):
            skipped_count += 1
            results.append(
                {
                    **basic_result,
                    "imported": False,
                    "reason": "Skipped: email already imported.",
                }
            )
            continue

        if case_number not in subject:
            skipped_count += 1
            results.append(
                {
                    **basic_result,
                    "imported": False,
                    "reason": f"Skipped: subject does not contain case number {case_number}.",
                }
            )
            continue

        supplier = None

        supplier_code_from_subject = extract_supplier_code_from_subject(subject)

        if supplier_code_from_subject:
            supplier = repo.find_case_supplier_by_code(
                case_id=case_id,
                supplier_code=supplier_code_from_subject,
            )

        if supplier is None:
            supplier = repo.find_case_supplier_by_email(
                case_id=case_id,
                sender_email=sender_email,
            )

        if supplier is None and email_test_mode and sender_email_normalized == test_supplier_email:
            if len(selected_suppliers) == 1:
                supplier = selected_suppliers[0]
            else:
                skipped_count += 1
                results.append(
                    {
                        **basic_result,
                        "imported": False,
                        "reason": (
                            "Skipped: test email matched but supplier could not be identified. "
                            "For multi-supplier testing, reply to emails with subject containing "
                            "[SUPPLIER:CODE]."
                        ),
                    }
                )
                continue

        if supplier is None:
            skipped_count += 1
            results.append(
                {
                    **basic_result,
                    "imported": False,
                    "reason": "Skipped: sender is not a selected supplier for this case.",
                }
            )
            continue

        if not display_body.strip():
            skipped_count += 1
            results.append(
                {
                    **basic_result,
                    "supplier_name": supplier["name"],
                    "imported": False,
                    "reason": "Skipped: email body is empty.",
                }
            )
            continue

        if extraction_issues:
            repo.log_worker_event(
                case_id=case_id,
                event_type="attachment_extraction_failed",
                details=(
                    "Could not read price data from attachment(s): "
                    f"{'; '.join(extraction_issues)}. The file was saved "
                    "for manual review, but automatic price extraction used "
                    "only the email's typed text."
                ),
            )

        if repo.inbound_message_duplicate_exists(
            case_id=case_id,
            supplier_id=int(supplier["id"]),
            channel="email",
            body=display_body,
        ):
            skipped_count += 1
            results.append(
                {
                    **basic_result,
                    "supplier_name": supplier["name"],
                    "imported": False,
                    "reason": "Skipped: duplicate inbound email body already exists in chat.",
                }
            )
            continue

        result = record_supplier_message_simple(
            case_id=case_id,
            supplier_id=int(supplier["id"]),
            channel="email",
            body=display_body,
            analysis_text=analysis_text,
        )
        inbound_message_id = int(result["inbound_message_id"])

        for attachment in attachments:
            save_case_attachment(
                case_id=case_id,
                original_filename=attachment.get("filename") or "attachment",
                file_bytes=attachment["content_bytes"],
                supplier_id=int(supplier["id"]),
                message_id=inbound_message_id,
                channel="email",
                direction="inbound",
            )

        internet_message_id = (
                email.get("internet_message_id")
                or email.get("internetMessageId")
                or email.get("message_id")
        )

        subject = email.get("subject") or ""

        graph_conversation_id = (
                email.get("conversation_id")
                or email.get("conversationId")
        )

        if internet_message_id:
            repo.record_email_message_header(
                message_id=inbound_message_id,
                case_id=case_id,
                supplier_id=int(supplier["id"]),
                subject=subject,
                internet_message_id=internet_message_id,
                in_reply_to=email.get("in_reply_to"),
                reference_chain=email.get("references"),
                graph_conversation_id=graph_conversation_id,
            )
        else:
            repo.log_worker_event(
                case_id=case_id,
                event_type="email_thread_header_missing",
                details=(
                    f"Imported email from {sender_email} but internet_message_id was missing. "
                    "Future outbound email may not thread correctly."
                ),
            )

        repo.record_email_import(
            graph_message_id=graph_message_id,
            case_id=case_id,
            message_id=int(result["inbound_message_id"]),
            sender_email=sender_email,
            subject=subject,
            received_at=email.get("received_at"),
        )

        imported_count += 1

        results.append(
            {
                **basic_result,
                "supplier_name": supplier["name"],
                "imported": True,
                "reason": "Imported into chat and price extraction executed.",
            }
        )

    return {
        "case_number": case_number,
        "recent_email_count": len(recent_messages),
        "imported_count": imported_count,
        "skipped_count": skipped_count,
        "results": results,
    }