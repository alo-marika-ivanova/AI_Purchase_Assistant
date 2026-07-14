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
    Conservative deterministic interpretation of common negotiation replies.

    The LLM remains the primary semantic interpreter. This policy layer only
    overrides it when the supplier text contains a common, objectively
    verifiable price pattern or a common risk indicator.
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

_TOTAL_OR_RANGE_PATTERN = re.compile(
    r"\b(?:total|altogether|for\s+all|range|between|from)\b",
    re.IGNORECASE,
)

_QUANTITY_TIER_PATTERN = re.compile(
    r"\b(?:if|when|above|over|at\s+least|minimum|min\.?|"
    r"more\s+than)\s+\d+\b",
    re.IGNORECASE,
)

_COMMON_RISK_PATTERN = re.compile(
    r"\b(?:"
    r"deposit|prepayment|pre-payment|payment\s+term|"
    r"pay\s+in\s+advance|advance\s+payment|cash\s+payment|"
    r"delivery|lead\s+time|ship(?:ping)?|"
    r"specification|specifications|different\s+material|"
    r"alternative\s+material|quality|certificate|certification|"
    r"return|refund|reject(?:ion|ed)?|"
    r"legal|liability|contract|penalty|"
    r"customs|sanction|compliance|"
    r"confidential|confidentiality|exclusive|exclusivity|"
    r"dispute|claim|"
    r"call\s+me|phone\s+me|telephone|video\s+call"
    r")\b",
    re.IGNORECASE,
)

_RISKY_LLM_CATEGORIES = {
    "PAYMENT_TERMS",
    "DEPOSIT_OR_PREPAYMENT",
    "CASH_PAYMENT",
    "DELIVERY_ISSUE",
    "CHANGED_SPECIFICATION",
    "QUALITY_ISSUE",
    "RETURN_OR_REJECTION",
    "LEGAL_OR_LIABILITY",
    "CUSTOMS_OR_COMPLIANCE",
    "CONFIDENTIALITY_OR_EXCLUSIVITY",
    "SUPPLIER_DISPUTE",
    "UNKNOWN",
}

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
    Apply safe deterministic invariants to a negotiation reply.

    Priority:
    1. common risk or human-review signal -> review;
    2. multiple/conditional/total prices -> review;
    3. one clear explicit price -> compare numerically;
    4. contextual target acceptance -> save target;
    5. otherwise retain the classifier result.
    """
    text = (supplier_text or "").strip()

    category = str(
        analysis.get("message_category") or ""
    ).strip().upper()

    classifier_action = str(
        analysis.get("recommended_action") or ""
    ).strip().upper()

    requires_human_review = bool(
        analysis.get("requires_human_review")
    )

    safe_for_automation = analysis.get(
        "safe_for_automation"
    )

    if (
        _COMMON_RISK_PATTERN.search(text)
        or category in _RISKY_LLM_CATEGORIES
        or requires_human_review
        or safe_for_automation is False
        or classifier_action == "PAUSE_FOR_REVIEW"
    ):
        return CommonNegotiationDecision(
            action="PAUSE_FOR_REVIEW",
            unit_price_usd=None,
            reason=(
                "The reply contains a common commercial-risk or "
                "human-review signal."
            ),
        )

    if (
        bool(analysis.get("has_multiple_prices"))
        or bool(analysis.get("is_conditional"))
        or str(analysis.get("price_basis") or "").upper()
        in {"TOTAL", "RANGE"}
        or _TOTAL_OR_RANGE_PATTERN.search(text)
        or _QUANTITY_TIER_PATTERN.search(text)
    ):
        return CommonNegotiationDecision(
            action="PAUSE_FOR_REVIEW",
            unit_price_usd=None,
            reason=(
                "The reply contains multiple, total, range, or "
                "quantity-dependent pricing."
            ),
        )

    prices = extract_common_explicit_prices(text)

    if len(prices) > 1:
        return CommonNegotiationDecision(
            action="PAUSE_FOR_REVIEW",
            unit_price_usd=None,
            reason=(
                "More than one distinct explicit price was found."
            ),
        )

    if len(prices) == 1:
        price = prices[0]

        if price <= 0:
            return CommonNegotiationDecision(
                action="PAUSE_FOR_REVIEW",
                unit_price_usd=None,
                reason="The extracted price is not positive.",
            )

        if previous_best_price_usd is None:
            return CommonNegotiationDecision(
                action="SAVE_OFFER",
                unit_price_usd=price,
                reason=(
                    "One clear explicit unit price was found."
                ),
            )

        previous = float(previous_best_price_usd)

        if price < previous - _PRICE_TOLERANCE:
            return CommonNegotiationDecision(
                action="SAVE_OFFER",
                unit_price_usd=price,
                reason=(
                    f"The supplier explicitly improved the price "
                    f"from USD {previous:.2f} to USD {price:.2f}."
                ),
            )

        if abs(price - previous) <= _PRICE_TOLERANCE:
            return CommonNegotiationDecision(
                action="RECORD_PRICE_REFUSAL",
                unit_price_usd=previous,
                reason=(
                    f"The supplier repeated the existing best price "
                    f"of USD {previous:.2f}."
                ),
            )

        return CommonNegotiationDecision(
            action="PAUSE_FOR_REVIEW",
            unit_price_usd=None,
            reason=(
                f"The supplier stated USD {price:.2f}, above the "
                f"previous best price of USD {previous:.2f}."
            ),
        )

    supplier_accepts_target = bool(
        analysis.get("supplier_accepts_target")
    )

    if (
        supplier_accepts_target
        and target_price_usd is not None
    ):
        target = float(target_price_usd)

        return CommonNegotiationDecision(
            action="SAVE_OFFER",
            unit_price_usd=target,
            reason=(
                "The supplier contextually accepted the explicit "
                f"target price of USD {target:.2f}."
            ),
        )

    return CommonNegotiationDecision(
        action="USE_CLASSIFIER_RESULT",
        unit_price_usd=None,
        reason=(
            "No conservative deterministic override applies."
        ),
    )
