"""The two stage-1 demo requests, scripted, end to end on the testbench."""

import asyncio

import pytest

from qumus2.app import build_services
from qumus2.runtime.events import Event


def _run(scenario: str, seed: int, db: str = ":memory:"):
    s = build_services(db, scenario=scenario, seed=seed)
    asyncio.run(s.runtime.send("go"))
    events: list[Event] = s.store.events_after(0, limit=100000)
    return s, events


def _agents(events):
    return {e.agent for e in events if e.kind in ("tool.call", "agent.message", "agent.done")}


@pytest.mark.parametrize("seed", [1, 7])
def test_give_me_a_graphene_flake(seed):
    s, events = _run("graphene_flake", seed)
    calls = [(a, t, r.status) for a, t, r in s.runtime.results]
    assert ("hands", "exfoliate_chips", "pending") in calls
    waits = [st for a, t, st in calls if t == "wait_job"]
    assert waits and all(st in ("ok", "corrected") for st in waits)
    flakes = s.store.list_samples(kind="flake")
    assert any(f["status"] == "catalogued" and f["props"]["material"] == "Gr" for f in flakes)
    assert {"orchestrator", "advisor", "eyes", "hands"} <= _agents(events)
    assert not [e for e in events if e.kind == "tool.denied"]
    # progress is marked by code as delegations start and return
    proj = s.store.get_project(s.project_id)
    assert [x["status"] for x in proj["plan"]] == ["done"] * 5 and proj["plan_revision"] == 1
    assert any(e.kind == "plan.step" and e.payload["status"] == "running" for e in events)


# seed 1 now draws five transfer-stage placements of the electrode chip over the
# 0.5 mm tolerance in a row and parks, as the placeholder error model allows now
# and then (2026-10-22: the grip check's camera look moved to the stamp_over
# steps, so the random stream shifted); the scenario is not about that
# seed 2 now draws a miss of the 70 % first-flake pick-up chance (the flake was
# under the contact; #211 shifted the random stream), and a plain stack has no
# re-press, so it parks; the scenario is not about that either
@pytest.mark.parametrize("seed", [3, 4])
def test_make_a_graphene_device(seed, tmp_path):
    # a file run store, so the testbench saves the microscope images the
    # picker must look at (require_optical_images)
    s, events = _run("graphene_device", seed, str(tmp_path / "qumus.db"))
    assert _agents(events) == {"orchestrator", "advisor", "eyes", "hands", "picker",
                               "designer"}
    status = {t: r.status for a, t, r in s.runtime.results}
    # the picker found nothing at first and asked for exfoliation, then selected
    assert status["report_selection_blocked"] == "ok"
    assert status["submit_flake_selection"] == "ok"
    assert status["submit_design"] == "ok"
    state = s.store.get_project(s.project_id)["state"]
    assert state["design_accepted"] and len(state["selected_flakes"]) == 3
    roles = state["selected_by_role"]
    mats = {r: s.store.get_sample(ids[0])["props"]["material"] for r, ids in roles.items()}
    assert mats == {"top_hbn": "hBN", "channel": "Gr", "bottom_hbn": "hBN"}
    views = [r for a, t, r in s.runtime.results if t == "view_flake_images"]
    assert len(views) >= 3 and all(r.status == "ok" and len(r.images) >= 2 for r in views)
    assert set(state["selected_flakes"]) <= {r.data["flake_id"] for r in views}
    # stacking runs with exactly the code-checked selection
    stack_calls = [e for e in events if e.kind == "tool.call"
                   and e.payload["tool"] == "stack_device"]
    assert stack_calls and not [e for e in events if e.kind == "tool.denied"]
    stack = next(r for a, t, r in s.runtime.results if t == "stack_device")
    assert stack.status == "pending", stack.summary
    devices = s.store.list_samples(kind="device")
    assert len(devices) == 1 and devices[0]["status"] == "stacked"
    last = [e for e in events if e.kind == "agent.message"][-1]
    assert last.agent == "orchestrator" and "device is stacked" in last.payload["text"]
    plan = s.store.get_project(s.project_id)["plan"]
    assert [x["status"] for x in plan] == ["done"] * 5
    assert [x["status"] for x in s.store.plan_history(s.project_id)[-1]["plan"]] == ["done"] * 5


def test_a_parked_exfoliation_stops_the_scripted_demo_cleanly(tmp_path):
    s = build_services(str(tmp_path / "qumus.db"), scenario="graphene_device", seed=1)
    s.lab.faults.inject("pick_fail_next", {"count": 50})  # exfoliation cannot pick
    asyncio.run(s.runtime.send("go"))  # used to raise KeyError 'job_id'
    events = s.store.events_after(0, limit=100000)
    helps = [e for e in events if e.kind == "notification"
             and e.payload["level"] == "needs_human"]
    assert helps and "wait_job returned needs_attention" in helps[-1].payload["text"]
    assert helps[-1].run_id  # the parked job is named
    last = [e for e in events if e.kind == "agent.message"][-1]
    assert last.agent == "orchestrator" and last.payload["text"].startswith("I have to stop")
    plan = {x["id"]: x["status"] for x in s.store.get_project(s.project_id)["plan"]}
    assert plan["s1"] == "done" and plan["s3"] == "pending"  # left open, not done
