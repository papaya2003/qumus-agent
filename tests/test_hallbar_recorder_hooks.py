"""Version3 recorder hooks use the existing runtime/registry without inference."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.config import simulation_profile
from scripts import record_three_demos as demo


@pytest.fixture
def v3(tmp_path):
    config = simulation_profile("previous")
    profiles = {story: {"prompt": f"Synthetic offline {story}", "events": []}
                for story in ("recover", "escalate", "adapt")}
    models = ["openai/gpt-6-astra", "atria/atria-dawn", "olares-qumusphusis/qwen-3.8-27b",
              "qwen-token-plan/deepseek-v4.1-flash", "qwen-token-plan/qwen3.8-max"]
    cells = [{"id": f"route{i}-{story}", "model": model, "story": story}
             for i, model in enumerate(models) for story in profiles]
    campaign = {"version": 3, "campaign_id": "synthetic-v3", "campaign_path": str(tmp_path / "v3"),
        "seed": 8, "identity": {"source": "synthetic-only"}, "cells": cells,
        "first_cell_id": cells[0]["id"], "first_cell_model": models[0],
        "allow_retries": True, "simulation_config_sha256": demo.digest(config),
        "launch_authorized": True, "integrated_acceptance": True, "protocol_ready": True}
    limits = {story: {**demo.LIMITS, "estimated_usd": 100} for story in profiles}
    ledger = tmp_path / "shared-ledger.db"
    ledger.touch()
    return campaign, profiles, config, limits, ledger


def registry(v3, guard=lambda *a, **kw: None):
    campaign, profiles, _, limits, _ = v3
    return demo.TrialRegistry(campaign["campaign_path"], model=campaign["first_cell_model"], profiles=profiles,
        limits_by_story=limits, campaign_contract=campaign, campaign_guard=guard)


def start(value, ledger, **kwargs):
    return value.start("recover", source="synthetic-v3-source", ledger=ledger,
                       limits=value.limits_by_story["recover"], **kwargs)


def test_declared_astra_first_holds_other_fourteen_and_failed_retry_is_explicit(v3, monkeypatch):
    campaign, _, _, _, ledger = v3
    calls = []
    value = registry(v3, lambda _, manifest, **kw: calls.append(manifest["attempt_number"]))
    assert demo.TrialRegistry.first_identity(campaign) == {
        "id": "route0-recover", "model": "openai/gpt-6-astra", "story": "recover"}
    value.release_first()
    for cell in campaign["cells"][1:]:
        with pytest.raises(ValueError, match="Delivery gate"):
            value.require_delivery(campaign, cell["id"])
    first = start(value, ledger)
    (Path(first["workspace"]) / "startup-failure.json").write_text("{}")
    value.finish("recover", passed=False, result={"machine_checks_passed": False})
    with pytest.raises(ValueError, match="Delivery gate"):
        start(value, ledger)
    second = start(value, ledger, retry_failed=True)
    assert second["attempt_number"] == 2
    assert second["prior_attempts"] == [{key: value for key, value in first.items() if key != "prior_attempts"}]
    assert second["simulation_config_sha256"] == campaign["simulation_config_sha256"]
    assert calls == [1, 2]
    (Path(second["workspace"]) / "startup-failure.json").write_text("{}")
    value.finish("recover", passed=False, result={"machine_checks_passed": False})
    # The accounting boundary is already covered against real ledger rows by
    # existing recorder tests; this fixture supplies exhausted cumulative use.
    monkeypatch.setattr(demo, "attempt_usage", lambda _: {
        "input_tokens": 0, "output_tokens": 0, "http_attempts": 0,
        "estimated_usd": 100, "unknown_reserved_usd": 100, "unknown_usage_attempts": 1})
    with pytest.raises(ValueError, match="estimated_usd.*exhausted"):
        start(value, ledger, retry_failed=True)
    assert not (value.root / "route0-recover-attempt-003").exists()


def test_v3_retry_permission_cannot_be_invented_after_freeze(v3):
    campaign, _, _, _, ledger = v3
    campaign["allow_retries"] = False
    value = registry(v3)
    value.release_first()
    first = start(value, ledger)
    (Path(first["workspace"]) / "startup-failure.json").write_text("{}")
    value.finish("recover", passed=False, result={"machine_checks_passed": False})
    with pytest.raises(ValueError, match="Delivery gate"):
        start(value, ledger, retry_failed=True)


@pytest.mark.parametrize("change", ["hash", "hooks", "guard"])
def test_v3_invalid_contract_stops_before_credentials_runtime_or_directory(v3, monkeypatch, change):
    from qumus2 import app
    from qumus2.agents import openai_credentials

    campaign, profiles, config, limits, _ = v3
    def forbidden(*a, **kw):
        pytest.fail("Invalid hooks reached credentials/runtime")
    monkeypatch.setattr(app, "build_services", forbidden)
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", forbidden)
    kwargs = dict(simulation_config=config, scenario_setup=lambda *a: None,
                  scenario_drive=lambda *a: None, scenario_audit=lambda *a: ({"scenario": True}, {}),
                  campaign_guard=lambda *a, **kw: None)
    if change == "hash":
        campaign["simulation_config_sha256"] = "0"*64
    elif change == "hooks":
        kwargs["scenario_audit"] = None
    else:
        kwargs["campaign_guard"] = None
    with pytest.raises(ValueError):
        asyncio.run(demo.record_demo("recover", "synthetic-v3-source", model=campaign["first_cell_model"],
            profiles=profiles, limits_by_story=limits, source_reader=lambda: "synthetic-v3-source",
            campaign_path=campaign["campaign_path"], campaign_contract=campaign,
            services_check=lambda *a: None, evidence_reader=lambda *a: {}, **kwargs))
    assert not Path(campaign["campaign_path"]).exists()


@pytest.mark.parametrize("failure", ["prepare", "source", "build"])
def test_attempt_preparation_runs_before_services_and_retains_startup_failure(v3, monkeypatch, failure):
    from qumus2 import app

    campaign, profiles, config, limits, ledger = v3
    registry(v3).release_first()
    seen, current_source, folders = [], ["synthetic-v3-source"], []
    def prepare(manifest, *, continuing):
        seen.append("prepare")
        folders.append(Path(manifest["workspace"]))
        assert not continuing and not (Path(manifest["workspace"]) / "qumus.db").exists()
        if failure == "prepare":
            raise ValueError("Declared preparation refusal")
        if failure == "source":
            current_source[0] = "changed-after-preparation"
    def build(*args, **kwargs):
        seen.append("build")
        raise ValueError("Declared stop before actual services")
    monkeypatch.setattr(app, "build_services", build)
    creds = OpenAICredentials("synthetic-never-sent", model_id=campaign["first_cell_model"],
        input_price=0, output_price=0, budget_path=str(ledger))
    summary = asyncio.run(demo.record_demo("recover", "synthetic-v3-source", model=creds.model_id,
        profiles=profiles, limits_by_story=limits, source_reader=lambda: current_source[0],
        credentials=creds, campaign_path=campaign["campaign_path"], campaign_contract=campaign,
        campaign_guard=lambda *a, **kw: None, services_check=lambda *a: pytest.fail("Reached service checks"),
        evidence_reader=lambda *a: pytest.fail("Reached evidence"), simulation_config=config,
        scenario_setup=lambda *a: None, scenario_drive=lambda *a: None,
        scenario_audit=lambda *a: ({"declared": True}, {}), attempt_preparation=prepare))
    assert seen == (["prepare", "build"] if failure == "build" else ["prepare"])
    assert not summary["machine_checks_passed"] and summary["error"]
    attempt = folders[0]
    assert json.loads((attempt / "startup-failure.json").read_text())["error"] == summary["error"]
    assert not (attempt / "qumus.db").exists() and ledger.read_bytes() == b""


@pytest.mark.parametrize("callback,flag", [("audit", "request_accounting_complete"),
    ("audit", "source_unchanged"), ("evidence", "media_complete"), ("evidence", "driver_completed"),
    ("evidence", "all_requests_astra"), ("clean", None)])
def test_v3_hooks_lifecycle_cannot_overwrite_generic_checks(v3, monkeypatch, callback, flag):
    from qumus2 import app
    from qumus2.agents.driver import ConversationDriver

    campaign, profiles, config, limits, ledger = v3
    registry(v3).release_first()
    seen = []
    real_build = app.build_services
    original_stories = copy.deepcopy(demo.STORIES)
    def build(path, **kwargs):
        assert kwargs["testbench_config"] == config and kwargs["testbench_config"] is not config
        assert kwargs["seed"] == 8 and kwargs["use_openai"] is True
        services = real_build(path, use_openai=False, testbench_config=kwargs["testbench_config"], seed=8)
        class OfflineRuntime:
            busy = budget_exhausted = False
            session_id = "synthetic-v3-offline"
            async def send(self, text):
                seen.append(text)
                services.store.emit("synthetic.offline_fixture", {"no_inference": True})
            async def close(self):
                pass
            async def interrupt(self):
                pass
        runtime = OfflineRuntime()
        return replace(services, runtime=runtime, driver=ConversationDriver(services.store, runtime, services.jobs))
    monkeypatch.setattr(app, "build_services", build)
    monkeypatch.setattr(demo, "audit_demo", lambda *a, **kw: pytest.fail("Version3 used the old Pd/reactor audit"))
    def setup(services, sequence, manifest):
        seen.append("setup")
        assert sequence.story == "recover" and manifest["simulation_config_sha256"] == demo.digest(config)
    async def drive(services, sequence, manifest, turn):
        services.store.add_instruction("Second requirement", project_id=services.project_id)
        await turn()
    def audit(story, services, events, manifest):
        assert story == "recover" and len(seen) == 3
        return ({flag: True} if callback == "audit" else {"scenario_result": True}), {"scenario": "synthetic"}
    def evidence(services, manifest):
        return {"checks": {flag: True} if callback == "evidence" else {}, "evidence": {"fixture": True}}
    creds = OpenAICredentials("synthetic-never-sent", model_id=campaign["first_cell_model"],
        input_price=0, output_price=0, budget_path=str(ledger))
    summary = asyncio.run(demo.record_demo("recover", "synthetic-v3-source", model=creds.model_id,
        profiles=profiles, limits_by_story=limits, source_reader=lambda: "synthetic-v3-source",
        credentials=creds, campaign_path=campaign["campaign_path"], campaign_contract=campaign,
        campaign_guard=lambda *a, **kw: None, services_check=lambda *a: None, evidence_reader=evidence,
        simulation_config=config, scenario_setup=setup, scenario_drive=drive, scenario_audit=audit))
    assert not summary["machine_checks_passed"]  # no actual model calls exist in this fixture
    assert summary["checks"]["all_requests_astra"] is False
    if callback == "audit":
        assert "cannot overwrite" in summary["error"]
    elif callback == "evidence":
        assert summary["checks"]["independent_truth_available"] is False
    else:
        assert summary["error"] is None and summary["checks"]["scenario_result"] is True
    assert demo.STORIES == original_stories and demo.MAX_TOTAL_ESTIMATED_USD == 120
    assert len(seen) == 3 and not any(e["kind"] == "agent.request" for e in
        json.loads((Path(summary["export"]) / "recording.json").read_text())["events"])
