# CLAUDE.md

This file guides Claude Code when working with this repository.

## Scope of these instructions

These instructions are default safety and architectural guidelines, not permanent
restrictions on future development.

An explicit user request may authorize new functionality or changes to the database
schema, public interfaces, architecture, business rules, or existing workflows.

When a requested feature conflicts with a guideline in this file, explain the conflict,
identify the affected components, and propose a safe implementation plan. Proceed after
the user approves the intended change.

## Project

This is an internal purchasing and price-negotiation assistant for one buyer.

The system:

* creates purchasing cases;
* contacts suppliers by email or WhatsApp;
* receives and classifies supplier replies;
* extracts and compares offers;
* negotiates price;
* escalates unclear or risky cases for human review;
* proposes a winner;
* requires human approval before winner notification.

The LLM may classify messages, extract structured information, and generate supplier-facing text.

Deterministic Python code must continue to control:

* reminders and deadlines;
* negotiation attempts;
* state transitions;
* target-price calculations;
* human-review rules;
* winner approval.

## Negotiation rules

`docs/rules.md` is the authoritative functional specification for negotiation behavior.

Before changing any logic related to:

* RFQs;
* reminders;
* deadlines;
* offer validation;
* price extraction;
* negotiation attempts;
* target prices;
* human review;
* supplier states;
* winner selection or notification;

read `docs/rules.md` and compare the planned change against it.

If the code, tests, and `docs/rules.md` disagree, report the conflict before changing behavior. Do not silently change the rules or modify tests only to make them pass.

## Commands

Run commands through the project virtual environment.

```powershell
# Full test suite
.venv\Scripts\python.exe -m pytest -q .\tests

# One test file
.venv\Scripts\python.exe -m pytest -q tests\test_rfq_lifecycle.py

# Streamlit UI
.venv\Scripts\streamlit.exe run ui\streamlit_app_clean.py

# FastAPI
.venv\Scripts\uvicorn.exe app.main:app --reload

# Email worker
.venv\Scripts\python.exe scripts\email_worker.py

# Initialize database
.venv\Scripts\python.exe scripts\init_db.py
```

There is no configured linting or formatting workflow. Do not invent Ruff, Black, Flake8, or similar commands.

`requirements.txt` is the dependency source.

## Architecture

```text
ui/streamlit_app_clean.py
    Buyer-facing UI.

app/main.py
    FastAPI application and WhatsApp webhook.

scripts/email_worker.py
    Background email polling worker.

app/services/*
    Application orchestration and side effects.

app/negotiation/*
    Deterministic negotiation rules and policy.

app/llm/*
    Classification, extraction, safeguards, and message generation.

app/integrations/*
    SMTP, Microsoft Graph, and WhatsApp integrations.

app/db/repository.py
    Central database-access layer.

app/db/database.py
    SQLite connection and schema initialization.
```

Keep UI, services, negotiation rules, LLM logic, integrations, and database access separate.

All SQL is currently centralized in `PurchasingRepository`. Do not add raw SQL outside the database layer.

Schema changes should be additive, non-destructive, and safe for existing databases.

Do not redesign the repository or architecture as part of an unrelated feature. Explicitly requested architectural changes are allowed after a plan is approved.

## Negotiation state and timing

`app/negotiation/states.py` defines case and supplier states.

`app/negotiation/policy.py` and `config/negotiation_policy.json` define timings, deadlines, retry limits, target reduction, and tolerance.

There are two timing modes:

* `testing`: minute-based;
* `production`: business-day-based.

Do not treat business days as elapsed hours. Keep timing values in policy configuration rather than hardcoding them in services.

When changing state logic:

* inspect all readers and writers of the affected state;
* preserve delayed supplier responses after worker restarts;
* do not send another negotiation message while waiting for a reply;
* preserve terminal states and human-review behavior.

## LLM provider

The current implementation uses local Ollama in:

* `app/llm/supplier_message_classifier.py`;
* `app/llm/communication_writer.py`.

Price-related LLM output is validated by deterministic safeguards, including:

* `app/llm/rfq_price_safeguard.py`;
* `app/llm/rfq_tentative_price_safeguard.py`.

The approved target is the Anthropic Claude API, initially using a configurable Claude Sonnet model.

When migrating:

* introduce a provider-independent LLM interface;
* use one centralized Claude adapter;
* do not scatter direct API calls across services;
* keep the model name configurable;
* load API keys from environment variables or approved secret storage;
* never commit secrets;
* preserve all price safeguards, fallbacks, test behavior, and human-review logic;
* keep Ollama available until the Claude implementation has been tested and approved.

Changes to classification or price extraction must include regression tests using realistic difficult supplier messages.

Do not change negotiation business rules as part of the LLM-provider migration unless explicitly requested.

## Communication safety

Each supplier uses either `email` or `whatsapp`.

Email:

* inbound: Microsoft Graph;
* outbound: SMTP.

WhatsApp:

* inbound: FastAPI webhook;
* outbound: WhatsApp Cloud API.

Email sending is controlled by:

* `EMAIL_DRY_RUN`;
* `EMAIL_TEST_MODE`;
* `EMAIL_TEST_SUPPLIER_TO`.

Sending is also controlled by the case-level `auto_send_messages` setting.

Preserve both global and case-level safety controls. Do not send real supplier messages from tests or bypass redirection.

## Tests

Each pytest test uses its own temporary SQLite database through `tests/conftest.py`.

Do not share database paths between tests. Ensure SQLite connections are closed, especially on Windows, where open files may cause `PermissionError` or `WinError 32`.

`scripts/test_*.py` files are manual/debug scripts and are not part of the normal pytest suite.

For behavioral changes:

1. add or update focused regression tests;
2. run the affected tests;
3. run the full suite;
4. report the exact commands and results;
5. do not claim tests passed unless they were run.

## Secrets

`.env` is not committed. Document new variables in `.env.example`.

`.claude/settings.json` denies Claude Code access to secrets, databases, attachments, and uploads.

Respect that boundary. Do not bypass it through shell commands or scripts.

Never expose or commit API keys, tokens, passwords, confidential supplier data, or production database contents.

## Change safety

These are default safety rules, not permanent restrictions on future development.

An explicit user request may authorize new functionality or changes to the database, interfaces, architecture, rules, or workflows.

For substantial changes:

1. inspect the relevant code;
2. read `docs/rules.md` when negotiation behavior is affected;
3. identify affected files;
4. present a plan;
5. identify unresolved business decisions;
6. edit after approval.

When editing:

* read complete affected files;
* do not replace files with shortened reconstructions;
* do not remove unrelated code;
* avoid unrelated refactoring;
* preserve existing behavior unless a change is explicitly requested.

After editing:

* list changed files;
* show or summarize the diff;
* run relevant tests;
* report remaining risks.

Do not commit, push, merge, deploy, or modify production data unless explicitly requested.

## Documentation

Keep this file concise and aligned with the repository.

* Describe current functionality as current.
* Describe planned functionality as planned.
* Update this file when major architecture changes are completed.
* Keep detailed documentation under `docs/`.
* Read large documents only when relevant to the current task.
