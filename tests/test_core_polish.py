"""What the agents see (2026-10-22 pre-acceptance audit): compact job results
with the ledger outcome and correction lines, only photographed candidates to
assess, device-plan thickness ranges mapped to the ledger's labels, one
hardware job of a flow at a time, a lean project context."""

from __future__ import annotations

import asyncio
import json

from qumus2.core.materials import labels_for_nm, labels_from_text
from qumus2.flows.flow import get_flow


def call(s, agent, tool, args):
    return asyncio.run(s.gateway.call(agent, tool, args))[0]


def _plan(layers):
    return {"device_name": "test device", "layer_stack": layers,
            "contact_scheme": "pre-patterned bottom electrodes", "gate_scheme": "Si back gate",
            "channel": "Hall bar 5x10 um min", "evidence": "test",
            "target_chip_id": "chip_e01", "bridge_requirement": {
                "contact_overlap_um": 1, "channel_width_um": 2,
                "placement_margin_um": 0.5, "rationale": "synthetic test fixture"}}


def test_thickness_ranges_map_to_the_ledger_bins():
    assert labels_for_nm("hBN", 10, 30) == ["10-20", "20-30"]
    assert labels_for_nm("hBN", 20, 40) == ["20-30", "30-40"]
    assert labels_for_nm("hBN", 45, 60) == [">40"]
    assert labels_for_nm("hBN", 25, 25) == ["20-30"]
    assert labels_for_nm("Gr", 0.3, 1.1) == ["monolayer", "bilayer", "trilayer"]
    assert labels_from_text("hBN", "10-40 nm") == ["10-20", "20-30", "30-40"]
    assert labels_from_text("Gr", "1-3 layers") == ["monolayer", "bilayer", "trilayer"]
    assert labels_from_text("Gr", "1L") == ["monolayer"]
    assert labels_from_text("Gr", "thin") == []


def test_device_plan_reads_every_way_of_saying_the_thickness(services):
    # the real run E3 (2026-10-22) wrote thickness_nm and thickness_label, fields the
    # model did not have, and the plan was accepted with no thickness target at all
    r = call(services, "advisor", "submit_device_plan", _plan([
        {"material": "hBN", "role": "top_hbn", "thickness_nm": [10, 30], "min_area_um2": 100},
        {"material": "Gr", "role": "channel", "thickness_label": "monolayer"},
        {"material": "hBN", "role": "bottom_hbn", "thickness_nm": [20, 40]},
        {"material": "Gr", "role": "gate", "thickness": "10-20 layers"}]))
    assert r.status == "ok", r.summary
    labels = {x["role"]: x["thickness_labels"] for x in r.data["flake_roles"]}
    assert labels == {"top_hbn": ["10-20", "20-30"], "channel": ["monolayer"],
                      "bottom_hbn": ["20-30", "30-40"], "gate": ["manylayer"]}
    state = services.store.get_project(services.project_id)["state"]
    assert all(x["thickness_labels"] for x in state["device_plan"]["flake_roles"])


def test_device_plan_without_a_thickness_target_is_refused(services):
    r = call(services, "advisor", "submit_device_plan", _plan([
        {"material": "hBN", "role": "top_hbn", "thickness": "thin", "min_area_um2": 100},
        {"material": "Gr", "role": "channel", "thickness_labels": ["monolayer"]}]))
    assert r.status == "error" and "top_hbn" in r.summary and "10-20" in r.summary
    assert services.store.get_project(services.project_id)["state"].get("device_plan") is None
    # saying "any" on purpose is allowed and means no filter
    r = call(services, "advisor", "submit_device_plan", _plan([
        {"material": "hBN", "role": "top_hbn", "thickness_labels": ["any"]},
        {"material": "Gr", "role": "channel", "thickness_labels": ["monolayer"]}]))
    assert r.status == "ok", r.summary
    assert [x["thickness_labels"] for x in r.data["flake_roles"]] == [[], ["monolayer"]]


def test_orchestrator_may_read_the_device_plan(services):
    assert call(services, "orchestrator", "get_device_plan", {}).status == "ok"


def test_job_result_is_compact_and_carries_the_ledger_outcome(services):
    s = services
    s.lab.world.faults.next_placement = {"dx": 2.0, "dy": -1.0, "drot": 6.0}
    r = call(s, "hands", "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    st = call(s, "hands", "wait_job", {"job_id": r.data["job_id"], "timeout_s": 30})
    assert st.status == "corrected", st.summary
    d = st.data
    assert d["outcome"] == {"chip": "chip_001", "status": s.store.get_sample("chip_001")["status"],
                            "location": "transfer_stage", "material": None}
    assert d["corrections"] and all("attempt" in line for line in d["corrections"])
    assert "expected" not in d["steps"][0] and {"seq", "name", "status"} <= set(d["steps"][0])
    assert d["result"]["steps_ok"] >= 1 and d["result"]["steps_to_look_at"]
    assert all(o["corrections"] for o in d["result"]["steps_to_look_at"])
    assert len(json.dumps(d)) < 6000
    full = call(s, "hands", "get_job_status", {"job_id": r.data["job_id"], "full": True})
    assert "expected" in full.data["steps"][0] and isinstance(full.data["result"], list)
    assert full.data["outcome"] == d["outcome"]


def test_inspect_bench_keeps_every_detection(services):
    r = call(services, "eyes", "inspect_bench", {})
    assert r.status == "ok" and isinstance(r.data["result"], list)
    assert r.data["result"][0]["observations"][0]["measurements"]


def test_a_second_job_of_a_flow_is_queued_behind_the_first(services):
    s = services
    flow = get_flow("place_chip_on_transfer_stage")
    assert s.store.acquire_locks(flow.devices, "someone_else")  # the first job stays queued
    try:
        first = call(s, "hands", "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
        assert first.status == "pending" and first.data["queued_behind"] is None
        second = call(s, "hands", "place_chip_on_transfer_stage", {"chip_id": "chip_002"})
        assert second.status == "pending", second.summary
        assert second.data["queued_behind"] == first.data["job_id"]
        assert f"queued behind {first.data['job_id']}" in second.summary
    finally:
        s.store.release_locks("someone_else")
    for r in (first, second):
        st = call(s, "hands", "wait_job", {"job_id": r.data["job_id"], "timeout_s": 30})
        assert st.status in ("ok", "corrected", "needs_attention"), st.summary


def test_two_exfoliations_submitted_back_to_back_both_succeed(tmp_path):
    from qumus2.app import build_services

    s = build_services(str(tmp_path / "qumus.db"), scenario="two_exfoliations", seed=1)
    asyncio.run(s.runtime.send("go"))
    runs = [r for r in s.store.list_runs() if r["flow"] == "exfoliate_chips"]
    assert [r["status"] for r in runs] == ["succeeded", "succeeded"], \
        [(r["status"], r.get("message")) for r in runs]
    slots = [r["params"]["store_slots"] for r in runs]
    assert not set(slots[0]) & set(slots[1]), slots
    chips = [r["params"]["chip_ids"] for r in runs]
    assert not set(chips[0]) & set(chips[1]), chips  # today both re-exfoliate chip_001
    waits = [r for a, t, r in s.runtime.results if t == "wait_job"]
    assert [w.status for w in waits] == ["ok", "ok"] or all(
        w.status in ("ok", "corrected") for w in waits)
    assert any(r.data.get("queued_behind") for a, t, r in s.runtime.results
               if t == "exfoliate_chips")


def test_advisor_first_scenario_refuses_then_passes(tmp_path):
    """F8 regression: the scripted orchestrator sends the hands to exfoliate
    before the advisor; the denial names the rule; after get_experience the
    same delegation passes and the job succeeds."""
    from qumus2.app import build_services

    s = build_services(str(tmp_path / "qumus.db"), scenario="advisor_first", seed=1)
    asyncio.run(s.runtime.send("go"))
    evs = s.store.events_after(0, limit=1_000_000)
    denied = [e.payload for e in evs if e.kind == "tool.denied"]
    assert [(d["tool"], d["rule"]) for d in denied] == [("delegate", "advisor_first")], denied
    assert "advisor" in denied[0]["reason"] and "Gr" in denied[0]["reason"]
    assert [e.payload["to"] for e in evs if e.kind == "agent.delegate"] == \
        ["eyes", "advisor", "hands"]
    said = [e.payload["text"] for e in evs if e.kind == "agent.message"]
    assert any(t.startswith("Delegation to the hands: denied") for t in said)
    runs = [r for r in s.store.list_runs() if r["flow"] == "exfoliate_chips"]
    assert [r["status"] for r in runs] == ["succeeded"], runs


def test_picker_sees_only_photographed_candidates(services, tmp_path, monkeypatch, pickup_inspection):
    s = services
    monkeypatch.setattr("qumus2.agents.tools.picking.require_images", lambda: True)
    imgs = {}
    for mag in ("20x", "100x"):
        imgs[mag] = str(tmp_path / f"shot_{mag}.png")
        (tmp_path / f"shot_{mag}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    s.store.upsert_sample("f_shot", "flake", location="chip_001", status="catalogued",
                          props={"material": "Gr", "thickness_label": "monolayer",
                                 "area_um2": 900.0, "images": imgs, "stage_xy": [1, 2],
                                 "rel_corner_mm": [0.1, 0.2]})
    s.store.upsert_sample("f_10x", "flake", location="chip_001", status="candidate",
                          props={"material": "Gr", "thickness_label": "monolayer",
                                 "area_um2": 950.0})
    pickup_inspection(s, ["f_shot", "f_10x"])
    r = call(s, "advisor", "submit_device_plan", _plan([
        {"material": "Gr", "role": "channel", "thickness_labels": ["monolayer"]},
        {"material": "Gr", "role": "gate", "thickness_labels": ["manylayer"]}]))
    assert r.status == "ok", r.summary
    r = call(s, "picker", "get_flake_candidates", {})
    assert [f["id"] for f in r.data["candidates_by_role"]["channel"]] == ["f_shot"]
    assert "stage_xy" not in r.data["candidates_by_role"]["channel"][0]
    assert [f["id"] for f in r.data["unphotographed_by_role"]["channel"]] == ["f_10x"]
    assert "1 more at 10X only" in r.summary
    # the 10X-only flake need not be assessed; an unassessed photographed one is refused
    call(s, "picker", "view_flake_images", {"flake_id": "f_shot"})
    r = call(s, "picker", "report_selection_blocked", {
        "role": "gate", "action": "exfoliate_and_search_one_chip", "reason": "none"})
    assert r.status == "ok"
    sel = {"selected_by_role": {"channel": ["f_shot"], "gate": []}, "assessments": [],
           "reasons": "test"}
    r = call(s, "picker", "submit_flake_selection", sel)
    assert r.status == "error" and "missing: ['f_shot']" in r.summary \
        and "f_10x" not in r.summary


def test_project_context_leaves_the_bulky_state_out(services):
    s = services
    s.store.update_project_state(s.project_id, selection_assessments=[{"x": 1}] * 50,
                                 agents_last={"hands": {"message": "hi"}}, selected_flakes=["a"])
    r = call(s, "orchestrator", "get_project_context", {})
    assert "selection_assessments" not in r.data["state"]
    assert r.data["state"]["selection_assessments_count"] == 50
    assert r.data["state"]["selected_flakes"] == ["a"]
    assert s.store.get_project(s.project_id)["state"]["selection_assessments"]


def test_help_is_refused_right_after_a_rule_denial(services):
    s = services
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1})
    assert r.status == "denied" and r.data["rule"] == "experience_checked"
    r = call(s, "hands", "request_human_help", {"reason": "exfoliation was refused"})
    assert r.status == "error" and "experience_checked" in r.summary
    assert not [e for e in s.store.events_after(0, limit=100_000)
                if e.kind == "notification" and e.payload.get("level") == "needs_human"]
    # after another tool answer the hands may ask again; the recovery budget
    # being spent is the one denial after which a person is the next step
    call(s, "hands", "list_samples", {"kind": "chip"})
    assert call(s, "hands", "request_human_help", {"reason": "really stuck"}).status == "ok"
    s.store.update_project_state(s.project_id, recovery={"total": 20, "jobs": {}})
    r = call(s, "hands", "retry_job", {"job_id": "run_x", "reason": "x"})
    assert r.status == "denied" and r.data["rule"] == "recovery_budget"
    assert call(s, "hands", "request_human_help", {"reason": "budget spent"}).status == "ok"


def test_each_flow_marks_its_own_plan_step(services):
    s = services
    s.store.set_plan(s.project_id, [
        {"id": "s1", "title": "Exfoliate Gr onto one bare chip", "owner": "hands",
         "status": "pending"},
        {"id": "s2", "title": "Search the chip under the microscope", "owner": "hands",
         "status": "pending"},
        {"id": "s3", "title": "Select flakes", "owner": "picker", "status": "pending"}],
        author="orchestrator")
    ex = s.store.create_run("exfoliate_chips", {"material": "Gr"}, project_id=s.project_id)
    se = s.store.create_run("search_chip", {"chip_id": "chip_001"}, project_id=s.project_id)
    s.store.set_run_status(ex, "succeeded")
    s.store.set_run_status(se, "parked", error={"message": "corner not found"})
    g = s.gateway
    assert g.delegation_started("k", "hands", "s1: exfoliate Gr, then search it") == "s1"
    g._open["k"].calls += [
        {"tool": "wait_job", "status": "ok", "summary": "", "data": {"job_id": ex}},
        {"tool": "wait_job", "status": "needs_attention", "summary": "",
         "data": {"job_id": se}}]
    facts = g.delegation_finished("k")
    plan = {x["id"]: x["status"] for x in s.store.get_project(s.project_id)["plan"]}
    assert plan == {"s1": "done", "s2": "failed", "s3": "pending"}, plan
    assert "Plan step s2 is now failed" in facts and "Plan step s1 is now done" in facts
    assert "eyes must inspect" in facts
