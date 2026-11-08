"""Offline model campaign boundaries; no provider, runtime, hardware or ledger."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import v2_recording

from qumus2.agents.openai_budget import nominal_rates
from scripts.record_model_demos import (
    IDENTITIES,
    MODELS,
    RESOURCES,
    action_evidence,
    attempt_allowance,
    campaign_template,
    main,
    preflight,
    qualification_plan,
    score_export,
    source_identity,
    validate_campaign,
)
from scripts.record_three_demos import STORIES, digest


@pytest.fixture
def campaign():
    identity = dict.fromkeys(IDENTITIES, "a" * 64)
    c = campaign_template(identity)
    c.update(version=1, launch_authorized=True, integrated_acceptance=True, protocol_ready=True)
    for model, entry in c["models"].items():
        entry["capabilities"] = {
            "gateway": c["gateway"], "model": model, "deployment": entry["deployment"],
            "evidence_sha256": "b" * 64, "context_window_tokens": 131072,
            "account_listed": True, "access_verified": True, "vision": True, "tools": True,
            "api": c["api"], "source": "Synthetic offline evidence fixture", "verified_at": "2026-10-29",
        }
        entry["pricing"] = nominal_rates(c["gateway"], model)
    return c


def request_events(model, *, known=False, failed=True, input_allowance=100, role="hands"):
    identity = {"call_id": "call", "request_id": "request", "model": model,
                "session_id": "session", "attempt": 1, "gateway": "https://physics.bytefuture.ai/v1",
                "api": "responses"}
    events = [
        {"id": 1, "kind": "demo.http_admitted", "agent": role,
         "payload": {"call_id": "call", "reservation_id": "reservation", "sdk_retry_count": 0,
                     "input_allowance": input_allowance, "output_allowance": 20}},
        {"id": 2, "kind": "agent.request", "agent": role, "payload": identity},
    ]
    if known:
        events.append({"id": 3, "kind": "agent.request_usage", "agent": role,
                       "payload": {**identity, "input_tokens": 10, "output_tokens": 2}})
    if failed:
        events.append({"id": 4, "kind": "agent.request_failure", "agent": role,
                       "payload": {**identity, "failure_kind": "http_error", "http_status": 503}})
    return events


def attempt(campaign, cell=None, **changes):
    cell = cell or campaign["cells"][0]
    return {"cell_id": cell["id"], "number": 1, "status": "failed",
            "identity": campaign["identity"], "events": request_events(cell["model"]),
            "campaign_sha256": digest(campaign), "retained_unknown_reservation_ids": ["reservation"],
            "ledger_reserved_and_settled_usd": 1, "wall_seconds": 60, **changes}


def next_usage(**changes):
    return {"estimated_usd": 0, "http_attempts": 1, "input_tokens": 100,
            "output_tokens": 20, "wall_seconds": 60, **changes}


def test_exact_requested_matrix_matches_private_owner_plan(campaign):
    validate_campaign(campaign)
    assert len(campaign["cells"]) == 12
    plan = Path(__file__).resolve().parents[2] / "notes/planning/next-phase-20261029.json"
    if plan.exists():
        task = next(t for t in json.loads(plan.read_text())["tasks"] if t["id"] == 5)
        assert MODELS == {m["id"]: m["deployment"] for m in task["requested_models"]}


@pytest.mark.parametrize("mutation", ["duplicate", "extra", "deployment", "role", "astra"])
def test_substitution_or_matrix_expansion_rejected(campaign, mutation):
    if mutation == "duplicate":
        campaign["cells"][-1] = copy.deepcopy(campaign["cells"][0])
    elif mutation == "extra":
        campaign["cells"].append(copy.deepcopy(campaign["cells"][0]))
    elif mutation == "deployment":
        campaign["models"][next(iter(MODELS))]["deployment"] = "similar-family"
    elif mutation == "role":
        campaign["models"][next(iter(MODELS))]["roles"]["eyes"] = "hidden-vision-model"
    else:
        campaign["models"]["openai/gpt-6-astra"] = copy.deepcopy(next(iter(campaign["models"].values())))
    with pytest.raises(ValueError):
        validate_campaign(campaign)


@pytest.mark.parametrize("scope", ["campaign", "model", "cell"])
@pytest.mark.parametrize("value", [None, 0, -1, float("inf"), float("nan"), True])
def test_finite_positive_caps_required(campaign, scope, value):
    spec = {"campaign": campaign, "model": next(iter(campaign["models"].values())),
            "cell": campaign["cells"][0]}[scope]
    spec["limits"]["http_attempts"] = value
    with pytest.raises(ValueError, match="Finite"):
        validate_campaign(campaign)


@pytest.mark.parametrize("scope", ["model", "campaign"])
def test_oversubscribed_allocations_rejected(campaign, scope):
    spec = campaign if scope == "campaign" else next(iter(campaign["models"].values()))
    spec["limits"]["estimated_usd"] = 1
    with pytest.raises(ValueError, match="allocations exceed"):
        validate_campaign(campaign)


@pytest.mark.parametrize("identity", IDENTITIES)
def test_changed_source_state_skills_or_protocol_block_preflight(campaign, identity):
    current = {**campaign["identity"], identity: "d" * 64}
    result = preflight(campaign, current)
    assert not result["ready"]
    assert any(identity in b for b in result["blockers"])


def test_template_is_unfunded_and_cannot_create_or_launch(campaign):
    template = campaign_template(campaign["identity"])
    result = preflight(template, template["identity"])
    assert not result["ready"] and not result["paid_execution_available"]
    assert any("release" in b for b in result["blockers"])
    with pytest.raises(SystemExit):
        main(["--live"])


def test_preflight_cli_signals_blocked_without_mutating_manifest(campaign, tmp_path, monkeypatch, capsys):
    from scripts import record_model_demos

    manifest = tmp_path / "unfunded.json"
    campaign["launch_authorized"] = False
    manifest.write_text(json.dumps(campaign))
    raw = manifest.read_bytes()
    monkeypatch.setattr(record_model_demos, "source_identity", lambda **kw: campaign["identity"])
    assert main(["preflight", str(manifest)]) == 2
    assert json.loads(capsys.readouterr().out)["ready"] is False
    assert manifest.read_bytes() == raw


def test_known_zero_prices_are_not_invented_as_positive_rates(campaign):
    result = preflight(campaign, campaign["identity"])
    assert result["scientific_matrix_ready"] and not result["ready"]
    assert not result["paid_execution_available"] and result["legacy_read_only"]
    assert campaign["models"]["olares-qumusphusis/qwen-3.8-27b"]["pricing"]["input_price"] == 0


@pytest.mark.parametrize("capability", ["access_verified", "vision", "tools"])
@pytest.mark.parametrize("value", [None, False])
def test_unknown_or_unsupported_capability_blocks_exact_model(campaign, capability, value):
    model = next(iter(MODELS))
    campaign["models"][model]["capabilities"][capability] = value
    result = preflight(campaign, campaign["identity"])
    assert not result["ready"]
    assert not result["models"][model]["ready"]
    assert any(capability in b for b in result["models"][model]["blockers"])


@pytest.mark.parametrize("cell_index", range(1, 12))
def test_staged_blocked_cells_never_read_credentials_or_create_workspace(campaign, tmp_path, monkeypatch, cell_index):
    import asyncio

    from qumus2.agents import openai_credentials
    from scripts import record_model_demos as module
    campaign.update(version=2, campaign_path=str(tmp_path / "staged-zero-send"))
    monkeypatch.setattr(module, "source_identity", lambda **kwargs: campaign["identity"])
    module.delivery_registry(campaign).release_first()
    def forbidden(*args, **kwargs):
        pytest.fail("Unreleased cell accessed credentials/normal recorder")
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", forbidden)
    monkeypatch.setattr("scripts.record_three_demos.record_demo", forbidden)
    cell = campaign["cells"][cell_index]
    with pytest.raises(ValueError, match="Delivery gate"):
        asyncio.run(module.run_cell(campaign, cell["id"], initial_state=tmp_path / "none.json",
                                    experience=tmp_path / "none-experience.json"))
    assert not (Path(campaign["campaign_path"]) / cell["id"]).exists()
    status = preflight(campaign, campaign["identity"], cell_id=cell["id"])
    assert status["selected_cell"]["scientifically_eligible"] and not status["selected_cell"]["delivery_authorized"]


def test_staged_first_identity_cannot_be_relabelled(campaign):
    campaign["version"] = 2
    campaign["cells"][0]["id"], campaign["cells"][3]["id"] = campaign["cells"][3]["id"], campaign["cells"][0]["id"]
    with pytest.raises(ValueError, match="Atria robustness"):
        validate_campaign(campaign)


def test_release_control_cli_keeps_frozen_manifest_and_has_no_execution(campaign, tmp_path, monkeypatch, capsys):
    from scripts import record_model_demos as module
    campaign.update(version=2, campaign_path=str(tmp_path / "release-control"))
    frozen = digest({"identity": campaign["identity"], "campaign": campaign})
    path = tmp_path / "frozen-campaign.json"
    path.write_text(json.dumps(campaign))
    before = path.read_bytes()
    monkeypatch.setattr(module, "source_identity", lambda **kwargs: campaign["identity"])
    assert main(["release-first", str(path), "--initial-state", "synthetic-state.json",
                 "--experience", "synthetic-experience.json", "--freeze", frozen]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "first_cell_released"
    status = preflight(campaign, campaign["identity"])
    assert status["delivery"]["authorized_cells"] == ["m1-complete"]
    assert status["eligible_models"] == ["atria/atria-dawn"]
    assert status["execution_source_sha256"] == frozen and path.read_bytes() == before
    with module.delivery_registry(campaign).connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 0


def test_legacy_matched_manifest_cannot_launch_new_execution(campaign, tmp_path):
    import asyncio

    from scripts import record_model_demos as module
    with pytest.raises(ValueError, match="read-only"):
        asyncio.run(module.run_cell(campaign, "m1-complete", initial_state=tmp_path / "none",
                                    experience=tmp_path / "none"))


def test_capability_evidence_cannot_be_borrowed_from_another_deployment(campaign):
    model = next(iter(MODELS))
    campaign["models"][model]["capabilities"]["deployment"] = "other"
    assert not preflight(campaign, campaign["identity"])["models"][model]["ready"]


def test_missing_listing_does_not_override_scoped_verified_access(campaign):
    model = next(iter(MODELS))
    campaign["models"][model]["capabilities"]["account_listed"] = False
    result = preflight(campaign, campaign["identity"])
    assert result["scientific_matrix_ready"] and not result["ready"]
    assert result["models"][model]["account_listed"] is False


def test_unsupported_atria_does_not_block_other_eligible_cells(campaign, monkeypatch, tmp_path, capsys):
    from scripts import record_model_demos as module

    campaign["models"]["atria/atria-dawn"]["capabilities"]["vision"] = False
    selected = next(c for c in campaign["cells"] if c["model"] == "qwen-token-plan/qwen3.8-max")
    result = preflight(campaign, campaign["identity"], cell_id=selected["id"])
    assert not result["whole_matrix_ready"] and not result["ready"]
    assert result["selected_cell"]["scientifically_eligible"] and result["legacy_read_only"]
    assert not result["selected_cell"]["eligible"] and not result["eligible_models"]
    assert len([c for c in result["cells"] if c["status"] == "unsupported"]) == 3
    assert result["qualification"]["atria/atria-dawn"]["disposition"] == "unsupported"
    manifest = tmp_path / "matrix.json"
    manifest.write_text(json.dumps(campaign))
    monkeypatch.setattr(module, "source_identity", lambda **kw: campaign["identity"])
    assert main(["preflight", str(manifest), "--cell", selected["id"]]) == 2
    assert json.loads(capsys.readouterr().out)["whole_matrix_ready"] is False


def test_unknown_route_has_bounded_first_cell_qualification_plan_without_capability_invention(campaign):
    model = "olares-qumusphusis/qwen-3.8-27b"
    for capability in ("access_verified", "vision", "tools"):
        campaign["models"][model]["capabilities"][capability] = None
    before = copy.deepcopy(campaign)
    plan = qualification_plan(campaign, model)
    first = next(c for c in campaign["cells"] if c["id"] == plan["charged_cell_id"])
    assert first["model"] == model and first["story"] == "complete"
    assert plan["disposition"] == "qualification_pending_acceptance"
    assert plan["same_registry_attempt_and_ledger"] and not plan["separate_pilot_or_runner"]
    assert plan["fits_first_cell"] and all(plan["limits"][k] <= first["limits"][k] for k in RESOURCES)
    assert plan["limits"]["http_attempts"] == 2
    assert not plan["access_or_capabilities_invented"] and plan["execution_supported"]
    assert campaign == before


def test_global_source_failure_still_blocks_every_otherwise_ready_cell(campaign):
    current = {**campaign["identity"], "harness": "f" * 64}
    result = preflight(campaign, current, cell_id=campaign["cells"][0]["id"])
    assert not result["selected_cell"]["eligible"]
    assert not result["eligible_models"] and not result["paid_execution_available"]


@pytest.mark.parametrize("field", ["launch_authorized", "integrated_acceptance"])
def test_json_string_false_is_not_authorization_or_acceptance(campaign, field):
    campaign[field] = "false"
    assert not preflight(campaign, campaign["identity"])["ready"]


@pytest.mark.parametrize("field", ["api", "source", "verified_at"])
def test_capability_evidence_requires_transport_and_provenance(campaign, field):
    model = next(iter(MODELS))
    campaign["models"][model]["capabilities"].pop(field)
    assert not preflight(campaign, campaign["identity"])["ready"]


@pytest.mark.parametrize("pricing", [{"evidence_sha256": "c" * 64},
                                    {"gateway": "other", "model": "similar-model"},
                                    {"input_price": float("nan"), "output_price": 0}])
def test_arbitrary_hash_or_unscoped_rates_are_not_pricing_evidence(campaign, pricing):
    campaign["models"][next(iter(MODELS))]["pricing"] = pricing
    assert not preflight(campaign, campaign["identity"])["ready"]


def test_unknown_usage_keeps_conservative_bounds_and_explicit_retry(campaign):
    a = attempt(campaign)
    with pytest.raises(ValueError, match="explicitly"):
        attempt_allowance(campaign, a["cell_id"], [a], next_usage())
    before = copy.deepcopy(a)
    result = attempt_allowance(campaign, a["cell_id"], [a], next_usage(), explicit_retry=True)
    assert result["used"]["cell"]["input_tokens"] == 100
    assert result["used"]["cell"]["output_tokens"] == 20
    assert result["used"]["cell"]["estimated_usd"] == 1
    assert result["attempt_number"] == 2 and not result["creates_attempt"]
    assert a == before


@pytest.mark.parametrize("resource", RESOURCES)
def test_retry_never_resets_cell_caps(campaign, resource):
    a = attempt(campaign)
    usage = next_usage(**{resource: campaign["cells"][0]["limits"][resource]})
    with pytest.raises(ValueError, match="Cumulative cell"):
        attempt_allowance(campaign, a["cell_id"], [a], usage, explicit_retry=True)


def test_cost_and_time_accumulate_across_model_and_campaign(campaign):
    attempts = [attempt(campaign, c, ledger_reserved_and_settled_usd=c["limits"]["estimated_usd"])
                for c in campaign["cells"]]
    with pytest.raises(ValueError, match="Cumulative campaign estimated_usd"):
        attempt_allowance(campaign, campaign["cells"][0]["id"], attempts,
                          next_usage(estimated_usd=0.01), explicit_retry=True)


@pytest.mark.parametrize("change", ["duplicate", "source", "model", "active", "successful", "gap", "unknown_cost"])
def test_attempt_identity_and_evidence_rejected(campaign, change):
    a = attempt(campaign)
    attempts = [a]
    if change == "duplicate":
        attempts.append(copy.deepcopy(a))
    elif change == "source":
        a["identity"] = {**a["identity"], "application": "e" * 64}
    elif change == "model":
        a["events"][1]["payload"]["model"] = "hidden-model"
    elif change == "active":
        a["status"] = "started"
    elif change == "successful":
        a["status"] = "succeeded"
    elif change == "gap":
        a["number"] = 2
    else:
        a["ledger_reserved_and_settled_usd"] = None
    with pytest.raises(ValueError):
        attempt_allowance(campaign, a["cell_id"], attempts, next_usage(), explicit_retry=True)


def test_unresolved_send_cannot_receive_a_failed_replacement(campaign):
    a = attempt(campaign)
    a["events"] = request_events(campaign["cells"][0]["model"], failed=False)
    with pytest.raises(ValueError, match="Unresolved send"):
        attempt_allowance(campaign, a["cell_id"], [a], next_usage(), explicit_retry=True)


def test_unknown_reservation_cannot_disappear(campaign):
    a = attempt(campaign, retained_unknown_reservation_ids=[])
    with pytest.raises(ValueError, match="retain every unknown"):
        attempt_allowance(campaign, a["cell_id"], [a], next_usage(), explicit_retry=True)


def test_borrowed_usage_cannot_reduce_cumulative_token_reservation(campaign):
    model = campaign["cells"][0]["model"]
    a = attempt(campaign, events=request_events(model, known=True), retained_unknown_reservation_ids=[])
    a["events"][2]["payload"]["session_id"] = "other-session"
    with pytest.raises(ValueError, match="identity changed"):
        attempt_allowance(campaign, a["cell_id"], [a], next_usage(), explicit_retry=True)


@pytest.mark.parametrize("field,value", [("gateway", "https://other.example/v1"), ("api", "chat_completions")])
def test_wrong_route_or_transport_cannot_enter_frozen_campaign(campaign, field, value):
    a = attempt(campaign)
    for event in a["events"]:
        if event["kind"] in ("agent.request", "agent.request_failure"):
            event["payload"][field] = value
    with pytest.raises(ValueError, match="gateway/API"):
        attempt_allowance(campaign, a["cell_id"], [a], next_usage(), explicit_retry=True)


@pytest.fixture
def live_adapter(campaign, tmp_path, monkeypatch):
    from qumus2.agents.openai_budget import SpendLedger
    from scripts import record_model_demos as module
    from scripts.record_three_demos import TrialRegistry

    state, experience = tmp_path / "state.json", tmp_path / "experience.json"
    state.write_text('{}')
    experience.write_text('{}')
    campaign["campaign_path"] = str(tmp_path / "synthetic-campaign")
    campaign["version"] = 2
    module.delivery_registry(campaign).release_first()
    monkeypatch.setattr(module, "source_identity", lambda **kw: campaign["identity"])
    model = next(iter(MODELS))
    adapter = module.CampaignAdapter(campaign, model, initial_state=state, experience=experience)
    ledger = SpendLedger(tmp_path / "synthetic-ledger.db", limit=100)
    registry = TrialRegistry(campaign["campaign_path"], model=model, profiles=campaign["profiles"],
                             limits_by_story=adapter.limits_by_story, campaign_contract=campaign,
                             campaign_guard=adapter.guard)
    pricing = campaign["models"][model]["pricing"]
    contract = {"base_url": campaign["gateway"], "api": campaign["api"], "model_id": model,
                "input_price": pricing["input_price"], "output_price": pricing["output_price"],
                "cached_input_price": None, "stream": False, "context_window_tokens": 131072,
                "test_budget_usd": 2, "stage_budget_usd": 100}
    manifest = registry.start("complete", source=adapter.expected_source, ledger=ledger.path,
                              limits=adapter.limits_by_story["complete"], runtime_contract=contract)
    return adapter, registry, ledger, manifest


def test_real_adapter_guard_reconciles_known_unsent_zero_settlement(live_adapter):
    from scripts.record_three_demos import digest

    adapter, registry, ledger, manifest = live_adapter
    path = Path(manifest["workspace"]) / "qumus.db"
    from qumus2.runtime.store import RunStore

    store = RunStore(path)
    reservation = ledger.reserve(str(path), 0.1, 2)
    ledger.settle(reservation, 0)  # Known unsent pre-send rejection; no HTTP admission.
    result = adapter.guard(registry, manifest, next_usage=next_usage())
    assert result["used"]["cell"]["estimated_usd"] == 0
    assert manifest["campaign_sha256"] == digest(adapter.campaign)
    store.close()


def test_direct_legacy_adapter_guard_cannot_admit_a_request(live_adapter, tmp_path):
    from scripts import record_model_demos as module
    adapter, registry, _, manifest = live_adapter
    legacy = {**adapter.campaign, "version": 1, "campaign_path": str(tmp_path / "legacy-read-only")}
    old_adapter = module.CampaignAdapter(legacy, adapter.model,
        initial_state=adapter.initial_state, experience=adapter.experience)
    with pytest.raises(ValueError, match="legacy_read_only"):
        old_adapter.guard(registry, manifest, next_usage=next_usage())
    assert not Path(legacy["campaign_path"]).exists()


def test_real_adapter_guard_rejects_unknown_unadmitted_reservation(live_adapter):
    adapter, registry, ledger, manifest = live_adapter
    ledger.reserve(str(Path(manifest["workspace"]) / "qumus.db"), 0.1, 2)
    with pytest.raises(ValueError, match="lacks matching HTTP"):
        adapter.guard(registry, manifest)


def test_real_adapter_guard_counts_current_unsent_reserve_before_send(live_adapter):
    from qumus2.runtime.store import RunStore

    adapter, registry, ledger, manifest = live_adapter
    path = Path(manifest["workspace"]) / "qumus.db"
    store = RunStore(path)
    ledger.reserve(str(path), manifest["limits"]["estimated_usd"], 3)
    with pytest.raises(ValueError, match="Cumulative cell estimated_usd"):
        adapter.guard(registry, manifest, next_usage=next_usage(estimated_usd=0.1))
    store.close()


def test_real_adapter_blocks_experience_tools_and_changed_library(live_adapter, monkeypatch):
    from types import SimpleNamespace

    from qumus2.agents.policy import Decision
    from qumus2.records.experience import ExperienceBook
    from qumus2.runtime.store import RunStore

    adapter, registry, _, manifest = live_adapter
    state = {"library": "original"}
    adapter.initial_state.write_text('{"truth":1}')
    adapter.experience.write_text(json.dumps(state))
    monkeypatch.setattr(ExperienceBook, "summary", lambda self: dict(state))
    store = RunStore(Path(manifest["workspace"]) / "qumus.db")
    gateway = SimpleNamespace(registry=SimpleNamespace(get=lambda name: SimpleNamespace(name=name)),
                              pre=lambda *a: Decision(True))
    services = SimpleNamespace(store=store, gateway=gateway)
    adapter.truth_reader = lambda s: {"truth": 1}
    adapter.check_services(services, manifest, False)
    assert not gateway.pre("orchestrator", "teach_experience", {}, "id").allow
    assert gateway.pre("orchestrator", "get_experience", {}, "id").allow
    state["library"] = "changed"
    with pytest.raises(ValueError, match="Active experience changed"):
        adapter.guard(registry, manifest)
    store.close()


@pytest.mark.parametrize("mutation", [None, "role", "gateway", "api", "price"])
def test_normal_admission_uses_actual_roles_route_and_campaign_guard(live_adapter, mutation):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from qumus2.agents.roster import ALL_AGENTS
    from qumus2.runtime.store import RunStore
    from scripts.record_three_demos import DemoAdmission

    adapter, registry, ledger, manifest = live_adapter
    path = Path(manifest["workspace"]) / "qumus.db"
    store = RunStore(path)
    contract = manifest["runtime_contract"]
    creds = SimpleNamespace(**contract, budget_path=str(ledger.path), web_search=False)
    reservation = ledger.reserve(str(path), 0.1, 2)
    rt = SimpleNamespace(creds=creds, cfg={"openai": {"min_request_interval_s": 60.0}},
                         _http_max_retries=2, store=store, _budget_scope=str(path), _ledger=ledger,
                         _request_context={"agent": "hands", "call_id": "call", "context_window_tokens": 131072},
                         _reservations={"hands": reservation}, run_budget_usd=2,
                         _agents={role: SimpleNamespace(model=adapter.model) for role in ALL_AGENTS})
    url = adapter.campaign["gateway"] + "/responses"
    if mutation == "role":
        rt._agents["eyes"].model = "hidden-vision-model"
    elif mutation == "gateway":
        url = "https://other.example/v1/responses"
    elif mutation == "api":
        url = adapter.campaign["gateway"] + "/chat/completions"
    elif mutation == "price":
        creds.input_price = 0
    admission = DemoAdmission(manifest, source_reader=adapter.source, model=adapter.model,
                              limits_reader=lambda s: adapter.limits_by_story[s], registry=registry,
                              campaign_guard=adapter.guard)
    request = httpx.Request("POST", url, json={"model": adapter.model, "max_output_tokens": 10})
    if mutation:
        with pytest.raises(ValueError):
            asyncio.run(admission(request, rt))
        assert not store.events_after(0)
    else:
        asyncio.run(admission(request, rt))
        assert [e.kind for e in store.events_after(0)] == ["demo.http_admitted"]
    assert ledger.total(str(path)) == 0.1
    store.close()


def test_historical_priors_use_shared_typed_events_once_and_bind_resume(live_adapter, tmp_path):
    import hashlib

    from qumus2.records.lab_geometry import LabEvidence
    from qumus2.runtime.store import RunStore

    adapter, _, _, manifest = live_adapter
    evidence = LabEvidence(assertion_id="fact_offline_prior", subject="stage", property="historical_range",
                           bench="sim", value=None, units="mm", frame="v1_unknown",
                           unknown_reason="Synthetic fixture range remains unknown",
                           source={"kind": "historical_constant", "reference": "synthetic:v1-source"})
    path = tmp_path / "historical-priors.json"
    path.write_text(json.dumps({"destination_bench": "sim", "facts": [evidence.model_dump(mode="json")]}))
    adapter.historical_evidence = path
    adapter.campaign["identity"]["historical_evidence"] = hashlib.sha256(path.read_bytes()).hexdigest()
    store = RunStore(Path(manifest["workspace"]) / "qumus.db")
    adapter.initialize_priors(store, False)
    events = [e for e in store.events_after(0) if e.kind == "lab.evidence"]
    assert len(events) == 1 and events[0].payload["value"] is None
    adapter.initialize_priors(store, True)
    assert store.last_event_id() == events[0].id
    path.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="packet changed"):
        adapter.initialize_priors(store, True)
    store.close()


def test_explicitly_absent_prior_packet_is_pinned(live_adapter):
    from qumus2.runtime.store import RunStore

    adapter, _, _, manifest = live_adapter
    store = RunStore(Path(manifest["workspace"]) / "qumus.db")
    adapter.initialize_priors(store, False)
    assert not [e for e in store.events_after(0) if e.kind == "lab.evidence"]
    assert store.get_meta("demo.historical_evidence_sha256") == adapter.campaign["identity"]["historical_evidence"]
    adapter.initialize_priors(store, True)
    store.close()


def test_terminal_truth_is_private_pure_and_bound_to_the_export(live_adapter):
    import hashlib

    from qumus2.app import build_services
    from qumus2.records.experience import ExperienceBook

    adapter, _, _, manifest = live_adapter
    s = build_services(Path(manifest["workspace"]) / "qumus.db", use_openai=False, seed=8)
    truth = s.lab.world.evaluation_snapshot()
    adapter.initial_state.write_text(json.dumps(truth))
    adapter.experience.write_text(json.dumps(ExperienceBook(s.store).summary()))
    adapter.check_services(s, manifest, False)
    before_clock, before_rng = s.store.clock.now(), s.lab.world.rng.getstate()
    result = adapter.collect_evidence(s, manifest)
    assert s.lab.world.evaluation_snapshot() == truth
    assert s.store.clock.now() == before_clock and s.lab.world.rng.getstate() == before_rng
    assert result["checks"]["independent_truth_available"]
    assert result["checks"]["independent_terminal_truth_consistent"] is False  # No experiment was performed.
    assert "terminal_truth" not in result["evidence"]
    path = Path(manifest["workspace"]) / result["evidence"]["terminal_truth_file"]
    packet = json.loads(path.read_text())
    file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert packet["truth"] == truth and packet["cell_id"] == manifest["cell_id"]
    recording = {"events": [e.model_dump() for e in s.store.events_after(0)], "runs": {},
                 "meta": {"execution_source_sha256": manifest["source"],
                          "campaign_sha256": manifest["campaign_sha256"], "cell_id": manifest["cell_id"],
                          "acceptance_evidence": result["evidence"]}}
    recording_sha = hashlib.sha256(json.dumps(recording).encode()).hexdigest()
    binding = {"original_event_prefix_sha256": packet["original_event_prefix_sha256"],
               "export_events_sha256": digest(recording["events"]), "recording_file_sha256": recording_sha,
               "execution_source_sha256": manifest["source"], "campaign_sha256": manifest["campaign_sha256"],
               "cell_id": manifest["cell_id"], "truth_file_sha256": file_sha}
    score = score_export(recording, "complete", model=adapter.model, expected_source=manifest["source"],
                         profiles=adapter.campaign["profiles"], truth_packet=packet, truth_file_sha256=file_sha,
                         binding=binding, recording_file_sha256=recording_sha)
    assert all(score["terminal_truth_binding"].values())
    assert score["independent_world_truth"] is False
    packet["cell_id"] = "different-cell"
    score = score_export(recording, "complete", model=adapter.model, expected_source=manifest["source"],
                         profiles=adapter.campaign["profiles"], truth_packet=packet, truth_file_sha256=file_sha,
                         binding=binding, recording_file_sha256=recording_sha)
    assert score["terminal_truth_binding"]["cell"] is False
    assert score["independent_world_truth"] is False
    packet["cell_id"] = manifest["cell_id"]
    recording["events"][0]["payload"]["tampered_without_changing_event_id"] = True
    score = score_export(recording, "complete", model=adapter.model, expected_source=manifest["source"],
                         profiles=adapter.campaign["profiles"], truth_packet=packet, truth_file_sha256=file_sha,
                         binding=binding, recording_file_sha256=recording_sha)
    assert score["terminal_truth_binding"]["prefix"]
    assert not score["terminal_truth_binding"]["export_events"]
    s.store.close()


@pytest.fixture
def truth_projection_owner():
    """Container-only projection fixture; no SimWorld, devices or lab jobs."""
    import random
    from dataclasses import dataclass
    from threading import RLock

    from qumus2.core.types import Pose
    from qumus2.testbench.optics import electrode_pattern
    from qumus2.testbench.world import SimFlake

    @dataclass
    class Manipulator:
        x: float = 40.
        y: float = 90.
        z: float = 6.5

    def prohibited_clock_read():
        raise AssertionError("The private projection must not read Clock or advance physics")

    def make(nonempty):
        def flake(name):
            return SimFlake(name, "Gr", 3, 0., 0., 400.,
                outline_mm=((-0.01, -0.01), (0.01, -0.01), (0.01, 0.01), (-0.01, 0.01)))
        target = SimpleNamespace(id="target", kind="chip", pose=Pose(x=0., y=0.), size_mm=(10., 10.),
            held_by=None, on=None, material="Si", flakes=[], electrodes=electrode_pattern(),
            electrode_geometry_source={"source": "projection-fixture", "units": "mm"}, electrode_reference_only=False)
        source = SimpleNamespace(id="source", kind="chip", pose=Pose(x=20., y=0.), size_mm=(10., 10.),
            held_by=None, on=None, material="Si", flakes=[flake("source-body")] if nonempty else [])
        stamp = SimpleNamespace(film=True, max_depth=0., front_radius_mm=0., drift_xy_mm=(0., 0.),
            slip_rotation_deg=0., stack=[(flake("carried-body"), .01, -.02)] if nonempty else [])
        surface = SimpleNamespace(offset=(1., 2.), height=.1, rotation=0., pivot=lambda: (3., 4.))
        return SimpleNamespace(lock=RLock(), objects={"target": target, "source": source},
            stamps={"stamp": stamp}, manip=Manipulator(), surfaces={"stage": surface}, collisions=[],
            transfer_dynamics={"updated_at": 123., "holder_z": 6.5, "epoch": {"at": 120.}},
            model={"stamp": {"dynamics": {"nested_tuple": (1, (2, 3)), "signed_zero": -0.0}}},
            clock=SimpleNamespace(now=prohibited_clock_read), rng=random.Random(1), optical_rng=random.Random(2))
    return make


@pytest.mark.parametrize("nonempty", [False, True])
def test_truth_projection_json_roundtrip_is_pure_for_patterns_and_complete_bodies(truth_projection_owner, nonempty):
    from qumus2.testbench.world import SimWorld

    owner = truth_projection_owner(nonempty)
    before = owner.rng.getstate(), owner.optical_rng.getstate()
    snapshot = SimWorld.evaluation_snapshot(owner)
    assert json.loads(json.dumps(snapshot)) == snapshot
    assert isinstance(snapshot["objects"][0]["electrode_geometry"]["polygons"][0]["points"][0], list)
    assert snapshot["transfer_dynamics"]["model"]["nested_tuple"] == [1, [2, 3]]
    if nonempty:
        assert snapshot["objects"][1]["flakes"][0]["outline_mm"][0] == [-.01, -.01]
        assert snapshot["stamps"]["stamp"]["stack"][0]["flake"]["outline_mm"][0] == [-.01, -.01]
    assert (owner.rng.getstate(), owner.optical_rng.getstate()) == before
    assert SimWorld.evaluation_snapshot(owner) == snapshot


def test_truth_projection_returned_containers_are_deeply_isolated(truth_projection_owner):
    from copy import deepcopy

    from qumus2.testbench.world import SimWorld

    owner = truth_projection_owner(True)
    snapshot = SimWorld.evaluation_snapshot(owner)
    baseline = deepcopy(snapshot)
    snapshot["objects"][0]["electrode_geometry"]["polygons"][0]["points"][0][0] = 999
    snapshot["objects"][1]["flakes"][0]["outline_mm"][0][0] = 999
    snapshot["stamps"]["stamp"]["stack"][0]["flake"]["outline_mm"][0][0] = 999
    snapshot["transfer_dynamics"]["model"]["nested_tuple"][1].append(999)
    assert SimWorld.evaluation_snapshot(owner) == baseline


@pytest.mark.parametrize("model", MODELS)
def test_cell_adapter_calls_only_the_normal_engine_with_exact_prices(campaign, tmp_path, monkeypatch, model):
    import asyncio

    from qumus2.agents import openai_credentials
    from qumus2.agents.openai_credentials import OpenAICredentials
    from scripts import record_model_demos as module
    from scripts import record_three_demos as engine

    state, experience = tmp_path / "state.json", tmp_path / "experience.json"
    state.write_text('{}')
    experience.write_text('{}')
    campaign["campaign_path"] = str(tmp_path / "synthetic-campaign")
    campaign["version"] = 2
    monkeypatch.setattr(module.TrialRegistry, "require_delivery", lambda *args, **kwargs: None)  # Routing-only unit; real gate covered separately.
    monkeypatch.setattr(module, "source_identity", lambda **kw: campaign["identity"])
    credentials = OpenAICredentials("synthetic", base_url=campaign["gateway"], model_id=engine.MODEL,
                                    input_price=10, output_price=50, cached_input_price=1,
                                    context_window_tokens=1_050_000)
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", lambda path: credentials)
    calls = []
    async def record(story, frozen, **kwargs):
        calls.append((story, frozen, kwargs))
        return {"not_executed": True}
    monkeypatch.setattr(engine, "record_demo", record)
    cell = next(c for c in campaign["cells"] if c["model"] == model and c["story"] == "honest_stop")
    result = asyncio.run(module.run_cell(campaign, cell["id"], initial_state=state, experience=experience))
    assert result == {"not_executed": True} and len(calls) == 1
    story, source, kwargs = calls[0]
    assert story == "honest_stop" and source == kwargs["source_reader"]()
    assert kwargs["model"] == kwargs["credentials"].model_id == model
    assert len(kwargs["profiles"]["honest_stop"]["events"]) == 4
    assert len(engine.STORIES["honest_stop"]["events"]) == 1 and engine.MODEL == "openai/gpt-6-astra"
    assert kwargs["credentials"].input_price == campaign["models"][model]["pricing"]["input_price"]
    assert kwargs["credentials"].output_price == campaign["models"][model]["pricing"]["output_price"]
    assert kwargs["credentials"].cached_input_price is None
    assert kwargs["credentials"].context_window_tokens == 131072
    assert not kwargs["credentials"].stream and not kwargs["credentials"].web_search
    assert not Path(campaign["campaign_path"]).exists()


def test_unfunded_cell_adapter_stops_before_credentials_or_registry(campaign, tmp_path, monkeypatch):
    import asyncio

    from qumus2.agents import openai_credentials
    from scripts import record_model_demos as module

    campaign["version"] = 2
    campaign["launch_authorized"] = False
    campaign["campaign_path"] = str(tmp_path / "unfunded-campaign")
    monkeypatch.setattr(module, "source_identity", lambda **kw: campaign["identity"])
    def forbidden(path):
        pytest.fail("Unfunded campaign must not read credentials")
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", forbidden)
    with pytest.raises(ValueError, match="not released"):
        asyncio.run(module.run_cell(campaign, campaign["cells"][0]["id"],
                                    initial_state=tmp_path / "state.json", experience=tmp_path / "experience.json"))
    assert not Path(campaign["campaign_path"]).exists()


def test_already_qualified_route_does_not_start_an_extra_probe_attempt(campaign, tmp_path, monkeypatch):
    import asyncio

    from qumus2.agents import openai_credentials
    from scripts import record_model_demos as module

    campaign["version"] = 2
    monkeypatch.setattr(module.TrialRegistry, "require_delivery", lambda *args, **kwargs: None)
    campaign["campaign_path"] = str(tmp_path / "unused-qualified-campaign")
    monkeypatch.setattr(module, "source_identity", lambda **kw: campaign["identity"])
    def forbidden(path):
        pytest.fail("Already qualified route must not read credentials for an extra probe")
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", forbidden)
    with pytest.raises(ValueError, match="already qualified"):
        asyncio.run(module.run_cell(campaign, campaign["cells"][0]["id"], initial_state=tmp_path / "state.json",
                                    experience=tmp_path / "experience.json", qualification=True))
    assert not Path(campaign["campaign_path"]).exists()


def test_run_cli_requires_explicit_live_flag_and_frozen_hash(campaign, tmp_path):
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(campaign))
    argv = ["run", str(path), "--cell", campaign["cells"][0]["id"],
            "--initial-state", str(tmp_path / "state.json"), "--experience", str(tmp_path / "experience.json"),
            "--freeze", "wrong"]
    with pytest.raises(SystemExit):
        main(argv)
    with pytest.raises(ValueError, match="source hash"):
        main([*argv, "--live"])


def test_other_cell_attempt_gaps_cannot_evade_campaign_ceiling(campaign):
    other = attempt(campaign, campaign["cells"][1], number=2)
    with pytest.raises(ValueError, match="complete attempt sequence"):
        attempt_allowance(campaign, campaign["cells"][0]["id"], [other], next_usage())


def test_fractional_elapsed_time_is_preserved(campaign):
    a = attempt(campaign, wall_seconds=1.2)
    result = attempt_allowance(campaign, a["cell_id"], [a], next_usage(wall_seconds=0.5), explicit_retry=True)
    assert result["used"]["cell"]["wall_seconds"] == 1.2


def action_events(story):
    events = []
    for action in STORIES[story]["events"]:
        start = len(events) + 1
        effect = ({"kind": "fault.injected", "payload": {"fault": action["fault"], "params": action["params"]}}
                  if "fault" in action else {"kind": "user.message", "payload": {"text": action["message"]}})
        events.extend([
            {"id": start, "kind": "demo.action_pending",
             "payload": {"profile_sha256": digest(STORIES[story]), "action": dict(action)}},
            {"id": start + 1, **effect},
            {"id": start + 2, "kind": "demo.action_applied",
             "payload": {"action_id": action["id"], "event_id": start + 1}},
        ])
    return events


def test_not_reached_is_neither_exposure_nor_recovery():
    score = score_export({"events": [], "meta": {"acceptance_checks": {"all_passed": True}}},
                         "complete", model=next(iter(MODELS)))
    assert not score["recoverable_chain_observed"]
    assert all(a["state"] == "not_reached" for a in score["actions"]["actions"])
    assert score["independent_world_truth"] is None
    assert score["source_matches"] is None
    assert not score["qualified_for_new_campaign"]


@pytest.mark.parametrize("change", ["receipt", "message", "profile", "order", "duplicate"])
def test_action_counts_cannot_replace_exact_receipt_validation(change):
    events = action_events("collaboration")
    if change == "receipt":
        events[2]["payload"]["event_id"] = 5
    elif change == "message":
        events[1]["payload"]["text"] = "different goal"
    elif change == "profile":
        events[0]["payload"]["profile_sha256"] = "wrong"
    elif change == "order":
        events[0], events[3] = events[3], events[0]
    else:
        events.append(copy.deepcopy(events[2]))
    result = action_evidence(events, STORIES["collaboration"])
    assert result["errors"] and not result["all_applied"]


def test_injection_receipts_do_not_prove_consumption():
    score = score_export({"events": action_events("complete")}, "complete", model=next(iter(MODELS)))
    assert score["actions"]["all_applied"]
    assert not score["recoverable_chain_observed"]


def recovery_recording(story):
    """Native-shaped events only; no lab, job runner, model or truth snapshot."""
    from scripts.record_model_demos import matched_stories

    profiles, events = matched_stories(), []
    def add(kind, payload, run_id=None):
        events.append({"id": len(events) + 1, "kind": kind, "payload": payload, "run_id": run_id})
    for action in profiles[story]["events"]:
        add("demo.action_pending", {"profile_sha256": digest(profiles[story]), "action": copy.deepcopy(action)})
        add("fault.injected", {"fault": action["fault"], "params": copy.deepcopy(action["params"]), "demo_action": action["id"]})
        add("demo.action_applied", {"action_id": action["id"], "event_id": events[-1]["id"]})
    params = {"chip_id": "chip_002", "material": "Gr"}
    add("job.submitted", {"flow": "search_chip", "params": dict(params)}, "search")
    place = "place_chip_on_transfer_stage"
    add("step.started", {"name": place, "seq": 1, "expected": {"sample": "chip_002", "at": "transfer_stage"}}, "search")
    add("notification", {"text": f"{place}: transient error, retrying: arm_right: serial timeout (no reply)"}, "search")
    add("notification", {"text": "arm_right: pick came up empty, picking again"}, "search")
    add("step.corrected", {"name": place, "seq": 1, "action": "re-pick at measured pose and re-place"}, "search")
    add("step.finished", {"name": place, "seq": 1, "status": "done", "corrections": 1}, "search")
    if story == "honest_stop":
        add("step.started", {"name": "chip_to_microscope", "seq": 2, "expected": {"at": "microscope"}}, "search")
        add("notification", {"text": "chip_to_microscope: transient error, retrying: microscope offline"}, "search")
        add("step.finished", {"name": "chip_to_microscope", "seq": 2, "status": "failed", "note": "microscope offline"}, "search")
    status = "succeeded" if story == "complete" else "parked"
    add("job.status", {"status": status, "message": "" if story == "complete" else "microscope offline"}, "search")
    return {"events": events, "runs": {"search": {"id": "search", "flow": "search_chip", "params": params, "status": status}}}, profiles


def assert_recovery_consumers(recording, story, profiles, monkeypatch, expected):
    """Exercise both real consumers with a read-only in-memory store facade."""
    from qumus2.records import experiment_completion
    from qumus2.ui import app
    from scripts import record_three_demos as demo

    store = SimpleNamespace(
        get_project=lambda _: {"state": {"status": "done" if story == "complete" else "blocked"}},
        list_runs=lambda: list(recording.get("runs", {}).values()),
        list_samples=lambda **_: [], pending_instructions=lambda: [])
    services = SimpleNamespace(store=store, project_id="project", lab=SimpleNamespace(bench={}))
    monkeypatch.setattr(experiment_completion, "completion_error", lambda *_: None)
    monkeypatch.setattr(app, "bench_state", lambda _: {"devices": []})
    before = copy.deepcopy((recording, profiles))
    checks, evidence = demo.audit_demo(story, services, recording["events"], profiles=profiles)
    score = score_export(recording, story, model=next(iter(MODELS)), profiles=profiles)
    key = ("three_faults_recovered_in_one_search" if story == "complete"
           else "three_recoverable_faults_observed_before_human_stop")
    assert checks[key] is expected
    assert score["recoverable_chain_observed"] is expected
    assert evidence["recoverable_search"] == score["recoverable_search"]
    assert score["fault_consumption_independently_verified"] is None
    assert score["independent_world_truth"] is None
    assert (recording, profiles) == before
    return score["recoverable_search"]


@pytest.mark.parametrize("story", ["complete", "honest_stop"])
@pytest.mark.parametrize("late_fault", [False, True])
def test_recovery_consumers_share_scoped_native_evidence(story, late_fault, monkeypatch):
    recording, profiles = recovery_recording(story)
    if late_fault:
        recording["events"].append({"id": len(recording["events"]) + 1, "kind": "fault.injected",
                                    "payload": {"fault": "serial_timeout", "params": {"device": "arm_left", "count": 1}}})
    result = assert_recovery_consumers(recording, story, profiles, monkeypatch, True)
    assert result["state"] == "observed" and result["chip_id"] == "chip_002"
    assert result["search_run_id"] == "search"


def reindex_recovery_events(events):
    ids = {event["id"]: index for index, event in enumerate(events, 1)}
    for event in events:
        if event["kind"] == "demo.action_applied":
            event["payload"]["event_id"] = ids[event["payload"]["event_id"]]
        event["id"] = ids[event["id"]]


@pytest.mark.parametrize("story", ["complete", "honest_stop"])
@pytest.mark.parametrize("change", [
    "fault_params", "profile_hash", "effect_binding", "missing_receipt", "injection_only",
    "wrong_step", "wrong_sequence", "no_repick", "wrong_chip", "wrong_submitted_chip",
    "other_run", "split_notifications", "missing_run", "missing_terminal", "later_status", "unfinished_placement",
    "correction_after_placement", "first_search_before_actions", "borrow_later_search", "confounding_fault",
    "interleaved_fault", "changed_profile", "out_of_order",
])
def test_recovery_consumers_reject_unbound_or_unfinished_chains(story, change, monkeypatch):
    recording, profiles = recovery_recording(story)
    events = recording["events"]
    def first(kind):
        return next(event for event in events if event["kind"] == kind)
    corrected = first("step.corrected")
    if change == "fault_params":
        first("fault.injected")["payload"]["params"] = {"count": 2}
    elif change == "profile_hash":
        first("demo.action_pending")["payload"]["profile_sha256"] = "wrong"
    elif change == "effect_binding":
        first("fault.injected")["payload"]["demo_action"] = "another_action"
    elif change == "missing_receipt":
        events.remove(first("demo.action_applied"))
    elif change == "injection_only":
        recording["events"] = events[:len(profiles[story]["events"]) * 3]
    elif change == "wrong_step":
        corrected["payload"]["name"] = "unrelated_step"
    elif change == "wrong_sequence":
        corrected["payload"]["seq"] = 99
    elif change == "no_repick":
        corrected["payload"]["action"] = "retry an unrelated adjustment"
    elif change == "wrong_chip":
        first("step.started")["payload"]["expected"]["sample"] = "chip_003"
    elif change == "wrong_submitted_chip":
        first("job.submitted")["payload"]["params"]["chip_id"] = "chip_003"
    elif change == "other_run":
        corrected["run_id"] = "another_search"
    elif change == "split_notifications":
        first("notification")["run_id"] = "another_search"
    elif change == "missing_run":
        recording["runs"] = {}
    elif change == "missing_terminal":
        events.remove(first("job.status"))
    elif change == "later_status":
        events.append({"id": len(events) + 1, "kind": "job.status", "run_id": "search", "payload": {"status": "running"}})
    elif change == "unfinished_placement":
        first("step.finished")["payload"]["status"] = "failed"
    elif change == "correction_after_placement":
        events.remove(corrected)
        events.insert(events.index(first("step.finished")) + 1, corrected)
        reindex_recovery_events(events)
    elif change == "first_search_before_actions":
        events.insert(0, {"id": len(events) + 1, "kind": "job.submitted", "run_id": "prior",
                          "payload": {"flow": "search_chip", "params": {"chip_id": "chip_002"}}})
        reindex_recovery_events(events)
    elif change == "borrow_later_search":
        later = copy.deepcopy(events[len(profiles[story]["events"]) * 3:])
        corrected["payload"]["name"] = "unrelated_step"
        recording["runs"]["later"] = {**recording["runs"]["search"], "id": "later"}
        for event in later:
            event.update(id=len(events) + 1, run_id="later")
            events.append(event)
    elif change in ("confounding_fault", "interleaved_fault"):
        index = events.index(corrected) if change == "confounding_fault" else 3
        events.insert(index, {"id": len(events) + 1, "kind": "fault.injected",
                                              "payload": {"fault": "next_placement", "params": {"dx": 9}}})
        reindex_recovery_events(events)
    elif change == "changed_profile":
        profiles[story]["events"][0]["params"]["count"] = 2
        first("demo.action_pending")["payload"]["action"]["params"]["count"] = 2
        first("fault.injected")["payload"]["params"]["count"] = 2
        for event in events:
            if event["kind"] == "demo.action_pending":
                event["payload"]["profile_sha256"] = digest(profiles[story])
    elif change == "out_of_order":
        events[-1], events[-2] = events[-2], events[-1]
    result = assert_recovery_consumers(recording, story, profiles, monkeypatch, False)
    assert result["state"] in ("unknown", "not_reached")


@pytest.mark.parametrize("change", ["early_failure", "prose_only", "succeeded_search", "missing_offline_receipt",
                                    "missing_microscope_start", "wrong_microscope_sequence"])
def test_human_stop_requires_actual_microscope_failure_after_correction(change, monkeypatch):
    recording, profiles = recovery_recording("honest_stop")
    events = recording["events"]
    failure = next(event for event in events if event["kind"] == "step.finished" and event["payload"]["status"] == "failed")
    if change == "early_failure":
        events.remove(failure)
        corrected = next(event for event in events if event["kind"] == "step.corrected")
        events.insert(events.index(corrected), failure)
        reindex_recovery_events(events)
    elif change == "prose_only":
        failure["kind"] = "agent.message"
    elif change == "succeeded_search":
        recording["runs"]["search"]["status"] = "succeeded"
        events[-1]["payload"]["status"] = "succeeded"
    elif change == "missing_offline_receipt":
        events[:] = [event for event in events if event["payload"].get("action_id") != "offline"]
    elif change == "missing_microscope_start":
        events[:] = [event for event in events if not (event["kind"] == "step.started"
                     and event["payload"]["name"] == "chip_to_microscope")]
    elif change == "wrong_microscope_sequence":
        failure["payload"]["seq"] = 99
    assert_recovery_consumers(recording, "honest_stop", profiles, monkeypatch, False)


def test_complete_recovery_requires_successful_search(monkeypatch):
    recording, profiles = recovery_recording("complete")
    recording["runs"]["search"]["status"] = "failed"
    recording["events"][-1]["payload"]["status"] = "failed"
    assert_recovery_consumers(recording, "complete", profiles, monkeypatch, False)


def test_unrecognized_action_is_malformed_not_just_not_reached():
    events = action_events("honest_stop")
    events[0]["payload"]["action"]["id"] = "unexpected"
    events[2]["payload"]["action_id"] = "unexpected"
    assert action_evidence(events, STORIES["honest_stop"])["errors"]


def test_request_identity_requires_actual_session_call_attempt_and_gateway():
    score = score_export({"events": [{"id": 1, "kind": "agent.request", "agent": "hands",
                                      "payload": {"model": next(iter(MODELS))}}]},
                         "honest_stop", model=next(iter(MODELS)))
    assert not score["request_role_model_identity_valid"]


def test_role_and_usage_identity_mismatch_cannot_be_hidden_by_acceptance_claims():
    model = next(iter(MODELS))
    events = request_events(model, known=True, failed=False)
    events[-1]["payload"]["session_id"] = "other-session"
    score = score_export({"events": events, "meta": {"acceptance_checks": {"all_requests_astra": True}}},
                         "honest_stop", model=model)
    assert not score["request_role_model_identity_valid"]


def test_recorded_safety_uses_readings_not_success_labels():
    event = {"id": 1, "kind": "experiment.stages_verified", "payload": {
        "project_id": "project", "cooling_job_id": "cooling", "below_c": 40, "readings": {
        "exfoliation_heater": {"temperature_c": 40, "target_c": None, "fan_commanded_on": False},
        "stamp_heater": {"temperature_c": 22, "target_c": None, "fan_commanded_on": False},
    }}}
    run = {"flow": "cool_experiment_stages", "status": "succeeded", "project_id": "project"}
    score = score_export({"events": [event], "runs": {"cooling": run}}, "complete", model=next(iter(MODELS)))
    assert score["latest_cooling_job_scope_valid"]
    assert score["latest_recorded_stages_below_40"] is False


def test_cooling_evidence_cannot_be_borrowed_from_another_project():
    event = {"id": 1, "kind": "experiment.stages_verified", "payload": {
        "project_id": "other-project", "cooling_job_id": "cooling", "readings": {}}}
    run = {"flow": "cool_experiment_stages", "status": "succeeded", "project_id": "project"}
    score = score_export({"events": [event], "runs": {"cooling": run}}, "complete", model=next(iter(MODELS)))
    assert score["latest_cooling_job_scope_valid"] is False
    assert score["latest_recorded_stages_below_40"] is None


def test_snapshot_identity_reads_only_explicit_json(tmp_path):
    snapshot = tmp_path / "state.json"
    snapshot.write_text('{"seed":8,"rng":"saved"}')
    before = snapshot.read_bytes()
    result = source_identity(initial_state=snapshot, experience=snapshot)
    assert result["initial_state"] == result["experience"]
    assert snapshot.read_bytes() == before
    with pytest.raises(ValueError, match="JSON"):
        source_identity(initial_state=tmp_path / "qumus.db")


@pytest.mark.parametrize("override", ["QUMUS2_CONFIG_DIR", "QUMUS2_MICROSCOPE_REPLAY_DIR"])
def test_config_or_image_override_cannot_keep_packaged_identity(monkeypatch, override):
    monkeypatch.setenv(override, "/different-config")
    with pytest.raises(ValueError, match="without overrides"):
        source_identity()


def test_executable_release_harness_mutation_changes_identity(tmp_path, monkeypatch):
    from scripts import record_model_demos as module

    (tmp_path / "scripts").mkdir()
    for name in ("record_model_demos.py", "record_three_demos.py", "record_release.py"):
        (tmp_path / "scripts" / name).write_text("# Synthetic executable dependency fixture\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="synthetic"\n')
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "source_fingerprint", lambda: "a" * 64)
    before = module.source_identity()
    (tmp_path / "scripts/record_release.py").write_text("def require_prepared_material(*args):\n    raise RuntimeError('changed')\n")
    after = module.source_identity()
    assert after["harness"] != before["harness"]
    assert {k: v for k, v in after.items() if k != "harness"} == {k: v for k, v in before.items() if k != "harness"}


def test_existing_accepted_exports_are_read_only_evidence_not_new_acceptance():
    expected = {"astra-complete-recovery-20261029": ("complete", 92, 91, 1),
                "astra-honest-stop-20261029": ("honest_stop", 37, 37, 0),
                "astra-collaborative-refinement-20261029": ("collaboration", 84, 82, 2)}
    for name, (story, requests, usage, failures) in expected.items():
        p = v2_recording(name) / "recording.json"
        raw = p.read_bytes()
        d = json.loads(raw)
        score = score_export(d, story, model="openai/gpt-6-astra",
                             expected_source=d["meta"]["execution_source_sha256"])
        assert (score["requests"], score["usage_rows"], len(score["failure_event_ids"])) == (requests, usage, failures)
        assert score["source_matches"] and score["request_role_model_identity_valid"]
        assert score["actions"]["all_applied"]
        assert score["http"]["all_http_attempts_accounted_conservatively"]
        assert score["http"]["provider_usage_complete"] is (story == "honest_stop")
        assert score["http"]["failed_http_recoveries_verified"] is (story != "collaboration")
        assert score["independent_world_truth"] is None
        assert not score["qualified_for_new_campaign"]
        assert raw == p.read_bytes()


@pytest.mark.parametrize("broken", [False, True])
@pytest.mark.parametrize("v2,tail,target,witness,mica", [(False, False, False, False, False), (True, False, False, False, False),
    (True, True, False, False, False), (True, False, True, False, False), (True, False, True, True, False),
    (True, False, True, True, True), (True, True, True, True, False)])
def test_v1_process_live_audit_and_export_share_actual_record_consistency(monkeypatch, broken, v2, tail, target, witness, mica):
    from qumus2.records import experiment_completion
    from scripts import record_three_demos as demo
    from tests.test_demo_scoring import (
        native_process_recording,
        target_relative_release_recording,
        witnessed_release_recording,
    )

    recording = (witnessed_release_recording(mica=mica, tail=tail) if witness else
        target_relative_release_recording(complete=True) if target else native_process_recording(v2=v2, tail=tail))
    if broken:
        recording["runs"]["transfer"]["meta"]["normal_transfer"]["phases"]["v1_pick_up_gr"]["pending_intent"] = {"axis": "z"}
    binding = recording["meta"]["acceptance_evidence"]["v1_process_binding"]
    store = SimpleNamespace(get_project=lambda _: {"state": {"status": "done"}},
        list_runs=lambda: list(recording["runs"].values()), list_samples=lambda **_: [], pending_instructions=lambda: [])
    context = SimpleNamespace(verification={"transfer_sequence": binding["profile"], "manipulator": {"tolerance_mm": .005}},
                              bench={"transfer_arm": {"safe_z": 6.5}})
    if v2:
        context.verification.update(pickup={"max_press_mm": .2}, refind_flake={"centre_tolerance_um": 6})
        context.bench["transfer_arm"]["park"] = dict(zip(("x", "y"), binding["park_xy_mm"], strict=True))
    if target:
        context.verification["focus"] = binding["endpoint_focus_profile"]
    services = SimpleNamespace(store=store, project_id="project", lab=SimpleNamespace(bench=context.bench),
                              jobs=SimpleNamespace(context_factory=lambda *_: context))
    monkeypatch.setattr(experiment_completion, "completion_error", lambda *_: None)
    before = copy.deepcopy(recording)
    checks, evidence = demo.audit_demo("complete", services, recording["events"], execution_source="a"*64)
    offline = score_export(recording, "complete", model=next(iter(MODELS)))
    assert evidence["recorded_v1_process"] == offline["recorded_v1_process"]
    assert evidence["v1_process_binding"] == binding
    assert checks["recorded_v1_process_consistent"] is (not broken)
    assert offline["independent_world_truth"] is None
    assert recording == before


@pytest.mark.parametrize("v2", [False, True])
def test_v1_process_offline_export_never_infers_missing_historical_binding(v2):
    from tests.test_demo_scoring import native_process_recording

    recording = native_process_recording(v2=v2)
    recording["meta"]["acceptance_evidence"] = {}
    offline = score_export(recording, "complete", model=next(iter(MODELS)))
    assert offline["recorded_v1_process"]["state"] == "incomplete"
    assert offline["recorded_v1_process"]["consistent"] is False


@pytest.mark.parametrize("missing", ["pickup", "refind_flake", "park"])
def test_v2_process_binding_missing_loaded_bound_stays_unknown(missing):
    from scripts.record_three_demos import recorded_v1_process_evidence
    from tests.test_demo_scoring import native_process_recording

    recording = native_process_recording(v2=True)
    binding = recording["meta"]["acceptance_evidence"]["v1_process_binding"]
    context = SimpleNamespace(verification={"transfer_sequence": binding["profile"], "manipulator": {"tolerance_mm": .005},
        "pickup": {"max_press_mm": .2}, "refind_flake": {"centre_tolerance_um": 6}},
        bench={"transfer_arm": {"safe_z": 6.5, "park": {"x": 0., "y": 0.}}})
    if missing == "park":
        del context.bench["transfer_arm"]["park"]
    else:
        del context.verification[missing]
    services = SimpleNamespace(store=SimpleNamespace(list_runs=lambda: list(recording["runs"].values())),
        jobs=SimpleNamespace(context_factory=lambda *_: context))
    audit, saved = recorded_v1_process_evidence(services, recording["events"], "complete", "a"*64)
    assert saved is None and audit["state"] == "incomplete" and not audit["problems"]


@pytest.mark.parametrize("missing_focus", [False, True])
@pytest.mark.parametrize("policy", ["native20-target-reference-parked-release-v1", "native20-latest-pickup-witness-parked-release-v2"])
def test_v2_target_release_live_binding_captures_only_loaded_fresh_policy_bounds(missing_focus, policy):
    from scripts.record_three_demos import recorded_v1_process_evidence
    from tests.test_demo_scoring import native_process_recording

    recording = native_process_recording(v2=True)
    recording["runs"]["transfer"]["params"]["release_policy"] = policy
    original = recording["meta"]["acceptance_evidence"]["v1_process_binding"]
    focus = {"coarse_points": 9, "fine_points": 9, "min_sharpness": {20: 5.5}, "minimum_peak_gain": .02}
    context = SimpleNamespace(verification={"transfer_sequence": original["profile"], "manipulator": {"tolerance_mm": .005},
        "pickup": {"max_press_mm": .2}, "refind_flake": {"centre_tolerance_um": 6}},
        bench={"transfer_arm": {"safe_z": 6.5, "park": {"x": 0., "y": 0.}}})
    if not missing_focus:
        context.verification["focus"] = focus
    services = SimpleNamespace(store=SimpleNamespace(list_runs=lambda: list(recording["runs"].values())),
        jobs=SimpleNamespace(context_factory=lambda *_: context))
    before = copy.deepcopy(recording)
    audit, saved = recorded_v1_process_evidence(services, recording["events"], "complete", "a"*64)
    if missing_focus:
        assert saved is None and audit["state"] == "incomplete" and not audit["problems"]
    else:
        assert saved == {**original, "release_policy": policy,
                         "endpoint_focus_profile": json.loads(json.dumps(focus))}
        focus["coarse_points"] = 7
        assert saved["endpoint_focus_profile"]["coarse_points"] == 9
    assert recording == before


@pytest.mark.parametrize("missing_profile", [False, True])
def test_v1_process_live_evidence_does_not_borrow_terminal_world_truth(live_adapter, monkeypatch, missing_profile):
    from qumus2.records.experience import ExperienceBook
    from tests.test_demo_scoring import native_process_recording

    adapter, _, _, manifest = live_adapter
    recording = native_process_recording()
    binding = recording["meta"]["acceptance_evidence"]["v1_process_binding"]
    event_count = len(recording["events"])
    store = SimpleNamespace(last_event_id=lambda: event_count, clock=SimpleNamespace(now=lambda: 100.), experience_sandbox=False,
        list_runs=lambda: list(recording["runs"].values()), list_samples=lambda: [],
        events_after=lambda *_args, **_kwargs: [SimpleNamespace(model_dump=lambda e=event: copy.deepcopy(e)) for event in recording["events"]])
    context = SimpleNamespace(verification={"transfer_sequence": binding["profile"], "manipulator": {"tolerance_mm": .005}},
                              bench={"transfer_arm": {"safe_z": 6.5}})
    if missing_profile:
        context.verification = {}
    services = SimpleNamespace(store=store, lab=SimpleNamespace(bench=context.bench),
                              jobs=SimpleNamespace(context_factory=lambda *_: context))
    adapter.truth_reader = lambda _: {}  # Independent truth deliberately unavailable; no simulator is constructed.
    adapter.experience.write_text("{}")
    monkeypatch.setattr(ExperienceBook, "__init__", lambda _self, _store: None)
    monkeypatch.setattr(ExperienceBook, "summary", lambda _: {})
    evidence = adapter.collect_evidence(services, manifest)
    assert evidence["checks"]["recorded_v1_process_consistent"] is (not missing_profile)
    assert evidence["checks"]["independent_truth_available"] is True
    assert evidence["checks"]["independent_terminal_truth_consistent"] is False
    exported = {**recording, "meta": {"execution_source_sha256": manifest["source"], "acceptance_evidence": evidence["evidence"]}}
    offline = score_export(exported, "complete", model=adapter.model)["recorded_v1_process"]
    if missing_profile:
        assert offline["state"] == "incomplete" and offline["consistent"] is False
    else:
        assert offline == evidence["evidence"]["recorded_v1_process"]


@pytest.mark.parametrize("story", ["collaboration", "honest_stop"])
def test_v1_process_unexercised_noncomplete_consumers_do_not_require_transfer(monkeypatch, story):
    from qumus2.ui import app
    from scripts import record_release
    from scripts import record_three_demos as demo

    store = SimpleNamespace(get_project=lambda _: {"state": {"status": "blocked"}}, list_runs=lambda: [],
        list_samples=lambda **_: [], pending_instructions=lambda: [], get_sample=lambda _: None)
    services = SimpleNamespace(store=store, project_id="project", lab=SimpleNamespace(bench={}))
    monkeypatch.setattr(app, "bench_state", lambda _: {"devices": []})
    monkeypatch.setattr(record_release, "require_prepared_material", lambda *_: None)
    checks, evidence = demo.audit_demo(story, services, [], execution_source="a"*64)
    offline = score_export({"events": []}, story, model=next(iter(MODELS)))
    assert "recorded_v1_process_consistent" not in checks
    assert evidence["recorded_v1_process"] == offline["recorded_v1_process"]
    assert offline["recorded_v1_process"]["state"] == "not_applicable"
