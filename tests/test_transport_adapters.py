from __future__ import annotations

import smtplib

import pytest
import requests

from app.integrations import email_adapter, whatsapp_adapter


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict, headers: dict | None = None):
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}
        self.text = str(json_body)

    def json(self) -> dict:
        return self._json_body


def _patch_whatsapp_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(whatsapp_adapter, "WHATSAPP_DRY_RUN", False)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "test-phone-id")


def test_whatsapp_dry_run_never_calls_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(whatsapp_adapter, "WHATSAPP_DRY_RUN", True)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("requests.post must not be called in dry-run mode.")

    monkeypatch.setattr(requests, "post", _fail_if_called)

    result = whatsapp_adapter.send_whatsapp_text(
        to_number="+420700000001", body="hello"
    )

    assert result["success"] is True
    assert result["delivery_outcome"] == "dry_run"
    assert result["provider_message_id"] == "dry-run-whatsapp"


def test_whatsapp_success_response_is_classified_as_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_whatsapp_config(monkeypatch)

    def fake_post(*args, **kwargs):
        return _FakeResponse(200, {"messages": [{"id": "wamid.ABC"}]})

    monkeypatch.setattr(requests, "post", fake_post)

    result = whatsapp_adapter.send_whatsapp_text(
        to_number="+420700000001", body="hello"
    )

    assert result["success"] is True
    assert result["delivery_outcome"] == "sent"
    assert result["provider_message_id"] == "wamid.ABC"


def test_whatsapp_rate_limit_is_classified_as_transient_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_whatsapp_config(monkeypatch)

    def fake_post(*args, **kwargs):
        return _FakeResponse(429, {"error": "rate limited"}, {"Retry-After": "30"})

    monkeypatch.setattr(requests, "post", fake_post)

    result = whatsapp_adapter.send_whatsapp_text(
        to_number="+420700000001", body="hello"
    )

    assert result["success"] is False
    assert result["delivery_outcome"] == "transient"
    assert result["retry_after_seconds"] == 30


def test_whatsapp_server_error_is_classified_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_whatsapp_config(monkeypatch)

    def fake_post(*args, **kwargs):
        return _FakeResponse(503, {"error": "server error"})

    monkeypatch.setattr(requests, "post", fake_post)

    result = whatsapp_adapter.send_whatsapp_text(
        to_number="+420700000001", body="hello"
    )

    assert result["delivery_outcome"] == "transient"


def test_whatsapp_auth_error_is_classified_as_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_whatsapp_config(monkeypatch)

    def fake_post(*args, **kwargs):
        return _FakeResponse(401, {"error": "invalid token"})

    monkeypatch.setattr(requests, "post", fake_post)

    result = whatsapp_adapter.send_whatsapp_text(
        to_number="+420700000001", body="hello"
    )

    assert result["delivery_outcome"] == "permanent"


def test_whatsapp_invalid_recipient_is_classified_as_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_whatsapp_config(monkeypatch)

    def fake_post(*args, **kwargs):
        return _FakeResponse(400, {"error": "invalid recipient"})

    monkeypatch.setattr(requests, "post", fake_post)

    result = whatsapp_adapter.send_whatsapp_text(
        to_number="+420700000001", body="hello"
    )

    assert result["delivery_outcome"] == "permanent"


def test_whatsapp_connect_timeout_is_classified_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_whatsapp_config(monkeypatch)

    def fake_post(*args, **kwargs):
        raise requests.exceptions.ConnectTimeout("could not connect")

    monkeypatch.setattr(requests, "post", fake_post)

    result = whatsapp_adapter.send_whatsapp_text(
        to_number="+420700000001", body="hello"
    )

    assert result["delivery_outcome"] == "transient"


def test_whatsapp_read_timeout_is_classified_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_whatsapp_config(monkeypatch)

    def fake_post(*args, **kwargs):
        raise requests.exceptions.ReadTimeout("no response in time")

    monkeypatch.setattr(requests, "post", fake_post)

    result = whatsapp_adapter.send_whatsapp_text(
        to_number="+420700000001", body="hello"
    )

    assert result["delivery_outcome"] == "unknown"


def test_whatsapp_missing_credentials_are_classified_as_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(whatsapp_adapter, "WHATSAPP_DRY_RUN", False)
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)

    result = whatsapp_adapter.send_whatsapp_text(
        to_number="+420700000001", body="hello"
    )

    assert result["delivery_outcome"] == "permanent"


def test_email_dry_run_never_calls_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_adapter, "EMAIL_DRY_RUN", True)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("smtplib.SMTP must not be constructed in dry-run mode.")

    monkeypatch.setattr(smtplib, "SMTP", _fail_if_called)

    result = email_adapter.send_email_message(
        to_email="supplier@example.test",
        subject="RFQ",
        body="hello",
    )

    assert result["success"] is True
    assert result["delivery_outcome"] == "dry_run"


def test_classify_smtp_response_exception_4xx_is_transient() -> None:
    exc = smtplib.SMTPResponseException(450, b"Mailbox temporarily unavailable")

    assert email_adapter._classify_smtp_response_exception(exc) == "transient"


def test_classify_smtp_response_exception_5xx_is_permanent() -> None:
    exc = smtplib.SMTPResponseException(550, b"No such user")

    assert email_adapter._classify_smtp_response_exception(exc) == "permanent"
