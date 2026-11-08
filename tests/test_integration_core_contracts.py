"""Counterexamples from A01-A06/A09 and their valid model-facing transitions."""
from __future__ import annotations

import asyncio
import json
import pickle
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import quiet_bench

from qumus2.agents.gateway import ToolGateway
from qumus2.agents.tooling import ToolRegistry, default_registry
from qumus2.app import build_lab_side, build_services
from qumus2.records.plan_completion import executable_flow_names, unmet_reactor_outcome
from qumus2.runtime.jobs import JobStatus
from qumus2.runtime.store import RunStore
from qumus2.testbench.persist import lab_state, save_lab_state


def call(s, agent, tool, args=None):
    return asyncio.run(s.gateway.call(agent, tool, args or {}))[0]


def guidance(**overrides):
    return {"device_name": "straight bridge", "contact_scheme": "existing Au electrodes",
            "gate_scheme": "Si back gate", "channel": "straight bridge", "evidence": "synthetic test design",
            "target_chip_id": "chip_e01", "bridge_requirement": {
                "contact_overlap_um": 1, "channel_width_um": 2, "placement_margin_um": 0.5, "rationale": "test"},
            "layer_stack": [{"role": "channel", "material": "Gr", "thickness_labels": ["monolayer"], "min_area_um2": 200},
                            {"role": "contact_metal", "material": "Au", "flake": False}], **overrides}


def edit(s, plan):
    ctx = call(s, "orchestrator", "get_project_context")
    return call(s, "orchestrator", "update_plan", {"plan": plan, "note": "test change",
                                                   "expected_version": ctx.data["plan_version"]})


def test_restored_matching_geometry_preserves_saved_world_and_evidence(tmp_path):
    db, checkpoint = tmp_path / "q.db", tmp_path / "world.pkl"
    s = build_services(db, seed=1, testbench_config=quiet_bench())
    target = s.store.get_sample("chip_e01")
    props = target["props"]
    props.pop("electrode_geometry")
    s.store._x("UPDATE sample SET props_json=? WHERE id='chip_e01'", (json.dumps(props),))
    save_lab_state(s.lab, s.store.clock, checkpoint)
    before = lab_state(s.lab, s.store.clock)
    events = s.store.events_after(limit=10000)
    photos = {p: Path(p).read_bytes() for p in props["images"].values()}
    s.store.close()
    restored = build_lab_side(db, state_path=checkpoint, testbench_config=quiet_bench())
    after = lab_state(restored.lab, restored.clock)
    assert after["world"]["rng"] == before["world"]["rng"]
    assert after["world"]["objects"] == before["world"]["objects"]
    assert after["clock"] == before["clock"]
    assert restored.store.events_after(limit=len(events)) == events
    assert restored.store.get_sample("chip_e01")["props"]["electrode_geometry"]["contact_gap_um"] == 28
    assert all(Path(p).read_bytes() == data for p, data in photos.items())
    restored.store.close()
    reopened = build_lab_side(db, state_path=checkpoint, testbench_config=quiet_bench())
    gateway = ToolGateway(default_registry(), reopened.store, reopened.jobs,
                          project_id=reopened.store.list_projects()[0]["id"], bench=reopened.lab.bench)
    assert asyncio.run(gateway.call("advisor", "submit_device_plan", guidance()))[0].status == "ok"
    reopened.store.close()


@pytest.mark.parametrize("change", ["different_design", "untrusted_source", "real_bench"])
def test_restore_never_invents_unknown_geometry(services, change):
    s = services
    s.store.upsert_sample("chip_e01", "chip", props={"electrode_geometry": None, "image_source": "simulated"})
    if change == "different_design":
        s.lab.world.objects["chip_e01"].electrodes[0]["points"][0] = (123, 456)
    elif change == "untrusted_source":
        s.store.upsert_sample("chip_e01", "chip", props={"image_source": "external"})
    else:
        s.store.bench = "real"
    s.lab.restore_optical_references(s.store)
    assert call(s, "advisor", "get_target_geometry", {"chip_id": "chip_e01"}).status == "needs_attention"


def test_repaired_display_reference_never_becomes_prior_geometry_evidence(tmp_path):
    db, checkpoint = tmp_path / "q.db", tmp_path / "world.pkl"
    side = build_lab_side(db, seed=1, testbench_config=quiet_bench())
    side.store.upsert_sample("chip_e01", "chip", props={"electrode_geometry": None})
    side.lab.world.objects["chip_e01"].electrodes = []
    original_photos = side.store.get_sample("chip_e01")["props"]["images"]
    for _ in range(3):
        save_lab_state(side.lab, side.clock, checkpoint)
        side.store.close()
        side = build_lab_side(db, state_path=checkpoint, testbench_config=quiet_bench())
        chip = side.lab.world.objects["chip_e01"]
        assert chip.electrodes and chip.electrode_reference_only
        props = side.store.get_sample(chip.id)["props"]
        assert not props.get("electrode_geometry")
        assert props["images"] == original_photos
    side.store.close()


def test_explicit_registration_copies_only_person_supplied_dimensions(services):
    s = services
    data = {"chip_id": "chip_e01", "source": "calibrated_image", "reference": "owner calibration record",
            "units": "um", "contact_gap_um": 19, "contact_span_um": 4, "contact_depth_um": 20, "um_per_pixel": 0.2}
    wrong = s.store.add_instruction(json.dumps({"target_geometry": data}), source="agent", project_id=s.project_id)
    assert call(s, "orchestrator", "register_target_geometry", {"chip_id": "chip_e01", "instruction_id": wrong}).status == "error"
    iid = s.store.add_instruction(json.dumps({"target_geometry": data}), project_id=s.project_id)
    assert call(s, "orchestrator", "register_target_geometry", {"chip_id": "chip_e01", "instruction_id": iid}).status == "ok"
    assert call(s, "advisor", "get_target_geometry", {"chip_id": "chip_e01"}).data["contact_gap_um"] == 19
    assert call(s, "orchestrator", "register_target_geometry", {"chip_id": "chip_001", "instruction_id": iid}).status == "error"


def test_geometry_cannot_be_bypassed_or_erased_by_wording(services):
    s = services
    args = guidance()
    missing = {k: v for k, v in args.items() if k not in ("target_chip_id", "bridge_requirement")}
    assert call(s, "advisor", "submit_device_plan", missing).status == "error"
    assert call(s, "advisor", "submit_device_plan", {**args, "bridge_requirement": None}).status == "error"
    assert call(s, "advisor", "submit_device_plan", args).status == "ok"
    r = call(s, "advisor", "submit_device_plan", missing)
    assert r.status == "ok"
    assert r.data["flake_roles"][0]["required_footprint"]["width_um"] == 31
    changed = guidance(bridge_requirement={"contact_overlap_um": .5, "channel_width_um": 2.1,
        "placement_margin_um": .5, "rationale": "similar area but narrower footprint"})
    assert call(s, "advisor", "submit_device_plan", changed).status == "error"


def accept_layout(s):
    assert call(s, "advisor", "submit_device_plan", guidance()).status == "ok"
    s.store.upsert_sample("f", "flake", location="chip_001", status="catalogued")
    s.store.update_project_state(s.project_id, selected_flakes=["f"], selected_by_role={"channel": ["f"]})
    result = call(s, "designer", "submit_design", {"flake_ids": ["f"], "shapes": [
        {"layer": "channel", "x_um": 0, "y_um": 0, "w_um": 31, "h_um": 3},
        {"layer": "contact_metal", "x_um": 0, "y_um": 0, "w_um": 2, "h_um": 3}]})
    assert result.status == "ok", result.summary


def test_stack_bound_to_current_target_geometry_guidance_and_layout(services):
    s = services
    accept_layout(s)
    right = {"flake_ids": ["f"], "target_chip_id": "chip_e01"}
    assert s.gateway.pre("hands", "stack_device", right).allow
    wrong = call(s, "hands", "stack_device", {**right, "target_chip_id": "chip_002"})
    assert wrong.status == "denied" and wrong.data["rule"] == "stack_authorized"
    assert not s.store.list_runs() and not s.store.reservations()
    geom = s.store.get_sample("chip_e01")["props"]["electrode_geometry"]
    s.store.upsert_sample("chip_e01", "chip", props={"electrode_geometry": {**geom, "contact_gap_um": 40}})
    assert not s.gateway.pre("hands", "stack_device", right).allow
    assert call(s, "advisor", "submit_device_plan", guidance()).status == "ok"
    state = s.store.get_project(s.project_id)["state"]
    assert state["selected_flakes"] == [] and not state["design_accepted"] and state["design_binding"] is None


SEARCH = {"id": "s1", "title": "Search chip_001 and return to storage", "owner": "hands",
          "sample_outcomes": [{"sample_id": "chip_001", "status": "searched", "in_storage": True}]}


@pytest.mark.parametrize("replacement", [[], None, [{"sample_id": "chip_001", "status": "bare"}]])
def test_new_done_cannot_erase_or_weaken_pending_obligation(services, replacement):
    s = services
    assert edit(s, [SEARCH]).status == "ok"
    step = {**SEARCH, "status": "done"}
    if replacement is None:
        step.pop("sample_outcomes")
    else:
        step["sample_outcomes"] = replacement
    assert edit(s, [step]).status == "error"
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"
    assert not s.store.list_runs() and s.store.get_sample("chip_001")["status"] == "bare"


def test_pending_physical_work_cannot_be_completed_by_changing_owner_or_project_label(services):
    s = services
    assert edit(s, [SEARCH]).status == "ok"
    assert edit(s, [{**SEARCH, "owner": "orchestrator", "status": "done", "sample_outcomes": []}]).status == "error"
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "complete"}).status == "error"


@pytest.mark.parametrize("replacement", [[], [{"id": "s9", "title": "Complete", "owner": "orchestrator", "status": "done"}],
    [{**SEARCH, "owner": "orchestrator"}], [{**SEARCH, "status": "skipped"}], [{**SEARCH, "sample_outcomes": []}]])
def test_unfinished_obligations_survive_identity_removal_and_two_step_rewrites(services, replacement):
    s = services
    assert edit(s, [SEARCH]).status == "ok"
    assert edit(s, replacement).status == "error"
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "complete"}).status == "error"
    assert s.store.get_sample("chip_001")["status"] == "bare" and not s.store.list_runs()


def change_plan(s, steps, iid, action="cancel"):
    version = call(s, "orchestrator", "get_project_context").data["plan_version"]
    return call(s, "orchestrator", "update_plan", {"plan": steps, "note": "user changed the task",
        "expected_version": version, "physical_changes": [{"step_id": "s1", "action": action,
            "instruction_id": iid, "replacement_step_ids": ["s2"] if action == "supersede" else []}]})


@pytest.mark.parametrize("bad", ["stale", "unrelated", "negated", "other_project", "agent_source", "other_step"])
def test_cancellation_requires_matching_new_user_authority(services, bad):
    s = services
    text = {"unrelated": "continue", "negated": "Do not cancel s1.", "other_step": "Cancel s2."}.get(bad, "Cancel s1.")
    if bad == "stale":
        iid = s.store.add_instruction(text, project_id=s.project_id)
    assert edit(s, [SEARCH]).status == "ok"
    if bad != "stale":
        iid = s.store.add_instruction(text, project_id="other" if bad == "other_project" else s.project_id,
                                      source="agent" if bad == "agent_source" else "ui")
    assert change_plan(s, [], iid).status == "error"
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"


@pytest.mark.parametrize("action", ["cancel", "supersede"])
def test_person_can_cancel_or_retarget_without_claiming_execution(services, action):
    s = services
    assert edit(s, [SEARCH]).status == "ok"
    text = "Cancel s1." if action == "cancel" else "Replace s1 with s2."
    iid = s.store.add_instruction(text, project_id=s.project_id)
    replacement = {**SEARCH, "id": "s2", "title": "Search chip_002", "sample_outcomes": [
        {"sample_id": "chip_002", "status": "searched", "in_storage": True}]}
    assert change_plan(s, [] if action == "cancel" else [replacement], iid, action).status == "ok"
    plan = s.store.get_project(s.project_id)["plan"]
    old = next(step for step in plan if step["id"] == "s1")
    assert old["status"] == "skipped" and old["physical_resolution"]["instruction_id"] == iid
    assert old["sample_outcomes"][0]["sample_id"] == "chip_001"
    if action == "supersede":
        assert next(step for step in plan if step["id"] == "s2")["status"] == "pending"
        assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "complete"}).status == "error"
    assert s.store.get_sample("chip_001")["status"] == "bare" and not s.store.list_runs()


def test_refinement_preserves_outcomes_and_cancelled_delegation_stays_cancelled(services):
    s = services
    assert edit(s, [SEARCH]).status == "ok"
    refined = {**SEARCH, "operation": "search_chip", "title": "Search current chip and store it"}
    assert edit(s, [refined]).status == "ok"
    assert s.store.get_project(s.project_id)["plan"][0]["sample_outcomes"][0]["in_storage"]
    s.gateway.delegation_started("a", "hands", "s1: Search chip_001")
    iid = s.store.add_instruction(json.dumps({"plan_changes": [{"step_id": "s1", "action": "cancel"}]}),
                                  project_id=s.project_id)
    assert change_plan(s, [], iid).status == "ok"
    s.gateway.delegation_finished("a")
    plan = s.store.get_project(s.project_id)["plan"]
    assert plan[0]["status"] == "skipped"
    assert edit(s, plan).status == "ok"
    assert s.store.get_project(s.project_id)["plan"][0]["physical_resolution"]["instruction_id"] == iid


def test_user_cancellation_authority_survives_unrelated_plan_edits(services):
    s = services
    assert edit(s, [SEARCH]).status == "ok"
    iid = s.store.add_instruction("Cancel s1.", project_id=s.project_id)
    assert edit(s, [SEARCH, {"id": "s2", "owner": "eyes", "title": "Inspect"}]).status == "ok"
    assert change_plan(s, [], iid).status == "ok"


RETARGET_TEXT = ("Change of plan: keep chip_001 untouched for another experiment. Use chip_002 for graphene instead. "
                 "Update the task before moving anything, and still prepare just one chip.")


@pytest.mark.parametrize("variant", ["actual_instruction", "quoted", "negated_keep", "negated_use", "unrelated", "wrong_target",
                                     "weakened_condition"])
def test_natural_target_change_preserves_authority_and_physical_obligations(services, variant):
    s = services
    assert edit(s, [SEARCH]).status == "ok"
    text = RETARGET_TEXT
    if variant == "quoted":
        text = 'An example: "' + text + '"'
    elif variant == "negated_keep":
        text = text.replace("keep chip_001", "do not keep chip_001")
    elif variant == "negated_use":
        text = text.replace("Use chip_002", "Do not use chip_002")
    elif variant == "unrelated":
        text = "We previously kept chip_001 untouched and used chip_002 instead. Continue the current plan."
    elif variant == "wrong_target":
        text = text.replace("chip_002", "chip_003")
    iid = s.store.add_instruction(text, project_id=s.project_id)
    replacement = {**SEARCH, "id": "s2", "title": "Search chip_002", "sample_outcomes": [
        {"sample_id": "chip_002", "status": "searched", "in_storage": variant != "weakened_condition"}]}
    result = change_plan(s, [replacement], iid, "supersede")
    assert result.status == ("ok" if variant == "actual_instruction" else "error")
    plan = s.store.get_project(s.project_id)["plan"]
    if variant == "actual_instruction":
        assert next(step for step in plan if step["id"] == "s1")["status"] == "skipped"
        assert next(step for step in plan if step["id"] == "s2")["status"] == "pending"
    else:
        assert plan[0]["id"] == "s1" and plan[0]["status"] == "pending"
    assert not s.store.list_runs() and s.store.get_sample("chip_001")["status"] == "bare"


def test_real_flow_completion_and_historical_done_are_preserved(services):
    s = services
    s.store.update_project_state(s.project_id, eyes_checked=True)
    step = {"id": "s1", "owner": "hands", "title": "Place chip_001", "operation": "place_chip_on_transfer_stage",
            "sample_outcomes": [{"sample_id": "chip_001", "location": "transfer_stage"}]}
    assert edit(s, [step]).status == "ok"
    assert s.gateway.delegate("hands", task="s1: Place chip_001", require_sample_outcomes=True).allow
    s.gateway.delegation_started("a", "hands", "s1: Place chip_001")
    r = call(s, "hands", "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert call(s, "hands", "wait_job", {"job_id": r.data["job_id"]}).status in ("ok", "corrected")
    s.gateway.delegation_finished("a")
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "done"
    # Later independent processing changes state; it does not undo the recorded execution.
    s.store.upsert_sample("chip_001", "chip", location="mid_chip_1")
    assert edit(s, [{**step, "status": "done"}]).status == "ok"


class MixedJobs:
    """Only job execution is a fixture; gateway/tools/delegation checks are real."""
    def __init__(self, store, status):
        self.store, self.other_status, self.records = store, status, {}

    def submit(self, flow, params, **kwargs):
        rid = self.store.create_run(flow, params, **kwargs)
        status = "succeeded" if flow == "search_chip" else self.other_status
        self.store.set_run_status(rid, status)
        self.store.upsert_sample("chip_001", "chip", status="searched",
            location="mid_chip_1" if flow == "search_chip" else "transfer_stage",
            props={"location_uncertain": flow != "search_chip"})
        self.records[rid] = JobStatus(rid, flow, status, status != "running",
            steps=[{"seq": 1, "name": "fixture", "status": status, "corrections": 0}])
        return rid

    def wait(self, rid, timeout_s):
        return self.status(rid)

    def status(self, rid):
        return self.records[rid]


class RepeatedFlowJobs(MixedJobs):
    """Deterministic attempts; real tools, SQLite links and completion gates."""
    def submit(self, flow, params, **kwargs):
        state = self.other_status if not self.records else "succeeded"
        rid = self.store.create_run(flow, params, **kwargs)
        self.records[rid] = JobStatus(rid, flow, state, state != "running")
        self.settle(rid, state)
        return rid

    def settle(self, rid, state):
        flow = self.records[rid].flow
        self.store.set_run_status(rid, state)
        chip = self.store.get_run(rid)["params"]["chip_id"]
        self.store.upsert_sample(chip, "chip", location="transfer_stage" if state == "succeeded" else "mid_chip_1",
                                 props={"location_uncertain": state not in ("succeeded", "cancelled")})
        self.records[rid] = JobStatus(rid, flow, state, state not in ("running", "queued"),
            steps=[{"seq": 1, "name": "fixture", "status": state, "corrections": 0}])

    def retry(self, rid):
        self.settle(rid, "succeeded")


def placement(chip="chip_002", sid="s1"):
    return {"id": sid, "title": f"Place {chip}", "owner": "hands", "operation": "place_chip_on_transfer_stage",
            "sample_outcomes": [{"sample_id": chip, "location": "transfer_stage"}]}


def place(s, chip):
    submitted = call(s, "hands", "place_chip_on_transfer_stage", {"chip_id": chip})
    rid = submitted.data["job_id"]
    call(s, "hands", "wait_job", {"job_id": rid})
    return rid


@pytest.mark.parametrize("state", ["parked", "running", "failed", "queued", "paused", "waiting_human"])
@pytest.mark.parametrize("boundary", ["same_delegation", "next_delegation", "new_gateway"])
def test_distinct_jobs_of_one_flow_remain_outstanding_across_delegations(services, state, boundary):
    s = services
    s.store.update_project_state(s.project_id, eyes_checked=True)
    s.gateway.jobs = RepeatedFlowJobs(s.store, state)
    assert edit(s, [placement()]).status == "ok"
    s.gateway.delegation_started("a", "hands", "s1: Place chip_002")
    first = place(s, "chip_001")
    if boundary != "same_delegation":
        s.gateway.delegation_finished("a")
        if boundary == "new_gateway":
            s.gateway = ToolGateway(default_registry(), s.store, s.gateway.jobs, project_id=s.project_id, bench=s.lab.bench)
        s.store.update_project_state(s.project_id, eyes_checked=True)
        s.gateway.delegation_started("a", "hands", "s1: Place chip_002")
    second = place(s, "chip_002")
    facts = s.gateway.delegation_finished("a")
    assert first in facts and second in facts and state in facts
    assert s.store.get_project(s.project_id)["plan"][0]["status"] != "done"
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "complete"}).status == "error"
    assert s.store.get_sample("chip_002")["location"] == "transfer_stage"


def test_same_run_recovery_succeeds_after_a_new_gateway(services):
    s = services
    s.store.update_project_state(s.project_id, eyes_checked=True)
    s.gateway.jobs = RepeatedFlowJobs(s.store, "parked")
    assert edit(s, [placement("chip_001")]).status == "ok"
    s.gateway.delegation_started("a", "hands", "s1: Place chip_001")
    rid = place(s, "chip_001")
    s.gateway.delegation_finished("a")
    s.gateway = ToolGateway(default_registry(), s.store, s.gateway.jobs, project_id=s.project_id, bench=s.lab.bench)
    s.gateway.delegation_started("b", "hands", "s1: Recover chip_001")
    assert call(s, "hands", "retry_job", {"job_id": rid, "reason": "fixture camera restored"}).status == "pending"
    assert call(s, "hands", "wait_job", {"job_id": rid}).status == "ok"
    facts = s.gateway.delegation_finished("b")
    assert facts.count(f"job {rid}") == 1
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "done"
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "complete"}).status == "ok"
    # Completed history is resolved, not implicit proof of a new delegation.
    s.gateway.delegation_started("c", "hands", "s1: Place chip_001 again")
    assert "without running a hardware job" in s.gateway.delegation_finished("c")
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"


def test_project_gate_keeps_jobs_after_step_supersession_until_cancelled(services):
    s = services
    s.store.update_project_state(s.project_id, eyes_checked=True)
    s.gateway.jobs = RepeatedFlowJobs(s.store, "parked")
    assert edit(s, [placement("chip_001")]).status == "ok"
    s.gateway.delegation_started("a", "hands", "s1: Place chip_001")
    first = place(s, "chip_001")
    s.gateway.delegation_finished("a")
    iid = s.store.add_instruction("Replace s1 with s2.", project_id=s.project_id)
    assert change_plan(s, [placement("chip_002", "s2")], iid, "supersede").status == "ok"
    s.gateway.delegation_started("b", "hands", "s2: Place chip_002")
    place(s, "chip_002")
    s.gateway.delegation_finished("b")
    result = call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "complete"})
    assert result.status == "error" and first in result.summary
    s.gateway.jobs.settle(first, "cancelled")
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "complete"}).status == "ok"


def test_other_project_and_unlinked_archived_failures_do_not_block_current_work(services):
    s = services
    for pid in (s.store.create_project("older project"), s.project_id):
        rid = s.store.create_run("place_chip_on_transfer_stage", {"chip_id": "chip_003"}, project_id=pid)
        s.store.set_run_status(rid, "failed")
        if pid != s.project_id:
            s.store.emit("agent.job_linked", {"project_id": pid, "step_id": "s1", "job_id": rid}, run_id=rid)
    s.store.update_project_state(s.project_id, eyes_checked=True)
    s.gateway.jobs = RepeatedFlowJobs(s.store, "succeeded")
    assert edit(s, [placement()]).status == "ok"
    s.gateway.delegation_started("a", "hands", "s1: Place chip_002")
    place(s, "chip_002")
    s.gateway.delegation_finished("a")
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "complete"}).status == "ok"


@pytest.mark.parametrize("unresolved", ["reserved", "uncertain", "reactor_uncertain"])
def test_cancelled_attempt_with_unresolved_physical_state_is_not_retired(services, unresolved):
    s = services
    s.store.update_project_state(s.project_id, eyes_checked=True)
    s.gateway.jobs = RepeatedFlowJobs(s.store, "cancelled")
    assert edit(s, [placement()]).status == "ok"
    s.gateway.delegation_started("a", "hands", "s1: Place chip_002")
    first = place(s, "chip_001")
    if unresolved == "reserved":
        s.store.reserve("sample", "chip_001", first)
    else:
        key = "reactor_state_uncertain" if unresolved == "reactor_uncertain" else "location_uncertain"
        s.store.upsert_sample("chip_001", "chip", props={key: True})
    place(s, "chip_002")
    facts = s.gateway.delegation_finished("a")
    assert first in facts
    assert s.store.get_project(s.project_id)["plan"][0]["status"] != "done"
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "complete"}).status == "error"


@pytest.mark.parametrize("other", ["parked", "running"])
def test_mixed_jobs_never_skip_current_outcome_check(services, other):
    s = services
    s.store.update_project_state(s.project_id, eyes_checked=True)
    s.gateway.jobs = MixedJobs(s.store, other)
    assert edit(s, [{**SEARCH, "operation": "search_chip"},
                    {"id": "s2", "owner": "hands", "title": "Place chip_001 on transfer stage"}]).status == "ok"
    assert s.gateway.delegate("hands", task="s1: Search chip_001", require_sample_outcomes=True).allow
    s.gateway.delegation_started("a", "hands", "s1: Search chip_001")
    for tool in ("search_chip", "place_chip_on_transfer_stage"):
        r = call(s, "hands", tool, {"chip_id": "chip_001"})
        call(s, "hands", "wait_job", {"job_id": r.data["job_id"]})
    facts = s.gateway.delegation_finished("a")
    assert "uncertain" in facts and "pending" in facts
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"


@pytest.mark.parametrize("operation", [None, "place_chip_on_transfer_stage"])
def test_prohibited_exfoliation_does_not_create_advice_work(services, operation):
    s = services
    s.store.update_project_state(s.project_id, eyes_checked=True)
    step = {**SEARCH, "title": "Place chip_001", "operation": operation}
    assert edit(s, [step]).status == "ok"
    decision = s.gateway.delegate("hands", task="s1: Place chip_001. Do not exfoliate, search, or stack.",
                                  require_sample_outcomes=True)
    assert decision.allow if operation else decision.rule == "physical_operation_required"
    if operation is None:
        assert s.gateway.delegate("hands", task="s1: Do not search; then exfoliate graphene.").rule == "advisor_first"


def test_explicit_exfoliation_operation_requires_advice(services):
    s = services
    s.store.update_project_state(s.project_id, eyes_checked=True)
    step = {**SEARCH, "title": "Prepare chip_001", "operation": "exfoliate_chips",
            "sample_outcomes": [{"sample_id": "chip_001", "status": "exfoliated", "in_storage": True}]}
    assert edit(s, [step]).status == "ok"
    assert s.gateway.delegate("hands", task="s1: Prepare graphene").rule == "advisor_first"


@pytest.fixture
def reactor():
    cfg = quiet_bench()
    cfg["reactor"] = {"enabled": True}
    return build_services(":memory:", seed=1, testbench_config=cfg)


def disposition(mode="cold_storage"):
    return {"chip_id": "reactor_chip_01", "disposition": mode,
            "temperature_target_c": 820 if mode == "observation_hold" else None,
            "flow_target_sccm": 200 if mode == "observation_hold" else 0, "rationale": "test final disposition"}


def process(s, wanted, **changes):
    rid = s.store.create_run("observe_reactor", {"chip_id": wanted["chip_id"]}, project_id=s.project_id)
    s.store.set_run_status(rid, "succeeded")
    payload = {k: wanted[k] for k in ("chip_id", "temperature_target_c", "flow_target_sccm")}
    payload.update(temperature_c=820 if wanted["disposition"] == "observation_hold" else 22,
                   flow_sccm=wanted["flow_target_sccm"])
    s.store.emit("reactor.process", {**payload, **changes}, run_id=rid)
    return rid


def check_process(s, wanted):
    return unmet_reactor_outcome(s.store, s.lab.bench, wanted, project_id=s.project_id)


def preparation_step(status="reactor_pair_prepared", operation="prepare_reactor_pair"):
    return {"id": "s1", "title": "Prepare pair and store cold", "owner": "hands" if operation == "prepare_reactor_pair" else "reactor",
            "operation": operation, "reactor_outcome": disposition(),
            "sample_outcomes": [{"sample_id": "reactor_chip_01", "status": status, "in_storage": True}]}


def operation_step(owner, operation):
    return {**(preparation_step(None, operation) if owner == "reactor" else SEARCH), "owner": owner, "operation": operation}


@pytest.mark.parametrize("owner,operation", [
    ("reactor", "assess_reactor_pair_candidates"),
    ("hands", "exfoliate_and_search_one_chip"),
    ("reactor", "inspect_reactor_product_ordinary_microscope"),
    ("reactor", "get_reactor_setup"), ("hands", ""), ("hands", " search_chip "),
    ("reactor", "prepare_reactor_pair"), ("hands", "inspect_reactor_product"), ("reactor", "search_chip"),
])
@pytest.mark.parametrize("existing", [False, True])
def test_unknown_physical_operations_are_rejected_before_plan_write(reactor, owner, operation, existing):
    s = reactor
    if existing:
        assert edit(s, [operation_step(owner, None)]).status == "ok"
    before, history = s.store.get_project(s.project_id), s.store.plan_history(s.project_id)
    updates = [e for e in s.store.events_after(0, limit=10000) if e.kind == "plan.updated"]
    result = edit(s, [operation_step(owner, operation)])
    assert result.status == "error" and f"not executable by {owner}" in result.summary
    assert result.data["operation"] == operation
    assert result.data["owner"] == owner
    assert result.data["registered_operations"] == executable_flow_names(owner, s.gateway.registry)
    assert operation not in result.data["registered_operations"]
    assert s.store.get_project(s.project_id) == before and s.store.plan_history(s.project_id) == history
    assert [e for e in s.store.events_after(0, limit=10000) if e.kind == "plan.updated"] == updates
    assert not s.store.list_runs()


@pytest.mark.parametrize("owner,operation", [("hands", "search_chip"), ("hands", "revisit_chip"),
                                            ("reactor", "verify_reactor_state"), ("reactor", "inspect_reactor_product")])
def test_future_blank_operation_can_be_refined_but_not_delegated(reactor, owner, operation):
    s = reactor
    s.store.update_project_state(s.project_id, eyes_checked=True)
    assert edit(s, [operation_step(owner, None)]).status == "ok"
    decision = s.gateway.delegate(owner, task="s1: Execute the specified step", require_sample_outcomes=True)
    assert not decision.allow and decision.rule == "physical_operation_required"
    assert operation in decision.reason and not s.store.list_runs()
    assert edit(s, [operation_step(owner, operation)]).status == "ok"
    assert s.gateway.delegate(owner, task="s1: Execute the specified step", require_sample_outcomes=True).allow


@pytest.mark.parametrize("owner,alias", [("hands", "exfoliate_and_search_one_chip"),
                                         ("reactor", "inspect_reactor_product_ordinary_microscope")])
@pytest.mark.parametrize("status", ["pending", "done", "skipped"])
def test_unchanged_legacy_alias_remains_readable_but_never_executable(reactor, owner, alias, status):
    s = reactor
    old = {**operation_step(owner, alias), "status": status}
    s.store.set_plan(s.project_id, [old], author="fixture", note="old operation alias")
    s.store.update_project_state(s.project_id, eyes_checked=True)
    assert edit(s, [old, {"id": "s2", "title": "Report progress", "owner": "orchestrator"}]).status == "ok"
    saved = s.store.get_project(s.project_id)["plan"][0]
    assert saved["operation"] == alias and saved["status"] == status
    decision = s.gateway.delegate(owner, task="s1: Execute the specified step", require_sample_outcomes=True)
    assert not decision.allow and decision.rule == "physical_operation_required"
    assert not s.store.list_runs()


@pytest.mark.parametrize("replacement", [None, "inspect_reactor_product", "another_invented_flow"])
def test_pending_legacy_alias_is_not_automatically_mapped_or_erased(reactor, replacement):
    s = reactor
    old = operation_step("reactor", "inspect_reactor_product_ordinary_microscope")
    s.store.set_plan(s.project_id, [old], author="fixture", note="unknown legacy semantics")
    assert edit(s, [{**old, "operation": replacement}]).status == "error"
    assert s.store.get_project(s.project_id)["plan"] == [old]


def test_legacy_alias_cannot_be_reused_for_a_changed_sample_contract(reactor):
    s = reactor
    old = operation_step("reactor", "inspect_reactor_product_ordinary_microscope")
    s.store.set_plan(s.project_id, [old], author="fixture", note="unknown legacy semantics")
    changed = deepcopy(old)
    changed["sample_outcomes"][0]["status"] = "reactor_processed"
    assert edit(s, [changed]).status == "error"
    assert s.store.get_project(s.project_id)["plan"] == [old]


@pytest.mark.parametrize("owner,operation", [("reactor", "prepare_reactor_pair"), ("reactor", "search_chip")])
def test_legacy_wrong_role_flow_remains_readable_but_not_executable(reactor, owner, operation):
    s = reactor
    old = operation_step(owner, operation)
    s.store.set_plan(s.project_id, [old], author="fixture", note="legacy wrong owner")
    s.store.update_project_state(s.project_id, eyes_checked=True)
    assert edit(s, [old]).status == "ok"
    decision = s.gateway.delegate(owner, task="s1: Execute the specified step", require_sample_outcomes=True)
    assert not decision.allow and decision.rule == "physical_operation_required"
    assert operation not in executable_flow_names(owner, s.gateway.registry)
    assert not s.store.list_runs()


def test_revisit_mapping_requires_hands_actual_search_tool_permission(reactor):
    s = reactor
    assert executable_flow_names("hands", s.gateway.registry) == [
        "assess_reactor_inputs", "exfoliate_chips", "place_chip_on_transfer_stage", "prepare_material_metal", "prepare_reactor_pair", "revisit_chip", "search_chip", "stack_device"]
    assert "inspect_reactor_product" in executable_flow_names("reactor", s.gateway.registry)
    assert "revisit_chip" not in executable_flow_names("reactor", s.gateway.registry)
    limited = ToolRegistry()
    for tool in s.gateway.registry.all():
        if tool.name != "search_chip":
            limited.add(tool)
    s.gateway.registry = limited
    result = edit(s, [operation_step("hands", "revisit_chip")])
    assert result.status == "error"
    assert "search_chip" not in result.data["registered_operations"]
    assert "revisit_chip" not in result.data["registered_operations"]
    assert "prepare_reactor_pair" in result.data["registered_operations"]
    s.store.set_plan(s.project_id, [operation_step("hands", "revisit_chip")], author="fixture")
    s.store.update_project_state(s.project_id, eyes_checked=True)
    assert s.gateway.delegate("hands", task="s1: Revisit chip_001", require_sample_outcomes=True).rule == "physical_operation_required"


def test_operation_contract_loads_the_flow_registry_in_a_fresh_process():
    result = subprocess.run([sys.executable, "-c", """
from qumus2.flows.flow import FLOWS
from qumus2.records.plan_completion import registered_flow_names
assert not FLOWS
names = registered_flow_names()
assert names == sorted(FLOWS)
assert {'search_chip', 'prepare_reactor_pair', 'assess_reactor_inputs', 'inspect_reactor_product'} <= set(names)
"""], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def legacy_preparation(s, status="stacked"):
    # Import an old plan directly: the model-facing admission must now reject it.
    s.store.set_plan(s.project_id, [preparation_step(status)], author="fixture", note="legacy obligation")


def preparation_evidence(s, *, linked=True, job_status="succeeded", project=None, step="s1", **event_changes):
    rid = s.store.create_run("prepare_reactor_pair", {"target_chip_id": "reactor_chip_01",
        "flake_ids": ["input_a", "input_b"]}, project_id=project or s.project_id)
    s.store.set_run_status(rid, job_status)
    if linked:
        s.store.emit("agent.job_linked", {"project_id": project or s.project_id, "step_id": step,
            "job_id": rid, "flow": "prepare_reactor_pair"}, run_id=rid)
    s.store.emit("reactor.prepared", {"chip_id": "reactor_chip_01", "preparation_id": rid,
        "preparation_source": "stacking_job", "input_flake_ids": ["input_b", "input_a"], **event_changes}, run_id=rid)
    return rid


@pytest.mark.parametrize("existing", [False, True])
def test_plan_rejects_impossible_flow_role_status_before_writing(reactor, existing):
    s = reactor
    if existing:
        assert edit(s, [{"id": "s1", "title": "Prepare pair", "owner": "hands"}]).status == "ok"
    before = s.store.get_project(s.project_id)
    history = s.store.plan_history(s.project_id)
    result = edit(s, [preparation_step("stacked")])
    assert result.status == "error" and "reactor_pair_prepared" in result.summary
    assert result.data["outcome_contract_errors"][0]["role"] == "carrier"
    assert s.store.get_project(s.project_id) == before
    assert s.store.plan_history(s.project_id) == history
    assert not s.store.list_runs()


@pytest.mark.parametrize("operation", ["inspect_reactor_product", "unload_reactor_chip", "verify_reactor_state",
                                       "prepare_reactor_pair", "reactor_reaction", None])
@pytest.mark.parametrize("existing", [False, True])
def test_plan_rejects_stored_carrier_pseudostatus_before_writing(reactor, operation, existing):
    s = reactor
    step = preparation_step("stored", operation)
    if existing:
        assert edit(s, [{"id": "s1", "title": "Prepare and inspect carrier", "owner": step["owner"]}]).status == "ok"
    before, history = s.store.get_project(s.project_id), s.store.plan_history(s.project_id)
    events = [e for e in s.store.events_after(0, limit=10000) if e.kind == "plan.updated"]
    result = edit(s, [step])
    assert result.status == "error" and "in_storage: true" in result.summary
    assert result.data["outcome_contract_errors"][0]["declared_status"] == "stored"
    assert "get_reactor_setup" in result.summary
    assert s.store.get_project(s.project_id) == before and s.store.plan_history(s.project_id) == history
    assert [e for e in s.store.events_after(0, limit=10000) if e.kind == "plan.updated"] == events
    assert not s.store.list_runs()


@pytest.mark.parametrize("status", [None, "reactor_pair_prepared", "reactor_processed"])
def test_inspection_storage_requirement_does_not_invent_or_infer_status(reactor, status):
    s = reactor
    step = preparation_step(status, "inspect_reactor_product")
    assert edit(s, [step]).status == "ok"
    saved = s.store.get_project(s.project_id)["plan"][0]["sample_outcomes"][0]
    assert saved["in_storage"] and saved["status"] == status


def test_other_reactor_disposition_target_cannot_hide_a_stored_pseudostatus(reactor):
    step = preparation_step("stored", "inspect_reactor_product")
    step["reactor_outcome"]["chip_id"] = "reactor_chip_02"
    result = edit(reactor, [step])
    assert result.status == "error" and "in_storage: true" in result.summary


def test_existing_stored_obligation_cannot_be_replaced_with_an_arbitrary_real_status(reactor):
    s = reactor
    old = preparation_step("stored", "inspect_reactor_product")
    s.store.set_plan(s.project_id, [old], author="fixture", note="legacy impossible status")
    step = preparation_step("reactor_processed", "inspect_reactor_product")
    assert edit(s, [step]).status == "error"
    assert s.store.plan_history(s.project_id)[-1]["plan"] == [old]


def storage_evidence(s, operation="inspect_reactor_product", *, job_status="succeeded", linked=True,
                     project=None, step="s1", chip="reactor_chip_01"):
    rid = s.store.create_run(operation, {"chip_id": chip}, project_id=project or s.project_id)
    s.store.set_run_status(rid, job_status)
    if linked:
        s.store.emit("agent.job_linked", {"project_id": project or s.project_id, "step_id": step,
            "job_id": rid, "flow": operation}, run_id=rid)
    return rid


@pytest.mark.parametrize("operation", ["inspect_reactor_product", "unload_reactor_chip", "verify_reactor_state"])
@pytest.mark.parametrize("executed", [False, True])
def test_redundant_stored_status_can_be_corrected_without_erasing_storage(reactor, operation, executed):
    s = reactor
    old = preparation_step("stored", operation)
    s.store.set_plan(s.project_id, [old], author="fixture", note="legacy storage typo")
    rid = storage_evidence(s, operation) if executed else None
    sample = s.store.get_sample("reactor_chip_01")
    result = edit(s, [preparation_step(None, operation)])
    assert result.status == "ok", result.summary
    correction = result.data["outcome_corrections"][0]
    assert correction["correction_kind"] == "stored_location_redundancy"
    assert correction["declared_status"] == "stored" and correction["allowed_status"] is None
    assert (correction["execution_evidence"]["job_id"] if executed else correction["execution_evidence"]) == rid
    saved = s.store.get_project(s.project_id)["plan"]
    assert saved[0]["status"] == "pending"
    assert saved[0]["sample_outcomes"][0]["status"] is None and saved[0]["sample_outcomes"][0]["in_storage"]
    assert saved[0]["reactor_outcome"] == old["reactor_outcome"]
    assert s.store.plan_history(s.project_id)[-1]["plan"] == saved
    event = [e for e in s.store.events_after(0, limit=10000) if e.kind == "plan.updated"][-1]
    assert event.payload["plan"] == saved
    assert s.store.get_sample("reactor_chip_01") == sample
    assert edit(s, saved).status == "ok"
    assert s.store.get_project(s.project_id)["plan"][0]["outcome_corrections"] == [correction]
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "must verify"}).status == "error"


@pytest.mark.parametrize("bad", ["remove_storage", "no_original_storage", "real_status", "change_owner",
    "change_flow", "unsupported_flow", "change_chip", "unknown_carrier", "change_disposition", "remove_location",
    "remove_other_sample", "drop_future_step", "done", "skip"])
def test_stored_correction_preserves_every_real_obligation(reactor, bad):
    s = reactor
    old = preparation_step("stored", "inspect_reactor_product")
    old["sample_outcomes"][0]["location"] = "reactor_chip_slot_1"
    old["sample_outcomes"].append({"sample_id": "chip_001", "in_storage": True})
    if bad == "no_original_storage":
        old["sample_outcomes"][0]["in_storage"] = False
    elif bad == "real_status":
        old["sample_outcomes"][0]["status"] = "reactor_processed"
    elif bad == "unsupported_flow":
        old["operation"] = "shutdown_reactor"
    elif bad == "unknown_carrier":
        old["sample_outcomes"][0]["sample_id"] = "chip_002"
        old["reactor_outcome"]["chip_id"] = "chip_002"
    future = {**SEARCH, "id": "future"}
    s.store.set_plan(s.project_id, [old, future], author="fixture", note="legacy obligations")
    step = deepcopy(old)
    step["sample_outcomes"][0]["status"] = None
    if bad == "remove_storage":
        step["sample_outcomes"][0]["in_storage"] = False
    elif bad == "change_owner":
        step["owner"] = "hands"
    elif bad == "change_flow":
        step["operation"] = "unload_reactor_chip"
    elif bad == "change_chip":
        step["sample_outcomes"][0]["sample_id"] = "reactor_chip_02"
        step["reactor_outcome"]["chip_id"] = "reactor_chip_02"
    elif bad == "change_disposition":
        step["reactor_outcome"] = disposition("observation_hold")
    elif bad == "remove_location":
        step["sample_outcomes"][0].pop("location")
    elif bad == "remove_other_sample":
        step["sample_outcomes"].pop()
    elif bad in ("done", "skip"):
        step["status"] = "done" if bad == "done" else "skipped"
    plan = [step] if bad == "drop_future_step" else [step, future]
    assert edit(s, plan).status == "error"
    assert s.store.get_project(s.project_id)["plan"] == [old, future]
    assert s.store.get_project(s.project_id)["plan_revision"] == 1


@pytest.mark.parametrize("bad", ["parked", "unlinked", "other_step", "other_project", "wrong_chip", "wrong_flow"])
def test_stored_correction_requires_matching_successful_job_after_submission(reactor, bad):
    s = reactor
    old = preparation_step("stored", "inspect_reactor_product")
    s.store.set_plan(s.project_id, [old], author="fixture", note="legacy storage typo")
    kwargs = {"parked": {"job_status": "parked"}, "unlinked": {"linked": False}, "other_step": {"step": "s9"},
              "other_project": {"project": "foreign"}, "wrong_chip": {"chip": "reactor_chip_02"},
              "wrong_flow": {"operation": "verify_reactor_state"}}[bad]
    if bad == "other_project":
        storage_evidence(s, job_status="parked")
    storage_evidence(s, **kwargs)
    assert edit(s, [preparation_step(None, "inspect_reactor_product")]).status == "error"
    assert s.store.get_project(s.project_id)["plan_revision"] == 1


def test_storage_typo_correction_does_not_retire_an_unfinished_linked_job(reactor):
    s = reactor
    old = preparation_step("stored", "inspect_reactor_product")
    s.store.set_plan(s.project_id, [old], author="fixture", note="legacy storage typo")
    good = storage_evidence(s)
    parked = storage_evidence(s, job_status="parked")
    assert edit(s, [preparation_step(None, "inspect_reactor_product")]).status == "ok"
    s.gateway.delegation_started("inspect", "reactor", "s1: Inspect reactor product")
    s.gateway._open["inspect"].calls.append({"tool": "inspect_reactor_product", "status": "ok", "summary": "fixture",
        "data": {"job_id": good, "flow": "inspect_reactor_product"}})
    facts = s.gateway.delegation_finished("inspect")
    assert parked in facts and "parked" in facts
    assert s.store.get_project(s.project_id)["plan"][0]["status"] != "done"
    assert s.store.get_run(parked)["status"] == "parked"
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "must refuse"}).status == "error"
    # Exercise the independent project-level job guard after the step-level check.
    s.store.set_step_status(s.project_id, "s1", "done")
    result = call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "still must refuse"})
    assert result.status == "error" and parked in result.summary


def test_flow_contract_distinguishes_carrier_input_flakes_and_status_preserving_moves(reactor):
    s = reactor
    s.store.upsert_sample("input_a", "flake", location="chip_001", status="catalogued")
    step = preparation_step()
    step["sample_outcomes"].append({"sample_id": "input_a", "status": "stacked", "location": "reactor_chip_01"})
    wrong = deepcopy(step)
    wrong["sample_outcomes"][1]["status"] = "reactor_pair_prepared"
    assert edit(s, [wrong]).status == "error"
    assert edit(s, [step]).status == "ok"
    setup = call(s, "orchestrator", "get_lab_setup").data["fixed_flow_sample_statuses"]
    reactor_setup = call(s, "orchestrator", "get_reactor_setup").data["completion_contracts"]
    assert setup["exfoliate_chips"] == {"receiving_chip": "exfoliated"}
    assert "exfoliate_chips" not in reactor_setup
    for operation in ("prepare_reactor_pair", "reactor_reaction", "prepare_material_metal", "reactor_material_metal"):
        for role, status in setup[operation].items():
            assert reactor_setup[operation][role]["status"] == status
    move = {**preparation_step(operation="unload_reactor_chip"), "id": "s2"}
    assert edit(s, [step, move]).status == "ok"


@pytest.mark.parametrize("executed", [False, True])
def test_legacy_cross_role_correction_is_audited_atomic_and_remains_pending(reactor, executed):
    s = reactor
    legacy_preparation(s)
    rid = preparation_evidence(s) if executed else None
    before = s.store.get_sample("reactor_chip_01")
    result = edit(s, [preparation_step()])
    assert result.status == "ok", result.summary
    correction = result.data["outcome_corrections"][0]
    assert correction["declared_status"] == "stacked" and correction["allowed_status"] == "reactor_pair_prepared"
    assert (correction["execution_evidence"]["job_id"] if executed else correction["execution_evidence"]) == rid
    project = s.store.get_project(s.project_id)
    assert project["plan"][0]["status"] == "pending"
    assert project["plan"][0]["outcome_corrections"] == [correction]
    assert s.store.plan_history(s.project_id)[-1]["plan"] == project["plan"]
    event = [e for e in s.store.events_after(0, limit=10000) if e.kind == "plan.updated"][-1]
    assert event.payload["plan"] == project["plan"]
    assert s.store.get_sample("reactor_chip_01") == before
    assert call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "prepared"}).status == "error"
    assert edit(s, project["plan"]).status == "ok"
    assert s.store.get_project(s.project_id)["plan"][0]["outcome_corrections"] == [correction]
    assert s.store.plan_history(s.project_id)[0]["plan"][0]["sample_outcomes"][0]["status"] == "stacked"


@pytest.mark.parametrize("bad", ["parked", "unlinked", "other_step", "other_project", "wrong_event_chip", "wrong_inputs"])
def test_legacy_status_repair_requires_this_steps_verified_success_after_submission(reactor, bad):
    s = reactor
    legacy_preparation(s)
    kwargs = {"job_status": "parked"} if bad == "parked" else {
        "unlinked": {"linked": False}, "other_step": {"step": "s9"},
        "other_project": {"project": "foreign"}, "wrong_event_chip": {"chip_id": "reactor_chip_02"},
        "wrong_inputs": {"input_flake_ids": ["other_a", "other_b"]}}[bad]
    if bad == "other_project":
        preparation_evidence(s, job_status="parked")
    preparation_evidence(s, **kwargs)
    before = s.store.plan_history(s.project_id)
    assert edit(s, [preparation_step()]).status == "error"
    assert s.store.plan_history(s.project_id) == before


@pytest.mark.parametrize("bad", ["remove_storage", "change_chip", "change_operation", "change_owner", "done", "skip", "arbitrary_status"])
def test_status_correction_never_authorizes_other_obligation_changes(reactor, bad):
    s = reactor
    legacy_preparation(s, "reactor_processed" if bad == "arbitrary_status" else "stacked")
    preparation_evidence(s)
    step = preparation_step()
    if bad == "remove_storage":
        step["sample_outcomes"][0]["in_storage"] = False
    elif bad == "change_chip":
        step["sample_outcomes"][0]["sample_id"] = "reactor_chip_02"
        step["reactor_outcome"]["chip_id"] = "reactor_chip_02"
    elif bad == "change_operation":
        step["operation"] = "unload_reactor_chip"
    elif bad == "change_owner":
        step["owner"] = "reactor"
    elif bad in ("done", "skip"):
        step["status"] = "done" if bad == "done" else "skipped"
    assert edit(s, [step]).status == "error"
    assert s.store.get_project(s.project_id)["plan_revision"] == 1


def test_contract_correction_cannot_certify_an_inflight_delegation_or_overwrite_stale_plan(reactor):
    s = reactor
    legacy_preparation(s)
    version = s.store.get_project(s.project_id)["plan_version"]
    s.gateway.delegation_started("preparation", "hands", "s1: Prepare pair")
    preparation_evidence(s)
    result = call(s, "orchestrator", "update_plan", {"plan": [preparation_step()], "note": "correct role",
        "expected_version": version})
    assert result.status == "error" and "changed" in result.summary
    assert not s.store.get_project(s.project_id)["plan"][0].get("outcome_corrections")
    assert edit(s, [preparation_step()]).status == "ok"
    failures = s.gateway._unmet_contract(s.gateway._open["preparation"], "s1")
    assert any("conditions changed" in item for item in failures)
    assert any("expected stacked" in item for item in failures)
    s.gateway.delegation_finished("preparation")
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"


def test_unchanged_historical_completion_is_not_rewritten_by_new_admission(reactor):
    s = reactor
    legacy_preparation(s)
    s.store.set_step_status(s.project_id, "s1", "done")
    assert edit(s, [preparation_step("stacked")]).status == "ok"
    step = s.store.get_project(s.project_id)["plan"][0]
    assert step["status"] == "done" and step["sample_outcomes"][0]["status"] == "stacked"


def test_reactor_verification_requires_disposition_but_no_pair_goal(reactor):
    s = reactor
    s.store.update_project_state(s.project_id, eyes_checked=True)
    step = {"id": "s1", "owner": "reactor", "title": "Verify cold carrier", "operation": "verify_reactor_state"}
    assert edit(s, [step]).status == "ok"
    assert s.gateway.delegate("reactor", task="s1: Prepare pair", require_sample_outcomes=True).rule == "reactor_outcome_required"
    assert edit(s, [{**step, "reactor_outcome": disposition()}]).status == "ok"
    assert s.gateway.delegate("reactor", task="s1: Prepare pair", require_sample_outcomes=True).allow
    assert s.store.get_project(s.project_id)["state"].get("reactor_goal") is None


@pytest.mark.parametrize("mode", ["cold_storage", "observation_hold"])
def test_reactor_process_evidence_and_feedback(reactor, mode):
    s, wanted = reactor, disposition(mode)
    if mode == "observation_hold":
        s.store.upsert_sample(wanted["chip_id"], "chip", location="reactor_port")
    assert check_process(s, wanted)
    process(s, wanted)
    assert check_process(s, wanted) == []
    if mode == "cold_storage":
        process(s, wanted, temperature_c=120)
        assert "cold-access" in " ".join(check_process(s, wanted))
    else:
        process(s, wanted, temperature_c=22, flow_sccm=0)
        assert "stabilized" in " ".join(check_process(s, wanted))
    process(s, wanted)
    s.store.emit("device.command", {"device": "reactor_heater", "command": "set_target", "params": {"celsius": 900}})
    assert "later control" in " ".join(check_process(s, wanted))


@pytest.mark.parametrize("bad", ["other_chip", "other_project", "parked", "unknown_reading"])
def test_reactor_rejects_unrelated_or_unsuccessful_readback(reactor, bad):
    s, wanted = reactor, disposition()
    rid = process(s, wanted)
    if bad == "other_chip":
        s.store.set_run_params(rid, {"chip_id": "reactor_chip_02"}, note="test")
    elif bad == "other_project":
        s.store._x("UPDATE run SET project_id='other' WHERE id=?", (rid,))
    elif bad == "parked":
        s.store.set_run_status(rid, "parked")
    else:
        process(s, wanted, temperature_c=None)
    assert check_process(s, wanted)


def test_other_reactor_step_cannot_skip_product_acceptance(reactor):
    s, wanted = reactor, disposition("observation_hold")
    steps = [{"id": sid, "owner": "reactor", "title": title, "operation": flow,
              "reactor_outcome": wanted} for sid, title, flow in (
                  ("s1", "Load reactor", "load_reactor_chip"),
                  ("s2", "React pair", "reactor_reaction"))]
    assert edit(s, steps).status == "ok"
    s.gateway.delegation_started("a", "reactor", "s1: Load reactor")
    s.store.upsert_sample(wanted["chip_id"], "chip", location="reactor_port")
    for flow in ("load_reactor_chip", "reactor_reaction"):
        rid = s.store.create_run(flow, {"chip_id": wanted["chip_id"]}, project_id=s.project_id)
        s.store.set_run_status(rid, "succeeded")
        s.gateway._open["a"].calls.append({"tool": flow, "status": "ok", "summary": "fixture",
                                          "data": {"flow": flow, "job_id": rid}})
    process(s, wanted)
    facts = s.gateway.delegation_finished("a")
    assert "No current code-checked reactor target was accepted" in facts
    assert all(step["status"] == "pending" for step in s.store.get_project(s.project_id)["plan"])


def test_old_checkpoint_preserves_legacy_morphology_and_new_checkpoint_profile(tmp_path):
    s = build_services(":memory:", seed=1, testbench_config=quiet_bench())
    s.lab.world.material_morphology = {"name": "saved profile"}
    checkpoint = tmp_path / "world.pkl"
    save_lab_state(s.lab, s.store.clock, checkpoint)
    from qumus2.testbench.persist import load_lab_state
    s.lab.world.material_morphology = {"name": "new default"}
    load_lab_state(s.lab, s.store.clock, checkpoint)
    assert s.lab.world.material_morphology == {"name": "saved profile"}
    old = pickle.loads(checkpoint.read_bytes())
    old["world"].pop("material_morphology")
    checkpoint.write_bytes(pickle.dumps(old))
    s.lab.world.material_morphology = {"name": "new default"}
    load_lab_state(s.lab, s.store.clock, checkpoint)
    assert s.lab.world.material_morphology is None


def test_prepared_material_initialized_before_camera_and_restored_in_storage(tmp_path):
    cfg = quiet_bench()
    cfg["reactor"] = {"enabled": True, "synthetic_prepared_pairs": True}
    db, checkpoint = tmp_path / "q.db", tmp_path / "world.pkl"
    side = build_lab_side(db, seed=1, testbench_config=cfg)
    chip = side.lab.world.objects["reactor_chip_01"]
    assert len(chip.flakes) == 2
    location = side.store.get_sample(chip.id)["location"]
    assert side.lab.bench["slots"][location]["station"] == "reactor_storage"
    before = deepcopy(lab_state(side.lab, side.clock))
    samples = side.store.list_samples()
    events = side.store.events_after(limit=10000)
    save_lab_state(side.lab, side.clock, checkpoint)
    side.store.close()
    restored = build_lab_side(db, state_path=checkpoint, testbench_config=cfg)
    after = lab_state(restored.lab, restored.clock)
    assert after["world"]["objects"] == before["world"]["objects"]
    assert after["world"]["reactor_state"]["pair_models"] == before["world"]["reactor_state"]["pair_models"]
    assert after["world"]["rng"] == before["world"]["rng"]
    assert after["clock"] == before["clock"]
    assert restored.store.list_samples() == samples
    assert restored.store.events_after(limit=10000) == events
    restored.store.close()


def test_material_assessment_and_area_survive_database_and_actual_ui_event_replay(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise the inventory replay consumer")
    path = tmp_path / "q.db"
    store = RunStore(path)
    store.upsert_sample("flake", "flake", location="source_chip", status="catalogued", props={"area_um2": 768})
    historical = store.events_after(limit=1)[0].model_dump()
    values = {"reactor_pickup_assessment": {"suitable": True, "job_id": "inspection_job", "images": []},
              "measured_area_um2": 768, "geometry_area_um2": 800}
    store.upsert_sample("flake", "flake", props=values)
    store.upsert_sample("flake", "flake", location="target_chip", status="stacked")
    store.close()
    reopened = RunStore(path)
    props = reopened.get_sample("flake")["props"]
    assert {k: props[k] for k in values} == values
    events = [e.model_dump() for e in reopened.events_after(limit=100)]
    assert events[0] == historical
    assert not set(values) & events[0]["payload"]["props"].keys()
    script = """
import { readFileSync } from 'node:fs';
import { Inventory } from './qumus2/ui/static/inventory.js';
const inventory = new Inventory();
for (const event of JSON.parse(readFileSync(0, 'utf8'))) inventory.add(event);
process.stdout.write(JSON.stringify(inventory.samples.get('flake')));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(events),
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    replayed = json.loads(result.stdout)
    assert {k: replayed["props"][k] for k in values} == values
    assert replayed["props"]["area_um2"] == 768 and replayed["location"] == "target_chip"
    reopened.close()
