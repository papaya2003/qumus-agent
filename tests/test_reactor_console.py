"""ReactorConsole (reactor Lab Console agent API, ADR 0010) against a fake console."""

from __future__ import annotations

import json

import pytest

from qumus2.contract.errors import Conflict, OutcomeUnknown, TransportError
from qumus2.reactor.adapter import ReactorPolicyError
from qumus2.reactor.console import (
    DEVICES,
    VPN_CONSOLE_URL,
    ReactorConsole,
    main,
)
from qumus2.reactor.console_fake import FakeConsole


@pytest.fixture
def fake():
    with FakeConsole() as console:
        yield console


def test_default_url_is_the_vpn_proxy():
    assert VPN_CONSOLE_URL == "http://100.64.0.1:8080/lab/"
    assert ReactorConsole().page == "agent-qumus"


def test_probe_sends_gets_only(fake):
    report = ReactorConsole(fake.base_url).probe()
    assert report["reachable"] is True
    assert report["console"] == {"commit": "fake", "demo": True}
    assert set(report["devices"]) == set(DEVICES)
    assert "snapshot" in report["devices"]["microscope"]["commands"]
    assert report["devices"]["robot"]["holder"] is None
    assert fake.posts == []  # nothing but reads reached the console


def test_probe_unreachable_reports_error():
    report = ReactorConsole("http://127.0.0.1:9/lab/", timeout_s=0.5).probe()
    assert report["reachable"] is False
    assert "error" in report


def test_reads_raise_transport_error_when_down():
    with pytest.raises(TransportError):
        ReactorConsole("http://127.0.0.1:9/lab/", timeout_s=0.5).overview()


def test_unknown_device_rejected_locally(fake):
    with pytest.raises(ValueError):
        ReactorConsole(fake.base_url).device_state("laser")


def test_motion_and_heat_refused_before_sending(fake):
    console = ReactorConsole(fake.base_url)
    with pytest.raises(ReactorPolicyError):
        console.take("robot")
    with pytest.raises(ReactorPolicyError):
        console.command("furnace", "apply_setpoint", {"target_c": 300})
    with pytest.raises(ReactorPolicyError):
        console.command("microscope", "autofocus", {"mode": "full"})
    assert fake.posts == []


def test_read_only_command_flow(fake):
    console = ReactorConsole(fake.base_url)
    console.take("microscope")
    result = console.command("microscope", "snapshot")
    assert result.ok and result.outcome == "ok"
    assert result.command_id is not None
    path, page, body = fake.posts[-1]
    assert path == "api/console/command/microscope"
    assert page == "agent-qumus"
    assert body == {"action": "snapshot", "params": {}}
    assert console.release_session() == {"microscope": "released"}


def test_command_without_control_is_conflict(fake):
    console = ReactorConsole(fake.base_url)
    with pytest.raises(Conflict):
        console.command("vision", "detect", {"camera": "camera1"})


def test_lost_command_reply_is_outcome_unknown_and_not_retried(fake):
    console = ReactorConsole(fake.base_url)
    console.take("vision")
    fake.drop_next_command = True
    with pytest.raises(OutcomeUnknown):
        console.command("vision", "detect", {"camera": "camera1"})
    sent = [p for p, _, _ in fake.posts if p.startswith("api/console/command/")]
    assert sent == ["api/console/command/vision"]


def test_owner_can_allow_actuation(fake):
    console = ReactorConsole(fake.base_url, allow_actuation=True)
    console.take("stage")
    assert console.command("stage", "goto", {"motor": 1, "name": "load"}).ok


def test_stops_need_no_control(fake):
    console = ReactorConsole(fake.base_url)
    assert console.stop("furnace") == "confirmed"
    results = console.stop_all_motion()
    assert results == {"console": "accepted", "robot": "confirmed",
                       "stage": "confirmed", "microscope": "confirmed"}
    assert fake.stops == ["furnace", "dashboard:motion", "robot", "stage", "microscope"]


def test_stop_unreachable():
    assert ReactorConsole("http://127.0.0.1:9/lab/", timeout_s=0.5).stop("robot") == "unreachable"


def test_events_filter(fake):
    console = ReactorConsole(fake.base_url)
    console.stop("robot")
    console.stop("stage")
    assert [e["service"] for e in console.events()] == ["robot", "stage"]
    assert [e["service"] for e in console.events(device="stage")] == ["stage"]
    rows = console.events()
    assert [e["seq"] for e in rows] == [1, 1]  # seq is per service on the real console
    assert console.events(since_ts=rows[-1]["ts_unix"]) == []
    assert [e["service"] for e in console.events(since_ts=rows[0]["ts_unix"])] == ["stage"]


def test_cli_probe(fake, capsys):
    assert main(["probe", "--url", fake.base_url]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reachable"] is True
    assert fake.posts == []


# --------------------------------------------------------------- recipe runs
def test_recipe_runs_borrow_the_sessions_devices(fake):
    fake.slots["A1"] = "chip-7"
    console = ReactorConsole(fake.base_url, allow_actuation=True, sleep=lambda s: None)
    result = console.run_recipe("crucible-load", {"from_slot": "A1"})
    assert result.ok and result.status == "completed"
    assert fake.crucible == "chip-7" and fake.slots["A1"] is None
    assert {d for d, h in fake.holders.items() if h == "agent-qumus"} == {"robot", "stage", "furnace"}
    paths = [p for p, _, _ in fake.posts]
    assert paths.index("api/heartbeat") < paths.index("api/runs") < paths.index("api/release")
    run_post = next(b for p, _, b in fake.posts if p == "api/runs")
    assert run_post == {"recipe": "crucible-load", "params": {"from_slot": "A1"}, "borrow": True}
    assert fake.orchestrator_holder is None  # orchestrator lease released at the end


def test_slot_state_result_is_the_last_step(fake):
    fake.slots["A2"] = "chip-1"
    console = ReactorConsole(fake.base_url, allow_actuation=True, sleep=lambda s: None)
    assert console.run_recipe("slot-state", {"slot": "A2"}).result["state"] == "present"
    assert console.run_recipe("slot-state", {"slot": "A3"}).result["state"] == "empty"


def test_failed_recipe_reports_the_detail(fake):
    console = ReactorConsole(fake.base_url, allow_actuation=True, sleep=lambda s: None)
    result = console.run_recipe("crucible-load", {"from_slot": "A1"})  # slot empty
    assert result.outcome == "failed" and result.status == "aborted"
    assert "No chip" in result.message
    assert result.steps[0]["error"]


def test_held_recipe_is_aborted(fake):
    fake.hold_recipe = "slot-state"
    console = ReactorConsole(fake.base_url, allow_actuation=True, sleep=lambda s: None)
    result = console.run_recipe("slot-state", {"slot": "A1"})
    assert result.outcome == "failed" and result.status == "aborted"
    assert any(p.endswith("/abort") for p, _, _ in fake.posts)


def test_moving_recipes_are_refused_without_actuation(fake):
    console = ReactorConsole(fake.base_url)
    for recipe in ("crucible-load", "crucible-unload", "slot-state", "microscope-record"):
        with pytest.raises(ReactorPolicyError):
            console.run_recipe(recipe, {})
    assert fake.posts == []


def test_unknown_recipe_is_refused_before_sending(fake):
    with pytest.raises(ValueError):
        ReactorConsole(fake.base_url, allow_actuation=True).run_recipe("tray-transfer", {})
    assert fake.posts == []


def test_recipe_refused_when_another_page_holds_a_device(fake):
    fake.holders["robot"] = "page-owner"
    console = ReactorConsole(fake.base_url, allow_actuation=True, sleep=lambda s: None)
    result = console.run_recipe("crucible-load", {"from_slot": "A1"})
    assert result.outcome == "refused" and "another" in result.message.lower()
    assert "api/runs" not in [p for p, _, _ in fake.posts]


def test_furnace_sequence_and_stop_all(fake):
    console = ReactorConsole(fake.base_url, allow_actuation=True, sleep=lambda s: None)
    console.take("furnace")
    steps = [{"temperature_c": 300, "ramp_rate_c_per_min": 20, "hold_minutes": 1}]
    assert console.command("furnace", "start_sequence", {"steps": steps}).outcome == "ok"
    state = console.furnace_wait(timeout_s=10)
    assert state["sequence"]["running"] is False and state["sequence"]["phase"] == "complete"
    results = console.stop_all()
    assert results["furnace"] == "confirmed" and results["console"] == "accepted"
    assert {"robot", "stage", "microscope", "furnace"} <= set(fake.stops)


def test_microscope_frame_from_the_sim_base(fake):
    from qumus2.reactor.console_fake import FAKE_JPEG

    assert ReactorConsole(fake.base_url).microscope_frame() == FAKE_JPEG
    with pytest.raises(TransportError):
        ReactorConsole(fake.base_url, timeout_s=0.5).microscope_frame("http://127.0.0.1:9/")
