from __future__ import annotations

from datetime import datetime

from app.db.repository import PurchasingRepository
from app.services.human_review_notification_service import (
    create_human_review_item_with_notification,
)
from app.llm.supplier_message_classifier import (
    analyze_supplier_message_with_ollama,
)
from app.negotiation.common_reply_policy import (
    decide_common_negotiation_reply,
)
from app.negotiation.states import CaseState, SupplierState
from app.services.offer_service import add_offer


repo = PurchasingRepository()


def _find_case_supplier(case_id: int, supplier_id: int) -> dict:
    for supplier in repo.list_case_suppliers(case_id):
        if int(supplier["id"]) == int(supplier_id):
            return supplier

    raise ValueError("Supplier is not linked to this case.")


def _extract_supplier_authored_text(body: str) -> str:
    """
    Remove common quoted-email history before semantic classification.

    This is transport cleanup, not semantic classification. The complete
    original email body remains stored in the messages table.
    """
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


def _finish_case_if_all_negotiation_replies_received(
    case_id: int,
) -> bool:
    """
    Move a case to BUYER_REVIEW when every supplier that received a target
    request has provided a final response or has been paused/rejected.
    """
    terminal_states = {
        SupplierState.FINAL_OFFER_RECEIVED.value,
        SupplierState.PAUSED_REVIEW.value,
        SupplierState.REJECTED.value,
        SupplierState.NO_RESPONSE.value,
        SupplierState.CLOSED.value,
        SupplierState.WINNER.value,
    }

    negotiated_supplier_count = 0

    for supplier in repo.list_case_suppliers(case_id):
        supplier_id = int(supplier["id"])

        request_count = repo.count_supplier_outbound_message_type(
            case_id=case_id,
            supplier_id=supplier_id,
            message_type="price_reduction_request",
        )

        if request_count == 0:
            continue

        negotiated_supplier_count += 1

        state_row = repo.get_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
        )

        state_value = (
            state_row["state"]
            if state_row
            else SupplierState.NOT_CONTACTED.value
        )

        if state_value not in terminal_states:
            return False

    if negotiated_supplier_count == 0:
        return False

    repo.update_case_status_with_event(
        case_id=case_id,
        status=CaseState.BUYER_REVIEW.value,
        event_type="negotiation_replies_completed",
        details=(
            "All suppliers contacted during price negotiation have "
            "provided a final response or require human review."
        ),
    )

    return True


def record_negotiation_supplier_message(
    case_id: int,
    supplier_id: int,
    channel: str,
    body: str,
) -> dict:
    """
    Handle one supplier reply received after a target-price request.

    Supported common cases:
    - contextual target acceptance;
    - one clear improved unit price;
    - one clear unchanged/final price;
    - refusal to reduce;
    - promise to reply later;
    - risky, compound, or unclear replies -> human review.
    """
    clean_body = (body or "").strip()

    if not clean_body:
        raise ValueError("Supplier message body is required.")

    case_data = repo.get_case_basic(case_id)

    if case_data is None:
        raise ValueError("Case not found.")

    if case_data.get("status") != CaseState.NEGOTIATING.value:
        raise ValueError("Case is not in NEGOTIATING state.")

    repo.ensure_supplier_linked_to_case(
        case_id,
        supplier_id,
    )

    supplier = _find_case_supplier(
        case_id,
        supplier_id,
    )

    previous_state_row = repo.get_supplier_state(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    previous_state = (
        previous_state_row["state"]
        if previous_state_row
        else SupplierState.NOT_CONTACTED.value
    )

    previous_offer = repo.get_best_offer_for_case_supplier(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    previous_best_price = (
        float(previous_offer["unit_price_usd"])
        if previous_offer is not None
        else None
    )

    context = repo.get_case_negotiation_context(
        case_id
    )

    if context is None:
        raise ValueError(
            "Negotiating case has no "
            "case_negotiation_context row."
        )

    target_price_usd = float(
        context["target_price_usd"]
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

    repo.set_supplier_policy_state(
        case_id=case_id,
        supplier_id=supplier_id,
        state=(
            SupplierState
            .RESPONDED_NEEDS_EXTRACTION
            .value
        ),
        best_offer_usd=previous_best_price,
        target_price_usd=target_price_usd,
    )

    history = repo.list_messages_for_case_supplier(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    supplier_text = _extract_supplier_authored_text(
        clean_body
    )

    analysis = analyze_supplier_message_with_ollama(
        message_body=supplier_text,
        case_data=case_data,
        supplier=supplier,
        message_history=history,
        conversation_stage="NEGOTIATION",
        supplier_state=previous_state,
        target_price_usd=target_price_usd,
        supplier_best_price_usd=previous_best_price,
    )

    common_decision = decide_common_negotiation_reply(
        supplier_text=supplier_text,
        analysis=analysis,
        previous_best_price_usd=previous_best_price,
        target_price_usd=target_price_usd,
    )

    action = analysis["recommended_action"]

    if common_decision.action != "USE_CLASSIFIER_RESULT":
        action = common_decision.action

    if (
        common_decision.action == "SAVE_OFFER"
        and common_decision.unit_price_usd is not None
    ):
        analysis["unit_price_usd"] = (
            common_decision.unit_price_usd
        )
        analysis["stated_price_amount"] = (
            common_decision.unit_price_usd
        )
        analysis["currency"] = "USD"
        analysis["price_basis"] = "UNIT"
        analysis["is_price_clear"] = True
        analysis["is_currency_clear"] = True
        analysis["safe_for_automation"] = True
        analysis["requires_human_review"] = False

    inbound_at = datetime.utcnow().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    def pause_for_review(
        review_type: str,
        reason: str,
    ) -> dict:
        repo.update_negotiation_state_after_inbound(
            case_id=case_id,
            supplier_id=supplier_id,
            last_inbound_at=inbound_at,
            best_offer_usd=previous_best_price,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.PAUSED_REVIEW.value,
            best_offer_usd=previous_best_price,
            target_price_usd=target_price_usd,
        )

        review_item_id = create_human_review_item_with_notification(
            case_id=case_id,
            supplier_id=supplier_id,
            message_id=inbound_message_id,
            review_type=review_type,
            reason=reason,
        )

        _finish_case_if_all_negotiation_replies_received(
            case_id
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "common_decision": {
                "action": common_decision.action,
                "unit_price_usd": (
                    common_decision.unit_price_usd
                ),
                "reason": common_decision.reason,
            },
            "extraction": None,
            "saved_offer_id": None,
            "review_item_id": review_item_id,
            "supplier_state": (
                SupplierState.PAUSED_REVIEW.value
            ),
        }

    if action == "PAUSE_FOR_REVIEW":
        return pause_for_review(
            review_type="common_negotiation_review",
            reason=common_decision.reason,
        )

    if action == "SAVE_OFFER":
        unit_price_usd = analysis.get(
            "unit_price_usd"
        )

        if unit_price_usd is None:
            return pause_for_review(
                review_type=(
                    "invalid_negotiation_offer_result"
                ),
                reason=(
                    "The classifier recommended saving a "
                    "negotiation offer but did not return a "
                    "usable unit price."
                ),
            )

        new_price = float(unit_price_usd)

        if (
            previous_best_price is not None
            and new_price
            > previous_best_price + 0.005
        ):
            return pause_for_review(
                review_type="supplier_increased_price",
                reason=(
                    f"Supplier previously offered USD "
                    f"{previous_best_price:.2f} but now stated "
                    f"USD {new_price:.2f}. The lower offer was "
                    f"retained."
                ),
            )

        saved_offer_id = add_offer(
            case_id=case_id,
            supplier_id=supplier_id,
            unit_price_usd=new_price,
            quantity=None,
            message_id=inbound_message_id,
            extraction_method=(
                "llm_plus_common_policy"
            ),
            extraction_confidence=analysis.get(
                "confidence",
                "low",
            ),
            notes=(
                common_decision.reason
                if common_decision.action
                != "USE_CLASSIFIER_RESULT"
                else analysis.get("reason", "")
            ),
        )

        effective_best_price = (
            min(previous_best_price, new_price)
            if previous_best_price is not None
            else new_price
        )

        repo.update_negotiation_state_after_inbound(
            case_id=case_id,
            supplier_id=supplier_id,
            last_inbound_at=inbound_at,
            best_offer_usd=effective_best_price,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=(
                SupplierState
                .FINAL_OFFER_RECEIVED
                .value
            ),
            best_offer_usd=effective_best_price,
            target_price_usd=target_price_usd,
        )

        repo.log_worker_event(
            case_id=case_id,
            event_type=(
                "supplier_final_offer_recorded"
            ),
            details=(
                f"Supplier ID {supplier_id} final offer "
                f"recorded: USD {new_price:.2f}. "
                f"Effective supplier best: USD "
                f"{effective_best_price:.2f}. "
                f"Classifier category: "
                f"{analysis['message_category']}. "
                f"Common policy: "
                f"{common_decision.action}."
            ),
        )

        case_completed = (
            _finish_case_if_all_negotiation_replies_received(
                case_id
            )
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "common_decision": {
                "action": common_decision.action,
                "unit_price_usd": (
                    common_decision.unit_price_usd
                ),
                "reason": common_decision.reason,
            },
            "extraction": {
                "unit_price_usd": new_price,
                "confidence": analysis.get(
                    "confidence",
                    "low",
                ),
                "method": (
                    "llm_plus_common_policy"
                ),
                "needs_review": False,
                "reason": (
                    common_decision.reason
                    if common_decision.action
                    != "USE_CLASSIFIER_RESULT"
                    else analysis.get("reason", "")
                ),
            },
            "saved_offer_id": saved_offer_id,
            "review_item_id": None,
            "supplier_state": (
                SupplierState
                .FINAL_OFFER_RECEIVED
                .value
            ),
            "case_completed": case_completed,
        }

    if action == "RECORD_PRICE_REFUSAL":
        repo.update_negotiation_state_after_inbound(
            case_id=case_id,
            supplier_id=supplier_id,
            last_inbound_at=inbound_at,
            best_offer_usd=previous_best_price,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=(
                SupplierState
                .FINAL_OFFER_RECEIVED
                .value
            ),
            best_offer_usd=previous_best_price,
            target_price_usd=target_price_usd,
        )

        repo.log_worker_event(
            case_id=case_id,
            event_type=(
                "supplier_price_reduction_refused"
            ),
            details=(
                f"Supplier ID {supplier_id} did not "
                f"improve the existing offer. Existing best "
                f"offer USD {previous_best_price} retained. "
                f"Common policy: "
                f"{common_decision.action}."
            ),
        )

        case_completed = (
            _finish_case_if_all_negotiation_replies_received(
                case_id
            )
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "common_decision": {
                "action": common_decision.action,
                "unit_price_usd": (
                    common_decision.unit_price_usd
                ),
                "reason": common_decision.reason,
            },
            "extraction": None,
            "saved_offer_id": None,
            "review_item_id": None,
            "supplier_state": (
                SupplierState
                .FINAL_OFFER_RECEIVED
                .value
            ),
            "case_completed": case_completed,
        }

    if action == "WAIT_FOR_SUPPLIER":
        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=(
                SupplierState
                .DISCOUNT_REQUEST_SENT
                .value
            ),
            best_offer_usd=previous_best_price,
            target_price_usd=target_price_usd,
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "common_decision": {
                "action": common_decision.action,
                "unit_price_usd": (
                    common_decision.unit_price_usd
                ),
                "reason": common_decision.reason,
            },
            "extraction": None,
            "saved_offer_id": None,
            "review_item_id": None,
            "supplier_state": (
                SupplierState
                .DISCOUNT_REQUEST_SENT
                .value
            ),
        }

    if action == "MARK_REJECTED":
        repo.update_negotiation_state_after_inbound(
            case_id=case_id,
            supplier_id=supplier_id,
            last_inbound_at=inbound_at,
            best_offer_usd=previous_best_price,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.REJECTED.value,
            best_offer_usd=previous_best_price,
            target_price_usd=target_price_usd,
        )

        case_completed = (
            _finish_case_if_all_negotiation_replies_received(
                case_id
            )
        )

        return {
            "inbound_message_id": inbound_message_id,
            "analysis": analysis,
            "classification": analysis,
            "common_decision": {
                "action": common_decision.action,
                "unit_price_usd": (
                    common_decision.unit_price_usd
                ),
                "reason": common_decision.reason,
            },
            "extraction": None,
            "saved_offer_id": None,
            "review_item_id": None,
            "supplier_state": SupplierState.REJECTED.value,
            "case_completed": case_completed,
        }

    if action == "ASK_PRICE_CLARIFICATION":
        return pause_for_review(
            review_type="unclear_negotiation_reply",
            reason=(
                "The supplier response to the explicit "
                "target was not clear enough to record as a "
                "final offer. "
                f"LLM reason: "
                f"{analysis.get('reason', '')}"
            ),
        )

    return pause_for_review(
        review_type=analysis.get(
            "message_category",
            "UNKNOWN",
        ),
        reason=(
            analysis.get("reason")
            or "The negotiation reply requires human review."
        ),
    )
