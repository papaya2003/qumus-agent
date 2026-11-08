"""Jialei 2026-11-03: a chip placed off target is placed again automatically."""
from __future__ import annotations

import asyncio
from copy import deepcopy

from conftest import put_sample

from qumus2.agents.scripted import ScriptedRuntime


def _off_target_job(services, max_corrections=0):
    """One placement whose L0 re-places are disabled, so it parks off target."""
    put_sample(services, "chip_001", "mid_chip_1")
    original = services.jobs.context_factory

    def context(run_id, params, project_id):
        ctx = original(run_id, params, project_id)
        ctx.verification = deepcopy(ctx.verification)
        ctx.verification["place_chip_on_transfer_stage"]["max_corrections"] = max_corrections
        return ctx

    services.jobs.context_factory = context
    services.lab.faults.inject("next_placement", {"dx": 2., "dy": -1.5, "drot": 6.})
    return services.jobs.submit("place_chip_on_transfer_stage", {"chip_id": "chip_001"},
                                project_id=services.project_id)


def test_scripted_hands_place_an_off_target_chip_again(services):
    rid = _off_target_job(services)
    runtime = ScriptedRuntime(services.gateway, {"turns": []})
    result = asyncio.run(runtime._tool("hands", "wait_job", {"job_id": rid, "timeout_s": 30}))
    assert result.status in ("ok", "corrected"), result
    assert services.store.get_run(rid)["status"] == "succeeded"
    tools = [name for _, name, _ in runtime.results]
    assert tools == ["wait_job", "retry_job", "wait_job"]
    state = services.store.get_project(services.project_id)["state"]
    assert state["recovery"]["jobs"] == {rid: 1}


def test_scripted_retries_are_bounded_and_end_parked(services):
    rid = _off_target_job(services)
    # every placement lands off target: the hands stop after the per-job
    # recovery budget (config/agents.yaml recovery.per_job = 2) and the job
    # stays parked for a person
    services.lab.world.faults.big_error_prob = 1.0
    services.lab.world.faults.big_error_mm = 1.0  # always outside 0.5 mm, never lost
    runtime = ScriptedRuntime(services.gateway, {"turns": []})
    result = asyncio.run(runtime._tool("hands", "wait_job", {"job_id": rid, "timeout_s": 30}))
    assert result.status == "needs_attention"
    assert services.store.get_run(rid)["status"] == "parked"
    retries = [r for _, name, r in runtime.results if name == "retry_job"]
    assert [r.status for r in retries] == ["pending", "pending"]
    state = services.store.get_project(services.project_id)["state"]
    assert state["recovery"]["jobs"] == {rid: 2}


def test_live_hands_are_told_to_re_place_an_off_target_chip():
    """The same rule for a live model: an off-target placement gets retry_job."""
    from qumus2.agents.roster import load_roster

    prompt = " ".join(load_roster()["hands"].prompt.split())
    assert "landed outside tolerance after its own re-places" in prompt
    assert "gets retry_job: place it again" in prompt
