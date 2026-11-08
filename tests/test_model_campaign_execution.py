"""Offline campaign execution contracts; synthetic fixtures never prove model success."""
import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.runtime.store import RunStore
from scripts import record_model_demos as campaign_demo
from scripts import record_three_demos as demo


@pytest.fixture
def campaign():
    value = campaign_demo.campaign_template(dict.fromkeys(campaign_demo.IDENTITIES, "a" * 64))
    value.update(launch_authorized=True, integrated_acceptance=True, protocol_ready=True)
    return value


@pytest.fixture
def profiles():
    matched = copy.deepcopy(demo.STORIES)
    matched["honest_stop"]["events"] = (
        copy.deepcopy(demo.STORIES["complete"]["events"])
        + copy.deepcopy(demo.STORIES["honest_stop"]["events"]))
    return matched


def selected_limits(campaign, model):
    return {cell["story"]: {**demo.LIMITS, **cell["limits"]}
            for cell in campaign["cells"] if cell["model"] == model}


def registry_for(root, campaign, model, profiles, guard=lambda *a, **kw: None):
    campaign["campaign_path"] = str(Path(root).resolve())
    return demo.TrialRegistry(root, model=model, profiles=profiles,
                              limits_by_story=selected_limits(campaign, model),
                              campaign_contract=campaign, campaign_guard=guard)


def begin(registry, ledger, story="complete", **kwargs):
    return registry.start(story, source="synthetic-frozen-source", ledger=ledger,
                          limits=registry.limits_by_story[story], **kwargs)


@pytest.fixture
def staged(tmp_path, profiles):
    campaign = campaign_demo.campaign_template(dict.fromkeys(campaign_demo.IDENTITIES, "a" * 64))
    campaign.update(campaign_path=str(tmp_path / "staged"), launch_authorized=True,
                    integrated_acceptance=True, protocol_ready=True)
    registry = registry_for(Path(campaign["campaign_path"]), campaign, "atria/atria-dawn", profiles)
    ledger = tmp_path / "staged-ledger.db"
    ledger.touch()
    return campaign, registry, ledger


def reviewed_first(staged, tmp_path):
    """Synthetic operator attestation fixture; never evidence of a real owner review."""
    import hashlib
    campaign, registry, ledger = staged
    registry.release_first()
    manifest = begin(registry, ledger)
    export = tmp_path / "synthetic-first-replay"
    export.mkdir()
    replay = export / "recording.json"
    replay.write_text(json.dumps({"meta": {"campaign_sha256": demo.digest(campaign), "cell_id": "m1-complete",
        "model": manifest["model"], "execution_source_sha256": manifest["source"]}}))
    registry.finish("complete", passed=True, result={"machine_checks_passed": True, "error": None,
        "checks": {"synthetic_contract_fixture": True}, "export": str(export)})
    with registry.connect() as db:
        outcome = json.loads(db.execute("SELECT result FROM attempt WHERE story='m1-complete'").fetchone()[0])
    approval = {"version": 1, "source_kind": "operator_attestation", "operator": "synthetic test operator",
        "owner_confirms_intention": True, "direct_instruction_reference": {
            "source_kind": "direct_owner_instruction", "reference": "synthetic-only-owner-reference",
            "instruction_sha256": "b" * 64}, "campaign_sha256": demo.digest(campaign),
        "first_cell_id": "m1-complete", "attempt_number": 1, "first_outcome_sha256": demo.digest(outcome), "evidence": {}}
    for role in ("replay", "physical_process", "fault_recovery", "cache_aware_cost"):
        path = replay if role == "replay" else tmp_path / (role + ".json")
        if role != "replay":
            path.write_text(json.dumps({k: approval[k] for k in (
                "campaign_sha256", "first_cell_id", "attempt_number", "first_outcome_sha256")}))
        approval["evidence"][role] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return approval


def test_staged_first_release_allows_exactly_one_cell_and_no_replacement(staged):
    campaign, registry, ledger = staged
    before = demo.digest(campaign)
    with pytest.raises(ValueError, match="Delivery gate"):
        begin(registry, ledger)
    registry.release_first()
    assert demo.TrialRegistry.delivery_status(campaign)["authorized_cells"] == ["m1-complete"]
    for cell in campaign["cells"][1:]:
        with pytest.raises(ValueError, match="Delivery gate"):
            demo.TrialRegistry.require_delivery(campaign, cell["id"])
    manifest = begin(registry, ledger)
    (Path(manifest["workspace"]) / "startup-failure.json").write_text('{"synthetic_no_http_startup_failure":true}')
    registry.finish("complete", passed=False, result={"machine_checks_passed": False})
    assert demo.TrialRegistry.delivery_status(campaign)["state"] == "await_owner_review"
    with pytest.raises(ValueError, match="Delivery gate"):
        begin(registry, ledger, retry_failed=True)
    with pytest.raises(ValueError):
        registry.release_first()
    assert demo.digest(campaign) == before and registry.totals("complete")["estimated_usd"] == 0


def test_first_release_claim_is_atomic_under_concurrent_starts(staged):
    from concurrent.futures import ThreadPoolExecutor
    _, registry, ledger = staged
    registry.release_first()
    def claim():
        try:
            return begin(registry, ledger)["cell_id"]
        except ValueError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: claim(), range(2))) == ["blocked", "m1-complete"]
    with registry.connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 1


def test_success_still_holds_until_bound_operator_attestation(staged, tmp_path):
    campaign, registry, _ = staged
    frozen = demo.digest(campaign)
    approval = reviewed_first(staged, tmp_path)
    with pytest.raises(ValueError, match="await_owner_review"):
        demo.TrialRegistry.require_delivery(campaign, "m2-complete")
    registry.approve_first(approval)
    status = demo.TrialRegistry.delivery_status(campaign)
    assert status["state"] == "remaining_cells_released" and len(status["authorized_cells"]) == 11
    assert "m1-complete" not in status["authorized_cells"]
    assert demo.digest(campaign) == frozen
    with registry.connect() as db:
        assert [json.loads(r[0])["state"] for r in db.execute("SELECT payload FROM delivery_release ORDER BY revision")] == [
            "first_cell_released", "first_cell_in_progress", "await_owner_review", "remaining_cells_released"]
    Path(approval["evidence"]["cache_aware_cost"]["path"]).write_text('{"changed":true}')
    with pytest.raises(ValueError, match="attestation"):
        demo.TrialRegistry.require_delivery(campaign, "m2-complete")


@pytest.mark.parametrize("change", ["absent", "worker", "instruction", "outcome", "replay", "missing_cost"])
def test_malformed_or_mismatched_owner_attestation_cannot_release(staged, tmp_path, change):
    _, registry, _ = staged
    approval = reviewed_first(staged, tmp_path)
    if change == "absent":
        approval = None
    elif change == "worker":
        approval["source_kind"] = "worker_success"
    elif change == "instruction":
        approval["direct_instruction_reference"] = {"source_kind": "agent_review"}
    elif change == "outcome":
        approval["first_outcome_sha256"] = "f" * 64
    elif change == "replay":
        approval["evidence"]["replay"]["sha256"] = "f" * 64
    else:
        del approval["evidence"]["cache_aware_cost"]
    with pytest.raises(ValueError, match="attestation"):
        registry.approve_first(approval)
    assert demo.TrialRegistry.delivery_status(registry.contract)["state"] == "await_owner_review"


def test_unsupported_first_remains_unstarted_and_cannot_be_approved(staged):
    campaign, registry, _ = staged
    registry.release_first()
    registry.unsupported_first({"scope": "synthetic unsupported fixture", "http_attempts": 0})
    assert demo.TrialRegistry.delivery_status(campaign)["state"] == "await_owner_review"
    with registry.connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 0
    with pytest.raises(ValueError, match="attestation"):
        registry.approve_first({})


def test_failed_first_cannot_use_an_otherwise_well_formed_approval(staged, tmp_path):
    _, registry, _ = staged
    approval = reviewed_first(staged, tmp_path)
    # A persisted failed first outcome cannot be relabelled by approval metadata.
    with registry.connect() as db:
        db.execute("UPDATE attempt SET status='failed' WHERE story='m1-complete'")
    with pytest.raises(ValueError, match="attestation"):
        registry.approve_first(approval)
    assert demo.TrialRegistry.delivery_status(registry.contract)["authorized_cells"] == []


def test_default_astra_registry_keeps_original_profiles_limits_and_keys(tmp_path):
    ledger = tmp_path / "synthetic-ledger.db"
    ledger.touch()
    registry = demo.TrialRegistry(tmp_path / "synthetic-astra-campaign")
    manifest = begin(registry, ledger, "honest_stop")
    assert registry.model == demo.MODEL == "openai/gpt-6-astra"
    assert registry.profiles is demo.STORIES
    assert registry.keys == {story: story for story in demo.STORIES}
    assert manifest["model"] == demo.MODEL
    assert manifest["limits"] == demo.story_limits("honest_stop")
    assert Path(manifest["workspace"]).name == "honest_stop"
    assert "campaign_sha256" not in manifest and "cell_id" not in manifest
    assert len(registry.profiles["honest_stop"]["events"]) == 1
    registry.finish("honest_stop", passed=True)
    assert registry.latest("honest_stop")[1] == "finished"
    with registry.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_release'").fetchone()
    assert demo.MAX_TOTAL_ESTIMATED_USD == 120


@pytest.mark.parametrize("version", [None, 1, "2", 3])
def test_direct_engine_rejects_nonstaged_matched_before_credentials_runtime_or_directories(
        tmp_path, monkeypatch, campaign, profiles, version):
    from qumus2 import app
    from qumus2.agents import openai_credentials
    campaign["version"] = version
    root = tmp_path / "must-stay-absent"
    def forbidden(*args, **kwargs):
        pytest.fail("Nonstaged custom engine reached source/credentials/runtime")
    monkeypatch.setattr(app, "build_services", forbidden)
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", forbidden)
    with pytest.raises(ValueError, match="read-only"):
        asyncio.run(demo.record_demo("complete", "synthetic-freeze", model="atria/atria-dawn",
            campaign_path=root, profiles=profiles, limits_by_story=selected_limits(campaign, "atria/atria-dawn"),
            source_reader=forbidden, campaign_contract=campaign, campaign_guard=lambda *a, **kw: None,
            services_check=forbidden, evidence_reader=forbidden))
    assert not root.exists()


def test_direct_noncustom_astra_engine_keeps_original_credential_entry(tmp_path, monkeypatch):
    from qumus2.agents import openai_credentials
    called = []
    def credential_sentinel(*args):
        called.append(True)
        raise RuntimeError("synthetic legacy credential boundary")
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", credential_sentinel)
    with pytest.raises(RuntimeError, match="legacy credential boundary"):
        asyncio.run(demo.record_demo("complete", "synthetic-freeze", source_reader=lambda: "synthetic-freeze"))
    assert called == [True]


def test_direct_legacy_registry_start_resume_and_http_admission_fail_closed(tmp_path, campaign, profiles):
    campaign["version"] = 1
    registry = registry_for(tmp_path / "legacy-readonly", campaign, "atria/atria-dawn", profiles)
    assert registry.delivery_status(campaign) == {"state": "legacy_read_only", "authorized_cells": []}
    with pytest.raises(ValueError, match="read-only"):
        begin(registry, tmp_path / "absent-ledger.db")
    with pytest.raises(ValueError, match="read-only"):
        registry.resume("complete", {}, {})
    manifest = {"campaign_sha256": demo.digest(campaign), "cell_id": "m1-complete"}
    admission = demo.DemoAdmission(manifest, registry=registry, campaign_guard=lambda *a, **kw: None)
    with pytest.raises(ValueError, match="legacy_read_only"):
        asyncio.run(admission(httpx.Request("POST", "https://synthetic.invalid/v1/responses"), SimpleNamespace()))
    with pytest.raises(ValueError, match="staged campaign registry"):
        asyncio.run(demo.DemoAdmission(manifest)(httpx.Request("POST", "https://synthetic.invalid/v1/responses"), SimpleNamespace()))
    with registry.connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 0
    assert not (registry.root / "m1-complete").exists()


def test_models_share_one_registry_without_reusing_story_rows(tmp_path, campaign, profiles):
    ledger = tmp_path / "synthetic-ledger.db"
    ledger.touch()
    root = tmp_path / "synthetic-matched-campaign"
    before = copy.deepcopy(demo.STORIES)
    original_model = demo.MODEL
    first = registry_for(root, campaign, "atria/atria-dawn", profiles)
    first.approve_first(reviewed_first((campaign, first, ledger), tmp_path))
    manifests = [json.loads(first.latest("complete")[0])]
    for model in list(campaign_demo.MODELS)[1:]:
        registry = registry_for(root, campaign, model, profiles)
        manifest = begin(registry, ledger)
        manifests.append(manifest)
        assert manifest["model"] == model
        assert manifest["cell_id"] == registry.keys["complete"]
        assert Path(manifest["workspace"]).name == registry.keys["complete"]
        assert manifest["profile_sha256"] == demo.digest(profiles["complete"])
        assert manifest["campaign_identity"] == campaign["identity"]
        assert manifest["campaign_sha256"] == demo.digest(campaign)
        registry.finish("complete", passed=False)
    with registry.connect() as db:
        rows = db.execute("SELECT story,status FROM attempt ORDER BY story").fetchall()
        assert db.execute("SELECT count(*) FROM trial").fetchone()[0] == 4
    assert rows == [(m["cell_id"], "finished" if i == 0 else "failed") for i, m in enumerate(manifests)]
    assert len({m["workspace"] for m in manifests}) == 4
    assert demo.STORIES == before and demo.MODEL == original_model


def test_active_campaign_cell_blocks_other_model_until_explicit_finish(tmp_path, campaign, profiles):
    ledger = tmp_path / "synthetic-ledger.db"
    ledger.touch()
    models = list(campaign_demo.MODELS)
    reviewed = registry_for(tmp_path / "synthetic-campaign", campaign, models[0], profiles)
    reviewed.approve_first(reviewed_first((campaign, reviewed, ledger), tmp_path))
    first = registry_for(reviewed.root, campaign, models[1], profiles)
    second = registry_for(first.root, campaign, models[2], profiles)
    begin(first, ledger)
    with pytest.raises(ValueError, match="active campaign cell"):
        begin(second, ledger)
    with second.connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 2
    first.finish("complete", passed=False)
    assert begin(second, ledger)["attempt_number"] == 1


def test_campaign_guard_rejection_precedes_attempt_and_workspace_creation(tmp_path, campaign, profiles):
    ledger = tmp_path / "synthetic-ledger.db"
    ledger.touch()
    calls = []
    def reject(registry, manifest):
        calls.append(manifest)
        assert not Path(manifest["workspace"]).exists()
        with registry.connect() as db:
            assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 0
        raise ValueError("synthetic cumulative cap rejection")
    registry = registry_for(tmp_path / "synthetic-campaign", campaign,
                            next(iter(campaign_demo.MODELS)), profiles, reject)
    registry.release_first()
    with pytest.raises(ValueError, match="cumulative cap rejection"):
        begin(registry, ledger)
    assert len(calls) == 1
    with registry.connect() as db:
        assert db.execute("SELECT count(*) FROM trial").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 0
    assert not Path(calls[0]["workspace"]).exists()


@pytest.mark.parametrize("resource", campaign_demo.RESOURCES)
def test_custom_registry_rejects_unfrozen_cell_caps(tmp_path, campaign, profiles, resource):
    ledger = tmp_path / "synthetic-ledger.db"
    ledger.touch()
    registry = registry_for(tmp_path / "synthetic-campaign", campaign,
                            next(iter(campaign_demo.MODELS)), profiles)
    changed = {**registry.limits_by_story["complete"]}
    changed[resource] += 1
    with pytest.raises(ValueError, match="Approved story limits"):
        registry.start("complete", source="synthetic", ledger=ledger, limits=changed)
    with registry.connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 0


def test_same_cell_resume_keeps_checkpoint_and_rechecks_frozen_campaign(tmp_path, campaign, profiles):
    ledger = tmp_path / "synthetic-ledger.db"
    ledger.touch()
    calls = []
    registry = registry_for(tmp_path / "synthetic-campaign", campaign,
                            next(iter(campaign_demo.MODELS)), profiles,
                            lambda registry, manifest: calls.append(manifest["cell_id"]))
    approval = reviewed_first((campaign, registry, ledger), tmp_path)
    registry.approve_first(approval)
    calls.clear()
    manifest = begin(registry, ledger, "honest_stop")
    path = Path(manifest["workspace"]) / "qumus.db"
    store = RunStore(path)
    store.emit("synthetic.offline_fixture", {"note": "No model or laboratory experiment"})
    store.close()
    for name in ("qumus.testbench.pkl", "qumus.openai-sessions.db"):
        (path.parent / name).write_text("Synthetic checkpoint bytes only")
    checkpoint = registry.interrupt("honest_stop", "synthetic-session")
    changed = copy.deepcopy(campaign)
    changed["identity"]["initial_state"] = "b" * 64
    reopened = registry_for(registry.root, changed, registry.model, profiles)
    with pytest.raises(ValueError, match="frozen campaign|Delivery release"):
        reopened.resume("honest_stop", manifest, checkpoint)
    assert registry.resume("honest_stop", manifest, checkpoint) == manifest
    assert calls == [manifest["cell_id"], manifest["cell_id"]]
    with registry.connect() as db:
        assert db.execute("SELECT story,number,status FROM attempt WHERE story=?", (manifest["cell_id"],)).fetchall() == [
            (manifest["cell_id"], 1, "started")]


@pytest.mark.parametrize("artifact", ["qumus.testbench.pkl", "qumus.openai-sessions.db"])
def test_matched_resume_refuses_changed_controller_or_session_bytes(tmp_path, campaign, profiles, artifact):
    ledger = tmp_path / "synthetic-ledger.db"
    ledger.touch()
    registry = registry_for(tmp_path / "synthetic-campaign", campaign, next(iter(campaign_demo.MODELS)), profiles)
    registry.release_first()
    manifest = begin(registry, ledger)
    folder = Path(manifest["workspace"])
    store = RunStore(folder / "qumus.db")
    store.emit("synthetic.fixture", {})
    store.close()
    for name in ("qumus.testbench.pkl", "qumus.openai-sessions.db"):
        (folder / name).write_bytes(b"SYNTHETIC CHECKPOINT; NEVER DESERIALIZED")
    checkpoint = registry.interrupt("complete", "synthetic-session")
    (folder / artifact).write_bytes(b"CHANGED SYNTHETIC CHECKPOINT")
    with pytest.raises(ValueError, match="controller/model session"):
        registry.resume("complete", manifest, checkpoint)
    assert registry.latest("complete")[1] == "interrupted"


def test_honest_stop_sequence_uses_all_matched_faults_and_preserves_original(tmp_path, profiles):
    before = copy.deepcopy(demo.STORIES)
    store = RunStore(tmp_path / "synthetic-qumus.db")
    try:
        sequence = demo.StorySequence("honest_stop", store, profiles)
        assert sequence.propose("before_first_preparation_commit") is None
        for expected in profiles["honest_stop"]["events"]:
            action = sequence.propose("before_first_search")
            assert action == expected
            receipt = store.emit("fault.injected", {"fault": action["fault"], "params": action["params"]})
            sequence.acknowledge(action["id"], {"event_id": receipt})
        assert sequence.propose("before_first_search") is None
        applied = [e.payload["action_id"] for e in store.events_after(0)
                   if e.kind == "demo.action_applied"]
        assert applied == ["empty", "placement", "timeout", "offline"]
        assert demo.STORIES == before
    finally:
        store.close()


@pytest.mark.parametrize("model", campaign_demo.MODELS)
def test_pre_send_guard_uses_selected_model_and_frozen_limits(tmp_path, model):
    ledger = tmp_path / "synthetic-ledger.db"
    ledger.touch()
    workspace = tmp_path / "synthetic-workspace"
    workspace.mkdir()
    store = RunStore(workspace / "qumus.db")
    limits = {**demo.story_limits("honest_stop"), "http_max_retries": 0}
    manifest = {"source": "synthetic", "story": "honest_stop", "limits": limits,
                "started_wall": 10, "ledger": str(ledger), "workspace": str(workspace)}
    runtime = SimpleNamespace(
        creds=OpenAICredentials("synthetic-offline", model_id=model, budget_path=str(ledger)),
        cfg={"openai": {"min_request_interval_s": limits["min_request_interval_s"]}},
        _http_max_retries=0, _budget_scope=str(workspace / "qumus.db"))
    admission = demo.DemoAdmission(manifest, source_reader=lambda: "synthetic", now=lambda: 11,
                                   model=model, limits_reader=lambda story: limits)
    def request(selected):
        return httpx.Request("POST", "https://synthetic.invalid/v1/responses",
                             json={"model": selected, "max_output_tokens": 10})
    try:
        with pytest.raises(ValueError, match="selected model"):
            asyncio.run(admission(request(demo.MODEL), runtime))
        runtime.creds = replace(runtime.creds, model_id=demo.MODEL)
        with pytest.raises(ValueError, match="model, retry or limit"):
            asyncio.run(admission(request(model), runtime))
        assert not store.events_after(0)
    finally:
        store.close()


@pytest.mark.parametrize("model", campaign_demo.MODELS)
@pytest.mark.parametrize("source_failure", [False, True])
def test_shared_recorder_lifecycle_routes_selected_model_without_inference(
        tmp_path, monkeypatch, campaign, profiles, model, source_failure):
    """Use the real recorder/driver/export, with a runtime that cannot send HTTP."""
    from qumus2 import app
    from qumus2.agents.driver import ConversationDriver
    from qumus2.ui import recording

    monkeypatch.delenv("QUMUS2_CONFIG_DIR", raising=False)
    monkeypatch.delenv("QUMUS2_MICROSCOPE_REPLAY_DIR", raising=False)
    ledger = tmp_path / "SYNTHETIC-OFFLINE-ledger.db"
    ledger.touch()
    root = tmp_path / "SYNTHETIC-OFFLINE-campaign"
    limits = selected_limits(campaign, model)
    creds = OpenAICredentials("synthetic-offline", model_id=model, input_price=0,
                              output_price=0, budget_path=str(ledger))
    before = copy.deepcopy(demo.STORIES)
    real_build, real_export = app.build_services, recording.export_recording
    observed = []
    source_broken = []
    def source_reader():
        if source_broken:
            raise ValueError("Synthetic immutable source snapshot became unavailable")
        return "synthetic-source"
    def build(path, **kwargs):
        assert Path(path).is_relative_to(tmp_path)
        assert kwargs["use_openai"] is True
        assert kwargs["openai_credentials"].model_id == model
        assert kwargs["pre_send_admission"].model == model
        assert kwargs["http_max_retries"] == limits["honest_stop"]["http_max_retries"]
        services = real_build(path, use_openai=False)
        class OfflineRuntime:
            busy = False
            budget_exhausted = False
            session_id = "synthetic-offline-session"
            async def send(self, text):
                observed.append(text)
                services.store.emit("synthetic.offline_fixture", {
                    "note": "Recorder lifecycle only; no inference or experiment"})
            async def close(self):
                pass
            async def interrupt(self):
                pass
        runtime = OfflineRuntime()
        return replace(services, runtime=runtime,
                       driver=ConversationDriver(services.store, runtime, services.jobs))
    def export(*args, **kwargs):
        kwargs["title"] = "SYNTHETIC OFFLINE CAMPAIGN LIFECYCLE"
        kwargs["description"] = "No model inference or laboratory experiment"
        kwargs["meta"]["synthetic_offline_fixture"] = True
        return real_export(*args, **kwargs)
    def services_check(services, manifest, resume):
        assert not resume and manifest["model"] == model
        assert manifest["profile_sha256"] == demo.digest(profiles["honest_stop"])
        if source_failure:
            source_broken.append(True)
    def evidence_reader(services, manifest):
        import hashlib

        private = Path(manifest["workspace"]) / "synthetic-private-truth.json"
        private.write_text('{"synthetic_offline_fixture":true}')
        return {"evidence": {"synthetic_offline_fixture": True,
                             "terminal_truth_file": private.name,
                             "terminal_truth_file_sha256": hashlib.sha256(private.read_bytes()).hexdigest(),
                             "terminal_truth_last_event_id": services.store.last_event_id()},
                "checks": {"actual_model_execution": False}}
    monkeypatch.setattr(app, "build_services", build)
    monkeypatch.setattr(recording, "export_recording", export)
    first = registry_for(root, campaign, "atria/atria-dawn", profiles)
    first.approve_first(reviewed_first((campaign, first, ledger), tmp_path))
    summary = asyncio.run(demo.record_demo(
        "honest_stop", "synthetic-source", model=model, campaign_path=root,
        profiles=profiles, limits_by_story=limits, source_reader=source_reader,
        credentials=creds, campaign_contract=campaign,
        campaign_guard=lambda *a, **kw: None, services_check=services_check,
        evidence_reader=evidence_reader))
    assert summary["error"] is None
    assert not summary["machine_checks_passed"]
    assert summary["checks"]["actual_model_execution"] is False
    assert summary["checks"]["source_unchanged"] is not source_failure
    if source_failure:
        assert "unavailable" in summary["evidence"]["source_error"]
    assert len(observed) == 1
    assert observed[0].startswith(profiles["honest_stop"]["prompt"])
    data = json.loads((Path(summary["export"]) / "recording.json").read_text())
    assert data["meta"]["model"] == model
    assert data["meta"]["synthetic_offline_fixture"] is True
    assert data["meta"]["campaign"] == campaign["campaign_id"]
    binding = json.loads((Path(summary["export"]).parent / data["meta"]["acceptance_evidence"]["terminal_binding_file"]).read_text())
    assert binding["export_events_sha256"] == demo.digest(data["events"])
    assert binding["cell_id"] == data["meta"]["cell_id"]
    assert not any(e["kind"] == "agent.request" for e in data["events"])
    registry = registry_for(root, campaign, model, profiles)
    with registry.connect() as db:
        assert db.execute("SELECT story,status FROM attempt WHERE story=?", (registry.keys["honest_stop"],)).fetchall() == [
            (registry.keys["honest_stop"], "failed")]
    assert demo.MODEL == "openai/gpt-6-astra" and demo.STORIES == before


@pytest.mark.parametrize("normal,seed", [(False, 1), (True, 8)])
def test_matched_human_stop_consumes_recoverable_faults_before_offline_microscope(tmp_path, normal, seed):
    from conftest import quiet_bench

    from qumus2.app import build_services
    from qumus2.config import simulation_profile

    s = build_services(tmp_path / "SYNTHETIC-FAULT-PREFLIGHT.db", use_openai=False, seed=seed,
                       testbench_config=simulation_profile() if normal else quiet_bench())
    try:
        prep = s.jobs.submit("exfoliate_chips", {"material": "Gr", "num_chips": 1, "chip_ids": ["chip_002"],
                                                "dwell_s": 10, "num_massages": 1, "peel_speed": 500,
                                                "target_temp_c": 22})
        assert s.jobs.wait(prep, 60).status == "succeeded"
        sequence = demo.StorySequence("honest_stop", s.store, campaign_demo.matched_stories())
        demo.install_search_faults(s, sequence)
        search = s.jobs.submit("search_chip", {"chip_id": "chip_002", "material": "Gr"})
        status = s.jobs.wait(search, 60)
        assert status.status in ("parked", "waiting_human", "failed")
        events = [e.model_dump() for e in s.store.events_after(0, limit=1_000_000)]
        score = campaign_demo.score_export({"events": events, "runs": {r["id"]: r for r in s.store.list_runs()}},
                                          "honest_stop", model="SYNTHETIC-NO-MODEL",
                                          profiles=campaign_demo.matched_stories())
        assert score["actions"]["all_applied"] and score["recoverable_chain_observed"]
        relevant = [e for e in events if e.get("run_id") == search]
        corrected = [e["id"] for e in relevant if e["kind"] == "step.corrected"
                     and e["payload"].get("name") == "place_chip_on_transfer_stage"]
        microscope = [e["id"] for e in relevant if "microscope" in json.dumps(e["payload"]).lower()
                      and "offline" in json.dumps(e["payload"]).lower()]
        assert corrected and microscope and max(corrected) < min(microscope)
        assert not any(e["kind"] in ("safety.violation", "agent.request", "agent.usage") for e in events)
        assert s.store.spent_usd() == 0
    finally:
        s.jobs.stop_all()
        s.store.close()
