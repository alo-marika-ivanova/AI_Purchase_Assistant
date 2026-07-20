# Human-review email notifications

This patch is based on the tracked current project state:

- original `AIPurchaseAssistant160726.zip`
- delayed-response worker fix
- deterministic RFQ price safeguard
- crash-recoverable action locks
- normalized negotiation policy
- hidden Streamlit case-creation dialog
- provisional-offer milestone excluded, as requested

## Replace

- `app/db/schema.sql`
- `app/db/repository.py`
- `app/integrations/email_adapter.py`
- `app/services/case_service.py`
- `app/services/simple_chat_service.py`
- `app/services/negotiation_reply_service.py`
- `app/main.py`
- `ui/streamlit_app_clean.py`

## Add

- `app/services/human_review_notification_service.py`
- `tests/test_human_review_email_notifications.py`

## Configuration

The notification recipient is resolved as:

1. `BUYER_REVIEW_NOTIFICATION_EMAIL`, when set
2. otherwise existing `BUYER_EMAIL`

Supplier test-email redirection does not affect internal buyer notifications.
`EMAIL_DRY_RUN=true` still prevents real SMTP delivery.

## Database

No destructive migration is used. `initialize_database()` creates two additive tables:

- `case_notification_preferences`
- `human_review_email_notifications`

Existing cases behave as if the checkbox were unchecked.

## Test

```powershell
~\AppData\Local\Programs\Python\Python312\python.exe -m pytest -q .\tests
```

Expected for the tracked test suite: `29 passed`.
