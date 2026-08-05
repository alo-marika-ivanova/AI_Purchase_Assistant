from __future__ import annotations

import hashlib
import mimetypes
import os
import uuid
from pathlib import Path

from app.db.repository import PurchasingRepository

repo = PurchasingRepository()

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ATTACHMENT_STORAGE_DIR = Path(
    os.getenv("ATTACHMENT_STORAGE_DIR", str(PROJECT_ROOT / "data" / "attachments"))
)


def save_case_attachment(
    case_id: int,
    original_filename: str,
    file_bytes: bytes,
    supplier_id: int | None = None,
    message_id: int | None = None,
    channel: str = "manual",
    direction: str = "outbound",
) -> dict:
    """Persist one uploaded file to disk and record its metadata.

    This is the single storage path shared by the UI, email worker, and
    WhatsApp webhook (later phases), so every attachment - regardless of
    channel - ends up hashed, stored under its case, and recorded the same
    way.
    """
    case_dir = ATTACHMENT_STORAGE_DIR / str(case_id)
    case_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(original_filename).suffix
    stored_path = case_dir / f"{uuid.uuid4().hex}{suffix}"
    stored_path.write_bytes(file_bytes)

    sha256_hash = hashlib.sha256(file_bytes).hexdigest()
    mime_type, _ = mimetypes.guess_type(original_filename)

    attachment_id = repo.add_attachment(
        case_id=case_id,
        original_filename=original_filename,
        stored_path=str(stored_path),
        mime_type=mime_type,
        size_bytes=len(file_bytes),
        sha256_hash=sha256_hash,
        supplier_id=supplier_id,
        message_id=message_id,
        channel=channel,
        direction=direction,
    )

    return {
        "id": attachment_id,
        "original_filename": original_filename,
        "stored_path": str(stored_path),
        "mime_type": mime_type,
        "size_bytes": len(file_bytes),
        "sha256_hash": sha256_hash,
    }


def list_case_attachments(case_id: int) -> list[dict]:
    return repo.list_attachments_for_case(case_id)
