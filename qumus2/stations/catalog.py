"""Which module runs which job.

The stations list their jobs in contract v1 `describe().commands` (a whole-job
action is a command whose body is `{"action": flow, ...params, "job": {...}}`).
Before a station answers, the brain uses the known catalogue below, copied from
the simulated laboratory (qumus-simlab) so plans can be checked offline. A
connected station's describe() overrides it.
"""

from __future__ import annotations

import threading
from typing import Any

#: [default] whole-job actions and the module that offers them (qumus-simlab layout)
KNOWN_FLOWS: dict[str, str] = {
    "exfoliate_chips": "exfoliation",
    "place_chip_on_transfer_stage": "stacking",
    "search_chip": "stacking",
    "revisit_chip": "stacking",
    "stack_device": "stacking",
    "cool_experiment_stages": "stacking",
    "inspect_bench": "stacking",
    "prepare_reactor_pair": "stacking",
    "prepare_material_metal": "stacking",
    "assess_reactor_inputs": "reactor",
    "inspect_reactor_product": "reactor",
    "load_reactor_chip": "reactor",
    "unload_reactor_chip": "reactor",
    "reactor_growth": "reactor",
    "reactor_reaction": "reactor",
    "reactor_material_metal": "reactor",
    "verify_reactor_state": "reactor",
    "hold_reactor_observation": "reactor",
    "observe_reactor": "reactor",
    "position_reactor_slot": "reactor",
    "shutdown_reactor": "reactor",
}

#: job controls every module serves; not jobs themselves
JOB_CONTROLS = frozenset({
    "job_cancel", "job_retry", "job_reverify", "job_resume", "job_resume_all",
    "job_review_waiting", "job_status", "complete_experiment", "put_records",
})


class FlowCatalog:
    """flow name -> module; filled from describe() when stations answer."""

    def __init__(self, known: dict[str, str] | None = None) -> None:
        self._known = dict(KNOWN_FLOWS if known is None else known)
        self._described: dict[str, str] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def learn(self, module: str, commands: dict[str, Any]) -> None:
        """Record the job actions one station's describe() lists."""
        with self._lock:
            for name in list(self._described):
                if self._described[name] == module:
                    del self._described[name]
            for name, spec in commands.items():
                if name in JOB_CONTROLS:
                    continue
                if name in self._known or getattr(spec, "raw", {}).get("job"):
                    self._described[name] = module
                    self._schemas[name] = getattr(spec, "params", None) or {}

    def module_of(self, flow: str) -> str | None:
        with self._lock:
            return self._described.get(flow) or self._known.get(flow)

    def flow_names(self, module: str | None = None) -> list[str]:
        with self._lock:
            merged = {**self._known, **self._described}
        return sorted(f for f, m in merged.items() if module is None or m == module)

    def schema(self, flow: str) -> dict[str, Any] | None:
        with self._lock:
            return self._schemas.get(flow)


#: process-wide catalogue (plan checks use it before any station is connected)
CATALOG = FlowCatalog()
