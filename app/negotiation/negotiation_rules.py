from __future__ import annotations

from app.db.repository import PurchasingRepository
from app.negotiation.actions import (
    NegotiationAction,
    NegotiationActionType,
)
from app.negotiation.policy import load_negotiation_policy
from app.negotiation.states import CaseState, SupplierState


repo = PurchasingRepository()


_FINISHED_OR_PAUSED_STATES = {
    SupplierState.DISCOUNT_REQUEST_SENT.value,
    SupplierState.FINAL_OFFER_RECEIVED.value,
    SupplierState.PAUSED_REVIEW.value,
    SupplierState.REJECTED.value,
    SupplierState.NO_RESPONSE.value,
    SupplierState.CLOSED.value,
    SupplierState.WINNER.value,
}


def plan_initial_target_price_actions(
    case_id: int,
) -> list[NegotiationAction]:
    """
    Plan the first target-price request for every supplier with a valid offer.

    Current Step 3B scope:
    - case must already be NEGOTIATING;
    - a persisted comparison and target must exist;
    - send at most one target request per supplier;
    - do not send reminders;
    - do not interpret negotiation replies here.
    """
    policy = load_negotiation_policy()

    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    if case_data.get("status") != CaseState.NEGOTIATING.value:
        return []

    context = repo.get_case_negotiation_context(case_id)

    if context is None:
        raise ValueError(
            "Case is NEGOTIATING but has no "
            "case_negotiation_context row."
        )

    target_price_usd = float(context["target_price_usd"])

    offers = repo.list_best_supplier_offers_for_case(case_id)

    actions: list[NegotiationAction] = []

    for offer in offers:
        supplier_id = int(offer["supplier_id"])
        supplier_best_price_usd = float(
            offer["unit_price_usd"]
        )

        state_row = repo.get_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
        )

        state_value = (
            state_row["state"]
            if state_row
            else SupplierState.NOT_CONTACTED.value
        )

        if state_value in _FINISHED_OR_PAUSED_STATES:
            continue

        if state_value != SupplierState.PRICE_EXTRACTED.value:
            continue

        existing_request_count = (
            repo.count_supplier_outbound_message_type(
                case_id=case_id,
                supplier_id=supplier_id,
                message_type="price_reduction_request",
            )
        )

        if (
            existing_request_count
            >= policy.max_discount_requests_per_supplier
        ):
            repo.set_supplier_policy_state(
                case_id=case_id,
                supplier_id=supplier_id,
                state=SupplierState.DISCOUNT_REQUEST_SENT.value,
                best_offer_usd=supplier_best_price_usd,
                target_price_usd=target_price_usd,
            )
            continue

        actions.append(
            NegotiationAction(
                action_type=(
                    NegotiationActionType.SEND_DISCOUNT_REQUEST
                ),
                case_id=case_id,
                supplier_id=supplier_id,
                message_type="price_reduction_request",
                llm_intent="ask_for_target_price",
                target_price_usd=target_price_usd,
                supplier_best_price_usd=(
                    supplier_best_price_usd
                ),
                reason=(
                    f"Supplier's current offer is USD "
                    f"{supplier_best_price_usd:.2f} per unit. "
                    f"Ask whether the supplier can reach the "
                    f"common target of USD "
                    f"{target_price_usd:.2f} per unit."
                ),
            )
        )

    return actions