"""Primary evidence helper mocks; never run the primary physical case."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from tests.test_direct_contact_contract import public_source as observed_public_source
from tests.test_direct_contact_contract import resolved
from tests.test_direct_gr_stacking import (
    _assert_direct_gr_detector_paths,
    _save_pretransfer_snapshot,
)

public_source = observed_public_source


@pytest.mark.parametrize("missing", [None, "pickup", "release", "all", "ids"])
def test_primary_detector_guard_requires_both_nonempty_native20_gr_paths(missing):
    calls = [
        {
            "method": "find_flakes",
            "objective": 20,
            "material": "Gr",
            "phase": phase,
            "remaining_ids": missing == "ids",
        }
        for phase in ("v1_pick_up_gr", "v1_drop_down")
    ]
    if missing == "pickup":
        calls = calls[1:]
    elif missing == "release":
        calls = calls[:1]
    elif missing == "all":
        calls = []
    if missing is None:
        _assert_direct_gr_detector_paths(calls, "gr")
    else:
        with pytest.raises(AssertionError):
            _assert_direct_gr_detector_paths(calls, "gr")


def frozen_choice(s):
    c, _, _ = resolved(s)
    return {
        "direct_contact_contract": c.model_dump(mode="json"),
        "source_manifest": {
            "status": "qualified_frozen_before_transfer",
            "selected": "gr",
            "jobs": [{"id": r["id"]} for r in s.store.list_runs()],
        },
    }


def test_pretransfer_snapshot_uses_existing_backup_and_persistence_once(public_source, tmp_path):
    s = public_source
    frozen = frozen_choice(s)
    before = (s.store.last_event_id(), s.clock.now())
    receipt = _save_pretransfer_snapshot(s, tmp_path, frozen)
    assert receipt["consistent"] and receipt["before"] == receipt["after"]
    assert before == (s.store.last_event_id(), s.clock.now())
    for key in ("database", "state"):
        item = receipt[key]
        p = Path(item["path"])
        assert hashlib.sha256(p.read_bytes()).hexdigest() == item["sha256"]
        assert p.stat().st_mode & 0o222 == 0
    database = Path(receipt["database"]["path"])
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as copy:
        assert copy.execute("SELECT location FROM sample WHERE id='gr'").fetchone()[0] == "chip_001"
    with pytest.raises(AssertionError, match="cannot be overwritten"):
        _save_pretransfer_snapshot(s, tmp_path, frozen)


def test_pretransfer_snapshot_refuses_unsettled_jobs(public_source, tmp_path):
    s = public_source
    frozen = frozen_choice(s)
    s.store.create_run("search_chip", {})  # Journal-only queued mock, no worker.
    with pytest.raises(AssertionError):
        _save_pretransfer_snapshot(s, tmp_path, frozen)
    assert not list(tmp_path.glob("pre-transfer.*"))


def test_pretransfer_snapshot_detects_event_race_without_authorizing_transfer(
    public_source, tmp_path, monkeypatch
):
    from tests import test_direct_gr_stacking as primary

    s = public_source
    frozen = frozen_choice(s)
    real_save = primary.save_lab_state

    def event_race(lab, clock, path):
        real_save(lab, clock, path)
        s.store.emit("fixture.concurrent_update", {"private_persistence_test": True})

    monkeypatch.setattr(primary, "save_lab_state", event_race)
    with pytest.raises(AssertionError, match="changed clock/event/job state"):
        _save_pretransfer_snapshot(s, tmp_path, frozen)
    assert not (tmp_path / "pre-transfer-frozen.json").exists()
