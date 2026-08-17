from __future__ import annotations

import json

import app.llm.communication_writer as communication_writer
from app.llm.communication_writer import write_buyer_message


SUPPLIER = {"name": "Test Supplier", "supplier_code": "SUP_TEST"}


class _FakeProvider:
    name = "fake"
    model = "fake"

    def __init__(self):
        self.last_prompt: str | None = None

    def generate(self, prompt: str, *, timeout_seconds: int) -> str:
        self.last_prompt = prompt
        return json.dumps(
            {"message": "Fake supplier message.\n\nBest regards", "reason": "test"}
        )


def test_single_item_rfq_is_unchanged_without_items_key() -> None:
    """Legacy single-item cases have no "items" key in case_data - this must
    keep producing exactly the original single-item wording."""
    case_data = {
        "case_number": "TAN-2026-07-30-01",
        "item_material": "Tanzanite (TAN)",
        "quantity": 40.0,
        "notes": None,
    }

    result = write_buyer_message(
        intent="initial_rfq",
        case_data=case_data,
        supplier=SUPPLIER,
        use_llm=False,
    )

    assert result["success"] is True
    assert "Tanzanite (TAN)" in result["message"]
    assert "40.0" in result["message"]
    # Should not accidentally render a bulleted multi-item list.
    assert "- Tanzanite" not in result["message"]


def test_multi_item_subcase_rfq_lists_every_item_with_its_quantity() -> None:
    case_data = {
        "case_number": "ORD-2026-07-30-01",
        "item_material": "Order (2 items): Tanzanite (TAN), Ruby (RBN)",
        "quantity": 50.0,
        "notes": None,
        "items": [
            {"item_material": "Tanzanite (TAN)", "quantity": 40.0},
            {"item_material": "Ruby (RBN)", "quantity": 10.0},
        ],
    }

    result = write_buyer_message(
        intent="initial_rfq",
        case_data=case_data,
        supplier=SUPPLIER,
        use_llm=False,
    )

    assert result["success"] is True
    message = result["message"]
    assert "Tanzanite (TAN): quantity 40.0" in message
    assert "Ruby (RBN): quantity 10.0" in message
    # A single combined RFQ, not one message per item.
    assert message.count("Best regards") == 1


def test_multi_item_subcase_rfq_asks_for_a_price_for_each_item() -> None:
    case_data = {
        "case_number": "ORD-2026-07-30-01",
        "item_material": "Order (2 items): Tanzanite (TAN), Ruby (RBN)",
        "quantity": 50.0,
        "notes": None,
        "items": [
            {"item_material": "Tanzanite (TAN)", "quantity": 40.0},
            {"item_material": "Ruby (RBN)", "quantity": 10.0},
        ],
    }

    result = write_buyer_message(
        intent="initial_rfq",
        case_data=case_data,
        supplier=SUPPLIER,
        use_llm=False,
    )

    assert "unit price" in result["message"].lower()


def test_multi_item_provisional_acknowledgement_lists_every_item_price() -> None:
    """Reproduces a real bug: a supplier attachment quoting 4 different
    subcase prices only surfaced ONE of them in the acknowledgement message
    (the case+supplier-level lookup only kept the most recently saved
    offer). Passing item_provisional_prices must mention every item's own
    price, not just one shared number."""
    case_data = {
        "case_number": "ORD-2026-08-06-01",
        "item_material": "RFQ order (2 items): Garnet Pink, Peridote (PER).",
        "quantity": 152.0,
        "notes": None,
        "items": [
            {"item_material": "Garnet Pink", "quantity": 12.0},
            {"item_material": "Peridote (PER)", "quantity": 100.0},
        ],
    }

    result = write_buyer_message(
        intent="acknowledge_tentative_price",
        case_data=case_data,
        supplier=SUPPLIER,
        item_provisional_prices=[
            {"item_material": "Garnet Pink", "unit_price_usd": 44.0},
            {"item_material": "Peridote (PER)", "unit_price_usd": 20.0},
        ],
        use_llm=False,
    )

    assert result["success"] is True
    message = result["message"]
    assert "44.0" in message or "44" in message
    assert "20.0" in message or "20" in message
    assert "Garnet Pink" in message
    assert "Peridote (PER)" in message
    # A single combined acknowledgement, not one message per item.
    assert message.count("Best regards") == 1


def test_llm_multi_item_provisional_acknowledgement_is_rejected_if_incomplete(
    monkeypatch,
) -> None:
    """If the LLM's generated wording drops one item's provisional price,
    the safeguard must reject it and fall back to the deterministic
    template rather than silently sending an incomplete acknowledgement."""
    fake_provider = _FakeProvider()
    monkeypatch.setattr(
        communication_writer, "get_llm_provider", lambda: fake_provider
    )

    case_data = {
        "case_number": "ORD-2026-08-06-01",
        "item_material": "RFQ order (2 items): Garnet Pink, Peridote (PER).",
        "quantity": 152.0,
        "notes": None,
        "items": [
            {"item_material": "Garnet Pink", "quantity": 12.0},
            {"item_material": "Peridote (PER)", "quantity": 100.0},
        ],
    }

    result = write_buyer_message(
        intent="acknowledge_tentative_price",
        case_data=case_data,
        supplier=SUPPLIER,
        item_provisional_prices=[
            {"item_material": "Garnet Pink", "unit_price_usd": 44.0},
            {"item_material": "Peridote (PER)", "unit_price_usd": 20.0},
        ],
        use_llm=True,
    )

    # _FakeProvider's canned message only mentions neither price, so the
    # per-item safeguard should reject it and use the fallback template.
    assert result["method"] == "fallback_after_llm_provider_failed"
    assert "44.0" in result["message"] or "44" in result["message"]
    assert "20.0" in result["message"] or "20" in result["message"]


def test_llm_prompt_does_not_leak_other_items_when_scoped(monkeypatch) -> None:
    """Regression test for a real bug: the case-level item_material/quantity
    describe the WHOLE multi-supplier order. When a supplier is only linked
    to a subset of items, the prompt must not also show the full-order
    summary alongside the scoped list - the LLM was observed echoing the
    wrong (unrelated) items when both were present."""
    fake_provider = _FakeProvider()
    monkeypatch.setattr(
        communication_writer, "get_llm_provider", lambda: fake_provider
    )

    case_data = {
        "case_number": "RFQ-2026-07-31-01",
        "item_material": (
            "RFQ order (3 items): Tanzanite (TAN), Blue sapphire (SA), "
            "Ruby (RBN)."
        ),
        "quantity": 66.0,
        "notes": None,
        "items": [
            {"item_material": "Tanzanite (TAN)", "quantity": 40.0},
        ],
    }

    write_buyer_message(
        intent="initial_rfq",
        case_data=case_data,
        supplier=SUPPLIER,
        use_llm=True,
    )

    assert fake_provider.last_prompt is not None
    prompt = fake_provider.last_prompt

    assert "Tanzanite (TAN)" in prompt
    # The other two items only belong to the whole order's summary label,
    # not to what this supplier was linked to - must not appear at all.
    assert "Blue sapphire" not in prompt
    assert "Ruby (RBN)" not in prompt
    assert "RFQ order (3 items)" not in prompt
    assert "66.0" not in prompt


MULTI_ITEM_TARGETS = [
    {
        "item_material": "Tanzanite (TAN)",
        "best_price_usd": 40.0,
        "target_price_usd": 36.0,
    },
    {
        "item_material": "Ruby (RBN)",
        "best_price_usd": 70.0,
        "target_price_usd": 63.0,
    },
]


def test_round_2_fallback_lists_every_pending_item_own_target() -> None:
    """Reproduces a real gap: rounds 2-4 (acknowledge_refusal_and_recheck_target,
    express_interest_request_improvement, ask_absolute_best_price) only had
    single-item fallback wording, even for a multi-item order - the
    per-item targets were computed correctly but never reached the
    message. Each of the three intents must list every still-pending
    item's own target once item_targets is given."""
    case_data = {
        "case_number": "ORD-2026-08-08-01",
        "item_material": "RFQ order (2 items): Tanzanite (TAN), Ruby (RBN)",
        "quantity": 50.0,
        "notes": None,
    }

    for intent in (
        "acknowledge_refusal_and_recheck_target",
        "express_interest_request_improvement",
        "ask_absolute_best_price",
    ):
        result = write_buyer_message(
            intent=intent,
            case_data=case_data,
            supplier=SUPPLIER,
            item_targets=MULTI_ITEM_TARGETS,
            use_llm=False,
        )

        assert result["success"] is True
        message = result["message"]
        assert "Tanzanite (TAN)" in message
        assert "Ruby (RBN)" in message


def test_round_2_fallback_without_item_targets_is_unchanged() -> None:
    """Legacy single-item negotiation rounds (no item_targets) must keep
    using the original single-value wording."""
    case_data = {
        "case_number": "TAN-2026-08-08-01",
        "item_material": "Tanzanite (TAN)",
        "quantity": 40.0,
        "notes": None,
    }

    result = write_buyer_message(
        intent="acknowledge_refusal_and_recheck_target",
        case_data=case_data,
        supplier=SUPPLIER,
        target_price_usd=36.0,
        use_llm=False,
    )

    assert result["success"] is True
    assert "Tanzanite (TAN)" in result["message"]
    assert "36.0" in result["message"] or "36" in result["message"]
