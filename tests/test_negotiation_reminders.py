from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.repository import PurchasingRepository
from app.negotiation.actions import NegotiationAction, NegotiationActionType
from app.negotiation.business_time import add_business_days, compute_next_reminder_due_at
from app.negotiation import negotiation_reminder_rules as reminder_rules_module
from app.negotiation.negotiation_reminder_rules import plan_negotiation_reminder_actions
from app.negotiation.policy import NegotiationPolicy
from app.negotiation.states import CaseState, SupplierState
from app.services import simple_chat_service
from app.services.case_service import create_case
from app.services.offer_service import add_offer


repo = PurchasingRepository()

_DT_FORMAT = "%Y-%m-%d %H:%M:%S"


def _fmt(value: datetime) -> str:
    return value.strftime(_DT_FORMAT)


def _create_negotiating_case_with_round_1_sent(
    supplier_id: int,
    initial_price: float,
    target_discount_percent: float = 10.0,
) -> tuple[int, float]:
    """
    Build a case in NEGOTIATING state with round 1 (the initial
    target-price request) sent through the real execute_negotiation_rule_action
    path, so the reminder clock it schedules matches production behavior
    exactly.

    Returns (case_id, target_price_usd).
    """
    case_id = create_case(
        item_material="Pink Sapphire (PSA)",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_id],
    )

    saved_offer_id = add_offer(
        case_id=case_id,
        supplier_id=supplier_id,
        unit_price_usd=initial_price,
        extraction_method="manual",
        extraction_confidence="human_verified",
        notes="Initial RFQ offer for test setup.",
    )

    target_price_usd = round(
        initial_price * (1 - target_discount_percent / 100), 4
    )

    repo.upsert_case_negotiation_context(
        case_id=case_id,
        initial_best_offer_usd=initial_price,
        target_price_usd=target_price_usd,
        best_supplier_id=supplier_id,
        best_offer_id=saved_offer_id,
        valid_offer_count=1,
        target_discount_percent=target_discount_percent,
        ranking_json="[]",
    )

    repo.update_case_status_with_event(
        case_id=case_id,
        status=CaseState.NEGOTIATING.value,
        event_type="test_setup_negotiating",
        details="Test fixture: case moved directly to NEGOTIATING.",
    )

    repo.set_supplier_policy_state(
        case_id=case_id,
        supplier_id=supplier_id,
        state=SupplierState.PRICE_EXTRACTED.value,
        best_offer_usd=initial_price,
        target_price_usd=target_price_usd,
    )

    action = NegotiationAction(
        action_type=NegotiationActionType.SEND_DISCOUNT_REQUEST,
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="price_reduction_request",
        llm_intent="ask_for_target_price",
        target_price_usd=target_price_usd,
        supplier_best_price_usd=initial_price,
        reason="Test setup: send round 1.",
    )

    result = simple_chat_service.execute_negotiation_rule_action(action)
    assert result.get("state_updated") is True

    return case_id, target_price_usd


def _set_due_at(case_id: int, supplier_id: int, due_at: datetime) -> None:
    from app.db.database import get_connection

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE supplier_negotiation_state
            SET next_negotiation_reminder_due_at = ?
            WHERE case_id = ? AND supplier_id = ?
            """,
            (_fmt(due_at), case_id, supplier_id),
        )
        conn.commit()


def _round_count(case_id: int, supplier_id: int) -> int:
    return repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="price_reduction_request",
    )


def _reminder_count(case_id: int, supplier_id: int) -> int:
    return repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="negotiation_no_response_reminder",
    )


def _run_reminder_cycle(case_id: int) -> list[dict]:
    actions = plan_negotiation_reminder_actions(case_id)
    return [
        simple_chat_service.execute_negotiation_reminder_action(action)
        for action in actions
    ]


# ---------------------------------------------------------------------
# 1. Production business-day timing
# ---------------------------------------------------------------------


def test_production_business_day_timing(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    production_policy = NegotiationPolicy(mode="production")
    monkeypatch.setattr(
        reminder_rules_module, "load_negotiation_policy", lambda: production_policy
    )
    monkeypatch.setattr(
        simple_chat_service, "load_negotiation_policy", lambda: production_policy
    )

    # Business-day arithmetic itself: Friday + 2 business days -> Tuesday.
    friday = datetime(2026, 7, 24, 9, 0, 0)
    assert add_business_days(friday, 2) == datetime(2026, 7, 28, 9, 0, 0)
    assert compute_next_reminder_due_at(production_policy, friday) == datetime(
        2026, 7, 28, 9, 0, 0
    )

    # Not yet due: due_at computed as 2 real business days from now.
    _set_due_at(
        case_id, supplier_id, add_business_days(datetime.utcnow(), 2)
    )
    results = _run_reminder_cycle(case_id)
    assert results == []
    assert _reminder_count(case_id, supplier_id) == 0

    # Due: due_at in the past.
    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
    results = _run_reminder_cycle(case_id)
    assert len(results) == 1
    assert results[0]["state_updated"] is True
    assert _reminder_count(case_id, supplier_id) == 1

    # The next due time was scheduled 2 business days ahead again.
    state = repo.get_supplier_state(case_id, supplier_id)
    new_due_at = datetime.strptime(
        state["next_negotiation_reminder_due_at"], _DT_FORMAT
    )
    assert new_due_at > datetime.utcnow() + timedelta(hours=23)


# ---------------------------------------------------------------------
# 2. Testing two-minute timing
# ---------------------------------------------------------------------


def test_testing_mode_two_minute_timing(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    # Round 1 just sent -- due time should be ~2 minutes from now, not due yet.
    results = _run_reminder_cycle(case_id)
    assert results == []
    assert _reminder_count(case_id, supplier_id) == 0

    # Simulate 2 minutes elapsing.
    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
    results = _run_reminder_cycle(case_id)
    assert len(results) == 1
    assert _reminder_count(case_id, supplier_id) == 1

    state = repo.get_supplier_state(case_id, supplier_id)
    new_due_at = datetime.strptime(
        state["next_negotiation_reminder_due_at"], _DT_FORMAT
    )
    # Roughly 2 minutes ahead again (allow generous slack for test speed).
    assert new_due_at > datetime.utcnow() + timedelta(seconds=100)


# ---------------------------------------------------------------------
# 3. Three-reminder limit
# ---------------------------------------------------------------------


def test_reminder_limit_is_three(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    for expected_count in (1, 2, 3):
        _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
        results = _run_reminder_cycle(case_id)
        assert len(results) == 1
        assert results[0]["state_updated"] is True
        assert _reminder_count(case_id, supplier_id) == expected_count

    state = repo.get_supplier_state(case_id, supplier_id)
    assert state["negotiation_reminder_count"] == 3
    assert state["state"] == SupplierState.DISCOUNT_REQUEST_SENT.value

    # A 4th attempt must not send a 4th reminder.
    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
    results = _run_reminder_cycle(case_id)
    assert results == []
    assert _reminder_count(case_id, supplier_id) == 3
    # The 4th "due" check finalized instead of sending -- see test 4.
    state = repo.get_supplier_state(case_id, supplier_id)
    assert state["state"] == SupplierState.FINAL_OFFER_RECEIVED.value


# ---------------------------------------------------------------------
# 4. Finalization after the final waiting period
# ---------------------------------------------------------------------


def test_finalizes_after_third_reminder_and_final_wait(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    for _ in range(3):
        _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
        _run_reminder_cycle(case_id)

    assert _reminder_count(case_id, supplier_id) == 3

    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
    results = _run_reminder_cycle(case_id)

    assert results == []

    state = repo.get_supplier_state(case_id, supplier_id)
    assert state["state"] == SupplierState.FINAL_OFFER_RECEIVED.value

    best_offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    assert best_offer["unit_price_usd"] == pytest.approx(30.0)

    case_data = repo.get_case_basic(case_id)
    assert case_data["status"] == CaseState.BUYER_REVIEW.value


# ---------------------------------------------------------------------
# 5. Cancellation when a supplier replies
# ---------------------------------------------------------------------


def test_supplier_reply_cancels_pending_reminder_sequence(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    from app.services import negotiation_reply_service

    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    # Send 1 reminder for round 1.
    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
    _run_reminder_cycle(case_id)
    assert _reminder_count(case_id, supplier_id) == 1

    state_before = repo.get_supplier_state(case_id, supplier_id)
    assert state_before["negotiation_reminder_count"] == 1

    # A genuine supplier reply arrives (a soft refusal continues to round 2).
    monkeypatch.setattr(
        negotiation_reply_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: {
            "success": True,
            "message_category": "SOFT_REFUSAL",
            "recommended_action": "RECORD_PRICE_REFUSAL",
            "unit_price_usd": None,
            "requires_human_review": False,
            "safe_for_automation": True,
        },
    )

    negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="This is our final price, no more margin.",
    )

    # The reminder sequence for round 1 was cancelled -- round 2 starts a
    # fresh reminder clock at count 0, not due immediately.
    state_after = repo.get_supplier_state(case_id, supplier_id)
    assert state_after["negotiation_reminder_count"] == 0
    assert state_after["next_negotiation_reminder_due_at"] is not None

    results = _run_reminder_cycle(case_id)
    assert results == []
    assert _reminder_count(case_id, supplier_id) == 1  # unchanged


# ---------------------------------------------------------------------
# 6. Restart persistence
# ---------------------------------------------------------------------


def test_reminder_state_survives_a_restart(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
    _run_reminder_cycle(case_id)

    # Constructing a new repository object simulates a fresh process
    # reading the state persisted by a previous worker/app process.
    restarted_repo = PurchasingRepository()
    state = restarted_repo.get_supplier_state(case_id, supplier_id)

    assert state is not None
    assert state["negotiation_reminder_count"] == 1
    assert state["next_negotiation_reminder_due_at"] is not None
    assert state["followup_sent_at"] is not None
    assert bool(state["awaiting_supplier_reply"]) is True


# ---------------------------------------------------------------------
# 7. Duplicate-worker protection
# ---------------------------------------------------------------------


def test_duplicate_reminder_send_is_prevented_by_action_lock(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))

    # Simulate a concurrent worker execution that already claimed the lock
    # for reminder #1.
    lock_acquired = repo.acquire_action_lock(
        case_id=case_id,
        supplier_id=supplier_id,
        action_key=f"SEND_NEGOTIATION_NO_RESPONSE_REMINDER:{supplier_id}:1",
        action_type="SEND_NEGOTIATION_NO_RESPONSE_REMINDER",
    )
    assert lock_acquired is True

    actions = plan_negotiation_reminder_actions(case_id)
    assert len(actions) == 1

    result = simple_chat_service.execute_negotiation_reminder_action(actions[0])

    assert result.get("skipped") is True
    assert _reminder_count(case_id, supplier_id) == 0


# ---------------------------------------------------------------------
# 8. Reminders do not increase the negotiation-round count
# ---------------------------------------------------------------------


def test_reminders_do_not_increase_negotiation_round_count(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    assert _round_count(case_id, supplier_id) == 1
    state = repo.get_supplier_state(case_id, supplier_id)
    assert state["negotiation_attempts"] == 1

    for _ in range(3):
        _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
        _run_reminder_cycle(case_id)

    assert _reminder_count(case_id, supplier_id) == 3
    # Negotiation round count/message count must be completely unaffected.
    assert _round_count(case_id, supplier_id) == 1
    state = repo.get_supplier_state(case_id, supplier_id)
    assert state["negotiation_attempts"] == 1


# ---------------------------------------------------------------------
# 9. No reminder during a human-review pause
# ---------------------------------------------------------------------


def test_no_reminder_sent_while_human_review_is_open(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    repo.create_human_review_item(
        case_id=case_id,
        supplier_id=supplier_id,
        message_id=None,
        review_type="test_pause",
        reason="Test: pause this supplier for human review.",
    )

    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))

    actions = plan_negotiation_reminder_actions(case_id)
    assert actions == []
    assert _reminder_count(case_id, supplier_id) == 0


# ---------------------------------------------------------------------
# 10. Correct target price passed to the communication writer
# ---------------------------------------------------------------------


def test_correct_target_price_reaches_the_communication_writer(
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )
    # target_price_usd == 27.0 for a 10% discount off 30.

    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
    results = _run_reminder_cycle(case_id)
    assert len(results) == 1

    # Reminder #1 is a friendly status check (no hard price requirement),
    # so check reminder #2 instead, which must state the exact target.
    _set_due_at(case_id, supplier_id, datetime.utcnow() - timedelta(seconds=1))
    _run_reminder_cycle(case_id)

    latest_reminder = repo.get_latest_supplier_outbound_message_of_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="negotiation_no_response_reminder",
    )

    assert latest_reminder is not None
    assert f"{target_price_usd:.2f}".rstrip("0").rstrip(".") in latest_reminder[
        "body"
    ] or f"{target_price_usd:.2f}" in latest_reminder["body"]
