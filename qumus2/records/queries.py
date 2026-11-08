"""Run-record queries for the advisor (spec 10, self-evolution).

Written in code, not by the AI (v1 query_experiments let the LLM write SQL).
Everything reads the run store: which parameters were used for a flow and
material, how often they worked, and (exfoliation) how many good flakes the
chips carried once searched. Only runs of this store's bench are counted:
testbench statistics never mix with real-lab ones.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from qumus2.core.materials import normalize_material_code
from qumus2.records.outcomes import exfoliation_outcome, recipe_keys
from qumus2.runtime.store import RunStore

FINISHED = {"succeeded", "failed", "parked", "waiting_human", "cancelled"}


def _material(run: dict[str, Any]) -> str | None:
    return normalize_material_code((run.get("params") or {}).get("material"))


def runs_for(store: RunStore, flow: str | None = None,
             material: str | None = None) -> list[dict[str, Any]]:
    mat = normalize_material_code(material)
    return [r for r in store.list_runs()
            if r["bench"] == store.bench
            and (flow is None or r["flow"] == flow) and (mat is None or _material(r) == mat)]


def recipe_stats(store: RunStore, flow: str, material: str | None = None) -> list[dict[str, Any]]:
    """Success rate per parameter set, most-used first. For exfoliation also the
    good flakes per searched chip (the real result, not just "the moves ran")."""
    keys = recipe_keys(flow)
    groups: dict[tuple[Any, ...], dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "succeeded": 0, "corrections": 0, "run_ids": [],
                 "searched_chips": 0, "good_flakes": 0, "with_target": 0})
    for r in runs_for(store, flow, material):
        if r["status"] not in FINISHED:
            continue
        p = r.get("params") or {}
        g = groups[tuple(p.get(k) for k in keys)]
        g["runs"] += 1
        g["succeeded"] += r["status"] == "succeeded"
        g["corrections"] += sum(s["corrections"] for s in store.get_steps(r["id"]))
        g["run_ids"].append(r["id"])
        if flow == "exfoliate_chips":
            out = exfoliation_outcome(store, r)
            if out["complete"] and out["good_flakes"] is not None:
                g["with_target"] += 1
                g["searched_chips"] += out["searched_chips"]
                g["good_flakes"] += out["good_flakes"]
    out = []
    for key, g in groups.items():
        row = {"params": {k: v for k, v in zip(keys, key, strict=True) if v is not None},
               "runs": g["runs"], "succeeded": g["succeeded"],
               "success_rate": round(g["succeeded"] / g["runs"], 3),
               "corrections_per_run": round(g["corrections"] / g["runs"], 2),
               "run_ids": g["run_ids"][-5:]}
        if flow == "exfoliate_chips":
            row["searched_runs"] = g["with_target"]
            row["good_flakes_per_chip"] = (round(g["good_flakes"] / g["searched_chips"], 3)
                                           if g["searched_chips"] else None)
        out.append(row)
    return sorted(out, key=lambda x: (-x["runs"], -x["success_rate"]))


def flake_yield(store: RunStore, material: str | None = None) -> dict[str, Any]:
    """Flakes found per searched chip, by thickness label. A store holds one
    bench only (RunStore refuses to mix), so its ledger is that bench's."""
    mat = normalize_material_code(material)
    flakes = [f for f in store.list_samples(kind="flake")
              if mat is None or normalize_material_code(f["props"].get("material")) == mat]
    by_label: dict[str, int] = defaultdict(int)
    for f in flakes:
        by_label[str(f["props"].get("thickness_label"))] += 1
    chips = {f["props"].get("chip_id") or f["location"] for f in flakes}
    return {"flakes": len(flakes), "chips": len(chips), "by_thickness_label": dict(by_label)}
