from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.db.repository import PurchasingRepository
from app.negotiation.policy import NegotiationPolicy, load_negotiation_policy
from app.negotiation.states import CaseState, SupplierState

repo = PurchasingRepository()


@dataclass(frozen=True)
class RfqRuleAction:
    action_type: str
    case_id: int
    supplier_id: int | None = None
    message_type: str | None = None
    llm_intent: str | None = None
    reason: str = ""


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        pass

    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

def _age_minutes(value: str | None) -> float | None:
    dt = _parse_datetime(value)
    if dt is None:
        return None

    # SQLite CURRENT_TIMESTAMP is UTC.
    # Keep this comparison in UTC to avoid local-time offset errors.
    return (datetime.utcnow() - dt).total_seconds() / 60


def _valid_offer_supplier_count(case_id: int) -> int:
    offers = repo.list_offers_for_case(case_id)

    return len(
        {
            int(offer["supplier_id"])
            for offer in offers
        }
    )


def _plan_supplier_rfq_action(
    case_id: int,
    supplier: dict,
    policy: NegotiationPolicy,
) -> RfqRuleAction | None:
    supplier_id = int(supplier["id"])

    best_offer = repo.get_best_offer_for_case_supplier(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    if best_offer is not None:
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.PRICE_EXTRACTED.value,
        )
        return None

    state = repo.get_supplier_state(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    state_value = (
        state["state"]
        if state
        else SupplierState.NOT_CONTACTED.value
    )

    if state_value in {
        SupplierState.PAUSED_REVIEW.value,
        SupplierState.REJECTED.value,
        SupplierState.NO_RESPONSE.value,
        SupplierState.FINAL_OFFER_RECEIVED.value,
        SupplierState.CLOSED.value,
    }:
        return None

    if state_value == SupplierState.NEEDS_CASE_ANSWER.value:
        return RfqRuleAction(
            action_type="SEND_CASE_ANSWER",
            case_id=case_id,
            supplier_id=supplier_id,
            message_type="supplier_question_response",
            llm_intent="answer_supplier_question",
            reason=(
                "Answer the supplier's latest question using only the "
                "purchasing case data, then repeat the request for the "
                "best USD unit price."
            ),
        )

    if state_value == SupplierState.WAITING_FOR_OFFER.value:
        # Supplier explicitly said that an offer will follow later.
        # Do not send a price clarification immediately.
        return None

    if state_value == SupplierState.RESPONDED_NEEDS_EXTRACTION.value:
        # The inbound supplier message is currently being interpreted.
        # Another process may run the planner concurrently, so wait.
        return None

    if state_value == SupplierState.NEEDS_CLARIFICATION.value:
        clarification_count = repo.count_supplier_outbound_message_type(
            case_id=case_id,
            supplier_id=supplier_id,
            message_type="clarification_request",
        )

        if clarification_count == 0:
            return RfqRuleAction(
                action_type="SEND_CLARIFICATION_REQUEST",
                case_id=case_id,
                supplier_id=supplier_id,
                message_type="clarification_request",
                llm_intent="clarify_price",
                reason=(
                    "Supplier replied, but the local LLM did not find one "
                    "clear, unconditional USD unit price. Ask exactly one "
                    "short clarification question."
                ),
            )

        return None

    if state_value == SupplierState.CLARIFICATION_SENT.value:
        return None

    rfq_count = repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="rfq",
    )

    if rfq_count == 0:
        return RfqRuleAction(
            action_type="SEND_RFQ",
            case_id=case_id,
            supplier_id=supplier_id,
            message_type="rfq",
            llm_intent="initial_rfq",
            reason="No RFQ has been sent to this supplier yet.",
        )

    has_inbound = repo.supplier_has_inbound_message(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    # Never send another RFQ reminder when an inbound message already exists.
    #
    # record_supplier_message_simple() is responsible for classifying the
    # inbound message and either storing a supported state or creating a
    # genuine human-review item.
    #
    # Creating a review item here caused a race while Ollama was still
    # processing the newly imported supplier response.
    if has_inbound:
        return None

    latest_outbound = repo.get_latest_supplier_outbound_message(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    latest_age = _age_minutes(
        latest_outbound.get("created_at") if latest_outbound else None
    )

    first_rfq = repo.get_first_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="rfq",
    )

    rfq_age = _age_minutes(
        first_rfq.get("created_at") if first_rfq else None
    )

    reminder_count = repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="rfq_reminder",
    )

    if (
        rfq_age is not None
        and rfq_age >= policy.rfq_deadline_minutes
        and reminder_count >= policy.max_rfq_reminders
    ):
        latest_inbound = repo.get_latest_supplier_inbound_message(
            case_id=case_id,
            supplier_id=supplier_id,
        )

        latest_outbound = repo.get_latest_supplier_outbound_message(
            case_id=case_id,
            supplier_id=supplier_id,
        )

        inbound_after_latest_outbound = False

        if latest_inbound and latest_outbound:
            inbound_time = _parse_datetime(
                latest_inbound.get("created_at")
            )
            outbound_time = _parse_datetime(
                latest_outbound.get("created_at")
            )

            inbound_after_latest_outbound = (
                inbound_time is not None
                and outbound_time is not None
                and inbound_time >= outbound_time
            )

        if not inbound_after_latest_outbound:
            repo.set_supplier_state(
                case_id=case_id,
                supplier_id=supplier_id,
                state=SupplierState.NO_RESPONSE.value,
            )

        return None

    if latest_age is None:
        return None

    if latest_age < policy.rfq_reminder_wait_minutes:
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.AWAITING_RESPONSE.value,
        )
        return None

    if reminder_count < policy.max_rfq_reminders:
        return RfqRuleAction(
            action_type="SEND_RFQ_REMINDER",
            case_id=case_id,
            supplier_id=supplier_id,
            message_type="rfq_reminder",
            llm_intent="followup_no_response",
            reason=(
                f"No response after {latest_age:.1f} minutes. "
                f"RFQ reminder {reminder_count + 1} of "
                f"{policy.max_rfq_reminders}."
            ),
        )

    if rfq_age is not None and rfq_age >= policy.rfq_deadline_minutes:
        repo.set_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
            state=SupplierState.NO_RESPONSE.value,
        )

    return None

def _case_should_advance(
    case_id: int,
    policy: NegotiationPolicy,
) -> RfqRuleAction | None:
    suppliers = repo.list_case_suppliers(case_id)
    valid_offer_count = _valid_offer_supplier_count(case_id)

    if valid_offer_count >= policy.minimum_valid_offers:
        return RfqRuleAction(
            action_type="PREPARE_NEGOTIATION",
            case_id=case_id,
            reason=(
                f"Minimum valid offers reached: "
                f"{valid_offer_count} of required "
                f"{policy.minimum_valid_offers}. "
                "Prepare supplier ranking and target price."
            ),
        )

    all_finished = True

    for supplier in suppliers:
        supplier_id = int(supplier["id"])

        state = repo.get_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
        )

        state_value = (
            state["state"]
            if state
            else SupplierState.NOT_CONTACTED.value
        )

        if state_value not in {
            SupplierState.PRICE_EXTRACTED.value,
            SupplierState.NO_RESPONSE.value,
            SupplierState.PAUSED_REVIEW.value,
            SupplierState.REJECTED.value,
            SupplierState.FINAL_OFFER_RECEIVED.value,
        }:
            all_finished = False
            break

    if not all_finished:
        return None

    if (
        valid_offer_count == 1
        and policy.allow_limited_competition_with_one_offer
    ):
        return RfqRuleAction(
            action_type="MOVE_CASE_TO_LIMITED_COMPETITION",
            case_id=case_id,
            reason=(
                "Only one valid offer received. "
                "Marking case as limited competition."
            ),
        )

    if valid_offer_count == 0:
        return RfqRuleAction(
            action_type="MOVE_CASE_TO_NO_VALID_OFFERS",
            case_id=case_id,
            reason=(
                "No valid offers received. "
                "Buyer review required."
            ),
        )

    return None

def plan_rfq_stage_actions(
    case_id: int,
) -> list[RfqRuleAction]:
    """
    RFQ-stage planner.

    Handles:
    - initial RFQ;
    - RFQ reminders;
    - clarification requests;
    - supplier questions;
    - no-response state;
    - transition into multi-supplier negotiation.
    """
    policy = load_negotiation_policy()

    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    if case_data["status"] in {
        CaseState.NEGOTIATING.value,
        CaseState.BUYER_REVIEW.value,
        CaseState.LIMITED_COMPETITION.value,
        CaseState.NO_VALID_OFFERS.value,
        CaseState.WINNER_SELECTED.value,
        CaseState.WINNER_NOTIFIED.value,
        CaseState.CLOSED.value,
        CaseState.CANCELLED.value,
    }:
        return []

    early_advance = _case_should_advance(
        case_id=case_id,
        policy=policy,
    )

    if (
        early_advance is not None
        and early_advance.action_type
        == "PREPARE_NEGOTIATION"
    ):
        return [early_advance]

    actions: list[RfqRuleAction] = []

    suppliers = repo.list_case_suppliers(case_id)

    for supplier in suppliers:
        action = _plan_supplier_rfq_action(
            case_id=case_id,
            supplier=supplier,
            policy=policy,
        )

        if action is not None:
            actions.append(action)

    if not actions:
        advance_action = _case_should_advance(
            case_id=case_id,
            policy=policy,
        )

        if advance_action is not None:
            actions.append(advance_action)

    return actions
