"""Two-flake ledger/acceptance guards, independent of morphology implementation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from threading import Event

import pytest
from test_reactor_devices import reactor_services
from test_reactor_workflow import CHIP, call

from qumus2.records.reactor_pairs import (
    prepared_pair,
    record_pair_product,
    validate_pair_observation,
)
from qumus2.runtime.store import RunStore


def seed_pair(store, chip_id="carrier"):
    manifest = {"schema_version": 1, "preparation_id": "prep-1", "chip_id": chip_id,
                "input_flake_ids": ["bottom", "top"], "preparation_source": "synthetic_fixture"}
    store.upsert_sample(chip_id, "chip", location="reactor_port",
                        props={"reactor_substrate": True, "reactor_pair": manifest})
    for fid, area in (("bottom", 400), ("top", 200)):
        store.upsert_sample(fid, "flake", location=chip_id, props={"area_um2": area, "material": fid})
    return manifest


def observation():
    return {"preparation_id": "prep-1", "input_flake_ids": ["bottom", "top"],
            "visible": True, "reaction_fraction": 1, "status": "complete", "model_id": "fixture-v1",
            "product": {"id": "product", "material": "synthetic product", "area_um2": 100,
                        "source_flake_ids": ["bottom", "top"], "material_state": {"fixture": True}}}


@pytest.fixture
def store():
    s = RunStore()
    seed_pair(s)
    return s


@pytest.mark.parametrize("ids", [["bottom"], ["bottom", "bottom"], ["bottom", "top", "third"]])
def test_cardinality_is_exactly_two_distinct_inputs(store, ids):
    pair = store.get_sample("carrier")["props"]["reactor_pair"]
    store.upsert_sample("carrier", "chip", props={"reactor_pair": {**pair, "input_flake_ids": ids}})
    with pytest.raises(ValueError):
        prepared_pair(store, "carrier")


@pytest.mark.parametrize("mutation", ["third", "moved", "reserved", "consumed", "bad_area"])
def test_manifest_cannot_override_actual_ledger_state(store, mutation):
    if mutation == "third":
        store.upsert_sample("third", "flake", location="carrier", props={"area_um2": 10})
    elif mutation == "moved":
        store.upsert_sample("top", "flake", location="another_chip")
    elif mutation == "reserved":
        rid = store.create_run("other", {})
        store.reserve("sample", "top", rid, "other")
    else:
        store.upsert_sample("top", "flake", props={"reactor_consumed_by": "previous"}
                            if mutation == "consumed" else {"area_um2": True})
    with pytest.raises(ValueError):
        prepared_pair(store, "carrier")


@pytest.mark.parametrize("mutation", ["wrong_pair", "wrong_lineage", "too_large", "same_id", "nan", "boolean"])
def test_product_rejects_identity_and_area_forgery(store, mutation):
    obs = observation()
    if mutation == "wrong_pair":
        obs["input_flake_ids"] = ["top", "bottom"]
    else:
        key, value = {"wrong_lineage": ("source_flake_ids", ["bottom", "missing"]),
                      "too_large": ("area_um2", 201), "same_id": ("id", "bottom"),
                      "nan": ("area_um2", float("nan")), "boolean": ("area_um2", True)}[mutation]
        obs["product"][key] = value
    with pytest.raises(ValueError):
        validate_pair_observation(store, "carrier", obs, require_product=True)


@pytest.mark.parametrize("change", [{"product": None}, {"visible": False},
                                   {"status": "failed", "failure_reason": "fixture failure"}])
def test_no_verified_product_is_not_success(store, change):
    obs = {**observation(), **change}
    validate_pair_observation(store, "carrier", obs)
    with pytest.raises(ValueError, match="no visible verified"):
        validate_pair_observation(store, "carrier", obs, require_product=True)


def test_product_is_idempotent_and_sources_are_retained(store):
    pid = store.create_project("pair")
    rid = store.create_run("reactor_reaction", {"chip_id": "carrier"}, project_id=pid)
    store.set_run_status(rid, "succeeded")
    for _ in range(2):
        assert record_pair_product(store, "carrier", observation(), reaction_job_id=rid, project_id=pid) == "product"
    assert len(store.list_samples(kind="flake")) == 3
    assert all(store.get_sample(fid)["status"] == "reactor_consumed" for fid in ("bottom", "top"))
    with pytest.raises(ValueError, match="consumed"):
        prepared_pair(store, "carrier")
    obs = deepcopy(observation())
    obs["product"]["id"] = "second_product"
    with pytest.raises(ValueError, match="different product"):
        validate_pair_observation(store, "carrier", obs, require_product=True)
    exported = [e.payload for e in store.events_after(0) if e.kind == "sample.updated"]
    assert any(e["props"].get("source_flake_ids") == ["bottom", "top"] for e in exported)


def test_competing_results_across_connections_create_only_one_product(tmp_path, monkeypatch):
    path = tmp_path / "pair.db"
    first, second = RunStore(path), RunStore(path)
    seed_pair(first)
    pid = first.create_project("pair")
    rid = first.create_run("reactor_reaction", {"chip_id": "carrier"}, project_id=pid)
    first.set_run_status(rid, "succeeded")
    first_validated, release_first, second_started = Event(), Event(), Event()
    original = first.upsert_sample

    def hold_first_write(sample_id, *args, **kwargs):
        if sample_id == "product-a":
            first_validated.set()
            assert release_first.wait(timeout=5)
        return original(sample_id, *args, **kwargs)

    monkeypatch.setattr(first, "upsert_sample", hold_first_write)

    def accept(store, product_id):
        obs = observation()
        obs["product"]["id"] = product_id
        if product_id == "product-b":
            second_started.set()
        try:
            return record_pair_product(store, "carrier", obs, reaction_job_id=rid, project_id=pid)
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(accept, first, "product-a")
        assert first_validated.wait(timeout=5)
        two = pool.submit(accept, second, "product-b")
        assert second_started.wait(timeout=5)
        try:
            # The second connection must wait before any acceptance reads,
            # even while the first has validated but has not inserted a row.
            with pytest.raises(TimeoutError):
                two.result(timeout=0.1)
        finally:
            release_first.set()
        futures = [one, two]
        results = [f.result(timeout=10) for f in futures]
    assert sum(x is not None for x in results) == 1
    assert len(first.list_samples(kind="flake")) == 3


def test_write_failure_rolls_back_product_sources_and_events(store, monkeypatch):
    pid = store.create_project("pair")
    rid = store.create_run("reactor_reaction", {"chip_id": "carrier"}, project_id=pid)
    store.set_run_status(rid, "succeeded")
    before = [e.model_dump() for e in store.events_after(0)]
    original = store.upsert_sample

    def broken(sample_id, *args, **kwargs):
        original(sample_id, *args, **kwargs)
        if sample_id == "bottom":
            raise ValueError("injected interruption after first consumed source")

    monkeypatch.setattr(store, "upsert_sample", broken)
    with pytest.raises(ValueError, match="injected"):
        record_pair_product(store, "carrier", observation(), reaction_job_id=rid, project_id=pid)
    assert store.get_sample("product") is None
    assert all(not store.get_sample(fid)["props"].get("reactor_consumed_by") for fid in ("bottom", "top"))
    assert [e.model_dump() for e in store.events_after(0)] == before


def test_pair_result_checks_job_goal_uncertainty_and_current_observation():
    s = reactor_services()
    seed_pair(s.store, CHIP)
    s.lab.bench["reactor_module"]["experiment_kind"] = "two_flake_reaction"
    assert call(s, "orchestrator", "set_reactor_pair_goal", {"chip_id": CHIP}).status == "ok"
    rid = s.store.create_run("reactor_reaction", {"chip_id": CHIP}, project_id=s.project_id)
    s.store.set_run_status(rid, "succeeded")
    evidence = {"reaction_job_id": rid, "pair_observation": {**observation(), "product": None}}
    s.store.upsert_sample(CHIP, "chip", props={"reactor_result": evidence})
    args = {"chip_id": CHIP, "reaction_job_id": rid}
    assert call(s, "reactor", "submit_reactor_pair_result", args).status == "needs_attention"
    s.store.upsert_sample(CHIP, "chip", props={"reactor_result": {**evidence, "pair_observation": observation()},
                                              "reactor_state_uncertain": True})
    assert call(s, "reactor", "submit_reactor_pair_result", args).status == "error"
    s.store.upsert_sample(CHIP, "chip", props={"reactor_state_uncertain": False})
    assert call(s, "reactor", "submit_reactor_pair_result", args).status == "ok"
    later = s.store.create_run("reactor_reaction", {"chip_id": CHIP}, project_id=s.project_id)
    s.store.set_run_status(later, "failed")
    assert "later" in call(s, "reactor", "submit_reactor_pair_result", args).summary


def test_reaction_flow_reserves_both_inputs_and_disables_legacy_growth():
    s = reactor_services()
    seed_pair(s.store, CHIP)
    s.lab.bench["reactor_module"]["experiment_kind"] = "two_flake_reaction"
    from qumus2.flows.library.reactor import GrowthParams, ReactorReactionFlow

    flow = ReactorReactionFlow()
    flow._bench = s.lab.bench
    params = flow.resolve_params(GrowthParams(chip_id=CHIP), s.store)
    rid = s.store.create_run("reactor_reaction", params.model_dump())
    flow.reserve(params, s.store, rid)
    assert {CHIP, "bottom", "top"} <= set(s.store.reserved_samples())
    s.store.set_run_status(rid, "succeeded")
    with pytest.raises(ValueError, match="field-growth"):
        s.jobs.submit("reactor_growth", {"chip_id": CHIP})
