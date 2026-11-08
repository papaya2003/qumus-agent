"""ReactorLab against doc-faithful fakes of the reactor services (offline, no reactor code)."""

from __future__ import annotations

import pytest

from qumus2.contract import Conflict, ContractClient
from qumus2.reactor import REACTOR_SERVICES, ReactorLab, ReactorPolicyError
from qumus2.reactor.fakes import running_reactor_fakes


@pytest.fixture
def lab():
    with running_reactor_fakes() as fakes:
        endpoints = {sid: f.base_url for sid, f in fakes.items()}
        yield ReactorLab(endpoints=endpoints, client_id="qumus-agent-test"), fakes


def test_default_ports_match_reactor_adr_0003():
    lab = ReactorLab()
    assert lab.base_url("robot") == "http://127.0.0.1:8766/"
    assert lab.base_url("stage") == "http://127.0.0.1:8767/contract/"
    assert lab.base_url("thermal") == "http://127.0.0.1:8768/"
    assert lab.base_url("furnace") == "http://127.0.0.1:8769/"
    assert lab.base_url("microscope") == "http://127.0.0.1:8770/"
    assert lab.base_url("vision") == "http://127.0.0.1:8771/"
    assert lab.base_url("orchestrator") == "http://127.0.0.1:8772/"


def test_discover_all_services(lab):
    reactor, _ = lab
    reports = reactor.discover()
    assert set(reports) == set(REACTOR_SERVICES)
    for sid, report in reports.items():
        assert report.reachable, (sid, report.error)
        assert report.description.contract == 1
        assert report.demo is True
        assert report.state.service == sid


def test_unreachable_service_is_reported_not_raised():
    reactor = ReactorLab(endpoints={"thermal": "http://127.0.0.1:9/"}, services=("thermal",))
    report = reactor.discover()["thermal"]
    assert not report.reachable and "refused" in report.error


def test_reactor_header_is_sent(lab):
    reactor, fakes = lab
    assert reactor.client("thermal").run("connect").ok
    posts = [r for r in fakes["thermal"].requests if r[0] == "POST"]
    assert posts and posts[-1][2].get("x-mg400-client") == "qumus-agent-test"


def test_safe_read_only_commands(lab):
    reactor, _ = lab
    assert reactor.client("thermal").run("shutter").ok
    vision = reactor.client("vision")
    with vision.lease_keeper():  # reactor services renew through api/renew
        assert vision.run("detect", {"camera": 1}).ok
        assert vision.run("read_labels", {"camera": 1}).ok
    microscope = reactor.client("microscope")
    with microscope.lease_keeper():
        assert microscope.run("snapshot").ok


@pytest.mark.parametrize("service,action,params", [
    ("robot", "execute", {"targets": {"x": 1, "y": 2, "z": 3, "r": 0}}),
    ("robot", "vacuum", {"operation": "suck"}),
    ("robot", "keyboard", {}),
    ("stage", "move", {"motor": "insert", "direction": "cw", "speed": 1, "steps": 10}),
    ("furnace", "apply_setpoint", {"target_c": 100, "ramp_c_per_min": 5}),
    ("furnace", "set_gas_flow", {"channel_index": 0, "flow_pct": 10}),
    ("microscope", "focus_move", {"steps": 100}),
    ("vision", "calibrate_camera", {"camera": 1}),
])
def test_policy_refuses_motion_heat_and_raw_coordinates(lab, service, action, params):
    reactor, fakes = lab
    with pytest.raises(ReactorPolicyError):
        reactor.client(service).command(action, params)
    assert not any(r[1].endswith("api/command") for r in fakes[service].requests)


def test_policy_can_be_disabled_for_owner_tools(lab):
    _, fakes = lab
    reactor = ReactorLab(endpoints={"stage": fakes["stage"].base_url}, services=("stage",),
                         enforce_policy=False)
    stage = reactor.client("stage")
    with stage.lease_keeper():
        assert stage.run("move", {"motor": "insert", "direction": "cw", "speed": 1,
                                  "steps": 10}).ok


def test_stop_all_needs_no_lease(lab):
    reactor, _ = lab
    results = reactor.stop_all()
    assert set(results) == set(REACTOR_SERVICES)
    assert all(getattr(r, "confirmed", False) for r in results.values())


def test_robot_lease_model_differs(lab):
    """Gap R3: robot control is taken by the start_sync command; heartbeat only renews."""
    reactor, _ = lab
    robot = reactor.client("robot")
    with pytest.raises(Conflict):
        robot.lease_keeper().start()


def test_robot_requires_panel_header(lab):
    _, fakes = lab
    plain = ContractClient(fakes["robot"].base_url, "q", client_header="X-MG400-Client")
    with pytest.raises(Exception) as info:
        plain.stop()
    assert getattr(info.value, "status", None) == 403


def test_capability_table(lab):
    reactor, _ = lab
    rows = reactor.capability_table()
    by_key = {(r["service"], r["action"]): r for r in rows}
    assert by_key[("microscope", "snapshot")]["agent_allowed"]
    assert by_key[("microscope", "snapshot")]["qumus_action"] == "capture"
    assert not by_key[("robot", "execute")]["agent_allowed"]
    assert "raw coordinates" in by_key[("robot", "execute")]["reason"]
    assert not by_key[("furnace", "apply_setpoint")]["agent_allowed"]
    assert "timeout_s" in by_key[("furnace", "apply_setpoint")]["reason"]


def test_orchestrator_reads_only(lab):
    reactor, _ = lab
    assert reactor.orchestrator_read("api/recipes")["recipes"][0]["id"] == "example-rehearsal"
    assert reactor.orchestrator_read("api/runs") == {"runs": []}
    with pytest.raises(ReactorPolicyError):
        reactor.orchestrator_read("api/proposals")
    with pytest.raises(ReactorPolicyError):
        reactor.orchestrator_read("api/runs/../proposals")
