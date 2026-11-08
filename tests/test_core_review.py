"""Owner-confirmed core review: atomic plans and bounded, scoped tool contracts."""
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from qumus2.agents.tooling import default_registry
from qumus2.agents.tools.experience import EXPERIENCE_CHAR_BUDGET
from qumus2.records.budget import exploration_budget
from qumus2.runtime.store import PlanConflict, RunStore


def call(s, agent, tool, args=None):
    return asyncio.run(s.gateway.call(agent, tool, args or {}))[0]


@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("table", ["project", "plan_revision", "event"])
def test_plan_write_failure_rolls_back_every_representation(table, progress):
    s = RunStore()
    pid = s.create_project("atomic")
    s.set_plan(pid, [{"id": "s1", "status": "pending"}], author="orchestrator")
    before = s.get_project(pid), s.plan_history(pid), s.events_after()
    op = "INSERT" if table == "event" or table == "plan_revision" and not progress else "UPDATE"
    s._x(f"CREATE TRIGGER fail_write BEFORE {op} ON {table} BEGIN "
         "SELECT RAISE(ABORT, 'injected write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        if progress:
            s.set_step_status(pid, "s1", "done")
        else:
            s.set_plan(pid, [{"id": "s2"}], author="orchestrator")
    assert (s.get_project(pid), s.plan_history(pid), s.events_after()) == before
    s.close()


def test_plan_concurrent_writers_stale_edits_and_reopen(tmp_path):
    path = tmp_path / "q.db"
    a, b = RunStore(path), RunStore(path)
    pid = a.create_project("concurrent")
    a.set_plan(pid, [{"id": "s1", "status": "pending"}], author="orchestrator")
    version = a.get_project(pid)["plan_version"]
    barrier = Barrier(2)

    def write(store):
        barrier.wait()
        try:
            return store.set_plan(pid, [{"id": "s1", "status": "pending"}],
                                  author="orchestrator", expected_version=version)
        except PlanConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, [a, b]))
    assert sorted(map(str, results)) == ["2", "conflict"]
    stale = a.get_project(pid)["plan_version"]
    b.set_step_status(pid, "s1", "done")
    with pytest.raises(PlanConflict):
        a.set_plan(pid, [{"id": "s1"}], author="orchestrator", expected_version=stale)
    a.close()
    b.close()
    with_store = RunStore(path)
    proj = with_store.get_project(pid)
    assert proj["plan"] == with_store.plan_history(pid)[-1]["plan"]
    events = with_store.events_after()
    assert events[-1].payload["plan"] == proj["plan"]
    assert events[0].payload["plan"][0]["status"] == "pending"
    assert [e.payload["version"] for e in events] == [1, 2, 3]
    with pytest.raises(sqlite3.IntegrityError):
        with_store._x("INSERT INTO plan_revision(project_id, revision) VALUES (?, 2)", (pid,))
    with_store.close()


def test_tool_boundaries_and_inventory_material_provenance(services):
    s = services
    reg = default_registry()
    for agent, forbidden in [("eyes", "get_lab_setup"), ("hands", "get_experience"),
                             ("advisor", "query_run_records")]:
        assert forbidden not in {t.name for t in reg.for_agent(agent)}
        assert call(s, agent, forbidden).data["rule"] == "permission"
    inventory = call(s, "eyes", "list_samples", {"kind": "carrier"})
    assert inventory.data["samples"][0]["human_review"]["status"] == "unreviewed"
    setup = call(s, "advisor", "get_lab_setup")
    assert "carriers" not in setup.data and "chips_by_status" not in setup.data
    assert setup.data["readiness"] == "not evaluated by this query"
    s.store.emit("lab.frame", {"device": "microscope", "microscope": {"objective": 20}})
    status = call(s, "eyes", "get_instrument_status")
    assert not status.data["fresh_measurement"]
    assert status.data["observations"][-1]["frame"]["microscope"]["objective"] == 20


def test_existing_plan_cannot_be_replaced_without_read_version(services):
    s = services
    args = {"note": "start", "plan": [{"id": "s1", "title": "Inspect", "owner": "eyes"}]}
    assert call(s, "orchestrator", "update_plan", args).status == "ok"
    read = call(s, "orchestrator", "get_project_context")
    s.store.set_step_status(s.project_id, "s1", "done")
    assert call(s, "orchestrator", "update_plan", args).status == "error"
    assert call(s, "orchestrator", "update_plan", {**args, "expected_version": read.data["plan_version"]}).status == "error"
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "done"


def test_experience_limits_material_inputs_and_serialized_escaping(services, monkeypatch):
    from qumus2.records.experience import ExperienceBook

    assert call(services, "advisor", "get_experience", {"material": "x" * 100000}).status == "denied"
    assert call(services, "advisor", "get_experience", {"materials": ["x" * 100000]}).status == "denied"
    monkeypatch.setattr(ExperienceBook, "entries", lambda *a, **k: [])
    monkeypatch.setattr(ExperienceBook, "baseline", lambda *a: {"recipe": {
        str(i): "\x00" * 400 for i in range(100)}})
    result = call(services, "advisor", "get_experience", {"material": "\x00" * 64,
                     "materials": ["中" * 64, "😀" * 64, '"' * 64]})
    assert len(result.to_text()) <= EXPERIENCE_CHAR_BUDGET
    assert result.status == "needs_attention" and "omitted" in result.data


def test_experience_response_stays_bounded_and_keeps_counterevidence(services, monkeypatch):
    from qumus2.records.experience import ExperienceBook

    entries = [{"id": f"e{i}", "topic": "exfoliation", "status": "trial", "source": "review",
                "scope": {"material": "Gr"}, "recipe": {}, "claim": "Claim " * 1000,
                "label": "learned from real runs", "detail": {"observation": "long " * 5000},
                "support": [f"run{j}" for j in range(1000)],
                "against": [f"other{j}" for j in range(20)] if i == 49 else [],
                "updated_at": i} for i in range(50)]
    monkeypatch.setattr(ExperienceBook, "entries", lambda *a, **kw: entries)
    monkeypatch.setattr(ExperienceBook, "baseline", lambda *a, **kw: None)
    monkeypatch.setattr("qumus2.agents.tools.experience.recipe_stats", lambda *a: [
        {"runs": 5, "params": {"temperature": i}, "run_ids": ["r"] * 100} for i in range(100)])
    result = call(services, "advisor", "get_experience", {"material": "Gr", "materials": ["hBN", "MoS2", "WSe2"]})
    assert len(result.to_text()) <= EXPERIENCE_CHAR_BUDGET
    for item in result.data["materials"].values():
        assert item["omitted_entries"] > 0 and item["omitted_recipe_groups"] == 97
        conflict = item["experience"][0]
        assert conflict["id"] == "e49" and conflict["against_count"] == 20
        assert len(conflict["against"]) == 3 and conflict["support_count"] == 1000
    assert "Gr" in services.store.get_project(services.project_id)["state"]["experience_checked"]


def test_allowance_explains_unknown_device_demand_and_standalone_experiment(services):
    s = services
    b = exploration_budget(s.store, s.project_id, "Gr")
    assert b["spare_chips"] is None and b["task_chips_needed"] is None
    s.store.update_project_state(s.project_id, device_plan={"flake_roles": [
        {"role": "channel", "material": "Gr", "count": 1, "min_area_um2": 200,
         "thickness_labels": ["monolayer"]}]})
    b = exploration_budget(s.store, s.project_id, "Gr")
    assert (b["bare_chips"], b["task_chips_needed"], b["spare_chips"]) == (4, 3, 1)
    assert b["margin"] == 1.5 and "assumption" in b["good_flakes_per_chip"]["source"]
    s.store.update_project_state(s.project_id, device_plan=None, task_kind="experiment")
    assert exploration_budget(s.store, s.project_id, "Gr")["spare_chips"] == 4


def test_candidate_shortage_and_single_chip_query(services, pickup_inspection):
    s = services
    s.store.update_project_state(s.project_id, device_plan={"flake_roles": [{"role": "channel",
        "material": "Gr", "count": 1, "thickness_labels": ["monolayer", "bilayer"], "min_area_um2": 200}]})
    for i in range(5):
        s.store.upsert_sample(f"thick{i}", "flake", location="chip_001", status="catalogued",
            props={"material": "Gr", "thickness_label": "manylayer", "area_um2": 1000})
    pickup_inspection(s, [f"thick{i}" for i in range(5)])
    result = call(s, "picker", "get_flake_candidates")
    assert result.data["candidates_by_role"]["channel"] == []
    d = result.data["diagnostics_by_role"]["channel"]
    assert d["rejection_counts"] == {"thickness": 5}
    assert d["next_action"] == "exfoliate_and_search_one_chip"
    s.store.upsert_sample("older", "flake", location="chip_002", status="catalogued",
        props={"material": "Gr", "thickness_label": "monolayer", "area_um2": 1000})
    pickup_inspection(s, ["older"], "chip_002")
    scoped = call(s, "hands", "list_samples", {"kind": "flake", "location": "chip_001"})
    assert len(scoped.data["samples"]) == 5
    assert call(s, "picker", "get_flake_candidates").data["unphotographed_by_role"]["channel"][0]["id"] == "older"


def test_prepatterned_requirements_are_derived_and_unknown_geometry_is_not_accepted(services):
    s = services
    args = {"device_name": "straight bridge", "contact_scheme": "prepatterned Au contacts",
            "gate_scheme": "Si back gate",
            "layer_stack": [{"role": "channel", "material": "Gr", "thickness_labels": ["monolayer"], "min_area_um2": 1},
                            {"role": "contact_metal", "material": "Au", "flake": False}],
            "evidence": "synthetic centre-pair design", "target_chip_id": "chip_e01",
            "bridge_requirement": {"contact_overlap_um": 1, "channel_width_um": 2,
                                   "placement_margin_um": 0.5, "rationale": "test"}}
    geom = call(s, "advisor", "get_target_geometry", {"chip_id": "chip_e01"})
    assert geom.data["contact_gap_um"] == 28 and geom.data["source"] == "simulated_design"
    accepted = call(s, "advisor", "submit_device_plan", args)
    assert accepted.status == "ok", accepted.summary
    role = accepted.data["flake_roles"][0]
    assert role["required_footprint"]["width_um"] == 31
    assert role["required_footprint"]["height_um"] == 3
    assert role["min_area_um2"] == 93
    s.store.upsert_sample("chip_e01", "chip", props={"electrode_geometry": None})
    rejected = call(s, "advisor", "submit_device_plan", args)
    assert rejected.status == "error" and rejected.data["requirements_status"] == "unresolved"


def test_equal_area_does_not_replace_continuous_footprint(services, monkeypatch, pickup_inspection):
    from qumus2.agents.tooling import ToolContext
    from qumus2.agents.tools.picking import SubmitSelectionArgs, validate_selection

    s = services
    monkeypatch.setattr("qumus2.agents.tools.picking.require_images", lambda: False)
    s.store.upsert_sample("f", "flake", status="catalogued", props={"material": "Gr", "area_um2": 200})
    pickup_inspection(s, ["f"])
    s.store.update_project_state(s.project_id, viewed_flake_images=["f"], device_plan={"flake_roles": [
        {"role": "channel", "material": "Gr", "count": 1, "min_area_um2": 93,
         "required_footprint": {"width_um": 31, "height_um": 3}}]})
    a = {"flake_id": "f", "feasible": True, "geometry_fitness": 80, "visible_cleanliness": 80,
         "continuity_uniformity": 80, "evidence_confidence": 80, "inventory_tiebreaker": 50,
         "overall_score": 80, "reason": "test", "surface_residue": "clean",
         "residue_evidence": "Explicit clean-surface fixture; geometry is the tested constraint",
         "usable_width_um": 100, "usable_height_um": 2, "geometry_evidence": "test calibrated outline"}
    args = SubmitSelectionArgs(selected_by_role={"channel": ["f"]}, reasons="test", assessments=[a])
    ctx = ToolContext(s.store, s.jobs, "picker", s.project_id)
    assert any("does not fit" in e for e in validate_selection(ctx, args))
    args.assessments[0].usable_width_um, args.assessments[0].usable_height_um = 40, 5
    assert validate_selection(ctx, args) == []


def test_picker_missing_pickup_inspection_remains_a_required_gate(services, monkeypatch):
    from qumus2.agents.tooling import ToolContext
    from qumus2.agents.tools.picking import SubmitSelectionArgs, validate_selection

    monkeypatch.setattr("qumus2.agents.tools.picking.require_images", lambda: False)
    services.store.upsert_sample("uninspected", "flake", location="chip_001", status="catalogued",
        props={"material": "Gr", "area_um2": 1000})
    services.store.update_project_state(services.project_id, device_plan={"flake_roles": [
        {"role": "channel", "material": "Gr", "count": 1}]})
    result = call(services, "picker", "get_flake_candidates")
    assert result.data["candidates_by_role"]["channel"] == []
    diagnostic = result.data["diagnostics_by_role"]["channel"]
    assert diagnostic["rejection_counts"] == {"pickup_neighbourhood_needs_inspection": 1}
    assert diagnostic["next_action"] == "inspect_pickup_neighbourhood"
    errors = validate_selection(ToolContext(services.store, services.jobs, "picker", services.project_id),
        SubmitSelectionArgs(selected_by_role={"channel": ["uninspected"]}, reasons="Missing evidence control", assessments=[]))
    assert any("fresh inspection" in error for error in errors)
