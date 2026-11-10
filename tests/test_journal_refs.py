"""Laboratory evidence names lab events by the laboratory's ids (brain mirrors them)."""

from __future__ import annotations

import json

from qumus2.perception.rgb_material import algorithm_sha256
from qumus2.records.recognition import known_algorithm_sha256s
from qumus2.runtime.store import RunStore


def test_unmirrored_store_uses_its_own_ids():
    store = RunStore(":memory:")
    eid = store.emit("microscope.rgb_recognition", {"flake_id": "f1"}, run_id="run_a")
    assert not store.journal_mirrored() and store.journal_ref_sql() == "id"
    row = store.journal_event(eid)
    assert row["id"] == eid and json.loads(row["payload_json"]) == {"flake_id": "f1"}
    assert store.journal_event("7") is None


def test_mirrored_store_resolves_lab_ids_and_drops_mirror_keys():
    store = RunStore(":memory:")
    store.set_meta("mirror_records_lab_id", "lab1")
    store.emit("agent.message", {"text": "brain's own event"})
    store.emit("agent.message", {"text": "another"})
    brain_id = store.emit("microscope.rgb_recognition", {"flake_id": "f1", "lab_event_id": 1, "lab_ts": 5.0},
                          run_id="run_a")
    store.emit("job.status", {"status": "succeeded", "lab_event_id": 2}, run_id="run_a")
    row = store.journal_event(1)
    assert brain_id == 3 and row["id"] == 1 and row["kind"] == "microscope.rgb_recognition"
    assert json.loads(row["payload_json"]) == {"flake_id": "f1"}
    assert store.journal_event(3) is None  # a brain id is not a lab reference
    done = store._x(f"SELECT MIN({store.journal_ref_sql()}) AS id FROM event WHERE run_id='run_a' "
                    "AND kind='job.status'").fetchone()["id"]
    assert done == 2


def test_lab_copy_of_the_recognition_files_is_accepted():
    known = known_algorithm_sha256s()
    assert algorithm_sha256() in known and len(known) == 2
