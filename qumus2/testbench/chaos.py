"""Chaos runs: the stacking task on the full-noise testbench with faults
injected at random moments, many seeds in a row (`qumus2 chaos`).

Each seed runs twice. First without faults (the baseline: how many device
calls, how much tape, stamps and sim time the task takes on this seed), then
with faults: exfoliate graphene and hBN onto one chip each, search both under
the microscope, stack hBN/graphene/hBN onto the electrode chip. The first
fault of a run cycles through FAULT_CASES by seed, so every fault gets the same
number of runs; further faults (`n_faults` > 1) are drawn at random. Each one
is injected before a random device call of a job, within the baseline's call
count.

A run is classified against the simulated world, not against what the system
says:

    ok            the system says done and the world agrees
    honest_stop   the system stopped, said why, and its records match the world
    bad_stop      the system stopped but its records are wrong (or something
                  collided)                                       <- a bug
    false_ok      the system says done but the world disagrees    <- a bug
    error         the harness or the system raised
    no_fault      no injected fault ever took effect: the run says nothing
                  about fault handling and is left out of the robustness counts

Every injection is checked on its own: did it take effect (`fired`: the
armed failure was used up, or the faulty device was used afterwards), and did
the system do what a correct system must (`handled`: see FaultCase.expect).

The counts measure the control software's robustness on this simplified
model. They are NOT lab success rates: the probabilities (placement error,
detector misses, flake yield, ...) are placeholders, not measurements.
"""

from __future__ import annotations

import asyncio
import random
import re
import threading
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ARMS = ["arm_left", "arm_right"]
TARGET_CHIP = "chip_e01"


@dataclass(frozen=True)
class FaultCase:
    """One fault the chaos series injects.

    expect: "recover" (a correct system finishes the task), "stop" (it cannot:
    it must stop and say why, `reason` matching the stop note) or "either"
    (recovering and an honest stop are both correct; a stop note must match
    `reason` when one is given).
    pending: how much of an armed fault is still waiting to happen (fired when
    it went down). uses: (device or devices, command or None): the fault fired
    when such a device was called after the injection (any call when command is
    None). A "device" parameter "@arm" becomes the arm the job is using at that
    moment, "@now" the device it is calling, "@board" the storage board it
    looked at last, so the fault meets the task. during: the task flows (by
    index into TASK) the fault is injected in, for faults that only matter
    there; default anywhere.
    """

    name: str
    make: Callable[[random.Random], dict[str, Any]]
    expect: str
    why: str
    reason: str = ""
    pending: Callable[[Any, dict[str, Any]], int] | None = None
    uses: Callable[[dict[str, Any]], tuple[Any, str | None]] | None = None
    during: Callable[[dict[str, Any]], tuple[int, ...]] | None = None


def _w(s: Any) -> Any:
    return s.lab.world


FAULT_CASES: list[FaultCase] = [
    FaultCase("next_placement",
              lambda r: {"dx": r.uniform(-3, 3), "dy": r.uniform(-3, 3), "drot": r.uniform(-8, 8)},
              "recover", "the camera sees the misplaced object and it is put down again",
              pending=lambda s, p: int(bool(_w(s).faults.next_placement))),
    FaultCase("pick_fail_next", lambda r: {"count": r.randint(1, 2)},
              "recover", "the empty nozzle is seen and the pick is tried again",
              pending=lambda s, p: _w(s).faults.pick_fail_next),
    FaultCase("pick_fail_prob", lambda r: {"value": r.uniform(0.2, 0.4)},
              "either", "picks are retried; many failures in a row may end in a stop",
              uses=lambda p: (ARMS, "set_vacuum")),
    FaultCase("big_error_prob", lambda r: {"value": r.uniform(0.2, 0.4)},
              "either", "each bad placement is seen and corrected",
              uses=lambda p: (ARMS, "set_vacuum")),
    FaultCase("drop_in_transit", lambda r: {},
              "either", "the object is missing from the destination: found again or a stop",
              pending=lambda s, p: int(_w(s).faults.drop_next)),
    # both only mean something while stacking (task flow 4) and wait for their
    # first chance: armed when stacking starts, so every run with them counts
    # (armed at a random call, after the last pick-up, they never fired: the
    # no_fault runs of the 2026-10-22 series)
    FaultCase("pickup_fail_next", lambda r: {"count": 1},
              "recover", "the flake is still on the chip: the stamp presses again",
              pending=lambda s, p: _w(s).faults.pickup_fail_next, during=lambda p: (4,)),
    FaultCase("release_fail_next", lambda r: {"count": 1},
              "recover", "the stack is still on the stamp: heated more and released",
              pending=lambda s, p: _w(s).faults.release_fail_next, during=lambda p: (4,)),
    FaultCase("device_offline",
              lambda r: {"device": "@now", "offline": True},
              "stop", "nothing can replace a dead device: stop and name it",
              reason="{device}|offline|unavailable",
              uses=lambda p: (p["device"], None)),
    FaultCase("camera_frame_drop", lambda r: {"count": r.randint(1, 3)},
              "recover", "a dropped frame is a transient error: look again",
              pending=lambda s, p: s.lab.devices.get("overview_camera").drop_next),
    FaultCase("qr_unreadable",
              lambda r: {"board": "@board", "looks": r.randint(1, 3)},
              "recover", "no calibration for a few frames is transient: look again",
              pending=lambda s, p: s.lab.locator.qr_unreadable.get(p["board"], 0)),
    FaultCase("chip_removed", lambda r: {"place": "?"},
              "stop", "the electrode chip is gone from its slot: stop and say it is missing",
              reason=TARGET_CHIP + "|not (found|seen)|missing|empty|nothing",
              during=lambda p: (2, 3)),
    FaultCase("arm_step_loss",
              lambda r: {"device": "@arm", "dx": r.uniform(-2, 2), "dy": r.uniform(-2, 2)},
              "recover", "the camera sees the offset; placements are corrected or the arm homes",
              uses=lambda p: (p["device"], None)),
    FaultCase("arm_reset", lambda r: {"device": "@arm"},
              "recover", "the gate refuses moves until the arm homes; the flow homes it",
              uses=lambda p: (p["device"], None)),
    FaultCase("stamp_torn", lambda r: {},
              "stop", "observed film damage leaves partial transfer unknown: stop for human "
              "review without automatic replacement, including with fresh spares or an empty ledger",
              reason=r"^(?:unclear observation:\s*)?Observed (?:film damage|torn film); "
                     r"partial transfer remains unknown\.\s*Human review is required\b",
              uses=lambda p: ("transfer_arm", None)),
    FaultCase("tape_roll_empty", lambda r: {"left_mm": 0.0},
              "stop", "no tape left: stop and ask for a new roll", reason="tape|roll",
              uses=lambda p: ("exfoliator", "advance"), during=lambda p: (0,)),
    FaultCase("perception",
              lambda r: {"miss_prob": r.uniform(0.05, 0.2),
                         "false_positive_prob": r.uniform(0.0, 0.1),
                         "confuse_material_prob": r.uniform(0.0, 0.15),
                         "tail_prob": r.uniform(0.1, 0.4)},
              "either", "worse detection: more looks and corrections, maybe a stop",
              uses=lambda p: ("overview_camera", None)),
    FaultCase("serial_timeout",
              lambda r: {"device": "@now", "count": r.randint(1, 2)},
              "recover", "a timeout is transient: retried",
              pending=lambda s, p: s.lab.faults.timeouts.get(p["device"], 0)),
    FaultCase("sensor_stuck",
              lambda r: {"device": "stamp_heater", "value": round(r.uniform(22, 26), 1)},
              "either", "a frozen reading: a heating step times out and stops; a cooling "
              "step cannot tell",
              reason="{device}|temperature|heat|cool",
              uses=lambda p: (p["device"], None), during=lambda p: (4,)),
    FaultCase("vacuum_stuck",
              lambda r: {"device": r.choice(["exfoliation_vacuum", "transfer_arm_vacuum"]),
                         "value": False},
              "either", "the object is not held: seen by the next check",
              uses=lambda p: (p["device"], None),
              during=lambda p: (0, 1) if p["device"] == "exfoliation_vacuum" else (4,)),
    FaultCase("crash", lambda r: {"after_calls": 1},
              "recover", "the job service restarts and resumes from the step checkpoint",
              pending=lambda s, p: int(s.lab.faults.crash_at is not None)),
]
#: faults the series leaves out, and why (docs/testbench.md)
NOT_IN_CHAOS = {
    "hang": "only means something with a separate lab process (serve --lab-process); "
            "tests/test_lab_process.py covers it",
    "stuck_at": "the general form of sensor_stuck and vacuum_stuck, already in the series",
}
CASES = {c.name: c for c in FAULT_CASES}

STOPPED = {"parked", "waiting_human", "stopped_danger"}
POSE_TOL_MM = 3.0
BUGS = ("false_ok", "bad_stop", "error")


def _best_flakes(s: Any, chip: str) -> list[str]:
    fl = [f for f in s.store.list_samples(kind="flake", location=chip)
          if f["status"] == "catalogued"]
    return [f["id"] for f in sorted(fl, key=lambda f: -f["props"]["area_um2"])]


def _stack_params(s: Any) -> dict[str, Any] | None:
    from qumus2.records.geometry import ranked_pickup_combinations

    h, g = _best_flakes(s, "chip_002"), _best_flakes(s, "chip_001")
    if len(h) < 2 or not g:
        return None
    ranked = ranked_pickup_combinations(s.store, [h, g, h])
    return {"flake_ids": ranked[0]["flake_ids"], "target_chip_id": TARGET_CHIP} if ranked else None


#: (flow, params or a function of the services giving them; None = skip)
#: the exfoliation recipe the hands' tool submits by default (agents/tools/
#: hardware.py ExfoliateArgs); since 2026-11-03 the simulation does not score
#: recipes, so this only keeps the task's process parameters explicit
RECIPE = {"target_temp_c": 50, "dwell_s": 30, "num_massages": 2, "press_depth_mm": 0.2,
          "peel_speed": 200, "peel_angle_deg": 30.0}

TASK: list[tuple[str, Any]] = [
    ("exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_001"], **RECIPE}),
    ("exfoliate_chips", {"material": "hBN", "chip_ids": ["chip_002"], **RECIPE}),
    ("search_chip", {"chip_id": "chip_001"}),
    ("search_chip", {"chip_id": "chip_002"}),
    ("stack_device", _stack_params),
]


@dataclass
class ChaosRun:
    seed: int
    outcome: str = "ok"
    injected: list[dict[str, Any]] = field(default_factory=list)
    flows: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    collisions: list[dict[str, Any]] = field(default_factory=list)
    cost: dict[str, float] = field(default_factory=dict)
    #: extra cost over the fault-free run of the same seed (None: no baseline)
    extra: dict[str, float] | None = None
    calls: int = 0
    #: job device calls at the end of each task flow (cumulative)
    flow_calls: list[int] = field(default_factory=list)
    #: Local disposal is separate from physical grading; unfinished work is not success.
    cleanup: dict[str, Any] | None = None


def _dispose(s: Any, res: ChaosRun) -> None:
    try:
        res.cleanup = {**asyncio.run(s.close_local()), "outcome_before_cleanup": res.outcome}
        if not _cleanup_settled(res.cleanup):
            res.problems.append("Local shutdown remains unfinished; workers/resources retained: " + str(res.cleanup))
            res.outcome = "error"
    except Exception:  # Preserve the original task error and add disposal evidence.
        res.cleanup = {"closed": False, "error": traceback.format_exc()[-1500:]}
        res.problems.append("Local shutdown failed: " + res.cleanup["error"])
        res.outcome = "error"
    if res.outcome == "error":
        res.extra = None
        for injection in res.injected:
            injection["handled"] = False


def _cleanup_settled(report: dict[str, Any]) -> bool:
    return (report["safe_to_close_resources"] is True and report["closed"] is True
            and not report["alive_workers"] and report["stop_pending"] is False
            and not report["unsettled_jobs"] and not report["unconfirmed_energy_jobs"]
            and not report["evidence_pending"] and not report.get("stop_error")
            and not report.get("runtime_close_error"))


class _ServicesSetupError(RuntimeError):
    def __init__(self, cause: Exception, cleanup: dict[str, Any]) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cleanup = cleanup


def _world_agrees(s: Any, excused: str = "") -> list[str]:
    """Ledger vs world: every chip/carrier is where the ledger says and carries
    the material the ledger says, nothing on a nozzle, no safety violation, no
    collision. On a stop, problems with a
    sample the stop note names are excused (the note told the human)."""
    from qumus2.flows.bench import positions

    places = positions(s.lab.bench)
    w = s.lab.world
    problems = []
    with w.lock:
        objects = dict(w.objects)
        collisions = list(w.collisions)
    for o in objects.values():
        if o.held_by is not None and o.kind in ("chip", "carrier") and o.id not in excused:
            problems.append(f"{o.id} still held by {o.held_by}")
    for sample in s.store.list_samples():
        o = objects.get(sample["id"])
        if o is None or sample["kind"] not in ("chip", "carrier") or sample["id"] in excused:
            continue  # gone: taken away by a fault (chip_removed)
        if sample["props"].get("location_uncertain"):
            continue  # the records say they do not know: honest
        if sample["kind"] == "chip" and (sample["props"].get("material") or None) != o.material:
            problems.append(f"{sample['id']}: ledger says material "
                            f"{sample['props'].get('material')}, really {o.material}")
        loc = sample["location"]
        surface = w.surfaces.get(loc)
        if surface is not None:  # a moving stage: the object must ride on it
            if o.on != loc or not surface.contains(o.pose.x, o.pose.y):
                problems.append(f"{sample['id']}: ledger says {loc}, really not on it")
        elif loc in places:
            p = places[loc]
            d = ((o.pose.x - p["x"]) ** 2 + (o.pose.y - p["y"]) ** 2) ** 0.5
            if d > POSE_TOL_MM:
                problems.append(f"{sample['id']}: ledger says {loc}, really "
                                f"{d:.1f} mm away")
    if any(e.kind == "safety.violation" for e in s.store.events_after(0, limit=10_000_000)):
        problems.append("a safety violation was logged")
    problems += [f"collision: {c.get('what')} ({c.get('by')})" for c in collisions]
    return problems


def _cost(s: Any, t0: float, roll0: float) -> dict[str, float]:
    """Tape used (a tape_roll_empty fault takes the roll away: not counted),
    stamps no longer fresh, sim time."""
    w = s.lab.world
    stamps = [x for x in s.store.list_samples(kind="stamp") if x["status"] != "fresh"]
    return {"tape_mm": round(max(0.0, roll0 - w.tape.roll_left_mm), 1)
            if w.tape.roll_left_mm > 0 else 0.0,
            "stamps": float(len(stamps)),
            "sim_min": round((s.clock.now() - t0) / 60.0, 1)}


def _run_task(s: Any, res: ChaosRun, timeout: float) -> str:
    """The task's flows in order until one does not succeed; its last status."""
    c0 = s.lab.faults.calls
    for flow, params in TASK:
        p = params(s) if callable(params) else params
        if p is None:
            res.flows.append({"flow": flow, "status": "skipped",
                              "note": "no compatible observed stack in the current catalogue"})
            return "skipped"
        st = s.jobs.wait(s.jobs.submit(flow, p), timeout)
        note = next((x.get("note") for x in st.steps
                     if x["status"] not in ("done", "pending")), None)
        status = st.status
        err = st.error if isinstance(st.error, dict) else {}
        if status == "failed" and (err.get("status") == "dangerous" or "type" in err):
            # stopped on purpose: a dangerous verdict (everything to its safe
            # state) or a hardware error the flow raised, with its reason
            status = "stopped_danger"
            note = note or err.get("message")
        res.flows.append({"flow": flow, "status": status, "note": note})
        res.flow_calls.append(s.lab.faults.calls - c0)
        if status != "succeeded":
            return status
    return "succeeded"


def _services(seed: int, config: dict[str, Any] | None) -> Any:
    from qumus2.app import build_services
    from qumus2.config import load_yaml
    from qumus2.testbench.render import ensure_diagnostic_images

    cfg = config if config is not None else load_yaml("testbench.yaml")
    services = build_services(":memory:", seed=seed, testbench_config=cfg)
    try:
        ensure_diagnostic_images(services)
    except Exception as exc:
        try:
            cleanup = asyncio.run(services.close_local())
        except Exception as cleanup_error:
            cleanup = {"closed": False, "safe_to_close_resources": False, "error": repr(cleanup_error)}
        raise _ServicesSetupError(exc, cleanup) from exc
    return services


def baseline(seed: int, config: dict[str, Any] | None = None,
             timeout: float = 900) -> ChaosRun:
    """The same seed without faults."""
    res = ChaosRun(seed=seed)
    s = None
    try:
        s = _services(seed, config)
        t0, c0, roll0 = s.clock.now(), s.lab.faults.calls, s.lab.world.tape.roll_left_mm
        last = _run_task(s, res, timeout)
        res.calls = s.lab.faults.calls - c0
        res.cost = _cost(s, t0, roll0)
        res.problems = _world_agrees(s)
        res.outcome = "ok" if last == "succeeded" and not res.problems else (
            "honest_stop" if last in STOPPED | {"skipped"} else "error")
    except Exception as exc:  # noqa: BLE001 - one broken run must not stop the series
        res.outcome = "error"
        if isinstance(exc, _ServicesSetupError):
            res.cleanup = exc.cleanup
        res.problems.append(traceback.format_exc()[-1500:])
    finally:
        if s is not None:
            _dispose(s, res)
    return res


def _schedule(seed: int, r: random.Random, n_faults: int,
              base: ChaosRun) -> list[tuple[int, FaultCase, dict[str, Any]]]:
    """(job call before which to inject, case, params), within the baseline's
    calls (or the calls of the flows the case names)."""
    cases = [FAULT_CASES[seed % len(FAULT_CASES)]]
    cases += [r.choice(FAULT_CASES) for _ in range(n_faults - 1)]
    ends = base.flow_calls or [2500]
    out = []
    for c in cases:
        params = c.make(r)
        i = r.choice(c.during(params)) if c.during and len(ends) == len(TASK) else None
        lo, hi = (ends[i - 1] if i else 0) + 1, ends[i] if i is not None else ends[-1]
        if i is not None and c.pending is not None:
            hi = lo  # a "next N" fault waits for its chance: arm it as the flow starts
        out.append((r.randint(lo, max(lo, hi)), c, params))
    return sorted(out, key=lambda x: x[0])


def _inject(s: Any, case: FaultCase, params: dict[str, Any], now: str,
            arm: str) -> tuple[dict[str, Any], str]:
    """Inject; return the params really used and a note ("" = injected)."""
    looked = s.lab.locator.last_boards
    where = {"@now": now, "@arm": arm, "@board": looked[-1] if looked else "middle_storage"}
    params = {k: where.get(v, v) if isinstance(v, str) else v for k, v in params.items()}
    if case.name == "chip_removed":
        t = s.store.get_sample(TARGET_CHIP)
        if t is None or t["status"] == "stacked":
            return params, "the electrode chip is already done"
        params = {"place": t["location"]}
    try:
        s.lab.faults.inject(case.name, params)
    except ValueError as e:
        return params, f"not injected: {e}"
    return params, ""


def _takes_effect_at_once(s: Any, case: FaultCase, params: dict[str, Any],
                          note: str) -> bool:
    """A vacuum stuck at the other state than it was switched to changes what
    it holds at once, with no further command (seed 98 of the 2026-10-22
    series: stuck off under a held chip, which then came off with the tape)."""
    if note or case.name != "vacuum_stuck" or params["value"] is None:
        return False
    return s.lab.devices.get(params["device"]).get() != bool(params["value"])


def _fired(s: Any, inj: dict[str, Any], used: Counter) -> bool:
    case, p = CASES[inj["fault"]], inj["params"]
    if inj.get("note"):
        return False
    if inj.get("at_once"):
        return True
    if case.name == "chip_removed":
        return True  # gone while the task still needs it (checked at injection)
    if case.pending is not None:
        return case.pending(s, p) < inj["pending"]
    assert case.uses is not None
    dev, cmd = case.uses(p)
    devs = {dev} if isinstance(dev, str) else set(dev)
    if cmd is None and any(used[(inj["n"], d)] > 0 for d in devs):
        return True
    return any(e.kind == "device.command" and e.payload.get("device") in devs
               and (cmd is None or e.payload.get("command") == cmd)
               for e in s.store.events_after(inj["mark"], limit=10_000_000))


def _handled(outcome: str, note: str, case: FaultCase, params: dict[str, Any],
             expect: str | None = None) -> bool:
    """`expect`: the expectation saved with this injection."""
    expect = expect or case.expect
    if outcome in BUGS:
        return False
    if expect == "recover":
        return outcome == "ok"
    rx = case.reason.format(**params) if case.reason else ""
    stop_ok = outcome == "honest_stop" and (not rx or re.search(rx, note, re.I) is not None)
    return stop_ok if expect == "stop" else outcome == "ok" or stop_ok


def run_one(seed: int, n_faults: int = 1, config: dict[str, Any] | None = None,
            base: ChaosRun | None = None, timeout: float = 900) -> ChaosRun:
    """One seed with faults. `base`: its fault-free run (made here if None)."""
    base = base if base is not None else baseline(seed, config, timeout)
    res = ChaosRun(seed=seed)
    if base.cleanup is not None and not _cleanup_settled(base.cleanup):
        res.outcome = "error"
        res.problems = [*base.problems, "Faulted task not started: baseline local shutdown is unfinished"]
        res.cleanup = {"closed": False, "baseline": base.cleanup}
        return res
    s = None
    try:
        s = _services(seed, config)
        r = random.Random(f"chaos:{seed}")
        plan = _schedule(seed, r, n_faults, base)
        faults = s.lab.faults
        real = faults.before_call
        calls = {"n": 0}
        last = {"arm": "arm_left"}  # the arm the job used last
        used: Counter = Counter()  # (injection index, device) -> job calls after it

        def before_call(device_id: str) -> None:
            if threading.current_thread().name.startswith("job-"):
                calls["n"] += 1
                if device_id in ARMS:
                    last["arm"] = device_id
                while plan and plan[0][0] <= calls["n"]:
                    _, case, params = plan.pop(0)
                    mark = s.store.last_event_id()
                    params, note = _inject(s, case, params, device_id, last["arm"])
                    pend = case.pending(s, params) if case.pending and not note else 0
                    res.injected.append({"fault": case.name, "params": params, "expect": case.expect,
                                         "call": calls["n"], "device": device_id,
                                         "mark": mark, "pending": pend, "note": note,
                                         "n": len(res.injected),
                                         "at_once": _takes_effect_at_once(s, case, params,
                                                                          note)})
                for i in range(len(res.injected)):  # this call already meets the fault
                    used[(i, device_id)] += 1
            real(device_id)

        faults.before_call = before_call
        t0, roll0 = s.clock.now(), s.lab.world.tape.roll_left_mm
        last = _run_task(s, res, timeout)
        res.calls = calls["n"]
        res.cost = _cost(s, t0, roll0)
        with s.lab.world.lock:
            res.collisions = list(s.lab.world.collisions)
        note = next((f["note"] or "" for f in reversed(res.flows)), "") or ""
        if last == "succeeded":
            res.problems = _world_agrees(s)
            res.outcome = "false_ok" if res.problems else "ok"
        elif last in STOPPED | {"skipped"}:  # skipped: too few flakes to plan a stack
            res.problems = _world_agrees(s, excused=note)
            res.outcome = "bad_stop" if res.problems else "honest_stop"
        else:
            res.problems = _world_agrees(s)
            res.outcome = "error"
            res.problems.append(f"{res.flows[-1]['flow']} ended {last}")
        for inj in res.injected:
            inj["fired"] = _fired(s, inj, used)
            case = CASES[inj["fault"]]
            inj["handled"] = inj["fired"] and _handled(res.outcome, note, case, inj["params"],
                                                       inj.get("expect"))
        if res.injected and not any(i["fired"] for i in res.injected) \
                and res.outcome not in BUGS:
            res.outcome = "no_fault"
        if base.outcome == "ok" and res.outcome == "ok":
            res.extra = {k: round(res.cost[k] - base.cost.get(k, 0.0), 1) for k in res.cost}
    except Exception as exc:  # noqa: BLE001 - one broken run must not stop the series
        res.outcome = "error"
        if isinstance(exc, _ServicesSetupError):
            res.cleanup = exc.cleanup
        res.problems.append(traceback.format_exc()[-1500:])
    finally:
        if s is not None:
            _dispose(s, res)
    return res


def run_series(seeds: list[int], n_faults: int = 1) -> list[ChaosRun]:
    return [run_one(seed, n_faults) for seed in seeds]


def seeds_for(fault: str, n: int, first: int = 1) -> list[int]:
    """`n` seeds from `first` on whose first fault is `fault` (`qumus2 chaos
    --fault`): the series cycles through FAULT_CASES by seed."""
    k = [c.name for c in FAULT_CASES].index(fault)  # ValueError: not in the series
    start = first + (k - first) % len(FAULT_CASES)
    return [start + i * len(FAULT_CASES) for i in range(n)]


def _mean(xs: list[float]) -> str:
    return f"{sum(xs) / len(xs):+.1f}" if xs else "-"


def render_markdown(runs: list[ChaosRun], n_faults: int) -> str:
    c = Counter(r.outcome for r in runs)
    valid = [r for r in runs if r.outcome != "no_fault"]
    lines = [
        "# Testbench chaos runs",
        "",
        f"{len(runs)} seeds, {n_faults} fault(s) each (the first cycles through the "
        f"{len(FAULT_CASES)} faults by seed), on the full-noise testbench (testbench.yaml). "
        "Task: exfoliate graphene and hBN onto one chip each, search both, stack "
        "hBN/graphene/hBN onto the electrode chip. Every seed also runs once without "
        "faults (the baseline for the extra cost).",
        "",
        "> These counts measure how the control software copes with faults on a "
        "simplified model. They are **not** lab success rates: the error and yield "
        "probabilities are placeholders, not measurements.",
        "",
        "| outcome | runs | meaning |",
        "|---|---|---|",
        f"| ok | {c.get('ok', 0)} | done, and the world agrees |",
        f"| honest_stop | {c.get('honest_stop', 0)} | stopped, said why, records right |",
        f"| bad_stop | {c.get('bad_stop', 0)} | stopped with wrong records or a collision "
        "(bug) |",
        f"| false_ok | {c.get('false_ok', 0)} | says done, the world disagrees (bug) |",
        f"| error | {c.get('error', 0)} | raised (bug) |",
        f"| no_fault | {c.get('no_fault', 0)} | no fault took effect: not counted |",
        "",
        f"World collisions in all runs: **{sum(len(r.collisions) for r in runs)}** "
        "(the world's own record, not the safety layer's; must be 0).",
        "",
        "## By fault",
        "",
        "`fired` = the fault took effect; `handled` = the system did what a correct "
        "system must (expected behaviour). Extra cost: mean over runs that ended ok, "
        "compared with the same seed without faults.",
        "",
        "| fault | expected | injected | fired | handled | extra tape mm | extra stamps "
        "| extra sim min |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in FAULT_CASES:
        injs = [(r, i) for r in runs for i in r.injected if i["fault"] == case.name]
        if not injs:
            continue
        fired = [(r, i) for r, i in injs if i["fired"]]
        ok = [r for r, _ in fired if r.extra is not None]
        expects = Counter(i.get("expect") or case.expect for _, i in fired)
        expected = case.expect if not expects or set(expects) == {case.expect} else \
            " / ".join(f"{k} {v}" for k, v in sorted(expects.items()))
        lines.append(
            f"| {case.name} | {expected} | {len(injs)} | {len(fired)} | "
            f"{sum(1 for _, i in fired if i['handled'])} | "
            f"{_mean([r.extra['tape_mm'] for r in ok])} | "  # type: ignore[index]
            f"{_mean([r.extra['stamps'] for r in ok])} | "  # type: ignore[index]
            f"{_mean([r.extra['sim_min'] for r in ok])} |")  # type: ignore[index]
    lines += ["", "Expected behaviour per fault:", ""]
    lines += [f"- **{c.name}** ({c.expect}): {c.why}." for c in FAULT_CASES]
    lines += ["", "Not in the series: " + "; ".join(f"`{k}` ({v})"
                                                    for k, v in NOT_IN_CHAOS.items()) + "."]
    lines += ["", "## Runs to look at", "",
              f"Runs that are a bug or where a fired fault was not handled "
              f"({len(valid)} runs had a fault that took effect).", ""]
    for r in runs:
        if r.outcome == "no_fault" or (r.outcome not in BUGS
                                       and all(i["handled"] for i in r.injected
                                               if i["fired"])):
            continue
        faults = ", ".join(f"{i['fault']}@{i['call']}"
                           + ("" if i["fired"] else " (did not fire)")
                           for i in r.injected) or "none"
        where = "; ".join(f"{f['flow']}: {f['status']}" + (f" ({f['note']})" if f["note"]
                                                            else "") for f in r.flows)
        lines.append(f"- seed {r.seed} [{r.outcome}] faults: {faults}. {where}"
                     + (f" Problems: {'; '.join(p.splitlines()[-1] for p in r.problems)}"
                        if r.problems else ""))
    return "\n".join(lines) + "\n"
