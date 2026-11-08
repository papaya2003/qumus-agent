"""Primary ideal PC case: fresh sole Gr directly on existing named contacts.

This is a long physical simulation, separately admitted from the small mocks.
The generic hBN/Gr/hBN regression remains in test_stacking.py.
Its return/heater-target checks do not establish fresh temperatures below 40 C
or fans off; ordinary task/recording cold completion remains a separate gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from qumus2.app import build_services
from qumus2.config import ideal_stacking_profile
from qumus2.core.polygons import transform
from qumus2.records.geometry import (
    actual_target_contact_snapshot,
    direct_contact_intent,
    direct_contact_result,
)
from qumus2.testbench.persist import save_lab_state
from qumus2.ui.recording import source_fingerprint
from tests.direct_gr_source import SOURCE_POLICY, build_direct_gr_source
from tests.test_stacking import (
    _assert_terminal_stack_snapshot,
    _close_settled_stack_store,
    _finish_stacking_diagnostics,
    _ideal_without_optional_detector_ids,
    _unsettled_stack_jobs,
    events,
    run,
)


def _assert_direct_gr_detector_paths(calls, flake_id):
    native = [
        row
        for row in calls
        if row["method"] == "find_flakes" and row["objective"] == 20 and row["material"] == "Gr"
    ]
    assert f"v1_pick_up_{flake_id}" in {row["phase"] for row in native}, (
        "Gr pickup detector endpoint not reached"
    )
    assert "v1_drop_down" in {row["phase"] for row in native}, (
        "Gr release detector endpoint not reached"
    )
    assert not any(row["remaining_ids"] for row in calls)


def _save_pretransfer_snapshot(s, folder, frozen):
    """One immutable persistence pair after public choice; never read into control."""
    assert frozen["source_manifest"]["status"] == "qualified_frozen_before_transfer"
    assert (
        frozen["source_manifest"]["selected"]
        == frozen["direct_contact_contract"]["source_flake_id"]
    )
    database, state = folder / "pre-transfer.db", folder / "pre-transfer.pkl"
    assert not database.exists() and not state.exists(), (
        "pre-transfer snapshot cannot be overwritten"
    )

    def binding():
        runs = [{"id": r["id"], "status": r["status"]} for r in s.store.list_runs()]
        assert all(r["status"] == "succeeded" for r in runs)
        assert not _unsettled_stack_jobs(s), "pre-transfer persistence requires all workers settled"
        assert {r["id"] for r in runs} == {j["id"] for j in frozen["source_manifest"]["jobs"]}
        assert not s.store.locks() and not s.store.reservations()
        return {"event_id": s.store.last_event_id(), "clock": s.clock.now(), "jobs": runs}

    with s.store._lock, s.lab.world.lock:
        before = binding()
        with sqlite3.connect(database) as copy:
            s.store._conn.backup(copy)
        save_lab_state(s.lab, s.clock, state)
        after = binding()
        assert before == after, "pre-transfer persistence changed clock/event/job state"
    database.chmod(0o444)
    state.chmod(0o444)
    return {
        "database": {
            "path": str(database),
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        },
        "state": {"path": str(state), "sha256": hashlib.sha256(state.read_bytes()).hexdigest()},
        "before": before,
        "after": after,
        "consistent": True,
        "scope": "private persistence/evaluation only; never ranking, control, fallback or reseeding",
    }


def test_ideal_sole_gr_direct_existing_contacts(tmp_path, request, monkeypatch):
    assert not os.environ.get("QUMUS2_CONFIG_DIR")
    assert not os.environ.get("QUMUS2_MICROSCOPE_REPLAY_DIR")
    config = ideal_stacking_profile()
    config["seed"] = SOURCE_POLICY["seed"]
    s = build_services(tmp_path / "source.db", seed=8, testbench_config=config, use_openai=False)
    request.session._qumus_public_case = ("fresh-direct-gr-pc-primary", s)
    request.addfinalizer(lambda: _close_settled_stack_store(s))
    try:
        intent = direct_contact_intent(
            s.store,
            "chip_e01",
            "centre_pair",
            {
                "contact_overlap_um": 1,
                "channel_width_um": 2,
                "placement_margin_um": 0.5,
                "rationale": "prospective coordinator-adopted ideal synthetic criterion; not measured laboratory specification",
            },
        )
        c, manifest = build_direct_gr_source(s, intent, tmp_path)
        frozen = {
            "source_sha256": source_fingerprint(),
            "config": config,
            "source_manifest": manifest,
            "direct_contact_contract": c.model_dump(mode="json"),
            "completion_scope": "geometric/recorded interface and return/heater target off; fresh <40 C and fans off not established",
        }
        frozen["pre_transfer_snapshot"] = _save_pretransfer_snapshot(s, tmp_path, frozen)
        (tmp_path / "pre-transfer-frozen.json").write_text(json.dumps(frozen, indent=2) + "\n")
        calls = _ideal_without_optional_detector_ids(s, monkeypatch)
        params = {
            "flake_ids": [c.source_flake_id],
            "target_chip_id": "chip_e01",
            "stamp_id": "stamp_01",
            "direct_contact_material": "Gr",
            "target_contact_region_id": "centre_pair",
            "bridge_requirement": intent["bridge_requirement"],
            "direct_contact_intent": intent,
            "direct_contact_contract": c.model_dump(mode="json"),
        }
        status = run(s, "stack_device", params)
        assert status.status == "succeeded", {"error": status.error, "steps": status.steps}
        assert not events(s, "safety.violation")
        assert frozen["source_sha256"] == source_fingerprint()
        saved = s.store.get_run(status.run_id)["params"]
        assert saved["direct_contact_contract"] == c.model_dump(mode="json")
        devices = s.store.list_samples(kind="device")
        assert len(devices) == 1 and devices[0]["props"]["layers_top_first"] == [c.source_flake_id]
        receipt = devices[0]["props"]["direct_contact_result"]
        assert receipt["status"] == "pass" and receipt["binding"]["run_id"] == status.run_id
        assert len(receipt["binding"]["terminal_frame_sha256"]) == 3
        _assert_direct_gr_detector_paths(calls, c.source_flake_id)
        terminal = s.lab.world.evaluation_snapshot()  # Exit-only private oracle, after all control.
        physical_ids = {c.source_flake_id: s.store.get_sample(c.source_flake_id)["props"]["sim_id"]}
        bodies = _assert_terminal_stack_snapshot(
            terminal, "chip_e01", physical_ids, saved["stamp_id"]
        )
        body = bodies[c.source_flake_id]
        target = next(o for o in terminal["objects"] if o["id"] == "chip_e01")
        actual_geometry = actual_target_contact_snapshot(
            target["id"],
            target["size_mm"],
            target.get("electrode_geometry"),
            intent["target_contact"]["region"],
        )
        result = direct_contact_result(
            {
                "flake_id": c.source_flake_id,
                "material": body["material"],
                "outline_mm": transform(body["outline_mm"], xy=(body["x_mm"], body["y_mm"])),
                "outline_complete": True,
                "frame": "chip_center_axes",
                "units": "mm",
                "stamp_film": saved["stamp_film"],
                "source_geometry_sha256": c.source_geometry_sha256,
                "incoming_layers_top_first": [
                    {"flake_id": c.source_flake_id, "material": body["material"]}
                ],
                "observation_binding": {
                    "run_id": status.run_id,
                    "source": "private_terminal_persisted_snapshot",
                },
            },
            {
                "chip_id": target["id"],
                "geometry": actual_geometry,
                "receiving_layers_above_contacts": [],
                "before_observation_binding": receipt["binding"]["target_before_sha256"],
            },
            c,
        )
        (tmp_path / "private-terminal-contact.json").write_text(json.dumps(result, indent=2) + "\n")
        assert result["status"] == "pass", result
        assert not terminal["stamps"][saved["stamp_id"]]["stack"]
        assert s.lab.world.stamps[saved["stamp_id"]].film is False
        assert s.lab.world.stamp_snapshot()["held"] is None
        assert s.lab.devices.get("stamp_heater").get_target() is None
        assert s.lab.world.objects["chip_e01"].on is None
    finally:
        _finish_stacking_diagnostics(s, tmp_path, config)
