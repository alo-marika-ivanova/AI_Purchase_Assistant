from __future__ import annotations

import hashlib
from pathlib import Path

import app.services.attachment_service as attachment_service_module
from app.db.repository import PurchasingRepository
from app.services.attachment_service import (
    list_case_attachments,
    save_case_attachment,
)
from app.services.case_service import create_case


repo = PurchasingRepository()


def _create_test_case(supplier_ids: dict[str, int]) -> int:
    return create_case(
        item_material="Amethyst Pink (AMP)",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_ids["email"]],
    )


def test_save_case_attachment_writes_file_and_metadata(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_test_case(supplier_ids)
    file_bytes = b"fake spreadsheet bytes"

    result = save_case_attachment(
        case_id=case_id,
        original_filename="offer.xlsx",
        file_bytes=file_bytes,
    )

    stored_path = Path(result["stored_path"])
    assert stored_path.exists()
    assert stored_path.read_bytes() == file_bytes
    assert (
        stored_path.parent
        == attachment_service_module.ATTACHMENT_STORAGE_DIR / str(case_id)
    )
    assert result["sha256_hash"] == hashlib.sha256(file_bytes).hexdigest()
    assert result["mime_type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert result["size_bytes"] == len(file_bytes)


def test_save_case_attachment_preserves_original_filename_with_uuid_storage_name(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_test_case(supplier_ids)

    result = save_case_attachment(
        case_id=case_id,
        original_filename="my report (final).pdf",
        file_bytes=b"%PDF-1.4 fake",
    )

    stored_path = Path(result["stored_path"])
    assert stored_path.name != "my report (final).pdf"
    assert stored_path.suffix == ".pdf"

    attachments = list_case_attachments(case_id)
    assert len(attachments) == 1
    assert attachments[0]["original_filename"] == "my report (final).pdf"
    assert attachments[0]["stored_path"] == result["stored_path"]


def test_list_case_attachments_returns_files_in_upload_order(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_test_case(supplier_ids)

    save_case_attachment(case_id=case_id, original_filename="a.csv", file_bytes=b"a")
    save_case_attachment(case_id=case_id, original_filename="b.csv", file_bytes=b"b")

    attachments = list_case_attachments(case_id)
    assert [a["original_filename"] for a in attachments] == ["a.csv", "b.csv"]


def test_attachment_defaults_to_manual_outbound_with_no_supplier_or_message(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_test_case(supplier_ids)

    save_case_attachment(case_id=case_id, original_filename="a.csv", file_bytes=b"a")

    attachment = list_case_attachments(case_id)[0]
    assert attachment["channel"] == "manual"
    assert attachment["direction"] == "outbound"
    assert attachment["supplier_id"] is None
    assert attachment["message_id"] is None


def test_get_case_details_includes_attachments(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_test_case(supplier_ids)
    save_case_attachment(case_id=case_id, original_filename="a.csv", file_bytes=b"a")

    details = repo.get_case_details(case_id)

    assert details is not None
    assert len(details["attachments"]) == 1
    assert details["attachments"][0]["original_filename"] == "a.csv"


def test_get_case_details_reports_no_attachments_for_a_fresh_case(
    supplier_ids: dict[str, int],
) -> None:
    case_id = _create_test_case(supplier_ids)

    details = repo.get_case_details(case_id)

    assert details is not None
    assert details["attachments"] == []


def test_attachments_are_isolated_per_case(
    supplier_ids: dict[str, int],
) -> None:
    case_id_1 = _create_test_case(supplier_ids)
    case_id_2 = _create_test_case(supplier_ids)

    save_case_attachment(
        case_id=case_id_1, original_filename="only-in-1.csv", file_bytes=b"x"
    )

    assert len(list_case_attachments(case_id_1)) == 1
    assert list_case_attachments(case_id_2) == []
