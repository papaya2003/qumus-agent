"""Public static optical declarations remain independent from the retired servo."""
import math
from copy import deepcopy

import pytest

from qumus2.app import build_services
from qumus2.config import load_yaml


@pytest.mark.parametrize("tilt", [-.01, 0., .01])
def test_public_tilt_bound_is_static_configured_norm_not_runtime_scene(tmp_path, tilt):
    cfg = deepcopy(load_yaml("testbench.yaml"))
    cfg["microscope"]["focus_tilt_per_mm"] = tilt
    s = build_services(":memory:", seed=1, image_dir=tmp_path, testbench_config=cfg)
    declaration = s.jobs.context_factory(s.store.create_run("axis_declaration", {}), {}, s.project_id).gate.microscope("microscope").focus_axis_calibration(20)
    assert declaration["lateral_focus_slope_norm_bound_mm_per_mm"] == pytest.approx(math.sqrt(2)*abs(tilt))
    assert declaration["source_refraction_projection_norm_bound_mm_per_mm"] == pytest.approx(
        math.hypot(*cfg["stamp"]["dynamics"]["refraction_xy_per_gap_mm"]))
    assert declaration["apex_drift_clip_per_axis_mm"] == cfg["stamp"]["dynamics"]["maximum_drift_mm"]
    s.store.close()
