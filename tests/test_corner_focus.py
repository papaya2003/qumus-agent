"""FindChipCorner focuses the candidate edge, not an unobservable inboard field."""

import pytest

from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.core.errors import SafetyViolation, StopRequested
from qumus2.core.types import Pose
from qumus2.flows.library.primitives import autofocus, microscope_frame
from qumus2.flows.library.search import FindChipCorner, _move_stage
from qumus2.flows.step import Verdict


@pytest.fixture
def corner(tmp_path):
    s = build_services(
        ":memory:", seed=8, testbench_config=simulation_profile(), image_dir=tmp_path
    )
    rid = s.store.create_run("corner_focus_fixture", {}, project_id=s.project_id)
    ctx = s.jobs.context_factory(rid, {}, s.project_id)
    # Controlled flat chip fixture; production physics, detector and focus settings stay unchanged.
    station = ctx.bench["stations"]["transfer_stage"]
    chip = s.lab.world.objects["chip_001"]
    chip.pose = Pose(x=station["x"], y=station["y"])
    chip.on = "transfer_stage"
    chip.held_by = None
    chip.flakes = []
    measured = Pose(x=station["x"] + 0.3, y=station["y"] + 0.2)
    s.store.upsert_sample(
        "chip_001",
        "chip",
        location="transfer_stage",
        props={"pose_on_stage": measured.model_dump()},
    )
    scope = ctx.gate.microscope("microscope")
    scope.set_objective(10)
    scope.set_focus(1.1)
    step = FindChipCorner("chip_001")
    step.preconditions(ctx)
    return s, ctx, step


def last_focus(s, ctx):
    return s.store.get_run(ctx.run_id)["meta"]["last_focus_observation"]


def test_visible_blurred_corner_focuses_at_the_actual_candidate_pose(corner):
    s, ctx, step = corner
    nx, ny = step.nominal(ctx)
    scope = ctx.gate.microscope("microscope")
    scope.set_focus(1.1)
    _move_stage(ctx, nx, ny)
    blurred = microscope_frame(ctx, phase="visible_blurred_corner_before_focus")
    assert blurred.meta["chip_corners"]  # Test-fixture visibility only; never a flow input.
    assert (
        ctx.analyzer.plane_sharpness(blurred, "chip")
        < ctx.verification["focus"]["min_sharpness"][10]
    )
    step.act(ctx, None)
    observation = step.verify(ctx)
    focus = last_focus(s, ctx)
    assert focus["pixel_score"] > 0 and focus["peak_observable"]
    assert focus["focus_interval_mm"] is not None
    assert focus["focus_plane"] == "chip"
    assert observation.verdict == Verdict.OK
    # Every sweep sample is bound to the existing corner candidate, not the old inboard offset.
    for curve in focus["sampled_passes"]:
        for sample in curve["samples"]:
            pose = sample["stage_kinematics"]["sample_pose"]
            assert (pose["x"], pose["y"]) == pytest.approx((nx, ny))


def test_truly_disjoint_blank_field_cannot_supply_a_focus_peak(corner):
    s, ctx, step = corner
    nx, ny = step.nominal(ctx)
    _move_stage(ctx, nx-12, ny)  # Whole 10 mm chip is outside the calibrated 10x FOV.
    autofocus(ctx, 10)
    observation = last_focus(s, ctx)
    assert observation["pixel_score"] == 0
    assert not observation["peak_observable"] and observation["focus_interval_mm"] is None
    assert observation["focus_z"] == pytest.approx(1.1)


def test_normal_focused_corner_still_verifies(corner):
    s, ctx, step = corner
    ctx.gate.microscope("microscope").set_focus(-0.1)
    step.act(ctx, None)
    assert step.verify(ctx).verdict == Verdict.OK
    assert step.max_corrections == 4


def test_all_existing_correction_candidates_are_focused_in_place(corner, monkeypatch):
    _, ctx, step = corner
    observed = []

    def focus_at_candidate(context, objective):
        observed.append((objective, context.gate.stage("transfer_stage").get_sample_pose()))

    monkeypatch.setattr("qumus2.flows.library.search.autofocus", focus_at_candidate)
    nx, ny = step.nominal(ctx)
    expected = [(0.6, 0), (0, 0.6), (-0.6, 0), (0, -0.6)]
    for offset in expected:
        correction = step.correct(ctx, None)
        assert correction.params["offset"] == offset
        step.act(ctx, correction)
        objective, pose = observed[-1]
        assert objective == ctx.bench["microscope"]["scan_objective"]
        assert (pose.x, pose.y) == pytest.approx((nx + offset[0], ny + offset[1]))
    assert step.max_corrections == 4
    assert step.correct(ctx, None).params["offset"] == (1.2, 0)


@pytest.mark.parametrize("residual", [(0, .463), (0, -.463), (.463, 0), (-.463, 0), (.088, .463)])
def test_same_finite_gate_search_handles_both_signed_axis_residuals(corner, residual):
    s, ctx, step = corner
    station = ctx.bench["stations"]["transfer_stage"]
    # Declared navigation fixture: equal opposite public camera residuals.
    # No source flakes, tolerance changes or model-derived controller inputs.
    measured = Pose(x=station["x"]-residual[0], y=station["y"]-residual[1])
    s.store.upsert_sample("chip_001", "chip", props={"pose_on_stage": measured.model_dump()})
    nominal = step.nominal(ctx)
    correction = None
    observation = None
    for attempt in range(step.max_corrections+1):
        step.act(ctx, correction)
        observation = step.verify(ctx)
        if observation.verdict == Verdict.OK:
            break
        assert observation.verdict == Verdict.CORRECTABLE
        if attempt < step.max_corrections:
            correction = step.correct(ctx, observation)
    assert observation.verdict == Verdict.OK
    assert last_focus(s, ctx)["peak_observable"]
    assert step.max_corrections == 4 and step.nominal(ctx) == nominal
    if abs(residual[1]) > .43:
        assert attempt > 0  # Initially outside the 10x frame; a real signed probe is necessary.


def test_stop_prevents_stage_travel_and_autofocus(corner, monkeypatch):
    _, ctx, step = corner
    before = ctx.gate.stage("transfer_stage").get_sample_pose()
    monkeypatch.setattr(
        "qumus2.flows.library.search.autofocus",
        lambda *_: pytest.fail("Stop must precede autofocus"),
    )
    ctx.cancel.set()
    with pytest.raises(StopRequested):
        step.act(ctx, None)
    assert ctx.gate.stage("transfer_stage").get_sample_pose() == before


def test_invalid_candidate_keeps_stage_limits_authoritative(corner, monkeypatch):
    _, ctx, step = corner
    before = ctx.gate.stage("transfer_stage").get_sample_pose()
    step._nominal = (10000.0, 10000.0)
    monkeypatch.setattr(
        "qumus2.flows.library.search.autofocus",
        lambda *_: pytest.fail("Limit refusal must precede autofocus"),
    )
    with pytest.raises(SafetyViolation):
        step.act(ctx, None)
    assert ctx.gate.stage("transfer_stage").get_sample_pose() == before
