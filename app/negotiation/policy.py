from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = PROJECT_ROOT / "config" / "negotiation_policy.json"


@dataclass(frozen=True)
class NegotiationPolicy:
    mode: str = "testing"

    testing_rfq_reminder_wait_minutes: int = 2
    testing_rfq_deadline_minutes: int = 4
    testing_max_rfq_reminders: int = 1

    production_rfq_reminder_wait_hours: int = 24
    production_rfq_deadline_hours: int = 120
    production_max_rfq_reminders: int = 4

    minimum_valid_offers: int = 2
    allow_limited_competition_with_one_offer: bool = True

    target_discount_percent: float = 10
    acceptance_tolerance_above_target_percent: float = 5

    max_discount_requests_per_supplier: int = 1
    max_negotiation_reminders_per_supplier: int = 3

    pause_on_unknown_or_risky_topic: bool = True

    @property
    def rfq_reminder_wait_minutes(self) -> int:
        if self.mode == "production":
            return self.production_rfq_reminder_wait_hours * 60

        return self.testing_rfq_reminder_wait_minutes

    @property
    def rfq_deadline_minutes(self) -> int:
        if self.mode == "production":
            return self.production_rfq_deadline_hours * 60

        return self.testing_rfq_deadline_minutes

    @property
    def max_rfq_reminders(self) -> int:
        if self.mode == "production":
            return self.production_max_rfq_reminders

        return self.testing_max_rfq_reminders


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