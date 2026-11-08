"""Lab twin (runtime/twin.py): `lab.frame` after each command, bench geometry,
camera previews for the UI."""

from __future__ import annotations

import math

from fastapi.testclient import TestClient

from qumus2.app import build_services
from qumus2.config import load_yaml
from qumus2.flows.bench import (
    manipulator_head,
    over_stage,
    positions,
    regions,
    stamp_dome_offset,
    stamp_handoff,
    stamp_seat,
)
from qumus2.ui.app import create_app
from tests.conftest import quiet_bench


def frames(s, after=0):
    return [e.payload for e in s.store.events_after(after, limit=1_000_000)
            if e.kind == "lab.frame"]


def test_frames_follow_the_devices_and_the_chip(services):
    s = services
    mark = s.store.last_event_id()
    assert s.jobs.wait(s.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_001"}),
                       60).status == "succeeded"
    fr = frames(s, mark)
    assert fr and all(f["device"] and f["command"] for f in fr)
    # every arm move shows up with the arm's world position
    moves = [f for f in fr if "arms" in f and "arm_right" in f["arms"]]
    assert moves
    last = {}
    for f in fr:
        for k, v in f.get("objects", {}).items():
            last[k] = v
    chip = last["chip_001"]
    stage = next(p for n, p in positions(s.lab.bench).items() if n == "transfer_stage")
    assert abs(chip["x"] - stage["x"]) < 1 and abs(chip["y"] - stage["y"]) < 1
    assert chip["on"] == "transfer_stage" and chip["held_by"] is None
    # a capture changes nothing: no frame for it
    n = len(frames(s))
    s.gate.camera("overview_camera").capture()
    assert len(frames(s)) == n


def test_stage_moves_carry_the_chip_to_the_microscope(services):
    s = services
    assert s.jobs.wait(s.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_001"}),
                       60).status == "succeeded"
    mark = s.store.last_event_id()
    ta = s.lab.bench["transfer_stage"]["microscope"]
    from qumus2.core.types import Pose

    s.gate.stage("transfer_stage").move_to(Pose(x=ta["x"], y=ta["y"]))
    motion = frames(s, mark)
    assert len(motion) > 1  # intermediate Clock-bound poses are replayable
    f = next(f for f in reversed(motion) if "stages" in f)
    assert f["stages"]["transfer_stage"]["x"] == ta["x"]
    c = s.lab.bench["microscope"]["center"]
    chip = f["objects"]["chip_001"]
    assert abs(chip["x"] - c["x"]) < 1 and abs(chip["y"] - c["y"]) < 1


def test_stamp_holder_axes_and_handoff():
    s = build_services(":memory:", seed=1, testbench_config=quiet_bench())
    b = s.lab.bench
    ta, c = b["transfer_arm"], b["microscope"]["center"]
    x, y = manipulator_head(b, ta["over_stage"])
    assert (x, y) == (c["x"], c["y"])
    # a pure x/y/z stage: one mm of x moves the head one mm along world x
    x2, y2 = manipulator_head(b, {"x": ta["over_stage"]["x"] + 10, "y": ta["over_stage"]["y"]})
    assert (x2 - x, y2 - y) == (10, 0)
    h = stamp_handoff(b)
    assert positions(b)["stamp_handoff"] == h
    r = regions(b)["stamp_handoff"]
    assert r["x"][0] < h["x"] < r["x"][1] and r["y"][0] < h["y"] < r["y"][1]
    # the hand-off sits at the bottom right of the search & stacking module
    # (Jialei's sketch, 2026-10-22): right of and below the objective, inside
    # the module, never over the stage
    mod = b["stations"]["transfer_stage"]["module"]
    assert h["x"] > c["x"] + 50 and h["y"] < c["y"] - 50
    assert mod["x"][0] < h["x"] < mod["x"][1] and mod["y"][0] < h["y"] < mod["y"][1]
    # the holder starts at the hand-off; between steps that keep the same stamp
    # it waits at the rail corner, where the y leg from the hand-off meets the
    # x leg to the objective (owner, 2026-11-04, #181), never over the stage
    assert manipulator_head(b, ta["home"]) == manipulator_head(b, ta["load"])
    park = manipulator_head(b, ta["park"])
    assert park == (manipulator_head(b, ta["load"])[0], c["y"])
    assert not over_stage(b, ta["park"])
    assert mod["x"][0] < park[0] < mod["x"][1] and mod["y"][0] < park[1] < mod["y"][1]
    # arm_right holds a slide by its centre and puts it down so that the dome
    # lands exactly under the head: nothing shifts at the hand-off
    dx, dy = stamp_dome_offset(b)
    assert (dx, dy) == (-13.0, 0.0)  # 40 mm slide, dome 7 mm from its left end
    assert (h["x"] + dx, h["y"] + dy) == manipulator_head(b, ta["load"])
    assert stamp_seat(b, ta["load"]) == (h["x"], h["y"])
    # stamps are slide-sized and lie along x everywhere, as on the holder
    stamps = [o for o in s.lab.world.snapshot() if o["kind"] == "stamp"]
    assert stamps and all(o["size_mm"] == [40.0, 25.0] for o in stamps)


def test_stamp_slots_fit_the_board_and_arm_right_reaches_them():
    b = load_yaml("bench.yaml")
    m = b.get("slot_margin_mm", 3.0)
    board = b["stations"]["right_storage"]["region"]
    slots = {n: sl for n, sl in b["slots"].items() if sl["station"] == "right_storage"}
    assert sum(sl["holds"] == "stamp" for sl in slots.values()) == 3
    for n, sl in slots.items():
        w, h = sl["size"]
        assert board["x"][0] < sl["x"] - w / 2 and sl["x"] + w / 2 < board["x"][1], n
        assert board["y"][0] < sl["y"] - h / 2 and sl["y"] + h / 2 < board["y"][1], n
        if sl["holds"] == "stamp":
            assert (w, h) == tuple(b["stamp_slide"]["size"]), n
    names = sorted(slots)
    for i, a in enumerate(names):  # frames plus their margins do not overlap
        for c in names[i + 1:]:
            p, q = slots[a], slots[c]
            assert (abs(p["x"] - q["x"]) >= (p["size"][0] + q["size"][0]) / 2 + 2 * m
                    or abs(p["y"] - q["y"]) >= (p["size"][1] + q["size"][1]) / 2 + 2 * m), (a, c)
    base, reach = b["arm_right"]["base"], b["arm_right"]["reach_mm"]
    lim = load_yaml("limits.yaml")["arms"]["arm_right"]
    for n, pt in [*((n, slots[n]) for n in names), ("stamp_handoff", stamp_handoff(b))]:
        ax, ay = pt["x"] - base["x"], pt["y"] - base["y"]
        assert math.hypot(ax, ay) <= reach, n
        assert lim["x"][0] <= ax <= lim["x"][1] and lim["y"][0] <= ay <= lim["y"][1], n


def test_exfoliator_upper_roller_covers_the_stage_while_applied(services):
    s = services
    mark = s.store.last_event_id()
    assert s.jobs.wait(s.jobs.submit("exfoliate_chips", {"material": "Gr"}), 120).status \
        == "succeeded"
    ex = [f["exfoliator"] for f in frames(s, mark) if "exfoliator" in f]
    applied = [e for e in ex if e["applied"]]
    assert applied
    stage = s.lab.bench["stations"]["exfoliation_stage"]["region"]
    # tape runs from the fixed roller (right of the stage) to the upper one (left of it)
    origin = s.lab.bench["exfoliator"]["origin"]["x"]
    assert all(e["roller_x"] <= stage["x"][0] for e in applied)
    assert origin >= stage["x"][1]
    assert ex[-1]["roller_x"] == origin and not ex[-1]["applied"]


def test_camera_previews(tmp_path):
    s = build_services(str(tmp_path / "x.db"), seed=1,
                       testbench_config=quiet_bench())
    with TestClient(create_app(s, start_driver=False)) as c:
        ov = c.get("/api/camera/overview_camera/latest").json()
        assert ov["image"].startswith("/api/image?path=")
        assert c.get(ov["image"]).status_code == 200
        mic = c.get("/api/camera/microscope/latest").json()
        assert mic["image"] and mic["meta"]["objective"] == 10  # current blank field is visible
        assert mic["meta"]["verification"] is False
        frame = s.gate.microscope("microscope").capture()
        frame.saved_path()
        mic = c.get("/api/camera/microscope/latest").json()
        assert mic["image"] and c.get(mic["image"]).status_code == 200
        assert c.get("/api/camera/nope/latest").status_code == 404
        twin = c.get("/api/bench").json()["twin"]
        assert {"arms", "stages", "manipulator", "exfoliator", "objects"} <= set(twin)
    s.lab.devices.get("overview_camera").offline = True
    with TestClient(create_app(s, start_driver=False)) as c:
        assert c.get("/api/camera/overview_camera/latest").json()["image"] is None


def test_geometry_fields_for_the_page(services):
    """The additive fields the page's bench drawing reads (2026-10-22): the
    stamp holder over the stage with its gap and stamp, the exfoliator's rolls,
    peel angle and stage vacuum, which plane the microscope sees sharp."""
    s = services
    st = s.lab.twin.state()
    m, ex, mic = st["manipulator"], st["exfoliator"], st["microscope"]
    assert m["over_stage"] is False and m["stamp"] is None and isinstance(m["gap_mm"], float)
    assert set(ex["rolls"]) == {"movable", "fixed"} and ex["rolls"]["fixed"]["x"] == ex["tape"]["x1"]
    assert ex["vacuum"] is False and ex["peel_angle_deg"] == 30.0
    assert mic["focus_plane"] is None
    assert s.jobs.wait(s.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_001"}),
                       60).status == "succeeded"
    s.gate.switch("exfoliation_vacuum").set(True)
    st = s.lab.twin.state()
    assert st["exfoliator"]["vacuum"] is True
    # Independent microscope focus leaves the stage and its mechanical gap alone.
    stage = s.gate.stage("transfer_stage")
    from qumus2.core.types import Pose

    ta = s.lab.bench["transfer_stage"]["microscope"]
    stage.move_to(Pose(x=ta["x"], y=ta["y"]))
    mic_dev = s.lab.devices.get("microscope")
    best = mic_dev._view().get("best_focus_z")
    if best is not None:
        before = stage.get_pose()
        s.gate.microscope("microscope").set_focus(best)
        assert stage.get_pose() == before
        assert s.lab.twin.state()["microscope"]["focus_plane"] == "chip"
