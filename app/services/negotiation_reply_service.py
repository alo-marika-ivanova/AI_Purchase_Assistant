from __future__ import annotations

from datetime import datetime

from app.db.repository import PurchasingRepository
from app.services.human_review_notification_service import (
    create_human_review_item_with_notification,
)
from app.llm.communication_writer import write_buyer_message
from app.llm.supplier_message_classifier import (
    analyze_supplier_message_with_ollama,
)
from app.negotiation.common_reply_policy import (
    decide_common_negotiation_reply,
)
from app.negotiation.negotiation_engine import plan_negotiation_round
from app.negotiation.policy import load_negotiation_policy
from app.negotiation.states import CaseState, SupplierState
from app.services.offer_service import add_offer


repo = PurchasingRepository()

# Tolerance for float comparisons of USD unit prices (matches the tolerance
# used in app/negotiation/common_reply_policy.py).
_PRICE_TOLERANCE = 0.005


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

    policy = load_negotiation_policy()

    # Number of price-negotiation rounds already sent to this supplier
    # (>= 1, since this function is only reached once round 1 has been
    # sent). Used to decide whether a refusal or a still-above-target
    # improvement should continue negotiating or finalize.
    existing_round_count = repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="price_reduction_request",
    )

    def continue_or_finalize_negotiation(
        effective_best_price: float,
        finalize_reason: str,
    ) -> dict:
        """
        Persistent but controlled negotiation: send the next round if any
        remain, otherwise finalize and retain the best historical offer.

        Shared by the "improved offer still above target" path and the
        "refusal / same price repeated" path, since both follow the exact
        same round-continuation rule.
        """
        next_round_number = existing_round_count + 1

        if (
            existing_round_count
            >= policy.max_negotiation_rounds_per_supplier
        ):
            repo.set_supplier_policy_state(
                case_id=case_id,
                supplier_id=supplier_id,
                state=SupplierState.FINAL_OFFER_RECEIVED.value,
                best_offer_usd=effective_best_price,
                target_price_usd=target_price_usd,
            )

            repo.log_worker_event(
                case_id=case_id,
                event_type="negotiation_rounds_exhausted",
                details=(
                    f"Supplier ID {supplier_id} reached the maximum of "
                    f"{policy.max_negotiation_rounds_per_supplier} "
                    "negotiation rounds. Best historical offer retained: "
                    f"USD {effective_best_price:.2f}. {finalize_reason}"
                ),
            )

            return {
                "finalized": True,
                "sent_round_number": None,
            }

        # Defensive guards against sending a round we already sent, or
        # sending while still awaiting the reply to an already-sent round.
        # The action lock below is the primary duplicate-prevention
        # mechanism; this re-check of fresh DB state is a second,
        # independent safeguard.
        fresh_state = repo.get_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
        ) or {}

        current_attempts = int(
            fresh_state.get("negotiation_attempts") or 0
        )
        already_awaiting = bool(
            fresh_state.get("awaiting_supplier_reply")
        )

        if current_attempts >= next_round_number or already_awaiting:
            return {
                "finalized": False,
                "sent_round_number": None,
                "skipped_reason": (
                    "The next negotiation round was already sent, or the "
                    "supplier is already awaiting a reply."
                ),
            }

        round_plan = plan_negotiation_round(
            round_number=next_round_number,
            target_price_usd=target_price_usd,
            supplier_best_price_usd=effective_best_price,
        )

        action_key = (
            f"SEND_NEGOTIATION_ROUND:{supplier_id}:{next_round_number}"
        )

        lock_acquired = repo.acquire_action_lock(
            case_id=case_id,
            supplier_id=supplier_id,
            action_key=action_key,
            action_type="SEND_NEGOTIATION_ROUND",
        )

        if not lock_acquired:
            return {
                "finalized": False,
                "sent_round_number": None,
                "skipped_reason": (
                    "Action lock already exists. Duplicate negotiation "
                    "round prevented."
                ),
            }

        # Deferred import: app.services.simple_chat_service imports
        # record_negotiation_supplier_message from this module at module
        # load time, so importing it back at module level here would
        # create a circular import. Importing inside the function body
        # avoids that; both modules are already fully loaded by the time
        # this function actually runs.
        from app.services.simple_chat_service import (
            send_or_display_outbound_message,
        )

        message_result = write_buyer_message(
            intent=round_plan.llm_intent,
            case_data=case_data,
            supplier=supplier,
            message_history=repo.list_messages_for_case_supplier(
                case_id=case_id,
                supplier_id=supplier_id,
            ),
            target_price_usd=round_plan.requested_price_usd,
            supplier_best_price_usd=effective_best_price,
            extra_context=round_plan.extra_context,
        )

        send_result = send_or_display_outbound_message(
            case_id=case_id,
            supplier_id=supplier_id,
            body=message_result["message"],
            message_type="price_reduction_request",
        )

        delivery = send_result.get("send_result")
        real_send_failed = (
            send_result.get("send_real_message")
            and (
                delivery is None
                or not delivery.get("success", False)
            )
        )

        if real_send_failed:
            repo.release_action_lock(
                case_id=case_id,
                supplier_id=supplier_id,
                action_key=action_key,
            )

            return {
                "finalized": False,
                "sent_round_number": None,
                "skipped_reason": (
                    "Next negotiation round was generated, but real "
                    "delivery failed. Supplier state was not advanced."
                ),
            }

        repo.record_negotiation_round_sent(
            case_id=case_id,
            supplier_id=supplier_id,
            strategy=round_plan.strategy,
            requested_price_usd=round_plan.requested_price_usd,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.DISCOUNT_REQUEST_SENT.value,
            best_offer_usd=effective_best_price,
            target_price_usd=target_price_usd,
        )

        repo.log_worker_event(
            case_id=case_id,
            event_type="negotiation_round_sent",
            details=(
                f"Negotiation round {next_round_number} "
                f"({round_plan.strategy}) sent to supplier ID "
                f"{supplier_id}. {finalize_reason}"
            ),
        )

        return {
            "finalized": False,
            "sent_round_number": next_round_number,
        }

    if action == "PAUSE_FOR_REVIEW":
        return pause_for_review(
            review_type="common_negotiation_review",
            reason=common_decision.reason,
        )

    if action == "STOP_NEGOTIATION_HARD":
        repo.update_negotiation_state_after_inbound(
            case_id=case_id,
            supplier_id=supplier_id,
            last_inbound_at=inbound_at,
            best_offer_usd=previous_best_price,
        )

        repo.record_negotiation_hard_stop(
            case_id=case_id,
            supplier_id=supplier_id,
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.FINAL_OFFER_RECEIVED.value,
            best_offer_usd=previous_best_price,
            target_price_usd=target_price_usd,
        )

        repo.log_worker_event(
            case_id=case_id,
            event_type="supplier_requested_hard_stop",
            details=(
                f"Supplier ID {supplier_id} explicitly asked to stop "
                "price negotiation. Automated negotiation ended "
                "immediately. Best historical offer retained: USD "
                f"{previous_best_price if previous_best_price is not None else 0:.2f}."
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
                SupplierState.FINAL_OFFER_RECEIVED.value
            ),
            "case_completed": case_completed,
        }

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

        target_reached = (
            new_price <= target_price_usd + _PRICE_TOLERANCE
        )

        if target_reached:
            # Target accepted (or bettered): save the offer and stop
            # negotiation immediately, regardless of rounds remaining.
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
                event_type="supplier_accepted_target",
                details=(
                    f"Supplier ID {supplier_id} accepted the target "
                    f"price. Final offer: USD {effective_best_price:.2f}."
                ),
            )

            continuation = {"finalized": True, "sent_round_number": None}
        else:
            # Improved, but still above target: continue negotiating if
            # rounds remain, otherwise finalize with the best price seen.
            continuation = continue_or_finalize_negotiation(
                effective_best_price=effective_best_price,
                finalize_reason=(
                    f"Supplier improved to USD {new_price:.2f}, still "
                    f"above target USD {target_price_usd:.2f}."
                ),
            )

        case_completed = (
            _finish_case_if_all_negotiation_replies_received(
                case_id
            )
        )

        supplier_state_value = (
            SupplierState.FINAL_OFFER_RECEIVED.value
            if continuation["finalized"]
            else SupplierState.DISCOUNT_REQUEST_SENT.value
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
            "supplier_state": supplier_state_value,
            "case_completed": case_completed,
            "negotiation_continuation": continuation,
        }

    if action == "RECORD_PRICE_REFUSAL":
        refusal_category = analysis.get("message_category")
        refusal_strength = (
            refusal_category
            if refusal_category in {"SOFT_REFUSAL", "FIRM_REFUSAL"}
            # Legacy/general PRICE_REFUSAL (or the common-reply-policy
            # fallback's "same price repeated" case) has no strength
            # distinction from the classifier. Default to SOFT: it is the
            # more permissive assumption and keeps negotiation going
            # rather than silently treating an ordinary refusal as firm.
            else "SOFT_REFUSAL"
        )

        repo.record_negotiation_refusal(
            case_id=case_id,
            supplier_id=supplier_id,
            refusal_strength=refusal_strength,
        )

        repo.update_negotiation_state_after_inbound(
            case_id=case_id,
            supplier_id=supplier_id,
            last_inbound_at=inbound_at,
            best_offer_usd=previous_best_price,
        )

        continuation = continue_or_finalize_negotiation(
            effective_best_price=previous_best_price,
            finalize_reason=(
                f"Supplier refusal recorded ({refusal_strength}). Best "
                f"known offer retained: USD {previous_best_price:.2f}."
            ),
        )

        if not continuation["finalized"]:
            repo.log_worker_event(
                case_id=case_id,
                event_type="supplier_price_reduction_refused",
                details=(
                    f"Supplier ID {supplier_id} did not improve the "
                    f"existing offer ({refusal_strength}). Existing best "
                    f"offer USD {previous_best_price} retained. Common "
                    f"policy: {common_decision.action}."
                ),
            )

        case_completed = (
            _finish_case_if_all_negotiation_replies_received(
                case_id
            )
        )

        supplier_state_value = (
            SupplierState.FINAL_OFFER_RECEIVED.value
            if continuation["finalized"]
            else SupplierState.DISCOUNT_REQUEST_SENT.value
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
            "supplier_state": supplier_state_value,
            "case_completed": case_completed,
            "negotiation_continuation": continuation,
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
