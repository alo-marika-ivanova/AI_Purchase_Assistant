from __future__ import annotations

import pytest

from app.db.repository import PurchasingRepository
from app.negotiation.actions import NegotiationAction, NegotiationActionType
from app.negotiation.comparison import prepare_case_for_negotiation
from app.negotiation.negotiation_rules import plan_initial_target_price_actions
from app.services import simple_chat_service
from app.services.case_service import create_case, create_case_from_detected_items
from app.services.offer_service import add_offer


repo = PurchasingRepository()


def _build_two_item_case_with_distinct_prices(
    supplier_ids: dict[str, int]
) -> tuple[int, int, int, int]:
    """Reproduces the reported scenario: one supplier (A) quotes two items
    at genuinely different prices, and a second supplier (B) is needed only
    so the case-wide minimum_valid_offers gate is satisfied.

    Returns (case_id, supplier_a, garnet_item_id, peridote_item_id).
    """
    supplier_a = supplier_ids["email"]
    supplier_b = supplier_ids["whatsapp"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet Pink",
                "quantity": 12.0,
                "supplier_ids": [supplier_a, supplier_b],
            },
            {
                "item_material": "Peridote (PER)",
                "quantity": 140.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    items = repo.list_case_items(case_id)
    garnet_id = next(i["id"] for i in items if i["item_material"] == "Garnet Pink")
    peridote_id = next(
        i["id"] for i in items if i["item_material"] == "Peridote (PER)"
    )

    add_offer(
        case_id=case_id,
        case_item_id=garnet_id,
        supplier_id=supplier_a,
        unit_price_usd=18.0,
    )
    add_offer(
        case_id=case_id,
        case_item_id=peridote_id,
        supplier_id=supplier_a,
        unit_price_usd=33.0,
    )
    add_offer(
        case_id=case_id,
        case_item_id=garnet_id,
        supplier_id=supplier_b,
        unit_price_usd=20.0,
    )

    return case_id, supplier_a, garnet_id, peridote_id


def test_per_item_negotiation_context_is_persisted_with_distinct_targets(
    supplier_ids: dict[str, int]
) -> None:
    case_id, _supplier_a, garnet_id, peridote_id = (
        _build_two_item_case_with_distinct_prices(supplier_ids)
    )

    prepare_case_for_negotiation(case_id)

    garnet_context = repo.get_case_item_negotiation_context(garnet_id)
    peridote_context = repo.get_case_item_negotiation_context(peridote_id)

    assert garnet_context is not None
    assert peridote_context is not None
    # Garnet's own best offer is 18.0 (supplier A undercut by B at 20.0);
    # 10% below that is 16.2 - independent of Peridote's own number.
    assert garnet_context["target_price_usd"] == pytest.approx(16.2)
    assert peridote_context["target_price_usd"] == pytest.approx(29.7)
    assert garnet_context["target_price_usd"] != peridote_context["target_price_usd"]


def test_plan_initial_target_price_actions_builds_distinct_per_item_targets(
    supplier_ids: dict[str, int]
) -> None:
    case_id, supplier_a, garnet_id, peridote_id = (
        _build_two_item_case_with_distinct_prices(supplier_ids)
    )

    prepare_case_for_negotiation(case_id)

    actions = plan_initial_target_price_actions(case_id)
    action_for_a = next(a for a in actions if a.supplier_id == supplier_a)

    assert action_for_a.item_targets is not None
    targets_by_item = {
        entry["case_item_id"]: entry["target_price_usd"]
        for entry in action_for_a.item_targets
    }
    assert targets_by_item[garnet_id] == pytest.approx(16.2)
    assert targets_by_item[peridote_id] == pytest.approx(29.7)


def test_negotiation_ask_message_states_each_items_own_target(
    supplier_ids: dict[str, int]
) -> None:
    """The actual bug report: the outbound price-reduction-request message
    must state each item's own target, not one shared number for both."""
    case_id, supplier_a, _garnet_id, _peridote_id = (
        _build_two_item_case_with_distinct_prices(supplier_ids)
    )

    prepare_case_for_negotiation(case_id)

    actions = plan_initial_target_price_actions(case_id)
    action_for_a = next(a for a in actions if a.supplier_id == supplier_a)

    result = simple_chat_service.execute_negotiation_rule_action(action_for_a)

    assert result["state_updated"] is True
    message = result["message"]
    assert "16.2" in message
    assert "29.7" in message


def test_legacy_single_item_case_negotiation_ask_is_unaffected(
    supplier_ids: dict[str, int]
) -> None:
    """A manually created single-item case (no case_items) must keep using
    the existing case-wide target price, with no item_targets involved."""
    supplier_id = supplier_ids["email"]
    other_supplier_id = supplier_ids["whatsapp"]

    # A second supplier is linked from case creation, required to satisfy
    # minimum_valid_offers.
    case_id = create_case(
        item_material="Tanzanite (TAN)",
        quantity=40.0,
        notes="",
        supplier_ids=[supplier_id, other_supplier_id],
    )

    add_offer(
        case_id=case_id,
        supplier_id=supplier_id,
        unit_price_usd=100.0,
    )

    add_offer(
        case_id=case_id,
        supplier_id=other_supplier_id,
        unit_price_usd=110.0,
    )

    prepare_case_for_negotiation(case_id)

    actions = plan_initial_target_price_actions(case_id)
    action_for_supplier = next(
        a for a in actions if a.supplier_id == supplier_id
    )

    assert action_for_supplier.item_targets is None
    assert action_for_supplier.target_price_usd == pytest.approx(90.0)

    result = simple_chat_service.execute_negotiation_rule_action(
        action_for_supplier
    )
    assert result["state_updated"] is True
    assert "90" in result["message"]
