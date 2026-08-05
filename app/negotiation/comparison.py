from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP

from app.db.repository import PurchasingRepository
from app.negotiation.policy import load_negotiation_policy
from app.negotiation.states import CaseState, SupplierState


repo = PurchasingRepository()


def _round_money(value: Decimal) -> float:
    return float(
        value.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def _prepare_case_item_negotiation_contexts(
    case_id: int,
    discount_fraction: Decimal,
    target_discount_percent: float,
) -> None:
    """Persist a per-item negotiation target for every case_item that
    already has at least one active offer, using the same
    target-discount-percent formula as the case-wide target above, applied
    to that item's own best offer instead of a blended case-wide one.

    An order-case's items can have genuinely different market prices (e.g.
    two different stones); a single case-wide target is only meaningful
    when the case has one implicit item, which is exactly the legacy
    (no case_items) shape that continues to rely on
    case_negotiation_context above, untouched. Items with zero offers so
    far are skipped - there is nothing to compute a target from yet, and a
    later re-run of this function (should the case be re-prepared) fills
    them in once an offer exists.
    """
    for item in repo.list_case_items(case_id):
        item_offers = repo.list_best_offers_for_case_item(item["id"])
        if not item_offers:
            continue

        best_item_offer = item_offers[0]
        best_item_price = Decimal(str(best_item_offer["unit_price_usd"]))
        item_target_price = _round_money(
            best_item_price * (Decimal("1") - discount_fraction)
        )

        repo.upsert_case_item_negotiation_context(
            case_item_id=int(item["id"]),
            case_id=case_id,
            initial_best_offer_usd=float(best_item_price),
            target_price_usd=item_target_price,
            best_supplier_id=int(best_item_offer["supplier_id"]),
            best_offer_id=int(best_item_offer["offer_id"]),
            valid_offer_count=len(item_offers),
            target_discount_percent=target_discount_percent,
            ranking_json=json.dumps(
                [
                    {
                        "rank": rank,
                        "offer_id": int(offer["offer_id"]),
                        "supplier_id": int(offer["supplier_id"]),
                        "supplier_name": offer["supplier_name"],
                        "unit_price_usd": float(offer["unit_price_usd"]),
                    }
                    for rank, offer in enumerate(item_offers, start=1)
                ],
                ensure_ascii=False,
            ),
        )


def prepare_case_for_negotiation(case_id: int) -> dict:
    """
    Build and persist the initial multi-supplier comparison.

    This function does not send supplier messages.

    It:
    - selects each supplier's best current offer;
    - ranks suppliers;
    - calculates the common negotiation target;
    - persists the comparison snapshot;
    - stores the target for suppliers with valid offers;
    - moves the case to NEGOTIATING.
    """
    policy = load_negotiation_policy()

    case_data = repo.get_case_basic(case_id)
    if case_data is None:
        raise ValueError("Case not found.")

    offers = repo.list_best_supplier_offers_for_case(case_id)

    if len(offers) < policy.minimum_valid_offers:
        raise ValueError(
            f"At least {policy.minimum_valid_offers} valid supplier "
            "offers are required before negotiation can start."
        )

    ranking: list[dict] = []

    for rank, offer in enumerate(offers, start=1):
        ranking.append(
            {
                "rank": rank,
                "offer_id": int(offer["offer_id"]),
                "supplier_id": int(offer["supplier_id"]),
                "supplier_code": offer["supplier_code"],
                "supplier_name": offer["supplier_name"],
                "unit_price_usd": float(
                    offer["unit_price_usd"]
                ),
                "extraction_confidence": (
                    offer["extraction_confidence"]
                ),
            }
        )

    best_offer = ranking[0]

    best_price = Decimal(
        str(best_offer["unit_price_usd"])
    )

    discount_fraction = (
        Decimal(str(policy.target_discount_percent))
        / Decimal("100")
    )

    target_price = _round_money(
        best_price
        * (
            Decimal("1")
            - discount_fraction
        )
    )

    repo.upsert_case_negotiation_context(
        case_id=case_id,
        initial_best_offer_usd=float(best_price),
        target_price_usd=target_price,
        best_supplier_id=int(best_offer["supplier_id"]),
        best_offer_id=int(best_offer["offer_id"]),
        valid_offer_count=len(ranking),
        target_discount_percent=float(
            policy.target_discount_percent
        ),
        ranking_json=json.dumps(
            ranking,
            ensure_ascii=False,
        ),
    )

    for offer in ranking:
        repo.set_supplier_policy_state(
            case_id=case_id,
            supplier_id=int(offer["supplier_id"]),
            state=SupplierState.PRICE_EXTRACTED.value,
            best_offer_usd=float(
                offer["unit_price_usd"]
            ),
            target_price_usd=target_price,
        )

    _prepare_case_item_negotiation_contexts(
        case_id=case_id,
        discount_fraction=discount_fraction,
        target_discount_percent=float(policy.target_discount_percent),
    )

    repo.update_case_status_with_event(
        case_id=case_id,
        status=CaseState.NEGOTIATING.value,
        event_type="initial_comparison_completed",
        details=(
            f"Compared {len(ranking)} valid supplier offers. "
            f"Best current offer: USD {float(best_price):.2f}. "
            f"Negotiation target: USD {target_price:.2f} "
            f"({policy.target_discount_percent:.1f}% below "
            "the best offer)."
        ),
    )

    return {
        "case_id": case_id,
        "valid_offer_count": len(ranking),
        "initial_best_offer_usd": float(best_price),
        "target_price_usd": target_price,
        "target_discount_percent": float(
            policy.target_discount_percent
        ),
        "best_supplier_id": int(
            best_offer["supplier_id"]
        ),
        "best_supplier_name": (
            best_offer["supplier_name"]
        ),
        "ranking": ranking,
    }