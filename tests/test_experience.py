"""Self-evolution (spec 10): run records, the experience book, secure-then-
explore, the after-experiment review.

Experience comes only from real-lab runs. These tests run on the testbench, so
they check two things: (1) on a normal (sim) store nothing ever becomes
experience; (2) the whole pipeline works, in a sandbox store where sim runs are
allowed as evidence and every row is marked sandbox and hidden from the lab's
experience page.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from qumus2.records.budget import exploration_budget
from qumus2.records.experience import NO_REAL_EXPERIENCE, ExperienceBook, ExperienceError
from qumus2.records.outcomes import exfoliation_outcome
from qumus2.runtime.store import RunStore
from qumus2.ui.app import create_app

V1 = {"target_temp_c": 22, "dwell_s": 10, "num_massages": 1, "peel_speed": 500}


def call(s, agent, tool, args):
    return asyncio.run(s.gateway.call(agent, tool, args))[0]


def wait(s, result, timeout=120):
    st = s.jobs.wait(result.data["job_id"], timeout)
    assert st.status == "succeeded", (st.status, st.error, st.steps)
    return st


def exfoliate(s, **args):
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1, **V1, **args})
    assert r.status == "pending", r.summary
    return r


def exfoliate_and_search(s, **args):
    r = exfoliate(s, **args)
    wait(s, r)
    chip = s.store.get_run(r.data["job_id"])["params"]["chip_ids"][0]
    wait(s, call(s, "hands", "search_chip", {"chip_id": chip}), 300)
    return r.data["job_id"]


def person_says(s, text):
    return s.store.add_instruction(text, project_id=s.project_id)


@pytest.fixture
def sandbox(services):
    services.store.experience_sandbox = True
    return services


# ----- run store: bench and run meta -----------------------------------------

def test_store_records_its_bench_and_refuses_to_mix(tmp_path):
    path = tmp_path / "lab.db"
    store = RunStore(path)
    assert store.bench == "sim"
    rid = store.create_run("exfoliate_chips", {"material": "Gr"})
    store.update_run_meta(rid, purpose="secure")
    assert store.get_run(rid)["bench"] == "sim"
    assert store.get_run(rid)["meta"] == {"purpose": "secure"}
    store.close()
    with pytest.raises(ValueError, match="never mix"):
        RunStore(path, bench="real")
    assert RunStore(path).bench == "sim"
    assert RunStore(tmp_path / "real.db", bench="real").bench == "real"


def test_old_database_files_gain_the_new_run_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE run (id TEXT PRIMARY KEY, project_id TEXT, flow TEXT NOT NULL, "
                "params_json TEXT, status TEXT NOT NULL, created_at REAL, updated_at REAL, "
                "result_json TEXT, error_json TEXT, requested_by TEXT)")
    con.execute("INSERT INTO run (id, flow, params_json, status) VALUES "
                "('run_old', 'search_chip', '{}', 'succeeded')")
    con.commit()
    con.close()
    old = RunStore(path).get_run("run_old")
    assert old["bench"] == "sim" and old["meta"] == {}


# ----- what the lab starts with ----------------------------------------------

def test_seeded_knowledge_is_labelled_and_not_experience(services):
    book = ExperienceBook(services.store)
    summary = book.summary()
    assert summary["learned"] == 0 and summary["real_runs"] == 0
    assert summary["note"] == NO_REAL_EXPERIENCE
    by_id = {e["id"]: e for e in summary["experiences"]}
    prior = by_id["prior_v1_exfoliation_default_Gr"]
    assert prior["status"] == "trial" and prior["source"] == "prior"
    assert prior["label"] == "prior knowledge, not verified by v2 experiments"
    assert prior["verified_by_real_runs"] == 0 and "qumus_agent.py" in prior["detail"]["reference"]
    assert by_id["human_good_flake_criteria"]["status"] == "confirmed"
    base = book.baseline("graphene")
    assert base["recipe"] == V1 and base["enforced"] is False
    ExperienceBook(services.store)  # seeding is idempotent
    assert len(services.store.list_experiences()) == len(summary["experiences"])


# ----- testbench runs never become experience -----------------------------------

def test_sim_runs_are_never_evidence(services):
    s = services
    call(s, "advisor", "get_experience", {"material": "Gr"})
    rid = exfoliate_and_search(s, target_thickness_labels=["fewlayer", "manylayer"])
    r = call(s, "advisor", "review_runs", {})
    assert r.status == "ok" and r.data["runs"] == [] and "simulated" in r.summary
    r = call(s, "advisor", "propose_experience", {
        "topic": "exfoliation", "scope": {"material": "Gr"}, "claim": "22 C works for Gr.",
        "evidence_run_ids": [rid], "observation": "flakes found"})
    assert r.status == "error" and "testbench" in r.summary
    r = call(s, "advisor", "vote_experience", {
        "experience_id": "prior_v1_exfoliation_default_Gr", "run_id": rid,
        "verdict": "support", "note": "it worked"})
    assert r.status == "error" and "sim" in r.summary
    book = ExperienceBook(s.store)
    assert book.summary()["learned"] == 0
    assert all(e["source"] != "review" for e in s.store.list_experiences(sandbox=None))
    assert not book.auto_vote(s.store.get_run(rid))
    # the testbench still keeps complete records of what happened
    out = exfoliation_outcome(s.store, s.store.get_run(rid))
    assert out["complete"] and out["searched_chips"] == 1 and out["good_flakes"] is not None
    meta = s.store.get_run(rid)["meta"]
    assert meta["purpose"] == "secure" and meta["target"]["thickness_labels"]
    assert meta["conditions"]["humidity_pct"] is None
    assert "tape_uses_before" in meta["conditions"]


# ----- before an experiment ---------------------------------------------------

def test_exfoliation_waits_for_an_experience_lookup(services):
    s = services
    r = call(s, "hands", "exfoliate_chips", {"material": "hBN", "num_chips": 1})
    assert r.status == "denied" and r.data["rule"] == "experience_checked"
    r = call(s, "advisor", "get_experience", {"materials": ["hBN", "Gr"]})
    assert r.status == "ok" and "testbench" in r.summary
    hbn = r.data["materials"]["hBN"]
    assert hbn["baseline_recipe"]["experience_id"] == "prior_v1_exfoliation_default_hBN"
    assert hbn["exploration"]["bare_chips"] > 0
    r = call(s, "hands", "exfoliate_chips", {"material": "hBN", "num_chips": 1})
    assert r.status == "pending", r.summary


def _plan_needing(s, material, count, labels):
    s.store.update_project_state(s.project_id, device_plan={"flake_roles": [{
        "role": "channel", "material": material, "count": count,
        "thickness_labels": labels, "min_area_um2": 50}]})


def test_secure_first_explore_only_with_spare_chips(services):
    s = services
    call(s, "advisor", "get_experience", {"material": "Gr"})
    bare = exploration_budget(s.store, s.project_id, "Gr")["bare_chips"]
    # the task needs every bare chip: nothing to spare
    _plan_needing(s, "Gr", bare, ["monolayer"])
    b = exploration_budget(s.store, s.project_id, "Gr")
    assert b["task_chips_needed"] >= bare and b["spare_chips"] == 0
    explore = {"purpose": "explore", "changed": ["target_temp_c"], "target_temp_c": 50,
               "hypothesis": "warmer tape gives more monolayers"}
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1, **V1, **explore})
    assert r.status == "denied" and "spare" in r.summary and "Secure the task" in r.summary
    # a small task leaves spare chips (default 0.5 good flakes/chip, margin 1.5: 1 flake -> 3)
    _plan_needing(s, "Gr", 1, ["monolayer"])
    b = exploration_budget(s.store, s.project_id, "Gr")
    assert b["task_chips_needed"] == 3 and b["spare_chips"] == bare - 3
    for bad, why in [({"hypothesis": ""}, "hypothesis"),
                     ({"dwell_s": 30}, "exactly one"),
                     ({"num_chips": 3}, "at most 2"),
                     ({"changed": ["dwell_s"]}, "differs in")]:
        r = call(s, "hands", "exfoliate_chips",
                 {"material": "Gr", "num_chips": 1, **V1, **explore, **bad})
        assert r.status == "denied" and why in r.summary, (bad, r.summary)
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1, **V1, **explore})
    assert r.status == "pending", r.summary
    assert s.store.get_run(r.data["job_id"])["meta"]["purpose"] == "explore"
    wait(s, r)
    # a prior (unconfirmed) recipe is only a suggestion: secure runs may differ
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1,
                                             "target_temp_c": 70})
    assert r.status == "pending", r.summary


def test_a_confirmed_recipe_holds_the_task_chips(sandbox):
    s = sandbox
    call(s, "advisor", "get_experience", {"material": "Gr"})
    recipe = {**V1, "target_temp_c": 50}
    iid = person_says(s, "For graphite always use 50 C.")
    r = call(s, "orchestrator", "teach_experience", {
        "topic": "exfoliation", "scope": {"material": "Gr"}, "recipe": recipe,
        "claim": "Graphite: exfoliate at 50 C.", "instruction_id": iid})
    assert r.status == "ok", r.summary
    base = ExperienceBook(s.store).baseline("Gr")
    assert base["enforced"] and base["recipe"] == recipe
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1, **V1})
    assert r.status == "denied" and "confirmed best recipe" in r.summary
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1, **V1,
                                             "purpose": "user", "instruction_id": "ins_x"})
    assert r.status == "denied" and "instruction_id" in r.summary
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1, **V1,
                                             "purpose": "user", "instruction_id": iid})
    assert r.status == "pending", r.summary
    wait(s, r)
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1, **recipe})
    assert r.status == "pending", r.summary


# ----- after an experiment (sandbox: sim runs stand in for real ones) ----------

def test_review_votes_and_status_follow_real_results(sandbox):
    s = sandbox
    call(s, "advisor", "get_experience", {"material": "Gr"})
    labels = ["monolayer", "bilayer", "trilayer", "fewlayer", "manylayer"]
    first = exfoliate_and_search(s, target_thickness_labels=labels, target_min_area_um2=1,
                                 expected_good_per_chip=1)
    # the next exfoliation of this material waits for the review
    r = call(s, "hands", "exfoliate_chips", {"material": "Gr", "num_chips": 1, **V1})
    assert r.status == "denied" and r.data["rule"] == "runs_reviewed"
    r = call(s, "advisor", "review_runs", {"material": "Gr"})
    assert r.status == "ok", r.summary
    packets = {p["run_id"]: p for p in r.data["runs"]}
    out = packets[first]["outcome"]
    assert out["searched_chips"] == 1 and out["good_flakes"] >= 1
    assert packets[first]["expected_vs_actual"]["expected_good_per_chip"] == 1
    good = out["good_per_chip"]
    # the advisor writes it down; the cited run is its first support
    r = call(s, "advisor", "propose_experience", {
        "topic": "exfoliation", "scope": {"material": "Gr"}, "recipe": V1,
        "claim": "The v1 default recipe gives graphite flakes of every thickness.",
        "evidence_run_ids": [first], "observation": f"{good} good flake(s) per chip",
        "cause_guess": "a hypothesis only", "expected_good_per_chip": 1})
    assert r.status == "error" and "already experience prior_v1" in r.summary
    r = call(s, "advisor", "propose_experience", {
        "topic": "exfoliation", "scope": {"material": "Gr"}, "recipe": {"target_temp_c": 22},
        "claim": "At 22 C graphite gives at least one good flake per chip.",
        "evidence_run_ids": [first], "observation": f"{good} good flake(s) per chip",
        "expected_good_per_chip": 1})
    assert r.status == "ok" and r.data["status"] == "trial", r.summary
    eid = r.data["experience_id"]
    assert call(s, "advisor", "vote_experience", {"experience_id": eid, "run_id": first,
                                                  "verdict": "support", "note": "again"}
                ).status == "error"  # one vote per run
    # one more run in this batch: code votes, but one batch is not enough
    second = exfoliate_and_search(s, target_thickness_labels=labels, target_min_area_um2=1)
    call(s, "advisor", "review_runs", {})
    e = s.store.get_experience(eid)
    assert e["support"] == [first, second] and e["status"] == "trial"
    # a third supporting run from another batch (project) confirms it
    book = ExperienceBook(s.store)
    third = s.jobs.submit("exfoliate_chips", {"material": "Gr", **V1}, project_id="prj_other")
    assert s.jobs.wait(third, 120).status == "succeeded"
    s.store.update_run_meta(third, target={"thickness_labels": labels, "min_area_um2": 1})
    chip = s.store.get_run(third)["params"]["chip_ids"][0]
    assert s.jobs.wait(s.jobs.submit("search_chip", {"chip_id": chip}), 300).status == \
        "succeeded"
    votes = book.auto_vote(s.store.get_run(third))
    assert {"experience_id": eid, "verdict": "support", "status": "confirmed"} in votes
    assert s.store.get_experience(eid)["status"] == "confirmed"
    # the same runs against an impossible expectation retire it
    hope = book.propose(topic="exfoliation", scope={"material": "Gr"},
                        recipe={"dwell_s": 10}, expect={"good_per_chip": 500},
                        claim="10 s dwell gives 500 good flakes per chip.",
                        evidence_run_ids=[first], observation="none")
    for rid in (second, third):
        book.vote(hope, rid, "against", note="far fewer")
    assert s.store.get_experience(hope)["status"] == "retired"
    # none of it is lab experience: the page shows no sandbox rows
    with TestClient(create_app(s, start_driver=False)) as c:
        page = c.get("/api/experience").json()
    assert page["learned"] == 0 and page["note"] == NO_REAL_EXPERIENCE
    assert eid not in {e["id"] for e in page["experiences"]}
    assert all(not e["sandbox"] for e in page["experiences"])


def test_people_teach_retire_and_report(sandbox):
    s = sandbox
    with pytest.raises(ExperienceError, match="person"):
        ExperienceBook(s.store).teach(topic="flake_choice", claim="x", scope={},
                                      instruction_id="ins_fake")
    iid = person_says(s, "The hBN from carrier 2 is too thick.")
    r = call(s, "orchestrator", "teach_experience", {
        "topic": "flake_choice", "scope": {"material": "hBN", "crystal": "carrier_hbn_02"},
        "claim": "hBN from carrier_hbn_02 is too thick for encapsulation.",
        "instruction_id": iid})
    assert r.status == "ok"
    eid = r.data["experience_id"]
    assert s.store.get_experience(eid)["status"] == "confirmed"
    later = person_says(s, "Forget that, carrier 2 is fine now.")
    r = call(s, "orchestrator", "retire_experience", {"experience_id": eid, "reason": "fine now",
                                                      "instruction_id": later})
    assert r.status == "ok" and s.store.get_experience(eid)["status"] == "retired"
    s.store.upsert_sample("device_001", "device", props={"layers_top_first": ["f1", "f2"]})
    s.store.upsert_sample("f1", "flake", props={})
    r = call(s, "orchestrator", "record_outcome", {
        "sample_id": "device_001", "verdict": "bad", "note": "bubbles in the channel",
        "instruction_id": iid})
    assert r.status == "ok"
    assert s.store.get_sample("f1")["props"]["outcomes"][0]["about"] == "device_001"
    assert s.store.get_sample("device_001")["props"]["outcomes"][0]["verdict"] == "bad"


def test_experience_never_talks_an_agent_out_of_a_check(sandbox):
    book = ExperienceBook(sandbox.store)
    with pytest.raises(ExperienceError, match="never tells"):
        book.propose(topic="equipment", scope={}, claim="Skip the camera check when in a hurry.",
                     evidence_run_ids=["run_x"], observation="-")


def test_experience_page_api(services):
    with TestClient(create_app(services, start_driver=False)) as c:
        page = c.get("/api/experience").json()
    assert page["bench"] == "sim" and page["learned"] == 0
    assert page["counts"]["confirmed"] == 1 and page["counts"]["trial"] == 3
    assert page["baselines"]["Gr"]["enforced"] is False
    assert page["rule"]["confirm_min_support"] == 3
