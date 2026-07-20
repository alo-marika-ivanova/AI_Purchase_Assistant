from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import make_msgid

from dotenv import load_dotenv


load_dotenv()

EMAIL_DRY_RUN = os.getenv("EMAIL_DRY_RUN", "true").lower() == "true"
EMAIL_TEST_MODE = os.getenv("EMAIL_TEST_MODE", "true").lower() == "true"
EMAIL_TEST_SUPPLIER_TO = os.getenv("EMAIL_TEST_SUPPLIER_TO", "").strip()


def resolve_recipient_email(real_supplier_email: str) -> str:
    """
    In test mode, send all supplier emails to one safe test inbox.
    """
    if EMAIL_TEST_MODE:
        if not EMAIL_TEST_SUPPLIER_TO:
            raise ValueError("EMAIL_TEST_MODE=true but EMAIL_TEST_SUPPLIER_TO is empty.")
        return EMAIL_TEST_SUPPLIER_TO

    return real_supplier_email


def send_email_message(
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> dict:
    """
    Send one email message using SMTP.

    Threading support:
    - Message-ID is generated for every outbound email.
    - In-Reply-To and References are added when available.

    This improves mailbox threading, but final grouping still depends on the email provider/client.
    """

    try:
        final_to_email = resolve_recipient_email(to_email)
    except Exception as exc:
        return {
            "success": False,
            "provider_message_id": None,
            "internet_message_id": None,
            "error": str(exc),
        }

    if not final_to_email:
        return {
            "success": False,
            "provider_message_id": None,
            "internet_message_id": None,
            "error": "Missing recipient email.",
        }

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = os.getenv("SMTP_PORT")
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")

    # Generate a stable outbound email Message-ID.
    # Domain part can be customized later.
    internet_message_id = make_msgid(domain="purchasing-ai.local")

    if EMAIL_DRY_RUN:
        print("EMAIL DRY RUN")
        print("TO:", final_to_email)
        print("SUBJECT:", subject)
        print("MESSAGE-ID:", internet_message_id)
        print("IN-REPLY-TO:", in_reply_to)
        print("REFERENCES:", references)
        print("BODY:", body)

        return {
            "success": True,
            "provider_message_id": "dry-run-email",
            "internet_message_id": internet_message_id,
            "error": None,
        }

    if not smtp_host or not smtp_port or not smtp_user or not smtp_pass:
        return {
            "success": False,
            "provider_message_id": None,
            "internet_message_id": None,
            "error": "Missing SMTP configuration in .env.",
        }

    try:

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = final_to_email
        msg["Message-ID"] = internet_message_id

        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to

        if references:
            msg["References"] = references

        context = ssl.create_default_context()

        with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [final_to_email], msg.as_string())

        return {
            "success": True,
            "provider_message_id": f"smtp:{final_to_email}",
            "internet_message_id": internet_message_id,
            "error": None,
        }

    except Exception as exc:
        return {
            "success": False,
            "provider_message_id": None,
            "internet_message_id": None,
            "error": str(exc),
        }

def send_internal_email_message(
    to_email: str,
    subject: str,
    body: str,
) -> dict:
    """Send an internal notification directly to the buyer mailbox.

    Unlike supplier emails, this function deliberately does not apply
    EMAIL_TEST_MODE or EMAIL_TEST_SUPPLIER_TO redirection.
    """
    final_to_email = (to_email or "").strip()

    if not final_to_email:
        return {
            "success": False,
            "provider_message_id": None,
            "internet_message_id": None,
            "error": "Missing internal recipient email.",
        }

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = os.getenv("SMTP_PORT")
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    internet_message_id = make_msgid(domain="purchasing-ai.local")

    if EMAIL_DRY_RUN:
        print("INTERNAL EMAIL DRY RUN")
        print("TO:", final_to_email)
        print("SUBJECT:", subject)
        print("MESSAGE-ID:", internet_message_id)
        print("BODY:", body)

        return {
            "success": True,
            "provider_message_id": "dry-run-internal-email",
            "internet_message_id": internet_message_id,
            "error": None,
        }

    if not smtp_host or not smtp_port or not smtp_user or not smtp_pass:
        return {
            "success": False,
            "provider_message_id": None,
            "internet_message_id": None,
            "error": "Missing SMTP configuration in .env.",
        }

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = final_to_email
        msg["Message-ID"] = internet_message_id

        context = ssl.create_default_context()

        with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [final_to_email], msg.as_string())

        return {
            "success": True,
            "provider_message_id": f"smtp:{final_to_email}",
            "internet_message_id": internet_message_id,
            "error": None,
        }

    except Exception as exc:
        return {
            "success": False,
            "provider_message_id": None,
            "internet_message_id": None,
            "error": str(exc),
        }
