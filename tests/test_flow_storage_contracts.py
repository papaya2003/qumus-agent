"""Admission and actual local-flow evidence for dynamically selected chip storage."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from conftest import put_sample

from qumus2.flows.library.exfoliation import STORE_BOARD
from qumus2.flows.library.transfer import MoveObject
from qumus2.records.plan_completion import completion_contract, flow_storage_contracts


def call(s, agent, tool, args=None):
    return asyncio.run(s.gateway.call(agent, tool, args or {}))[0]


def step(operation="search_chip", location=None, **overrides):
    return {"id": "s1", "owner": "hands", "title": "Process chip_002 and return to storage",
            "operation": operation, "status": "pending", "sample_outcomes": [{
                "sample_id": "chip_002", "status": "exfoliated" if operation == "exfoliate_chips" else "searched",
                "location": location, "in_storage": True}], **overrides}


def edit(s, plan):
    context = call(s, "orchestrator", "get_project_context")
    return call(s, "orchestrator", "update_plan", {"plan": plan, "note": "Storage contract test",
        "expected_version": context.data["plan_version"], "task_kind": "experiment"})


def state(s):
    return (s.store.get_project(s.project_id), s.store.plan_history(s.project_id),
            s.store.list_samples(), s.store.list_runs(), s.store.reservations())


def assert_no_execution(s, after):
    assert not any(e.kind in ("plan.updated", "plan.step", "job.submitted", "device.command",
                             "sample.updated", "agent.delegation_started")
                   for e in s.store.events_after(after, limit=10000))


@pytest.mark.parametrize("operation", ["exfoliate_chips", "search_chip", "revisit_chip"])
@pytest.mark.parametrize("location", ["left_chip_2", "mid_chip_1", "right_stamp_1", "invented_slot"])
def test_exact_chip_destinations_are_refused_before_saving(services, operation, location):
    s = services
    before, last = state(s), s.store.last_event_id()
    result = edit(s, [step(operation, location)])
    assert result.status == "error" and result.data["error"] == "flow_location_contract"
    assert result.data["location_contract_errors"][0]["declared_location"] == location
    assert "explicitly requires a fixed slot" in result.summary
    assert "repeated authorization" in result.summary
    assert state(s) == before
    assert_no_execution(s, last)
    # Only the unsaved model guess is corrected; there is no saved obligation to weaken.
    assert edit(s, [step(operation)]).status == "ok"


@pytest.mark.parametrize("owner", ["hands", "reactor"])
def test_unbound_physical_draft_cannot_lock_in_an_unchecked_destination(services, owner):
    s = services
    before = state(s)
    result = edit(s, [step(None, "left_chip_2", owner=owner)])
    assert result.status == "error" and "Choose a registered operation" in result.summary
    assert state(s) == before
    assert edit(s, [step(None, owner=owner, sample_outcomes=[])]).status == "ok"
    if owner == "hands":
        assert edit(s, [step("exfoliate_chips")]).status == "ok"


@pytest.mark.parametrize("operation", ["exfoliate_chips", "search_chip", "revisit_chip"])
def test_legacy_fixed_requirement_is_preserved_but_not_delegated(services, operation):
    s = services
    # Deliberate legacy fixture: these rows predate the new admission check.
    saved = step(operation, "left_chip_2")
    s.store.set_plan(s.project_id, [saved], author="legacy_fixture", note="Explicit fixed-slot requirement")
    s.store.update_project_state(s.project_id, eyes_checked=True, experience_checked=["Gr"])
    before, last = state(s), s.store.last_event_id()
    decision = s.gateway.delegate("hands", task="s1: Process chip_002", require_sample_outcomes=True)
    assert not decision.allow and decision.rule == "flow_location_contract"
    assert state(s) == before
    assert_no_execution(s, last)
    assert edit(s, [step(operation)]).status == "error"  # A04 still prevents deletion.
    assert completion_contract(s.store.get_project(s.project_id)["plan"][0]) == completion_contract(saved)
    assert edit(s, [saved, {"id": "review", "owner": "human", "title": "Review the fixed-slot requirement"}]).status == "ok"
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "pending"


def test_unchanged_historical_completion_is_not_reinterpreted(services):
    s = services
    old = step("exfoliate_chips", "left_chip_2", status="done")
    s.store.set_plan(s.project_id, [old], author="historical_fixture", note="Pre-existing completion")
    assert edit(s, [old]).status == "ok"
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "done"
    assert not s.store.list_runs()


def test_precise_placement_and_flake_carrier_conditions_are_not_broadly_rejected(services):
    s = services
    placement = step("place_chip_on_transfer_stage", "transfer_stage")
    placement["sample_outcomes"][0].update(status=None, in_storage=False)
    assert edit(s, [placement]).status == "ok"
    s.store.update_project_state(s.project_id, eyes_checked=True)
    assert s.gateway.delegate("hands", task="s1: Place chip_002", require_sample_outcomes=True).allow
    s.store.upsert_sample("known_flake", "flake", location="chip_002", status="candidate")
    flake = step("search_chip", id="s2", sample_outcomes=[{
        "sample_id": "known_flake", "location": "chip_002", "status": "catalogued", "in_storage": True}])
    assert edit(s, [placement, flake]).status == "ok"


def test_capability_query_exposes_registered_destination_semantics_without_allocating(services):
    s = services
    before = state(s)
    result = call(s, "advisor", "get_lab_setup")
    contracts = result.data["flow_storage_contracts"]
    assert contracts == flow_storage_contracts()
    assert contracts["exfoliate_chips"]["storage_station"] == STORE_BOARD == "middle_storage"
    assert contracts["exfoliate_chips"]["resolved_parameter"] == "store_slots"
    for operation in ("search_chip", "revisit_chip"):
        assert contracts[operation]["mode"] == "home_at_submission"
        assert contracts[operation]["resolved_parameter"] == "home_slot"
        assert not contracts[operation]["exact_location_supported"]
    assert state(s) == before


@pytest.mark.parametrize("obstruct_and_recover", [False, True])
def test_actual_exfoliation_search_and_recovery_close_generic_storage_goals(services, monkeypatch, obstruct_and_recover):
    s = services
    planned = [step("exfoliate_chips"), step("search_chip", id="s2")]
    from qumus2.agents.readiness import RECIPE_FIELDS
    from qumus2.agents.tools.hardware import ExfoliateArgs

    recipe = {key: ExfoliateArgs.model_fields[key].default for key in RECIPE_FIELDS}
    planned[0]["operation_args"] = {"material": "Gr", **recipe}
    assert edit(s, planned).status == "ok"
    original = deepcopy(s.store.get_project(s.project_id)["plan"])
    assert call(s, "eyes", "inspect_bench").status in ("ok", "corrected")
    assert call(s, "advisor", "get_experience", {"material": "Gr"}).status == "ok"
    injected = set()
    preconditions = MoveObject.preconditions

    def obstruct_return(move, ctx):
        # Explicit simulated external obstruction; normal workflow still uses
        # the real camera, MoveObject fallback, reservations and SafetyGate.
        if obstruct_and_recover and move.name not in injected:
            if move.name == "store_exfoliated_chip":
                injected.add(move.name)
                put_sample(s, "chip_003", move.dest)
            elif move.name == "store_chip_from_transfer_stage":
                injected.add(move.name)
                put_sample(s, "chip_004", move.dest)
                s.lab.faults.inject("device_offline", {"device": "overview_camera", "offline": True})
        return preconditions(move, ctx)

    monkeypatch.setattr(MoveObject, "preconditions", obstruct_return)
    runs = []
    for sid, operation, args in [
        ("s1", "exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_002"], **recipe}),
        ("s2", "search_chip", {"chip_id": "chip_002", "min_area_um2": 50, "max_revisits": 2}),
    ]:
        assert s.gateway.delegate("hands", task=f"{sid}: Process chip_002", require_sample_outcomes=True).allow
        s.gateway.delegation_started(sid, "hands", f"{sid}: Process chip_002")
        submitted = call(s, "hands", operation, args)
        assert submitted.status == "pending", submitted.summary
        rid = submitted.data["job_id"]
        runs.append(rid)
        result = call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 60})
        if sid == "s2" and obstruct_and_recover:
            assert result.status == "needs_attention", result.summary
            s.gateway.delegation_finished(sid)
            current = {x["id"]: x["status"] for x in s.store.get_project(s.project_id)["plan"]}
            assert current["s1"] == "done" and current["s2"] != "done"
            s.lab.faults.inject("device_offline", {"device": "overview_camera", "offline": False})
            assert call(s, "eyes", "inspect_bench").status in ("ok", "corrected")
            assert s.gateway.delegate("hands", task="s2: Recover storage", require_sample_outcomes=True).allow
            s.gateway.delegation_started("recovery", "hands", "s2: Recover storage")
            retried = call(s, "hands", "retry_job", {"job_id": rid, "reason": "Simulated camera restored; retry return"})
            assert retried.status == "pending" and retried.data["job_id"] == rid
            result = call(s, "hands", "wait_job", {"job_id": rid, "timeout_s": 60})
            s.gateway.delegation_finished("recovery")
        else:
            s.gateway.delegation_finished(sid)
        assert result.status in ("ok", "corrected"), result.summary
        assert s.store.get_run(rid)["status"] == "succeeded"
        current = {x["id"]: x["status"] for x in s.store.get_project(s.project_id)["plan"]}
        assert current[sid] == "done"
        if sid == "s1":
            assert current["s2"] == "pending"
    chip = s.store.get_sample("chip_002")
    assert (chip["status"], chip["location"]) == ("searched", "mid_chip_3" if obstruct_and_recover else "mid_chip_1")
    assert s.store.get_run(runs[0])["params"]["home_slots"] == ["left_chip_2"]
    assert s.store.get_run(runs[0])["params"]["store_slots"] == ["mid_chip_2" if obstruct_and_recover else "mid_chip_1"]
    assert s.store.get_run(runs[1])["params"]["home_slot"] == chip["location"]
    current_plan = s.store.get_project(s.project_id)["plan"]
    assert all(x["status"] == "done" for x in current_plan)
    assert [completion_contract(x) for x in current_plan] == [completion_contract(x) for x in original]
    assert len(s.store.list_runs()) == 3 + int(obstruct_and_recover)  # Recovery adds an inspection, not a new search.
    assert {r["id"] for r in s.store.list_runs() if r["flow"] in ("exfoliate_chips", "search_chip")} == set(runs)
    assert not s.store.reservations()
    events = s.store.events_after(0, limit=100000)
    assert any(e.kind == "step.verified" and e.payload.get("name") == "scan_chip" for e in events)
    assert not any(e.kind == "safety.violation" for e in events)
    if obstruct_and_recover:
        assert len([e for e in events if e.kind == "notification" and "goes to" in e.payload.get("text", "")]) == 2
        assert s.store.get_project(s.project_id)["state"]["recovery"]["total"] == 1
