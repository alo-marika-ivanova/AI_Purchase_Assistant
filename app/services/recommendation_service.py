from __future__ import annotations

from app.db.repository import PurchasingRepository


repo = PurchasingRepository()


def get_offer_recommendation(case_id: int) -> dict | None:
    offers = repo.get_active_offers_for_recommendation(case_id)

    if not offers:
        return None

    best = offers[0]
    second = offers[1] if len(offers) > 1 else None

    if second:
        savings_per_unit = float(second["unit_price_usd"]) - float(best["unit_price_usd"])
        savings_percent = (savings_per_unit / float(second["unit_price_usd"])) * 100

        explanation = (
            f"{best['supplier_name']} has the lowest unit price at "
            f"USD {best['unit_price_usd']}. "
            f"The next best offer is {second['supplier_name']} at "
            f"USD {second['unit_price_usd']}. "
            f"Estimated saving is USD {savings_per_unit:.2f} per unit "
            f"({savings_percent:.2f}%)."
        )
    else:
        explanation = (
            f"{best['supplier_name']} is currently recommended because it is "
            f"the only recorded active offer."
        )

    return {
        "recommended_offer": best,
        "all_offers": offers,
        "explanation": explanation,
    }


def approve_recommended_winner(case_id: int, offer_id: int, reason: str) -> int:
    return repo.approve_winner(case_id=case_id, offer_id=offer_id, reason=reason)


def get_winner_decision(case_id: int) -> dict | None:
    return repo.get_winner_decision(case_id)