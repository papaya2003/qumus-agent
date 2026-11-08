"""Utilities for mirroring fake instrument status to a JSON file."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from common.services.instrument_status_service import InstrumentStatusService, get_instrument_status_service


STATUS_DIR = Path(__file__).resolve().parent / "status"
STATUS_FILE = STATUS_DIR / "true_instrument_status.json"
_WRITE_LOCK = threading.Lock()


def mirror_true_status(service: Optional[InstrumentStatusService] = None) -> Path:
    """Persist the latest true instrument state to the fake status JSON file."""

    service = service or get_instrument_status_service()
    snapshot = service.read_true_instrument_state()
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        with open(STATUS_FILE, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2, sort_keys=True)
    return STATUS_FILE


def ensure_status_file(service: Optional[InstrumentStatusService] = None) -> Path:
    """Make sure the fake status file exists with at least the default payload."""

    if STATUS_FILE.exists():
        return STATUS_FILE
    return mirror_true_status(service)


# Initialize the snapshot at import time so the UI can read a file immediately.
ensure_status_file()
