"""Independent source refind/identity contracts retained after servo retirement."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from qumus2.core.errors import NeedsHumanError
from qumus2.core.polygons import area_um2
from qumus2.flows.library.primitives import autofocus, microscope_frame, observed_outline_pixels
from qumus2.flows.library.stacking import RefindFlake
from qumus2.flows.step import StepRunner, Verdict
from qumus2.testbench.render import MicroscopeRenderer
from tests.test_focus_evidence import focus_context  # noqa: F401


@pytest.fixture
def source_case(focus_context, tmp_path):  # noqa: F811
    s, ctx, raw = focus_context
    from qumus2.flows.library.stacking import PROCEDURE_ID, TRANSFER_PROFILE_ID
    ctx.params.update(procedure_contract=PROCEDURE_ID, transfer_profile=TRANSFER_PROFILE_ID)
    raw.renderer = MicroscopeRenderer(tmp_path, size=(240, 160))
    # No stamp here, so no grip takes the holder up to its corner standby
    # (#181): put it there, out of the parked source optical path.
    ctx.gate.manipulator("transfer_arm").move_axis_to("y", ctx.bench["transfer_arm"]["park"]["y"])
    # The inherited focus-only fixture intentionally has approximate area.
    # This source-geometry fixture needs the exact area of its existing polygon.
    source = s.lab.world.objects["chip_001"].flakes[0]
    source.area_um2 = area_um2(source.outline_mm)
    autofocus(ctx, 20, attempts=1, half_range=.3)
    original = microscope_frame(ctx, phase="original_native20_source")
    detection = ctx.analyzer.find_flakes(original, "Gr")[0]
    catalogue = s.store.create_run("search_chip", {}, project_id=s.project_id)
    s.store.set_run_status(catalogue, "succeeded")
    s.store.upsert_sample("chip_001", "chip", props={"corner_stage_xy": [0, 80], "rotation_deg": 0})
    s.store.upsert_sample("source", "flake", location="chip_001", status="catalogued", props={
        "material": "Gr", "chip_id": "chip_001", "rel_corner_mm": [0, 0], "area_um2": detection.area_um2,
        "images": {"20x": original.saved_path()},
        "optical_geometry": {"source_chip_id": "chip_001", "job_id": catalogue},
        "tracking_references": {"20x": {"image": original.saved_path(), "timestamp": original.timestamp,
            "polygon_px": observed_outline_pixels(original, detection), "job_id": catalogue,
            "meta": {k: deepcopy(original.meta.get(k)) for k in
                     ("objective", "focus_z", "focus_plane", "fov_mm", "calibration")}}}})
    return s, ctx, raw, original


def test_refind_preserves_original_named_source_pixels_and_applies_zero_default_shift(source_case):
    s, ctx, _, original = source_case
    before = Path(original.saved_path()).read_bytes()
    step = RefindFlake("source", "chip_001", "Gr")
    result = StepRunner().run(step, ctx, 0)
    assert result.ok, result
    reference = s.store.get_run(ctx.run_id)["meta"]["normal_transfer"]["source_references"]["source"]
    assert reference["source_identity"]["original"]["image"] == original.saved_path()
    assert reference["source_identity"]["design_shift_chip_mm"] == [0., 0.]
    assert Path(original.saved_path()).read_bytes() == before


def test_deleted_selected_source_cannot_be_replaced_by_camera_background(source_case):
    s, ctx, _, original = source_case
    before = Path(original.saved_path()).read_bytes()
    s.lab.world.objects["chip_001"].flakes.clear()
    result = RefindFlake("source", "chip_001", "Gr").verify(ctx)
    assert result.verdict == Verdict.UNCLEAR and result.measurements["found"] is False
    assert Path(original.saved_path()).read_bytes() == before


def test_refind_correction_includes_nonzero_fine_xy_in_sample_pose(source_case, monkeypatch):
    s, ctx, _, _ = source_case
    source = s.lab.world.objects["chip_001"].flakes[0]
    source.x_mm, source.y_mm = -.09, .185
    stage = ctx.gate.stage("transfer_stage")
    stage.move_fine_to(.1, -.2)
    step = RefindFlake("source", "chip_001", "Gr")
    nearest, detections = step._nearest, []
    def observed(context, images=None):
        detection = nearest(context, images)
        detections.append(detection)
        return detection
    monkeypatch.setattr(step, "_nearest", observed)
    observation = step.verify(ctx)
    detection = detections[0]
    assert detection is not None and observation.verdict == Verdict.CORRECTABLE
    sample, coarse = stage.get_sample_pose(), stage.get_pose()
    assert stage.get_fine_position() == pytest.approx((.1, -.2), abs=.003)
    expected = [round(sample.x-detection.dx_mm, 4), round(sample.y-detection.dy_mm, 4)]
    assert observation.measurements["stage_xy"] == expected
    assert expected != [round(coarse.x-detection.dx_mm, 4), round(coarse.y-detection.dy_mm, 4)]


@pytest.mark.parametrize("changed", ["objective", "profile", "identity"])
def test_original_source_binding_cannot_relabel_changed_data_or_move_holder(source_case, monkeypatch, changed):
    s, ctx, _, original = source_case
    before = Path(original.saved_path()).read_bytes()
    sample = s.store.get_sample("source")
    reference = deepcopy(sample["props"]["tracking_references"]["20x"])
    if changed == "objective":
        reference["meta"]["objective"] = 100
    elif changed == "profile":
        reference["meta"]["calibration"]["photometry_profile"]["raster"] = "old-raster"
    else:
        reference["job_id"] = "another-source"
    s.store.upsert_sample("source", "flake", props={"tracking_references": {"20x": reference}})
    monkeypatch.setattr(s.lab.devices.get("transfer_arm"), "move_axis_to",
                        lambda *args, **kwargs: pytest.fail("Source rejection cannot move holder"))
    with pytest.raises(NeedsHumanError):
        RefindFlake("source", "chip_001", "Gr").verify(ctx)
    assert Path(original.saved_path()).read_bytes() == before


def test_refind_optional_shift_is_declared_and_applied_after_centering(source_case):
    s, ctx, _, _ = source_case
    step = RefindFlake("source", "chip_001", "Gr", design_shift_mm=(.01, -.02))
    result = StepRunner().run(step, ctx, 0)
    assert result.ok, result
    reference = s.store.get_run(ctx.run_id)["meta"]["normal_transfer"]["source_references"]["source"]
    assert reference["source_identity"]["design_shift_chip_mm"] == [.01, -.02]
    stage = ctx.gate.stage("transfer_stage").get_sample_pose()
    centred = s.store.get_sample("source")["props"]["stage_xy"]
    assert [stage.x-centred[0], stage.y-centred[1]] == pytest.approx([.01, -.02], abs=.003)


def test_identifiable_real_pixel_remnant_cannot_authorize_swap_or_transfer_ledger(source_case, tmp_path, monkeypatch):
    from PIL import Image, ImageChops, ImageDraw

    import qumus2.flows.library.stacking as module
    from qumus2.devices.base import Frame
    from qumus2.flows.step import StepOutcome, StepStatus
    from tests.test_v1_transfer_sequence import bound_params

    s, ctx, _, original = source_case
    reference = s.store.get_sample("source")["props"]["tracking_references"]["20x"]
    before = Path(original.saved_path()).read_bytes()
    intact = Image.open(original.saved_path()).convert("RGB")
    remnant = intact.copy()
    vx, vy = max(reference["polygon_px"], key=lambda point: point[0])
    substrate = intact.getpixel((min(int(vx)+8, intact.width-1), int(vy)))
    ImageDraw.Draw(remnant).rectangle((int(vx)-4, int(vy)-3, int(vx)+3, int(vy)+3), fill=substrate)
    assert ImageChops.difference(intact, remnant).getbbox()
    path = tmp_path/"retained-source-remnant.png"
    remnant.save(path)
    current = Frame("microscope", ctx.clock.now(), path=str(path), meta=deepcopy(original.meta))
    # Exercise the real production matcher: a small missing piece still leaves
    # an identifiable original. No match result or completeness flag is faked.
    identity = module.RefindFlake("source", "chip_001", "Gr")._source_identity(ctx, current)
    assert identity["quality"] >= ctx.verification["vision_control"]["min_match_quality"]
    assert identity["current"]["sha256"] != identity["original"]["sha256"]

    step = module.PickUpFlake("source", "stamp_01", "Gr")
    ctx.params["flake_chips"] = {"source": "chip_001"}
    s.store.update_run_meta(ctx.run_id, normal_transfer={
        "source_references": {"source": module._frame_record(original)},
        "phases": {step.name: {"name": step.name, "complete": True, "coverage": {"covered": True}}}})
    def source_remnant(context, material, radius, images, *, frames, source_reference):
        images.append(str(path))
        frames.append(current)
        return [object()]  # The source-presence path runs the real matcher above again.
    monkeypatch.setattr(module, "_chip_near_centre", source_remnant)
    observed = step.verify(ctx)
    assert observed.verdict == Verdict.UNCLEAR
    assert observed.measurements["no_transfer_confirmed"] is None
    assert observed.measurements["transfer_confirmed"] is False
    outcome = StepOutcome(StepStatus.NEEDS_AGENT, step.name, 1, 0, [observed])
    assert module.StackDeviceFlow().on_step_failed(bound_params(), step, outcome, ctx) is None
    assert s.store.get_sample("source")["status"] == "catalogued"
    assert s.store.get_sample("source")["location"] == "chip_001"
    assert not s.store.list_samples(kind="device")
    assert Path(original.saved_path()).read_bytes() == before


def test_native_focus_binding_is_acquired_after_final_design_shift(source_case):
    s, ctx, _, original = source_case
    source_before = Path(original.saved_path()).read_bytes()
    step = RefindFlake('source', 'chip_001', 'Gr', design_shift_mm=(.008, -.006))
    outcome = StepRunner().run(step, ctx, 0)
    assert outcome.ok, outcome.message
    reference = s.store.get_run(ctx.run_id)['meta']['normal_transfer']['source_references']['source']
    binding = reference['focus_binding']
    events = s.store.events_after(0, limit=10000, run_id=ctx.run_id)
    moves = [e for e in events if e.kind == 'device.command'
             and e.payload.get('device') == 'transfer_stage' and e.payload.get('command') == 'move_to']
    assert moves and binding['event_id'] > max(e.id for e in moves)
    assert binding['observation']['stage_kinematics'] == reference['meta']['stage_kinematics']
    assert reference['meta']['stage_kinematics'] == ctx.gate.stage('transfer_stage').kinematics()
    assert binding['capture_event_id'] > binding['event_id']
    assert Path(original.saved_path()).read_bytes() == source_before
    assert reference['source_identity']['original']['image'] == original.saved_path()
