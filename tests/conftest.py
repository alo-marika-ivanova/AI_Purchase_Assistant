from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# app.db.database reads its default path at import time. Point that initial
# import at a harmless test-only location. The autouse fixture below then
# assigns a different database file to every test.
_TEST_SESSION_DIRECTORY = Path(
    tempfile.mkdtemp(prefix="aipurchase_pytest_")
)
os.environ["PURCHASING_AI_DB_PATH"] = str(
    _TEST_SESSION_DIRECTORY / "bootstrap.sqlite3"
)
os.environ["USE_LLM_COMMUNICATION_WRITER"] = "false"

import app.db.database as database_module
from app.db.database import get_connection, initialize_database


def _insert_test_suppliers() -> dict[str, int]:
    conn = get_connection()
    try:
        email_cursor = conn.execute(
            """
            INSERT INTO suppliers
            (
                supplier_code,
                name,
                contact_channel,
                email,
                active
            )
            VALUES (?, ?, 'email', ?, 1)
            """,
            (
                "TEST-EMAIL",
                "Test Email Supplier",
                "supplier.email@example.test",
            ),
        )

        whatsapp_cursor = conn.execute(
            """
            INSERT INTO suppliers
            (
                supplier_code,
                name,
                contact_channel,
                whatsapp_number,
                active
            )
            VALUES (?, ?, 'whatsapp', ?, 1)
            """,
            (
                "TEST-WHATSAPP",
                "Test WhatsApp Supplier",
                "+420700000001",
            ),
        )

        conn.commit()

        return {
            "email": int(email_cursor.lastrowid),
            "whatsapp": int(whatsapp_cursor.lastrowid),
        }
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test its own initialized SQLite database file.

    A unique file avoids deleting an SQLite database while application
    connections are still open, which Windows correctly rejects with
    PermissionError / WinError 32.
    """
    test_db_path = tmp_path / "test_purchasing_ai.sqlite3"
    monkeypatch.setattr(database_module, "DB_PATH", test_db_path)
    initialize_database()
    yield


@pytest.fixture
def supplier_ids() -> dict[str, int]:
    return _insert_test_suppliers()
