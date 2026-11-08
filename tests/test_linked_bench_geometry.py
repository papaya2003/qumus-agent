"""Source-linked layout, safety and history contracts without a world or job."""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from types import SimpleNamespace

import pytest

from qumus2.config import load_yaml, simulation_profile
from qumus2.core.errors import PreconditionFailed, SafetyViolation
from qumus2.core.types import Pose
from qumus2.devices.base import Arm, DeviceBundle
from qumus2.flows.bench import arm_positions, positions, slots, stamp_handoff, to_arm, to_world
from qumus2.safety.gate import SafetyGate
from qumus2.safety.limits import Limits
from qumus2.testbench.bench_geometry import (
    PROFILE,
    admit_workspace_geometry,
    compose_bench_geometry,
    geometry_binding,
    limits_for_bench,
)
from qumus2.testbench.persist import mechanics_signature
from qumus2.testbench.reactor_config import with_reactor
from qumus2.ui.app import bench_layout


def layout(name="hallbar-linked"):
    config = simulation_profile(name)
    bench, config = with_reactor(load_yaml("bench.yaml"), config)
    return compose_bench_geometry(bench, config), config


def test_paired_slots_and_distinct_board_edges_share_one_frame():
    bench, _ = layout()
    expected = [(200, 160), (240, 160), (280, 160), (320, 160),
                (200, 110), (240, 110), (280, 110), (320, 110),
                (200, 60), (240, 60), (280, 60), (320, 60), (240, 20), (280, 20)]
    assert bench["arm_left"]["base"] == {"x": 320., "y": 0.}
    assert bench["arm_right"]["base"] == {"x": 840., "y": 0.}
    for index, (x, y) in enumerate(expected, 1):
        slot = bench["slots"][f"mid_chip_{index}"]
        world = Pose(x=slot["x"], y=slot["y"])
        assert slot["source_label"] == f"S{index}"
        assert to_arm(bench, "arm_left", world) == Pose(x=x, y=y)
        assert to_arm(bench, "arm_right", world) == Pose(x=x-520, y=y)
        for arm in ("arm_left", "arm_right"):
            assert to_world(bench, arm, to_arm(bench, arm, world)) == world
    # the v1 fiducial centres are unchanged; the drawn board holds their 36 mm printed squares
    assert bench["qr_markers"]["middle_storage"] == [[512, 15], [648, 15], [648, 210], [512, 210]]
    assert bench["stations"]["middle_storage"]["region"] == {"x": [494, 666], "y": [-3, 228]}
    assert len([s for s in bench["slots"] if s.startswith("mid_chip_")]) == 14
    # v1 let the arms use only 7 of them; the SIM opens all 14 (owner, 2026-11-03; #139)
    allowed = {f"mid_chip_{i}" for i in range(1, 15)}
    assert {s for s in allowed if bench["slots"][s]["v1_robot_accessible"]} == {
        f"mid_chip_{i}" for i in (1, 2, 5, 6, 9, 10, 13)}
    assert set(slots(bench, "middle_storage")) == allowed
    for arm in ("arm_left", "arm_right"):
        assert {s for s in arm_positions(bench, arm) if s.startswith("mid_chip_")} == allowed


def test_common_stage_optical_holder_and_tape_geometry():
    bench, _ = layout()
    assert positions(bench)["exfoliation_stage"] == {"x": 320., "y": 256.}
    assert positions(bench)["transfer_stage"] == {"x": 850., "y": 300.}
    assert to_arm(bench, "arm_right", Pose(x=850, y=300)) == Pose(x=10, y=300)
    assert bench["microscope"]["center"] == {"x": 850., "y": 380.}
    assert bench["transfer_arm"]["origin"] == {"x": 810., "y": 290.}
    # a 75 mm stamp slide (dome 7 mm from its left end): its centre sits 30.5 mm right of the dome
    assert stamp_handoff(bench) == {"x": 990.5, "y": 300.}
    assert bench["exfoliator"]["origin"] == {"x": 360., "y": 256.}
    assert bench["exfoliator"]["rail"] == {"x": [199., 442.], "y": 292.}
    projection = bench_layout(bench)
    assert projection["geometry_profile"] == bench["geometry_profile"]
    assert projection["positions"] == positions(bench)
    assert "estimated" in projection["geometry_profile"]
    assert "unknown" in projection["geometry_profile"]["uncertainty"]


def test_complete_reactor_group_preserves_local_vectors_and_parameters():
    original, config = with_reactor(load_yaml("bench.yaml"), simulation_profile("hallbar-linked"))
    before = deepcopy(original)
    bench = compose_bench_geometry(original, config)
    assert original == before  # Including aliased model/bench region references.
    old, new = before["reactor_module"]["geometry"], bench["reactor_module"]["geometry"]
    for key in ("load", "microscope_center", "storage_origin"):
        assert new[key] == {"x": old[key]["x"]+208, "y": old[key]["y"]-28.5}
    # Arm 3 stands at the front edge like Arms 1 and 2 (owner, 2026-11-02): x moves with the group, y = 0
    assert new["arm_base"] == {"x": old["arm_base"]["x"]+208, "y": 0.}
    for key in ("chamber_region", "storage_region"):
        assert new[key] == {"x": [x+208 for x in old[key]["x"]], "y": [y-28.5 for y in old[key]["y"]]}
    for key in set(old)-{"load", "microscope_center", "arm_base", "storage_origin", "chamber_region", "storage_region", "table"}:
        assert new[key] == old[key], key
    for key in set(before["reactor_module"])-{"geometry"}:
        assert bench["reactor_module"][key] == before["reactor_module"][key], key
    assert bench["arm_reactor"]["base"] == {"x": 1288., "y": 0.}
    assert bench["arm_reactor"]["reach_mm"] == 450.  # synthetic assumption, was 280 before the move
    assert bench["stations"]["reactor"]["region"] == new["chamber_region"]
    assert bench["stations"]["reactor_storage"]["region"] == new["storage_region"]
    assert bench["slots"]["reactor_port"]["x"] == new["load"]["x"]
    assert bench["slots"]["reactor_port"]["y"] == new["load"]["y"]
    assert new["load"]["x"]-new["arm_base"]["x"] == old["load"]["x"]-old["arm_base"]["x"]
    assert new["load"]["y"]-new["arm_base"]["y"] == old["load"]["y"]-old["arm_base"]["y"]+97.5
    assert new["microscope_center"]["y"]-new["load"]["y"] == new["process_y_mm"] == 160
    assert new["chamber_region"]["y"][0]-new["load"]["y"] == new["chamber_entry_y_mm"] == 60
    assert bench["table"] == bench["regions"]["workspace"] == new["table"] == {"x": [0, 1648], "y": [0, 630]}
    board, arm = bench["stations"]["right_storage"]["region"], bench["arm_reactor"]["base"]
    assert not (board["x"][0] <= arm["x"] <= board["x"][1] and board["y"][0] <= arm["y"] <= board["y"][1])


@pytest.mark.parametrize("key", ["load", "microscope_center", "arm_base", "storage_origin",
                                 "chamber_region", "storage_region", "table", "extra", "missing"])
def test_unknown_reactor_world_schema_or_changed_source_anchor_refuses_without_mutation(key):
    bench, config = with_reactor(load_yaml("bench.yaml"), simulation_profile("hallbar-linked"))
    geometry = bench["reactor_module"]["geometry"]
    if key == "extra":
        geometry["future_world_anchor"] = {"x": 1220, "y": 300}
    elif key == "missing":
        del geometry["microscope_center"]
    elif isinstance(geometry[key]["x"], list):
        geometry[key]["x"][0] += .001
    else:
        geometry[key]["x"] += .001
    before = deepcopy(bench)
    with pytest.raises(ValueError):
        compose_bench_geometry(bench, config)
    assert bench == before


def test_custom_local_reactor_physics_and_dimensions_are_preserved():
    config = simulation_profile("hallbar-linked")
    config["reactor"]["reaction"] = {"activation_temperature_c": 675, "activation_flow_sccm": 12}
    bench, config = with_reactor(load_yaml("bench.yaml"), config)
    bench["reactor_module"]["geometry"]["rod_width_mm"] = 3.5
    before = deepcopy(bench["reactor_module"])
    result = compose_bench_geometry(bench, config)
    assert result["reactor_module"]["reaction"] == before["reaction"]
    assert result["reactor_module"]["geometry"]["rod_width_mm"] == 3.5


def test_profile_safety_only_rebases_explicit_xy_and_preserves_every_other_limit():
    bench, _ = layout()
    base = load_yaml("limits.yaml")
    result = limits_for_bench(bench, base)
    assert base == load_yaml("limits.yaml")
    for arm in ("arm_left", "arm_right", "arm_reactor"):
        assert result["arms"][arm]["reach_mm"] == 450
        assert result["arms"][arm]["x"] == [-450, 450]
        assert result["arms"][arm]["y"] == [-80, 450]
        for name, target in arm_positions(bench, arm).items():
            assert math.hypot(target.x, target.y) < 450, name
            assert -450 <= target.x <= 450 and -80 <= target.y <= 450
    # Arm 3 moved 97.5 mm toward the operator (owner, 2026-11-02): its reactor loading
    # region keeps its world place, so in Arm 3's frame it moves 97.5 mm further out
    (lock,), (original,) = result["reactor_interlocks"], base["reactor_interlocks"]
    assert lock["loading_region"] == {"x": original["loading_region"]["x"],
                                      "y": [y + 97.5 for y in original["loading_region"]["y"]]}
    assert {**lock, "loading_region": original["loading_region"]} == original
    assert {k: v for k, v in result["arms"]["arm_reactor"].items() if k not in ("x", "y", "reach_mm")} == \
        {k: v for k, v in base["arms"]["arm_reactor"].items() if k not in ("x", "y", "reach_mm")}
    for group in ("stages", "manipulators", "heaters", "microscopes", "tapes", "gas_flows"):
        assert result[group] == base[group], group
    assert result["geometry"]["stage_origin_xy"] == [850, 300]
    assert result["geometry"]["manipulator_origin_xy"] == [810, 290]
    assert result["geometry"]["objective_xy"] == [850, 380]
    # Normalize permitted edits, then compare the entire remaining safety contract.
    normalized = deepcopy(result)
    normalized["reactor_interlocks"] = base["reactor_interlocks"]
    for arm in ("arm_left", "arm_right", "arm_reactor"):
        for key in ("x", "y", "reach_mm"):
            normalized["arms"][arm][key] = base["arms"][arm][key]
    for name in ("interlocks", "stamp_interlocks"):
        for actual, original in zip(normalized[name], base[name], strict=True):
            if "region" in original:
                assert all(actual["region"][a][1]-actual["region"][a][0] == original["region"][a][1]-original["region"][a][0] for a in original["region"])
                actual["region"] = original["region"]
    normalized["arms"]["arm_left"]["floors"] = base["arms"]["arm_left"]["floors"]
    for key in ("stage_origin_xy", "manipulator_origin_xy", "objective_xy", "calibration_id"):
        normalized["geometry"][key] = base["geometry"][key]
    assert normalized == base


class RecordingArm(Arm):
    def __init__(self):
        super().__init__("arm_left")
        self.commands = []

    def move_to(self, pose, speed=None):
        self.commands.append(pose)

    def get_pose(self):
        return Pose()

    def home(self):
        pass

    def set_vacuum(self, on):
        pass

    def vacuum_on(self):
        return False


@pytest.mark.parametrize("pose", [Pose(x=450.001, y=0), Pose(x=400, y=300), Pose(x=0, y=-80.001), Pose(x=0, y=256, z=.59)])
def test_actual_gate_refuses_reach_xy_and_rebased_floor_before_any_device_command(pose):
    bench, _ = layout()
    arm, bundle = RecordingArm(), DeviceBundle()
    bundle.add(arm)
    gate = SafetyGate(bundle, Limits.load(limits_for_bench(bench, load_yaml("limits.yaml"))))
    with pytest.raises(SafetyViolation):
        gate.arm("arm_left").move_to(pose)
    assert not arm.commands


@pytest.mark.parametrize("name", ["previous", "integrated", "hallbar"])
def test_existing_profiles_layout_limits_and_signature_behavior_are_untouched(name):
    config = simulation_profile(name)
    original, config = with_reactor(load_yaml("bench.yaml"), config)
    bench = compose_bench_geometry(original, config)
    assert bench is original and "geometry_profile" not in bench
    limits = load_yaml("limits.yaml")
    assert limits_for_bench(bench, limits) is limits
    lab = SimpleNamespace(bench=bench, world=SimpleNamespace(model=config))
    before = mechanics_signature(lab)
    bench["arm_left"]["base"]["x"] += .1
    assert mechanics_signature(lab) == before  # Do not reprice/reinterpret legacy signature inputs.


@pytest.mark.parametrize("field", ["base", "slot", "limits", "profile"])
def test_linked_current_geometry_or_safety_binding_cannot_mutate(field):
    bench, config = layout()
    if field == "base":
        bench["arm_right"]["base"]["x"] += .001
    elif field == "slot":
        bench["slots"]["mid_chip_1"]["x"] += .001
    elif field == "profile":
        bench["geometry_profile"]["id"] = "unknown"
    else:
        bench["geometry_profile"]["limits_sha256"] = "0"*64
    with pytest.raises(ValueError):
        limits_for_bench(bench, load_yaml("limits.yaml"))
    if field != "limits":
        with pytest.raises(ValueError):
            mechanics_signature(SimpleNamespace(bench=bench, world=SimpleNamespace(model=config)))


def test_linked_signature_includes_geometry_and_profile_is_not_composed_twice():
    bench, config = layout()
    binding = geometry_binding(bench)
    assert len(binding["layout_sha256"]) == len(binding["limits_sha256"]) == 64
    with pytest.raises(ValueError, match="already composed"):
        compose_bench_geometry(bench, config)
    before, old_config = layout("hallbar")
    assert mechanics_signature(SimpleNamespace(bench=before, world=SimpleNamespace(model=old_config))) != mechanics_signature(
        SimpleNamespace(bench=bench, world=SimpleNamespace(model=config)))


@pytest.mark.parametrize("existing,selection,occupied,restoring,allowed", [
    (None, None, True, False, True), (None, {"profile": PROFILE}, False, False, True),
    (None, {"profile": PROFILE}, True, True, False), (PROFILE, None, True, True, False),
    (PROFILE, {"profile": PROFILE}, True, False, False),
    (PROFILE, {"profile": PROFILE}, True, True, True), (None, {"profile": "unknown"}, False, False, False)])
def test_workspace_geometry_guard_is_read_only(existing, selection, occupied, restoring, allowed):
    store = SimpleNamespace(get_meta=lambda _: existing, list_samples=lambda: [1] if occupied else [],
                            list_runs=lambda: [], list_projects=lambda: [])
    config = {"bench_geometry": selection} if selection is not None else {}
    if allowed:
        admit_workspace_geometry(store, config, restoring=restoring)
    else:
        with pytest.raises(PreconditionFailed, match="history/ledger have not been reset"):
            admit_workspace_geometry(store, config, restoring=restoring)


def test_application_refuses_old_ledger_before_any_lab_construction(monkeypatch):
    from qumus2 import app

    closed = []
    store = SimpleNamespace(get_meta=lambda _: None, list_samples=lambda: ["old sample"],
        list_runs=lambda: [], list_projects=lambda: [], close=lambda: closed.append(True))
    monkeypatch.setattr(app, "RunStore", lambda *a, **kw: store)
    monkeypatch.setattr(app, "build_sim_lab", lambda *a, **kw: pytest.fail("Old ledger reached lab construction"))
    with pytest.raises(PreconditionFailed, match="fresh workspace"):
        app.build_lab_side(testbench_config=simulation_profile("hallbar-linked"))
    assert closed == [True]


def test_old_checkpoint_rejected_before_any_world_overlay(tmp_path):
    import pickle

    from qumus2.testbench.persist import load_lab_state

    old, old_config = layout("hallbar")
    new, new_config = layout()
    before = SimpleNamespace(bench=old, world=SimpleNamespace(model=old_config))
    target = SimpleNamespace(bench=new, world=SimpleNamespace(model=new_config))
    path = tmp_path/"preserved.pkl"
    path.write_bytes(pickle.dumps({"mechanics_version": 2, "mechanics_signature": mechanics_signature(before)}))
    raw = path.read_bytes()
    with pytest.raises(PreconditionFailed, match="incompatible"):
        load_lab_state(target, None, path)
    assert path.read_bytes() == raw and vars(target.world) == {"model": new_config}


def test_future_campaign_rejects_previous_geometry_without_writing_it(tmp_path):
    from test_imported_hallbar_target import declared_import

    from scripts import record_hallbar_demos as recorder

    target = declared_import()
    bundle = tmp_path/"declared-target"
    bundle.mkdir()
    (bundle/"target.json").write_text(json.dumps(target.model_dump(mode="json")))
    (bundle/"electrode-layout.gds").write_bytes(b"UNIT ONLY; NOT ACTUAL GDS")
    campaign = recorder.campaign_template({}, campaign_path=tmp_path/"campaign.json",
        initial_state=tmp_path/"initial.json", experience=tmp_path/"experience.json", target_bundle=bundle)
    assert recorder.simulation_config(target_bundle=bundle)["bench_geometry"] == simulation_profile("hallbar-linked")["bench_geometry"]
    assert campaign["simulation_profile"] == "hallbar-linked"
    assert campaign["protocol_revision"] == recorder.PROTOCOL_REVISION
    assert len(campaign["cells"]) == 15 and campaign["seed"] == 8
    recorder.validate_campaign(campaign)
    campaign.pop("protocol_revision")
    path = tmp_path/"historical.json"
    path.write_text(json.dumps(campaign))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="frozen"):
        recorder.validate_campaign(json.loads(path.read_text()))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_outer_boards_follow_the_owner_sketch_and_v1_camera_records():
    bench, _ = layout()
    original = load_yaml("bench.yaml")["slots"]
    q = bench["qr_size_mm"]["left_storage"] / 2
    for board, arm in (("left_storage", "arm_left"), ("right_storage", "arm_right")):
        region, base = bench["stations"][board]["region"], bench[arm]["base"]
        assert board in bench[arm]["reach"]
        # the printed QR squares fill the board's corners on one side only
        markers = bench["qr_markers"][board]
        assert len(markers) == 2
        for x, y in markers:
            assert min(abs(x - q - region["x"][0]), abs(x + q - region["x"][1])) == 0
            assert min(abs(y - q - region["y"][0]), abs(y + q - region["y"][1])) == 0
        for slot_id, slot in bench["slots"].items():
            if slot["station"] != board:
                continue
            assert math.hypot(slot["x"] - base["x"], slot["y"] - base["y"]) <= bench[arm]["reach_mm"]
            w, h = slot.get("frame") or slot["size"]
            ox = (slot.get("frame_offset") or [0, 0])[0]
            assert region["x"][0] <= slot["x"] + ox - w / 2 and slot["x"] + ox + w / 2 <= region["x"][1], slot_id
            assert region["y"][0] <= slot["y"] - h / 2 and slot["y"] + h / 2 <= region["y"][1], slot_id
    # every old slot id keeps its station and the kind it holds; new frames are only added
    for slot_id, slot in original.items():
        if slot["station"] in ("left_storage", "right_storage"):
            assert (bench["slots"][slot_id]["station"], bench["slots"][slot_id]["holds"]) == (slot["station"], slot["holds"])
    # chip and tape frames sit on the middle board's v1 grid, measured from each board's own QR codes
    def arm(slot_id, name):
        return to_arm(bench, name, Pose(x=bench["slots"][slot_id]["x"], y=bench["slots"][slot_id]["y"]))

    mid = {(x - 192, y - 15) for x, y in ((200, 160), (240, 160), (280, 160), (320, 160), (200, 110), (240, 110),
                                          (280, 110), (320, 110), (200, 60), (240, 60), (280, 60), (320, 60),
                                          (240, 20), (280, 20), (240, 200), (280, 200))}
    rows = {y for _, y in mid}
    for i in range(1, 10):
        p = arm(f"left_chip_{i}", "arm_left")
        assert (p.x + 320) in {8, 48, 88, 128} and (p.y - 18) in rows
    for i in range(1, 9):
        p = arm(f"right_chip_{i}", "arm_right")
        assert (326 - p.x, p.y - 14) in mid
    assert [arm(f"left_chip_{i}", "arm_left") for i in (1, 2, 3)] == [Pose(x=-312, y=y) for y in (163, 113, 63)]
    assert [arm(f"left_slide_{i}", "arm_left") for i in (1, 2, 3)] == [Pose(x=-232, y=y) for y in (63, 113, 163)]
    # tape and stamp frames are the same size
    assert bench["slots"]["left_slide_1"]["frame"] == bench["slots"]["right_stamp_1"]["frame"] == [84, 28]
    # frames on a board never overlap
    for board in ("left_storage", "right_storage"):
        boxes = []
        for slot in (s for s in bench["slots"].values() if s["station"] == board):
            w, h = slot["frame"]
            cx = slot["x"] + (slot.get("frame_offset") or [0, 0])[0]
            boxes.append((cx, slot["y"], w, h))
        for i, (x, y, w, h) in enumerate(boxes):
            for x2, y2, w2, h2 in boxes[i + 1:]:
                assert abs(x - x2) >= (w + w2) / 2 or abs(y - y2) >= (h + h2) / 2
    # the three boards are the same physical board
    sizes = {tuple(round(r[a][1] - r[a][0]) for a in ("x", "y"))
             for r in (bench["stations"][b]["region"] for b in ("left_storage", "middle_storage", "right_storage"))}
    assert {w for w, _ in sizes} == {172} and max(h for _, h in sizes) - min(h for _, h in sizes) <= 1
    # real-size items: 75 x 25 mm slides along x (owner, 2026-11-02), hand-cleaved chips at
    # v1's measured median 27.8 x 34.9 mm (owner, 2026-11-04, #206);
    # the electrode chips on the right board keep their design size
    for slot_id, slot in bench["slots"].items():
        if slot["holds"] in ("carrier", "stamp") and slot["station"] in ("left_storage", "right_storage"):
            assert slot["size"] == [75, 25] and slot["size"][0] <= slot["frame"][0] - 2, slot_id
        if slot["holds"] == "chip" and slot["station"] in ("left_storage", "middle_storage"):
            assert slot["size"] == [27.8, 34.9] and slot["size_jitter_mm"] == [1.5, 2.0], slot_id
            assert all(s + 2 * j <= f for s, j, f in zip(slot["size"], slot["size_jitter_mm"], slot["frame"], strict=True))
    assert bench["stamp_slide"] == {"size": [75, 25], "dome_inset_mm": 7}
    # stamp rows still come from v1's camera records
    assert [arm(f"right_stamp_{i}", "arm_right").y for i in (1, 2, 3, 4)] == [187, 111, 72, 35]
    # stamps keep the stamp_slide geometry: the dome sits over a gap in the comb-shaped edge
    inset, length = bench["stamp_slide"]["dome_inset_mm"], bench["stamp_slide"]["size"][0]
    notches = bench["stations"]["right_storage"]["notches"]
    for i in range(1, 6):
        slot = bench["slots"][f"right_stamp_{i}"]
        dome = slot["x"] - length / 2 + inset
        assert any(n["x"][0] <= dome <= n["x"][1] and n["y"][0] <= slot["y"] <= n["y"][1] for n in notches)
