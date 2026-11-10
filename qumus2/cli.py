"""Command line.

    qumus2 serve     the brain: agents + web page, connected to the stations in stations.yaml
                     (starts qumus-simlab first when the stations are simulated and not running)
    qumus2 stations  print which station serves each module and whether it answers
    qumus2 export    turn a run store into a recording (History)
    qumus2 replay    serve one recording (scripts/record_video.py turns it into a video)
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any


def _serve(args: argparse.Namespace) -> None:
    import threading
    import webbrowser

    import uvicorn

    from qumus2.app import build_services
    from qumus2.ui.app import create_app
    from qumus2.ui.recording import attach_recorder

    scenario: Any = args.scenario
    seed = args.seed
    simlab_args = ["--target-bundle", args.target_bundle] if args.target_bundle else None
    resumed = None
    if args.resume:
        from qumus2.agents.scripted import load_scenario
        from qumus2.stations import config as stations_config
        from qumus2.stations.launcher import sim_offline
        from qumus2.stations.savepoint import restore_savepoint, resume_scenario

        st = stations_config.load(args.stations)
        if args.no_simlab or set(sim_offline(st)) != {m for m, s in st.modules.items() if s.kind == "sim"}:
            raise SystemExit("--resume starts qumus-simlab from the save point itself: stop the running "
                             "qumus-simlab first and do not pass --no-simlab")
        resumed = restore_savepoint(args.resume, args.db)
        # the lab half carries its own seed and target
        seed, simlab_args = None, ["--resume", str(resumed["lab_dir"])]
        from qumus2.runtime.store import RunStore

        restored = RunStore(args.db)
        try:
            projects = restored.list_projects()
            has_plan = bool(projects and (projects[0]["state"].get("device_plan") or {}).get("flake_roles"))
        finally:
            restored.close()
        scenario = resume_scenario(load_scenario(args.scenario), redo_plan=not has_plan)
        print(f"resuming from {resumed['resumed']['savepoint']} (event {resumed['last_event_id']}, "
              f"saved {resumed['saved_at']}, code {resumed.get('git_commit')})")
    if args.target_bundle:
        from qumus2.stations.target_bundle import import_target_bundle

        # the design tools read the target from the brain's own run folder
        import_target_bundle(args.target_bundle, Path(args.db).resolve().parent)
    services = build_services(args.db, stations=args.stations, scenario=scenario,
                              use_openai=args.openai and not args.mock,
                              run_budget_usd=args.budget, step_delay_s=0.6,
                              start_simlab=not args.no_simlab, simlab_seed=seed,
                              simlab_args=simlab_args)
    if not args.no_savepoints:
        from qumus2.stations.savepoint import SavePointWriter, savable

        if savable(services) is None:
            SavePointWriter(services, Path(args.db).resolve().parent / "savepoints", extra={
                "seed": args.seed if resumed is None else resumed.get("seed"),
                "target_bundle": args.target_bundle if resumed is None else resumed.get("target_bundle"),
                "scenario": args.scenario})
    if resumed is not None:
        from qumus2.stations.savepoint import RESUME_MESSAGE

        services.store.add_instruction(RESUME_MESSAGE, project_id=services.project_id)
    # bench state + real times next to the run store, for `qumus2 export` / replays
    recorder = attach_recorder(services)
    labs = ", ".join(f"{m} {s.url} ({s.kind})" for m, s in services.stations.modules.items())
    started = f"; started qumus-simlab (log {services.simlab.log_path})" \
        if services.simlab is not None and services.simlab.process is not None else ""
    url = f"http://{args.host}:{args.port}"
    print(f"Qumus brain ({services.runtime.mode} agents) on {url}")
    print(f"Stations: {labs}{started}")
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
        services.jobs.close()
        if services.simlab is not None:
            services.simlab.close()


def _stations(args: argparse.Namespace) -> None:
    from qumus2.stations import config
    from qumus2.stations.launcher import describe_stations

    st = config.load(args.stations)
    print(f"stations file: {st.path or 'packaged default'}; records from {st.records_from}; "
          f"simlab_dir {st.simlab_dir}")
    for row in describe_stations(st):
        print(f"  {row['module']:12s} {row['kind']:4s} {row['url']:32s} "
              f"{'online' if row['online'] else 'OFFLINE'}")


def _export(args: argparse.Namespace) -> None:
    from qumus2.ui.recording import export_recording, read_timeline, timeline_path

    after = 0
    if args.last_session:
        starts = [t["id"] for t in read_timeline(timeline_path(args.db)) if t.get("start")]
        after = starts[-1] if starts else 0
    out = export_recording(args.db, args.out, title=args.title, description=args.description,
                           after_id=after)
    print(f"exported to {out}; watch it in Qumus under History")


def _replay(args: argparse.Namespace) -> None:
    import uvicorn

    from qumus2.ui.replay import create_replay_app

    print(f"replay of {args.recording} on http://{args.host}:{args.port}/ (Ctrl+C to stop)")
    uvicorn.run(create_replay_app(args.recording), host=args.host, port=args.port, log_level="warning")


def _stack_dumps() -> None:
    """`kill -USR1 <pid>` prints every thread's stack to stderr (diagnoses a hung brain)."""
    import faulthandler
    import signal

    faulthandler.enable()
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _stack_dumps()
    ap = argparse.ArgumentParser(prog="qumus2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="start the brain (agents + web page) on the configured stations")
    sp.add_argument("--db", default="runs/sim-lab/manual/qumus.db")
    sp.add_argument("--stations", default=None,
                    help="stations.yaml (default: $QUMUS_STATIONS, ./stations.yaml, packaged default)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--seed", type=int, default=None,
                    help="seed for a qumus-simlab this command starts (ignored when it already runs)")
    sp.add_argument("--no-simlab", action="store_true",
                    help="never start qumus-simlab; fail if a simulated station does not answer")
    sp.add_argument("--target-bundle", default=None,
                    help="operator's imported Hall-bar target (target.json or its folder), passed to a "
                    "qumus-simlab this command starts")
    sp.add_argument("--stop-when-closed", type=float, default=None, metavar="SECONDS",
                    help="stop once no page has been open this long and nothing runs")
    sp.add_argument("--scenario", default="hallbar_device")
    sp.add_argument("--openai", action="store_true",
                    help="OpenAI Agents SDK through QUMUS_OPENAI_* (spends API credit)")
    sp.add_argument("--budget", type=float, default=None, help="estimated USD cap for this run")
    sp.add_argument("--mock", action="store_true", help="scripted agents (the default)")
    sp.add_argument("--resume", default=None, metavar="SAVEPOINT",
                    help="start from a save point (a savepoints/spNN folder, or a run folder: its newest); "
                    "needs a fresh --db and starts qumus-simlab from the save point")
    sp.add_argument("--no-savepoints", action="store_true",
                    help="do not write save points (default: one before flake selection, simulated "
                    "stations on this computer only)")
    sp.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    sp.set_defaults(fn=_serve)
    tp = sub.add_parser("stations", help="show the configured stations and whether they answer")
    tp.add_argument("--stations", default=None)
    tp.set_defaults(fn=_stations)
    rp = sub.add_parser("replay", help="serve one recording's replay page (for scripts/record_video.py)")
    rp.add_argument("recording", help="recording folder (recording.json + media/)")
    rp.add_argument("--host", default="127.0.0.1")
    rp.add_argument("--port", type=int, default=8792)
    rp.set_defaults(fn=_replay)
    ep = sub.add_parser("export", help="turn a run store into a recording (watch it in History)")
    ep.add_argument("--db", default="runs/sim-lab/manual/qumus.db")
    ep.add_argument("--out", required=True, help="output folder, e.g. recordings/my-run")
    ep.add_argument("--title", default="")
    ep.add_argument("--description", default="")
    ep.add_argument("--last-session", action="store_true",
                    help="only events since the last `serve` start")
    ep.set_defaults(fn=_export)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
