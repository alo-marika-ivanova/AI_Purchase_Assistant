from __future__ import annotations

import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DB_PATH = Path(
    os.getenv(
        "PURCHASING_AI_DB_PATH",
        str(PROJECT_ROOT / "purchasing_ai.sqlite3"),
    )
)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    """Return a SQLite connection configured for app + worker usage."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    return conn


def initialize_database() -> None:
    """Create tables if they do not exist. Existing data is preserved."""
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        conn.executescript(schema_sql)
        conn.commit()