"""Opt-in live model checks on the testbench, using the project's .env.

    python scripts/check_model.py connect
    python scripts/check_model.py place --askew
    python scripts/check_model.py device --fault stamp_torn

Paid model calls; default estimated caps: $20/test, $200/project phase.
The shared spend ledger survives separate invocations and failed responses.
No hardware drivers are loaded. Results live under runs/checks/model/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qumus2.agents.openai_credentials import load_openai_credentials  # noqa: E402
from qumus2.agents.openai_runtime import OpenAIRuntime  # noqa: E402
from qumus2.agents.scripted import ScriptedRuntime, load_scenario  # noqa: E402
from qumus2.app import build_services  # noqa: E402
from qumus2.config import load_yaml  # noqa: E402
from qumus2.ui.recording import attach_recorder, export_recording  # noqa: E402

TASKS = {
    "connect": "Say who you are in one sentence. This is a connection test; answer directly.",
    "place": "Place chip_001 on the transfer stage. Inspect the bench first, delegate the "
             "placement to hands, wait for its verified outcome and report the result.",
    "flake": "Get graphene flakes: exfoliate graphene (Gr) onto one bare chip, search it "
             "under the microscope, record the flakes and return the chip to storage. "
             "Follow the advisor's recipe. Report the verified outcome.",
    "device": "Make an hBN-encapsulated graphene Hall bar using the available patterned "
              "electrode chip. Have the advisor specify the device; obtain enough hBN "
              "and graphene flakes, select using microscope photos, submit a checked "
              "layout, then stack the device. Continue autonomously through the available "
              "operations and report the result; distinguish simulated results from real "
              "experimental evidence. External fabrication can be recorded as external.",
    "resume": "This is a restart verification of the existing completed project. Read "
              "get_project_context and report the saved device ID, chip, and completed "
              "jobs. Do not create a new plan or a new device; no hardware action is "
              "requested. Confirm which records you used.",
    "stack": "This diagnostic starts with a SCRIPTED TEST FIXTURE: flakes are already "
             "selected and the layout is code-accepted. Read get_project_context, have "
             "eyes inspect the bench, then delegate hands to stack that exact selection "
             "on chip_e01. Wait for the verified result and handle any recoverable "
             "fault. Preserve the existing device plan, selection and layout. Report "
             "stacking only; do not claim you prepared or selected the fixture.",
}


async def prepare_stacking_fixture(s) -> None:
    """Use the unchanged scripted scenario up to accepted design; stop before stacking."""
    scenario = load_scenario("graphene_device")

    def prefix(actions):
        out = []
        for action in actions:
            delegation = action.get("delegate", {})
            if delegation.get("to") == "hands" and delegation.get("task", "").startswith("Stack "):
                break
            if "branch" in action:
                body = action["branch"]
                body["cases"] = {k: prefix(v) for k, v in body.get("cases", {}).items()}
                if "default" in body:
                    body["default"] = prefix(body["default"])
            out.append(action)
        return out

    for turn in scenario["turns"]:
        turn["actions"] = prefix(turn["actions"])
    await ScriptedRuntime(s.gateway, scenario).send("Prepare scripted stacking fixture")
    state = s.store.get_project(s.project_id)["state"]
    if not state.get("design_accepted") or not state.get("selected_flakes"):
        raise RuntimeError("scripted stacking fixture could not prepare an accepted design")
    s.store.emit("notification", {"level": "info", "text": (
        "TEST FIXTURE: the scripted scenario prepared flakes and an accepted layout. "
        "The live OpenAI check begins here and tests stacking/recovery only.")})


async def run(args: argparse.Namespace) -> bool:
    creds = load_openai_credentials()
    if creds is None:
        raise SystemExit("Fill QUMUS_OPENAI_API_KEY and QUMUS_OPENAI_MODEL in .env first.")
    folder = Path(args.resume or args.out or f"runs/checks/model/{args.case}-{uuid4().hex[:8]}")
    folder.mkdir(parents=True, exist_ok=bool(args.resume))
    if args.resume and not args.lab_process:
        raise SystemExit("--resume requires --lab-process to restore the actual simulated bench")
    s = build_services(folder / "qumus.db", seed=args.seed, lab_process=args.lab_process)
    if args.case == "stack" and not args.resume:
        await prepare_stacking_fixture(s)
    initial_jobs = {r["id"] for r in s.store.list_runs()}
    cfg = load_yaml("agents.yaml")
    cfg["agents"]["orchestrator"]["max_turns"] = 100
    rt = OpenAIRuntime(s.gateway, creds, run_budget_usd=min(args.budget, 20), config=cfg)
    s.runtime = rt
    s.driver.runtime = rt
    s.store.emit("notification", {"level": "info", "text": "agents: sdk mode (OpenAI)"})
    if args.askew:
        s.lab.faults.inject("next_placement", {"dx": 2.0, "dy": -1.0, "drot": 6.0})
        s.store.emit("fault.injected", {"fault": "next_placement",
                                      "params": {"dx": 2.0, "dy": -1.0, "drot": 6.0}})
    if args.fault:
        s.lab.faults.inject(args.fault, {})
        s.store.emit("fault.injected", {"fault": args.fault, "params": {}})
    recorder = attach_recorder(s)
    error = None
    try:
        s.store.add_instruction(TASKS[args.case], project_id=s.project_id)
        await s.driver.wait_idle(timeout=args.timeout)
    except Exception as exc:
        error = str(exc)  # runtime has already redacted model exceptions
    finally:
        await rt.close()
        recorder.stop()
        if s.supervisor:
            s.supervisor.stop()
    events = s.store.events_after(0, limit=1_000_000)
    turns = [e.payload for e in events if e.kind == "agent.turn_end"]
    jobs = [{"flow": r["flow"], "status": r["status"], "id": r["id"]} for r in s.store.list_runs()]
    devices = s.store.list_samples(kind="device")
    new_hardware_jobs = [j for j in jobs if j["id"] not in initial_jobs and j["flow"] != "inspect_bench"]
    violations = sum(e.kind == "safety.violation" for e in events)
    stamp_swaps = [swap for r in s.store.list_runs() for swap in r["params"].get("stamp_swaps", [])]
    if args.case == "connect":
        outcome = bool(turns) and not turns[-1]["is_error"]
    elif args.case == "place":
        outcome = s.store.get_sample("chip_001")["location"] == "transfer_stage"
    elif args.case == "flake":
        outcome = any(f["status"] == "catalogued" for f in s.store.list_samples(kind="flake"))
    else:
        outcome = bool(devices) and all(d["status"] == "stacked" for d in devices)
    if args.case == "resume":
        outcome = outcome and not new_hardware_jobs
    if args.fault == "stamp_torn":
        outcome = outcome and bool(stamp_swaps)  # an unfired fault is not a passed fault test
    outcome = outcome and not error and bool(turns) and not turns[-1]["is_error"] and not violations
    summary = {"case": args.case, "model": creds.model_id, "provider": "openai", "passed": outcome,
               "fixture": "scripted preparation through accepted design" if args.case == "stack" else None,
               "error": error, "estimated_usd": s.store.spent_usd(), "jobs": jobs,
               "safety_violations": violations, "new_hardware_jobs": new_hardware_jobs,
               "requested_fault": args.fault, "stamp_swaps": stamp_swaps,
               "denials": [e.payload for e in events if e.kind == "tool.denied"],
               "messages": [{"agent": e.agent, "text": e.payload["text"]}
                            for e in events if e.kind == "agent.message"], "turns": turns}
    summary_name = f"summary-resume-{uuid4().hex[:6]}.json" if args.resume else "summary.json"
    (folder / summary_name).write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    recording = folder / (f"recording-resume-{uuid4().hex[:6]}" if args.resume else "recording")
    export_recording(folder / "qumus.db", recording, title=f"Model check: {args.case}",
                     meta={"provider": "openai", "model": creds.model_id,
                           "cost_basis": "configured_token_rates"})
    print(json.dumps({"passed": outcome, "case": args.case, "results": str(folder),
                      "estimated_usd": summary["estimated_usd"], "error": error}, indent=2))
    return outcome


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("case", choices=TASKS)
    ap.add_argument("--budget", type=float, default=20)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=1200)
    ap.add_argument("--out")
    ap.add_argument("--resume", help="continue an existing check folder with its saved bench")
    ap.add_argument("--lab-process", action="store_true", help="persist the bench in a supervised process")
    ap.add_argument("--askew", action="store_true")
    ap.add_argument("--fault", choices=("stamp_torn", "pickup_fail_next", "pick_fail_next"))
    args = ap.parse_args()
    if not 0 < args.budget <= 20:
        ap.error("budget must be in (0, 20] USD")
    if args.case == "resume" and not args.resume:
        ap.error("resume case requires --resume <existing check folder> --lab-process")
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
