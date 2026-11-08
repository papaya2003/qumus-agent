"""Bounded source control mocks; no real JobService execution."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qumus2.records.geometry import direct_contact_intent
from tests import direct_gr_source as source
from tests.test_direct_contact_contract import REQUIREMENT
from tests.test_direct_contact_contract import public_source as observed_public_source

public_source = observed_public_source
REAL_EXTRA_REVISIT = source.extra_revisit_candidates


@pytest.fixture
def mock_jobs(services, monkeypatch):
    calls = []

    def run(s, flow, params, timeout):
        calls.append((flow, params, timeout))
        rid = s.store.create_run(flow, params)
        s.store.set_run_status(rid, "succeeded")
        return SimpleNamespace(run_id=rid, status="succeeded")

    monkeypatch.setattr("tests.test_stacking.run", run)
    monkeypatch.setattr(source, "qualified_choice", lambda *a: None)
    monkeypatch.setattr(source, "extra_revisit_candidates", lambda *a: [])
    return services, calls


def intent(s):
    return direct_contact_intent(s.store, "chip_e01", "centre_pair", REQUIREMENT)


def test_four_round_policy_is_fixed_and_stops_honestly_without_transfer(mock_jobs, tmp_path):
    s, calls = mock_jobs
    with pytest.raises(source.DirectGrSourceInfeasible, match="no qualified Gr"):
        source.build_direct_gr_source(s, intent(s), tmp_path)
    assert [
        p["chip_ids"][0] for f, p, _ in calls if f == "exfoliate_chips"
    ] == source.SOURCE_POLICY["chip_order"]
    assert len(calls) == 8 and all(f != "stack_device" for f, _, _ in calls)
    assert all(
        p["prepare_mother_tape"] == "always" and p["material"] == "Gr"
        for f, p, _ in calls
        if f == "exfoliate_chips"
    )
    manifest = json.loads((tmp_path / "source-policy-manifest.json").read_text())
    assert manifest["status"] == "infeasible_or_unfinished_no_transfer"
    assert manifest["selected"] is None


def test_one_extra_batch_per_round_has_total_twelve_job_bound(mock_jobs, tmp_path, monkeypatch):
    s, calls = mock_jobs
    monkeypatch.setattr(
        source, "extra_revisit_candidates", lambda *a: [f"candidate-{i}" for i in range(20)]
    )
    with pytest.raises(source.DirectGrSourceInfeasible):
        source.build_direct_gr_source(s, intent(s), tmp_path)
    assert len(calls) == source.SOURCE_POLICY["max_jobs"] == 12
    assert len([f for f, _, _ in calls if f == "revisit_chip"]) == 4
    assert all(p["max_revisits"] == 20 for f, p, _ in calls if f == "revisit_chip")


def test_first_qualified_choice_is_frozen_before_any_later_chip_or_transfer(
    mock_jobs, tmp_path, monkeypatch
):
    s, calls = mock_jobs
    chosen = SimpleNamespace(
        source_flake_id="chosen", model_dump=lambda **kw: {"source_flake_id": "chosen"}
    )
    monkeypatch.setattr(
        source, "qualified_choice", lambda s, chip, intent: chosen if chip == "chip_002" else None
    )
    result, manifest = source.build_direct_gr_source(s, intent(s), tmp_path)
    assert result is chosen and manifest["before_transfer"]
    assert len(calls) == 4 and all("chip_003" not in str(p) for _, p, _ in calls)
    assert manifest["status"] == "qualified_frozen_before_transfer"


def test_preserved_reserved_and_uncertain_stock_is_never_used(mock_jobs, tmp_path):
    s, calls = mock_jobs
    rid = s.store.create_run("exfoliate_chips", {})
    s.store.reserve("sample", "chip_002", rid)
    s.store.upsert_sample("chip_003", "chip", props={"location_uncertain": True})
    s.store.upsert_sample("chip_004", "chip", props={"owner_preserved": True})
    with pytest.raises(source.DirectGrSourceInfeasible):
        source.build_direct_gr_source(s, intent(s), tmp_path, preserved_chip_ids=("chip_001",))
    assert calls == []


def test_insufficient_real_tape_reduces_policy_without_virtual_refill(mock_jobs, tmp_path):
    s, calls = mock_jobs
    s.store.upsert_sample("tape", "tape", props={"roll_left_mm": 0})
    with pytest.raises(source.DirectGrSourceInfeasible):
        source.build_direct_gr_source(s, intent(s), tmp_path)
    assert calls == []


def test_wall_cap_has_no_new_round_or_budget_reset(mock_jobs, tmp_path, monkeypatch):
    s, calls = mock_jobs
    monkeypatch.setattr(source, "monotonic", lambda: 1801 if calls else 0)
    with pytest.raises(source.DirectGrSourceInfeasible, match="budget exhausted"):
        source.build_direct_gr_source(s, intent(s), tmp_path)
    assert len(calls) == 1 and calls[0][0] == "exfoliate_chips"


def test_qualification_cannot_return_a_source_after_wall_cap(mock_jobs, tmp_path, monkeypatch):
    s, calls = mock_jobs
    clock = [0]
    monkeypatch.setattr(source, "monotonic", lambda: clock[0])
    chosen = SimpleNamespace(source_flake_id="late", model_dump=lambda **kw: {})

    def qualify(*args):
        clock[0] = 1801
        return chosen

    monkeypatch.setattr(source, "qualified_choice", qualify)
    with pytest.raises(source.DirectGrSourceInfeasible, match="during prospective qualification"):
        source.build_direct_gr_source(s, intent(s), tmp_path)
    assert len(calls) == 2 and all(f != "stack_device" for f, _, _ in calls)


def test_current_rejected_and_known_outlines_are_never_revisited(public_source):
    s = public_source
    assessment = s.store.get_sample("gr")["props"]["pickup_assessment"]
    s.store.upsert_sample(
        "gr", "flake", props={"pickup_assessment": {**assessment, "clear": False}}
    )
    for i in range(25):
        s.store.upsert_sample(
            f"new-{i:02d}",
            "flake",
            location="chip_001",
            status="candidate",
            props={"material": "Gr", "area_um2": 50 + i},
        )
    s.store.upsert_sample(
        "known-small",
        "flake",
        location="chip_001",
        status="catalogued",
        props={
            "material": "Gr",
            "area_um2": 1000,
            "optical_geometry": {"outline_mm": [[0, 0], [0.001, 0], [0, 0.001]]},
        },
    )
    ids = source.extra_revisit_candidates(s, "chip_001")
    assert len(ids) == 20 and ids[0] == "new-24" and ids[-1] == "new-05"
    assert "gr" not in ids and "known-small" not in ids
    for status in ("lost", "selected", "on_stamp", "stacked"):
        s.store.upsert_sample(
            f"excluded-{status}",
            "flake",
            location="chip_001",
            status=status,
            props={"material": "Gr", "area_um2": 2000},
        )
    assert source.extra_revisit_candidates(s, "chip_001") == ids
    assert source.qualified_choice(s, "chip_001", intent(s)) is None


def test_public_complete_fixed_orientation_choice_uses_shared_predicate(public_source):
    s = public_source
    chosen = source.qualified_choice(s, "chip_001", intent(s))
    assert chosen.source_flake_id == "gr"
    s.store.upsert_sample("chip_001", "chip", props={"rotation_deg": 90})
    assert source.qualified_choice(s, "chip_001", intent(s)) is None


def test_normal_candidate_discovery_reaches_one_extra_batch_before_next_blank(
    mock_jobs, tmp_path, monkeypatch
):
    from tests import test_stacking

    s, calls = mock_jobs
    base_run = test_stacking.run
    chosen = SimpleNamespace(
        source_flake_id="scan-candidate",
        model_dump=lambda **kw: {"source_flake_id": "scan-candidate"},
    )

    def discover(s, flow, params, timeout):
        result = base_run(s, flow, params, timeout)
        if flow == "search_chip":
            s.store.upsert_sample(
                "scan-candidate",
                "flake",
                location=params["chip_id"],
                status="candidate",
                props={"material": "Gr", "area_um2": 500},
            )
        elif flow == "revisit_chip":
            assert params["flake_ids"] == ["scan-candidate"]
            s.store.upsert_sample("scan-candidate", "flake", status="catalogued")
        return result

    monkeypatch.setattr(test_stacking, "run", discover)
    monkeypatch.setattr(source, "extra_revisit_candidates", REAL_EXTRA_REVISIT)
    monkeypatch.setattr(
        source,
        "qualified_choice",
        lambda *a: chosen if any(f == "revisit_chip" for f, _, _ in calls) else None,
    )
    _, manifest = source.build_direct_gr_source(s, intent(s), tmp_path)
    assert [f for f, _, _ in calls] == ["exfoliate_chips", "search_chip", "revisit_chip"]
    assert manifest["selected"] == "scan-candidate" and len(manifest["rounds"]) == 1


def test_qualified_selection_remains_catalogued_only(public_source):
    s = public_source
    s.store.upsert_sample("gr", "flake", status="candidate")
    assert source.qualified_choice(s, "chip_001", intent(s)) is None
    assert "gr" not in source.extra_revisit_candidates(s, "chip_001")
