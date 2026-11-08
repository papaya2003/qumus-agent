"""Configured simulation identity is usable without inventing a human review."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
from conftest import quiet_bench

from qumus2.agents.gateway import ToolGateway
from qumus2.agents.tooling import default_registry
from qumus2.app import build_lab_side
from qumus2.records.materials import material_review, review_material, simulation_identity
from qumus2.runtime.store import RunStore
from qumus2.testbench.persist import lab_state, save_lab_state


def call(gateway, agent, tool, args=None):
    return asyncio.run(gateway.call(agent, tool, args or {}))[0]


@pytest.mark.parametrize("sid,material", [("carrier_gr_01", "Gr"), ("carrier_hbn_01", "hBN")])
def test_seed_identity_is_explicit_and_query_does_not_create_a_human_review(services, sid, material):
    s = services
    before = s.store.list_samples()
    result = call(s.gateway, "eyes", "list_samples", {"kind": "carrier"})
    row = next(r for r in result.data["samples"] if r["id"] == sid)
    assert result.data["bench"] == "sim"
    assert row["material_source"] == "inventory_label"
    assert row["human_review"] == {"status": "unreviewed", "material": material}
    identity = row["simulation_identity"]
    assert identity["usable"] and identity["bench"] == "sim"
    assert identity["declaration"] == {"source": "testbench_config.initial_samples",
                                      "sample_id": sid, "material": material}
    assert "not physical material confirmation" in identity["scope"]
    assert identity["readiness"].startswith("not evaluated")
    assert s.store.list_samples() == before
    emitted = next(e for e in s.store.events_after(0, limit=10000)
                   if e.kind == "sample.updated" and e.payload["id"] == sid)
    assert emitted.payload["props"]["simulation_identity"] == identity["declaration"]
    assert "material_reviews" not in emitted.payload["props"]


@pytest.mark.parametrize("bad", ["missing", "unknown_material", "changed_label", "wrong_source", "wrong_id",
                                 "uncertain", "conflicting_review", "not_carrier"])
def test_unknown_or_conflicting_identity_cannot_use_the_simulation_exception(services, bad):
    sample = deepcopy(services.store.get_sample("carrier_gr_01"))
    props = sample["props"]
    if bad == "missing":
        props.pop("simulation_identity")
    elif bad == "unknown_material":
        props["material"] = None
    elif bad == "changed_label":
        props["material"] = "hBN"
    elif bad == "wrong_source":
        props["simulation_identity"]["source"] = "camera_appearance"
    elif bad == "wrong_id":
        props["simulation_identity"]["sample_id"] = "other_carrier"
    elif bad in ("uncertain", "conflicting_review"):
        props["material_reviews"] = [{"via": "ui:lab", "status": "uncertain" if bad == "uncertain" else "confirmed",
                                      "material": "Gr" if bad == "uncertain" else "hBN"}]
    else:
        sample["kind"] = "chip"
    before = deepcopy(sample)
    assert not simulation_identity(sample, "sim")["usable"]
    assert sample == before


@pytest.mark.parametrize("bench", ["real", "unknown"])
def test_simulation_declaration_never_confirms_material_on_another_bench(services, bench):
    sample = services.store.get_sample("carrier_gr_01")
    assert not simulation_identity(sample, bench)["usable"]
    assert material_review(sample)["status"] == "unreviewed"


def test_setup_scopes_physical_confirmation_and_preserves_query_boundaries(services):
    for bench in ("sim", "real"):
        store = RunStore(bench=bench)
        gateway = ToolGateway(default_registry(), store, None)
        result = call(gateway, "advisor", "get_lab_setup")
        assert result.data["bench"] == bench
        policy = result.data["material_identity_policy"]
        assert "simulation_identity.usable=true" in policy["simulation"]
        assert "real hardware" in policy["physical"]
        assert "uncertain or conflicting" in policy["unresolved"]
        assert "real hardware only" in result.data["external_dependencies"][-1]
        assert result.data["readiness"] == "not evaluated by this query"
        assert "samples" not in result.data and "carriers" not in result.data
        assert call(gateway, "eyes", "get_lab_setup").data["rule"] == "permission"
        store.close()


def test_identity_and_human_review_stay_distinct_after_reopen(services, tmp_path):
    path = tmp_path / "identities.db"
    store = RunStore(path)
    seeded = services.store.get_sample("carrier_gr_01")
    store.upsert_sample(seeded["id"], "carrier", location=seeded["location"], props=seeded["props"])
    store.upsert_sample("legacy", "carrier", location="left_slide_2", props={"material": "hBN"})
    store.close()
    store = RunStore(path)
    before = store.list_samples(), store.last_event_id()
    assert simulation_identity(store.get_sample(seeded["id"]), store.bench)["usable"]
    assert material_review(store.get_sample(seeded["id"]))["status"] == "unreviewed"
    assert not simulation_identity(store.get_sample("legacy"), store.bench)["usable"]
    assert (store.list_samples(), store.last_event_id()) == before
    store.close()


@pytest.mark.parametrize("material,sid", [("Gr", "carrier_gr_01"), ("hBN", "carrier_hbn_01")])
def test_simulated_preparation_retains_identity_review_and_actual_checks(services, material, sid):
    s = services
    args = {"material": material, "chip_ids": ["chip_002"], "num_chips": 1}
    # A usable identity is not Eyes observation or recipe evidence.
    assert simulation_identity(s.store.get_sample(sid), s.store.bench)["usable"]
    s.store.set_plan(s.project_id, [{"id": "s1", "owner": "hands", "title": "Exfoliate one chip",
        "operation": "exfoliate_chips", "sample_outcomes": [
            {"sample_id": "chip_002", "status": "exfoliated", "in_storage": True}]}], author="fixture")
    assert s.gateway.delegate("hands", task=f"s1: exfoliate {material}", require_sample_outcomes=True).rule == "eyes_first"
    refused = call(s.gateway, "hands", "exfoliate_chips", args)
    assert refused.status == "denied" and refused.data["rule"] == "experience_checked"
    assert call(s.gateway, "eyes", "inspect_bench").status == "ok"
    refused = call(s.gateway, "hands", "exfoliate_chips", args)
    assert refused.status == "denied" and refused.data["rule"] == "experience_checked"
    assert call(s.gateway, "advisor", "get_experience", {"material": material}).status == "ok"
    submitted = call(s.gateway, "hands", "exfoliate_chips", args)
    assert submitted.status == "pending", submitted
    result = call(s.gateway, "hands", "wait_job", {"job_id": submitted.data["job_id"], "timeout_s": 60})
    assert result.status in ("ok", "corrected"), result
    chip = s.store.get_sample("chip_002")
    assert chip["status"] == "exfoliated" and chip["props"]["material"] == material
    source = s.store.get_sample(sid)
    assert material_review(source)["status"] == "unreviewed"
    assert "material_reviews" not in source["props"]
    assert simulation_identity(source, s.store.bench)["usable"]
    events = s.store.events_after(0, limit=100000)
    assert any(e.kind == "step.verified" for e in events)
    assert any(e.kind == "device.command" for e in events)
    assert not any(e.kind == "safety.violation" for e in events)
    assert s.store.get_sample("chip_001")["status"] == "bare"


def test_a_real_uncertain_review_still_prevents_preparation(services):
    s = services
    review_material(s.store, s.lab.bench, "carrier_gr_01", expected_material="Gr",
                    by="Test operator", status="uncertain", note="Conflicting preparation record")
    assert not simulation_identity(s.store.get_sample("carrier_gr_01"), s.store.bench)["usable"]
    before = s.store.last_event_id(), s.store.list_runs()
    with pytest.raises(ValueError, match="human material review"):
        s.jobs.submit("exfoliate_chips", {"material": "Gr"})
    assert (s.store.last_event_id(), s.store.list_runs()) == before
    review_material(s.store, s.lab.bench, "carrier_gr_01", expected_material="Gr",
                    by="Test operator", status="confirmed", note="Preparation record checked")
    assert simulation_identity(s.store.get_sample("carrier_gr_01"), s.store.bench)["usable"]
    assert material_review(s.store.get_sample("carrier_gr_01"))["status"] == "confirmed"


@pytest.mark.parametrize("case", ["legacy", "current", "missing_object", "wrong_kind", "wrong_material",
                                  "wrong_id", "unknown_material", "uncertain_review"])
def test_checkpoint_restore_uses_actual_objects_without_rewriting_history(tmp_path, case):
    db, checkpoint = tmp_path / "q.db", tmp_path / "world.pkl"
    side = build_lab_side(db, seed=1, testbench_config=quiet_bench())
    sid = "carrier_gr_01"
    sample = side.store.get_sample(sid)
    if case == "legacy":
        # Explicit pre-upgrade fixture; no original recording is edited.
        sample["props"].pop("simulation_identity")
        side.store._x("UPDATE sample SET props_json=? WHERE id=?", (json.dumps(sample["props"]), sid))
    obj = side.lab.world.objects[sid]
    if case == "missing_object":
        side.lab.world.objects.pop(sid)
    elif case == "wrong_kind":
        obj.kind = "chip"
    elif case == "wrong_material":
        obj.material = "hBN"
    elif case == "wrong_id":
        obj.id = "a_different_carrier"
    elif case == "unknown_material":
        obj.material = None
    elif case == "uncertain_review":
        review_material(side.store, side.lab.bench, sid, expected_material="Gr",
                        by="Test operator", status="uncertain")
    save_lab_state(side.lab, side.clock, checkpoint)
    before = deepcopy(lab_state(side.lab, side.clock))
    before["world"]["_observation_rng"] = before["world"]["_observation_rng"].getstate()
    samples, events = side.store.list_samples(), side.store.events_after(limit=10000)
    side.store.close()
    for _ in range(2):
        restored = build_lab_side(db, state_path=checkpoint, testbench_config=quiet_bench())
        assert restored.store.list_samples() == samples
        assert restored.store.events_after(limit=10000) == events
        after = lab_state(restored.lab, restored.clock)
        after["world"]["_observation_rng"] = after["world"]["_observation_rng"].getstate()
        assert after["world"] == before["world"] and after["clock"] == before["clock"]
        declarations = json.loads(restored.store.get_meta("simulation_identity_restore"))
        result = simulation_identity(restored.store.get_sample(sid), restored.store.bench, restored=declarations)
        assert result["usable"] == (case in ("legacy", "current"))
        if result["usable"]:
            assert result["declaration"]["source"] == "restored_testbench_world"
        if case != "uncertain_review":
            assert material_review(restored.store.get_sample(sid))["status"] == "unreviewed"
        restored.store.close()


def test_fresh_bench_clears_an_obsolete_restore_declaration(tmp_path):
    db = tmp_path / "q.db"
    side = build_lab_side(db, seed=1, testbench_config=quiet_bench())
    side.store.set_meta("simulation_identity_restore", "{}")
    side.store.close()
    fresh = build_lab_side(db, seed=1, testbench_config=quiet_bench())
    assert json.loads(fresh.store.get_meta("simulation_identity_restore")) is None
    assert simulation_identity(fresh.store.get_sample("carrier_gr_01"), fresh.store.bench)["usable"]
    fresh.store.close()
