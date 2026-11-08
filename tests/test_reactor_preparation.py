"""Historical two-flake compatibility: real catalog selection and guarded two-arm preparation on integrated stock."""

from time import monotonic

import pytest
from test_reactor_workflow import CHIP, call, ready

from qumus2.app import build_services
from qumus2.config import load_yaml, simulation_profile
from qumus2.records.plan_completion import unmet_reactor_outcome


def execute(s, role, tool, **args):
    # Preserve the original three 120s observations as ONE wall budget.
    deadline = monotonic() + 360
    submitted = call(s, role, tool, args)
    assert submitted.status == "pending", submitted
    rid = submitted.data["job_id"]
    recoveries = 0
    while True:
        remaining = deadline - monotonic()
        assert remaining > 0, f"Job {rid} did not settle within the original 360s wall budget"
        result = call(s, role, "wait_job", {"job_id": rid, "timeout_s": min(15, remaining)})
        assert result.data.get("run_id") == rid, result
        if result.data.get("done") is not True:
            assert result.status == "pending", result
            continue  # A wait window ending does not authorize recovery.
        if result.status in ("ok", "corrected"):
            assert result.data.get("status") == "succeeded", result
            return rid
        error = result.data.get("error") or {}
        assert (result.status == "needs_attention" and result.data.get("status") == "parked" and isinstance(error, dict)
                and error.get("category") not in {"dangerous", "needs_human"}), result
        assert recoveries < 2, result
        assert monotonic() < deadline, "Observation budget exhausted before recovery inspection"
        ready(s)
        assert monotonic() < deadline, "Observation budget exhausted during recovery inspection"
        recovery = "retry_job" if role == "hands" else (
            "recover_reactor_load" if tool == "load_reactor_chip" else "recover_reactor_preparation")
        retried = call(s, role, recovery, {"job_id": rid, "reason": "Bounded recovery after inspecting the failed reversible step"})
        assert retried.status == "pending" and retried.data.get("job_id") == rid, retried
        recoveries += 1


@pytest.fixture
def execute_helper_case(monkeypatch):
    """Tool/clock mocks only: never construct services or dispatch lab work."""
    from types import SimpleNamespace

    case = SimpleNamespace(now=0., states=[], waits=[], recoveries=[], inspections=0, trace=[])
    def answer(state, done=True, category=None):
        return SimpleNamespace(status=("pending" if not done else "ok" if state == "succeeded" else "needs_attention"),
            data={"run_id": "same-job", "status": state, "done": done,
                  "error": {"category": category} if category else None})
    case.answer = answer
    def mock_call(s, role, tool, args):
        case.trace.append(tool)
        if tool == "wait_job":
            case.waits.append(dict(args))
            case.now += args["timeout_s"]
            return case.states.pop(0) if case.states else answer("running", done=False)
        if tool in {"retry_job", "recover_reactor_load", "recover_reactor_preparation"}:
            case.recoveries.append((role, tool, dict(args)))
        return SimpleNamespace(status="pending", data={"job_id": "same-job"})
    def mock_ready(s):
        case.inspections += 1
    monkeypatch.setattr(f"{__name__}.monotonic", lambda: case.now)
    monkeypatch.setattr(f"{__name__}.call", mock_call)
    monkeypatch.setattr(f"{__name__}.ready", mock_ready)
    return case


def test_execute_helper_pending_to_done_never_recovers(execute_helper_case):
    case = execute_helper_case
    case.states = [case.answer("running", done=False), case.answer("parked", done=False), case.answer("succeeded")]
    assert execute(object(), "hands", "search_chip", chip_id="chip_001") == "same-job"
    assert len(case.waits) == 3 and case.now == 45
    assert all(p == {"job_id": "same-job", "timeout_s": 15} for p in case.waits)
    assert not case.recoveries and not case.inspections


@pytest.mark.parametrize("state,category", [("waiting_human", None), ("paused", None),
    ("failed", None), ("cancelled", None), ("parked", "dangerous"), ("parked", "needs_human")])
def test_execute_helper_human_or_ineligible_stop_never_recovers(execute_helper_case, state, category):
    case = execute_helper_case
    case.states = [case.answer(state, category=category)]
    with pytest.raises(AssertionError):
        execute(object(), "hands", "search_chip")
    assert not case.recoveries and not case.inspections


@pytest.mark.parametrize("role,tool,recovery", [("hands", "search_chip", "retry_job"),
    ("reactor", "load_reactor_chip", "recover_reactor_load"),
    ("reactor", "inspect_reactor_product", "recover_reactor_preparation")])
def test_execute_helper_recovery_is_settled_same_id_and_at_most_two(execute_helper_case, role, tool, recovery):
    case = execute_helper_case
    case.states = [case.answer("parked"), case.answer("parked"), case.answer("parked")]
    with pytest.raises(AssertionError):
        execute(object(), role, tool)
    assert len(case.recoveries) == case.inspections == 2
    assert all(r[0] == role and r[1] == recovery and r[2]["job_id"] == "same-job" for r in case.recoveries)
    assert case.trace.count(tool) == 1  # No replacement submission.


def test_execute_helper_pending_exhausts_total_budget_without_retry(execute_helper_case):
    case = execute_helper_case
    with pytest.raises(AssertionError, match="original 360s wall budget"):
        execute(object(), "hands", "search_chip")
    assert case.now == 360 and len(case.waits) == 24
    assert sum(p["timeout_s"] for p in case.waits) == 360
    assert not case.recoveries and not case.inspections


def test_execute_helper_recovery_does_not_reset_wall_budget(execute_helper_case):
    case = execute_helper_case
    case.states = [case.answer("parked")]
    with pytest.raises(AssertionError, match="original 360s wall budget"):
        execute(object(), "hands", "search_chip")
    assert case.now == 360 and len(case.recoveries) == case.inspections == 1
    assert sum(p["timeout_s"] for p in case.waits) == 360


def test_execute_helper_success_after_two_settled_recoveries(execute_helper_case):
    case = execute_helper_case
    case.states = [case.answer("parked"), case.answer("parked"), case.answer("succeeded")]
    assert execute(object(), "hands", "search_chip") == "same-job"
    assert len(case.recoveries) == case.inspections == 2
    assert all(p["job_id"] == "same-job" for p in case.waits)


def test_execute_helper_inspection_cannot_recover_after_total_deadline(execute_helper_case, monkeypatch):
    case = execute_helper_case
    case.states = [case.answer("parked")]
    monkeypatch.setattr(f"{__name__}.ready", lambda s: setattr(case, "now", 361))
    with pytest.raises(AssertionError, match="during recovery inspection"):
        execute(object(), "hands", "search_chip")
    assert not case.recoveries


def test_integrated_catalog_stack_react_recover_and_routed_product_observation(tmp_path):
    cfg = simulation_profile()
    original = load_yaml("testbench.yaml")
    assert cfg["placement"] == original["placement"] and cfg["locator"] == original["locator"]
    s = build_services(tmp_path / "qumus.db", testbench_config=cfg, seed=7, use_openai=False)
    assert not any(c["props"].get("reactor_pair") for c in s.store.list_samples(kind="chip"))
    assert call(s, "orchestrator", "update_plan", {"note": "Standalone reactor experiment", "task_kind": "experiment",
        "plan": [{"id": "s1", "title": "Obtain two catalogued flakes", "owner": "hands",
                  "sample_outcomes": [{"sample_id": "chip_001", "in_storage": True}]}]}).status == "ok"
    ready(s)
    assert call(s, "advisor", "get_experience", {"material": "Gr"}).status == "ok"
    execute(s, "hands", "exfoliate_chips", material="Gr", chip_ids=["chip_001"], num_chips=1, purpose="secure")
    execute(s, "hands", "search_chip", chip_id="chip_001", material="Gr", min_area_um2=50, max_revisits=8)
    candidates = [f["id"] for f in s.store.list_samples(kind="flake")
                  if f["status"] == "catalogued" and f["props"].get("images")]
    assert len(candidates) >= 2
    execute(s, "hands", "assess_reactor_inputs", chip_id=CHIP, flake_ids=candidates)
    choices = call(s, "hands", "get_reactor_pair_candidates").data["candidates"]
    clear = [c["flake_id"] for c in choices if c["pickup_clear"]]
    assert len(clear) >= 2
    assert any(not c["pickup_clear"] for c in choices), "Crowded input neighbourhoods must remain rejected"
    top_first = clear[:2]
    prepared = execute(s, "hands", "prepare_reactor_pair", chip_id=CHIP, flake_ids=top_first)
    carrier = s.store.get_sample(CHIP)
    pair = carrier["props"]["reactor_pair"]
    assert pair["preparation_source"] == "stacking_job" and pair["preparation_id"] == prepared
    assert pair["input_flake_ids"] == list(reversed(top_first))
    assert len(s.lab.world.objects[CHIP].flakes) == 2
    assert carrier["location"] == "reactor_chip_slot_1"
    assert s.lab.world.objects[CHIP].size_mm == (2.5, 10)
    for fid in top_first:
        props = s.store.get_sample(fid)["props"]
        assert props["material_placement"]["flake"]["source_chip_id"] == "chip_001"
        assert props["geometry_area_um2"] > 0 and props["measured_area_um2"] == props["area_um2"]
    outcome = {"chip_id": CHIP, "disposition": "cold_storage", "temperature_target_c": None,
               "flow_target_sccm": 0, "rationale": "Return after preparation/reaction"}
    assert not unmet_reactor_outcome(s.store, s.lab.bench, outcome)
    assert call(s, "orchestrator", "set_reactor_pair_goal", {"chip_id": CHIP}).status == "ok"
    execute(s, "reactor", "load_reactor_chip", chip_id=CHIP)
    reacted = execute(s, "reactor", "reactor_reaction", chip_id=CHIP, duration_s=80)
    accepted = call(s, "reactor", "submit_reactor_pair_result", {"chip_id": CHIP, "reaction_job_id": reacted})
    assert accepted.status == "ok", accepted
    product_id = accepted.data["product_id"]
    execute(s, "reactor", "unload_reactor_chip", chip_id=CHIP)
    inspected = execute(s, "reactor", "inspect_reactor_product", chip_id=CHIP)
    product = s.store.get_sample(product_id)
    assert set(product["props"]["source_flake_ids"]) == set(top_first)
    assert all(s.store.get_sample(fid)["status"] == "reactor_consumed" for fid in top_first)
    assert len(s.lab.world.objects[CHIP].flakes) == 1
    assert s.lab.world.objects[CHIP].flakes[0].id == product_id
    assert {"reactor", "ordinary_microscope"} <= product["props"]["images"].keys()
    assert s.store.get_sample(CHIP)["location"] == "reactor_chip_slot_1"
    assert not unmet_reactor_outcome(s.store, s.lab.bench, outcome)
    for rid in (prepared, inspected):
        commands = [e.payload.get("device") for e in s.store.events_after(0, limit=100000)
                    if e.kind == "device.command" and e.run_id == rid]
        assert "arm_right" in commands and "arm_reactor" in commands
    assert not any(e.kind == "safety.violation" for e in s.store.events_after(0, limit=100000))
    assert s.store.spent_usd() == 0


def test_unassessed_catalog_pair_cannot_start_preparation(tmp_path):
    s = build_services(tmp_path / "qumus.db", testbench_config=simulation_profile(), use_openai=False)
    ready(s)
    # Only the submission guard is under test; these rows do not claim a
    # physical preparation and must never reach the hardware sequence.
    for fid in ("catalog_a", "catalog_b"):
        s.store.upsert_sample(fid, "flake", location="chip_e01", status="catalogued",
            props={"material": "Gr", "rel_corner_mm": [1, 1], "images": {"100x": "fixture.png"}})
    before = len(s.store.list_runs())
    result = call(s, "hands", "prepare_reactor_pair", {"chip_id": CHIP, "flake_ids": ["catalog_a", "catalog_b"]})
    assert result.status == "error" and "current observed clear pickup-neighbourhood evidence" in result.summary
    assert "catalog_a" in result.summary
    assert result.data["details"] == {"flake_id": "catalog_a", "reason": "pickup_neighbourhood_not_clear"}
    assert len(s.store.list_runs()) == before
    assert not any(run["flow"] == "prepare_reactor_pair" for run in s.store.list_runs())
    assert not s.store.get_sample(CHIP)["props"].get("reactor_pair")


def test_pair_recipe_duration_uses_pair_profile_including_incubation():
    cfg = simulation_profile()
    cfg["reactor"]["reaction"] = {"profile": {"incubation_s": 12, "exposure_to_completion_s": 75}}
    s = build_services(testbench_config=cfg, use_openai=False)
    result = call(s, "reactor", "get_reactor_setup")
    assert result.data["nominal_recipe"]["duration_s"] == 87
    assert "two-flake" in result.data["recipe_provenance"]


def test_reactor_candidate_context_distinguishes_unknown_rejected_and_unavailable():
    s = build_services(testbench_config=simulation_profile(), use_openai=False)
    # Catalogue-only query fixtures: no physical preparation is claimed.
    rid = s.store.create_run("assess_reactor_inputs", {})
    s.store.set_run_status(rid, "succeeded")
    for fid, assessment in [("clear", {"clear": True}), ("rejected", {"clear": False}),
                            ("unassessed", None), ("stale", {"clear": True, "source_chip_id": "moved_source"})]:
        props = {"material": "Gr", "images": {"100x": "query-fixture.png"}}
        if assessment is not None:
            props["reactor_pickup_assessment"] = {"flake_id": fid, "images": ["query-fixture.png"], "job_id": rid, "source_chip_id": "chip_001", **assessment}
        s.store.upsert_sample(fid, "flake", location="chip_001", status="catalogued", props=props)
    s.store.upsert_sample("raw", "flake", location="chip_001", status="candidate", props={"material": "Gr"})
    s.store.upsert_sample("missing_photo", "flake", location="chip_001", status="catalogued", props={"material": "Gr"})
    before = len(s.store.list_runs())
    result = call(s, "orchestrator", "get_reactor_pair_candidates")
    assert result.data["counts"] == {"catalogued": 5, "selected": 0, "raw_candidates": 1,
        "clear": 1, "unassessed": 1, "rejected": 1, "stale": 1, "unavailable": 2}
    rows = {r["flake_id"]: r for r in result.data["candidates"]}
    assert rows["clear"]["pickup_clear"] is True
    assert rows["rejected"]["pickup_clear"] is False
    assert rows["unassessed"]["pickup_clear"] is rows["stale"]["pickup_clear"] is None
    assert {r["flake_id"] for r in result.data["unavailable_candidates"]} == {"raw", "missing_photo"}
    assert all(not r["eligible_for_assessment"] for r in result.data["unavailable_candidates"])
    assert "only for named evidence gaps" in result.data["next"]
    assert len(s.store.list_runs()) == before


def test_single_unassessed_input_and_raw_candidates_do_not_claim_rejection_or_offer_invalid_batch():
    s = build_services(testbench_config=simulation_profile(), use_openai=False)
    s.store.upsert_sample("photographed", "flake", location="chip_001", status="catalogued",
        props={"material": "Gr", "images": {"100x": "query-fixture.png"}})
    for i in range(3):
        s.store.upsert_sample(f"raw_{i}", "flake", location="chip_001", status="candidate", props={"material": "Gr"})
    result = call(s, "hands", "get_reactor_pair_candidates")
    assert result.data["counts"]["unassessed"] == 1 and result.data["counts"]["rejected"] == 0
    assert result.data["counts"]["raw_candidates"] == 3
    assert result.data["candidates"][0]["pickup_clear"] is None
    assert "photograph unavailable candidates first" in result.data["next"]
    assert "both required materials" in result.data["next"]
    # A complete pair needs both materials, but a targeted revisit can inspect
    # just one uncertain candidate. Do not reinstate a two-candidate batch gate.
    from qumus2.agents.tools.reactor import AssessReactorInputsArgs

    assert AssessReactorInputsArgs(chip_id=CHIP, flake_ids=["photographed"]).flake_ids == ["photographed"]
