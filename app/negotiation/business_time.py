from __future__ import annotations

from datetime import datetime, timedelta


def add_business_days(start: datetime, business_days: int) -> datetime:
    """Advance `start` by `business_days` weekdays (Mon-Fri).

    Saturdays and Sundays are skipped entirely. No holiday calendar is
    applied -- deliberately, per project scope. `business_days <= 0`
    returns `start` unchanged.
    """
    if business_days <= 0:
        return start

    current = start
    added = 0

    while added < business_days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1

    return current


def is_business_day(value: datetime) -> bool:
    """Return True for Monday-Friday."""
    return value.weekday() < 5


def compute_next_reminder_due_at(policy, from_dt: datetime) -> datetime:
    """
    Return when the next no-response reminder is due, counting from
    `from_dt` (the time the current round -- or the previous reminder --
    was sent).

    The same interval is reused both between reminders and for the final
    wait after the last reminder before finalizing, matching
    docs/rules.md's negotiation no-response timing. Deliberately kept in
    this dependency-free module (rather than alongside the reminder
    planner in app/negotiation/negotiation_reminder_rules.py) so it can be
    imported from both app/services/simple_chat_service.py and
    app/services/negotiation_reply_service.py without creating an import
    cycle through negotiation_reminder_rules.py.
    """
    if policy.is_testing:
        return from_dt + timedelta(
            seconds=policy.negotiation_reminder_interval_test_seconds
        )

    return add_business_days(
        from_dt,
        policy.negotiation_reminder_interval_business_days,
    )
