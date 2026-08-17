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
from app.negotiation.business_time import compute_next_reminder_due_at
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


def _per_item_negotiation_status(
    supplier_items: list[dict], supplier_id: int
) -> list[dict]:
    """Current best price, own target, and target-reached status for every
    item this supplier already has an active offer for.

    Each order-case item negotiates independently (its own target price,
    its own accept/continue decision) even while other items linked to
    the same supplier are still being negotiated or have already reached
    their own target - this is the per-item source of truth that drives
    that independence. An item this supplier never priced, or one with no
    per-item negotiation context yet (prepare_case_for_negotiation only
    creates one once at least one supplier has an active offer for it),
    is omitted - there is nothing to negotiate with this supplier on it.

    Legacy single-item cases have no case_items, so supplier_items is
    always empty for them and this returns an empty list - callers must
    fall back to the case-wide scalar target/best-price in that case,
    exactly as before this per-item behavior existed.
    """
    statuses: list[dict] = []

    for item in supplier_items:
        case_item_id = int(item["id"])

        item_context = repo.get_case_item_negotiation_context(case_item_id)
        if item_context is None:
            continue

        best_offer = repo.get_best_offer_for_case_item_supplier(
            case_item_id, supplier_id
        )
        if best_offer is None:
            continue

        current_best_price_usd = float(best_offer["unit_price_usd"])
        target_price_usd = float(item_context["target_price_usd"])

        statuses.append(
            {
                "case_item_id": case_item_id,
                "item_material": item["item_material"],
                "current_best_price_usd": current_best_price_usd,
                "target_price_usd": target_price_usd,
                "target_reached": (
                    current_best_price_usd
                    <= target_price_usd + _PRICE_TOLERANCE
                ),
            }
        )

    return statuses


def _item_targets_from_statuses(item_statuses: list[dict]) -> list[dict]:
    """Per-item {item_material, best_price_usd, target_price_usd} entries
    for items still above their own target - the shape write_buyer_message
    and plan_negotiation_round expect, built from only the items this
    negotiation round should actually ask about."""
    return [
        {
            "item_material": status["item_material"],
            "best_price_usd": status["current_best_price_usd"],
            "target_price_usd": status["target_price_usd"],
        }
        for status in item_statuses
        if not status["target_reached"]
    ]


def _item_offers_have_a_usable_confirmed_price(item_offers: object) -> bool:
    """True when at least one item_offers entry carries a confirmed,
    positive price.

    Used to catch a known LLM tendency: the whole-message
    recommended_action can default to ASK_PRICE_CLARIFICATION purely
    because several prices (or a trailing question like "would you
    accept?") appear in one reply, even though the per-item extraction
    already answered the request cleanly. Mirrors
    _item_offers_fully_confirm_supplier_items in simple_chat_service.py,
    but deliberately does not require every linked item to be covered -
    negotiation replies are expected to arrive incrementally, so an item
    this reply doesn't mention simply stays pending for a later round.
    """
    if not isinstance(item_offers, list):
        return False

    for entry in item_offers:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("price_certainty") or "").upper() != "CONFIRMED":
            continue
        try:
            price = float(entry.get("unit_price_usd"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            return True

    return False


def _save_negotiation_item_offer(
    case_id: int,
    case_item_id: int,
    supplier_id: int,
    unit_price_usd: float,
    message_id: int,
    extraction_method: str,
    confidence: str,
    notes: str,
) -> int:
    return add_offer(
        case_id=case_id,
        case_item_id=case_item_id,
        supplier_id=supplier_id,
        unit_price_usd=unit_price_usd,
        quantity=None,
        message_id=message_id,
        extraction_method=extraction_method,
        extraction_confidence=confidence,
        notes=notes,
    )


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


def _process_multi_item_negotiation_reply(
    case_id: int,
    supplier_id: int,
    analysis: dict,
    item_statuses: list[dict],
    supplier_items: list[dict],
    inbound_message_id: int,
    extraction_method: str,
    confidence: str,
    reason: str,
) -> list[dict]:
    """Resolve and save each item a multi-item negotiation reply actually
    addresses, independently of the others.

    Item_offers entries (per-item prices the classifier extracted) are
    matched to a linked item the same way the RFQ-stage path does, and
    saved at their own reported price - guarded per item against a price
    increase over that item's own current best, exactly like the
    single-item guard below, just scoped to one item instead of the whole
    supplier.

    A plain acceptance with no itemized prices at all (e.g. "I agree with
    your proposed prices") is treated as accepting every item still above
    its own target, at that item's own target price - the same
    substitution the single-item TARGET_ACCEPTANCE path already makes,
    just applied per item instead of once for the whole message.

    An item this reply does not address at all (or that couldn't be
    resolved from the message) is left untouched - exactly like a
    partial RFQ-stage reply, it simply waits for a future round.

    Returns one {case_item_id, item_material, unit_price_usd,
    target_reached, offer_id} entry per item actually updated.
    """
    from app.services.simple_chat_service import _resolve_case_item_id

    statuses_by_case_item_id = {
        status["case_item_id"]: status for status in item_statuses
    }
    results: list[dict] = []

    item_offers = analysis.get("item_offers")

    if item_offers:
        for entry in item_offers:
            case_item_id = _resolve_case_item_id(
                entry.get("item_material", ""), supplier_items
            )
            if (
                case_item_id is None
                or case_item_id not in statuses_by_case_item_id
            ):
                continue

            try:
                new_price = float(entry.get("unit_price_usd"))
            except (TypeError, ValueError):
                continue
            if new_price <= 0:
                continue

            status = statuses_by_case_item_id[case_item_id]

            if new_price > status["current_best_price_usd"] + _PRICE_TOLERANCE:
                repo.log_worker_event(
                    case_id=case_id,
                    event_type="negotiation_item_price_increase_ignored",
                    details=(
                        f"Supplier ID {supplier_id} stated USD "
                        f"{new_price:.2f} for {status['item_material']}, "
                        "above the existing best of USD "
                        f"{status['current_best_price_usd']:.2f}. The "
                        "existing price was kept."
                    ),
                )
                continue

            offer_id = _save_negotiation_item_offer(
                case_id=case_id,
                case_item_id=case_item_id,
                supplier_id=supplier_id,
                unit_price_usd=new_price,
                message_id=inbound_message_id,
                extraction_method=extraction_method,
                confidence=confidence,
                notes=reason,
            )

            results.append(
                {
                    "case_item_id": case_item_id,
                    "item_material": status["item_material"],
                    "unit_price_usd": new_price,
                    "target_reached": (
                        new_price
                        <= status["target_price_usd"] + _PRICE_TOLERANCE
                    ),
                    "offer_id": offer_id,
                }
            )

        if results:
            return results

    if analysis.get("message_category") == "TARGET_ACCEPTANCE" or bool(
        analysis.get("supplier_accepts_target")
    ):
        for status in item_statuses:
            if status["target_reached"]:
                continue

            new_price = status["target_price_usd"]

            offer_id = _save_negotiation_item_offer(
                case_id=case_id,
                case_item_id=status["case_item_id"],
                supplier_id=supplier_id,
                unit_price_usd=new_price,
                message_id=inbound_message_id,
                extraction_method=extraction_method,
                confidence=confidence,
                notes=(
                    "Supplier accepted the proposed target price for all "
                    "items still under negotiation (multi-item "
                    "acceptance)."
                ),
            )

            results.append(
                {
                    "case_item_id": status["case_item_id"],
                    "item_material": status["item_material"],
                    "unit_price_usd": new_price,
                    "target_reached": True,
                    "offer_id": offer_id,
                }
            )

    return results


def record_negotiation_supplier_message(
    case_id: int,
    supplier_id: int,
    channel: str,
    body: str,
    analysis_text: str | None = None,
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

    ``analysis_text``, when given, is what the classifier/regex fallback
    read instead of ``body`` - used when the buyer simulates a supplier
    reply that arrived as an attached file: ``body`` stays a short
    human-facing note (stored and shown in the chat) while
    ``analysis_text`` carries the file's extracted content, so the chat
    stays readable and the attachment's own download link is what surfaces
    the full content, not a wall of flattened spreadsheet text.
    """
    clean_body = (body or "").strip()

    if not clean_body:
        raise ValueError("Supplier message body is required.")

    text_for_analysis = (analysis_text or body or "").strip()

    if not text_for_analysis:
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

    # An order-case can hold several items shared across different
    # suppliers; this supplier's negotiation must only reference (and
    # independently track accept/continue decisions for) the items they
    # were actually linked to. Legacy single-item cases have no
    # case_items rows, so supplier_items stays empty and case_data is
    # unchanged - the rest of this function's single-scalar behavior is
    # unaffected for them.
    supplier_items = repo.list_case_items_for_supplier(
        case_id=case_id,
        supplier_id=supplier_id,
    )
    if supplier_items:
        case_data = {**case_data, "items": supplier_items}

    item_statuses = (
        _per_item_negotiation_status(supplier_items, supplier_id)
        if supplier_items
        else []
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
        text_for_analysis
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

    if (
        action == "ASK_PRICE_CLARIFICATION"
        and supplier_items
        and not analysis.get("requires_human_review")
        and not analysis.get("contains_risky_topic")
        and _item_offers_have_a_usable_confirmed_price(
            analysis.get("item_offers")
        )
    ):
        # A known LLM tendency: the whole-message recommended_action can
        # default to "needs clarification" purely because several prices
        # (or a trailing question like "would you accept?") appear in one
        # reply, even though the per-item extraction already answered the
        # request cleanly for every item it addressed. Already fixed for
        # the RFQ stage (_item_offers_fully_confirm_supplier_items in
        # simple_chat_service.py) - this is the same fix for negotiation
        # replies. An item this reply doesn't mention simply stays
        # pending, exactly like a partial reply always has.
        action = "SAVE_OFFER"

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
        item_targets: list[dict] | None = None,
    ) -> dict:
        """
        Persistent but controlled negotiation: send the next round if any
        remain, otherwise finalize and retain the best historical offer.

        Shared by the "improved offer still above target" path and the
        "refusal / same price repeated" path, since both follow the exact
        same round-continuation rule.

        ``item_targets``, when given, is the per-item breakdown (one entry
        per item still above its own target) for a multi-item order - only
        those items are asked about in the next round, mirroring round 1's
        per-item discount request. An empty list means every item this
        supplier is linked to has already reached its own target, so there
        is nothing left to negotiate: the supplier is finalized immediately
        without sending another round. When ``item_targets`` is None (the
        legacy single-item shape), behavior is unchanged from before.
        """
        if item_targets is not None and not item_targets:
            return {"finalized": True, "sent_round_number": None}

        round_target_price_usd = (
            min(entry["target_price_usd"] for entry in item_targets)
            if item_targets
            else target_price_usd
        )
        round_best_price_usd = (
            min(
                entry["best_price_usd"]
                for entry in item_targets
                if entry["target_price_usd"] == round_target_price_usd
            )
            if item_targets
            else effective_best_price
        )

        next_round_number = existing_round_count + 1

        if (
            existing_round_count
            >= policy.max_negotiation_rounds_per_supplier
        ):
            repo.set_supplier_policy_state(
                case_id=case_id,
                supplier_id=supplier_id,
                state=SupplierState.FINAL_OFFER_RECEIVED.value,
                best_offer_usd=round_best_price_usd,
                target_price_usd=round_target_price_usd,
            )

            repo.log_worker_event(
                case_id=case_id,
                event_type="negotiation_rounds_exhausted",
                details=(
                    f"Supplier ID {supplier_id} reached the maximum of "
                    f"{policy.max_negotiation_rounds_per_supplier} "
                    "negotiation rounds. Best historical offer retained: "
                    f"USD {round_best_price_usd:.2f}. {finalize_reason}"
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
            target_price_usd=round_target_price_usd,
            supplier_best_price_usd=round_best_price_usd,
            item_targets=item_targets,
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
            supplier_best_price_usd=round_best_price_usd,
            extra_context=round_plan.extra_context,
            item_targets=item_targets,
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
            next_reminder_due_at=compute_next_reminder_due_at(
                policy, datetime.utcnow()
            ).strftime("%Y-%m-%d %H:%M:%S"),
        )

        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.DISCOUNT_REQUEST_SENT.value,
            best_offer_usd=round_best_price_usd,
            target_price_usd=round_target_price_usd,
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

    if action == "SAVE_OFFER" and supplier_items:
        item_results = _process_multi_item_negotiation_reply(
            case_id=case_id,
            supplier_id=supplier_id,
            analysis=analysis,
            item_statuses=item_statuses,
            supplier_items=supplier_items,
            inbound_message_id=inbound_message_id,
            extraction_method="llm_plus_common_policy",
            confidence=analysis.get("confidence", "low"),
            reason=(
                common_decision.reason
                if common_decision.action != "USE_CLASSIFIER_RESULT"
                else analysis.get("reason", "")
            ),
        )

        if not item_results:
            return pause_for_review(
                review_type="invalid_negotiation_offer_result",
                reason=(
                    "The classifier recommended saving a negotiation "
                    "offer but none of the reply's item price(s) could "
                    "be matched to an item linked to this supplier."
                ),
            )

        # Recompute every linked item's status (not just the ones this
        # reply updated) to decide whether the supplier is fully done or
        # whether some items still need another round.
        updated_statuses = _per_item_negotiation_status(
            supplier_items, supplier_id
        )
        pending_item_targets = _item_targets_from_statuses(updated_statuses)
        overall_best_price = min(
            status["current_best_price_usd"] for status in updated_statuses
        )

        repo.update_negotiation_state_after_inbound(
            case_id=case_id,
            supplier_id=supplier_id,
            last_inbound_at=inbound_at,
            best_offer_usd=overall_best_price,
        )

        item_summary = ", ".join(
            f"{result['item_material']}: USD {result['unit_price_usd']:.2f} "
            "("
            + (
                "target reached"
                if result["target_reached"]
                else "still above target"
            )
            + ")"
            for result in item_results
        )

        if not pending_item_targets:
            repo.set_supplier_policy_state(
                case_id=case_id,
                supplier_id=supplier_id,
                state=SupplierState.FINAL_OFFER_RECEIVED.value,
                best_offer_usd=overall_best_price,
                target_price_usd=min(
                    status["target_price_usd"] for status in updated_statuses
                ),
            )

            repo.log_worker_event(
                case_id=case_id,
                event_type="supplier_accepted_target",
                details=(
                    f"Supplier ID {supplier_id} reached target on every "
                    f"item linked to this reply. {item_summary}."
                ),
            )

            continuation = {"finalized": True, "sent_round_number": None}
        else:
            continuation = continue_or_finalize_negotiation(
                effective_best_price=overall_best_price,
                finalize_reason=f"Item update: {item_summary}.",
                item_targets=pending_item_targets,
            )

        case_completed = _finish_case_if_all_negotiation_replies_received(
            case_id
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
                "unit_price_usd": common_decision.unit_price_usd,
                "reason": common_decision.reason,
            },
            "extraction": {
                "item_offers": item_results,
                "confidence": analysis.get("confidence", "low"),
                "method": "llm_plus_common_policy",
                "needs_review": False,
                "reason": item_summary,
            },
            "saved_offer_id": item_results[0]["offer_id"],
            "saved_offer_ids": [
                result["offer_id"] for result in item_results
            ],
            "review_item_id": None,
            "supplier_state": supplier_state_value,
            "case_completed": case_completed,
            "negotiation_continuation": continuation,
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

        if supplier_items and item_statuses:
            # A refusal changes no price - the items still above their own
            # target are exactly the ones that were already pending before
            # this reply, so the next round (if any) keeps asking about
            # only those, unaffected by items that already reached target.
            item_targets = _item_targets_from_statuses(item_statuses)
            refusal_best_price = min(
                status["current_best_price_usd"] for status in item_statuses
            )
            pending_summary = "; ".join(
                f"{status['item_material']}: USD "
                f"{status['current_best_price_usd']:.2f} vs target USD "
                f"{status['target_price_usd']:.2f}"
                for status in item_statuses
                if not status["target_reached"]
            ) or "no items remain above target"

            finalize_reason = (
                f"Supplier refusal recorded ({refusal_strength}). Items "
                f"still above target: {pending_summary}."
            )
        else:
            item_targets = None
            refusal_best_price = previous_best_price
            finalize_reason = (
                f"Supplier refusal recorded ({refusal_strength}). Best "
                f"known offer retained: USD {previous_best_price:.2f}."
            )

        repo.update_negotiation_state_after_inbound(
            case_id=case_id,
            supplier_id=supplier_id,
            last_inbound_at=inbound_at,
            best_offer_usd=refusal_best_price,
        )

        continuation = continue_or_finalize_negotiation(
            effective_best_price=refusal_best_price,
            finalize_reason=finalize_reason,
            item_targets=item_targets,
        )

        if not continuation["finalized"]:
            repo.log_worker_event(
                case_id=case_id,
                event_type="supplier_price_reduction_refused",
                details=(
                    f"Supplier ID {supplier_id} did not improve the "
                    f"existing offer ({refusal_strength}). {finalize_reason} "
                    f"Common policy: {common_decision.action}."
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
            (
                analysis.get("reason")
                or "The negotiation reply requires human review."
            )
            + (
                f" (LLM error: {analysis['error']})"
                if analysis.get("error")
                else ""
            )
        ),
    )
