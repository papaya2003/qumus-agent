"""Deterministic material-metal simulator integration, including negative paths.

A module fixture exfoliates, searches, reuses optical evidence and transfers layers.
Restored copies retain those device/world checkpoints; no preparation is seeded.
Its explicit ideal condition does not qualify ordinary PC/MICA04 outcomes,
laboratory calibration or experimental acceptance.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import qumus2.config as configuration
from qumus2.agents.gateway import ToolGateway
from qumus2.agents.tooling import default_registry
from qumus2.app import build_lab_side
from qumus2.core.errors import PreconditionFailed
from qumus2.records.material_metal_simulation import (
    device_preparation_errors,
    simulation_result_errors,
)
from qumus2.records.plan_completion import invalid_flow_sample_statuses
from qumus2.records.reactor_requirements import acceptance_errors, requirements_record
from qumus2.testbench.persist import save_lab_state

CHIP = "reactor_chip_01"


def call(s, role, tool, args=None):
    return asyncio.run(s.gateway.call(role, tool, args or {}))[0]


@pytest.fixture(scope="module", params=["PC"])
def prepared_snapshot(tmp_path_factory, request):
    folder = tmp_path_factory.mktemp(f"material-metal-{request.param}")
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("device_diagnostic", scripts / "check_material_metal_devices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = folder / "baseline"
    baseline.mkdir()

    def capture(s, prefix=""):
        with sqlite3.connect(baseline / f"{prefix}qumus.db") as connection:
            s.store._conn.backup(connection)
        save_lab_state(s.lab, s.clock, baseline / f"{prefix}world.pkl")
        if not prefix:
            try:
                diagnostic = module.save_private_evaluation(s, baseline, module.source_fingerprint(), conditions,
                    recorder_final_read_completed=False, capture_phase="after_preparation_checkpoint")
            except Exception as exc:
                diagnostic = {"status": "capture_failed", "type": type(exc).__name__, "message": str(exc)}
            if diagnostic["status"] != "captured":
                print(json.dumps({"private_preparation_evaluation_capture": diagnostic}))

    # Fresh central ideal condition; chemistry success is a separate declared assumption.
    overrides = {"success_probability": 1}
    cfg, conditions = module.diagnostic_configuration("ideal_stacking_v1", overrides, seed=8)
    assert asyncio.run(module.record(folder / "source-run", request.param,
        overrides=overrides, seed=8, simulation_condition="ideal_stacking_v1",
        after_preparation=capture, before_preparation=lambda s: capture(s, "before-")))
    return baseline, cfg, request.param


@pytest.fixture
def prepared(prepared_snapshot, tmp_path):
    return restore_snapshot(prepared_snapshot, tmp_path)


def restore_snapshot(prepared_snapshot, tmp_path, prefix=""):
    baseline, cfg, film = prepared_snapshot
    shutil.copy2(baseline / f"{prefix}qumus.db", tmp_path / "qumus.db")
    shutil.copy2(baseline / f"{prefix}world.pkl", tmp_path / "world.pkl")
    side = build_lab_side(tmp_path / "qumus.db", testbench_config=cfg, state_path=tmp_path / "world.pkl")
    project_id = side.store.list_projects()[0]["id"]
    gateway = ToolGateway(default_registry(), side.store, side.jobs, project_id=project_id, bench=side.lab.bench)
    s = SimpleNamespace(**vars(side), project_id=project_id, gateway=gateway, film=film)
    if not prefix:
        assert not device_preparation_errors(s.store, project_id, CHIP)
    return s


def start_reaction(s, **args):
    objective = {"chip_id": CHIP, "requirements_event_id": requirements_record(s.store, s.project_id)["event_id"],
        "objective": "Observe the frozen qualitative simulation criterion, preserve hBN and return cold",
        "final_disposition": "cold_storage"}
    decision = s.gateway.delegate("reactor", task="s10: Execute the guarded device simulation", objective=objective,
                                  require_sample_outcomes=True)
    assert decision.allow, decision.reason
    s.gateway.delegation_started("reaction", "reactor", "s10: Execute the guarded device simulation")
    result = call(s, "reactor", "reactor_material_metal", {"chip_id": CHIP, **args})
    assert result.status == "pending", result
    rid = result.data["job_id"]
    waited = call(s, "reactor", "wait_job", {"job_id": rid, "timeout_s": 120})
    return rid, waited


def finish(s, rid):
    result = call(s, "reactor", "submit_material_metal_result", {"chip_id": CHIP, "reaction_job_id": rid})
    s.gateway.delegation_finished("reaction")
    return result


def test_actual_device_pipeline_accepts_simulation_and_preserves_hbn(prepared):
    s = prepared
    events = s.store.events_after(0, limit=100000)
    selection = next(e.payload for e in events if e.kind == "scripted.selection")
    assert selection["chosen"] == selection["ranked_pairs"][0]["flake_ids"]
    assert selection["ranked_pairs"][0]["nominal_clearance_mm"] > selection["ranked_pairs"][-1]["nominal_clearance_mm"]
    assert not any(e.kind == "job.submitted" and e.payload["flow"] == "assess_reactor_inputs" for e in events)
    preparation_runs = [run for run in s.store.list_runs() if run["flow"] == "prepare_material_metal"
                        and run["status"] == "succeeded"]
    assert len(preparation_runs) == 1
    preparation_run = preparation_runs[0]
    phases = preparation_run["meta"]["normal_transfer"]["phases"]
    for fid in selection["chosen"]:
        phase = phases[f"v1_pick_up_{fid}"]
        coverage = phase["coverage"]
        assert phase["complete"] and coverage["covered"]
        assert Path(coverage["reference"]["image"]).is_file() and Path(coverage["current"]["image"]).is_file()
        assert coverage["transfer_confirmed"] is False  # Difference coverage remains a process observation.
    endpoints = [event.payload for event in events if event.kind == "stack.pickup_endpoint"
                 and event.run_id == preparation_run["id"]]
    assert [endpoint["flake_id"] for endpoint in endpoints] == selection["chosen"]
    assert all(endpoint["observation"]["measurements"]["transfer_confirmed"] for endpoint in endpoints)
    metal = s.store.get_sample(CHIP)["props"]["metal_geometry"]
    assert metal["image"] and metal["outline_mm"] and metal["source"] == "simulated_optical_outline"
    assert s.store.get_sample(CHIP)["status"] == "material_metal_prepared"
    preparation = {"id": "s9", "operation": "prepare_material_metal",
                   "sample_outcomes": [{"sample_id": CHIP, "status": "material_metal_prepared"}]}
    reaction = {"id": "s10", "operation": "reactor_material_metal",
                "sample_outcomes": [{"sample_id": CHIP, "status": "reactor_processed"}]}
    assert not invalid_flow_sample_statuses(s.store, preparation)
    assert not invalid_flow_sample_statuses(s.store, reaction)
    assert invalid_flow_sample_statuses(s.store, {**reaction, "sample_outcomes": preparation["sample_outcomes"]})
    assert invalid_flow_sample_statuses(s.store, {**preparation, "sample_outcomes": reaction["sample_outcomes"]})
    # Exercise the terminal declaration through real simulated execution and
    # the normal gateway check, not just a hand-written ledger fixture.
    plan = s.store.get_project(s.project_id)["plan"]
    next(step for step in plan if step["id"] == "s10")["sample_outcomes"] = reaction["sample_outcomes"]
    s.store.set_plan(s.project_id, plan, author="orchestrator", note="Require the actual Reactor terminal status")
    receipt = s.store.get_sample(CHIP)["props"]["material_metal_preparation"]
    reaction_step = next(step for step in plan if step["id"] == "s10")
    reaction_step["operation_args"] = {"chip_id": CHIP, "temperature_c": 300.0, "flow_sccm": 50.0,
        "duration_s": 180.0, "stamp_id": receipt["stamp_id"], "support": s.film,
        "preserve_images": True, "preserve_provenance": True}
    s.store.set_plan(s.project_id, plan, author="orchestrator", note="Bind real reaction and provenance assertions")
    before = deepcopy(s.lab.world.objects[CHIP].flakes)
    assert receipt["stamp_film"] == s.film and receipt["layer_order"] == ["hBN", "Gr", "Pd", "silicon"]
    assert receipt["source_images"] and receipt["release_images"]
    assert s.store.get_sample(receipt["stamp_id"])["status"] == "used"
    assert s.lab.world.stamps[receipt["stamp_id"]].film == (s.film == "mica")
    assert s.store.get_sample(receipt["stamp_id"])["props"]["film"] == ("mica" if s.film == "mica" else None)
    rid, waited = start_reaction(s)
    assert waited.status in ("ok", "corrected"), waited
    result = finish(s, rid)
    assert result.status == "ok", result
    for job_id in (receipt["preparation_job_id"], rid):
        full = s.jobs.status(job_id)
        manifest = full.workflow
        assert manifest["level"] == "assembly" and manifest["name"] == full.flow
        leaves = [leaf for part in manifest["children"] for leaf in part["checkpoints"]]
        assert [(leaf["seq"], leaf["name"]) for leaf in leaves] == [(step["seq"], step["name"]) for step in full.steps]
        assert all(step["status"] == "done" for step in full.steps)
        assert any(event.kind == "job.workflow" for event in s.store.events_after(0, limit=100000, run_id=job_id))
    assert "checkpoints" not in waited.data["workflow"]["children"][0]
    assert s.store.get_sample(CHIP)["status"] == "reactor_processed"
    assert result.data["simulation_accepted"] and not result.data["experiment_accepted"]
    assert not simulation_result_errors(s.store, s.project_id, result.data, bench=s.lab.bench)
    assert not acceptance_errors(s.store, s.project_id)
    from qumus2.records.material_metal_arguments import argument_errors, submission_errors

    run = s.store.get_run(rid)
    assert not argument_errors(s.store, s.project_id, reaction_step, phase="complete", run=run)
    for key, wrong in (("temperature_c", 450.0), ("duration_s", 1.0), ("chip_id", "reactor_chip_02")):
        altered = {**reaction_step, "operation_args": {**reaction_step["operation_args"], key: wrong}}
        assert argument_errors(s.store, s.project_id, altered, phase="complete", run=run)
    for key, wrong in (("temperature_c", 450.0), ("flow_sccm", 30.0), ("duration_s", 1.0)):
        assert submission_errors(s.store, s.project_id, reaction_step, {"chip_id": CHIP, key: wrong}, s.lab.bench)
    assert s.lab.world.objects[CHIP].flakes == before
    assert all(s.store.get_sample(fid)["status"] == "stacked" for fid in receipt["top_first_flake_ids"])

    assert s.lab.world.slot_of(s.lab.world.objects[CHIP]) == s.store.get_sample(CHIP)["location"] == "reactor_chip_slot_1"
    assert s.gate.heater("reactor_heater").get_target() is None
    assert s.gate.gas_flow("reactor_flow").get_target() == 0
    assert all(step["status"] == "done" for step in s.store.get_project(s.project_id)["plan"])
    done = call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "Simulated objective met; no real chemistry claim"})
    assert done.status == "pending", done
    assert call(s, "orchestrator", "wait_job", {"job_id": done.data["job_id"], "timeout_s": 30}).status == "ok"
    done = call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "Simulated objective met; no real chemistry claim"})
    assert done.status == "ok", done
    assert all(r["temperature_c"] < 40 for r in done.data["readings"].values())
    assert not any(e.kind == "safety.violation" for e in s.store.events_after(0, limit=100000))
    assert s.store.spent_usd() == 0
    # A restart must restore accumulated progress and both actual layers, without
    # recreating sources, redrawing success or resetting the exposure clock.
    target = Path(s.store.path).parent / "restored"
    target.mkdir()
    save_lab_state(s.lab, s.clock, target / "world.pkl")
    with sqlite3.connect(target / "qumus.db") as connection:
        s.store._conn.backup(connection)
    cfg = configuration.ideal_stacking_profile()
    cfg["seed"] = 8
    cfg["reactor"]["material_metal"] = receipt["model_assumptions"]
    restored = build_lab_side(target / "qumus.db", testbench_config=cfg, state_path=target / "world.pkl")
    assert restored.lab.world.reactor_state["material_metal_models"] == s.lab.world.reactor_state["material_metal_models"]
    assert restored.lab.world.objects[CHIP].flakes == before
    assert restored.jobs.status(rid).workflow == s.jobs.status(rid).workflow
    assert not acceptance_errors(restored.store, s.project_id)


def test_explicit_chamber_inspection_does_not_claim_storage_observation(prepared):
    s = prepared
    before = len(s.store.list_runs())
    result = call(s, "eyes", "inspect_bench", {"regions": ["reactor"]})
    assert result.status == "ok", result
    measured = result.data["steps"][0]["measurements"]
    assert measured["coverage"]["observed_regions"] == ["reactor"]
    assert "reactor_storage" in measured["coverage"]["unobserved_reactor_regions"]
    assert "reactor_storage" not in measured["empty"]
    complete = call(s, "eyes", "inspect_bench", {})
    assert complete.status == "ok", complete
    measured = complete.data["steps"][0]["measurements"]
    assert not measured["coverage"]["unobserved_reactor_regions"]
    assert any(d["at"] == "reactor_chip_slot_1" for d in measured["regions"]["reactor_storage"])
    assert len(s.store.list_runs()) == before + 2


def test_reactor_submission_checks_captured_process_before_job(prepared):
    s = prepared
    plan = s.store.get_project(s.project_id)["plan"]
    next(step for step in plan if step["id"] == "s10")["operation_args"] = {
        "chip_id": CHIP, "temperature_c": 300.0, "flow_sccm": 50.0, "duration_s": 180.0}
    s.store.set_plan(s.project_id, plan, author="orchestrator", note="Bind process before submission")
    objective = {"chip_id": CHIP, "requirements_event_id": requirements_record(s.store, s.project_id)["event_id"],
        "objective": "Preserve the original qualitative target and return cold", "final_disposition": "cold_storage"}
    assert s.gateway.delegate("reactor", task="s10: Execute the reaction", objective=objective,
                              require_sample_outcomes=True).allow
    s.gateway.delegation_started("bound-reaction", "reactor", "s10: Execute the reaction")
    before = s.store.list_runs(), s.store.list_samples()
    start = s.store.last_event_id()
    result = call(s, "reactor", "reactor_material_metal", {"chip_id": CHIP, "temperature_c": 450})
    assert result.status == "denied" and "temperature_c" in result.summary
    assert (s.store.list_runs(), s.store.list_samples()) == before
    assert not any(e.kind in ("job.submitted", "device.command") for e in s.store.events_after(start))


def test_insufficient_exposure_is_safe_but_never_accepted(prepared):
    s = prepared
    rid, waited = start_reaction(s, temperature_c=22, flow_sccm=0, duration_s=5)
    assert waited.status in ("ok", "corrected"), waited
    result = finish(s, rid)
    assert result.status == "error" and "has not met" in result.summary
    assert s.store.get_sample(CHIP)["props"]["material_metal_observation"]["qualitative_progress"] == 0
    assert s.lab.world.slot_of(s.lab.world.objects[CHIP]) == "reactor_chip_slot_1"
    assert s.store.get_project(s.project_id)["plan"][-1]["status"] != "done"
    assert acceptance_errors(s.store, s.project_id)


def test_observation_failure_during_exposure_disables_targets_and_retains_uncertainty(prepared, monkeypatch):
    s = prepared
    scope = s.lab.devices.get("reactor_microscope")
    capture = scope.capture

    def failed_camera():
        if scope.plant.state["temperature_target_c"] is not None:
            s.lab.faults.inject("device_offline", {"device": "reactor_microscope", "offline": True})
        return capture()

    monkeypatch.setattr(scope, "capture", failed_camera)
    rid, waited = start_reaction(s)
    assert waited.status not in ("ok", "corrected")
    assert s.store.get_sample(CHIP)["props"]["reactor_state_uncertain"]
    assert s.gate.heater("reactor_heater").get_target() is None
    assert s.gate.gas_flow("reactor_flow").get_target() == 0
    assert CHIP in s.store.reserved_samples()
    result = finish(s, rid)
    assert result.status == "error"
    denied = call(s, "reactor", "recover_material_metal_job", {"job_id": rid, "action": "retry", "reason": "Test that interrupted exposure cannot be replayed"})
    assert denied.status == "error" and "cannot be replayed" in denied.summary


def test_pre_exposure_camera_failure_reverifies_then_retries_same_job(prepared):
    s = prepared
    s.lab.faults.inject("device_offline", {"device": "reactor_microscope", "offline": True})
    rid, waited = start_reaction(s)
    assert waited.status not in ("ok", "corrected")
    assert not s.store.get_sample(CHIP)["props"].get("reactor_state_uncertain")
    assert CHIP in s.store.reserved_samples()
    assert s.gate.heater("reactor_heater").get_target() is None
    s.lab.faults.inject("device_offline", {"device": "reactor_microscope", "offline": False})
    recovered = call(s, "reactor", "recover_material_metal_job", {"job_id": rid,
        "action": "reverify", "reason": "Camera is restored before exposure; inspect the original job's postconditions"})
    assert recovered.status in ("ok", "pending"), recovered
    waited = call(s, "reactor", "wait_job", {"job_id": rid, "timeout_s": 120})
    # Reverification alone cannot claim an acquisition that never focused or
    # centred the stack. Its fresh image justifies a reversible optical retry.
    assert waited.status == "needs_attention", waited
    retried = call(s, "reactor", "recover_material_metal_job", {"job_id": rid,
        "action": "retry", "reason": "Restored camera shows an unfocused stack; repeat the cold optical acquisition"})
    assert retried.status == "pending", retried
    waited = call(s, "reactor", "wait_job", {"job_id": rid, "timeout_s": 120})
    assert waited.status in ("ok", "corrected"), waited
    assert finish(s, rid).status == "ok"
    assert not acceptance_errors(s.store, s.project_id)


def test_profile_geometry_goal_and_terminal_tampering_cannot_pass(prepared):
    s = prepared
    rid, waited = start_reaction(s)
    assert waited.status in ("ok", "corrected")
    result = finish(s, rid)
    assert result.status == "ok", result
    original = deepcopy(result.data)
    changed = deepcopy(original)
    changed["observation"]["qualitative_progress"] = 1
    assert simulation_result_errors(s.store, s.project_id, changed)
    changed = deepcopy(original)
    changed["chemistry_verified"] = True
    assert simulation_result_errors(s.store, s.project_id, changed)
    s.store.upsert_sample(CHIP, "chip", props={"metal_coating": {"metal": "Au"}})
    assert acceptance_errors(s.store, s.project_id)
    s.store.upsert_sample(CHIP, "chip", props={"metal_coating": original["goal"]["metal_coating"]})
    flake = s.lab.world.objects[CHIP].flakes[0]
    flake.x_mm += 0.1
    backend = s.lab.devices.get("reactor_microscope").plant.material_metal_backend
    with pytest.raises(PreconditionFailed, match="geometry"):
        backend.model(s.lab.world.objects[CHIP], s.lab.world.reactor_state["material_metal_models"])
    flake.x_mm -= 0.1
    s.gate.gas_flow("reactor_flow").set_flow(0)
    assert any("later device" in error for error in acceptance_errors(s.store, s.project_id))


def test_camera_sampling_does_not_advance_state_and_clipped_or_unfocused_is_unmeasurable(prepared):
    s = prepared
    plant = s.lab.devices.get("reactor_microscope").plant
    backend = plant.material_metal_backend
    model = backend.model(s.lab.world.objects[CHIP], plant.state.setdefault("material_metal_models", {}))
    before = deepcopy(model[0])
    for _ in range(5):
        obs, _ = backend.observation(model, center=(0, 0), fov=(.26, .172), in_focus=True, rotation=0)
    assert model[0] == before
    for center, focus in (((10, 10), True), ((0, 0), False)):
        obs, _ = backend.observation(model, center=center, fov=(.26, .172), in_focus=focus, rotation=0)
        assert obs["qualitative_progress"] is None and not obs["fully_visible"]
    # This failure trajectory is a model-level fixture, not an accepted ledger result.
    model[0]["trajectory"] = "stalled"
    backend.advance(model, 1000, 300, 50, True)
    assert model[0]["qualitative_progress"] < backend.config.minimum_progress
    assert len(s.lab.world.objects[CHIP].flakes) == 2


def test_release_failure_keeps_layers_on_stamp_without_preparation_claim(prepared_snapshot, tmp_path):
    s = restore_snapshot(prepared_snapshot, tmp_path, "before-")
    ids = s.store.get_project(s.project_id)["state"]["reactor_selection"]["confirmed_ids"]
    s.lab.faults.inject("release_fail_next", {"count": 100})
    result = call(s, "hands", "prepare_material_metal", {"chip_id": CHIP, "flake_ids": ids, "stamp_id": "stamp_01"})
    assert result.status == "pending", result
    rid = result.data["job_id"]
    waited = call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 120})
    # Bounded recovery of unrelated placement noise, never replay uncertain release.
    for _ in range(2):
        unfinished = [step for step in s.store.get_steps(rid) if step["status"] not in ("done", "skipped")]
        if not unfinished or unfinished[0]["name"] == "release_material_metal_stack":
            break
        retried = call(s, "hands", "recover_material_metal_job", {"job_id": rid, "action": "retry", "reason": "Repeat a measured reversible placement"})
        assert retried.status == "pending", retried
        waited = call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 120})
    assert waited.status not in ("ok", "corrected")
    assert s.store.get_sample(CHIP)["props"].get("material_metal_preparation") is None
    assert not s.lab.world.objects[CHIP].flakes
    assert [f.id for f, _, _ in s.lab.world.stamps["stamp_01"].stack] == [s.store.get_sample(fid)["props"]["sim_id"] for fid in ids]
    assert all(s.store.get_sample(fid)["location"] == "stamp_01" for fid in ids)
    assert CHIP in s.store.reserved_samples()
    assert s.gate.heater("stamp_heater").get_target() is None
    denied = call(s, "hands", "recover_material_metal_job", {"job_id": rid, "action": "retry", "reason": "Uncertain release is not replayable"})
    assert denied.status == "error" and "cannot be replayed" in denied.summary


def test_paused_exposure_parks_after_restart_without_reloading_or_reexposing(prepared, tmp_path, monkeypatch):
    from qumus2.core.errors import StopRequested
    from qumus2.flows.library.material_metal_device import MaterialMetalWindow
    from qumus2.flows.library.primitives import sleep_watched

    s = prepared
    calls = []

    def interrupted(self, ctx, correction):
        calls.append(ctx.run_id)
        sleep_watched(ctx, 1)  # real simulated exposure before the interruption
        raise StopRequested("Test interruption during the treatment window")

    monkeypatch.setattr(MaterialMetalWindow, "act", interrupted)
    rid, waited = start_reaction(s)
    assert s.jobs.status(rid).status == "paused"
    assert calls == [rid]
    assert s.store.get_sample(CHIP)["props"]["reactor_state_uncertain"]
    assert s.gate.heater("reactor_heater").get_target() is None
    assert s.gate.gas_flow("reactor_flow").get_target() == 0
    manifest = s.jobs.status(rid).workflow
    exposure = next(part for part in manifest["children"] if part["name"] == "observe_exposure")
    assert not exposure["checkpoints"][0]["safe_to_resume"]
    checkpoint = exposure["start_seq"]
    assert next(step for step in s.store.get_steps(rid) if step["seq"] == checkpoint)["status"] == "in_progress"
    starts = [event.payload["name"] for event in s.store.events_after(0, limit=100000, run_id=rid) if event.kind == "step.started"]
    assert starts.count("load_reactor_chip") == 1

    folder = tmp_path / "paused-restart"
    folder.mkdir()
    save_lab_state(s.lab, s.clock, folder / "world.pkl")
    with sqlite3.connect(folder / "qumus.db") as connection:
        s.store._conn.backup(connection)
    cfg = configuration.ideal_stacking_profile()
    cfg["seed"] = 8
    cfg["reactor"]["material_metal"] = s.store.get_sample(CHIP)["props"]["material_metal_preparation"]["model_assumptions"]
    restored = build_lab_side(folder / "qumus.db", testbench_config=cfg, state_path=folder / "world.pkl")
    assert restored.jobs.status(rid).workflow == manifest
    last = restored.store.last_event_id()
    decision = restored.jobs.resume(rid)
    assert decision["action"] == "park" and decision["start_seq"] == checkpoint
    assert restored.jobs.status(rid).status == "waiting_human"
    assert calls == [rid]
    assert not any(event.kind == "step.started" for event in restored.store.events_after(last, limit=100000, run_id=rid))
    assert restored.gate.heater("reactor_heater").get_target() is None
    assert restored.gate.gas_flow("reactor_flow").get_target() == 0
    assert CHIP in restored.store.reserved_samples()
    assert finish(s, rid).status == "error"


def test_interrupted_hot_release_does_not_restart_its_molecule(prepared_snapshot, tmp_path, monkeypatch):
    from qumus2.core.errors import StopRequested
    from qumus2.flows.library.material_metal_device import DeviceRelease

    s = restore_snapshot(prepared_snapshot, tmp_path, "before-")
    ids = s.store.get_project(s.project_id)["state"]["reactor_selection"]["confirmed_ids"]
    original = DeviceRelease.act
    calls = []

    def interrupted(self, ctx, correction):
        calls.append(ctx.run_id)
        original(self, ctx, correction)
        raise StopRequested("Test interruption after hot release before verified ledger commit")

    monkeypatch.setattr(DeviceRelease, "act", interrupted)
    submitted = call(s, "hands", "prepare_material_metal", {"chip_id": CHIP, "flake_ids": ids, "stamp_id": "stamp_01"})
    assert submitted.status == "pending", submitted
    rid = submitted.data["job_id"]
    call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 120})
    # This restored noisy transport may need its existing bounded reversible retry.
    for _ in range(2):
        if calls:
            break
        retry = call(s, "hands", "recover_material_metal_job", {"job_id": rid, "action": "retry", "reason": "Retry observed reversible transport before release"})
        assert retry.status == "pending", retry
        call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 120})
    assert calls == [rid]
    assert s.jobs.status(rid).status == "paused"
    manifest = s.jobs.status(rid).workflow
    release = next(part for part in manifest["children"] if part["name"] == "release_stack")
    leaf = next(leaf for leaf in release["checkpoints"] if leaf["name"] == "release_material_metal_stack")
    assert not leaf["safe_to_resume"]
    assert not s.store.get_sample(CHIP)["props"].get("material_metal_preparation")
    last = s.store.last_event_id()
    decision = s.jobs.resume(rid)
    assert decision["action"] == "park" and decision["start_seq"] == leaf["seq"]
    assert s.jobs.status(rid).status == "waiting_human"
    assert calls == [rid] and s.jobs.status(rid).workflow == manifest
    assert not any(event.kind == "step.started" for event in s.store.events_after(last, limit=100000, run_id=rid))
    assert s.gate.heater("stamp_heater").get_target() is None
    assert CHIP in s.store.reserved_samples()


@pytest.fixture
def wait_diagnostic(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("material_metal_wait_diagnostic", scripts / "check_material_metal_devices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("role", ["hands", "orchestrator"])
def test_diagnostic_wait_polls_one_job_until_success(wait_diagnostic, monkeypatch, role):
    wall = [0.0]
    monkeypatch.setattr(wait_diagnostic, "monotonic", lambda: wall[0])
    states = iter([("pending", False, "running", 100),
                   ("pending", False, "running", 100),
                   ("corrected", True, "succeeded", 10)])

    async def respond(agent, tool, args, expected):
        assert agent == role and tool == "wait_job" and expected is None
        status, done, state, elapsed = next(states)
        wall[0] += elapsed
        return SimpleNamespace(status=status, summary=state,
                               data={"run_id": "same-job", "done": done, "status": state})

    call = AsyncMock(side_effect=respond)
    jobs = SimpleNamespace(stop_all=Mock(), status=Mock())
    result = asyncio.run(wait_diagnostic.wait_for_job(call, jobs, role, "same-job", 250))
    assert result.status == "corrected" and result.data["status"] == "succeeded"
    assert [c.args[2] for c in call.await_args_list] == [
        {"job_id": "same-job", "timeout_s": 120},
        {"job_id": "same-job", "timeout_s": 120},
        {"job_id": "same-job", "timeout_s": 50}]
    jobs.stop_all.assert_not_called()
    jobs.status.assert_not_called()


@pytest.mark.parametrize("recovery,exhausted", [
    ("recover_material_metal_job", False), ("retry_job", False), ("recover_material_metal_job", True)])
def test_diagnostic_wait_recovers_only_terminal_jobs(wait_diagnostic, monkeypatch, recovery, exhausted):
    wall = [0.0]
    monkeypatch.setattr(wait_diagnostic, "monotonic", lambda: wall[0])
    states = iter([("needs_attention", True, "failed")] * 3 if exhausted else [
        ("needs_attention", True, "failed"), ("pending", False, "running"), ("ok", True, "succeeded")])

    async def respond(agent, tool, args=None, expected="ok"):
        wall[0] += 1
        if tool == "wait_job":
            status, done, state = next(states)
            return SimpleNamespace(status=status, summary=state,
                                   data={"run_id": "same-job", "done": done, "status": state})
        assert tool in ("inspect_bench", recovery)
        return SimpleNamespace(status=expected)

    call = AsyncMock(side_effect=respond)
    jobs = SimpleNamespace(stop_all=Mock(), status=Mock())
    result = asyncio.run(wait_diagnostic.wait_for_job(
        call, jobs, "hands", "same-job", 100, recovery=recovery))
    names = [c.args[1] for c in call.await_args_list]
    assert names == (["wait_job", "inspect_bench", recovery, "wait_job", "inspect_bench", recovery, "wait_job"]
                     if exhausted else ["wait_job", "inspect_bench", recovery, "wait_job", "wait_job"])
    assert result.status == ("needs_attention" if exhausted else "ok")
    for c in call.await_args_list:
        if c.args[1] != "inspect_bench":
            assert c.args[2]["job_id"] == "same-job"
        if c.args[1] == recovery:
            assert c.args[3] == "pending"
            assert (c.args[2].get("action") == "retry") == (recovery == "recover_material_metal_job")
    jobs.stop_all.assert_not_called()


@pytest.mark.parametrize("expiry", ["pending", "inspection", "recovery", "subsecond"])
def test_diagnostic_wait_deadline_requests_stop_without_reset(wait_diagnostic, monkeypatch, expiry):
    wall = [0.0]
    monkeypatch.setattr(wait_diagnostic, "monotonic", lambda: wall[0])

    async def respond(agent, tool, args=None, expected="ok"):
        if tool == "wait_job":
            assert 1 <= args["timeout_s"] <= 120
            wall[0] += 10 if expiry == "pending" else 2
            return SimpleNamespace(status="pending" if expiry == "pending" else "needs_attention",
                                   summary="unfinished", data={"run_id": "same-job",
                                   "done": expiry != "pending", "status": "running" if expiry == "pending" else "failed"})
        if tool == "inspect_bench":
            wall[0] += 9 if expiry == "inspection" else 3
        else:
            assert tool == "recover_material_metal_job"
            wall[0] += 6
        return SimpleNamespace(status=expected)

    call = AsyncMock(side_effect=respond)
    jobs = SimpleNamespace(stop_all=Mock(), status=Mock(return_value=SimpleNamespace(status="running")))
    with pytest.raises(TimeoutError, match="Stop requested; latest public status=running"):
        asyncio.run(wait_diagnostic.wait_for_job(
            call, jobs, "hands", "same-job", .5 if expiry == "subsecond" else 10,
            recovery="recover_material_metal_job"))
    assert [c.args[1] for c in call.await_args_list] == {
        "pending": ["wait_job"], "inspection": ["wait_job", "inspect_bench"],
        "recovery": ["wait_job", "inspect_bench", "recover_material_metal_job"], "subsecond": []}[expiry]
    jobs.stop_all.assert_called_once_with()
    jobs.status.assert_called_once_with("same-job")


def test_diagnostic_wait_does_not_recover_an_unfinished_nonpending_response(wait_diagnostic, monkeypatch):
    monkeypatch.setattr(wait_diagnostic, "monotonic", lambda: 0)
    call = AsyncMock(return_value=SimpleNamespace(status="error", summary="not terminal",
        data={"run_id": "same-job", "done": False, "status": "running"}))
    jobs = SimpleNamespace(stop_all=Mock(), status=Mock())
    with pytest.raises(RuntimeError, match="did not establish a terminal state"):
        asyncio.run(wait_diagnostic.wait_for_job(
            call, jobs, "hands", "same-job", 10, recovery="recover_material_metal_job"))
    assert [c.args[1] for c in call.await_args_list] == ["wait_job"]
    jobs.stop_all.assert_not_called()


@pytest.mark.parametrize("elapsed", [9.5, 10, 10.5])
def test_diagnostic_wait_terminal_result_uses_positive_remaining_budget(wait_diagnostic, monkeypatch, elapsed):
    wall = [0.0]
    monkeypatch.setattr(wait_diagnostic, "monotonic", lambda: wall[0])

    async def respond(*args):
        wall[0] = elapsed
        return SimpleNamespace(status="ok", summary="succeeded",
                               data={"run_id": "same-job", "done": True, "status": "succeeded"})

    call = AsyncMock(side_effect=respond)
    jobs = SimpleNamespace(stop_all=Mock(), status=Mock(return_value=SimpleNamespace(status="succeeded")))
    if elapsed < 10:
        result = asyncio.run(wait_diagnostic.wait_for_job(call, jobs, "hands", "same-job", 10))
        assert result.status == "ok" and result.data["done"] is True
        jobs.stop_all.assert_not_called()
        jobs.status.assert_not_called()
    else:
        with pytest.raises(TimeoutError, match="diagnostic wall deadline"):
            asyncio.run(wait_diagnostic.wait_for_job(call, jobs, "hands", "same-job", 10))
        jobs.stop_all.assert_called_once_with()
        jobs.status.assert_called_once_with("same-job")
    call.assert_awaited_once_with("hands", "wait_job", {"job_id": "same-job", "timeout_s": 10}, None)


@pytest.fixture
def ideal_diagnostic(wait_diagnostic, monkeypatch):
    module = wait_diagnostic
    factory = Mock(side_effect=module.configuration.ideal_stacking_profile)
    monkeypatch.setattr(module.configuration, "ideal_stacking_profile", factory)
    return module, factory


def test_diagnostic_standard_configuration_does_not_select_ideal(ideal_diagnostic):
    module, factory = ideal_diagnostic
    cfg, conditions = module.diagnostic_configuration()
    assert cfg == module.simulation_profile()
    factory.assert_not_called()
    assert conditions["simulation_condition"] == "standard" and conditions["offlist"] == []
    assert conditions["normalized_effective_configuration"]["reactor"]["material_metal"]["mica"]["release_c"] == 150


def test_diagnostic_effective_seed_matches_explicit_constructor_override(ideal_diagnostic):
    module, _ = ideal_diagnostic
    cfg, conditions = module.diagnostic_configuration("ideal_stacking_v1", {"success_probability": 1}, seed=8)
    assert conditions["configured_seed"] == module.simulation_profile()["seed"]
    assert cfg["seed"] == conditions["effective_seed"] == conditions["constructor_seed_override"] == 8
    assert all(row["path"] != ["seed"] for row in conditions["central_condition_changes"])
    assert any(row["path"] == ["seed"] and row["after"] == 8 for row in conditions["effective_changes_from_standard"])


def test_diagnostic_ideal_configuration_derives_changes_and_preserves_defaults(ideal_diagnostic):
    module, factory = ideal_diagnostic
    cfg, conditions = module.diagnostic_configuration("ideal_stacking_v1", {"success_probability": 1})
    factory.assert_called_once_with()
    model = cfg["reactor"]["material_metal"]
    assert model["mica"]["pickup_c"] == 80 and model["mica"]["release_c"] == 150
    assert model["mica"]["support_retained_on_stamp"] is True and model["success_probability"] == 1
    assert model["PC"]["pickup_c"] == 90 and model["PC"]["release_c"] == 170
    assert all(model[film][key] == 1 for film in ("PC", "mica") for key in ("pickup_probability", "release_probability"))
    rows = {tuple(row["path"]): row for row in conditions["central_condition_changes"]}
    assert rows[("simulation_condition",)]["kind"] == "condition_marker"
    assert rows[("microscope", "observation_noise_scale")]["kind"] == "added"
    # Jialei 2026-11-02: the default SIM stage already has zero move error,
    # so the ideal condition no longer changes it.
    assert ("stage", "sigma_mm") not in rows
    assert ("reactor", "material_metal", "mica", "release_c") not in rows
    assert ("reactor", "material_metal", "protocol_id") not in rows
    assert all(row["kind"] != "condition_marker" for row in conditions["offlist"])
    assert conditions["declared_material_overrides"] == {"success_probability": 1}
    cfg["stage"]["sigma_mm"] = 99
    assert conditions["effective_configuration"]["stage"]["sigma_mm"] == 0


@pytest.mark.parametrize("overrides", [
    {"PC": {"pickup_probability": .9}}, {"mica": {"release_probability": .9}}, {"mica": {"release_c": 170}}])
def test_diagnostic_ideal_configuration_rejects_conflicting_transfer_overrides(ideal_diagnostic, overrides):
    module, _ = ideal_diagnostic
    with pytest.raises(ValueError, match="conflicts|transfer recipes"):
        module.diagnostic_configuration("ideal_stacking_v1", overrides)


def test_diagnostic_ideal_configuration_rejects_a_mismatched_marker(ideal_diagnostic):
    module, factory = ideal_diagnostic
    factory.side_effect = lambda: module.simulation_profile()
    with pytest.raises(ValueError, match="marker"):
        module.diagnostic_configuration("ideal_stacking_v1")


@pytest.mark.parametrize("flow", ["prepare_material_metal", "stack_device"])
@pytest.mark.parametrize("matches", [True, False])
def test_diagnostic_freezes_actual_admitted_assumptions_without_rewriting(ideal_diagnostic, tmp_path, flow, matches):
    module, _ = ideal_diagnostic
    _, conditions = module.diagnostic_configuration("ideal_stacking_v1", {"success_probability": 1})
    expected = deepcopy(conditions["normalized_effective_configuration"]["reactor"]["material_metal"])
    actual = deepcopy(expected if flow == "prepare_material_metal" else expected["mica"])
    if not matches:
        (actual["mica"] if flow == "prepare_material_metal" else actual)["release_probability"] = .9
    params = {"receipt_inputs": {"model_assumptions": actual}} if flow == "prepare_material_metal" else {
        "stamp_film": "mica", "support_assumptions": actual}
    run = {"flow": flow, "status": "running", "params": params}
    original = deepcopy(run)
    s = SimpleNamespace(store=SimpleNamespace(get_run=Mock(return_value=run)), jobs=SimpleNamespace(stop_all=Mock()))
    if matches:
        module.record_admitted_transfer(s, tmp_path, conditions, "same-job")
        s.jobs.stop_all.assert_not_called()
    else:
        with pytest.raises(ValueError, match="Admitted.*conflicts"):
            module.record_admitted_transfer(s, tmp_path, conditions, "same-job")
        s.jobs.stop_all.assert_called_once_with()
    saved = json.loads((tmp_path / "simulation-conditions.json").read_text())["admitted_transfer_assumptions"][-1]
    assert saved["actual"] == actual and saved["matches_declared_configuration"] is matches
    assert run == original


@pytest.mark.parametrize("outcome", ["captured", "state_error", "serialization_error", "write_error"])
def test_diagnostic_private_evaluation_is_passive_and_qualifies_unfinished(ideal_diagnostic, tmp_path, monkeypatch, outcome):
    module, _ = ideal_diagnostic
    monkeypatch.setattr(module, "source_fingerprint", lambda: "source-sha")
    _, conditions = module.diagnostic_configuration("ideal_stacking_v1")
    clock = SimpleNamespace(now=Mock(return_value=42), event_clock_basis="sim:unit", event_clock_domain="simulation",
                            advance=Mock(), sleep=Mock())
    world = SimpleNamespace(evaluation_snapshot=Mock(return_value={"private_sentinel": "PRIVATE_ONLY"}),
                            rng=SimpleNamespace(random=Mock()), observation_rng=SimpleNamespace(random=Mock()))
    if outcome == "state_error":
        world.evaluation_snapshot.side_effect = RuntimeError("snapshot unavailable")
    if outcome == "serialization_error":
        world.evaluation_snapshot.return_value = {"unserializable": object()}
    if outcome == "write_error":
        original_write = Path.write_bytes
        def fail_private(path, data):
            if path.name == "simulation-evaluation.json":
                raise OSError("private write failed")
            return original_write(path, data)
        monkeypatch.setattr(Path, "write_bytes", fail_private)
    store = SimpleNamespace(list_runs=Mock(return_value=[{"id": "active", "flow": "stack_device", "status": "running"}]),
                            last_event_id=Mock(return_value=7))
    s = SimpleNamespace(store=store, clock=clock, lab=SimpleNamespace(world=world))
    diagnostic = module.save_private_evaluation(s, tmp_path, "source-sha", conditions, recorder_final_read_completed=True)
    world.evaluation_snapshot.assert_called_once_with()
    clock.advance.assert_not_called()
    clock.sleep.assert_not_called()
    world.rng.random.assert_not_called()
    world.observation_rng.random.assert_not_called()
    assert "PRIVATE_ONLY" not in json.dumps(diagnostic)
    if outcome == "write_error":
        assert diagnostic["status"] == "write_failed"
    else:
        saved = json.loads((tmp_path / "simulation-evaluation.json").read_text())
        assert saved["job_state_classification"] == "unfinished_or_unsuccessful" and saved["atomic_terminal_claim"] is False
        assert saved["binding_before"] == saved["binding_after"]
        assert saved["binding_before"]["event_prefix_id"] == 7 and saved["binding_before"]["clock_time"] == 42
        assert saved["configuration_sha256"] == conditions["configuration_sha256"]
        assert saved["execution_source_sha256"] == "source-sha" and saved["verification"] is False
        assert saved["application_source_matches_start"] is True
        assert len(saved["capture_source"]["harness_sha256"]) == 64
        assert diagnostic["status"] == {"captured": "captured", "state_error": "incomplete",
                                         "serialization_error": "serialization_failed"}[outcome]


@pytest.mark.parametrize("observer_error", [False, True])
def test_diagnostic_private_capture_follows_final_read_and_preserves_primary_failure(
        ideal_diagnostic, monkeypatch, tmp_path, observer_error):
    module, _ = ideal_diagnostic
    order = []
    store = SimpleNamespace(emit=Mock(side_effect=RuntimeError("PRIMARY_FAILURE")), events_after=Mock(return_value=[]),
        list_runs=Mock(return_value=[{"id": "active", "flow": "stack_device", "status": "running"}]),
        last_event_id=Mock(return_value=8))
    world = SimpleNamespace(evaluation_snapshot=Mock(side_effect=lambda: (order.append("evaluation") or {"sentinel": "PRIVATE_ONLY"})))
    s = SimpleNamespace(store=store, clock=SimpleNamespace(now=lambda: 42), lab=SimpleNamespace(world=world, bench={}),
        runtime=SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append("runtime_close"))), gateway=SimpleNamespace(call=AsyncMock()))
    recorder = SimpleNamespace(stop=Mock(side_effect=lambda: order.append("recorder_final_read")))
    builder = Mock(return_value=s)
    exporter = Mock(return_value=tmp_path / "export")
    monkeypatch.setattr(module, "build_services", builder)
    monkeypatch.setattr(module, "attach_recorder", Mock(return_value=recorder))
    monkeypatch.setattr(module, "source_fingerprint", lambda: "source-sha")
    monkeypatch.setattr(module, "export_recording", exporter)
    monkeypatch.setattr(module, "check_recording", lambda path: [])
    if observer_error:
        def failed_observer(*args, **kwargs):
            order.append("evaluation")
            raise RuntimeError("SECONDARY_CAPTURE_FAILURE")
        monkeypatch.setattr(module, "save_private_evaluation", failed_observer)
    folder = tmp_path / "fresh"
    assert asyncio.run(module.record(folder, seed=8, simulation_condition="ideal_stacking_v1")) is False
    assert order == ["runtime_close", "recorder_final_read", "evaluation"]
    summary = json.loads((folder / "summary.json").read_text())
    assert summary["error"] == "PRIMARY_FAILURE" and summary["passed"] is False
    assert summary["private_evaluation_capture"]["status"] == ("capture_failed" if observer_error else "captured")
    assert "PRIVATE_ONLY" not in json.dumps(summary) and "PRIVATE_ONLY" not in json.dumps(exporter.call_args.kwargs)
    assert "simulation-evaluation" not in json.dumps(exporter.call_args.kwargs)
    assert builder.call_args.kwargs["testbench_config"] == json.loads((folder / "simulation-conditions.json").read_text())["effective_configuration"]
    assert builder.call_args.kwargs["seed"] == builder.call_args.kwargs["testbench_config"]["seed"]
    s.gateway.call.assert_not_awaited()


@pytest.mark.parametrize("selector", [None, "ideal_stacking_v1"])
def test_diagnostic_cli_selects_condition_explicitly(ideal_diagnostic, monkeypatch, tmp_path, selector):
    module, _ = ideal_diagnostic
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "record", record)
    args = ["--out", str(tmp_path / "run")]
    if selector is not None:
        args += ["--simulation-condition", selector]
    assert module.main(args) == 0
    assert record.await_args.kwargs["simulation_condition"] == (selector or "standard")
    assert record.await_args.kwargs["seed"] == 8


def test_diagnostic_ideal_record_cannot_relabel_an_existing_run(ideal_diagnostic, monkeypatch, tmp_path):
    module, _ = ideal_diagnostic
    builder = Mock()
    monkeypatch.setattr(module, "build_services", builder)
    with pytest.raises(FileExistsError):
        asyncio.run(module.record(tmp_path, simulation_condition="ideal_stacking_v1"))
    builder.assert_not_called()


@pytest.mark.parametrize("capture_fails", [False, True])
def test_diagnostic_fixture_captures_preparation_before_reaction_without_masking(
        ideal_diagnostic, monkeypatch, tmp_path, capsys, capture_fails):
    module, _ = ideal_diagnostic
    order = []
    state = SimpleNamespace(
        store=SimpleNamespace(_conn=SimpleNamespace(backup=Mock(side_effect=lambda connection: order.append("db_checkpoint"))),
            list_runs=lambda: [{"id": "prepared", "flow": "prepare_material_metal", "status": "succeeded"}],
            last_event_id=lambda: 43),
        clock=SimpleNamespace(now=lambda: 42),
        lab=SimpleNamespace(world=SimpleNamespace(evaluation_snapshot=Mock(
            side_effect=lambda: (order.append("private_evaluation") or {"unit_preparation_identity": "PRIVATE_ONLY"})))))
    folder = tmp_path / "fixture"
    folder.mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys.modules[__name__], "save_lab_state", Mock(side_effect=lambda *args: order.append("state_checkpoint")))
    monkeypatch.setattr(module, "source_fingerprint", lambda: "source-sha")

    async def record_without_physics(out, film, **kwargs):
        assert kwargs["simulation_condition"] == "ideal_stacking_v1" and kwargs["seed"] == 8
        kwargs["before_preparation"](state)
        state.lab.world.evaluation_snapshot.assert_not_called()
        kwargs["after_preparation"](state)
        order.append("reaction_would_start")
        return True

    double = SimpleNamespace(diagnostic_configuration=module.diagnostic_configuration,
        record=record_without_physics, source_fingerprint=module.source_fingerprint,
        save_private_evaluation=module.save_private_evaluation)
    if capture_fails:
        double.save_private_evaluation = Mock(side_effect=RuntimeError("SECONDARY_CAPTURE_FAILURE"))
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda target: None))
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda supplied: double)
    result = prepared_snapshot.__wrapped__(SimpleNamespace(mktemp=lambda name: folder), SimpleNamespace(param="PC"))
    assert result[0] == folder / "baseline" and result[2] == "PC"
    assert result[1]["seed"] == 8
    assert order[:4] == ["db_checkpoint", "state_checkpoint", "db_checkpoint", "state_checkpoint"]
    assert order[-1] == "reaction_would_start"
    if capture_fails:
        assert "SECONDARY_CAPTURE_FAILURE" in capsys.readouterr().out
    else:
        assert order[-2] == "private_evaluation"
        saved = json.loads((folder / "baseline/simulation-evaluation.json").read_text())
        assert saved["capture_phase"] == "after_preparation_checkpoint"
        assert saved["recorder_final_read_completed"] is False
        assert saved["effective_seed"] == 8
        assert saved["job_state_classification"] == "all_jobs_succeeded" and saved["binding_before"]["event_prefix_id"] == 43


@pytest.mark.parametrize("status", ["paused", "parked", "waiting_human", "failed", "cancelled", "running", "succeeded", None])
def test_diagnostic_private_evaluation_classifies_recorded_job_outcomes(ideal_diagnostic, monkeypatch, tmp_path, status):
    module, _ = ideal_diagnostic
    monkeypatch.setattr(module, "source_fingerprint", lambda: "source-sha")
    _, conditions = module.diagnostic_configuration("ideal_stacking_v1")
    runs = [] if status is None else [{"id": "observed", "flow": "stack_device", "status": status}]
    original = deepcopy(runs)
    s = SimpleNamespace(store=SimpleNamespace(list_runs=lambda: runs, last_event_id=lambda: 4),
        clock=SimpleNamespace(now=lambda: 42),
        lab=SimpleNamespace(world=SimpleNamespace(evaluation_snapshot=Mock(return_value={"unit_only": True}))))
    assert module.save_private_evaluation(s, tmp_path, "source-sha", conditions,
                                         recorder_final_read_completed=True)["status"] == "captured"
    saved = json.loads((tmp_path / "simulation-evaluation.json").read_text())
    assert saved["binding_before"]["runs"] == saved["binding_after"]["runs"] == original
    expected_ids = ["observed"] if status not in (None, "succeeded") else []
    assert saved["binding_before"]["unfinished_or_unsuccessful_run_ids"] == expected_ids
    assert saved["binding_after"]["unfinished_or_unsuccessful_run_ids"] == expected_ids
    assert saved["job_state_classification"] == (
        "no_jobs" if status is None else "all_jobs_succeeded" if status == "succeeded" else "unfinished_or_unsuccessful")
    assert saved["atomic_terminal_claim"] is False and saved["verification"] is False
    assert runs == original


@pytest.mark.parametrize("before,after", [("succeeded", "running"), ("running", "succeeded")])
def test_diagnostic_private_evaluation_qualifies_status_changes_during_capture(
        ideal_diagnostic, monkeypatch, tmp_path, before, after):
    module, _ = ideal_diagnostic
    monkeypatch.setattr(module, "source_fingerprint", lambda: "source-sha")
    _, conditions = module.diagnostic_configuration("ideal_stacking_v1")
    store = SimpleNamespace(last_event_id=lambda: 4, list_runs=Mock(side_effect=[
        [{"id": "changing", "flow": "stack_device", "status": before}],
        [{"id": "changing", "flow": "stack_device", "status": after}]]))
    s = SimpleNamespace(store=store, clock=SimpleNamespace(now=lambda: 42),
        lab=SimpleNamespace(world=SimpleNamespace(evaluation_snapshot=Mock(return_value={"unit_only": True}))))
    module.save_private_evaluation(s, tmp_path, "source-sha", conditions, recorder_final_read_completed=True)
    saved = json.loads((tmp_path / "simulation-evaluation.json").read_text())
    assert saved["binding_before"]["runs"][0]["status"] == before
    assert saved["binding_after"]["runs"][0]["status"] == after
    assert saved["binding_changed"] is True and saved["atomic_terminal_claim"] is False
    assert saved["job_state_classification"] == "unfinished_or_unsuccessful"
