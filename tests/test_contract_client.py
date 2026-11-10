"""Contract v1 client against the in-process fake service (no lab, no network)."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from qumus2.contract import (
    BadRequest,
    CommandFailed,
    CommandTimeout,
    Conflict,
    ContractClient,
    ContractVersionError,
    Forbidden,
    LeaseLost,
    NotFound,
    OutcomeUnknown,
    TransportError,
)
from qumus2.contract.fake_server import FakeCommand, FakeService

MOVE = FakeCommand(
    params={"type": "object", "properties": {"point": {"type": "string",
                                                         "enum": ["home", "middle"]}},
            "required": ["point"], "additionalProperties": False},
    motion=True, duration_s=0.3,
    effect=lambda p, s: {"arm": {"point": p["point"]}},
)
COMMANDS = {
    "read_all": FakeCommand(duration_s=0.02),
    "go_to_point": MOVE,
    "jog": FakeCommand(params={"type": "object", "properties": {
        "mm": {"type": "number", "minimum": -0.5, "maximum": 0.5}},
        "required": ["mm"], "additionalProperties": False}, motion=True, local_only=True),
    "capture": FakeCommand(any_client=True, duration_s=0.02),
    "broken": FakeCommand(result="failed", message="sensor said no", duration_s=0.02),
    "slow": FakeCommand(duration_s=5.0, motion=True),
}


@pytest.fixture(params=["adr", "qumus"])
def svc(request):
    with FakeService("stacking", commands=COMMANDS, dialect=request.param,
                     device_fields={"arm": {"point": "home"}}) as s:
        yield s


def client_for(svc: FakeService, cid: str = "agent-test", **kw) -> ContractClient:
    return ContractClient(svc.base_url, cid, **kw)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_describe_state_events_both_dialects(svc):
    c = client_for(svc)
    d = c.describe()
    assert d.service == "stacking" and d.contract == 1 and d.demo
    assert set(d.commands) == set(COMMANDS)
    assert d.commands["go_to_point"].motion is True
    assert d.commands["go_to_point"].required_params == ["point"]
    assert d.commands["jog"].local_only
    assert d.commands["capture"].any_client
    st = c.state()
    assert st.service == "stacking" and not st.busy and not st.locked
    arm = st.devices.get("arm") or st.extras.get("arm")
    assert arm == {"point": "home"}
    page = c.events(0)
    assert page.events == [] and page.next == 0


def test_motion_needs_lease_then_runs(svc):
    c = client_for(svc)
    with pytest.raises(Conflict):
        c.command("go_to_point", {"point": "middle"})
    with c.lease_keeper():
        lc = c.run("go_to_point", {"point": "middle"}, timeout_s=5)
        assert lc.ok and lc.action == "go_to_point"
    st = c.state()
    arm = st.devices.get("arm") or st.extras.get("arm")
    assert arm == {"point": "middle"}
    assert st.lease.holder is None  # released on exit
    msgs = [e.message for e in c.events(0).events]
    assert "lease acquired" in msgs and "command ok" in msgs and "lease released" in msgs


def test_header_and_body_shape(svc):
    c = client_for(svc)
    with c.lease_keeper():
        c.run("go_to_point", {"point": "home"})
    method, path, headers, raw = [r for r in svc.requests if r[1].endswith("api/command")][0]
    assert headers.get("x-qumus-client") == "agent-test"
    import json
    body = json.loads(raw)
    if svc.dialect == "qumus":
        assert body == {"action": "go_to_point", "params": {"point": "home"}}
    else:
        assert body == {"action": "go_to_point", "point": "home"}


def test_any_client_without_lease(svc):
    assert client_for(svc).run("capture").ok


def test_unknown_action_refused_locally(svc):
    c = client_for(svc)
    with pytest.raises(BadRequest):
        c.command("teleport")
    assert not any(r[1].endswith("api/command") for r in svc.requests)


def test_bad_params_mapped_to_bad_request(svc):
    c = client_for(svc)
    with c.lease_keeper():
        with pytest.raises(BadRequest) as info:
            c.command("go_to_point", {"point": "moon"})
    assert info.value.status in (400, 422)


def test_failed_result_raises(svc):
    c = client_for(svc)
    with c.lease_keeper():
        with pytest.raises(CommandFailed) as info:
            c.run("broken")
    assert info.value.result == "failed" and "sensor said no" in str(info.value)


def test_busy_conflict_and_stop(svc):
    c = client_for(svc)
    with c.lease_keeper():
        receipt = c.command("slow")
        with pytest.raises(Conflict):
            c.command("read_all")
        other = client_for(svc, "someone-else")
        result = other.stop()  # any client, no lease
        assert result.confirmed
        lc = c.wait_for_command(receipt, raise_on_failure=False, timeout_s=2)
        assert lc.result == "stopped"


def test_second_client_cannot_take_lease(svc):
    a, b = client_for(svc, "a"), client_for(svc, "b")
    with a.lease_keeper():
        with pytest.raises(Conflict):
            b.heartbeat()
        with pytest.raises(Conflict):
            b.lease_keeper().start()


def test_wait_timeout_does_not_stop(svc):
    c = client_for(svc)
    with c.lease_keeper():
        receipt = c.command("slow")
        with pytest.raises(CommandTimeout):
            c.wait_for_command(receipt, timeout_s=0.2)
        assert c.state().busy  # still running: the client never stops by itself
        c.stop()


def test_lease_expiry_stops_motion_and_keeper_reports_loss(svc):
    svc.lease_seconds = 0.6
    c = client_for(svc)
    lost = []
    keeper = c.lease_keeper(on_lost=lost.append)
    keeper.start()
    receipt = c.command("slow")
    # Simulate a stalled agent: the keeper thread stops renewing.
    keeper._stop.set()
    keeper._thread.join()
    time.sleep(1.0)
    lc = c.wait_for_command(receipt, raise_on_failure=False, timeout_s=2)
    assert lc.result == "stopped"
    assert c.state().lease.holder is None
    keeper.release_on_exit = False


def test_keeper_never_reacquires_after_loss(svc):
    svc.lease_seconds = 5
    c = client_for(svc)
    lost = threading.Event()
    keeper = c.lease_keeper(interval_s=0.05, on_lost=lambda e: lost.set())
    keeper.start()
    # Another client steals the lease after an expiry; the keeper must just report it.
    with svc.lock:
        svc.holder = "intruder"
    assert lost.wait(2)
    assert keeper.lost.is_set()
    with pytest.raises(LeaseLost):
        keeper.check()
    keeper.stop()
    assert svc.holder == "intruder"


def test_unknown_outcome_locks_and_is_not_retried(svc):
    c = client_for(svc)
    with c.lease_keeper():
        svc.drop_next_command_reply()
        with pytest.raises(OutcomeUnknown):
            c.command("go_to_point", {"point": "middle"})
        posts = [r for r in svc.requests if r[1].endswith("api/command")]
        assert len(posts) == 1  # sent once, never resent
        st = c.state()
        assert st.locked and st.fault["code"] == "unknown_outcome"
        with pytest.raises(Conflict):
            c.command("read_all")
        assert c.stop().stop == "unconfirmed"


def test_local_only_refused_for_remote_host(svc):
    c = ContractClient(svc.base_url.replace("127.0.0.1", "lab-pc.example"), "x")
    c._description = client_for(svc).describe()
    with pytest.raises(Forbidden):
        c.command("jog", {"mm": 0.1})


def test_local_only_server_side_403():
    with FakeService("exfoliation", commands=COMMANDS, treat_clients_as_remote=True) as s:
        c = client_for(s)
        with c.lease_keeper():
            with pytest.raises(Forbidden):
                c.command("jog", {"mm": 0.1})


def test_guard_can_refuse(svc):
    def guard(action, params, spec):
        if spec.motion:
            raise Forbidden("agent may not move this")
    c = client_for(svc, guard=guard)
    with pytest.raises(Forbidden):
        c.command("go_to_point", {"point": "home"})
    assert c.run("capture").ok


def test_contract_version_refused():
    with FakeService("future", commands=COMMANDS) as s:
        original = s.describe
        s.describe = lambda: {**original(), "contract": 2}
        with pytest.raises(ContractVersionError):
            client_for(s).describe()


def test_refused_connection_is_transport_error_not_unknown():
    c = ContractClient(f"http://127.0.0.1:{free_port()}/", "x")
    with pytest.raises(TransportError) as info:
        c.state()
    assert not isinstance(info.value, OutcomeUnknown)
    c._description = None
    with pytest.raises(TransportError):
        c.describe()


def test_reactor_style_header_and_robot_quirks():
    with FakeService("robot", commands=COMMANDS, client_header="X-MG400-Client",
                     advertise_client_header=False, required_headers={"X-MG400-Panel": "1"},
                     reply_style="200-state") as s:
        bare = ContractClient(s.base_url, "q", extra_headers={"X-MG400-Panel": "1"})
        with pytest.raises(BadRequest):  # wrong client header name -> 400 from the service
            bare.heartbeat()
        c = ContractClient(s.base_url, "q", client_header="X-MG400-Client")
        with pytest.raises(Forbidden):  # missing X-MG400-Panel
            c.heartbeat()
        c = ContractClient(s.base_url, "q", client_header="X-MG400-Client",
                           extra_headers={"X-MG400-Panel": "1"})
        with c.lease_keeper():
            receipt = c.command("go_to_point", {"point": "middle"})
            assert receipt.command_id is None
            assert c.wait_for_command(receipt).ok  # matched by action + client + time


def test_renew_missing_is_not_found_and_keeper_uses_heartbeat():
    with FakeService("old", commands=COMMANDS, renew_route=False) as s:
        c = client_for(s)
        with pytest.raises(NotFound):
            c.renew()
        with c.lease_keeper(interval_s=0.05) as k:
            time.sleep(0.3)
            assert k.renewals >= 2 and not k.lost.is_set()


def test_base_path_prefix():
    with FakeService("stage", commands=COMMANDS, base_path="/contract/") as s:
        assert s.base_url.endswith("/contract/")
        assert client_for(s).describe().service == "stage"


def test_event_cursor_detects_service_restart(svc):
    c = client_for(svc)
    c.run("capture")
    page = c.events(0)
    assert page.next > 0 and not page.restarted
    with svc.lock:  # a restarted service renumbers from 0
        svc.events.clear()
        svc.seq = 0
    if svc.dialect == "qumus":
        pytest.skip("bare-list events carry no cursor")
    later = c.events(page.next)
    assert later.restarted and later.next == 0


def test_keeper_rides_out_slow_renewals_but_not_a_dead_service(svc):
    svc.lease_seconds = 1.5
    c = client_for(svc)
    keeper = c.lease_keeper(interval_s=0.1)
    keeper.start()
    real, fails = keeper._renew, {"n": 3}

    def flaky():
        if fails["n"] > 0:
            fails["n"] -= 1
            raise TransportError("timed out")
        return real()

    keeper._renew = flaky
    time.sleep(0.8)
    assert not keeper.lost.is_set() and keeper.missed == 3 and keeper.renewals > 0

    def dead():
        raise TransportError("timed out")

    keeper._renew = dead
    assert keeper.lost.wait(3)
    with pytest.raises(LeaseLost):
        keeper.check()
    keeper.release_on_exit = False


def test_keeper_refusal_is_final_at_once(svc):
    svc.lease_seconds = 5
    c = client_for(svc)
    keeper = c.lease_keeper(interval_s=0.05)
    keeper.start()

    def refused():
        raise Conflict("lease held by intruder", status=409)

    keeper._renew = refused
    assert keeper.lost.wait(0.5) and keeper.missed == 0
    keeper.release_on_exit = False
