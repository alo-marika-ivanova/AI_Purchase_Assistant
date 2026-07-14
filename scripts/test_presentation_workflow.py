from __future__ import annotations

import os
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMP_DIR = tempfile.TemporaryDirectory(prefix="aipurchase_test_")
os.environ["PURCHASING_AI_DB_PATH"] = str(
    Path(TEMP_DIR.name) / "presentation_test.sqlite3"
)
os.environ["USE_LLM_COMMUNICATION_WRITER"] = "false"

import sys

sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import get_connection, initialize_database
from app.db.repository import PurchasingRepository
from app.db.seed import seed_suppliers_from_csv
from app.llm.communication_writer import fallback_message
from app.services.case_service import create_case
from app.services import negotiation_reply_service
from app.services import simple_chat_service


repo = PurchasingRepository()


def fake_writer(
    intent: str,
    case_data: dict,
    supplier: dict,
    message_history=None,
    target_price_usd=None,
    supplier_best_price_usd=None,
    winning_price_usd=None,
    extra_context: str = "",
    use_llm=None,
) -> dict:
    return fallback_message(
        intent=intent,
        case_data=case_data,
        supplier=supplier,
        target_price_usd=target_price_usd,
        supplier_best_price_usd=supplier_best_price_usd,
        winning_price_usd=winning_price_usd,
    )


def _base_analysis() -> dict:
    return {
        "success": True,
        "provider": "test",
        "model": "test",
        "message_category": "GENERAL_NON_PRICE",
        "recommended_action": "PAUSE_FOR_REVIEW",
        "safe_for_automation": True,
        "requires_human_review": False,
        "confidence": "high",
        "stated_price_amount": None,
        "unit_price_usd": None,
        "currency": None,
        "price_basis": "NONE",
        "is_price_clear": False,
        "is_currency_clear": False,
        "has_multiple_prices": False,
        "is_conditional": False,
        "condition_summary": None,
        "supplier_will_reply_later": False,
        "supplier_refused": False,
        "supplier_accepts_target": False,
        "question_can_be_answered_from_case": False,
        "reason": "Test analysis.",
        "suggested_clarification_question": None,
        "suggested_buyer_reply": None,
        "raw_result": {},
        "error": None,
    }


def fake_classifier(
    *,
    message_body: str,
    case_data: dict,
    supplier: dict | None = None,
    message_history=None,
    conversation_stage: str,
    supplier_state: str | None = None,
    target_price_usd: float | None = None,
    supplier_best_price_usd: float | None = None,
) -> dict:
    text = message_body.lower()
    result = _base_analysis()

    if "yes" in text and target_price_usd is not None:
        result.update(
            message_category="TARGET_ACCEPTANCE",
            recommended_action="SAVE_OFFER",
            supplier_accepts_target=True,
            reason="Supplier accepted the explicit target.",
        )
        return result

    if "39" in text and "final" in text:
        # Deliberately mimic an imperfect LLM classification. The common
        # deterministic policy must still save the explicit improvement.
        result.update(
            message_category="PRICE_REFUSAL",
            recommended_action="RECORD_PRICE_REFUSAL",
            supplier_refused=True,
            reason="Supplier called the new price final.",
        )
        return result

    price = None
    if "42" in text:
        price = 42.0
    elif "50" in text:
        price = 50.0

    if price is not None:
        result.update(
            message_category="CLEAR_PRICE_OFFER",
            recommended_action="SAVE_OFFER",
            stated_price_amount=price,
            unit_price_usd=price,
            currency="USD",
            price_basis="UNIT",
            is_price_clear=True,
            is_currency_clear=True,
            reason=f"Clear USD unit offer: {price}.",
        )
        return result

    return result


def supplier_id(code: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM suppliers WHERE supplier_code = ?",
            (code,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"Supplier {code} not found.")
    return int(row["id"])


def assert_state(case_id: int, supplier_id_value: int, expected: str) -> None:
    state = repo.get_supplier_state(case_id, supplier_id_value)
    assert state is not None
    assert state["state"] == expected, state


def run_simulation_workflow() -> None:
    supplier_a = supplier_id("SUP-001")
    supplier_b = supplier_id("SUP-002")

    case_id = create_case(
        item_material="ruby",
        quantity=1.0,
        notes="Presentation test",
        supplier_ids=[supplier_a, supplier_b],
        auto_send_messages=False,
    )

    start_result = simple_chat_service.start_negotiating_case(case_id)
    assert len(start_result["actions"]) == 2, start_result

    messages_a = repo.list_messages_for_case_supplier(case_id, supplier_a)
    messages_b = repo.list_messages_for_case_supplier(case_id, supplier_b)
    assert messages_a[-1]["message_type"] == "rfq"
    assert messages_b[-1]["message_type"] == "rfq"
    assert messages_a[-1]["channel"] == "manual"
    assert messages_b[-1]["channel"] == "manual"
    assert messages_a[-1]["body"] != messages_b[-1]["body"]

    # Make RFQs old enough for the testing reminder policy.
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE messages
            SET created_at = datetime('now', '-3 minutes')
            WHERE case_id = ? AND message_type = 'rfq'
            """,
            (case_id,),
        )
        conn.commit()

    reminder_result = simple_chat_service.continue_negotiation_for_case(case_id)
    assert len(reminder_result["actions"]) == 2, reminder_result
    assert all(
        action.get("action") == "SEND_RFQ_REMINDER"
        for action in reminder_result["actions"]
    )

    first = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="Our price is 42 USD per unit.",
    )
    assert first["saved_offer_id"] is not None
    simple_chat_service.continue_negotiation_for_case(case_id)

    second = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_b,
        channel="manual",
        body="Our price is 50 USD per unit.",
    )
    assert second["saved_offer_id"] is not None

    progression = simple_chat_service.continue_negotiation_for_case(case_id)
    action_names = [action.get("action") for action in progression["actions"]]
    assert "PREPARE_NEGOTIATION" in action_names, progression
    assert action_names.count("SEND_DISCOUNT_REQUEST") == 2, progression

    case_data = repo.get_case_basic(case_id)
    assert case_data["status"] == "NEGOTIATING", case_data
    assert_state(case_id, supplier_a, "DISCOUNT_REQUEST_SENT")
    assert_state(case_id, supplier_b, "DISCOUNT_REQUEST_SENT")

    context = repo.get_case_negotiation_context(case_id)
    assert context is not None
    assert abs(float(context["target_price_usd"]) - 37.8) <= 0.001

    acceptance = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_a,
        channel="manual",
        body="Yes, we can do that.",
    )
    assert acceptance["saved_offer_id"] is not None
    assert_state(case_id, supplier_a, "FINAL_OFFER_RECEIVED")

    counter = simple_chat_service.record_supplier_message_simple(
        case_id=case_id,
        supplier_id=supplier_b,
        channel="manual",
        body="I can go to 39 USD, but that is my final offer.",
    )
    assert counter["saved_offer_id"] is not None
    assert_state(case_id, supplier_b, "FINAL_OFFER_RECEIVED")

    case_data = repo.get_case_basic(case_id)
    assert case_data["status"] == "BUYER_REVIEW", case_data

    recommendation = simple_chat_service.get_suggested_winner(case_id)
    assert recommendation is not None
    winner = recommendation["recommended_offer"]
    assert int(winner["supplier_id"]) == supplier_a
    assert abs(float(winner["unit_price_usd"]) - 37.8) <= 0.001

    notification = (
        simple_chat_service.generate_and_send_winner_notification_for_supplier(
            case_id=case_id,
            supplier_id=supplier_a,
        )
    )
    assert notification["send_result"] is None
    case_data = repo.get_case_basic(case_id)
    assert case_data["status"] == "WINNER_NOTIFIED", case_data


def run_real_routing_test() -> None:
    email_supplier = supplier_id("SUP-006")
    whatsapp_supplier = supplier_id("SUP-008")

    email_calls: list[dict] = []
    whatsapp_calls: list[dict] = []

    def fake_email_send(**kwargs):
        email_calls.append(kwargs)
        return {
            "success": True,
            "provider_message_id": "email-test-id",
            "internet_message_id": "<email-test-id@example.test>",
            "error": None,
        }

    def fake_whatsapp_send(**kwargs):
        whatsapp_calls.append(kwargs)
        return {
            "success": True,
            "provider_message_id": "wa-test-id",
            "error": None,
        }

    simple_chat_service.send_email_message = fake_email_send
    simple_chat_service.send_whatsapp_text = fake_whatsapp_send

    case_id = create_case(
        item_material="sapphire",
        quantity=2.0,
        notes="Mocked real routing",
        supplier_ids=[email_supplier, whatsapp_supplier],
        auto_send_messages=True,
    )

    result = simple_chat_service.start_negotiating_case(case_id)
    assert len(result["actions"]) == 2, result
    assert len(email_calls) == 1, email_calls
    assert len(whatsapp_calls) == 1, whatsapp_calls

    email_messages = repo.list_messages_for_case_supplier(case_id, email_supplier)
    whatsapp_messages = repo.list_messages_for_case_supplier(
        case_id, whatsapp_supplier
    )
    assert email_messages[-1]["status"] == "sent_email"
    assert email_messages[-1]["channel"] == "email"
    assert whatsapp_messages[-1]["status"] == "sent_whatsapp"
    assert whatsapp_messages[-1]["channel"] == "whatsapp"


def main() -> None:
    initialize_database()
    seed_suppliers_from_csv()

    simple_chat_service.write_buyer_message = fake_writer
    simple_chat_service.analyze_supplier_message_with_ollama = fake_classifier
    negotiation_reply_service.analyze_supplier_message_with_ollama = fake_classifier

    run_simulation_workflow()
    run_real_routing_test()

    print("PRESENTATION WORKFLOW TEST PASSED")
    print("- personalized simulated RFQs")
    print("- RFQ reminders")
    print("- offer extraction and comparison")
    print("- immediate target requests")
    print("- target acceptance and improved final offer")
    print("- winner recommendation and notification")
    print("- case-controlled email and WhatsApp routing")


if __name__ == "__main__":
    try:
        main()
    finally:
        TEMP_DIR.cleanup()
