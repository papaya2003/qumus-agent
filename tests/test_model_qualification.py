"""Offline qualification uses the normal SDK, tool gateway and spend ledger.

The HTTP replies below are synthetic fixtures. They establish execution and
accounting contracts, never the capabilities of any remote model.
"""
import asyncio
import copy
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import openai
import pytest

from qumus2.agents.openai_budget import SpendLedger
from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.agents.openai_runtime import OpenAIRuntime
from qumus2.app import build_services
from qumus2.records.experience import ExperienceBook
from scripts import record_model_demos as campaign_demo
from scripts import record_three_demos as demo

MODEL = "atria/atria-dawn"


def synthetic_response(model, kind, project_id, *, usage=True):
    if kind == "tool":
        output = [{"type": "function_call", "id": "fc-synthetic-qualification",
                   "call_id": "call-synthetic-qualification", "name": "get_project_context",
                   "arguments": "{}", "status": "completed"}]
    else:
        answer = {"top_colors": ["orange", "blue", "green"],
                  "lower_left_shape": "triangle", "project_id": project_id}
        if kind == "wrong_image":
            answer["lower_left_shape"] = "circle"
        elif kind == "wrong_project":
            answer["project_id"] = "another-project"
        output = [{"type": "message", "id": "msg-synthetic-qualification", "role": "assistant",
                   "status": "completed", "content": [{"type": "output_text",
                   "text": json.dumps(answer), "annotations": []}]}]
    response = {"id": f"response-synthetic-{kind}", "object": "response", "created_at": 1,
                "status": "completed", "model": model, "output": output}
    if usage:
        response["usage"] = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    return response


@pytest.fixture
def qualification_fixture(tmp_path, monkeypatch):
    """Create one first-cell attempt and entirely private synthetic state."""
    monkeypatch.delenv("QUMUS2_CONFIG_DIR", raising=False)
    monkeypatch.delenv("QUMUS2_MICROSCOPE_REPLAY_DIR", raising=False)
    original_sleep = asyncio.sleep
    elapsed, sent, sleeps = [0.0], [], []
    source_changed, truth_calls = [], []
    identity = dict.fromkeys(campaign_demo.IDENTITIES, "a" * 64)

    def source_identity(**kwargs):
        return {**identity, **({"application": "b" * 64} if source_changed else {})}

    monkeypatch.setattr(campaign_demo, "source_identity", source_identity)
    campaign = campaign_demo.campaign_template(identity)
    campaign.update(campaign_path=str(tmp_path / "synthetic-campaign"), launch_authorized=True,
                    integrated_acceptance=True, protocol_ready=True, protocol_blockers=[])
    # Known context capacity is a separate input; tool/image capabilities remain unknown.
    campaign["models"][MODEL]["capabilities"] = {
        "gateway": campaign["gateway"], "model": MODEL, "deployment": campaign_demo.MODELS[MODEL],
        "api": campaign["api"], "source": "synthetic route listing; image/tool access unknown",
        "evidence_sha256": "c" * 64, "verified_at": "2026-10-29T00:00:00Z",
        "context_window_tokens": 2_000_000}
    campaign["models"][MODEL]["pricing"].update(input_price=.1, output_price=.2)
    initial_path, experience_path = tmp_path / "initial.json", tmp_path / "experience.json"
    initial_path.write_text("{}")
    experience_path.write_text("{}")
    ledger_path = tmp_path / "synthetic-ledger.db"
    SpendLedger(ledger_path, limit=None)
    adapter = campaign_demo.CampaignAdapter(campaign, MODEL, initial_state=initial_path,
        experience=experience_path, qualification=True, now=lambda: time.time())
    creds = OpenAICredentials("synthetic-offline-key", base_url=campaign["gateway"], model_id=MODEL,
        input_price=.1, output_price=.2, context_window_tokens=2_000_000, stage_budget_usd=None,
        test_budget_usd=1, stream=False, budget_path=str(ledger_path))
    runtime_contract = {key: getattr(creds, key) for key in (
        "base_url", "api", "model_id", "input_price", "output_price", "cached_input_price",
        "context_window_tokens", "stage_budget_usd", "test_budget_usd", "stream")}
    campaign_demo.delivery_registry(campaign).release_first()
    registry = demo.TrialRegistry(tmp_path / "synthetic-campaign", model=MODEL,
        profiles=campaign["profiles"], limits_by_story=adapter.limits_by_story,
        campaign_contract=campaign, campaign_guard=adapter.guard)
    manifest = registry.start("complete", source=adapter.expected_source, ledger=ledger_path,
        limits=adapter.limits_by_story["complete"], runtime_contract=runtime_contract)
    services = build_services(Path(manifest["workspace"]) / "qumus.db", use_openai=False, seed=8)
    experience_before = ExperienceBook(services.store).summary()
    experience_path.write_text(json.dumps(experience_before))
    truth_before = services.lab.world.evaluation_snapshot()
    initial_path.write_text(json.dumps(truth_before))
    adapter.check_services(services, manifest, False)

    def pure_truth(current_services):
        truth_calls.append(True)
        return current_services.lab.world.evaluation_snapshot()

    def forbidden_observation(*args, **kwargs):
        pytest.fail("Qualification must use its synthetic image without laboratory observation")

    adapter.truth_reader = pure_truth
    microscope = services.lab.devices.get("microscope")
    for method in ("capture", "preview", "preview_snapshot"):
        monkeypatch.setattr(microscope, method, forbidden_observation)
    for method in ("sharpness", "chip_in_view", "find_corner", "fringes", "contact",
                   "find_stamp_flakes", "find_flakes"):
        monkeypatch.setattr(services.lab.analyzer, method, forbidden_observation)
    runtime = OpenAIRuntime(services.gateway, creds, run_budget_usd=manifest["attempt_budget_usd"],
        http_max_retries=adapter.limits_by_story["complete"]["http_max_retries"],
        pre_send_admission=demo.DemoAdmission(manifest, source_reader=adapter.source, model=MODEL,
            limits_reader=lambda story: adapter.limits_by_story[story],
            campaign_guard=adapter.guard, registry=registry))
    services = replace(services, runtime=runtime)
    adapter.services = services
    demo.configure_demo_pacing(runtime, adapter.limits_by_story["complete"])
    states = ["tool", "final"]
    response_project = [services.project_id]
    missing_usage = set()
    change_after = []

    def transport(request):
        number = len(sent)
        assert number < len(states), "Qualification sent beyond the synthetic fixture's allowance"
        sent.append({"request": request, "body": json.loads(request.content), "time": elapsed[0]})
        state = states[number]
        if change_after and len(sent) == change_after[0]:
            source_changed.append(True)
        if state == 429:
            return httpx.Response(429, headers={"retry-after": "1"},
                json={"error": {"message": "synthetic limit", "type": "rate_limit"}})
        return httpx.Response(200, json=synthetic_response(MODEL, state, response_project[0],
                                                        usage=number not in missing_usage))

    async def fake_sleep(delay):
        sleeps.append(delay)
        elapsed[0] += max(0, delay)
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(transport), **kwargs))
    calls = []
    real_call = services.gateway.call

    async def gateway_call(agent, tool, args, *args_rest, **kwargs):
        calls.append((agent, tool, copy.deepcopy(args)))
        return await real_call(agent, tool, args, *args_rest, **kwargs)

    monkeypatch.setattr(services.gateway, "call", gateway_call)
    world = services.lab.world
    before = {"truth": copy.deepcopy(truth_before), "rng": world.rng.getstate(),
              "observation_rng": world.observation_rng.getstate(), "experience": experience_before,
              "campaign": copy.deepcopy(campaign), "samples": services.store.list_samples()}

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "time", lambda: 10_000.0 + elapsed[0])
        demo.install_retry_observer(runtime)
        return await adapter.qualify(services, manifest)

    fixture = SimpleNamespace(adapter=adapter, campaign=campaign, services=services, registry=registry,
        manifest=manifest, runtime=runtime, states=states, missing_usage=missing_usage, sent=sent,
        sleeps=sleeps, elapsed=elapsed, response_project=response_project,
        source_changed=source_changed, change_after=change_after,
        truth_calls=truth_calls, before=before, calls=calls, run=lambda: asyncio.run(run()))
    yield fixture
    asyncio.run(runtime.close())
    services.jobs.stop_all()
    services.store.close()


def events(fixture, kind):
    return [e.model_dump() for e in fixture.services.store.events_after(0, limit=1_000_000)
            if e.kind == kind]


def spend_rows(fixture):
    with sqlite3.connect(fixture.manifest["ledger"]) as db:
        return db.execute("SELECT id,scope,reserved,actual FROM spend ORDER BY rowid").fetchall()


def assert_frozen_lab(fixture):
    services, before = fixture.services, fixture.before
    assert services.lab.world.evaluation_snapshot() == before["truth"]
    assert services.lab.world.rng.getstate() == before["rng"]
    assert services.lab.world.observation_rng.getstate() == before["observation_rng"]
    assert ExperienceBook(services.store).summary() == before["experience"]
    assert services.store.list_samples() == before["samples"]
    assert not services.store.list_runs()
    assert fixture.campaign == before["campaign"]
    assert fixture.adapter.campaign == before["campaign"]


def qualification_result(fixture):
    recorded = events(fixture, "demo.qualification_result")
    assert len(recorded) == 1
    result = recorded[0]["payload"]
    assert json.loads(fixture.services.store.get_meta("demo.qualification")) == result
    return result


def test_qualification_real_sdk_image_tool_and_fresh_story_session(qualification_fixture):
    fixture = qualification_fixture
    fixture.services.store.update_project_state(fixture.services.project_id,
                                               agents_last={"orchestrator": "earlier story status"})
    previous_status = fixture.services.store.get_project(fixture.services.project_id)["state"]["agents_last"]
    session = fixture.runtime.session_id
    fixture.run()
    result = qualification_result(fixture)
    assert result["passed"] is True
    assert all(result["checks"].values())
    assert len(fixture.sent) == len(events(fixture, "agent.request")) == 2
    assert fixture.calls == [("orchestrator", "get_project_context", {})]
    assert all(request["body"]["model"] == MODEL for request in fixture.sent)
    assert all(request["body"].get("stream", False) is False for request in fixture.sent)
    assert fixture.sent[1]["time"] - fixture.sent[0]["time"] >= 60
    first_input = fixture.sent[0]["body"]["input"]
    images = [content for item in first_input if isinstance(item, dict)
              for content in item.get("content", []) if isinstance(content, dict)
              and content.get("type") == "input_image"]
    assert len(images) == 1 and images[0]["image_url"].startswith("data:image/png;base64,")
    second_input = fixture.sent[1]["body"]["input"]
    outputs = [item for item in second_input if item.get("type") == "function_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "call-synthetic-qualification"
    assert fixture.services.project_id in json.dumps(outputs[0]["output"])
    assert sum(e["payload"]["output_tokens"] for e in events(fixture, "agent.request_usage")) <= 512
    assert len(spend_rows(fixture)) == 2
    assert all(row[3] is not None and row[3] > 0 for row in spend_rows(fixture))
    assert fixture.runtime.session_id != session
    assert fixture.services.store.get_project(fixture.services.project_id)["state"]["agents_last"] == previous_status
    assert fixture.runtime._session is None or not asyncio.run(fixture.runtime._session.get_items())
    with fixture.registry.connect() as db:
        assert db.execute("SELECT number,status FROM attempt").fetchall() == [(1, "started")]
    fixture.states.append("story")
    asyncio.run(fixture.runtime.send("SYNTHETIC FIRST STORY MESSAGE"))
    assert len(fixture.sent) == 3
    story_input = fixture.sent[-1]["body"]["input"]
    assert "SYNTHETIC FIRST STORY MESSAGE" in json.dumps(story_input)
    assert "call-synthetic-qualification" not in json.dumps(story_input)
    assert not any(content.get("type") == "input_image" for item in story_input
                   for content in item.get("content", []) if isinstance(content, dict))
    assert_frozen_lab(fixture)


@pytest.mark.parametrize("kind", ["wrong_image", "wrong_project"])
def test_qualification_rejects_unobserved_visual_or_project_claim(qualification_fixture, kind):
    fixture = qualification_fixture
    fixture.states[:] = ["tool", kind]
    with pytest.raises(ValueError):
        fixture.run()
    result = qualification_result(fixture)
    assert result["passed"] is False
    assert not all(result["checks"].values())
    assert len(fixture.sent) == 2
    assert_frozen_lab(fixture)


@pytest.mark.parametrize("missing_attempt", [0, 1])
def test_qualification_missing_usage_keeps_unknown_reservation(qualification_fixture, missing_attempt):
    fixture = qualification_fixture
    fixture.missing_usage.add(missing_attempt)
    with pytest.raises(ValueError):
        fixture.run()
    assert qualification_result(fixture)["passed"] is False
    assert len(fixture.sent) == missing_attempt + 1
    rows = spend_rows(fixture)
    assert any(row[3] is None and row[2] > 0 for row in rows)
    reported = [e for e in events(fixture, "agent.request_usage")
                if e["payload"].get("usage_status") == "reported"]
    assert len(reported) == missing_attempt
    assert_frozen_lab(fixture)


@pytest.mark.parametrize("states", [[429, 429, 429], [429, "tool", "final"]])
def test_qualification_retry_attempts_count_toward_same_two_send_limit(qualification_fixture, states):
    fixture = qualification_fixture
    fixture.states[:] = states
    with pytest.raises(ValueError):
        fixture.run()
    assert qualification_result(fixture)["passed"] is False
    assert len(fixture.sent) == len(events(fixture, "agent.request")) == 2
    assert len(events(fixture, "demo.http_admitted")) == 2
    rows = spend_rows(fixture)
    assert rows[0][3] is None and rows[0][2] > 0
    assert all(row[1] == str(Path(fixture.manifest["workspace"]) / "qumus.db") for row in rows)
    with fixture.registry.connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 1
    assert_frozen_lab(fixture)


@pytest.mark.parametrize("change_after", [0, 1])
def test_qualification_source_change_blocks_next_http_send(qualification_fixture, change_after):
    fixture = qualification_fixture
    if change_after:
        fixture.change_after.append(change_after)
    else:
        fixture.source_changed.append(True)
    with pytest.raises(ValueError):
        fixture.run()
    if change_after:
        assert qualification_result(fixture)["passed"] is False
    else:
        assert not events(fixture, "agent.request")
    assert len(fixture.sent) == change_after
    assert_frozen_lab(fixture)


def test_delivery_hold_blocks_actual_qualification_http_and_retains_unsent_accounting(qualification_fixture):
    fixture = qualification_fixture
    fixture.registry.finish("complete", passed=False, result={"machine_checks_passed": False})
    with pytest.raises(ValueError, match="Qualification did not pass"):
        fixture.run()
    assert not fixture.sent
    assert not events(fixture, "agent.request")
    assert demo.TrialRegistry.delivery_status(fixture.campaign)["state"] == "await_owner_review"
    assert all(actual == 0 for _, _, _, actual in spend_rows(fixture))


def test_delivery_hold_between_sdk_retries_blocks_second_send_without_resetting_unknown_spend(qualification_fixture):
    fixture = qualification_fixture
    fixture.states[:] = [429, "tool", "final"]
    original = fixture.runtime._pre_send_admission
    calls = []
    async def admission(request, runtime):
        calls.append(True)
        if len(calls) == 2:
            fixture.registry.finish("complete", passed=False, result={"machine_checks_passed": False})
        await original(request, runtime)
    fixture.runtime._pre_send_admission = admission
    with pytest.raises(ValueError, match="Qualification did not pass"):
        fixture.run()
    assert len(calls) == 2 and len(fixture.sent) == 1
    assert len(events(fixture, "agent.request")) == 1
    rows = spend_rows(fixture)
    assert rows[0][3] is None and rows[0][2] > 0
    assert demo.TrialRegistry.delivery_status(fixture.campaign)["state"] == "await_owner_review"


def test_known_atria_vision_unsupported_stays_unstarted(tmp_path, monkeypatch):
    identity = dict.fromkeys(campaign_demo.IDENTITIES, "a" * 64)
    campaign = campaign_demo.campaign_template(identity)
    campaign.update(campaign_path=str(tmp_path / "unstarted-campaign"), launch_authorized=True,
                    integrated_acceptance=True, protocol_ready=True, protocol_blockers=[])
    model = "atria/atria-dawn"
    campaign["models"][model]["capabilities"] = {
        "gateway": campaign["gateway"], "model": model, "deployment": campaign_demo.MODELS[model],
        "api": campaign["api"], "source": "synthetic unsupported capability evidence",
        "evidence_sha256": "c" * 64, "verified_at": "2026-10-29T00:00:00Z",
        "context_window_tokens": 2_000_000, "access_verified": True, "tools": True, "vision": False}
    monkeypatch.setattr(campaign_demo, "source_identity", lambda **kwargs: identity)
    campaign_demo.delivery_registry(campaign).release_first()

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported route must not create runtime, load credentials or send HTTP")

    from qumus2 import app
    from qumus2.agents import openai_credentials

    monkeypatch.setattr(openai_credentials, "load_openai_credentials", forbidden)
    monkeypatch.setattr(app, "build_services", forbidden)
    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", forbidden)
    with pytest.raises(ValueError, match="[Uu]nsupported|vision"):
        asyncio.run(campaign_demo.run_cell(campaign, "m1-complete",
            initial_state=tmp_path / "initial.json", experience=tmp_path / "experience.json",
            qualification=True, qualification_only=True))
    with campaign_demo.delivery_registry(campaign).connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone()[0] == 0
    assert demo.TrialRegistry.delivery_status(campaign)["state"] == "await_owner_review"


def test_qualification_only_interrupts_same_attempt_then_resumes_exact_story_goal(
        qualification_fixture, monkeypatch):
    from qumus2 import app
    from qumus2.agents import openai_credentials
    from qumus2.agents.driver import ConversationDriver
    from qumus2.testbench.persist import save_lab_state

    fixture = qualification_fixture
    campaign = copy.deepcopy(fixture.campaign)
    campaign["campaign_path"] = str(Path(campaign["campaign_path"]).with_name("qualification-only-campaign"))
    campaign_demo.delivery_registry(campaign).release_first()
    real_build = app.build_services
    built, story_turns = [], []

    def build(path, **kwargs):
        assert kwargs["use_openai"] and kwargs["lab_process"]
        continuing = bool(built)
        services = real_build(path, use_openai=False, seed=kwargs["seed"],
                              testbench_config=kwargs["testbench_config"])
        if not continuing:
            fixture.adapter.initial_state.write_text(json.dumps(services.lab.world.evaluation_snapshot()))
            fixture.adapter.experience.write_text(json.dumps(ExperienceBook(services.store).summary()))
            save_lab_state(services.lab, services.clock, Path(path).with_suffix(".testbench.pkl"))
        runtime = OpenAIRuntime(services.gateway, kwargs["openai_credentials"],
            run_budget_usd=kwargs["run_budget_usd"], pre_send_admission=kwargs["pre_send_admission"],
            http_max_retries=kwargs["http_max_retries"])
        if continuing:
            async def story_send(text):
                story_turns.append({"text": text, "session_id": runtime.session_id,
                                    "history": await runtime._session.get_items(),
                                    "marker": services.store.get_meta("demo.story_started")})
            monkeypatch.setattr(runtime, "send", story_send)
        fixture.response_project[0] = services.project_id
        built.append(str(path))
        return replace(services, runtime=runtime,
                       driver=ConversationDriver(services.store, runtime, services.jobs))

    monkeypatch.setattr(app, "build_services", build)
    monkeypatch.setattr(openai_credentials, "load_openai_credentials", lambda *args: fixture.runtime.creds)

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "time", lambda: 10_000.0 + fixture.elapsed[0])
        return await campaign_demo.run_cell(campaign, "m1-complete",
            initial_state=fixture.adapter.initial_state, experience=fixture.adapter.experience,
            qualification=True, qualification_only=True)

    summary = asyncio.run(run())
    assert summary["error"] is None
    assert summary["checks"]["qualification_only"]
    assert len(fixture.sent) == 2
    assert len(built) == 1
    registry = demo.TrialRegistry(Path(campaign["campaign_path"]), model=MODEL,
        profiles=campaign["profiles"], limits_by_story=fixture.adapter.limits_by_story,
        campaign_contract=campaign, campaign_guard=lambda *args, **kwargs: None)
    with registry.connect() as db:
        rows = db.execute("SELECT number,status,manifest,checkpoint FROM attempt").fetchall()
    assert len(rows) == 1 and rows[0][:2] == (1, "interrupted")
    assert demo.TrialRegistry.delivery_status(campaign)["state"] == "first_cell_in_progress"
    manifest, checkpoint = json.loads(rows[0][2]), json.loads(rows[0][3])
    assert manifest["cell_id"] == "m1-complete"
    assert manifest["ledger"] == fixture.manifest["ledger"]
    assert checkpoint["artifact_sha256"]
    data = json.loads((Path(summary["export"]) / "recording.json").read_text())
    results = [e["payload"] for e in data["events"] if e["kind"] == "demo.qualification_result"]
    assert len(results) == 1 and results[0]["passed"]
    assert not any(e["kind"] == "demo.action_applied" for e in data["events"])
    assert not data["runs"]
    probe_sessions = {e["payload"]["session_id"] for e in data["events"] if e["kind"] == "agent.request"}
    assert checkpoint["session_id"] not in probe_sessions
    path = Path(manifest["workspace"]) / "qumus.db"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM meta WHERE key='demo.story_started'").fetchone() is None
    ledger_before = spend_rows(fixture)
    artifacts_before = demo.checkpoint_artifacts(manifest["workspace"])

    async def resume():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "time", lambda: 10_000.0 + fixture.elapsed[0])
        return await campaign_demo.run_cell(campaign, "m1-complete",
            initial_state=fixture.adapter.initial_state, experience=fixture.adapter.experience, resume=True)

    resumed = asyncio.run(resume())
    assert resumed["error"] is None
    assert len(built) == 2 and built[0] == built[1]
    assert len(fixture.sent) == 2
    assert spend_rows(fixture) == ledger_before
    assert len(story_turns) == 1
    goal = "\n".join(filter(None, (campaign["profiles"]["complete"].get("setup"),
                                   campaign["profiles"]["complete"]["prompt"])))
    assert story_turns[0]["text"].rsplit(" [message id: ", 1)[0] == goal
    assert story_turns[0]["session_id"] == checkpoint["session_id"]
    assert story_turns[0]["history"] == []
    assert story_turns[0]["marker"] == "true"
    with registry.connect() as db:
        assert db.execute("SELECT number,status FROM attempt").fetchall() == [(1, "failed")]
    resumed_data = json.loads((Path(resumed["export"]) / "recording.json").read_text())
    assert sum(e["kind"] == "demo.qualification_started" for e in resumed_data["events"]) == 1
    assert sum(e["kind"] == "demo.qualification_result" for e in resumed_data["events"]) == 1
    assert not any(e["kind"] == "demo.action_applied" for e in resumed_data["events"])
    assert not resumed_data["runs"]
    assert demo.checkpoint_artifacts(manifest["workspace"])["qumus.testbench.pkl"] == artifacts_before["qumus.testbench.pkl"]


@pytest.mark.parametrize("continuation", [{"retry_failed": True}, {"resume": True}])
def test_qualification_cannot_be_released_as_retry_or_resume(qualification_fixture, monkeypatch, continuation):
    from qumus2.agents import openai_credentials

    fixture = qualification_fixture

    def forbidden(*args, **kwargs):
        pytest.fail("Qualification retry/resume must be rejected before credentials or HTTP")

    monkeypatch.setattr(openai_credentials, "load_openai_credentials", forbidden)
    with pytest.raises(ValueError, match="fresh first complete-cell attempt"):
        asyncio.run(campaign_demo.run_cell(fixture.campaign, "m1-complete",
            initial_state=fixture.adapter.initial_state, experience=fixture.adapter.experience,
            qualification=True, **continuation))
    assert not fixture.sent
    assert not events(fixture, "demo.qualification_started")
    assert not spend_rows(fixture)


def test_qualification_rejects_later_attempt_before_runtime_or_spend(qualification_fixture):
    fixture = qualification_fixture
    later = {**fixture.manifest, "attempt_number": 2}
    with pytest.raises(ValueError, match="first complete cell"):
        asyncio.run(fixture.adapter.qualify(fixture.services, later))
    assert not fixture.sent
    assert not events(fixture, "demo.qualification_started")
    assert not spend_rows(fixture)


@pytest.mark.parametrize("answer", ["final", "wrong_image"])
def test_new_adapter_cannot_repeat_saved_qualification(qualification_fixture, answer):
    fixture = qualification_fixture
    fixture.states[1] = answer
    if answer == "final":
        fixture.run()
    else:
        with pytest.raises(ValueError):
            fixture.run()
    original_events = events(fixture, "demo.qualification_result")
    original_spend = spend_rows(fixture)
    reopened = campaign_demo.CampaignAdapter(fixture.campaign, MODEL,
        initial_state=fixture.adapter.initial_state, experience=fixture.adapter.experience,
        qualification=True)
    with pytest.raises(ValueError, match="first complete cell"):
        asyncio.run(reopened.qualify(fixture.services, fixture.manifest))
    assert len(fixture.sent) == 2
    assert len(events(fixture, "demo.qualification_started")) == 1
    assert events(fixture, "demo.qualification_result") == original_events
    assert spend_rows(fixture) == original_spend
    assert_frozen_lab(fixture)


def test_preflight_uses_observed_qualification_without_editing_capabilities(qualification_fixture):
    fixture = qualification_fixture
    identity = fixture.campaign["identity"]
    before = campaign_demo.preflight(fixture.campaign, identity, cell_id="m1-complete")
    assert not before["selected_cell"]["eligible"]
    assert not before["models"][MODEL]["observed_qualification"]
    fixture.run()
    after = campaign_demo.preflight(fixture.campaign, identity, cell_id="m1-complete")
    assert after["selected_cell"]["eligible"]
    assert after["models"][MODEL]["ready"]
    assert after["models"][MODEL]["observed_qualification"] == qualification_result(fixture)
    assert after["eligible_models"] == [MODEL]
    assert not after["whole_matrix_ready"]
    assert campaign_demo.qualification_plan(fixture.campaign, MODEL)["disposition"] == "not_required"
    assert all(fixture.campaign["models"][MODEL]["capabilities"].get(key) is None
               for key in campaign_demo.CAPABILITIES)
    assert_frozen_lab(fixture)


@pytest.mark.parametrize("tampered", ["prefix", "receipt"])
def test_preflight_rejects_tampered_qualification_evidence(qualification_fixture, tampered):
    fixture = qualification_fixture
    fixture.run()
    assert campaign_demo.observed_qualification(fixture.campaign, MODEL)
    event = events(fixture, "agent.request" if tampered == "prefix" else "demo.qualification_result")[0]
    payload = copy.deepcopy(event["payload"])
    if tampered == "prefix":
        payload["synthetic_tamper"] = "changed after qualification was recorded"
    else:
        payload["gateway"] = "https://different-synthetic-gateway.invalid/v1"
    with sqlite3.connect(fixture.services.store.path) as db:
        db.execute("UPDATE event SET payload_json=? WHERE id=?", (json.dumps(payload), event["id"]))
    assert campaign_demo.observed_qualification(fixture.campaign, MODEL) is None
    status = campaign_demo.preflight(fixture.campaign, fixture.campaign["identity"], cell_id="m1-complete")
    assert not status["selected_cell"]["eligible"]
    assert not status["models"][MODEL]["ready"]
    assert campaign_demo.qualification_plan(fixture.campaign, MODEL)["disposition"] == "qualification_pending_acceptance"
    assert len(fixture.sent) == 2
    assert_frozen_lab(fixture)
