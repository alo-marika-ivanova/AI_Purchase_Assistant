from __future__ import annotations

import pytest

import app.llm.supplier_message_classifier as classifier_module
from app.db.repository import PurchasingRepository
from app.services import simple_chat_service
from app.services.case_service import create_case, create_case_from_detected_items


repo = PurchasingRepository()


def _clear_price_analysis(reason: str, item_offers: list[dict]) -> dict:
    return {
        "success": True,
        "provider": "test",
        "model": "deterministic-test",
        "message_category": "CLEAR_PRICE_OFFER",
        "recommended_action": "SAVE_OFFER",
        "safe_for_automation": True,
        "requires_human_review": False,
        "contains_risky_topic": False,
        "risk_category": "NONE",
        "confidence": "high",
        "unit_price_usd": None,
        "currency": "USD",
        "price_basis": "MULTIPLE",
        "is_price_clear": True,
        "is_currency_clear": True,
        "has_multiple_prices": True,
        "is_conditional": False,
        "condition_summary": None,
        "supplier_will_reply_later": False,
        "supplier_refused": False,
        "supplier_accepts_target": False,
        "question_can_be_answered_from_case": False,
        "item_offers": item_offers,
        "reason": reason,
        "suggested_clarification_question": None,
        "suggested_buyer_reply": None,
    }


def test_multi_item_reply_records_one_offer_per_item(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Tanzanite (TAN)",
                "quantity": 40.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Ruby (RBN)",
                "quantity": 10.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _clear_price_analysis(
            "Supplier quoted both stones.",
            [
                {
                    "item_material": "Tanzanite (TAN)",
                    "unit_price_usd": 180.0,
                    "price_certainty": "CONFIRMED",
                },
                {
                    "item_material": "Ruby (RBN)",
                    "unit_price_usd": 60.0,
                    "price_certainty": "CONFIRMED",
                },
            ],
        ),
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="Tanzanite 180 usd/ct, Ruby 60 usd/ct.",
    )

    assert result["saved_offer_id"] is not None
    assert len(result["saved_offer_ids"]) == 2

    items = repo.list_case_items(case_id)
    items_by_name = {item["item_material"]: item for item in items}

    tan_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Tanzanite (TAN)"]["id"], supplier_a
    )
    rbn_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Ruby (RBN)"]["id"], supplier_a
    )

    assert tan_offer["unit_price_usd"] == pytest.approx(180.0)
    assert rbn_offer["unit_price_usd"] == pytest.approx(60.0)

    supplier_state = repo.get_supplier_state(case_id, supplier_a)
    assert supplier_state["state"] == "PRICE_EXTRACTED"


def test_partial_reply_only_saves_the_mentioned_item(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Tanzanite (TAN)",
                "quantity": 40.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Ruby (RBN)",
                "quantity": 10.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _clear_price_analysis(
            "Supplier only quoted the ruby so far.",
            [
                {
                    "item_material": "Ruby (RBN)",
                    "unit_price_usd": 60.0,
                    "price_certainty": "CONFIRMED",
                },
            ],
        ),
    )

    simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="Ruby 60 usd/ct, still checking Tanzanite.",
    )

    items = repo.list_case_items(case_id)
    items_by_name = {item["item_material"]: item for item in items}

    assert (
        repo.get_best_offer_for_case_item_supplier(
            items_by_name["Ruby (RBN)"]["id"], supplier_a
        )
        is not None
    )
    assert (
        repo.get_best_offer_for_case_item_supplier(
            items_by_name["Tanzanite (TAN)"]["id"], supplier_a
        )
        is None
    )


def test_winner_can_differ_per_item_within_the_same_order(
    supplier_ids: dict[str, int],
) -> None:
    """Directly exercises the scenario from the user's own example: item A
    goes to one supplier, item B to another, within the same order."""
    supplier_a = supplier_ids["email"]
    supplier_b = supplier_ids["whatsapp"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Tanzanite (TAN)",
                "quantity": 40.0,
                "supplier_ids": [supplier_a, supplier_b],
            },
            {
                "item_material": "Ruby (RBN)",
                "quantity": 10.0,
                "supplier_ids": [supplier_a, supplier_b],
            },
        ],
        notes="",
    )

    items = repo.list_case_items(case_id)
    tan_item_id = next(
        i["id"] for i in items if i["item_material"] == "Tanzanite (TAN)"
    )
    rbn_item_id = next(
        i["id"] for i in items if i["item_material"] == "Ruby (RBN)"
    )

    # Supplier A is better on Tanzanite, supplier B is better on Ruby.
    tan_offer_a = repo.add_offer(
        case_id=case_id,
        case_item_id=tan_item_id,
        supplier_id=supplier_a,
        unit_price_usd=180.0,
        quantity=None,
        message_id=None,
        extraction_method="manual",
        extraction_confidence="human_verified",
        notes="",
    )
    repo.add_offer(
        case_id=case_id,
        case_item_id=tan_item_id,
        supplier_id=supplier_b,
        unit_price_usd=200.0,
        quantity=None,
        message_id=None,
        extraction_method="manual",
        extraction_confidence="human_verified",
        notes="",
    )
    repo.add_offer(
        case_id=case_id,
        case_item_id=rbn_item_id,
        supplier_id=supplier_a,
        unit_price_usd=70.0,
        quantity=None,
        message_id=None,
        extraction_method="manual",
        extraction_confidence="human_verified",
        notes="",
    )
    rbn_offer_b = repo.add_offer(
        case_id=case_id,
        case_item_id=rbn_item_id,
        supplier_id=supplier_b,
        unit_price_usd=55.0,
        quantity=None,
        message_id=None,
        extraction_method="manual",
        extraction_confidence="human_verified",
        notes="",
    )

    tan_best = repo.list_best_offers_for_case_item(tan_item_id)
    rbn_best = repo.list_best_offers_for_case_item(rbn_item_id)
    assert min(o["unit_price_usd"] for o in tan_best) == pytest.approx(180.0)
    assert min(o["unit_price_usd"] for o in rbn_best) == pytest.approx(55.0)

    repo.approve_winner(case_id, tan_offer_a, "Best price for Tanzanite.")
    case = repo.get_case_basic(case_id)
    assert case["status"] != "WINNER SELECTED"  # Ruby still undecided

    repo.approve_winner(case_id, rbn_offer_b, "Best price for Ruby.")
    case = repo.get_case_basic(case_id)
    assert case["status"] == "WINNER SELECTED"

    winners = repo.list_winner_decisions_for_case(case_id)
    assert len(winners) == 2
    winners_by_item = {w["case_item_id"]: w for w in winners}
    assert winners_by_item[tan_item_id]["supplier_id"] == supplier_a
    assert winners_by_item[rbn_item_id]["supplier_id"] == supplier_b


def test_casual_and_misspelled_item_names_resolve_via_fuzzy_matching(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces a real supplier reply: short casual names and one typo,
    against catalog item names that carry codes the supplier never repeats."""
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet Pink",
                "quantity": 12.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Peridote (PER)",
                "quantity": 140.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _clear_price_analysis(
            "Supplier quoted both stones informally.",
            [
                {
                    "item_material": "garnet",
                    "unit_price_usd": 18.0,
                    "price_certainty": "CONFIRMED",
                },
                {
                    "item_material": "paridot",
                    "unit_price_usd": 33.0,
                    "price_certainty": "CONFIRMED",
                },
            ],
        ),
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="garnet: 18 usd per item paridot: 33 usd per item",
    )

    assert result["review_item_id"] is None
    assert len(result["saved_offer_ids"]) == 2

    items = repo.list_case_items(case_id)
    items_by_name = {item["item_material"]: item for item in items}

    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Garnet Pink"]["id"], supplier_a
    )
    peridote_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Peridote (PER)"]["id"], supplier_a
    )
    assert garnet_offer["unit_price_usd"] == pytest.approx(18.0)
    assert peridote_offer["unit_price_usd"] == pytest.approx(33.0)


def test_short_name_missing_code_resolves_to_the_right_item(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Peridot" (no trailing "e", no code) must still resolve to the case
    item "Peridote (PER)"."""
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet Pink",
                "quantity": 12.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Peridote (PER)",
                "quantity": 140.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _clear_price_analysis(
            "Supplier confirmed both prices.",
            [
                {
                    "item_material": "Garnet PINK",
                    "unit_price_usd": 18.0,
                    "price_certainty": "CONFIRMED",
                },
                {
                    "item_material": "Peridot",
                    "unit_price_usd": 33.0,
                    "price_certainty": "CONFIRMED",
                },
            ],
        ),
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="I confirm 18 usd for Garnet PINK. The unit price for Peridot is 33 usd.",
    )

    assert result["review_item_id"] is None
    assert len(result["saved_offer_ids"]) == 2

    items = repo.list_case_items(case_id)
    peridote_offer = repo.get_best_offer_for_case_item_supplier(
        next(i for i in items if i["item_material"] == "Peridote (PER)")["id"],
        supplier_a,
    )
    assert peridote_offer["unit_price_usd"] == pytest.approx(33.0)


def test_full_line_item_description_echoed_back_resolves_to_the_right_item(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces a real supplier reply filled into a price-response
    template: the supplier doesn't retype a short name, they leave the
    RFQ's own long line-item description in place and just add a price next
    to it (e.g. "Peridot round regular 2 mm" for a case item stored as the
    catalog's "Peridote (PER)"). The extra descriptive words must not drag
    the match score down below the threshold the way comparing the two
    full strings directly would."""
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet Pink",
                "quantity": 12.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Peridote (PER)",
                "quantity": 140.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _clear_price_analysis(
            "Supplier priced both full line-item descriptions.",
            [
                {
                    "item_material": "Garnet pink round regular 5 mm",
                    "unit_price_usd": 44.0,
                    "price_certainty": "CONFIRMED",
                },
                {
                    "item_material": "Peridot round regular 2 mm",
                    "unit_price_usd": 20.0,
                    "price_certainty": "CONFIRMED",
                },
            ],
        ),
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="see the attached file",
    )

    assert result["review_item_id"] is None
    assert len(result["saved_offer_ids"]) == 2

    items = repo.list_case_items(case_id)
    peridote_offer = repo.get_best_offer_for_case_item_supplier(
        next(i for i in items if i["item_material"] == "Peridote (PER)")["id"],
        supplier_a,
    )
    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        next(i for i in items if i["item_material"] == "Garnet Pink")["id"],
        supplier_a,
    )
    assert peridote_offer["unit_price_usd"] == pytest.approx(20.0)
    assert garnet_offer["unit_price_usd"] == pytest.approx(44.0)


def test_full_description_of_a_different_variety_is_not_guessed(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The longer-description matching must not let a shared first word
    ("garnet") wrongly resolve a full description of one garnet variety
    against a case item for a genuinely different variety."""
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet Hessonite (GRO)",
                "quantity": 12.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _clear_price_analysis(
            "Supplier priced a differently-described garnet variety.",
            [
                {
                    "item_material": "Garnet pink round regular 5 mm",
                    "unit_price_usd": 44.0,
                    "price_certainty": "CONFIRMED",
                },
            ],
        ),
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="see the attached file",
    )

    assert result["review_item_id"] is not None
    assert result.get("saved_offer_id") is None


def test_ambiguous_short_name_between_two_similar_items_is_not_guessed(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When an order has two similarly named items, a generic short name
    must not be silently attributed to either one."""
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet Pink",
                "quantity": 12.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Garnet Red",
                "quantity": 15.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _clear_price_analysis(
            "Supplier quoted one ambiguous garnet price.",
            [
                {
                    "item_material": "garnet",
                    "unit_price_usd": 18.0,
                    "price_certainty": "CONFIRMED",
                },
            ],
        ),
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="garnet: 18 usd per item",
    )

    assert result["review_item_id"] is not None
    assert result.get("saved_offer_id") is None


def test_fully_confirmed_item_offers_are_not_downgraded_to_clarification(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces a real reply where the supplier clearly priced every
    requested item, but the analyzer's whole-message recommended_action
    was (incorrectly) ASK_PRICE_CLARIFICATION - out of single-item habit,
    treating "more than one price in this message" as ambiguous. The
    per-item extraction is complete, so the clarification is unnecessary
    and must be overridden."""
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet Pink",
                "quantity": 12.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Peridote (PER)",
                "quantity": 140.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    analysis = _clear_price_analysis(
        "Supplier quoted both stones clearly.",
        [
            {
                "item_material": "garnet pink",
                "unit_price_usd": 18.0,
                "price_certainty": "CONFIRMED",
            },
            {
                "item_material": "Peridote",
                "unit_price_usd": 33.0,
                "price_certainty": "CONFIRMED",
            },
        ],
    )
    analysis["recommended_action"] = "ASK_PRICE_CLARIFICATION"

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: analysis,
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="we have garnet pink for 18 usd per unit. Peridote is for 33 usd per unit.",
    )

    assert result["review_item_id"] is None
    assert len(result["saved_offer_ids"]) == 2

    items = repo.list_case_items(case_id)
    items_by_name = {item["item_material"]: item for item in items}
    garnet_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Garnet Pink"]["id"], supplier_a
    )
    peridote_offer = repo.get_best_offer_for_case_item_supplier(
        items_by_name["Peridote (PER)"]["id"], supplier_a
    )
    assert garnet_offer["unit_price_usd"] == pytest.approx(18.0)
    assert peridote_offer["unit_price_usd"] == pytest.approx(33.0)


def test_partial_item_offers_still_allow_clarification_for_the_rest(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If only SOME of the requested items have a confirmed price, the
    analyzer's clarification request must not be overridden - there is a
    genuine open question about the item(s) still missing a price."""
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet Pink",
                "quantity": 12.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Peridote (PER)",
                "quantity": 140.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    analysis = _clear_price_analysis(
        "Supplier only quoted the garnet so far.",
        [
            {
                "item_material": "Garnet Pink",
                "unit_price_usd": 18.0,
                "price_certainty": "CONFIRMED",
            },
        ],
    )
    analysis["recommended_action"] = "ASK_PRICE_CLARIFICATION"

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: analysis,
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="Garnet Pink is 18 usd per unit, still checking on Peridote.",
    )

    assert result["saved_offer_id"] is None
    assert result["review_item_id"] is None


def _tentative_price_analysis(reason: str, item_offers: list[dict]) -> dict:
    return {
        "success": True,
        "provider": "test",
        "model": "deterministic-test",
        "message_category": "TENTATIVE_PRICE",
        "recommended_action": "SAVE_PROVISIONAL_OFFER_AND_WAIT",
        "safe_for_automation": True,
        "requires_human_review": False,
        "contains_risky_topic": False,
        "risk_category": "NONE",
        "confidence": "high",
        "unit_price_usd": None,
        "currency": "USD",
        "price_basis": "MULTIPLE",
        "is_price_clear": True,
        "is_currency_clear": True,
        "has_multiple_prices": True,
        "is_conditional": False,
        "condition_summary": None,
        "supplier_will_reply_later": False,
        "supplier_refused": False,
        "supplier_accepts_target": False,
        "question_can_be_answered_from_case": False,
        "item_offers": item_offers,
        "reason": reason,
        "suggested_clarification_question": None,
        "suggested_buyer_reply": None,
    }


def test_multi_item_tentative_reply_acknowledges_every_item_price(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces a real bug: a supplier attachment quoting 4 different
    subcase prices was recorded correctly (one provisional offer per
    subcase), but the acknowledgement message only mentioned ONE price -
    whichever offer happened to be saved last - because the case-level
    lookup only kept the single most recently inserted provisional offer.
    The acknowledgement must mention every item's own provisional price."""
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet Pink",
                "quantity": 12.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Peridote (PER)",
                "quantity": 140.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: _tentative_price_analysis(
            "Supplier gave tentative prices for both stones, pending verification.",
            [
                {
                    "item_material": "Garnet Pink",
                    "unit_price_usd": 44.0,
                    "price_certainty": "TENTATIVE",
                },
                {
                    "item_material": "Peridote (PER)",
                    "unit_price_usd": 20.0,
                    "price_certainty": "TENTATIVE",
                },
            ],
        ),
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="Garnet Pink 44 usd/ct, Peridot 20 usd/ct - still verifying internally.",
    )

    assert len(result["saved_offer_ids"]) == 2

    items = repo.list_case_items(case_id)
    items_by_name = {item["item_material"]: item for item in items}

    provisional_offers = repo.list_provisional_offers_for_case_supplier(
        case_id, supplier_a
    )
    assert {offer["item_material"] for offer in provisional_offers} == {
        "Garnet Pink",
        "Peridote (PER)",
    }
    assert {
        float(offer["unit_price_usd"]) for offer in provisional_offers
    } == {44.0, 20.0}

    cycle = simple_chat_service.continue_negotiation_for_case(case_id)
    assert [action["action"] for action in cycle["actions"]] == [
        "SEND_PROVISIONAL_PRICE_ACKNOWLEDGEMENT"
    ]

    messages = repo.list_messages_for_case_supplier(case_id, supplier_a)
    acknowledgement = [
        message
        for message in messages
        if message.get("message_type") == "provisional_price_acknowledgement"
    ][0]
    body = acknowledgement["body"]
    assert "44" in body
    assert "20" in body
    assert "Garnet Pink" in body
    assert "Peridote (PER)" in body

    # Neither item's price was confirmed/active yet - both stay provisional.
    assert (
        repo.get_best_offer_for_case_item_supplier(
            items_by_name["Garnet Pink"]["id"], supplier_a
        )
        is None
    )
    assert (
        repo.get_best_offer_for_case_item_supplier(
            items_by_name["Peridote (PER)"]["id"], supplier_a
        )
        is None
    )


def test_attachment_derived_multi_item_reply_is_confirmed_without_calling_the_llm(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end reproduction of a real bug: a supplier's filled-in RFQ
    spreadsheet (no hedging language) was going through the LLM and
    sometimes coming back TENTATIVE_PRICE, forcing an unnecessary
    provisional-then-confirm round trip. The deterministic multi-item
    attachment safeguard must intercept this BEFORE any LLM call - the fake
    provider below raises if it is ever invoked, proving the shortcut is
    what produced the result, not a lucky LLM response."""
    supplier_a = supplier_ids["email"]

    case_id = create_case_from_detected_items(
        items=[
            {
                "item_material": "Garnet pink round regular 5 mm",
                "quantity": 12.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Peridot round regular 2 mm",
                "quantity": 100.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Peridot round regular 4 mm",
                "quantity": 24.0,
                "supplier_ids": [supplier_a],
            },
            {
                "item_material": "Peridot round regular 5 mm",
                "quantity": 16.0,
                "supplier_ids": [supplier_a],
            },
        ],
        notes="",
    )

    simple_chat_service.start_negotiating_case(case_id)

    def _fail_if_called():
        raise AssertionError(
            "The LLM provider must not be called for a clean, unhedged "
            "attachment-derived multi-item price table."
        )

    monkeypatch.setattr(classifier_module, "get_llm_provider", _fail_if_called)

    attachment_text = (
        "QUALITY REQUIREMENTS: TOP quality. Perfect cut, polish, symmetry.\n"
        "ALO ID | Description | Needed quantity, pcs | Eleonora IMPORTANT notes | Price USD/ct\n"
        "PKGRPI500 | Garnet pink round regular 5 mm | 12 | 3 sets (each set by 4 stones) | 44\n"
        "PKPE200 | Peridot round regular 2 mm | 100 | matching | 20\n"
        "PKPE400 | Peridot round regular 4 mm | 24 | 6 sets (each set by 4 stones) | 30\n"
        "PKPE500 | Peridot round regular 5 mm | 16 | 4 sets (each set by 4 stones) | 40"
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="Please see the attachment.",
        analysis_text=attachment_text,
    )

    assert result["analysis"]["provider"] == "deterministic"
    assert len(result["saved_offer_ids"]) == 4

    items = repo.list_case_items(case_id)
    items_by_name = {item["item_material"]: item for item in items}

    for item_material, expected_price in (
        ("Garnet pink round regular 5 mm", 44.0),
        ("Peridot round regular 2 mm", 20.0),
        ("Peridot round regular 4 mm", 30.0),
        ("Peridot round regular 5 mm", 40.0),
    ):
        offer = repo.get_best_offer_for_case_item_supplier(
            items_by_name[item_material]["id"], supplier_a
        )
        assert offer is not None
        assert offer["unit_price_usd"] == pytest.approx(expected_price)

    # Confirmed immediately - no provisional-acknowledge-then-confirm round
    # trip needed for a clean, unhedged attachment reply.
    supplier_state = repo.get_supplier_state(case_id, supplier_a)
    assert supplier_state["state"] == "PRICE_EXTRACTED"


def test_legacy_single_item_case_offer_recording_is_unaffected(
    supplier_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manually created case (no case_items at all) must behave exactly
    as before: one offer, case_item_id NULL, no item_offers involved."""
    case_id = create_case(
        item_material="Tanzanite (TAN)",
        quantity=40.0,
        notes="",
        supplier_ids=[supplier_ids["email"]],
    )

    simple_chat_service.start_negotiating_case(case_id)

    monkeypatch.setattr(
        simple_chat_service,
        "analyze_supplier_message_with_ollama",
        lambda **_: {
            "success": True,
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "safe_for_automation": True,
            "requires_human_review": False,
            "contains_risky_topic": False,
            "risk_category": "NONE",
            "confidence": "high",
            "unit_price_usd": 180.0,
            "currency": "USD",
            "price_basis": "UNIT",
            "is_price_clear": True,
            "is_currency_clear": True,
            "has_multiple_prices": False,
            "is_conditional": False,
            "reason": "Single clear price.",
        },
    )

    result = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_ids["email"],
        channel="manual",
        body="We can do 180 usd/ct.",
    )

    assert result["saved_offer_id"] is not None
    assert "saved_offer_ids" not in result

    offer = repo.get_best_offer_for_case_supplier(case_id, supplier_ids["email"])
    assert offer["unit_price_usd"] == pytest.approx(180.0)
