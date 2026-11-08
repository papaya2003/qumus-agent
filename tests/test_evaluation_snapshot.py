"""Trusted local/remote scorer projection copies state without observing it."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

from qumus2.core.clock import SimClock
from qumus2.core.types import Pose
from qumus2.runtime.labproc import rpc_methods
from qumus2.runtime.remote import RemoteLab
from qumus2.testbench.lab import build_sim_lab
from qumus2.testbench.world import STAMP_HOLDER, SimFlake, SimObject, StampState
from tests.conftest import quiet_bench


def test_evaluation_snapshot_is_json_copy_without_physical_or_rng_side_effects(monkeypatch):
    clock = SimClock()
    lab = build_sim_lab(clock, config=quiet_bench())
    flake = SimFlake("source", "Gr", 1, .1, .2, 100, origin={"source_chip_id": "original"})
    chip = SimObject("truth_chip", "chip", Pose(x=740, y=420), on="transfer_stage", flakes=[flake])
    stamp = SimObject("truth_stamp", "stamp", Pose(x=0, y=0), held_by=STAMP_HOLDER)
    lab.world.add(chip)
    lab.world.add(stamp)
    lab.world.stamps[stamp.id] = StampState(5.3, 0, 0, 165, stack=[(flake, .3, -.1)])
    rngs = (lab.world.rng.getstate(), lab.world.observation_rng.getstate())
    before = lab.world.evaluation_snapshot()
    monkeypatch.setattr(clock, "now", Mock(side_effect=AssertionError("snapshot read Clock")))
    monkeypatch.setattr(lab.world, "advance_transfer", Mock(side_effect=AssertionError("snapshot advanced physics")))
    monkeypatch.setattr(lab.analyzer, "sharpness", Mock(side_effect=AssertionError("snapshot observed")))
    result = lab.world.evaluation_snapshot()
    json.dumps(result, allow_nan=False)
    assert result == before
    assert (lab.world.rng.getstate(), lab.world.observation_rng.getstate()) == rngs
    saved = next(o for o in result["objects"] if o["id"] == chip.id)
    assert saved["flakes"][0]["origin"]["source_chip_id"] == "original"
    assert result["stamps"][stamp.id]["stack"][0]["relative_xy_mm"] == [.3, -.1]
    saved["flakes"][0]["origin"]["source_chip_id"] = "changed copy"
    result["collisions"].append({"fake": True})
    assert flake.origin["source_chip_id"] == "original" and not lab.world.collisions


def test_same_snapshot_is_available_only_through_trusted_readonly_rpc():
    lab = build_sim_lab(SimClock(), config=quiet_bench())
    methods = rpc_methods(SimpleNamespace(jobs=Mock(), lab=lab))
    calls = []

    class Supervisor:
        def call(self, method, *, ready_timeout_s):
            calls.append(method)
            return methods[method]()

    result = RemoteLab(Supervisor()).world.evaluation_snapshot()
    assert result == lab.world.evaluation_snapshot()
    assert calls == ["lab.evaluation_snapshot"]
