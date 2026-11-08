"""Public axis duration remains separate from the retired centering controller."""
import pytest
from test_focus_evidence import focus_context  # noqa: F401

from qumus2.devices.base import StampManipulator


def test_public_sim_axis_duration_matches_requested_clock_not_arrival(focus_context, monkeypatch):  # noqa: F811
    s, ctx, _ = focus_context
    arm = ctx.gate.manipulator("transfer_arm")
    raw = s.lab.devices.get("transfer_arm")
    start = arm.get_positions()["x"]
    expected = .0062/raw.speed
    assert arm.axis_motion_duration_bound("x", start, start-.0062) == pytest.approx(expected)
    assert StampManipulator.axis_motion_duration_bound(raw, "x", start, start-.0062) is None
    monkeypatch.setattr(raw, "axis_motion_duration_bound", lambda *a: float("nan"))
    assert arm.axis_motion_duration_bound("x", start, start-.0062) is None
