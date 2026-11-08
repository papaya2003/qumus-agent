"""Complete producer-shaped MOCK journals; no World, lab, jobs or RGB rendering.

These validate record consistency, not sensing/transfer. Original RGB classifier
controls live separately in test_release_support.py. Display locators deliberately
lack hashes and must never become analysis or remove their public encoder.
"""

import hashlib
from copy import deepcopy

import pytest

from qumus2.core.optical_profiles import RELEASE_SUPPORT_ASSUMPTIONS
from qumus2.flows.library import stacking as m
from qumus2.perception.contact_coverage import _release_opaque_masks, release_load_transport
from qumus2.perception.tracking import PixelCalibration
from qumus2.records.release_support import _encoder_projection, release_journal_certificate
from qumus2.testbench.demo_scoring import _recorded_observed_release_support
from tests.test_release_admission_hooks import declared_direct_contract

AXIS = {
    "source": "synthetic",
    "simulation_only": True,
    "scale_mm_per_focus_mm": 1.0,
    "apex_lateral_drift_model": "componentwise-clipped-synthetic-stamp-v1",
    "apex_lateral_drift_speed_norm_bound_mm_s": 2.23606797749979e-05,
    "apex_approach_drift_norm_bound_mm_per_mm": 0.0036055512754639895,
    "apex_drift_clip_per_axis_mm": 0.15,
    "contact_slip_rate_bound_deg_s": 0.002,
    "dome_curvature_radius_mm": 1.5,
    "source_refraction_projection_norm_bound_mm_per_mm": 0.005,
    "lateral_focus_slope_norm_bound_mm_per_mm": 0.01414213562373095,
}


def journal_fixture():
    events = []
    time = 10.0
    run_id = "r"
    name = "v1_drop_down"
    stage = {"sample_pose": {"x": 0.0, "y": 0.0, "z": 0.0, "rotation": 0.0}}
    pose = {"x": 40.0, "y": 90.0, "z": 6.5}
    contract = declared_direct_contract().model_dump(mode="json")
    params = {
        "release_policy": m.OBSERVED_RELEASE_POLICY_ID,
        "stamp_id": "stamp",
        "stamp_film": "PC",
        "target_chip_id": "target",
        "flake_ids": ["a"],
        "flake_chips": {"a": "ca"},
        "materials": {"a": "Gr"},
        "procedure_contract": m.PROCEDURE_ID,
        "transfer_profile": m.TRANSFER_PROFILE_ID,
        "direct_contact_contract": contract,
        "contact_interior_calibration": {},
    }

    def emit(kind, payload, *, ts=None, owner=run_id, hash_payload=True):
        row = {
            "id": len(events) + 1,
            "kind": kind,
            "payload": deepcopy(payload),
            "ts": time if ts is None else ts,
            "run_id": owner,
        }
        events.append(row)
        return (
            {"event_id": row["id"], "event_sha256": m._digest(payload)}
            if hash_payload
            else {"event_id": row["id"]}
        )

    def command(device, operation, **args):
        return emit("device.command", {"device": device, "command": operation, "params": args})

    def encoder(device="microscope", operation="capture", *, at_pose=None, ts=None):
        value = deepcopy(pose if at_pose is None else at_pose)
        payload = {
            "device": device,
            "command": operation,
            "geometry": {
                "source": "synthetic",
                "mechanics_version": 2,
                "microscope": {
                    "source": "synthetic",
                    "stamp_axes_frame": "transfer_arm",
                    "stamp_axes_xyz_mm": value,
                    "optical_axis_world_mm": [675.0, 420.0],
                },
            },
            "microscope_preview": {
                "purpose": "motion_preview",
                "verification": False,
                "source": "synthetic_scene",
                "image": "unhashed-display.png",
            },
        }
        if device == "transfer_arm":
            payload["manipulator"] = {k: round(v, 3) for k, v in value.items()}
        emit("lab.frame", payload, ts=ts, hash_payload=False)
        row = events[-1]
        return {
            "event_id": row["id"],
            "profile": "synthetic-contact-encoder-camera-v1",
            "projection_sha256": m._digest(_encoder_projection(row)),
        }

    def capture(objective, focus, plane):
        nonlocal time
        command("microscope", "capture")
        er = encoder()
        size = [2.6, 1.72] if objective == 5 else [0.65, 0.43]
        calibration = {
            "source": "synthetic",
            "image_size_px": [480, 320],
            "mm_per_pixel": [size[0] / 480, size[1] / 320],
            "release_support_profile": deepcopy(RELEASE_SUPPORT_ASSUMPTIONS),
            "observation_condition": {"observation_noise_scale": 1.0},
        }
        meta = {
            "objective": objective,
            "focus_z": focus,
            "focus_plane": plane,
            "calibration": calibration,
            "stage_kinematics": deepcopy(stage),
            "fov_mm": size,
        }
        path = f"mock-original-{len(events)}.png"
        sha = hashlib.sha256(path.encode()).hexdigest()
        record = {"image": path, "sha256": sha, "captured_at": time, "meta": meta}
        preview = {
            "phase": name,
            "purpose": "analysis",
            "immutable": True,
            "verification": False,
            "source": "synthetic",
            "image": path,
            "image_sha256": sha,
            "timestamp": time,
            **deepcopy(meta),
        }
        receipt = emit(
            "lab.frame",
            {"device": "microscope", "command": "capture", "microscope_preview": preview},
        )
        time += 0.1
        return (record, {"capture": receipt, "encoder": er})

    emit("job.submitted", {"flow": "stack_device", "params": params})
    start = emit("step.started", {"seq": 0, "name": name})
    reference, rr = capture(5, 0.8, "chip")
    command("microscope", "set_objective", magnification=20)
    time += 0.01
    command("microscope", "set_focus", z=0.3)
    time += 0.01
    capture(20, 0.3, "stamp")
    focus = emit(
        "microscope.focus",
        {
            "objective": 20,
            "plane": "stamp",
            "focus_plane": "stamp",
            "focus_axis_calibration": deepcopy(AXIS),
            "final_focus_readback_z_mm": 0.3,
            "holder_pose": deepcopy(pose),
            "stage_kinematics": deepcopy(stage),
        },
    )
    native, nr = capture(20, 0.3, "stamp")
    witness = {"load": {"release_start": start}}
    phase = {
        "name": name,
        "release_policy": m.OBSERVED_RELEASE_POLICY_ID,
        "started_at": 10.0,
        "reference": reference,
        "reference_event": rr["capture"],
        "release_reference_receipts": rr,
        "release_reference": {"witness": witness},
        "process_reference": {"minimum_z_mm": 6.4},
    }
    footprint = {
        "policy": m.OBSERVED_RELEASE_POLICY_ID,
        "profile": deepcopy(RELEASE_SUPPORT_ASSUMPTIONS),
        "footprint_captures": 1,
        "stamp_id": "stamp",
        "load_ids": ["a"],
        "frame": native,
        "receipts": nr,
        "focus_event": focus,
        "axis": deepcopy(AXIS),
        "noise_scale": 1.0,
        "loaded": [
            {
                "flake_id": "a",
                "material": "Gr",
                "outline_mm": contract["source_outline_mm"],
                "thickness_label": "manylayer",
            }
        ],
        "selection_sha256": m._digest({k: params.get(k) for k in m._WITNESS_SELECTION_FIELDS}),
        "witness_sha256": m._digest(witness),
        "design_contract_sha256": m._digest(contract),
        "classification_sha256": m._digest({}),
        "pose_window_admission_sha256": m._digest(None),
        "optical_commands": [
            {"event_id": e["id"], "event_sha256": m._digest(e["payload"])}
            for e in events
            if e["kind"] == "device.command"
            and e["payload"]["device"] == "microscope"
            and (rr["capture"]["event_id"] < e["id"])
        ],
    }
    phase["release_footprint"] = emit("stack.release_footprint", footprint)
    command("microscope", "set_focus", z=0.8)
    time += 0.01
    command("microscope", "set_objective", magnification=5)
    time += 0.01
    heater = command("stamp_heater", "set_target", celsius=155.5)
    time += 0.1
    capture(5, 0.8, "chip")
    phase["release_approach"] = emit(
        "stack.release_approach",
        {
            "phase": name,
            "footprint": phase["release_footprint"],
            "reference": phase["reference_event"],
            "entry_heat_commands": [heater],
            "at": time,
        },
    )
    command(
        "transfer_arm",
        "move_axis_to",
        axis="z",
        value=6.485,
        speed=0.1,
        from_pose=deepcopy(pose),
        duration_bound_s=0.15,
    )
    encoder("transfer_arm", "move_axis_to", at_pose={**pose, "z": 6.4925}, ts=time + 0.075)
    pose["z"] = 6.485
    encoder("transfer_arm", "move_axis_to", ts=time + 0.15)
    time += 0.15
    current, cr = capture(5, 0.8, "chip")
    values = {
        "events": events,
        "phase": phase,
        "params": params,
        "current": current,
        "current_receipts": cr,
        "run_id": run_id,
    }
    return values


def certify(values):
    return release_journal_certificate(**values, digest=m._digest)


def support_record():
    values = journal_fixture()
    journal = certify(values)
    events, phase = (values["events"], values["phase"])
    footprint = next(e["payload"] for e in events if e["kind"] == "stack.release_footprint")
    transports = []
    for key in ("footprint_to_reference", "footprint_to_current"):
        prefix = journal[key]
        transports.append(
            release_load_transport(
                axis=footprint["axis"],
                elapsed_s=prefix["elapsed_s"],
                z_variation_mm=prefix["z_total_variation_mm"],
                noise_scale=1.0,
                footprint_size_mm=[0.65, 0.43],
                camera_size_mm=[2.6, 1.72],
            )
        )
    masks = _release_opaque_masks(
        footprint["loaded"],
        (480, 320),
        PixelCalibration(2.6 / 480, -1.72 / 320),
        transports[0]["load_envelope_mm"],
        transports[1]["load_envelope_mm"],
    )
    result = {
        "conditional_modeled_support": True,
        "optical_whole_interior_observed": False,
        "integrity": "unknown",
        "recognition_tail_budget_centroid_only": 0.001,
        **{
            key: hashlib.sha256(mask.tobytes()).hexdigest()
            for key, mask in zip(
                (
                    "reference_opaque_mask_sha256",
                    "current_opaque_mask_sha256",
                    "opaque_unknown_mask_sha256",
                ),
                masks,
                strict=True,
            )
        },
    }
    proof = {
        "footprint": phase["release_footprint"],
        "reference": phase["reference"],
        "current": values["current"],
        "current_receipts": values["current_receipts"],
        "journal": journal,
        "transports": transports,
        "result": result,
        "actual_command_z_mm": 6.485,
    }
    row = {
        "id": len(events) + 1,
        "kind": "stack.release_support",
        "run_id": "r",
        "ts": values["current"]["captured_at"],
        "payload": proof,
    }
    events.append(row)
    phase["accepted_release_support"] = {"event_id": row["id"], "event_sha256": m._digest(proof)}
    return values


def score(values):
    return _recorded_observed_release_support(
        values["events"],
        run_id="r",
        phase=values["phase"],
        params=values["params"],
        digest=m._digest,
    )


def test_complete_mixed_preview_journal_derives_actual_time_and_all_encoder_variation():
    values = journal_fixture()
    proof = certify(values)
    assert proof["footprint_to_current"]["elapsed_s"] > 0
    assert proof["footprint_to_current"]["z_total_variation_mm"] == pytest.approx(0.015)
    assert len(proof["encoders"]) == 2
    actual = proof["encoders"][0]
    raw = next(row for row in values["events"] if row["id"] == actual["event_id"])
    assert actual["pose"]["z"] == 6.4925
    assert raw["payload"]["manipulator"]["z"] != actual["pose"]["z"]
    with pytest.raises(ValueError, match="matching content SHA"):
        m._digest(raw["payload"])  # Display attachments stay unhashable as analysis.
    assert proof["event_count"] == proof["last_event_id"] - proof["first_event_id"] + 1


@pytest.mark.parametrize(
    "fault",
    [
        "display_only",
        "duplicate_analysis",
        "wrong_phase",
        "bad_sha",
        "deleted_command",
        "deleted_encoder",
        "foreign_command",
        "xy",
        "extra_focus",
        "phase_boundary",
        "bad_clock",
        "raw_missing",
        "rounded_only",
        "missing_both_encoder_fields",
    ],
)
def test_complete_journal_refuses_missing_analysis_or_omitted_actual_work(fault):
    values = journal_fixture()
    rows = values["events"]
    current = values["current_receipts"]["capture"]["event_id"]
    capture_row = next(e for e in rows if e["id"] == current)
    move = next(
        e
        for e in rows
        if e["kind"] == "device.command" and e["payload"]["device"] == "transfer_arm"
    )
    encoders = [
        e for e in rows if e["kind"] == "lab.frame" and e["payload"].get("device") == "transfer_arm"
    ]
    if fault == "display_only":
        capture_row["payload"]["microscope_preview"] = {
            "purpose": "motion_preview",
            "verification": False,
        }
    elif fault == "duplicate_analysis":
        target = next(
            e for e in rows if e["id"] == values["current_receipts"]["encoder"]["event_id"]
        )
        target["payload"]["microscope_preview"] = deepcopy(
            capture_row["payload"]["microscope_preview"]
        )
    elif fault == "wrong_phase":
        capture_row["payload"]["microscope_preview"]["phase"] = "other"
    elif fault == "bad_sha":
        capture_row["payload"]["microscope_preview"]["image_sha256"] = "0" * 64
    elif fault == "deleted_command":
        rows.remove(move)
    elif fault == "deleted_encoder":
        rows.remove(encoders[0])
    elif fault == "foreign_command":
        row = next(
            e
            for e in rows
            if e["kind"] == "lab.frame"
            and e["id"] < values["phase"]["release_approach"]["event_id"]
        )
        row.update(
            kind="device.command",
            run_id="other",
            payload={
                "device": "stamp_heater",
                "command": "set_target",
                "params": {"celsius": 155.5},
            },
        )
    elif fault == "xy":
        move["payload"]["params"]["axis"] = "x"
    elif fault == "extra_focus":
        move["payload"] = {"device": "microscope", "command": "set_focus", "params": {"z": 0.9}}
    elif fault == "phase_boundary":
        encoders[0].update(kind="step.started", payload={"name": "other"})
    elif fault == "raw_missing":
        del encoders[0]["payload"]["geometry"]["microscope"]["stamp_axes_xyz_mm"]
    elif fault == "rounded_only":
        for row in encoders:
            del row["payload"]["geometry"]
    elif fault == "missing_both_encoder_fields":
        del encoders[0]["payload"]["geometry"], encoders[0]["payload"]["manipulator"]
    else:
        move["ts"] = 9.0
    if fault in ("display_only", "wrong_phase", "bad_sha"):
        values["current_receipts"]["capture"]["event_sha256"] = m._digest(capture_row["payload"])
    with pytest.raises(ValueError):
        certify(values)


def test_record_scorer_uses_same_complete_journal_and_not_claimed_scalars():
    values = support_record()
    assert score(values)
    proof = values["events"][-1]["payload"]
    proof["journal"]["footprint_to_current"]["z_total_variation_mm"] = 0.0
    proof["transports"][1] = deepcopy(proof["transports"][0])
    values["phase"]["accepted_release_support"]["event_sha256"] = m._digest(proof)
    assert score(values) is False
    assert certify({k: v for k, v in values.items() if k != "unused"})["footprint_to_current"][
        "z_total_variation_mm"
    ] == pytest.approx(0.015)


def test_actual_af_publication_tags_original_analysis_and_charges_once(tmp_path, monkeypatch):
    """Exercise the real publisher, not a World/AF/footprint acceptance path."""
    from types import SimpleNamespace

    from PIL import Image

    from qumus2.devices.base import Frame
    from qumus2.runtime.store import RunStore

    path = tmp_path / "original-analysis.png"
    Image.new("RGB", (480, 320), (122, 96, 168)).save(path)
    frame = Frame(
        "microscope",
        10.0,
        path=str(path),
        meta={
            "objective": 20,
            "focus_z": 0.3,
            "focus_plane": "stamp",
            "fov_mm": [0.65, 0.43],
            "calibration": {"source": "synthetic"},
            "stage_kinematics": {},
        },
    )
    store = RunStore(":memory:", bench="sim")
    phase = {"name": "v1_drop_down", "release_budget": {"footprint_pending": 1}}
    charged = []
    monkeypatch.setattr(
        m, "_release_exposure", lambda ctx, actual_phase, actual_frame: charged.append(actual_frame)
    )
    try:
        m._release_publish_capture(SimpleNamespace(store=store, run_id=None), phase, frame)
        row = store.events_after(0, limit=1)[0]
        preview = row.payload["microscope_preview"]
        assert preview["purpose"] == "analysis" and preview["immutable"] is True
        assert preview["verification"] is False and preview["phase"] == phase["name"]
        assert preview["image_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert preview["fov_mm"] == [0.65, 0.43]
        assert phase["release_budget"]["footprint_pending"] == 0 and charged == [frame]
    finally:
        store.close()


def test_release_journal_rows_keep_the_same_clock_rows_published_before_the_release_step():
    from qumus2.runtime.events import Event

    times = {1: 9.0, 2: 10.0, 3: 10.0, 4: 10.0, 5: 10.0, 6: 11.0, 7: 12.0}

    class Store:
        def events_after(self, after, limit=500):
            return [Event(id=i, ts=times[i], kind="lab.frame", run_id="r", agent="a", payload={})
                    for i in sorted(times) if i > after][:limit]

    phase = {"release_reference": {"witness": {"load": {"release_start": {"event_id": 4}}}}}
    # Event 4 starts the release Step; 2 and 3 share its Clock instant (the
    # previous Step's final encoder readback), 1 does not.
    assert [row["id"] for row in m._release_journal_rows(Store(), phase, 6)] == [2, 3, 4, 5, 6]
    times.update({2: 9.5, 3: 9.5})
    assert [row["id"] for row in m._release_journal_rows(Store(), phase, 6)] == [4, 5, 6]
