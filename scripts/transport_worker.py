from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import initialize_database
from app.services.transport_worker_service import run_transport_cycle


load_dotenv()


def debug_enabled() -> bool:
    return os.getenv("EMAIL_WORKER_DEBUG", "false").lower() == "true"


def print_cycle_result(results) -> None:
    """
    Print useful worker status to the terminal.
    """

    if not results:
        print("No active cases to process.")
        print(
            "If you are testing one case, check EMAIL_WORKER_CASE_ID in .env."
        )
        return

    for result in results:
        if result.error:
            print(
                f"[CASE {result.case_number} / ID {result.case_id}] ERROR: {result.error}"
            )
            continue

        print(
            f"[CASE {result.case_number} / ID {result.case_id} / "
            f"{result.communication_mode}] "
            f"imported={result.imported_count}, "
            f"skipped={result.skipped_count}, "
            f"actions={len(result.rule_actions or [])}"
        )

        if debug_enabled():
            if result.import_results:
                print("  Import details:")
                for item in result.import_results:
                    print(f"    - {item}")

        if result.rule_actions:
            print("  Rule actions:")
            for action in result.rule_actions:
                print(f"    - {action}")


def main() -> None:
    initialize_database()

    enabled = os.getenv("EMAIL_WORKER_ENABLED", "true").lower() == "true"
    poll_seconds = int(os.getenv("EMAIL_WORKER_POLL_SECONDS", "30"))
    case_filter = os.getenv("EMAIL_WORKER_CASE_ID", "").strip()

    if not enabled:
        print("EMAIL_WORKER_ENABLED=false. Worker will not run.")
        return

    print("Unified transport worker started.")
    print(
        "Polling inbound email, processing stored inbound email/WhatsApp "
        "messages, advancing negotiation, and delivering/retrying queued "
        "outbound email and WhatsApp messages."
    )
    print(f"Polling every {poll_seconds} seconds.")

    if case_filter:
        print(f"Processing only EMAIL_WORKER_CASE_ID={case_filter}")
    else:
        print("Processing all active cases.")

    print("Press Ctrl+C to stop.")

    while True:
        try:
            results = run_transport_cycle()
            print_cycle_result(results)

        except KeyboardInterrupt:
            print("Transport worker stopped.")
            break

        except Exception as exc:
            print(f"Worker cycle failed: {exc}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
