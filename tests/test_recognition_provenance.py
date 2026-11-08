"""Mask identity controls, independent of recognition quality or polygon support."""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest
from PIL import Image

from qumus2.records.recognition import (
    validate_recognition_chain,
    validate_rgb_capture_binding,
    validate_rgb_outline,
)


def evidence(tmp_path):
    labels = np.zeros((10, 12), dtype=np.uint16)
    labels[2:8, 3:9] = 7
    labels[4:6, 5:7] = 0  # Preserve a hole; downstream convex admission must reject it.
    labels[0, 0] = 3
    arrays = {
        "image": np.zeros((10, 12, 3), dtype=np.uint8),
        "original_mask": labels,
        "geometry_mask": (labels == 7).astype(np.uint8) * 255,
    }
    chain = {
        "version": "v1-rgb-mask-chain-v1",
        "backend": "v1_lab_rgb",
        "algorithm_sha256": "a" * 64,
        "material_policy_sha256": "b" * 64,
        "selected_label_ids": [7],
        "calibration": {"mm_per_pixel": [0.001, 0.002]},
        "derivation": {
            "policy": "center-priority-original-component-v1",
            "operation": "select_original_label_only",
            "morphology": "none",
            "selected_label_id": 7,
        },
    }
    for key, array in arrays.items():
        path = tmp_path / (key + ".png")
        Image.fromarray(array).save(path)
        chain[key] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    chain["image"].update(objective=20, size_px=[12, 10])
    return chain


def test_original_component_and_holes_are_preserved(tmp_path):
    chain = evidence(tmp_path)
    assert validate_recognition_chain(chain, image_path=chain["image"]["path"]) == chain


@pytest.mark.parametrize("change", ["expand", "shrink", "fill_hole", "wrong_component"])
def test_rehashed_geometry_cannot_repair_or_replace_original_component(tmp_path, change):
    chain = evidence(tmp_path)
    path = chain["geometry_mask"]["path"]
    with Image.open(path) as im:
        mask = np.asarray(im).copy()
    if change == "expand":
        mask[1, 3] = 255
    elif change == "shrink":
        mask[2, 3] = 0
    elif change == "fill_hole":
        mask[4, 5] = 255
    else:
        mask[:] = 0
        mask[0, 0] = 255
    Image.fromarray(mask).save(path)
    from pathlib import Path

    chain["geometry_mask"]["sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="expanded, truncated or filled"):
        validate_recognition_chain(chain, image_path=chain["image"]["path"])


@pytest.mark.parametrize("field", ["objective", "source_image", "bytes", "missing", "labels"])
def test_missing_stale_or_wrong_exposure_refuses(tmp_path, field):
    chain = evidence(tmp_path)
    original = deepcopy(chain)
    if field == "objective":
        chain["image"]["objective"] = 100
    elif field == "source_image":
        chain["image"]["path"] = str(tmp_path / "another.png")
    elif field == "bytes":
        chain["original_mask"]["sha256"] = "c" * 64
    elif field == "missing":
        del chain["original_mask"]
    else:
        chain["selected_label_ids"] = [True]
    with pytest.raises(ValueError):
        validate_recognition_chain(chain, image_path=original["image"]["path"])


def outlined_evidence(tmp_path, *, hole=False, spacing=(.001, .002)):
    from pathlib import Path

    from qumus2.core.polygons import transform
    from qumus2.perception.rgb_material import component_geometry

    chain = evidence(tmp_path)
    chain["material"] = "Gr"
    chain["material_policy"] = {"material": "Gr", "objective": 20, "backend": "v1_lab_rgb"}
    chain["material_policy_sha256"] = hashlib.sha256(json.dumps(
        chain["material_policy"], sort_keys=True, allow_nan=False).encode()).hexdigest()
    chain["calibration"].update(image_size_px=[12, 10], u_axis="+world_x", v_axis="-world_y")
    chain["calibration"]["mm_per_pixel"] = list(spacing)
    if not hole:
        for key in ("original_mask", "geometry_mask"):
            path = Path(chain[key]["path"])
            with Image.open(path) as image:
                pixels = np.asarray(image).copy()
            pixels[4:6, 5:7] = 7 if key == "original_mask" else 255
            Image.fromarray(pixels).save(path)
            chain[key]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with Image.open(chain["geometry_mask"]["path"]) as image:
        geometry = component_geometry(np.asarray(image) != 0, chain["calibration"])
    chain["derivation"].update({k: geometry[k] for k in ("centroid_px", "rings_px", "status")})
    chain["derivation"].update(ambiguity_status="unique", selection_context="v1_center_priority_revisit")
    return {"source": "rgb_optical_outline", "recognition_chain": chain,
            "image": chain["image"]["path"], "chip_rotation_deg": 31.,
            "outline_mm": transform(geometry.get("outline_mm", ((0., 0.), (.01, 0.), (0., .01))), rotation_deg=-31.)}


def test_rotated_original_pixel_outline_is_admitted(tmp_path):
    observation = outlined_evidence(tmp_path)
    validate_rgb_outline(observation, material="Gr")


@pytest.mark.parametrize("change", ["rotation", "outline", "rings", "centroid", "calibration", "missing",
                                    "policy", "material", "ambiguity"])
def test_replaced_geometry_or_transform_is_refused(tmp_path, change):
    observation = outlined_evidence(tmp_path)
    chain = observation["recognition_chain"]
    if change == "rotation":
        observation["chip_rotation_deg"] += 5
    elif change == "outline":
        observation["outline_mm"] = [(x * 1.2, y * 1.2) for x, y in observation["outline_mm"]]
    elif change == "rings":
        chain["derivation"]["rings_px"][0][0][0] += .1
    elif change == "centroid":
        chain["derivation"]["centroid_px"][0] += 1
    elif change == "calibration":
        chain["calibration"]["mm_per_pixel"][0] *= 2
    elif change == "policy":
        chain["material_policy"]["objective"] = 100
    elif change == "material":
        chain["material"] = "hBN"
    elif change == "ambiguity":
        chain["derivation"]["ambiguity_status"] = "not_evaluated_all_candidates"
    else:
        del observation["recognition_chain"]
    with pytest.raises(ValueError):
        validate_rgb_outline(observation, material="Gr")


def test_hole_mask_cannot_be_promoted_as_convex_outline(tmp_path):
    observation = outlined_evidence(tmp_path, hole=True)
    with pytest.raises(ValueError, match="holes_or_multiple_boundaries"):
        validate_rgb_outline(observation, material="Gr")


def capture_links(store, observation):
    rotation = store.emit("step.verified", {"name": "measure_chip_rotation", "verdict": "ok",
        "measurements": {"rotation_deg": observation["chip_rotation_deg"]}}, run_id=observation["job_id"])
    capture = store.emit("device.command", {"device": "microscope", "command": "capture", "params": {}},
                         run_id=observation["job_id"])
    return {"chip_rotation_event_id": rotation, "capture_event_id": capture}


def test_current_designer_rechecks_original_mask_bytes(tmp_path):
    """A saved accepted design cannot outlive its original mask artifacts."""
    from pathlib import Path

    from test_device_design import design_fixture, fake_artifact, save_design

    from qumus2.records.device_design import (
        bind_artifact,
        build_design_spec,
        validate_design_current,
    )
    from qumus2.records.geometry import selection_source_snapshot

    store, project, placements, original = design_fixture(tmp_path, wide=True)
    try:
        sample = store.get_sample("flake_Gr")
        evidence_dir = tmp_path / "rgb"
        evidence_dir.mkdir()
        rgb = outlined_evidence(evidence_dir, spacing=(.01, .014))
        g = {**sample["props"]["optical_geometry"], **rgb}
        g["job_id"] = store.create_run("search_chip", {"chip_id": sample["location"]}, project_id=project)
        links = capture_links(store, g)
        g["recognition_event_id"] = store.emit("microscope.rgb_recognition", {
            "version": 1, "flake_id": sample["id"], "source_chip_id": sample["location"],
            "material": "Gr", "chip_rotation_deg": g["chip_rotation_deg"],
            "recognition_chain": g["recognition_chain"], "capture_timestamp": 1., **links,
        }, run_id=g["job_id"])
        store.set_run_status(g["job_id"], "succeeded")
        store.upsert_sample("flake_Gr", "flake", location=sample["location"],
                            props={**sample["props"], "optical_geometry": g})
        state = store.get_project(project)["state"]
        store.update_project_state(project, selection_source_evidence=selection_source_snapshot(
            store, state["selected_flakes"]))
        spec = build_design_spec(store, project, placements, 1,
                                 design_id=original.design_id, revision=1)
        design = bind_artifact(spec, fake_artifact(store, spec))
        save_design(store, project, design)
        assert validate_design_current(store, project) == design
        Path(g["recognition_chain"]["original_mask"]["path"]).unlink()
        with pytest.raises(ValueError, match="original RGB/mask evidence"):
            validate_design_current(store, project)
    finally:
        store.close()


@pytest.mark.parametrize("change", [None, "calibration_and_outline", "rotation_and_outline", "foreign",
                                    "late", "missing", "missing_link", "reversed_links", "wrong_capture"])
def test_original_capture_binding_refuses_coherent_inventory_rewrite(tmp_path, change):
    from qumus2.core.polygons import transform
    from qumus2.runtime.store import RunStore

    store = RunStore(tmp_path / "qumus.db")
    try:
        g = outlined_evidence(tmp_path)
        g["job_id"] = store.create_run("search_chip", {"chip_id": "chip"})
        flake = {"id": "flake", "location": "chip", "props": {"material": "Gr"}}
        links = capture_links(store, g)
        if change == "missing_link":
            links.pop("capture_event_id")
        elif change == "reversed_links":
            links["capture_event_id"], links["chip_rotation_event_id"] = links["chip_rotation_event_id"], links["capture_event_id"]
        elif change == "wrong_capture":
            links["capture_event_id"] = store.emit("device.command", {"device": "microscope", "command": "set_objective"}, run_id=g["job_id"])
        if change == "late":
            store.set_run_status(g["job_id"], "succeeded")
        g["recognition_event_id"] = store.emit("microscope.rgb_recognition", {
            "version": 1, "flake_id": "flake", "source_chip_id": "chip", "material": "Gr",
            "chip_rotation_deg": g["chip_rotation_deg"], "recognition_chain": g["recognition_chain"],
            "capture_timestamp": 1., **links,
        }, run_id=g["job_id"] if change != "foreign" else "another-job")
        store.set_run_status(g["job_id"], "succeeded")
        if change == "calibration_and_outline":
            g["recognition_chain"]["calibration"]["mm_per_pixel"] = [.002, .004]
            g["outline_mm"] = [(2*x, 2*y) for x, y in g["outline_mm"]]
            validate_rgb_outline(g, material="Gr")  # Self-consistency alone is insufficient.
        elif change == "rotation_and_outline":
            g["chip_rotation_deg"] += 5
            g["outline_mm"] = transform(g["outline_mm"], rotation_deg=-5)
            validate_rgb_outline(g, material="Gr")
        elif change == "missing":
            del g["recognition_event_id"]
        if change is None:
            validate_rgb_capture_binding(store, flake, g)
        else:
            with pytest.raises(ValueError):
                validate_rgb_capture_binding(store, flake, g)
    finally:
        store.close()


def test_rgb_chain_cannot_be_disguised_as_legacy_outline(tmp_path):
    from qumus2.records.geometry import _pickup_outline
    from qumus2.runtime.store import RunStore

    store = RunStore(tmp_path / "qumus.db")
    try:
        job = store.create_run("search_chip", {"chip_id": "chip"})
        store.set_run_status(job, "succeeded")
        g = outlined_evidence(tmp_path)
        g.update(source="simulated_optical_outline", source_chip_id="chip", job_id=job,
                 units="mm", frame="flake_center_chip_axes")
        sample = {"id": "flake", "location": "chip", "props": {"material": "Gr", "optical_geometry": g}}
        with pytest.raises(ValueError, match="legacy outline source"):
            _pickup_outline(store, sample)
    finally:
        store.close()


@pytest.mark.parametrize("corrupt", [False, True])
def test_picker_attaches_distinct_original_and_geometry_masks(tmp_path, corrupt):
    import asyncio
    from pathlib import Path

    from qumus2.agents.gateway import ToolGateway
    from qumus2.agents.tooling import default_registry
    from qumus2.runtime.store import RunStore

    chain = evidence(tmp_path)  # Its hole remains visible; viewing does not admit a design.
    store = RunStore(tmp_path / "qumus.db")
    try:
        project = store.create_project("RGB evidence viewing control")
        store.upsert_sample("flake", "flake", location="chip", props={
            "material": "Gr", "images": {"20x": chain["image"]["path"]},
            "recognition_20x": chain, "mask_20x": chain["original_mask"]["path"],
            "geometry_mask_20x": chain["geometry_mask"]["path"],
        })
        if corrupt:
            Path(chain["geometry_mask"]["path"]).write_bytes(b"changed evidence")
        gateway = ToolGateway(default_registry(), store, None, project_id=project)
        result, _ = asyncio.run(gateway.call("picker", "view_flake_images", {"flake_id": "flake"}))
        assert result.status == "ok"
        if corrupt:
            assert result.images == [chain["image"]["path"]]
            assert result.data["recognition_evidence"]["status"] == "unresolved"
        else:
            assert result.images == [chain[k]["path"] for k in ("image", "original_mask", "geometry_mask")]
            assert result.data["recognition_evidence"]["status"] == "original_masks_verified"
    finally:
        store.close()
