from __future__ import annotations

from dataclasses import dataclass


# Persistent but controlled negotiation: up to
# policy.max_negotiation_rounds_per_supplier price-negotiation requests may be
# sent per supplier. Each round uses a distinct strategy/tone, but always
# requests the same case target price -- this module decides the strategy
# and the requested price deterministically; app/llm/communication_writer.py
# only phrases the chosen strategy, it does not choose what to ask for.

STRATEGY_REQUEST_TARGET = "REQUEST_TARGET"
STRATEGY_ACKNOWLEDGE_AND_RECHECK = "ACKNOWLEDGE_AND_RECHECK"
STRATEGY_EXPRESS_INTEREST_REQUEST_IMPROVEMENT = (
    "EXPRESS_INTEREST_REQUEST_IMPROVEMENT"
)
STRATEGY_ASK_ABSOLUTE_BEST = "ASK_ABSOLUTE_BEST"

_STRATEGY_BY_ROUND = {
    1: STRATEGY_REQUEST_TARGET,
    2: STRATEGY_ACKNOWLEDGE_AND_RECHECK,
    3: STRATEGY_EXPRESS_INTEREST_REQUEST_IMPROVEMENT,
    4: STRATEGY_ASK_ABSOLUTE_BEST,
}

_INTENT_BY_STRATEGY = {
    STRATEGY_REQUEST_TARGET: "ask_for_target_price",
    STRATEGY_ACKNOWLEDGE_AND_RECHECK: "acknowledge_refusal_and_recheck_target",
    STRATEGY_EXPRESS_INTEREST_REQUEST_IMPROVEMENT: (
        "express_interest_request_improvement"
    ),
    STRATEGY_ASK_ABSOLUTE_BEST: "ask_absolute_best_price",
}


@dataclass(frozen=True)
class NegotiationRoundPlan:
    """One deterministically chosen negotiation round.

    ``requested_price_usd`` is the case's fixed target price for a
    single-item case; for a multi-item order it is the lowest of the
    still-pending items' own targets (see ``item_targets``), kept only for
    round-tracking/audit purposes - the strategies differ in tone/framing
    across rounds, not in what number is asked for.

    ``item_targets``, when given, is the per-item breakdown (one entry per
    item still above its own target) that the caller should also pass
    through to ``write_buyer_message`` - the same per-item price context
    already used for round 1's initial discount request.
    """

    round_number: int
    strategy: str
    llm_intent: str
    requested_price_usd: float
    extra_context: str
    item_targets: list[dict] | None = None


def strategy_for_round(round_number: int) -> str:
    """Return the strategy identifier for a given negotiation round (1-4)."""
    if round_number not in _STRATEGY_BY_ROUND:
        raise ValueError(
            f"No negotiation strategy is defined for round {round_number}."
        )
    return _STRATEGY_BY_ROUND[round_number]


def intent_for_strategy(strategy: str) -> str:
    """Return the communication-writer intent for a strategy."""
    if strategy not in _INTENT_BY_STRATEGY:
        raise ValueError(f"Unknown negotiation strategy: {strategy!r}")
    return _INTENT_BY_STRATEGY[strategy]


def _extra_context_for_round(
    round_number: int,
    target_price_usd: float,
    supplier_best_price_usd: float,
    item_targets: list[dict] | None = None,
) -> str:
    if item_targets:
        item_summary = "; ".join(
            f"{entry['item_material']}: current USD "
            f"{entry['best_price_usd']:.2f}, target USD "
            f"{entry['target_price_usd']:.2f}"
            for entry in item_targets
        )

        if round_number == 1:
            return (
                "Ask specifically whether the supplier can reach each "
                f"item's own target individually - {item_summary}. This is "
                "the first price-negotiation message. Keep it concise, "
                "natural, commercially firm, and polite. Do not say that "
                "an order is confirmed. Do not invent a deadline or other "
                "conditions."
            )

        if round_number == 2:
            return (
                "The supplier declined at least one item's target. "
                "Acknowledge their previous answer politely, and ask them "
                "to check once more (for example with their supplier or "
                "management) whether each item still above target can "
                f"reach it after all - {item_summary}. Do not sound pushy "
                "or repeat the exact same wording as before. Do not "
                "invent new facts or deadlines."
            )

        if round_number == 3:
            return (
                "The supplier has declined at least one item's target "
                "more than once. Express genuine interest in working with "
                "them and ask for one more concrete price improvement, "
                f"per item still above target - {item_summary}. Make "
                "clear this is an important opportunity for them, without "
                "promising a guaranteed order or naming competitors."
            )

        if round_number == 4:
            return (
                "This is the final price-negotiation message with this "
                "supplier before finalizing the comparison. Ask for their "
                "absolute best possible final USD unit price for each "
                f"item still above target - {item_summary}. Make clear "
                "this is the last opportunity to improve the offer before "
                "a decision is made. Do not disclose competitor names or "
                "prices, and do not promise a guaranteed order."
            )

        raise ValueError(
            f"No negotiation strategy is defined for round {round_number}."
        )

    target_text = f"{target_price_usd:.2f}"
    current_text = f"{supplier_best_price_usd:.2f}"

    if round_number == 1:
        return (
            f"The supplier's own current offer is USD {current_text} per "
            f"unit. Ask specifically whether they can reach USD "
            f"{target_text} per unit. This is the first price-negotiation "
            "message. Keep it concise, natural, commercially firm, and "
            "polite. Do not say that an order is confirmed. Do not invent "
            "a deadline or other conditions."
        )

    if round_number == 2:
        return (
            f"The supplier declined the target price of USD {target_text} "
            "per unit. Acknowledge their previous answer politely, and ask "
            "them to check once more (for example with their supplier or "
            f"management) whether USD {target_text} per unit is possible "
            "after all. Do not sound pushy or repeat the exact same "
            "wording as before. Do not invent new facts or deadlines."
        )

    if round_number == 3:
        return (
            "The supplier has declined the target price more than once. "
            "Express genuine interest in working with them and ask for "
            f"one more concrete price improvement toward USD {target_text} "
            "per unit. Make clear this is an important opportunity for "
            "them, without promising a guaranteed order or naming "
            "competitors."
        )

    if round_number == 4:
        return (
            "This is the final price-negotiation message with this "
            "supplier before finalizing the comparison. Ask for their "
            "absolute best possible final USD unit price. Make clear this "
            "is the last opportunity to improve the offer before a "
            "decision is made. Do not disclose competitor names or "
            "prices, and do not promise a guaranteed order."
        )

    raise ValueError(f"No negotiation strategy is defined for round {round_number}.")


def plan_negotiation_round(
    round_number: int,
    target_price_usd: float,
    supplier_best_price_usd: float,
    item_targets: list[dict] | None = None,
) -> NegotiationRoundPlan:
    """Deterministically choose the strategy, intent, and requested price
    for one negotiation round. The communication writer only phrases this
    plan; it must not change the strategy or the requested price."""
    strategy = strategy_for_round(round_number)

    return NegotiationRoundPlan(
        round_number=round_number,
        strategy=strategy,
        llm_intent=intent_for_strategy(strategy),
        requested_price_usd=float(target_price_usd),
        extra_context=_extra_context_for_round(
            round_number=round_number,
            target_price_usd=float(target_price_usd),
            supplier_best_price_usd=float(supplier_best_price_usd),
            item_targets=item_targets,
        ),
        item_targets=item_targets,
    )


# ---------------------------------------------------------------------
# No-response reminders
#
# Reminders are a separate concern from negotiation rounds above: they
# never request a new price and must never count as a persuasive
# negotiation round. Each of the (at most 3) reminders for one
# unanswered round uses a distinct, gradually more conclusive strategy,
# and always states the exact, already-decided target price -- the LLM
# must not invent one.
# ---------------------------------------------------------------------

REMINDER_STATUS_CHECK = "STATUS_CHECK"
REMINDER_TARGET_FOLLOWUP = "TARGET_FOLLOWUP"
REMINDER_FINAL_FOLLOWUP = "FINAL_FOLLOWUP"

_REMINDER_STRATEGY_BY_POSITION = {
    1: REMINDER_STATUS_CHECK,
    2: REMINDER_TARGET_FOLLOWUP,
    3: REMINDER_FINAL_FOLLOWUP,
}

_REMINDER_INTENT_BY_STRATEGY = {
    REMINDER_STATUS_CHECK: "negotiation_status_check",
    REMINDER_TARGET_FOLLOWUP: "negotiation_target_followup",
    REMINDER_FINAL_FOLLOWUP: "negotiation_final_followup",
}


@dataclass(frozen=True)
class NegotiationReminderPlan:
    """One deterministically chosen no-response reminder.

    ``target_price_usd`` is always the case's fixed target price, passed
    through so the communication writer never has to invent a number.
    """

    position: int
    strategy: str
    llm_intent: str
    target_price_usd: float
    extra_context: str


def reminder_strategy_for_position(position: int) -> str:
    """Return the strategy identifier for a reminder position (1-3)."""
    if position not in _REMINDER_STRATEGY_BY_POSITION:
        raise ValueError(
            f"No no-response reminder strategy is defined for position "
            f"{position}."
        )
    return _REMINDER_STRATEGY_BY_POSITION[position]


def reminder_intent_for_strategy(strategy: str) -> str:
    """Return the communication-writer intent for a reminder strategy."""
    if strategy not in _REMINDER_INTENT_BY_STRATEGY:
        raise ValueError(f"Unknown no-response reminder strategy: {strategy!r}")
    return _REMINDER_INTENT_BY_STRATEGY[strategy]


def _extra_context_for_reminder(position: int, target_price_usd: float) -> str:
    target_text = f"{target_price_usd:.2f}"

    if position == 1:
        return (
            "The supplier has not replied to our price-negotiation "
            "message yet. Send a brief, friendly status check asking "
            "whether they have had a chance to look at our requested "
            "price. Keep it short and low-pressure. Do not restate the "
            "exact number as a demand; a light mention is fine, but the "
            "tone should be a simple check-in, not a follow-up push."
        )

    if position == 2:
        return (
            "The supplier still has not replied after a first status "
            "check. Send a direct follow-up asking clearly whether USD "
            f"{target_text} per unit would be possible for them. Be "
            "polite but more direct than the previous message."
        )

    if position == 3:
        return (
            "The supplier still has not replied after two previous "
            "reminders. This is the final reminder before the "
            "comparison is closed. Ask for their best possible price "
            "before a decision is made. Make clear, politely and "
            "professionally, that this is the last opportunity to "
            "respond, without sounding aggressive."
        )

    raise ValueError(
        f"No no-response reminder strategy is defined for position {position}."
    )


def plan_negotiation_reminder(
    position: int,
    target_price_usd: float,
) -> NegotiationReminderPlan:
    """Deterministically choose the strategy, intent, and exact target
    price for one no-response reminder. The communication writer only
    phrases this plan; it must not change the strategy or invent a price."""
    strategy = reminder_strategy_for_position(position)

    return NegotiationReminderPlan(
        position=position,
        strategy=strategy,
        llm_intent=reminder_intent_for_strategy(strategy),
        target_price_usd=float(target_price_usd),
        extra_context=_extra_context_for_reminder(
            position=position,
            target_price_usd=float(target_price_usd),
        ),
    )
