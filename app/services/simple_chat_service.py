from __future__ import annotations

from datetime import datetime

from app.db.repository import PurchasingRepository
from app.services.human_review_notification_service import (
    create_human_review_item_with_notification,
)
from app.llm.communication_writer import write_buyer_message
from app.services.offer_service import add_offer
from app.services.recommendation_service import get_offer_recommendation
from app.services.negotiation_reply_service import (
    record_negotiation_supplier_message,
)
from app.services.transport_delivery_service import (
    attempt_email_delivery,
    attempt_whatsapp_delivery,
)
from app.negotiation.rfq_rules import RfqRuleAction, plan_rfq_stage_actions
from app.negotiation.actions import NegotiationAction, NegotiationActionType
from app.negotiation.negotiation_rules import plan_initial_target_price_actions
from app.negotiation.states import CaseState, SupplierState
from app.negotiation.supplier_message_policy import (
    decide_supplier_message_policy,
)
from app.negotiation.policy import load_negotiation_policy
from app.negotiation.comparison import prepare_case_for_negotiation
from app.llm.supplier_message_classifier import (
    analyze_supplier_message_with_ollama,
)

repo = PurchasingRepository()


def _parse_service_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        pass

    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

def _age_minutes_for_service(value: str | None) -> float | None:
    dt = _parse_service_datetime(value)

    if dt is None:
        return None

    # SQLite CURRENT_TIMESTAMP is UTC.
    # Keep this comparison in UTC to avoid local-time offset errors.
    return (datetime.utcnow() - dt).total_seconds() / 60





def _find_case_supplier(case_id: int, supplier_id: int) -> dict:
    for supplier in repo.list_case_suppliers(case_id):
        if int(supplier["id"]) == int(supplier_id):
            return supplier

    raise ValueError("Supplier is not linked to this case.")


def _case_uses_real_communication(case_id: int) -> bool:
    """Return the communication mode stored on the case.

    The case-level Streamlit checkbox is the single source of truth for
    automatic outbound communication. Environment variables do not switch an
    individual case between simulation and real delivery.
    """
    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    return bool(case_data.get("auto_send_messages"))


def _extract_supplier_authored_text(body: str) -> str:
    """Remove common quoted email history before semantic classification."""
    clean = (body or "").strip()
    separators = (
        "---------- Původní e-mail ----------",
        "---------- Původní e‑mail ----------",
        "-----Original Message-----",
        "----- Original Message -----",
    )

    for separator in separators:
        if separator in clean:
            clean = clean.split(separator, 1)[0].strip()

    return clean or (body or "").strip()


def _build_case_email_subject(
    case_number: str,
    item_material: str,
    supplier_code: str | None,
) -> str:
    if supplier_code:
        return f"[{case_number}] [SUPPLIER:{supplier_code}] RFQ - {item_material}"

    return f"[{case_number}] RFQ - {item_material}"

def _reply_subject(subject: str) -> str:
    clean_subject = (subject or "").strip()

    if not clean_subject:
        return ""

    if clean_subject.lower().startswith("re:"):
        return clean_subject

    return f"Re: {clean_subject}"


def _build_references(
    previous_reference_chain: str | None,
    previous_message_id: str | None,
) -> str | None:
    parts = []

    if previous_reference_chain:
        parts.extend(
            part.strip()
            for part in previous_reference_chain.split()
            if part.strip()
        )

    if previous_message_id:
        parts.append(previous_message_id.strip())

    # Keep order, remove duplicates.
    unique_parts = []
    seen = set()

    for part in parts:
        if part not in seen:
            unique_parts.append(part)
            seen.add(part)

    if not unique_parts:
        return None

    return " ".join(unique_parts)


def build_email_delivery_context(message: dict) -> dict:
    """Build the subject/threading fields needed to (re)send one stored
    outbound email message.

    Shared by the immediate first-attempt path (_send_message_by_email) and
    the transport worker's later retry of the same message, so an outbound
    email is sent with consistent threading headers regardless of which
    attempt actually succeeds. Recomputed from the current thread state
    each time it is called, rather than frozen at enqueue time; if a new
    supplier reply arrives between the first attempt and a retry, a retry
    reflects that newer thread state.
    """
    case_data = repo.get_case_basic(int(message["case_id"]))
    if case_data is None:
        raise ValueError("Case not found.")

    base_subject = _build_case_email_subject(
        case_number=case_data["case_number"],
        item_material=case_data["item_material"],
        supplier_code=message.get("supplier_code"),
    )

    latest_header = None

    if hasattr(repo, "get_latest_email_thread_header"):
        latest_header = repo.get_latest_email_thread_header(
            case_id=int(message["case_id"]),
            supplier_id=int(message["supplier_id"]),
        )

    in_reply_to = None
    references = None

    if latest_header and latest_header.get("internet_message_id"):
        in_reply_to = latest_header.get("internet_message_id")
        references = _build_references(
            previous_reference_chain=latest_header.get("reference_chain"),
            previous_message_id=latest_header.get("internet_message_id"),
        )

        # Use the existing thread subject.
        subject = _reply_subject(latest_header.get("subject") or base_subject)

    else:
        # First RFQ starts a new thread.
        subject = base_subject

    return {
        "subject": subject,
        "in_reply_to": in_reply_to,
        "references": references,
    }


def _send_message_by_email(message_id: int) -> dict:
    message = repo.get_message_by_id(message_id)

    if message is None:
        raise ValueError("Message not found.")

    if message["direction"] != "outbound":
        raise ValueError("Only outbound messages can be sent by email.")

    if not message.get("email"):
        repo.mark_message_send_failed(
            message_id=message_id,
            error="Supplier has no email address.",
        )
        return {
            "success": False,
            "error": "Supplier has no email address.",
        }

    context = build_email_delivery_context(message)

    return attempt_email_delivery(
        message_id=message_id,
        case_id=int(message["case_id"]),
        supplier_id=int(message["supplier_id"]),
        to_email=message["email"],
        subject=context["subject"],
        body=message["body"],
        in_reply_to=context["in_reply_to"],
        references=context["references"],
    )

def _send_message_by_whatsapp(message_id: int) -> dict:
    message = repo.get_message_by_id(message_id)

    if message is None:
        raise ValueError("Message not found.")

    if message["direction"] != "outbound":
        raise ValueError("Only outbound messages can be sent by WhatsApp.")

    whatsapp_number = message.get("whatsapp_number")

    if not whatsapp_number:
        repo.mark_message_send_failed(
            message_id=message_id,
            error="Supplier has no WhatsApp number.",
        )
        return {
            "success": False,
            "error": "Supplier has no WhatsApp number.",
        }

    return attempt_whatsapp_delivery(
        message_id=message_id,
        case_id=int(message["case_id"]),
        supplier_id=int(message["supplier_id"]),
        to_number=whatsapp_number,
        body=message["body"],
    )


def apply_deferred_delivery_side_effects(message_id: int) -> None:
    """Apply the supplier-state/case-status update a synchronous send
    success would have applied immediately, for a message whose first
    attempt failed transiently and was only delivered later by the
    transport worker's automatic retry.

    Must only ever be called from the transport worker's retry path (see
    transport_worker_service._retry_one_due_outbox_job). A message reaches
    that path only after its outbox job was left in transient_failure by a
    previous attempt, so calling this here can never double-apply a state
    transition that a first-attempt success already applied inline in
    execute_rfq_rule_action / execute_negotiation_rule_action /
    generate_and_send_winner_notification_for_supplier below. Kept
    deliberately separate from those functions (rather than sharing code
    with them) so this reconciliation path cannot change their existing,
    already-tested behavior; if the on-success side effects for an action
    type change there, update the matching branch here too.

    Message types with no listed branch had no special on-success state
    update in the synchronous path either, so no action is taken for them.
    """
    message = repo.get_message_by_id(message_id)

    if message is None or message.get("supplier_id") is None:
        return

    case_id = int(message["case_id"])
    supplier_id = int(message["supplier_id"])
    message_type = message.get("message_type")

    supplier_name = message.get("supplier_name") or "the supplier"

    if message_type == "rfq":
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.AWAITING_RESPONSE.value,
        )
        repo.update_case_status_with_event(
            case_id=case_id,
            status=CaseState.COLLECTING_OFFERS.value,
            event_type="rfq_sent",
            details=(
                f"RFQ delivered for supplier {supplier_name} after an "
                "automatic retry."
            ),
        )

    elif message_type == "rfq_reminder":
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.RFQ_REMINDER_SENT.value,
        )

    elif message_type == "clarification_request":
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.CLARIFICATION_SENT.value,
        )
        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.CLARIFICATION_SENT.value,
        )
        repo.update_case_status_with_event(
            case_id=case_id,
            status=CaseState.COLLECTING_OFFERS.value,
            event_type="clarification_request_sent",
            details=(
                f"Clarification request delivered for supplier "
                f"{supplier_name} after an automatic retry."
            ),
        )

    elif message_type == "provisional_price_acknowledgement":
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.AWAITING_PRICE_CONFIRMATION.value,
        )
        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.AWAITING_PRICE_CONFIRMATION.value,
        )
        repo.log_worker_event(
            case_id=case_id,
            event_type="provisional_price_acknowledged",
            details=(
                f"Acknowledged provisional price for supplier "
                f"{supplier_name} after an automatic retry."
            ),
        )

    elif message_type == "supplier_question_response":
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.WAITING_FOR_OFFER.value,
        )
        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.WAITING_FOR_OFFER.value,
        )
        repo.log_worker_event(
            case_id=case_id,
            event_type="supplier_question_answered",
            details=(
                f"Answered case-related question for supplier "
                f"{supplier_name} after an automatic retry."
            ),
        )

    elif message_type == "price_reduction_request":
        best_offer = repo.get_best_offer_for_case_supplier(
            case_id=case_id,
            supplier_id=supplier_id,
        )
        context = repo.get_case_negotiation_context(case_id)

        if best_offer is None or context is None:
            return

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.DISCOUNT_REQUEST_SENT.value,
            best_offer_usd=float(best_offer["unit_price_usd"]),
            target_price_usd=float(context["target_price_usd"]),
        )
        repo.increment_negotiation_attempt(
            case_id=case_id,
            supplier_id=supplier_id,
        )
        repo.log_worker_event(
            case_id=case_id,
            event_type="target_price_request_sent",
            details=(
                f"Target-price request delivered for supplier "
                f"{supplier_name} after an automatic retry."
            ),
        )

    elif message_type == "winner_notification":
        repo.update_case_status_with_event(
            case_id=case_id,
            status=CaseState.WINNER_NOTIFIED.value,
            event_type="winner_notification_sent",
            details=(
                f"Winner notification delivered for supplier "
                f"{supplier_name} after an automatic retry."
            ),
        )

def send_or_display_outbound_message(
    case_id: int,
    supplier_id: int,
    body: str,
    message_type: str,
    send_email: bool = False,
    send_whatsapp: bool = False,
    send_real_message: bool | None = None,
) -> dict:
    """Store an outbound message and deliver it according to case mode.

    The value of ``negotiation_cases.auto_send_messages`` is authoritative:
    - false: store a simulated manual-channel message only;
    - true: deliver through the supplier contact channel.

    ``send_email`` and ``send_whatsapp`` may choose a specific channel for a
    real case, but they cannot turn a simulation case into a real one.
    ``send_real_message`` is retained only for backward-compatible callers and
    is intentionally not used as a mode override.
    """
    clean_body = body.strip()
    if not clean_body:
        raise ValueError("Message body is required.")

    repo.ensure_supplier_linked_to_case(case_id, supplier_id)
    supplier = _find_case_supplier(case_id, supplier_id)
    case_real_mode = _case_uses_real_communication(case_id)

    real_channel = None

    if case_real_mode:
        if send_email:
            real_channel = "email"
        elif send_whatsapp:
            real_channel = "whatsapp"
        else:
            preferred_channel = (
                supplier.get("contact_channel") or ""
            ).strip().lower()

            if preferred_channel in {"email", "whatsapp"}:
                real_channel = preferred_channel
            elif supplier.get("email"):
                real_channel = "email"
            elif supplier.get("whatsapp_number"):
                real_channel = "whatsapp"

        if real_channel is None:
            raise ValueError(
                "This is a real-communication case, but the supplier has no "
                "usable email or WhatsApp contact."
            )

    channel = real_channel or "manual"

    message_id = repo.add_message(
        case_id=case_id,
        supplier_id=supplier_id,
        direction="outbound",
        channel=channel,
        body=clean_body,
        status="sent_simulated",
        message_type=message_type,
        approval_required=False,
        approved_by_buyer=True,
    )

    send_result = None

    if real_channel == "email":
        send_result = _send_message_by_email(message_id)
    elif real_channel == "whatsapp":
        send_result = _send_message_by_whatsapp(message_id)

    return {
        "message_id": message_id,
        "case_real_mode": case_real_mode,
        "send_real_message": bool(real_channel),
        "real_channel": real_channel,
        "send_result": send_result,
    }


def record_supplier_message_simple(
    case_id: int,
    supplier_id: int,
    channel: str,
    body: str,
) -> dict:
    """
    Save and semantically interpret one inbound supplier message.

    Ollama interprets the language. Deterministic application code decides
    which state transition is allowed.
    """
    clean_body = body.strip()
    if not clean_body:
        raise ValueError("Supplier message body is required.")

    repo.ensure_supplier_linked_to_case(case_id, supplier_id)

    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    case_status = case_data.get("status")

    supplier_state_row = repo.get_supplier_state(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    supplier_state_before_reply = (
        supplier_state_row["state"]
        if supplier_state_row
        else SupplierState.NOT_CONTACTED.value
    )

    price_reduction_request_count = repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="price_reduction_request",
    )

    supplier_is_in_negotiation = price_reduction_request_count > 0

    if (
        case_status == CaseState.NEGOTIATING.value
        and supplier_is_in_negotiation
    ):
        return record_negotiation_supplier_message(
            case_id=case_id,
            supplier_id=supplier_id,
            channel=channel,
            body=clean_body,
        )

    inbound_message_id = repo.add_message(
        case_id=case_id,
        supplier_id=supplier_id,
        direction="inbound",
        channel=channel,
        body=clean_body,
        status="recorded",
        message_type="supplier_response",
        approval_required=False,
        approved_by_buyer=False,
    )

    # Mark the message as being interpreted before calling Ollama.
    # The RFQ planner may run concurrently in the Streamlit process or
    # email worker; this state prevents it from treating the new inbound
    # message as an unhandled supplier response while Ollama is working.
    repo.set_supplier_policy_state(
        case_id=case_id,
        supplier_id=supplier_id,
        state=SupplierState.RESPONDED_NEEDS_EXTRACTION.value,
    )

    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    supplier = _find_case_supplier(case_id, supplier_id)

    supplier_state = supplier_state_before_reply

    history = repo.list_messages_for_case_supplier(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    # Classify by this supplier's history, not by the whole case status.
    # A case may already be negotiating with Supplier A while Supplier B is
    # only sending a late RFQ response. Supplier B must still be interpreted
    # as an RFQ-stage reply unless they already received a target-price request.
    conversation_stage = (
        "NEGOTIATION"
        if supplier_is_in_negotiation
        else "RFQ"
    )

    supplier_text = _extract_supplier_authored_text(clean_body)

    latest_provisional_offer = (
        repo.get_latest_provisional_offer_for_case_supplier(
            case_id=case_id,
            supplier_id=supplier_id,
        )
    )

    analysis = analyze_supplier_message_with_ollama(
        message_body=supplier_text,
        case_data=case_data,
        supplier=supplier,
        message_history=history,
        conversation_stage=conversation_stage,
        supplier_state=supplier_state,
        target_price_usd=None,
        provisional_price_usd=(
            float(latest_provisional_offer["unit_price_usd"])
            if latest_provisional_offer is not None
            else None
        ),
    )

    analyzer_recommended_action = analysis.get("recommended_action")
    policy_decision = decide_supplier_message_policy(analysis)
    analysis = {
        **analysis,
        "analyzer_recommended_action": analyzer_recommended_action,
        "recommended_action": policy_decision.action,
        "policy_action": policy_decision.action,
        "policy_reason": policy_decision.reason,
    }
    action = policy_decision.action
    provider = analysis.get("provider")
    if provider == "deterministic":
        extraction_method = "deterministic_rfq_price_parser"
    elif provider == "deterministic_context":
        extraction_method = "deterministic_context_confirmation"
    else:
        extraction_method = "llm_semantic_classifier"

    def pause_for_review(
        review_type: str,
        reason: str,
    ) -> dict:
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.PAUSED_REVIEW.value,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.PAUSED_REVIEW.value,
        )

        review_item_id = create_human_review_item_with_notification(
            case_id=case_id,
            supplier_id=supplier_id,
            message_id=inbound_message_id,
            review_type=review_type,
            reason=reason,
        )

        # Pause only this supplier. Do not stop the entire case.
        repo.log_worker_event(
            case_id=case_id,
            event_type="supplier_paused_for_human_review",
            details=(
                f"Supplier ID {supplier_id} paused. "
                f"Review item ID {review_item_id}. Reason: {reason}"
            ),
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "extraction": {
                "unit_price_usd": analysis.get("unit_price_usd"),
                "confidence": analysis.get("confidence", "low"),
                "method": extraction_method,
                "needs_review": True,
                "reason": reason,
            },
            "saved_offer_id": None,
            "review_item_id": review_item_id,
        }

    if action == "SAVE_PROVISIONAL_OFFER_AND_WAIT":
        unit_price_usd = analysis.get("unit_price_usd")

        if unit_price_usd is None:
            return pause_for_review(
                review_type="invalid_provisional_offer_result",
                reason=(
                    "The analyzer identified a tentative price but did not "
                    "return a usable unit price."
                ),
            )

        repo.supersede_provisional_offers_for_case_supplier(
            case_id=case_id,
            supplier_id=supplier_id,
            reason=(
                "Superseded by a newer provisional price from the supplier."
            ),
        )

        saved_offer_id = add_offer(
            case_id=case_id,
            supplier_id=supplier_id,
            unit_price_usd=float(unit_price_usd),
            quantity=None,
            message_id=inbound_message_id,
            extraction_method=extraction_method,
            extraction_confidence=analysis.get("confidence", "low"),
            notes=analysis.get("reason", ""),
            status="provisional",
        )

        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.AWAITING_PRICE_CONFIRMATION.value,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.AWAITING_PRICE_CONFIRMATION.value,
        )

        repo.update_case_status_with_event(
            case_id=case_id,
            status=CaseState.COLLECTING_OFFERS.value,
            event_type="provisional_supplier_price_recorded",
            details=(
                f"Supplier stated a provisional unit price of USD "
                f"{float(unit_price_usd):.2f}. It is excluded from comparison "
                "until confirmed."
            ),
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "extraction": {
                "unit_price_usd": float(unit_price_usd),
                "confidence": analysis.get("confidence", "low"),
                "method": extraction_method,
                "needs_review": False,
                "offer_status": "provisional",
                "reason": analysis.get("reason", ""),
            },
            "saved_offer_id": saved_offer_id,
            "review_item_id": None,
        }

    if action == "SAVE_OFFER":
        unit_price_usd = analysis.get("unit_price_usd")

        if unit_price_usd is None:
            return pause_for_review(
                review_type="invalid_llm_offer_result",
                reason=(
                    "The classifier recommended saving an offer but did not "
                    "return a usable unit price."
                ),
            )

        repo.supersede_provisional_offers_for_case_supplier(
            case_id=case_id,
            supplier_id=supplier_id,
            reason=(
                "Superseded because the supplier subsequently provided a "
                "confirmed price."
            ),
        )

        saved_offer_id = add_offer(
            case_id=case_id,
            supplier_id=supplier_id,
            unit_price_usd=float(unit_price_usd),
            quantity=None,
            message_id=inbound_message_id,
            extraction_method=extraction_method,
            extraction_confidence=analysis.get("confidence", "low"),
            notes=analysis.get("reason", ""),
        )

        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.PRICE_EXTRACTED.value,
        )

        if supplier_state_before_reply == SupplierState.NO_RESPONSE.value:
            repo.update_case_status_with_event(
                case_id=case_id,
                status=CaseState.COLLECTING_OFFERS.value,
                event_type="late_supplier_response_recorded",
                details=(
                    "Supplier replied after being marked NO_RESPONSE. "
                    f"Late offer was recorded: USD {float(unit_price_usd):.2f}."
                ),
            )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.PRICE_EXTRACTED.value,
            best_offer_usd=float(unit_price_usd),
        )

        case_status = (
            CaseState.NEGOTIATING.value
            if case_data.get("status") == CaseState.NEGOTIATING.value
            else CaseState.COLLECTING_OFFERS.value
        )

        repo.update_case_status_with_event(
            case_id=case_id,
            status=case_status,
            event_type="supplier_offer_recorded",
            details=(
                f"Supplier response recorded. Confirmed unit offer "
                f"USD {unit_price_usd}. LLM category: "
                f"{analysis['message_category']}."
            ),
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "extraction": {
                "unit_price_usd": float(unit_price_usd),
                "confidence": analysis.get("confidence", "low"),
                "method": extraction_method,
                "needs_review": False,
                "offer_status": "confirmed",
                "reason": analysis.get("reason", ""),
            },
            "saved_offer_id": saved_offer_id,
            "review_item_id": None,
        }

    if action == "ASK_PRICE_CLARIFICATION":
        clarification_count = repo.count_supplier_outbound_message_type(
            case_id=case_id,
            supplier_id=supplier_id,
            message_type="clarification_request",
        )

        if clarification_count >= 1:
            return pause_for_review(
                review_type="clarification_failed",
                reason=(
                    "The supplier response remained unclear after one "
                    "clarification request. "
                    f"LLM reason: {analysis.get('reason', '')}"
                ),
            )

        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.NEEDS_CLARIFICATION.value,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.NEEDS_CLARIFICATION.value,
        )

        repo.log_worker_event(
            case_id=case_id,
            event_type="supplier_response_needs_clarification",
            details=(
                f"Supplier ID {supplier_id} needs one price clarification. "
                f"Category: {analysis['message_category']}. "
                f"Reason: {analysis.get('reason', '')}"
            ),
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "extraction": {
                "unit_price_usd": analysis.get("unit_price_usd"),
                "confidence": analysis.get("confidence", "low"),
                "method": extraction_method,
                "needs_review": True,
                "reason": analysis.get("reason", ""),
            },
            "saved_offer_id": None,
            "review_item_id": None,
        }

    if action == "WAIT_FOR_SUPPLIER":
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.WAITING_FOR_OFFER.value,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.WAITING_FOR_OFFER.value,
        )

        repo.log_worker_event(
            case_id=case_id,
            event_type="supplier_will_reply_later",
            details=(
                f"Supplier ID {supplier_id} acknowledged the request and "
                f"will provide an offer later. Reason: "
                f"{analysis.get('reason', '')}"
            ),
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "extraction": None,
            "saved_offer_id": None,
            "review_item_id": None,
        }

    if action == "ANSWER_FROM_CASE_AND_REPEAT_REQUEST":
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.NEEDS_CASE_ANSWER.value,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.NEEDS_CASE_ANSWER.value,
        )

        repo.log_worker_event(
            case_id=case_id,
            event_type="supplier_question_answerable_from_case",
            details=(
                f"Supplier ID {supplier_id} asked a question that can be "
                f"answered from case data. Reason: {analysis.get('reason', '')}"
            ),
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "extraction": None,
            "saved_offer_id": None,
            "review_item_id": None,
        }

    if action == "MARK_REJECTED":
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.REJECTED.value,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.REJECTED.value,
        )

        repo.log_worker_event(
            case_id=case_id,
            event_type="supplier_declined_or_unavailable",
            details=(
                f"Supplier ID {supplier_id} was marked REJECTED. "
                f"Reason: {analysis.get('reason', '')}"
            ),
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "extraction": None,
            "saved_offer_id": None,
            "review_item_id": None,
        }

    if action == "RECORD_PRICE_REFUSAL":
        return pause_for_review(
            review_type="price_refusal_before_step_3",
            reason=(
                "A price-refusal message was received before the Step 3 "
                "negotiation policy is active. "
                f"LLM reason: {analysis.get('reason', '')}"
            ),
        )

    return pause_for_review(
        review_type=analysis.get("message_category", "UNKNOWN"),
        reason=(
            analysis.get("reason")
            or "The message requires human review."
        ),
    )

def continue_negotiation_for_case(
    case_id: int,
    send_email: bool = False,
    send_real_message: bool | None = None,
) -> dict:
    """Advance one case through every immediately available workflow step.

    A single call can both prepare the comparison and send the initial target
    requests. Time-based reminders still happen on later worker cycles.
    """
    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    results: list[dict] = []
    case_real_mode = _case_uses_real_communication(case_id)

    # Phase 1: RFQ collection, reminders, clarification and comparison.
    if case_data.get("status") != CaseState.NEGOTIATING.value:
        actions = plan_rfq_stage_actions(case_id)

        for action in actions:
            results.append(
                execute_rfq_rule_action(
                    action=action,
                    send_email=send_email,
                    send_real_message=case_real_mode,
                )
            )

        case_data = repo.get_case_basic(case_id)
        if case_data is None:
            raise ValueError("Case not found after RFQ-stage processing.")

    # Phase 2: if comparison changed the case to NEGOTIATING, immediately
    # generate the initial target request for each supplier with a valid offer.
    if case_data.get("status") == CaseState.NEGOTIATING.value:
        actions = plan_initial_target_price_actions(case_id)

        for action in actions:
            results.append(
                execute_negotiation_rule_action(
                    action=action,
                    send_email=send_email,
                    send_real_message=case_real_mode,
                )
            )

    return {"actions": results}


def start_negotiating_case(
    case_id: int,
    send_email: bool = False,
    send_real_message: bool | None = None,
) -> dict:
    """Start a ready case or safely continue an already-started case."""
    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    status = case_data.get("status")

    if status in {
        CaseState.DRAFT.value,
        CaseState.READY_TO_START.value,
    }:
        repo.update_case_status_with_event(
            case_id=case_id,
            status=CaseState.CONTACTING_SUPPLIERS.value,
            event_type="negotiation_started",
            details="Buyer started negotiation.",
        )
    elif status in {
        CaseState.BUYER_REVIEW.value,
        CaseState.LIMITED_COMPETITION.value,
        CaseState.NO_VALID_OFFERS.value,
        CaseState.WINNER_SELECTED.value,
        CaseState.WINNER_NOTIFIED.value,
        CaseState.CLOSED.value,
        CaseState.CANCELLED.value,
    }:
        raise ValueError(
            f"Case cannot be started from terminal status {status}."
        )

    return continue_negotiation_for_case(case_id=case_id)



def build_supplier_overview(case_id: int) -> list[dict]:
    suppliers = repo.list_case_suppliers(case_id)

    rows = []

    for supplier in suppliers:
        supplier_id = int(supplier["id"])
        best_offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
        provisional_offer = (
            repo.get_latest_provisional_offer_for_case_supplier(
                case_id=case_id,
                supplier_id=supplier_id,
            )
        )

        rows.append(
            {
                "supplier_id": supplier_id,
                "supplier": supplier["name"],
                "code": supplier["supplier_code"],
                "channel": supplier.get("contact_channel"),
                "email": supplier.get("email"),
                "best_unit_price_usd": (
                    best_offer["unit_price_usd"] if best_offer else None
                ),
                "best_offer_confidence": (
                    best_offer["extraction_confidence"] if best_offer else None
                ),
                "offer_id": best_offer["offer_id"] if best_offer else None,
                "provisional_unit_price_usd": (
                    provisional_offer["unit_price_usd"]
                    if provisional_offer
                    else None
                ),
                "provisional_offer_id": (
                    provisional_offer["offer_id"]
                    if provisional_offer
                    else None
                ),
            }
        )

    return rows


def generate_and_send_winner_notification_for_supplier(
    case_id: int,
    supplier_id: int,
    send_email: bool = False,
    send_real_message: bool | None = None,
) -> dict:
    """Generate the winner message after the buyer selects a supplier.

    Delivery follows the case communication mode. A simulation case only
    stores the notification in chat; a real case uses the supplier channel.
    """
    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    supplier = _find_case_supplier(case_id, supplier_id)
    best_offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)

    if best_offer is None:
        raise ValueError("Selected supplier has no confirmed offer.")

    repo.approve_winner(
        case_id=case_id,
        offer_id=int(best_offer["offer_id"]),
        reason=(
            "Buyer clicked notify winner button for this supplier. "
            "This supplier was selected manually by the buyer."
        ),
    )

    history = repo.list_messages_for_case_supplier(case_id, supplier_id)
    winning_price = float(best_offer["unit_price_usd"])

    message_result = write_buyer_message(
        intent="winner_notification",
        case_data=case_data,
        supplier=supplier,
        message_history=history,
        winning_price_usd=winning_price,
        extra_context=(
            "The buyer clicked the winner notification button. Write a "
            "careful professional notification that this supplier was "
            "selected. Do not mention AI or automation."
        ),
    )

    result = send_or_display_outbound_message(
        case_id=case_id,
        supplier_id=supplier_id,
        body=message_result["message"],
        message_type="winner_notification",
        send_email=send_email,
    )

    send_result = result.get("send_result")

    if send_result is None or send_result.get("success"):
        repo.update_case_status_with_event(
            case_id=case_id,
            status=CaseState.WINNER_NOTIFIED.value,
            event_type="winner_notification_sent",
            details=(
                f"Winner notification generated for {supplier['name']} "
                f"at USD {winning_price}."
            ),
        )

    return {
        "winner_supplier": supplier,
        "winning_price": winning_price,
        "message": message_result["message"],
        "message_method": message_result.get("method"),
        "send_result": send_result,
    }



def get_suggested_winner(case_id: int) -> dict | None:
    return get_offer_recommendation(case_id)


def execute_negotiation_rule_action(
    action: NegotiationAction,
    send_email: bool = False,
    send_real_message: bool = False,
) -> dict:
    """
    Execute one Step 3 negotiation action.

    Current Step 3B scope is intentionally limited to the first target-price
    request. Duplicate prevention is enforced both by message count and by a
    stable action lock.
    """
    if (
        action.action_type
        != NegotiationActionType.SEND_DISCOUNT_REQUEST
    ):
        raise ValueError(
            f"Unsupported negotiation action: {action.action_type}"
        )

    if action.supplier_id is None:
        raise ValueError("Supplier ID is required.")

    if action.target_price_usd is None:
        raise ValueError("Target price is required.")

    if action.supplier_best_price_usd is None:
        raise ValueError("Supplier best price is required.")

    case_data = repo.get_case_basic(action.case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    if case_data.get("status") != CaseState.NEGOTIATING.value:
        return {
            "action": action.action_type.value,
            "supplier_id": action.supplier_id,
            "skipped": True,
            "reason": (
                "Target request skipped because the case is not in "
                "NEGOTIATING state."
            ),
        }

    policy = load_negotiation_policy()

    existing_request_count = (
        repo.count_supplier_outbound_message_type(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            message_type="price_reduction_request",
        )
    )

    if (
        existing_request_count
        >= policy.max_discount_requests_per_supplier
    ):
        return {
            "action": action.action_type.value,
            "supplier_id": action.supplier_id,
            "skipped": True,
            "reason": (
                "Target request skipped because the maximum number of "
                "discount requests has already been sent."
            ),
        }

    state_row = repo.get_supplier_state(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
    )

    state_value = (
        state_row["state"]
        if state_row
        else SupplierState.NOT_CONTACTED.value
    )

    if state_value != SupplierState.PRICE_EXTRACTED.value:
        return {
            "action": action.action_type.value,
            "supplier_id": action.supplier_id,
            "skipped": True,
            "reason": (
                f"Target request skipped because supplier state is "
                f"{state_value}, not PRICE_EXTRACTED."
            ),
        }

    action_key = (
        f"SEND_TARGET_PRICE_REQUEST:{action.supplier_id}:"
        f"{float(action.target_price_usd):.4f}"
    )

    lock_acquired = repo.acquire_action_lock(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
        action_key=action_key,
        action_type=action.action_type.value,
    )

    if not lock_acquired:
        return {
            "action": action.action_type.value,
            "supplier_id": action.supplier_id,
            "skipped": True,
            "reason": (
                "Action lock already exists. Duplicate target request "
                "prevented."
            ),
        }

    supplier = _find_case_supplier(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
    )

    history = repo.list_messages_for_case_supplier(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
    )

    context = repo.get_case_negotiation_context(
        action.case_id
    )

    is_initial_best_supplier = bool(
        context
        and int(context["best_supplier_id"])
        == int(action.supplier_id)
    )

    supplier_position_context = (
        "This supplier currently has the best initial offer, but do not "
        "tell them that and do not weaken the negotiation request."
        if is_initial_best_supplier
        else
        "This supplier's offer is not the best initial offer, but do not "
        "mention competitors, rankings, or any competing price."
    )

    extra_context = (
        f"The supplier's own current offer is USD "
        f"{float(action.supplier_best_price_usd):.2f} per unit. "
        f"Ask specifically whether they can reach USD "
        f"{float(action.target_price_usd):.2f} per unit. "
        f"{supplier_position_context} "
        "This is the first price-negotiation message. Keep it concise, "
        "natural, commercially firm, and polite. Do not say that an order "
        "is confirmed. Do not invent a deadline or other conditions."
    )

    message_result = write_buyer_message(
        intent=action.llm_intent or "ask_for_target_price",
        case_data=case_data,
        supplier=supplier,
        message_history=history,
        target_price_usd=float(action.target_price_usd),
        supplier_best_price_usd=float(
            action.supplier_best_price_usd
        ),
        extra_context=extra_context,
    )

    result = send_or_display_outbound_message(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
        body=message_result["message"],
        message_type=action.message_type or "price_reduction_request",
        send_email=send_email,
        send_real_message=send_real_message,
    )

    send_result = result.get("send_result")
    real_send_failed = (
        result.get("send_real_message")
        and (
            send_result is None
            or not send_result.get("success", False)
        )
    )

    if real_send_failed:
        repo.release_action_lock(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            action_key=action_key,
        )

        return {
            "action": action.action_type.value,
            "supplier": supplier["name"],
            "message_id": result["message_id"],
            "send_result": send_result,
            "state_updated": False,
            "reason": (
                "Target request was generated, but real delivery failed. "
                "Supplier state remains PRICE_EXTRACTED."
            ),
        }

    repo.set_supplier_policy_state(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
        state=SupplierState.DISCOUNT_REQUEST_SENT.value,
        best_offer_usd=float(
            action.supplier_best_price_usd
        ),
        target_price_usd=float(action.target_price_usd),
    )

    repo.increment_negotiation_attempt(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
    )

    repo.log_worker_event(
        case_id=action.case_id,
        event_type="target_price_request_sent",
        details=(
            f"Target-price request sent/generated for supplier "
            f"{supplier['name']}. Supplier offer: USD "
            f"{float(action.supplier_best_price_usd):.2f}; target: USD "
            f"{float(action.target_price_usd):.2f}."
        ),
    )

    return {
        "action": action.action_type.value,
        "supplier": supplier["name"],
        "message_id": result["message_id"],
        "message": message_result["message"],
        "message_method": message_result.get("method"),
        "send_result": send_result,
        "state_updated": True,
        "target_price_usd": float(action.target_price_usd),
        "supplier_best_price_usd": float(
            action.supplier_best_price_usd
        ),
        "reason": action.reason,
    }


def refresh_mailbox_and_continue_case(
    case_id: int,
    send_email: bool = False,
    send_real_message: bool | None = None,
) -> dict:
    """Run one worker/UI cycle for a case.

    Simulation cases never call Microsoft Graph. Real cases import email only
    when at least one selected supplier uses email. Both modes then advance the
    same deterministic workflow.
    """
    from app.services.email_transport_service import import_supplier_emails_for_case

    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    case_real_mode = bool(case_data.get("auto_send_messages"))
    suppliers = repo.list_case_suppliers(case_id)
    has_email_supplier = any(
        (supplier.get("contact_channel") or "").strip().lower() == "email"
        for supplier in suppliers
    )

    if case_real_mode and has_email_supplier:
        import_result = import_supplier_emails_for_case(case_id)
    else:
        import_result = {
            "imported_count": 0,
            "skipped_count": 0,
            "results": [],
            "reason": (
                "Mailbox import skipped for simulation case."
                if not case_real_mode
                else "Mailbox import skipped because this case has no email suppliers."
            ),
        }

    negotiation_result = continue_negotiation_for_case(case_id=case_id)

    return {
        "import_result": import_result,
        "negotiation_result": negotiation_result,
    }


def execute_rfq_rule_action(
    action: RfqRuleAction,
    send_email: bool = False,
    send_real_message: bool = False,
) -> dict:
    case_data = repo.get_case_basic(action.case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    # ------------------------------------------------------------------
    # Case-level actions
    # ------------------------------------------------------------------

    if action.action_type == "PREPARE_NEGOTIATION":
        comparison = prepare_case_for_negotiation(action.case_id)

        return {
            "action": action.action_type,
            "reason": action.reason,
            "comparison": comparison,
        }

    if action.action_type == "MOVE_CASE_TO_BUYER_REVIEW":
        repo.update_case_status_with_event(
            case_id=action.case_id,
            status=CaseState.BUYER_REVIEW.value,
            event_type="case_ready_for_buyer_review",
            details=action.reason,
        )

        return {
            "action": action.action_type,
            "reason": action.reason,
        }

    if action.action_type == "MOVE_CASE_TO_LIMITED_COMPETITION":
        repo.update_case_status_with_event(
            case_id=action.case_id,
            status=CaseState.LIMITED_COMPETITION.value,
            event_type="limited_competition",
            details=action.reason,
        )

        return {
            "action": action.action_type,
            "reason": action.reason,
        }

    if action.action_type == "MOVE_CASE_TO_NO_VALID_OFFERS":
        repo.update_case_status_with_event(
            case_id=action.case_id,
            status=CaseState.NO_VALID_OFFERS.value,
            event_type="no_valid_offers",
            details=action.reason,
        )

        return {
            "action": action.action_type,
            "reason": action.reason,
        }

    # ------------------------------------------------------------------
    # Supplier-level actions
    # ------------------------------------------------------------------

    if action.supplier_id is None:
        raise ValueError("Supplier ID is required for supplier action.")

    policy = load_negotiation_policy()
    provisional_price_for_message: float | None = None

    # ------------------------------------------------------------------
    # Execution-level anti-duplicate and anti-spam guards.
    # These guards are intentionally here, not only in rfq_rules.py,
    # because Streamlit and email_worker can process the same case close together.
    # ------------------------------------------------------------------

    if action.action_type == "SEND_RFQ":
        existing_rfq_count = repo.count_supplier_outbound_message_type(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            message_type="rfq",
        )

        if existing_rfq_count > 0:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "RFQ skipped because this supplier already has an RFQ.",
            }

        action_key = f"SEND_RFQ:{action.supplier_id}"

    elif action.action_type == "SEND_RFQ_REMINDER":
        supplier_has_replied = repo.supplier_has_inbound_message(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
        )

        if supplier_has_replied:
            best_offer = repo.get_best_offer_for_case_supplier(
                case_id=action.case_id,
                supplier_id=action.supplier_id,
            )

            if best_offer is not None:
                repo.set_supplier_state(
                    case_id=action.case_id,
                    supplier_id=action.supplier_id,
                    state=SupplierState.PRICE_EXTRACTED.value,
                )
            else:
                repo.set_supplier_state(
                    case_id=action.case_id,
                    supplier_id=action.supplier_id,
                    state=SupplierState.NEEDS_CLARIFICATION.value,
                )

            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "RFQ reminder skipped because supplier has already replied.",
            }

        latest_outbound = repo.get_latest_supplier_outbound_message(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
        )

        latest_age_minutes = _age_minutes_for_service(
            latest_outbound.get("created_at") if latest_outbound else None
        )

        if latest_age_minutes is None:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "RFQ reminder skipped because latest outbound message time is unknown.",
            }

        if latest_age_minutes < policy.rfq_reminder_wait_minutes:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": (
                    f"RFQ reminder skipped because only {latest_age_minutes:.1f} "
                    f"minutes passed. Required: {policy.rfq_reminder_wait_minutes}."
                ),
            }

        reminder_count = repo.count_supplier_outbound_message_type(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            message_type="rfq_reminder",
        )

        if reminder_count >= policy.max_rfq_reminders:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": (
                    f"RFQ reminder skipped because max reminders already reached: "
                    f"{reminder_count}."
                ),
            }

        action_key = f"SEND_RFQ_REMINDER:{action.supplier_id}:{reminder_count + 1}"

    elif action.action_type == "SEND_CLARIFICATION_REQUEST":
        state = repo.get_supplier_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
        )

        state_value = state["state"] if state else SupplierState.NOT_CONTACTED.value

        if state_value == SupplierState.PAUSED_REVIEW.value:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "Clarification skipped because supplier is paused for human review.",
            }

        best_offer = repo.get_best_offer_for_case_supplier(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
        )

        if best_offer is not None:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "Clarification skipped because supplier already has a valid offer.",
            }

        history_for_guard = repo.list_messages_for_case_supplier(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
        )

        latest_message = history_for_guard[-1] if history_for_guard else None

        if latest_message and latest_message.get("direction") == "outbound":
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "Clarification skipped because latest message is already outbound.",
            }

        clarification_count = repo.count_supplier_outbound_message_type(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            message_type="clarification_request",
        )

        if clarification_count >= 1:
            latest_inbound = repo.get_latest_supplier_inbound_message(
                case_id=action.case_id,
                supplier_id=action.supplier_id,
            )

            review_item_id = create_human_review_item_with_notification(
                case_id=action.case_id,
                supplier_id=action.supplier_id,
                message_id=int(latest_inbound["id"]) if latest_inbound else None,
                review_type="clarification_limit_reached",
                reason="Clarification request was already sent and no valid offer is available.",
            )

            repo.set_supplier_state(
                case_id=action.case_id,
                supplier_id=action.supplier_id,
                state=SupplierState.PAUSED_REVIEW.value,
            )

            repo.set_supplier_policy_state(
                case_id=action.case_id,
                supplier_id=action.supplier_id,
                state=SupplierState.PAUSED_REVIEW.value,
            )

            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": (
                    "Clarification skipped because clarification limit was reached. "
                    f"Human review item ID {review_item_id} created."
                ),
            }

        action_key = f"SEND_CLARIFICATION_REQUEST:{action.supplier_id}:1"

    elif action.action_type == "SEND_PROVISIONAL_PRICE_ACKNOWLEDGEMENT":
        state = repo.get_supplier_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
        )
        state_value = (
            state["state"]
            if state
            else SupplierState.NOT_CONTACTED.value
        )
        if state_value != SupplierState.AWAITING_PRICE_CONFIRMATION.value:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": (
                    "Provisional-price acknowledgment skipped because the "
                    "supplier is no longer awaiting price confirmation."
                ),
            }

        provisional_offer = (
            repo.get_latest_provisional_offer_for_case_supplier(
                case_id=action.case_id,
                supplier_id=action.supplier_id,
            )
        )
        if provisional_offer is None:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "No provisional offer is available to acknowledge.",
            }

        acknowledgement_count = repo.count_supplier_outbound_message_type(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            message_type="provisional_price_acknowledgement",
        )
        if acknowledgement_count > 0:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "Provisional price was already acknowledged.",
            }

        history_for_guard = repo.list_messages_for_case_supplier(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
        )
        latest_message = history_for_guard[-1] if history_for_guard else None
        if not latest_message or latest_message.get("direction") != "inbound":
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": (
                    "Provisional-price acknowledgment skipped because the "
                    "latest message is not inbound."
                ),
            }

        provisional_price_for_message = float(
            provisional_offer["unit_price_usd"]
        )
        action_key = (
            f"SEND_PROVISIONAL_PRICE_ACKNOWLEDGEMENT:"
            f"{action.supplier_id}:{provisional_offer['offer_id']}"
        )

    elif action.action_type == "SEND_CASE_ANSWER":
        state = repo.get_supplier_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
        )

        state_value = (
            state["state"]
            if state
            else SupplierState.NOT_CONTACTED.value
        )

        if state_value != SupplierState.NEEDS_CASE_ANSWER.value:
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "Case answer skipped because supplier no longer needs it.",
            }

        history_for_guard = repo.list_messages_for_case_supplier(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
        )
        latest_message = history_for_guard[-1] if history_for_guard else None

        if not latest_message or latest_message.get("direction") != "inbound":
            return {
                "action": action.action_type,
                "supplier_id": action.supplier_id,
                "skipped": True,
                "reason": "Case answer skipped because the latest message is not inbound.",
            }

        action_key = (
            f"SEND_CASE_ANSWER:{action.supplier_id}:"
            f"{latest_message['id']}"
        )

    else:
        existing_count = repo.count_supplier_outbound_message_type(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            message_type=action.message_type or "manual_note",
        )

        action_key = (
            f"{action.action_type}:"
            f"{action.supplier_id}:"
            f"{action.message_type}:"
            f"{existing_count + 1}"
        )

    lock_acquired = repo.acquire_action_lock(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
        action_key=action_key,
        action_type=action.action_type,
    )

    if not lock_acquired:
        return {
            "action": action.action_type,
            "supplier_id": action.supplier_id,
            "skipped": True,
            "reason": "Action lock already exists. Duplicate automatic action prevented.",
        }

    supplier = _find_case_supplier(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
    )

    history = repo.list_messages_for_case_supplier(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
    )

    if action.action_type == "SEND_RFQ":
        extra_context = (
            "Send the initial RFQ. Ask for best USD unit price. "
            "Mention item/material and quantity. Do not confirm a purchase."
        )

    elif action.action_type == "SEND_RFQ_REMINDER":
        extra_context = (
            "Supplier has not responded to RFQ. Send a short firm reminder. "
            "Ask for the best USD unit price. Do not confirm a purchase."
        )

    elif action.action_type == "SEND_CLARIFICATION_REQUEST":
        extra_context = (
            "Supplier replied but did not provide one clear usable USD unit price. "
            "Read the supplier's latest message and ask exactly one short, specific "
            "clarification question that resolves the actual ambiguity. "
            "Do not negotiate. Do not mention AI or internal rules."
        )

    elif action.action_type == "SEND_PROVISIONAL_PRICE_ACKNOWLEDGEMENT":
        extra_context = (
            "The supplier stated a tentative unit price and said they still "
            "need to verify it internally. Thank them, mention the provisional "
            "amount exactly, ask them to confirm it after verification, and do "
            "not describe it as accepted or final."
        )

    elif action.action_type == "SEND_CASE_ANSWER":
        extra_context = (
            "Answer the supplier's latest question using only facts present in the "
            "case data. Then repeat the request for their best unit price in USD. "
            "Keep the response short and do not invent any information."
        )

    else:
        extra_context = action.reason

    message_result = write_buyer_message(
        intent=action.llm_intent or "custom",
        case_data=case_data,
        supplier=supplier,
        message_history=history,
        supplier_best_price_usd=provisional_price_for_message,
        extra_context=extra_context,
    )

    result = send_or_display_outbound_message(
        case_id=action.case_id,
        supplier_id=action.supplier_id,
        body=message_result["message"],
        message_type=action.message_type or "manual_note",
        send_email=send_email,
        send_real_message=send_real_message,
    )

    send_result = result.get("send_result")
    real_send_failed = (
        result.get("send_real_message")
        and (send_result is None or not send_result.get("success", False))
    )

    if real_send_failed:
        repo.release_action_lock(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            action_key=action_key,
        )

        return {
            "action": action.action_type,
            "supplier": supplier["name"],
            "message_id": result["message_id"],
            "send_result": send_result,
            "state_updated": False,
            "reason": (
                "Message was generated, but real delivery failed. "
                "The workflow state was not advanced and the action may retry."
            ),
        }

    if action.action_type == "SEND_RFQ":
        repo.set_supplier_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            state=SupplierState.AWAITING_RESPONSE.value,
        )

        repo.update_case_status_with_event(
            case_id=action.case_id,
            status=CaseState.COLLECTING_OFFERS.value,
            event_type="rfq_sent",
            details=f"RFQ sent/generated for supplier {supplier['name']}.",
        )

    elif action.action_type == "SEND_RFQ_REMINDER":
        repo.set_supplier_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            state=SupplierState.RFQ_REMINDER_SENT.value,
        )

    elif action.action_type == "SEND_CLARIFICATION_REQUEST":
        repo.set_supplier_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            state=SupplierState.CLARIFICATION_SENT.value,
        )

        repo.set_supplier_policy_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            state=SupplierState.CLARIFICATION_SENT.value,
        )

        repo.update_case_status_with_event(
            case_id=action.case_id,
            status=CaseState.COLLECTING_OFFERS.value,
            event_type="clarification_request_sent",
            details=f"Clarification request sent/generated for supplier {supplier['name']}.",
        )

    elif action.action_type == "SEND_PROVISIONAL_PRICE_ACKNOWLEDGEMENT":
        repo.set_supplier_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            state=SupplierState.AWAITING_PRICE_CONFIRMATION.value,
        )
        repo.set_supplier_policy_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            state=SupplierState.AWAITING_PRICE_CONFIRMATION.value,
        )
        repo.log_worker_event(
            case_id=action.case_id,
            event_type="provisional_price_acknowledged",
            details=(
                f"Acknowledged provisional price from supplier "
                f"{supplier['name']} and requested confirmation."
            ),
        )

    elif action.action_type == "SEND_CASE_ANSWER":
        repo.set_supplier_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            state=SupplierState.WAITING_FOR_OFFER.value,
        )

        repo.set_supplier_policy_state(
            case_id=action.case_id,
            supplier_id=action.supplier_id,
            state=SupplierState.WAITING_FOR_OFFER.value,
        )

        repo.log_worker_event(
            case_id=action.case_id,
            event_type="supplier_question_answered",
            details=(
                f"Answered case-related question for supplier {supplier['name']} "
                "and repeated the USD unit-price request."
            ),
        )

    return {
        "action": action.action_type,
        "supplier": supplier["name"],
        "message_id": result["message_id"],
        "send_result": result.get("send_result"),
        "reason": action.reason,
    }
