"""Saved evidence constructs an observed scene independently of physical truth."""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from qumus2.core.clock import RealClock, SimClock
from qumus2.records.lab_geometry import (
    LabEvidence,
    Source,
    evidence_view,
    observed_scene,
    record_evidence,
)
from qumus2.records.materials import review_material
from qumus2.runtime.store import RunStore
from qumus2.ui.app import bench_state
from qumus2.ui.recording import Recorder


def cv(store, x=12, *, frame="world", subject="region:fixture", age=0):
    return LabEvidence(subject=subject, property="overview_observation", bench=store.bench,
        value={"detections": [{"label": "chip", "pose": {"x": x, "y": 25}, "confidence": .8,
                               "sim_id": "hidden-truth-id", "sample_id": "chip_001"}]},
        units="mm,deg", frame=frame, observed_at=store.clock.now()-age, timebase="store_clock",
        source=Source(kind="synthetic", reference="camera fixture"))


def test_cv_observation_moves_scene_without_identity_or_truth_feedback(services):
    s = services
    before = s.lab.world.evaluation_snapshot()
    record_evidence(s.store, cv(s.store))
    first = observed_scene(s.store)
    obj, = first["objects"].values()
    assert obj["x"] == 12 and obj["subject"] is None and obj["identity_status"] == "unidentified"
    assert "size_mm" not in obj and "hidden-truth-id" not in json.dumps(first)
    record_evidence(s.store, cv(s.store, 31), supersede_previous=True)
    moved, = observed_scene(s.store)["objects"].values()
    assert moved["x"] == 31
    assert s.lab.world.evaluation_snapshot() == before


def test_first_bench_poll_does_not_repair_initialized_physical_state(services):
    before = services.lab.world.evaluation_snapshot()
    bench_state(services)
    assert services.lab.world.evaluation_snapshot() == before


@pytest.mark.parametrize("hidden_id", ["hidden-truth-id", "different-hidden-id"])
def test_hidden_detector_extras_do_not_influence_observed_conflicts(services, hidden_id):
    store = services.store
    record_evidence(store, cv(store))
    second = cv(store)
    second.value["detections"][0]["sim_id"] = hidden_id
    record_evidence(store, second)
    scene = observed_scene(store)
    obj, = scene["objects"].values()
    assert obj["x"] == 12 and obj["evidence"]["status"] == "recorded"
    assert len(obj["evidence"]["assertion_ids"]) == 2
    assert "hidden" not in json.dumps(scene)
    # Raw evidence remains immutable; only this consumer's equality is scoped.
    raw = evidence_view(store)["facts"][0]
    assert raw["status"] == ("recorded" if hidden_id == "hidden-truth-id" else "conflicted")
    assert raw["assertions"][-1]["value"] == second.value
    record_evidence(store, cv(store, x=13))
    visible_conflict = observed_scene(store)
    assert not visible_conflict["objects"]
    assert visible_conflict["unresolved"][0]["reason"] == "conflicted"


def test_admitted_human_semantics_change_unlocated_subject_attributes(services):
    s = services
    assert not observed_scene(s.store)["subjects"]
    review_material(s.store, s.lab.bench, "carrier_gr_01", expected_material="Gr", by="Ada", status="confirmed")
    subject, = observed_scene(s.store)["subjects"]
    assert subject["subject"] == "carrier_gr_01" and subject["pose_key"] is None
    attribute = subject["attributes"]["material_identity"]
    assert attribute["value"] == "Gr" and attribute["evidence"]["source"]["reporter"] == "Ada"
    assert attribute["evidence"]["freshness"]["status"] == "unknown"


def test_declared_pose_binds_semantics_but_conflicting_pose_aliases_stay_unlocated(services):
    s = services
    review_material(s.store, s.lab.bench, "carrier_gr_01", expected_material="Gr", by="Ada", status="confirmed")
    for prop, x in [("pose", 17), ("position", 29)]:
        record_evidence(s.store, LabEvidence(subject="carrier_gr_01", property=prop, bench="sim",
            value={"x": x, "y": 43}, units="mm", frame="world", observed_at=s.clock.now(),
            timebase="store_clock", source=Source(kind="synthetic", reference="declared pose fixture")))
        scene = observed_scene(s.store)
        if prop == "pose":
            assert scene["objects"]["carrier_gr_01"]["attributes"]["material_identity"]["value"] == "Gr"
    assert not scene["objects"] and scene["subjects"][0]["pose_key"] is None
    assert sum(row["reason"] == "conflicting_subject_poses" for row in scene["unresolved"]) == 2


def test_conflicts_stale_and_unknown_frames_are_unresolved(services):
    store = services.store
    record_evidence(store, cv(store, subject="region:conflict"))
    record_evidence(store, cv(store, 42, subject="region:conflict"))
    record_evidence(store, cv(store, age=301, subject="region:old"))
    record_evidence(store, cv(store, frame="unknown_camera", subject="region:unregistered"))
    scene = observed_scene(store)
    assert not scene["objects"]
    reasons = {r["reason"] for r in scene["unresolved"]}
    assert {"conflicted", "stale_observation_time", "unknown_frame"} <= reasons
    assert all(r["evidence"]["uncertainty"] is None for r in scene["unresolved"])
    conflicted = next(r for r in scene["unresolved"] if r["reason"] == "conflicted")
    assert {v["value"]["detections"][0]["pose"]["x"] for v in conflicted["conflicting_values"]} == {12, 42}
    assert "hidden-truth-id" not in json.dumps(scene)


def test_historical_coordinates_are_drawable_only_in_their_original_frame(services):
    store = services.store
    # Deliberately invented coordinates, unrelated to the private v1 packet.
    for prop, value in [("seeded_xy", [17, 29]), ("QR_1A_1B_1C_1D", [[-7, 23], [-7, 5], [19, 5], [19, 23]])]:
        record_evidence(store, LabEvidence(subject="historical:v1:fixture:"+prop, property=prop,
            bench=store.bench, value=value, units="mm", frame="v1:arm_left",
            source=Source(kind="historical_constant", reference="private historical fixture")))
    scene = observed_scene(store)
    assert not scene["objects"]
    frame, = scene["historical_frames"]
    assert frame["id"] == "v1:arm_left" and frame["count"] == 5
    assert any(p["x"] == 17 and p["y"] == 29 for p in frame["objects"].values())
    assert all(p["frame"] == "v1:arm_left" and p["evidence"]["calibration_id"] is None
               and p["evidence"]["freshness"]["status"] == "unknown" for p in frame["objects"].values())


def test_only_an_evidenced_current_transform_can_map_a_registered_camera_frame(services):
    store = services.store
    record_evidence(store, cv(store, frame="camera:local"))
    before = store.last_event_id()
    record_evidence(store, LabEvidence(subject="calibration:camera", property="frame_transform", bench=store.bench,
        value={"from_frame": "camera:local", "to_frame": "world", "translation_mm": [10, 20, 0], "rotation_deg": 0},
        units="mm,deg", frame="camera:local", observed_at=store.clock.now(), timebase="store_clock",
        calibration_id="fixture-calibration", calibration_version="1",
        source=Source(kind="synthetic", reference="calibration fixture")))
    assert not observed_scene(store, through_event_id=before)["objects"]
    obj, = observed_scene(store)["objects"].values()
    assert obj["frame"] == "world" and (obj["x"], obj["y"]) == (22, 45)
    assert obj["evidence"]["frame"] == "camera:local" and obj["evidence"]["transform"]["calibration_id"] == "fixture-calibration"
    assert obj["evidence"]["transform"]["evidence"]["source"]["reference"] == "calibration fixture"
    assert obj["evidence"]["transform"]["uncertainty_propagation"] == "not evaluated"


@pytest.mark.parametrize("source_frame", [["camera:local"], {}, None, "other_camera"])
def test_malformed_or_mismatched_transform_stays_unresolved(services, source_frame):
    store = services.store
    record_evidence(store, cv(store, frame="camera:local"))
    record_evidence(store, LabEvidence(subject="calibration:malformed", property="frame_transform", bench=store.bench,
        value={"from_frame": source_frame, "to_frame": "world", "translation_mm": [0, 0, 0], "rotation_deg": 0},
        units="mm,deg", frame="camera:local", observed_at=store.clock.now(), timebase="store_clock",
        calibration_id="synthetic-malformed", calibration_version="1",
        source=Source(kind="synthetic", reference="malformed fixture")))
    scene = observed_scene(store)
    assert not scene["objects"]
    assert any(r["reason"] == "unknown_frame" for r in scene["unresolved"])


def test_original_microscope_linear_axis_is_not_relabelled_as_sample_z(services):
    store = services.store
    record_evidence(store, LabEvidence(subject="prior:invented-microscope", property="microscope_XYL_waypoint",
        bench=store.bench, value=[17, 29, 6], units="mm", frame="historical:microscope_XYL",
        source=Source(kind="historical_constant", reference="invented prior fixture")))
    scene = observed_scene(store)
    obj, = scene["historical_frames"][0]["objects"].values()
    assert obj["z"] is None and obj["linear_mm"] == 6 and obj["original_coordinates"] == [17, 29, 6]
    assert not scene["objects"]


def test_large_unresolved_values_are_honestly_bounded_after_reduction(services):
    store = services.store
    record_evidence(store, LabEvidence(subject="large:unlocated", property="note", bench=store.bench,
        value="unlocated " * 1000, units="categorical", frame="unknown",
        source=Source(kind="estimate", reference="synthetic large fixture")))
    row, = observed_scene(store)["unresolved"]
    assert row["value"]["display_omitted"] is True
    assert row["value"]["serialized_characters"] > 4096 and len(row["value"]["preview"]) == 512


def test_full_reduction_precedes_ui_bounds_and_discloses_omissions(services):
    store = services.store
    record_evidence(store, cv(store, subject="region:oldest"))
    for i in range(130):
        record_evidence(store, LabEvidence(subject=f"unlocated:{i}", property="note", bench=store.bench,
            value="unlocated", units="categorical", frame="unknown", source=Source(kind="estimate", reference="fixture")))
    scene = observed_scene(store, unresolved_limit=3)
    assert len(scene["objects"]) == 1  # it was older than the newest 100 properties
    assert scene["counts"]["unresolved"] == 130 and scene["omissions"]["unresolved"] == 127
    assert scene["complete_reduction"] is True


def test_projection_is_pure_and_old_bench_snapshot_excludes_future_reports(services, tmp_path, monkeypatch):
    s = services
    record_evidence(s.store, cv(s.store))
    prefix = s.store.last_event_id()
    first = observed_scene(s.store)
    truth, rngs = copy.deepcopy(s.lab.world.evaluation_snapshot()), (s.lab.world.rng.getstate(), s.lab.world.observation_rng.getstate())
    with monkeypatch.context() as patch:
        patch.setattr(s.clock, "now", lambda: (_ for _ in ()).throw(AssertionError("projection read Clock")))
        assert observed_scene(s.store) == first
    assert s.lab.world.evaluation_snapshot() == truth
    assert (s.lab.world.rng.getstate(), s.lab.world.observation_rng.getstate()) == rngs
    timeline = tmp_path/"timeline.jsonl"
    recorder = Recorder(s.store, lambda: bench_state(s), timeline)
    recorder.tick(force_bench=True)
    review_material(s.store, s.lab.bench, "carrier_gr_01", expected_material="Gr", by="Ada", status="confirmed")
    recorder.tick(force_bench=True)
    frames = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines() if '"bench"' in line]
    old, new = [f["bench"]["twin"]["observed_scene"] for f in frames]
    assert old["through_event_id"] == prefix and not old["subjects"]
    assert new["through_event_id"] > prefix and new["subjects"]
    assert observed_scene(s.store, through_event_id=prefix) == first
    assert not observed_scene(s.store, through_event_id=0)["objects"]


def test_existing_inspect_bench_is_an_actual_observed_scene_producer(services):
    job = services.jobs.submit("inspect_bench", {"regions": ["left_storage"]})
    assert services.jobs.wait(job, 30).status == "succeeded"
    scene = bench_state(services)["twin"]["observed_scene"]
    assert scene["objects"] and scene["source"] == "stored_lab_evidence"
    assert all(o["identity_status"] == "unidentified" for o in scene["objects"].values())


def test_saved_clock_basis_separates_lab_simulation_and_parent_utc(tmp_path, monkeypatch):
    path = tmp_path / "mixed-clocks.db"
    lab = RunStore(path, clock=SimClock(start=100))
    parent_clock = RealClock()
    monkeypatch.setattr(parent_clock, "now", lambda: 1000)
    parent = RunStore(path, clock=parent_clock)
    try:
        prefix = record_evidence(lab, cv(lab))
        first = observed_scene(lab)
        parent.emit("agent.request_usage", {"fixture": True})
        obj, = observed_scene(parent)["objects"].values()
        assert obj["evidence"]["freshness"] == next(iter(first["objects"].values()))["evidence"]["freshness"]
        assert obj["evidence"]["freshness"]["age_s"] == 0
        lab.clock.advance(301)
        lab.emit("lab.frame", {"fixture": True})
        stale = observed_scene(parent)
        assert not stale["objects"]
        assert stale["unresolved"][0]["evidence"]["freshness"]["age_s"] == 301
        assert observed_scene(parent, through_event_id=prefix) == first
    finally:
        parent.close()
        lab.close()


def test_independent_simulation_epochs_with_equal_timestamps_are_not_comparable(tmp_path):
    path = tmp_path / "simulation-epochs.db"
    first = RunStore(path, clock=SimClock(start=100))
    second = RunStore(path, clock=SimClock(start=100))
    try:
        prefix = record_evidence(first, cv(first, subject="old-epoch"))
        original = observed_scene(first)
        second.emit("lab.frame", {"fixture": True})
        scene = observed_scene(second)
        assert not scene["objects"]
        assert scene["unresolved"][0]["evidence"]["freshness"]["status"] == "unknown"
        assert scene["unresolved"][0]["evidence"]["freshness"]["age_s"] is None
        record_evidence(second, cv(second, subject="new-epoch"))
        obj, = observed_scene(second)["objects"].values()
        assert obj["evidence"]["freshness"]["status"] == "recent"
        assert observed_scene(second, through_event_id=prefix) == original
        assert not observed_scene(second, through_event_id=0)["objects"]
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize("timebase", ["store_clock", "utc"])
def test_legacy_untagged_evidence_remains_unknown_without_rewriting_events(tmp_path, timebase):
    path = tmp_path / "legacy.db"
    store = RunStore(path, clock=SimClock(start=100))
    event_id = record_evidence(store, cv(store).model_copy(update={"timebase": timebase}))
    # Match an actual historical schema: neither metadata table nor backfill.
    store._x("DROP TABLE event_clock")
    original = [tuple(r) for r in store._x("SELECT * FROM event")]
    store.close()
    reopened = RunStore(path, clock=SimClock(start=100))
    try:
        assert [tuple(r) for r in reopened._x("SELECT * FROM event")] == original
        reopened.emit("lab.frame", {"fixture": True})
        assert [tuple(r) for r in reopened._x("SELECT * FROM event WHERE id=?", (event_id,))] == original
        scene = observed_scene(reopened)
        assert not scene["objects"]
        assert scene["unresolved"][0]["evidence"]["freshness"]["status"] == "unknown"
    finally:
        reopened.close()


def test_custom_clock_requires_explicit_comparability_and_never_clamps_negative_age():
    class CustomClock:
        def now(self):
            return 100

        def sleep(self, seconds):
            pass

    store = RunStore(clock=CustomClock())
    try:
        record_evidence(store, cv(store), supersede_previous=True)
        assert not observed_scene(store)["objects"]
        store.clock.event_clock_basis = "fixture:continuous"
        record_evidence(store, cv(store), supersede_previous=True)
        assert observed_scene(store)["objects"]
        record_evidence(store, cv(store, age=-1), supersede_previous=True)
        scene = observed_scene(store)
        assert not scene["objects"]
        assert scene["unresolved"][0]["evidence"]["freshness"]["age_s"] == -1
        assert scene["unresolved"][0]["evidence"]["freshness"]["status"] == "unknown"
    finally:
        store.close()


def test_utc_observation_is_comparable_across_real_clock_instances(tmp_path, monkeypatch):
    first_clock, second_clock = RealClock(), RealClock()
    monkeypatch.setattr(first_clock, "now", lambda: 100)
    monkeypatch.setattr(second_clock, "now", lambda: 401)
    path = tmp_path / "utc.db"
    first, second = RunStore(path, clock=first_clock), RunStore(path, clock=second_clock)
    try:
        record_evidence(first, cv(first).model_copy(update={"timebase": "utc"}))
        second.emit("agent.request_usage", {"fixture": True})
        scene = observed_scene(second)
        assert not scene["objects"]
        assert scene["unresolved"][0]["evidence"]["freshness"]["age_s"] == 301
        assert scene["unresolved"][0]["evidence"]["freshness"]["clock_basis"] == "utc"
    finally:
        second.close()
        first.close()


def test_event_clock_metadata_rolls_back_atomically():
    store = RunStore(clock=SimClock())
    try:
        store._x("CREATE TRIGGER refuse_clock BEFORE INSERT ON event_clock "
                 "BEGIN SELECT RAISE(ABORT, 'fixture refusal'); END")
        with pytest.raises(sqlite3.IntegrityError, match="fixture refusal"):
            store.emit("fixture")
        assert store.last_event_id() == 0
        assert not store._x("SELECT * FROM event_clock").fetchall()
    finally:
        store.close()
