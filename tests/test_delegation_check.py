"""Code checks what a subagent did before its plan step counts as done, and
the delegation rules mean what they say (eyes really looked, the advisor
looked up experience, web look-ups are logged)."""

from __future__ import annotations

import asyncio

from qumus2.agents.tooling import ToolResult
from qumus2.runtime.jobs import JobStatus


def plan(s, *owners: str) -> None:
    s.store.set_plan(s.project_id, [{"id": f"s{i}", "title": o, "owner": o, "status": "pending"}
                                    for i, o in enumerate(owners, start=1)],
                     author="orchestrator")


def steps(s) -> dict[str, str]:
    return {st["id"]: st["status"] for st in s.store.get_project(s.project_id)["plan"]}


def checked(s) -> list[dict]:
    return [e.payload for e in s.store.events_after(0, limit=100_000)
            if e.kind == "agent.delegation_checked"]


class FakeJobs:
    def __init__(self, **status: str) -> None:
        self.st = status

    def status(self, rid: str) -> JobStatus:
        bad = self.st[rid] != "succeeded"
        return JobStatus(rid, "place_chip_on_transfer_stage", self.st[rid], True,
                         steps=[{"name": "move_object", "status": "failed" if bad else
                                 "succeeded"}],
                         error={"message": "chip still off by 0.4 mm"} if bad else None)


def submitted(g, rid: str) -> None:
    g.post("hands", "place_chip_on_transfer_stage", ToolResult(
        status="pending", summary=f"job {rid} submitted",
        data={"job_id": rid, "flow": "place_chip_on_transfer_stage"}))


def test_exfoliation_is_delegated_to_the_advisor_first(services):
    """F8 (2026-10-22): the hands cannot look up experience, so sending them
    to exfoliate before the advisor looked the material up is refused at the
    delegation, not three tool calls later."""
    s, g = services, services.gateway
    plan(s, "hands")
    s.store.update_project_state(s.project_id, eyes_checked=True)
    d = g.delegate("hands", task="s1: exfoliate\nExfoliate hBN onto two bare chips.")
    assert d.rule == "advisor_first" and "hBN" in d.reason and "advisor" in d.reason
    assert g.delegate("hands", task="Place chip_001 on the transfer stage.").allow
    s.store.update_project_state(s.project_id, experience_checked=["hBN"])
    assert g.delegate("hands", task="Exfoliate hBN onto two bare chips.").allow
    d = g.delegate("hands", task="Peel graphene onto a chip, then wait for the job.")
    assert d.rule == "advisor_first" and "Gr" in d.reason and "hBN" not in d.reason
    assert g.delegate("hands", task="exfoliate one more chip").allow  # no material named
    denied = [e.payload for e in s.store.events_after(0, limit=100_000)
              if e.kind == "tool.denied" and e.payload["rule"] == "advisor_first"]
    assert len(denied) == 2 and denied[0]["args"]["to"] == "hands"
    # the hands' own rule tells them not to retry and not to call a person
    r = asyncio.run(g.call("hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1}))[0]
    assert r.status == "denied" and r.data["rule"] == "experience_checked"
    assert "Do not retry" in r.summary and "advisor must look up experience for Gr" in r.summary


def test_hands_step_done_only_when_its_job_succeeded(services):
    s, g = services, services.gateway
    plan(s, "hands", "hands")
    s.store.update_project_state(s.project_id, eyes_checked=True)
    g.jobs = FakeJobs(r1="succeeded", r2="parked")

    g.delegation_started("a", "hands", "s1: place")
    submitted(g, "r1")
    facts = g.delegation_finished("a")
    assert "job r1 (place_chip_on_transfer_stage) succeeded" in facts
    assert steps(s)["s1"] == "done"
    assert g.delegation_finished("a") == facts  # the second caller gets the same facts

    g.delegation_started("b", "hands", "s2: place again")
    submitted(g, "r2")
    facts = g.delegation_finished("b")  # the hands said "done"; the job parked
    assert steps(s)["s2"] == "failed"
    assert "parked at step move_object: chip still off by 0.4 mm" in facts
    # the bench may have changed: the eyes must look again before the hands go
    assert not s.store.get_project(s.project_id)["state"]["eyes_checked"]
    assert g.delegate("hands").rule == "eyes_first"
    assert [c["status"] for c in checked(s)] == ["done", "failed"]


def test_new_job_of_same_flow_cannot_hide_an_unresolved_attempt(services):
    s, g = services, services.gateway
    plan(s, "hands")
    g.jobs = FakeJobs(r1="failed", r2="succeeded")
    g.delegation_started("a", "hands", "s1: place")
    submitted(g, "r1")
    submitted(g, "r2")
    facts = g.delegation_finished("a")
    assert steps(s)["s1"] == "failed"
    assert "job r1" in facts and "job r2" in facts


def test_recovery_of_same_run_uses_current_state_not_old_observations(services):
    s, g = services, services.gateway
    plan(s, "hands")
    g.jobs = FakeJobs(r1="parked")
    g.delegation_started("a", "hands", "s1: place")
    submitted(g, "r1")
    g.post("hands", "wait_job", ToolResult(status="needs_attention", summary="parked",
        data={"run_id": "r1", "flow": "place_chip_on_transfer_stage"}))
    g.jobs.st["r1"] = "succeeded"
    submitted(g, "r1")
    facts = g.delegation_finished("a")
    assert steps(s)["s1"] == "done" and facts.count("job r1") == 1


def test_hands_without_a_job_and_unfinished_jobs(services):
    s, g = services, services.gateway
    plan(s, "hands", "hands")
    g.jobs = FakeJobs(r1="running")
    g.delegation_started("a", "hands", "s1: place")
    assert "without running a hardware job" in g.delegation_finished("a")
    assert steps(s)["s1"] == "pending"
    g.delegation_started("b", "hands", "s2: place")
    submitted(g, "r1")
    g.delegation_finished("b")
    assert steps(s)["s2"] == "running"  # still going: neither done nor failed


def test_picker_and_designer_need_an_accepted_result(services):
    s, g = services, services.gateway
    plan(s, "picker", "designer")
    g.delegation_started("a", "picker", "s1: select")
    g.post("picker", "report_selection_blocked", ToolResult(
        status="ok", summary="role channel blocked: exfoliate_and_search_one_chip"))
    facts = g.delegation_finished("a")
    assert steps(s)["s1"] == "pending" and "role channel blocked" in facts

    g.delegation_started("b", "picker", "s1: select again")
    g.post("picker", "submit_flake_selection", ToolResult(status="ok", summary="selected 3"))
    g.delegation_finished("b")
    assert steps(s)["s1"] == "done"

    g.delegation_started("c", "designer", "s2: lay out")
    g.post("designer", "submit_design", ToolResult(status="error", summary="channel too small"))
    g.delegation_finished("c")
    assert steps(s)["s2"] == "pending"


def test_advisor_consult_is_done_but_a_refused_plan_is_not(services):
    s, g = services, services.gateway
    plan(s, "advisor", "advisor")
    g.delegation_started("a", "advisor", "s1: parameters")
    g.post("advisor", "get_experience", ToolResult(status="ok", summary="2 entries"))
    g.delegation_finished("a")
    g.delegation_started("b", "advisor", "s2: device plan")
    g.post("advisor", "submit_device_plan", ToolResult(status="error", summary="no roles"))
    g.delegation_finished("b")
    assert steps(s) == {"s1": "done", "s2": "pending"}


def test_explicit_status_is_kept(services):
    """A step handed back unfinished (scripted "running") or an error stays so."""
    s, g = services, services.gateway
    plan(s, "picker")
    g.delegation_started("a", "picker", "s1: select")
    g.delegation_finished("a", "running")
    assert steps(s)["s1"] == "running"


def test_eyes_first_needs_a_successful_inspect_bench(services):
    s, g = services, services.gateway
    plan(s, "eyes", "hands")
    assert g.delegate("eyes").allow  # delegating to the eyes alone proves nothing
    assert g.delegate("hands").rule == "eyes_first"

    async def call(agent, tool, args):
        return (await g.call(agent, tool, args))[0]

    asyncio.run(call("eyes", "list_samples", {}))
    assert g.delegate("hands").rule == "eyes_first"
    r = asyncio.run(call("eyes", "inspect_bench", {}))
    assert r.status in ("ok", "corrected")
    assert g.delegate("hands").allow


def test_experience_counts_only_when_the_advisor_looks(services):
    s, g = services, services.gateway

    async def call(agent):
        await g.call(agent, "get_experience", {"material": "Gr"})
        return s.store.get_project(s.project_id)["state"].get("experience_checked", [])

    assert asyncio.run(call("hands")) == []
    assert asyncio.run(call("orchestrator")) == []
    assert asyncio.run(call("advisor")) == ["Gr"]


def test_successful_wrong_direction_job_does_not_meet_storage_goal(services):
    s, g = services, services.gateway
    plan(s, 'hands')
    task = s.store.get_project(s.project_id)['plan']
    task[0]['sample_outcomes'] = [{'sample_id': 'chip_001', 'status': 'searched', 'in_storage': True}]
    s.store.set_plan(s.project_id, task, author='orchestrator')
    s.store.upsert_sample('chip_001', 'chip', location='transfer_stage', status='searched')
    g.jobs = FakeJobs(r1='succeeded')
    g.delegation_started('storage', 'hands', 's1: return the chip to storage')
    submitted(g, 'r1')
    facts = g.delegation_finished('storage')
    assert steps(s)['s1'] == 'pending'
    assert 'transfer_stage is not a matching storage slot' in facts
    assert checked(s)[-1]['ok'] is False
    # The same success may complete only once its explicit ledger goal is met.
    s.store.upsert_sample('chip_001', 'chip', location='mid_chip_1')
    g.delegation_started('correct', 'hands', 's1: return the chip to storage')
    submitted(g, 'r1')
    g.delegation_finished('correct')
    assert steps(s)['s1'] == 'done'


def test_inflight_sample_goal_cannot_be_removed_to_claim_completion(services):
    s, g = services, services.gateway
    plan(s, 'hands')
    task = s.store.get_project(s.project_id)['plan']
    task[0]['sample_outcomes'] = [{'sample_id': 'chip_001', 'location': 'mid_chip_1'}]
    s.store.set_plan(s.project_id, task, author='orchestrator')
    g.jobs = FakeJobs(r1='succeeded')
    g.delegation_started('a', 'hands', 's1: return chip')
    task[0]['sample_outcomes'] = []
    s.store.set_plan(s.project_id, task, author='orchestrator')
    submitted(g, 'r1')
    assert 'expected mid_chip_1' in g.delegation_finished('a')
    assert steps(s)['s1'] == 'pending'


def test_uncertain_location_cannot_satisfy_a_sample_outcome(services):
    from qumus2.records.plan_completion import unmet_sample_outcomes

    s = services
    s.store.upsert_sample('chip_001', 'chip', location='mid_chip_1', props={'location_uncertain': True})
    errors = unmet_sample_outcomes(s.store, s.lab.bench, [{'sample_id': 'chip_001', 'in_storage': True}])
    assert errors and 'uncertain' in errors[0]


def test_changed_sample_goal_does_not_inherit_done(services):
    s = services
    original = [{'id': 's1', 'title': 'Search chip', 'owner': 'hands', 'status': 'done',
                 'sample_outcomes': [{'sample_id': 'chip_001', 'status': 'searched'}]}]
    s.store.set_plan(s.project_id, original, author='orchestrator')
    updated = [{**original[0], 'status': 'pending',
                'sample_outcomes': [{'sample_id': 'chip_002', 'status': 'searched'}]}]
    s.store.set_plan(s.project_id, updated, author='orchestrator', preserve_progress_ids={'s1'})
    assert steps(s)['s1'] == 'pending'


def test_completed_old_target_cannot_complete_a_retargeted_step(services):
    s, g = services, services.gateway
    task = [{'id': 's1', 'title': 'Place chip', 'owner': 'hands', 'status': 'pending',
             'sample_outcomes': [{'sample_id': 'chip_001', 'location': 'transfer_stage'}]}]
    s.store.set_plan(s.project_id, task, author='orchestrator')
    g.delegation_started('a', 'hands', 's1: place chip_001')
    s.store.upsert_sample('chip_001', 'chip', location='transfer_stage')
    task[0]['sample_outcomes'] = [{'sample_id': 'chip_002', 'location': 'transfer_stage'}]
    s.store.set_plan(s.project_id, task, author='orchestrator')
    g.jobs = FakeJobs(r1='succeeded')
    submitted(g, 'r1')
    assert 'completion conditions changed' in g.delegation_finished('a')
    assert steps(s)['s1'] == 'pending'


def test_update_plan_checks_done_conditions_through_actual_gateway(services):
    s, g = services, services.gateway
    step = {'id': 's1', 'title': 'Store chip', 'owner': 'hands', 'status': 'done',
            'sample_outcomes': [{'sample_id': 'chip_001', 'in_storage': True}]}
    result = asyncio.run(g.call('orchestrator', 'update_plan', {'plan': [step], 'note': 'verify stored state'}))[0]
    assert result.status == 'error' and 'delegation/job check' in result.summary
    # A current ledger predicate is not proof that a new physical task ran.
    step['status'] = 'pending'
    assert asyncio.run(g.call('orchestrator', 'update_plan', {'plan': [step], 'note': 'start'}))[0].status == 'ok'
    step['status'] = 'done'
    s.store.upsert_sample('chip_001', 'chip', location='transfer_stage')
    s.store.set_step_status(s.project_id, 's1', 'pending')
    version = s.store.get_project(s.project_id)['plan_version']
    result = asyncio.run(g.call('orchestrator', 'update_plan', {'plan': [step], 'note': 'claim storage',
                                                            'expected_version': version}))[0]
    assert result.status == 'error' and 'delegation/job check' in result.summary
    # Omitting conditions from this new pending step cannot masquerade as legacy history.
    step.pop('sample_outcomes')
    result = asyncio.run(g.call('orchestrator', 'update_plan', {'plan': [step], 'note': 'legacy',
                                                            'expected_version': version}))[0]
    assert result.status == 'error', result.summary


def test_later_processing_does_not_reopen_a_completed_historical_condition(services):
    s, g = services, services.gateway
    s.store.upsert_sample('chip_001', 'chip', location='mid_chip_1', status='exfoliated')
    step = {'id': 's1', 'title': 'Exfoliate', 'owner': 'hands', 'status': 'done',
            'sample_outcomes': [{'sample_id': 'chip_001', 'status': 'exfoliated', 'in_storage': True}]}
    # Fixture: historical execution already completed, rather than a new model claim.
    s.store.set_plan(s.project_id, [step], author='orchestrator')
    s.store.upsert_sample('chip_001', 'chip', status='searched')
    version = s.store.get_project(s.project_id)['plan_version']
    result = asyncio.run(g.call('orchestrator', 'update_plan', {'plan': [step], 'note': 'later processing',
                                                            'expected_version': version}))[0]
    assert result.status == 'ok', result.summary
    assert steps(s)['s1'] == 'done'
