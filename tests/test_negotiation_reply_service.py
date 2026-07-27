from __future__ import annotations

import pytest

from app.db.repository import PurchasingRepository
from app.negotiation.states import CaseState, SupplierState
from app.services import negotiation_reply_service
from app.services.case_service import create_case
from app.services.offer_service import add_offer


repo = PurchasingRepository()


# ---------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------


def _create_negotiating_case_with_round_1_sent(
    supplier_id: int,
    initial_price: float,
    target_discount_percent: float = 10.0,
) -> tuple[int, float]:
    """
    Build a case already in NEGOTIATING state, with round 1 (the initial
    target-price request) already sent to `supplier_id`, and a confirmed
    initial offer on record.

    Constructed directly through the repository rather than through the
    full RFQ workflow so these tests exercise only
    negotiation_reply_service's round-continuation logic, not RFQ
    classification or comparison. Mirrors exactly what
    execute_negotiation_rule_action does when it sends round 1 in
    production.

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

    repo.add_message(
        case_id=case_id,
        supplier_id=supplier_id,
        direction="outbound",
        channel="manual",
        body=(
            f"Could you please confirm whether you can reach USD "
            f"{target_price_usd:.2f} per unit?"
        ),
        status="sent_simulated",
        message_type="price_reduction_request",
        approval_required=False,
        approved_by_buyer=True,
    )

    repo.set_supplier_policy_state(
        case_id=case_id,
        supplier_id=supplier_id,
        state=SupplierState.DISCOUNT_REQUEST_SENT.value,
        best_offer_usd=initial_price,
        target_price_usd=target_price_usd,
    )

    repo.increment_negotiation_attempt(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    return case_id, target_price_usd


def _patch_analysis(monkeypatch: pytest.MonkeyPatch, analysis: dict) -> None:
    """Replace the LLM classifier call with a fixed, controlled result so
    these tests exercise only the deterministic round-continuation logic
    in negotiation_reply_service, not the LLM prompt/classification
    itself (covered separately in test_llm_classifier_normalization.py)."""
    monkeypatch.setattr(
        negotiation_reply_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: dict(analysis),
    )


def _soft_refusal_analysis() -> dict:
    return {
        "success": True,
        "message_category": "SOFT_REFUSAL",
        "recommended_action": "RECORD_PRICE_REFUSAL",
        "unit_price_usd": None,
        "requires_human_review": False,
        "safe_for_automation": True,
        "has_multiple_prices": False,
        "is_conditional": False,
        "confidence": "high",
        "reason": "Supplier gave an ordinary final-price refusal.",
    }


def _improved_offer_analysis(unit_price_usd: float) -> dict:
    return {
        "success": True,
        "message_category": "IMPROVED_PRICE_OFFER",
        "recommended_action": "SAVE_OFFER",
        "unit_price_usd": unit_price_usd,
        "requires_human_review": False,
        "safe_for_automation": True,
        "has_multiple_prices": False,
        "is_conditional": False,
        "confidence": "high",
        "reason": "Supplier improved the price.",
    }


def _target_acceptance_analysis(target_price_usd: float) -> dict:
    return {
        "success": True,
        "message_category": "TARGET_ACCEPTANCE",
        "recommended_action": "SAVE_OFFER",
        "unit_price_usd": target_price_usd,
        "requires_human_review": False,
        "safe_for_automation": True,
        "has_multiple_prices": False,
        "is_conditional": False,
        "confidence": "high",
        "reason": "Supplier accepted the target price.",
    }


def _hard_stop_analysis() -> dict:
    return {
        "success": True,
        "message_category": "HARD_STOP",
        "recommended_action": "STOP_NEGOTIATION_HARD",
        "unit_price_usd": None,
        "requires_human_review": False,
        "safe_for_automation": True,
        "has_multiple_prices": False,
        "is_conditional": False,
        "confidence": "high",
        "reason": "Supplier explicitly asked to stop negotiating.",
    }


def _risky_topic_analysis() -> dict:
    return {
        "success": True,
        "message_category": "DEPOSIT_OR_PREPAYMENT",
        "recommended_action": "PAUSE_FOR_REVIEW",
        "unit_price_usd": None,
        "requires_human_review": True,
        "safe_for_automation": False,
        "contains_risky_topic": True,
        "risk_category": "DEPOSIT_OR_PREPAYMENT",
        "has_multiple_prices": False,
        "is_conditional": False,
        "confidence": "high",
        "reason": "Supplier requested a deposit before continuing.",
    }


def _round_count(case_id: int, supplier_id: int) -> int:
    return repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="price_reduction_request",
    )


# ---------------------------------------------------------------------
# 1. Continuation after a soft final-price refusal
# ---------------------------------------------------------------------


def test_soft_refusal_continues_negotiation_while_rounds_remain(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _patch_analysis(monkeypatch, _soft_refusal_analysis())

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="This is our final price, we have no more margin.",
    )

    assert result["supplier_state"] == SupplierState.DISCOUNT_REQUEST_SENT.value
    assert _round_count(case_id, supplier_id) == 2

    state = repo.get_supplier_state(case_id, supplier_id)
    assert state["negotiation_attempts"] == 2
    assert bool(state["awaiting_supplier_reply"]) is True
    assert state["refusal_strength"] == "SOFT_REFUSAL"
    assert bool(state["hard_stop"]) is False


# ---------------------------------------------------------------------
# 2. Repeated refusals while rounds remain
# ---------------------------------------------------------------------


def test_repeated_refusals_continue_through_all_remaining_rounds(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _patch_analysis(monkeypatch, _soft_refusal_analysis())

    # Round 1 was already sent. Each refusal reply should send exactly one
    # more round, up to the 4-round maximum.
    for expected_round_after_reply in (2, 3, 4):
        result = negotiation_reply_service.record_negotiation_supplier_message(
            case_id=case_id,
            supplier_id=supplier_id,
            channel="email",
            body="Still no margin, this remains our final price.",
        )
        assert (
            result["supplier_state"]
            == SupplierState.DISCOUNT_REQUEST_SENT.value
        )
        assert _round_count(case_id, supplier_id) == expected_round_after_reply


# ---------------------------------------------------------------------
# 3. Improved offer above target
# ---------------------------------------------------------------------


def test_improved_offer_still_above_target_continues_negotiation(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )
    # target_price_usd == 27.0 for a 10% discount off 30.

    improved_price = 28.5
    _patch_analysis(monkeypatch, _improved_offer_analysis(improved_price))

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="We could go to 28.50 usd, but that's the lowest we can get.",
    )

    assert result["saved_offer_id"] is not None
    assert result["supplier_state"] == SupplierState.DISCOUNT_REQUEST_SENT.value
    assert _round_count(case_id, supplier_id) == 2

    best_offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    assert best_offer["unit_price_usd"] == pytest.approx(improved_price)


# ---------------------------------------------------------------------
# 4. Target acceptance
# ---------------------------------------------------------------------


def test_target_acceptance_stops_negotiation_immediately(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _patch_analysis(
        monkeypatch, _target_acceptance_analysis(target_price_usd)
    )

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="Yes, we can do that.",
    )

    assert result["supplier_state"] == SupplierState.FINAL_OFFER_RECEIVED.value
    # No second round sent -- negotiation stopped immediately.
    assert _round_count(case_id, supplier_id) == 1

    best_offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    assert best_offer["unit_price_usd"] == pytest.approx(target_price_usd)


# ---------------------------------------------------------------------
# 5. Hard stop
# ---------------------------------------------------------------------


def test_hard_stop_ends_negotiation_without_human_review(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _patch_analysis(monkeypatch, _hard_stop_analysis())

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="Please stop asking us to lower the price.",
    )

    assert result["supplier_state"] == SupplierState.FINAL_OFFER_RECEIVED.value
    assert result["review_item_id"] is None
    assert _round_count(case_id, supplier_id) == 1

    state = repo.get_supplier_state(case_id, supplier_id)
    assert bool(state["hard_stop"]) is True
    assert state["refusal_strength"] == "HARD_STOP"

    open_reviews = repo.list_open_human_review_items_for_case(case_id)
    assert not any(
        int(item.get("supplier_id") or -1) == supplier_id
        for item in open_reviews
    )


# ---------------------------------------------------------------------
# 6. Finalization after the fourth round
# ---------------------------------------------------------------------


def test_finalizes_and_retains_best_offer_after_fourth_round(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _patch_analysis(monkeypatch, _soft_refusal_analysis())

    # Rounds 2, 3, 4 get sent in response to refusals 1, 2, 3.
    for _ in range(3):
        negotiation_reply_service.record_negotiation_supplier_message(
            case_id=case_id,
            supplier_id=supplier_id,
            channel="email",
            body="Still our final price.",
        )

    assert _round_count(case_id, supplier_id) == 4

    # The reply to round 4 must finalize rather than send a 5th round.
    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="This really is our absolute final price.",
    )

    assert result["supplier_state"] == SupplierState.FINAL_OFFER_RECEIVED.value
    assert _round_count(case_id, supplier_id) == 4

    best_offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    assert best_offer["unit_price_usd"] == pytest.approx(30.0)


# ---------------------------------------------------------------------
# 7. No new request while awaiting a response
# ---------------------------------------------------------------------


def test_no_new_round_sent_while_already_awaiting_a_reply(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    # Simulate "round 2 was already sent and we are awaiting its reply"
    # at the persisted-state level, without actually recording a round-2
    # outbound message. The continuation guard must key off this state
    # and refuse to send another round.
    repo.record_negotiation_round_sent(
        case_id=case_id,
        supplier_id=supplier_id,
        strategy="ACKNOWLEDGE_AND_RECHECK",
        requested_price_usd=target_price_usd,
    )

    _patch_analysis(monkeypatch, _soft_refusal_analysis())

    negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="Still our final price.",
    )

    # No new price_reduction_request message was created -- the guard
    # detected we were already awaiting a reply for a round already sent.
    assert _round_count(case_id, supplier_id) == 1


# ---------------------------------------------------------------------
# 8. Duplicate-message prevention
# ---------------------------------------------------------------------


def test_duplicate_round_send_is_prevented_by_action_lock(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    # Pre-acquire the lock for round 2, simulating a concurrent/duplicate
    # attempt to send the same round.
    lock_acquired = repo.acquire_action_lock(
        case_id=case_id,
        supplier_id=supplier_id,
        action_key=f"SEND_NEGOTIATION_ROUND:{supplier_id}:2",
        action_type="SEND_NEGOTIATION_ROUND",
    )
    assert lock_acquired is True

    _patch_analysis(monkeypatch, _soft_refusal_analysis())

    negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="Still our final price.",
    )

    # The lock prevented a second round-2 send.
    assert _round_count(case_id, supplier_id) == 1


# ---------------------------------------------------------------------
# 9. Preservation of the best historical offer
# ---------------------------------------------------------------------


def test_best_historical_offer_is_always_preserved(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _patch_analysis(monkeypatch, _improved_offer_analysis(28.5))
    negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="We can go to 28.50 usd.",
    )
    best_offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    assert best_offer["unit_price_usd"] == pytest.approx(28.5)

    # A soft refusal with no new price must not lose the 28.50 offer.
    _patch_analysis(monkeypatch, _soft_refusal_analysis())
    negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="28.50 is our final price, no more margin.",
    )
    best_offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    assert best_offer["unit_price_usd"] == pytest.approx(28.5)

    # A further improvement must lower the retained best offer.
    _patch_analysis(monkeypatch, _improved_offer_analysis(28.0))
    negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="We can go to 28.00 usd.",
    )
    best_offer = repo.get_best_offer_for_case_supplier(case_id, supplier_id)
    assert best_offer["unit_price_usd"] == pytest.approx(28.0)

    state = repo.get_supplier_state(case_id, supplier_id)
    assert float(state["best_offer_usd"]) == pytest.approx(28.0)


# ---------------------------------------------------------------------
# 10. Restart persistence
# ---------------------------------------------------------------------


def test_negotiation_round_state_survives_a_restart(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, target_price_usd = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _patch_analysis(monkeypatch, _soft_refusal_analysis())
    negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="This is our final price, no more margin.",
    )

    # Constructing a new repository object simulates a fresh process
    # reading the state persisted by a previous worker/app process.
    restarted_repo = PurchasingRepository()
    state = restarted_repo.get_supplier_state(case_id, supplier_id)

    assert state is not None
    assert state["negotiation_attempts"] == 2
    assert bool(state["awaiting_supplier_reply"]) is True
    assert state["last_negotiation_strategy"] == "ACKNOWLEDGE_AND_RECHECK"
    assert float(state["last_requested_price_usd"]) == pytest.approx(
        target_price_usd
    )
    assert state["refusal_strength"] == "SOFT_REFUSAL"
    assert bool(state["hard_stop"]) is False


# ---------------------------------------------------------------------
# 11. Risky-topic escalation
# ---------------------------------------------------------------------


def test_risky_topic_still_escalates_to_human_review(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, _ = _create_negotiating_case_with_round_1_sent(
        supplier_id, initial_price=30.0
    )

    _patch_analysis(monkeypatch, _risky_topic_analysis())

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="We can do 27 USD, but only with a 50 percent deposit upfront.",
    )

    assert result["supplier_state"] == SupplierState.PAUSED_REVIEW.value
    assert result["review_item_id"] is not None
    # No negotiation round is sent while paused for human review.
    assert _round_count(case_id, supplier_id) == 1
