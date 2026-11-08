"""Actual simulated microscope/transfer hooks and reactor manifest bridge."""

import math
import random
from dataclasses import replace

import pytest
from conftest import quiet_bench
from test_material_simulation import pair

from qumus2.core.clock import SimClock
from qumus2.core.polygons import area_um2, rectangle, transform
from qumus2.core.types import Pose
from qumus2.testbench.lab import build_sim_lab
from qumus2.testbench.material_adapter import catalog_placement, microscope_flake, sim_outline
from qumus2.testbench.material_flakes import flake_outline
from qumus2.testbench.material_reaction import LocalReaction
from qumus2.testbench.material_view import assembly_from_manifest, observe_pair, render_pair
from qumus2.testbench.persist import load_lab_state, save_lab_state
from qumus2.testbench.render import Canvas, MicroscopeRenderer, flake_tint
from qumus2.testbench.world import SimFlake, SimObject, SimWorld, StampState


def test_actual_microscope_projects_one_outline_across_objectives_and_rotation(tmp_path):
    clock = SimClock()
    lab = build_sim_lab(clock, seed=7, config=quiet_bench())
    scope = lab.devices.get("microscope")
    cx, cy = scope.center
    chip = lab.world.objects["chip_001"]
    chip.on, chip.pose = "transfer_stage", Pose(x=cx, y=cy, rotation=29)
    outline = flake_outline("one", 800)
    chip.flakes = [SimFlake("one", "Gr", 1, 0, 0, 800, outline)]
    observed = []
    for mag in (10, 20, 100, 20, 10):
        scope.set_objective(mag)
        view = scope.capture().meta
        f = view["flakes"][0]
        assert f["sim_id"] == "one"
        assert area_um2(f["outline_mm"]) == pytest.approx(800)
        assert f["outline_mm"] == transform(outline, rotation_deg=29)
        observed.append(f)
    assert all(f == observed[0] for f in observed)
    assert chip.flakes[0].outline_mm == outline
    state = tmp_path / "material.pkl"
    save_lab_state(lab, clock, state)
    chip.flakes.clear()
    load_lab_state(lab, clock, state)
    assert scope.capture().meta["flakes"] == [observed[0]]


def test_partial_flake_is_visible_with_center_outside_field():
    fl = SimFlake("edge", "Gr", 1, 0, 0, 1600, rectangle(0.04, 0.04))
    seen = microscope_flake(fl, (0.03, 0), (0.04, 0.04))
    assert seen and seen["visible_area_um2"] == pytest.approx(400)


def test_renderer_uses_polygon_pixels_instead_of_area_based_random_shape():
    outline = rectangle(0.04, 0.02)
    f = {"sim_id": "drawn", "material": "Gr", "layers": 1, "area_um2": 800,
         "dx_mm": 0, "dy_mm": 0, "outline_mm": outline}
    canvas = Canvas(100, 100, (0, 0, 0))
    MicroscopeRenderer._flake(canvas, f, lambda x, y: (50 + x * 1000, 50 - y * 1000),
                              1000, 1000, random.Random(1))
    pixels = [tuple(canvas.px[i:i+3]) for i in range(0, len(canvas.px), 3)]
    assert pixels.count(flake_tint("Gr", 1)) == 800
    assert pixels.count((0, 0, 0)) == 9200


def test_pickup_drop_preserve_shape_orientation_and_origin():
    world = SimWorld(seed=1, model={"stamp": {"pickup_first": {"Gr": 1}}})
    source = SimObject(id="source", kind="chip", pose=Pose(x=0, y=0, rotation=35), on="transfer_stage")
    target = SimObject(id="target", kind="chip", pose=Pose(x=0, y=0, rotation=-18), on="transfer_stage")
    world.add(source)
    world.add(target)
    original = SimFlake("stable", "Gr", 1, 0, 0, 700, flake_outline("stable", 700),
                         {"source_chip_id": "source", "batch_id": "one"})
    source.flakes = [original]
    active = [source]
    world.dome_surface = lambda *_: (active[0], 0)
    world.stage_temp = lambda: 90
    stamp = StampState(touch_z=0, apex_dx=0, apex_dy=0, melt_c=150)
    stamp.contact_history = world._contact_footprint(None, (0, 0), .1, stamp.stack, stamp.stack)
    world._release_contact(stamp, (0, 0))
    assert not source.flakes and len(stamp.stack) == 1
    carried = stamp.stack[0][0]
    assert carried.outline_mm == transform(original.outline_mm, rotation_deg=35)
    active[0] = target
    world.stage_temp = lambda: 170
    stamp.contact_history = world._contact_footprint(None, (0, 0), .1, stamp.stack, stamp.stack)
    world._release_contact(stamp, (0, 0))
    landed = target.flakes[0]
    assert landed.id == original.id and landed.origin == original.origin
    assert landed.area_um2 == original.area_um2
    for actual, expected in zip(transform(landed.outline_mm, rotation_deg=-18),
                                transform(original.outline_mm, rotation_deg=35), strict=True):
        assert actual == pytest.approx(expected)


def test_legacy_area_only_flakes_have_deterministic_nonmutating_fallback():
    fl = SimFlake("old", "Gr", 1, 0, 0, 200)
    del fl.__dict__["outline_mm"]  # emulate a pre-upgrade pickle
    del fl.__dict__["origin"]
    assert sim_outline(fl) == sim_outline(fl)
    assert area_um2(sim_outline(fl)) == pytest.approx(200)
    assert "outline_mm" not in fl.__dict__
    # The transfer path explicitly supplies absent legacy fields to replace().
    assert replace(fl, outline_mm=sim_outline(fl), origin={}).id == "old"


@pytest.mark.parametrize("clustered", [False, True])
def test_world_cleavage_and_images_replay_same_seed_without_camera_rng_effects(clustered):
    labs = [build_sim_lab(SimClock(), seed=9, config=quiet_bench()) for _ in range(2)]
    for lab in labs:
        if clustered:
            lab.world.model["exfoliation"]["morphology"] = {}
        else:
            lab.world.model["exfoliation"].pop("morphology", None)
        t = lab.world.tape
        t.material, t.massages, t.peak_temp_c, t.press_depth = "Gr", 18, 70, 0.3
        t.pressed_obj = "chip_001"
        t.pressed = {(i, j) for i in range(20) for j in range(20)}
    for _ in range(30):
        labs[0].devices.get("microscope").capture()
        labs[0].world.rng.random()
    for lab in labs:
        lab.world._exfoliate_onto(lab.world.objects["chip_001"], 150, 25)
    a, b = [lab.world.objects["chip_001"].flakes for lab in labs]
    assert a == b and len(a) > 0
    assert all(f.origin["source_chip_id"] == "chip_001" for f in a)
    assert all(math.isclose(f.area_um2, area_um2(f.outline_mm), rel_tol=1e-9) for f in a)


def test_reactor_manifest_bridge_and_observation_acceptance():
    assembly = pair()
    manifest = {"schema_version": 1, "preparation_id": assembly.id, "chip_id": "target",
                "input_flake_ids": ["A", "B"], "preparation_source": "synthetic_fixture"}
    samples = [{"id": p.flake.id, "props": {"material": p.flake.material,
                "area_um2": p.flake.area_um2, "material_placement": p.model_dump(mode="json")}}
               for p in assembly.inputs]
    assert assembly_from_manifest(manifest, samples, chip_size_mm=(10, 10)) == assembly
    reaction = LocalReaction(assembly, seed=7)
    reaction.advance(20, active=True)
    partial = observe_pair(reaction)
    assert partial["product"] is None and partial["render_product"] is not None
    assert partial["status"] == "partial" and partial["visible"]
    reaction.advance(40, active=True)
    complete = observe_pair(reaction)
    assert complete["experiment_kind"] == "two_flake_reaction"
    assert complete["preparation_id"] == assembly.id and complete["input_flake_ids"] == ["A", "B"]
    assert complete["product"]["source_flake_ids"] == ["A", "B"]
    assert complete["product"]["geometry"]["frame"] == "chip_local_mm"
    assert render_pair(complete).startswith(b"\x89PNG")
    for kwargs in ({"in_focus": False}, {"center_mm": (1, 1)}):
        hidden = observe_pair(reaction, **kwargs)
        assert not hidden["visible"] and hidden["product"] is None
    samples[0]["props"]["area_um2"] = 999999
    with pytest.raises(ValueError):
        assembly_from_manifest(manifest, samples, chip_size_mm=(10, 10))


def test_catalog_bridge_maps_identity_and_keeps_original_source_after_transfer():
    source = transform(flake_outline("catalog", 500), xy=(1, 2))
    current = transform(source, xy=(-1, -2))
    current = transform(current, rotation_deg=36)
    fl = SimFlake("physical_id", "Test", 2, 0.01, 0.02, 500, current,
                  {"source_chip_id": "source", "batch_id": "first", "source_outline_mm": source})
    placed = catalog_placement(fl, "catalog_id")
    assert placed.flake.id == "catalog_id"
    assert placed.flake.source_chip_id == "source" and placed.flake.outline_mm == source
    for actual, expected in zip(placed.outline_mm, transform(current, xy=(0.01, 0.02)), strict=True):
        assert actual == pytest.approx(expected)
    with pytest.raises(ValueError):
        catalog_placement(SimFlake("legacy", "Test", 1, 0, 0, 200), "catalog_id")


def test_completed_renderer_contains_only_one_product_not_remaining_input_flakes():
    import io

    from PIL import Image

    reaction = LocalReaction(pair(), seed=8)
    reaction.advance(60, active=True)
    view = observe_pair(reaction)
    raw = Image.open(io.BytesIO(render_pair(view))).tobytes()
    pixels = [tuple(raw[i:i+3]) for i in range(0, len(raw), 3)]
    assert (223, 187, 100) in pixels  # one gold product
    assert (81, 142, 181) not in pixels and (171, 136, 186) not in pixels
    assert view["input_geometry_role"] == "provenance_only"
    clipped = observe_pair(reaction, fov_mm=(0.01, 0.01))
    assert clipped["visible"] and clipped["product"] is None
    assert clipped["render_product"] is not None


@pytest.mark.parametrize("explicit_overlay", [False, True])
def test_clustered_runtime_overlay_uses_real_guarded_flow_and_separate_noise(tmp_path, explicit_overlay):
    from qumus2.app import build_services
    from qumus2.config import load_yaml

    cfg = quiet_bench()
    if explicit_overlay:
        cfg["exfoliation"].update(load_yaml("material-morphology.yaml")["exfoliation"])
    s = build_services(":memory:", seed=9, testbench_config=cfg)
    job = s.jobs.wait(s.jobs.submit("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"],
        "target_temp_c": 70, "num_massages": 3, "peel_speed": 150, "peel_angle_deg": 25,
        "press_depth_mm": 0.3}), 120)
    assert job.status == "succeeded", job
    chip = s.lab.world.objects["chip_001"]
    assert chip.flakes and all(f.origin["population_mode"] == "clustered_contact_regions" for f in chip.flakes)
    scope = s.lab.devices.get("microscope")
    cx, cy = scope.center
    # View setup after the guarded deposition; no further physical action implied.
    f = chip.flakes[0]
    chip.pose, chip.on = Pose(x=cx-f.x_mm, y=cy-f.y_mm), "transfer_stage"
    s.lab.world.model["microscope"]["focus_tilt_per_mm"] = 0
    scope._focus = chip.focus_offset
    frame = scope.capture()
    hardware_rng = s.lab.world.rng.getstate()
    measurement_rng = s.lab.world.observation_rng.getstate()
    for _ in range(20):
        assert s.lab.analyzer.find_flakes(frame, "Gr")
    assert s.lab.world.rng.getstate() == hardware_rng
    assert s.lab.world.observation_rng.getstate() != measurement_rng
    state = tmp_path / "clustered-state.pkl"
    save_lab_state(s.lab, s.clock, state)
    measurement_rng = s.lab.world.observation_rng.getstate()
    s.lab.world.observation_rng.random()
    load_lab_state(s.lab, s.clock, state)
    assert s.lab.world.observation_rng.getstate() == measurement_rng
    assert f.id in [seen["sim_id"] for seen in frame.meta["flakes"]]
    assert all(seen.get("outline_mm") for seen in frame.meta["flakes"])


def test_default_observation_noise_cannot_change_the_next_physical_placement():
    from qumus2.devices.base import Frame
    from qumus2.testbench.locator import SimMicroscopeAnalyzer

    worlds = [SimWorld(seed=37) for _ in range(2)]
    for w in worlds:
        w.add(SimObject(id="chip", kind="chip", pose=Pose(x=100, y=100), held_by="arm"))
    frame = Frame("microscope", 0, meta={"objective": 20, "sharpness_chip": 10.5,
        "flakes": [{"sim_id": "flake", "material": "Gr", "layers": 2, "area_um2": 500,
                    "dx_mm": 0, "dy_mm": 0}]})
    analyzer = SimMicroscopeAnalyzer(worlds[0])
    for _ in range(20):
        analyzer.find_flakes(frame, "Gr")
    assert worlds[0].rng.getstate() == worlds[1].rng.getstate()
    assert worlds[0].observation_rng.getstate() != worlds[1].observation_rng.getstate()
    for w in worlds:
        w.release("arm", Pose(x=300, y=300, z=0))
    assert worlds[0].objects["chip"].pose == worlds[1].objects["chip"].pose


def test_legacy_checkpoint_keeps_its_noise_semantics_without_rewriting_it(tmp_path):
    import pickle

    from qumus2.testbench.persist import lab_state

    clock = SimClock()
    lab = build_sim_lab(clock, seed=3, config=quiet_bench())
    state = lab_state(lab, clock)
    state["world"].pop("_observation_rng")
    state["world"].pop("_legacy_observation_rng")
    path = tmp_path / "old-state.pkl"
    original = pickle.dumps(state)
    path.write_bytes(original)
    assert load_lab_state(lab, clock, path)
    assert lab.world.observation_rng is lab.world.rng
    assert path.read_bytes() == original
    fresh = build_sim_lab(SimClock(), seed=3, config=quiet_bench())
    assert fresh.world.observation_rng is not fresh.world.rng
    explicit = SimWorld(seed=3, model={"legacy_shared_observation_rng": True})
    assert explicit.observation_rng is explicit.rng
