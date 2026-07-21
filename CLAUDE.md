# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AI-assisted purchasing/negotiation assistant. Buyers create a "case" (an item + quantity to
procure), the system contacts candidate suppliers over email or WhatsApp, an Ollama-backed LLM
classifies inbound supplier replies and extracts offers, a rules engine drives the negotiation
state machine (RFQ → price extraction → discount negotiation → winner selection), and a Streamlit
UI plus a FastAPI webhook provide the human-facing surfaces.

## Commands

Run everything through the project venv's Python (`.venv\Scripts\python.exe` on Windows).

```powershell
# Run the full test suite
.venv\Scripts\python.exe -m pytest -q .\tests

# Run a single test file / test
.venv\Scripts\python.exe -m pytest -q tests\test_rfq_lifecycle.py
.venv\Scripts\python.exe -m pytest -q tests\test_rfq_lifecycle.py::test_some_case -v

# Streamlit UI (buyer-facing app)
.venv\Scripts\streamlit.exe run ui\streamlit_app_clean.py

# FastAPI app (WhatsApp webhook + /health)
.venv\Scripts\uvicorn.exe app.main:app --reload

# Background worker that polls the mailbox and advances negotiations
.venv\Scripts\python.exe scripts\email_worker.py

# One-off: import supplier/material catalog from data\supplier_catalog.xlsx
.venv\Scripts\python.exe scripts\import_supplier_filter_xlsx.py

# Initialize/create the SQLite schema without starting anything else
.venv\Scripts\python.exe scripts\init_db.py
```

There is no lint/format tooling configured in this repo (no ruff/black/flake8 config) — don't
invent lint commands. `requirements.txt` is the single dependency source (no lockfile,
no pyproject.toml).

## Architecture

### Layering

```
ui/streamlit_app_clean.py   Buyer-facing UI — calls services directly, no HTTP layer
app/main.py                 FastAPI app — mounts only the WhatsApp webhook + /health
scripts/email_worker.py     Polling loop; calls transport_worker_service on a timer
        ↓
app/services/*              Orchestration: one call here = one buyer action or one worker tick
app/negotiation/*           Pure(ish) rules/policy: decides WHAT should happen next
app/llm/*                   Ollama-backed classification/generation (supplier replies → structured
                             intent+offer JSON; buyer messages → natural-language text)
app/integrations/*          Outbound/inbound transport: SMTP, MS Graph (inbound email),
                             WhatsApp Cloud API
app/db/repository.py        Single repository class (PurchasingRepository) — ALL SQL lives here
app/db/database.py          Connection + schema bootstrap (SQLite, WAL mode)
```

Everything funnels through `app.db.repository.PurchasingRepository`. Do not write raw SQL
elsewhere — add a method to the repository (it's large, ~3800 lines, but it is intentionally
the only place that touches `sqlite3` directly). `app/db/database.get_connection()` is the only
place a connection is opened; `initialize_database()` runs `schema.sql` idempotently
(`CREATE TABLE IF NOT EXISTS`) — schema changes must stay additive/non-destructive, matching the
migration style already used for tables like `case_notification_preferences`.

### Negotiation state machine

`app/negotiation/states.py` defines two enums that drive everything:
- `CaseState` — case-level lifecycle (DRAFT → READY_TO_START → CONTACTING_SUPPLIERS →
  COLLECTING_OFFERS → NEGOTIATING → BUYER_REVIEW → WINNER_SELECTED → ... → CLOSED).
- `SupplierState` — per-(case, supplier) lifecycle (NOT_CONTACTED → REQUEST_SENT → ... →
  FINAL_OFFER_RECEIVED/NO_RESPONSE/REJECTED/WINNER, with `PAUSED_REVIEW` as an escape hatch to
  human review). `TERMINAL_SUPPLIER_STATES` marks which states end that supplier's involvement.

`app/negotiation/policy.py` (`NegotiationPolicy`, loaded from `config/negotiation_policy.json`)
holds all tunable timings/thresholds — reminder waits, deadlines, discount targets, retry caps.
It has two timing modes:
- `testing`: minute-based waits, for fast local iteration/tests.
- `production`: business-day based waits.

These are **not interchangeable units** — the dataclass raises `BusinessTimeNotImplementedError`
if minute-based accessors (`rfq_reminder_wait_minutes`, etc.) are called while
`mode == "production"`, specifically to prevent silently treating "1 business day" as "24 elapsed
hours". If you add a new timing-dependent rule, follow this same testing/production split rather
than adding a raw minutes field.

Rule modules (`app/negotiation/rfq_rules.py`, `negotiation_rules.py`, `common_reply_policy.py`,
`supplier_message_policy.py`) inspect current DB state + policy and return `*Action` dataclasses
(e.g. `RfqRuleAction`) describing what should happen — they don't perform side effects themselves.
The services layer (mainly `simple_chat_service.py` and `negotiation_reply_service.py`) executes
those actions: sending messages via `app/integrations`, writing offers, updating supplier/case
state, and — when `pause_on_unknown_or_risky_topic` fires or classification is uncertain — raising
a human review item via `human_review_notification_service.create_human_review_item_with_notification`.

### LLM usage

Both supplier-reply classification (`app/llm/supplier_message_classifier.py`) and buyer-message
generation (`app/llm/communication_writer.py`) call a local Ollama server (`OLLAMA_URL`,
`OLLAMA_MODEL` env vars, default `llama3.1`) via raw HTTP `requests` calls — there is no LLM
client SDK abstraction. Classifier output is parsed into structured JSON (see
`_extract_json_object`/`_normalize_result` in `supplier_message_classifier.py`); price safeguards
(`app/llm/rfq_price_safeguard.py`, `rfq_tentative_price_safeguard.py`) then sanity-check any
extracted price before it's trusted, since the LLM output is not implicitly trusted for money-bearing
fields. `USE_LLM_COMMUNICATION_WRITER=false` (set by `tests/conftest.py`) disables the
LLM-generated writer path during tests in favor of deterministic templates.

## LLM provider

- The current implementation still uses local Ollama in some components.
- The approved target architecture is Anthropic Claude API using Claude Sonnet.
- Do not add new Ollama-specific dependencies or deepen the Ollama integration.
- New LLM functionality must be implemented behind a provider-independent interface.
- Existing Ollama calls should be migrated incrementally to a shared Claude API adapter.
- API keys must be loaded from environment variables and must never be committed to Git.
- The LLM may classify messages, extract structured information and generate text.
- Deterministic Python code must continue to control reminders, deadlines, negotiation attempts, state transitions, target-price calculations and winner approval.

## Migration safety

- Before replacing any Ollama-related file, inspect its complete current contents.
- Preserve all existing fallback logic and deterministic safeguards.
- Do not remove Ollama support until the Claude API integration has been tested successfully.
- Add regression tests before changing message classification or price extraction behavior.

### Transport / channel duality

Every supplier has a `contact_channel` of `whatsapp` or `email` (`app/db/schema.sql`,
`suppliers` table), and most negotiation flows branch on this. Email uses MS Graph for inbound
polling (`app/integrations/graph_email_adapter.py`) and SMTP for outbound
(`app/integrations/email_adapter.py`); WhatsApp uses the Cloud API for both directions
(`app/integrations/whatsapp_adapter.py`, plus the inbound webhook in
`app/api/whatsapp_webhook.py`). Real sends are gated by env flags — `EMAIL_DRY_RUN` (log instead
of SMTP send) and `EMAIL_TEST_MODE`/`EMAIL_TEST_SUPPLIER_TO` (redirect supplier email to a test
inbox) — and independently by the per-case `auto_send_messages` flag (case-level
"real send" vs "simulation" toggle set from the Streamlit UI), consumed in
`transport_worker_service.process_case_email_transport`. When changing send behavior, respect
both gates rather than assuming one implies the other.

### Tests

`tests/conftest.py` gives every test an isolated SQLite file (via a `tmp_path`-based
`isolated_database` autouse fixture that monkeypatches `database_module.DB_PATH`) rather than
mocking the database — tests run against a real, freshly-initialized schema per test. A unique
file per test exists specifically to avoid Windows `PermissionError`/`WinError 32` from deleting
a SQLite file while a connection is still open — don't share DB paths across tests. The
`supplier_ids` fixture seeds one email and one WhatsApp supplier for tests that need both channels.
`scripts/test_*.py` are standalone manual/debug scripts (not part of the `tests/` pytest suite).

### Config and secrets

`.env` (not committed; see `.env.example` for the full variable list) drives Ollama, SMTP, MS
Graph, WhatsApp, and worker behavior. `.claude/settings.json` denies Claude Code read access to
`.env`, secrets, DB files, and attachment/upload directories — respect that boundary rather than
routing around it (e.g. don't `cat` `.env` via Bash).
