"""Original pixel holes/components and source-bound boundary-budget controls."""
import hashlib
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from test_recognition_provenance import capture_links, outlined_evidence

from qumus2.core.regions import region_dict, transform_region
from qumus2.perception import measurement
from qumus2.perception.rgb_material import component_geometry
from qumus2.records.geometry import _pickup_region, geometry_digest
from qumus2.records.recognition import (
    validate_boundary_uncertainty,
    validate_rgb_capture_binding,
    validate_rgb_outline,
)
from qumus2.runtime.store import RunStore


def analytic_qualification_test_double(monkeypatch, observation):
    """A local math/provenance double; no production profile is qualified."""
    chain = observation["recognition_chain"]
    profile = {"version": "rgb-boundary-measurement-profile-v1",
        "profile_id": "analytic_unit_profile_not_physical_calibration",
        "status": "qualified_conditional_sim", "capture_profile": chain["calibration"].get("material_capture_profile"),
        "method_id": "analytic-unit-test-double-only", "qualification_sha256": "7"*64,
        "algorithm_sha256": chain["algorithm_sha256"], "domain": measurement._capture_domain(chain),
        "boundary_uncertainty_mm": observation["boundary_uncertainty_mm"]}
    observation["boundary_uncertainty_provenance"].update(profile_id=profile["profile_id"],
        profile_sha256=geometry_digest(profile), method_id=profile["method_id"],
        qualification_sha256=profile["qualification_sha256"], capture_profile=profile["capture_profile"],
        domain_sha256=geometry_digest(profile["domain"]))
    monkeypatch.setattr(measurement, "_measurement_profile", lambda: deepcopy(profile))
    return profile


def regional_rgb_evidence(tmp_path, monkeypatch=None):
    observation = outlined_evidence(tmp_path, hole=True)
    chain = observation["recognition_chain"]
    with Image.open(chain["geometry_mask"]["path"]) as image:
        geometry = component_geometry(np.asarray(image) != 0, chain["calibration"])
    observation.pop("outline_mm")
    observation["region_mm"] = region_dict(transform_region(geometry["region_mm"], rotation_deg=-31.))
    observation["boundary_uncertainty_mm"] = .0001
    observation["boundary_uncertainty_provenance"] = {
        "version": "sim-boundary-uncertainty-v1", "source": "simulation_observation_profile",
        "profile_id": "analytic_unit_profile_not_physical_calibration", "profile_sha256": "1"*64,
        "image_sha256": chain["image"]["sha256"],
        "calibration_sha256": geometry_digest(chain["calibration"])}
    if monkeypatch is not None:
        analytic_qualification_test_double(monkeypatch, observation)
    return observation


def test_analytic_qualification_double_preserves_original_holed_region(tmp_path, monkeypatch):
    observation = regional_rgb_evidence(tmp_path, monkeypatch)
    validate_rgb_outline(observation, material="Gr")
    assert len(observation["region_mm"]["components"][0]["holes"]) == 1
    changed = deepcopy(observation)
    changed["region_mm"]["components"][0]["holes"] = []
    with pytest.raises(ValueError, match="topology differs"):
        validate_rgb_outline(changed, material="Gr")


@pytest.mark.parametrize("change", [None, "region", "bound", "profile", "zero", "missing", "calibration"])
def test_capture_event_binds_exact_region_and_analytic_test_budget(tmp_path, monkeypatch, change):
    store = RunStore(tmp_path/"qumus.db")
    try:
        observation = regional_rgb_evidence(tmp_path, monkeypatch)
        job = store.create_run("search_chip", {"chip_id": "chip"})
        observation.update(job_id=job, source_chip_id="chip", units="mm", frame="flake_center_chip_axes")
        links = capture_links(store, observation)
        observation["recognition_event_id"] = store.emit("microscope.rgb_recognition", {
            "version": 1, "flake_id": "flake", "source_chip_id": "chip", "material": "Gr",
            "chip_rotation_deg": observation["chip_rotation_deg"],
            "recognition_chain": deepcopy(observation["recognition_chain"]),
            **{key: deepcopy(observation[key]) for key in (
                "region_mm", "boundary_uncertainty_mm", "boundary_uncertainty_provenance")},
            **links}, run_id=job)
        store.set_run_status(job, "succeeded")
        if change == "region":
            observation["region_mm"]["components"][0]["holes"] = []
        elif change == "bound":
            observation["boundary_uncertainty_mm"] /= 2
        elif change == "profile":
            observation["boundary_uncertainty_provenance"]["profile_sha256"] = "2"*64
        elif change == "zero":
            observation["boundary_uncertainty_mm"] = 0.
        elif change == "missing":
            del observation["boundary_uncertainty_mm"]
        elif change == "calibration":
            observation["boundary_uncertainty_provenance"]["calibration_sha256"] = "3"*64
        flake = {"id": "flake", "kind": "flake", "location": "chip", "status": "catalogued",
                 "props": {"material": "Gr", "optical_geometry": observation}}
        if change is None:
            validate_rgb_capture_binding(store, flake, observation)
            assert len(_pickup_region(store, flake).components[0].holes) == 1
        else:
            with pytest.raises(ValueError):
                validate_rgb_capture_binding(store, flake, observation)
    finally:
        store.close()


def test_arbitrary_positive_bound_and_profile_hash_cannot_qualify_observed_region(tmp_path):
    observation = regional_rgb_evidence(tmp_path)
    original = deepcopy(observation)
    with pytest.raises(ValueError, match="profile is unknown"):
        validate_boundary_uncertainty(observation)
    assert observation == original
    # Refusing metrology does not delete or alter the original image, paired
    # masks, exact pixel region or their immutable chain.
    for key in ("image", "original_mask", "geometry_mask"):
        receipt = observation["recognition_chain"][key]
        assert hashlib.sha256(Path(receipt["path"]).read_bytes()).hexdigest() == receipt["sha256"]


def test_current_native_profile_is_explicitly_unqualified_and_input_cannot_upgrade_it(tmp_path):
    observation = regional_rgb_evidence(tmp_path)
    status = measurement.boundary_measurement_status()
    assert status["status"] == "unqualified" and "boundary_uncertainty_mm" not in status
    provenance = observation["boundary_uncertainty_provenance"]
    provenance.update(profile_id=status["profile_id"], profile_sha256=status["profile_sha256"],
        status="qualified_conditional_sim", method_id="forged", qualification_sha256="f"*64)
    with pytest.raises(ValueError, match="profile is unqualified"):
        validate_boundary_uncertainty(observation)


@pytest.mark.parametrize("change", ["bound", "method", "domain", "algorithm", "capture", "qualification"])
def test_analytic_authority_double_binds_method_domain_algorithm_capture_and_numeric_budget(tmp_path, monkeypatch, change):
    observation = regional_rgb_evidence(tmp_path, monkeypatch)
    if change == "bound":
        observation["boundary_uncertainty_mm"] /= 2
    elif change == "method":
        observation["boundary_uncertainty_provenance"]["method_id"] = "changed"
    elif change == "domain":
        observation["recognition_chain"]["image"]["objective"] = 100
    elif change == "algorithm":
        observation["recognition_chain"]["algorithm_sha256"] = "f"*64
    elif change == "capture":
        observation["recognition_chain"]["calibration"]["material_capture_profile"] = "other"
        observation["boundary_uncertainty_provenance"]["calibration_sha256"] = geometry_digest(observation["recognition_chain"]["calibration"])
    else:
        observation["boundary_uncertainty_provenance"]["qualification_sha256"] = "f"*64
    with pytest.raises(ValueError, match="qualified method, domain, algorithm or capture"):
        validate_boundary_uncertainty(observation)


def test_native_convex_rgb_cannot_bypass_qualification_through_legacy_outline_schema(tmp_path):
    observation = outlined_evidence(tmp_path)
    observation["recognition_chain"]["calibration"]["material_capture_profile"] = measurement.boundary_measurement_status()["capture_profile"]
    with pytest.raises(ValueError, match="boundary uncertainty"):
        validate_rgb_outline(observation, material="Gr")
