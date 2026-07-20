from __future__ import annotations

import pytest

from app.negotiation.policy import (
    BusinessTimeNotImplementedError,
    NegotiationPolicy,
    load_negotiation_policy,
)


def test_loaded_policy_matches_testing_timing_rules() -> None:
    policy = load_negotiation_policy()

    assert policy.mode == "testing"
    assert policy.rfq_reminder_wait_minutes == 2
    assert policy.rfq_deadline_minutes == 4
    assert policy.negotiation_reply_wait_minutes == 2
    assert policy.negotiation_finalization_minutes == 4


def test_loaded_policy_matches_production_business_day_rules() -> None:
    policy = load_negotiation_policy()

    assert policy.production_rfq_reminder_business_days == 1
    assert policy.production_rfq_deadline_business_days == 2
    assert policy.production_negotiation_reply_business_days == 1
    assert policy.production_negotiation_finalization_business_days == 2


def test_loaded_policy_matches_message_limits() -> None:
    policy = load_negotiation_policy()

    assert policy.max_rfq_reminders == 1
    assert policy.max_discount_requests_per_supplier == 2
    assert policy.max_negotiation_reminders_per_supplier == 1
    assert policy.max_outbound_without_supplier_reply == 2


def test_loaded_policy_matches_price_strategy() -> None:
    policy = load_negotiation_policy()

    assert policy.target_discount_percent == pytest.approx(10)
    assert (
        policy.acceptance_tolerance_above_target_percent
        == pytest.approx(5)
    )


def test_production_business_days_are_not_converted_to_elapsed_minutes() -> None:
    policy = NegotiationPolicy(mode="production")

    with pytest.raises(BusinessTimeNotImplementedError):
        _ = policy.rfq_reminder_wait_minutes

    with pytest.raises(BusinessTimeNotImplementedError):
        _ = policy.rfq_deadline_minutes

    with pytest.raises(BusinessTimeNotImplementedError):
        _ = policy.negotiation_reply_wait_minutes

    with pytest.raises(BusinessTimeNotImplementedError):
        _ = policy.negotiation_finalization_minutes


def test_invalid_policy_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="testing.*production"):
        NegotiationPolicy(mode="demo")


def test_policy_rejects_deadline_before_reminder() -> None:
    with pytest.raises(ValueError, match="Testing RFQ deadline"):
        NegotiationPolicy(
            testing_rfq_reminder_wait_minutes=5,
            testing_rfq_deadline_minutes=4,
        )
