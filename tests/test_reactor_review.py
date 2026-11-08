"""Owner review regressions. Synthetic ledger fixtures are not physical evidence."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from qumus2.agents.tooling import default_registry
from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.records.reactor_requirements import (
    ReactorRequirements,
    capture_reactor_objective,
    input_errors,
    requirements_record,
)

CHIP = "reactor_chip_01"


def call(s, tool, args=None, role="orchestrator"):
    return asyncio.run(s.gateway.call(role, tool, args or {}))[0]


@pytest.fixture
def lab(tmp_path):
    s = build_services(tmp_path / "review.db", testbench_config=simulation_profile(), use_openai=False)
    yield s
    asyncio.run(s.runtime.close())


def establish(s, material="Gr", metal="Pd"):
    iid = s.store.add_instruction(f"Use {material} and {metal} with top hBN; preserve images and source provenance.",
                                 project_id=s.project_id)
    r = call(s, "establish_reactor_requirements", {"instruction_id": iid, "reactive_material": material, "metal": metal})
    assert r.status == "ok", r
    return r.data


def plan():
    steps = [{"id": "s1", "title": "Inspect", "owner": "eyes"}]
    for material in ("Gr", "hBN"):
        for operation in ("exfoliate_chips", "search_chip"):
            steps.append({"id": f"s{len(steps)+1}", "title": f"{operation} {material}", "owner": "hands",
                          "operation": operation, "materials": [material]})
    steps += [{"id": "s6", "title": "Screen inputs", "owner": "picker", "experiment_stage": "screen"},
        {"id": "s7", "title": "Check neighbourhoods", "owner": "hands", "operation": "assess_reactor_inputs"},
        {"id": "s8", "title": "Confirm inputs", "owner": "picker", "experiment_stage": "confirm"},
        {"id": "s9", "title": "Assemble", "owner": "hands", "operation": "prepare_material_metal", "materials": ["hBN", "Gr"]},
        {"id": "s10", "title": "React Gr with Pd", "owner": "reactor", "operation": "reactor_material_metal"}]
    return steps


def save(s, steps):
    version = s.store.get_project(s.project_id)["plan_version"]
    return call(s, "update_plan", {"plan": steps, "note": "Synthetic review regression", "expected_version": version})


def test_roles_and_unified_status_include_unknown_reactor(lab):
    tools = default_registry()
    assert "get_reactor_setup" not in {t.name for t in tools.for_agent("eyes")}
    for name in ("assess_reactor_inputs", "prepare_reactor_pair"):
        assert tools.get(name).agents == {"hands"}
    status = call(lab, "get_instrument_status", role="eyes")
    assert status.data["reactor"] == {"enabled": True, "fresh_measurement": False, "readiness": "unknown", "observations": []}
    lab.store.emit("lab.frame", {"device": "reactor_heater", "temperature_c": 22})
    assert call(lab, "get_instrument_status", role="eyes").data["reactor"]["observations"][-1]["age_s"] >= 0


def test_refine_identity_progress_and_structural_conflict(lab):
    establish(lab)
    assert save(lab, plan()).status == "ok"
    lab.store.set_step_status(lab.project_id, "s1", "done")
    # Structural revision is still 1 despite newer progress versions.
    r = call(lab, "refine_plan", {"expected_revision": 1, "updates": [{"step_id": "s7", "fields": {"note": "Batch backups on each chip"}}],
        "additions": [{"title": "Report", "owner": "orchestrator"}], "note": "Refine without drift"})
    assert r.status == "ok", r
    current = r.data["plan"]
    assert current[0]["id"] == "s1" and current[0]["status"] == "done"
    assert current[-1]["id"] == "s11" and current[6]["owner"] == "hands"
    failed = call(lab, "refine_plan", {"expected_revision": 1, "note": "stale structural edit"})
    assert failed.data["error"] == "plan_revision_conflict"
    assert failed.data["current_plan"] == current
    assert requirements_record(lab.store, lab.project_id)["requirements"]["reactive_material"] == "Gr"


def test_added_steps_do_not_recycle_removed_historical_ids(lab):
    assert save(lab, [{"id": "s1", "owner": "orchestrator", "title": "Earlier report"}]).status == "ok"
    assert save(lab, []).status == "ok"
    result = call(lab, "refine_plan", {"expected_revision": 2, "note": "Add a new task",
        "additions": [{"owner": "orchestrator", "title": "New report"}]})
    assert result.status == "ok"
    assert result.data["plan"][0]["id"] == "s2"


def test_plan_failure_cannot_drop_hbn_or_rename_it_to_graphene(lab):
    original = establish(lab)
    assert save(lab, plan()).status == "ok"
    for changed in ([s for s in plan() if s.get("materials") != ["hBN"]], deepcopy(plan())):
        for s in changed:
            s["materials"] = ["Gr" if m == "hBN" else m for m in s.get("materials", [])]
        result = save(lab, changed)
        assert result.status == "error" and result.data["error"] == "experiment_requirements"
    assert requirements_record(lab.store, lab.project_id) == original
    assert call(lab, "establish_reactor_requirements", {"instruction_id": original["instruction_id"], "reactive_material": "Gr"}).status == "error"
    # Even an imported all-done plan and legacy accepted Gr/Gr result cannot certify the experiment.
    lab.store.set_plan(lab.project_id, [], author="synthetic adversarial fixture")
    lab.store.update_project_state(lab.project_id, reactor_result={"accepted": True, "scope": "two_flake_reaction"})
    assert call(lab, "save_project_status", {"status": "done", "summary": "All done"}).status == "error"
    assert call(lab, "set_reactor_pair_goal", {"chip_id": CHIP}).status == "error"
    assert call(lab, "reactor_reaction", {"chip_id": CHIP}, role="reactor").status in ("denied", "needs_attention")


def test_explicit_requirement_revision_keeps_original(lab):
    original = establish(lab)
    iid = lab.store.add_instruction("Change the experiment to Gr and Au, keeping top hBN.", project_id=lab.project_id)
    r = call(lab, "revise_reactor_requirements", {"instruction_id": iid, "reactive_material": "Gr", "metal": "Au",
        "expected_event_id": original["event_id"], "revision_reason": "User changed metal"})
    assert r.status == "ok" and r.data["previous_event_id"] == original["event_id"]
    events = [e for e in lab.store.events_after(0) if e.kind == "experiment.requirements"]
    assert [e.payload["requirements"]["metal"] for e in events] == ["Pd", "Au"]


def test_readiness_reports_all_gaps_and_complete_recipe_before_hands(lab):
    lab.store.update_project_state(lab.project_id, eyes_checked=True, experience_checked=["Gr"])
    step = {"id": "s1", "title": "Exfoliate Gr", "owner": "hands", "operation": "exfoliate_chips",
            "operation_args": {"material": "Gr", "target_temp_c": 50, "dwell_s": 30, "num_massages": 2, "peel_speed": 200}}
    result = save(lab, [step])
    assert result.status == "ok" and not result.data["executable"]
    errors = result.data["readiness_errors"]
    assert {e["rule"] for e in errors} >= {"sample_outcomes_required", "recipe_incomplete"}
    assert next(e for e in errors if e["rule"] == "recipe_incomplete")["missing_fields"] == ["press_depth_mm", "peel_angle_deg"]
    d = lab.gateway.delegate("hands", task="s1: Exfoliate Gr", require_sample_outcomes=True)
    assert not d.allow and d.args["readiness_errors"] == errors
    step["sample_outcomes"] = [{"sample_id": "chip_001", "in_storage": True}]
    step["operation_args"].update(press_depth_mm=0.2, peel_angle_deg=30)
    assert save(lab, [step]).status == "ok"
    assert lab.gateway.delegate("hands", task="s1: Exfoliate Gr", require_sample_outcomes=True).allow
    lab.gateway.delegation_started("h", "hands", "s1: Exfoliate Gr")
    bad = call(lab, "exfoliate_chips", {**step["operation_args"], "press_depth_mm": 0.3}, role="hands")
    assert bad.status == "denied" and bad.data["rule"] == "recipe_mismatch"
    assert not lab.store.list_runs()


def synthetic_candidates(s):
    for fid, material, chip in [("top", "hBN", "chip_001"), ("bottom", "Gr", "chip_002"), ("wrong", "Gr", "chip_002")]:
        s.store.upsert_sample(fid, "flake", location=chip, status="catalogued", props={"material": material,
            "chip_id": chip, "images": {"catalogue": "synthetic-fixture.png"}})
    s.store.upsert_sample(CHIP, "chip", props={"metal_coating": {"metal": "Pd", "substrate": "silicon",
        "preparation_id": "synthetic-coating", "prepared_by": "synthetic fixture", "source": "synthetic_fixture"}})


def test_picker_screen_hands_check_picker_confirm_and_no_legacy_bypass(lab, tmp_path):
    establish(lab)
    synthetic_candidates(lab)
    lab.store.update_project_state(lab.project_id, eyes_checked=True)
    args = {"chip_id": CHIP, "flake_ids": ["top", "bottom"], "rationale": "Synthetic workflow test"}
    assert call(lab, "submit_reactor_selection", {**args, "phase": "confirm"}, role="picker").status == "error"
    assert call(lab, "submit_reactor_selection", {**args, "phase": "screen"}, role="picker").status == "ok"
    assert call(lab, "submit_reactor_selection", {**args, "phase": "confirm"}, role="picker").status == "error"
    rid = lab.store.create_run("assess_reactor_inputs", {}, project_id=lab.project_id)
    lab.store.set_run_status(rid, "succeeded")
    for fid in args["flake_ids"]:
        f = lab.store.get_sample(fid)
        lab.store.upsert_sample(fid, "flake", props={"reactor_pickup_assessment": {"flake_id": fid, "clear": True,
            "source_chip_id": f["location"], "job_id": rid, "images": ["synthetic-local-fixture.png"]}})
    from test_agent_tools import assessment
    args["assessments"] = []
    for fid in args["flake_ids"]:
        image = tmp_path / f"{fid}.png"
        image.write_bytes(b"synthetic optical fixture")
        lab.store.upsert_sample(fid, "flake", props={"images": {"20x": str(image), "100x": str(image)}})
        call(lab, "view_flake_images", {"flake_id": fid}, role="picker")
        item = assessment("f_top" if fid == "top" else fid)
        item["flake_id"] = fid
        args["assessments"].append(item)
    # Selection needs actual outline evidence in addition to isolated local views.
    for fid, half in (("top", .1), ("bottom", .02)):
        f = lab.store.get_sample(fid)
        lab.store.upsert_sample(fid, "flake", props={"optical_geometry": {
            "outline_mm": [[-half, -half], [half, -half], [half, half], [-half, half]],
            "source_chip_id": f["location"], "job_id": rid, "image": "synthetic-outline.png",
            "units": "mm", "frame": "flake_center_chip_axes", "source": "simulated_optical_outline"}})
    assert call(lab, "submit_reactor_selection", {**args, "phase": "confirm"}, role="picker").status == "ok"
    before = lab.store.list_runs()
    result = call(lab, "prepare_reactor_pair", {k: args[k] for k in ("chip_id", "flake_ids")}, role="hands")
    assert result.status == "needs_attention" and "Use prepare_material_metal" in result.summary
    assert "legacy two-reactant fixture" in result.summary
    assert lab.store.list_runs() == before
    wrong = input_errors(lab.store, ReactorRequirements(reactive_material="Gr", metal="Pd"), ["bottom", "wrong"], carrier_id=CHIP)
    assert any("materials/order" in e for e in wrong)


def test_objective_is_captured_in_delegation_and_bound_to_provenance(lab):
    from qumus2.records.reactor_requirements import ReactorTaskObjective

    record = establish(lab)
    synthetic_candidates(lab)
    rid = lab.store.create_run("prepare_reactor_pair", {"target_chip_id": CHIP}, project_id=lab.project_id)
    lab.store.set_run_status(rid, "succeeded")
    manifest = {"top_first_flake_ids": ["top", "bottom"], "requirements_event_id": record["event_id"],
                "preparation_job_id": rid, "layer_order": ["hBN", "Gr", "Pd", "silicon"]}
    for fid in ("top", "bottom"):
        lab.store.upsert_sample(fid, "flake", location=CHIP, status="stacked")
    lab.store.emit("reactor.prepared", {"material_metal_preparation": manifest}, run_id=rid)
    # Explicit synthetic manifest to test the persistence boundary, not a motion claim.
    lab.store.upsert_sample(CHIP, "chip", props={"material_metal_preparation": manifest})
    objective = ReactorTaskObjective(chip_id=CHIP, requirements_event_id=record["event_id"],
                                    objective="Observe the Gr/Pd process; retain hBN", final_disposition="cold_storage")
    step = {"id": "s10", "owner": "reactor", "title": "React Gr with Pd", "operation": "reactor_material_metal",
            "reactor_outcome": {"chip_id": CHIP, "disposition": "cold_storage"}}
    lab.store.set_plan(lab.project_id, [step], author="synthetic fixture")
    lab.store.update_project_state(lab.project_id, eyes_checked=True)
    missing = lab.gateway.delegate("reactor", task="s10: React Gr with Pd", require_sample_outcomes=True)
    assert not missing.allow and missing.rule == "reactor_objective_required"
    accepted = lab.gateway.delegate("reactor", task="s10: React Gr with Pd", require_sample_outcomes=True,
                                    objective=objective.model_dump())
    assert accepted.allow, accepted.reason
    goal = lab.store.get_project(lab.project_id)["state"]["reactor_goal"]
    assert goal["source"] == "reactor_task_dialogue" and goal["metal_coating"]["preparation_id"] == "synthetic-coating"
    assert goal["requirements"]["protective_role"] == "nonreactive_pickup_and_protection"
    manifest["top_first_flake_ids"] = ["wrong", "bottom"]
    lab.store.upsert_sample(CHIP, "chip", props={"material_metal_preparation": manifest})
    with pytest.raises(ValueError, match="materials/order"):
        capture_reactor_objective(lab.store, lab.project_id, "s10: retry", objective, step)
    assert lab.store.get_project(lab.project_id)["state"]["reactor_goal"] == goal


def test_sdk_reactor_dialogue_schema_and_gateway_rejection(lab):
    pytest.importorskip("agents")
    from test_openai_runtime import events, fc, runtime, said

    establish(lab)
    lab.store.update_project_state(lab.project_id, eyes_checked=True)
    lab.store.set_plan(lab.project_id, [{"id": "s10", "owner": "reactor", "operation": "reactor_material_metal",
        "title": "React Gr with Pd", "reactor_outcome": {"chip_id": CHIP, "disposition": "cold_storage"}}],
        author="synthetic fixture")
    rt, model = runtime(lab, [[fc("delegate_reactor", {"input": "s10: React Gr with Pd", "objective": None}, "r1")],
                             said("Verified preparation and a matching objective are still missing.")])
    asyncio.run(rt.send("Check the established experiment readiness."))
    model.assert_complete()
    by_name = {tool.name: tool for tool in model.first_call.tools}
    assert "set_reactor_pair_goal" not in by_name
    schema = by_name["delegate_reactor"].params_json_schema
    assert "objective" in schema["required"]
    assert set(schema["properties"]["objective"]["anyOf"][0]["required"]) == {
        "chip_id", "requirements_event_id", "objective", "final_disposition"}
    assert events(lab, "tool.denied")[-1].payload["rule"] == "reactor_objective_required"
    assert not events(lab, "agent.delegate") and not lab.store.list_runs()
    asyncio.run(rt.close())


def test_metal_coating_report_is_idempotent_and_preserves_provenance(lab):
    text = f"{CHIP} has 20 nm Pd on silicon, preparation batch-42 prepared by operator-A."
    iid = lab.store.add_instruction(text, project_id=lab.project_id)
    args = {"chip_id": CHIP, "metal": "Pd", "thickness_nm": 20, "preparation_id": "batch-42",
            "prepared_by": "operator-A", "instruction_id": iid}
    first = call(lab, "record_metal_coating", args)
    assert first.status == "ok"
    repeated = call(lab, "record_metal_coating", args)
    assert repeated.status == "ok" and repeated.data == first.data
    iid2 = lab.store.add_instruction(text.replace("Pd", "Au"), project_id=lab.project_id)
    assert call(lab, "record_metal_coating", {**args, "metal": "Au", "instruction_id": iid2}).status == "error"
    assert lab.store.get_sample(CHIP)["props"]["metal_coating"] == first.data


@pytest.mark.parametrize("report", ["stamp_01 carries mica.", "stamp_01\u4e0a\u662fmica\u8584\u819c\u3002"])
def test_known_stamp_film_does_not_refresh_used_support(lab, report):
    from qumus2.records.stamps import contact_film

    lab.store.upsert_sample("stamp_01", "stamp", status="used")
    iid = lab.store.add_instruction(report, project_id=lab.project_id)
    result = call(lab, "record_stamp_support", {"stamp_id": "stamp_01", "film_type": "mica", "instruction_id": iid})
    assert result.status == "ok"
    sample = lab.store.get_sample("stamp_01")
    assert contact_film(sample) == "mica" and sample["status"] == "used"
    assert call(lab, "record_stamp_support", {"stamp_id": "stamp_01", "film_type": "PC", "instruction_id": iid}).status == "error"


def test_nested_checked_write_rolls_back_with_outer_transaction(lab):
    before = lab.store.last_event_id()
    with pytest.raises(RuntimeError):
        with lab.store._transaction():
            lab.store.set_plan(lab.project_id, [{"id": "s1"}], author="synthetic fixture")
            raise RuntimeError("abort compound plan check")
    assert not lab.store.get_project(lab.project_id)["plan"]
    assert lab.store.last_event_id() == before


def test_heated_exfoliation_applies_tape_once_without_changing_massage_count(lab):
    from test_reactor_preparation import execute
    from test_reactor_workflow import ready

    ready(lab)
    assert call(lab, "get_experience", {"material": "Gr"}, role="advisor").status == "ok"
    rid = execute(lab, "hands", "exfoliate_chips", material="Gr", chip_ids=["chip_001"],
                  target_temp_c=50, dwell_s=30, num_massages=2, peel_speed=200,
                  press_depth_mm=0.2, peel_angle_deg=30)
    assert lab.store.get_run(rid)["params"]["num_massages"] == 2
    active, commands = None, []
    for event in lab.store.events_after(0, limit=100000):
        if event.run_id != rid:
            continue
        if event.kind == "step.started":
            active = event.payload["name"]
        if event.kind == "device.command" and event.payload["device"] == "exfoliator":
            commands.append((active, event.payload["command"]))
    assert [(step, command) for step, command in commands if step in
            ("press_tape", "heat_applied_tape", "cool_and_peel") and command in ("apply", "peel")] == [
                ("press_tape", "apply"), ("cool_and_peel", "peel")]


def test_candidate_backups_share_one_loading_session_per_source(lab):
    from qumus2.flows.library.reactor_preparation import (
        AssessReactorInputsFlow,
        AssessReactorInputsParams,
    )

    flow = AssessReactorInputsFlow()
    flow._bench = lab.lab.bench
    p = AssessReactorInputsParams(chip_id=CHIP, flake_ids=["a", "b", "backup_a", "backup_b"],
        home_slots={"chip_001": "mid_chip_1", "chip_002": "mid_chip_2"},
        flake_chips={"a": "chip_001", "b": "chip_002", "backup_a": "chip_001", "backup_b": "chip_002"},
        materials={"a": "hBN", "b": "Gr", "backup_a": "hBN", "backup_b": "Gr"})
    names = [s.name for s in flow.build_steps(p)]
    for chip in p.home_slots:
        assert names.count(f"return_assessed_source_{chip}") == 1
    for fid in p.flake_ids:
        assert names.count(f"assess_reactor_pickup_{fid}") == 1
    assert not any("scan_chip" in n for n in names)


@pytest.mark.parametrize("material", ["graphene", "Graphene", "  graphene  "])
def test_requirement_alias_normalizes_and_canonical_retry_is_idempotent(lab, material):
    original = "Build hBN/graphene on Pd/silicon, inspect and react it, then cool and store it."
    iid = lab.store.add_instruction(original, project_id=lab.project_id)
    args = {"instruction_id": iid, "reactive_material": material, "metal": "Pd"}
    result = call(lab, "establish_reactor_requirements", args)
    assert result.status == "ok", result
    retained = requirements_record(lab.store, lab.project_id)
    assert retained["requirements"]["reactive_material"] == "Gr"
    assert retained["original_request"] == original
    assert lab.store.get_instruction(iid)["text"] == original
    project = lab.store.get_project(lab.project_id)
    again = call(lab, "establish_reactor_requirements", {**args, "reactive_material": "Gr"})
    assert again.status == "ok" and again.data == retained
    assert requirements_record(lab.store, lab.project_id) == retained
    assert lab.store.get_project(lab.project_id) == project
    assert len([e for e in lab.store.events_after(0, limit=10000)
                if e.kind == "experiment.requirements"]) == 1


def test_requirement_alias_absent_from_source_does_not_mutate_requirements(lab):
    iid = lab.store.add_instruction("Build hBN on Pd/silicon, inspect and react it.",
                                   project_id=lab.project_id)
    project, samples = lab.store.get_project(lab.project_id), lab.store.list_samples()
    boundary = lab.store.last_event_id()
    result = call(lab, "establish_reactor_requirements", {
        "instruction_id": iid, "reactive_material": "graphene", "metal": "Pd"})
    assert result.status == "error" and "must be explicit" in result.summary
    assert requirements_record(lab.store, lab.project_id) is None
    assert lab.store.get_project(lab.project_id) == project
    assert lab.store.list_samples() == samples
    # Gateway call/result audit events remain; no domain mutation is recorded.
    assert not any(e.kind == "experiment.requirements"
                   for e in lab.store.events_after(boundary, limit=10000))
