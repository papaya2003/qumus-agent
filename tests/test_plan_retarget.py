"""Natural chip changes preserve deferred physical work and give actionable errors."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from qumus2.agents.tools.hardware import ExfoliateArgs
from qumus2.records.plan_completion import completion_contract

TEXT = ("Change of plan: keep chip_001 untouched for another experiment. Use chip_002 for graphene instead. "
        "Update the task before moving anything, and still prepare just one chip.")
PLAN = [
    {"id": "s1", "title": "Inspect bench and chip_001 inventory", "owner": "eyes", "status": "pending"},
    {"id": "s2", "title": "Obtain graphene exfoliation recipe", "owner": "advisor", "status": "pending"},
    {"id": "s3", "title": "Exfoliate graphene onto chip_001", "owner": "hands", "status": "pending",
     "operation": "exfoliate_chips", "sample_outcomes": []},
    {"id": "s4", "title": "Search chip_001 and save microscope images", "owner": "hands", "status": "pending",
     "operation": "search_chip", "sample_outcomes": []},
    {"id": "s5", "title": "Stop for user review", "owner": "orchestrator", "status": "pending"}]


def call(s, tool, args=None, agent="orchestrator"):
    return asyncio.run(s.gateway.call(agent, tool, args or {}))[0]


def setup(s, text=TEXT):
    assert call(s, "update_plan", {"plan": PLAN, "note": "Initial future steps", "task_kind": "experiment"}).status == "ok"
    s.store.set_step_status(s.project_id, "s1", "running")
    iid = s.store.add_instruction(text, project_id=s.project_id, kind="plan_change")
    s.store.set_step_status(s.project_id, "s1", "done")
    args = {"plan": deepcopy(PLAN), "note": "Change only the target", "expected_version": 3,
            "physical_changes": [{"step_id": sid, "action": "supersede", "replacement_step_ids": [sid + "b"],
                                  "instruction_id": iid} for sid in ("s3", "s4")]}
    args["plan"][0]["status"] = "done"
    for step in args["plan"][2:4]:
        step["id"] += "b"
        step["title"] = step["title"].replace("chip_001", "chip_002")
    return args


def physical_snapshot(s):
    return s.store.list_samples(), s.store.list_runs()


def assert_retargeted(s, before):
    plan = {step["id"]: step for step in s.store.get_project(s.project_id)["plan"]}
    for sid in ("s3", "s4"):
        assert plan[sid]["status"] == "skipped" and plan[sid + "b"]["status"] == "pending"
        assert completion_contract(plan[sid]) == completion_contract(plan[sid + "b"])
        assert plan[sid]["physical_resolution"]["replacement_step_ids"] == [sid + "b"]
    assert physical_snapshot(s) == before
    assert call(s, "save_project_status", {"status": "done", "summary": "Cannot declare physical work done"}).status == "error"
    s.store.update_project_state(s.project_id, eyes_checked=True, experience_checked=["Gr"])
    denied = s.gateway.delegate("hands", task="s3b: Exfoliate graphene", require_sample_outcomes=True)
    assert denied.rule == "sample_outcomes_required"


def test_actual_stale_cancel_supersede_sequence_has_reusable_repairs(services):
    s = services
    args = setup(s)
    before = physical_snapshot(s)
    result = call(s, "update_plan", {**args, "expected_version": 1})
    assert result.data["error"] == "plan_version_conflict"
    assert result.data["next_action"] == "get_project_context"
    args["expected_version"] = call(s, "get_project_context").data["plan_version"]
    for change in args["physical_changes"]:
        change["action"] = "cancel"
    result = call(s, "update_plan", args)
    assert result.data["error"] == "replacement_action_required"
    assert "another user confirmation" in result.summary
    args["physical_changes"] = result.data["physical_changes"]
    assert call(s, "update_plan", args).status == "ok"
    assert_retargeted(s, before)


def test_omitted_companion_change_returns_a_valid_structure_using_same_instruction(services):
    s = services
    args = setup(s)
    before = physical_snapshot(s)
    iid = args["physical_changes"][0]["instruction_id"]
    args["physical_changes"].pop()
    result = call(s, "update_plan", args)
    assert result.data["error"] == "missing_physical_change" and result.data["step_id"] == "s4"
    assert {c["instruction_id"] for c in result.data["physical_changes"]} == {iid}
    assert s.store.get_project(s.project_id)["plan_version"] == 3
    args["physical_changes"] = result.data["physical_changes"]
    assert call(s, "update_plan", args).status == "ok"
    assert_retargeted(s, before)


@pytest.mark.parametrize("bad", ["operation", "owner", "title_meaning", "extra_target", "unknown_target",
                                 "wrong_kind", "new_done", "quoted", "negated", "foreign_instruction",
                                 "wrong_instruction", "added_foreign_outcome"])
def test_title_only_retarget_is_narrow_and_preserves_existing_guards(services, bad):
    s = services
    args = setup(s, 'Example: "' + TEXT + '"' if bad == "quoted" else
                 TEXT.replace("Use chip_002", "Do not use chip_002") if bad == "negated" else TEXT)
    target = args["plan"][2]
    if bad == "operation":
        target["operation"] = "place_chip_on_transfer_stage"
    elif bad == "owner":
        target["owner"] = "reactor"
    elif bad == "title_meaning":
        target["title"] = "Do not exfoliate graphene onto chip_002"
    elif bad == "extra_target":
        target["title"] += " and chip_003"
    elif bad == "unknown_target":
        s.store.remove_sample("chip_002", reason="missing-target fixture")
    elif bad == "wrong_kind":
        s.store.upsert_sample("chip_002", "stamp")
    elif bad == "new_done":
        target["status"] = "done"
    elif bad in ("foreign_instruction", "wrong_instruction"):
        iid = s.store.add_instruction(TEXT if bad == "foreign_instruction" else "Continue.",
                                      project_id="another_project" if bad == "foreign_instruction" else s.project_id)
        for change in args["physical_changes"]:
            change["instruction_id"] = iid
    elif bad == "added_foreign_outcome":
        target["sample_outcomes"] = [{"sample_id": "chip_003", "in_storage": True}]
    before = physical_snapshot(s), s.store.get_project(s.project_id)
    assert call(s, "update_plan", args).status == "error"
    assert (physical_snapshot(s), s.store.get_project(s.project_id)) == before


def test_retarget_may_refine_future_conditions_but_not_delete_existing_ones(services):
    s = services
    args = setup(s)
    for step, status in zip(args["plan"][2:4], ("exfoliated", "searched"), strict=True):
        step["sample_outcomes"] = [{"sample_id": "chip_002", "status": status, "in_storage": True}]
    assert call(s, "update_plan", args).status == "ok"
    current = s.store.get_project(s.project_id)
    changed = deepcopy(current["plan"])
    next(step for step in changed if step["id"] == "s3b")["sample_outcomes"] = []
    assert call(s, "update_plan", {"plan": changed, "note": "Cannot erase a declared goal",
                                  "expected_version": current["plan_version"]}).status == "error"
    assert s.store.get_project(s.project_id) == current


def test_historical_trial_can_be_completed_with_distinct_documented_simulation_defaults(services):
    s = services
    evidence = call(s, "get_experience", {"material": "Gr"}, agent="advisor").data["materials"]["Gr"]
    baseline = evidence["baseline_recipe"]
    assert not baseline["enforced"]
    original = deepcopy(baseline["recipe"])
    defaults = call(s, "get_lab_setup", agent="advisor").data["exfoliation_recipe_defaults"]
    completed = {**defaults["values"], **original}
    args = ExfoliateArgs.model_validate({"material": "Gr", "purpose": "secure", **completed})
    assert args.press_depth_mm == 0.2 and args.peel_angle_deg == 30
    assert all(completed[key] == value for key, value in original.items())
    assert not defaults["validated"] and defaults["source"] == "registered_tool_defaults"
    assert "fields missing" in defaults["usage"] and "real hardware" in defaults["usage"]
    assert baseline["recipe"] == original and "press_depth_mm" not in original
    again = call(s, "get_experience", {"material": "Gr"}, agent="advisor").data["materials"]["Gr"]
    assert again["baseline_recipe"] == baseline
