from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from app.db.database import DB_PATH, initialize_database
from import_supplier_filter_xlsx import (
    get_default_xlsx_path,
    import_workbook,
)


load_dotenv()


def main() -> None:
    existed_before = DB_PATH.exists()

    initialize_database()

    xlsx_path = get_default_xlsx_path()

    print(f"Database path: {DB_PATH}")
    print("Database already existed." if existed_before else "Database created.")
    print(f"Supplier catalog XLSX: {xlsx_path}")

    if not xlsx_path.exists():
        raise FileNotFoundError(
            "Supplier catalog XLSX not found. "
            "Expected data/supplier_catalog.xlsx "
            "or set SUPPLIER_CATALOG_XLSX in .env."
        )

    result = import_workbook(
        xlsx_path=xlsx_path,
        dry_run=False,
    )

    print("Supplier catalog imported from XLSX.")

    for key, value in result.items():
        print(f"{key}: {value}")

    print("Initialization complete.")


if __name__ == "__main__":
    main()