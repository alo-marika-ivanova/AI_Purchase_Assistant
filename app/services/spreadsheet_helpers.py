from __future__ import annotations

_HEADER_SEARCH_ROWS = 10


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def coerce_float(value: object) -> float | None:
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


def find_header_row(
    ws, required_headers: set[str], search_rows: int = _HEADER_SEARCH_ROWS
) -> tuple[int, dict[str, int]] | None:
    """Find the first row (within search_rows) containing every header in
    required_headers as an exact (case-insensitive) cell value. Returns the
    row index and a header-text -> column-index map for that row."""
    for row_index in range(1, min(search_rows, ws.max_row) + 1):
        found: dict[str, int] = {}
        for col_index in range(1, ws.max_column + 1):
            text = clean_text(ws.cell(row=row_index, column=col_index).value).lower()
            if text in required_headers:
                found[text] = col_index

        if required_headers.issubset(found.keys()):
            return row_index, found

    return None


def find_column_containing(ws, header_row_index: int, substring: str) -> int | None:
    substring = substring.lower()
    for col_index in range(1, ws.max_column + 1):
        text = clean_text(ws.cell(row=header_row_index, column=col_index).value).lower()
        if substring in text:
            return col_index
    return None


def find_exact_column(ws, header_row_index: int, header_text: str) -> int | None:
    header_text = header_text.lower()
    for col_index in range(1, ws.max_column + 1):
        text = clean_text(ws.cell(row=header_row_index, column=col_index).value).lower()
        if text == header_text:
            return col_index
    return None
