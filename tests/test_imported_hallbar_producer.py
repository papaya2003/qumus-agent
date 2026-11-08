"""Pure target-producer projections; no World construction or physical job."""
from __future__ import annotations

from copy import deepcopy
from threading import RLock
from types import SimpleNamespace

import pytest
from test_imported_hallbar_target import declared_import

from qumus2.core.types import Pose
from qumus2.records.device_design import target_import_binding
from qumus2.testbench.hallbar import electrode_pattern_from_target, hallbar_target
from qumus2.testbench.lab import _configured_target_geometry
from qumus2.testbench.world import ManipulatorState, SimObject, SimWorld


def projection(target, binding=None):
    props = {"device_design_target": target.model_dump(mode="json")}
    if binding is not None:
        props["imported_target_binding"] = binding
    typed, pattern, source = _configured_target_geometry(props)
    obj = SimObject("target", "chip", Pose(x=0, y=0), size_mm=typed.size_mm,
                    electrodes=pattern, electrode_geometry_source=source)
    # Exercise the actual snapshot method without constructing/sampling a World.
    state = SimpleNamespace(lock=RLock(), objects={obj.id: obj}, stamps={},
        manip=ManipulatorState(), surfaces={}, collisions=[], transfer_dynamics={}, model={})
    return SimWorld.evaluation_snapshot(state)["objects"][0]["electrode_geometry"]


def test_imported_physical_projection_preserves_all_original_metal_and_binding():
    target = declared_import()
    geometry = projection(target, target_import_binding(target))
    assert len(geometry["polygons"]) == 34
    assert [p["source_primitive_index"] for p in geometry["polygons"]] == list(range(34))
    assert geometry["polygons"] == electrode_pattern_from_target(target)
    assert geometry["source_binding"] == target_import_binding(target)
    assert geometry["layout_source_sha256"] == target.import_receipt.source_sha256
    assert geometry["coverage_contract"] == "complete_contact_region_containment"
    assert geometry["frame"] == "chip_center_axes"
    assert geometry["end_regions"] == [{"id": c.id, "role": c.role,
        "source_primitive_index": c.source_primitive_index,
        "points": [[x-.5, y-.5] for x, y in c.points_mm]} for c in target.contacts]


@pytest.mark.parametrize("change", ["missing", "source", "target", "receipt"])
def test_imported_producer_refuses_unbound_or_changed_target(change):
    target = declared_import()
    binding = deepcopy(target_import_binding(target))
    if change == "missing":
        binding = None
    else:
        binding[{"source": "source_sha256", "target": "target_sha256",
                 "receipt": "import_receipt_sha256"}[change]] = "0"*64
    with pytest.raises(ValueError, match="configured source binding"):
        projection(target, binding)


def test_legacy_physical_projection_keeps_original_shape():
    target = hallbar_target()
    geometry = projection(target)
    assert set(geometry) == {"source", "reference", "units", "frame", "polygons"}
    assert len(geometry["polygons"]) == 8
    assert all(set(p) == {"id", "kind", "points"} for p in geometry["polygons"])


def test_linked_discovery_declaration_is_bound_before_world_construction(monkeypatch):
    from qumus2.config import simulation_profile, with_stamp_discovery
    from qumus2.core.clock import SimClock
    from qumus2.records.discovery import discovery_policy
    from qumus2.testbench import lab
    from qumus2.testbench.bench_geometry import geometry_binding

    compose = lab.__dict__.get("compose_bench_geometry")
    assert compose is None  # The real producer imports composition locally.
    from qumus2.testbench import bench_geometry

    original = bench_geometry.compose_bench_geometry
    seen = []
    def checked(bench, config):
        assert bench["stamp_discovery"] == discovery_policy()
        composed = original(bench, config)
        seen.append(geometry_binding(composed))
        return composed
    class BeforeWorld(Exception):
        pass
    def no_world(*args, **kwargs):
        raise BeforeWorld
    monkeypatch.setattr(bench_geometry, "compose_bench_geometry", checked)
    monkeypatch.setattr(lab, "SimWorld", no_world)
    with pytest.raises(BeforeWorld):
        # Discovery is opt-in since #204.
        lab.build_sim_lab(SimClock(), config=with_stamp_discovery(simulation_profile("hallbar-linked")))
    assert len(seen) == 1 and seen[0]["layout_sha256"]


@pytest.mark.parametrize("cropped", [False, True])
def test_observed_endpoint_requires_complete_imported_contact_ends(monkeypatch, cropped):
    from test_device_design_pose_screen import bound_design
    from test_imported_hallbar_target import imported_spec

    from qumus2.core.polygons import rectangle
    from qumus2.flows.library import stacking

    design = bound_design(imported_spec())
    monkeypatch.setattr(stacking, "_device_design_public_binding", lambda *a: design)
    sources = {s.material: s.placed_outline() for s in design.sources}
    if cropped:
        # Nonzero overlap at each voltage tip cannot satisfy full-end coverage.
        sources["Gr"] = rectangle(.021, .042, (.5, .5))
    groups = [{"material": material, "outlines": [{"outline_mm": outline}],
               "observation_binding": {}} for material, outline in sources.items()]*3
    ctx = SimpleNamespace(params={}, store=SimpleNamespace(get_run=lambda rid: {}), run_id="unit")
    result = stacking._device_design_endpoint(ctx, {"materials_found": {"hBN": [], "Gr": []}},
        {"observed_outlines": groups, "device_design_digest": design.canonical_digest})
    assert result["status"] == ("fail" if cropped else "pass")
    coverage = result["contact_coverage"] if cropped else result["observations"][0]["contact_coverage"]
    assert coverage["policy"] == "complete_contact_region_containment"
    assert coverage["status"] == ("fail" if cropped else "pass")
