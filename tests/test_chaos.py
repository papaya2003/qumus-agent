"""The chaos series (`qumus2 chaos`): fault coverage, "did the fault fire",
expected behaviour, records checked on a stop."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from qumus2.testbench import chaos
from qumus2.testbench.faults import FAULTS


def test_every_fault_is_in_the_series_or_says_why_not():
    names = [c.name for c in chaos.FAULT_CASES]
    assert len(names) == len(set(names)) == 20
    assert set(names) | set(chaos.NOT_IN_CHAOS) == set(FAULTS)
    assert all(c.expect in ("recover", "stop", "either") and c.why for c in chaos.FAULT_CASES)


def test_the_first_fault_cycles_through_all_faults_by_seed():
    base = chaos.ChaosRun(seed=0, flow_calls=[100, 200, 900, 1500, 3000])
    import random

    seen = {chaos._schedule(seed, random.Random(seed), 1, base)[0][1].name
            for seed in range(len(chaos.FAULT_CASES))}
    assert seen == set(chaos.CASES)
    # a fault that only matters in one flow is injected inside that flow
    tape = next(s for s in range(40) if chaos.FAULT_CASES[s % 20].name == "tape_roll_empty")
    call = chaos._schedule(tape, random.Random(tape), 1, base)[0][0]
    assert 1 <= call <= 100


def test_a_stop_that_needs_a_human_is_judged_by_its_reason():
    case = chaos.CASES["tape_roll_empty"]
    assert chaos._handled("honest_stop", "exfoliator: tape roll empty (0 mm left)", case, {})
    assert not chaos._handled("honest_stop", "chip_001 not found", case, {})
    assert not chaos._handled("ok", "", case, {})  # cannot finish without tape
    assert not chaos._handled("bad_stop", "tape roll empty", case, {})
    offline = chaos.CASES["device_offline"]
    assert chaos._handled("honest_stop", "stamp_heater offline", offline,
                          {"device": "stamp_heater"})


def test_tape_running_out_fires_and_stops_honestly():
    seed = next(s for s in range(40) if chaos.FAULT_CASES[s % 20].name == "tape_roll_empty")
    r = chaos.run_one(seed)
    (inj,) = r.injected
    assert inj["fired"] and inj["handled"], (r.flows, r.problems)
    assert r.outcome == "honest_stop" and not r.collisions
    md = chaos.render_markdown([r], 1)
    assert "| tape_roll_empty | stop | 1 | 1 | 1 |" in md


def test_a_placement_given_up_leaves_the_ledger_true(services):
    """Every placement off by a few mm: the step gives up, and the ledger says
    where the chip really is (or that it does not know), not the old slot."""
    s = services
    s.lab.faults.inject("big_error_prob", {"value": 1.0})
    st = s.jobs.wait(s.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_001"}), 120)
    assert st.status != "succeeded"
    chip = s.store.get_sample("chip_001")
    o = s.lab.world.objects["chip_001"]
    if chip["props"].get("location_uncertain"):
        assert chip["props"]["last_seen"]["at"] != chip["location"]
    else:  # off target at the destination of the move that gave up
        seen = chip["props"]["last_seen"]
        assert chip["location"] == seen["at"] != "left_chip_1" and seen["offset_mm"] > 0
        assert abs(o.pose.x - seen["pose"]["x"]) < 0.5 and abs(o.pose.y - seen["pose"]["y"]) < 0.5
    assert chip["status"] == "bare"


@pytest.fixture
def torn_grader(monkeypatch):
    """Controlled collaborators exercise real orchestration/grading, with no physics job.

    Physical fault consumption/damage/ledger coverage belongs to the three
    qualified torn-film cases in test_stacking.py and requires separate execution.
    """
    case = chaos.CASES["stamp_torn"]
    faults = SimpleNamespace(before_call=lambda device: None, inject=Mock())
    s = SimpleNamespace(clock=SimpleNamespace(now=lambda: 0),
        store=SimpleNamespace(last_event_id=lambda: 1),
        lab=SimpleNamespace(faults=faults, locator=SimpleNamespace(last_boards=[]),
            world=SimpleNamespace(tape=SimpleNamespace(roll_left_mm=1), lock=nullcontext(), collisions=[])))
    s.close_local = AsyncMock(return_value={"closed": True, "alive_workers": [], "stop_pending": False,
        "safe_to_close_resources": True, "unsettled_jobs": [], "unconfirmed_energy_jobs": [], "evidence_pending": False})
    monkeypatch.setattr(chaos, "_services", Mock(return_value=s))
    monkeypatch.setattr(chaos, "_schedule", lambda *args: [(1, case, {})])
    monkeypatch.setattr(chaos, "_cost", lambda *args: {"tape_mm": 0, "stamps": 0, "sim_min": 0})
    agrees = Mock()
    fired = Mock()
    monkeypatch.setattr(chaos, "_world_agrees", agrees)
    monkeypatch.setattr(chaos, "_fired", fired)
    current = {}

    def task(services, result, timeout):
        with patch.object(chaos.threading, "current_thread", return_value=SimpleNamespace(name="job-grader-unit")):
            faults.before_call("transfer_arm")
        result.flows.append({"flow": "stack_device", "status": current["status"], "note": current["note"]})
        return current["status"]

    monkeypatch.setattr(chaos, "_run_task", task)

    def grade(note, *, status="waiting_human", did_fire=True, problems=()):
        current.update(note=note, status=status)
        agrees.return_value = list(problems)
        fired.return_value = did_fire
        faults.inject.reset_mock()
        result = chaos.run_one(13, base=chaos.ChaosRun(seed=13))
        faults.inject.assert_called_once_with("stamp_torn", {})
        assert result.calls == 1 and len(result.injected) == 1
        assert result.injected[0]["expect"] == "stop"
        return result

    return grade


@pytest.mark.parametrize("note", [
    "Observed film damage; partial transfer remains unknown. Human review is required.",
    "unclear observation: Observed film damage; partial transfer remains unknown. Human review is required; no automatic replacement.",
    "Observed torn film; partial transfer remains unknown. Human review is required."])
def test_stamp_torn_requires_an_observed_damage_stop(torn_grader, note):
    assert chaos.CASES["stamp_torn"].expect == "stop"
    result = torn_grader(note)
    assert result.outcome == "honest_stop" and result.injected[0]["handled"]
    assert not result.problems and not result.collisions
    assert chaos.seeds_for("stamp_torn", 3) == [13, 33, 53]
    assert all(chaos.FAULT_CASES[x % 20].name == "stamp_torn"
               for x in chaos.seeds_for("stamp_torn", 2, first=40))


@pytest.mark.parametrize("note", [
    "Selected source pixels are not identifiable: specimen_not_present",
    "stamp_heater offline", "stamp_01 was not found", "tape roll empty",
    "film is torn", "No observed film damage; partial transfer remains unknown. Human review is required."])
def test_stamp_torn_wrong_stop_reasons_are_not_credited(torn_grader, note):
    result = torn_grader(note)
    assert result.outcome == "honest_stop" and result.injected[0]["fired"]
    assert not result.injected[0]["handled"]


def test_stamp_torn_grader_requires_the_fault_to_fire(torn_grader):
    result = torn_grader("Observed film damage; partial transfer remains unknown. Human review is required.",
                        did_fire=False)
    assert result.outcome == "no_fault"
    assert not result.injected[0]["fired"] and not result.injected[0]["handled"]


@pytest.mark.parametrize("status,problems,outcome", [
    ("waiting_human", ("ledger disagreement",), "bad_stop"),
    ("succeeded", ("ledger disagreement",), "false_ok"),
    ("failed", (), "error"), ("succeeded", (), "ok")])
def test_stamp_torn_grader_preserves_ledger_bug_and_error_classification(torn_grader, status, problems, outcome):
    result = torn_grader("Observed film damage; partial transfer remains unknown. Human review is required.",
                        status=status, problems=problems)
    assert result.outcome == outcome and not result.injected[0]["handled"]


def test_stamp_torn_saved_expectation_is_preserved_in_reports(torn_grader):
    current = torn_grader("Observed film damage; partial transfer remains unknown. Human review is required.")
    assert "| stamp_torn | stop | 1 | 1 | 1 |" in chaos.render_markdown([current], 1)
    # Synthetic historical receipt: reporting must not relabel its saved expectation.
    saved = chaos.ChaosRun(seed=13, outcome="honest_stop",
        injected=[{"fault": "stamp_torn", "params": {}, "expect": "recover", "call": 1,
                   "fired": True, "handled": False}], flows=[{"flow": "stack_device", "status": "waiting_human", "note": "saved stop"}])
    assert "| stamp_torn | recover 1 | 1 | 1 | 0 |" in chaos.render_markdown([saved], 1)
    assert saved.injected[0]["expect"] == "recover" and not saved.injected[0]["handled"]


def test_chaos_faulted_task_disposes_owned_services_after_physical_grading(torn_grader, monkeypatch):
    s = chaos._services.return_value
    order = []
    monkeypatch.setattr(chaos, "_world_agrees", lambda *args, **kwargs: order.append("graded") or [])
    async def close():
        order.append("disposed")
        return {"closed": True, "alive_workers": [], "stop_pending": False, "safe_to_close_resources": True,
                "unsettled_jobs": [], "unconfirmed_energy_jobs": [], "evidence_pending": False}
    s.close_local.side_effect = close
    result = torn_grader("Observed film damage; partial transfer remains unknown. Human review is required.")
    assert result.outcome == "honest_stop" and result.cleanup["closed"]
    assert order == ["graded", "disposed"]
    s.close_local.assert_awaited_once()


def test_chaos_unfinished_disposal_preserves_original_error_and_cannot_claim_handled(torn_grader):
    s = chaos._services.return_value
    s.close_local.return_value = {"closed": False, "alive_workers": ["job-0"], "stop_pending": False,
                                  "safe_to_close_resources": False}
    result = torn_grader("original fault note", status="failed")
    assert result.outcome == "error" and not result.injected[0]["handled"]
    assert any("stack_device ended failed" in problem for problem in result.problems)
    assert any("shutdown remains unfinished" in problem for problem in result.problems)
    assert result.cleanup["alive_workers"] == ["job-0"]


def test_chaos_baseline_disposes_and_does_not_start_faulted_work_when_unsettled(torn_grader, monkeypatch):
    s = chaos._services.return_value
    s.close_local.return_value = {"closed": False, "alive_workers": ["job-0"], "stop_pending": False,
                                  "safe_to_close_resources": False}
    monkeypatch.setattr(chaos, "_run_task", lambda *args: "succeeded")
    result = chaos.run_one(13)
    assert chaos._services.call_count == 1  # Baseline only; no overlapping second physical world.
    s.close_local.assert_awaited_once()
    assert result.outcome == "error" and result.cleanup["baseline"]["alive_workers"] == ["job-0"]
    assert "Faulted task not started" in result.problems[-1]


def test_chaos_retained_energy_hold_is_not_clean_disposal_or_handled_success(torn_grader):
    s = chaos._services.return_value
    s.close_local.return_value = {"closed": True, "alive_workers": [], "stop_pending": False, "stop_error": None,
        "safe_to_close_resources": False, "unsettled_jobs": [], "unconfirmed_energy_jobs": ["fixture_job"], "evidence_pending": False}
    result = torn_grader("Observed film damage; partial transfer remains unknown. Human review is required.")
    assert result.outcome == "error" and not result.injected[0]["handled"]
    assert result.cleanup["outcome_before_cleanup"] == "honest_stop"
    assert result.cleanup["unconfirmed_energy_jobs"] == ["fixture_job"]


def test_chaos_partial_setup_carries_unsafe_cleanup_and_never_starts_second_world(monkeypatch):
    from qumus2 import app
    from qumus2.testbench import render

    services = SimpleNamespace(close_local=AsyncMock(return_value={"closed": False, "safe_to_close_resources": False}))
    factory = Mock(return_value=services)
    monkeypatch.setattr(app, "build_services", factory)
    monkeypatch.setattr(render, "ensure_diagnostic_images", Mock(side_effect=RuntimeError("Original setup error")))
    result = chaos.run_one(14)
    factory.assert_called_once()
    services.close_local.assert_awaited_once()
    assert result.outcome == "error" and not result.cleanup["closed"]
    assert any("Original setup error" in problem for problem in result.problems)
    assert "Faulted task not started" in result.problems[-1]
