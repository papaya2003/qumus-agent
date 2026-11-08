"""End-to-end material/plant binding; requires the material-line module commit."""

from __future__ import annotations

import pytest
from conftest import quiet_bench
from test_reactor_devices import load_by_hand, start_growth
from test_reactor_workflow import CHIP, call, ready, run

pytest.importorskip("qumus2.testbench.material_view", reason="material branch must be integrated")

from qumus2.app import build_lab_side, build_services  # noqa: E402
from qumus2.core.clock import SimClock  # noqa: E402
from qumus2.core.errors import PreconditionFailed  # noqa: E402
from qumus2.core.types import Pose  # noqa: E402
from qumus2.runtime.store import RunStore  # noqa: E402
from qumus2.testbench.lab import build_sim_lab  # noqa: E402
from qumus2.testbench.persist import lab_state, load_lab_state, save_lab_state  # noqa: E402


def pair_services(image_dir=None):
    cfg = quiet_bench()
    cfg["placement"]["sigma_mm"] = 0.02
    cfg["reactor"] = {"enabled": True}
    return build_services(testbench_config=cfg, image_dir=image_dir, seed=1)


def test_default_reactor_is_a_prepared_two_flake_experiment():
    s = pair_services()
    setup = call(s, "orchestrator", "get_reactor_setup").data
    assert setup["experiment_kind"] == "two_flake_reaction"
    assert len(s.store.get_sample(CHIP)["props"]["reactor_pair"]["input_flake_ids"]) == 2
    with pytest.raises(ValueError, match="field-growth"):
        s.jobs.submit("reactor_growth", {"chip_id": CHIP})


def test_empty_pair_camera_does_not_fall_back_to_a_growth_image(tmp_path, monkeypatch):
    from qumus2.testbench import reactor_devices

    def forbid_legacy(*args):
        raise AssertionError("pair camera must not render a legacy growth field")

    monkeypatch.setattr(reactor_devices, "_render", forbid_legacy)
    s = pair_services(tmp_path)
    frame = s.gate.microscope("reactor_microscope").capture()
    assert frame.meta["reactor"]["experiment_kind"] == "two_flake_reaction"
    assert frame.meta["reactor"]["reason"] == "carrier_empty"
    assert frame.saved_path()


def test_load_react_accept_and_unload_one_product(tmp_path):
    s = pair_services(tmp_path)
    ready(s)
    assert call(s, "orchestrator", "set_reactor_pair_goal", {"chip_id": CHIP}).status == "ok"
    run(s, "load_reactor_chip", chip_id=CHIP)
    run(s, "observe_reactor", chip_id=CHIP)
    initial = s.store.get_sample(CHIP)["props"]["reactor_result"]["pair_observation"]
    assert initial["visible"] and initial["product"] is None
    rid = run(s, "reactor_reaction", chip_id=CHIP, duration_s=80)
    result = call(s, "reactor", "submit_reactor_pair_result", {"chip_id": CHIP, "reaction_job_id": rid})
    assert result.status == "ok", result.summary
    products = [x for x in s.store.list_samples(kind="flake", location=CHIP) if x["status"] == "reactor_product"]
    assert len(products) == 1
    assert 0 < products[0]["props"]["area_um2"] <= 288.001
    assert products[0]["props"]["material_state"]["geometry"]["frame"] == "chip_local_mm"
    assert products[0]["props"]["images"]["reactor"] == result.images[-1]
    assert products[0]["props"]["chip_id"] == CHIP
    with pytest.raises(ValueError, match="consumed"):
        s.jobs.submit("reactor_reaction", {"chip_id": CHIP})
    run(s, "unload_reactor_chip", chip_id=CHIP)
    assert s.store.get_sample(CHIP)["location"] == "reactor_chip_slot_1"


def test_captures_do_not_advance_and_xy_changes_view_not_assembly_geometry():
    s = pair_services()
    chip = load_by_hand(s)
    scope = s.lab.devices.get("reactor_microscope")
    s.gate.stage("reactor_insert").move_to(Pose(x=0, y=160))
    s.gate.microscope("reactor_microscope").set_focus(chip.focus_offset)
    first = scope.capture().meta["reactor"]
    for _ in range(20):
        assert scope.capture().meta["reactor"] == first
    original = first["material_view"]["inputs"]
    s.gate.stage("reactor_slot").move_to(Pose(x=0.01, y=0.015))
    moved = scope.capture().meta["reactor"]
    assert moved["view_center_mm"] == pytest.approx([-0.01, -0.015])
    assert [x["outline_mm"] for x in moved["material_view"]["inputs"]] == [x["outline_mm"] for x in original]
    assert moved["pair_observation"]["reaction_fraction"] == 0


def test_pair_state_roundtrip_and_out_of_field_never_verifies_product():
    from qumus2.testbench.material_reaction import LocalReaction

    s = pair_services()
    start_growth(s)
    s.clock.sleep(300)
    scope = s.lab.devices.get("reactor_microscope")
    observed = scope.capture().meta["reactor"]
    assert observed["pair_observation"]["product"] is not None
    saved = scope.plant.state["pair_models"][CHIP]
    assert LocalReaction.restore(saved).snapshot() == saved
    s.gate.stage("reactor_slot").move_to(Pose(x=1, y=0))
    out = scope.capture().meta["reactor"]["pair_observation"]
    assert not out["visible"] and out["product"] is None


def test_failed_model_cannot_become_accepted_by_a_successful_job(tmp_path):
    cfg = quiet_bench()
    cfg["placement"]["sigma_mm"] = 0.02
    cfg["reactor"] = {"enabled": True, "reaction": {"profile": {"failure_probability": 1}}}
    s = build_services(testbench_config=cfg, image_dir=tmp_path, seed=1)
    ready(s)
    call(s, "orchestrator", "set_reactor_pair_goal", {"chip_id": CHIP})
    run(s, "load_reactor_chip", chip_id=CHIP)
    rid = run(s, "reactor_reaction", chip_id=CHIP, duration_s=80)
    result = call(s, "reactor", "submit_reactor_pair_result", {"chip_id": CHIP, "reaction_job_id": rid})
    assert result.status == "needs_attention"
    assert result.data["observation"]["pair_observation"]["product"] is None
    assert result.data["observation"]["pair_observation"]["status"] == "failed"


def test_actual_lab_restart_binds_existing_ledger_and_retains_material_history(tmp_path):
    cfg = quiet_bench()
    cfg["reactor"] = {"enabled": True}
    db, state = tmp_path / "pair.db", tmp_path / "pair.pkl"
    original = build_services(db, testbench_config=cfg, seed=1)
    start_growth(original)
    original.clock.sleep(200.5)
    original.gate.microscope("reactor_microscope").capture()
    save_lab_state(original.lab, original.clock, state)
    samples = original.store.list_samples()
    events = original.store.events_after(0, limit=10000)
    restored = build_lab_side(db, testbench_config=cfg, seed=999, state_path=state)
    assert restored.lab.world.seed == 1
    assert restored.store.list_samples() == samples
    assert restored.store.events_after(0, limit=10000) == events
    original.clock.sleep(100.5)
    restored.clock.sleep(100.5)
    assert original.gate.microscope("reactor_microscope").capture().meta == \
        restored.gate.microscope("reactor_microscope").capture().meta
    backend = restored.lab.devices.get("reactor_microscope").plant.pair_backend
    other = restored.lab.world.objects["reactor_chip_02"]
    assert backend.model(other, restored.lab.world.reactor_state["pair_models"]).state.seed == 1


def test_restart_does_not_reseed_consumed_inputs_or_duplicate_product(tmp_path):
    cfg = quiet_bench()
    cfg["placement"]["sigma_mm"] = 0.02
    cfg["reactor"] = {"enabled": True}
    db, state = tmp_path / "accepted.db", tmp_path / "accepted.pkl"
    s = build_services(db, testbench_config=cfg, seed=1)
    ready(s)
    call(s, "orchestrator", "set_reactor_pair_goal", {"chip_id": CHIP})
    run(s, "load_reactor_chip", chip_id=CHIP)
    rid = run(s, "reactor_reaction", chip_id=CHIP, duration_s=80)
    assert call(s, "reactor", "submit_reactor_pair_result", {"chip_id": CHIP, "reaction_job_id": rid}).status == "ok"
    save_lab_state(s.lab, s.clock, state)
    before = s.store.list_samples()
    restored = build_lab_side(db, testbench_config=cfg, state_path=state)
    assert restored.store.list_samples() == before
    assert len([x for x in before if x["status"] == "reactor_product"]) == 1
    with pytest.raises(ValueError, match="consumed"):
        restored.jobs.submit("reactor_reaction", {"chip_id": CHIP})


def test_pair_survives_separate_lab_process_restart(tmp_path):
    cfg = quiet_bench()
    cfg["placement"]["sigma_mm"] = 0.02
    cfg["reactor"] = {"enabled": True}
    db = tmp_path / "remote-pair.db"
    s = build_services(db, testbench_config=cfg, seed=1, lab_process=True)
    try:
        ready(s)
        call(s, "orchestrator", "set_reactor_pair_goal", {"chip_id": CHIP})
        run(s, "load_reactor_chip", chip_id=CHIP)
        rid = run(s, "reactor_reaction", chip_id=CHIP, duration_s=80)
        accepted = call(s, "reactor", "submit_reactor_pair_result", {"chip_id": CHIP, "reaction_job_id": rid})
        assert accepted.status == "ok", accepted.summary
        product_id = accepted.data["product_id"]
    finally:
        s.supervisor.stop()
    restored = build_services(db, testbench_config=cfg, seed=999, lab_process=True)
    try:
        run(restored, "observe_reactor", chip_id=CHIP)
        obs = restored.store.get_sample(CHIP)["props"]["reactor_result"]["pair_observation"]
        assert obs["product"]["id"] == product_id
        assert len([x for x in restored.store.list_samples(kind="flake") if x["status"] == "reactor_product"]) == 1
        run(restored, "unload_reactor_chip", chip_id=CHIP)
        assert restored.store.get_sample(CHIP)["location"] == "reactor_chip_slot_1"
    finally:
        restored.supervisor.stop()


@pytest.mark.parametrize("source_enabled,target_enabled,changed_profile", [
    (False, True, False), (True, False, False), (True, True, True),
])
def test_incompatible_restore_preserves_checkpoint_ledger_and_new_lab(
        tmp_path, source_enabled, target_enabled, changed_profile):
    import pickle

    cfg = quiet_bench()
    cfg["reactor"] = {"enabled": source_enabled}
    db, state = tmp_path / "old.db", tmp_path / "old.pkl"
    old = build_lab_side(db, testbench_config=cfg, seed=1)
    old.gate.arm("arm_left").move_to(Pose(x=12, y=100, z=90))
    save_lab_state(old.lab, old.clock, state)
    saved, samples = state.read_bytes(), old.store.list_samples()
    events = old.store.events_after(0, limit=10000)
    desired = quiet_bench()
    desired["reactor"] = {"enabled": target_enabled}
    if changed_profile:
        desired["reactor"]["reaction"] = {"activation_temperature_c": 710}
    fresh_clock, fresh_store = SimClock(), RunStore()
    fresh = build_sim_lab(fresh_clock, store=fresh_store, config=desired, seed=4)
    before = pickle.dumps(lab_state(fresh, fresh_clock))
    # Enabling/disabling the module now changes the earlier mechanics identity;
    # changing only reaction configuration still reaches the reactor guard.
    expected_refusal = ("explicitly select a fresh simulation workspace"
                        if source_enabled != target_enabled else "fresh explicitly named")
    with pytest.raises(PreconditionFailed, match=expected_refusal):
        load_lab_state(fresh, fresh_clock, state)
    assert pickle.dumps(lab_state(fresh, fresh_clock)) == before
    with pytest.raises(PreconditionFailed, match="incompatible"):
        build_lab_side(db, testbench_config=desired, state_path=state)
    assert state.read_bytes() == saved
    assert old.store.list_samples() == samples
    assert old.store.events_after(0, limit=10000) == events


def _pre_metal_patch_layout(lab):
    """Independently reproduce the pre-fixture signature format."""
    from copy import deepcopy

    config = lab.bench["reactor_module"]
    layout = {k: deepcopy(config[k]) for k in ("geometry", "bench_layout")}
    del layout["geometry"]["metal_patch_outline_mm"]
    if config["experiment_kind"] == "two_flake_reaction":
        layout.update(experiment_kind=config["experiment_kind"], reaction=deepcopy(config["reaction"]))
    return layout


def _layout_hash(layout):
    import hashlib
    import json

    return hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()


def _comparable_lab_state(state):
    # Random instances use identity equality; compare their complete state.
    state["world"]["_observation_rng"] = state["world"]["_observation_rng"].getstate()
    # Historical command wrappers left an incidental None callback in devices.
    # It is not physical state; preserve every other key and any non-None value.
    for attrs in state["devices"].values():
        if "motion_observer" in attrs and attrs["motion_observer"] is None:
            del attrs["motion_observer"]
    return state


@pytest.mark.parametrize("kind", ["two_flake_reaction", "legacy_field_growth"])
def test_pre_metal_patch_checkpoint_preserves_physics_ledger_and_random_streams(tmp_path, kind):
    import pickle

    cfg = quiet_bench()
    cfg["reactor"] = {"enabled": True, "experiment_kind": kind}
    db, checkpoint = tmp_path / "lab.db", tmp_path / "old.pkl"
    old = build_lab_side(db, testbench_config=cfg, seed=8)
    old.gate.arm("arm_left").move_to(Pose(x=12, y=100, z=90))
    old.gate.heater("stamp_heater").set_target(42)
    old.clock.sleep(10)
    old.lab.world.rng.random()
    old.lab.world._observation_rng.random()
    current_signature = old.lab.world.reactor_state["layout_signature"]
    state = lab_state(old.lab, old.clock)
    assert all("motion_observer" not in attrs for attrs in state["devices"].values())
    # Reproduce the exact historical bookkeeping that preceded this fix.
    for device in ("arm_left", "stamp_heater"):
        state["devices"][device]["motion_observer"] = None
    state["world"]["reactor_state"]["layout_signature"] = _layout_hash(_pre_metal_patch_layout(old.lab))
    checkpoint.write_bytes(pickle.dumps(state))
    saved, samples = checkpoint.read_bytes(), old.store.list_samples()
    events = old.store.events_after(0, limit=10000)

    restored = build_lab_side(db, testbench_config=cfg, seed=999, state_path=checkpoint)
    expected = pickle.loads(saved)
    expected["world"]["reactor_state"]["layout_signature"] = current_signature
    assert _comparable_lab_state(lab_state(restored.lab, restored.clock)) == _comparable_lab_state(expected)
    assert restored.lab.world.snapshot() == old.lab.world.snapshot()
    assert restored.store.list_samples() == samples
    assert restored.store.events_after(0, limit=10000) == events
    assert checkpoint.read_bytes() == saved
    # The adopted signature is also usable by the plant and future checkpoints.
    assert restored.lab.devices.get("reactor_microscope").plant.state["layout_signature"] == current_signature
    next_checkpoint = tmp_path / "next.pkl"
    save_lab_state(restored.lab, restored.clock, next_checkpoint)
    again = build_lab_side(db, testbench_config=cfg, state_path=next_checkpoint)
    assert again.lab.world.reactor_state["layout_signature"] == current_signature


@pytest.mark.parametrize("change", ["mechanics", "devices", "geometry", "bench_layout", "experiment", "reaction", "metal_patch"])
def test_pre_metal_patch_compatibility_does_not_relax_other_boundaries(tmp_path, change):
    import pickle

    cfg = quiet_bench()
    cfg["reactor"] = {"enabled": True}
    clock, store = SimClock(), RunStore()
    lab = build_sim_lab(clock, store=store, config=cfg, seed=8)
    before = pickle.dumps(lab_state(lab, clock))
    state = pickle.loads(before)
    layout = _pre_metal_patch_layout(lab)
    if change == "mechanics":
        state["world"]["reactor_state"]["mechanics_version"] = 1
    elif change == "devices":
        del state["devices"]["reactor_insert"]
    elif change == "geometry":
        layout["geometry"]["rod_length_mm"] += 1
    elif change == "bench_layout":
        layout["bench_layout"]["arm_storage_pitch_mm"] += 1
    elif change == "experiment":
        del layout["experiment_kind"], layout["reaction"]
    elif change == "reaction":
        layout["reaction"]["activation_temperature_c"] += 1
    elif change == "metal_patch":
        # A checkpoint that already has a different fixture is not a legacy one.
        layout["geometry"]["metal_patch_outline_mm"] = [[0, 0], [1, 0], [0, 1]]
    state["world"]["reactor_state"]["layout_signature"] = _layout_hash(layout)
    checkpoint = tmp_path / "incompatible.pkl"
    checkpoint.write_bytes(pickle.dumps(state))
    saved, samples = checkpoint.read_bytes(), store.list_samples()
    events = store.events_after(0, limit=10000)
    with pytest.raises(PreconditionFailed, match="incompatible"):
        load_lab_state(lab, clock, checkpoint)
    assert pickle.dumps(lab_state(lab, clock)) == before
    assert checkpoint.read_bytes() == saved
    assert store.list_samples() == samples
    assert store.events_after(0, limit=10000) == events
