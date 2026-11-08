"""Autofocus retains observed axial bounds without consulting scene truth."""

from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

from qumus2.app import build_services
from qumus2.core.errors import NeedsHumanError
from qumus2.core.types import Pose
from qumus2.devices.base import Frame, Microscope
from qumus2.flows.library.primitives import autofocus
from qumus2.testbench.render import MicroscopeRenderer
from qumus2.testbench.world import SimFlake
from tests.conftest import quiet_bench
from tests.test_v1_transfer_sequence import bound_params, motion_case  # noqa: F401


@pytest.fixture
def focus_context():
    cfg = quiet_bench(stage__sigma_mm=0)
    cfg["microscope"]["focus_tilt_per_mm"] = 0
    services = build_services(":memory:", seed=1, testbench_config=cfg)
    run_id = services.store.create_run("focus_evidence_fixture", {})
    ctx = services.jobs.context_factory(run_id, {}, services.project_id)
    ctx.gate.stage("transfer_stage").move_to(Pose(x=0, y=80))
    chip = services.lab.world.objects["chip_001"]
    chip.pose, chip.on, chip.held_by = Pose(x=740, y=420), "transfer_stage", None
    chip.focus_offset = .07
    chip.flakes = [SimFlake("texture", "Gr", 60, 0, 0, 15000,
                           ((-.06, -.04), (.04, -.05), (.07, .01), (0, .05), (-.05, .03)))]
    scope = ctx.gate.microscope("microscope")
    scope.set_objective(20)
    scope.set_focus(0)
    raw = services.lab.devices.get("microscope")
    raw.renderer.size = (240, 160)
    yield services, ctx, raw
    services.store.close()


def last_focus(services, ctx):
    observation = services.store.get_run(ctx.run_id)["meta"]["last_focus_observation"]
    events = [event for event in services.store.events_after(0, limit=100000)
              if event.kind == "microscope.focus"]
    assert events[-1].payload == observation
    json.dumps(observation, allow_nan=False)
    return observation


def test_actual_renderer_bracket_binds_captured_pixels_and_public_poses(focus_context, monkeypatch):
    services, ctx, raw = focus_context
    original_capture = raw.capture
    frames = []

    def misleading_metadata():
        frame = original_capture()
        # Pixels were already formed. Contradictory diagnostic truth must not
        # move the focus to these asserted oracle coordinates.
        frame.meta.update(best_focus_z=99., best_focus_chip=-99., sharpness=100.)
        frames.append(frame)
        return frame

    monkeypatch.setattr(raw, "capture", misleading_metadata)
    before = ctx.clock.now()
    quality = autofocus(ctx, 20, half_range=.3)
    observation = last_focus(services, ctx)
    assert isinstance(quality, float)
    assert quality == ctx.analyzer.sharpness(frames[-1])
    assert observation["focus_interval_mm"][0] <= .07 <= observation["focus_interval_mm"][1]
    assert observation["focus_interval_mm"][0] <= raw.get_focus() <= observation["focus_interval_mm"][1]
    assert observation["readback_focus_z_mm"] == observation["final_focus_readback_z_mm"] == raw.get_focus() == frames[-1].meta["focus_z"]
    assert observation["objective"] == observation["objective_readback"] == 20
    assert observation["verification"] is False
    assert observation["focus_axis_calibration"]["source"] == "synthetic"
    assert observation["focus_axis_calibration"]["simulation_only"] is True
    assert observation["focus_axis_calibration"]["comparison_scope"] == "same_objective_only"
    assert len(observation["sampled_passes"]) == 2
    captured_samples = frames[1:10] + frames[11:20]
    samples = [sample for record in observation["sampled_passes"] for sample in record["samples"]]
    for sample, frame in zip(samples, captured_samples, strict=True):
        assert sample["image_sha256"] == hashlib.sha256(frame.data).hexdigest()
        assert sample["captured_at"] == frame.timestamp
        assert before <= sample["captured_at"] <= ctx.clock.now()
        assert sample["requested_focus_z_mm"] == sample["readback_focus_z_mm"]
        assert sample["optical_calibration"] == frame.meta["calibration"]
        assert sample["stage_kinematics"] == ctx.gate.stage("transfer_stage").kinematics()
        assert sample["holder_pose"] == ctx.gate.manipulator("transfer_arm").get_positions()
        assert frame.path is None  # Numeric curves retain no sweep PNG files.
    frozen = deepcopy(observation)
    frames[1].meta["calibration"]["id"] = "later mutation"
    raw.renderer.size = (120, 80)
    assert last_focus(services, ctx) == frozen


def textured_frame(blur):
    image = Image.new("RGB", (160, 144), (125, 102, 171))
    draw = ImageDraw.Draw(image)
    draw.polygon([(40, 25), (106, 37), (120, 92), (75, 118), (30, 80)], fill=(30, 25, 45))
    draw.polygon([(54, 45), (98, 55), (70, 91)], fill=(230, 210, 240))
    draw.line((35, 75, 106, 37), fill=(240, 230, 250), width=3)
    stream = io.BytesIO()
    image.filter(ImageFilter.GaussianBlur(blur)).save(stream, format="PNG")
    return stream.getvalue()


def curve_capture(ctx, raw, monkeypatch, blur):
    calibration = deepcopy(raw.capture().meta["calibration"])

    def capture():
        return Frame("microscope", ctx.clock.now(), data=textured_frame(blur(raw.get_focus())),
                     meta={"calibration": calibration, "sharpness": 100.,
                           "sharpness_chip": 100., "best_focus_z": 500.})

    monkeypatch.setattr(raw, "capture", capture)


@pytest.mark.parametrize("curve", ["flat", "edge", "multiple"])
def test_flat_edge_and_unresolved_raster_peaks_supply_no_interval(focus_context, monkeypatch, curve):
    services, ctx, raw = focus_context
    blur = {"flat": lambda z: 0., "edge": lambda z: max(0., (.4-z)*20),
            "multiple": lambda z: min(abs(z-.15), abs(z+.15))*70}[curve]
    curve_capture(ctx, raw, monkeypatch, blur)
    quality = autofocus(ctx, 20, half_range=.3)
    observation = last_focus(services, ctx)
    assert quality >= 5.5  # An analyzer's high sharpness cannot create a bracket.
    assert observation["focus_interval_mm"] is None
    assert observation["interval_pass_index"] is None
    if curve == "multiple":
        assert any(record["unresolved_peaks"] for record in observation["sampled_passes"])


def test_unbracketed_fine_plateau_retains_observed_coarse_bound(focus_context, monkeypatch):
    services, ctx, raw = focus_context
    curve_capture(ctx, raw, monkeypatch, lambda z: max(0., abs(z)-.08)*70)
    autofocus(ctx, 20, half_range=.3)
    observation = last_focus(services, ctx)
    coarse, fine = observation["sampled_passes"]
    assert coarse["bracket_mm"] is not None and fine["bracket_mm"] is None
    assert observation["focus_interval_mm"] == coarse["bracket_mm"]
    assert observation["interval_pass_index"] == 0
    assert fine["samples"][0]["readback_focus_z_mm"] > observation["focus_interval_mm"][0]
    assert fine["samples"][-1]["readback_focus_z_mm"] < observation["focus_interval_mm"][1]


def test_unknown_axial_calibration_keeps_curve_but_no_interval(focus_context, monkeypatch):
    services, ctx, raw = focus_context
    monkeypatch.setattr(raw, "focus_axis_calibration", lambda objective: None)
    autofocus(ctx, 20, half_range=.3)
    observation = last_focus(services, ctx)
    assert observation["focus_axis_calibration"] is None
    assert observation["focus_interval_mm"] is None
    assert any(record["bracket_mm"] is not None for record in observation["sampled_passes"])
    assert Microscope.focus_axis_calibration(raw, 20) is None


def test_declared_focus_readback_error_expands_sampled_bracket(focus_context, monkeypatch):
    services, ctx, raw = focus_context
    original_readback, original_calibration = raw.get_focus, raw.focus_axis_calibration
    monkeypatch.setattr(raw, "get_focus", lambda: original_readback()+.004)

    def calibration(objective):
        return {**original_calibration(objective), "readback_error_mm": .003}

    monkeypatch.setattr(raw, "focus_axis_calibration", calibration)
    autofocus(ctx, 20, half_range=.3)
    observation = last_focus(services, ctx)
    selected = observation["sampled_passes"][observation["interval_pass_index"]]
    assert observation["focus_interval_mm"] == pytest.approx(
        [selected["bracket_mm"][0]-.003, selected["bracket_mm"][1]+.003])
    assert observation["readback_focus_z_mm"] == pytest.approx(observation["focus_z"]+.004)
    assert all(sample["readback_focus_z_mm"] == pytest.approx(sample["requested_focus_z_mm"]+.004)
               for record in observation["sampled_passes"] for sample in record["samples"])


def test_opt_in_retains_only_final_capture_for_exact_pixel_identity(focus_context, monkeypatch, tmp_path):
    services, ctx, raw = focus_context
    raw.renderer = MicroscopeRenderer(tmp_path, size=(240, 160))
    capture, frames = raw.capture, []

    def collect():
        frame = capture()
        frames.append(frame)
        return frame

    monkeypatch.setattr(raw, "capture", collect)
    autofocus(ctx, 20, half_range=.3, retain_final_image=True)
    observation = last_focus(services, ctx)
    assert observation["image"] == frames[-1].path
    assert Path(observation["image"]).read_bytes() == frames[-1].data
    assert observation["image_sha256"] == hashlib.sha256(frames[-1].data).hexdigest()
    assert observation["captured_at"] == frames[-1].timestamp
    assert observation["fov_mm"] == frames[-1].meta["fov_mm"]
    assert all(frame.path is None for frame in frames[:-1])


def test_changed_final_readback_cannot_relabel_a_prior_capture(focus_context, monkeypatch):
    import qumus2.flows.library.primitives as module

    services, ctx, raw = focus_context
    freeze, bindings = module._focus_capture_binding, 0
    last_binding = ctx.verification["focus"]["coarse_points"] + ctx.verification["focus"]["fine_points"] + 3

    def displaced_after_binding(*args):
        nonlocal bindings
        binding = freeze(*args)
        bindings += 1
        if bindings == last_binding:
            ctx.gate.microscope("microscope").set_focus(binding["readback_focus_z_mm"]+.01)
        return binding

    monkeypatch.setattr(module, "_focus_capture_binding", displaced_after_binding)
    autofocus(ctx, 20, half_range=.3)
    observation = last_focus(services, ctx)
    assert observation["final_focus_readback_z_mm"] == raw.get_focus()
    assert observation["readback_focus_z_mm"] != observation["final_focus_readback_z_mm"]
    assert observation["focus_interval_mm"] is None
    assert observation["interval_pass_index"] is None


@pytest.mark.parametrize("attempts", [0, 1])
def test_final_af_exposure_precedes_no_further_axis_command(focus_context, monkeypatch, tmp_path, attempts):
    import math

    services, ctx, raw = focus_context
    scope = ctx.gate.microscope("microscope")
    # Fresh real Gate/Clock interpolation reproduces the public one-ULP
    # non-idempotence; no old recording, fabricated readback or image is used.
    scope.set_focus(.08750000000000009)
    target = .02083333333333343
    scope.set_focus(target)
    first = scope.get_focus()
    scope.set_focus(target)
    assert scope.get_focus()-first == math.ulp(target)
    raw.renderer = MicroscopeRenderer(tmp_path, size=(240, 160))
    capture, move = raw.capture, raw.set_focus
    frames, trace, retained = [], [], []
    def collect():
        frame = capture()
        frames.append(frame)
        trace.append(("capture", frame))
        return frame
    def record_move(z):
        move(z)
        trace.append(("move", z, raw.get_focus()))
    monkeypatch.setattr(raw, "capture", collect)
    monkeypatch.setattr(raw, "set_focus", record_move)
    mark = services.store.last_event_id()
    autofocus(ctx, 20, attempts=attempts, retain_final_image=True, final_frames=retained)
    observation = last_focus(services, ctx)
    assert trace[-1][0] == "capture"
    commands = [e.payload["command"] for e in services.store.events_after(mark, limit=10000)
                if e.kind == "device.command" and e.payload["device"] == "microscope"]
    assert commands[-1] == "capture"
    frame = frames[-1]
    assert retained == [frame]
    assert observation["readback_focus_z_mm"] == observation["final_focus_readback_z_mm"] == scope.get_focus() == frame.meta["focus_z"]
    assert observation["image_sha256"] == hashlib.sha256(frame.data).hexdigest()
    assert Path(observation["image"]).read_bytes() == frame.data
    assert observation["captured_at"] == frame.timestamp
    points = ctx.verification["focus"]["coarse_points"] + ctx.verification["focus"]["fine_points"]
    assert len(frames) == (points+3 if attempts else 1)
    assert sum(entry[0] == "move" for entry in trace) == (points+2 if attempts else 0)
    if attempts:
        assert observation["focus_z"] == next(entry[1] for entry in reversed(trace) if entry[0] == "move")
        assert observation["focus_interval_mm"][0] <= scope.get_focus() <= observation["focus_interval_mm"][1]
    else:
        assert observation["focus_interval_mm"] is None  # No sweep cannot invent a bracket.


def test_actual_context_change_before_final_binding_refuses_interval(focus_context):
    services, ctx, _ = focus_context
    frames = []
    final_capture = ctx.verification["focus"]["coarse_points"] + ctx.verification["focus"]["fine_points"] + 3
    def change_context(frame):
        frames.append(frame)
        if len(frames) == final_capture:
            ctx.gate.stage("transfer_stage").move_fine_to(.001, 0)
    autofocus(ctx, 20, half_range=.3, on_capture=change_context)
    observation = last_focus(services, ctx)
    assert frames[-1].meta["stage_kinematics"] != observation["stage_kinematics"]
    assert observation["stage_kinematics"] == ctx.gate.stage("transfer_stage").kinematics()
    assert observation["focus_interval_mm"] is observation["interval_pass_index"] is None


@pytest.mark.parametrize(("field", "value"), [
    ("scale_mm_per_focus_mm", float("inf")), ("scale_mm_per_focus_mm", 0.),
    ("mapping_error_mm", float("nan")), ("readback_error_mm", -1.),
    ("readback_error_mm", True), ("objective", 100), ("objective", 20.),
    ("comparison_scope", "cross_objective"), ("calibration_id", "unknown"),
    ("source", "unknown"), ("simulation_only", False),
])
def test_guarded_axis_calibration_refuses_invalid_declarations(focus_context, monkeypatch, field, value):
    _, ctx, raw = focus_context
    declaration = raw.focus_axis_calibration(20)
    declaration[field] = value
    monkeypatch.setattr(raw, "focus_axis_calibration", lambda objective: declaration)
    assert ctx.gate.microscope("microscope").focus_axis_calibration(20) is None


def test_guarded_axis_calibration_copies_provenance_and_refuses_synthetic_on_real(focus_context, monkeypatch):
    _, ctx, raw = focus_context
    declaration = raw.focus_axis_calibration(20)
    monkeypatch.setattr(raw, "focus_axis_calibration", lambda objective: declaration)
    scope = ctx.gate.microscope("microscope")
    accepted = scope.focus_axis_calibration(20)
    assert accepted == declaration and accepted is not declaration
    declaration["readback_error_mm"] = .02
    assert accepted["readback_error_mm"] == 0.
    monkeypatch.setattr(raw, "is_simulated", False)
    assert scope.focus_axis_calibration(20) is None


@pytest.fixture
def native_reference(focus_context, tmp_path, request, monkeypatch):  # noqa: F811
    from qumus2.flows.library.stacking import _capture_chip_focus
    services, ctx, raw = focus_context
    services.store.set_run_status(ctx.run_id, 'succeeded')
    case = getattr(getattr(request.node, 'callspec', None), 'params', {})
    order = case.get('witness_order', ['first'])
    params = bound_params(flake_ids=order, flake_chips={fid: 'chip_001' for fid in order},
        materials={fid: 'Gr' for fid in order}, stamp_film=case.get('film', 'PC'),
        target_chip_id='chip_001', target_xy_mm=(0., 0.), home_slots={'chip_001': 'chip_home'}).model_dump()
    run_id = services.store.create_run('stack_device', params)
    ctx = services.jobs.context_factory(run_id, params, services.project_id)
    _record_stamp_load(ctx)
    raw.renderer = MicroscopeRenderer(tmp_path, size=(160, 144))
    if getattr(request, 'param', None):
        calibration = raw.focus_axis_calibration
        monkeypatch.setattr(raw, 'focus_axis_calibration', lambda objective:
            {**calibration(objective), **request.param})
    frame, reference = _capture_chip_focus(ctx, 'refind_first_source_reference', parked=True)
    reference.update(source_id='first', source_chip_id='chip_001', target_offset_mm=[0., 0.])
    capture = {k: deepcopy(reference[k]) for k in ('image', 'sha256', 'captured_at', 'meta')}
    reference['source_identity'] = {'source_id': 'first', 'source_chip_id': 'chip_001',
        'original': capture, 'current': deepcopy(capture), 'post_shift_capture': deepcopy(capture)}
    return services, ctx, raw, frame, reference


def _record_stamp_load(ctx):
    """Controlled handoff observation, actual guarded grip; no physical-load claim."""
    import qumus2.flows.library.stacking as module
    from qumus2.flows.step import Observation, StepRunner, Verdict
    store, rid = ctx.store, ctx.run_id
    props = {'film': 'PC'}
    if ctx.params['stamp_film'] == 'mica':
        support = {'film_type': 'mica', 'source': 'operator_report', 'instruction_id': 'support_fixture', 'ts': ctx.clock.now()}
        props.update(support=support, support_history=[support])
    store.upsert_sample('stamp', 'stamp', location=ctx.params['stamp_slot'], status='fresh', props=props)
    store.begin_step(rid, 100, 'load_stamp')
    observed = Observation(Verdict.OK, {'found': True}, {'sample': 'stamp', 'at': module.HANDOFF})
    store.emit('step.verified', {'seq': 100, 'name': 'load_stamp', **observed.to_dict()}, run_id=rid)
    store.upsert_sample('stamp', 'stamp', location=module.HANDOFF)
    store.finish_step(rid, 100, 'done', measurements=observed.measurements)
    assert StepRunner().run(module.GripStamp('stamp'), ctx, 101).ok


def _original_pickup_witness(services, ctx, raw, tmp_path, monkeypatch, *, first_depth=.08, partial=False):
    """Real shared pickup/Gate/camera/verified producer with controlled raster input."""
    import qumus2.flows.library.stacking as module
    from qumus2.flows.step import StepRunner
    for index, fid in enumerate(ctx.params['flake_ids']):
        if index:
            _, source = module._capture_chip_focus(ctx, f'refind_{fid}_source_reference', parked=True)
            source.update(source_id=fid, source_chip_id='chip_001', target_offset_mm=[0., 0.])
            capture = {k: deepcopy(source[k]) for k in ('image', 'sha256', 'captured_at', 'meta')}
            source['source_identity'] = {'source_id': fid, 'source_chip_id': 'chip_001',
                'original': capture, 'current': deepcopy(capture), 'post_shift_capture': deepcopy(capture)}
            state = module._transfer_state(ctx)
            state['source_references'][fid] = source
            module._save_transfer(ctx, state)
        module.InitializeStamp('stamp', f'v1_initialize_{fid}').act(ctx, None)
        if partial:
            ctx.gate.manipulator(module.MANIP).move_axis_to('z', 6.55)
        state = module._transfer_state(ctx)
        process = module._process_reference(state, fid, 'stamp', ctx.run_id, .2, services.store)
        threshold = process['reference_interval_mm'][1]-(first_depth if index == 0 else .11)
        focus = state['source_references'][fid]['meta']['focus_z']+ctx.verification['transfer_sequence']['focus_offset_mm']
        capture, count = raw.capture, [0]
        name = f'v1_pick_up_{fid}'
        def camera(capture=capture, focus=focus, name=name, threshold=threshold, fid=fid, count=count):
            frame = capture()
            image = (Image.new('RGB', (160, 144), (125, 102, 171)) if raw.get_objective() == 5 else
                Image.open(io.BytesIO(textured_frame(min(8., abs(raw.get_focus()-focus)*70)))).convert('RGB'))
            phase = module._transfer_state(ctx).get('phases', {}).get(name, {})
            if (raw.get_objective() == 5 and phase.get('pending_intent')
                    and (phase.get('final_partial_macro') if partial else module._pos(ctx)['z'] < threshold)):
                ImageDraw.Draw(image).ellipse((40, 32, 120, 112), fill=(245, 245, 245))
            stream = io.BytesIO()
            image.save(stream, format='PNG')
            frame.data, frame.save = stream.getvalue(), None
            frame.meta.update(sharpness=8.5, sharpness_chip=8.5, focus_plane='chip')
            frame.path = str(tmp_path/f'original-witness-{fid}-{count[0]}.png')
            Path(frame.path).write_bytes(frame.data)
            count[0] += 1
            return frame
        with monkeypatch.context() as patch:
            patch.setattr(raw, 'capture', camera)
            patch.setattr(ctx.analyzer, 'find_flakes', lambda *args: [])
            outcome = StepRunner().run(module.PickUpFlake(fid, 'stamp', 'Gr'), ctx, 102+index)
            assert outcome.ok, outcome.message
        module.ManipulatorTo('park', name=f'park_stamp_{fid}', stamp_id='stamp').act(ctx, None)


def test_native_source_focus_records_actual_events_without_sample_duplication(native_reference):
    from qumus2.flows.library.stacking import _validate_focus_reference
    services, ctx, raw, frame, reference = native_reference
    binding = reference['focus_binding']
    assert 'sampled_passes' not in binding['observation']
    assert binding['observation']['focus_interval_mm'][0] <= raw.get_focus() <= binding['observation']['focus_interval_mm'][1]
    _validate_focus_reference(reference, ctx.run_id, parked=True, store=services.store)
    event = services.store.events_after(binding['event_id']-1, limit=1)[0]
    assert event.kind == 'microscope.focus' and len(event.payload['sampled_passes']) == 2
    capture = services.store.events_after(binding['capture_event_id']-1, limit=1)[0]
    assert capture.payload['microscope_preview']['image'] == frame.saved_path()
    assert hashlib.sha256(Path(frame.saved_path()).read_bytes()).hexdigest() == reference['sha256']


@pytest.mark.parametrize('fault', ['stage', 'axis', 'interval', 'event', 'image', 'path'])
def test_native_source_binding_rejects_changed_or_missing_historical_proof(native_reference, fault):
    from qumus2.flows.library.stacking import _validate_focus_reference
    services, ctx, _, _, original = native_reference
    reference = deepcopy(original)
    if fault == 'stage':
        reference['meta']['stage_kinematics'] = {}
    elif fault == 'axis':
        reference['focus_binding']['observation']['focus_axis_calibration']['scale_mm_per_focus_mm'] = -1.
    elif fault == 'interval':
        reference['focus_binding']['observation']['focus_interval_mm'] = None
    elif fault == 'event':
        reference['focus_binding']['event_id'] += 1
    elif fault == 'path':
        reference['focus_binding']['optical_path'] = 'held_target'
    else:
        reference['sha256'] = '0'*64
    with pytest.raises((KeyError, ValueError, TypeError, NeedsHumanError)):
        _validate_focus_reference(reference, ctx.run_id, parked=True, store=services.store)


@pytest.fixture
def event_bound_placement(native_reference, motion_case, monkeypatch, tmp_path, request):  # noqa: F811
    """Actual first-candidate Step/event producer on duration-declaring fake axes.

    Native source AF is real raster evidence; the candidate camera is a controlled
    difference signal. This is not a physical-contact or normal-job simulation.
    """
    import qumus2.flows.library.stacking as module
    services, native_ctx, raw, _, first = native_reference
    case = motion_case
    ctx = case.ctx
    ctx.run_id, ctx.store = native_ctx.run_id, services.store
    ctx.params.update(flake_ids=native_ctx.params['flake_ids'])
    ctx.clock.sleep(max(0, native_ctx.clock.now()-ctx.clock.now())+.01)
    services.store.clock = ctx.clock
    services.store.update_run_meta(ctx.run_id, normal_transfer={'source_references': {'first': first}})
    stage = ctx.gate.stage('transfer_stage')
    monkeypatch.setattr(stage, 'kinematics', lambda: deepcopy(first['meta']['stage_kinematics']))
    capture, move = case.scope.capture, case.manip.move_axis_to
    def candidate_camera():
        frame = capture()
        frame.meta['stage_kinematics'] = stage.kinematics()
        if case.manip.positions['z'] < 6.5:
            image = Image.open(frame.path).convert('RGB')
            ImageDraw.Draw(image).ellipse((210, 130, 270, 190), fill='white')
            image.save(frame.path)
        return frame
    def tracked_move(*args, **kwargs):
        case.metadata['normal_transfer'] = deepcopy(services.store.get_run(ctx.run_id)['meta']['normal_transfer'])
        return move(*args, **kwargs)
    monkeypatch.setattr(case.scope, 'capture', candidate_camera)
    monkeypatch.setattr(case.manip, 'move_axis_to', tracked_move)
    af = module.autofocus
    monkeypatch.setattr(module, 'autofocus', lambda context, objective, *args, **kwargs:
                        8. if objective == 5 else af(context, objective, *args, **kwargs))
    step = module.AdjustFirstContact('stamp', 'v1_first_contact_stamp')
    step.act(ctx, None)
    observation = step.verify(ctx)
    step.on_success(ctx, observation)
    state = services.store.get_run(ctx.run_id)['meta']['normal_transfer']
    native_ctx.clock.sleep(max(0, ctx.clock.now()-native_ctx.clock.now()))
    services.store.clock = native_ctx.clock
    case_params = getattr(getattr(request.node, 'callspec', None), 'params', {})
    _original_pickup_witness(services, native_ctx, raw, tmp_path, monkeypatch,
        first_depth=case_params.get('witness_first_depth', .08), partial=case_params.get('witness_partial', False))
    state = services.store.get_run(ctx.run_id)['meta']['normal_transfer']
    return services, native_ctx, raw, first, state


@pytest.mark.parametrize('native_reference', [None, {'mapping_error_mm': .01, 'readback_error_mm': .003}], indirect=True)
def test_process_reference_uses_first_anchor_and_conservative_interval(event_bound_placement, monkeypatch):
    from qumus2.flows.library.stacking import (
        _capture_chip_focus,
        _process_reference,
    )
    services, ctx, raw, first, state = event_bound_placement
    # Camera pixels alone supply the new focus observation; contradictory scene
    # metadata cannot choose its focus or the process reference.
    capture = raw.capture
    def lower_plane():
        frame = capture()
        frame.data = textured_frame(min(8., abs(raw.get_focus()+.58)*70))
        frame.meta.update(sharpness=8.5, sharpness_chip=8.5, focus_plane='chip', best_focus_z=999.)
        frame.save = None
        frame.path = None
        # Retain the actual synthetic camera bytes, not the renderer's old lazy save.
        image = Path(first['image']).parent / f'lower-{ctx.clock.now()}-{raw.get_focus()}.png'
        image.write_bytes(frame.data)
        frame.path = str(image)
        return frame
    monkeypatch.setattr(raw, 'capture', lower_plane)
    _, current = _capture_chip_focus(ctx, 'refind_second_source_reference', parked=True)
    current.update(source_id='second', source_chip_id='chip_002')
    state['source_references']['second'] = current
    candidate_z = state['placements']['stamp']['candidate_z_mm']
    process = _process_reference(state, 'second', 'stamp', ctx.run_id, .2, services.store)
    a = first['focus_binding']['observation']['focus_interval_mm']
    b = current['focus_binding']['observation']['focus_interval_mm']
    error = 2*first['focus_binding']['observation']['focus_axis_calibration']['mapping_error_mm']
    assert process['delta_interval_mm'] == pytest.approx([b[0]-a[1]-error, b[1]-a[0]+error])
    assert process['minimum_z_mm'] == pytest.approx(candidate_z+b[1]-a[0]+error-.2)
    assert process['minimum_z_mm'] > sum(process['reference_interval_mm'])/2-.2
    assert process['anchor_source_id'] == 'first'
    original = _process_reference(state, 'first', 'stamp', ctx.run_id, .2, services.store)
    assert original['delta_interval_mm'] == [0., 0.] and original['minimum_z_mm'] == pytest.approx(candidate_z-.2)


def test_evidence_digest_is_path_independent_but_content_and_pose_bound(native_reference, tmp_path):
    from qumus2.flows.library.stacking import _digest
    _, _, _, _, reference = native_reference
    moved = deepcopy(reference)
    moved['image'] = 'media/renamed.png'
    moved['focus_binding']['af_frame']['image'] = 'media/renamed-af.png'
    assert _digest(moved) == _digest(reference)
    moved['meta']['focus_z'] += .01
    assert _digest(moved) != _digest(reference)
    moved = deepcopy(reference)
    moved['sha256'] = '0'*64
    assert _digest(moved) != _digest(reference)
    with pytest.raises(ValueError):
        _digest({'image': 'media/unknown.png'})
    with pytest.raises(ValueError):
        _digest({'image': False, 'sha256': reference['sha256']})
    # The actual capture and AF event digest use the same content projection.
    services, _, _, _, _ = native_reference
    for key in ['event_id', 'capture_event_id']:
        payload = deepcopy(services.store.events_after(reference['focus_binding'][key]-1, limit=1)[0].payload)
        original_digest = _digest(payload)
        view = payload.get('microscope_preview', payload)
        view['image'] = 'media/exported.png'
        assert _digest(payload) == original_digest
        view['image_sha256'] = '0'*64
        assert _digest(payload) != original_digest


@pytest.mark.parametrize('fault', [None, 'deleted', 'partial', 'candidate', 'nan', 'working_xy'])
def test_completed_first_candidate_requires_retained_matching_placement(event_bound_placement, fault):
    from qumus2.flows.library.stacking import (
        PROCEDURE_ID,
        TRANSFER_PROFILE_ID,
        StackParams,
    )
    from qumus2.flows.workflow import WorkflowMismatch
    services, ctx, _, first, state = event_bound_placement
    raw = {'procedure_contract': PROCEDURE_ID, 'transfer_profile': TRANSFER_PROFILE_ID, 'stamp_swaps': [],
        'release_policy': ctx.params['release_policy'],
        'flake_ids': ['first'], 'flake_chips': {'first': 'chip_001'}, 'stamp_id': 'stamp'}
    phase_name = 'v1_first_contact_stamp'
    candidate = state['phases'][phase_name]['first_contact_region']
    if fault == 'deleted':
        state['placements'] = {}
    elif fault == 'partial':
        del state['placements']['stamp']['source_anchor']
    elif fault == 'candidate':
        state['placements']['stamp']['candidate_z_mm'] -= .4
        candidate['actual_holder_pose']['z'] -= .4
    elif fault == 'nan':
        state['placements']['stamp']['candidate_z_mm'] = float('nan')
        candidate['actual_holder_pose']['z'] = float('nan')
    elif fault == 'working_xy':
        state['placements']['stamp']['working_xy_mm'][0] += 1
    services.store.update_run_meta(ctx.run_id, normal_transfer=state)
    services.store.begin_step(ctx.run_id, 0, 'refind_first')
    services.store.finish_step(ctx.run_id, 0, 'done')
    services.store.begin_step(ctx.run_id, 1, phase_name, safe_to_resume=False)
    services.store.finish_step(ctx.run_id, 1, 'done')
    run = services.store.get_run(ctx.run_id)
    if fault:
        with pytest.raises(WorkflowMismatch):
            StackParams.validate_saved_execution_binding(raw, saved_run=run, store=services.store)
    else:
        StackParams.validate_saved_execution_binding(raw, saved_run=run, store=services.store)


def _observed_target_baseline(ctx):
    from qumus2.flows.library.stacking import GoToTarget
    from qumus2.flows.step import StepRunner
    outcome = StepRunner().run(GoToTarget('chip_001', (0., 0.)), ctx, 0)
    assert outcome.ok, outcome.message
    return ctx.store.get_run(ctx.run_id)['meta']['normal_transfer']['target_reference']


def _release_checkpoint(ctx, name, *, complete=False):
    """Endpoint/recovery fixture: real immutable source/baseline, no physical claim."""
    import qumus2.flows.library.stacking as module
    phase = module._begin_phase(ctx, name, difference=False)
    state = ctx.store.get_run(ctx.run_id)['meta']['normal_transfer']
    process, reference = module._release_reference(state, ctx.params, ctx.run_id, ctx.verification, ctx.store)
    phase.update(release_policy=module.RELEASE_POLICY_ID, process_reference=process, release_reference=reference,
        release_budget={'stage': 'off' if complete else 'entry_heat', 'start_event_id': phase['reference_event']['event_id'],
            'endpoint_limit': 24, 'endpoint_observations': 0, 'park_observations': 0,
            'thermal_observations': 0, 'park_complete': False})
    if complete:
        phase.update(complete=True, outputs_off=True, peak_c=170.5)
    module._save_phase(ctx, phase)
    return phase


@pytest.mark.parametrize('shift', [-.12, .12])
@pytest.mark.parametrize('native_reference', [None, {'mapping_error_mm': .01, 'readback_error_mm': .003}], indirect=True)
def test_target_release_reuses_first_anchor_and_upper_reference(event_bound_placement, monkeypatch, tmp_path, shift):
    import qumus2.flows.library.stacking as module
    services, ctx, raw, first, _ = event_bound_placement
    services.store.upsert_sample('chip_001', 'chip', props={'corner_stage_xy': [0., 80.]})
    monkeypatch.setattr(ctx.analyzer, 'find_flakes', lambda *a: [])
    capture, count = raw.capture, [0]
    def observed_plane():
        frame = capture()
        frame.data = textured_frame(min(8., abs(raw.get_focus()-shift)*70))
        frame.meta.update(sharpness=8.5, sharpness_chip=8.5, focus_plane='chip', best_focus_z=999.)
        frame.path = str(tmp_path/f'release-reference-{count[0]}.png')
        count[0] += 1
        Path(frame.path).write_bytes(frame.data)
        frame.save = None
        return frame
    monkeypatch.setattr(raw, 'capture', observed_plane)
    before = _observed_target_baseline(ctx)
    services.store.begin_step(ctx.run_id, 1, 'v1_drop_down', safe_to_resume=False)
    state = services.store.get_run(ctx.run_id)['meta']['normal_transfer']
    process, reference = module._release_reference(state, ctx.params, ctx.run_id, ctx.verification, services.store)
    a = first['focus_binding']['observation']['focus_interval_mm']
    b = before['focus_reference']['focus_binding']['observation']['focus_interval_mm']
    error = 2*first['focus_binding']['observation']['focus_axis_calibration']['mapping_error_mm']
    candidate = state['placements']['stamp']['candidate_z_mm']
    assert process['delta_interval_mm'] == pytest.approx([b[0]-a[1]-error, b[1]-a[0]+error])
    witness = reference['witness']
    delta = witness['command_z_mm']-witness['source_reference_upper_mm']
    assert reference['requested_z_mm'] == pytest.approx(candidate+b[1]-a[0]+error+delta)
    assert reference['requested_z_mm'] != pytest.approx(sum(process['reference_interval_mm'])/2+delta)
    assert reference['requested_z_mm']-.005 >= process['minimum_z_mm']
    assert reference['target_verified'] == state['target_reference_verified']
    assert reference['target_before_sha256'] == module._digest(before)
    assert process['anchor_source_id'] == 'first' and process['source_id'] == ctx.params['target_chip_id']


def _witness_target_checkpoint(services, ctx, monkeypatch):
    services.store.upsert_sample('chip_001', 'chip', props={'corner_stage_xy': [0., 80.]})
    monkeypatch.setattr(ctx.analyzer, 'find_flakes', lambda *a: [])
    _observed_target_baseline(ctx)
    services.store.begin_step(ctx.run_id, 1, 'v1_drop_down', safe_to_resume=False)
    return _release_checkpoint(ctx, 'v1_drop_down')


@pytest.mark.parametrize('witness_order', [['first', 'second']])
@pytest.mark.parametrize('witness_first_depth', [.15])
def test_latest_original_witness_is_ordered_not_deepest_or_later_reverify(event_bound_placement, monkeypatch, witness_order, witness_first_depth):
    import qumus2.flows.library.stacking as module
    services, ctx, _, _, _ = event_bound_placement
    phase = _witness_target_checkpoint(services, ctx, monkeypatch)
    state = module._transfer_state(ctx)
    reference = phase['release_reference']
    witness = reference['witness']
    assert witness['flake_id'] == witness_order[-1]
    assert [p['flake_id'] for p in witness['required_pickup_verified']] == witness_order
    first = state['phases']['v1_pick_up_first']
    first_z = first['coverage']['actual_holder_pose']['z']
    assert first_z-first['process_reference']['reference_interval_mm'][1] < reference['relative_intent_mm']
    original = services.store.events_after(first['endpoint_verified']['event_id']-1, limit=1)[0]
    services.store.emit('step.verified', deepcopy(original.payload), run_id=ctx.run_id)
    # Original committed success is the witness; a later observation is not selection.
    _, again = module._release_reference(state, ctx.params, ctx.run_id, ctx.verification, services.store)
    assert again == reference
    changed = deepcopy(ctx.verification)
    changed['transfer_sequence']['release_z_mm'] -= .2
    _, provenance_only = module._release_reference(state, ctx.params, ctx.run_id, changed, services.store)
    assert provenance_only['requested_z_mm'] == reference['requested_z_mm']
    assert provenance_only['nominal_requested_z_mm'] != reference['nominal_requested_z_mm']


@pytest.mark.parametrize('witness_partial', [True])
def test_witness_uses_executed_covered_segment_not_final_partial_macro(event_bound_placement, monkeypatch, witness_partial):
    import qumus2.flows.library.stacking as module
    services, ctx, _, _, _ = event_bound_placement
    phase = _witness_target_checkpoint(services, ctx, monkeypatch)
    witness = phase['release_reference']['witness']
    original = module._transfer_state(ctx)['phases'][witness['phase']]
    partial = original['final_partial_macro']
    assert partial['status'] == 'covered'
    assert witness['command_z_mm'] > partial['admission']['admitted_target_mm']
    assert phase['release_reference']['relative_intent_mm'] == pytest.approx(
        witness['command_z_mm']-witness['source_reference_upper_mm'])
    assert witness['coverage_command']['event_id'] > partial['event_id']


def test_new_witness_authority_projection_ignores_exported_display_paths_only():
    from types import SimpleNamespace

    import qumus2.flows.library.stacking as module
    selection = {k: k for k in module._WITNESS_SELECTION_FIELDS}
    cases = [
        ('job.submitted', {'flow': 'stack_device', 'params': {**selection, 'design_path': '/private/design.gds'}},
            lambda p: p['params'].update(design_path='redacted'), lambda p: p['params'].update(stamp_id='other')),
        ('sample.updated', {'id': 'stamp', 'kind': 'stamp', 'location': module.MANIP, 'status': 'in_use',
            'props': {'film': 'PC', 'stack': [], 'images': ['/private/photo.png'],
                'film_reports': [{'instruction_id': 'report', 'ts': 1., 'what': '/private/notes', 'images': ['/private/report.png']}]}},
            lambda p: p['props'].update(images=['media/photo.png'], film_reports=[
                {'instruction_id': 'report', 'ts': 1., 'what': 'redacted', 'images': ['media/report.png']}]),
            lambda p: p['props']['film_reports'][0].update(instruction_id='another')),
        ('stack.pickup_endpoint', {'stamp_id': 'stamp', 'flake_id': 'first', 'procedure': module.PROCEDURE_ID,
            'observation': {'verdict': 'ok', 'measurements': {'coverage': {'covered': True}}, 'expected': {},
                            'images': ['/private/source.png'], 'message': '/private/text'}},
            lambda p: p['observation'].update(images=['media/source.png'], message='redacted'),
            lambda p: p['observation']['measurements']['coverage'].update(covered=False)),
    ]
    for kind, payload, incidental, authority in cases:
        event = SimpleNamespace(id=1, kind=kind, payload=payload)
        expected = module._witness_binding(event)
        incidental(payload)
        assert module._witness_binding(event) == expected
        authority(payload)
        assert module._witness_binding(event) != expected
    event = SimpleNamespace(id=2, ts=3., kind='lab.frame', payload={'device': module.MANIP,
        'command': 'move_axis_to', 'manipulator': {'x': 1., 'y': 2., 'z': 6.}, 'preview': '/private/frame.png'})
    expected = module._witness_binding(event, projection='witness-manipulator-encoder-v1')
    event.payload['preview'] = 'media/frame.png'
    assert module._witness_binding(event, projection='witness-manipulator-encoder-v1') == expected
    event.payload['manipulator']['z'] -= .01
    assert module._witness_binding(event, projection='witness-manipulator-encoder-v1') != expected


@pytest.mark.parametrize('bad', [None, [], 0, 'corrupt', False], ids=['null', 'array', 'number', 'string', 'boolean'])
@pytest.mark.parametrize('kind,field', [('job.submitted', 'params'), ('sample.updated', 'props')])
def test_witness_authority_projection_rejects_malformed_containers(kind, field, bad):
    from types import SimpleNamespace

    import qumus2.flows.library.stacking as module
    event = SimpleNamespace(id=1, kind=kind, payload={'flow': 'stack_device', field: bad})
    with pytest.raises(ValueError):
        module._witness_binding(event)


@pytest.mark.parametrize('bad', [None, [], 0, 'corrupt', False], ids=['null', 'array', 'number', 'string', 'boolean'])
def test_witness_authority_containers_refuse_act_ledger_and_c5(event_bound_placement, monkeypatch, bad):
    import qumus2.flows.library.stacking as module
    services, ctx, _, _, _ = event_bound_placement
    store = services.store
    phase = _witness_target_checkpoint(services, ctx, monkeypatch)
    state = module._transfer_state(ctx)
    run = store.get_run(ctx.run_id)
    witness = phase['release_reference']['witness']
    submitted = next(module._transfer_events(store, ctx.run_id, 'job.submitted'))
    later = next(e for e in module._transfer_events(store, None, 'sample.updated', witness['load']['stamp_event']['event_id'])
                 if e.id < witness['load']['release_start']['event_id'] and e.payload.get('id') == ctx.params['stamp_id'])
    step = module.DropDown(ctx.params['stamp_id'], ctx.params['flake_ids'], ctx.params['materials'],
                          ctx.params['target_chip_id'], ctx.params['target_xy_mm'], 170., None)
    for event, field in ((submitted, 'params'), (later, 'props')):
        payload = deepcopy(event.payload)
        payload[field] = bad
        last = store.events_after(0, limit=1_000_000)[-1].id
        try:
            store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(payload), event.id))
            with pytest.raises(ValueError):
                module._release_reference(state, ctx.params, ctx.run_id, ctx.verification, store)
            with pytest.raises(NeedsHumanError, match='admitted target process reference'):
                step.act(ctx, None)
            with pytest.raises(NeedsHumanError, match='ledger requires current observable endpoint'):
                step._admit_release_commit(ctx, None)
            with pytest.raises(module.WorkflowMismatch) as rejected:
                module.StackParams.validate_saved_execution_binding(run['params'], saved_run=store.get_run(ctx.run_id), store=store)
            assert rejected.value.reason == 'transfer_reference_changed'
            assert not list(module._transfer_events(store, ctx.run_id, 'device.command', last))
        finally:
            store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(event.payload), event.id))
    module.StackParams.validate_saved_execution_binding(run['params'], saved_run=store.get_run(ctx.run_id), store=store)


def test_witness_plan_and_global_load_mutations_refuse_before_context(event_bound_placement, monkeypatch):
    import qumus2.flows.library.stacking as module
    services, ctx, _, _, _ = event_bound_placement
    store = services.store
    phase = _witness_target_checkpoint(services, ctx, monkeypatch)
    snapshot = module._transfer_state(ctx)
    run = store.get_run(ctx.run_id)
    module.StackParams.validate_saved_execution_binding(run['params'], saved_run=run, store=store)
    witness = phase['release_reference']['witness']
    for fault in ('selected', 'missing_latest', 'original_verify', 'command', 'readback', 'source_ref', 'encoder', 'cutoff'):
        changed = deepcopy(snapshot)
        plan = changed['phases']['v1_drop_down']['release_reference']['witness']
        if fault == 'selected':
            plan['flake_id'] = 'earlier'
        elif fault == 'missing_latest':
            del changed['phases']['v1_pick_up_first']
        elif fault == 'original_verify':
            changed['phases']['v1_pick_up_first']['endpoint_verified']['event_id'] += 1
        elif fault == 'command':
            plan['command_z_mm'] = changed['phases']['v1_pick_up_first']['last_intent']['to_mm']
        elif fault == 'readback':
            plan['readback_z_mm'] += .01
        elif fault == 'source_ref':
            plan['source_reference_upper_mm'] += .01
        elif fault == 'encoder':
            plan['encoder_event_count'] += 1
        else:
            plan['load']['release_start']['event_id'] = phase['reference_event']['event_id']
        store.update_run_meta(ctx.run_id, normal_transfer=changed)
        with pytest.raises(module.WorkflowMismatch):
            module.StackParams.validate_saved_execution_binding(run['params'], saved_run=store.get_run(ctx.run_id), store=store)
    store.update_run_meta(ctx.run_id, normal_transfer=snapshot)
    # A regrip command with run_id=NULL still breaks the bounded GLOBAL prefix.
    commands = list(module._transfer_events(store, ctx.run_id, 'device.command'))
    original = next(e for e in commands if witness['load']['vacuum_command']['event_id'] < e.id
                    < witness['load']['release_start']['event_id'] and e.payload.get('device') == module.MICROSCOPE)
    for on in (True, False, 'unknown'):
        payload = {'device': module.MANIP_VACUUM, 'command': 'set', 'params': {'on': on}}
        store._x('UPDATE event SET payload_json=?,run_id=NULL WHERE id=?', (json.dumps(payload), original.id))
        with pytest.raises(module.WorkflowMismatch):
            module.StackParams.validate_saved_execution_binding(run['params'], saved_run=store.get_run(ctx.run_id), store=store)
        store._x('UPDATE event SET payload_json=?,run_id=? WHERE id=?', (json.dumps(original.payload), ctx.run_id, original.id))
    loaded = store.events_after(witness['load']['stamp_event']['event_id']-1, limit=1)[0]
    for fault in ('film', 'missing_stack', 'location', 'other_stamp'):
        payload = deepcopy(loaded.payload)
        if fault == 'film':
            payload['props']['film'] = 'mica'
        elif fault == 'missing_stack':
            payload['props']['stack'] = None  # present malformed is not the original absent-empty default
        elif fault == 'location':
            payload['location'] = module.HANDOFF
        else:
            payload['id'] = 'another_stamp'
        store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(payload), loaded.id))
        with pytest.raises(module.WorkflowMismatch):
            module.StackParams.validate_saved_execution_binding(run['params'], saved_run=store.get_run(ctx.run_id), store=store)
        store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(loaded.payload), loaded.id))
    changed = deepcopy(snapshot)
    changed['phases']['v1_drop_down']['release_reference']['witness']['coverage_command']['event_id'] += 1
    store.update_run_meta(ctx.run_id, normal_transfer=changed, held_heaters={'stamp_heater': 170.})
    store.set_run_status(ctx.run_id, 'parked')
    monkeypatch.setattr(services.jobs, 'context_factory', lambda *a: pytest.fail('witness refusal precedes context'))
    monkeypatch.setattr(services.jobs.executor, '_restore_heaters', lambda *a: pytest.fail('witness refusal precedes energy'))
    services.jobs.retry(ctx.run_id)
    assert store.get_run(ctx.run_id)['status'] == 'waiting_human'


def test_witness_recomputes_bytes_even_with_consistent_copied_covered_flags(event_bound_placement, monkeypatch):
    import qumus2.flows.library.stacking as module
    services, ctx, _, _, _ = event_bound_placement
    store = services.store
    _witness_target_checkpoint(services, ctx, monkeypatch)
    original = module._transfer_state(ctx)
    changed = deepcopy(original)
    pickup = changed['phases']['v1_pick_up_first']
    coverage = pickup['coverage']
    path = Path(coverage['current']['image'])
    image_bytes = path.read_bytes()
    events = list(module._transfer_events(store, ctx.run_id, 'step.verified'))
    verified = next(e for e in events if e.id == pickup['endpoint_verified']['event_id'])
    endpoint_event = next(module._transfer_events(store, ctx.run_id, 'stack.pickup_endpoint', verified.id))
    capture = next(e for e in module._transfer_events(store, ctx.run_id, 'lab.frame')
                   if e.payload.get('microscope_preview', {}).get('image') == coverage['current']['image'])
    try:
        path.write_bytes(Path(coverage['reference']['image']).read_bytes())
        coverage['current']['sha256'] = coverage['reference']['sha256']
        pickup['endpoint']['coverage'] = deepcopy(coverage)
        payload = deepcopy(verified.payload)
        payload['measurements'] = deepcopy(pickup['endpoint'])
        pickup['endpoint_verified']['observation_sha256'] = module._digest(module._verified_body(payload))
        store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(payload), verified.id))
        payload = deepcopy(endpoint_event.payload)
        payload['observation']['measurements'] = deepcopy(pickup['endpoint'])
        store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(payload), endpoint_event.id))
        payload = deepcopy(capture.payload)
        payload['microscope_preview']['image_sha256'] = coverage['current']['sha256']
        store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(payload), capture.id))
        store._x('UPDATE step SET measurements_json=? WHERE run_id=? AND seq=?',
            (json.dumps(pickup['endpoint']), ctx.run_id, verified.payload['seq']))
        store.update_run_meta(ctx.run_id, normal_transfer=changed)
        assert coverage['covered'] and module._visual_pickup_proof(pickup['endpoint'],
            changed['source_references']['first'], store, ctx.run_id)
        with pytest.raises(ValueError, match='raster coverage'):
            module._release_reference(changed, ctx.params, ctx.run_id, ctx.verification, store)
    finally:
        path.write_bytes(image_bytes)
        for event in (verified, endpoint_event, capture):
            store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(event.payload), event.id))
        store._x('UPDATE step SET measurements_json=? WHERE run_id=? AND seq=?',
            (json.dumps(original['phases']['v1_pick_up_first']['endpoint']), ctx.run_id, verified.payload['seq']))
        store.update_run_meta(ctx.run_id, normal_transfer=original)


def test_witness_relative_intent_guard_does_not_clamp_or_fall_back(event_bound_placement, monkeypatch):
    import math

    import qumus2.flows.library.stacking as module
    services, ctx, _, _, _ = event_bound_placement
    phase = _witness_target_checkpoint(services, ctx, monkeypatch)
    state = module._transfer_state(ctx)
    witness = phase['release_reference']['witness']
    tau = ctx.verification['manipulator']['tolerance_mm']
    for delta in (0., .01, -.2, float('inf'), float('-inf'), float('nan')):
        changed = deepcopy(witness)
        changed['command_z_mm'] = changed['source_reference_upper_mm']+delta
        monkeypatch.setattr(module, '_pickup_release_witness', lambda *a, value=changed: deepcopy(value))
        with pytest.raises(ValueError, match='unchanged process budget'):
            module._release_reference(state, ctx.params, ctx.run_id, ctx.verification, services.store)
    changed = deepcopy(witness)
    changed['command_z_mm'] = math.nextafter(changed['source_reference_upper_mm']+tau-.2, math.inf)
    monkeypatch.setattr(module, '_pickup_release_witness', lambda *a: deepcopy(changed))
    process, plan = module._release_reference(state, ctx.params, ctx.run_id, ctx.verification, services.store)
    assert plan['relative_intent_mm'] >= tau-.2
    assert plan['requested_z_mm']-tau >= process['minimum_z_mm']


@pytest.mark.parametrize('fault', ['policy', 'target', 'axis', 'capture', 'candidate', 'baseline', 'offset'])
def test_target_release_reference_rejects_changed_public_proof_before_context(event_bound_placement, monkeypatch, fault):
    import qumus2.flows.library.stacking as module
    services, ctx, _, _, _ = event_bound_placement
    services.store.upsert_sample('chip_001', 'chip', props={'corner_stage_xy': [0., 80.]})
    monkeypatch.setattr(ctx.analyzer, 'find_flakes', lambda *a: [])
    _observed_target_baseline(ctx)
    services.store.begin_step(ctx.run_id, 1, 'v1_drop_down', safe_to_resume=False)
    state = services.store.get_run(ctx.run_id)['meta']['normal_transfer']
    if fault == 'policy':
        ctx.params['release_policy'] = None
    elif fault == 'target':
        ctx.params['target_chip_id'] = 'wrong_target'
    elif fault == 'axis':
        state['target_reference']['focus_reference']['focus_binding']['observation']['focus_axis_calibration']['scale_mm_per_focus_mm'] = -1
    elif fault == 'capture':
        state['target_reference']['focus_reference']['focus_binding']['capture_event_id'] += 1
    elif fault == 'candidate':
        state['placements']['stamp']['candidate_z_mm'] -= .1
    elif fault == 'baseline':
        state['target_reference']['frames'][0]['sha256'] = '0'*64
    else:
        state['phases']['v1_pick_up_first']['process_reference']['reference_interval_mm'][1] += .3
    with pytest.raises((ValueError, NeedsHumanError)):
        module._release_reference(state, ctx.params, ctx.run_id, ctx.verification, services.store)


@pytest.mark.parametrize('entry', ['retry', 'reverify', 'resume', 'recover', 'worker', 'executor'])
@pytest.mark.parametrize('policy', [None, 'held-target-v2', 'native20-target-reference-parked-release-v1'])
def test_old_release_policy_refuses_actual_routes_before_context(native_reference, entry, policy):
    services, ctx, _, _, _ = native_reference
    store, jobs = services.store, services.jobs
    store.set_run_status(ctx.run_id, 'succeeded')
    params = dict(ctx.params)
    params['release_policy'] = policy
    rid = store.create_run('stack_device', params)
    store.set_run_status(rid, 'paused' if entry == 'resume' else 'queued' if entry in ('worker', 'recover') else 'parked')
    jobs.context_factory = lambda *a: pytest.fail('raw release policy must refuse before context/restore')
    if entry == 'worker':
        jobs.enqueue(rid)
        jobs._spawn(94)
        assert jobs._done[rid].wait(1)
    elif entry == 'executor':
        assert jobs.executor.execute(rid, ctx, restore=True) == 'parked'
    else:
        getattr(jobs, entry)(rid) if entry != 'recover' else jobs.recover()
    assert store.get_run(rid)['status'] == 'waiting_human'


@pytest.mark.parametrize('film,entry,release,terminal_fault', [
    ('PC', 155., 170., None), ('mica', 135., 150., None),
    ('PC', 155., 170., 'unknown_duration'), ('PC', 155., 170., 'late_transport')])
def test_shared_release_real_thermal_motion_park_native_exposures_and_saved_binding(event_bound_placement, monkeypatch, tmp_path, film, entry, release, terminal_fault):
    """A controlled camera, real thermal adapter/Gates and actual shared Steps."""
    import qumus2.flows.library.stacking as module
    from qumus2.devices.base import DeviceBundle
    from qumus2.devices.sim.devices import SimHeater
    from qumus2.flows.step import Verdict
    from qumus2.perception.base import FlakeDetection
    from qumus2.safety.gate import SafetyGate
    from qumus2.safety.limits import Limits
    services, ctx, raw, _, _ = event_bound_placement
    store = services.store
    ctx.params.update(stamp_film=film, pickup_temp_c=90. if film == 'PC' else 80.,
                      drop_temp_c=entry, melt_temp_c=release,
                      support_assumptions={} if film == 'PC' else {'source': 'synthetic'})
    store._x('UPDATE run SET params_json=? WHERE id=?', (json.dumps(ctx.params), ctx.run_id))
    store.upsert_sample('chip_001', 'chip', props={'corner_stage_xy': [0., 80.]})
    store.upsert_sample('stamp', 'stamp', props={'stack': ['first']})
    store.upsert_sample('first', 'flake', location='stamp')
    capture, state = raw.capture, {'material': False, 'captures': 0, 'times': []}
    def camera():
        frame = capture()
        im = Image.open(io.BytesIO(textured_frame(0))).convert('RGB')
        if state['material']:
            ImageDraw.Draw(im).rectangle((65, 65, 82, 82), fill=(60, 210, 195))
        stream = io.BytesIO()
        im.filter(ImageFilter.GaussianBlur(min(8., max(0., abs(raw.get_focus()+.12)-.015)*70))).save(stream, format='PNG')
        frame.data = stream.getvalue()
        frame.meta.update(sharpness=8.5, sharpness_chip=8.5, focus_plane='chip')
        frame.path, frame.save = str(tmp_path/f'release-step-{state["captures"]}.png'), None
        state['captures'] += 1
        state['times'].append(frame.timestamp)
        Path(frame.path).write_bytes(frame.data)
        return frame
    monkeypatch.setattr(raw, 'capture', camera)
    detection = FlakeDetection(dx_mm=0, dy_mm=0, area_um2=300, material='Gr', thickness_label='fewlayer', confidence=.9)
    monkeypatch.setattr(ctx.analyzer, 'find_flakes', lambda frame, material: [detection] if state['material'] else [])
    before = _observed_target_baseline(ctx)
    module.InitializeStamp('stamp', 'v1_initialize_target').act(ctx, None)
    heater = SimHeater('stamp_heater', ctx.clock, ambient=ctx.params['pickup_temp_c'], tau_s=60.)
    monkeypatch.setattr(heater, 'true_temperature', lambda: pytest.fail('Only public thermal readings'))
    devices = DeviceBundle()
    devices.add(heater)
    thermal_gate = SafetyGate(devices, Limits.load(), ctx.clock,
        sink=lambda kind, payload: store.emit(kind, payload, run_id=ctx.run_id))
    monkeypatch.setattr(ctx.gate, 'heater', thermal_gate.heater)
    monkeypatch.setattr(ctx.gate, 'watchdog_tick', thermal_gate.watchdog_tick)
    step = module.DropDown('stamp', ['first'], {'first': 'Gr'}, 'chip_001', (0., 0.), release, None)
    store.begin_step(ctx.run_id, 1, step.name, safe_to_resume=False)
    step.preconditions(ctx)
    step.act(ctx, None)
    phase = store.get_run(ctx.run_id)['meta']['normal_transfer']['phases'][step.name]
    assert phase['withdrawal']['from_readback']['z'] == pytest.approx(phase['release_reference']['requested_z_mm'], abs=.005)
    assert phase['release_reference']['requested_z_mm'] != pytest.approx(5.7)
    assert phase['outputs_off'] and heater.get_target() is None
    # The genuine post-act/pre-endpoint checkpoint must not demand future images.
    run = store.get_run(ctx.run_id)
    module.StackParams.validate_saved_execution_binding(run['params'], saved_run=run, store=store)
    # A later real capture cannot become a new unexecuted baseline after a stroke.
    original_state = deepcopy(run['meta']['normal_transfer'])
    reset = deepcopy(original_state)
    copied = reset['phases'][step.name]
    later = next(e for e in reversed(list(module._transfer_events(store, ctx.run_id, 'lab.frame')))
        if e.payload.get('microscope_preview', {}).get('phase') == step.name
        and e.payload['microscope_preview'].get('image_sha256'))
    assert later.id > copied['reference_event']['event_id']
    copied.update(reference=deepcopy(copied['last_frame']),
        reference_event={'event_id': later.id, 'event_sha256': module._digest(later.payload)},
        started_at=copied['last_frame']['captured_at'], observed_at=copied['last_frame']['captured_at'],
        observations=1, travel_mm=0., complete=False,
        release_budget={'stage': 'entry_heat', 'start_event_id': later.id, 'endpoint_limit': 24,
            'endpoint_observations': 0, 'park_observations': 0, 'thermal_observations': 0, 'park_complete': False})
    for key in ('withdrawal', 'peak_c', 'outputs_off', 'pending_intent', 'held_reason', 'last_intent'):
        copied.pop(key, None)
    store.update_run_meta(ctx.run_id, normal_transfer=reset, held_heaters={'stamp_heater': release})
    with pytest.raises(module.WorkflowMismatch):
        module.StackParams.validate_saved_execution_binding(run['params'], saved_run=store.get_run(ctx.run_id), store=store)
    store.set_run_status(ctx.run_id, 'parked')
    with monkeypatch.context() as patch:
        patch.setattr(services.jobs, 'context_factory', lambda *a: pytest.fail('rebased release must refuse before context'))
        patch.setattr(services.jobs.executor, '_restore_heaters', lambda *a: pytest.fail('rebased release must refuse before heat'))
        services.jobs.retry(ctx.run_id)
    assert store.get_run(ctx.run_id)['status'] == 'waiting_human'
    store.update_run_meta(ctx.run_id, normal_transfer=original_state, held_heaters=run['meta'].get('held_heaters', {}))
    store.set_run_status(ctx.run_id, run['status'])
    observations, captures = phase['observations'], state['captures']
    cursor = store.last_event_id()
    with pytest.raises(NeedsHumanError, match='No further release press'):
        module._guarded_z(ctx, phase, phase['release_reference']['requested_z_mm'])
    with pytest.raises(NeedsHumanError, match='declared park'):
        module._travel(ctx, 40., 90., phase=phase)
    assert store.last_event_id() == cursor
    if terminal_fault:
        adapter = services.lab.devices.get(module.MANIP)
        if terminal_fault == 'unknown_duration':
            duration = adapter.axis_motion_duration_bound
            monkeypatch.setattr(adapter, 'axis_motion_duration_bound', lambda axis, start, target, speed=None:
                None if axis in ('x', 'y') else duration(axis, start, target, speed))
        else:
            move = adapter.move_axis_to
            def delayed(axis, value, speed=None):
                move(axis, value, speed)
                if axis in ('x', 'y'):
                    ctx.clock.sleep(.01)  # Beyond computed Clock roundoff, not a production change.
            monkeypatch.setattr(adapter, 'move_axis_to', delayed)
        with pytest.raises(NeedsHumanError):
            step.verify(ctx)
        failed = store.get_run(ctx.run_id)
        assert failed['meta']['normal_transfer']['phases'][step.name]['release_budget']['endpoint_status'] == 'started'
        assert state['captures'] == captures and heater.get_target() is None
        with pytest.raises(module.WorkflowMismatch):
            module.StackParams.validate_saved_execution_binding(failed['params'], saved_run=failed, store=store)
        with pytest.raises(NeedsHumanError, match='already observed'):
            step.verify(ctx)
        assert state['captures'] == captures
        return
    state['material'] = True  # Controlled post-transfer optical input, no World outcome assertion.
    try:
        observed = step.verify(ctx)
    except NeedsHumanError as exc:
        pytest.fail(f'{exc.message}: {exc.details}')
    phase = store.get_run(ctx.run_id)['meta']['normal_transfer']['phases'][step.name]
    assert observed.verdict == Verdict.OK
    assert phase['release_budget']['park_complete']
    assert phase['release_budget']['endpoint_observations'] == 24
    transport = phase['park_transport']
    assert [t['axis'] for t in transport] == ['x', 'y']
    assert sum(t['duration_bound_s'] for t in transport) >= 90.
    assert abs(sum(t['ended_at']-t['started_at'] for t in transport)-sum(t['duration_bound_s'] for t in transport)) <= sum(
        t['clock_roundoff_s'] for t in transport)
    for t in transport:
        assert abs(t['requested_mm']-t['from_pose'][t['axis']])/t['duration_bound_s'] == pytest.approx(2.)
        command = module._historical_event(store, t['command_event'], ctx.run_id, 'device.command')
        assert command.payload['params']['from_pose'] == t['from_pose']
        assert command.payload['params']['duration_bound_s'] == t['duration_bound_s']
        assert command.payload['params']['speed'] is None
        frames = module._park_command_frames(store, ctx.run_id, command)
        assert t['started_at'] == command.ts and t['ended_at'] == frames[-1].ts
        assert t['encoder_end_event_id'] == frames[-1].id
        assert t['encoder_event_count'] == len(frames)
        assert t['encoder_event_sha256'] == module._park_frames_digest(frames)
        assert t['actual_pose'] == {k: next(e for e in reversed(frames) if 'manipulator' in e.payload).payload['manipulator'][k]
                                   for k in ('x', 'y', 'z')}
    long_gaps = [(a, b) for a, b in zip(state['times'], state['times'][1:], strict=False)
                 if a >= phase['started_at'] and b-a > 2.]
    assert len(long_gaps) == 1
    assert long_gaps[0][0] <= transport[0]['started_at'] <= transport[-1]['ended_at'] <= long_gaps[0][1]
    assert phase['observations']-observations == state['captures']-captures == 24+phase['release_budget']['park_observations']
    assert observed.measurements['target_after']['focus_reference']['focus_binding']['optical_path'] == 'parked_unobstructed'
    assert before['focus_reference']['meta']['focus_z'] == pytest.approx(-.12, abs=.04)
    assert phase['observations'] < 600 and phase['observed_at']-phase['started_at'] < 900
    assert not any('endpoint_park' in name for name in store.get_run(ctx.run_id)['meta']['normal_transfer']['phases'])
    store.emit('step.verified', {'seq': 1, 'name': step.name, **observed.to_dict()}, run_id=ctx.run_id)
    step.on_success(ctx, observed)
    store.finish_step(ctx.run_id, 1, 'done', measurements=observed.measurements)
    run = store.get_run(ctx.run_id)
    module.StackParams.validate_saved_execution_binding(run['params'], saved_run=run, store=store)
    assert store.get_sample('first')['location'] == 'chip_001'
    snapshot = deepcopy(run['meta']['normal_transfer'])
    for fault in ('plan', 'missing_plan', 'total_count', 'endpoint_count', 'endpoint_window', 'park',
                  'park_duration', 'park_duration_increased', 'park_from_pose', 'park_actual_pose',
                  'park_started_at', 'park_ended_at', 'park_encoder_count', 'park_encoder_end',
                  'park_encoder_digest', 'park_command', 'park_pending'):
        changed = deepcopy(snapshot)
        p = changed['phases'][step.name]
        if fault == 'plan':
            p['release_reference']['requested_z_mm'] -= .01
        elif fault == 'missing_plan':
            del p['release_reference']
        elif fault == 'total_count':
            p['observations'] += 1
        elif fault == 'endpoint_count':
            p['release_budget']['endpoint_observations'] -= 1
        elif fault == 'endpoint_window':
            p['release_budget']['endpoint_end_event_id'] = p['release_budget']['endpoint_start_event_id']
        elif fault == 'park':
            p['park']['actual_holder_pose']['x'] += 1
        elif fault == 'park_duration':
            p['park_transport'][0]['duration_bound_s'] -= 1
        elif fault == 'park_duration_increased':
            p['park_transport'][0]['duration_bound_s'] += 1
        elif fault == 'park_from_pose':
            p['park_transport'][0]['from_pose']['z'] -= 1
            p['park_transport'][0]['actual_pose']['z'] -= 1
        elif fault == 'park_actual_pose':
            p['park_transport'][0]['actual_pose']['x'] += .001
        elif fault == 'park_started_at':
            p['park_transport'][0]['started_at'] -= .01
        elif fault == 'park_ended_at':
            p['park_transport'][0]['ended_at'] += .01
        elif fault == 'park_encoder_count':
            p['park_transport'][0]['encoder_event_count'] -= 1
        elif fault == 'park_encoder_end':
            p['park_transport'][0]['encoder_end_event_id'] -= 1
        elif fault == 'park_encoder_digest':
            p['park_transport'][0]['encoder_event_sha256'] = '0'*64
        elif fault == 'park_command':
            p['park_transport'][0]['command_event']['event_sha256'] = '0'*64
        else:
            p['park_pending_intent'] = {}
        store.update_run_meta(ctx.run_id, normal_transfer=changed)
        with pytest.raises(module.WorkflowMismatch):
            module.StackParams.validate_saved_execution_binding(run['params'], saved_run=store.get_run(ctx.run_id), store=store)
    store.update_run_meta(ctx.run_id, normal_transfer=snapshot)
    command = module._historical_event(store, transport[0]['command_event'], ctx.run_id, 'device.command')
    encoder = next(e for e in module._park_command_frames(store, ctx.run_id, command) if e.payload.get('manipulator'))
    for fault in ('held_axis', 'clearance', 'time'):
        payload, timestamp = deepcopy(encoder.payload), encoder.ts
        if fault == 'time':
            timestamp = transport[0]['ended_at']+1.
        else:
            payload['manipulator']['y' if fault == 'held_axis' else 'z'] -= 1.
        store._x('UPDATE event SET payload_json=?,ts=? WHERE id=?', (json.dumps(payload), timestamp, encoder.id))
        with pytest.raises(module.WorkflowMismatch):
            module.StackParams.validate_saved_execution_binding(run['params'], saved_run=store.get_run(ctx.run_id), store=store)
        store._x('UPDATE event SET payload_json=?,ts=? WHERE id=?', (json.dumps(encoder.payload), encoder.ts, encoder.id))
    commands = list(module._transfer_events(store, ctx.run_id, 'device.command'))
    press = next(e for e in commands if e.payload.get('device') == module.MANIP
        and e.payload.get('command') == 'move_axis_to' and e.payload['params'].get('axis') == 'z'
        and abs(e.payload['params']['value']-phase['release_reference']['requested_z_mm']) <= 1e-8)
    changed = deepcopy(press.payload)
    changed['params']['value'] += .01
    store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(changed), press.id))
    with pytest.raises(module.WorkflowMismatch):
        module.StackParams.validate_saved_execution_binding(run['params'], saved_run=store.get_run(ctx.run_id), store=store)
    store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(press.payload), press.id))


@pytest.mark.parametrize('consumer', ['ordinary', 'device', 'reactor'])
@pytest.mark.parametrize('case', ['appearance', 'preexisting', 'missing', 'unchanged_pixels', 'unobservable', 'wrong_roi', 'boolean_only', 'missing_phase', 'missing_verified', 'completed_missing_endpoint', 'completed_missing_binding', 'completed_changed_event'])
def test_release_uses_qualified_type_appearance_without_stamp_negative(event_bound_placement, monkeypatch, tmp_path, case, consumer):
    import qumus2.flows.library.stacking as module
    from qumus2.flows.library.stacking import DropDown, StackParams
    from qumus2.flows.step import Verdict
    from qumus2.perception.base import FlakeDetection
    services, ctx, raw, _, _ = event_bound_placement
    # This matrix isolates native optical/ledger criteria; the full actual
    # command history is exercised by the shared real-Step test above.
    monkeypatch.setattr(module, '_release_execution_binding', lambda *a: None)
    services.store.upsert_sample('chip_001', 'chip', props={'corner_stage_xy': [0., 80.]})
    services.store.upsert_sample('stamp', 'stamp', props={'stack': ['first']})
    services.store.upsert_sample('first', 'flake', location='stamp')
    state = {'material': case == 'preexisting', 'pixels': case == 'preexisting', 'counter': 0}
    capture = raw.capture
    def camera():
        frame = capture()
        im = Image.open(io.BytesIO(textured_frame(0))).convert('RGB')
        if case == 'unobservable' and state.get('after'):
            im = Image.new('RGB', im.size, (120, 130, 140))
        elif state['pixels']:
            ImageDraw.Draw(im).rectangle((65, 65, 82, 82), fill=(60, 210, 195))
        stream = io.BytesIO()
        # A real flat pixel plateau keeps the unchanged-content negative exact
        # across different, equally qualified native focus sampling grids.
        im.filter(ImageFilter.GaussianBlur(min(8., max(0., abs(raw.get_focus()-.07)-.015)*70))).save(stream, format='PNG')
        frame.data = stream.getvalue()
        path = tmp_path/f"target-camera-{state['counter']}.png"
        state['counter'] += 1
        path.write_bytes(frame.data)
        frame.path, frame.save = str(path), None
        return frame
    monkeypatch.setattr(raw, 'capture', camera)
    detection = FlakeDetection(dx_mm=0, dy_mm=0, area_um2=300, material='Gr', thickness_label='fewlayer', confidence=.9)
    monkeypatch.setattr(ctx.analyzer, 'find_flakes', lambda frame, material: [detection] if state['material'] else [])
    _observed_target_baseline(ctx)
    ctx.clock.sleep(.1)
    from qumus2.flows.library.material_metal_device import DeviceRelease
    from qumus2.flows.library.reactor_preparation import ReactorDropDown
    cls = {'ordinary': DropDown, 'device': DeviceRelease, 'reactor': ReactorDropDown}[consumer]
    ctx.params['receipt_inputs'] = {'stamp_film': 'mica'}
    step = cls('stamp', ['first'], {'first': 'Gr'}, 'chip_001', (0., 0.), 170., None)
    devices_before = len(services.store.list_samples(kind='device'))
    # A completed release act leaves the holder clear over the working area.
    # Keep this optical fixture consistent before its real terminal park.
    module.ManipulatorTo('over_stage', stamp_id='stamp').act(ctx, None)
    services.store.begin_step(ctx.run_id, 1, step.name, safe_to_resume=False)
    phase = _release_checkpoint(ctx, step.name, complete=True)
    snapshot = services.store.get_run(ctx.run_id)['meta']['normal_transfer']
    snapshot['phases'][step.name] = phase
    services.store.update_run_meta(ctx.run_id, normal_transfer=snapshot)
    state['material'] = case != 'missing'
    state['pixels'] = case not in ('missing', 'unchanged_pixels')
    state['after'] = True
    if case == 'wrong_roi':
        from qumus2.flows.library.stacking import GoToTarget
        monkeypatch.setattr(GoToTarget, '_xy', lambda self, context: (1., 80.))
        with pytest.raises(NeedsHumanError, match='requested stage ROI'):
            step.verify(ctx)
        return
    observed = step.verify(ctx)
    if case == 'appearance':
        parked_phase = services.store.get_run(ctx.run_id)['meta']['normal_transfer']['phases'][step.name]
        for rail in parked_phase['park_transport']:
            command = module._historical_event(services.store, rail['command_event'], ctx.run_id, 'device.command')
            assert abs(command.payload['params']['from_pose'][rail['axis']]-command.payload['params']['value']) > .005
            assert command.payload['params']['duration_bound_s'] > 0.
            frames = module._park_command_frames(services.store, ctx.run_id, command)
            final = next(e for e in reversed(frames) if 'manipulator' in e.payload)
            assert rail['actual_pose'] == {k: final.payload['manipulator'][k] for k in ('x', 'y', 'z')}
            for malformed in (None, {}, [], False, 0, '', {'x': 150., 'y': 10.}):
                payload = deepcopy(final.payload)
                payload['manipulator'] = malformed
                services.store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(payload), final.id))
                with pytest.raises(ValueError, match='public encoder stream'):
                    module._park_command_frames(services.store, ctx.run_id, command)
                changed_frames = deepcopy(frames)
                next(e for e in changed_frames if e.id == final.id).payload['manipulator'] = malformed
                with pytest.raises(ValueError, match='public encoder stream'):
                    module._park_frames_digest(changed_frames)
                services.store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(final.payload), final.id))
    if case == 'unobservable':
        assert observed.verdict == Verdict.UNCLEAR
        assert observed.measurements['release_confirmed'] is False
        assert 'target_observation_unavailable' in observed.measurements
        snapshot = services.store.get_run(ctx.run_id)['meta']['normal_transfer']
        assert snapshot['phases'][step.name]['release_budget']['endpoint_status'] == 'unavailable'
        assert snapshot['phases'][step.name]['release_budget']['endpoint_observations'] <= 24
        count = state['counter']
        with pytest.raises(NeedsHumanError, match='already observed'):
            step.verify(ctx)
        assert state['counter'] == count
        assert services.store.get_sample('first')['location'] == 'stamp'
        return
    if case == 'unchanged_pixels':
        before = services.store.get_run(ctx.run_id)['meta']['normal_transfer']['target_reference']
        assert {f['sha256'] for f in before['frames']} == {
            f['sha256'] for f in observed.measurements['target_after']['frames']}
    if case in ('boolean_only', 'missing_phase', 'missing_verified'):
        if case == 'boolean_only':
            observed.measurements = {'release_confirmed': True}
        elif case == 'missing_phase':
            snapshot = services.store.get_run(ctx.run_id)['meta']['normal_transfer']
            snapshot['phases'] = {}
            services.store.update_run_meta(ctx.run_id, normal_transfer=snapshot)
        with pytest.raises(NeedsHumanError):
            step.on_success(ctx, observed)
        assert services.store.get_sample('first')['location'] == 'stamp'
        return
    assert observed.measurements['stamp_evaluated'] is False
    assert observed.measurements['stamp_flakes_seen'] is None
    assert observed.measurements['full_identity_completeness_observed'] is False
    assert len(observed.images) == 6
    if case == 'appearance' or case.startswith('completed_'):
        assert observed.verdict == Verdict.OK
        services.store.emit('step.verified', {'seq': 1, 'name': step.name, **observed.to_dict()}, run_id=ctx.run_id)
        step.on_success(ctx, observed)
        assert services.store.get_sample('first')['location'] == 'chip_001'
        assert len(services.store.list_samples(kind='device')) == devices_before+(consumer == 'ordinary')
        if consumer == 'device':
            assert services.store.get_sample('stamp')['props']['film'] == 'mica'
        if case.startswith('completed_'):
            services.store.finish_step(ctx.run_id, 1, 'done', measurements=observed.measurements)
            run = services.store.get_run(ctx.run_id)
            StackParams.validate_saved_execution_binding(run['params'], saved_run=run, store=services.store)
            snapshot = run['meta']['normal_transfer']
            if case == 'completed_missing_endpoint':
                del snapshot['phases'][step.name]['endpoint']
            elif case == 'completed_missing_binding':
                del snapshot['phases'][step.name]['endpoint_verified']
            else:
                event_id = snapshot['phases'][step.name]['endpoint_verified']['event_id']
                payload = deepcopy(services.store.events_after(event_id-1, limit=1)[0].payload)
                payload['measurements']['release_confirmed'] = False
                services.store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(payload), event_id))
            services.store.update_run_meta(ctx.run_id, normal_transfer=snapshot, held_heaters={'stamp_heater': 170.})
            services.store.set_run_status(ctx.run_id, 'parked')
            services.jobs.context_factory = lambda *args: pytest.fail('completed release refusal precedes context')
            services.jobs.executor._restore_heaters = lambda *args: pytest.fail('completed release refusal precedes energy')
            services.jobs.retry(ctx.run_id)
            assert services.store.get_run(ctx.run_id)['status'] == 'waiting_human'
    else:
        assert observed.verdict == Verdict.UNCLEAR
        count = state['counter']
        with pytest.raises(NeedsHumanError, match='already observed'):
            step.verify(ctx)
        assert state['counter'] == count
        with pytest.raises(NeedsHumanError, match='ledger requires'):
            step.on_success(ctx, observed)
        assert services.store.get_sample('first')['location'] == 'stamp'


@pytest.mark.parametrize('entry', ['retry', 'reverify', 'resume', 'recover', 'worker', 'executor'])
@pytest.mark.parametrize('fault', ['missing_phase', 'pending', 'interrupted'])
def test_started_release_never_restores_energy_with_missing_or_interrupted_phase(event_bound_placement, monkeypatch, entry, fault):
    from qumus2.flows.library.stacking import StackParams
    services, ctx, _, _, _ = event_bound_placement
    store, jobs = services.store, services.jobs
    store.upsert_sample('chip_001', 'chip', props={'corner_stage_xy': [0., 80.]})
    monkeypatch.setattr(ctx.analyzer, 'find_flakes', lambda *args: [])
    before = _observed_target_baseline(ctx)
    ctx.clock.sleep(.1)
    store.begin_step(ctx.run_id, 1, 'v1_drop_down', safe_to_resume=False)
    phase = _release_checkpoint(ctx, 'v1_drop_down')
    state = store.get_run(ctx.run_id)['meta']['normal_transfer']
    state['target_reference'] = before
    store.update_run_meta(ctx.run_id, normal_transfer=state)
    # A genuine single-reference pre-act phase does not require a future endpoint.
    StackParams.validate_saved_execution_binding(ctx.params, saved_run=store.get_run(ctx.run_id), store=store)
    if fault == 'missing_phase':
        state['phases'] = {}
    elif fault == 'pending':
        state['phases'][phase['name']]['pending_intent'] = {'axis': 'z'}
    else:
        state['phases'][phase['name']]['observations'] = 2
    store.update_run_meta(ctx.run_id, normal_transfer=state, held_heaters={'stamp_heater': 170.})
    store.set_run_status(ctx.run_id, 'paused' if entry == 'resume' else
                         'queued' if entry in ('worker', 'recover') else 'parked')
    jobs.context_factory = lambda *args: pytest.fail('release phase refusal must precede context')
    jobs.executor._restore_heaters = lambda *args: pytest.fail('release phase refusal must precede energy restoration')
    if entry == 'executor':
        assert jobs.executor.execute(ctx.run_id, ctx, restore=True) == 'parked'
    elif entry == 'worker':
        jobs.enqueue(ctx.run_id)
        jobs._spawn(92)
        assert jobs._done[ctx.run_id].wait(1)
    else:
        getattr(jobs, entry)(ctx.run_id) if entry != 'recover' else jobs.recover()
    assert store.get_run(ctx.run_id)['status'] == 'waiting_human'


@pytest.mark.parametrize('case', ['disappeared', 'remains', 'identical_false_negative'])
def test_pickup_visual_source_criterion_and_identical_content_guard(native_reference, monkeypatch, tmp_path, case):
    import qumus2.flows.library.stacking as module
    from qumus2.flows.step import Verdict
    services, ctx, raw, _, _ = native_reference
    state = {'body': True, 'counter': 0}
    capture = raw.capture
    def camera():
        frame = capture()
        image = Image.open(io.BytesIO(textured_frame(0))).convert('RGB')
        if state['body']:
            ImageDraw.Draw(image).rectangle((72, 65, 88, 79), fill=(60, 210, 195))
        stream = io.BytesIO()
        image.filter(ImageFilter.GaussianBlur(min(8., abs(raw.get_focus()-.07)*70))).save(stream, format='PNG')
        frame.data = stream.getvalue()
        path = tmp_path/f"source-camera-{state['counter']}.png"
        state['counter'] += 1
        path.write_bytes(frame.data)
        frame.path, frame.save = str(path), None
        return frame
    monkeypatch.setattr(raw, 'capture', camera)
    _, reference = module._capture_chip_focus(ctx, 'selected_source_before', parked=True)
    reference.update(source_id='first', source_chip_id='chip_001', target_offset_mm=[0., 0.])
    state['body'] = case != 'disappeared'
    after, _ = module._capture_chip_focus(ctx, 'selected_source_after', parked=True)
    observed_presence = case == 'remains'
    def source_observation(context, material, radius, images, *, frames, source_reference):
        frames.append(after)
        images.append(after.saved_path())
        return [object()] if observed_presence else []
    monkeypatch.setattr(module, '_chip_near_centre', source_observation)
    monkeypatch.setattr(module, 'autofocus', lambda *args, **kwargs: pytest.fail('no mandatory stamp autofocus'))
    monkeypatch.setattr(ctx.analyzer, 'find_stamp_flakes', lambda *args: pytest.fail('no stamp count readiness'))
    step = module.PickUpFlake('first', 'stamp', 'Gr')
    services.store.upsert_sample('stamp', 'stamp', props={'stack': []})
    services.store.upsert_sample('first', 'flake', location='chip_001')
    phase = {'name': step.name, 'profile': module.TRANSFER_PROFILE_ID, 'complete': True, 'coverage': {'covered': True}}
    services.store.update_run_meta(ctx.run_id, normal_transfer={'source_references': {'first': reference}, 'phases': {step.name: phase}})
    obs = step.verify(ctx)
    assert obs.measurements['method'] == 'source_disappearance'
    assert obs.measurements['stamp_evaluated'] is False and obs.measurements['flake_on_stamp'] is None
    if case == 'disappeared':
        assert obs.verdict == Verdict.OK and obs.measurements['process_confirmation'] is True
        assert obs.measurements['full_identity_completeness_observed'] is False
        services.store.emit('step.verified', {'seq': 1, 'name': step.name, **obs.to_dict()}, run_id=ctx.run_id)
        step.on_success(ctx, obs)
        assert services.store.get_sample('first')['location'] == 'stamp'
    else:
        assert obs.verdict == Verdict.UNCLEAR
        assert obs.measurements['process_confirmation'] is False and obs.measurements['transfer_confirmed'] is False
        with pytest.raises(NeedsHumanError):
            step.on_success(ctx, obs)
        assert services.store.get_sample('first')['location'] == 'chip_001'
        if case == 'identical_false_negative':
            assert obs.measurements['source_detection_count'] == 0
            assert obs.measurements['source_frames'][0]['sha256'] == reference['sha256']
            forged = {**obs.measurements, 'process_confirmation': True, 'transfer_confirmed': True}
            assert module._visual_pickup_proof(forged, reference, services.store, ctx.run_id) is False


@pytest.mark.parametrize('fault', ['count', 'missing_binding', 'roi', 'verified_event'])
def test_target_baseline_observation_cannot_be_rewritten_before_release(native_reference, monkeypatch, fault):
    services, ctx, _, _, _ = native_reference
    store, jobs = services.store, services.jobs
    store.upsert_sample('chip_001', 'chip', props={'corner_stage_xy': [0., 80.]})
    before = _observed_target_baseline(ctx)
    assert before['materials_found']['Gr'] > 0  # Actual preexisting material observer result.
    state = store.get_run(ctx.run_id)['meta']['normal_transfer']
    if fault == 'count':
        state['target_reference']['materials_found']['Gr'] = 0
    elif fault == 'missing_binding':
        del state['target_reference_verified']
    elif fault == 'roi':
        state['target_reference']['target_xy_mm'][0] += 1
    else:
        event_id = state['target_reference_verified']['event_id']
        payload = deepcopy(store.events_after(event_id-1, limit=1)[0].payload)
        payload['measurements']['target_reference_sha256'] = '0'*64
        store._x('UPDATE event SET payload_json=? WHERE id=?', (json.dumps(payload), event_id))
    store.update_run_meta(ctx.run_id, normal_transfer=state, held_heaters={'stamp_heater': 170.})
    store.set_run_status(ctx.run_id, 'parked')
    jobs.context_factory = lambda *args: pytest.fail('baseline refusal must precede context')
    jobs.executor._restore_heaters = lambda *args: pytest.fail('baseline refusal must precede energy')
    jobs.retry(ctx.run_id)
    assert store.get_run(ctx.run_id)['status'] == 'waiting_human'


def test_source_endpoint_refuses_missing_selected_location_before_af(native_reference, monkeypatch):
    import qumus2.flows.library.stacking as module
    _, ctx, _, _, original = native_reference
    reference = deepcopy(original)
    del reference['target_offset_mm']
    monkeypatch.setattr(module, 'autofocus', lambda *args, **kwargs: pytest.fail('unknown source ROI must refuse before AF'))
    with pytest.raises(NeedsHumanError, match='selected-source location'):
        module._chip_near_centre(ctx, 'Gr', .06, source_reference=reference)


@pytest.mark.parametrize('case', ['picked', 'present', 'flat_reference', 'missed_plane'])
def test_blank_source_spot_is_observed_only_at_a_bracketed_textured_plane(native_reference, monkeypatch, case):
    """After a pickup, bare SiO2 has no texture and no focus peak (#109): a flat
    sweep bracketing the textured pre-pickup plane observes absence; a flake
    still there is found, and an untextured or missed reference parks."""
    import qumus2.flows.library.stacking as module
    services, ctx, _, _, original = native_reference
    reference = deepcopy(original)
    # Look through no stamp, as the design flow does with the holder parked:
    # the restored plane is the recorded source plane itself.
    profile = module._profile
    monkeypatch.setattr(module, '_profile', lambda c: {**profile(c), 'focus_offset_mm': 0})
    if case != 'present':
        services.lab.world.objects['chip_001'].flakes = []
    if case == 'flat_reference':
        path = Path(reference['image']).with_name('flat-reference.png')
        Image.new('RGB', Image.open(reference['image']).size, (125, 125, 125)).save(path)
        reference.update(image=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    if case == 'missed_plane':  # a flat sweep that does not bracket the recorded plane
        evidence = {'plane': 'chip', 'objective': 20, 'pixel_score': 0, 'focus_z': .5,
                    'sampled_passes': [{'samples': [{'raster_score': 0, 'readback_focus_z_mm': z}
                                                    for z in (.6, .65, .7)]}]}
        assert module._flat_sweep_absence_plane(evidence, reference, .5, 20, .02) is None
        evidence['sampled_passes'][0]['samples'][0]['readback_focus_z_mm'] = .4
        assert module._flat_sweep_absence_plane(evidence, reference, .5, 20, .02) is not None
        return
    if case == 'flat_reference':
        with pytest.raises(NeedsHumanError, match='ordinary focus peak'):
            module._chip_near_centre(ctx, 'Gr', .06, source_reference=reference)
        return
    found = module._chip_near_centre(ctx, 'Gr', .06, source_reference=reference)
    flat = [e for e in services.store.events_after(0, limit=100000) if e.kind == 'stack.source_absence_flat_sweep']
    if case == 'present':
        assert found and not flat
        return
    assert found == [] and len(flat) == 1
    record = flat[0].payload
    assert record['method'] == 'flat_sweep_brackets_textured_reference' and record['reference_raster_score'] > 0
    low, high = record['sweep_focus_z_mm']
    assert low < record['restored_focus_z_mm'] < high
    af = last_focus(services, ctx)
    assert af['peak_observable'] is False
    # The commit-time re-derivation (_visual_pickup_proof) accepts the same look,
    # and nothing else: an untextured reference or a moved plane fails it.
    assert module._source_absence_focus_ok(af, reference)
    assert not module._source_absence_focus_ok({**af, 'focus_z': af['focus_z'] + 1}, reference)


@pytest.mark.parametrize('fault', ['unknown_axis', 'flat_pixels'])
def test_current_focus_cannot_create_reference_without_calibration_or_texture(native_reference, monkeypatch, tmp_path, fault):
    from qumus2.flows.library.stacking import _capture_chip_focus
    _, ctx, raw, _, _ = native_reference
    if fault == 'unknown_axis':
        monkeypatch.setattr(raw, 'focus_axis_calibration', lambda objective: None)
    else:
        capture = raw.capture
        counter = [0]
        def flat_camera():
            frame = capture()
            stream = io.BytesIO()
            Image.new('RGB', (160, 144), (125, 125, 125)).save(stream, format='PNG')
            frame.data = stream.getvalue()
            path = tmp_path/f'flat-{counter[0]}.png'
            counter[0] += 1
            path.write_bytes(frame.data)
            frame.path, frame.save = str(path), None
            return frame
        monkeypatch.setattr(raw, 'capture', flat_camera)
    with pytest.raises(NeedsHumanError, match='unique calibrated native20 focus interval'):
        _capture_chip_focus(ctx, 'unavailable_source_reference', parked=True)
