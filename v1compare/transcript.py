"""Readable transcript of a v2 run from its run store (for reviewing agent runs).

    python -m v1compare.transcript runs/checks/v1compare/agent/v2_baseline_s1/qumus.db > t.txt

One line per event that matters for judging the agents: who said what, which
tool was called with what and what came back, policy denials, job and step
outcomes, injected faults and safety violations. Device commands and world
snapshots are counted, not listed. Long texts are cut to --width characters.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from qumus2.runtime.store import RunStore

SKIP = {"device.command", "world.snapshot"}


def _short(v: Any, width: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= width else s[: width - 3] + "..."


def line(e: Any, width: int, t0: float = 0.0) -> str | None:
    p, k, who = e.payload, e.kind, e.agent or "-"
    t = f"{e.ts - t0:8.1f}s" if isinstance(e.ts, (int, float)) else str(e.ts)
    if k == "agent.message":
        body = f"says: {_short(p.get('text', ''), width)}"
    elif k == "agent.delegate":
        body = f"-> {p.get('to')}: {_short(p.get('task', ''), width)}"
    elif k == "agent.done":
        body = f"done: {_short(p.get('summary', ''), width)}"
    elif k == "tool.call":
        body = f"CALL {p.get('tool')} {_short(p.get('args', {}), width)}"
    elif k == "tool.result":
        body = (f"RESULT {p.get('tool')} [{p.get('status')}] "
                f"{_short(p.get('summary', ''), width)}")
    elif k == "tool.denied":
        body = (f"DENIED {p.get('tool')} rule={p.get('rule')}: "
                f"{_short(p.get('reason', ''), width)}")
    elif k == "agent.turn_end":
        body = (f"turn end: error={p.get('is_error')} turns={p.get('num_turns')} "
                f"cost=${float(p.get('cost_usd') or 0):.3f} {p.get('reason') or ''}")
    elif k in ("job.submitted", "job.status", "step.finished", "step.corrected",
               "fault.injected", "safety.violation", "plan.updated", "user.message",
               "agent.interrupted"):
        body = f"{k} {_short(p, width)}"
    elif k == "step.verified" and p.get("verdict") not in ("ok", "OK"):
        body = f"{k} {_short(p, width)}"
    else:
        return None
    run = f" {e.run_id}" if e.run_id else ""
    return f"{t} {who:12s}{run} {body}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db")
    ap.add_argument("--width", type=int, default=400)
    a = ap.parse_args(argv)
    store = RunStore(Path(a.db))
    counts: Counter[str] = Counter()
    after, t0 = 0, None
    while True:
        evs = store.events_after(after, limit=5000)
        if not evs:
            break
        for e in evs:
            if t0 is None and isinstance(e.ts, (int, float)):
                t0 = e.ts
            counts[e.kind] += 1
            if e.kind in SKIP:
                continue
            text = line(e, a.width, t0 or 0.0)
            if text:
                sys.stdout.write(text + "\n")
        after = evs[-1].id
    sys.stdout.write("\n# event counts: " + ", ".join(f"{k}={v}" for k, v in
                                                       sorted(counts.items())) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
