"""Experimental jobs and agent checks, including quality and interruption honesty."""

from __future__ import annotations

import asyncio
import math

import pytest
from conftest import quiet_bench
from fastapi.testclient import TestClient
from test_reactor_devices import reactor_services, start_growth

from qumus2.app import build_services
from qumus2.flows.library.reactor import LoadReactorChip
from qumus2.flows.step import Verdict
from qumus2.records.outcomes import bare_chips
from qumus2.runtime.recovery import plan_recovery
from qumus2.ui.app import create_app

CHIP = "reactor_chip_01"


def call(s, agent, tool, args=None):
    return asyncio.run(s.gateway.call(agent, tool, args or {}))[0]


def run(s, flow, **params):
    rid = s.jobs.submit(flow, params, project_id=s.project_id)
    state = s.jobs.wait(rid, 20)
    assert state.status == "succeeded", (state.status, state.error)
    return rid


def ready(s):
    result = call(s, "eyes", "inspect_bench")
    assert result.status == "ok", result.summary
    return result


def job(s, tool, **params):
    result = call(s, "reactor", tool, params)
    assert result.status == "pending", result.summary
    rid = result.data["job_id"]
    result = call(s, "reactor", "wait_job", {"job_id": rid, "timeout_s": 20})
    assert result.status in ("ok", "corrected"), result.summary
    return rid


def test_disabled_module_has_no_jobs_or_reactor_goal(services):
    s = services
    assert call(s, "orchestrator", "get_reactor_setup").data == {"enabled": False}
    before = len(s.store.list_runs())
    with pytest.raises(ValueError, match="disabled"):
        s.jobs.submit("shutdown_reactor", {})
    assert len(s.store.list_runs()) == before


def test_role_schema_eyes_and_dedicated_inventory_guards():
    s = reactor_services()
    assert call(s, "hands", "load_reactor_chip", {"chip_id": CHIP}).data["rule"] == "permission"
    assert call(s, "reactor", "set_reactor_goal", {"chip_id": CHIP}).data["rule"] == "permission"
    assert call(s, "reactor", "load_reactor_chip", {"chip_id": CHIP}).data["rule"] == "reactor_ready"
    ready(s)
    for params in ({"chip_id": CHIP, "temperature_c": math.nan},
                   {"chip_id": CHIP, "flow_sccm": math.inf},
                   {"chip_id": CHIP, "rotation": 1}):
        assert call(s, "reactor", "reactor_growth", params).data["rule"] == "schema"
    wrong = call(s, "reactor", "load_reactor_chip", {"chip_id": "chip_001"})
    assert wrong.status == "error" and "dedicated" in wrong.summary
    assert CHIP not in bare_chips(s.store)
    for flow, params in (("exfoliate_chips", {"material": "Gr", "chip_ids": [CHIP]}),
                         ("search_chip", {"chip_id": CHIP, "material": "Gr"})):
        with pytest.raises(ValueError, match="dedicated"):
            s.jobs.submit(flow, params)


def test_eyes_follow_inserted_carrier_and_scan_dedicated_storage():
    s = reactor_services()
    result = ready(s)
    regions = result.data["result"][0]["observations"][0]["measurements"]["regions"]
    assert len(regions["reactor_storage"]) == 4
    run(s, "load_reactor_chip", chip_id=CHIP)
    result = call(s, "eyes", "inspect_bench", {"regions": ["reactor_port"]})
    regions = result.data["result"][0]["observations"][0]["measurements"]["regions"]
    assert regions["reactor_port"][0]["at"] == "reactor_port"
    assert regions["reactor_port"][0]["pose"]["y"] == pytest.approx(430, abs=0.5)


@pytest.mark.parametrize("dx,rotation,verdict", [
    (0, 0, Verdict.OK), (0.35, 0, Verdict.CORRECTABLE), (0.1, 2, Verdict.CORRECTABLE),
])
def test_narrow_slot_requires_the_whole_measured_chip_to_fit(dx, rotation, verdict):
    from qumus2.core.types import Pose

    cfg = quiet_bench(locator__sigma_mm=0, locator__sigma_deg=0)
    cfg["reactor"] = {"enabled": True, "experiment_kind": "legacy_field_growth"}
    s = build_services(testbench_config=cfg, seed=1)
    chip = s.lab.world.objects[CHIP]
    load = s.lab.bench["reactor_module"]["geometry"]["load"]
    chip.pose, chip.on = Pose(x=load["x"] + dx, y=load["y"], rotation=rotation), "reactor"
    params = {"chip_id": CHIP, "home_slot": "reactor_chip_slot_1"}
    rid = s.store.create_run("load_reactor_chip", params, project_id=s.project_id)
    ctx = s.jobs.context_factory(rid, params, s.project_id)
    obs = LoadReactorChip(CHIP, "reactor_chip_slot_1").verify(ctx)
    assert obs.verdict == verdict
    assert (obs.measurements["slot_edge_clearance_mm"] >= 0.05) == (verdict == Verdict.OK)


def test_closed_loop_requires_measured_quality_not_successful_job(tmp_path):
    s = reactor_services(tmp_path)
    ready(s)
    goal = call(s, "orchestrator", "set_reactor_goal", {"chip_id": CHIP})
    assert goal.status == "ok"
    job(s, "load_reactor_chip", chip_id=CHIP)
    poor = job(s, "reactor_growth", chip_id=CHIP, flow_sccm=0, duration_s=120)
    result = call(s, "reactor", "submit_reactor_result", {"chip_id": CHIP, "growth_job_id": poor})
    assert result.status == "needs_attention" and result.data["observation"]["coverage"] == 0
    good = job(s, "reactor_growth", chip_id=CHIP, duration_s=600)
    result = call(s, "reactor", "submit_reactor_result", {"chip_id": CHIP, "growth_job_id": good})
    assert result.status == "ok" and result.data["accepted"] is True
    assert result.data["observation"]["coverage"] > 0.8
    assert result.images and "observed field" in result.data["scope"]
    job(s, "unload_reactor_chip", chip_id=CHIP)
    assert s.store.get_sample(CHIP)["location"] == "reactor_chip_slot_1"
    assert s.gate.heater("reactor_heater").get_target() is None
    assert s.gate.gas_flow("reactor_flow").get_target() == 0
    assert s.gate.heater("reactor_heater").read_temperature() <= 80
    assert s.gate.stage("reactor_slot").get_pose().y == 0
    unload = next(r for r in s.store.list_runs() if r["flow"] == "unload_reactor_chip")
    outcome = call(s, "reactor", "get_job_status", {"job_id": unload["id"]}).data["outcome"]
    access = next(x for x in outcome["process_checks"] if x["step"] == "wait_for_reactor_access")
    assert access["measurements"]["temperature_c"] <= 80


def test_reserved_carrier_refuses_second_chip_before_creating_job():
    s = reactor_services()
    owner = s.store.create_run("load_reactor_chip", {"chip_id": CHIP})
    s.store.reserve("slot", "reactor_port", owner, "load_reactor_chip")
    with pytest.raises(ValueError, match="reserved"):
        s.jobs.submit("load_reactor_chip", {"chip_id": "reactor_chip_02"})
    assert len(s.store.list_runs()) == 1


@pytest.mark.parametrize("seq", [0, 1])
def test_interrupted_exposure_parks_and_fresh_photo_does_not_erase_history(seq):
    s = reactor_services()
    start_growth(s)
    rid = s.store.create_run("reactor_growth", {"chip_id": CHIP}, project_id=s.project_id)
    s.store.set_run_status(rid, "running")
    name = "set_reactor_conditions" if seq == 0 else "observe_reactor_growth"
    s.store.begin_step(rid, seq, name, safe_to_resume=False)
    [decision] = plan_recovery(s.store, s.jobs.context_factory, gate=s.gate)
    assert decision.action == "park"
    assert s.gate.heater("reactor_heater").get_target() is None
    assert s.gate.gas_flow("reactor_flow").get_target() == 0
    assert s.store.get_sample(CHIP)["props"]["reactor_state_uncertain"] is True
    run(s, "observe_reactor", chip_id=CHIP)
    assert s.store.get_sample(CHIP)["props"]["reactor_state_uncertain"] is True
    with pytest.raises(ValueError, match="history is uncertain"):
        s.jobs.submit("reactor_growth", {"chip_id": CHIP})


def test_missing_growth_image_stops_targets_and_cannot_claim_quality():
    s = reactor_services()
    run(s, "load_reactor_chip", chip_id=CHIP)
    s.lab.devices.get("reactor_microscope").offline = True
    rid = s.jobs.submit("reactor_growth", {"chip_id": CHIP}, project_id=s.project_id)
    assert s.jobs.wait(rid, 20).status in ("parked", "waiting_human")
    assert s.gate.heater("reactor_heater").get_target() is None
    assert s.gate.gas_flow("reactor_flow").get_target() == 0
    assert s.store.get_sample(CHIP)["props"]["reactor_state_uncertain"] is True


def test_result_is_project_bound_and_cannot_reuse_older_growth():
    s = reactor_services()
    ready(s)
    call(s, "orchestrator", "set_reactor_goal", {"chip_id": CHIP})
    run(s, "load_reactor_chip", chip_id=CHIP)
    rid = run(s, "reactor_growth", chip_id=CHIP, duration_s=600)
    other = s.store.create_project("other")
    original = s.gateway.project_id
    s.gateway.project_id = other
    call(s, "orchestrator", "set_reactor_goal", {"chip_id": CHIP})
    result = call(s, "reactor", "submit_reactor_result", {"chip_id": CHIP, "growth_job_id": rid})
    assert result.status == "error" and "project" in result.summary
    s.gateway.project_id = original
    later = s.store.create_run("reactor_growth", {"chip_id": CHIP}, project_id=original)
    s.store.set_run_status(later, "failed")
    result = call(s, "reactor", "submit_reactor_result", {"chip_id": CHIP, "growth_job_id": rid})
    assert result.status == "error" and "later" in result.summary


def test_delegation_requires_current_accepted_target_and_goal_revision():
    s = reactor_services()
    ready(s)
    run(s, "load_reactor_chip", chip_id=CHIP)
    call(s, "orchestrator", "update_plan", {"note": "test", "plan": [
        {"id": "s1", "title": "Grow the reactor chip", "owner": "reactor"}]})
    call(s, "orchestrator", "set_reactor_goal", {"chip_id": CHIP})
    s.gateway.delegation_started("first", "reactor", "s1: reactor growth")
    job(s, "reactor_growth", chip_id=CHIP, duration_s=60)
    facts = s.gateway.delegation_finished("first")
    assert "No current code-checked" in facts
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"

    s.gateway.delegation_started("second", "reactor", "s1: reactor growth")
    rid = job(s, "reactor_growth", chip_id=CHIP, duration_s=540)
    assert call(s, "reactor", "submit_reactor_result", {
        "chip_id": CHIP, "growth_job_id": rid}).status == "ok"
    # A new objective invalidates acceptance under the old objective.
    call(s, "orchestrator", "set_reactor_goal", {"chip_id": CHIP, "target_coverage": 0.99})
    facts = s.gateway.delegation_finished("second")
    assert "No current code-checked" in facts
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"

def test_optional_reactor_camera_is_not_available_on_default_bench(services):
    with TestClient(create_app(services, start_driver=False)) as client:
        assert client.get("/api/camera/reactor_microscope/latest").status_code == 404


def test_reactor_jobs_feedback_and_images_through_separate_lab_process(tmp_path):
    cfg = quiet_bench()
    cfg["reactor"] = {"enabled": True, "experiment_kind": "legacy_field_growth"}
    s = build_services(tmp_path / "q.db", seed=1, testbench_config=cfg, lab_process=True)
    try:
        run(s, "load_reactor_chip", chip_id=CHIP)
        run(s, "reactor_growth", chip_id=CHIP, duration_s=600)
        with TestClient(create_app(s, start_driver=False, data_dir=tmp_path)) as client:
            bench = client.get("/api/bench").json()
            flow = next(d for d in bench["devices"] if d["id"] == "reactor_flow")
            assert flow["flow_sccm"] == pytest.approx(200, abs=1)
            assert flow["flow_target_sccm"] == 200
            image = client.get("/api/camera/reactor_microscope/latest").json()
            assert image["meta"]["reactor"]["coverage"] > 0.8
            assert client.get(image["image"]).status_code == 200
            assert bench["twin"]["reactor_microscope"]["focus_z"] != 0
            assert bench["twin"]["microscope"]["objective"] == 10
        run(s, "unload_reactor_chip", chip_id=CHIP)
        assert s.store.get_sample(CHIP)["location"] == "reactor_chip_slot_1"
    finally:
        s.supervisor.stop()
