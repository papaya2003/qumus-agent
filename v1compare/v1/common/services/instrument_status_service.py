from __future__ import annotations

import copy
import json
import time
from typing import Any, Callable

from smolagents import Tool

from database.db_utils import init_db, material_session_scope
from database import models
from database.repositories.instrument_status_repository import InstrumentStatusRepository, get_status_repository
from .agent_run_log import AgentRunLogger


class InstrumentStatusService:

    def __init__(
        self,
        instrument_path: str | None = None,
        true_instrument_path: str | None = None,
        cv_path: str | None = None,
        status_repo: InstrumentStatusRepository | None = None,
    ):
        init_db()
        self._status_repo = status_repo or get_status_repository()

        # Keep existing parameter shape (still allow custom key names)
        self.instrument_path = instrument_path or "instrument"
        self.true_instrument_path = true_instrument_path or "true_instrument"
        self.cv_path = cv_path or "cv"

        # Initialize default status payloads
        stage_default = {
            "occupancy": "empty",
            "raw_carrier_home": None,
            "chip_home": None,
            "chip_storage": None,
            "raw_material_slots": ["free"] * 6,
            "exfoliated_chip_slots": ["free"] * 14,
        }

        def _instrument_defaults() -> dict[str, Any]:
            return {
                "robot_arm_left": {},
                "robot_arm_right": {},
                "vacuum": {"state": "unknown"},
                "motors": {"motor1_power": None, "motor2_power": None, "last_action": None},
                "heating": {"voltage": None, "power_on": None},
                "exfoliator": {"power_on": None, "last_action": None},
                "stage": copy.deepcopy(stage_default),
                # CV-fed runtime instruments.
                "exfoliation_stage": {},
                "transfer_stage": {},
                "stamp_holder": {},
                "scotch_tape_crystal": {},
                "updated_at": None,
            }

        instrument_defaults = _instrument_defaults()
        self._status_repo.ensure_status(self.instrument_path, instrument_defaults)
        self._status_repo.ensure_status(self.true_instrument_path, copy.deepcopy(instrument_defaults))
        cv_defaults = {
            "last_request_item_type": None,
            "selected": None,
            "available_counts": {},
            "stage": {"exfoliated_chip_slots": ["free"] * 14},
            "updated_at": None,
        }
        self._status_repo.ensure_status(self.cv_path, cv_defaults)

        # Existing DB rows may predate schema additions.
        self._ensure_schema_defaults(self.instrument_path, instrument_defaults)
        self._ensure_schema_defaults(self.true_instrument_path, instrument_defaults)
        self._ensure_schema_defaults(self.cv_path, cv_defaults)

    def _read(self, name: str) -> dict[str, Any]:
        return self._status_repo.read_status(name)

    def _write(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(data)
        payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        return self._status_repo.write_status(name, payload)

    def _merge(self, base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        # Shallow/deep merge: only go one level deep for dict values
        merged = dict(base)
        for k, v in updates.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        return merged

    def _merge_defaults(self, current: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        """Recursively add missing keys from defaults while preserving existing values."""
        merged = copy.deepcopy(current)
        for key, default_value in defaults.items():
            if key not in merged:
                merged[key] = copy.deepcopy(default_value)
                continue
            if isinstance(merged[key], dict) and isinstance(default_value, dict):
                merged[key] = self._merge_defaults(merged[key], default_value)
        return merged

    def _ensure_schema_defaults(self, name: str, defaults: dict[str, Any]) -> None:
        current = self._read(name)
        merged = self._merge_defaults(current, defaults)
        if merged != current:
            self._write(name, merged)

    def read_instrument_state(self) -> dict[str, Any]:
        return self._read(self.instrument_path)

    def read_true_instrument_state(self) -> dict[str, Any]:
        return self._read(self.true_instrument_path)

    def update_instrument(self, updates: dict[str, Any], event: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self._read(self.instrument_path)
        merged = self._merge(current, updates)
        merged = self._write(self.instrument_path, merged)
        if event:
            self.log_action(event, {"type": "instrument", "updates": updates, **(context or {})})
        return merged

    def update_true_instrument(self, updates: dict[str, Any], event: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self._read(self.true_instrument_path)
        merged = self._merge(current, updates)
        merged = self._write(self.true_instrument_path, merged)
        if event:
            self.log_action(event, {"type": "true_instrument", "updates": updates, **(context or {})})
        return merged

    def update_cv(self, updates: dict[str, Any], event: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self._read(self.cv_path)
        merged = self._merge(current, updates)
        merged = self._write(self.cv_path, merged)
        if event:
            self.log_action(event, {"type": "cv", "updates": updates, **(context or {})})
        return merged

    def log_action(self, event: str, payload: dict[str, Any]) -> None:
        record_payload = {"event": event, "ts": time.time(), **(payload or {})}
        self._status_repo.append_status_log(event, record_payload.get("type"), record_payload)
        try:
            AgentRunLogger().append_run_log(event=event, payload=payload)
        except Exception:
            pass

    def verify(self, expected: dict[str, Any]) -> bool:
        """Verify instrument state includes the expected subset."""

        state = self._read(self.instrument_path)

        def _subset(a: dict[str, Any], b: dict[str, Any]) -> bool:
            for k, v in b.items():
                if isinstance(v, dict):
                    if not isinstance(a.get(k), dict) or not _subset(a.get(k, {}), v):
                        return False
                else:
                    if a.get(k) != v:
                        return False
            return True

        return _subset(state, expected)

    def wait_until(self, predicate: Callable[[], bool], timeout: float = 5.0, poll_interval: float = 0.1) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if predicate():
                return True
            time.sleep(poll_interval)
        return False

    def after_action(self, event: str, instrument_updates: dict[str, Any] | None = None, cv_updates: dict[str, Any] | None = None, expect: dict[str, Any] | None = None) -> bool:
        if instrument_updates:
            self.update_instrument(instrument_updates, event=event)
        if cv_updates:
            self.update_cv(cv_updates, event=event)
        if expect:
            return self.verify(expect)
        return True

    # Preset flow verifiers that can be called directly
    def expect_vacuum_on(self) -> bool:
        return self.verify({"vacuum": {"state": "on"}})

    def expect_single_item_on_stage(self) -> bool:
        # Placeholder; extend with real occupancy logic. Currently always True.
        return True

    def get_latest_status(self, key: str | None = None) -> Any:
        """
        Retrieve the latest status for a specific key.
        Prioritizes 'true_instrument' (sensor data) over 'instrument' (agent belief).
        """
        # Data merge strategy: True Instrument > Instrument
        inst = self._read(self.instrument_path)
        true_inst = self._read(self.true_instrument_path)
        
        # Merge top-level keys
        merged = {**inst, **true_inst}
        
        if key:
            return merged.get(key)
        return merged


_default_recorder: InstrumentStatusService | None = None


def get_instrument_status_service() -> InstrumentStatusService:
    global _default_recorder
    if _default_recorder is None:
        _default_recorder = InstrumentStatusService()
    return _default_recorder


def build_status_summary(max_length: int = 600) -> str:
    """Build a short status summary string for memory injection or display."""
    store = get_instrument_status_service()
    inst = store._read(store.instrument_path)
    cv = store._read(store.cv_path)
    parts: list[str] = []
    vac = inst.get("vacuum", {})
    motors = inst.get("motors", {})
    heat = inst.get("heating", {})
    exf = inst.get("exfoliator", {})
    arm = inst.get("robot_arm_left", {})
    parts.append(f"vacuum={vac.get('state')}")
    parts.append(f"motors(m1={'on' if motors.get('motor1_power') else 'off'}, m2={'on' if motors.get('motor2_power') else 'off'}, act={motors.get('last_action')})")
    parts.append(f"heating(V={heat.get('voltage')}, on={heat.get('power_on')})")
    parts.append(f"exfoliator(on={exf.get('power_on')}, act={exf.get('last_action')})")
    if position := arm.get("position"):
        parts.append(f"arm_left_position={position}")
    right_arm = inst.get("robot_arm_right", {})
    if right_position := right_arm.get("position"):
        parts.append(f"arm_right_position={right_position}")
    if cv.get("selected"):
        parts.append(f"cv_selected={cv.get('selected')}")
    slot_note = _build_slot_summary()
    if slot_note:
        parts.append(slot_note)
    summary = " | ".join(parts)
    return summary[:max_length]


def _build_slot_summary(limit: int = 3) -> str:
    """Summarize occupied chip slots from DB chip_slot/chip tables."""
    try:
        with material_session_scope() as session:
            rows = session.query(models.ChipSlot).filter(models.ChipSlot.chip.isnot(None)).all()
            if not rows:
                return ""
            entries = []
            for slot in rows:
                chip_label = slot.chip.label if slot.chip else "?"
                entries.append(f"{slot.label}={chip_label}")
            text = ",".join(entries[:limit])
            if len(entries) > limit:
                text += ",…"
            return f"slots[{text}]"
    except Exception:
        return ""


def status_step_callback(memory_step, agent=None):
    """After each step, append the status summary to observations for next-step context."""
    try:
        summary = build_status_summary()
        prefix = "\nStatus summary: "
        memory_step.observations = (memory_step.observations or "") + prefix + summary
    except Exception:
        # Do not affect main flow
        pass


def _approx_equal(a: float, b: float, tol: float = 2.0) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except Exception:
        return False


def compute_cv_consistency() -> dict[str, Any]:
    """Compare expected instrument state with CV observations (if provided).

    Rule: compare fields only when both sides provide them (skip missing without error):
    - stage.exfoliation occupancy
    - raw/exfoliated slot arrays
    - robot_arm_left.position (approximate compare with ±2.0 tolerance)
    Returns: { ok: bool, mismatches: [str] }
    """
    store = get_instrument_status_service()
    inst = store._read(store.instrument_path)
    cv = store._read(store.cv_path)
    mismatches: list[str] = []

    # 1) Stage occupancy
    inst_stage = (inst.get("stage") or {}).get("exfoliation")
    cv_stage = (cv.get("stage") or {}).get("exfoliation")
    if inst_stage is not None and cv_stage is not None:
        if inst_stage != cv_stage:
            mismatches.append(f"exfoliation_stage mismatch: inst={inst_stage}, cv={cv_stage}")

    # 2) Slot arrays (compare element-wise when arrays exist)
    def _cmp_slots(name: str):
        a = (inst.get("stage") or {}).get(name)
        b = (cv.get("stage") or {}).get(name)
        if isinstance(a, list) and isinstance(b, list):
            L = min(len(a), len(b))
            for i in range(L):
                if a[i] != b[i]:
                    mismatches.append(f"{name}[{i}] mismatch: inst={a[i]}, cv={b[i]}")

    _cmp_slots("raw_material_slots")
    _cmp_slots("exfoliated_chip_slots")

    # 3) Robot arm position (if CV provides)
    arm_position = (inst.get("robot_arm_left") or {}).get("position")
    cv_arm = (cv.get("robot_arm_left") or {}).get("position")
    if isinstance(arm_position, dict) and isinstance(cv_arm, dict):
        for key in ("x", "y"):
            if key in arm_position and key in cv_arm and not _approx_equal(arm_position[key], cv_arm[key]):
                mismatches.append(f"robot_arm_left.{key} mismatch: inst={arm_position[key]}, cv={cv_arm[key]}")

    ok = len(mismatches) == 0
    return {"ok": ok, "mismatches": mismatches}


CONSISTENCY_IGNORE_FIELDS = {
    "updated_at",
    "timestamp",
    "last_calibrated",
    "last_seen_frame",
}


def _subset_approx(a: Any, b: Any, path: str, mismatches: list[str], tol: float = 2.0) -> None:
    if isinstance(b, dict):
        if not isinstance(a, dict):
            return
        for k, v in b.items():
            if k in CONSISTENCY_IGNORE_FIELDS:
                continue
            # Compare only overlapping keys to avoid false alarms for newly added fields.
            if k not in a:
                continue
            _subset_approx(a.get(k), v, f"{path}.{k}" if path else k, mismatches, tol)
        return
    if a is None or b is None:
        return
    # Unknown expected values should not produce mismatch noise.
    if isinstance(a, str) and a.strip().lower() in {"unknown", "n/a", "unset"}:
        return
    if isinstance(b, (int, float)) and isinstance(a, (int, float)):
        if not _approx_equal(float(a), float(b), tol=tol):
            mismatches.append(f"{path} mismatch: inst={a}, true={b}")
        return
    if a != b:
        mismatches.append(f"{path} mismatch: inst={a}, true={b}")


def check_instrument_consistency(tol: float = 2.0) -> dict[str, Any]:
    """Compare expected instrument state with the read-back true_instrument state.

    Rule: compare only fields present in true and also in instrument; numeric fields compared approximately.
    Returns: { ok: bool, mismatches: [str] }
    """
    store = get_instrument_status_service()
    inst = store._read(store.instrument_path)
    truth = store._read(store.true_instrument_path)
    mismatches: list[str] = []

    # Only validate subset provided by truth, approximately
    _subset_approx(inst, truth, path="", mismatches=mismatches, tol=tol)

    ok = len(mismatches) == 0
    return {"ok": ok, "mismatches": mismatches}


def instrument_consistency_callback(memory_step, agent=None):
    """After each step, run an instrument vs true_instrument consistency check and append a note."""
    try:
        result = check_instrument_consistency()
        get_instrument_status_service().log_action("instrument_consistency_check", result)
        note = "\nInstrument consistency: OK" if result["ok"] else "\nInstrument consistency mismatches: " + "; ".join(result["mismatches"])
        memory_step.observations = (memory_step.observations or "") + note
    except Exception:
        pass


class ReadStatusTool(Tool):
    name = "read_status"
    description = "Strictly read-only: return the current instrument/cv/true_instrument status snapshot without sending any hardware, heater, relay, reset, cleanup, or power command. Source can be 'instrument', 'cv', 'true_instrument', or 'all'. Returns a compact JSON string."
    inputs = {"source": {"type": "string", "description": "instrument | cv | true_instrument | all", "nullable": True}}
    output_type = "string"

    def __init__(self):
        super().__init__()

    def forward(self, source: str = "all") -> str:
        store = get_instrument_status_service()
        source = (source or "all").lower()
        if source == "instrument":
            return json.dumps({"instrument": store._read(store.instrument_path)}, ensure_ascii=False)
        elif source == "cv":
            return json.dumps({"cv": store._read(store.cv_path)}, ensure_ascii=False)
        elif source == "true_instrument":
            return json.dumps({"true_instrument": store._read(store.true_instrument_path)}, ensure_ascii=False)
        else:
            return json.dumps(
                {"instrument": store._read(store.instrument_path), "true_instrument": store._read(store.true_instrument_path), "cv": store._read(store.cv_path)}, ensure_ascii=False
            )


class CheckInstrumentConsistencyTool(Tool):
    name = "check_instrument_consistency"
    description = "Compare expected instrument status with true instrument readings. Returns mismatches if any."
    inputs = {"tol": {"type": "number", "description": "Tolerance for numeric comparison.", "default": 2.0, "nullable": True}}
    output_type = "string"

    def __init__(self):
        super().__init__()

    def forward(self, tol: float = 2.0) -> str:
        actual_tol = tol if tol is not None else 2.0
        result = check_instrument_consistency(tol=actual_tol)
        get_instrument_status_service().log_action("instrument_consistency_check", result)
        if result["ok"]:
            return "✅ Instrument vs True OK"
        return "❌ Mismatches: " + "; ".join(result["mismatches"])


# Backwards-compatible aliases
StatusStore = InstrumentStatusService  # Backwards-compatible alias
