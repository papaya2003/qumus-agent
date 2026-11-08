"""Resolved target geometry and legacy channel rules must not contradict."""
import pytest

from qumus2.agents.tools.design import SubmitDesignArgs, validate_layout


def layout(width, height):
    return SubmitDesignArgs(flake_ids=["flake"], shapes=[
        {"layer": "channel", "x_um": 0, "y_um": 0, "w_um": width, "h_um": height},
        {"layer": "contact_metal", "x_um": 0, "y_um": 0, "w_um": 2, "h_um": 4},
    ])


def state(footprint=None, channel=""):
    return {"selected_flakes": ["flake"], "device_plan": {
        "flake_roles": [{"role": "channel"}], "contact_scheme": "existing contacts",
        "channel": channel, "required_footprint": footprint,
    }}


@pytest.mark.parametrize("size", [(33, 4), (4, 33), (34, 4.3)])
def test_resolved_bridge_footprint_overrides_generic_qh_default(size):
    s = state({"width_um": 33, "height_um": 4, "source": "simulated_design"},
              channel="legacy note: 5 x 10 um minimum")
    assert validate_layout(s, layout(*size)) == []


@pytest.mark.parametrize("size", [(32.9, 4), (33, 3.9), (5, 10)])
def test_resolved_bridge_dimensions_are_still_enforced(size):
    errs = validate_layout(state({"width_um": 33, "height_um": 4}), layout(*size))
    assert any("below the minimum 4.0x33.0" in e for e in errs)


def test_unresolved_legacy_plan_keeps_existing_channel_rule():
    assert validate_layout(state(), layout(33, 4))
    assert validate_layout(state(), layout(5, 10)) == []
    assert validate_layout(state(channel="6 x 12 um minimum"), layout(5, 10))
    assert validate_layout(state(channel="6 x 12 um minimum"), layout(6, 12)) == []
