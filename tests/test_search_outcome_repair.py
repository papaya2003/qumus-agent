"""Plan identity repairs: real hBN/Gr flows plus explicitly synthetic rejection fixtures."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from qumus2.agents.readiness import RECIPE_FIELDS
from qumus2.agents.tools.hardware import ExfoliateArgs


def call(s, role, tool, args=None):
    return asyncio.run(s.gateway.call(role, tool, args or {}))[0]


def search_step(material="hBN", sample="carrier_hbn_01"):
    return {"id": "s2", "owner": "hands", "title": f"Search the {material} receiving chip",
        "operation": "search_chip", "materials": [material], "status": "pending",
        "sample_outcomes": [{"sample_id": sample, "status": "searched", "in_storage": True}]}


def refine(s, desired):
    return call(s, "orchestrator", "refine_plan", {
        "expected_revision": s.store.get_project(s.project_id)["plan_revision"],
        "updates": [{"step_id": desired["id"], "fields": {"sample_outcomes": desired["sample_outcomes"]}}],
        "note": "Bind the actual receiving chip using successful exfoliation evidence"})


def execute(s, sid, tool, args):
    decision = s.gateway.delegate("hands", task=f"{sid}: Execute the declared operation", require_sample_outcomes=True)
    assert decision.allow, decision.reason
    s.gateway.delegation_started(sid, "hands", f"{sid}: Execute the declared operation")
    result = call(s, "hands", tool, args)
    assert result.status == "pending", result
    rid = result.data["job_id"]
    waited = call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 60})
    assert waited.status in ("ok", "corrected"), waited
    facts = s.gateway.delegation_finished(sid)
    assert f"Plan step {sid} is now done" in facts
    return rid


@pytest.mark.parametrize("material,carrier", [("hBN", "carrier_hbn_01"), ("Gr", "carrier_gr_01")])
def test_actual_chain_corrects_identity_and_rechecks_original_search_without_motion(services, material, carrier):
    s = services
    assert call(s, "eyes", "inspect_bench").status in ("ok", "corrected")
    assert call(s, "advisor", "get_experience", {"material": material}).status == "ok"
    recipe = {key: ExfoliateArgs.model_fields[key].default for key in RECIPE_FIELDS}
    recipe["peel_speed"] = 200
    recipe = {"material": material, **recipe}
    prep = {"id": "s1", "owner": "hands", "title": f"Exfoliate {material}", "operation": "exfoliate_chips",
        "materials": [material], "operation_args": recipe,
        "sample_outcomes": [{"sample_id": "chip_001", "status": "exfoliated", "in_storage": True}]}
    future = search_step(material, "chip_001")
    assert call(s, "orchestrator", "update_plan", {"plan": [prep, future], "task_kind": "experiment", "note": "Actual flow evidence"}).status == "ok"
    prep_job = execute(s, "s1", "exfoliate_chips", {**recipe, "chip_ids": ["chip_001"]})
    assert s.store.get_sample("chip_001")["props"]["from_carrier"] == carrier
    completed_prep = s.store.get_project(s.project_id)["plan"][0]
    legacy = search_step(material, carrier)
    # This models the previously admitted bad plan, not a new accepted request.
    s.store.set_plan(s.project_id, [completed_prep, legacy], author="legacy_fixture", note="Old source/receiving-chip confusion")
    refused = s.gateway.delegate("hands", task="s2: Search", require_sample_outcomes=True)
    assert not refused.allow and refused.rule == "search_outcome_contract"
    before = s.store.last_event_id()
    result = refine(s, future)
    assert result.status == "ok", result
    correction = result.data["outcome_corrections"][0]
    assert correction["original_condition"]["sample_id"] == carrier
    assert correction["corrected_condition"]["sample_id"] == "chip_001"
    assert correction["execution_evidence"]["job_id"] == prep_job
    assert s.store.get_project(s.project_id)["plan"][1]["status"] == "pending"
    assert not any(e.kind in ("job.submitted", "device.command") for e in s.store.events_after(before))
    search_job = execute(s, "s2", "search_chip", {"chip_id": "chip_001", "min_area_um2": 50, "max_revisits": 2})
    # A restored legacy completion condition can be repaired using the existing
    # search job. No second scan or model-written done status is needed.
    s.store.set_plan(s.project_id, [completed_prep, legacy], author="legacy_fixture", note="Old saved condition after successful search")
    fixed = refine(s, future)
    assert fixed.status == "ok", fixed
    assert fixed.data["outcome_corrections"][0]["execution_evidence"]["search_job_ids"] == [search_job]
    assert s.store.get_project(s.project_id)["plan"][1]["status"] == "pending"
    before = s.store.last_event_id()
    assert s.gateway.delegate("hands", task="s2: Recheck the original job", require_sample_outcomes=True).allow
    s.gateway.delegation_started("recheck", "hands", "s2: Recheck the original job")
    assert call(s, "hands", "wait_job", {"job_id": search_job, "timeout_s": 1}).status == "ok"
    facts = s.gateway.delegation_finished("recheck")
    current = s.store.get_project(s.project_id)
    assert current["plan"][1]["status"] == "done"
    assert f"plan_revision={current['plan_revision']}, plan_version={current['plan_version']}" in facts
    events = s.store.events_after(before, limit=10000)
    assert not any(e.kind in ("job.submitted", "device.command") for e in events)
    checked = [e for e in events if e.kind == "agent.delegation_checked"][-1]
    assert checked.payload["plan_version"] == current["plan_version"]
    assert len(s.store.list_runs()) == 3  # Eyes, exfoliation, search.
    final_args = {"status": "done", "summary": "Original search evidence rechecked"}
    cooled = call(s, "orchestrator", "save_project_status", final_args)
    assert cooled.status == "pending", cooled
    assert call(s, "orchestrator", "wait_job", {"job_id": cooled.data["job_id"], "timeout_s": 30}).status == "ok"
    assert call(s, "orchestrator", "save_project_status", final_args).status == "ok"


@pytest.fixture
def identity_fixture(services):
    """Synthetic records only, for exhaustive rejection paths."""
    s = services
    old = search_step()
    s.store.set_plan(s.project_id, [old], author="legacy_fixture", note="Invalid historical binding")
    s.store.update_project_state(s.project_id, task_kind="experiment", eyes_checked=True, experience_checked=["hBN"])
    s.store.upsert_sample("chip_001", "chip", location="mid_chip_1", status="searched",
                          props={"material": "hBN", "from_carrier": "carrier_hbn_01"})
    rid = s.store.create_run("exfoliate_chips", {"carrier_id": "carrier_hbn_01", "chip_ids": ["chip_001"], "material": "hBN"}, project_id=s.project_id)
    s.store.set_run_status(rid, "succeeded")
    return s, rid, old, search_step(sample="chip_001")


@pytest.mark.parametrize("bad", ["no_job", "failed_job", "running_job", "other_project", "multi_output", "second_output",
    "wrong_material", "missing_origin", "wrong_origin", "uncertain", "reserved", "wrong_target", "remove_condition",
    "fixed_source", "fixed_source_chinese", "fixed_other_target", "fixed_location", "explicit_operation_target"])
def test_identity_repair_refuses_missing_ambiguous_or_human_fixed_evidence(identity_fixture, bad):
    s, rid, old, desired = identity_fixture
    if bad == "no_job":
        s.store._x("UPDATE run SET flow='other_flow' WHERE id=?", (rid,))
    elif bad in ("failed_job", "running_job"):
        s.store.set_run_status(rid, "failed" if bad == "failed_job" else "running")
    elif bad == "other_project":
        s.store._x("UPDATE run SET project_id='another_project' WHERE id=?", (rid,))
    elif bad == "multi_output":
        s.store.set_run_params(rid, {"carrier_id": "carrier_hbn_01", "chip_ids": ["chip_001", "chip_002"], "material": "hBN"}, note="Synthetic ambiguity")
    elif bad == "second_output":
        second = s.store.create_run("exfoliate_chips", {"carrier_id": "carrier_hbn_01", "chip_ids": ["chip_002"], "material": "hBN"}, project_id=s.project_id)
        s.store.set_run_status(second, "succeeded")
    elif bad in ("wrong_material", "missing_origin", "wrong_origin", "uncertain"):
        props = {"wrong_material": {"material": "Gr"}, "missing_origin": {"from_carrier": None},
                 "wrong_origin": {"from_carrier": "carrier_gr_01"}, "uncertain": {"location_uncertain": True}}[bad]
        s.store.upsert_sample("chip_001", "chip", props=props)
    elif bad == "reserved":
        s.store.reserve("sample", "chip_001", rid, "Synthetic concurrent work")
    elif bad == "wrong_target":
        desired["sample_outcomes"][0]["sample_id"] = "chip_002"
    elif bad == "remove_condition":
        desired["sample_outcomes"][0].pop("in_storage")
    elif bad.startswith("fixed_") and bad != "fixed_location":
        text = {"fixed_source": "Search only carrier_hbn_01; do not substitute another target.",
                "fixed_source_chinese": "只扫描carrier_hbn_01，不要换目标。",
                "fixed_other_target": "Search only chip_003."}[bad]
        s.store.add_instruction(text, project_id=s.project_id)
    elif bad == "fixed_location":
        old["sample_outcomes"][0]["location"] = "left_slide_2"
        s.store.set_plan(s.project_id, [old], author="legacy_fixture", note="Explicit fixed destination")
    elif bad == "explicit_operation_target":
        old["operation_args"] = {"chip_id": "carrier_hbn_01"}
        s.store.set_plan(s.project_id, [old], author="legacy_fixture", note="Explicit target")
    before = deepcopy(s.store.get_project(s.project_id))
    result = refine(s, desired)
    assert result.status == "error", (bad, result)
    assert s.store.get_project(s.project_id) == before
    assert not s.store.get_project(s.project_id)["plan"][0].get("outcome_corrections")


def test_search_role_validation_and_execution_target_are_checked_before_motion(services):
    s = services
    bad = search_step()
    result = call(s, "orchestrator", "update_plan", {"plan": [bad], "note": "Invalid search role"})
    assert result.status == "error" and result.data["outcome_contract_errors"][0]["role"] == "carrier"
    assert not s.store.get_project(s.project_id)["plan"]
    good = search_step(sample="chip_001")
    assert call(s, "orchestrator", "update_plan", {"plan": [good], "note": "Actual chip"}).status == "ok"
    s.store.update_project_state(s.project_id, eyes_checked=True, experience_checked=["hBN"])
    s.gateway.delegation_started("search", "hands", "s2: Search actual chip")
    before = s.store.last_event_id()
    result = call(s, "hands", "search_chip", {"chip_id": "chip_002"})
    assert result.status == "denied" and result.data["rule"] == "search_target_mismatch"
    assert not any(e.kind in ("job.submitted", "device.command") for e in s.store.events_after(before))


@pytest.mark.parametrize("bad", ["other_chip", "other_project"])
def test_recheck_cannot_use_a_different_search_job_to_certify_the_bound_target(identity_fixture, bad):
    s, _, _, desired = identity_fixture
    assert refine(s, desired).status == "ok"
    rid = s.store.create_run("search_chip", {"chip_id": "chip_002" if bad == "other_chip" else "chip_001"},
        project_id="other_project" if bad == "other_project" else s.project_id)
    s.store.set_run_status(rid, "succeeded")
    s.gateway.delegation_started("recheck", "hands", "s2: Recheck")
    assert call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 1}).status == "ok"
    facts = s.gateway.delegation_finished("recheck")
    assert "cannot certify completion" in facts
    assert s.store.get_project(s.project_id)["plan"][0]["status"] != "done"


def test_refinement_reports_all_id_errors_then_adds_and_reorders_without_losing_progress(services):
    s = services
    initial = [{"id": "s1", "owner": "orchestrator", "title": "Existing one"},
               {"id": "s2", "owner": "orchestrator", "title": "Existing two"}]
    assert call(s, "orchestrator", "update_plan", {"plan": initial, "note": "Start"}).status == "ok"
    s.store.set_step_status(s.project_id, "s1", "done")
    before = s.store.get_project(s.project_id)
    result = call(s, "orchestrator", "refine_plan", {"expected_revision": 1,
        "order": ["s1", "s1", "7"], "additions": [{"id": "s7", "title": "New", "owner": "orchestrator"}], "note": "Invalid IDs"})
    assert result.status == "error"
    assert result.data["existing_ids"] == ["s1", "s2"]
    assert result.data["unknown_ids"] == ["7"] and result.data["missing_ids"] == ["s2"]
    assert result.data["duplicate_ids"] == ["s1"] and result.data["addition_id_errors"] == [{"index": 0, "id": "s7"}]
    assert s.store.get_project(s.project_id) == before
    added = call(s, "orchestrator", "refine_plan", {"expected_revision": 1,
        "additions": [{"title": "New", "owner": "orchestrator"}], "note": "Allocate IDs first"})
    assert added.status == "ok"
    new_id = added.data["plan"][-1]["id"]
    reordered = call(s, "orchestrator", "refine_plan", {"expected_revision": added.data["revision"],
        "order": ["s1", new_id, "s2"], "note": "Insert using returned identity"})
    assert reordered.status == "ok" and reordered.data["plan"][0]["status"] == "done"
