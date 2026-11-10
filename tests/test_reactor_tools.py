"""Brain reactor tools (reactor Lab Console vocabulary) against the fake console."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from qumus2.agents.tooling import ToolContext, default_registry
from qumus2.reactor import tools as rt
from qumus2.reactor.console import ReactorConsole
from qumus2.reactor.console_fake import FAKE_JPEG, FakeConsole
from qumus2.runtime.store import RunStore
from qumus2.stations import config as stations_config


@pytest.fixture
def fake():
    with FakeConsole() as console:
        yield console


def _ctx(fake, tmp_path, *, kind="sim", allow=None, store=None):
    console = ReactorConsole(fake.base_url, allow_actuation=(kind == "sim") if allow is None else allow,
                             sleep=lambda s: None)
    station = rt.ReactorStation(console, kind, None, tmp_path, rt.SIM_SLOTS)
    store = store or RunStore(":memory:")
    project = store.create_project("t")
    return ToolContext(store=store, jobs=SimpleNamespace(), agent="reactor", project_id=project,
                       extra={"stations": {"reactor": station}})


def run(coro):
    return asyncio.run(coro)


def test_tools_registered_for_the_reactor_agent():
    names = {t.name for t in default_registry().for_agent("reactor")}
    assert set(rt.REACTOR_TOOLS) <= names


def test_sim_round_trip(fake, tmp_path):
    ctx = _ctx(fake, tmp_path)
    ok = lambda r: r.status == "ok"  # noqa: E731
    r = run(rt.reactor_handover_in(rt.HandoverArgs(chip_id="chip_r1", slot="A1"), ctx))
    assert ok(r), r.summary
    assert fake.slots["A1"] == "chip_r1"
    assert ctx.store.get_sample("chip_r1")["location"] == "reactor:tray:A1"
    r = run(rt.reactor_load(rt.LoadArgs(chip_id="chip_r1", from_slot="A1"), ctx))
    assert ok(r), r.summary
    assert fake.crucible == "chip_r1"
    assert ctx.store.get_sample("chip_r1")["location"] == "reactor:crucible"
    r = run(rt.reactor_heat(rt.HeatArgs(chip_id="chip_r1", steps=[
        {"temperature_c": 300, "ramp_rate_c_per_min": 50, "hold_minutes": 1,
         "gas": {"0": 20}}], end_gas={"0": 10}, end_gas_until_c=80, record=True), ctx))
    assert ok(r), r.summary
    sent = next(b for p, _, b in fake.posts if p == "api/console/command/furnace")
    assert sent["action"] == "start_sequence"
    assert sent["params"]["steps"][0]["gas"] == {"0": 20} and sent["params"]["end_gas_until_c"] == 80
    assert r.data["recording"]["outcome"] == "ok"
    r = run(rt.reactor_snapshot(rt.ChipArgs(chip_id="chip_r1"), ctx))
    assert ok(r) and open(r.images[0], "rb").read() == FAKE_JPEG
    r = run(rt.reactor_unload(rt.UnloadArgs(chip_id="chip_r1", to_slot="A2"), ctx))
    assert ok(r), r.summary
    assert fake.slots["A2"] == "chip_r1" and fake.crucible is None
    r = run(rt.reactor_handover_out(rt.HandoverArgs(chip_id="chip_r1", slot="A2"), ctx))
    assert ok(r), r.summary
    assert fake.slots["A2"] is None
    assert ctx.store.get_sample("chip_r1")["location"] == "person"
    flows = [row["flow"] for row in ctx.store.list_runs()]
    assert sorted(flows) == sorted(["reactor_handover_in", "reactor_load", "reactor_heat",
                                    "reactor_snapshot", "reactor_unload", "reactor_handover_out"])
    assert all(row["status"] == "succeeded" for row in ctx.store.list_runs())
    status = run(rt.reactor_status(rt.NoArgs(), ctx))
    assert status.status == "ok" and status.data["devices"]["furnace"]["sequence_running"] is False


def test_load_needs_the_chip_in_that_slot(fake, tmp_path):
    ctx = _ctx(fake, tmp_path)
    r = run(rt.reactor_load(rt.LoadArgs(chip_id="nochip", from_slot="A1"), ctx))
    assert r.status == "error" and "not reactor:tray:A1" in r.summary
    assert "api/runs" not in [p for p, _, _ in fake.posts]


def test_load_refused_while_hot(fake, tmp_path):
    ctx = _ctx(fake, tmp_path)
    run(rt.reactor_handover_in(rt.HandoverArgs(chip_id="c1", slot="A1"), ctx))
    fake.states["furnace"]["pv_c"] = 250.0
    r = run(rt.reactor_load(rt.LoadArgs(chip_id="c1", from_slot="A1"), ctx))
    assert r.status == "error" and "250" in r.summary


def test_failed_recipe_marks_the_chip_unknown(fake, tmp_path):
    ctx = _ctx(fake, tmp_path)
    ctx.store.upsert_sample("c1", "chip", location="reactor:tray:A1")  # ledger says A1, tray empty
    r = run(rt.reactor_load(rt.LoadArgs(chip_id="c1", from_slot="A1"), ctx))
    assert r.status == "needs_attention"
    assert ctx.store.get_sample("c1")["location"] == "reactor:unknown"


def test_unknown_slot_is_refused(fake, tmp_path):
    ctx = _ctx(fake, tmp_path)
    r = run(rt.reactor_handover_in(rt.HandoverArgs(chip_id="c1", slot="Z9"), ctx))
    assert r.status == "error" and "unknown tray slot" in r.summary


def test_gas_needs_an_end_plan():
    with pytest.raises(ValueError):
        rt.HeatArgs(chip_id="c", steps=[{"temperature_c": 300, "ramp_rate_c_per_min": 10,
                                         "hold_minutes": 1, "gas": {"0": 20}}])
    with pytest.raises(ValueError):
        rt.HeatArgs(chip_id="c", steps=[{"temperature_c": 300, "ramp_rate_c_per_min": 10,
                                         "hold_minutes": 1, "gas": {"0": 120}}],
                    end_gas={"0": 0}, end_gas_until_c=50)


def test_real_reactor_refuses_motion_and_heat_before_sending(fake, tmp_path):
    ctx = _ctx(fake, tmp_path, kind="real")
    ctx.store.upsert_sample("c1", "chip", location="reactor:tray:A1")
    r = run(rt.reactor_load(rt.LoadArgs(chip_id="c1", from_slot="A1"), ctx))
    assert r.status == "denied"
    ctx.store.upsert_sample("c1", "chip", location="reactor:crucible")
    r = run(rt.reactor_heat(rt.HeatArgs(chip_id="c1", steps=[
        {"temperature_c": 300, "ramp_rate_c_per_min": 10, "hold_minutes": 1}]), ctx))
    assert r.status == "denied"
    r = run(rt.reactor_record(rt.RecordArgs(chip_id="c1", seconds=10), ctx))
    assert r.status == "denied"  # recording also waits for Jialei's OK
    posted = [p for p, _, _ in fake.posts]
    assert "api/runs" not in posted and "api/console/command/furnace" not in posted
    assert not any(p.startswith("api/console/control/") for p in posted)
    # reads, snapshot and stop still work
    assert run(rt.reactor_status(rt.NoArgs(), ctx)).status == "ok"
    assert run(rt.reactor_snapshot(rt.ChipArgs(chip_id="c1"), ctx)).status == "ok"
    assert run(rt.reactor_stop(rt.NoArgs(), ctx)).status == "ok"
    assert "furnace" in fake.stops


def test_real_handover_waits_for_the_person(fake, tmp_path):
    ctx = _ctx(fake, tmp_path, kind="real")
    args = rt.HandoverArgs(chip_id="c9", slot="A3")
    r = run(rt.reactor_handover_in(args, ctx))
    assert r.status == "pending"
    run_id = r.data["run_id"]
    assert ctx.store.get_run(run_id)["status"] == "waiting_human"
    notes = [e for e in ctx.store.events_after(0) if e.kind == "notification"]
    assert notes and "A3" in notes[-1].payload["text"]
    confirm = rt.HandoverArgs(chip_id="c9", slot="A3", confirmed=True)
    assert run(rt.reactor_handover_in(confirm, ctx)).status == "pending"  # no reply yet
    ctx.store.add_instruction("好了")
    r = run(rt.reactor_handover_in(confirm, ctx))
    assert r.status == "ok" and "not checked by vision" in r.summary
    assert ctx.store.get_sample("c9")["location"] == "reactor:tray:A3"
    assert ctx.store.get_run(run_id)["status"] == "succeeded"
    assert fake.posts == []  # nothing was sent to the real reactor


def test_stations_yaml_console_adapter(tmp_path):
    st = stations_config.parse({"records_from": "stacking", "modules": {
        "stacking": {"url": "http://127.0.0.1:8782/", "kind": "sim"},
        "reactor": {"url": "http://127.0.0.1:8783/lab/", "kind": "sim", "adapter": "console",
                    "session": "qumus"}}})
    assert st.contract_urls == {"stacking": "http://127.0.0.1:8782/"}
    assert set(st.consoles) == {"reactor"}
    from qumus2.app import console_stations

    station = console_stations(st)["reactor"]
    assert station.sim and station.console.allow_actuation and station.slots == rt.SIM_SLOTS
    real = stations_config.parse({"records_from": "stacking", "modules": {
        "stacking": {"url": "http://127.0.0.1:8782/"},
        "reactor": {"url": "http://127.0.0.1:18772/lab/", "kind": "real", "adapter": "console",
                    "microscope_url": "http://127.0.0.1:18770/"}}})
    station = console_stations(real)["reactor"]
    assert not station.console.allow_actuation and station.microscope_url == "http://127.0.0.1:18770/"
    with pytest.raises(ValueError):
        stations_config.parse({"records_from": "reactor", "modules": {
            "reactor": {"url": "http://127.0.0.1:8783/lab/", "adapter": "console"}}})
    with pytest.raises(ValueError):
        stations_config.parse({"modules": {"stacking": {"url": "http://x/", "adapter": "bridge"}}})


def test_not_ready_reactor_refuses_before_sending(fake, tmp_path):
    """The real reactor 11-10: robot and stage offline, stage automation unconfigured,
    no calibrated camera, gas controller offline. Tools say why and send nothing."""
    ctx = _ctx(fake, tmp_path)
    run(rt.reactor_handover_in(rt.HandoverArgs(chip_id="chip_r1", slot="A1"), ctx))
    fake.connected.update(robot=False, stage=False)
    fake.states["stage"]["automation"] = {"configured": False, "error": "stage.toml missing"}
    fake.states["vision"]["calibrated_cameras"] = []
    fake.states["furnace"]["flow"]["connected"] = False
    posts = len(fake.posts)
    r = run(rt.reactor_load(rt.LoadArgs(chip_id="chip_r1", from_slot="A1"), ctx))
    assert r.status == "error" and "robot is not connected" in r.summary
    assert "stage automation is not configured (stage.toml missing)" in r.data["not_ready"]
    assert len(fake.posts) == posts and fake.crucible is None
    status = run(rt.reactor_status(rt.NoArgs(), ctx))
    assert status.status == "ok" and "not ready" in status.summary
    assert status.data["ready"]["heat"] == []
    assert "vision has no calibrated camera" in status.data["ready"]["slot_check"]
    assert status.data["ready"]["heat_with_gas"] == ["the gas flow controller is not connected"]
    assert status.data["devices"]["robot"]["connected"] is False
    assert status.data["devices"]["furnace"]["gas_controller_connected"] is False
    fake.crucible = "chip_r1"
    rt._place(ctx, "chip_r1", rt.CRUCIBLE)
    r = run(rt.reactor_heat(rt.HeatArgs(chip_id="chip_r1", steps=[
        {"temperature_c": 300, "ramp_rate_c_per_min": 50, "hold_minutes": 1, "gas": {"0": 20}}],
        end_gas={"0": 5}, end_gas_until_c=80), ctx))
    assert r.status == "error" and "gas flow controller" in r.summary
    assert not any(p == "api/console/command/furnace" for p, _, _ in fake.posts)


def test_heat_limits_follow_the_real_furnace():
    with pytest.raises(ValueError):
        rt.HeatStep(temperature_c=1200, ramp_rate_c_per_min=10, hold_minutes=1)
    with pytest.raises(ValueError):
        rt.HeatStep(temperature_c=500, ramp_rate_c_per_min=150, hold_minutes=1)
