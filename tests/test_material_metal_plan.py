"""Recorded v3 contracts with isolated synthetic ledger replay; no device worker."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import v2_recording

from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.records.material_metal_plan import preparation_evidence, preparation_job_error
from qumus2.records.plan_completion import invalid_flow_sample_statuses
from qumus2.records.reactor_requirements import requirements_record

RECORDED = json.loads((Path(__file__).parent / "fixtures/material_metal_plan_live_v3.json").read_text())
RECORDED_V2 = json.loads((Path(__file__).parent / "fixtures/material_metal_plan_live_v2.json").read_text())
RECORDED_V4 = json.loads((Path(__file__).parent / "fixtures/material_metal_plan_live_v4.json").read_text())
CHIP, JOB = "reactor_chip_01", RECORDED["job"]["id"]


def call(s, tool, args=None, role="orchestrator"):
    return asyncio.run(s.gateway.call(role, tool, args or {}))[0]


@pytest.fixture
def lab(monkeypatch):
    s = build_services(":memory:", testbench_config=simulation_profile(), use_openai=False)

    def no_execution(*args, **kwargs):
        raise AssertionError("Plan/evidence checks must not submit hardware")

    monkeypatch.setattr(s.jobs, "submit", no_execution)
    s.store.set_plan(s.project_id, deepcopy(RECORDED["initial_plan"]), author="orchestrator", note="Recorded initial draft")
    iid = s.store.add_instruction(RECORDED["requirements"]["original_request"], project_id=s.project_id)
    assert call(s, "establish_reactor_requirements", {"instruction_id": iid, "reactive_material": "Gr", "metal": "Pd"}).status == "ok"
    s.store.update_project_state(s.project_id, eyes_checked=True, experience_checked=["hBN", "Gr"])
    for raw in RECORDED["samples"]:
        sample = deepcopy(raw)
        sample["props"].pop("material_metal_preparation", None)
        if sample["kind"] == "flake":
            sample["location"], sample["status"] = sample["props"]["chip_id"], "selected"
            sample["props"].pop("material_placement", None)
        if sample["id"] == CHIP:
            sample["status"] = "bare"
        s.store.upsert_sample(**{k: sample[k] for k in ("kind", "location", "status", "props")}, sample_id=sample["id"])
    yield s
    asyncio.run(s.runtime.close())


def restore_bad_plan(s, *, bound=False, author="orchestrator"):
    s.store.set_plan(s.project_id, deepcopy(RECORDED["bad_plan"]), author=author, note="Recorded model mistakes")
    if bound:
        s.store.set_plan(s.project_id, deepcopy(RECORDED["bound_plan"]), author=author, note="Recorded exact input binding")


def prepared(s, fixture=RECORDED):
    """Load the recorded successful receipt, remapping only project/instruction IDs."""
    record = requirements_record(s.store, s.project_id)

    def remap(value):
        if isinstance(value, dict):
            return {k: s.project_id if k == "project_id" else record["event_id"] if k == "requirements_event_id"
                    else record["instruction_id"] if k == "instruction_id" else remap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [remap(v) for v in value]
        return value

    for sample in remap(fixture["samples"]):
        s.store.upsert_sample(sample["id"], sample["kind"], location=sample["location"],
                              status=sample["status"], props=sample["props"])
    run = remap(fixture["job"])
    s.store.create_run(run["flow"], run["params"], project_id=s.project_id, requested_by="hands", run_id=run["id"])
    s.store.set_run_status(run["id"], run["status"])
    s.store.emit("agent.job_linked", {"project_id": s.project_id, "step_id": fixture.get("step", {}).get("id", "s12"),
                 "job_id": run["id"], "flow": "prepare_material_metal"}, run_id=run["id"], agent="hands")
    s.store.emit("reactor.prepared", remap(fixture["prepared_event"]), run_id=run["id"])
    if "process_event" in fixture:
        s.store.emit("reactor.process", remap(fixture["process_event"]), run_id=run["id"])


def corrected_args(s):
    updates = []
    for step in s.store.get_project(s.project_id)["plan"]:
        if step["id"] not in ("s7", "s12"):
            continue
        conditions = [{**c, "status": "material_metal_prepared"} if c["sample_id"] == CHIP else c
                      for c in step["sample_outcomes"]]
        updates.append({"step_id": step["id"], "fields": {"operation": "prepare_material_metal", "sample_outcomes": conditions}})
    return {"expected_revision": s.store.get_project(s.project_id)["plan_revision"], "updates": updates,
            "note": "Correct model mechanics against the unchanged original requirements"}


def snapshot(s):
    return deepcopy(s.store.get_project(s.project_id)), s.store.plan_history(s.project_id), s.store.list_samples(), s.store.list_runs()


def test_recorded_bad_legacy_and_bare_plan_are_rejected_before_hardware(lab):
    s = lab
    before = snapshot(s)
    args = deepcopy(RECORDED["bad_refinement_args"])
    args["expected_revision"] = s.store.get_project(s.project_id)["plan_revision"]
    result = call(s, "refine_plan", args)
    assert result.status == "error"
    assert result.data.get("error") == "material_metal_operation" or result.data.get("outcome_contract_errors")
    assert snapshot(s) == before
    # A restored legacy database must receive the same checks at delegation.
    restore_bad_plan(s)
    for sid, rule in [("s7", "material_metal_operation"), ("s12", "flow_status_contract")]:
        decision = s.gateway.delegate("hands", task=f"{sid}: Prepare the original stack", require_sample_outcomes=True)
        assert not decision.allow
        assert rule in {e["rule"] for e in decision.args["readiness_errors"]}
    assert not s.store.list_runs()


def test_terminal_statuses_match_device_roles_and_keep_abstract_fixture_separate(lab):
    for operation in ("prepare_material_metal", "reactor_material_metal"):
        carrier_status = "material_metal_prepared" if operation == "prepare_material_metal" else "reactor_processed"
        step = {"id": "s12", "operation": operation, "sample_outcomes": [
            {"sample_id": CHIP, "status": "bare"}, {"sample_id": "chip_001_f012", "status": "reactor_consumed"}]}
        errors = invalid_flow_sample_statuses(lab.store, step)
        assert {(e["role"], e["allowed_status"]) for e in errors} == {
            ("carrier", carrier_status), ("input_flakes", "stacked")}
        for outcome, status in zip(step["sample_outcomes"], (carrier_status, "stacked"), strict=True):
            outcome["status"] = status
        assert not invalid_flow_sample_statuses(lab.store, step)
        step["operation_args"] = {"protocol_id": "synthetic-material-metal-workflow-v1"}
        step["sample_outcomes"] = [{"sample_id": CHIP, "status": "bare"}]
        assert not invalid_flow_sample_statuses(lab.store, step)
    step["operation"], step["operation_args"] = "assess_reactor_inputs", {}
    assert not invalid_flow_sample_statuses(lab.store, step), "Local assessment preserves carrier status"


def test_original_goal_repairs_model_mistakes_before_execution_without_user_revision(lab):
    s = lab
    restore_bad_plan(s)
    before = s.store.get_project(s.project_id)
    requirements = requirements_record(s.store, s.project_id)
    result = call(s, "refine_plan", corrected_args(s))
    assert result.status == "ok", result
    assert requirements_record(s.store, s.project_id) == requirements
    assert [x["id"] for x in result.data["plan"]] == [x["id"] for x in before["plan"]]
    assert len(result.data["outcome_corrections"]) == 2
    for correction in result.data["outcome_corrections"]:
        assert correction["requirements_event_id"] == requirements["event_id"]
        assert correction["model_plan_event_ids"] and correction["execution_evidence"] is None
    for old, new in zip(before["plan"], result.data["plan"], strict=True):
        if old["id"] in ("s7", "s12"):
            assert new["status"] == "pending" and new["operation"] == "prepare_material_metal"
            assert old["materials"] == new["materials"] == ["hBN", "Gr"]
            assert old["operation_args"] == new["operation_args"]
            assert {k:v for k,v in old["sample_outcomes"][0].items() if k != "status"} == {
                k:v for k,v in new["sample_outcomes"][0].items() if k != "status"}
    assert not s.store.list_runs()


def test_recorded_refusal_can_be_repaired_atomically_with_the_original_input_bindings(lab):
    s = lab
    restore_bad_plan(s)
    args = deepcopy(RECORDED["correction_attempt_args"])
    args["expected_revision"] = s.store.get_project(s.project_id)["plan_revision"]
    # The original correction retained the incompatible legacy companion step.
    before = snapshot(s)
    assert call(s, "refine_plan", args).status == "error"
    assert snapshot(s) == before
    companion = next(u["fields"] for u in args["updates"] if u["step_id"] == "s7")
    companion["operation"] = "prepare_material_metal"
    companion["sample_outcomes"][0]["status"] = "material_metal_prepared"
    result = call(s, "refine_plan", args)
    assert result.status == "ok", result
    for sid in ("s7", "s12"):
        actual = next(p for p in result.data["plan"] if p["id"] == sid)
        requested = next(u["fields"] for u in RECORDED["correction_attempt_args"]["updates"] if u["step_id"] == sid)
        assert actual["operation_args"] == requested["operation_args"]
        assert actual["status"] == "pending"
    assert not s.store.list_runs()


def test_recorded_success_closes_both_corrected_ids_by_exact_job_recheck_without_replay(lab):
    s = lab
    restore_bad_plan(s, bound=True)
    prepared(s)
    assert RECORDED["completion_refusal"]["ok"] is False
    assert "expected bare" in " ".join(RECORDED["completion_refusal"]["facts"])
    requirements, samples, jobs = requirements_record(s.store, s.project_id), s.store.list_samples(), s.store.list_runs()
    start = s.store.last_event_id()
    result = call(s, "refine_plan", corrected_args(s))
    assert result.status == "ok", result
    assert all(c["execution_evidence"]["job_id"] == JOB for c in result.data["outcome_corrections"])
    context = call(s, "get_project_context").data["completed_preparation_evidence"]
    assert {e["step_id"] for e in context} == {"s7", "s12"}
    for sid in ("s12", "s7"):
        decision = s.gateway.delegate("hands", task=f"{sid}: Recheck exact original job {JOB}", require_sample_outcomes=True)
        assert decision.allow, decision.reason
        s.gateway.delegation_started(sid, "hands", f"{sid}: Recheck {JOB}")
        assert call(s, "wait_job", {"job_id": JOB}, role="hands").status == "ok"
        assert f"{sid} is now done" in s.gateway.delegation_finished(sid)
    assert requirements_record(s.store, s.project_id) == requirements
    assert s.store.list_samples() == samples and s.store.list_runs() == jobs
    assert not any(e.kind in ("job.submitted", "device.command", "sample.updated") for e in s.store.events_after(start))
    assert next(p for p in s.store.get_project(s.project_id)["plan"] if p["id"] == "s8")["status"] == "pending"


@pytest.mark.parametrize("conflict", ["human_status", "human_operation", "human_origin", "target", "storage", "material", "stamp", "metal"])
def test_correction_never_weakens_human_or_other_physical_obligations(lab, conflict):
    s = lab
    restore_bad_plan(s, bound=True, author="human" if conflict == "human_origin" else "orchestrator")
    if conflict == "human_status":
        s.store.add_instruction("The final carrier status must remain bare.", project_id=s.project_id)
    if conflict == "human_operation":
        s.store.add_instruction("Use prepare_reactor_pair explicitly.", project_id=s.project_id)
    args = corrected_args(s)
    fields = next(u["fields"] for u in args["updates"] if u["step_id"] == "s12")
    if conflict == "target":
        fields["sample_outcomes"][0]["sample_id"] = "reactor_chip_02"
    if conflict == "storage":
        fields["sample_outcomes"][0]["in_storage"] = False
    if conflict == "material":
        fields["materials"] = ["hBN", "MoS2"]
    if conflict in ("stamp", "metal"):
        step = next(p for p in s.store.get_project(s.project_id)["plan"] if p["id"] == "s12")
        fields["operation_args"] = {**step["operation_args"], **({"stamp_id": "stamp_02"} if conflict == "stamp" else {"metal": "Au"})}
    before = snapshot(s)
    assert call(s, "refine_plan", args).status == "error"
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["ambiguous", "parked", "legacy_job", "missing_optics", "unlinked", "changed_receipt", "wrong_flakes", "uncertain", "reserved"])
def test_execution_correction_requires_unique_matching_independent_evidence(lab, fault):
    s = lab
    restore_bad_plan(s, bound=True)
    prepared(s)
    if fault == "ambiguous":
        rid = s.store.create_run("prepare_material_metal", RECORDED["job"]["params"], project_id=s.project_id)
        s.store.set_run_status(rid, "succeeded")
    if fault == "parked":
        s.store.set_run_status(JOB, "parked")
    if fault == "legacy_job":
        s.store._x("UPDATE run SET flow='prepare_reactor_pair' WHERE id=?", (JOB,))
    if fault == "missing_optics":
        s.store._x("DELETE FROM event WHERE kind='reactor.prepared'")
    if fault == "unlinked":
        s.store._x("DELETE FROM event WHERE kind='agent.job_linked'")
    if fault == "changed_receipt":
        receipt = deepcopy(s.store.get_sample(CHIP)["props"]["material_metal_preparation"])
        receipt["requirements_event_id"] += 1
        s.store.upsert_sample(CHIP, "chip", props={"material_metal_preparation": receipt})
    if fault == "wrong_flakes":
        run = s.store.get_run(JOB)
        run["params"]["flake_ids"].reverse()
        s.store._x("UPDATE run SET params_json=? WHERE id=?", (json.dumps(run["params"]), JOB))
    if fault == "uncertain":
        s.store.upsert_sample(CHIP, "chip", props={"location_uncertain": True})
    if fault == "reserved":
        s.store.reserve("sample", CHIP, JOB, "prepare_material_metal")
    before = snapshot(s)
    result = call(s, "refine_plan", corrected_args(s))
    assert result.status == "error", result
    assert snapshot(s) == before


@pytest.mark.parametrize("unrelated", ["other_project", "other_target", "other_flow", "ambiguous_job"])
def test_unrelated_success_cannot_close_a_corrected_step(lab, unrelated):
    s = lab
    restore_bad_plan(s, bound=True)
    prepared(s)
    assert call(s, "refine_plan", corrected_args(s)).status == "ok"
    params = deepcopy(s.store.get_run(JOB)["params"])
    pid = s.store.create_project("Other synthetic project") if unrelated == "other_project" else s.project_id
    if unrelated == "other_target":
        params["target_chip_id"] = "reactor_chip_02"
    flow = "inspect_bench" if unrelated == "other_flow" else "prepare_material_metal"
    rid = s.store.create_run(flow, params, project_id=pid)
    s.store.set_run_status(rid, "succeeded")
    s.gateway.delegation_started("bad", "hands", "s12: Recheck original preparation")
    assert call(s, "wait_job", {"job_id": rid}, role="hands").status == "ok"
    facts = s.gateway.delegation_finished("bad")
    assert "cannot certify preparation" in facts
    assert next(p for p in s.store.get_project(s.project_id)["plan"] if p["id"] == "s12")["status"] != "done"
    assert len(s.store.list_runs()) == 2


def test_prepared_evidence_is_not_a_completion_or_reaction_claim(lab):
    s = lab
    restore_bad_plan(s, bound=True)
    prepared(s)
    step = next(p for p in s.store.get_project(s.project_id)["plan"] if p["id"] == "s12")
    assert preparation_evidence(s.store, s.project_id, step)["job_id"] == JOB
    assert step["status"] == "pending"
    assert call(s, "save_project_status", {"summary": "Preparation is not reaction acceptance", "status": "done"}).status == "error"


def test_abstract_fixture_cannot_close_corrected_device_preparation(lab):
    from qumus2.records.material_metal import FIXTURE_PROTOCOL
    from qumus2.records.material_metal_plan import preparation_job_error

    s = lab
    restore_bad_plan(s, bound=True)
    prepared(s)
    assert call(s, "refine_plan", corrected_args(s)).status == "ok"
    step = next(p for p in s.store.get_project(s.project_id)["plan"] if p["id"] == "s12")
    run = deepcopy(s.store.get_run(JOB))
    run["params"]["protocol_id"] = FIXTURE_PROTOCOL
    assert "Abstract fixture" in preparation_job_error(s.store, s.project_id, step, run)


def load_v2(s):
    # Import into a different isolated project with its own original instruction.
    # Neither source run nor its stored successful plan is edited.
    s.project_id = s.store.create_project("Recorded v2 evidence regression")
    s.gateway.project_id = s.project_id
    iid = s.store.add_instruction(RECORDED_V2["requirements"]["original_request"], project_id=s.project_id)
    assert call(s, "establish_reactor_requirements", {"instruction_id": iid, "reactive_material": "Gr", "metal": "Pd"}).status == "ok"
    step = deepcopy(RECORDED_V2["step"])
    s.store.set_plan(s.project_id, [step], author="orchestrator", note="Recorded v2 successful contract")
    prepared(s, RECORDED_V2)
    return step, s.store.get_run(RECORDED_V2["job"]["id"])


def test_recorded_v2_semantic_parameters_recheck_against_exact_original_evidence(lab):
    s = lab
    step, run = load_v2(s)
    assert {"top_material", "reactive_material", "metal", "substrate", "retain_images", "retain_source_provenance"} <= step["operation_args"].keys()
    assert preparation_evidence(s.store, s.project_id, step)["job_id"] == run["id"]
    assert preparation_job_error(s.store, s.project_id, step, run) is None
    before = s.store.list_samples(), s.store.list_runs()
    assert call(s, "wait_job", {"job_id": run["id"]}, role="hands").status == "ok"
    # V2 already proceeded through Reactor. Reading its earlier preparation
    # cannot undo its checked completion or reinterpret the advanced chip state.
    assert s.store.get_sample(CHIP)["status"] == "reactor_processed"
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "done"
    assert (s.store.list_samples(), s.store.list_runs()) == before


@pytest.mark.parametrize("fault", ["top_material", "reactive_material", "substrate", "retain_images", "retain_source_provenance",
                                 "missing_image", "source_provenance", "missing_optical_event", "unknown_argument"])
def test_v2_semantic_arguments_require_positive_evidence_not_blanket_aliases(lab, fault):
    s = lab
    step, run = load_v2(s)
    if fault in ("top_material", "reactive_material", "substrate"):
        step["operation_args"][fault] = "wrong"
    if fault in ("retain_images", "retain_source_provenance"):
        step["operation_args"][fault] = False
    if fault == "unknown_argument":
        step["operation_args"]["unverified_promise"] = True
    if fault == "missing_optical_event":
        s.store._x("DELETE FROM event WHERE kind='reactor.prepared'")
    if fault == "missing_image":
        s.store.upsert_sample(run["params"]["flake_ids"][0], "flake", props={"images": {"source": "different.png"}})
    if fault == "source_provenance":
        s.store.upsert_sample(run["params"]["flake_ids"][0], "flake", props={"chip_id": "different_source"})
    assert preparation_evidence(s.store, s.project_id, step) is None
    assert preparation_job_error(s.store, s.project_id, step, run)


def load_v4(s):
    s.project_id = s.store.create_project("Recorded v4 argument regression")
    s.gateway.project_id = s.project_id
    iid = s.store.add_instruction(RECORDED_V4["requirements"]["original_request"], project_id=s.project_id)
    assert call(s, "establish_reactor_requirements", {"instruction_id": iid, "reactive_material": "Gr", "metal": "Pd"}).status == "ok"
    step = deepcopy(RECORDED_V4["step"])
    s.store.set_plan(s.project_id, deepcopy(RECORDED_V4["plan"]), author="orchestrator", note="Original refused v4 contract")
    prepared(s, RECORDED_V4)
    s.store.update_project_state(s.project_id, eyes_checked=True, experience_checked=["hBN", "Gr"])
    return step, s.store.get_run(RECORDED_V4["job"]["id"])


def test_v4_raw_aliases_recheck_original_job_without_repeating_preparation(lab):
    from qumus2.agents.readiness import step_readiness
    from qumus2.records.material_metal_arguments import argument_errors, normalize_arguments

    s = lab
    step, run = load_v4(s)
    raw = deepcopy(step["operation_args"])
    assert not argument_errors(s.store, s.project_id, step)
    assert not step_readiness(s.store, s.store.get_project(s.project_id), step, s.gateway.registry)
    canonical, errors = normalize_arguments(step["operation"], raw)
    assert not errors and canonical["stamp_film"] == "mica" and canonical["retain_images"] is True
    assert preparation_job_error(s.store, s.project_id, step, run) is None
    start = s.store.last_event_id()
    before = s.store.list_samples(), s.store.list_runs()
    s.gateway.delegation_started("v4-recheck", "hands", "s6: Recheck the exact original preparation job")
    assert call(s, "wait_job", {"job_id": run["id"]}, role="hands").status in ("ok", "corrected")
    assert "s6 is now done" in s.gateway.delegation_finished("v4-recheck")
    current = s.store.get_project(s.project_id)
    assert next(p for p in current["plan"] if p["id"] == "s6")["operation_args"] == raw and current["state"]["eyes_checked"]
    assert (s.store.list_samples(), s.store.list_runs()) == before
    assert not any(e.kind in ("job.submitted", "device.command", "sample.updated") for e in s.store.events_after(start))


@pytest.mark.parametrize("changes", [
    {"invented_property": True}, {"stamp_film": "PC"}, {"preserve_images": "true"},
    {"preserve_provenance": False}, {"support": "unknown"}, {"metal_preparation_id": "wrong"},
    {"flake_ids": ["same", "same"]}, {"reactive_material": "MoS2"},
])
def test_v4_invalid_arguments_refused_at_save_and_delegation(lab, changes):
    from qumus2.agents.readiness import step_readiness

    s = lab
    step, _ = load_v4(s)
    args = {**step["operation_args"], **changes}
    before = snapshot(s)
    result = call(s, "refine_plan", {"expected_revision": s.store.get_project(s.project_id)["plan_revision"],
        "updates": [{"step_id": "s6", "fields": {"operation_args": args}}], "note": "Check invalid recorded argument variation"})
    assert result.status == "error" and result.data["error"] == "material_metal_arguments"
    assert snapshot(s) == before
    errors = step_readiness(s.store, s.store.get_project(s.project_id), {**step, "operation_args": args}, s.gateway.registry)
    assert any(e["rule"] == "material_metal_arguments" for e in errors)


def test_v4_submission_mismatch_is_refused_before_any_new_job(lab):
    from qumus2.records.material_metal_arguments import submission_errors

    s = lab
    step, _ = load_v4(s)
    supplied = {"chip_id": CHIP, "flake_ids": RECORDED_V4["job"]["params"]["flake_ids"], "stamp_id": "stamp_01"}
    assert not submission_errors(s.store, s.project_id, step, supplied, s.lab.bench)
    for changed in ({"stamp_id": "stamp_02"}, {"chip_id": "reactor_chip_02"}, {"flake_ids": list(reversed(supplied["flake_ids"]))}):
        assert submission_errors(s.store, s.project_id, step, {**supplied, **changed}, s.lab.bench)


def test_restored_unknown_plan_argument_does_not_invalidate_successful_physical_job(lab):
    s = lab
    _, run = load_v4(s)
    plan = s.store.get_project(s.project_id)["plan"]
    next(p for p in plan if p["id"] == "s6")["operation_args"]["unsupported_legacy_field"] = True
    s.store.set_plan(s.project_id, plan, author="orchestrator", note="Restored legacy plan before schema existed")
    before = s.store.list_samples(), s.store.list_runs()
    s.gateway.delegation_started("legacy-recheck", "hands", "s6: Read the original preparation only")
    assert call(s, "wait_job", {"job_id": run["id"]}, role="hands").status in ("ok", "corrected")
    facts = s.gateway.delegation_finished("legacy-recheck")
    assert "plan argument validation remains unresolved" in facts
    assert "inspect the bench again" not in facts
    assert s.store.get_project(s.project_id)["state"]["eyes_checked"]
    assert next(p for p in s.store.get_project(s.project_id)["plan"] if p["id"] == "s6")["status"] == "pending"
    assert (s.store.list_samples(), s.store.list_runs()) == before


@pytest.mark.parametrize("call_id", [11677, 11682])
def test_v5_confirmed_flake_binding_refines_null_placeholders_without_execution(lab, call_id):
    data = json.loads((v2_recording("openai-material-metal-default-20261028-v5")
                       / "recording.json").read_text())
    events = data["events"]
    old = next(e["payload"]["plan"] for e in reversed(events)
               if e["id"] < call_id and e["kind"] in ("plan.updated", "plan.step"))
    s = lab
    load_v4(s)  # same recorded source identities/materials in an isolated ledger
    s.store.set_plan(s.project_id, deepcopy(old), author="orchestrator", note="Recorded v5 before source binding")
    args = deepcopy(next(e["payload"]["args"] for e in events if e["id"] == call_id))
    args["expected_revision"] = s.store.get_project(s.project_id)["plan_revision"]
    samples, jobs = s.store.list_samples(), s.store.list_runs()
    start = s.store.last_event_id()
    result = call(s, "refine_plan", args)
    assert result.status == "ok", result.summary
    plan = s.store.get_project(s.project_id)["plan"]
    for step in plan:
        previous = next(p for p in old if p["id"] == step["id"])
        assert step["status"] == previous["status"]
        if step["id"] in ("s6", "s7"):
            assert step["operation_args"]["flake_ids"] == ["chip_001_f003", "chip_002_f009"]
            assert step["operation_args"]["metal"] == "Pd"
            assert step["operation_args"]["stamp_film"] == "mica"
    assert (s.store.list_samples(), s.store.list_runs()) == (samples, jobs)
    assert not any(e.kind in ("device.command", "job.submitted", "sample.updated") for e in s.store.events_after(start))


@pytest.mark.parametrize("operation", ["prepare_material_metal", "reactor_material_metal"])
def test_unbound_flake_refinement_never_changes_existing_ids_or_other_targets(operation):
    from qumus2.records.plan_completion import preserves_obligation

    old = {"owner": "hands", "operation": operation, "operation_args": {
        "chip_id": CHIP, "flake_ids": None, "hbn_flake_id": None, "reactive_flake_id": None,
        "stamp_film": "mica", "metal": "Pd", "temperature_c": None}}
    bound = deepcopy(old)
    bound["operation_args"].update(flake_ids=["hbn", "gr"], hbn_flake_id="hbn", reactive_flake_id="gr")
    assert preserves_obligation(old, bound)
    for key, value in (("chip_id", "other"), ("metal", "Au"), ("stamp_film", "PC"), ("temperature_c", 300)):
        changed = deepcopy(bound)
        changed["operation_args"][key] = value
        assert not preserves_obligation(old, changed)
    changed = deepcopy(bound)
    changed["operation_args"].update(flake_ids=["hbn", "other"], reactive_flake_id="other")
    assert not preserves_obligation(bound, changed)
    assert not preserves_obligation({**old, "operation": "unrelated"}, {**bound, "operation": "unrelated"})
    off = {**old, "reactor_outcome": {"chip_id": CHIP, "disposition": "cold_storage",
        "temperature_target_c": None, "flow_target_sccm": 0, "rationale": "retain off targets"}}
    on = {**bound, "reactor_outcome": {**off["reactor_outcome"], "temperature_target_c": 300}}
    assert not preserves_obligation(off, on)


def test_v6_future_search_drafts_do_not_block_ready_exfoliation(lab):
    s = lab
    plan = json.loads((Path(__file__).parent / "fixtures/material_metal_plan_live_v6.json").read_text())["plan"]
    s.store.set_plan(s.project_id, plan, author="orchestrator", note="Recorded v6 before premature human-help request")
    context = call(s, "get_project_context").data
    assert context["executable"] and not context["all_steps_ready"]
    assert context["executable_scope"] == "next_pending_step"
    assert context["next_pending_step"]["step_id"] == "s8"
    assert context["next_pending_step"]["readiness_errors"] == []
    assert {e["step_id"] for e in context["readiness_errors"]} == {"s9", "s11"}
    assert s.gateway.delegate("hands", task="s8: Exfoliate hBN", require_sample_outcomes=True).allow
    assert not s.gateway.delegate("hands", task="s9: Search hBN chip", require_sample_outcomes=True).allow
    result = call(s, "refine_plan", {"expected_revision": s.store.get_project(s.project_id)["plan_revision"],
        "updates": [], "note": "Retain ready producer and future unbound searches"})
    assert result.status == "ok" and result.data["executable"]
    before = snapshot(s)
    invalid = call(s, "refine_plan", {"expected_revision": s.store.get_project(s.project_id)["plan_revision"],
        "updates": [{"step_id": "s9", "fields": {"operation": None}}], "note": "Invalid removal of required search"})
    assert invalid.status == "error" and invalid.data["current_execution"]["executable"]
    assert snapshot(s) == before


@pytest.mark.parametrize("missing", ["recipe", "outcomes", "eyes"])
def test_v6_current_step_still_requires_all_prerequisites(lab, missing):
    s = lab
    plan = json.loads((Path(__file__).parent / "fixtures/material_metal_plan_live_v6.json").read_text())["plan"]
    step = next(p for p in plan if p["id"] == "s8")
    if missing == "recipe":
        step["operation_args"].pop("dwell_s")
    elif missing == "outcomes":
        step["sample_outcomes"] = []
    else:
        s.store.update_project_state(s.project_id, eyes_checked=False)
    s.store.set_plan(s.project_id, plan, author="orchestrator", note="Incomplete current producer")
    report = call(s, "get_project_context").data
    assert not report["executable"] and report["next_pending_step"]["readiness_errors"]
    assert not s.gateway.delegate("hands", task="s8: Exfoliate hBN", require_sample_outcomes=True).allow
    assert {"s9", "s11"} <= {e["step_id"] for e in report["readiness_errors"]}
