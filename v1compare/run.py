"""Run a comparison suite (each trial in its own process) and write a report.

    python -m v1compare.run --out runs/checks/v1compare/flows-1 --seeds 1-3
    python -m v1compare.run --out runs/checks/v1compare/flows-1 --report-only

One process per trial: v1 binds its databases and keeps module-level state at
import, so trials must not share a process.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from v1compare.trial import SCENARIOS


def _seeds(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def run_one(system: str, scenario: str, seed: int, out: Path) -> dict[str, Any]:
    name = f"{system}_{scenario}_s{seed}"
    res_path = out / "results" / f"{name}.json"
    if res_path.exists():
        return json.loads(res_path.read_text())
    res_path.parent.mkdir(parents=True, exist_ok=True)
    work = out / "work" / name
    cmd = [sys.executable, "-m", "v1compare.trial", "--system", system, "--scenario", scenario,
           "--seed", str(seed), "--workdir", str(work), "--out", str(res_path)]
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    p = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=3600)
    (out / "work").mkdir(parents=True, exist_ok=True)
    (out / "work" / f"{name}.stderr.txt").write_text(p.stderr[-20000:], encoding="utf-8")
    if not res_path.exists():
        res = {"system": system, "scenario": scenario, "seed": seed,
               "harness_error": f"exit {p.returncode}: {p.stderr[-3000:]}"}
        res_path.write_text(json.dumps(res, indent=2))
    return json.loads(res_path.read_text())


def _pct(n: int, d: int) -> str:
    return f"{n}/{d}" if d else "-"


def report(results: list[dict[str, Any]]) -> str:
    systems = sorted({r["system"] for r in results})
    scenarios = [s for s in SCENARIOS if any(r["scenario"] == s for r in results)]
    lines = ["# v1 vs v2 on the testbench: graphene task, flows mode", "",
             "Each cell: trials that really succeeded / trials. A real success needs the "
             "world to show the job done and the system's records to agree with it (the "
             "same rule as `qumus2 chaos`). `false ok` = the system "
             "reported success but the testbench shows the job not done. `extra chips` = "
             "chips exfoliated beyond the one asked for (a cost). Flows mode runs "
             "each system's own procedures without a model, so this compares control logic "
             "and error handling, not the agents and not real lab yield.", ""]
    head = "| scenario | " + " | ".join(
        f"{s} success | {s} false ok | {s} human steps | {s} extra chips"
        for s in systems) + " |"
    lines += [head, "|" + "---|" * (1 + 4 * len(systems))]
    for sc in scenarios:
        row = [sc]
        for sy in systems:
            rs = [r for r in results if r["system"] == sy and r["scenario"] == sc]
            ok = [r for r in rs if "score" in r]
            succ = sum(r["score"]["truth_success"] for r in ok)
            false_ok = sum(r["score"]["false_success"] for r in ok)
            hum = sum(r.get("interventions", 0) for r in ok)
            extra = sum(r["score"].get("extra_chips_used", 0) for r in ok)
            err = len(rs) - len(ok)
            row += [_pct(succ, len(rs)) + (f" ({err} harness error)" if err else ""),
                    str(false_ok), str(hum), str(extra)]
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## Flake records", "",
              "| system | trials | catalogued | real and distinct | label right | "
              "real monolayers found / on chip |", "|---|---|---|---|---|---|"]
    for sy in systems:
        ok = [r for r in results if r["system"] == sy and "score" in r]
        t = {k: sum(r["score"][k] for r in ok) for k in
             ("recorded_flakes", "recorded_real", "recorded_label_correct",
              "recorded_monolayers_real", "true_monolayers")}
        lines.append(f"| {sy} | {len(ok)} | {t['recorded_flakes']} | {t['recorded_real']} | "
                     f"{t['recorded_label_correct']} | {t['recorded_monolayers_real']} / "
                     f"{t['true_monolayers']} |")
    lines += ["", "## Every trial", ""]
    for r in sorted(results, key=lambda r: (r["scenario"], r["system"], r["seed"])):
        tag = f"{r['scenario']} / {r['system']} / seed {r['seed']}"
        if "score" not in r:
            lines.append(f"- {tag}: harness error: `{str(r.get('harness_error'))[-300:]}`")
            continue
        s = r["score"]
        verdict = "success" if s["truth_success"] else "FAILED"
        claim = "reported success" if s["claimed_success"] else "reported failure"
        extra = "; ".join(s["problems"]) or "no problems"
        lines.append(f"- {tag}: {verdict}, {claim}, {r.get('interventions', 0)} human "
                     f"step(s), {r.get('device_commands')} device commands. {extra}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--systems", default="v1,v2")
    ap.add_argument("--scenarios", default="all")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    if a.report_only:
        results = [json.loads(p.read_text()) for p in sorted((out / "results").glob("*.json"))]
    else:
        scenarios = list(SCENARIOS) if a.scenarios == "all" else a.scenarios.split(",")
        plan = [(sy, sc, seed) for sc in scenarios for seed in _seeds(a.seeds)
                for sy in a.systems.split(",")]
        with ThreadPoolExecutor(a.jobs) as pool:
            results = list(pool.map(lambda t: run_one(*t, out), plan))
    text = report(results)
    (out / "report.md").write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
