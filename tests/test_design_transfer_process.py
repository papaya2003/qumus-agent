"""New-design process receipts, finite sampling and actual Clock dwell."""
from copy import deepcopy

import pytest

from qumus2.config import load_yaml
from qumus2.core.errors import NeedsHumanError
from qumus2.flows.library import stacking as m
from tests.test_v1_transfer_sequence import motion_case as _motion_case


@pytest.fixture
def process_case(tmp_path, monkeypatch):
    case = _motion_case.__wrapped__(tmp_path)
    # These tests isolate real Clock/park receipt admission. Image formation,
    # original capture context and complete RGB cells have dedicated controls.
    case.replayed_contact = {"covered": True, "method": "isolated_contact_replay_fixture"}
    monkeypatch.setattr(m, "v1_pickup_coverage", lambda *args, **kwargs: deepcopy(case.replayed_contact))
    return case


def new_design_case(case):
    case.ctx.params.update(device_design={"sources": [{"flake_id": "a", "source_chip_id": "ca",
        "outline_mm": [[-.02, -.015], [.02, -.015], [.02, .015], [-.02, .015]]}]},
        first_contact_sampling=m._first_contact_sampling(case.ctx.verification),
        contact_dwell_s=1., contact_dwell_source="synthetic_recipe_default",
        design_park_pose={"x": 150., "y": 10., "z": 6.5})
    return case


def test_first_contact_jogs_10um_on_the_profile_budget_with_saved_receipt(process_case):
    """#204: the touch search is v1's 10 um jog, one command and one frame,
    on the ordinary profile budget; no 0.25 um fine cadence remains."""
    case = new_design_case(process_case)
    phase = m._begin_phase(case.ctx, "v1_first_contact_stamp")
    m._guarded_z(case.ctx, phase, 6.49, speed=.005)
    assert len(case.moves) == 1 and case.moves[0][2] == pytest.approx(6.49)
    assert m._phase_budget(case.ctx, phase) == {**m._profile(case.ctx), "max_unobserved_s": pytest.approx(2.05)}
    assert m._phase_budget(case.ctx, phase)["max_observations"] == 600
    phase["sampling_policy"]["min_z_mm"] -= 1
    with pytest.raises(NeedsHumanError, match="policy"):
        m._phase_budget(case.ctx, phase)


def test_fine_sampling_refuses_exhaustion_and_deleted_receipt(process_case):
    case = new_design_case(process_case)
    phase = m._begin_phase(case.ctx, "v1_first_contact_stamp")
    phase["observations"] = 600
    before = len(case.scope.frames)
    with pytest.raises(NeedsHumanError, match="exhausted"):
        m._phase_capture(case.ctx, phase)
    assert len(case.scope.frames) == before
    phase["observations"] = 1
    phase["sampling_policy_event"]["event_id"] += 1000
    with pytest.raises(ValueError, match="Historical"):
        m._phase_budget(case.ctx, phase)


def process_receipt(case):
    new_design_case(case)
    phase = m._begin_phase(case.ctx, "v1_pick_up_a")
    before = case.ctx.clock.now()
    m._design_contact_dwell(case.ctx, phase)
    assert case.ctx.clock.now()-before >= 1.
    assert phase["contact_dwell"]["completed"] is True
    park = {"actual_holder_pose": deepcopy(case.ctx.params["design_park_pose"]), "completed_at": case.ctx.clock.now()}
    phase["source_inspection_park"] = park
    body = {"phase": phase["name"], "park": park}
    event = case.ctx.store.emit("stack.design_source_park", body, run_id=case.ctx.run_id)
    phase["source_inspection_park_event"] = {"event_id": event, "event_sha256": m._digest(body)}
    focus = case.ctx.store.emit("microscope.focus", {"holder_pose": park["actual_holder_pose"]}, run_id=case.ctx.run_id)
    endpoint = {"focus_event_id": focus, "source_frames": [{"captured_at": case.ctx.clock.now()}]}
    case.ctx.store.emit("step.verified", {"name": "measure_rotation_ca", "verdict": "ok",
        "measurements": {"rotation_deg": 0.}}, run_id=case.ctx.run_id)
    current = case.scope.capture()
    phase["coverage"] = {"reference": phase["reference"], "current": m._frame_record(current),
                         **deepcopy(case.replayed_contact)}
    endpoint["coverage"] = deepcopy(phase["coverage"])
    source = {"source_id": "a", "target_offset_mm": [0., 0.], "design_source_rotation_deg": 0.}
    return phase, source, endpoint


def test_dwell_and_park_are_recomputed_read_only_from_original_frames(process_case):
    case = process_case
    phase, source, endpoint = process_receipt(case)
    start, saved = case.ctx.clock.now(), deepcopy(phase)
    m._validate_design_pickup_additions(case.ctx, phase, source, endpoint)
    assert case.ctx.clock.now() == start and phase == saved


def test_phase_coverage_cannot_diverge_from_its_verified_endpoint(process_case):
    phase, source, endpoint = process_receipt(process_case)
    phase["coverage"]["current"]["captured_at"] += 1
    with pytest.raises(ValueError, match="independently verified endpoint"):
        m._validate_design_pickup_additions(process_case.ctx, phase, source, endpoint)


@pytest.mark.parametrize("fault", ["incomplete", "short", "receipt", "not_parked", "early_frame", "coverage"])
def test_process_receipts_refuse_missing_dwell_clearance_or_whole_body(process_case, fault):
    case = process_case
    phase, source, endpoint = process_receipt(case)
    if fault == "incomplete":
        phase["contact_dwell"]["completed"] = False
    elif fault == "short":
        phase["contact_dwell"]["completed_at"] -= .5
    elif fault == "receipt":
        phase["contact_dwell_event"]["event_id"] += 1000
    elif fault == "not_parked":
        phase["source_inspection_park"]["actual_holder_pose"]["x"] = 40.
    elif fault == "early_frame":
        endpoint["source_frames"][0]["captured_at"] -= .1
    else:
        phase["coverage"]["covered"] = False
        endpoint["coverage"] = deepcopy(phase["coverage"])
    with pytest.raises(ValueError):
        m._validate_design_pickup_additions(case.ctx, phase, source, endpoint)


def test_v1_initialize_lowers_the_holder_to_the_preset_once(process_case):
    """#204: v1 molecule_initialize_stamp, Z 5.4 at 0.1 mm/s, before the search."""
    case = new_design_case(process_case)
    step = m.AdjustFirstContact("stamp_01", "v1_first_contact_stamp_01")
    step._v1_initialize(case.ctx)
    phase = m._transfer_state(case.ctx)["phases"]["v1_initialize_stamp_stamp_01"]
    assert phase["complete"] and "pending_intent" not in phase
    assert m._pos(case.ctx)["z"] == pytest.approx(5.4)
    assert {axis for axis, *_ in case.moves} == {"z"} and {move[3]["pending_intent"]["speed_mm_s"] for move in case.moves} == {.1}
    count = len(case.moves)
    step._v1_initialize(case.ctx)
    assert len(case.moves) == count


def test_touch_search_stops_for_a_person_at_the_anti_crush_floor(process_case, monkeypatch):
    """#204: no changed region in 10 um jogs from the preset down to the Z 4.9
    floor: stop and ask a person instead of pressing on (v1 had no floor)."""
    case = new_design_case(process_case)
    case.ctx.params["flake_ids"] = ["a"]
    state = m._transfer_state(case.ctx)
    state["source_references"] = {"a": {}}
    m._save_transfer(case.ctx, state)
    monkeypatch.setattr(m, "_validate_focus_reference", lambda *args, **kwargs: None)
    monkeypatch.setattr(m.AdjustFirstContact, "_beside_first_touch", lambda self, ctx: None)
    monkeypatch.setattr(m, "v1_first_contact", lambda *args, **kwargs: {"found": False})
    start = m._pos(case.ctx)["z"]
    with pytest.raises(NeedsHumanError, match="anti-crush"):
        m.AdjustFirstContact("stamp_01", "v1_first_contact_stamp_01").act(case.ctx, None)
    floor = case.ctx.verification["transfer_sequence"]["first_contact_min_z_mm"]
    searched = [before["z"]-target for axis, before, target, _ in case.moves if before["z"] <= 5.4+1e-9]
    assert all(step == pytest.approx(.01) for step in searched)
    assert m._pos(case.ctx)["z"] == pytest.approx(floor, abs=.0101) and m._pos(case.ctx)["z"] >= floor-1e-9
    assert m._pos(case.ctx)["z"] < start


def test_first_touch_is_offset_beside_the_centred_source_once(process_case, monkeypatch):
    """D12 (#136): refind centres the source under the apex, so the first touch
    is moved beside it at clearance height, once, before the v1 initialize."""
    case = new_design_case(process_case)
    # The landing image is checked by the dedicated landing tests below.
    monkeypatch.setattr(m, "autofocus", lambda *args, **kwargs: 0.)
    monkeypatch.setattr(m, "_first_touch_landing", lambda ctx, policy, calibration, pixels: {
        "chosen": {"direction_deg": 0, "holder_delta_mm": [0., .5]}})
    start = m._pos(case.ctx)
    step = m.AdjustFirstContact("stamp_01", "v1_first_contact_stamp_01")
    step._beside_first_touch(case.ctx)
    phase = m._transfer_state(case.ctx)["phases"]["beside_first_touch_stamp_01"]
    after = m._pos(case.ctx)
    assert phase["complete"] and "pending_intent" not in phase
    assert abs(after["y"]-start["y"]) == pytest.approx(.5)
    assert after["x"] == pytest.approx(start["x"]) and after["z"] == pytest.approx(start["z"])
    assert [move[0] for move in case.moves] == ["x", "y"]
    assert phase["readback"] == after and [i["axis"] for i in phase["xy_inputs"]] == ["x", "y"]
    count = len(case.moves)
    step._beside_first_touch(case.ctx)
    assert len(case.moves) == count
    state = m._transfer_state(case.ctx)
    state["phases"]["beside_first_touch_stamp_01"].update(complete=False)
    m._save_transfer(case.ctx, state)
    with pytest.raises(NeedsHumanError, match="Interrupted beside-source"):
        step._beside_first_touch(case.ctx)


@pytest.mark.parametrize("discovery", [False, True])
def test_stamp_discovery_keeps_the_head_on_its_admitted_clear_domain(process_case, monkeypatch, discovery):
    """With stamp discovery the head already sits on an all-material clear
    domain that is re-admitted at its exact centre, so no beside-source move."""
    case = new_design_case(process_case)
    case.ctx.params.setdefault("flake_ids", ["a"])
    if discovery:
        case.ctx.params["stamp_discovery_policy"] = {"fixture": True}
    calls = []

    class Stop(Exception):
        pass

    def begin(ctx, name, **kwargs):
        calls.append(name)
        raise Stop
    monkeypatch.setattr(m, "_transfer_state", lambda ctx: {
        "source_references": {ctx.params["flake_ids"][0]: {}}, "phases": {}})
    monkeypatch.setattr(m, "_validate_focus_reference", lambda *args, **kwargs: None)
    monkeypatch.setattr(m.AdjustFirstContact, "_beside_first_touch", lambda self, ctx: calls.append("beside"))
    monkeypatch.setattr(m.AdjustFirstContact, "_v1_initialize", lambda self, ctx: calls.append("initialize"))
    monkeypatch.setattr(m, "_begin_phase", begin)
    with pytest.raises(Stop):
        m.AdjustFirstContact("stamp_01", "v1_first_contact_stamp_01").act(case.ctx, None)
    first = "v1_first_contact_stamp_01"
    assert calls == ([first] if discovery else ["beside", "initialize", first])


def _landing_ctx(view_local, *, size=(10., 10.), flakes=()):
    from types import SimpleNamespace

    corner = (20., 85.)
    samples = {"chip": {"id": "chip", "kind": "chip", "props": {
        "corner_stage_xy": list(corner), "rotation_deg": 0., "size_mm": list(size)}}}
    for fid, xy, area in flakes:
        samples[fid] = {"id": fid, "kind": "flake", "props": {"rel_corner_mm": list(xy), "area_um2": area}}
    store = SimpleNamespace(get_sample=samples.get,
        list_samples=lambda kind=None, location=None: [v for v in samples.values() if v["kind"] == kind])
    pose = SimpleNamespace(x=corner[0]-view_local[0], y=corner[1]-view_local[1])
    gate = SimpleNamespace(stage=lambda _: SimpleNamespace(get_sample_pose=lambda: pose))
    ctx = SimpleNamespace(params={"flake_ids": ["src"], "flake_chips": {"src": "chip"}}, store=store, gate=gate)
    return ctx, SimpleNamespace(mm_per_pixel_x=2.6/480, mm_per_pixel_y=-1.72/320)


@pytest.mark.parametrize("view,size,flakes,direction", [
    ((5., 5.), (10., 10.), (), 0.),                             # nearest ring, image-up
    ((5., 9.6), (10., 10.), (), 67.5),                          # up-ish landings leave the chip
    ((5., 5.), (10., 10.), [("near", (5., 5.5), 400.)], 45.),   # a catalogued neighbour near up
    ((.3, .3), (.6, .6), (), None),                             # nothing on a tiny chip
])
def test_first_touch_landing_searches_near_to_far_on_chip_and_clear(view, size, flakes, direction):
    """#136 (Jialei): the first touch lands on a clean area beside the source."""
    policy = m._first_contact_sampling(load_yaml("verification.yaml"))
    ctx, calibration = _landing_ctx(view, size=size, flakes=flakes)
    landing = m._first_touch_landing(ctx, policy, calibration)
    chosen = landing["chosen"]
    if direction is None:
        assert chosen is None and landing["candidates"]
        return
    assert chosen["direction_deg"] == direction and chosen["distance_mm"] == pytest.approx(.35)
    if direction == 0.:
        assert chosen["landing_chip_local_mm"] == pytest.approx([5., 5.35])
        assert chosen["holder_delta_mm"] == pytest.approx([0., .35])
    if flakes:
        assert landing["candidates"][0]["rejected"] == ["near:near"]


def test_first_touch_landing_avoids_a_visible_uncatalogued_speck():
    from PIL import Image, ImageDraw

    policy = m._first_contact_sampling(load_yaml("verification.yaml"))
    ctx, calibration = _landing_ctx((5., 5.))
    image = Image.new("RGB", (480, 320), (125, 105, 170))
    up = (240, 160-.35/abs(calibration.mm_per_pixel_y))
    ImageDraw.Draw(image).ellipse((up[0]-2, up[1]-2, up[0]+2, up[1]+2), fill=(60, 50, 70))
    landing = m._first_touch_landing(ctx, policy, calibration, image)
    assert landing["image_checked"] and landing["candidates"][0]["rejected"] == ["visible_feature"]
    assert landing["chosen"] is not None and landing["chosen"]["direction_deg"] != 0.
    clean = m._first_touch_landing(ctx, policy, calibration, Image.new("RGB", (480, 320), (125, 105, 170)))
    assert clean["chosen"]["direction_deg"] == 0.


def _square(cx, cy, half):
    from qumus2.core.regions import as_region

    return as_region([(cx-half, cy-half), (cx+half, cy-half), (cx+half, cy+half), (cx-half, cy+half)])


@pytest.mark.parametrize("others,error", [
    ([], None),                                         # only the designed hBN
    ([(.2, .1, .02)], None),                            # a co-picked neighbour away from the Gr
    ([(.03, 0., .02)], "overlaps the selected Gr"),     # a co-picked neighbour touching the Gr
    ([(0., 0., .05)], "Exactly one carried hBN"),       # two carried hBN both cover the Gr
    ("shifted", "extends 7.0 um beyond the main carried hBN"),  # main hBN misaligned by 7 um
])
def test_gr_pickup_uses_the_one_carried_hbn_over_the_gr(monkeypatch, others, error):
    """#136: co-picked neighbours are allowed only when they stay off the Gr."""
    from types import SimpleNamespace

    monkeypatch.setattr(m, "_complete_detection_region", lambda ctx, frame, d: d.region)
    gr = _square(0., 0., .02)
    if others == "shifted":
        # Jialei 2026-11-03: the Gr must lie entirely within the MAIN hBN.
        carried = [SimpleNamespace(region=_square(.027, 0., .04)), SimpleNamespace(region=_square(.2, .1, .02))]
    else:
        carried = [SimpleNamespace(region=_square(0., 0., .04))]+[SimpleNamespace(region=_square(*o)) for o in others]
    if error:
        with pytest.raises(NeedsHumanError, match=error):
            m._carried_hbn_over_gr(None, None, carried, gr)
    else:
        assert m._carried_hbn_over_gr(None, None, carried, gr) == carried[0].region
    with pytest.raises(NeedsHumanError, match="not observable"):
        m._carried_hbn_over_gr(None, None, [], gr)


@pytest.mark.parametrize("front,changed,ring_wait,clear", [
    (False, False, False, True), (False, True, False, False), (False, True, True, True),
    (True, False, False, False), (True, True, True, False)])
def test_discovery_precontact_exposure_may_be_a_recorded_ring_wait_exposure(monkeypatch, front, changed,
                                                                           ring_wait, clear):
    """D8 waits for the ring family after the first changed region, so the
    exposure before the candidate may show the gap's ring tint (a518 seed 2).
    It is clear only when recorded as a ring-wait exposure and frontless."""
    from types import SimpleNamespace

    import qumus2.perception.tracking as tracking

    monkeypatch.setattr(m, "_reference", lambda record: record)
    monkeypatch.setattr(tracking, "contact_front_domain", lambda image: (SimpleNamespace(has_contact=front), None))
    monkeypatch.setattr(m, "v1_first_contact", lambda *args, **kwargs: {"found": changed})
    previous = {"sha256": "previous"}
    phase = {"reference": {"sha256": "reference"}, "ring_wait_images": ["previous"] if ring_wait else []}
    assert m._precontact_exposure_clear(phase, previous, None, .01) is clear


def test_observed_fine_descent_never_commands_upward_after_actuator_overshoot(process_case):
    """a518 seed 2 (default noise): a readback can land below the fixed
    intent before the last step, and chasing it would command upward inside
    the fine approach that discovery admits only as monotone downward steps."""
    case = new_design_case(process_case)
    case.manip.offset = -.0006
    phase = m._begin_phase(case.ctx, "v1_first_contact_stamp")
    assert m._guarded_z(case.ctx, phase, 6.4987, speed=.005, observe=lambda frame: False) is False
    assert all(target < before["z"] for axis, before, target, _ in case.moves if axis == "z")
    assert case.manip.positions["z"] < 6.4987


@pytest.mark.parametrize("case,error", [
    ("designed_only", None),
    ("neighbour_off_gr", None),
    ("neighbour_on_gr", "overlaps a loaded layer"),
    ("two_covering", "missing or ambiguous"),
    ("second_gr_off_stack", "unplanned non-hBN flake; release refused"),
    ("second_gr_under_main", "overlaps a loaded layer"),
    ("second_gr_on_gr", "missing or ambiguous"),
    ("main_misses_gr", "missing or ambiguous"),
    ("no_gr", "missing or ambiguous"),
])
def test_release_footprint_records_a_co_picked_flake_beside_the_designed_stack(monkeypatch, case, error):
    """Jialei 2026-11-03: a small neighbour flake picked up with the main hBN
    is normal; the footprint loads the designed hBN+Gr and records the extra.
    An unplanned Gr anywhere on the stamp refuses the release (Jialei
    2026-11-03; a518 seed 7 lifted a stray Gr with the bare film)."""
    from types import SimpleNamespace

    monkeypatch.setattr(m, "_complete_detection_region", lambda ctx, frame, d: d.region)
    def flake(material, *square):
        return SimpleNamespace(material=material, region=_square(*square), thickness_label="20-30")
    main, gr = flake("hBN", 0., 0., .04), flake("Gr", 0., 0., .02)
    detections = {"designed_only": [main, gr],
                  "neighbour_off_gr": [main, gr, flake("hBN", .2, .1, .02)],
                  "neighbour_on_gr": [main, gr, flake("hBN", .03, 0., .02)],
                  "two_covering": [main, gr, flake("hBN", 0., 0., .05)],
                  "second_gr_off_stack": [main, gr, flake("Gr", .2, .1, .01)],
                  "second_gr_under_main": [main, gr, flake("Gr", .035, .035, .01)],
                  "second_gr_on_gr": [main, gr, flake("Gr", .005, 0., .01)],
                  "no_gr": [main, flake("hBN", .2, .1, .02)],
                  "main_misses_gr": [flake("hBN", .05, 0., .04), gr, flake("hBN", .2, .1, .02)]}[case]
    if error:
        with pytest.raises(NeedsHumanError, match=error):
            m._designed_loaded_detections(None, None, detections, ["hBN", "Gr"])
        return
    chosen, extras = m._designed_loaded_detections(None, None, detections, ["hBN", "Gr"])
    assert chosen == [main, gr]
    assert [e["material"] for e in extras] == {"designed_only": [], "neighbour_off_gr": ["hBN"]}[case]
