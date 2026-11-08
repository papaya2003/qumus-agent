"""One comparison trial: one system, one scenario, one seed, in this process.

    python -m v1compare.trial --system v1 --scenario baseline --seed 1 --workdir DIR

The graphene task has two phases, run back to back:

- exfoliate: put graphene from the raw-crystal carrier onto one bare chip;
- search: take that chip to the microscope, find its corner, scan it, revisit
  the best flakes at 20X/100X, record them, put the chip back in storage.

"flows" mode runs each system's own flows for these phases without a model
(v1: its assembly tools, called as its agent would call them; v2: its jobs).
It compares the control logic and error handling below the agents, and costs
nothing. The agent mode (real model replies) builds on the same trial and
scoring; see v1compare/README.md.

A scenario injects one testbench fault as soon as the world reaches a state,
checked before every device command (e.g. "the search arm's first command
after exfoliation", "the right arm is holding a chip"). The same triggers
work whether a flow or an agent drives the hardware. After a
simulated crash the system is restarted the way it would be in the lab: v1
has no resume, so an operator rebuilds it and runs the interrupted phase again
(one human intervention); v2 restarts its job service, which resumes the run
by itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qumus2.config import load_yaml
from qumus2.testbench.faults import SimulatedCrash
from v1compare.scoring import score

PHASES = ("exfoliate", "search")


def _search_starts(world: Any, device: str) -> bool:
    """The first command to the search arm once a chip carries flakes (both
    systems exfoliate with the left arm only)."""
    return device == "arm_right" and any(o.kind == "chip" and o.flakes
                                         for o in world.objects.values())


def _holding_chip(world: Any, device: str) -> bool:
    return any(o.kind == "chip" and o.held_by == "arm_right" for o in world.objects.values())


def _chip_on_transfer_stage(world: Any, device: str) -> bool:
    return any(o.kind == "chip" and o.on == "transfer_stage" for o in world.objects.values())


@dataclass
class Scenario:
    description: str
    title: str = ""  # what happens, for the recording's title (stage-1 review issue 1)
    when: Any = None  # (world, device about to be called) -> inject now?
    fault: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario("No injected fault; the testbench's normal placement and "
                         "measurement noise only.", title="no fault"),
    "askew_on_stage": Scenario(
        "The chip lands 2.5 mm and 6 degrees off target on the transfer stage.",
        title="the chip lands askew on the stage",
        when=_search_starts, fault="next_placement", params={"dx": 2.0, "dy": -1.5, "drot": 6.0}),
    "pick_fails_once": Scenario(
        "The first pick of the search phase comes up empty.", title="the first pick comes up empty",
        when=_search_starts, fault="pick_fail_next", params={"count": 1}),
    "drop_in_transit": Scenario(
        "The chip falls off the nozzle on its way to the transfer stage.",
        title="the chip drops in transit",
        when=_search_starts, fault="drop_in_transit"),
    "arm_timeout": Scenario(
        "One command to the search arm gets no reply (serial timeout).",
        title="the arm does not answer once",
        when=_search_starts, fault="serial_timeout", params={"device": "arm_right", "count": 1}),
    "microscope_offline": Scenario(
        "The microscope stops answering once the chip is on the transfer stage.",
        title="the microscope goes offline",
        when=_chip_on_transfer_stage, fault="device_offline",
        params={"device": "microscope", "offline": True}),
    "crash_holding_chip": Scenario(
        "The software dies while the search arm holds the chip; then it is restarted.",
        title="crash while the arm holds a chip",
        when=_holding_chip, fault="crash", params={"after_calls": 1}),
    "crash_during_scan": Scenario(
        "The software dies in the middle of the microscope scan; then it is restarted.",
        title="crash during the scan",
        when=_chip_on_transfer_stage, fault="crash", params={"after_calls": 400}),
}


def arm_scenario(trigger_list: list[dict[str, Any]], sc: Scenario) -> None:
    if sc.when is not None and sc.fault:
        trigger_list.append({"when": sc.when, "fault": sc.fault, "params": sc.params})


# ----- v1 -----------------------------------------------------------------------

V1_PHASES = {
    "exfoliate": [("live_yolo_capture", {"camera_view": "both", "max_frames": 10}),
                  ("assembly_exfoliation", {"raw_carrier_qr_message": "Gr", "num_chips": 1})],
    "search": [("live_yolo_capture", {"camera_view": "both", "max_frames": 10}),
               ("search_chip_assembly", {"material_type": "Gr"})],
}


def run_v1(sc: Scenario, seed: int, workdir: Path, cfg: dict[str, Any],
           log: list[str]) -> dict[str, Any]:
    from v1compare.bench import V1Bench

    bench = V1Bench(workdir / "v1", seed=seed, testbench_config=cfg)
    arm_scenario(bench.triggers, sc)
    t0 = bench.clock.now()
    claimed, interventions, crashes = True, 0, 0
    for phase in PHASES:
        attempt = 0
        while True:
            attempt += 1
            try:
                for tool, kwargs in V1_PHASES[phase]:
                    out = bench.call(tool, **kwargs)
                    text = out if isinstance(out, str) else json.dumps(out, default=str)
                    log.append(f"[v1 {phase} #{attempt}] {tool}: {text}")
                    if tool != "live_yolo_capture" and "❌" in text:
                        claimed = False
                break
            except SimulatedCrash as exc:
                crashes += 1
                log.append(f"[v1 {phase} #{attempt}] CRASH: {exc}")
                if attempt > 1:
                    claimed = False
                    break
                # v1 has no resume: the operator restarts it and reruns the phase
                interventions += 1
                bench._dead_threads.clear()
                bench.build_tools()
            except Exception as exc:  # noqa: BLE001 - a v1 tool raised: it reports failure
                claimed = False
                log.append(f"[v1 {phase} #{attempt}] EXCEPTION: {exc!r}")
                break
        if not claimed:
            break

    records, ledger = _v1_records(bench)
    s = score("v1", bench.lab.world, claimed_success=claimed, records=records, ledger=ledger)
    return {"score": s.to_dict(), "interventions": interventions, "crashes": crashes,
            "sim_seconds": round(bench.clock.now() - t0, 1),
            "device_commands": len(bench.journal.commands),
            "incidents": bench.journal.incidents[:50], "injected": bench.injected,
            "cost_usd": 0.0}


def _v1_records(bench: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from common.services import ChipInventoryService
    from common.services.flake_inventory_service import FlakeInventoryService

    from v1compare.calibration import ArmMap

    picks: dict[str, dict[str, Any]] = {}
    path = bench.workdir / "sim_revisits.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            row = json.loads(line)
            picks[row["path"]] = row
    records = []
    for f in FlakeInventoryService().list_flakes():
        p = picks.get(str(Path(f.image_100x_path).resolve())) if f.image_100x_path else None
        records.append({"sim_id": p["pick"] if p else None, "label": f.thickness_label,
                        "chip": f.chip_id})

    believed = []
    maps = {"left": ArmMap("left"), "right": ArmMap("right")}
    for c in ChipInventoryService().list_chips():
        if not c.present or not c.material_type:
            continue  # only chips v1 knows carry material
        for side, (x, y) in (("right", (c.pickup_right_x_mm, c.pickup_right_y_mm)),
                             ("left", (c.pickup_left_x_mm, c.pickup_left_y_mm))):
            if x is not None and y is not None:
                wx, wy, _, part = maps[side].to_world(float(x), float(y), 15.0)
                believed.append((wx, wy))
                break
    ledger: dict[str, Any] = {}
    with bench.lab.world.lock:
        chips = [o for o in bench.lab.world.objects.values() if o.kind == "chip" and o.flakes]
    for o in chips:
        near = min(believed, key=lambda b: (b[0] - o.pose.x) ** 2 + (b[1] - o.pose.y) ** 2,
                   default=None)
        ledger[o.id] = near
    return records, ledger


# ----- v2 -----------------------------------------------------------------------

V2_PHASES = {"exfoliate": ("exfoliate_chips", {"material": "Gr"}),
             "search": ("search_chip", {})}
V2_DONE = {"succeeded", "failed", "cancelled", "parked", "waiting_human"}


def run_v2(sc: Scenario, seed: int, workdir: Path, cfg: dict[str, Any],
           log: list[str]) -> dict[str, Any]:
    from qumus2.app import build_services
    from v1compare.bench import endless_tape, wrap_fault_injector

    s = build_services(":memory:", seed=seed, testbench_config=cfg)
    endless_tape(s.lab.world)
    lab, clock = s.lab, s.clock
    triggers: list[dict[str, Any]] = []
    injected: list[dict[str, Any]] = []
    wrap_fault_injector(lab, triggers, injected, set())
    arm_scenario(triggers, sc)
    t0 = clock.now()
    claimed, interventions = True, 0
    for phase in PHASES:
        flow, params = V2_PHASES[phase]
        try:
            rid = s.jobs.submit(flow, params)
        except ValueError as exc:
            claimed = False
            log.append(f"[v2 {phase}] refused: {exc}")
            break
        st = _wait_v2(s, rid)
        log.append(f"[v2 {phase}] {flow}: {st.status} "
                   f"{json.dumps(st.error, default=str)[:2000] if st.error else ''}")
        for step in st.steps:
            log.append(f"    {step['name']}: {step['status']} {step.get('note') or ''}")
        if st.status in ("parked", "waiting_human"):
            interventions += 1  # needs the agent or a person to decide
        if st.status != "succeeded":
            claimed = False
            break

    records, ledger = _v2_records(s)
    sc_ = score("v2", lab.world, claimed_success=claimed, records=records, ledger=ledger)
    commands = [e for e in s.store.events_after(0, limit=1_000_000) if e.kind == "device.command"]
    crashes = sum(1 for i in injected if i["fault"] == "crash")
    return {"score": sc_.to_dict(), "interventions": interventions, "crashes": crashes,
            "sim_seconds": round(clock.now() - t0, 1), "device_commands": len(commands),
            "incidents": [], "injected": injected, "cost_usd": 0.0}


def _wait_v2(s: Any, rid: str, timeout_s: float = 600.0) -> Any:
    deadline = time.monotonic() + timeout_s  # wall-clock guard for the harness only
    while True:
        st = s.jobs.wait(rid, 1.0)
        if st.status in V2_DONE or time.monotonic() > deadline:
            return st


def _v2_records(s: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = [{"sim_id": f["props"].get("sim_id"), "label": f["props"].get("thickness_label"),
                "chip": f["location"]}
               for f in s.store.list_samples(kind="flake") if f["status"] == "catalogued"]
    from qumus2.flows.bench import positions as named_places

    positions = {n: (p["x"], p["y"]) for n, p in named_places(s.lab.bench).items()}
    ledger: dict[str, Any] = {}
    with s.lab.world.lock:
        chips = [o.id for o in s.lab.world.objects.values() if o.kind == "chip" and o.flakes]
    for cid in chips:
        sample = s.store.get_sample(cid)
        ledger[cid] = positions.get(sample["location"]) if sample else None
    return records, ledger


# ----- entry ----------------------------------------------------------------------

def run_trial(system: str, scenario: str, seed: int, workdir: Path) -> dict[str, Any]:
    sc = SCENARIOS[scenario]
    cfg = load_yaml("testbench.yaml")
    log: list[str] = []
    workdir.mkdir(parents=True, exist_ok=True)
    w0 = time.monotonic()
    result: dict[str, Any] = {"system": system, "scenario": scenario, "seed": seed,
                              "mode": "flows"}
    try:
        runner = run_v1 if system == "v1" else run_v2
        result.update(runner(sc, seed, workdir, cfg, log))
    except BaseException as exc:  # noqa: BLE001 - the harness itself failed: record it
        result["harness_error"] = "".join(traceback.format_exception(exc))[-4000:]
    result["wall_seconds"] = round(time.monotonic() - w0, 1)
    (workdir / f"{system}_log.txt").write_text("\n".join(log), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--system", choices=("v1", "v2"), required=True)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="baseline")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out", help="write the result JSON here (default: stdout)")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve() if a.out else None  # v1's bench chdirs into the workdir
    res = run_trial(a.system, a.scenario, a.seed, Path(a.workdir).resolve())
    text = json.dumps(res, indent=2, default=str)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
