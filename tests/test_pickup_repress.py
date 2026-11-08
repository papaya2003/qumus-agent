"""Jialei 2026-11-03 (#109): a design pickup that left its whole source on the
chip is pressed again, at most pickup.max_represses times, each press its own
Step and phase. Anything short of an observed whole retained source parks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from qumus2.config import load_yaml
from qumus2.flows.library.stacking import PickUpFlake, StackParams, pickup_name, pickup_press_names
from qumus2.flows.step import Observation, StepOutcome, StepStatus, Verdict
from tests.test_design_stacking import flow_fixture


def _miss(**change):
    m = {"method": "source_disappearance", "flake_on_chip": True, "source_detection_count": 1,
         "transfer_confirmed": False, "coverage": {"covered": True, "method": "design_contact_region"},
         "source_presence": {"source_id": "f", "quality": .9}, "source_frames": [{"image": "a.png"}]}
    m.update(change)
    return m


def _outcome(step, measurements, verdict=Verdict.UNCLEAR, error=None):
    return StepOutcome(StepStatus.NEEDS_AGENT, step.name, 1, 0,
                       [Observation(verdict, measurements, {})], message="miss", error=error)


@pytest.fixture
def planned(tmp_path):
    store, project, _, design, flow = flow_fixture(tmp_path)
    params = flow.resolve_params(StackParams(flake_ids=list(design.pickup_order), target_chip_id="target",
                                             device_design=design), store)
    run_id = store.create_run("stack_device", params.model_dump(mode="json"), project_id=project)
    ctx = SimpleNamespace(store=store, run_id=run_id, verification=load_yaml("verification.yaml"))
    return flow, params, ctx


def _pickup(flow, params, fid):
    return next(s for s in flow.build_steps(params) if isinstance(s, PickUpFlake) and s.name == pickup_name(params, fid))


def test_names_of_every_planned_press():
    assert pickup_press_names("f", 0) == ["v1_pick_up_f"]
    assert pickup_press_names("f", 2) == ["v1_pick_up_f", "v1_pick_up_f_press2", "v1_pick_up_f_press3"]
    assert pickup_name({"pickup_represses": {"f": 1}}, "f") == "v1_pick_up_f_press2"
    assert pickup_name({}, "f") == "v1_pick_up_f"


def test_an_observed_miss_is_pressed_again_until_the_cap_then_parks(planned):
    flow, params, ctx = planned
    assert ctx.verification["pickup"]["max_represses"] == 2
    for fid in params.flake_ids:
        current = params
        for press in (2, 3):
            steps = flow.build_steps(current)
            step = _pickup(flow, current, fid)
            seq = [s.name for s in steps].index(step.name)
            plan = flow.on_step_failed(current, step, _outcome(step, _miss()), ctx)
            assert plan is not None and plan.params.pickup_represses[fid] == press-1
            new = flow.build_steps(plan.params)
            # the job service's own replan rule: keep every step up to the
            # failed one and add steps after it
            assert [(s.name, s.safe_to_resume) for s in new[:seq+1]] == [(s.name, s.safe_to_resume) for s in steps[:seq+1]]
            assert [s.name for s in new[seq+1:seq+4]] == [
                f"reheat_{fid}_press{press}", f"v1_initialize_{fid}_press{press}", f"v1_pick_up_{fid}_press{press}"]
            # the steps after the new press are the original tail
            assert [s.name for s in new[seq+4:]] == [s.name for s in steps[seq+1:]]
            current = plan.params
        step = _pickup(flow, current, fid)
        assert flow.on_step_failed(current, step, _outcome(step, _miss()), ctx) is None
    repress = [e for e in ctx.store.events_after(0, limit=500, run_id=ctx.run_id) if e.kind == "stack.pickup_repress"]
    assert [(e.payload["flake_id"], e.payload["press"]) for e in repress] == [
        (fid, press) for fid in params.flake_ids for press in (2, 3)]


def test_a_replanned_snapshot_keeps_every_submitted_value_exactly(planned):
    """a518 seed 8 forced miss: the submission (python-mode dump) kept the
    resolved park pose as ints (150) while the replan's JSON-mode dump made
    them floats (150.0), so the frozen-snapshot digest refused the re-press."""
    import json

    flow, params, ctx = planned
    submitted = json.loads(json.dumps(params.model_dump()))  # as jobs.submit stores it
    step = _pickup(flow, params, params.flake_ids[0])
    plan = flow.on_step_failed(params, step, _outcome(step, _miss()), ctx)
    replanned = plan.params.model_dump(mode="json")
    assert replanned.pop("pickup_represses") == {params.flake_ids[0]: 1}
    submitted.pop("pickup_represses")
    assert json.dumps(replanned, sort_keys=True) == json.dumps(submitted, sort_keys=True)


@pytest.mark.parametrize("change", [
    {"source_presence": None},                    # the retained source was not re-identified
    {"flake_on_chip": False},                     # nothing left to press
    {"source_detection_count": 2},
    {"transfer_confirmed": True},
    {"film_intact": False},                       # a torn film is always a human review
    {"coverage": {"covered": False}},             # the stroke itself did not cover
    {"method": "other"},
])
def test_anything_short_of_a_whole_retained_source_parks(planned, change):
    flow, params, ctx = planned
    step = _pickup(flow, params, params.flake_ids[-1])
    assert flow.on_step_failed(params, step, _outcome(step, _miss(**change)), ctx) is None


def test_errors_stale_presses_and_plain_stacks_park(planned):
    flow, params, ctx = planned
    fid = params.flake_ids[0]
    step = _pickup(flow, params, fid)
    assert flow.on_step_failed(params, step, _outcome(step, _miss(), error={"category": "needs_human"}), ctx) is None
    assert flow.on_step_failed(params, step, _outcome(step, _miss(), verdict=Verdict.CORRECTABLE), ctx) is None
    later = params.model_copy(update={"pickup_represses": {fid: 1}})
    assert flow.on_step_failed(later, step, _outcome(step, _miss()), ctx) is None  # not the planned last press
    plain = params.model_copy(update={"device_design": None})
    assert flow.on_step_failed(plain, step, _outcome(step, _miss()), ctx) is None
