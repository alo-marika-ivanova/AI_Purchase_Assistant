from __future__ import annotations

import re

from app.llm.supplier_message_analysis import add_structured_dimensions


# Deliberately duplicated from rfq_tentative_price_safeguard.py /
# rfq_price_safeguard.py rather than imported: each deterministic safeguard
# module owns its own conservative keyword list so they can be reviewed and
# tuned independently.
_RISK_OR_SCOPE_PATTERN = re.compile(
    r"\b(?:"
    r"deposit|prepayment|pre-payment|payment\s+term|cash\s+payment|"
    r"delivery|lead\s+time|ship(?:ping)?|"
    r"different\s+(?:item|material|stone)|alternative\s+(?:item|material|stone)|"
    r"specification|quality|certificate|return|refund|reject(?:ion|ed)?|"
    r"legal|liability|customs|sanction|compliance|"
    r"confidential|exclusive|dispute|claim"
    r")\b",
    re.IGNORECASE,
)

_TENTATIVE_PATTERN = re.compile(
    r"\b(?:"
    r"almost\s+sure|not\s+(?:yet\s+)?sure|I\s+think|I\s+believe|"
    r"probably|likely|should\s+be|seems?\s+to\s+be|appears?\s+to\s+be|"
    r"tentative(?:ly)?|not\s+final|"
    r"(?:will|need\s+to|must|let\s+me)\s+(?:check|verify|confirm)|"
    r"check\s+with|verify\s+with|confirm\s+with|ask\s+(?:my|our|the)\s+"
    r"(?:supervisor|manager|boss|team)"
    r")\b",
    re.IGNORECASE,
)

_PRICE_TOLERANCE = 0.005


def _coerce_price(value: str) -> float | None:
    try:
        price = float(value.strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _find_header(lines: list[str]) -> tuple[int, int, int] | None:
    """Find the row index, description-column index, and price-column index
    of the header row in a pipe-delimited flattened spreadsheet dump (see
    attachment_service.extract_text_from_spreadsheet)."""
    for row_index, line in enumerate(lines):
        cells = [cell.strip().lower() for cell in line.split(" | ")]

        description_index = next(
            (i for i, cell in enumerate(cells) if "description" in cell),
            None,
        )
        price_index = next(
            (i for i, cell in enumerate(cells) if "price" in cell),
            None,
        )

        if description_index is not None and price_index is not None:
            return row_index, description_index, price_index

    return None


def extract_safe_multi_item_rfq_prices(
    message_body: str,
    supplier_items: list[dict],
) -> list[dict] | None:
    """Extract one confirmed unit price per item from a supplier's
    attachment-derived, tabular reply.

    Only fires when EVERY item this supplier was asked to quote can be
    matched to a row with an exact item_material match and exactly one
    clean, unhedged price - anything less conclusive (a missing item, an
    unparseable or duplicate/conflicting price, a hedge or risk keyword
    anywhere in the text) returns None and leaves the whole message to the
    LLM classifier, mirroring the conservative bail-on-any-doubt philosophy
    of the single-item RFQ safeguards.

    Intentionally restricted to attachment-derived replies (see the
    is_attachment_reply gate in the caller): a supplier's free-typed text
    is better served by the LLM's semantic understanding, while a filled-in
    copy of the RFQ's own spreadsheet template is a highly structured,
    low-ambiguity shape a plain parser can safely handle - and case_items
    created from a detected natural-stone RFQ store the exact source row
    text as item_material, so a supplier who reuses that same template will
    match it verbatim.
    """
    text = (message_body or "").strip()
    if not text or not supplier_items:
        return None

    lines = [line for line in text.splitlines() if line.strip()]
    header = _find_header(lines)
    if header is None:
        return None

    header_index, description_index, price_index = header
    column_count = len(lines[header_index].split(" | "))

    # Only scan from the header onward: anything before it is the buyer's
    # own RFQ boilerplate (e.g. a standing "QUALITY REQUIREMENTS" preamble),
    # not supplier-authored text, and words like "quality" there are not a
    # real risk topic the supplier raised.
    text_from_header = "\n".join(lines[header_index:])
    if (
        _RISK_OR_SCOPE_PATTERN.search(text_from_header)
        or _TENTATIVE_PATTERN.search(text_from_header)
    ):
        return None

    matched_by_material: dict[str, float] = {}

    for line in lines[header_index + 1:]:
        cells = [cell.strip() for cell in line.split(" | ")]
        if len(cells) != column_count:
            continue

        description = cells[description_index]
        price = _coerce_price(cells[price_index])
        if price is None:
            continue

        for item in supplier_items:
            material = item["item_material"]
            if material.strip().lower() != description.lower():
                continue

            if (
                material in matched_by_material
                and abs(matched_by_material[material] - price) > _PRICE_TOLERANCE
            ):
                # Two different prices claimed for the same item in the
                # same file - genuinely ambiguous, not safe to guess.
                return None

            matched_by_material[material] = price
            break

    required_materials = {item["item_material"] for item in supplier_items}
    if set(matched_by_material) != required_materials:
        return None

    return [
        {
            "item_material": material,
            "unit_price_usd": price,
            "price_certainty": "CONFIRMED",
        }
        for material, price in matched_by_material.items()
    ]


def build_deterministic_multi_item_rfq_offer_result(item_offers: list[dict]) -> dict:
    """Return the same result shape as the LLM classifier for a fully
    confirmed multi-item price table."""
    return add_structured_dimensions(
        {
            "success": True,
            "provider": "deterministic",
            "model": None,
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "safe_for_automation": True,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "confidence": "high",
            "stated_price_amount": None,
            "unit_price_usd": None,
            "currency": "USD",
            "price_basis": "MULTIPLE",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": True,
            "is_conditional": False,
            "condition_summary": None,
            "supplier_will_reply_later": False,
            "supplier_refused": False,
            "supplier_accepts_target": False,
            "question_can_be_answered_from_case": False,
            "item_offers": item_offers,
            "reason": (
                "Every requested item was matched to exactly one clean, "
                "unhedged unit price in the supplier's attachment, verified "
                "by the deterministic multi-item safety parser."
            ),
            "suggested_clarification_question": None,
            "suggested_buyer_reply": None,
            "raw_result": None,
            "error": None,
        }
    )
