from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.api import whatsapp_webhook
from app.db.database import get_connection
from app.main import app
from app.services import whatsapp_transport_service


FAKE_SECRET = "test-meta-app-secret"


def _sign(raw_body: bytes, secret: str = FAKE_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _whatsapp_payload(wa_message_id: str, sender_phone: str, text: str) -> bytes:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": wa_message_id,
                                    "from": sender_phone,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def _inbound_event_count() -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM whatsapp_inbound_events"
        ).fetchone()
    return int(row["n"])


@pytest.fixture(autouse=True)
def _configure_webhook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_APP_SECRET", FAKE_SECRET)
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "test-verify-token")
    monkeypatch.setenv("WHATSAPP_INLINE_PROCESSING", "false")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_get_verification_succeeds_with_correct_token(client: TestClient) -> None:
    response = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "challenge-123",
        },
    )

    assert response.status_code == 200
    assert response.text == "challenge-123"


def test_get_verification_rejects_wrong_token(client: TestClient) -> None:
    response = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "challenge-123",
        },
    )

    assert response.status_code == 403


def test_post_with_valid_signature_is_accepted_and_persisted(
    client: TestClient,
) -> None:
    body = _whatsapp_payload("wamid.SEC1", "+420700000009", "hello")

    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert _inbound_event_count() == 1


def test_post_with_invalid_signature_is_rejected_and_nothing_persisted(
    client: TestClient,
) -> None:
    body = _whatsapp_payload("wamid.SEC2", "+420700000009", "hello")

    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body, secret="wrong-secret"),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 403
    assert _inbound_event_count() == 0


def test_post_with_missing_signature_is_rejected_and_nothing_persisted(
    client: TestClient,
) -> None:
    body = _whatsapp_payload("wamid.SEC3", "+420700000009", "hello")

    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 403
    assert _inbound_event_count() == 0


def test_post_with_no_secret_configured_is_rejected(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("META_APP_SECRET", raising=False)

    body = _whatsapp_payload("wamid.SEC4", "+420700000009", "hello")

    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 403
    assert _inbound_event_count() == 0


def test_duplicate_wa_message_id_is_persisted_only_once(
    client: TestClient,
) -> None:
    body = _whatsapp_payload("wamid.DUP1", "+420700000009", "hello")
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "Content-Type": "application/json",
    }

    client.post("/webhook/whatsapp", content=body, headers=headers)
    client.post("/webhook/whatsapp", content=body, headers=headers)

    assert _inbound_event_count() == 1


def test_async_mode_never_calls_the_synchronous_processing_path(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "The inline processing function must not be called when "
            "WHATSAPP_INLINE_PROCESSING=false."
        )

    monkeypatch.setattr(
        whatsapp_webhook, "process_inbound_whatsapp_message", _fail_if_called
    )

    body = _whatsapp_payload("wamid.ASYNC1", "+420700000009", "hello")

    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200


def test_inline_mode_uses_the_synchronous_processing_path(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_INLINE_PROCESSING", "true")

    called = {"n": 0}

    def fake_inline(**kwargs):
        called["n"] += 1
        return {"imported": False, "reason": "stubbed for test"}

    monkeypatch.setattr(
        whatsapp_webhook, "process_inbound_whatsapp_message", fake_inline
    )

    body = _whatsapp_payload("wamid.INLINE1", "+420700000009", "hello")

    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert called["n"] == 1
    # Inline mode never stages an event; it processes synchronously instead.
    assert _inbound_event_count() == 0
