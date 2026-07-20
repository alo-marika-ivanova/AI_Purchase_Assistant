from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.llm.supplier_message_classifier import (
    analyze_supplier_message_with_ollama,
)


CASE_DATA = {
    "case_number": "CASE-NEGOTIATION-TEST",
    "item_material": "ruby",
    "quantity": 1.0,
    "notes": None,
}

SUPPLIER = {
    "name": "Demo Supplier",
    "supplier_code": "SUP-DEMO",
}

HISTORY = [
    {
        "direction": "outbound",
        "body": (
            "Thank you for your offer of USD 40 per unit for ruby. "
            "Could you please confirm whether you can offer USD 36 per unit?"
        ),
    }
]

TESTS = [
    "Yes, we can do that.",
    "We can reduce the price to 38 per unit.",
    "No, 40 USD is our final price.",
    "We will check internally and reply tomorrow.",
    "We can do 36 USD, but only with a 50 percent deposit.",
]


def main() -> None:
    for index, message in enumerate(TESTS, start=1):
        result = analyze_supplier_message_with_ollama(
            message_body=message,
            case_data=CASE_DATA,
            supplier=SUPPLIER,
            message_history=HISTORY,
            conversation_stage="NEGOTIATION",
            supplier_state="DISCOUNT_REQUEST_SENT",
            target_price_usd=36.0,
            supplier_best_price_usd=40.0,
        )

        print("=" * 80)
        print(f"TEST {index}: {message}")
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
