from __future__ import annotations

from pathlib import Path

from app.db.database import get_connection
from app.services.rfq_detection_service import detect_rfq_selection


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "rfq_samples"

NATURAL_STONE_GOODS_NAMES = [
    "Amethyst African (AMA)",
    "Amethyst Green (AMG)",
    "Citrine Madeira (CIM)",
    "Garnet Pink",
    "Peridote (PER)",
    "Prehnite (PRE)",
    "Rhodolite Purple (RHF)",
    "Rhodolite Reddish (RHO)",
    "Spinel (SPI)",
    "Tanzanite (TAN)",
    "Topaz London (TOL)",
    "Topaz Sky (TOY)",
    "Topaz Swiss (TOS)",
]


def _seed_supplier_goods(
    supplier_id: int, goods_group: str, goods_names: list[str]
) -> None:
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO supplier_goods (supplier_id, goods_name, goods_group)
            VALUES (?, ?, ?)
            """,
            [(supplier_id, goods_name, goods_group) for goods_name in goods_names],
        )
        conn.commit()


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def test_natural_stone_rfq_is_recognized_and_matched(
    supplier_ids: dict[str, int],
) -> None:
    _seed_supplier_goods(
        supplier_ids["email"], "Precious stones", NATURAL_STONE_GOODS_NAMES
    )

    result = detect_rfq_selection(_read_fixture("prirodni.xlsx"), "prirodni.xlsx")

    assert result.recognized is True
    assert result.file_type == "natural stone"
    assert result.unresolved_lines == []

    detected_names = {item.goods_name for item in result.items}
    assert detected_names == set(NATURAL_STONE_GOODS_NAMES)


def test_natural_stone_rfq_sums_quantity_across_matching_rows(
    supplier_ids: dict[str, int],
) -> None:
    _seed_supplier_goods(
        supplier_ids["email"], "Precious stones", NATURAL_STONE_GOODS_NAMES
    )

    result = detect_rfq_selection(_read_fixture("prirodni.xlsx"), "prirodni.xlsx")

    by_name = {item.goods_name: item for item in result.items}
    # Amethyst afrikan round(16) + cushion(16) + octagon(12) + oval(16) = 60
    assert by_name["Amethyst African (AMA)"].quantity == 60.0
    # Peridot 2mm(100) + 4mm(24) + 5mm(16) = 140
    assert by_name["Peridote (PER)"].quantity == 140.0


def test_natural_stone_rfq_reports_unresolved_lines_for_unmatched_stones(
    supplier_ids: dict[str, int],
) -> None:
    # Only seed a subset of the catalog - the rest should fall to unresolved.
    _seed_supplier_goods(
        supplier_ids["email"], "Precious stones", ["Amethyst African (AMA)"]
    )

    result = detect_rfq_selection(_read_fixture("prirodni.xlsx"), "prirodni.xlsx")

    assert result.recognized is True
    detected_names = {item.goods_name for item in result.items}
    assert detected_names == {"Amethyst African (AMA)"}
    assert len(result.unresolved_lines) > 0
    assert any("Tanzanite" in line for line in result.unresolved_lines)


def test_brilliant_rfq_is_recognized_and_bucketed_as_colorless_up_to_1ct(
    supplier_ids: dict[str, int],
) -> None:
    _seed_supplier_goods(
        supplier_ids["email"], "Diamonds", ["up to 1ct", "1ct and up"]
    )

    result = detect_rfq_selection(_read_fixture("brilianty.xlsx"), "brilianty.xlsx")

    assert result.recognized is True
    assert result.file_type == "brilliant"
    assert result.unresolved_lines == []
    assert len(result.items) == 1
    assert result.items[0].goods_name == "up to 1ct"
    assert result.items[0].quantity == 1260.0


def test_detection_falls_back_when_catalog_is_empty(
    supplier_ids: dict[str, int],
) -> None:
    result = detect_rfq_selection(_read_fixture("prirodni.xlsx"), "prirodni.xlsx")

    assert result.recognized is False
    assert result.items == []


def test_detection_ignores_non_xlsx_filenames(
    supplier_ids: dict[str, int],
) -> None:
    result = detect_rfq_selection(b"item,quantity\nfoo,1\n", "offer.csv")

    assert result.recognized is False


def test_detection_reports_unrecognized_for_unrelated_xlsx(
    supplier_ids: dict[str, int],
) -> None:
    _seed_supplier_goods(
        supplier_ids["email"], "Precious stones", NATURAL_STONE_GOODS_NAMES
    )

    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    ws = workbook.active
    ws["A1"] = "Not an RFQ"
    ws["A2"] = "Just some notes"
    buffer = BytesIO()
    workbook.save(buffer)

    result = detect_rfq_selection(buffer.getvalue(), "notes.xlsx")

    assert result.recognized is False
