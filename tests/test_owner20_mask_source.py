"""Owner original20 geometry: exact pixels, public anchor and100 classification."""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from test_recognition_provenance import outlined_evidence

from qumus2.core.optical_profiles import V1_MATERIAL_CAPTURE_PROFILE
from qumus2.core.regions import as_region
from qumus2.core.types import Pose
from qumus2.flows.library import search
from qumus2.perception.rgb_material import algorithm_sha256, component_geometry
from qumus2.records.recognition import validate_rgb_capture_binding, validate_rgb_outline
from qumus2.runtime.store import RunStore


def source20(tmp_path):
    original = outlined_evidence(tmp_path, hole=True, spacing=(.000182, .000182))
    chain = original["recognition_chain"]
    chain["algorithm_sha256"] = algorithm_sha256()
    chain["derivation"]["unsupported_reason"] = None
    chain["calibration"]["material_capture_profile"] = V1_MATERIAL_CAPTURE_PROFILE
    with Image.open(chain["geometry_mask"]["path"]) as image:
        geometry = component_geometry(np.asarray(image) != 0, chain["calibration"])
    store = RunStore(tmp_path/"run.db", bench="sim")
    store.upsert_sample("chip", "chip", props={"rotation_deg": 31.})
    store.upsert_sample("flake", "flake", location="chip", props={"material": "Gr"})
    job = store.create_run("search_chip", {"chip_id": "chip"})
    store.emit("step.verified", {"name": "measure_chip_rotation", "verdict": "ok",
        "measurements": {"rotation_deg": 31.}}, run_id=job)
    readback = store.emit("microscope.source_stage_readback", {"version": 1,
        "source_chip_id": "chip", "flake_id": "flake", "objective": 20,
        "coarse_xy": [2., 3.], "fine_xy": [.1, -.2], "stage_xy": [2.1, 2.8]}, run_id=job)
    capture = store.emit("device.command", {"device": "microscope", "command": "capture"}, run_id=job)
    frame = SimpleNamespace(meta={"objective": 20}, timestamp=7., saved_path=lambda: chain["image"]["path"])
    detection = SimpleNamespace(material="Gr", thickness_label="monolayer", dx_mm=geometry["dx_mm"],
        dy_mm=geometry["dy_mm"], meta={"recognition_chain": chain})
    ctx = SimpleNamespace(store=store, run_id=job)
    result = search._rgb_revisit_evidence(ctx, SimpleNamespace(persist_detection_masks=lambda *a: chain),
        frame, detection, "chip", "flake", capture, capture_stage_xy=[2.1, 2.8],
        stage_readback_event_id=readback)
    store.upsert_sample("flake", "flake", location="chip", props={"material": "Gr",
        "stage_xy": result["source_anchor20"]["stage_xy"]})
    store.set_run_status(job, "succeeded")
    return store, result, geometry


def test_original20_pixel_union_retains_hole_pitch_and_public_centroid(tmp_path):
    store, result, geometry = source20(tmp_path)
    try:
        observation = result["optical_geometry"]
        validate_rgb_outline(observation, material="Gr")
        validate_rgb_capture_binding(store, store.get_sample("flake"), observation)
        region = as_region(observation["region_mm"])
        assert len(region.components[0].holes) == 1
        assert observation["recognition_chain"]["calibration"]["mm_per_pixel"] == [.000182, .000182]
        assert result["source_anchor20"]["stage_xy"] == [2.1-geometry["dx_mm"], 2.8-geometry["dy_mm"]]
        assert not any(key.startswith("measurement_") for key in result)
    finally:
        store.close()


@pytest.mark.parametrize("change", ["fill_hole", "centroid", "stage", "budget", "profile", "readback", "inventory"])
def test_owner20_refuses_geometry_budget_or_public_anchor_changes(tmp_path, change):
    store, result, _ = source20(tmp_path)
    try:
        observation = deepcopy(result["optical_geometry"])
        if change == "fill_hole":
            observation["region_mm"]["components"][0]["holes"] = []
        elif change == "centroid":
            observation["source_anchor20"]["centroid_px"][0] += 1
        elif change == "stage":
            observation["source_anchor20"]["stage_xy"][0] += .001
        elif change == "budget":
            observation["boundary_uncertainty_mm"] = .0005
        elif change == "profile":
            observation["recognition_chain"]["calibration"]["material_capture_profile"] = "old"
        elif change == "readback":
            observation["source_anchor20"]["stage_readback_event_id"] = observation["recognition_event_id"]
        else:
            store.upsert_sample("flake", "flake", props={"stage_xy": [99., 99.]})
        with pytest.raises(ValueError):
            validate_rgb_outline(observation, material="Gr")
            validate_rgb_capture_binding(store, store.get_sample("flake"), observation)
    finally:
        store.close()


def test_100x_is_journaled_classification_only_without_measurement(tmp_path, monkeypatch):
    store, result, _ = source20(tmp_path)
    try:
        chain = deepcopy(result["recognition_20x"])
        chain["image"]["objective"] = 100
        frame = SimpleNamespace(meta={"objective": 100})
        monkeypatch.setattr("qumus2.perception.measurement.measure_recognition_boundary",
            lambda *a, **k: pytest.fail("100X classification invoked derived boundary measurement"))
        current = search._rgb_revisit_evidence(SimpleNamespace(store=store, run_id="classification"),
            SimpleNamespace(persist_detection_masks=lambda *a: chain), frame,
            SimpleNamespace(material="Gr", thickness_label="monolayer"), "chip", "flake", 9)
        assert set(current) == {"recognition_100x", "classification_event_100x"}
        assert result["optical_geometry"]["recognition_chain"]["image"]["objective"] == 20
        event = store.events_after(current["classification_event_100x"]-1)[0]
        assert event.kind == "microscope.rgb_classification"
        assert event.payload["objective"] == 100
        assert event.payload["geometry_use"] == "classification_only"
    finally:
        store.close()


def test_normal_revisit_recenters_pickup_on20_not100_centroid(tmp_path, monkeypatch):
    store, original, geometry = source20(tmp_path)
    try:
        job = store.create_run("search_chip", {"chip_id": "chip"})
        store.emit("step.verified", {"name": "measure_chip_rotation", "verdict": "ok",
            "measurements": {"rotation_deg": 31.}}, run_id=job)
        position, objective = [2.1, 2.8], [20]
        chain20 = original["recognition_20x"]
        chain100 = deepcopy(chain20)
        chain100["image"]["objective"] = 100
        chains = {20: chain20, 100: chain100}
        stage = SimpleNamespace(get_pose=lambda: Pose(x=position[0], y=position[1]),
            get_fine_position=lambda: (0., 0.))
        def capture():
            store.emit("device.command", {"device": "microscope", "command": "capture"}, run_id=job)
            return SimpleNamespace(meta={"objective": objective[0]}, timestamp=7.,
                saved_path=lambda: chains[objective[0]]["image"]["path"])
        scope = SimpleNamespace(set_objective=lambda v: objective.__setitem__(0, v), capture=capture)
        def detection(frame, material):
            return SimpleNamespace(material=material, thickness_label="monolayer",
                dx_mm=geometry["dx_mm"] if objective[0] == 20 else .002,
                dy_mm=geometry["dy_mm"] if objective[0] == 20 else -.003,
                meta={"recognition_chain": chains[objective[0]]})
        an = SimpleNamespace(recognition_backend="v1_lab_rgb", revisit_target=detection,
            persist_detection_masks=lambda *a: chains[objective[0]])
        ctx = SimpleNamespace(store=store, run_id=job, check_cancel=lambda: None,
            verification={"revisit": {"refind_radius_um": 100.}},
            bench={"microscope": {"revisit_objectives": [20, 100]}},
            gate=SimpleNamespace(microscope=lambda *a: scope, stage=lambda *a: stage))
        step = search.RevisitCandidates("chip", "Gr", 1)
        monkeypatch.setattr(step, "_position", lambda *a: (2.1, 2.8))
        monkeypatch.setattr(search, "analyzer", lambda *a: an)
        monkeypatch.setattr(search, "autofocus", lambda *a: 3.)
        monkeypatch.setattr(search, "_move_stage", lambda c, x, y: position.__setitem__(slice(None), [x, y]))
        expected = [2.1-geometry["dx_mm"], 2.8-geometry["dy_mm"]]
        def neighbourhood(*args):
            assert position == expected
            return {"images": []}
        monkeypatch.setattr("qumus2.flows.library.pickup_evidence.capture_neighbourhood", neighbourhood)
        monkeypatch.setattr("qumus2.perception.measurement.measure_recognition_boundary",
            lambda *a, **k: pytest.fail("Normal revisit invoked derived RGB measurement"))
        result = step._acquire_candidate(ctx, store.get_sample("flake"))
        assert result["found"] and result["stage_xy"] == expected == position
        assert result["optical_geometry"]["recognition_chain"] == chain20
        assert result["recognition_100x"] == chain100
        assert not any(key.startswith("measurement_") for key in result)
    finally:
        store.close()
