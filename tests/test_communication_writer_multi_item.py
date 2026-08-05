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
