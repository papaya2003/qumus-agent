"""Normal-profile loading, bounded recovery and fresh terminal process evidence."""

import pytest
from test_reactor_workflow import CHIP, call, ready

from qumus2.app import build_services
from qumus2.config import load_yaml, simulation_profile
from qumus2.core.types import Pose
from qumus2.records.plan_completion import unmet_reactor_outcome


def normal_reactor(seed):
    cfg = load_yaml("testbench.yaml")
    # Explicit prepared fixture isolates mechanics; the preparation bridge has
    # separate end-to-end checks starting with integrated empty stock.
    cfg["reactor"] = {"enabled": True, "synthetic_prepared_pairs": True}
    return build_services(testbench_config=cfg, seed=seed, use_openai=False)


def wait(s, rid):
    return call(s, "reactor", "wait_job", {"job_id": rid, "timeout_s": 30})


@pytest.mark.parametrize("seed", range(8))
def test_normal_error_profile_loads_with_bounded_measured_acquisition(seed):
    s = normal_reactor(seed)
    ready(s)
    submitted = call(s, "reactor", "load_reactor_chip", {"chip_id": CHIP})
    assert submitted.status == "pending", submitted
    rid = submitted.data["job_id"]
    result = wait(s, rid)
    for _ in range(2):
        if result.status in ("ok", "corrected"):
            break
        ready(s)
        recovered = call(s, "reactor", "recover_reactor_load", {
            "job_id": rid, "reason": "Retry the cold, measured placement under its original booking"})
        assert recovered.status == "pending", recovered
        assert recovered.data["job_id"] == rid
        result = wait(s, rid)
    assert result.status in ("ok", "corrected"), result
    assert len([r for r in s.store.list_runs() if r["flow"] == "load_reactor_chip"]) == 1
    assert s.gate.microscope("reactor_microscope").capture().meta["reactor"]["pair_observation"]["visible"]
    assert s.store.get_sample(CHIP)["location"] == "reactor_port"
    assert not s.store.reserved_samples()
    events = s.store.events_after(0, limit=100000)
    assert not any(e.kind == "safety.violation" for e in events)
    assert sum(e.kind == "step.corrected" for e in events) <= 3 * 8
    for e in events:
        if e.kind == "reactor.acquisition":
            assert len(e.payload["positions"]) <= 121 * 4
    assert s.store.spent_usd() == 0


def test_cold_camera_recovery_uses_same_job_and_reservations():
    s = normal_reactor(7)
    ready(s)
    scope = s.lab.devices.get("reactor_microscope")
    scope.offline = True
    rid = call(s, "reactor", "load_reactor_chip", {"chip_id": CHIP}).data["job_id"]
    assert wait(s, rid).status == "needs_attention"
    assert CHIP in s.store.reserved_samples()
    scope.offline = False
    ready(s)
    result = call(s, "reactor", "recover_reactor_load", {"job_id": rid, "reason": "Camera connection restored"})
    assert result.status == "pending", result
    assert wait(s, rid).status in ("ok", "corrected")
    assert len([r for r in s.store.list_runs() if r["flow"] == "load_reactor_chip"]) == 1


def test_invisible_pair_parks_at_search_bound_without_claiming_success():
    s = normal_reactor(7)
    ready(s)
    scope = s.lab.devices.get("reactor_microscope")
    original = scope.capture

    def invisible():
        frame = original()
        m = frame.meta["reactor"]
        m["visible"] = False
        m["pair_observation"]["visible"] = False
        m["reason"] = "no_visible_pair"
        return frame

    scope.capture = invisible
    rid = call(s, "reactor", "load_reactor_chip", {"chip_id": CHIP}).data["job_id"]
    assert wait(s, rid).status == "needs_attention"
    acquisition = [e for e in s.store.events_after(0, limit=100000) if e.kind == "reactor.acquisition"]
    assert len(acquisition) == 1 and len(acquisition[0].payload["positions"]) == 121
    assert s.gate.heater("reactor_heater").get_target() is None
    assert s.gate.gas_flow("reactor_flow").get_target() == 0


def test_unload_emits_current_measured_process_evidence():
    s = normal_reactor(7)
    ready(s)
    rid = call(s, "reactor", "load_reactor_chip", {"chip_id": CHIP}).data["job_id"]
    assert wait(s, rid).status in ("ok", "corrected")
    rid = call(s, "reactor", "unload_reactor_chip", {"chip_id": CHIP}).data["job_id"]
    assert wait(s, rid).status in ("ok", "corrected")
    e = [e for e in s.store.events_after(0, limit=100000) if e.kind == "reactor.process"][-1]
    assert e.run_id == rid and e.payload["chip_id"] == CHIP
    assert e.payload["location"] == "reactor_chip_slot_1"
    assert e.payload["temperature_target_c"] is None and e.payload["flow_target_sccm"] == 0
    assert e.payload["temperature_c"] <= 80 and e.payload["flow_sccm"] <= 0.5
    assert e.payload["insert_y_mm"] == 0
    assert e.payload["timestamp"] == s.clock.now()


@pytest.mark.parametrize("prepared", [False, True])
def test_verify_stored_carrier_refreshes_chip_and_project_bound_proof_after_shutdown(prepared):
    cfg = simulation_profile()
    cfg["reactor"]["synthetic_prepared_pairs"] = prepared
    s = build_services(testbench_config=cfg, use_openai=False)
    ready(s)
    original = s.store.get_sample(CHIP)
    wanted = {"chip_id": CHIP, "disposition": "cold_storage", "temperature_target_c": None,
              "flow_target_sccm": 0, "rationale": "Verify the already stored carrier"}
    assert call(s, "hands", "verify_reactor_state", {"chip_id": CHIP}).status == "denied"
    for attempt in range(2):
        rid = call(s, "reactor", "verify_reactor_state", {"chip_id": CHIP}).data["job_id"]
        assert wait(s, rid).status == "ok"
        assert s.store.get_run(rid)["project_id"] == s.project_id
        assert not unmet_reactor_outcome(s.store, s.lab.bench, wanted, project_id=s.project_id)
        assert unmet_reactor_outcome(s.store, s.lab.bench, wanted, project_id="different_project")
        assert unmet_reactor_outcome(s.store, s.lab.bench, {**wanted, "chip_id": "reactor_chip_02"}, project_id=s.project_id)
        events = [e for e in s.store.events_after(0, limit=100000) if e.run_id == rid]
        assert not any(e.kind == "device.command" for e in events)
        proof = next(e for e in events if e.kind == "reactor.process")
        assert proof.payload["source"] == "guarded_device_feedback"
        assert proof.payload["location"] == original["location"] and proof.payload["insert_y_mm"] == 0
        assert unmet_reactor_outcome(s.store, s.lab.bench, wanted, project_id=s.project_id, after_event_id=proof.id)
        if attempt == 0:
            shutdown = call(s, "reactor", "shutdown_reactor").data["job_id"]
            assert wait(s, shutdown).status == "ok"
            assert any("later" in f for f in unmet_reactor_outcome(s.store, s.lab.bench, wanted, project_id=s.project_id))
    after = s.store.get_sample(CHIP)
    assert (after["location"], after["status"], after["props"]) == (original["location"], original["status"], original["props"])
    assert s.store.spent_usd() == 0


@pytest.mark.parametrize(("device", "method", "reading"), [
    ("reactor_heater", "read_temperature", 81),
    ("reactor_flow", "read_flow", 1),
    ("reactor_heater", "get_target", 30),
    ("reactor_insert", "get_pose", Pose(x=0, y=1)),
])
def test_stored_state_verification_refuses_non_cold_feedback_without_emitting_proof(monkeypatch, device, method, reading):
    s = build_services(testbench_config=simulation_profile(), use_openai=False)
    ready(s)
    # Inject measured-device failures at the readback boundary, not fake success.
    monkeypatch.setattr(s.lab.devices.get(device), method, lambda: reading)
    rid = call(s, "reactor", "verify_reactor_state", {"chip_id": CHIP}).data["job_id"]
    assert wait(s, rid).status == "needs_attention"
    events = [e for e in s.store.events_after(0, limit=100000) if e.run_id == rid]
    assert not any(e.kind in {"reactor.process", "device.command"} for e in events)


def test_parked_state_verification_recovers_by_readback_without_restoring_heat(monkeypatch):
    s = build_services(testbench_config=simulation_profile(), use_openai=False)
    ready(s)
    heater = s.lab.devices.get("reactor_heater")
    with monkeypatch.context() as fault:
        fault.setattr(heater, "get_target", lambda: 30)
        rid = call(s, "reactor", "verify_reactor_state", {"chip_id": CHIP}).data["job_id"]
        assert wait(s, rid).status == "needs_attention"
    after = s.store.events_after(0, limit=100000)[-1].id
    result = call(s, "reactor", "recover_reactor_preparation", {"job_id": rid, "reason": "Target readback fault resolved; verify actual cold state"})
    assert result.status in {"pending", "ok", "corrected"}, result
    assert wait(s, rid).status in {"ok", "corrected"}
    assert heater.get_target() is None
    events = s.store.events_after(after, limit=100000)
    assert any(e.kind == "reactor.process" and e.run_id == rid for e in events)
    assert not any(e.kind == "device.command" for e in events)


def test_stored_state_verification_rejects_unknown_location_and_reports_status_contracts():
    s = build_services(testbench_config=simulation_profile(), use_openai=False)
    ready(s)
    s.store.upsert_sample(CHIP, "chip", props={"location_uncertain": True})
    before = len(s.store.list_runs())
    assert call(s, "reactor", "verify_reactor_state", {"chip_id": CHIP}).status == "error"
    assert call(s, "reactor", "verify_reactor_state", {"chip_id": "chip_001"}).status == "error"
    assert len(s.store.list_runs()) == before
    contracts = call(s, "orchestrator", "get_reactor_setup").data["completion_contracts"]
    assert contracts["prepare_reactor_pair"]["carrier"]["status"] == "reactor_pair_prepared"
    assert contracts["prepare_reactor_pair"]["input_flakes"]["status"] == "stacked"
    assert contracts["reactor_reaction"]["carrier"]["status"] == "reactor_processed"
    assert "status" not in contracts["inspect_reactor_product"]["carrier"]
