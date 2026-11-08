"""Time and fingerprint the first search fields of a hallbar_device run (free).

Runs the scripted hallbar_device scenario on the linked Hall-bar bench (like
scripts/record_hallbar_night.py) and, for the first `--fields` 10x scan fields,
writes one JSON line per field: the rendered picture's SHA-256, the v1
recognition result's digest, and how long drawing and recognising took. Then
it stops. Run it before and after a speed change and compare:

    python scripts/check_search_fields.py --bundle <target dir> --seed 8 \\
        --fields 60 --out runs/checks/fields-before.jsonl
    (change the code)
    python scripts/check_search_fields.py ... --out runs/checks/fields-after.jsonl
    python scripts/check_search_fields.py --compare runs/checks/fields-before.jsonl \\
        runs/checks/fields-after.jsonl

A speed change is acceptable only when every picture and every detection is
identical. The workspace is a throwaway folder under runs/checks/.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _digest(detections, work: Path) -> str:
    """What was found: position, size, material, thickness, confidence and the
    recogniser's notes, with this run's folder name taken out of any path."""
    root = str(work.resolve())
    rows = []
    for d in detections:
        data = d.model_dump(mode="json") if hasattr(d, "model_dump") else dict(vars(d))
        rows.append(json.dumps(data, sort_keys=True, default=repr).replace(root, "<run>"))
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def compare(a: Path, b: Path) -> int:
    ra = [json.loads(x) for x in a.read_text().splitlines()]
    rb = [json.loads(x) for x in b.read_text().splitlines()]
    n = min(len(ra), len(rb))
    pics = sum(x["picture"] != y["picture"] for x, y in zip(ra[:n], rb[:n], strict=True))
    dets = sum(x["detections"] != y["detections"] for x, y in zip(ra[:n], rb[:n], strict=True))

    def mean(rows, key):
        return sum(r[key] for r in rows[:n]) / max(n, 1)

    print(f"{n} fields compared: {pics} pictures differ, {dets} detection results differ")
    for key in ("render_s", "recognise_s", "field_wall_s"):
        print(f"  {key:13s} before {mean(ra, key):.3f} s   after {mean(rb, key):.3f} s")
    return 1 if pics or dets else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle")
    ap.add_argument("--seed", type=int, default=8)
    ap.add_argument("--fields", type=int, default=60)
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args(argv)
    if args.compare:
        return compare(Path(args.compare[0]), Path(args.compare[1]))
    if not args.bundle or not args.out:
        ap.error("--bundle and --out are needed (or --compare)")

    os.environ.setdefault("QUMUS_KLAYOUT_BACKEND", "python")
    from qumus2.app import build_services
    from qumus2.config import simulation_profile
    from qumus2.perception.rgb_material import RGBMaterialAnalyzer
    from qumus2.testbench.hallbar import load_imported_hallbar_target, with_hallbar_target
    from qumus2.testbench.render import MicroscopeRenderer

    Path("runs/checks").mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="fields-", dir="runs/checks"))
    bundle = Path(args.bundle).resolve()
    load_imported_hallbar_target(bundle, workspace=work)
    cfg = with_hallbar_target(simulation_profile("hallbar-linked"),
                              target=load_imported_hallbar_target(bundle))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sink = out.open("w")
    state = {"n": 0, "last": None}
    render_s: dict[str, float] = {}  # picture digest -> seconds to draw it

    render = MicroscopeRenderer.render_bytes

    def timed_render(self, device_id, meta):
        t0 = time.perf_counter()
        data = render(self, device_id, meta)
        if meta.get("objective") == 10:
            if len(render_s) > 64:
                render_s.clear()
            render_s[hashlib.sha256(data).hexdigest()] = time.perf_counter() - t0
        return data

    find = RGBMaterialAnalyzer.find_flakes

    def timed_find(self, frame, material, **kw):
        t0 = time.perf_counter()
        found = find(self, frame, material, **kw)
        dt = time.perf_counter() - t0
        now = time.perf_counter()
        wall = now - state["last"] if state["last"] is not None else 0.0
        state["last"] = now
        data = getattr(frame, "data", None)
        if data and frame.meta.get("objective") == 10 and not kw:
            pic = hashlib.sha256(data).hexdigest()
            state["n"] += 1
            sink.write(json.dumps({"field": state["n"], "material": material, "picture": pic,
                                   "detections": _digest(found, work), "n_detections": len(found),
                                   "render_s": round(render_s.get(pic, 0.0), 4),
                                   "recognise_s": round(dt, 4),
                                   "field_wall_s": round(wall, 4)}) + "\n")
            sink.flush()
            if state["n"] >= args.fields:
                print(f"{state['n']} fields written to {out}; workspace {work}", flush=True)
                import multiprocessing

                for child in multiprocessing.active_children():  # recognition workers
                    child.terminate()
                os._exit(0)
        return found

    MicroscopeRenderer.render_bytes = timed_render
    RGBMaterialAnalyzer.find_flakes = timed_find

    s = build_services(str(work / "qumus.db"), scenario="hallbar_device", seed=args.seed,
                       testbench_config=cfg)
    print(f"workspace {work}", flush=True)

    async def go() -> None:
        s.store.add_instruction("Make an hBN/graphene Hall bar on the eight-electrode chip "
                                "chip_e01, starting from the tapes.", project_id=s.project_id)
        await s.driver.wait_idle(timeout=6 * 3600)

    asyncio.run(go())
    print("the run ended before enough fields", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
