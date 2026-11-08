"""Synthetic job records test exact references without a lab or device worker."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from qumus2.agents.gateway import ToolGateway
from qumus2.agents.tooling import default_registry
from qumus2.config import load_yaml
from qumus2.runtime.jobs import ThreadJobService
from qumus2.runtime.store import RunStore


def call(s, tool, job_id, role="hands"):
    return asyncio.run(s.gateway.call(role, tool, {"job_id": job_id}))[0]


def linked(s, *, project=None, step="s8", status="succeeded", chip="chip_001"):
    pid = project or s.project_id
    rid = s.store.create_run("search_chip", {"chip_id": chip, "material": "hBN"}, project_id=pid)
    s.store.set_run_status(rid, status)
    proof = s.store.emit("agent.job_linked", {"project_id": pid, "step_id": step,
                        "job_id": rid, "flow": "search_chip"}, run_id=rid, agent="hands")
    return rid, proof


@pytest.fixture
def context(monkeypatch):
    store = RunStore(":memory:")
    pid = store.create_project("Synthetic reference test")
    store.upsert_sample("chip_001", "chip", status="searched", location="mid_chip_1", props={"material": "hBN"})
    step = {"id": "s8", "owner": "hands", "title": "Search hBN", "status": "pending", "operation": "search_chip",
            "materials": ["hBN"], "sample_outcomes": [{"sample_id": "chip_001", "status": "searched", "in_storage": True}]}
    store.set_plan(pid, [step, {**step, "id": "s10"}], author="synthetic_fixture", note="Explicitly synthetic jobs")

    def forbidden(*args, **kwargs):
        raise AssertionError("Reference checks cannot submit or execute jobs")

    jobs = ThreadJobService(store, None, forbidden, workers=0)
    monkeypatch.setattr(jobs, "submit", forbidden)
    gateway = ToolGateway(default_registry(), store, jobs, project_id=pid, bench=load_yaml("bench.yaml"))
    s = SimpleNamespace(store=store, project_id=pid, jobs=jobs, gateway=gateway)
    yield s
    store.close()


@pytest.mark.parametrize("tool", ["wait_job", "get_job_status"])
def test_truncated_reference_returns_nested_exact_evidence_without_certifying_or_executing(context, tool):
    s = context
    rid, proof = linked(s)
    s.gateway.delegation_started("failed-lookup", "hands", f"s8: Read {rid[:-1]}")
    before, jobs = s.store.last_event_id(), s.store.list_runs()
    result = call(s, tool, rid[:-1])
    assert result.status == "error" and "no job" in result.summary
    assert "submit the job first" not in result.summary
    assert result.data["requested_job_id"] == rid[:-1]
    assert result.data["step_id"] == "s8" and result.data["project_id"] == s.project_id
    assert not {"job_id", "run_id"} & result.data.keys()
    known = result.data["known_jobs"]
    assert len(known) == 1 and known[0]["job_id"] == rid
    assert known[0]["status"] == "succeeded" and known[0]["targets"]["chip_id"] == "chip_001"
    assert known[0]["linked_event_ids"] == [proof]
    assert known[0]["status_event_id"] < proof
    facts = s.gateway.delegation_finished("failed-lookup")
    assert "s8 is now pending" in facts
    events = s.store.events_after(before)
    assert not any(e.kind in ("job.submitted", "device.command", "agent.job_linked") for e in events)
    assert jobs == s.store.list_runs()
    # Explicitly using the exact ID is a separate observation; only its normal
    # delegation check may complete the step.
    s.gateway.delegation_started("exact-lookup", "hands", "s8: Read the exact original job")
    assert call(s, tool, rid).status == "ok"
    assert "s8 is now done" in s.gateway.delegation_finished("exact-lookup")
    assert jobs == s.store.list_runs()


def test_multiple_candidates_are_evidence_not_a_fuzzy_replacement(context):
    s = context
    first, _ = linked(s)
    second, _ = linked(s, status="failed")
    s.gateway.delegation_started("lookup", "hands", "s8: Inspect saved references")
    result = call(s, "wait_job", "completely_unrelated_id")
    assert result.status == "error"
    assert {j["job_id"]: j["status"] for j in result.data["known_jobs"]} == {first: "succeeded", second: "failed"}
    assert "replacement" in result.summary
    assert s.store.get_project(s.project_id)["plan"][0]["status"] == "running"
    assert len(s.store.list_runs()) == 2


def test_other_steps_projects_and_forged_cross_project_links_are_excluded(context):
    s = context
    original, _ = linked(s)
    other_step, _ = linked(s, step="s10")
    other_project = s.store.create_project("Other")
    foreign, _ = linked(s, project=other_project)
    s.store.emit("agent.job_linked", {"project_id": s.project_id, "step_id": "s8", "job_id": foreign,
                 "flow": "search_chip"}, run_id=foreign, agent="hands")
    s.gateway.delegation_started("lookup", "hands", "s8: Inspect the original job")
    result = call(s, "get_job_status", foreign[:-1])
    assert [r["job_id"] for r in result.data["known_jobs"]] == [original]
    summary = s.gateway.project_summary()
    evidence = summary.split("Current delegated step job records", 1)[1]
    assert original in evidence and foreign not in evidence and other_step not in evidence


@pytest.mark.parametrize("scope", ["no_delegation", "ambiguous", "other_role", "wrong_owner"])
def test_unproven_active_step_does_not_offer_guessed_references(context, scope):
    s = context
    linked(s)
    s.store.set_step_status(s.project_id, "s8", "running")
    if scope != "no_delegation":
        s.gateway.delegation_started("one", "hands" if scope != "wrong_owner" else "reactor", "s8: Read")
    if scope == "ambiguous":
        s.gateway.delegation_started("two", "hands", "s10: Read")
    role = "orchestrator" if scope == "other_role" else "reactor" if scope == "wrong_owner" else "hands"
    result = call(s, "get_job_status", "run_missing", role)
    assert result.status == "error" and result.data["step_id"] is None
    assert result.data["known_jobs"] == []


def test_specialist_context_contains_successful_original_job_without_mutation(context):
    s = context
    rid, proof = linked(s)
    s.gateway.delegation_started("lookup", "hands", f"s8: Read incorrectly transcribed {rid[:-1]}")
    before = s.store.last_event_id()
    summary = s.gateway.project_summary()
    assert "read-only evidence" in summary and f'"job_id": "{rid}"' in summary
    assert '"status": "succeeded"' in summary and f'"linked_event_ids": [{proof}]' in summary
    assert s.store.last_event_id() == before


def test_job_reference_ui_evidence():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for offline recovery-event tests")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("job_reference_ui.mjs"))],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
