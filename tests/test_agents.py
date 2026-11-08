"""Tool contract, PreToolUse policy, gateway (spec 6.3)."""

from __future__ import annotations

import asyncio

from qumus2.agents.policy import RULES
from qumus2.agents.roster import ALL_AGENTS, load_roster
from qumus2.agents.tooling import default_registry, mcp_name, short_name
from qumus2.runtime.events import EVENT_KINDS


def call(s, agent, tool, args):
    return asyncio.run(s.gateway.call(agent, tool, args))


def test_registry_is_consistent():
    reg = default_registry()
    roster = load_roster()
    assert set(roster) == set(ALL_AGENTS)
    for t in reg.all():
        assert t.agents <= set(ALL_AGENTS), t.name
        assert set(t.requires) <= set(RULES), t.name
        assert t.json_schema()["type"] == "object"
        assert short_name(mcp_name(t.name)) == t.name
    assert {t.name for t in reg.for_agent("hands")} >= {"place_chip_on_transfer_stage",
                                                         "wait_job"}
    assert not any(t.kind == "hardware" for t in reg.for_agent("eyes"))


def test_wrong_agent_is_denied(services):
    r, _ = call(services, "eyes", "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert r.status == "denied" and r.data["rule"] == "permission"
    ev = services.store.events_after(0, limit=1000)[-1]
    assert ev.kind == "tool.denied" and ev.agent == "eyes"
    assert services.store.list_runs() == []


def test_bad_args_and_invented_ids_are_denied(services):
    r, _ = call(services, "hands", "place_chip_on_transfer_stage", {"chip": "chip_001"})
    assert r.data["rule"] == "schema"
    r, _ = call(services, "hands", "place_chip_on_transfer_stage", {"chip_id": "chip_999"})
    assert r.data["rule"] == "refs"
    r, _ = call(services, "hands", "mcp__qumus__wait_job", {"job_id": "x", "timeout_s": 9999})
    assert r.data["rule"] == "schema"


def test_cross_tool_rule_and_veto(services):
    s = services
    r, _ = call(s, "designer", "get_design_context", {})
    assert r.status == "denied" and r.data["rule"] == "flakes_selected"
    s.store.upsert_sample("flake_1", "flake", location="chip_001")
    r, _ = call(s, "orchestrator", "exclude_flake", {"flake_id": "flake_1", "reason": "user"})
    assert r.status == "ok"
    r, _ = call(s, "picker", "submit_flake_selection",
                {"selected_by_role": {"channel": ["flake_1"]}, "assessments": [],
                 "reasons": "r"})
    assert r.data["rule"] == "not_vetoed"


def test_user_instruction_is_appended_after_next_tool_result(services):
    s = services
    s.store.add_instruction("use chip_002 instead", project_id=s.project_id)
    # a subagent gets a heads-up once, without consuming it ...
    r, extra = call(s, "eyes", "list_samples", {"kind": "chip"})
    assert r.status == "ok" and "use chip_002 instead" in extra
    assert call(s, "eyes", "list_samples", {"kind": "chip"})[1] is None
    assert len(s.store.pending_instructions()) == 1
    # ... the orchestrator gets it for real
    r, extra = call(s, "orchestrator", "list_samples", {"kind": "chip"})
    assert "use chip_002 instead" in extra
    assert s.store.pending_instructions() == []


def test_hardware_tool_runs_job_end_to_end(services):
    s = services
    r, _ = call(s, "hands", "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert r.status == "pending"
    r, _ = call(s, "hands", "wait_job", {"job_id": r.data["job_id"], "timeout_s": 10})
    assert r.status == "ok"


def test_all_emitted_event_kinds_are_documented(services):
    s = services
    s.lab.world.faults.next_placement = {"dx": 2.0}
    s.store.add_instruction("go", project_id=s.project_id)
    asyncio.run(s.driver.wait_idle(timeout=20))
    kinds = {e.kind for e in s.store.events_after(0, limit=100_000)}
    assert kinds <= set(EVENT_KINDS), kinds - set(EVENT_KINDS)
