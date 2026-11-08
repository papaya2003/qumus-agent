"""Receiving-chip admission and evidenced repairs of legacy carrier/output mistakes."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from qumus2.agents.readiness import RECIPE_FIELDS
from qumus2.agents.tools.hardware import ExfoliateArgs
from qumus2.flows.library.exfoliation import ExfoliationParams


def call(s, role, tool, args=None):
    return asyncio.run(s.gateway.call(role, tool, args or {}))[0]


def step(sample="carrier_hbn_01", material="hBN"):
    recipe = {key: ExfoliateArgs.model_fields[key].default for key in RECIPE_FIELDS}
    return {"id": "s1", "owner": "hands", "title": f"Exfoliate {material}", "status": "pending",
        "operation": "exfoliate_chips", "materials": [material], "operation_args": {"material": material, **recipe},
        "sample_outcomes": [{"sample_id": sample, "status": "exfoliated", "in_storage": True}]}


def refine(s, desired):
    return call(s, "orchestrator", "refine_plan", {
        "expected_revision": s.store.get_project(s.project_id)["plan_revision"],
        "updates": [{"step_id": desired["id"], "fields": {
            key: desired[key] for key in ("sample_outcomes", "operation_args")}}],
        "note": "Correct the model-written output using the original successful job"})


def test_new_carrier_output_is_refused_but_source_storage_condition_is_valid(services):
    s = services
    before = deepcopy(s.store.get_project(s.project_id))
    result = call(s, "orchestrator", "update_plan", {"plan": [step()], "note": "Wrong output role"})
    assert result.status == "error"
    assert result.data["outcome_contract_errors"][0]["role"] == "source_carrier"
    assert s.store.get_project(s.project_id) == before
    good = step("chip_001")
    good["sample_outcomes"].append({"sample_id": "carrier_hbn_01", "status": "available", "in_storage": True})
    assert call(s, "orchestrator", "update_plan", {"plan": [good], "note": "Preserve the source"}).status == "ok"


@pytest.mark.parametrize("material,carrier", [("hBN", "carrier_hbn_01"), ("Gr", "carrier_gr_01")])
def test_actual_exfoliation_repair_rechecks_original_job_without_repeating_motion(services, material, carrier):
    s = services
    assert call(s, "eyes", "inspect_bench").status in ("ok", "corrected")
    assert call(s, "advisor", "get_experience", {"material": material}).status == "ok"
    good = step("chip_001", material)
    good["sample_outcomes"].append({"sample_id": carrier, "in_storage": True})
    assert call(s, "orchestrator", "update_plan", {
        "plan": [good], "task_kind": "experiment", "note": "Actual offline flow"}).status == "ok"
    assert s.gateway.delegate("hands", task="s1: Exfoliate", require_sample_outcomes=True).allow
    s.gateway.delegation_started("execute", "hands", "s1: Exfoliate")
    result = call(s, "hands", "exfoliate_chips", {**good["operation_args"], "chip_ids": ["chip_001"]})
    assert result.status == "pending", result
    rid = result.data["job_id"]
    assert call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 60}).status in ("ok", "corrected")
    assert "now done" in s.gateway.delegation_finished("execute")
    legacy = deepcopy(good)
    legacy["sample_outcomes"][0]["sample_id"] = carrier
    # Simulate the old model-authored contract that admission now prevents.
    s.store.set_plan(s.project_id, [legacy], author="orchestrator", note="Previously admitted output-role mistake")
    assert not s.gateway.delegate("hands", task="s1: Exfoliate", require_sample_outcomes=True).allow
    before = s.store.last_event_id()
    result = refine(s, good)
    assert result.status == "ok", result
    correction = result.data["outcome_corrections"][0]
    assert correction["execution_evidence"]["job_id"] == rid
    assert correction["execution_evidence"]["storage_verification_event_id"]
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"
    assert s.gateway.delegate("hands", task="s1: Recheck original job", require_sample_outcomes=True).allow
    s.gateway.delegation_started("recheck", "hands", "s1: Recheck original job")
    assert call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 1}).status == "ok"
    assert "now done" in s.gateway.delegation_finished("recheck")
    assert not any(e.kind in ("job.submitted", "device.command") for e in s.store.events_after(before, limit=10000))
    assert len([r for r in s.store.list_runs() if r["flow"] == "exfoliate_chips"]) == 1
    assert s.store.get_sample(carrier)["status"] == "available"


@pytest.fixture
def legacy_output(services):
    """Explicitly synthetic typed evidence for rejection-path tests."""
    s = services
    old, desired = step(), step("chip_001")
    old["sample_outcomes"].append({"sample_id": "carrier_hbn_01", "in_storage": True})
    desired["sample_outcomes"].append({"sample_id": "carrier_hbn_01", "in_storage": True})
    s.store.set_plan(s.project_id, [old], author="orchestrator", note="Legacy model output typo")
    s.store.update_project_state(s.project_id, task_kind="experiment", eyes_checked=True, experience_checked=["hBN"])
    s.store.upsert_sample("chip_001", "chip", location="mid_chip_1", status="exfoliated",
                          props={"material": "hBN", "from_carrier": "carrier_hbn_01"})
    params = ExfoliationParams(**old["operation_args"], chip_ids=["chip_001"], carrier_id="carrier_hbn_01",
        carrier_slot="left_slide_2", home_slots=["left_chip_1"], store_slots=["mid_chip_1"]).model_dump()
    rid = s.store.create_run("exfoliate_chips", params, project_id=s.project_id)
    s.store.emit("agent.job_linked", {"project_id": s.project_id, "step_id": "s1", "job_id": rid,
                                     "flow": "exfoliate_chips"}, run_id=rid, agent="hands")
    s.store.emit("step.verified", {"name": "store_exfoliated_chip", "verdict": "ok",
        "expected": {"sample": "chip_001", "at": "mid_chip_1"}, "measurements": {"found_at": "mid_chip_1"}}, run_id=rid)
    s.store.set_run_status(rid, "succeeded")
    return s, rid, old, desired


@pytest.mark.parametrize("bad", ["wrong_step", "wrong_project", "wrong_flow", "failed_job", "running_job", "no_link",
    "multi_chip", "second_job", "reused_output", "wrong_source", "wrong_material", "missing_origin", "wrong_origin",
    "uncertain", "reserved", "wrong_target", "drop_storage", "drop_other_obligation", "fixed_location", "changed_recipe",
    "changed_source_arg", "changed_new_arg", "no_verification", "wrong_verification_sample", "wrong_verification_slot",
    "human_other_target", "human_other_target_chinese", "human_source", "human_authored_plan", "manual_done", "metadata_recipe_collision", "unknown_metadata_arg"])
def test_repair_refuses_missing_conflicting_or_ambiguous_evidence_atomically(legacy_output, bad):
    s, rid, old, desired = legacy_output
    if bad == "wrong_step":
        s.store._x("UPDATE event SET payload_json=json_set(payload_json,'$.step_id','s2') WHERE kind='agent.job_linked'")
    elif bad == "wrong_project":
        s.store._x("UPDATE run SET project_id='another_project' WHERE id=?", (rid,))
    elif bad == "wrong_flow":
        s.store._x("UPDATE run SET flow='search_chip' WHERE id=?", (rid,))
    elif bad in ("failed_job", "running_job"):
        s.store.set_run_status(rid, "failed" if bad == "failed_job" else "running")
    elif bad == "no_link":
        s.store._x("DELETE FROM event WHERE kind='agent.job_linked'")
    elif bad in ("multi_chip", "wrong_source", "changed_recipe"):
        params = s.store.get_run(rid)["params"]
        params.update({"multi_chip": {"num_chips": 2, "chip_ids": ["chip_001", "chip_002"]},
                       "wrong_source": {"carrier_id": "carrier_gr_01"}, "changed_recipe": {"dwell_s": 20}}[bad])
        s.store.set_run_params(rid, params, note="Synthetic mismatch")
    elif bad in ("second_job", "reused_output"):
        other = s.store.create_run("exfoliate_chips", s.store.get_run(rid)["params"], project_id=s.project_id)
        s.store.set_run_status(other, "succeeded")
        if bad == "second_job":
            s.store.emit("agent.job_linked", {"project_id": s.project_id, "step_id": "s1"}, run_id=other)
    elif bad in ("wrong_material", "missing_origin", "wrong_origin", "uncertain"):
        props = {"wrong_material": {"material": "Gr"}, "missing_origin": {"from_carrier": None},
                 "wrong_origin": {"from_carrier": "carrier_gr_01"}, "uncertain": {"location_uncertain": True}}[bad]
        s.store.upsert_sample("chip_001", "chip", props=props)
    elif bad == "reserved":
        s.store.reserve("sample", "chip_001", rid, "Synthetic conflicting work")
    elif bad == "wrong_target":
        desired["sample_outcomes"][0]["sample_id"] = "chip_002"
    elif bad == "drop_storage":
        desired["sample_outcomes"][0].pop("in_storage")
    elif bad == "drop_other_obligation":
        desired["sample_outcomes"].pop()
    elif bad in ("fixed_location", "changed_source_arg"):
        if bad == "fixed_location":
            old["sample_outcomes"][0]["location"] = "left_slide_2"
        else:
            old["operation_args"]["from_carrier"] = "carrier_gr_01"
            desired["operation_args"]["from_carrier"] = "carrier_gr_01"
        s.store.set_plan(s.project_id, [old], author="orchestrator", note="Stricter original obligation")
    elif bad == "changed_new_arg":
        desired["operation_args"]["chip_ids"] = ["chip_002"]
    elif bad == "no_verification":
        s.store._x("DELETE FROM event WHERE kind='step.verified'")
    elif bad.startswith("wrong_verification"):
        key, value = ("sample", "chip_002") if bad.endswith("sample") else ("at", "mid_chip_2")
        s.store._x("UPDATE event SET payload_json=json_set(payload_json,?,?) WHERE kind='step.verified'",
                   (f"$.expected.{key}", value))
    elif bad.startswith("human_") and bad != "human_authored_plan":
        text = {"human_other_target": "Exfoliate onto chip_003 only.",
                "human_other_target_chinese": "只剥离到chip_003，不要换目标。",
                "human_source": "carrier_hbn_01 must itself be exfoliated."}[bad]
        s.store.add_instruction(text, project_id=s.project_id)
    elif bad == "human_authored_plan":
        s.store._x("UPDATE event SET agent='user' WHERE kind='plan.updated'")
    elif bad == "metadata_recipe_collision":
        params = s.store.get_run(rid)["params"]
        params["dwell_s"] = 20
        s.store.set_run_params(rid, params, note="Different executed recipe")
        s.store._x("UPDATE run SET meta_json=? WHERE id=?", ('{"dwell_s": 10}', rid))
    elif bad == "unknown_metadata_arg":
        old["operation_args"]["unrecognized_physical_target"] = "chip_001"
        desired["operation_args"]["unrecognized_physical_target"] = "chip_001"
        s.store.set_plan(s.project_id, [old], author="orchestrator", note="Unknown semantic argument")
        s.store._x("UPDATE run SET meta_json=? WHERE id=?", ('{"unrecognized_physical_target": "chip_001"}', rid))
    elif bad == "manual_done":
        desired["status"] = "done"
    before = deepcopy(s.store.get_project(s.project_id))
    samples, runs, event_id = s.store.list_samples(), s.store.list_runs(), s.store.last_event_id()
    result = (call(s, "orchestrator", "update_plan", {"plan": [desired],
        "expected_version": before["plan_version"], "note": "Attempt manual completion"})
        if bad == "manual_done" else refine(s, desired))
    assert result.status == "error", (bad, result)
    assert s.store.get_project(s.project_id) == before
    assert s.store.list_samples() == samples and s.store.list_runs() == runs
    assert not any(e.kind in ("plan.updated", "job.submitted", "device.command") for e in s.store.events_after(event_id))


@pytest.mark.parametrize("other", ["other_chip", "other_project", "other_flow"])
def test_corrected_step_cannot_complete_using_a_different_job(legacy_output, other):
    s, _, _, desired = legacy_output
    assert refine(s, desired).status == "ok"
    rid = s.store.create_run("search_chip" if other == "other_flow" else "exfoliate_chips",
        {"material": "hBN", "chip_ids": ["chip_002"]},
        project_id="another_project" if other == "other_project" else s.project_id)
    s.store.set_run_status(rid, "succeeded")
    s.gateway.delegation_started("recheck", "hands", "s1: Recheck")
    assert call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 1}).status == "ok"
    assert "cannot certify completion" in s.gateway.delegation_finished("recheck")
    assert s.store.get_project(s.project_id)["plan"][0]["status"] != "done"


def test_historical_done_contract_is_preserved_and_legacy_draft_does_not_block_other_work(legacy_output):
    s, _, old, _ = legacy_output
    report = {"id": "s2", "owner": "orchestrator", "title": "Report the unresolved condition"}
    result = call(s, "orchestrator", "update_plan", {"plan": [old, report], "expected_version": s.store.get_project(s.project_id)["plan_version"], "note": "Retain the legacy draft"})
    assert result.status == "ok", result
    assert not s.gateway.delegate("hands", task="s1: Exfoliate", require_sample_outcomes=True).allow
    s.store.set_step_status(s.project_id, "s1", "done")
    old["status"] = "done"
    assert call(s, "orchestrator", "update_plan", {"plan": [old, report], "expected_version": s.store.get_project(s.project_id)["plan_version"], "note": "Preserve historical progress"}).status == "ok"
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "done"


def test_unsubmitted_legacy_source_status_repair_preserves_identity_and_storage(services):
    s = services
    old = step()
    old["sample_outcomes"][0]["location"] = "left_slide_2"
    s.store.set_plan(s.project_id, [old], author="orchestrator", note="Legacy unsubmitted output typo")
    desired = deepcopy(old)
    desired["sample_outcomes"][0]["status"] = None
    before = s.store.last_event_id()
    result = refine(s, desired)
    assert result.status == "ok", result
    correction = result.data["outcome_corrections"][0]
    assert correction["correction_kind"] == "exfoliation_source_status"
    assert correction["execution_evidence"] is None
    saved = s.store.get_project(s.project_id)["plan"][0]
    assert saved["status"] == "pending"
    assert saved["sample_outcomes"] == desired["sample_outcomes"]
    assert not any(e.kind in ("job.submitted", "device.command") for e in s.store.events_after(before))


@pytest.mark.parametrize("bad", ["identity_retarget", "drop_storage", "drop_location", "human_source",
    "human_other_target", "human_plan", "uncertain", "submitted", "material", "recipe_change"])
def test_unsubmitted_source_repair_refuses_any_other_obligation_change(services, bad):
    s = services
    old = step()
    old["sample_outcomes"][0]["location"] = "left_slide_2"
    desired = deepcopy(old)
    desired["sample_outcomes"][0]["status"] = None
    if bad == "identity_retarget":
        desired["sample_outcomes"][0]["sample_id"] = "chip_001"
    elif bad == "drop_storage":
        old["sample_outcomes"][0]["in_storage"] = False
        desired["sample_outcomes"][0]["in_storage"] = False
    elif bad == "drop_location":
        desired["sample_outcomes"][0].pop("location")
    elif bad in ("human_source", "human_other_target"):
        s.store.add_instruction("carrier_hbn_01 must become exfoliated." if bad == "human_source"
                                else "Exfoliate onto chip_003 only.", project_id=s.project_id)
    elif bad == "uncertain":
        s.store.upsert_sample("carrier_hbn_01", "carrier", props={"location_uncertain": True})
    elif bad == "submitted":
        s.store.create_run("exfoliate_chips", {"material": "hBN"}, project_id=s.project_id)
    elif bad == "material":
        old["materials"] = ["Gr"]
    elif bad == "recipe_change":
        desired["operation_args"]["dwell_s"] = 20
    s.store.set_plan(s.project_id, [old], author="user" if bad == "human_plan" else "orchestrator", note="Legacy typo")
    before = deepcopy(s.store.get_project(s.project_id))
    result = refine(s, desired)
    assert result.status == "error", (bad, result)
    assert s.store.get_project(s.project_id) == before


def test_matching_human_receiving_chip_and_exact_run_args_allow_repair(legacy_output):
    s, rid, old, desired = legacy_output
    s.store.add_instruction("Exfoliate hBN onto chip_001.", project_id=s.project_id)
    for value in (old, desired):
        value["operation_args"].update({"from_carrier": "carrier_hbn_01", "chip_ids": ["chip_001"], "purpose": "secure"})
    s.store._x("UPDATE run SET meta_json=? WHERE id=?", ('{"purpose": "secure"}', rid))
    s.store.set_plan(s.project_id, [old], author="orchestrator", note="Original exact source and recipe")
    result = refine(s, desired)
    assert result.status == "ok", result
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"
