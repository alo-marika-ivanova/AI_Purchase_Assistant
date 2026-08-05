from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from io import BytesIO

from openpyxl import load_workbook

from app.db.repository import PurchasingRepository

repo = PurchasingRepository()

NATURAL_STONE_GOODS_GROUP = "Precious stones"
BRILLIANT_GOODS_GROUP = "Diamonds"

# Below this fuzzy-match score, a description is treated as unresolved
# rather than guessed. Calibrated against the real prirodni.xlsx fixture
# (matches score 0.90-1.00) versus unrelated stone names (score <= 0.76).
NATURAL_STONE_MATCH_THRESHOLD = 0.82

_HEADER_SEARCH_ROWS = 10
_BRILLIANT_COLOR_KEYWORDS = ("blue", "green", "orange", "pink", "yellow")
_CT_PATTERN = re.compile(r"(\d+[.,]?\d*)\s*ct", re.IGNORECASE)
_MM_PATTERN = re.compile(r"\bmm\b", re.IGNORECASE)


@dataclass(frozen=True)
class DetectedRfqItem:
    """One catalog item (a stone, or a brilliant size/color bucket) found in
    an uploaded RFQ file, plus enough context for the buyer to review it."""

    goods_name: str
    description: str
    quantity: float | None


@dataclass(frozen=True)
class RfqDetectionResult:
    """Outcome of trying to read case setup (item(s) + implied suppliers)
    directly out of an uploaded RFQ spreadsheet."""

    recognized: bool
    file_type: str | None = None
    items: list[DetectedRfqItem] = field(default_factory=list)
    unresolved_lines: list[str] = field(default_factory=list)


def detect_rfq_selection(file_bytes: bytes, filename: str) -> RfqDetectionResult:
    """Try to recognize an uploaded file as a natural-stone or brilliant RFQ
    and extract the item(s) it requests.

    Only .xlsx is attempted: both known RFQ layouts (and the only example
    fixtures available) are XLSX. Anything else - or an XLSX that doesn't
    match either layout - is reported as unrecognized so the caller falls
    back to manual material/supplier selection.
    """
    if not filename.lower().endswith(".xlsx"):
        return RfqDetectionResult(recognized=False)

    try:
        workbook = load_workbook(BytesIO(file_bytes), data_only=True)
    except Exception:
        return RfqDetectionResult(recognized=False)

    natural_stone_result = _detect_natural_stone_rfq(workbook)
    if natural_stone_result is not None:
        return natural_stone_result

    brilliant_result = _detect_brilliant_rfq(workbook)
    if brilliant_result is not None:
        return brilliant_result

    return RfqDetectionResult(recognized=False)


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------

def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", ".")
    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def _find_header_row(
    ws, required_headers: set[str], search_rows: int = _HEADER_SEARCH_ROWS
) -> tuple[int, dict[str, int]] | None:
    """Find the first row (within search_rows) containing every header in
    required_headers as an exact (case-insensitive) cell value. Returns the
    row index and a header-text -> column-index map for that row."""
    for row_index in range(1, min(search_rows, ws.max_row) + 1):
        found: dict[str, int] = {}
        for col_index in range(1, ws.max_column + 1):
            text = _clean_text(ws.cell(row=row_index, column=col_index).value).lower()
            if text in required_headers:
                found[text] = col_index

        if required_headers.issubset(found.keys()):
            return row_index, found

    return None


def _find_column_containing(ws, header_row_index: int, substring: str) -> int | None:
    substring = substring.lower()
    for col_index in range(1, ws.max_column + 1):
        text = _clean_text(ws.cell(row=header_row_index, column=col_index).value).lower()
        if substring in text:
            return col_index
    return None


def _find_exact_column(ws, header_row_index: int, header_text: str) -> int | None:
    header_text = header_text.lower()
    for col_index in range(1, ws.max_column + 1):
        text = _clean_text(ws.cell(row=header_row_index, column=col_index).value).lower()
        if text == header_text:
            return col_index
    return None


# ---------------------------------------------------------------------
# Natural-stone RFQ detection (e.g. prirodni.xlsx)
# ---------------------------------------------------------------------

def _normalize_for_matching(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _best_catalog_match(
    description: str, catalog_names: list[str]
) -> tuple[float, str | None]:
    """Fuzzy-match a free-text stone description against catalog goods_name
    entries, trying progressively shorter leading-word windows (stone names
    are always described before shape/size details in the real fixture,
    e.g. "Amethyst afrikan round regular 7 mm")."""
    catalog_by_norm = {_normalize_for_matching(name): name for name in catalog_names}
    tokens = _normalize_for_matching(description).split()

    best_score = 0.0
    best_goods_name: str | None = None

    for window in range(min(4, len(tokens)), 0, -1):
        candidate = " ".join(tokens[:window])
        matches = difflib.get_close_matches(
            candidate, catalog_by_norm.keys(), n=1, cutoff=0.0
        )
        if not matches:
            continue

        ratio = difflib.SequenceMatcher(None, candidate, matches[0]).ratio()
        if ratio > best_score:
            best_score = ratio
            best_goods_name = catalog_by_norm[matches[0]]

    return best_score, best_goods_name


def _detect_natural_stone_rfq(workbook) -> RfqDetectionResult | None:
    catalog = repo.list_goods_names_by_group(NATURAL_STONE_GOODS_GROUP)
    if not catalog:
        return None

    for sheet_name in workbook.sheetnames:
        ws = workbook[sheet_name]
        header = _find_header_row(ws, {"description"})
        if header is None:
            continue

        header_row_index, header_cols = header
        description_col = header_cols["description"]
        quantity_col = _find_column_containing(ws, header_row_index, "quantity")

        matched_quantities: dict[str, float] = {}
        matched_descriptions: dict[str, list[str]] = {}
        unresolved_lines: list[str] = []

        for row_index in range(header_row_index + 1, ws.max_row + 1):
            description_text = _clean_text(
                ws.cell(row=row_index, column=description_col).value
            )
            if not description_text:
                continue

            quantity_value = None
            if quantity_col is not None:
                quantity_value = _coerce_float(
                    ws.cell(row=row_index, column=quantity_col).value
                )

            score, goods_name = _best_catalog_match(description_text, catalog)
            if goods_name is not None and score >= NATURAL_STONE_MATCH_THRESHOLD:
                matched_quantities[goods_name] = matched_quantities.get(
                    goods_name, 0.0
                ) + (quantity_value or 0.0)
                matched_descriptions.setdefault(goods_name, []).append(
                    description_text
                )
            else:
                unresolved_lines.append(description_text)

        if not matched_quantities:
            continue

        items = [
            DetectedRfqItem(
                goods_name=goods_name,
                description="; ".join(matched_descriptions[goods_name]),
                quantity=quantity or None,
            )
            for goods_name, quantity in matched_quantities.items()
        ]

        return RfqDetectionResult(
            recognized=True,
            file_type="natural stone",
            items=items,
            unresolved_lines=unresolved_lines,
        )

    return None


# ---------------------------------------------------------------------
# Brilliant RFQ detection (e.g. brilianty.xlsx)
# ---------------------------------------------------------------------

def _classify_brilliant_size(size_text: str) -> str | None:
    """Bucket one "Sizes" cell into 'up_to_1ct' or '1ct_and_up'.

    mm-labeled rows are melee (a round brilliant only reaches ~1ct around
    6.5mm diameter, far above anything seen sized in mm in practice), so
    they are always up_to_1ct without needing a precise mm-to-carat
    conversion. ct-labeled rows use a literal >= 1.0 threshold.
    """
    ct_match = _CT_PATTERN.search(size_text)
    if ct_match:
        value = float(ct_match.group(1).replace(",", "."))
        return "1ct_and_up" if value >= 1.0 else "up_to_1ct"

    if _MM_PATTERN.search(size_text):
        return "up_to_1ct"

    return None


def _detect_color_from_notes(ws, header_row_index: int) -> str | None:
    """Brilliant RFQ color/clarity is stated once for the whole batch in the
    free-text note rows above the size table, not per row."""
    for row_index in range(1, header_row_index):
        for col_index in range(1, ws.max_column + 1):
            text = _clean_text(ws.cell(row=row_index, column=col_index).value).lower()
            if not text:
                continue
            for color in _BRILLIANT_COLOR_KEYWORDS:
                if color in text:
                    return color

    return None


def _brilliant_goods_name(color: str | None, bucket: str) -> str:
    threshold_text = "1ct and up" if bucket == "1ct_and_up" else "up to 1ct"
    if color:
        return f"{color.capitalize()} Diamonds {threshold_text}"
    return threshold_text


def _detect_brilliant_rfq(workbook) -> RfqDetectionResult | None:
    catalog = repo.list_goods_names_by_group(BRILLIANT_GOODS_GROUP)
    if not catalog:
        return None

    catalog_by_norm = {name.strip().lower(): name for name in catalog}

    for sheet_name in workbook.sheetnames:
        ws = workbook[sheet_name]
        header = _find_header_row(ws, {"sizes"})
        if header is None:
            continue

        header_row_index, header_cols = header
        sizes_col = header_cols["sizes"]
        quantity_col = _find_exact_column(
            ws, header_row_index, "order cts"
        ) or _find_column_containing(ws, header_row_index, "order cts")

        if quantity_col is None:
            continue

        color = _detect_color_from_notes(ws, header_row_index)

        bucket_quantities: dict[str, float] = {}
        bucket_examples: dict[str, list[str]] = {}
        unresolved_lines: list[str] = []

        for row_index in range(header_row_index + 1, ws.max_row + 1):
            size_text = _clean_text(ws.cell(row=row_index, column=sizes_col).value)
            if not size_text:
                continue

            quantity_value = _coerce_float(
                ws.cell(row=row_index, column=quantity_col).value
            )
            if not quantity_value:
                continue

            bucket = _classify_brilliant_size(size_text)
            if bucket is None:
                unresolved_lines.append(size_text)
                continue

            candidate_name = _brilliant_goods_name(color, bucket)
            goods_name = catalog_by_norm.get(candidate_name.lower())
            if goods_name is None:
                unresolved_lines.append(
                    f"{size_text} (no catalog entry for {candidate_name!r})"
                )
                continue

            bucket_quantities[goods_name] = (
                bucket_quantities.get(goods_name, 0.0) + quantity_value
            )
            bucket_examples.setdefault(goods_name, []).append(size_text)

        if not bucket_quantities:
            continue

        items = [
            DetectedRfqItem(
                goods_name=goods_name,
                description=(
                    f"{len(bucket_examples[goods_name])} size(s), e.g. "
                    f"{bucket_examples[goods_name][0]}"
                ),
                quantity=quantity or None,
            )
            for goods_name, quantity in bucket_quantities.items()
        ]

        return RfqDetectionResult(
            recognized=True,
            file_type="brilliant",
            items=items,
            unresolved_lines=unresolved_lines,
        )

    return None
