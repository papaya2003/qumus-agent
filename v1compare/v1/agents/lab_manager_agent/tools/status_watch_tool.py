from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from smolagents import Tool
from common.services.instrument_status_service import (
    check_instrument_consistency,
    get_instrument_status_service,
)


class InstrumentStatusWatchTool(Tool):
    name = "monitor_instrument_status"
    description = (
        "Poll instrument/true_instrument status on an interval to observe live changes. " "Returns a compact JSON log and optionally records each sample to instrument_status_log."
    )
    inputs = {
        "interval_sec": {
            "type": "number",
            "description": "Seconds to wait between polls.",
            "default": 1.0,
            "nullable": True,
        },
        "samples": {
            "type": "integer",
            "description": "How many snapshots to collect.",
            "default": 10,
            "nullable": True,
        },
        "include_cv": {
            "type": "boolean",
            "description": "Whether to include CV status snapshots.",
            "default": False,
            "nullable": True,
        },
        "log": {
            "type": "boolean",
            "description": "Whether to append each poll into instrument_status_log for later inspection.",
            "default": True,
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self) -> None:
        super().__init__()
        self._status = get_instrument_status_service()

    def forward(
        self,
        interval_sec: float = 1.0,
        samples: int = 10,
        include_cv: bool = False,
        log: bool = True,
    ) -> str:
        # Handle optional inputs robustly in case None is passed
        interval = max(float(interval_sec if interval_sec is not None else 1.0), 0.05)
        total = max(int(samples if samples is not None else 10), 1)
        do_log = log if log is not None else True

        snapshots: List[Dict[str, Any]] = []

        for idx in range(total):
            snapshot: Dict[str, Any] = {
                "idx": idx,
                "ts": time.time(),
                "instrument": self._status._read(self._status.instrument_path),
                "true_instrument": self._status._read(self._status.true_instrument_path),
                "consistency": check_instrument_consistency(),
            }
            if include_cv:
                snapshot["cv"] = self._status._read(self._status.cv_path)

            if do_log:
                try:
                    payload = {"type": "monitor", **snapshot}
                    self._status.log_action("instrument_status_poll", payload)
                except Exception:
                    # Logging failure should not break monitoring
                    pass

            snapshots.append(snapshot)
            if idx < total - 1:
                time.sleep(interval)

        return json.dumps({"samples": snapshots}, ensure_ascii=False)
