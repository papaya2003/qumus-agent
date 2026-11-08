"""Public frozen-state/C5 controls only; no device or physical job execution."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from qumus2.core.errors import NeedsHumanError
from qumus2.flows.library import stacking
from qumus2.flows.workflow import WorkflowMismatch
from qumus2.records.geometry import DirectContactContract, geometry_digest
from qumus2.runtime.jobs import build_run_steps
from tests.test_direct_contact_contract import REQUIREMENT, mock_pickup_record
from tests.test_direct_contact_contract import public_source as observed_public_source

public_source = observed_public_source


@pytest.fixture
def queued_source(public_source):
    s = public_source
    with patch.object(s.jobs, "enqueue") as enqueue:
        rid = s.jobs.submit(
            "stack_device",
            {
                "flake_ids": ["gr"],
                "target_chip_id": "chip_e01",
                "direct_contact_material": "Gr",
                "target_contact_region_id": "centre_pair",
                "bridge_requirement": REQUIREMENT,
            },
        )
        enqueue.assert_called_once_with(rid)
    return s, rid, s.store.get_run(rid)["params"]


def mutate(s, raw, change):
    c = raw["direct_contact_contract"]
    if change == "image":
        Path(c["source_observation"]["image"]).write_bytes(b"queued source bytes changed")
    elif change == "geometry":
        g = deepcopy(s.store.get_sample("gr")["props"]["optical_geometry"])
        g["outline_mm"][0][0] += 0.001
        s.store.upsert_sample("gr", "flake", props={"optical_geometry": g})
    elif change == "receiver_history":
        s.store.upsert_sample("chip_e01", "chip", props={"layers_top_first": ["foreign_hbn"]})
        s.store.upsert_sample("chip_e01", "chip", props={"layers_top_first": []})
    else:
        s.store.upsert_sample(
            "chip_e01",
            "chip",
            props={"layers_top_first": None if change == "unknown_receiver" else ["foreign_hbn"]},
        )


@pytest.mark.parametrize("phase", ["queued", "postpickup"])
@pytest.mark.parametrize(
    "change", ["image", "geometry", "receiver", "unknown_receiver", "receiver_history"]
)
def test_actual_queued_c5_refuses_external_drift_before_build_or_device_dispatch(
    queued_source, monkeypatch, change, phase
):
    s, rid, raw = queued_source
    if phase == "postpickup":
        pickup_phase(s, rid, raw, monkeypatch)
    mutate(s, raw, change)
    before = s.store.last_event_id()
    monkeypatch.setattr(
        stacking.StackDeviceFlow,
        "build_steps",
        lambda *a: pytest.fail("C5 refusal must precede step construction"),
    )
    with pytest.raises(WorkflowMismatch) as error:
        build_run_steps(s.store.get_run(rid), s.lab.bench, store=s.store)
    assert error.value.reason == "direct_contact_binding_changed"
    assert not any(e.kind == "device.command" for e in s.store.events_after(before))
    assert s.store.get_run(rid)["params"] == raw  # Frozen contract is never replaced.


def test_unchanged_actual_queued_binding_constructs_without_motion_or_resolution(
    queued_source, monkeypatch
):
    s, rid, raw = queued_source
    for name in ("resolve_direct_contact_contract", "direct_contact_intent"):
        monkeypatch.setattr(
            stacking, name, lambda *a, **kw: pytest.fail("saved evidence cannot be re-resolved")
        )
    before = s.store.last_event_id()
    _, params, steps = build_run_steps(s.store.get_run(rid), s.lab.bench, store=s.store)
    assert steps and params.model_dump(mode="json") == raw
    assert not any(e.kind == "device.command" for e in s.store.events_after(before))


@pytest.mark.parametrize("field,value", [("flake_chips", "chip_002"), ("materials", "hBN")])
def test_c5_raw_source_dispatch_fields_must_match_frozen_selected_gr(queued_source, field, value):
    s, rid, raw = queued_source
    changed = deepcopy(raw)
    changed[field]["gr"] = value
    with pytest.raises(WorkflowMismatch):
        stacking.StackParams.validate_saved_execution_binding(changed, saved_run=s.store.get_run(rid), store=s.store)


def pickup_phase(s, rid, raw, monkeypatch):
    c = DirectContactContract.model_validate(raw["direct_contact_contract"])
    mock_pickup_record(s, c, rid)
    # This fixture tests state admission, not the existing optical algorithm.
    monkeypatch.setattr(stacking, "_visual_pickup_proof", lambda *a: True)
    frame = {"image": c.source_observation["image"], "sha256": c.source_observation["image_sha256"]}
    before = {
        "target_contact": c.target_intent["target_contact"],
        "receiving_layers_above_contacts": [],
        "frames": [frame],
        "ledger_event_id": s.store.last_event_id(),
    }
    return c, before


@pytest.mark.parametrize("change", ["image", "geometry", "receiver"])
def test_release_step_refuses_changed_public_binding_before_any_device_access(
    queued_source, monkeypatch, change
):
    s, rid, raw = queued_source
    _, before = pickup_phase(s, rid, raw, monkeypatch)
    mutate(s, raw, change)
    monkeypatch.setattr(stacking, "_profile", lambda *a: {})
    monkeypatch.setattr(stacking, "_target_baseline", lambda *a: before)

    class NoDevice:
        def __getattr__(self, name):
            pytest.fail("changed release evidence cannot reach a device")

    ctx = SimpleNamespace(
        params=raw, store=s.store, run_id=rid, verification={"heating": {}}, gate=NoDevice()
    )
    step = stacking.DropDown(
        "stamp_01",
        ["gr"],
        {"gr": "Gr"},
        "chip_e01",
        tuple(raw["target_xy_mm"]),
        raw["melt_temp_c"],
        None,
    )
    with pytest.raises(NeedsHumanError, match="before release"):
        step.act(ctx, None)


@pytest.mark.parametrize("change", ["image", "geometry", "receiver"])
def test_pickup_step_refuses_external_drift_before_device_preconditions(
    queued_source, monkeypatch, change
):
    s, rid, raw = queued_source
    mutate(s, raw, change)
    monkeypatch.setattr(
        stacking, "_profile", lambda *a: pytest.fail("binding must be checked first")
    )
    with pytest.raises(NeedsHumanError, match="before pickup"):
        stacking.PickUpFlake("gr", "stamp_01", "Gr").preconditions(
            SimpleNamespace(params=raw, store=s.store, run_id=rid)
        )


def test_valid_postpickup_c5_and_prerelease_states_keep_frozen_contract(queued_source, monkeypatch):
    s, rid, raw = queued_source
    _, before = pickup_phase(s, rid, raw, monkeypatch)
    result = stacking._direct_contact_public_binding(
        raw, s.store, s.store.get_run(rid), stage="release", before=before
    )
    assert result["incoming"] == [{"flake_id": "gr", "material": "Gr"}] and not result["released"]
    # Other generic C5 optical/process checks have their own existing fixtures.
    monkeypatch.setattr(stacking, "_admit_saved_transfer", lambda *a: None)
    stacking.StackParams.validate_saved_execution_binding(
        raw, saved_run=s.store.get_run(rid), store=s.store
    )
    assert s.store.get_run(rid)["params"] == raw


def test_verified_pickup_cannot_be_relabelled_as_an_initial_source(queued_source, monkeypatch):
    s, rid, raw = queued_source
    pickup_phase(s, rid, raw, monkeypatch)
    s.store.upsert_sample("gr", "flake", location="chip_001", status="catalogued")
    s.store.upsert_sample("stamp_01", "stamp", props={"stack": []})
    with pytest.raises(ValueError, match="expected pickup phase"):
        stacking._direct_contact_public_binding(raw, s.store, s.store.get_run(rid))


def released_phase(s, rid, raw, before):
    c = DirectContactContract.model_validate(raw["direct_contact_contract"])
    contact = {
        "status": "pass",
        "binding": {"contract_sha256": geometry_digest(c.model_dump(mode="json"))},
    }
    endpoint = {
        "release_confirmed": True,
        "direct_contact_result": contact,
        "target_after": {"frames": before["frames"]},
    }
    payload = {
        "seq": 1,
        "name": "v1_drop_down",
        "verdict": "ok",
        "measurements": endpoint,
        "expected": {},
        "images": [f["image"] for f in before["frames"] * 2],
    }
    s.store.begin_step(rid, 1, "v1_drop_down", expected={})
    s.store.emit("step.verified", payload, run_id=rid)
    verified = {
        "event_id": s.store.last_event_id(),
        "observation_sha256": stacking._digest(stacking._verified_body(payload)),
    }
    state = s.store.get_run(rid)["meta"]["normal_transfer"]
    state["target_reference"] = before
    state["phases"]["v1_drop_down"] = {
        "complete": True,
        "endpoint": endpoint,
        "endpoint_verified": verified,
    }
    s.store.update_run_meta(rid, normal_transfer=state)
    s.store.upsert_sample(
        "device_mock",
        "device",
        location="chip_e01",
        status="stacked",
        props={
            "run_id": rid,
            "layers_top_first": ["gr"],
            "direct_contact_contract": c.model_dump(mode="json"),
            "direct_contact_result": contact,
        },
    )
    s.store.upsert_sample(
        "gr", "flake", location="chip_e01", status="stacked", props={"device": "device_mock"}
    )
    s.store.upsert_sample("stamp_01", "stamp", props={"stack": [], "film": None})
    s.store.upsert_sample("chip_e01", "chip", props={"layers_top_first": ["gr"]})
    s.store.finish_step(rid, 1, "done", measurements=endpoint)


@pytest.mark.parametrize("change", ["none", "foreign_layer", "wrong_receipt_run"])
def test_postrelease_c5_accepts_only_own_verified_transition(queued_source, monkeypatch, change):
    s, rid, raw = queued_source
    _, before = pickup_phase(s, rid, raw, monkeypatch)
    released_phase(s, rid, raw, before)
    monkeypatch.setattr(stacking, "_target_baseline", lambda *a: before)
    monkeypatch.setattr(
        stacking, "_admit_saved_transfer", lambda *a: None
    )  # Nonphysical phase fixture.
    if change == "foreign_layer":
        s.store.upsert_sample("chip_e01", "chip", props={"layers_top_first": ["gr", "foreign_hbn"]})
    elif change == "wrong_receipt_run":
        s.store.upsert_sample("device_mock", "device", props={"run_id": "another-run"})
    if change == "none":
        stacking.StackParams.validate_saved_execution_binding(
            raw, saved_run=s.store.get_run(rid), store=s.store
        )
    else:
        with pytest.raises(WorkflowMismatch):
            stacking.StackParams.validate_saved_execution_binding(
                raw, saved_run=s.store.get_run(rid), store=s.store
            )
    assert s.store.get_run(rid)["params"] == raw
