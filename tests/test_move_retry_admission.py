"""Same-job observed off-position recovery without an already-at-destination bypass."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from conftest import put_sample

from qumus2.core.errors import PreconditionFailed
from qumus2.flows.library.place_chip import PlaceChipOnTransferStage
from qumus2.perception.base import Detection


@pytest.fixture
def failed_placement(services):
    """An explicit one-shot fault with L0 disabled only in this unit fixture."""
    put_sample(services, "chip_001", "mid_chip_1")
    original = services.jobs.context_factory
    def context(run_id, params, project_id):
        ctx = original(run_id, params, project_id)
        ctx.verification = deepcopy(ctx.verification)
        if not any(e.kind == "step.finished" for e in services.store.events_after(0, run_id=run_id)):
            ctx.verification["place_chip_on_transfer_stage"]["max_corrections"] = 0
        return ctx
    services.jobs.context_factory = context
    services.lab.faults.inject("next_placement", {"dx": 2., "dy": -1.5, "drot": 6.})
    rid = services.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_001"},
                               project_id=services.project_id)
    assert services.jobs.wait(rid, 10).status == "parked"
    run = services.store.get_run(rid)
    assert services.store.get_sample("chip_001")["location"] == "transfer_stage"
    ctx = original(rid, run["params"], services.project_id)
    assert ctx.verification["place_chip_on_transfer_stage"]["max_corrections"] == 4
    return services, rid, ctx


def test_same_job_retry_uses_fresh_public_pose_and_normal_gate_moves(failed_placement):
    services, rid, ctx = failed_placement
    before = services.store.last_event_id()
    result, _ = asyncio.run(services.gateway.call("hands", "retry_job", {
        "job_id": rid, "reason": "Public placement check found the chip on the stage outside tolerance"}))
    assert result.status == "pending" and result.data["job_id"] == rid
    assert services.jobs.wait(rid, 10).status == "succeeded"
    assert len(services.store.list_runs()) == 1
    events = services.store.events_after(before, run_id=rid)
    assert any(e.kind == "device.command" and e.payload.get("device") == "arm_right" for e in events)
    state = services.store.get_project(services.project_id)["state"]
    assert state["recovery"] == {"total": 1, "jobs": {rid: 1}}
    step = PlaceChipOnTransferStage("chip_001")
    with pytest.raises(PreconditionFailed, match="already at"):
        step.preconditions(ctx)  # A completed placement is not another retry grant.


@pytest.mark.parametrize("change", ["other_job", "completed", "stale_failure", "uncertain", "changed_pose",
                                    "occupied", "ambiguous", "moved_since_failure"])
def test_same_destination_rejects_unbound_or_ambiguous_recovery(failed_placement, monkeypatch, change):
    services, rid, ctx = failed_placement
    step = PlaceChipOnTransferStage("chip_001")
    if change == "other_job":
        other = services.store.create_run("place_chip_on_transfer_stage", {"chip_id": "chip_001"},
                                          project_id=services.project_id)
        ctx = services.jobs.context_factory(other, {"chip_id": "chip_001"}, services.project_id)
    elif change == "completed":
        services.store.finish_step(rid, 0, "done")
    elif change == "stale_failure":
        services.store.begin_step(rid, 0, step.name)
        services.store.finish_step(rid, 0, "failed", note="precondition refused without a new observation")
    elif change == "uncertain":
        services.store.upsert_sample("chip_001", "chip", props={"location_uncertain": True})
    elif change == "changed_pose":
        pose = dict(services.store.get_sample("chip_001")["props"]["pose"])
        pose["x"] += 1
        services.store.upsert_sample("chip_001", "chip", props={"pose": pose})
    elif change == "occupied":
        services.store.upsert_sample("other_chip", "chip", location="transfer_stage")
    else:
        pose = step._failed_placement_pose(ctx)
        assert pose is not None
        detections = [Detection(label="chip", pose=pose, confidence=.9)]*2 if change == "ambiguous" else [
            Detection(label="chip", pose=pose.offset(dx=3.1), confidence=.9)]
        monkeypatch.setattr(step, "_look", lambda *a, **kw: detections)
    before = services.store.last_event_id()
    with pytest.raises(PreconditionFailed):
        step.preconditions(ctx)
    assert not any(e.kind == "device.command" and e.payload.get("device") == "arm_right"
                   for e in services.store.events_after(before))
