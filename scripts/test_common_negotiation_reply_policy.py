from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from app.negotiation.common_reply_policy import (
    decide_common_negotiation_reply,
)


BASE_ANALYSIS = {
    "message_category": "GENERAL_NON_PRICE",
    "recommended_action": "PAUSE_FOR_REVIEW",
    "safe_for_automation": True,
    "requires_human_review": False,
    "has_multiple_prices": False,
    "is_conditional": False,
    "price_basis": "NONE",
    "supplier_accepts_target": False,
}


TESTS = [
    {
        "name": "improved final offer overrides refusal",
        "text": "I can go to 39 USD, but that's my final offer.",
        "analysis": {
            **BASE_ANALYSIS,
            "message_category": "PRICE_REFUSAL",
            "recommended_action": "RECORD_PRICE_REFUSAL",
        },
        "previous": 42.0,
        "target": 36.0,
        "expected_action": "SAVE_OFFER",
        "expected_price": 39.0,
    },
    {
        "name": "ordinary improved price",
        "text": "We can reduce the price to 38 per unit.",
        "analysis": {
            **BASE_ANALYSIS,
            "message_category": "IMPROVED_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "unit_price_usd": 38.0,
        },
        "previous": 42.0,
        "target": 36.0,
        "expected_action": "SAVE_OFFER",
        "expected_price": 38.0,
    },
    {
        "name": "unchanged final price",
        "text": "No, 42 USD is our final price.",
        "analysis": {
            **BASE_ANALYSIS,
            "message_category": "PRICE_REFUSAL",
            "recommended_action": "RECORD_PRICE_REFUSAL",
        },
        "previous": 42.0,
        "target": 36.0,
        "expected_action": "RECORD_PRICE_REFUSAL",
        "expected_price": 42.0,
    },
    {
        "name": "contextual target acceptance",
        "text": "Yes, we can do that.",
        "analysis": {
            **BASE_ANALYSIS,
            "message_category": "TARGET_ACCEPTANCE",
            "recommended_action": "SAVE_OFFER",
            "supplier_accepts_target": True,
        },
        "previous": 42.0,
        "target": 36.0,
        "expected_action": "SAVE_OFFER",
        "expected_price": 36.0,
    },
    {
        "name": "deposit condition pauses",
        "text": (
            "We can do 36 USD, but only with a "
            "50 percent deposit."
        ),
        "analysis": {
            **BASE_ANALYSIS,
            "message_category": "CONDITIONAL_PRICE",
            "recommended_action": "ASK_PRICE_CLARIFICATION",
            "is_conditional": True,
        },
        "previous": 42.0,
        "target": 36.0,
        "expected_action": "PAUSE_FOR_REVIEW",
        "expected_price": None,
    },
    {
        "name": "two prices pause",
        "text": (
            "We can do 40 USD, or 38 USD above "
            "100 pieces."
        ),
        "analysis": {
            **BASE_ANALYSIS,
            "message_category": "MULTIPLE_PRICES",
            "recommended_action": "ASK_PRICE_CLARIFICATION",
            "has_multiple_prices": True,
        },
        "previous": 42.0,
        "target": 36.0,
        "expected_action": "PAUSE_FOR_REVIEW",
        "expected_price": None,
    },
    {
        "name": "price increase pauses",
        "text": "Our final price is 45 USD.",
        "analysis": {
            **BASE_ANALYSIS,
            "message_category": "CLEAR_PRICE_OFFER",
            "recommended_action": "SAVE_OFFER",
            "unit_price_usd": 45.0,
        },
        "previous": 42.0,
        "target": 36.0,
        "expected_action": "PAUSE_FOR_REVIEW",
        "expected_price": None,
    },
    {
        "name": "reply later remains classifier decision",
        "text": "We will check and reply tomorrow.",
        "analysis": {
            **BASE_ANALYSIS,
            "message_category": (
                "ACKNOWLEDGEMENT_WILL_REPLY"
            ),
            "recommended_action": "WAIT_FOR_SUPPLIER",
        },
        "previous": 42.0,
        "target": 36.0,
        "expected_action": "USE_CLASSIFIER_RESULT",
        "expected_price": None,
    },
]


def main() -> None:
    passed = 0

    for index, test in enumerate(TESTS, start=1):
        result = decide_common_negotiation_reply(
            supplier_text=test["text"],
            analysis=test["analysis"],
            previous_best_price_usd=test["previous"],
            target_price_usd=test["target"],
        )

        print("=" * 80)
        print(f"TEST {index}: {test['name']}")
        print(f"TEXT: {test['text']}")
        print(f"ACTION: {result.action}")
        print(f"PRICE: {result.unit_price_usd}")
        print(f"REASON: {result.reason}")

        assert result.action == test["expected_action"]

        expected_price = test["expected_price"]

        if expected_price is None:
            assert result.unit_price_usd is None
        else:
            assert result.unit_price_usd is not None
            assert (
                abs(
                    result.unit_price_usd
                    - expected_price
                )
                <= 0.005
            )

        passed += 1
        print("RESULT: PASS")

    print("=" * 80)
    print(f"ALL TESTS PASSED: {passed}/{len(TESTS)}")


if __name__ == "__main__":
    main()
