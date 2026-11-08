"""Record a simulated run for replay.

    python scripts/record_demo.py                  # scripted, no model requests
    python scripts/record_demo.py --openai          # paid, explicit opt-in

Configure QUMUS_OPENAI_* and token rates for paid runs. The runtime enforces
--budget across turns plus its persistent project-wide ledger. Outputs go to
recordings/<name>; private source data remains in runs/checks/demos/<name>.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from qumus2.app import build_services
from qumus2.ui.recording import attach_recorder, export_recording

REQUEST = "Make a graphene device: an hBN-encapsulated graphene Hall bar."
MID_RUN = ("When the device is done, end with a three-line summary I can paste into my "
           "lab notebook: flakes used, layout, anything that went wrong.")
ASKEW = {"dx": 2.2, "dy": -1.2, "drot": 2.0}


async def record(a: argparse.Namespace) -> int:
    run_dir = Path("runs/checks/demos") / a.name
    run_dir.mkdir(parents=True, exist_ok=False)
    # drawn microscope pictures only: replayed real photos are lab data
    os.environ.pop("QUMUS2_MICROSCOPE_REPLAY_DIR", None)
    s = build_services(run_dir / "qumus.db", scenario=a.scenario, use_openai=a.openai,
                       seed=a.seed, step_delay_s=0.0 if a.openai else 0.6,
                       run_budget_usd=a.budget if a.openai else None)
    rec = attach_recorder(s)
    t0 = time.monotonic()
    faults = ([("next_placement", ASKEW)] if a.askew else []) + a.fault
    for name, params in faults:  # armed now; each fires at its first chance
        s.lab.faults.inject(name, params)
        s.store.emit("fault.injected", {"fault": name, "params": params})
    s.store.add_instruction(a.request, project_id=s.project_id)
    task = asyncio.create_task(s.driver.wait_idle(timeout=a.timeout))
    if a.mid_run:
        # like a person typing in the UI once the hands have started work
        while not task.done() and not any(
                e.kind == "tool.call" and e.agent == "hands"
                for e in s.store.events_after(0, limit=1_000_000)):
            await asyncio.sleep(0.5)
        if not task.done():
            await asyncio.sleep(2.0)
            s.store.add_instruction(a.mid_run, project_id=s.project_id)
    try:
        await task
        await s.driver.wait_idle(timeout=a.timeout)  # a message after the turn ended
    finally:
        if rec:
            rec.stop()
        await s.runtime.close()
    events = s.store.events_after(0, limit=1_000_000)
    cost = s.store.spent_usd()
    runs = s.store.list_runs()
    out = export_recording(
        run_dir / "qumus.db", Path(a.out_root) / a.name,
        title=a.title, layout=s.lab.bench,
        description=a.description or (
            f"{'OpenAI Agents SDK' if a.openai else 'Scripted agents'} on the testbench: "
            f"\"{a.request}\""
            + (" One placement was made askew on purpose." if a.askew else "")
            + "".join(f" Injected fault: {n}." for n, _ in a.fault)),
        meta={"request": a.request, "seed": a.seed,
              "model": os.environ.get("QUMUS_MODEL_LABEL") or (None if not a.openai else s.runtime.creds.model_id)})
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"recorded {len(events)} events, {len(runs)} jobs "
          f"({', '.join(r['status'] for r in runs)}), {time.monotonic() - t0:.0f} s, "
          f"${cost:.2f} -> {out} ({size / 1e6:.1f} MB)")
    print(f"recorded {out}; watch it in History")
    return 0


def _fault(text: str) -> tuple[str, dict]:
    from qumus2.testbench.faults import FAULTS

    name, _, params = text.partition("=")
    if name not in FAULTS:
        raise argparse.ArgumentTypeError(f"unknown fault {name!r}; one of {sorted(FAULTS)}")
    try:
        return name, json.loads(params) if params else {}
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"{name}: parameters are not JSON ({e})") from e


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--openai", action="store_true", help="OpenAI Agents SDK (costs money)")
    ap.add_argument("--name", default=None, help="folder name under recordings/")
    ap.add_argument("--out-root", default="recordings")
    ap.add_argument("--request", default=REQUEST)
    ap.add_argument("--mid-run", default=MID_RUN, help="message sent once hands start; '' = none")
    ap.add_argument("--no-askew", dest="askew", action="store_false")
    ap.add_argument("--fault", action="append", default=[], type=_fault, metavar="NAME[=JSON]",
                    help="also inject this testbench fault at the start (repeatable), e.g. "
                         "stamp_torn or pickup_fail_next='{\"count\": 1}'; it fires at its "
                         "first chance (stacking faults: when stacking starts)")
    ap.add_argument("--scenario", default="graphene_device", help="scripted mode only")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--budget", type=float, default=15.0,
                    help="USD cap for the whole recording, all turns together (OpenAI Agents SDK)")
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--title", default="Make a graphene device")
    ap.add_argument("--description", default="")
    a = ap.parse_args()
    if a.name is None:
        a.name = "graphene-device-" + ("openai" if a.openai else "scripted")
    if a.openai:
        from qumus2.agents.openai_credentials import load_openai_credentials

        if load_openai_credentials() is None:
            print("QUMUS_OPENAI_API_KEY is not set; see README (Mac setup).")
            return 2
    return asyncio.run(record(a))


if __name__ == "__main__":
    sys.exit(main())
