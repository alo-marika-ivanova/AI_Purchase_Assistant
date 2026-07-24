from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.transport_worker import main


if __name__ == "__main__":
    print(
        "scripts/email_worker.py is deprecated. Starting "
        "scripts/transport_worker.py instead, which now also delivers and "
        "retries queued WhatsApp messages in addition to email. Please "
        "update any scheduled task, service definition, or shortcut to run "
        "scripts/transport_worker.py directly."
    )
    main()
