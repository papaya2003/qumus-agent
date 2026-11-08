"""The ported per-agent tools: device plan, flake picking and design checks,
the hands recovery menu and its budget (spec 4.3, 6.3, 7.1)."""

from __future__ import annotations

import asyncio

import pytest

GRAPHENE_QH = {
    "device_name": "graphene quantum Hall device, edge contacts",
    "layer_stack": [
        {"role": "top_hbn", "material": "hBN", "thickness_labels": ["10-20", "20-30"]},
        {"role": "channel", "material": "graphene", "thickness_labels": ["monolayer"]},
        {"role": "bottom_hbn", "material": "hBN", "thickness_labels": ["30-40"]},
        {"role": "graphite_gate", "material": "Gr", "thickness_labels": ["manylayer"]},
        {"role": "contact_metal", "material": "Cr/Au", "flake": False},
    ],
    "contact_scheme": "edge contacts, 5 nm Cr / 60 nm Au",
    "gate_scheme": "bottom graphite gate",
    "channel": "Hall bar 10x20 um preferred, 5x10 um min",
    "evidence": "v1 graphene-qh-device skill defaults",
    "target_chip_id": "chip_002",  # unpatterned substrate for the new edge-contact layout
}
FLAKES = {"f_top": ("hBN", "10-20"), "f_ch": ("Gr", "monolayer"),
          "f_bot": ("hBN", "30-40"), "f_gate": ("Gr", "manylayer"),
          "f_ch2": ("Gr", "monolayer")}


def call(s, agent, tool, args):
    return asyncio.run(s.gateway.call(agent, tool, args))[0]


def assessment(fid, **kw):
    base = {"flake_id": fid, "feasible": True, "geometry_fitness": 80,
            "visible_cleanliness": 80, "continuity_uniformity": 80, "evidence_confidence": 70,
            "inventory_tiebreaker": 50, "overall_score": 78, "reason": "clean",
            "surface_residue": "clean", "residue_evidence": "Synthetic clean optical fixture"}
    if fid in ("f_top", "f_bot"):
        base["hbn_color_judgement"] = {"relative_body_color": "blue",
                                       "background_comparison": "distinct",
                                       "thick_edge_visible": False,
                                       "classification": "usable_color_family",
                                       "evidence": "blue body"}
    return {**base, **kw}


@pytest.fixture
def planned(services, tmp_path, monkeypatch):
    s = services
    monkeypatch.setattr("qumus2.agents.tools.picking.require_images", lambda: True)
    geometry_job = s.store.create_run("search_chip", {}, project_id=s.project_id)
    s.store.set_run_status(geometry_job, "succeeded")
    for fid, (mat, label) in FLAKES.items():
        imgs = {}
        for mag in ("20x", "100x"):
            imgs[mag] = str(tmp_path / f"{fid}_{mag}.png")
            (tmp_path / f"{fid}_{mag}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        s.store.upsert_sample(fid, "flake", location="chip_001", status="catalogued",
                              props={"material": mat, "thickness_label": label,
                                     "area_um2": 900.0, "images": imgs,
                                     "pickup_assessment": {"flake_id": fid, "source_chip_id": "chip_001",
                                         "images": [imgs["20x"]], "job_id": geometry_job, "clear": True},
                                     "optical_geometry": {"outline_mm": [[-.05,-.05],[.05,-.05],[.05,.05],[-.05,.05]] if fid == "f_top" else [[-.01,-.01],[.01,-.01],[.01,.01],[-.01,.01]],
                                         "units": "mm", "frame": "flake_center_chip_axes", "source": "simulated_optical_outline",
                                         "source_chip_id": "chip_001", "image": imgs["20x"], "job_id": geometry_job}})
    r = call(s, "advisor", "submit_device_plan", GRAPHENE_QH)
    assert r.status == "ok", r.summary
    assert [x["role"] for x in r.data["flake_roles"]] == ["top_hbn", "channel", "bottom_hbn",
                                                         "graphite_gate"]
    assert r.data["is_new_recipe"] is True
    return s


def selection(**override):
    sel = {"top_hbn": ["f_top"], "channel": ["f_ch"], "bottom_hbn": ["f_bot"],
           "graphite_gate": ["f_gate"]}
    sel.update(override)
    return {"selected_by_role": sel, "reasons": "best set",
            "assessments": [assessment(f) for f in FLAKES]}


def view_all(s):
    for f in FLAKES:
        call(s, "picker", "view_flake_images", {"flake_id": f})


def test_incomplete_device_plan_is_rejected(services):
    bad = {**GRAPHENE_QH, "contact_scheme": "", "gate_scheme": "",
           "layer_stack": [{"role": "channel", "material": "graphene transistor"}]}
    r = call(services, "advisor", "submit_device_plan", bad)
    assert r.status == "error" and "at least 2 layers" in r.summary
    assert "gate_scheme" in r.summary


def test_selection_checks(planned):
    s = planned
    r = call(s, "picker", "get_flake_candidates", {})
    assert {f["id"] for f in r.data["candidates_by_role"]["channel"]} == {"f_ch", "f_ch2"}
    # images not looked at yet
    r = call(s, "picker", "submit_flake_selection", selection())
    assert r.status == "error" and "view_flake_images" in r.summary
    view_all(s)
    # wrong count, wrong material, reuse
    r = call(s, "picker", "submit_flake_selection", selection(channel=["f_ch", "f_ch2"]))
    assert "exactly 1" in r.summary
    r = call(s, "picker", "submit_flake_selection", selection(channel=["f_gate"]))
    assert "not a candidate for role 'channel'" in r.summary and "used twice" in r.summary
    # hBN too thick by colour
    args = selection()
    args["assessments"][0]["hbn_color_judgement"]["classification"] = "too_thick"
    assert "too_thick" in call(s, "picker", "submit_flake_selection", args).summary
    # missing audit of a rejected candidate
    args = selection()
    args["assessments"] = args["assessments"][:-1]
    assert "f_ch2" in call(s, "picker", "submit_flake_selection", args).summary
    r = call(s, "picker", "submit_flake_selection", selection())
    assert r.status == "ok", r.summary
    st = s.store.get_project(s.project_id)["state"]
    assert set(st["selected_flakes"]) == {"f_top", "f_ch", "f_bot", "f_gate"}


def test_veto_of_a_selected_flake_resets_selection(planned):
    s = planned
    view_all(s)
    assert call(s, "picker", "submit_flake_selection", selection()).status == "ok"
    r = call(s, "orchestrator", "exclude_flake", {"flake_id": "f_ch", "reason": "user: dirty"})
    assert "redone" in r.summary
    st = s.store.get_project(s.project_id)["state"]
    assert st["selected_flakes"] == [] and st["design_accepted"] is False
    r = call(s, "picker", "submit_flake_selection", selection())
    assert r.status == "denied" and r.data["rule"] == "not_vetoed"
    r = call(s, "picker", "submit_flake_selection", selection(channel=["f_ch2"]))
    assert r.status == "ok", r.summary


def good_layout(**kw):
    shapes = [
        {"layer": "top_hbn", "x_um": -5, "y_um": -5, "w_um": 40, "h_um": 30},
        {"layer": "bottom_hbn", "x_um": -5, "y_um": -5, "w_um": 40, "h_um": 30},
        {"layer": "channel", "x_um": 0, "y_um": 0, "w_um": 20, "h_um": 10},
        {"layer": "graphite_gate", "x_um": -2, "y_um": -2, "w_um": 30, "h_um": 20},
        {"layer": "contact_metal", "x_um": 20, "y_um": 0, "w_um": 10, "h_um": 10},
        {"layer": "gate_metal", "x_um": -20, "y_um": 0, "w_um": 5, "h_um": 5},
    ]
    return {"flake_ids": ["f_top", "f_ch", "f_bot", "f_gate"], "shapes": shapes, **kw}


def test_design_checks_and_stacking_rules(planned):
    s = planned
    view_all(s)
    assert call(s, "picker", "submit_flake_selection", selection()).status == "ok"
    r = call(s, "hands", "stack_device", {"flake_ids": ["f_top", "f_ch", "f_bot", "f_gate"],
                                          "target_chip_id": "chip_002"})
    assert r.status == "denied" and r.data["rule"] == "stack_authorized"
    bad = good_layout()
    bad["shapes"][2] = {"layer": "channel", "x_um": 0, "y_um": 0, "w_um": 4, "h_um": 8}
    bad["shapes"][5] = {"layer": "gate_metal", "x_um": 31, "y_um": 0, "w_um": 2, "h_um": 2}
    r = call(s, "designer", "submit_design", bad)
    assert r.status == "error" and "below the minimum" in r.summary and "3.0 um" in r.summary
    r = call(s, "designer", "submit_design", good_layout())
    assert r.status == "ok", r.summary
    r = call(s, "hands", "stack_device", {"flake_ids": ["f_top", "f_ch"],
                                          "target_chip_id": "chip_002"})
    assert r.status == "denied" and r.data["rule"] == "stack_authorized"


def test_design_infeasible_hands_back_and_blocks_same_set(planned):
    s = planned
    view_all(s)
    call(s, "picker", "submit_flake_selection", selection())
    r = call(s, "designer", "report_design_infeasible",
             {"flake_ids": ["f_top", "f_ch", "f_bot", "f_gate"], "failed_roles": ["channel"],
              "reason": "no 5x10 region at any offset"})
    assert r.status == "ok"
    r = call(s, "picker", "submit_flake_selection", selection())
    assert "proven impossible" in r.summary
    assert call(s, "picker", "submit_flake_selection",
                selection(channel=["f_ch2"])).status == "ok"


def test_blocked_report_names_the_right_action(planned):
    s = planned
    # Two channel candidates cannot fill a genuine three-flake requirement.
    device_plan = s.store.get_project(s.project_id)["state"]["device_plan"]
    next(r for r in device_plan["flake_roles"] if r["role"] == "channel")["count"] = 3
    s.store.update_project_state(s.project_id, device_plan=device_plan)
    r = call(s, "picker", "report_selection_blocked",
             {"role": "channel", "action": "exfoliate_and_search_one_chip",
              "reason": "too few"})
    assert r.status == "ok"
    r = call(s, "picker", "report_selection_blocked",
             {"role": "channel", "action": "relink_or_capture_optical_images", "reason": "x"})
    assert r.status == "error"
    s.store.upsert_sample("f_ch2", "flake", props={"images": {}})
    r = call(s, "picker", "report_selection_blocked",
             {"role": "channel", "action": "exfoliate_and_search_one_chip", "reason": "x"})
    assert r.status == "error" and "relink_or_capture" in r.summary
    r = call(s, "picker", "report_selection_blocked",
             {"role": "channel", "action": "relink_or_capture_optical_images", "reason": "x"})
    assert r.status == "ok"


def park_a_placement(s):
    s.lab.faults.inject("pick_fail_next", {"count": 20})  # pick keeps failing -> parked
    r = call(s, "hands", "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    st = call(s, "hands", "wait_job", {"job_id": r.data["job_id"], "timeout_s": 30})
    assert st.status == "needs_attention", st.summary
    return r.data["job_id"]


def test_recovery_menu_and_budget(services, monkeypatch):
    s = services
    monkeypatch.setattr("qumus2.agents.tools.recovery.budget",
                        lambda: {"per_job": 1, "per_project": 5})
    job = park_a_placement(s)
    r = call(s, "hands", "reverify_job", {"job_id": job, "reason": "photo unclear"})
    assert r.status in ("pending", "needs_attention", "ok", "corrected"), r.summary
    s.jobs.wait(job, 30)
    r = call(s, "hands", "retry_job", {"job_id": job, "reason": "re-place"})
    assert r.status == "denied" and r.data["rule"] == "recovery_budget"
    assert "request_human_help" in r.summary
    counts = s.store.get_project(s.project_id)["state"]["recovery"]
    assert counts["jobs"][job] == 1 and counts["total"] == 1


def test_replace_chip_counts_against_the_original_job(services, monkeypatch):
    s = services
    monkeypatch.setattr("qumus2.agents.tools.recovery.budget",
                        lambda: {"per_job": 1, "per_project": 5})
    job = park_a_placement(s)
    r = call(s, "hands", "skip_chip", {"chip_id": "chip_001", "reason": "cracked"})
    assert s.store.get_sample("chip_001")["status"] == "skipped"
    r = call(s, "hands", "replace_chip", {"job_id": job, "new_chip_id": "chip_001",
                                          "reason": "x"})
    assert r.status == "error" and "skipped" in r.summary
    st = s.store.get_project(s.project_id)["state"]["recovery"]
    assert st["total"] == 1


def test_retry_of_a_running_job_is_refused(services):
    s = services
    r = call(s, "hands", "retry_job", {"job_id": "run_nope", "reason": "x"})
    assert r.status == "error" and "unknown job" in r.summary


def test_query_run_records_reports_success_rates(services):
    s = services
    call(s, "hands", "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    for r in s.store.list_runs():
        s.jobs.wait(r["id"], 30)
    r = call(s, "orchestrator", "query_run_records", {"flow": "place_chip_on_transfer_stage"})
    assert r.status == "ok" and r.data["recipes"][0]["runs"] == 1
    r = call(s, "advisor", "get_lab_setup", {})
    assert "exfoliate_chips" in r.data["flows"]
    from qumus2.agents.tools.hardware import ExfoliateArgs
    from qumus2.records.outcomes import recipe_keys
    defaults = r.data["exfoliation_recipe_defaults"]
    assert defaults["source"] == "registered_tool_defaults" and defaults["validated"] is False
    assert defaults["values"] == {key: ExfoliateArgs.model_fields[key].default for key in recipe_keys("exfoliate_chips")}
    assert "not confirmed experience" in defaults["note"]


def test_prompts_name_only_tools_the_agent_has():
    """A prompt that tells an agent to call a tool it cannot use wastes turns."""
    import re

    from qumus2.agents.roster import ALL_AGENTS, ORCHESTRATOR, load_roster
    from qumus2.agents.tooling import default_registry

    reg = default_registry()
    names = {t.name for t in reg.all() if t.model_visible}
    roster = load_roster()
    for agent in ALL_AGENTS:
        mine = {t.name for t in reg.for_agent(agent)}
        mentioned = set(re.findall(r"(?<![.\w])[a-z]+(?:_[a-z0-9]+)+\b", roster[agent].prompt)) & names
        assert mentioned <= mine, (agent, sorted(mentioned - mine))
        if agent == ORCHESTRATOR:  # it is told its whole tool set, to not try others
            assert mentioned == mine, sorted(mine - mentioned)
        assert "Skeleton prompt" not in roster[agent].prompt


def test_update_plan_numbers_missing_step_ids(services):
    s = services
    r = call(s, "orchestrator", "update_plan", {"note": "x", "plan": [
        {"title": "a", "owner": "eyes"}, {"id": "s1", "title": "b", "owner": "hands"},
        {"title": "c", "owner": "orchestrator"}]})
    assert r.status == "ok", r.summary
    ids = [st["id"] for st in s.store.get_project(s.project_id)["plan"]]
    assert ids == ["s2", "s1", "s3"]


def test_thickness_labels_are_normalised_or_refused(services):
    plan = {**GRAPHENE_QH, "layer_stack": [
        {**GRAPHENE_QH["layer_stack"][0], "thickness_labels": ["10–20 nm"]},
        {**GRAPHENE_QH["layer_stack"][1], "thickness_labels": ["1L"]},
        *GRAPHENE_QH["layer_stack"][2:]]}
    r = call(services, "advisor", "submit_device_plan", plan)
    assert r.status == "ok", r.summary
    roles = {x["role"]: x["thickness_labels"] for x in r.data["flake_roles"]}
    assert roles["top_hbn"] == ["10-20"] and roles["channel"] == ["monolayer"]
    bad = {**plan, "layer_stack": [{**plan["layer_stack"][0], "thickness_labels": ["thick"]},
                                   *plan["layer_stack"][1:]]}
    r = call(services, "advisor", "submit_device_plan", bad)
    assert r.status == "error" and "'thick'" in r.summary


def test_looser_plan_needs_the_designers_yes(planned):
    s = planned
    looser = {**GRAPHENE_QH, "layer_stack": [
        *GRAPHENE_QH["layer_stack"][:2],
        {**GRAPHENE_QH["layer_stack"][2], "thickness_labels": ["20-30", "30-40"]},
        *GRAPHENE_QH["layer_stack"][3:]]}
    r = call(s, "advisor", "submit_device_plan", looser)
    assert r.status == "error" and "bottom_hbn" in r.summary and "designer" in r.summary
    assert call(s, "advisor", "get_device_plan", {}).data["device_plan"]["flake_roles"][2][
        "thickness_labels"] == ["30-40"]  # still the accepted plan

    no = {"role": "bottom_hbn", "thickness_labels": ["20-30", "30-40"], "acceptable": False,
          "reason": "a thinner gate dielectric leaks"}
    assert call(s, "designer", "assess_relaxation", no).status == "ok"
    assert call(s, "advisor", "submit_device_plan", looser).status == "error"

    yes = {**no, "acceptable": True, "reason": "20 nm is still enough dielectric"}
    assert call(s, "designer", "assess_relaxation", yes).status == "ok"
    r = call(s, "advisor", "submit_device_plan", looser)
    assert r.status == "ok", r.summary
    notes = [e.payload["text"] for e in s.store.events_after(0, limit=10_000)
             if e.kind == "notification"]
    assert any(t.startswith("Requirement relaxed for bottom_hbn") for t in notes)

    # the yes covers exactly that: anything looser still needs the designer
    loosest = {**looser, "layer_stack": [
        *looser["layer_stack"][:2],
        {**looser["layer_stack"][2], "thickness_labels": ["10-20", "20-30", "30-40"]},
        *looser["layer_stack"][3:]]}
    assert call(s, "advisor", "submit_device_plan", loosest).status == "error"
    # a stricter plan is always fine
    assert call(s, "advisor", "submit_device_plan", GRAPHENE_QH).status == "ok"


def test_plan_rewrite_keeps_progress_of_the_same_steps(services):
    s = services
    plan = [{"id": "s1", "title": "Check the bench", "owner": "eyes"},
            {"title": "Place chip_001", "owner": "hands"}]
    call(s, "orchestrator", "update_plan", {"note": "start", "plan": plan})
    s.store.set_step_status(s.project_id, "s1", "done")
    s.store.set_step_status(s.project_id, "s2", "running")
    rewrite = [plan[0], {"title": "Place chip_002 instead", "owner": "hands"},
               {"title": "Report", "owner": "orchestrator"}]
    rejected = call(s, "orchestrator", "update_plan", {"note": "unbound target change", "expected_version": s.store.get_project(s.project_id)["plan_version"], "plan": rewrite})
    assert rejected.status == "error"  # an omitted ID must not redefine an active physical task
    steps = s.store.get_project(s.project_id)["plan"]
    assert [(x["id"], x["status"]) for x in steps] == [("s1", "done"), ("s2", "running")]
    result = call(s, "orchestrator", "update_plan", {"note": "Keep identities and append report", "expected_version": s.store.get_project(s.project_id)["plan_version"],
        "plan": [*plan, {"title": "Report", "owner": "orchestrator"}]})
    assert result.status == "ok"
    steps = s.store.get_project(s.project_id)["plan"]
    assert [(x["id"], x["status"]) for x in steps] == [("s1", "done"), ("s2", "running"), ("s3", "pending")]
    assert s.store.get_project(s.project_id)["plan_revision"] == 2


def test_update_plan_accepts_the_field_names_claude_used(services):
    """Every real run first wrote steps as {step, agent} and owner "user"."""
    r = call(services, "orchestrator", "update_plan", {"note": "start", "plan": [
        {"step": "Check the bench", "agent": "eyes"},
        {"step": "Look at the flakes", "agent": "user"}]})
    assert r.status == "ok", r.summary
    plan = services.store.get_project(services.project_id)["plan"]
    assert [(x["title"], x["owner"]) for x in plan] == [("Check the bench", "eyes"),
                                                        ("Look at the flakes", "human")]


def test_selection_keeps_the_residue_judgement(planned):
    """A good flake has a clean surface: residue is judged for selected flakes,
    heavy residue is refused, and the judgement stays on the flake."""
    s = planned
    view_all(s)
    args = selection()
    args["assessments"][1]["surface_residue"] = "heavy"  # f_ch, the channel
    r = call(s, "picker", "submit_flake_selection", args)
    assert r.status == "error" and "heavy tape residue" in r.summary
    args = selection()
    del args["assessments"][0]["surface_residue"]
    r = call(s, "picker", "submit_flake_selection", args)
    assert r.status == "error" and "surface_residue is required" in r.summary
    args = selection()
    args["assessments"][4]["surface_residue"] = "heavy"  # f_ch2, not selected
    args["assessments"][4]["residue_evidence"] = "film over the whole flake"
    r = call(s, "picker", "submit_flake_selection", args)
    assert r.status == "ok", r.summary
    assert s.store.get_sample("f_ch")["props"]["residue"]["level"] == "clean"
    assert s.store.get_sample("f_ch2")["props"]["residue"] == {
        "level": "heavy", "evidence": "film over the whole flake", "by": "picker",
        "project_id": s.project_id}
    # a heavy-residue flake is no longer a candidate
    r = call(s, "picker", "get_flake_candidates", {})
    assert "f_ch2" not in {f["id"] for f in r.data["candidates_by_role"]["channel"]}


def test_wait_job_reports_a_crash_the_job_service_recovered(make_services):
    s = make_services()
    s.lab.faults.inject("crash", {"after_calls": 3})
    r = call(s, "hands", "place_chip_on_transfer_stage", {"chip_id": "chip_001"})
    assert s.lab.faults.crashed.wait(30)
    st = call(s, "hands", "wait_job", {"job_id": r.data["job_id"], "timeout_s": 60})
    assert st.status == "corrected", st.summary
    assert "crashed" in st.summary and st.data["recovered_after_crash"]
    again = call(s, "hands", "get_job_status", {"job_id": r.data["job_id"]})
    assert again.data["recovered_after_crash"] == st.data["recovered_after_crash"]


def test_waiting_on_a_made_up_job_id_is_an_error_result(services):
    # a real orchestrator once waited on "job_9d50..." before submitting anything
    for tool, args in (("wait_job", {"job_id": "job_9d50b36d2093", "timeout_s": 1}),
                       ("get_job_status", {"job_id": "job_9d50b36d2093"})):
        r = call(services, "hands", tool, args)
        assert r.status == "error" and "no job" in r.summary
