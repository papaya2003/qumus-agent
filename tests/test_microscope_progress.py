"""Microscope field display and scan progress remain separate from verdicts."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from qumus2.core.clock import SimClock
from qumus2.core.errors import PreconditionFailed, StopRequested
from qumus2.core.types import Pose
from qumus2.devices.base import Frame
from qumus2.flows.context import FlowContext
from qumus2.flows.library import search
from qumus2.flows.step import Correction, Verdict
from qumus2.runtime.store import RunStore
from qumus2.testbench.render import MicroscopeRenderer
from qumus2.ui.recording import export_recording


def scene(**changes):
    return {"objective": 10, "fov_mm": [1.3, .86], "on_chip": "chip",
            "sharpness_chip": 10.5, "flakes": [], **changes}


def test_preview_projects_blank_travel_focus_contact_without_reference_photo(tmp_path):
    photos = tmp_path / "photos" / "graphene"
    photos.mkdir(parents=True)
    (photos / "reference.png").write_bytes(b"reference photo must not become a moving field")
    renderer = MicroscopeRenderer(tmp_path / "images", replay_dir=photos.parent, size=(160, 108))
    flake = {"sim_id": "f", "material": "Gr", "layers": 1,
             "outline_mm": [[-.2, -.1], [.2, -.1], [.1, .15]]}
    fields = [scene(), scene(on_chip=None), scene(flakes=[flake]),
              scene(flakes=[flake], sharpness_chip=.5),
              scene(stamp={"film": True}, contact={"dx_mm": 0, "dy_mm": 0, "radius_mm": .2})]
    before, rng_state = copy.deepcopy(fields), random.getstate()
    snapshots = [Path(renderer.render_preview("microscope", field, immutable=True)) for field in fields]
    assert len({p.read_bytes() for p in snapshots}) == len(fields)
    assert fields == before and random.getstate() == rng_state
    immutable = snapshots[0].read_bytes()
    live = Path(renderer.render_preview("microscope", fields[0]))
    assert renderer.render_preview("microscope", fields[2]) == str(live)
    assert live.read_bytes() == snapshots[2].read_bytes()
    assert snapshots[0].read_bytes() == immutable
    assert len(list((tmp_path / "images").glob("*.png"))) == len(fields) + 1


def test_live_polling_does_not_change_physics_random_streams_clock_or_evidence(make_services, tmp_path):
    services = make_services(image_dir=tmp_path)
    world = services.lab.world
    scope = services.lab.devices.get("microscope")
    before = (copy.deepcopy(world.snapshot()), world.rng.getstate(),
              world.observation_rng.getstate(), services.clock.now(),
              services.store.last_event_id(), scope.get_focus())
    previews = [services.lab.preview("microscope") for _ in range(8)]
    assert all(p["meta"]["purpose"] == "motion_preview" for p in previews)
    assert len({p["path"] for p in previews}) == 1
    assert before == (world.snapshot(), world.rng.getstate(), world.observation_rng.getstate(),
                      services.clock.now(), services.store.last_event_id(), scope.get_focus())


@pytest.fixture
def scan_context(monkeypatch):
    clock = SimClock()
    store = RunStore(clock=clock, bench="sim")
    rid = store.create_run("search_chip", {"chip_id": "c"})
    store.upsert_sample("c", "chip", props={"size_mm": [2, 1], "corner_stage_xy": [0, 0],
                                            "rotation_deg": 0})
    stage = SimpleNamespace(pose=Pose(x=0, y=0), moves=[], fine=(0., 0.))

    def move(pose):
        stage.pose = pose
        stage.moves.append(pose)
        clock.sleep(.1)

    stage.move_to, stage.get_pose = move, lambda: stage.pose
    stage.get_fine_position = lambda: stage.fine
    stage.get_sample_pose = lambda: stage.pose.model_copy(update={
        "x": stage.pose.x + stage.fine[0], "y": stage.pose.y + stage.fine[1]})
    microscope = SimpleNamespace(objective=10, quality=10., captures=0, on_capture=None)

    def capture():
        microscope.captures += 1
        if microscope.on_capture:
            microscope.on_capture(microscope.captures)
        return Frame("microscope", clock.now(), meta={"objective": microscope.objective,
                     "focus_z": .3, "focus_plane": "chip", "quality": microscope.quality,
                     "sample_pose": {"x": 900, "y": 900},  # scene truth is not encoder readback
                     "stage_kinematics": {"sample_pose": {"x": stage.pose.x + .02,
                                                          "y": stage.pose.y}}})

    microscope.capture = capture
    microscope.set_objective = lambda obj: setattr(microscope, "objective", obj)
    gate = SimpleNamespace(stage=lambda _: stage, microscope=lambda _: microscope)
    ctx = FlowContext(rid, store, gate, clock, None,
                      {"stations": {"transfer_stage": {"x": 0, "y": 0}},
                       "microscope": {"scan_objective": 10, "fov_mm_10x": [1, 1]}},
                      {"scan": {"overlap": 0, "max_blurred_tiles": 0, "focus_policy": "per_tile",
                                "max_corrections": 1, "refocus_every_row": False},
                       "focus": {"min_sharpness": {10: 5, 20: 5}}})
    monkeypatch.setattr(search, "autofocus", lambda *args: 10.)
    monkeypatch.setattr(search, "analyzer", lambda _: SimpleNamespace(
        sharpness=lambda f: f.meta["quality"], find_flakes=lambda f, mat: []))
    yield ctx, microscope, stage
    store.close()


def test_blank_tiles_publish_analysis_with_capture_time_and_explicit_unavailable_pixels(scan_context):
    ctx, microscope, _ = scan_context
    step = search.ScanChip("c", "Gr", 10)
    step.act(ctx, None)
    meta = ctx.store.get_run(ctx.run_id)["meta"]["microscope_scan"]
    assert meta["phase"] == "acquisition_complete" and meta["covered_tiles"] == [[0, 0], [1, 0]]
    assert meta["captures"]["0,0"]["stage_xy"] == [-.48, -.5]
    assert meta["captures"]["0,0"]["pose_source"] == "capture_readback"
    assert all(c["image_available"] is False for c in meta["captures"].values())
    frames = [e.payload["microscope_preview"] for e in ctx.store.events_after(0)
              if e.kind == "lab.frame"]
    assert len(frames) == microscope.captures == 2
    assert all(p["image"] is None and p["verification"] is False and p["purpose"] == "analysis" for p in frames)
    assert step.verify(ctx).verdict == Verdict.OK
    assert ctx.store.list_samples(kind="flake") == []


def test_interrupted_restart_reacquires_and_keeps_original_prefix(scan_context):
    ctx, microscope, stage = scan_context
    microscope.on_capture = lambda n: ctx.cancel.set() if n == 1 else None
    with pytest.raises(StopRequested):
        search.ScanChip("c", "Gr", 10).act(ctx, None)
    first = ctx.store.get_run(ctx.run_id)["meta"]["microscope_scan"]
    prefix = [e.model_dump() for e in ctx.store.events_after(0)]
    assert first["covered_tiles"] == [[0, 0]]
    ctx.cancel.clear()
    microscope.on_capture = None
    step = search.ScanChip("c", "Gr", 10)
    assert step.verify(ctx).verdict != Verdict.OK
    step.act(ctx, None)
    assert ctx.store.get_run(ctx.run_id)["meta"]["microscope_scan"]["attempt"] == 2
    assert len(stage.moves) == 3 and microscope.captures == 3
    assert [e.model_dump() for e in ctx.store.events_after(0)][:len(prefix)] == prefix


def test_blurred_tiles_are_reacquired_and_geometry_change_refuses_old_coverage(scan_context):
    ctx, microscope, _ = scan_context
    step = search.ScanChip("c", "Gr", 10)
    microscope.quality = 1
    step.act(ctx, None)
    obs = step.verify(ctx)
    assert obs.verdict == Verdict.CORRECTABLE and obs.measurements["tiles_scanned"] == 0
    correction = step.correct(ctx, obs)
    microscope.quality = 10
    step.act(ctx, correction)
    assert step.verify(ctx).verdict == Verdict.OK
    ctx.store.upsert_sample("c", "chip", props={"rotation_deg": 2})
    with pytest.raises(PreconditionFailed, match="geometry changed"):
        step.act(ctx, Correction("rescan", {"tiles": [[0, 0]]}))


def test_grid_uses_scan_objective_field_size(scan_context):
    ctx, _, _ = scan_context
    ctx.bench["microscope"]["scan_objective"] = 20
    step = search.ScanChip("c", "Gr", 10)
    assert step.expected(ctx)["tiles"] == 8


def test_scan_translation_preserves_fine_rotation_and_mechanical_height(scan_context):
    ctx, _, stage = scan_context
    stage.fine = (.1, -.2)
    stage.pose = Pose(x=0, y=0, z=.4, rotation=12)
    search._move_stage(ctx, 2, 3)
    assert stage.get_sample_pose() == Pose(x=2, y=3, z=.4, rotation=12)
    assert stage.fine == (.1, -.2)


def test_export_preserves_distinct_preview_snapshots_and_rejects_ephemeral_files(tmp_path):
    root = tmp_path / "run"
    db = root / "run.db"
    store = RunStore(db, bench="sim")
    renderer = MicroscopeRenderer(root / "images", size=(80, 60))
    paths = [renderer.render_preview("microscope", field, immutable=True)
             for field in (scene(), scene(on_chip=None))]
    for path in paths:
        store.emit("lab.frame", {"microscope_preview": {"image": path, "purpose": "preview",
                                                       "verification": False}})
    folder = export_recording(db, tmp_path / "export")
    rec = json.loads((folder / "recording.json").read_text())
    images = [e["payload"]["microscope_preview"]["image"] for e in rec["events"]]
    assert [Path(folder / image).read_bytes() for image in images] == [Path(path).read_bytes() for path in paths]
    live = renderer.render_preview("microscope", scene())
    store.emit("lab.frame", {"microscope_preview": {"image": live}})
    with pytest.raises(RuntimeError, match="immutable"):
        export_recording(db, tmp_path / "bad")
    store.close()


def test_new_missing_preview_media_fails_export_but_explicit_unknown_is_preserved(tmp_path):
    db = tmp_path / "run" / "run.db"
    store = RunStore(db, bench="sim")
    store.emit("lab.frame", {"microscope_preview": {"image": None, "purpose": "analysis",
                                                   "verification": False}})
    out = export_recording(db, tmp_path / "unknown")
    saved = json.loads((out / "recording.json").read_text())
    assert saved["events"][0]["payload"]["microscope_preview"]["image"] is None
    store.emit("lab.frame", {"microscope_preview": {"image": str(db.parent / "missing.png")}})
    with pytest.raises(RuntimeError, match="missing or outside"):
        export_recording(db, tmp_path / "missing")
    store.close()


def test_detections_near_the_chip_edge_never_become_candidates(scan_context, monkeypatch):
    # #199: v1 RGB recognition reads the lit chip edge as 40+ hBN, one strip
    # per edge tile. The pickup rule refuses any source whose centre is that
    # close to the edge, so the scan records it with the reason instead.
    from qumus2.perception.base import FlakeDetection

    ctx, _, _ = scan_context
    ctx.verification["pickup"] = {"source_chip_edge_clearance_mm": .2}

    def flakes(frame, material):
        return [FlakeDetection(dx_mm=dx, dy_mm=0, area_um2=5390, material=material,
                               thickness_label="40+", confidence=.9) for dx in (0., -.45)]

    monkeypatch.setattr(search, "analyzer", lambda _: SimpleNamespace(
        sharpness=lambda f: f.meta["quality"], find_flakes=flakes))
    step = search.ScanChip("c", "hBN", 10)
    step.act(ctx, None)
    obs = step.verify(ctx)
    assert obs.verdict == Verdict.OK
    # tile 0 sees the strip 0.03 mm from the left edge; tile 1 sees the same
    # offset 1.03 mm in, which is on-chip interior
    assert obs.measurements["candidates"] == 3 and obs.measurements["near_chip_edge_detections"] == 1
    step.on_success(ctx, obs)
    kept = [f["props"]["rel_corner_mm"] for f in ctx.store.list_samples(kind="flake")]
    assert sorted(kept) == [[.48, .5], [1.03, .5], [1.48, .5]]
    [event] = [e.payload for e in ctx.store.events_after(0) if e.kind == "microscope.scan_near_chip_edge"]
    assert event["required_mm"] == .2
    assert [d["rel_corner_mm"] for d in event["detections"]] == [[.03, .5]]
    assert event["detections"][0]["centre_edge_clearance_mm"] == pytest.approx(.03)


def test_chip_plane_focuses_three_corners_once_and_keeps_only_fields_with_material(scan_context, monkeypatch):
    """#203: one focus plane per chip; blank fields are neither saved nor shown."""
    ctx, microscope, stage = scan_context
    ctx.verification["scan"]["focus_policy"] = "chip_plane"
    ctx.store.upsert_sample("c", "chip", props={"size_mm": [3, 2]})
    microscope.focus = .3
    microscope.set_focus = lambda z: setattr(microscope, "focus", z)
    microscope.get_focus = lambda: microscope.focus
    ctx.gate.limits = SimpleNamespace(microscopes={search.MICROSCOPE: SimpleNamespace(focus=(0., 1.))})
    calls = []

    def focus(_ctx, _obj):
        # a chip tilted 0.01 mm per mm along x around Z 0.3
        microscope.focus = .3 + .01*stage.pose.x
        calls.append((stage.pose.x, stage.pose.y))
        return 10.

    monkeypatch.setattr(search, "autofocus", focus)
    saved = []

    def capture():
        microscope.captures += 1
        frame = Frame("microscope", ctx.clock.now(), meta={"objective": 10, "focus_z": microscope.focus,
                      "quality": 10., "x": stage.pose.x,
                      "stage_kinematics": {"sample_pose": {"x": stage.pose.x, "y": stage.pose.y}}})
        frame.data = b"jpeg bytes"
        frame.save = lambda: saved.append(stage.pose.x) or f"/tmp/field_{len(saved)}.jpg"
        return frame

    microscope.capture = capture
    flake = SimpleNamespace(dx_mm=0., dy_mm=0., area_um2=500., thickness_label="1L", confidence=1.,
                            meta={})
    monkeypatch.setattr(search, "analyzer", lambda _: SimpleNamespace(
        sharpness=lambda f: f.meta["quality"],
        find_flakes=lambda f, mat: [flake] if abs(f.meta["x"]) == 1.5 else []))
    step = search.ScanChip("c", "Gr", 10)
    step.act(ctx, None)
    assert len(calls) == 3  # three corner fields, then no per-field refocus
    meta = ctx.store.get_run(ctx.run_id)["meta"]["microscope_scan"]
    assert len(meta["covered_tiles"]) == 6 and meta["focus_plane_fit"]["policy"] == "chip_plane_three_corner_v1"
    # every field sits on the fitted plane
    assert all(abs(c["focus_z"] - (.3 + .01*c["stage_xy"][0])) < 1e-9 for c in meta["captures"].values())
    frames = [e.payload["microscope_preview"] for e in ctx.store.events_after(0) if e.kind == "lab.frame"]
    assert [abs(x) for x in saved] == [1.5, 1.5] and len(frames) == 2  # only the two fields with material
    assert sum(c["image_available"] for c in meta["captures"].values()) == 2
    assert step.verify(ctx).verdict == Verdict.OK
