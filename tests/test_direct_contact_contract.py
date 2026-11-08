"""Small public admission/geometry checks; no physical jobs or model calls."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from qumus2.agents.tooling import ToolContext
from qumus2.agents.tools.advisor import DevicePlanArgs, submit_device_plan, validate_device_plan
from qumus2.agents.tools.design import SubmitDesignArgs, validate_layout
from qumus2.core.polygons import rectangle, transform
from qumus2.flows.library.stacking import StackDeviceFlow, StackParams, _direct_contact_endpoint
from qumus2.flows.workflow import WorkflowMismatch
from qumus2.records.geometry import (
    DirectContactContract,
    contact_bridge_geometry,
    direct_contact_intent,
    direct_contact_result,
    geometry_digest,
    guidance_binding,
    resolve_direct_contact_contract,
    target_contact_snapshot,
    target_geometry,
    target_snapshot,
)
from qumus2.testbench.optics import electrode_pattern

REQUIREMENT = {
    "contact_overlap_um": 1,
    "channel_width_um": 2,
    "placement_margin_um": 0.5,
    "rationale": "prospective synthetic test criterion",
}


def plan_args(**kw):
    return DevicePlanArgs(
        device_name="direct bridge",
        target_chip_id="chip_e01",
        target_contact_region_id="centre_pair",
        direct_contact_material="Gr",
        bridge_requirement=REQUIREMENT,
        layer_stack=[
            {"role": "channel", "material": "Gr", "thickness_labels": ["monolayer"]},
            {"role": "existing_contacts", "material": "existing electrode", "flake": False},
        ],
        contact_scheme="direct existing contacts",
        channel="straight bridge",
        evidence="explicit synthetic design",
        **kw,
    )


@pytest.fixture
def public_source(services, tmp_path):
    s = services
    image = tmp_path / "source.png"
    image.write_bytes(b"qualified fixture image")
    s.store.upsert_sample("chip_001", "chip", location="mid_chip_1", props={"rotation_deg": 0.0})
    rid = s.store.create_run("revisit_chip", {"chip_id": "chip_001"})
    s.store.set_run_status(rid, "succeeded")
    s.store.emit("job.status", {"status": "succeeded"}, run_id=rid)
    geometry = {
        "outline_mm": [list(p) for p in rectangle(0.04, 0.008)],
        "image": str(image),
        "job_id": rid,
        "source_chip_id": "chip_001",
        "frame": "flake_center_chip_axes",
        "units": "mm",
        "source": "simulated_optical_outline",
    }
    assessment = {
        "flake_id": "gr",
        "source_chip_id": "chip_001",
        "job_id": rid,
        "images": [str(image)],
        "clear": True,
    }
    s.store.upsert_sample(
        "gr",
        "flake",
        location="chip_001",
        status="catalogued",
        props={
            "material": "Gr",
            "optical_geometry": geometry,
            "rel_corner_mm": [1, 1],
            "pickup_assessment": assessment,
            "area_um2": 320,
            "images": {"20x": str(image)},
            "tracking_references": {
                "20x": {"job_id": rid, "image": str(image), "meta": {"objective": 20}}
            },
        },
    )
    return s


def resolved(s):
    intent = direct_contact_intent(s.store, "chip_e01", "centre_pair", REQUIREMENT)
    c = resolve_direct_contact_contract(s.store, ["gr"], intent)
    gr = {
        "flake_id": "gr",
        "material": "Gr",
        "outline_mm": c.source_outline_mm,
        "outline_complete": True,
        "frame": "chip_center_axes",
        "units": "mm",
        "stamp_film": "PC",
        "incoming_layers_top_first": [{"flake_id": "gr", "material": "Gr"}],
        "source_geometry_sha256": c.source_geometry_sha256,
        "observation_binding": {"run_id": "actual", "image_sha256": "fixture"},
    }
    target = {
        "chip_id": "chip_e01",
        "geometry": intent["target_contact"],
        "receiving_layers_above_contacts": [],
        "before_observation_binding": {"image_sha256": "before"},
    }
    return c, gr, target


def mock_pickup_record(s, c, rid):
    """Nonphysical journal fixture; optical proof remains an explicit test stub."""
    from qumus2.flows.library import stacking

    fid = c.source_flake_id
    frame = {"image": c.source_observation["image"], "sha256": c.source_observation["image_sha256"]}
    measurements = {"fixture": True, "source_frames": [frame]}
    payload = {
        "seq": 0,
        "name": f"v1_pick_up_{fid}",
        "verdict": "ok",
        "measurements": measurements,
        "expected": {},
        "images": [frame["image"]],
    }
    s.store.emit("step.verified", payload, run_id=rid)
    verified = {
        "event_id": s.store.last_event_id(),
        "observation_sha256": stacking._digest(stacking._verified_body(payload)),
    }
    s.store.update_run_meta(
        rid,
        normal_transfer={
            "source_references": {fid: {"fixture": True}},
            "phases": {
                f"v1_pick_up_{fid}": {
                    "complete": True,
                    "endpoint": measurements,
                    "endpoint_verified": verified,
                }
            },
        },
    )
    s.store.upsert_sample(
        "stamp_01",
        "stamp",
        location="transfer_arm",
        status="in_use",
        props={"stack": [fid], "film": "PC"},
    )
    s.store.upsert_sample(fid, "flake", location="stamp_01", status="on_stamp")
    s.store.emit(
        "stack.pickup_endpoint",
        {"flake_id": fid, "stamp_id": "stamp_01", "observation": {"measurements": measurements}},
        run_id=rid,
    )


def test_named_regions_derive_exact_existing_polygons_and_exclude_gate(services):
    target = target_contact_snapshot(services.store, "chip_e01", "centre_pair")
    pattern = electrode_pattern()
    assert target["region"]["contact_pair"] == ["contact_1", "contact_4"]
    for actual, expected in zip(
        target["region"]["polygons"], (pattern[1], pattern[4]), strict=True
    ):
        assert set(map(tuple, actual["points_mm"])) == set(expected["points"])
        assert actual["kind"] == "contact"
    assert target["size_mm"] == services.store.get_sample("chip_e01")["props"]["size_mm"]
    assert target["source"] == "simulated_design"


def test_dimensions_only_legacy_remains_valid_but_cannot_claim_named_contact(services):
    g = services.store.get_sample("chip_e01")["props"]["electrode_geometry"]
    g.pop("contact_regions")
    services.store.upsert_sample("chip_e01", "chip", props={"electrode_geometry": g})
    assert target_geometry(services.store, "chip_e01")["status"] == "resolved"
    with pytest.raises(ValueError, match="dimensions alone"):
        target_contact_snapshot(services.store, "chip_e01", "centre_pair")


def test_region_changes_invalidate_existing_guidance_binding(services):
    plan = {
        "target_chip_id": "chip_e01",
        "target_snapshot": target_snapshot(services.store, "chip_e01"),
    }
    guidance_binding(services.store, plan)
    g = services.store.get_sample("chip_e01")["props"]["electrode_geometry"]
    g["contact_regions"][0]["anchor_mm"][0] = 0.001
    services.store.upsert_sample("chip_e01", "chip", props={"electrode_geometry": g})
    with pytest.raises(ValueError, match="changed"):
        guidance_binding(services.store, plan)


@pytest.mark.parametrize("receiver", [None, ["hbn"]])
def test_missing_or_layered_receiver_cannot_be_inferred_fresh(services, receiver):
    services.store.upsert_sample("chip_e01", "chip", props={"layers_top_first": receiver})
    with pytest.raises(ValueError, match="explicit fresh"):
        direct_contact_intent(services.store, "chip_e01", "centre_pair", REQUIREMENT)


def test_new_bench_preserves_explicit_configured_receiving_layers():
    from conftest import quiet_bench

    from qumus2.app import build_services

    config = quiet_bench()
    sample = next(s for s in config["initial_samples"] if s["id"] == "chip_e01")
    sample["props"]["layers_top_first"] = ["declared_intervening_layer"]
    s = build_services(":memory:", testbench_config=config, use_openai=False)
    try:
        assert s.store.get_sample("chip_e01")["props"]["layers_top_first"] == [
            "declared_intervening_layer"
        ]
        with pytest.raises(ValueError, match="explicit fresh"):
            direct_contact_intent(s.store, "chip_e01", "centre_pair", REQUIREMENT)
    finally:
        s.store.close()


@pytest.mark.parametrize("materials", [["hBN", "Gr", "hBN"], ["Gr", "hBN"], ["unknown", "Gr"]])
def test_structured_direct_intent_rejects_intervening_or_extra_flakes(materials):
    args = plan_args()
    args.layer_stack = [
        type(args.layer_stack[0])(role=f"layer{i}", material=m) for i, m in enumerate(materials)
    ] + [args.layer_stack[-1]]
    assert not validate_device_plan(args)["complete"]


def test_target_only_advisor_intent_requires_no_source_selection(services):
    ctx = ToolContext(services.store, services.jobs, "advisor", services.project_id)
    result = asyncio.run(submit_device_plan(plan_args(), ctx))
    assert result.status == "ok", result.summary
    plan = services.store.get_project(services.project_id)["state"]["device_plan"]
    assert plan["direct_contact_intent"]["sole_transferred_layer"]
    assert "source_flake_id" not in str(plan["direct_contact_intent"])
    assert plan["required_footprint"]["width_um"] == 31
    assert plan["required_footprint"]["height_um"] == 3


def test_one_shared_predicate_preserves_complete_shape_and_positive_pair_contact(public_source):
    c, gr, target = resolved(public_source)
    result = direct_contact_result(gr, target, c)
    assert result["status"] == "pass", result
    assert all(v > 0 for v in result["metrics"]["contact_overlap_um2"].values())
    assert result["binding"]["target_intent_sha256"] == c.target_intent_sha256
    assert result["metrics"]["full_shape_error_mm"] == 0


@pytest.mark.parametrize(
    "change,status",
    [
        ("one_contact", "fail"),
        ("partial", "fail"),
        ("wrong_id", "fail"),
        ("mica", "fail"),
        ("bottom_hbn", "fail"),
        ("unknown_receiver", "unresolved"),
        ("layered_receiver", "fail"),
        ("missing_observation", "unresolved"),
        ("incomplete", "unresolved"),
        ("rotated", "fail"),
        ("target_changed", "fail"),
    ],
)
def test_contact_counterexamples_fail_closed(public_source, change, status):
    c, gr, target = resolved(public_source)
    if change == "one_contact":
        gr["outline_mm"] = transform(c.source_outline_mm, xy=(0.02, 0))
    elif change == "partial":
        gr["outline_mm"] = rectangle(0.032, 0.008)
    elif change == "wrong_id":
        gr["flake_id"] = "other"
    elif change == "mica":
        gr["stamp_film"] = "mica"
    elif change == "bottom_hbn":
        gr["incoming_layers_top_first"].append({"flake_id": "hbn", "material": "hBN"})
    elif change == "unknown_receiver":
        target["receiving_layers_above_contacts"] = None
    elif change == "layered_receiver":
        target["receiving_layers_above_contacts"] = ["hbn"]
    elif change == "missing_observation":
        gr["observation_binding"] = None
    elif change == "incomplete":
        gr["outline_complete"] = False
    elif change == "rotated":
        gr["outline_mm"] = transform(c.source_outline_mm, rotation_deg=10)
    elif change == "target_changed":
        target["geometry"] = deepcopy(target["geometry"])
        target["geometry"]["region"]["anchor_mm"][0] += 0.001
    result = direct_contact_result(gr, target, c)
    assert result["status"] == status, result


def test_layout_uses_bound_existing_contacts_without_fabricated_metal(services):
    intent = direct_contact_intent(services.store, "chip_e01", "centre_pair", REQUIREMENT)
    state = {
        "selected_flakes": ["gr"],
        "device_plan": {
            "flake_roles": [{"role": "channel"}],
            "direct_contact_material": "Gr",
            "direct_contact_intent": intent,
            "contact_scheme": "existing contacts",
            "required_footprint": {"width_um": 31, "height_um": 3},
        },
    }
    args = SubmitDesignArgs(
        flake_ids=["gr"],
        shapes=[{"layer": "channel", "x_um": -15.5, "y_um": -1.5, "w_um": 31, "h_um": 3}],
    )
    assert validate_layout(state, args) == []
    args.shapes[0].x_um += 10
    assert any("both bound existing contacts" in e for e in validate_layout(state, args))
    state["device_plan"].pop("direct_contact_intent")
    assert any("contact_metal" in e for e in validate_layout(state, args))


def test_mica_and_undersized_source_are_rejected_before_job_dispatch(public_source):
    s = public_source
    params = dict(
        flake_ids=["gr"],
        target_chip_id="chip_e01",
        target_contact_region_id="centre_pair",
        direct_contact_material="Gr",
        bridge_requirement=REQUIREMENT,
        stamp_id="stamp_01",
    )
    s.store.upsert_sample("stamp_01", "stamp", props={"film": "mica"})
    before = s.store.list_runs()
    with patch.object(s.jobs, "enqueue") as enqueue:
        with pytest.raises(ValueError, match="PC/mica|requires PC"):
            s.jobs.submit("stack_device", params)
        enqueue.assert_not_called()
    assert s.store.list_runs() == before
    s.store.upsert_sample("stamp_01", "stamp", props={"film": "PC"})
    g = s.store.get_sample("gr")["props"]["optical_geometry"]
    g["outline_mm"] = [list(p) for p in rectangle(0.02, 0.03)]
    s.store.upsert_sample("gr", "flake", props={"optical_geometry": g})
    with patch.object(s.jobs, "enqueue") as enqueue:
        with pytest.raises(ValueError, match="cannot bridge"):
            s.jobs.submit("stack_device", params)
        enqueue.assert_not_called()
    assert s.store.list_runs() == before


def test_resolved_submission_binds_source_and_saved_contract(public_source):
    s = public_source
    flow = StackDeviceFlow()
    flow._bench = s.lab.bench
    p = flow.resolve_params(
        StackParams(
            flake_ids=["gr"],
            target_chip_id="chip_e01",
            direct_contact_material="Gr",
            target_contact_region_id="centre_pair",
            bridge_requirement=REQUIREMENT,
        ),
        s.store,
    )
    raw = p.model_dump(mode="json")
    StackParams.validate_saved_execution_binding(raw, store=s.store)
    assert raw["target_xy_mm"] == [
        v / 2 for v in s.store.get_sample("chip_e01")["props"]["size_mm"]
    ]
    assert raw["direct_contact_contract"]["source_flake_id"] == "gr"
    raw["stamp_film"] = "mica"
    with pytest.raises(WorkflowMismatch, match="binding"):
        StackParams.validate_saved_execution_binding(raw, store=s.store)


def test_frozen_source_image_change_is_rejected_before_dispatch(public_source):
    from pathlib import Path

    s = public_source
    c, _, _ = resolved(s)
    Path(c.source_observation["image"]).write_bytes(b"different image after prospective freeze")
    with patch.object(s.jobs, "enqueue") as enqueue:
        with pytest.raises(ValueError, match="frozen selected-source"):
            s.jobs.submit(
                "stack_device",
                {
                    "flake_ids": ["gr"],
                    "target_chip_id": "chip_e01",
                    "direct_contact_material": "Gr",
                    "target_contact_region_id": "centre_pair",
                    "bridge_requirement": REQUIREMENT,
                    "direct_contact_intent": c.target_intent,
                    "direct_contact_contract": c.model_dump(mode="json"),
                },
            )
        enqueue.assert_not_called()


def test_terminal_endpoint_never_passes_from_cached_source_without_actual_observation(
    public_source,
):
    c, _, _ = resolved(public_source)
    ctx = SimpleNamespace(
        params={"direct_contact_contract": c.model_dump(mode="json"), "stamp_id": "stamp_01"},
        store=public_source.store,
        run_id="actual",
    )
    result = _direct_contact_endpoint(ctx, {"receiving_layers_above_contacts": []}, {})
    assert result["status"] == "unresolved"


def test_private_snapshot_copies_actual_electrodes_and_unknown_legacy_stays_unknown(services):
    from qumus2.records.geometry import actual_target_contact_snapshot

    world = services.lab.world
    before = world.evaluation_snapshot()
    target = next(o for o in before["objects"] if o["id"] == "chip_e01")
    assert target["electrode_geometry"]["polygons"] == world.objects["chip_e01"].electrodes
    public = target_contact_snapshot(services.store, "chip_e01", "centre_pair")
    actual = actual_target_contact_snapshot(
        target["id"], target["size_mm"], target["electrode_geometry"], public["region"]
    )
    assert actual == public
    assert (
        actual_target_contact_snapshot(target["id"], target["size_mm"], None, public["region"])
        is None
    )
    world.objects["chip_e01"].electrodes[1]["points"][0] = (-0.016, -0.002)
    after = world.evaluation_snapshot()
    assert (
        target["electrode_geometry"]
        != next(o for o in after["objects"] if o["id"] == "chip_e01")["electrode_geometry"]
    )
    world.objects["chip_e01"].electrode_reference_only = True
    assert "electrode_geometry" not in next(
        o for o in world.evaluation_snapshot()["objects"] if o["id"] == "chip_e01"
    )


def test_tampered_source_or_target_digest_is_not_a_contract(public_source):
    c, _, _ = resolved(public_source)
    raw = c.model_dump(mode="json")
    raw["source_outline_mm"][0][0] += 0.001
    with pytest.raises(ValueError, match="source outline"):
        DirectContactContract.model_validate(raw)
    raw = c.model_dump(mode="json")
    raw["target_intent"]["target_contact"]["region"]["anchor_mm"][0] += 0.001
    with pytest.raises(ValueError, match="intent digest"):
        DirectContactContract.model_validate(raw)
    assert geometry_digest(c.model_dump(mode="json"))


def test_fixed_criterion_rejects_known_undersized_gr_without_inflating_outline(services):
    target = target_contact_snapshot(services.store, "chip_e01", "centre_pair")
    result = contact_bridge_geometry(rectangle(0.0202051, 0.0270925), target, REQUIREMENT)
    assert result["status"] == "fail"


@pytest.mark.parametrize("rotation", [0, 90])
def test_target_observation_uses_actual_public_pose_and_outline_without_private_ids(
    services, tmp_path, monkeypatch, rotation
):
    from qumus2.core.types import Pose
    from qumus2.devices.base import Frame
    from qumus2.flows.library import stacking
    from qumus2.perception.base import FlakeDetection

    size, target = [12.0, 8.0], (6.0, 4.0)
    actual = Pose(x=4, y=6)
    vx, vy = transform(((6.0, 4.0),), rotation_deg=rotation)[0]
    services.store.upsert_sample(
        "chip_e01",
        "chip",
        props={
            "size_mm": size,
            "corner_stage_xy": [actual.x + vx, actual.y + vy],
            "rotation_deg": rotation,
        },
    )
    expected = transform(rectangle(0.04, 0.008), xy=(0.001, -0.002))
    dx, dy = transform(((0.001, -0.002),), rotation_deg=rotation)[0]
    detection = FlakeDetection(
        dx_mm=dx,
        dy_mm=dy,
        area_um2=320,
        material="Gr",
        thickness_label="monolayer",
        confidence=0.9,
        meta={
            "outline_complete": True,
            "outline_mm": transform(rectangle(0.04, 0.008), rotation_deg=rotation),
            "geometry_source": "qualified_fixture",
        },
    )
    path = tmp_path / "endpoint.png"
    path.write_bytes(b"actual endpoint fixture")
    meta = {
        "focus_z": 1.0,
        "objective": 20,
        "stage_kinematics": {"public": True},
        "electrodes": "forbidden private truth",
        "flakes": "forbidden private truth",
    }
    frame = Frame("microscope", 1.0, path=str(path), meta=meta)
    stage = SimpleNamespace(get_sample_pose=lambda: actual)
    scope = SimpleNamespace(get_focus=lambda: 1.0)
    ctx = SimpleNamespace(
        store=services.store,
        run_id="actual",
        params={
            "direct_contact_contract": {"present": True},
            "target_contact_region_id": "centre_pair",
        },
        gate=SimpleNamespace(stage=lambda *a: stage, microscope=lambda *a: scope),
        verification={"refind_flake": {"centre_tolerance_um": 6}, "drop": {"stack_radius_um": 20}},
        analyzer=SimpleNamespace(find_flakes=lambda *a: [detection]),
    )
    captures = []
    monkeypatch.setattr(
        stacking, "_capture_chip_focus", lambda *a, **kw: (frame, stacking._frame_record(frame))
    )
    monkeypatch.setattr(stacking, "microscope_frame", lambda *a, **kw: captures.append(1) or frame)
    monkeypatch.setattr(stacking, "_held_source", lambda *a: {"public": True})
    monkeypatch.setattr(stacking, "_pos", lambda *a: {"z": 8.0})
    monkeypatch.setattr(stacking, "_require_source_plane", lambda *a: None)
    result = stacking._target_view(ctx, "chip_e01", target, ["Gr"], "actual", parked=True)
    assert len(captures) == 2 and len(result["frames"]) == 3
    for group in result["observed_outlines"]:
        for point, want in zip(group["outlines"][0]["outline_mm"], expected, strict=True):
            assert point == pytest.approx(want)
    assert "forbidden private truth" not in str(result)
    assert all("sim_id" not in str(group) for group in result["observed_outlines"])


@pytest.mark.parametrize(
    "change",
    ["none", "missing_actual", "shifted_actual", "layer_history", "wrong_film", "unknown_stack"],
)
def test_endpoint_uses_actual_outline_and_public_layer_continuity(
    public_source, monkeypatch, change
):
    from qumus2.flows.library import stacking

    s = public_source
    c, gr, target = resolved(s)
    rid = s.store.create_run("stack_device", {})
    mock_pickup_record(s, c, rid)
    monkeypatch.setattr(stacking, "_visual_pickup_proof", lambda *a: True)
    before = {
        "receiving_layers_above_contacts": [],
        "target_contact": target["geometry"],
        "frames": [{"sha256": "before"}],
        "ledger_event_id": s.store.last_event_id(),
    }
    after = {
        "target_chip_id": "chip_e01",
        "target_contact": target["geometry"],
        "receiving_layers_above_contacts": [],
        "frames": [{"sha256": f"actual-{i}"} for i in range(3)],
        "observed_outlines": [
            {
                "material": "Gr",
                "outlines": [
                    {
                        k: gr[k]
                        for k in ("material", "outline_mm", "outline_complete", "frame", "units")
                    }
                ],
                "observation_binding": {"image_sha256": f"actual-{i}"},
            }
            for i in range(3)
        ],
    }
    if change == "missing_actual":
        after["observed_outlines"] = []
    elif change == "shifted_actual":
        for group in after["observed_outlines"]:
            group["outlines"][0]["outline_mm"] = transform(c.source_outline_mm, xy=(0.02, 0))
    elif change == "layer_history":
        s.store.upsert_sample("chip_e01", "chip", props={"layers_top_first": ["hbn"]})
        s.store.upsert_sample("chip_e01", "chip", props={"layers_top_first": []})
    elif change == "wrong_film":
        s.store.upsert_sample("stamp_01", "stamp", props={"film": "mica"})
    elif change == "unknown_stack":
        s.store.upsert_sample("stamp_01", "stamp", props={"stack": []})
    ctx = SimpleNamespace(
        store=s.store,
        run_id=rid,
        params={"stamp_id": "stamp_01", "direct_contact_contract": c.model_dump(mode="json")},
    )
    result = stacking._direct_contact_endpoint(ctx, before, after)
    assert result["status"] == (
        "pass"
        if change == "none"
        else "fail"
        if change in ("shifted_actual", "wrong_film")
        else "unresolved"
    ), result
    # The receipt remains JSON serializable, including all independent exposures.
    import json

    json.dumps(result, allow_nan=False)
