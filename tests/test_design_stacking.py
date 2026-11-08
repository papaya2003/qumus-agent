"""New design-to-process boundaries; legacy direct-Gr contracts stay separate."""
from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace

import pytest

from qumus2.agents.gateway import ToolGateway
from qumus2.agents.tooling import default_registry
from qumus2.config import load_yaml
from qumus2.core.polygons import convex_polygon, transform
from qumus2.flows.library.search import MeasureCornerRotation
from qumus2.flows.library.stacking import (
    StackDeviceFlow,
    StackParams,
    _device_design_endpoint,
    _device_design_public_binding,
)
from qumus2.records.device_design import bind_artifact, build_design_spec
from qumus2.records.geometry import selection_source_snapshot
from qumus2.testbench.hallbar import with_hallbar_target
from tests.test_device_design import design_fixture, fake_artifact, save_design


def flow_fixture(tmp_path):
    store, project, placements, spec = design_fixture(tmp_path)
    # These are observed-record unit fixtures, not a substitute for fresh-source
    # physical acceptance. Admission uses the same recorded search evidence.
    for index, source in enumerate(spec.sources, 1):
        store.upsert_sample(source.source_chip_id, "chip", location=f"mid_chip_{index}", status="searched", props={"size_mm": [1, 1]})
        job = source.observation["job_id"]
        store.upsert_sample(source.flake_id, "flake", status="selected", props={"rel_corner_mm": [.5, .5],
            "pickup_assessment": {"flake_id": source.flake_id, "source_chip_id": source.source_chip_id,
                "images": [source.observation["image"]], "job_id": job, "clear": True}})
        if source.material == "hBN":
            # Explicit observed-record optical-class fixture. The fake bytes/GDS
            # remain binding doubles; no camera or live process is claimed.
            store.upsert_sample(source.flake_id, "flake", props={"image_source": "simulated",
                "thickness_label": "0-10", "labels": {"100x": "0-10"}, "tracking_references": {
                    "100x": {"image": source.observation["image"], "job_id": job,
                             "meta": {"calibration": {"source": "synthetic", "fixture": "record_binding_only"}}}}})
    store.upsert_sample("target", "chip", location="right_chip_1")
    store.upsert_sample("stamp", "stamp", location="right_stamp_1", status="fresh", props={"film": "PC"})
    store.update_project_state(project, selection_source_evidence=selection_source_snapshot(store, list(spec.pickup_order)))
    spec = build_design_spec(store, project, placements, 1, design_id=spec.design_id, revision=1)
    design = bind_artifact(spec, fake_artifact(store, spec))
    save_design(store, project, design)
    flow = StackDeviceFlow()
    return store, project, placements, design, flow


def test_advisor_reads_new_target_and_does_not_require_legacy_pair(tmp_path):
    store, project, _, spec = design_fixture(tmp_path)
    gateway = ToolGateway(default_registry(), store, None, project_id=project)
    geometry, _ = asyncio.run(gateway.call("advisor", "get_target_geometry", {"chip_id": "target"}))
    assert geometry.status == "ok" and len(geometry.data["contacts"]) == 8
    # A new plan is independent of existing selection and intentionally clears it.
    store.update_project_state(project, device_plan=None)
    result, _ = asyncio.run(gateway.call("advisor", "submit_device_plan", {
        "device_name": "Eight-contact Gr Hall-bar transfer", "target_chip_id": "target",
        "layer_stack": [{"role": "top_hbn", "material": "hBN", "thickness_labels": ["any"]}, {"role": "channel", "material": "Gr", "thickness_labels": ["any"]},
                        {"role": "existing_electrodes", "material": "metal", "flake": False}],
        "contact_scheme": "Gr directly on all eight existing electrodes", "channel": "Transfer-only geometry",
        "evidence": "Explicit synthetic wafer design"}))
    assert result.status == "ok", result.summary
    plan = store.get_project(project)["state"]["device_plan"]
    assert plan["design_contract"] == "device_design_v1" and not plan.get("direct_contact_intent")
    assert plan["required_footprint"]["width_um"] > 0
    assert plan["required_footprint"]["min_area_um2"] == 0
    assert all(r["min_area_um2"] == 0 for r in plan["flake_roles"])
    assert [r["material"] for r in plan["flake_roles"]] == ["hBN", "Gr"]
    candidates, _ = asyncio.run(gateway.call("picker", "get_flake_candidates", {}))
    assert candidates.data["design_contract"] == "device_design_v1"
    assert len(candidates.data["device_design_target"]["contacts"]) == 8


def test_new_selection_checks_complete_proposed_transforms_without_legacy_rectangle(tmp_path):
    from qumus2.agents.tools.picking import SubmitSelectionArgs, selection_coverage_errors
    from qumus2.core.polygons import rectangle
    from qumus2.records.device_design import FlakePlacement

    store, project, _, design, _ = flow_fixture(tmp_path)
    state = store.get_project(project)["state"]
    plan = {**state["device_plan"], "design_contract": "device_design_v1"}
    store.update_project_state(project, device_plan=plan)
    for source in design.sources:
        flake = store.get_sample(source.flake_id)
        outline = rectangle(.02, .06) if source.material == "hBN" else rectangle(.04, .01)
        store.upsert_sample(source.flake_id, "flake", props={"optical_geometry": {
            **flake["props"]["optical_geometry"], "outline_mm": [list(p) for p in outline]}})
    ctx = SimpleNamespace(store=store, project_id=project)
    args = SubmitSelectionArgs(selected_by_role={"top_hbn": [design.pickup_order[0]], "channel": [design.pickup_order[1]]},
        assessments=[], reasons="Public complete-polygon feasibility",
        design_placements=[FlakePlacement(flake_id=s.flake_id, translation_mm=(.5, .5),
            rotation_deg=90 if s.material == "hBN" else 0) for s in design.sources])
    assert selection_coverage_errors(ctx, args, list(design.pickup_order)) == []
    args.design_placements[0] = args.design_placements[0].model_copy(update={"rotation_deg": 0})
    assert selection_coverage_errors(ctx, args, list(design.pickup_order))
    args.design_placements = []
    assert "requires exactly" in selection_coverage_errors(ctx, args, list(design.pickup_order))[0]
    args.design_placements = [FlakePlacement(flake_id=s.flake_id, translation_mm=(.5, .5), rotation_deg=0) for s in design.sources]
    store.update_project_state(project, device_plan=state["device_plan"])
    assert "requires the eight-contact" in selection_coverage_errors(ctx, args, list(design.pickup_order))[0]


def test_new_target_registration_requires_human_instruction_and_matching_size(tmp_path):
    store, project, _, spec = design_fixture(tmp_path)
    gateway = ToolGateway(default_registry(), store, None, project_id=project)
    body = {"device_design_target": {"chip_id": "target", **spec.target.model_dump(mode="json")}}
    instruction = store.add_instruction(json.dumps(body), project_id=project)
    result, _ = asyncio.run(gateway.call("orchestrator", "register_target_geometry", {
        "chip_id": "target", "instruction_id": instruction}))
    assert result.status == "ok", result.summary
    body["device_design_target"]["size_mm"] = [.5, .5]
    instruction = store.add_instruction(json.dumps(body), project_id=project)
    bad, _ = asyncio.run(gateway.call("orchestrator", "register_target_geometry", {
        "chip_id": "target", "instruction_id": instruction}))
    assert bad.status == "error"


def test_flow_uses_design_source_rotation_and_rigid_translation(tmp_path):
    store, project, placements, design, flow = flow_fixture(tmp_path)
    # Rotate asymmetric source polygons, and shift both centres off the target
    # anchor; compute a larger hBN independently so full coverage stays valid.
    for source in design.sources:
        # Asymmetric sizes large enough for the current fresh pose-window screen.
        polygon = convex_polygon([[-.042, -.072], [.048, -.066], [.036, .078], [-.039, .063]]) if source.material == "hBN" else convex_polygon([[-.02,-.04],[.02,-.04],[.018,.04],[-.016,.038]])
        sample = store.get_sample(source.flake_id)
        geometry = {**sample["props"]["optical_geometry"], "outline_mm": polygon}
        store.upsert_sample(source.flake_id, "flake", props={"optical_geometry": geometry})
    store.update_project_state(project, selection_source_evidence=selection_source_snapshot(store, list(design.pickup_order)))
    placements = [p.model_copy(update={"translation_mm": (.5002, .4998), "rotation_deg": 0.}) for p in placements]
    spec = build_design_spec(store, project, placements, .5, design_id="design_"+"f"*32, revision=2)
    design = bind_artifact(spec, fake_artifact(store, spec))
    save_design(store, project, design)
    resolved = flow.resolve_params(StackParams(flake_ids=list(design.pickup_order), target_chip_id="target", device_design=design), store)
    assert resolved.target_xy_mm == (.5, .5)
    assert resolved.contact_dwell_s == 1 and resolved.contact_dwell_source == "synthetic_recipe_default"
    for source in design.sources:
        shift = resolved.design_shifts_mm[source.flake_id]
        angle = math.radians(resolved.design_rotations_deg[source.flake_id])
        world_shift = (shift[0]*math.cos(angle)-shift[1]*math.sin(angle), shift[0]*math.sin(angle)+shift[1]*math.cos(angle))
        assert world_shift == pytest.approx((.0002, -.0002))
    steps = flow.build_steps(resolved)
    # v1 (#204): no chip is turned to a design angle; each source placement's
    # own rotation is measured as v1's rough refind does, from one corner photo
    names = {s.name for s in steps}
    assert not any(n.startswith(("orient_design_", "measure_design_rotation_", "find_design_corner_")) for n in names)
    assert {f"measure_rotation_{s.source_chip_id}" for s in design.sources} | {"measure_rotation_target"} <= names
    by_name = {s.name: s for s in steps}
    for source in design.sources:
        corner = by_name[f"measure_rotation_{source.source_chip_id}"]
        assert isinstance(corner, MeasureCornerRotation) and corner.flake_id == source.flake_id
    assert not isinstance(by_name["measure_rotation_target"], MeasureCornerRotation)
    assert "center_stamp_aperture" in {s.name for s in steps}
    # v1 profile (#131): v1 never aligns the paper window; SIM-only switch.
    v1 = flow.build_steps(resolved.model_copy(update={"skip_aperture_alignment": True}))
    names = [s.name for s in v1]
    assert "center_stamp_aperture" not in names
    assert [n for n in names if n != "center_stamp_aperture"] == [n for n in (s.name for s in steps) if n != "center_stamp_aperture"]
    raw = resolved.model_copy(update={"skip_aperture_alignment": True}).model_dump(mode="json")
    store.bench = "lab"
    with pytest.raises(ValueError, match="simulation-only"):
        flow.validate_saved_params(raw, store=store)


@pytest.mark.parametrize("change", ["missing_class", "missing_native_reference", "changed_source", "caller_profile"])
def test_flow_refuses_unsupported_or_caller_modified_contact_calibration(tmp_path, change):
    store, project, _, design, flow = flow_fixture(tmp_path)
    args = StackParams(flake_ids=list(design.pickup_order), target_chip_id="target", device_design=design)
    fid = next(source.flake_id for source in design.sources if source.material == "hBN")
    if change == "missing_class":
        store.upsert_sample(fid, "flake", props={"thickness_label": None, "labels": {}})
    elif change == "missing_native_reference":
        store.upsert_sample(fid, "flake", props={"tracking_references": {"100x": None}})
    elif change == "changed_source":
        store.upsert_sample(fid, "flake", props={"image_source": "camera"})
    else:
        args.contact_interior_calibration = {"profile": "caller_loosened"}
    with pytest.raises(ValueError):
        flow.resolve_params(args, store)


def test_a_rotated_design_source_is_refused_as_v1_never_rotates_a_chip(tmp_path):
    store, project, _, design, flow = flow_fixture(tmp_path)
    placements = [s.placement.model_copy(update={"rotation_deg": .25}) for s in design.sources]
    spec = build_design_spec(store, project, placements, .5, design_id="design_"+"e"*32, revision=2)
    rotated = bind_artifact(spec, fake_artifact(store, spec))
    save_design(store, project, rotated)
    with pytest.raises(ValueError, match="v1 never rotates"):
        flow.resolve_params(StackParams(flake_ids=list(rotated.pickup_order), target_chip_id="target",
                                        device_design=rotated), store)


def test_endpoint_requires_actual_complete_all_contact_geometry(tmp_path, monkeypatch):
    _, _, _, design, _ = flow_fixture(tmp_path)
    import qumus2.flows.library.stacking as module

    monkeypatch.setattr(module, "_device_design_public_binding", lambda *a: design)
    ctx = SimpleNamespace(params={}, store=SimpleNamespace(get_run=lambda _: {}), run_id="run_test", verification=load_yaml("verification.yaml"))
    def after(shift=(0, 0)):
        groups = []
        for _ in range(3):
            for source in design.sources:
                polygon = transform(source.placed_outline(), xy=shift)
                groups.append({"material": source.material, "outlines": [{"outline_mm": polygon}], "observation_binding": {"frame": "observed"}})
        return {"observed_outlines": groups, "device_design_digest": design.canonical_digest}
    before = {"materials_found": {"hBN": 0, "Gr": 0}}
    assert _device_design_endpoint(ctx, before, after())["status"] == "pass"
    assert _device_design_endpoint(ctx, before, after((.01, 0)))["status"] == "fail"
    # Owner endpoint tolerance (Jialei 2026-11-03: 2 um): a 1.7 um placement
    # passes; the frozen 1 um design margin is unchanged, and electrode
    # coverage is still judged from the observed outline (it fails here first).
    assert design.pose_tolerance.translation_um == 1
    accepted = _device_design_endpoint(ctx, before, after((.0017, 0)))
    assert accepted["status"] == "pass" and accepted["accepted_translation_tolerance_um"] == 2
    assert _device_design_endpoint(ctx, before, after((.0025, 0)))["status"] == "fail"
    ambiguous = after()
    ambiguous["observed_outlines"][0]["outlines"] *= 2
    assert _device_design_endpoint(ctx, before, ambiguous)["status"] == "unresolved"
    cropped = after()
    for group in cropped["observed_outlines"]:
        if group["material"] == "hBN":
            group["outlines"][0]["outline_mm"] = [[5e-4 + x*.999, 5e-4 + y*.999]
                for x, y in group["outlines"][0]["outline_mm"]]
    assert _device_design_endpoint(ctx, before, cropped)["status"] == "fail"


def test_seeded_target_shares_physical_polygons_and_starts_without_flakes(tmp_path):
    from qumus2.app import build_services
    from qumus2.config import ideal_stacking_profile

    config = with_hallbar_target(ideal_stacking_profile())
    services = build_services(tmp_path / "bench.db", seed=8, testbench_config=config)
    assert services.store.list_samples(kind="flake") == []
    assert [s["id"] for s in services.store.list_samples(kind="chip")
            if s["status"] == "bare" and s["location"].startswith("left_chip_")] == [f"chip_{i:03d}" for i in range(1, 7)]
    target = services.store.get_sample("chip_e01")["props"]["device_design_target"]
    physical = services.lab.world.objects["chip_e01"].electrodes
    assert {c["id"] for c in target["contacts"]} == {c["id"] for c in physical}
    for contact in physical:
        expected = next(c for c in target["contacts"] if c["id"] == contact["id"])
        assert transform(convex_polygon(contact["points"]), xy=(5, 5)) == convex_polygon(expected["points_mm"])
    asyncio.run(services.runtime.close())


@pytest.mark.parametrize("change", ["reexfoliation", "source_chip", "transform", "extra_receiver_layer", "unverified_pickup", "material"])
def test_saved_execution_rejects_stale_history_and_unexplained_transitions(tmp_path, change):
    store, project, _, design, flow = flow_fixture(tmp_path)
    params = flow.resolve_params(StackParams(flake_ids=list(design.pickup_order), target_chip_id="target", device_design=design), store)
    raw = params.model_dump(mode="json")
    run_id = store.create_run("stack_device", raw, project_id=project)
    flow.reserve(params, store, run_id)  # the submission receipt jobs.submit records
    assert _device_design_public_binding(raw, store, store.get_run(run_id)) == design
    if change == "reexfoliation":
        store.create_run("exfoliate_chips", {"chip_ids": [design.sources[0].source_chip_id]}, project_id=project)
    elif change == "source_chip":
        raw["flake_chips"][design.sources[0].flake_id] = "other_chip"
    elif change == "transform":
        raw["design_shifts_mm"][design.sources[0].flake_id] = [.1, .1]
    elif change == "extra_receiver_layer":
        store.upsert_sample("extra", "flake", location="target")
    elif change == "material":
        store.upsert_sample(design.sources[0].flake_id, "flake", props={"material": "Gr"})
    else:
        store.upsert_sample(design.sources[0].flake_id, "flake", location=params.stamp_id, status="on_stamp")
    with pytest.raises(ValueError):
        _device_design_public_binding(raw, store, store.get_run(run_id))


@pytest.mark.parametrize("label_100x", ["0-10", None])
def test_carried_hbn_class_accepts_native20_geometry_with_observed_100x_label(tmp_path, label_100x):
    """A large hBN rarely fits whole in the 100x field: 20x geometry + 100x class (#121)."""
    from qumus2.flows.library.stacking import _contact_classification_snapshot

    store, _, _, design, _ = flow_fixture(tmp_path)
    source = next(s for s in design.sources if s.material == "hBN")
    image, job = source.observation["image"], source.observation["job_id"]
    labels = {"20x": "0-10", **({"100x": label_100x} if label_100x else {})}
    store.upsert_sample(source.flake_id, "flake", props={"labels": labels, "thickness_label": "0-10",
        "tracking_references": {"20x": {"image": image, "job_id": job, "meta": {"objective": 20,
            "calibration": {"source": "synthetic", "fixture": "record_binding_only"}}}}})
    sample = store.get_sample(source.flake_id)
    assert "100x" not in sample["props"]["tracking_references"]
    if label_100x is None:
        with pytest.raises(ValueError, match="complete-outline"):
            _contact_classification_snapshot(store, design)
        return
    snapshot = _contact_classification_snapshot(store, design)
    assert snapshot["source"]["image"] == image and snapshot["source"]["job_id"] == job
    assert snapshot["source"]["native_labels"]["100x"] == "0-10"


def test_saved_discovery_policy_checked_only_against_a_bound_bench(monkeypatch):
    """saved_flow() preflights before binding the bench (#122); the bound check still refuses."""
    from qumus2.flows.flow import Flow
    from qumus2.flows.workflow import WorkflowMismatch

    # Isolate the bench comparison; the shared parameter binding has its own tests.
    monkeypatch.setattr(Flow, "validate_saved_params", lambda self, raw, **kwargs: None)
    flow = StackDeviceFlow()
    raw = {"stamp_discovery_policy": {"version": "saved"}}
    flow.validate_saved_params(raw)  # unbound: bench.yaml is not the composed bench
    flow._bench = {"stamp_discovery": {"version": "saved"}}
    flow.validate_saved_params(raw)
    flow._bench = {"stamp_discovery": {"version": "changed"}}
    with pytest.raises(WorkflowMismatch):
        flow.validate_saved_params(raw)


@pytest.mark.parametrize("extra,status", [
    ("hbn_on_contact", "pass"),        # Jialei 2026-11-03: extra hBN on electrodes is harmless
    ("gr_on_one_contact", "pass"),
    ("gr_bridging_contacts", "fail"),  # graphite joining two electrodes is a short
])
def test_endpoint_accepts_co_picked_extras_unless_graphite_joins_electrodes(tmp_path, monkeypatch, extra, status):
    _, _, _, design, _ = flow_fixture(tmp_path)
    import qumus2.flows.library.stacking as module

    monkeypatch.setattr(module, "_device_design_public_binding", lambda *a: design)
    ctx = SimpleNamespace(params={}, store=SimpleNamespace(get_run=lambda _: {}), run_id="run_test",
                          verification=load_yaml("verification.yaml"))
    shapes = {"hbn_on_contact": ("hBN", [[.507, .486], [.514, .486], [.514, .494], [.507, .494]]),
              "gr_on_one_contact": ("Gr", [[.5075, .4875], [.5125, .4875], [.5125, .4925], [.5075, .4925]]),
              "gr_bridging_contacts": ("Gr", [[.5075, .4875], [.5125, .4875], [.5125, .5025], [.5075, .5025]])}
    material, outline = shapes[extra]
    groups = []
    for _ in range(3):
        for source in design.sources:
            outlines = [{"outline_mm": source.placed_outline()}]
            if source.material == material:
                outlines.append({"outline_mm": outline})
            groups.append({"material": source.material, "outlines": outlines, "observation_binding": {"frame": "observed"}})
    after = {"observed_outlines": groups, "device_design_digest": design.canonical_digest}
    result = _device_design_endpoint(ctx, {"materials_found": {"hBN": 0, "Gr": 0}}, after)
    assert result["status"] == status, result.get("reason")
    if status == "pass":
        assert [f["material"] for f in result["extra_flakes"]] == [material]
    else:
        assert result["reason"] == "an extra graphite flake joins two electrodes"
