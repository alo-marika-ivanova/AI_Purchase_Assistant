from __future__ import annotations

from datetime import datetime

from app.db.repository import PurchasingRepository
from app.negotiation.actions import NegotiationAction, NegotiationActionType
from app.negotiation.policy import load_negotiation_policy
from app.negotiation.states import CaseState, SupplierState
from app.services.negotiation_reply_service import (
    _finish_case_if_all_negotiation_replies_received,
)


repo = PurchasingRepository()


def _parse_datetime(value: str | None) -> datetime | None:
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


def plan_negotiation_reminder_actions(case_id: int) -> list[NegotiationAction]:
    """
    Plan no-response reminders for one NEGOTIATING case, and finalize any
    supplier whose full reminder sequence (3 reminders plus one more
    waiting period) has elapsed without a reply.

    This is a background/worker-cycle planner, mirroring
    app.negotiation.rfq_rules.plan_rfq_stage_actions: it re-reads fresh
    state every time it runs and is safe to call repeatedly. Reminders
    are strictly separate from negotiation rounds -- they never request a
    new price and never touch negotiation_attempts (see
    repository.record_negotiation_reminder_sent).
    """
    policy = load_negotiation_policy()

    case_data = repo.get_case_basic(case_id)
    if case_data is None or case_data.get("status") != CaseState.NEGOTIATING.value:
        return []

    context = repo.get_case_negotiation_context(case_id)
    if context is None:
        return []

    target_price_usd = float(context["target_price_usd"])
    now = datetime.utcnow()

    # Fetched once per case rather than per supplier -- an extra safety
    # net alongside the PAUSED_REVIEW state check below, matching the
    # requirement that no reminder is ever sent while a human-review
    # pause exists for that supplier.
    open_reviews = repo.list_open_human_review_items_for_case(case_id)
    suppliers_with_open_review = {
        int(item["supplier_id"])
        for item in open_reviews
        if item.get("supplier_id") is not None
    }

    actions: list[NegotiationAction] = []

    for supplier in repo.list_case_suppliers(case_id):
        supplier_id = int(supplier["id"])

        state_row = repo.get_supplier_state(
            case_id=case_id,
            supplier_id=supplier_id,
        )

        if state_row is None:
            continue

        # Negotiation must still be active for this supplier: sent a
        # round, awaiting a reply, no hard stop, no human-review pause.
        if state_row["state"] != SupplierState.DISCOUNT_REQUEST_SENT.value:
            continue

        if not bool(state_row["awaiting_supplier_reply"]):
            continue

        if bool(state_row["hard_stop"]):
            continue

        if supplier_id in suppliers_with_open_review:
            continue

        due_at = _parse_datetime(
            state_row.get("next_negotiation_reminder_due_at")
        )

        if due_at is None or now < due_at:
            continue

        reminder_count = int(state_row.get("negotiation_reminder_count") or 0)

        if reminder_count >= policy.max_negotiation_no_response_reminders:
            # The final waiting period after the last reminder has
            # elapsed with no reply: retain the best historical offer and
            # finalize, exactly like a rounds-exhausted refusal.
            best_offer = repo.get_best_offer_for_case_supplier(
                case_id=case_id,
                supplier_id=supplier_id,
            )

            best_price = (
                float(best_offer["unit_price_usd"])
                if best_offer is not None
                else (
                    float(state_row["best_offer_usd"])
                    if state_row.get("best_offer_usd") is not None
                    else None
                )
            )

            repo.set_supplier_policy_state(
                case_id=case_id,
                supplier_id=supplier_id,
                state=SupplierState.FINAL_OFFER_RECEIVED.value,
                best_offer_usd=best_price,
                target_price_usd=target_price_usd,
            )

            repo.log_worker_event(
                case_id=case_id,
                event_type="negotiation_no_response_finalized",
                details=(
                    f"Supplier ID {supplier_id} did not reply after "
                    f"{policy.max_negotiation_no_response_reminders} "
                    "no-response reminders and the final waiting period. "
                    "Best historical offer retained: USD "
                    f"{best_price if best_price is not None else 0:.2f}."
                ),
            )

            # Reuse the exact case-completion check the inbound-reply
            # handler already uses, rather than duplicating it here.
            _finish_case_if_all_negotiation_replies_received(case_id)
            continue

        supplier_best_price_usd = (
            float(state_row["best_offer_usd"])
            if state_row.get("best_offer_usd") is not None
            else target_price_usd
        )

        actions.append(
            NegotiationAction(
                action_type=(
                    NegotiationActionType
                    .SEND_NEGOTIATION_NO_RESPONSE_REMINDER
                ),
                case_id=case_id,
                supplier_id=supplier_id,
                message_type="negotiation_no_response_reminder",
                target_price_usd=target_price_usd,
                supplier_best_price_usd=supplier_best_price_usd,
                reason=(
                    f"No-response reminder {reminder_count + 1} of "
                    f"{policy.max_negotiation_no_response_reminders} is "
                    f"due for supplier ID {supplier_id}."
                ),
            )
        )

    return actions
