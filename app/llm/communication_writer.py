from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

from app.llm.json_utils import extract_json_object
from app.llm.provider import get_llm_provider


load_dotenv()

WRITER_TIMEOUT_SECONDS = int(
    os.getenv("LLM_WRITER_TIMEOUT_SECONDS", "45")
)

# Controls whether negotiation messages try the configured LLM provider first.
# If the LLM call fails, fallback templates are used automatically.
USE_LLM_COMMUNICATION_WRITER = (
    os.getenv("USE_LLM_COMMUNICATION_WRITER", "true").lower() == "true"
)


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()

def _normalize_generated_message(value: Any) -> str:
    """
    Validate and normalize one buyer message returned by Ollama.

    Malformed output is rejected so the deterministic fallback template
    can be used instead.
    """
    if not isinstance(value, str):
        raise ValueError("LLM message must be a string.")

    text = (
        value
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )

    if not text:
        raise ValueError("LLM returned an empty message.")

    nonempty_lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    # Detect output where nearly every character is on a separate line.
    if len(nonempty_lines) >= 12:
        short_line_count = sum(
            len(line) <= 2
            for line in nonempty_lines
        )

        short_line_ratio = (
            short_line_count
            / len(nonempty_lines)
        )

        if short_line_ratio >= 0.55:
            raise ValueError(
                "LLM returned character-per-line formatting."
            )

    normalized = "\n".join(nonempty_lines)

    if len(normalized) > 1500:
        raise ValueError(
            "LLM returned an excessively long message."
        )

    return normalized


def _format_usd_price(value: float) -> str:
    formatted = f"{float(value):.2f}"

    if formatted.endswith(".00"):
        return formatted[:-3]

    return formatted


def _message_mentions_target_price(
    message: str,
    target_price_usd: float,
) -> bool:
    """
    Ensure a target-price negotiation message states the actual target.
    """
    normalized = " ".join(
        message.lower().split()
    )

    compact = _format_usd_price(
        target_price_usd
    )

    fixed = f"{float(target_price_usd):.2f}"

    accepted_forms = {
        f"${compact}",
        f"${fixed}",
        f"usd {compact}",
        f"usd {fixed}",
        f"{compact} usd",
        f"{fixed} usd",
    }

    return any(
        form in normalized
        for form in accepted_forms
    )

def _format_recent_history(message_history: list[dict], limit: int = 8) -> str:
    """
    Convert recent message history into compact text for the LLM.

    We keep it short to avoid slow prompts and irrelevant context.
    """
    if not message_history:
        return "No previous messages with this supplier."

    lines: list[str] = []

    for msg in message_history[-limit:]:
        direction = msg.get("direction")
        body = _safe_text(msg.get("body"))

        if not body:
            continue

        if direction == "inbound":
            speaker = msg.get("supplier_name") or "Supplier"
        else:
            speaker = "Buyer"

        lines.append(f"{speaker}: {body}")

    return "\n".join(lines) if lines else "No previous messages with this supplier."


def _supplier_style_variant(supplier: dict) -> int:
    """Return a stable wording variant for one supplier."""
    key = str(
        supplier.get("supplier_code")
        or supplier.get("name")
        or "supplier"
    )
    return sum(ord(character) for character in key) % 3


def _supplier_style_hint(supplier: dict) -> str:
    hints = (
        "Use a direct, concise business tone with a simple greeting.",
        "Use a polite, slightly warmer professional tone while staying brief.",
        "Use a compact, formal purchasing tone without sounding stiff.",
    )
    return hints[_supplier_style_variant(supplier)]


def _fallback_opening(supplier: dict) -> str:
    openings = ("Hello", "Hi", "Good day")
    return openings[_supplier_style_variant(supplier)]



def fallback_message(
    intent: str,
    case_data: dict,
    supplier: dict,
    target_price_usd: float | None = None,
    supplier_best_price_usd: float | None = None,
    winning_price_usd: float | None = None,
) -> dict:
    """Safe supplier-specific fallback used when Ollama is unavailable."""
    supplier_name = supplier.get("name", "there")
    item = case_data.get("item_material")
    quantity = case_data.get("quantity")
    notes = case_data.get("notes")
    variant = _supplier_style_variant(supplier)
    opening = _fallback_opening(supplier)

    note_text = f" Additional details: {notes}" if notes else ""

    if intent == "initial_rfq":
        templates = (
            (
                f"{opening} {supplier_name},\n\n"
                f"Could you please quote your best unit price in USD for "
                f"{quantity} unit(s) of {item}?{note_text}\n\n"
                "Best regards"
            ),
            (
                f"{opening} {supplier_name},\n\n"
                f"We are currently sourcing {item}, quantity {quantity}. "
                f"Please send your most competitive unit quotation in USD."
                f"{note_text}\n\nThank you."
            ),
            (
                f"{opening} {supplier_name},\n\n"
                f"Please provide your best USD unit offer for {item} "
                f"in the requested quantity of {quantity}.{note_text}\n\n"
                "Kind regards"
            ),
        )
        body = templates[variant]
        reason = "Supplier-specific fallback RFQ."

    elif intent == "followup_no_response":
        templates = (
            (
                f"{opening} {supplier_name},\n\n"
                f"I am following up on our quotation request for {item}, "
                f"quantity {quantity}. Could you send your best unit price in USD?\n\n"
                "Best regards"
            ),
            (
                f"{opening} {supplier_name},\n\n"
                f"Could you please update us with your best USD unit offer for "
                f"{item}, quantity {quantity}?\n\nThank you."
            ),
            (
                f"{opening} {supplier_name},\n\n"
                f"A short reminder regarding the RFQ for {quantity} unit(s) of "
                f"{item}. Please let us know your best unit price in USD.\n\n"
                "Kind regards"
            ),
        )
        body = templates[variant]
        reason = "Supplier-specific fallback RFQ reminder."

    elif intent == "acknowledge_tentative_price":
        if supplier_best_price_usd is None:
            raise ValueError(
                "Provisional price is required for acknowledgment wording."
            )

        provisional_text = _format_usd_price(supplier_best_price_usd)
        body = (
            f"{opening} {supplier_name},\n\n"
            f"Thank you for the update. Please confirm the USD "
            f"{provisional_text} unit price once you have verified it "
            f"internally.\n\nBest regards"
        )
        reason = "Fallback acknowledgment of provisional price."

    elif intent == "clarify_price":
        body = (
            f"{opening} {supplier_name},\n\n"
            f"Thank you for your reply. Could you please confirm one unit price "
            f"in USD that applies to {item}, quantity {quantity}?\n\n"
            "Best regards"
        )
        reason = "Fallback price clarification."

    elif intent == "answer_supplier_question":
        body = (
            f"{opening} {supplier_name},\n\n"
            f"The request is for {item}, quantity {quantity}. "
            f"Could you now please confirm your best unit price in USD?\n\n"
            "Best regards"
        )
        reason = "Fallback case-information answer."

    elif intent == "ask_for_target_price":
        if target_price_usd is None:
            raise ValueError("Target price is required for negotiation wording.")

        target_text = _format_usd_price(target_price_usd)
        current_text = (
            _format_usd_price(supplier_best_price_usd)
            if supplier_best_price_usd is not None
            else None
        )

        current_phrase = (
            f"Thank you for your offer of USD {current_text} per unit for {item}. "
            if current_text is not None
            else f"Thank you for your offer for {item}. "
        )

        requests = (
            f"Could you please confirm whether you can reach USD {target_text} per unit?",
            f"Would it be possible to improve the price to USD {target_text} per unit?",
            f"Please let us know whether you can offer USD {target_text} per unit.",
        )

        body = (
            f"{opening} {supplier_name},\n\n"
            f"{current_phrase}{requests[variant]}\n\n"
            "Best regards"
        )
        reason = "Supplier-specific fallback target request."

    elif intent == "winner_notification":
        price_text = (
            _format_usd_price(winning_price_usd)
            if winning_price_usd is not None
            else ""
        )
        templates = (
            (
                f"{opening} {supplier_name},\n\n"
                f"Thank you for your quotation. We have selected your offer for "
                f"{item}, quantity {quantity}, at USD {price_text} per unit. "
                "Please confirm receipt.\n\nBest regards"
            ),
            (
                f"{opening} {supplier_name},\n\n"
                f"We would like to confirm that your offer for {item}, quantity "
                f"{quantity}, at USD {price_text} per unit has been selected. "
                "Please acknowledge this message.\n\nThank you."
            ),
            (
                f"{opening} {supplier_name},\n\n"
                f"Your quotation for {item}, quantity {quantity}, at USD "
                f"{price_text} per unit has been approved as the winning offer. "
                "Please confirm receipt.\n\nKind regards"
            ),
        )
        body = templates[variant]
        reason = "Supplier-specific fallback winner notification."

    else:
        body = (
            f"{opening} {supplier_name},\n\n"
            f"Regarding {item}, quantity {quantity}, could you please confirm "
            f"your best unit price in USD?\n\nBest regards"
        )
        reason = "Fallback general supplier message."

    return {
        "success": True,
        "method": "fallback_template",
        "message": body,
        "reason": reason,
        "error": None,
    }



def write_buyer_message(
    intent: str,
    case_data: dict,
    supplier: dict,
    message_history: list[dict] | None = None,
    target_price_usd: float | None = None,
    supplier_best_price_usd: float | None = None,
    winning_price_usd: float | None = None,
    extra_context: str = "",
    use_llm: bool | None = None,
) -> dict:
    """
    Generate a natural buyer message.

    The negotiation engine decides the intent.
    This function only decides wording.

    Important safety rules:
    - Do not confirm purchase unless intent is winner_notification.
    - Do not reveal automation or AI.
    - Do not invent facts.
    - Do not disclose competitor names.
    """

    if use_llm is None:
        use_llm = USE_LLM_COMMUNICATION_WRITER

    if not use_llm:
        return fallback_message(
            intent=intent,
            case_data=case_data,
            supplier=supplier,
            target_price_usd=target_price_usd,
            supplier_best_price_usd=supplier_best_price_usd,
            winning_price_usd=winning_price_usd,
        )

    history_text = _format_recent_history(message_history or [])

    prompt = f"""
You write professional purchasing messages for a jewelry manufacturing buyer.

Your task:
Write the actual buyer message to the supplier.

Style requirements:
- Natural and human-sounding.
- Professional but not stiff.
- Short.
- Clear.
- No robotic template language.
- Follow the supplier-specific style instruction and vary phrasing naturally.
- Avoid sounding like an automated system.
- Do not mention AI, automation, rules, or the system.
- Do not over-explain.
- Do not use aggressive negotiation language.

Business rules:
- Negotiation is only about price.
- Currency is USD.
- Do not invent prices, quantities, suppliers, deadlines, or facts.
- Do not disclose competitor supplier names.
- Do not say an order is confirmed unless the intent is winner_notification.
- For negotiation, ask politely for improvement or target price.
- For acknowledge_tentative_price, acknowledge the provisional amount, ask the
  supplier to confirm it after internal verification, and do not call it final.
- For winner_notification, keep wording careful: selected/approved in the current negotiation context.

Intent:
{intent}

Case:
- Case number: {case_data.get("case_number")}
- Item/material: {case_data.get("item_material")}
- Quantity: {case_data.get("quantity")}
- Notes: {case_data.get("notes") or ""}

Supplier:
- Name: {supplier.get("name")}
- Code: {supplier.get("supplier_code")}
- Supplier-specific style: {_supplier_style_hint(supplier)}

Price context:
- Supplier best price USD: {supplier_best_price_usd}
- Target price USD: {target_price_usd}
- Winning price USD: {winning_price_usd}

Extra context:
{extra_context or "None"}

Recent conversation with this supplier:
{history_text}

Return JSON only in this exact format:
{{
  "message": "the buyer message",
  "reason": "brief explanation"
}}
"""

    try:
        provider = get_llm_provider()
        raw_response = provider.generate(
            prompt,
            timeout_seconds=WRITER_TIMEOUT_SECONDS,
        )
        parsed = extract_json_object(raw_response)

        message = _normalize_generated_message(
            parsed.get("message")
        )

        reason = _safe_text(
            parsed.get("reason")
        )

        if intent == "ask_for_target_price":
            if target_price_usd is None:
                raise ValueError(
                    "Target price is required for "
                    "negotiation wording."
                )

            if not _message_mentions_target_price(
                message=message,
                target_price_usd=target_price_usd,
            ):
                raise ValueError(
                    "LLM negotiation message omitted "
                    "the explicit target price."
                )

        if intent == "acknowledge_tentative_price":
            if supplier_best_price_usd is None:
                raise ValueError(
                    "Provisional price is required for acknowledgment wording."
                )
            if not _message_mentions_target_price(
                message=message,
                target_price_usd=supplier_best_price_usd,
            ):
                raise ValueError(
                    "LLM acknowledgment omitted the provisional price."
                )

        return {
            "success": True,
            "method": f"{provider.name}_communication_writer",
            "message": message,
            "reason": reason or "Generated by communication writer.",
            "error": None,
        }

    except Exception as exc:
        fallback = fallback_message(
            intent=intent,
            case_data=case_data,
            supplier=supplier,
            target_price_usd=target_price_usd,
            supplier_best_price_usd=supplier_best_price_usd,
            winning_price_usd=winning_price_usd,
        )

        fallback["method"] = "fallback_after_llm_provider_failed"
        fallback["error"] = str(exc)
        return fallback