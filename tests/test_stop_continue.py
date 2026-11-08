"""Stop and Continue (UI buttons; runtime/jobs.py, agents/driver.py).

Stop at many device calls of every flow, then Continue: the job must end
where an uninterrupted reversible run ends. Transfer Steps have explicit Stop
boundaries: interrupted unsafe progress requires review. Heater targets must be
restored before a compatible next safe action; complete physical transfer is
verified separately by the named physical regression."""

from __future__ import annotations

import asyncio

import pytest

from qumus2.flows.bench import positions
from tests.test_focus_evidence import focus_context  # noqa: F401
from tests.test_workflow_consistency import admitted  # noqa: F401

PLACE = ("place_chip_on_transfer_stage", {"chip_id": "chip_001"})
EXFOLIATE = ("exfoliate_chips", {"material": "Gr"})
SEARCH = ("search_chip", {})


@pytest.mark.parametrize('already_reported', [False, True])
def test_driver_keeps_one_verified_terminal_error(already_reported):
    from qumus2.agents.driver import ConversationDriver
    from qumus2.agents.runtime import ReportedAgentError
    from qumus2.runtime.store import RunStore

    store = RunStore(':memory:')
    driver = ConversationDriver(store, None, None)

    async def fail(text):
        event_id = store.emit('agent.error', {'error': 'connection failed'}, agent='orchestrator') if already_reported else 999
        raise ReportedAgentError('connection failed', event_id)

    with pytest.raises(ReportedAgentError):
        asyncio.run(driver._run(fail, 'test'))
    errors = [e for e in store.events_after() if e.kind == 'agent.error']
    assert len(errors) == 1
    assert errors[0].payload['error'] == 'connection failed'


def run(s, flow, params, timeout=120):
    return s.jobs.wait(s.jobs.submit(flow, params), timeout)


def events(s, kind):
    return [e for e in s.store.events_after(0, limit=1_000_000) if e.kind == kind]


def stop_at(s, k):
    """Press Stop just before the k-th device call of a job from now."""
    faults, real = s.lab.faults, s.lab.faults.before_call
    start, fired = faults.calls, []

    def before_call(device_id):
        real(device_id)
        if not fired and faults.calls - start >= k:
            fired.append(device_id)
            s.jobs.stop_all()

    faults.before_call = before_call
    return fired


def end_state(s):
    """What a run leaves behind, comparable between two services."""
    w = s.lab.world
    samples = sorted((x["id"], x["kind"], x["location"], x["status"])
                     for x in s.store.list_samples() if x["kind"] != "tape")
    flakes = {o.id: len(o.flakes) for o in w.objects.values()}
    return samples, flakes


def consistent(s):
    w = s.lab.world
    for o in w.objects.values():
        assert o.held_by is None or o.held_by == "transfer_arm", f"{o.id} held by {o.held_by}"
    for sample in s.store.list_samples():
        if sample["kind"] in ("chip", "carrier") and sample["location"] in positions(s.lab.bench):
            p = positions(s.lab.bench)[sample["location"]]
            o = w.objects[sample["id"]]
            assert abs(o.pose.x - p["x"]) < 1.0 and abs(o.pose.y - p["y"]) < 1.0, sample
    assert not w.tape.applied
    assert not events(s, "safety.violation")


def calls_of(make_services, flow, params, before=None):
    s = make_services()
    if before:
        assert run(s, *before).status == "succeeded"
    c0 = s.lab.faults.calls
    assert run(s, flow, params).status == "succeeded"
    return s.lab.faults.calls - c0, end_state(s)


def stop_and_continue(make_services, flow, params, k, before=None):
    s = make_services()
    if before:
        assert run(s, *before).status == "succeeded"
    fired = stop_at(s, k)
    rid = s.jobs.submit(flow, params)
    st = s.jobs.wait(rid, 120)
    if not fired or st.status == "succeeded":  # the stop came after the job's last check
        return s, st, None
    assert st.status == "paused", (k, st.status, s.store.get_run(rid)["meta"])
    paused = s.store.get_run(rid)["meta"]["paused"]
    for h in paused["heaters"]:
        assert s.lab.devices.get(h).get_target() is None, f"{h} still on while paused"
    (d,) = s.jobs.resume_all()
    assert d["action"] == "resume", d
    return s, s.jobs.wait(rid, 120), paused


@pytest.mark.parametrize("flow,params,before,stride", [
    (*PLACE, None, 1),          # every call
    (*EXFOLIATE, None, 3),
    (*SEARCH, EXFOLIATE, 37),  # ~1800 calls, mostly autofocus and scan
])
def test_stop_anywhere_then_continue_ends_like_no_stop(make_services, flow, params, before,
                                                       stride):
    n, want = calls_of(make_services, flow, params, before)
    paused_in = set()
    for k in range(1, n, stride):
        s, st, paused = stop_and_continue(make_services, flow, params, k, before)
        assert st.status == "succeeded", (k, [x for x in st.steps if x["status"] != "done"])
        consistent(s)
        assert end_state(s) == want, k
        if paused:
            paused_in.add(paused["step"])
    assert len(paused_in) >= 2  # really stopped inside the flow, more than once


def test_stop_never_pauses_inside_a_step_that_cannot_be_resumed(make_services):
    """Heating with the tape on (safe_to_resume = False) runs to its end first."""
    params = {"material": "Gr", "target_temp_c": 70}
    n, _ = calls_of(make_services, "exfoliate_chips", params)
    for k in range(1, n, 5):
        s, st, paused = stop_and_continue(make_services, "exfoliate_chips", params, k)
        if paused:
            assert not (paused["in_progress"] and paused["step"] == "heat_applied_tape")
        assert st.status == "succeeded"


@pytest.mark.parametrize("kind", ["pickup", "release"])
def test_stop_obeys_the_current_transfer_step_boundary(admitted, monkeypatch, kind):  # noqa: F811
    import threading

    from qumus2.core.errors import StopRequested
    from qumus2.flows.library.stacking import DropDown, PickUpFlake
    from qumus2.flows.step import Observation, StepRunner, Verdict

    h = admitted
    ctx = h.factory(h.rid, {}, None)
    outer = threading.Event()
    ctx.cancel = outer
    def check_cancel():
        if ctx.cancel.is_set():
            raise StopRequested("Stop at the named action boundary")
    ctx.check_cancel = check_cancel
    step = (PickUpFlake("a", "stamp", "hBN") if kind == "pickup" else
            DropDown("stamp", ["a"], {"a": "hBN"}, "target", (0., 0.), 170., None))
    reached = []
    def stop_during_action(context, correction):
        reached.append(step.name)
        outer.set()
        context.check_cancel()
    monkeypatch.setattr(step, "expected", lambda context: {})
    monkeypatch.setattr(step, "preconditions", lambda context: None)
    monkeypatch.setattr(step, "act", stop_during_action)
    monkeypatch.setattr(step, "verify", lambda context: Observation(Verdict.OK))
    monkeypatch.setattr(step, "on_success", lambda *args: None)
    if kind == "pickup":
        assert step.pause_inside is True and step.safe_to_resume is False
        with pytest.raises(StopRequested):
            StepRunner().run(step, ctx, 0)
        assert h.store.get_steps(h.rid)[0]["status"] == "in_progress"
    else:
        assert step.pause_inside is None and step.safe_to_resume is False
        assert StepRunner().run(step, ctx, 0).ok
        assert h.store.get_steps(h.rid)[0]["status"] == "done"
    assert reached == [step.name] and ctx.cancel is outer and outer.is_set()


def test_pause_preserves_heater_target_and_restores_measured_readiness(focus_context, monkeypatch):  # noqa: F811
    from qumus2.flows.library.stacking import HeatTransferStage, StackDeviceFlow

    s, ctx, _ = focus_context
    heater = ctx.gate.heater("stamp_heater")
    executor = s.jobs.executor
    readings = []
    raw = s.lab.devices.get("stamp_heater")
    read = raw.read_temperature
    def observed_reading():
        value = read()
        readings.append(value)
        return value
    monkeypatch.setattr(raw, "read_temperature", observed_reading)
    try:
        HeatTransferStage(90).act(ctx, None)
        assert heater.get_target() == 90
        assert executor._pause(ctx.run_id, ctx, StackDeviceFlow(), [], 0, in_progress=False) == "paused"
        held = s.store.get_run(ctx.run_id)
        assert held["meta"]["held_heaters"] == {"stamp_heater": 90.}
        assert held["meta"]["paused"]["heaters"] == {"stamp_heater": 90.}
        assert heater.get_target() is None
        # Natural cooling makes restoration exercise measured readiness, rather than an already-hot fast path.
        ctx.clock.sleep(30)
        assert heater.read_temperature() < 90 - ctx.verification["heating"]["band_c"]
        mark, before = s.store.last_event_id(), ctx.clock.now()
        executor._restore_heaters(held, ctx)
        assert heater.get_target() == 90
        assert abs(readings[-1] - 90) <= ctx.verification["heating"]["band_c"]
        assert s.store.get_run(ctx.run_id)["meta"]["held_heaters"] == {}
        assert ctx.clock.now() > before
        ctx.gate.microscope("microscope").capture()
        commands = [e for e in s.store.events_after(mark, limit=10000) if e.kind == "device.command"]
        sets = [e.id for e in commands if e.payload["device"] == "stamp_heater"
                and e.payload["command"] == "set_target"]
        capture = next(e.id for e in commands if e.payload["device"] == "microscope"
                       and e.payload["command"] == "capture")
        assert sets and max(sets) < capture
    finally:
        heater.off()


def test_compatible_safe_resume_restores_before_the_next_action(admitted):  # noqa: F811
    h = admitted
    h.store.begin_step(h.rid, 0, "first")
    h.store.finish_step(h.rid, 0, "done")
    h.store.set_run_status(h.rid, "paused")
    decision = h.service.resume(h.rid)
    assert decision["action"] == "resume" and decision["start_seq"] == 1
    rid, seq, mode = h.service._q.get_nowait()
    assert (rid, seq, mode) == (h.rid, 1, "resume")
    assert h.service.executor.execute(rid, h.factory(rid, {}, None), start_seq=seq, restore=True) == "succeeded"
    assert ("act", "first") not in h.trace
    assert h.trace.index(("restore_heaters",)) < h.trace.index(("act", "second"))
    assert h.store.get_run(rid)["status"] == "succeeded"



def test_retry_job_continues_a_paused_job(services):
    s = services
    stop_at(s, 12)
    rid = s.jobs.submit(*PLACE)
    assert s.jobs.wait(rid, 60).status == "paused"
    s.jobs.retry(rid)
    assert s.jobs.wait(rid, 60).status == "succeeded"
    with pytest.raises(ValueError):
        s.jobs.resume(rid)


def test_driver_stop_drops_queued_messages_and_continue_resumes(services):
    s = services
    calls = []

    class Rt:
        mode, busy, stopped = "scripted", False, False

        async def send(self, text):
            calls.append(("send", text))

        async def interrupt(self):
            calls.append(("interrupt", None))

        async def continue_turn(self, text):
            calls.append(("continue", text))

        async def close(self):
            pass

    from qumus2.agents.driver import ConversationDriver

    d = ConversationDriver(s.store, Rt(), s.jobs)
    stop_at(s, 12)
    rid = s.jobs.submit(*PLACE)
    assert s.jobs.wait(rid, 60).status == "paused"

    async def go():
        Rt.busy = True  # the queued message waits for the turn
        s.store.add_instruction("also do X", project_id=s.project_id)
        s.store.add_instruction("STOP", kind="stop", project_id=s.project_id)
        await d.tick()
        Rt.busy = False
        s.store.add_instruction("CONTINUE", kind="continue", project_id=s.project_id)
        await d.tick()
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert ("interrupt", None) in calls
    assert not any(c[0] == "send" for c in calls)  # "also do X" was dropped
    (dropped,) = events(s, "user.dropped")
    assert dropped.payload["text"] == "also do X"
    (cont,) = events(s, "system.continued")
    assert cont.payload["resumed"][0]["run_id"] == rid
    text = next(t for c, t in calls if c == "continue")
    assert rid in text and "continues from step" in text
    assert s.jobs.wait(rid, 60).status == "succeeded"


def test_cancel_ends_a_job_cancelled_not_paused(services):
    s = services
    fired = []
    faults, real = s.lab.faults, s.lab.faults.before_call
    start = faults.calls

    def before_call(device_id):
        real(device_id)
        if not fired and faults.calls - start >= 12:
            fired.append(device_id)
            (run,) = s.store.list_runs("running")
            s.jobs.cancel(run["id"])

    faults.before_call = before_call
    rid = s.jobs.submit(*PLACE)
    assert s.jobs.wait(rid, 60).status == "cancelled" and fired
    stop_at(s, 3)
    rid2 = s.jobs.submit(*PLACE)
    assert s.jobs.wait(rid2, 60).status == "paused"
    s.jobs.cancel(rid2)
    assert s.jobs.status(rid2).status == "cancelled"
    assert s.jobs.resume_all() == []
