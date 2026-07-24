from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


DecisionAction = Literal[
    "SAVE_OFFER",
    "RECORD_PRICE_REFUSAL",
    "PAUSE_FOR_REVIEW",
    "USE_CLASSIFIER_RESULT",
]


@dataclass(frozen=True)
class CommonNegotiationDecision:
    """
    Deterministic fallback for negotiation replies.

    The LLM classification is authoritative for message interpretation,
    price extraction, and human-review triggering. This policy layer is
    only consulted when the LLM result itself is unusable: the LLM call
    failed, or it recommended saving an offer without returning a usable
    price. In that case a conservative regex scan of the raw text is used
    to try to recover a single explicit price.
    """

    action: DecisionAction
    unit_price_usd: float | None
    reason: str


_AMOUNT = r"(?P<amount>\d+(?:[.,]\d{1,4})?)"

_PRICE_PATTERNS = (
    # USD 39 / USD: 39
    re.compile(
        rf"\bUSD\s*[:=]?\s*{_AMOUNT}\b",
        re.IGNORECASE,
    ),

    # 39 USD
    re.compile(
        rf"\b{_AMOUNT}\s*USD\b",
        re.IGNORECASE,
    ),

    # $39
    re.compile(
        rf"\$\s*{_AMOUNT}\b",
        re.IGNORECASE,
    ),

    # 39 per unit / 39 per piece / 39 / unit
    re.compile(
        rf"\b{_AMOUNT}\s*(?:per|/)\s*"
        rf"(?:unit|piece|pieces|pc|pcs|stone|stones|carat|ct)\b",
        re.IGNORECASE,
    ),

    # price is 39 / price: 39 / offer is 39
    re.compile(
        rf"\b(?:price|offer|quote|quotation)\s*"
        rf"(?:is|at|of|for|:|=)?\s*{_AMOUNT}\b",
        re.IGNORECASE,
    ),

    # reduce to 39 / go to 39 / do 39 / accept 39
    re.compile(
        rf"\b(?:reduce(?:\s+the\s+price)?\s+to|"
        rf"go\s+(?:down\s+)?to|"
        rf"come\s+down\s+to|"
        rf"do|accept|offer)\s*{_AMOUNT}\b",
        re.IGNORECASE,
    ),
)

_NUMBER_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d{1,4})?\b"
)

_COMMON_PRICE_LANGUAGE_PATTERN = re.compile(
    r"\b(?:price|offer|quote|quotation|reduce|reduction|"
    r"per\s+unit|per\s+piece|per\s+stone|"
    r"go\s+(?:down\s+)?to|come\s+down\s+to|"
    r"we\s+can\s+do|can\s+do|accept)\b",
    re.IGNORECASE,
)

_PRICE_TOLERANCE = 0.005


def _to_float(value: str) -> float:
    return float(value.replace(",", "."))


def _deduplicate_prices(
    values: list[float],
) -> list[float]:
    unique: list[float] = []

    for value in values:
        if not any(
            abs(value - existing) <= _PRICE_TOLERANCE
            for existing in unique
        ):
            unique.append(value)

    return unique


def extract_common_explicit_prices(
    supplier_text: str,
) -> list[float]:
    """
    Extract only conservative, common unit-price expressions.

    This is intentionally not a general language parser. Ambiguous and
    compound messages are left for human review.
    """
    text = (supplier_text or "").strip()

    if not text:
        return []

    prices: list[float] = []

    for pattern in _PRICE_PATTERNS:
        for match in pattern.finditer(text):
            prices.append(
                _to_float(match.group("amount"))
            )

    prices = _deduplicate_prices(prices)

    if prices:
        return prices

    # In an active target-price negotiation, suppliers often write:
    # "We can do 39."
    #
    # Accept one bare number only when common price language is present.
    raw_numbers = [
        _to_float(match.group(0))
        for match in _NUMBER_PATTERN.finditer(text)
    ]

    raw_numbers = _deduplicate_prices(raw_numbers)

    if (
        len(raw_numbers) == 1
        and _COMMON_PRICE_LANGUAGE_PATTERN.search(text)
    ):
        return raw_numbers

    return []


def decide_common_negotiation_reply(
    *,
    supplier_text: str,
    analysis: dict,
    previous_best_price_usd: float | None,
    target_price_usd: float | None,
) -> CommonNegotiationDecision:
    """
    Deterministic fallback for a negotiation reply.

    The LLM classification (`analysis`) is authoritative for message
    interpretation, price extraction, risk-topic detection, and
    human-review triggering. This function does not re-derive any of
    that from the raw text and must not override a usable LLM result.

    It only takes over when the LLM result itself cannot be used:
    - the LLM call failed; or
    - the LLM recommended saving an offer but returned no usable price.

    In either case, a conservative regex scan of the raw text is used to
    try to recover exactly one explicit price. Anything less conclusive
    escalates to human review.
    """
    text = (supplier_text or "").strip()

    llm_succeeded = bool(analysis.get("success", True))

    classifier_action = str(
        analysis.get("recommended_action") or ""
    ).strip().upper()

    needs_price_but_missing = (
        classifier_action
        in {"SAVE_OFFER", "SAVE_PROVISIONAL_OFFER_AND_WAIT"}
        and analysis.get("unit_price_usd") is None
    )

    if llm_succeeded and not needs_price_but_missing:
        return CommonNegotiationDecision(
            action="USE_CLASSIFIER_RESULT",
            unit_price_usd=None,
            reason="The LLM classification is authoritative and usable.",
        )

    prices = extract_common_explicit_prices(text)

    if len(prices) != 1:
        return CommonNegotiationDecision(
            action="PAUSE_FOR_REVIEW",
            unit_price_usd=None,
            reason=(
                "The LLM result was unusable and the deterministic "
                "fallback could not find exactly one explicit price."
            ),
        )

    price = prices[0]

    if price <= 0:
        return CommonNegotiationDecision(
            action="PAUSE_FOR_REVIEW",
            unit_price_usd=None,
            reason="The fallback-extracted price is not positive.",
        )

    if previous_best_price_usd is None:
        return CommonNegotiationDecision(
            action="SAVE_OFFER",
            unit_price_usd=price,
            reason=(
                "The LLM result was unusable; fallback extraction found "
                "one clear explicit unit price."
            ),
        )

    previous = float(previous_best_price_usd)

    if price < previous - _PRICE_TOLERANCE:
        return CommonNegotiationDecision(
            action="SAVE_OFFER",
            unit_price_usd=price,
            reason=(
                f"The LLM result was unusable; fallback extraction found "
                f"an improved price from USD {previous:.2f} to "
                f"USD {price:.2f}."
            ),
        )

    if abs(price - previous) <= _PRICE_TOLERANCE:
        return CommonNegotiationDecision(
            action="RECORD_PRICE_REFUSAL",
            unit_price_usd=previous,
            reason=(
                f"The LLM result was unusable; fallback extraction found "
                f"the supplier repeating the existing best price of "
                f"USD {previous:.2f}."
            ),
        )

    return CommonNegotiationDecision(
        action="PAUSE_FOR_REVIEW",
        unit_price_usd=None,
        reason=(
            f"The LLM result was unusable; fallback extraction found "
            f"USD {price:.2f}, above the previous best price of "
            f"USD {previous:.2f}."
        ),
    )
