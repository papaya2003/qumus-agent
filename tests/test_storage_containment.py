"""Storage goals follow current typed carriers without changing plan conditions."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from qumus2.records.plan_completion import completion_contract, unmet_sample_outcomes


def carried_sample(s, kind="flake", *, parent="chip_e01", location="right_chip_1"):
    s.store.upsert_sample(parent, "chip", location=location, status="stacked", props={"location_uncertain": False})
    s.store.upsert_sample("assembled", kind, location=parent, status="stacked",
                          props={"chip_id": "chip_001", "location_uncertain": False})
    return {"sample_id": "assembled", "location": parent, "status": "stacked", "in_storage": True}


def unmet(s, condition, bench=None):
    events, samples = s.store.last_event_id(), s.store.list_samples()
    result = unmet_sample_outcomes(s.store, bench or s.lab.bench, [condition])
    assert s.store.last_event_id() == events and s.store.list_samples() == samples
    return result


@pytest.mark.parametrize("kind", ["flake", "device"])
def test_carried_material_is_stored_through_its_current_chip(services, kind):
    s = services
    condition = carried_sample(s, kind)
    # Historical source-chip provenance must not redirect the storage check.
    s.store.upsert_sample("chip_001", "chip", location="transfer_stage")
    assert unmet(s, condition) == []
    bench = deepcopy(s.lab.bench)
    bench["slots"]["unusual_bin_name"] = {"station": "right_storage", "holds": "chip"}
    s.store.upsert_sample("chip_e01", "chip", location="unusual_bin_name")
    assert unmet(s, condition, bench) == []


@pytest.mark.parametrize("kind", ["flake", "device"])
@pytest.mark.parametrize("where", ["transfer_stage", "arm_right", "reactor_port", "missing_slot", "right_stamp_1"])
def test_carrier_must_be_in_a_matching_configured_storage_slot(services, kind, where):
    s = services
    condition = carried_sample(s, kind, location=where)
    assert unmet(s, condition)


@pytest.mark.parametrize("kind", ["flake", "device"])
@pytest.mark.parametrize("uncertain", ["assembled", "chip_e01"])
def test_each_containment_link_requires_a_certain_location(services, kind, uncertain):
    s = services
    condition = carried_sample(s, kind)
    sample = s.store.get_sample(uncertain)
    s.store.upsert_sample(uncertain, sample["kind"], props={"location_uncertain": True})
    assert "uncertain" in " ".join(unmet(s, condition))


@pytest.mark.parametrize("kind", ["flake", "device"])
@pytest.mark.parametrize("parent_kind", ["flake", "device", "stamp", "carrier"])
def test_a_stored_nonchip_parent_is_not_a_valid_carrier(services, kind, parent_kind):
    s = services
    condition = carried_sample(s, kind)
    s.store.upsert_sample("chip_e01", parent_kind, location="right_stamp_1")
    assert "not a carrier chip" in " ".join(unmet(s, condition))


@pytest.mark.parametrize("kind", ["flake", "device"])
def test_missing_carrier_is_not_inferred_from_source_metadata(services, kind):
    s = services
    condition = carried_sample(s, kind)
    s.store.remove_sample("chip_e01", reason="missing current carrier fixture")
    assert "missing" in " ".join(unmet(s, condition))


@pytest.mark.parametrize("cycle", ["child_self", "carrier_self", "two_links", "chip_chain"])
def test_cycles_and_extra_sample_nesting_do_not_establish_storage(services, cycle):
    s = services
    condition = carried_sample(s)
    if cycle == "child_self":
        s.store.upsert_sample("assembled", "flake", location="assembled")
        condition["location"] = "assembled"
    elif cycle == "carrier_self":
        s.store.upsert_sample("chip_e01", "chip", location="chip_e01")
    elif cycle == "two_links":
        s.store.upsert_sample("chip_e01", "chip", location="assembled")
    else:
        s.store.upsert_sample("chip_e01", "chip", location="chip_001")
        s.store.upsert_sample("chip_001", "chip", location="chip_e01")
    assert unmet(s, condition)


@pytest.mark.parametrize("bad", ["wrong_holds", "not_storage", "unknown_station"])
def test_storage_station_and_slot_type_are_both_checked(services, bad):
    s = services
    condition = carried_sample(s)
    bench = deepcopy(s.lab.bench)
    if bad == "wrong_holds":
        bench["slots"]["right_chip_1"]["holds"] = "stamp"
    elif bad == "not_storage":
        bench["stations"]["right_storage"]["kind"] = "reactor"
    else:
        bench["slots"]["right_chip_1"]["station"] = "unknown"
    assert unmet(s, condition, bench)


@pytest.mark.parametrize("sample_id,kind,slot", [("chip_001", "chip", "left_chip_1"),
                                                ("stamp_01", "stamp", "right_stamp_1")])
def test_direct_chip_and_stamp_storage_semantics_are_preserved(services, sample_id, kind, slot):
    s = services
    s.store.upsert_sample(sample_id, kind, location=slot, props={"location_uncertain": False})
    condition = {"sample_id": sample_id, "in_storage": True}
    assert unmet(s, condition) == []
    s.store.upsert_sample(sample_id, kind, location="transfer_stage")
    assert "transfer_stage is not a matching storage slot" in " ".join(unmet(s, condition))


def test_containment_does_not_relax_explicit_child_status_or_location(services):
    s = services
    condition = carried_sample(s)
    assert unmet(s, {**condition, "status": "catalogued"})
    assert unmet(s, {**condition, "location": "chip_001"})


@pytest.mark.parametrize("carrier_stored", [True, False])
def test_status_recheck_can_complete_existing_transfer_only_with_stored_carrier(services, carrier_stored):
    s, g = services, services.gateway
    condition = carried_sample(s, location="right_chip_1" if carrier_stored else "transfer_stage")
    task = {"id": "s8", "title": "Stack the selected flake", "owner": "hands", "operation": "stack_device",
            "sample_outcomes": [condition]}
    s.store.set_plan(s.project_id, [task], author="fixture")
    # Recorded-success fixture: this test only rechecks, it never submits motion.
    rid = s.store.create_run("stack_device", {"flake_ids": ["assembled"], "target_chip_id": "chip_e01"},
                             project_id=s.project_id)
    s.store.set_run_status(rid, "succeeded")
    before_runs, before_samples = s.store.list_runs(), s.store.list_samples()
    last = s.store.last_event_id()
    g.delegation_started("recheck", "hands", "s8: Recheck the existing successful stacking job")
    result = asyncio.run(g.call("hands", "get_job_status", {"job_id": rid}))[0]
    assert result.status == "ok"
    facts = g.delegation_finished("recheck")
    saved = s.store.get_project(s.project_id)["plan"][0]
    assert saved["status"] == ("done" if carrier_stored else "pending"), facts
    assert completion_contract(saved) == completion_contract(task)
    assert s.store.list_runs() == before_runs and s.store.list_samples() == before_samples
    assert not any(e.kind in ("job.submitted", "device.command", "sample.updated")
                   for e in s.store.events_after(last, limit=10000))
