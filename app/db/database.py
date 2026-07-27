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
        _apply_additive_migrations(conn)
        conn.commit()


# Additive, backward-compatible column migrations. schema.sql's
# CREATE TABLE IF NOT EXISTS statements only create a table on a brand-new
# database; a table that already exists on disk keeps its original columns
# forever unless something explicitly ALTERs it. Each entry here is applied
# with ALTER TABLE ... ADD COLUMN, guarded by a PRAGMA table_info check so it
# is safe to run on every startup, against both a fresh database (where
# schema.sql already created the table without these columns) and an
# existing production database created before this change.
_SUPPLIER_NEGOTIATION_STATE_MIGRATION_COLUMNS = {
    # Strategy used for the most recently sent negotiation round (one of
    # the NEGOTIATION_STRATEGY_* values in app/negotiation/negotiation_engine.py).
    "last_negotiation_strategy": "TEXT",
    # Price requested in the most recently sent negotiation round.
    "last_requested_price_usd": "REAL",
    # Strength of the supplier's most recent refusal, if any: NONE, SOFT,
    # FIRM. Persisted so the negotiation history survives a restart.
    "refusal_strength": "TEXT NOT NULL DEFAULT 'NONE'",
    # 1 once the supplier has explicitly asked not to be negotiated with
    # further. Distinct from `closed` so the reason a supplier stopped
    # negotiating remains visible after the fact.
    "hard_stop": "INTEGER NOT NULL DEFAULT 0",
}


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(supplier_negotiation_state)"
        ).fetchall()
    }

    for column_name, column_definition in (
        _SUPPLIER_NEGOTIATION_STATE_MIGRATION_COLUMNS.items()
    ):
        if column_name in existing_columns:
            continue

        conn.execute(
            "ALTER TABLE supplier_negotiation_state "
            f"ADD COLUMN {column_name} {column_definition}"
        )