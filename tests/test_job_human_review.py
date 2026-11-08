"""A person's per-job hold review is audited, inert and unavailable to agents."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from qumus2.flows.library.place_chip import PlaceChipOnTransferStage
from qumus2.flows.workflow import WorkflowMismatch
from qumus2.runtime import jobs
from qumus2.runtime.labproc import rpc_methods
from qumus2.runtime.remote import RemoteJobService
from qumus2.runtime.rpc import RpcClient, RpcServer
from qumus2.ui.app import create_app


def held_job(services):
    """Explicit synthetic journal fixture; no real job or device command runs."""
    store = services.store
    rid = store.create_run("place_chip_on_transfer_stage", {"chip_id": "chip_001"}, project_id=services.project_id)
    step = "place_chip_on_transfer_stage"
    store.begin_step(rid, 0, step)
    store.emit("step.verified", {"seq": 0, "name": step, "verdict": "unclear",
        "measurements": {"found": False}, "expected": {}, "images": [], "message": "Fixture requires human inspection"}, run_id=rid)
    store.finish_step(rid, 0, "failed", note="Fixture requires human inspection")
    result = [{"step": step, "status": "needs_agent", "attempts": 1, "corrections": 0,
               "observations": [], "error": {"type": "NeedsHumanError", "category": "needs_human",
                   "message": "Fixture requires human inspection", "details": {}}}]
    store.set_run_status(rid, "waiting_human", result=result, message="Fixture requires human inspection")
    return rid, store.last_event_id()


def report(services, rid, failure, **changes):
    return {"project_id": services.project_id, "failure_event_id": failure,
            "reviewer": "Fixture operator", "reason": "Inspected the fixture; normal recovery must recheck it", **changes}


def test_review_records_one_receipt_and_only_parks_without_motion(services):
    rid, failure = held_job(services)
    services.store.update_project_state(services.project_id, recovery={"total": 2, "jobs": {rid: 2}})
    world, clock = services.lab.world.evaluation_snapshot(), services.clock.now()
    original = services.store.get_run(rid)
    steps, start = services.store.get_steps(rid), services.store.last_event_id()
    output = services.jobs.review_waiting(rid, **report(services, rid, failure))
    assert output["status"] == "parked" and output["motion_started"] is False
    assert services.store.get_run(rid)["status"] == "parked"
    assert services.store.get_run(rid)["result"] == original["result"]
    assert services.store.get_steps(rid) == steps
    assert services.lab.world.evaluation_snapshot() == world and services.clock.now() == clock
    assert services.store.get_project(services.project_id)["state"]["recovery"] == {"total": 2, "jobs": {rid: 2}}
    events = services.store.events_after(start)
    assert [e.kind for e in events] == ["job.human_reviewed", "job.status"]
    body = events[0].payload
    assert body["failure_event_id"] == failure and body["failed_step"]["seq"] == 0
    assert len(body["failure_event_sha256"]) == len(body["failed_step"]["event_sha256"]) == 64
    assert body["reviewer"] == "Fixture operator" and body["safe_energy_targets"]
    assert services.jobs.resume_all() == []
    current = services.store.last_event_id()
    with pytest.raises(ValueError, match="changed"):
        services.jobs.review_waiting(rid, **report(services, rid, failure))
    assert services.store.last_event_id() == current


@pytest.mark.parametrize("change", ["stale", "other_project", "blank", "bool_id", "dangerous", "shutdown",
    "pending", "unknown", "scalar_outcome", "malformed_error", "malformed_run_error", "stored_unsafe", "current_unsafe",
    "motion", "busy_job", "conflicting_lock", "on", "unreadable", "source"])
def test_review_rejects_stale_or_unsafe_holds_without_any_mutation(services, monkeypatch, change):
    rid, failure = held_job(services)
    args = report(services, rid, failure)
    if change == "stale":
        services.store.set_run_status(rid, "waiting_human", message="A fresh failure superseded this review")
    elif change == "other_project":
        args["project_id"] = "another_project"
    elif change == "blank":
        args["reason"] = "  "
    elif change == "bool_id":
        args["failure_event_id"] = True
    elif change in ("dangerous", "shutdown"):
        error = {"category": "dangerous"} if change == "dangerous" else {"safe_shutdown_failed": ["stamp_heater"]}
        services.store.set_run_status(rid, "waiting_human", error=error)
        args["failure_event_id"] = services.store.last_event_id()
    elif change == "pending":
        services.store.update_run_meta(rid, normal_transfer={"phases": {"fixture": {"pending_intent": {"axis": "z"}}}})
    elif change == "unknown":
        services.store.set_run_status(rid, "waiting_human", result=[])
        args["failure_event_id"] = services.store.last_event_id()
    elif change in ("scalar_outcome", "malformed_error", "malformed_run_error"):
        original = services.store.get_run(rid)["result"]
        if change == "scalar_outcome":
            original = ["unknown legacy outcome"]
        elif change == "malformed_error":
            original[-1]["error"] = "unknown legacy error"
        services.store.set_run_status(rid, "waiting_human", result=original,
                                      error="unknown legacy error" if change == "malformed_run_error" else None)
        args["failure_event_id"] = services.store.last_event_id()
    elif change == "stored_unsafe":
        services.store._x("UPDATE step SET safe_to_resume=0 WHERE run_id=?", (rid,))
    elif change == "current_unsafe":
        monkeypatch.setattr(PlaceChipOnTransferStage, "safe_to_resume", False)
    elif change == "motion":
        services.lab.devices.get("arm_right").motion_active = True
    elif change == "busy_job":
        services.store.create_run("place_chip_on_transfer_stage", {"chip_id": "chip_002"}, project_id=services.project_id)
    elif change == "conflicting_lock":
        services.store.acquire_locks(["arm_right"], "another_job")
    elif change == "on":
        services.gate.heater("stamp_heater").set_target(25.)
    elif change == "unreadable":
        def unavailable():
            raise RuntimeError("Fixture cannot confirm heater target")
        monkeypatch.setattr(services.lab.devices.get("stamp_heater"), "get_target", unavailable)
    elif change == "source":
        def mismatch(*args, **kwargs):
            raise WorkflowMismatch("fixture_changed", "Saved procedure changed")
        monkeypatch.setattr(jobs, "build_run_steps", mismatch)
    original, steps, cursor = deepcopy(services.store.get_run(rid)), services.store.get_steps(rid), services.store.last_event_id()
    with pytest.raises(ValueError):
        services.jobs.review_waiting(rid, **args)
    assert services.store.get_run(rid) == original and services.store.get_steps(rid) == steps
    assert services.store.last_event_id() == cursor


def test_api_has_fresh_failure_identity_and_strict_human_only_payload(services):
    rid, failure = held_job(services)
    with TestClient(create_app(services, start_driver=False)) as client:
        body = client.get(f"/api/runs/{rid}").json()
        assert body["human_review"]["failure_event_id"] == failure
        payload = {k: v for k, v in report(services, rid, failure).items() if k != "project_id"}
        assert client.post(f"/api/runs/{rid}/human-review", json={**payload, "force": True}).status_code == 422
        assert client.post(f"/api/runs/{rid}/human-review", json={**payload, "reviewer": " "}).status_code == 422
        assert client.post(f"/api/runs/{rid}/human-review", json={**payload, "failure_event_id": True}).status_code == 422
        assert client.post(f"/api/runs/{rid}/human-review", json={**payload, "failure_event_id": failure-1}).status_code == 409
        response = client.post(f"/api/runs/{rid}/human-review", json=payload)
        assert response.status_code == 200 and response.json()["motion_started"] is False
        assert client.get(f"/api/runs/{rid}").json()["human_review"] is None
    assert services.gateway.registry.get("review_waiting") is None
    output, _ = asyncio.run(services.gateway.call("hands", "review_waiting", {}))
    assert output.status in ("denied", "error")


def test_remote_rpc_uses_same_central_review_and_rejects_duplicate(services):
    rid, failure = held_job(services)
    auth = b"explicit-local-unit-fixture"
    server = RpcServer(rpc_methods(SimpleNamespace(jobs=services.jobs, lab=services.lab)), auth)
    server.start()
    client = RpcClient(server.address, auth)
    class Bridge:
        store = services.store
        def call(self, name, *args, **kwargs):
            kwargs.pop("ready_timeout_s", None)
            return client.call(name, *args, **kwargs)
    try:
        remote = RemoteJobService(Bridge())
        output = remote.review_waiting(rid, **report(services, rid, failure))
        assert output["status"] == services.store.get_run(rid)["status"] == "parked"
        with pytest.raises(ValueError, match="changed"):
            remote.review_waiting(rid, **report(services, rid, failure))
    finally:
        server.close()
