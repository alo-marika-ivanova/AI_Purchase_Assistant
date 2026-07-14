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
    "case_number": "CASE-DEMO-001",
    "item_material": "ruby",
    "quantity": 10,
    "notes": "Natural ruby, requested specification unchanged.",
}


SUPPLIER = {
    "name": "Demo Supplier",
    "supplier_code": "SUP-DEMO",
}


TEST_MESSAGES = [
    "Our best unit price is 43 USD.",
    "The price is 32.",
    "USD 100 total for 10 pieces.",
    "It is 32 USD, or 29 USD above 100 pieces.",
    "We received the request and will quote tomorrow.",
    "Call me tomorrow.",
    "Can you confirm the requested quantity?",
    "Can you explain your payment conditions?",
    "We require a 50 percent deposit before production.",
    "Unfortunately we cannot supply this material.",
]


def main() -> None:
    print("Testing Ollama supplier-message classifier")
    print("=" * 80)

    for index, message in enumerate(TEST_MESSAGES, start=1):
        result = analyze_supplier_message_with_ollama(
            message_body=message,
            case_data=CASE_DATA,
            supplier=SUPPLIER,
            message_history=[],
            conversation_stage="RFQ",
            supplier_state="AWAITING_RESPONSE",
            target_price_usd=None,
        )

        print()
        print(f"TEST {index}")
        print(f"Message: {message}")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("-" * 80)


if __name__ == "__main__":
    main()