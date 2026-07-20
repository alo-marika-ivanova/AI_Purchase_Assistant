from __future__ import annotations

from app.db.repository import PurchasingRepository
from app.services.case_service import create_case
from app.services import human_review_notification_service as notification_service


repo = PurchasingRepository()


def _create_test_case(
    supplier_id: int,
    *,
    notifications_enabled: bool,
) -> int:
    return create_case(
        item_material="Topaz Sky",
        quantity=1.0,
        notes="",
        supplier_ids=[supplier_id],
        auto_send_messages=False,
        notify_buyer_on_human_review=notifications_enabled,
    )


def _add_supplier_message(case_id: int, supplier_id: int) -> int:
    return repo.add_message(
        case_id=case_id,
        supplier_id=supplier_id,
        direction="inbound",
        channel="manual",
        body="Please review this supplier message.",
        status="recorded",
        message_type="supplier_response",
        approval_required=False,
        approved_by_buyer=True,
    )


def test_case_notification_preference_is_persisted(supplier_ids) -> None:
    case_id = _create_test_case(
        supplier_ids["email"],
        notifications_enabled=True,
    )

    case_data = repo.get_case_basic(case_id)

    assert case_data is not None
    assert case_data["notify_human_review_email"] == 1


def test_enabled_case_sends_one_email_for_duplicate_review_item(
    supplier_ids,
    monkeypatch,
) -> None:
    case_id = _create_test_case(
        supplier_ids["email"],
        notifications_enabled=True,
    )
    message_id = _add_supplier_message(
        case_id,
        supplier_ids["email"],
    )

    sent_messages: list[dict] = []

    monkeypatch.setenv(
        "BUYER_REVIEW_NOTIFICATION_EMAIL",
        "buyer@example.test",
    )

    def fake_send_internal_email_message(
        to_email: str,
        subject: str,
        body: str,
    ) -> dict:
        sent_messages.append(
            {
                "to_email": to_email,
                "subject": subject,
                "body": body,
            }
        )
        return {
            "success": True,
            "provider_message_id": "test-message",
            "internet_message_id": "test-internet-message",
            "error": None,
        }

    monkeypatch.setattr(
        notification_service,
        "send_internal_email_message",
        fake_send_internal_email_message,
    )

    first_review_id = (
        notification_service.create_human_review_item_with_notification(
            case_id=case_id,
            supplier_id=supplier_ids["email"],
            message_id=message_id,
            review_type="unknown_supplier_message",
            reason="The message requires buyer review.",
        )
    )
    second_review_id = (
        notification_service.create_human_review_item_with_notification(
            case_id=case_id,
            supplier_id=supplier_ids["email"],
            message_id=message_id,
            review_type="unknown_supplier_message",
            reason="The message requires buyer review.",
        )
    )

    assert first_review_id == second_review_id
    assert len(sent_messages) == 1
    assert sent_messages[0]["to_email"] == "buyer@example.test"
    assert "Topaz Sky" in sent_messages[0]["body"]
    assert "Please review this supplier message." in sent_messages[0]["body"]

    notification = repo.get_human_review_email_notification(
        first_review_id
    )
    assert notification is not None
    assert notification["status"] == "sent"


def test_disabled_case_creates_review_without_email(
    supplier_ids,
    monkeypatch,
) -> None:
    case_id = _create_test_case(
        supplier_ids["email"],
        notifications_enabled=False,
    )
    message_id = _add_supplier_message(
        case_id,
        supplier_ids["email"],
    )

    send_call_count = 0

    def fake_send_internal_email_message(
        to_email: str,
        subject: str,
        body: str,
    ) -> dict:
        nonlocal send_call_count
        send_call_count += 1
        return {
            "success": True,
            "error": None,
        }

    monkeypatch.setattr(
        notification_service,
        "send_internal_email_message",
        fake_send_internal_email_message,
    )

    review_id = (
        notification_service.create_human_review_item_with_notification(
            case_id=case_id,
            supplier_id=supplier_ids["email"],
            message_id=message_id,
            review_type="unknown_supplier_message",
            reason="The message requires buyer review.",
        )
    )

    assert review_id > 0
    assert send_call_count == 0
    assert repo.get_human_review_email_notification(review_id) is None
