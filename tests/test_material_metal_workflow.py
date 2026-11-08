"""Positive adapter pipeline using an explicitly authorized ABSTRACT fixture.

Catalogue photos and assessment records are synthetic inputs. Preparation and
observation jobs really run through the gateway/job engine. The adapter does not
claim physical transfers, reactor exposure or chemistry, and cannot finish the
original experiment. Original SimWorld and sample locations must not change.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from PIL import Image, ImageDraw

from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.records.material_metal import (
    FIXTURE_PROTOCOL,
    adapter_for,
    preparation_errors,
    workflow_result_errors,
)
from qumus2.records.reactor_requirements import requirements_record

CHIP = "reactor_chip_01"


def call(s, role, tool, args=None):
    return asyncio.run(s.gateway.call(role, tool, args or {}))[0]


def execute(s, role, tool, args):
    submitted = call(s, role, tool, args)
    assert submitted.status == "pending", submitted
    job_id = submitted.data["job_id"]
    result = call(s, role, "wait_job", {"job_id": job_id, "timeout_s": 30})
    assert result.status in ("ok", "corrected"), result
    assert s.store.get_run(job_id)["status"] == "succeeded"
    return job_id


@pytest.fixture
def workflow(tmp_path):
    s = build_services(tmp_path / "workflow.db", testbench_config=simulation_profile(), use_openai=False)
    original = s.store.add_instruction("Use Gr and Pd with nonreactive top hBN; keep source provenance.", project_id=s.project_id)
    assert call(s, "orchestrator", "establish_reactor_requirements", {"instruction_id": original,
        "reactive_material": "Gr", "metal": "Pd"}).status == "ok"
    permission = s.store.add_instruction(f"Authorize {FIXTURE_PROTOCOL} solely to test software handoffs. "
        "Do not claim real motion, release, chemical products or experimental acceptance.", project_id=s.project_id)
    photo = tmp_path / "catalogue-software-fixture.png"
    image = Image.new("RGB", (500, 60), "white")
    ImageDraw.Draw(image).text((8, 20), "SYNTHETIC WORKFLOW INPUT - NOT A MICROGRAPH", fill="black")
    image.save(photo)
    assessment = s.store.create_run("assess_reactor_inputs", {}, project_id=s.project_id)
    s.store.set_run_status(assessment, "succeeded")
    for fid, material, source in (("fixture_top", "hBN", "chip_001"), ("fixture_reactive", "Gr", "chip_002")):
        s.store.upsert_sample(fid, "flake", location=source, status="catalogued", props={"material": material,
            "chip_id": source, "images": {"synthetic_catalogue_fixture": str(photo), "20x": str(photo), "100x": str(photo)},
            "optical_geometry": {"outline_mm": [[-.1,-.1],[.1,-.1],[.1,.1],[-.1,.1]] if material == "hBN" else [[-.01,-.01],[.01,-.01],[.01,.01],[-.01,.01]],
                "units": "mm", "frame": "flake_center_chip_axes", "source": "simulated_optical_outline", "source_chip_id": source,
                "image": str(photo), "job_id": assessment},
            "fixture_scope": "abstract_workflow_inputs_not_optical_assessment",
            "reactor_pickup_assessment": {"flake_id": fid, "clear": True, "source_chip_id": source,
                "job_id": assessment, "images": [str(photo)]}})
    coating = s.store.add_instruction(f"{CHIP} has 20 nm Pd on silicon, preparation fixture-pd by fixture-operator.", project_id=s.project_id)
    assert call(s, "orchestrator", "record_metal_coating", {"chip_id": CHIP, "metal": "Pd", "thickness_nm": 20,
        "preparation_id": "fixture-pd", "prepared_by": "fixture-operator", "instruction_id": coating}).status == "ok"
    s.store.update_project_state(s.project_id, eyes_checked=True, experience_checked=["Gr", "hBN"])
    steps = [
        {"id": "s1", "title": "Screen fixture inputs", "owner": "picker", "experiment_stage": "screen"},
        {"id": "s2", "title": "Check fixture input records", "owner": "hands", "operation": "assess_reactor_inputs"},
        {"id": "s3", "title": "Confirm fixture inputs", "owner": "picker", "experiment_stage": "confirm"},
        {"id": "s4", "title": "Exercise material-metal preparation software", "owner": "hands", "operation": "prepare_material_metal",
         "materials": ["hBN", "Gr"], "sample_outcomes": [{"sample_id": CHIP, "in_storage": True}]},
        {"id": "s5", "title": "Observe material-metal workflow receipt", "owner": "reactor", "operation": "reactor_material_metal",
         "reactor_outcome": {"chip_id": CHIP, "disposition": "cold_storage", "temperature_target_c": None,
             "flow_target_sccm": 0, "rationale": "Abstract fixture keeps physical samples in storage"}}]
    saved = call(s, "orchestrator", "update_plan", {"plan": steps, "note": "Explicit software fixture alongside an uncompleted experiment", "task_kind": "experiment"})
    assert saved.status == "ok", saved
    selected = {"chip_id": CHIP, "flake_ids": ["fixture_top", "fixture_reactive"], "rationale": "Explicitly synthetic input records"}
    from test_agent_tools import assessment as quality
    selected["assessments"] = []
    for fid in selected["flake_ids"]:
        call(s, "picker", "view_flake_images", {"flake_id": fid})
        item = quality("f_top" if fid == "fixture_top" else fid)
        item["flake_id"] = fid
        item["reason"] = "Explicit abstract fixture, not a physical optical assessment"
        selected["assessments"].append(item)
    assert call(s, "picker", "submit_reactor_selection", {**selected, "phase": "screen"}).status == "ok"
    assert call(s, "picker", "submit_reactor_selection", {**selected, "phase": "confirm"}).status == "ok"
    yield s, permission, selected
    asyncio.run(s.runtime.close())


def prepare(workflow, film="mica"):
    s, permission, selected = workflow
    iid = s.store.add_instruction(f"stamp_01 carries {film}.", project_id=s.project_id)
    assert call(s, "orchestrator", "record_stamp_support", {"stamp_id": "stamp_01", "film_type": film, "instruction_id": iid}).status == "ok"
    args = {"chip_id": CHIP, "flake_ids": selected["flake_ids"], "stamp_id": "stamp_01",
            "protocol_id": FIXTURE_PROTOCOL, "fixture_instruction_id": permission}
    assert s.gateway.delegate("hands", task="s4: Exercise preparation software", require_sample_outcomes=True).allow
    s.gateway.delegation_started("prepare", "hands", "s4: Exercise preparation software")
    job = execute(s, "hands", "prepare_material_metal", args)
    s.gateway.delegation_finished("prepare")
    assert s.store.get_project(s.project_id)["plan"][3]["status"] == "done"
    return job


@pytest.mark.parametrize("film", ["PC", "mica"])
def test_positive_ordered_jobs_objective_result_without_fabricated_physics(workflow, film):
    s, permission, selected = workflow
    before_samples = {row["id"]: (row["location"], row["status"]) for row in s.store.list_samples()}
    before_world = deepcopy(s.lab.world.snapshot())
    prepared = prepare(workflow, film)
    assert not preparation_errors(s.store, s.project_id, CHIP)
    receipt = s.store.get_sample(CHIP)["props"]["material_metal_preparation"]
    assert receipt["preparation_job_id"] == prepared
    assert receipt["stamp_film"] == film and receipt["layer_order"] == ["hBN", "Gr", "Pd", "silicon"]
    assert receipt["physical_motion_verified"] is receipt["chemistry_verified"] is False
    rid = requirements_record(s.store, s.project_id)["event_id"]
    objective = {"chip_id": CHIP, "requirements_event_id": rid, "objective": "Verify only the abstract software handoff",
                 "final_disposition": "cold_storage"}
    decision = s.gateway.delegate("reactor", task="s5: Observe the software fixture", objective=objective, require_sample_outcomes=True)
    assert decision.allow, decision.reason
    s.gateway.delegation_started("observe", "reactor", "s5: Observe the software fixture")
    observed = execute(s, "reactor", "reactor_material_metal", {"chip_id": CHIP, "protocol_id": FIXTURE_PROTOCOL,
                                                              "fixture_instruction_id": permission})
    accepted = call(s, "reactor", "submit_material_metal_result", {"chip_id": CHIP, "reaction_job_id": observed})
    assert accepted.status == "ok", accepted
    assert accepted.data["workflow_accepted"] and not accepted.data["experiment_accepted"]
    assert not workflow_result_errors(s.store, s.project_id, accepted.data)
    assert [step["action"] for step in accepted.data["observation"]["sequence"]] == ["pickup", "pickup", "release", "observe"]
    assert accepted.data["observation"]["product_identity"] is accepted.data["observation"]["reaction_fraction"] is None
    s.gateway.delegation_finished("observe")
    assert s.store.get_project(s.project_id)["plan"][4]["status"] == "done"
    assert s.lab.world.snapshot() == before_world
    assert {row["id"]: (row["location"], row["status"]) for row in s.store.list_samples()} == before_samples
    # Software success can never finish the chemical experiment, even if an
    # imported plan is empty. hBN stays catalogued and is never consumed.
    s.store.set_plan(s.project_id, [], author="adversarial completion fixture")
    done = call(s, "orchestrator", "save_project_status", {"status": "done", "summary": "All fixture jobs passed"})
    assert done.status == "error" and "No material-metal result" in done.summary
    assert not s.store.get_project(s.project_id)["state"].get("reactor_result")
    assert not any(row["status"] in ("reactor_consumed", "reactor_product") for row in s.store.list_samples(kind="flake"))
    assert s.store.spent_usd() == 0


def test_fixture_cannot_be_selected_by_an_ordinary_request_or_physical_bench(workflow):
    s, permission, selected = workflow
    ordinary = requirements_record(s.store, s.project_id)["instruction_id"]
    args = {"chip_id": CHIP, "flake_ids": selected["flake_ids"], "stamp_id": "stamp_01",
            "protocol_id": FIXTURE_PROTOCOL, "fixture_instruction_id": ordinary}
    assert call(s, "hands", "prepare_material_metal", args).status == "error"
    assert call(s, "hands", "prepare_material_metal", {**args, "protocol_id": "invented-pd-release"}).status == "error"
    s.store.bench = "physical"
    with pytest.raises(ValueError, match="physical"):
        adapter_for(s.store, FIXTURE_PROTOCOL, permission, s.project_id)
    s.store.bench = "sim"
    assert all(run["flow"] != "prepare_material_metal" for run in s.store.list_runs())


def test_positive_receipt_cannot_bypass_goal_change_or_provenance_tampering(workflow):
    s, permission, _ = workflow
    prepare(workflow)
    args = {"chip_id": CHIP, "protocol_id": FIXTURE_PROTOCOL, "fixture_instruction_id": permission}
    assert call(s, "reactor", "reactor_material_metal", args).status == "error"
    receipt = s.store.get_sample(CHIP)["props"]["material_metal_preparation"]
    s.store.upsert_sample(CHIP, "chip", props={"material_metal_preparation": {**receipt, "layer_order": ["Gr", "hBN", "Pd", "silicon"]}})
    assert any("layer order" in e for e in preparation_errors(s.store, s.project_id, CHIP))
    objective = {"chip_id": CHIP, "requirements_event_id": receipt["requirements_event_id"],
                 "objective": "Observe fixture", "final_disposition": "cold_storage"}
    assert not s.gateway.delegate("reactor", task="s5: Observe fixture", objective=objective, require_sample_outcomes=True).allow
    s.store.upsert_sample(CHIP, "chip", props={"material_metal_preparation": receipt})
    other = s.store.create_run("reactor_material_metal", {"chip_id": CHIP}, project_id="wrong-project")
    s.store.set_run_status(other, "succeeded")
    assert call(s, "reactor", "submit_material_metal_result", {"chip_id": CHIP, "reaction_job_id": other}).status == "error"


def test_interrupted_release_resumes_the_same_job_without_claiming_early_preparation(workflow, monkeypatch):
    from qumus2.testbench.material_metal_fixture import SyntheticMaterialMetalAdapter

    s, permission, selected = workflow
    original = SyntheticMaterialMetalAdapter.apply
    failed = False

    def interrupt_release(self, ctx, action, payload):
        nonlocal failed
        if action == "release" and not failed:
            failed = True
            raise ValueError("Injected abstract release-adapter failure; no physical action occurred")
        return original(self, ctx, action, payload)

    monkeypatch.setattr(SyntheticMaterialMetalAdapter, "apply", interrupt_release)
    submitted = call(s, "hands", "prepare_material_metal", {"chip_id": CHIP, "flake_ids": selected["flake_ids"],
        "stamp_id": "stamp_01", "protocol_id": FIXTURE_PROTOCOL, "fixture_instruction_id": permission})
    assert submitted.status == "pending"
    job_id = submitted.data["job_id"]
    first = call(s, "hands", "wait_job", {"job_id": job_id, "timeout_s": 30})
    assert first.status == "needs_attention"
    assert not s.store.get_sample(CHIP)["props"].get("material_metal_preparation")
    assert CHIP in s.store.reserved_samples()
    s.store.update_project_state(s.project_id, eyes_checked=True)
    retried = call(s, "hands", "retry_job", {"job_id": job_id,
        "reason": "The one-shot software fixture fault is cleared; no physical sample or device moved"})
    assert retried.status == "pending", retried
    final = call(s, "hands", "wait_job", {"job_id": job_id, "timeout_s": 30})
    assert final.status in ("ok", "corrected"), final
    assert not preparation_errors(s.store, s.project_id, CHIP)
    assert not s.store.reserved_samples()
    actions = [e.payload["action"] for e in s.store.events_after(0, limit=10000)
               if e.kind == "material_metal.fixture" and e.run_id == job_id]
    assert actions == ["initialize", "pickup", "pickup", "release"]
