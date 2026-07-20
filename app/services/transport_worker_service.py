from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from app.db.repository import PurchasingRepository
from app.services.simple_chat_service import refresh_mailbox_and_continue_case


load_dotenv()
repo = PurchasingRepository()


@dataclass
class WorkerCaseResult:
    case_id: int
    case_number: str
    communication_mode: str
    imported_count: int = 0
    skipped_count: int = 0
    rule_actions: list | None = None
    import_results: list[dict] | None = None
    error: str | None = None


def _get_worker_case_filter() -> int | None:
    raw_value = os.getenv("EMAIL_WORKER_CASE_ID", "").strip()

    if not raw_value:
        return None

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(
            "EMAIL_WORKER_CASE_ID must be empty or a numeric case ID."
        ) from exc


def get_cases_for_worker() -> list[dict]:
    case_filter = _get_worker_case_filter()
    cases = repo.list_cases_for_transport_worker()

    if case_filter is None:
        return cases

    return [
        case
        for case in cases
        if int(case["id"]) == case_filter
    ]


def process_case_email_transport(case: dict) -> WorkerCaseResult:
    case_id = int(case["id"])
    real_mode = bool(case.get("auto_send_messages"))

    result = WorkerCaseResult(
        case_id=case_id,
        case_number=case["case_number"],
        communication_mode="REAL" if real_mode else "SIMULATION",
        rule_actions=[],
        import_results=[],
    )

    try:
        cycle_result = refresh_mailbox_and_continue_case(case_id=case_id)

        import_result = cycle_result["import_result"]
        negotiation_result = cycle_result["negotiation_result"]

        result.imported_count = int(import_result.get("imported_count", 0))
        result.skipped_count = int(import_result.get("skipped_count", 0))
        result.import_results = import_result.get("results", [])
        result.rule_actions = negotiation_result.get("actions", [])

        return result

    except Exception as exc:
        result.error = str(exc)
        return result


def run_email_worker_cycle() -> list[WorkerCaseResult]:
    return [
        process_case_email_transport(case)
        for case in get_cases_for_worker()
    ]
