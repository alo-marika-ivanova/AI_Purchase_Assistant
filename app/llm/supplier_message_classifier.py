from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

from app.llm.json_utils import extract_json_object
from app.llm.provider import get_llm_provider
from app.llm.rfq_price_safeguard import (
    build_deterministic_rfq_offer_result,
    extract_safe_simple_rfq_unit_price,
)
from app.llm.rfq_tentative_price_safeguard import (
    build_contextual_price_confirmation_result,
    build_deterministic_tentative_rfq_result,
    extract_tentative_rfq_unit_price,
    is_contextual_provisional_price_confirmation,
)
from app.llm.supplier_message_analysis import add_structured_dimensions


load_dotenv()

CLASSIFIER_TIMEOUT_SECONDS = int(
    os.getenv("LLM_CLASSIFIER_TIMEOUT_SECONDS", "60")
)
DEFAULT_NEGOTIATION_CURRENCY = os.getenv(
    "DEFAULT_NEGOTIATION_CURRENCY",
    "USD",
).strip().upper()
ASSUME_DEFAULT_CURRENCY_FROM_CONTEXT = (
    os.getenv("ASSUME_DEFAULT_CURRENCY_FROM_CONTEXT", "true")
    .strip()
    .lower()
    == "true"
)

MESSAGE_CATEGORIES = {
    "CLEAR_PRICE_OFFER",
    "TENTATIVE_PRICE",
    "IMPROVED_PRICE_OFFER",
    "TARGET_ACCEPTANCE",
    "UNCLEAR_PRICE",
    "MULTIPLE_PRICES",
    "TOTAL_PRICE_ONLY",
    "CONDITIONAL_PRICE",
    "ACKNOWLEDGEMENT_WILL_REPLY",
    "SUPPLIER_QUESTION",
    "DECLINES_OR_UNAVAILABLE",
    "PRICE_REFUSAL",
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
    "GENERAL_NON_PRICE",
    "UNKNOWN",
}

RECOMMENDED_ACTIONS = {
    "SAVE_OFFER",
    "SAVE_PROVISIONAL_OFFER_AND_WAIT",
    "ASK_PRICE_CLARIFICATION",
    "WAIT_FOR_SUPPLIER",
    "ANSWER_FROM_CASE_AND_REPEAT_REQUEST",
    "MARK_REJECTED",
    "RECORD_PRICE_REFUSAL",
    "PAUSE_FOR_REVIEW",
}

RISKY_CATEGORIES = {
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
    "GENERAL_NON_PRICE",
    "UNKNOWN",
}

RISK_TOPIC_CATEGORIES = {
    "NONE",
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

CLEAR_OFFER_CATEGORIES = {
    "CLEAR_PRICE_OFFER",
    "IMPROVED_PRICE_OFFER",
    "TARGET_ACCEPTANCE",
}

CLARIFICATION_CATEGORIES = {
    "UNCLEAR_PRICE",
    "MULTIPLE_PRICES",
    "TOTAL_PRICE_ONLY",
    "CONDITIONAL_PRICE",
}

NO_PRICE_FACT_CATEGORIES = {
    "ACKNOWLEDGEMENT_WILL_REPLY",
    "SUPPLIER_QUESTION",
    "DECLINES_OR_UNAVAILABLE",
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
    "GENERAL_NON_PRICE",
    "UNKNOWN",
}


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _format_recent_history(
    message_history: list[dict] | None,
    limit: int = 10,
) -> str:
    if not message_history:
        return "No previous conversation."

    lines: list[str] = []
    for message in message_history[-limit:]:
        direction = message.get("direction")
        body = _safe_text(message.get("body"))
        if not body:
            continue
        speaker = "Supplier" if direction == "inbound" else "Buyer"
        lines.append(f"{speaker}: {body}")

    return "\n".join(lines) if lines else "No previous conversation."


def _nullable_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0", "", "null", "none"}:
            return False
    return default


def _apply_default_currency_rule(
    parsed: dict,
    conversation_stage: str,
) -> dict:
    result = dict(parsed)

    if not ASSUME_DEFAULT_CURRENCY_FROM_CONTEXT:
        return result
    if DEFAULT_NEGOTIATION_CURRENCY != "USD":
        return result
    if (conversation_stage or "").strip().upper() not in {
        "RFQ",
        "NEGOTIATION",
        "PRICE_NEGOTIATION",
    }:
        return result

    category = _safe_text(result.get("message_category")).upper()
    currency = _safe_text(result.get("currency")).upper()
    price_basis = _safe_text(result.get("price_basis")).upper()
    unit_price = _nullable_float(result.get("unit_price_usd"))
    stated_price = _nullable_float(result.get("stated_price_amount"))
    amount = unit_price if unit_price is not None else stated_price

    if currency == "OTHER":
        return result
    if amount is None or amount <= 0:
        return result
    if _to_bool(result.get("has_multiple_prices")):
        return result
    if _to_bool(result.get("is_conditional")):
        return result
    if price_basis not in {"", "UNIT", "UNKNOWN", "NONE"}:
        return result
    if category not in {
        "CLEAR_PRICE_OFFER",
        "IMPROVED_PRICE_OFFER",
        "TARGET_ACCEPTANCE",
        "UNCLEAR_PRICE",
    }:
        return result

    result.update(
        {
            "message_category": (
                "CLEAR_PRICE_OFFER"
                if category == "UNCLEAR_PRICE"
                else category
            ),
            "recommended_action": "SAVE_OFFER",
            "stated_price_amount": amount,
            "unit_price_usd": amount,
            "currency": "USD",
            "price_basis": "UNIT",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "requires_human_review": False,
            "reason": (
                "One unambiguous unit price was provided. USD was inferred "
                "from the purchasing conversation context."
            ),
        }
    )
    return result


def _normalize_result(
    parsed: dict,
    conversation_stage: str,
    target_price_usd: float | None,
    provider_name: str,
    model_name: str | None,
) -> dict:
    parsed = _apply_default_currency_rule(parsed, conversation_stage)

    category = _safe_text(parsed.get("message_category")).upper()
    model_action = _safe_text(parsed.get("recommended_action")).upper()
    confidence = _safe_text(parsed.get("confidence")).lower()

    if category not in MESSAGE_CATEGORIES:
        category = "UNKNOWN"
    if model_action not in RECOMMENDED_ACTIONS:
        model_action = "PAUSE_FOR_REVIEW"
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    stated_price_amount = _nullable_float(parsed.get("stated_price_amount"))
    unit_price_usd = _nullable_float(parsed.get("unit_price_usd"))
    currency = _safe_text(parsed.get("currency")).upper() or None
    if currency not in {"USD", "OTHER", "UNKNOWN", None}:
        currency = "UNKNOWN"

    raw_price_basis = _safe_text(parsed.get("price_basis")).upper()
    allowed_bases = {"UNIT", "TOTAL", "RANGE", "MULTIPLE", "UNKNOWN", "NONE"}
    price_basis = raw_price_basis if raw_price_basis in allowed_bases else "UNKNOWN"

    is_price_clear = _to_bool(parsed.get("is_price_clear"))
    is_currency_clear = _to_bool(parsed.get("is_currency_clear"))
    has_multiple_prices = _to_bool(parsed.get("has_multiple_prices"))
    is_conditional = _to_bool(parsed.get("is_conditional"))
    requires_human_review = _to_bool(
        parsed.get("requires_human_review")
    )

    contains_risky_topic = _to_bool(
        parsed.get("contains_risky_topic")
    )

    risk_category = _safe_text(
        parsed.get("risk_category")
    ).upper()

    if risk_category not in RISK_TOPIC_CATEGORIES:
        risk_category = "NONE"

    # A concrete risk category always has priority, even if the model
    # inconsistently returned contains_risky_topic=false.
    if risk_category != "NONE":
        contains_risky_topic = True

    # If the model detected risk but failed to name it safely,
    # classify it as UNKNOWN and pause.
    if contains_risky_topic and risk_category == "NONE":
        risk_category = "UNKNOWN"

    supplier_will_reply_later = _to_bool(
        parsed.get("supplier_will_reply_later")
    )
    supplier_refused = _to_bool(parsed.get("supplier_refused"))
    supplier_accepts_target = _to_bool(parsed.get("supplier_accepts_target"))
    question_can_be_answered_from_case = _to_bool(
        parsed.get("question_can_be_answered_from_case")
    )

    reason = _safe_text(parsed.get("reason"))
    clarification = _safe_text(parsed.get("suggested_clarification_question")) or None
    suggested_reply = _safe_text(parsed.get("suggested_buyer_reply")) or None
    condition_summary = _safe_text(parsed.get("condition_summary")) or None

    normalized_stage = (
        conversation_stage or ""
    ).strip().upper()

    # Risk screening has priority over price interpretation.
    #
    # For example:
    # "We can do USD 36, but only with a 50% deposit"
    #
    # contains a valid numeric price, but the system must not save or
    # automatically accept it. The deposit condition requires buyer review.
    if contains_risky_topic:
        category = risk_category
        requires_human_review = True

    # A contextual acceptance such as "yes, we can do that" means the
    # explicit target from the immediately preceding buyer message.
    if (
        normalized_stage in {"NEGOTIATION", "PRICE_NEGOTIATION"}
        and category == "TARGET_ACCEPTANCE"
        and target_price_usd is not None
    ):
        stated_price_amount = float(target_price_usd)
        unit_price_usd = float(target_price_usd)
        currency = "USD"
        price_basis = "UNIT"
        is_price_clear = True
        is_currency_clear = True
        has_multiple_prices = False
        is_conditional = False
        supplier_accepts_target = True
        requires_human_review = False

    if category in NO_PRICE_FACT_CATEGORIES:
        stated_price_amount = None
        unit_price_usd = None
        currency = None
        price_basis = "NONE"
        is_price_clear = False
        is_currency_clear = False
        has_multiple_prices = False
        is_conditional = False
        condition_summary = None
    elif category == "UNCLEAR_PRICE":
        is_price_clear = False
        if currency != "USD":
            is_currency_clear = False
        if price_basis == "UNKNOWN":
            price_basis = "NONE"
    elif category == "TOTAL_PRICE_ONLY":
        price_basis = "TOTAL"
        is_price_clear = False
        has_multiple_prices = False
        is_conditional = False
        if currency == "USD":
            is_currency_clear = True
    elif category == "MULTIPLE_PRICES":
        price_basis = "MULTIPLE"
        is_price_clear = False
        has_multiple_prices = True
        if currency == "USD":
            is_currency_clear = True
    elif category == "CONDITIONAL_PRICE":
        is_price_clear = False
        is_conditional = True
        if "MULTIPLE" in raw_price_basis or "|" in raw_price_basis:
            price_basis = "MULTIPLE"
            has_multiple_prices = True
        elif price_basis not in {"UNIT", "TOTAL", "RANGE", "MULTIPLE"}:
            price_basis = "UNKNOWN"
        if currency == "USD":
            is_currency_clear = True
        if not condition_summary:
            condition_summary = "Supplier provided conditional or tiered pricing."
    elif category in CLEAR_OFFER_CATEGORIES:
        if currency == "USD":
            is_currency_clear = True
        if price_basis == "UNKNOWN":
            price_basis = "UNIT"

    supplier_will_reply_later = category in {
        "ACKNOWLEDGEMENT_WILL_REPLY",
        "TENTATIVE_PRICE",
    }
    supplier_refused = category in {"DECLINES_OR_UNAVAILABLE", "PRICE_REFUSAL"}
    supplier_accepts_target = category == "TARGET_ACCEPTANCE"
    if category != "SUPPLIER_QUESTION":
        question_can_be_answered_from_case = False

    valid_offer = (
        unit_price_usd is not None
        and unit_price_usd > 0
        and currency == "USD"
        and price_basis == "UNIT"
        and is_price_clear
        and is_currency_clear
        and not has_multiple_prices
        and not is_conditional
    )

    if category in RISKY_CATEGORIES:
        action = "PAUSE_FOR_REVIEW"
        requires_human_review = True
    elif category == "ACKNOWLEDGEMENT_WILL_REPLY":
        action = "WAIT_FOR_SUPPLIER"
        requires_human_review = False
    elif category == "DECLINES_OR_UNAVAILABLE":
        action = "MARK_REJECTED"
        requires_human_review = False
    elif category == "PRICE_REFUSAL":
        action = "RECORD_PRICE_REFUSAL"
        requires_human_review = False
    elif category == "SUPPLIER_QUESTION":
        if question_can_be_answered_from_case:
            action = "ANSWER_FROM_CASE_AND_REPEAT_REQUEST"
            requires_human_review = False
        else:
            action = "PAUSE_FOR_REVIEW"
            requires_human_review = True
    elif category == "TENTATIVE_PRICE":
        action = (
            "SAVE_PROVISIONAL_OFFER_AND_WAIT"
            if valid_offer
            else "ASK_PRICE_CLARIFICATION"
        )
        requires_human_review = False
    elif category in CLARIFICATION_CATEGORIES:
        action = "ASK_PRICE_CLARIFICATION"
        requires_human_review = False
    elif category in CLEAR_OFFER_CATEGORIES:
        action = "SAVE_OFFER" if valid_offer else "ASK_PRICE_CLARIFICATION"
        requires_human_review = False
    else:
        action = model_action

    if action in {"SAVE_OFFER", "SAVE_PROVISIONAL_OFFER_AND_WAIT"} and not valid_offer:
        action = "ASK_PRICE_CLARIFICATION"
        requires_human_review = False

    if action == "ASK_PRICE_CLARIFICATION" and not clarification:
        if category == "TOTAL_PRICE_ONLY":
            clarification = "Please confirm the unit price in USD for the requested quantity."
        elif category in {"MULTIPLE_PRICES", "CONDITIONAL_PRICE"}:
            clarification = (
                "Please confirm the single applicable unit price in USD "
                "for our requested quantity."
            )
        else:
            clarification = (
                "Please confirm the unit price in USD for the requested item and quantity."
            )

    safe_for_automation = action in {
        "SAVE_OFFER",
        "SAVE_PROVISIONAL_OFFER_AND_WAIT",
        "ASK_PRICE_CLARIFICATION",
        "WAIT_FOR_SUPPLIER",
        "ANSWER_FROM_CASE_AND_REPEAT_REQUEST",
        "MARK_REJECTED",
        "RECORD_PRICE_REFUSAL",
    }

    if not reason:
        reason = "The supplier message was interpreted by the LLM classifier."

    return add_structured_dimensions({
        "success": True,
        "provider": provider_name,
        "model": model_name,
        "message_category": category,
        "recommended_action": action,
        "safe_for_automation": safe_for_automation,
        "requires_human_review": requires_human_review,
        "contains_risky_topic": contains_risky_topic,
        "risk_category": risk_category,
        "confidence": confidence,
        "stated_price_amount": stated_price_amount,
        "unit_price_usd": unit_price_usd,
        "currency": currency,
        "price_basis": price_basis,
        "is_price_clear": is_price_clear,
        "is_currency_clear": is_currency_clear,
        "has_multiple_prices": has_multiple_prices,
        "is_conditional": is_conditional,
        "condition_summary": condition_summary,
        "supplier_will_reply_later": supplier_will_reply_later,
        "supplier_refused": supplier_refused,
        "supplier_accepts_target": supplier_accepts_target,
        "question_can_be_answered_from_case": question_can_be_answered_from_case,
        "price_certainty": parsed.get("price_certainty"),
        "supplier_commitment": parsed.get("supplier_commitment"),
        "pending_supplier_action": parsed.get("pending_supplier_action"),
        "offer_status": parsed.get("offer_status"),
        "reason": reason,
        "suggested_clarification_question": clarification,
        "suggested_buyer_reply": suggested_reply,
        "raw_result": parsed,
        "error": None,
    })


def _failure_result(
    error: str,
    provider_name: str,
    model_name: str | None,
) -> dict:
    return add_structured_dimensions({
        "success": False,
        "provider": provider_name,
        "model": model_name,
        "message_category": "UNKNOWN",
        "recommended_action": "PAUSE_FOR_REVIEW",
        "safe_for_automation": False,
        "requires_human_review": True,
        "contains_risky_topic": True,
        "risk_category": "UNKNOWN",
        "confidence": "low",
        "stated_price_amount": None,
        "unit_price_usd": None,
        "currency": None,
        "price_basis": "UNKNOWN",
        "is_price_clear": False,
        "is_currency_clear": False,
        "has_multiple_prices": False,
        "is_conditional": False,
        "condition_summary": None,
        "supplier_will_reply_later": False,
        "supplier_refused": False,
        "supplier_accepts_target": False,
        "question_can_be_answered_from_case": False,
        "reason": "The LLM could not safely classify the supplier message.",
        "suggested_clarification_question": None,
        "suggested_buyer_reply": None,
        "raw_result": None,
        "error": error,
    })


def analyze_supplier_message_with_ollama(
    message_body: str,
    case_data: dict,
    supplier: dict | None = None,
    message_history: list[dict] | None = None,
    conversation_stage: str = "RFQ",
    supplier_state: str | None = None,
    target_price_usd: float | None = None,
    supplier_best_price_usd: float | None = None,
    provisional_price_usd: float | None = None,
) -> dict:
    clean_body = (message_body or "").strip()
    if not clean_body:
        fallback_provider_name = os.getenv("LLM_PROVIDER", "claude").strip().lower()
        return _failure_result(
            "Supplier message is empty.",
            provider_name=fallback_provider_name,
            model_name=None,
        )

    if (conversation_stage or "").strip().upper() == "RFQ":
        if (
            provisional_price_usd is not None
            and provisional_price_usd > 0
            and is_contextual_provisional_price_confirmation(clean_body)
        ):
            return build_contextual_price_confirmation_result(
                float(provisional_price_usd)
            )

        deterministic_price = extract_safe_simple_rfq_unit_price(
            message_body=clean_body,
            case_data=case_data,
        )
        if deterministic_price is not None:
            return add_structured_dimensions(
                build_deterministic_rfq_offer_result(deterministic_price)
            )

        tentative_price = extract_tentative_rfq_unit_price(
            message_body=clean_body,
            case_data=case_data,
        )
        if tentative_price is not None:
            return build_deterministic_tentative_rfq_result(
                tentative_price
            )

    history_text = _format_recent_history(message_history)

    prompt = f"""
You classify one new supplier message for a jewelry purchasing negotiation.
Interpret meaning from the new message, the stage, and the recent conversation.
Do not send a message and do not choose a winner. Return JSON only.

Stage: {conversation_stage}
Supplier state before this reply: {supplier_state or 'UNKNOWN'}
Item: {case_data.get('item_material')}
Requested quantity: {case_data.get('quantity')}
Current supplier best price USD: {supplier_best_price_usd}
Explicit target price USD: {target_price_usd}

Recent conversation:
{history_text}

New supplier-authored text:
{clean_body}

Important negotiation rules:
- During RFQ, one unambiguous price with no other currency is treated as USD.
- During NEGOTIATION, interpret the reply against the explicit target in context.
- "Yes", "agreed", "we can do that", or equivalent after an explicit target means
  TARGET_ACCEPTANCE even if the reply does not repeat the number.
- A new clear price below the supplier's previous offer but above target is
  IMPROVED_PRICE_OFFER.
- "We cannot reduce", "our previous price is final", "no", or equivalent is
  PRICE_REFUSAL.
- "We will check and reply tomorrow" with no price is ACKNOWLEDGEMENT_WILL_REPLY.
- If the supplier states one price but says it is uncertain or still needs internal
  verification, use TENTATIVE_PRICE and SAVE_PROVISIONAL_OFFER_AND_WAIT.
- A tentative price is useful information but is not a confirmed offer and must not
  be used for ranking, target calculation, or winner selection.
- Do not confuse quoted earlier email text with the supplier's new intent.
RISK CLASSIFICATION HAS PRIORITY OVER PRICE CLASSIFICATION.

First determine whether the new supplier message contains any risky or
out-of-scope commercial topic.

Risk topics include:
- payment terms;
- deposit or prepayment;
- cash payment;
- delivery problems or delays;
- changed item, material, quantity, or specification;
- quality issues;
- returns or rejected deliveries;
- legal or liability terms;
- customs, sanctions, or compliance;
- confidentiality or exclusivity;
- supplier disputes;
- another unusual topic requiring buyer judgment.

If any risk topic is present:
- contains_risky_topic must be true;
- risk_category must identify the topic;
- message_category must use the corresponding risky category;
- recommended_action must be PAUSE_FOR_REVIEW;
- requires_human_review must be true.

This priority applies even when the message also contains a clear price.

Example:
"We can do 36 USD, but only with a 50 percent deposit."

Correct result:
- contains_risky_topic=true
- risk_category=DEPOSIT_OR_PREPAYMENT
- message_category=DEPOSIT_OR_PREPAYMENT
- recommended_action=PAUSE_FOR_REVIEW
- requires_human_review=true

Do NOT classify this example as CONDITIONAL_PRICE.

CONDITIONAL_PRICE is reserved for price-only commercial conditions such as
quantity tiers or volume thresholds when no risky topic is present.

Payment, deposit, delivery, specification, quality, legal, customs,
confidentiality, disputes, and unusual topics require human review.

Allowed message_category values:
{sorted(MESSAGE_CATEGORIES)}

Allowed recommended_action values:
{sorted(RECOMMENDED_ACTIONS)}

Return exactly one JSON object with these keys:
{{
  "message_category": "ONE_ALLOWED_CATEGORY",
  "recommended_action": "ONE_ALLOWED_ACTION",
  "confidence": "high | medium | low",
  "stated_price_amount": number or null,
  "unit_price_usd": number or null,
  "currency": "USD | OTHER | UNKNOWN | null",
  "price_basis": "UNIT | TOTAL | RANGE | MULTIPLE | UNKNOWN | NONE",
  "is_price_clear": true or false,
  "is_currency_clear": true or false,
  "has_multiple_prices": true or false,
  "is_conditional": true or false,
  "condition_summary": "short text or null",
  "requires_human_review": true or false,
  "contains_risky_topic": true or false,
  "risk_category": "NONE | PAYMENT_TERMS | DEPOSIT_OR_PREPAYMENT | CASH_PAYMENT | DELIVERY_ISSUE | CHANGED_SPECIFICATION | QUALITY_ISSUE | RETURN_OR_REJECTION | LEGAL_OR_LIABILITY | CUSTOMS_OR_COMPLIANCE | CONFIDENTIALITY_OR_EXCLUSIVITY | SUPPLIER_DISPUTE | UNKNOWN",
  "supplier_will_reply_later": true or false,
  "supplier_refused": true or false,
  "supplier_accepts_target": true or false,
  "question_can_be_answered_from_case": true or false,
  "price_certainty": "NONE | TENTATIVE | CONFIRMED",
  "supplier_commitment": "NONE | WILL_VERIFY | CONFIRMED",
  "pending_supplier_action": "short text or null",
  "offer_status": "NONE | PROVISIONAL | CONFIRMED",
  "reason": "short precise explanation",
  "suggested_clarification_question": "question or null",
  "suggested_buyer_reply": "short draft or null"
}}
"""

    provider = None
    try:
        provider = get_llm_provider()
        raw_text = provider.generate(
            prompt,
            timeout_seconds=CLASSIFIER_TIMEOUT_SECONDS,
            temperature=0.0,
        )
        parsed = extract_json_object(raw_text)
        return _normalize_result(
            parsed=parsed,
            conversation_stage=conversation_stage,
            target_price_usd=target_price_usd,
            provider_name=provider.name,
            model_name=provider.model,
        )
    except Exception as exc:
        fallback_provider_name = (
            provider.name
            if provider is not None
            else os.getenv("LLM_PROVIDER", "claude").strip().lower()
        )
        fallback_model_name = provider.model if provider is not None else None
        return _failure_result(
            str(exc),
            provider_name=fallback_provider_name,
            model_name=fallback_model_name,
        )
