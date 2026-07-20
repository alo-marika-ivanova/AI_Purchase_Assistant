from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = PROJECT_ROOT / "config" / "negotiation_policy.json"


class BusinessTimeNotImplementedError(RuntimeError):
    """Raised when legacy minute-based code requests production timing.

    Production waits are intentionally stored as business days. Converting
    them to 24-hour periods would produce incorrect behavior over weekends.
    The RFQ and negotiation planners must use the business-time helper before
    production mode is enabled.
    """


@dataclass(frozen=True)
class NegotiationPolicy:
    mode: str = "testing"

    # Fast deterministic timings used only during development and tests.
    testing_rfq_reminder_wait_minutes: int = 2
    testing_rfq_deadline_minutes: int = 4
    testing_negotiation_reply_wait_minutes: int = 2
    testing_negotiation_finalization_minutes: int = 4

    # Production timings use business days, not elapsed 24-hour periods.
    production_rfq_reminder_business_days: int = 1
    production_rfq_deadline_business_days: int = 2
    production_negotiation_reply_business_days: int = 1
    production_negotiation_finalization_business_days: int = 2

    minimum_valid_offers: int = 2
    allow_limited_competition_with_one_offer: bool = True

    target_discount_percent: float = 10
    acceptance_tolerance_above_target_percent: float = 5

    max_rfq_reminders: int = 1
    max_discount_requests_per_supplier: int = 2
    max_negotiation_reminders_per_supplier: int = 1
    max_outbound_without_supplier_reply: int = 2

    pause_on_unknown_or_risky_topic: bool = True

    def __post_init__(self) -> None:
        if self.mode not in {"testing", "production"}:
            raise ValueError(
                "Negotiation policy mode must be 'testing' or 'production'."
            )

        positive_integer_fields = (
            "testing_rfq_reminder_wait_minutes",
            "testing_rfq_deadline_minutes",
            "testing_negotiation_reply_wait_minutes",
            "testing_negotiation_finalization_minutes",
            "production_rfq_reminder_business_days",
            "production_rfq_deadline_business_days",
            "production_negotiation_reply_business_days",
            "production_negotiation_finalization_business_days",
            "minimum_valid_offers",
            "max_rfq_reminders",
            "max_discount_requests_per_supplier",
            "max_negotiation_reminders_per_supplier",
            "max_outbound_without_supplier_reply",
        )

        for field_name in positive_integer_fields:
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(
                    f"Negotiation policy field '{field_name}' must be a "
                    "positive integer."
                )

        percentage_fields = (
            "target_discount_percent",
            "acceptance_tolerance_above_target_percent",
        )

        for field_name in percentage_fields:
            value = float(getattr(self, field_name))
            if value <= 0 or value >= 100:
                raise ValueError(
                    f"Negotiation policy field '{field_name}' must be "
                    "greater than 0 and lower than 100."
                )

        if (
            self.testing_rfq_deadline_minutes
            < self.testing_rfq_reminder_wait_minutes
        ):
            raise ValueError(
                "Testing RFQ deadline cannot be earlier than the RFQ "
                "reminder wait."
            )

        if (
            self.testing_negotiation_finalization_minutes
            < self.testing_negotiation_reply_wait_minutes
        ):
            raise ValueError(
                "Testing negotiation finalization cannot be earlier than "
                "the negotiation reply wait."
            )

        if (
            self.production_rfq_deadline_business_days
            < self.production_rfq_reminder_business_days
        ):
            raise ValueError(
                "Production RFQ deadline cannot be earlier than the RFQ "
                "reminder wait."
            )

        if (
            self.production_negotiation_finalization_business_days
            < self.production_negotiation_reply_business_days
        ):
            raise ValueError(
                "Production negotiation finalization cannot be earlier than "
                "the negotiation reply wait."
            )

    @property
    def is_testing(self) -> bool:
        return self.mode == "testing"

    @property
    def is_production(self) -> bool:
        return self.mode == "production"

    @property
    def rfq_reminder_wait_minutes(self) -> int:
        """Return the testing RFQ reminder wait for legacy planners.

        Current RFQ code compares elapsed minutes. That is correct in testing
        mode only. Production mode must use business-day calculations and is
        deliberately rejected rather than silently treating one business day
        as 24 elapsed hours.
        """
        self._require_testing_minute_mode("RFQ reminder")
        return self.testing_rfq_reminder_wait_minutes

    @property
    def rfq_deadline_minutes(self) -> int:
        """Return the testing RFQ deadline for legacy planners."""
        self._require_testing_minute_mode("RFQ deadline")
        return self.testing_rfq_deadline_minutes

    @property
    def negotiation_reply_wait_minutes(self) -> int:
        """Return the testing wait before a negotiation reminder."""
        self._require_testing_minute_mode("negotiation reply wait")
        return self.testing_negotiation_reply_wait_minutes

    @property
    def negotiation_finalization_minutes(self) -> int:
        """Return the testing deadline for finalizing an unanswered offer."""
        self._require_testing_minute_mode("negotiation finalization")
        return self.testing_negotiation_finalization_minutes

    def _require_testing_minute_mode(self, timing_name: str) -> None:
        if self.is_production:
            raise BusinessTimeNotImplementedError(
                f"{timing_name} uses business days in production. Add the "
                "business-time helper to the workflow planner before setting "
                "policy mode to 'production'."
            )


def load_negotiation_policy() -> NegotiationPolicy:
    if not POLICY_PATH.exists():
        return NegotiationPolicy()

    data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    allowed = set(NegotiationPolicy.__dataclass_fields__.keys())
    clean = {
        key: value
        for key, value in data.items()
        if key in allowed
    }

    return NegotiationPolicy(**clean)
