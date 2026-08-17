from __future__ import annotations

import pytest

from app.db.repository import PurchasingRepository
from app.negotiation.states import CaseState, SupplierState
from app.services import negotiation_reply_service
from app.services.case_service import create_case_from_detected_items
from app.services.offer_service import add_offer


repo = PurchasingRepository()


def _round_count(case_id: int, supplier_id: int) -> int:
    return repo.count_supplier_outbound_message_type(
        case_id=case_id,
        supplier_id=supplier_id,
        message_type="price_reduction_request",
    )


def _patch_analysis(monkeypatch: pytest.MonkeyPatch, analysis: dict) -> None:
    monkeypatch.setattr(
        negotiation_reply_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: dict(analysis),
    )


def _create_multi_item_negotiating_case_with_round_1_sent(
    supplier_id: int,
    item_prices: dict[str, float],
    target_discount_percent: float = 10.0,
) -> tuple[int, dict[str, dict]]:
    """Build a multi-item case already in NEGOTIATING state, with round 1
    already sent to `supplier_id`, and a confirmed initial offer per item.

    Mirrors _create_negotiating_case_with_round_1_sent from
    test_negotiation_reply_service.py, but for an order with several
    case_items - each gets its own case_item_negotiation_context, exactly
    as prepare_case_for_negotiation would build per item.

    Returns (case_id, {item_material: {"case_item_id", "target_price_usd"}}).
    """
    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": item_material,
                "quantity": 10.0,
                "supplier_ids": [supplier_id],
            }
            for item_material in item_prices
        ],
        notes="",
    )

    case_items = repo.list_case_items(case_id)
    case_items_by_material = {
        item["item_material"]: item for item in case_items
    }

    item_info: dict[str, dict] = {}
    case_wide_target_prices = []

    for item_material, initial_price in item_prices.items():
        case_item_id = int(case_items_by_material[item_material]["id"])

        saved_offer_id = add_offer(
            case_id=case_id,
            case_item_id=case_item_id,
            supplier_id=supplier_id,
            unit_price_usd=initial_price,
            extraction_method="manual",
            extraction_confidence="human_verified",
            notes="Initial RFQ offer for test setup.",
        )

        target_price_usd = round(
            initial_price * (1 - target_discount_percent / 100), 4
        )
        case_wide_target_prices.append(target_price_usd)

        repo.upsert_case_item_negotiation_context(
            case_item_id=case_item_id,
            case_id=case_id,
            initial_best_offer_usd=initial_price,
            target_price_usd=target_price_usd,
            best_supplier_id=supplier_id,
            best_offer_id=saved_offer_id,
            valid_offer_count=1,
            target_discount_percent=target_discount_percent,
            ranking_json="[]",
        )

        item_info[item_material] = {
            "case_item_id": case_item_id,
            "target_price_usd": target_price_usd,
        }

    # The case-wide context is only used as a fallback/existence check by
    # record_negotiation_supplier_message for a multi-item order - real
    # accept/continue decisions go through the per-item contexts above.
    repo.upsert_case_negotiation_context(
        case_id=case_id,
        initial_best_offer_usd=min(item_prices.values()),
        target_price_usd=min(case_wide_target_prices),
        best_supplier_id=supplier_id,
        best_offer_id=saved_offer_id,
        valid_offer_count=len(item_prices),
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
        best_offer_usd=min(item_prices.values()),
        target_price_usd=min(case_wide_target_prices),
    )

    repo.add_message(
        case_id=case_id,
        supplier_id=supplier_id,
        direction="outbound",
        channel="manual",
        body="Could you please confirm whether you can reach our target price?",
        status="sent_simulated",
        message_type="price_reduction_request",
        approval_required=False,
        approved_by_buyer=True,
    )

    repo.set_supplier_policy_state(
        case_id=case_id,
        supplier_id=supplier_id,
        state=SupplierState.DISCOUNT_REQUEST_SENT.value,
        best_offer_usd=min(item_prices.values()),
        target_price_usd=min(case_wide_target_prices),
    )

    repo.increment_negotiation_attempt(
        case_id=case_id,
        supplier_id=supplier_id,
    )

    return case_id, item_info


def _plain_acceptance_analysis() -> dict:
    return {
        "success": True,
        "message_category": "TARGET_ACCEPTANCE",
        "recommended_action": "SAVE_OFFER",
        "unit_price_usd": None,
        "supplier_accepts_target": True,
        "requires_human_review": False,
        "safe_for_automation": True,
        "has_multiple_prices": False,
        "is_conditional": False,
        "confidence": "high",
        "reason": "Supplier accepted the proposed prices for all items.",
        "item_offers": None,
    }


def _item_offers_analysis(item_offers: list[dict]) -> dict:
    return {
        "success": True,
        "message_category": "IMPROVED_PRICE_OFFER",
        "recommended_action": "SAVE_OFFER",
        "unit_price_usd": None,
        "requires_human_review": False,
        "safe_for_automation": True,
        "has_multiple_prices": True,
        "is_conditional": False,
        "confidence": "high",
        "reason": "Supplier updated some item prices.",
        "item_offers": item_offers,
    }


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


# ---------------------------------------------------------------------
# 1. Plain acceptance ("I agree with your proposed prices") accepts every
#    still-pending item at its own target.
# ---------------------------------------------------------------------


def test_plain_acceptance_accepts_every_pending_item_at_its_own_target(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, item_info = _create_multi_item_negotiating_case_with_round_1_sent(
        supplier_id,
        item_prices={"Garnet Pink": 40.0, "Peridote (PER)": 20.0},
    )

    _patch_analysis(monkeypatch, _plain_acceptance_analysis())

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="OK, as you are our regular customer, I agree with your proposed prices.",
    )

    assert result["supplier_state"] == SupplierState.FINAL_OFFER_RECEIVED.value
    # No second round -- both items are fully resolved.
    assert _round_count(case_id, supplier_id) == 1

    for item_material, info in item_info.items():
        offer = repo.get_best_offer_for_case_item_supplier(
            info["case_item_id"], supplier_id
        )
        assert offer is not None
        assert offer["unit_price_usd"] == pytest.approx(
            info["target_price_usd"]
        )


# ---------------------------------------------------------------------
# 2. Partial improvement: one item improves (still above target), the
#    other is untouched by this reply - both remain visible/pending, and
#    the next round is sent covering both.
# ---------------------------------------------------------------------


def test_partial_improvement_updates_only_the_mentioned_item(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, item_info = _create_multi_item_negotiating_case_with_round_1_sent(
        supplier_id,
        item_prices={"Garnet Pink": 40.0, "Peridote (PER)": 20.0},
    )
    # targets: Garnet 36.0, Peridote 18.0 (10% discount)

    _patch_analysis(
        monkeypatch,
        _item_offers_analysis(
            [
                {
                    "item_material": "Garnet Pink",
                    "unit_price_usd": 38.0,
                    "price_certainty": "CONFIRMED",
                },
            ]
        ),
    )

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="We can do 38 usd for the garnet, still checking on the peridot.",
    )

    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        item_info["Garnet Pink"]["case_item_id"], supplier_id
    )
    peridote_offer = repo.get_best_offer_for_case_item_supplier(
        item_info["Peridote (PER)"]["case_item_id"], supplier_id
    )

    assert garnet_offer["unit_price_usd"] == pytest.approx(38.0)
    # Peridote untouched by this reply - still at its original price.
    assert peridote_offer["unit_price_usd"] == pytest.approx(20.0)

    # Both items are still above their own target -> negotiation continues.
    assert result["supplier_state"] == SupplierState.DISCOUNT_REQUEST_SENT.value
    assert _round_count(case_id, supplier_id) == 2


# ---------------------------------------------------------------------
# 3. One item reaches target, the other stays pending - the supplier as a
#    whole is NOT finalized, and the next round only asks about the
#    pending item.
# ---------------------------------------------------------------------


def test_one_item_reaching_target_does_not_finalize_the_other(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, item_info = _create_multi_item_negotiating_case_with_round_1_sent(
        supplier_id,
        item_prices={"Garnet Pink": 40.0, "Peridote (PER)": 20.0},
    )
    # targets: Garnet 36.0, Peridote 18.0

    _patch_analysis(
        monkeypatch,
        _item_offers_analysis(
            [
                {
                    "item_material": "Garnet Pink",
                    "unit_price_usd": 36.0,
                    "price_certainty": "CONFIRMED",
                },
            ]
        ),
    )

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="We can meet your target of 36 usd for the garnet.",
    )

    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        item_info["Garnet Pink"]["case_item_id"], supplier_id
    )
    assert garnet_offer["unit_price_usd"] == pytest.approx(36.0)

    # Peridote still pending -> supplier as a whole keeps negotiating.
    assert result["supplier_state"] == SupplierState.DISCOUNT_REQUEST_SENT.value
    assert _round_count(case_id, supplier_id) == 2

    # A further plain acceptance must only touch the still-pending item
    # (Garnet already has its own final offer and must not be re-saved).
    _patch_analysis(monkeypatch, _plain_acceptance_analysis())
    result_2 = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="OK, agreed on the peridot price too.",
    )

    peridote_offer = repo.get_best_offer_for_case_item_supplier(
        item_info["Peridote (PER)"]["case_item_id"], supplier_id
    )
    assert peridote_offer["unit_price_usd"] == pytest.approx(
        item_info["Peridote (PER)"]["target_price_usd"]
    )
    assert result_2["supplier_state"] == SupplierState.FINAL_OFFER_RECEIVED.value
    # Still only 2 rounds ever sent -- both items are now resolved.
    assert _round_count(case_id, supplier_id) == 2


# ---------------------------------------------------------------------
# 4. A price increase on one item is ignored (kept at its existing best),
#    while a genuine improvement on another item in the same reply is
#    still saved.
# ---------------------------------------------------------------------


def test_price_increase_on_one_item_is_ignored_other_item_still_saved(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, item_info = _create_multi_item_negotiating_case_with_round_1_sent(
        supplier_id,
        item_prices={"Garnet Pink": 40.0, "Peridote (PER)": 20.0},
    )

    _patch_analysis(
        monkeypatch,
        _item_offers_analysis(
            [
                {
                    "item_material": "Garnet Pink",
                    "unit_price_usd": 45.0,  # increase - must be rejected
                    "price_certainty": "CONFIRMED",
                },
                {
                    "item_material": "Peridote (PER)",
                    "unit_price_usd": 19.0,  # genuine improvement
                    "price_certainty": "CONFIRMED",
                },
            ]
        ),
    )

    negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="Garnet is now 45 usd, peridot we can do 19 usd.",
    )

    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        item_info["Garnet Pink"]["case_item_id"], supplier_id
    )
    peridote_offer = repo.get_best_offer_for_case_item_supplier(
        item_info["Peridote (PER)"]["case_item_id"], supplier_id
    )

    # Garnet kept at its original 40.0, not bumped up to 45.0.
    assert garnet_offer["unit_price_usd"] == pytest.approx(40.0)
    assert peridote_offer["unit_price_usd"] == pytest.approx(19.0)


# ---------------------------------------------------------------------
# 5. A refusal, with no price change, still keeps only the pending items
#    in the next round's targets.
# ---------------------------------------------------------------------


def test_refusal_after_one_item_accepted_only_asks_about_the_other(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, item_info = _create_multi_item_negotiating_case_with_round_1_sent(
        supplier_id,
        item_prices={"Garnet Pink": 40.0, "Peridote (PER)": 20.0},
    )

    _patch_analysis(
        monkeypatch,
        _item_offers_analysis(
            [
                {
                    "item_material": "Garnet Pink",
                    "unit_price_usd": 36.0,  # meets target exactly
                    "price_certainty": "CONFIRMED",
                },
            ]
        ),
    )
    negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="We can meet 36 usd for the garnet.",
    )
    assert _round_count(case_id, supplier_id) == 2

    _patch_analysis(monkeypatch, _soft_refusal_analysis())
    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="20 usd is our final price for the peridot, no more margin.",
    )

    assert result["supplier_state"] == SupplierState.DISCOUNT_REQUEST_SENT.value
    assert _round_count(case_id, supplier_id) == 3

    # Garnet's already-finalized price must be untouched by the refusal.
    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        item_info["Garnet Pink"]["case_item_id"], supplier_id
    )
    assert garnet_offer["unit_price_usd"] == pytest.approx(36.0)


# ---------------------------------------------------------------------
# 6. A whole-message ASK_PRICE_CLARIFICATION verdict must not override a
#    fully usable, confirmed per-item extraction (real bug: the LLM's own
#    "reason" text showed it understood both prices perfectly, but its
#    whole-message recommended_action still said "needs clarification",
#    likely because the reply also asked "would you accept?").
# ---------------------------------------------------------------------


def test_ask_price_clarification_is_overridden_when_item_offers_are_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    supplier_id = supplier_ids["email"]
    case_id, item_info = _create_multi_item_negotiating_case_with_round_1_sent(
        supplier_id,
        item_prices={"Garnet Pink": 33.0, "Peridote (PER)": 22.0},
    )
    # targets: Garnet 29.70, Peridote 19.80 (10% discount)

    _patch_analysis(
        monkeypatch,
        {
            "success": True,
            "message_category": "UNCLEAR_PRICE",
            "recommended_action": "ASK_PRICE_CLARIFICATION",
            "unit_price_usd": None,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "safe_for_automation": True,
            "has_multiple_prices": True,
            "is_conditional": False,
            "confidence": "high",
            "reason": (
                "Supplier lowered both garnet (33->30) and peridot "
                "(22->20) prices, an improvement over previous offer but "
                "still above the buyer's targets (29.70 and 19.80), and "
                "asks if buyer accepts."
            ),
            "item_offers": [
                {
                    "item_material": "Garnet Pink",
                    "unit_price_usd": 30.0,
                    "price_certainty": "CONFIRMED",
                },
                {
                    "item_material": "Peridote (PER)",
                    "unit_price_usd": 20.0,
                    "price_certainty": "CONFIRMED",
                },
            ],
        },
    )

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body=(
            "Hello, I could lower the price of garnet to 30 usd and "
            "peridot to 20 usd. Would you accept?"
        ),
    )

    assert result["review_item_id"] is None

    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        item_info["Garnet Pink"]["case_item_id"], supplier_id
    )
    peridote_offer = repo.get_best_offer_for_case_item_supplier(
        item_info["Peridote (PER)"]["case_item_id"], supplier_id
    )
    assert garnet_offer["unit_price_usd"] == pytest.approx(30.0)
    assert peridote_offer["unit_price_usd"] == pytest.approx(20.0)

    # Both improved but still above target -> negotiation continues.
    assert result["supplier_state"] == SupplierState.DISCOUNT_REQUEST_SENT.value
    assert _round_count(case_id, supplier_id) == 2


def test_ask_price_clarification_is_not_overridden_for_legacy_single_item(
    monkeypatch: pytest.MonkeyPatch,
    supplier_ids: dict[str, int],
) -> None:
    """The override is gated on supplier_items - a legacy single-item
    case (no item_offers possible at all) must keep its existing
    ASK_PRICE_CLARIFICATION -> human review behavior unchanged."""
    from app.services.case_service import create_case

    supplier_id = supplier_ids["email"]
    case_id = create_case(
        item_material="Tanzanite (TAN)",
        quantity=40.0,
        notes="",
        supplier_ids=[supplier_id],
    )

    saved_offer_id = add_offer(
        case_id=case_id,
        supplier_id=supplier_id,
        unit_price_usd=30.0,
        extraction_method="manual",
        extraction_confidence="human_verified",
        notes="Initial RFQ offer for test setup.",
    )
    repo.upsert_case_negotiation_context(
        case_id=case_id,
        initial_best_offer_usd=30.0,
        target_price_usd=27.0,
        best_supplier_id=supplier_id,
        best_offer_id=saved_offer_id,
        valid_offer_count=1,
        target_discount_percent=10.0,
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
        state=SupplierState.DISCOUNT_REQUEST_SENT.value,
        best_offer_usd=30.0,
        target_price_usd=27.0,
    )
    repo.increment_negotiation_attempt(case_id=case_id, supplier_id=supplier_id)

    _patch_analysis(
        monkeypatch,
        {
            "success": True,
            "message_category": "UNCLEAR_PRICE",
            "recommended_action": "ASK_PRICE_CLARIFICATION",
            "unit_price_usd": None,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "safe_for_automation": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "confidence": "high",
            "reason": "Ambiguous reply.",
        },
    )

    result = negotiation_reply_service.record_negotiation_supplier_message(
        case_id=case_id,
        supplier_id=supplier_id,
        channel="email",
        body="Not sure, let me get back to you.",
    )

    assert result["review_item_id"] is not None
