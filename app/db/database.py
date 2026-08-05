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
        _apply_negotiation_cases_migrations(conn)
        _apply_case_item_id_migrations(conn)
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
    # No-response reminder tracking for the current, still-unanswered
    # negotiation round. Reset to 0 / NULL whenever a reply arrives or a
    # new round is sent (see repository.update_negotiation_state_after_inbound
    # and repository.record_negotiation_round_sent).
    "negotiation_reminder_count": "INTEGER NOT NULL DEFAULT 0",
    "next_negotiation_reminder_due_at": "TEXT",
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


def _apply_negotiation_cases_migrations(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(negotiation_cases)").fetchall()
    }

    # Links a case to the rfq_batches row it was created from, when it came
    # from an uploaded RFQ file resolving to multiple items (one case per
    # item). NULL for manually created single-item cases.
    if "batch_id" not in existing_columns:
        conn.execute("ALTER TABLE negotiation_cases ADD COLUMN batch_id INTEGER")


def _apply_case_item_id_migrations(conn: sqlite3.Connection) -> None:
    # Which specific order item (case_items row) an offer/winner decision
    # applies to. NULL for a legacy single-item case, where the case itself
    # is the only thing being priced.
    #
    # The index is created here, after the column is guaranteed to exist,
    # rather than in schema.sql: schema.sql's executescript runs before this
    # migration, so an index statement there would fail on a pre-existing
    # database whose offers/winner_decisions tables predate this column.
    for table in ("offers", "winner_decisions"):
        existing_columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "case_item_id" not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN case_item_id INTEGER")

        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_case_item_id "
            f"ON {table}(case_item_id)"
        )