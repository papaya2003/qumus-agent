"""Record one hallbar_device run on the linked Hall-bar bench and export it.

Free by default (scripted agents). `--openai` uses the configured OpenAI
credentials and spends API credit, capped by `--budget`. The raw run
data goes to `--workspace` (default `runs/checks/<name>`), and the finished
recording is exported to `--out` (default `recordings/<name>`, where History
lists it). Export clones the photos (APFS clonefile / hard link), so the
recording takes no second copy of disk space (#203). Nothing is written into
an existing workspace.

    python scripts/record_hallbar_night.py --bundle <target dir> --seed 2 \
        [--out <recording dir>] [--openai --budget 40]

Save points: just before the picker reads the flake candidates (the search is
over), the run saves the whole simulated lab and run store under
`<workspace>/checkpoints/` (only the latest is kept, about 200 MB). A later run can start
there instead of exfoliating and searching again:

    python scripts/record_hallbar_night.py --bundle <target dir> \
        --resume runs/checks/<earlier workspace> [--openai --budget 10]

The seed comes from the save point. Resuming refuses when a file that decides
the search changed since the save (testbench/checkpoint.py GUARDED); pass
`--accept-changed <file>` (or `all`) only for a change proven not to alter the
search. Keep the earlier workspace: the resumed records point at its photos
until the recording is exported. The live model can resume a scripted save
point (same world): it then pays only from pairing on.

The recording embeds the imported electrode geometry: keep it out of GitHub.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

GOAL = ("Make an hBN/graphene Hall bar on the eight-electrode chip chip_e01, "
        "starting from the tapes.")
CONTINUE = ("Please continue from where you stopped until the device is stacked on "
            "chip_e01 and checked.")
RESUME = ("Make an hBN/graphene Hall bar on the eight-electrode chip chip_e01. The tapes are "
          "already exfoliated and the chips searched: the laboratory record lists every chip "
          "and flake found so far. Continue from flake selection until the device is stacked "
          "on chip_e01 and checked.")
RESUME_SAY = ("Resuming from a save point: the exfoliated chips and the flakes found on them "
              "are in the laboratory record, so I continue with the pair selection.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, help="imported target bundle directory")
    ap.add_argument("--seed", type=int, default=None, help="world seed (default 2; resume: the save point's)")
    ap.add_argument("--resume", default=None,
                    help="start from a save point: a workspace (its newest) or a checkpoints/cpNN folder")
    ap.add_argument("--accept-changed", action="append", default=[], metavar="FILE",
                    help="resume although this guarded file changed since the save (repeat; 'all')")
    ap.add_argument("--no-checkpoints", action="store_true", help="do not write save points")
    ap.add_argument("--out", default=None, help="recording folder (default recordings/<name>)")
    ap.add_argument("--workspace", default=None, help="fresh run folder (default runs/checks/<name>)")
    ap.add_argument("--openai", action="store_true", help="live model (spends API credit)")
    ap.add_argument("--budget", type=float, default=None, help="estimated USD cap (with --openai)")
    ap.add_argument("--timeout", type=float, default=4 * 3600, help="seconds per instruction")
    ap.add_argument("--continue-max", type=int, default=3,
                    help="live model only: extra 'continue' instructions if it stops early")
    ap.add_argument("--error-retries", type=int, default=0,
                    help="live model only: after a provider error, wait and continue (count)")
    ap.add_argument("--error-wait", type=float, default=330.0, help="seconds to wait after an error")
    ap.add_argument("--http-retries", type=int, default=8,
                    help="live model only: HTTP retries per model request (429/5xx, honours retry-after)")
    ap.add_argument("--step-delay", type=float, default=0.0)
    ap.add_argument("--title", default="")
    args = ap.parse_args(argv)
    if args.openai and args.budget is None:
        ap.error("--openai needs --budget")

    os.environ.setdefault("QUMUS_KLAYOUT_BACKEND", "python")
    from qumus2.agents.scripted import load_scenario
    from qumus2.app import build_services
    from qumus2.config import simulation_profile
    from qumus2.testbench.checkpoint import (
        CheckpointWriter,
        StaleCheckpoint,
        find_checkpoint,
        restore_checkpoint,
        resume_scenario,
    )
    from qumus2.testbench.hallbar import load_imported_hallbar_target, with_hallbar_target
    from qumus2.ui.recording import attach_recorder, check_recording, export_recording

    saved: dict = {}
    if args.resume:
        cp_meta = json.loads((find_checkpoint(args.resume) / "checkpoint.json").read_text())
        if args.seed is not None and args.seed != cp_meta.get("seed"):
            ap.error(f"the save point was made with seed {cp_meta.get('seed')}")
        args.seed = cp_meta.get("seed")
    if args.seed is None:
        args.seed = 2
    name = (f"hallbar-{'live' if args.openai else 'scripted'}-seed{args.seed}-"
            f"{time.strftime('%Y%m%d-%H%M')}{'-resumed' if args.resume else ''}")
    work = Path(args.workspace or Path("runs") / "checks" / name)
    out_dir = Path(args.out or Path("recordings") / name)
    work.mkdir(parents=True, exist_ok=True)
    if (work / "qumus.db").exists():
        raise SystemExit(f"{work} already has a run store; use a fresh workspace")
    bundle = Path(args.bundle).resolve()
    scenario: str | dict = "hallbar_device"
    if args.resume:
        try:
            saved = restore_checkpoint(args.resume, work, accept_changed=args.accept_changed)
        except StaleCheckpoint as e:
            raise SystemExit(f"cannot resume: {e}. Run from the start, or pass --accept-changed "
                             "for a change proven not to alter the search.") from None
        if saved.get("bundle") and Path(saved["bundle"]).resolve() != bundle:
            raise SystemExit(f"the save point used the target bundle {saved['bundle']}")
        scenario = resume_scenario(load_scenario("hallbar_device"), say=RESUME_SAY)
        print(f"resuming from {saved['resumed']['checkpoint']} (event {saved['last_event_id']}, "
              f"saved {saved['saved_at']}, code {saved.get('git_commit')})", flush=True)
        if saved["changed"]:
            print(f"accepted changes since the save: {', '.join(saved['changed'])}", flush=True)
    load_imported_hallbar_target(bundle, workspace=work)
    cfg = with_hallbar_target(simulation_profile("hallbar-linked"),
                              target=load_imported_hallbar_target(bundle))
    s = build_services(str(work / "qumus.db"), scenario=scenario, seed=args.seed,
                       testbench_config=cfg, use_openai=args.openai,
                       run_budget_usd=args.budget, step_delay_s=args.step_delay,
                       http_max_retries=args.http_retries,
                       state_path=saved.get("state_path"))
    if saved:
        s.store.emit("notification", {"level": "info", "text": (
            f"Resumed from a save point of {Path(saved['workspace']).name} (event "
            f"{saved['last_event_id']}, code {saved.get('git_commit')}): the events before this "
            "one come from that run.")})
    if not args.no_checkpoints:
        CheckpointWriter(s, work / "checkpoints", extra={
            "seed": args.seed, "bundle": str(bundle), "scenario": "hallbar_device"},
            log=lambda m: print(m, flush=True))
    recorder = attach_recorder(s)
    print(f"workspace {work}  agents {s.runtime.mode}", flush=True)
    t0 = time.time()

    def stacked() -> bool:
        for e in s.store.events_after(0, limit=1_000_000):
            if e.kind == "tool.result" and e.payload.get("tool") == "wait_job" \
                    and "stack" in json.dumps(e.payload, default=str).lower() \
                    and e.payload.get("status") in ("ok", "corrected"):
                return True
        return False

    async def go() -> None:
        first = RESUME if (saved and args.openai) else GOAL
        texts = [first] + ([CONTINUE] * args.continue_max if args.openai else [])
        errors_left = args.error_retries if args.openai else 0
        i = 0
        while i < len(texts):
            if i and stacked():
                break
            s.store.add_instruction(texts[i], project_id=s.project_id)
            try:
                await s.driver.wait_idle(timeout=args.timeout)
            except Exception as e:  # noqa: BLE001 - report and export what exists
                print(f"instruction {i}: {type(e).__name__}: {e}", flush=True)
                if errors_left <= 0 or isinstance(e, TimeoutError):
                    break
                # A provider error (e.g. 503 overloaded) ends the turn: wait,
                # then ask the same session to continue where it stopped.
                errors_left -= 1
                print(f"waiting {args.error_wait:.0f} s, then continuing "
                      f"({errors_left} error retries left)", flush=True)
                await asyncio.sleep(args.error_wait)
                texts.insert(i + 1, CONTINUE)
            else:
                print(f"instruction {i} done after {time.time() - t0:.0f} s", flush=True)
            i += 1

    asyncio.run(go())
    if recorder:
        recorder.stop()
    # Simulator truth at the end (private: flakes, stack, electrode geometry),
    # kept in the workspace only, for an offline sim-truth check of the device.
    lab = getattr(s, "lab", None)
    if lab is not None and hasattr(getattr(lab, "world", None), "evaluation_snapshot"):
        (work / "truth_end.json").write_text(json.dumps(lab.world.evaluation_snapshot(), indent=1))
    title = args.title or (f"hBN/Gr Hall bar on the eight-electrode chip "
                           f"({'live model' if args.openai else 'scripted'}, seed {args.seed})")
    description = ("hallbar_device on the linked Hall-bar bench with the operator's imported "
                   "electrode target.")
    if saved:
        if "resumed after the search" not in title:
            title += " — resumed after the search"
        description += (f" Resumed from a save point of {Path(saved['workspace']).name} "
                        f"(code {saved.get('git_commit')}); exfoliation and search come from that run.")
    out = export_recording(work / "qumus.db", out_dir, title=title, description=description,
                           meta={"resumed_from": saved["resumed"]} if saved else None,
                           extra_roots=[saved["photo_root"]] if saved else ())
    problems = check_recording(out)
    meta = json.loads((out / "recording.json").read_text())["meta"]
    print(json.dumps({"out": str(out), "wall_s": round(time.time() - t0),
                      "cost_usd": meta.get("cost_usd"), "n_events": meta.get("n_events"),
                      "problems": problems}), flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
