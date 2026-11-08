"""Replay real plan inputs without running jobs; synthetic variants test guards."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from qumus2.agents.tooling import default_registry
from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.records.reactor_requirements import plan_coverage_errors, requirements_record

RECORDED = json.loads((Path(__file__).parent / "fixtures/plan_insertion_live_v2.json").read_text())


def call(s, tool, args):
    return asyncio.run(s.gateway.call("orchestrator", tool, args))[0]


@pytest.fixture
def lab():
    s = build_services(":memory:", testbench_config=simulation_profile(), use_openai=False)
    # The recorded initial plan predates established requirements. Its successful
    # Eyes/Advisor progress is fixture evidence, not freshly executed work.
    result = call(s, "update_plan", {"plan": deepcopy(RECORDED["initial_plan"]),
        "task_kind": "experiment", "note": "Recorded v2 plan fixture", "expected_version": 0})
    assert result.status == "ok", result
    iid = s.store.add_instruction("Use Gr and Pd with top hBN; preserve images and source provenance.", project_id=s.project_id)
    assert call(s, "establish_reactor_requirements", {"instruction_id": iid, "reactive_material": "Gr", "metal": "Pd"}).status == "ok"
    s.store.update_project_state(s.project_id, eyes_checked=True, experience_checked=["hBN", "Gr"])
    yield s
    asyncio.run(s.runtime.close())


def anchored(args):
    args = deepcopy(args)
    for i, addition in enumerate(args["additions"]):
        addition["before_step_id"] = "s5" if i < 3 else "s6"
    return args


def snapshot(s):
    return deepcopy(s.store.get_project(s.project_id)), s.store.plan_history(s.project_id)


@pytest.mark.parametrize("attempt", RECORDED["attempts"], ids=lambda a: f"live-event-{a['call_event_id']}")
def test_recorded_additions_insert_atomically_without_changing_any_recorded_task_fields(lab, attempt):
    s = lab
    before, history = snapshot(s)
    requirements = requirements_record(s.store, s.project_id)
    refused = call(s, "refine_plan", attempt["args"])
    assert refused.status == "error"
    # Historical refusal text is immutable; the current policy also orders
    # preparation before screening and makes reacquisition conditional.
    assert any("Use preparation/search" in reason for reason in refused.data["requirement_errors"])
    assert refused.data["insertion_guidance"]["existing_ids"] == [f"s{i}" for i in range(1, 8)]
    assert "before_step_id/after_step_id" in refused.summary
    assert snapshot(s) == (before, history)
    # Progress-only changes after the orchestrator's read remain merged.
    s.store.set_step_status(s.project_id, "s3", "done")
    start = s.store.last_event_id()
    result = call(s, "refine_plan", anchored(attempt["args"]))
    assert result.status == "ok", result
    assert result.data["revision"] == 2
    plan = result.data["plan"]
    assert [step["id"] for step in plan] == ["s1", "s2", "s3", "s4", "s8", "s9", "s10", "s5", "s11", "s12", "s6", "s7"]
    assert [step["status"] for step in plan[:3]] == ["done"] * 3
    assert [step["id"] for step in plan if step["id"] in {s["id"] for s in before["plan"]}] == [s["id"] for s in before["plan"]]
    added = [step for step in plan if step["id"] not in {s["id"] for s in before["plan"]}]
    assert [step["title"] for step in added] == [step["title"] for step in attempt["args"]["additions"]]
    for saved, requested in zip(added, attempt["args"]["additions"], strict=True):
        for key, value in requested.items():
            assert saved[key] == value
    assert [step["id"] for step in result.data["added_steps"]] == ["s8", "s9", "s10", "s11", "s12"]
    assert all("before_step_id" not in step and "after_step_id" not in step for step in plan)
    assert not plan_coverage_errors(s.store, s.project_id, plan)
    assert requirements_record(s.store, s.project_id) == requirements
    assert not result.data["all_steps_ready"], "Future search chips still require real output bindings before delegation"
    assert result.data["executable_scope"] == "next_pending_step"
    assert result.data["next_pending_step"]["step_id"] == "s4"
    assert not s.store.list_runs()
    events = s.store.events_after(start)
    assert len([e for e in events if e.kind == "plan.updated"]) == 1
    assert not any(e.kind in ("job.submitted", "device.command") for e in events)
    stale = call(s, "refine_plan", anchored(attempt["args"]))
    assert stale.status == "error" and stale.data["error"] == "plan_revision_conflict"
    assert s.store.get_project(s.project_id)["plan"] == plan


@pytest.mark.parametrize("bad", ["wrong_sequence", "layer_order", "missing_gr_search", "weaken_existing_outcome"])
def test_atomic_insertion_keeps_existing_experiment_and_physical_guards(lab, bad):
    s = lab
    args = anchored(RECORDED["attempts"][-1]["args"])
    if bad == "wrong_sequence":
        args["additions"][-2]["before_step_id"] = "s7"
    elif bad == "layer_order":
        next(u for u in args["updates"] if u["step_id"] == "s6")["fields"]["materials"] = ["Gr", "hBN"]
    elif bad == "missing_gr_search":
        args["additions"].pop(2)
    else:
        initial = deepcopy(s.store.get_project(s.project_id)["plan"])
        initial[3]["sample_outcomes"] = [{"sample_id": "chip_003", "in_storage": True}]
        s.store.set_plan(s.project_id, initial, author="synthetic_fixture", note="Existing physical obligation")
        args["expected_revision"] = s.store.get_project(s.project_id)["plan_revision"]
    before = snapshot(s)
    result = call(s, "refine_plan", args)
    assert result.status == "error"
    assert result.data["error"] == ("physical_obligation_changed" if bad == "weaken_existing_outcome" else "experiment_requirements")
    assert snapshot(s) == before


@pytest.mark.parametrize("placement", [
    {"before_step_id": "not-a-step"}, {"after_step_id": "s8"},
    {"before_step_id": ""}, {"before_step_id": "s5", "after_step_id": "s4"},
])
def test_invalid_placement_references_are_actionable_and_do_not_save(lab, placement):
    s = lab
    args = anchored(RECORDED["attempts"][-1]["args"])
    args["additions"][0].pop("before_step_id")
    args["additions"][0].update(placement)
    before = snapshot(s)
    result = call(s, "refine_plan", args)
    assert result.status == "error" and result.data["error"] == "invalid_refinement_ids"
    assert result.data["placement_errors"][0]["index"] == 0
    assert result.data["existing_ids"] == [f"s{i}" for i in range(1, 8)]
    assert snapshot(s) == before


def test_after_anchors_preserve_group_order_in_the_same_save(lab):
    args = anchored(RECORDED["attempts"][-1]["args"])
    for i, addition in enumerate(args["additions"]):
        addition.pop("before_step_id")
        addition["after_step_id"] = "s4" if i < 3 else "s5"
    result = call(lab, "refine_plan", args)
    assert result.status == "ok", result
    assert [s["id"] for s in result.data["plan"]] == ["s1", "s2", "s3", "s4", "s8", "s9", "s10", "s5", "s11", "s12", "s6", "s7"]


def test_existing_order_and_placements_are_composed_without_recycling_ids(services):
    s = services
    old = [{"id": f"s{i}", "owner": "orchestrator", "title": f"Report {i}"} for i in range(1, 4)]
    assert call(s, "update_plan", {"plan": old, "note": "Original"}).status == "ok"
    assert call(s, "update_plan", {"plan": old[:2], "expected_version": 1, "note": "Retire s3"}).status == "ok"
    s.store.set_step_status(s.project_id, "s1", "done")
    result = call(s, "refine_plan", {"expected_revision": 2, "order": ["s2", "s1"], "note": "Atomic placement",
        "additions": [{"owner": "orchestrator", "title": "First", "after_step_id": "s2"},
                      {"owner": "orchestrator", "title": "Second", "after_step_id": "s2"},
                      {"owner": "orchestrator", "title": "Third", "before_step_id": "s1"},
                      {"owner": "orchestrator", "title": "Last"}]})
    assert result.status == "ok", result
    assert [step["id"] for step in result.data["plan"]] == ["s2", "s4", "s5", "s6", "s1", "s7"]
    assert next(step for step in result.data["plan"] if step["id"] == "s1")["status"] == "done"


def test_insertion_schema_exposes_anchors_without_runtime_changes():
    schema = default_registry().get("refine_plan").json_schema()
    addition = schema["$defs"]["PlanAddition"]
    assert {"before_step_id", "after_step_id"} <= addition["properties"].keys()
    assert schema["properties"]["additions"]["items"]["$ref"].endswith("/PlanAddition")
    assert "atomically" in schema["properties"]["order"]["description"]
