"""Command line: `qumus2 serve` (UI + testbench), `qumus2 demo` (headless) and
`qumus2 lab` (the lab process; `serve --lab-process` starts it under a supervisor)."""

from __future__ import annotations

import argparse
import asyncio
import logging


def _serve_config(args: argparse.Namespace, *, prepare: bool = True):
    """Resolve the operator's target before services or physical bench construction."""
    import json
    import sqlite3
    from pathlib import Path

    from qumus2.config import SIM_PROFILE, simulation_profile
    from qumus2.records.device_design import parse_hallbar_target, target_import_binding
    from qumus2.testbench.hallbar import load_imported_hallbar_target, with_hallbar_target

    database = Path(args.db).resolve()
    owner_workspace = database.parent.name == "hallbar-linked-owner-20261101"
    selected = getattr(args, "target_bundle", None)
    if owner_workspace and not selected:
        raise ValueError("The owner Hall-bar workspace requires its explicit imported target bundle")
    # One bench only: the v1-linked Hall-bar laboratory (owner, 2026-11-10).
    config = simulation_profile(SIM_PROFILE)
    if selected:
        bundle = Path(selected).resolve()
        if bundle.name == "target.json":
            bundle = bundle.parent
        target = load_imported_hallbar_target(bundle,
            approval_reference=getattr(args, "target_approval_reference", None),
            approval_sha256=getattr(args, "target_approval_sha256", None))
        # Never retrofit a prior workspace/checkpoint with a new physical target.
        if database.exists():
            wal = database.with_name(database.name + "-wal")
            if wal.exists() and wal.stat().st_size:
                raise ValueError("Owner workspace has an active/uncheckpointed WAL; stop it cleanly before startup validation")
            # mode=ro alone may create WAL/SHM files for a closed WAL database.
            with sqlite3.connect(database.as_uri()+"?mode=ro&immutable=1", uri=True) as connection:
                row = connection.execute("SELECT props_json FROM sample WHERE id='chip_e01'").fetchone()
            if wal.exists() and wal.stat().st_size:
                raise ValueError("Owner workspace changed during read-only startup validation")
            props = json.loads(row[0]) if row else {}
            if (parse_hallbar_target(props.get("device_design_target")) != target
                    or props.get("imported_target_binding") != target_import_binding(target)):
                raise ValueError("Existing workspace target differs; use a fresh workspace for changed geometry")
        # This is explicit startup preparation; preflight/HTTP checks never copy.
        if prepare:
            load_imported_hallbar_target(bundle, workspace=database.parent,
                approval_reference=getattr(args, "target_approval_reference", None),
                approval_sha256=getattr(args, "target_approval_sha256", None))
        config = with_hallbar_target(config, target=target)
    return config


def _serve(args: argparse.Namespace) -> None:
    import threading
    import webbrowser

    import uvicorn

    from qumus2.app import build_services
    from qumus2.ui.app import create_app
    from qumus2.ui.recording import attach_recorder

    testbench_config = _serve_config(args)
    services = build_services(args.db, scenario=args.scenario,
                              use_openai=args.openai and not args.mock,
                              run_budget_usd=args.budget, step_delay_s=0.6,
                              lab_process=args.lab_process, testbench_config=testbench_config,
                              contract_lab=args.contract_lab)
    # bench state + real times next to the run store, for `qumus2 export` / replays
    recorder = attach_recorder(services)
    lab = f", lab process pid {services.supervisor.pid}" if services.supervisor else ""
    if services.simlab is not None:
        lab = ", sim lab via contract v1: " + ", ".join(
            f"{m} {u}" for m, u in services.simlab.urls.items())
    url = f"http://{args.host}:{args.port}"
    print(f"Qumus v2 ({services.runtime.mode} agents{lab}) on {url}")
    print("Ctrl+C to stop." + (" Model requests spend API credit only while processing a message."
                               if services.runtime.mode == "sdk" else ""))
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        # a page still open must not hold Ctrl+C: what is left after 3 s is cancelled
        uvicorn.run(create_app(services, stop_when_closed_s=args.stop_when_closed),
                    host=args.host, port=args.port, log_level="warning",
                    timeout_graceful_shutdown=3)
    finally:
        if recorder:
            recorder.stop()
        if services.supervisor:
            services.supervisor.stop()
        if services.simlab is not None:
            services.jobs.close()
            services.simlab.close()


def _export(args: argparse.Namespace) -> None:
    from qumus2.ui.recording import export_recording, read_timeline, timeline_path

    after = 0
    if args.last_session:
        starts = [t["id"] for t in read_timeline(timeline_path(args.db)) if t.get("start")]
        after = starts[-1] if starts else 0
    out = export_recording(args.db, args.out, title=args.title, description=args.description,
                           after_id=after)
    print(f"exported to {out}; watch it in Qumus under History")


def _lab(args: argparse.Namespace) -> None:
    from qumus2.runtime.labproc import main

    main(args)


def _simlab(args: argparse.Namespace) -> None:
    from qumus2.simlab.server import main as simlab_main

    simlab_main(args)


def _demo(args: argparse.Namespace) -> None:
    import tempfile

    from qumus2.app import build_services

    # a throwaway file store, so the testbench saves the microscope images the
    # picker needs (":memory:" writes none)
    tmp = tempfile.mkdtemp(prefix="qumus2-demo-")
    s = build_services(f"{tmp}/qumus.db", scenario=args.scenario)
    if args.askew:
        s.lab.world.faults.next_placement = {"dx": 2.0, "dy": -1.0, "drot": 6.0}

    async def go() -> None:
        s.store.add_instruction(args.message, project_id=s.project_id)
        await s.driver.wait_idle(timeout=60)

    asyncio.run(go())
    for e in s.store.events_after(0, limit=1_000_000):
        if e.kind == "device.command":
            continue
        p = e.payload
        text = p.get("text") or p.get("summary") or p.get("task") or p.get("reason") or ""
        extra = ""
        if e.kind == "step.verified":
            m = p.get("measurements", {})
            extra = f"verdict={p.get('verdict')} offset={m.get('offset_mm')}mm " \
                    f"rot={m.get('drot_deg')}deg"
        elif e.kind in ("tool.call", "tool.result", "tool.denied"):
            extra = f"{p.get('tool')} {p.get('status', '')}"
        elif e.kind == "agent.delegation_checked":
            extra = f"{p.get('to')} {p.get('step')} -> {p.get('status')}: " + " ".join(
                p.get("facts", []))
        elif e.kind in ("job.status", "step.finished", "step.started"):
            extra = f"{p.get('name', '')} {p.get('status', '')}"
        print(f"{e.id:4d} {e.kind:16s} {e.agent or '':12s} {extra} {text}")


def _report(args: argparse.Namespace) -> None:
    from pathlib import Path

    from qumus2.app import build_services
    from qumus2.testbench.report import collect_not_verifiable, render_markdown, run_scenarios

    s = build_services(":memory:", seed=args.seed)
    runs = run_scenarios(s)
    text = render_markdown(collect_not_verifiable(s.store), runs)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(text)


def _chaos(args: argparse.Namespace) -> None:
    from pathlib import Path

    from qumus2.testbench.chaos import render_markdown, run_one, seeds_for

    runs = []
    seeds = (seeds_for(args.fault, args.seeds, args.first_seed) if args.fault
             else range(args.first_seed, args.first_seed + args.seeds))
    for seed in seeds:
        r = run_one(seed, args.faults)
        print(f"seed {seed}: {r.outcome} "
              f"{[(i['fault'], 'fired' if i['fired'] else 'no effect') for i in r.injected]} "
              f"{[f['status'] for f in r.flows]}", flush=True)
        runs.append(r)
    text = render_markdown(runs, args.faults)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(text)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="qumus2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="start testbench + agents + web UI")
    sp.add_argument("--db", default="runs/checks/manual/qumus.db")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--stop-when-closed", type=float, default=None, metavar="SECONDS",
                    help="stop once no page has been open this long and nothing runs")
    sp.add_argument("--scenario", default="hallbar_device")
    sp.add_argument("--openai", action="store_true",
                         help="OpenAI Agents SDK through QUMUS_OPENAI_* (spends API credit)")
    sp.add_argument("--budget", type=float, default=None, help="estimated USD cap for this run")
    sp.add_argument("--mock", action="store_true", help="scripted agents (the default)")
    sp.add_argument("--target-bundle", help="operator-selected private target.json or its directory; no synthetic fallback")
    sp.add_argument("--target-approval-reference", help="optional expected owner-instruction identity; verifies, never overrides")
    sp.add_argument("--target-approval-sha256", help="optional expected owner-instruction hash")
    sp.add_argument("--lab-process", action="store_true",
                    help="run devices, safety and jobs in a separate supervised process")
    sp.add_argument("--contract-lab", action="store_true",
                    help="agents reach the simulated lab only through contract v1 module "
                    "services (exfoliation :8781, stacking :8782)")
    sp.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    sp.set_defaults(fn=_serve)
    lp = sub.add_parser("lab", help="the lab process (started by serve --lab-process)")
    lp.add_argument("--db", default="runs/checks/manual/qumus.db")
    lp.add_argument("--seed", type=int, default=None)
    lp.add_argument("--image-dir", default=None)
    lp.add_argument("--heartbeat", type=float, default=0.5, help="heartbeat interval (s)")
    lp.set_defaults(fn=_lab)
    sl = sub.add_parser("simlab", help="the simulated lab as contract v1 services "
                        "(exfoliation :8781, stacking :8782; no agents, no model calls)")
    sl.add_argument("--db", default="runs/checks/simlab/qumus.db")
    sl.add_argument("--seed", type=int, default=None)
    sl.add_argument("--host", default="127.0.0.1", help="keep loopback (contract rule)")
    sl.add_argument("--exfoliation-port", type=int, default=8781)
    sl.add_argument("--stacking-port", type=int, default=8782)
    sl.add_argument("--motion-delay", type=float, default=0.3,
                    help="wall seconds per motion segment, so Stop can be tried by hand")
    sl.set_defaults(fn=_simlab)
    ep = sub.add_parser("export", help="turn a run store into a recording (watch it in History)")
    ep.add_argument("--db", default="runs/checks/manual/qumus.db")
    ep.add_argument("--out", required=True, help="output folder, e.g. recordings/my-run")
    ep.add_argument("--title", default="")
    ep.add_argument("--description", default="")
    ep.add_argument("--last-session", action="store_true",
                    help="only events since the last `serve` start")
    ep.set_defaults(fn=_export)
    dp = sub.add_parser("demo", help="run the scripted scenario headless and print events")
    dp.add_argument("--scenario", default="place_chip_demo")
    dp.add_argument("--message", default="Put a chip on the transfer stage")
    dp.add_argument("--askew", action="store_true", help="inject a bad first placement")
    dp.set_defaults(fn=_demo)
    rp = sub.add_parser("report", help="run every flow on the testbench and list the steps "
                        "that cannot be verified (sensor wish-list)")
    rp.add_argument("--out", default="runs/checks/not-verifiable.md")
    rp.add_argument("--seed", type=int, default=1)
    rp.set_defaults(fn=_report)
    cp = sub.add_parser("chaos", help="many seeded runs of the stacking task with injected "
                        "faults, each checked against the simulated world (free, no API)")
    cp.add_argument("--seeds", type=int, default=40, help="runs (2 per fault at 40)")
    cp.add_argument("--first-seed", type=int, default=1)
    cp.add_argument("--faults", type=int, default=1,
                    help="faults per run (the first cycles through all faults by seed)")
    cp.add_argument("--fault", default=None,
                    help="only seeds whose first fault is this one (e.g. stamp_torn)")
    cp.add_argument("--out", default="runs/checks/chaos.md")
    cp.set_defaults(fn=_chaos)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
