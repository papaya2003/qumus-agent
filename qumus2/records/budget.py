"""Secure first, explore with the rest (Jialei, 2026-10-21).

Before an exfoliation the code works out how many bare chips the task still
needs with the best known recipe, and only the chips beyond that may be used
to try something new:

    flakes the plan still needs  = flakes of this material the device plan asks for
                                   - good candidates already in the ledger
    chips the task still needs   = ceil(margin x flakes still needed / good flakes per chip)
                                   - chips already exfoliated for it and not yet searched
    spare chips (may explore)    = free bare chips - chips the task still needs - keep_bare_chips
    free bare chips              = bare chips not already taken by a queued or running
                                   exfoliation

Good flakes per chip comes from finished, searched runs of this material on
this bench, or a configured default while there are too few.
"""

from __future__ import annotations

from typing import Any

from qumus2.core.materials import normalize_material_code
from qumus2.records.outcomes import (
    bare_chips,
    cfg,
    chip_searched,
    chips_for,
    is_good_flake,
    usable_per_chip_estimate,
)
from qumus2.runtime.store import RunStore

SELECTABLE = {"candidate", "catalogued"}
ACTIVE = {"queued", "running", "parked", "waiting_human", "succeeded"}


def plan_target(state: dict[str, Any], material: str) -> dict[str, Any] | None:
    """What a good flake of this material means for the device plan: any of its
    roles' thickness labels, at least the smallest role minimum area."""
    roles = [r for r in (state.get("device_plan") or {}).get("flake_roles", [])
             if normalize_material_code(r.get("material")) == normalize_material_code(material)]
    if not roles:
        return None
    labels = sorted({lab for r in roles for lab in (r.get("thickness_labels") or [])})
    return {"thickness_labels": labels,
            "min_area_um2": min(float(r.get("min_area_um2") or 0) for r in roles)}


def flakes_still_needed(store: RunStore, state: dict[str, Any], material: str) -> dict[str, int]:
    mat = normalize_material_code(material)
    roles = [r for r in (state.get("device_plan") or {}).get("flake_roles", [])
             if normalize_material_code(r.get("material")) == mat]
    excluded = set(state.get("excluded_flakes", []))
    pool = [f for f in store.list_samples(kind="flake")
            if f["status"] in SELECTABLE and f["id"] not in excluded
            and normalize_material_code(f["props"].get("material")) == mat]
    used: set[str] = set()
    need = have = 0
    for r in roles:
        count = int(r.get("count", 1))
        need += count
        target = {"thickness_labels": r.get("thickness_labels") or [],
                  "min_area_um2": r.get("min_area_um2") or 0}
        fits = [f["id"] for f in pool if f["id"] not in used and is_good_flake(f, target)]
        take = fits[:count]
        used.update(take)
        have += len(take)
    return {"needed": need, "in_ledger": have, "missing": max(0, need - have)}


def pending_task_chips(store: RunStore, project_id: str | None, material: str) -> int:
    """Chips already exfoliated (or queued) for the task and not searched yet."""
    mat = normalize_material_code(material)
    n = 0
    for r in store.list_runs():
        if (r["flow"] != "exfoliate_chips" or r["project_id"] != project_id
                or r["status"] not in ACTIVE
                or normalize_material_code((r["params"] or {}).get("material")) != mat
                or (r["meta"] or {}).get("purpose") == "explore"):
            continue
        n += sum(not chip_searched(store, c) for c in (r["params"] or {}).get("chip_ids") or [])
    return n


def reserved_chips(store: RunStore) -> set[str]:
    """Bare chips a queued or running exfoliation will use."""
    return {c for r in store.list_runs()
            if r["flow"] == "exfoliate_chips" and r["status"] in ("queued", "running")
            for c in (r["params"] or {}).get("chip_ids") or []}


def exploration_budget(store: RunStore, project_id: str | None,
                       material: str) -> dict[str, Any]:
    ex = cfg().get("explore", {})
    proj = store.get_project(project_id) if project_id else None
    state = (proj or {}).get("state", {})
    bare = [c for c in bare_chips(store) if c not in reserved_chips(store)]
    flakes = flakes_still_needed(store, state, material)
    est = usable_per_chip_estimate(store, material)
    pending = pending_task_chips(store, project_id, material)
    task_chips = max(0, chips_for(flakes["missing"], est["usable_per_chip"],
                                  float(ex.get("margin", 1.5))) - pending)
    spare = max(0, len(bare) - task_chips - int(ex.get("keep_bare_chips", 0)))
    evaluated = bool(state.get("device_plan")) or state.get("task_kind") == "experiment"
    return {"material": normalize_material_code(material), "bare_chips": len(bare),
            "flakes": flakes, "good_flakes_per_chip": est, "pending_task_chips": pending,
            "task_chips_needed": task_chips if evaluated else None,
            "spare_chips": spare if evaluated else None,
            "label": "Exploration chip allowance", "evaluated": evaluated,
            "margin": float(ex.get("margin", 1.5)),
            "keep_bare_chips": int(ex.get("keep_bare_chips", 0)),
            "note": ("Estimate only, not a reservation, guaranteed yield or task attempt limit."
                     if evaluated else "Task demand unevaluated: obtain Device Guidance, or "
                     "explicitly identify a standalone experiment in the Task Plan."),
            "max_explore_chips_per_run": int(ex.get("max_chips_per_run", 2)),
            "has_device_plan": bool(state.get("device_plan"))}
