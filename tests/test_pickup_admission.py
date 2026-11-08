"""Observed neighbourhood admission, shared by ordinary selection and execution."""
import asyncio
from unittest.mock import patch

import pytest
from test_common_pickup_contract import observed_pair

from qumus2.agents.tooling import ToolContext
from qumus2.agents.tools.picking import (
    SubmitSelectionArgs,
    candidates_by_role,
    submit_flake_selection,
)
from qumus2.core.errors import PreconditionFailed
from qumus2.records.geometry import current_pickup_evidence, ranked_pickup_combinations
from qumus2.records.materials import pickup_admission_error


@pytest.fixture
def observed(services):
    services.store.upsert_sample("chip_001", "chip", location="mid_chip_1")
    observed_pair(services)
    for fid in ("top", "lower"):
        services.store.upsert_sample(fid, "flake", props={"rel_corner_mm": [.1, .1]})
    return services


def change_evidence(s, value):
    flake = s.store.get_sample("lower")
    assessment = {**flake["props"]["pickup_assessment"], "clear": value}
    s.store.upsert_sample("lower", "flake", props={"pickup_assessment": assessment})


@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_current_unclear_or_nonboolean_evidence_remains_visible_but_not_admitted(observed, value):
    s = observed
    change_evidence(s, value)
    flake = s.store.get_sample("lower")
    assert current_pickup_evidence(s.store, flake) is not None
    assert "fresh inspection or choose another" in pickup_admission_error(s.store, flake)
    assert ranked_pickup_combinations(s.store, [["top"], ["lower"]]) == []


@pytest.mark.parametrize("reason", ["missing", "stale", "unclear", "unknown", "other_flake", "missing_flake_id"])
def test_job_refusal_precedes_run_reservations_and_enqueue(observed, reason):
    s = observed
    if reason == "missing":
        s.store.upsert_sample("lower", "flake", props={"pickup_assessment": None})
    elif reason == "stale":
        s.store.create_run("exfoliate_chips", {"chip_ids": ["chip_001"], "material": "Gr"})
    elif reason in ("other_flake", "missing_flake_id"):
        assessment = dict(s.store.get_sample("lower")["props"]["pickup_assessment"])
        if reason == "other_flake":
            assessment["flake_id"] = "top"  # Same chip and same successful search.
        else:
            assessment.pop("flake_id")
        s.store.upsert_sample("lower", "flake", props={"pickup_assessment": assessment})
        assert current_pickup_evidence(s.store, s.store.get_sample("lower")) is None
    else:
        change_evidence(s, False if reason == "unclear" else None)
    before_runs = s.store.list_runs()
    before_samples = s.store.list_samples()
    before_reservations = {kind: s.store.reservations(kind) for kind in ("sample", "slot", "device")}
    before_event = s.store.last_event_id()
    with patch.object(s.jobs, "enqueue") as enqueue:
        with pytest.raises(PreconditionFailed, match="pickup-neighbourhood") as exc:
            s.jobs.submit("stack_device", {"flake_ids": ["lower"], "target_chip_id": "chip_e01"})
        assert exc.value.details["reason"] == "pickup_neighbourhood_not_clear"
        enqueue.assert_not_called()
    assert s.store.list_runs() == before_runs
    assert s.store.list_samples() == before_samples
    assert {kind: s.store.reservations(kind) for kind in before_reservations} == before_reservations
    assert not [e for e in s.store.events_after(before_event) if e.kind == "device.command"]


def test_clear_current_evidence_allows_normal_job_resolution(observed):
    s = observed
    assert pickup_admission_error(s.store, s.store.get_sample("lower")) is None
    with patch.object(s.jobs, "enqueue") as enqueue:
        rid = s.jobs.submit("stack_device", {"flake_ids": ["lower"], "target_chip_id": "chip_e01"})
        enqueue.assert_called_once_with(rid)
    assert s.store.get_run(rid)["params"]["flake_ids"] == ["lower"]
    assert s.store.reservations("sample")["chip_001"]["run_id"] == rid


@pytest.mark.parametrize("clear", [True, False, None, "stale"])
def test_picker_and_ranking_use_same_admission_without_mutating_rejected_selection(observed, monkeypatch, clear):
    s = observed
    if clear == "stale":
        s.store.create_run("exfoliate_chips", {"chip_ids": ["chip_001"], "material": "Gr"})
    else:
        change_evidence(s, clear)
    monkeypatch.setattr("qumus2.agents.tools.picking.require_images", lambda: False)
    s.store.update_project_state(s.project_id, viewed_flake_images=["lower"], device_plan={"flake_roles": [
        {"role": "channel", "material": "Gr", "count": 1}]})
    ctx = ToolContext(s.store, s.jobs, "picker", s.project_id)
    args = SubmitSelectionArgs(selected_by_role={"channel": ["lower"]}, reasons="Observed fixture",
        assessments=[{"flake_id": "lower", "feasible": True, "geometry_fitness": 80,
            "visible_cleanliness": 80, "continuity_uniformity": 80, "evidence_confidence": 80,
            "inventory_tiebreaker": 50, "overall_score": 80, "reason": "Observed fixture",
            "surface_residue": "clean", "residue_evidence": "Explicit optical fixture"}])
    before = s.store.get_project(s.project_id)["state"]
    result = asyncio.run(submit_flake_selection(args, ctx))
    assert [f["id"] for f in candidates_by_role(ctx)["channel"]] == (["lower"] if clear is True else [])
    assert bool(ranked_pickup_combinations(s.store, [["top"], ["lower"]])) is (clear is True)
    if clear is True:
        assert result.status == "ok", result.summary
        assert s.store.get_project(s.project_id)["state"]["selected_flakes"] == ["lower"]
    else:
        assert result.status == "error"
        assert "fresh inspection or choose another" in result.summary
        assert s.store.get_project(s.project_id)["state"] == before


@pytest.mark.parametrize("clear", [True, False, 1, None])
def test_reactor_assessment_uses_same_strict_admission(observed, clear):
    from qumus2.records.reactor_requirements import ReactorRequirements, input_errors

    change_evidence(observed, clear)
    errors = input_errors(observed.store, ReactorRequirements(reactive_material="Gr", metal="Pd"),
                          ["top", "lower"], require_assessment=True)
    assert bool(errors) is (clear is not True)
    if errors:
        assert "pickup-neighbourhood" in errors[0]
