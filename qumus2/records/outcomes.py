"""What an experiment actually produced (spec 10, self-evolution).

An exfoliation run that "succeeded" only means the arm and the tape did their
moves. Its real result is known later, when its chips are searched: how many
good flakes they carry. A good flake (Jialei, 2026-10-21) has the right
thickness, enough area and a clean surface (no excessive tape residue); the
thickness and area are the run's target, the residue is the picker's judgement.

Everything here is code over the run store; nothing is decided by an AI.
"""

from __future__ import annotations

import math
from typing import Any

from qumus2.config import load_yaml
from qumus2.core.materials import normalize_material_code, normalize_thickness_label
from qumus2.runtime.store import RunStore

TAPE_SAMPLE = "tape"  # the mother-tape ledger entry (flows/library/exfoliation.py)
BARE = ("bare", "available")


def cfg() -> dict[str, Any]:
    return load_yaml("experience.yaml")


def recipe_keys(flow: str) -> list[str]:
    return list(cfg().get("recipe_keys", {}).get(flow, []))


def recipe_of(run: dict[str, Any]) -> dict[str, Any]:
    """The recipe a run used: its recipe keys that the flow actually has."""
    p = run.get("params") or {}
    return {k: p[k] for k in recipe_keys(run["flow"]) if p.get(k) is not None}


def same_recipe(recipe: dict[str, Any], run: dict[str, Any]) -> bool:
    """Does the run use every value the (possibly partial) recipe names?"""
    used = recipe_of(run)
    return bool(recipe) and all(k in used and float(used[k]) == float(v)
                                for k, v in recipe.items())


def changed_keys(recipe: dict[str, Any], params: dict[str, Any]) -> list[str]:
    return sorted(k for k, v in recipe.items()
                  if params.get(k) is not None and float(params[k]) != float(v))


def residue_level(flake: dict[str, Any]) -> str | None:
    r = flake["props"].get("residue")
    return r.get("level") if isinstance(r, dict) else None


def is_good_flake(flake: dict[str, Any], target: dict[str, Any] | None) -> bool | None:
    """True/False against the target; None when the run had no target."""
    if not target:
        return None
    labels = set(target.get("thickness_labels") or [])
    mat = normalize_material_code(flake["props"].get("material"))
    label = normalize_thickness_label(mat or "", str(flake["props"].get("thickness_label")))
    if labels and label not in labels:
        return False
    if float(flake["props"].get("area_um2") or 0) < float(target.get("min_area_um2") or 0):
        return False
    return residue_level(flake) not in set(cfg().get("good_flake", {})
                                           .get("residue_rejected", ["heavy"]))


def flakes_on(store: RunStore, chip_id: str) -> list[dict[str, Any]]:
    """Flakes found on a chip, wherever they are now (stacked flakes keep chip_id)."""
    return [f for f in store.list_samples(kind="flake")
            if (f["props"].get("chip_id") or f["location"]) == chip_id]


def chip_searched(store: RunStore, chip_id: str) -> bool:
    chip = store.get_sample(chip_id) or {}
    return chip.get("status") == "searched" or bool(flakes_on(store, chip_id))


def exfoliation_outcome(store: RunStore, run: dict[str, Any]) -> dict[str, Any]:
    """Per chip of one exfoliation run: searched yet, flakes by thickness label,
    good flakes against the run's target, residue judged or not."""
    target = (run.get("meta") or {}).get("target")
    chips = []
    for cid in (run.get("params") or {}).get("chip_ids") or []:
        chip = store.get_sample(cid) or {}
        flakes = flakes_on(store, cid)
        by_label: dict[str, int] = {}
        for f in flakes:
            lab = str(f["props"].get("thickness_label"))
            by_label[lab] = by_label.get(lab, 0) + 1
        good = [f["id"] for f in flakes if is_good_flake(f, target)]
        chips.append({
            "chip_id": cid, "status": chip.get("status"), "searched": chip_searched(store, cid),
            "flakes": len(flakes), "by_thickness_label": by_label,
            "good_flakes": good if target else None,
            "residue_judged": sum(residue_level(f) is not None for f in flakes),
            "residue_heavy": [f["id"] for f in flakes if residue_level(f) == "heavy"]})
    used = [c for c in chips if c["status"] in ("exfoliated", "searched") or c["searched"]]
    searched = [c for c in used if c["searched"]]
    complete = run["status"] == "succeeded" and bool(used) and len(searched) == len(used)
    good = sum(len(c["good_flakes"] or []) for c in searched) if target else None
    return {"run_id": run["id"], "material": normalize_material_code(
                (run.get("params") or {}).get("material")),
            "recipe": recipe_of(run), "target": target, "run_status": run["status"],
            "chips": chips, "searched_chips": len(searched), "complete": complete,
            "good_flakes": good,
            "good_per_chip": (round(good / len(searched), 3)
                              if good is not None and searched else None)}


def bare_chips(store: RunStore) -> list[str]:
    """Bare chips in the ledger. The flows keep the ledger in step with what the
    cameras saw (every pick and place is verified), and the eyes look at the
    bench before the hands may act."""
    return [c["id"] for c in store.list_samples(kind="chip") if c["status"] in BARE
            and not c["props"].get("reactor_substrate")]


def conditions_now(store: RunStore, material: str | None) -> dict[str, Any]:
    """Conditions at submit that the recipe does not cover. Fields the lab cannot
    measure yet stay None (humidity, room temperature, crystal batch)."""
    tape = store.get_sample(TAPE_SAMPLE) or {"props": {}}
    same = normalize_material_code(tape["props"].get("material")) == normalize_material_code(
        material)
    carriers = [c for c in store.list_samples(kind="carrier")
                if normalize_material_code(c["props"].get("material"))
                == normalize_material_code(material)]
    return {"tape_material": tape["props"].get("material"),
            "tape_uses_before": int(tape["props"].get("uses") or 0) if same else None,
            "tape_roll_left_mm": tape["props"].get("roll_left_mm"),
            "crystal_batch": (carriers[0]["props"].get("batch") if carriers else None),
            "humidity_pct": None, "room_temp_c": None}


def usable_per_chip_estimate(store: RunStore, material: str) -> dict[str, Any]:
    """Good flakes per searched chip on this bench, or the configured default."""
    ex = cfg().get("explore", {})
    mat = normalize_material_code(material)
    outs = [exfoliation_outcome(store, r) for r in store.list_runs()
            if r["flow"] == "exfoliate_chips" and r["bench"] == store.bench
            and normalize_material_code((r["params"] or {}).get("material")) == mat]
    outs = [o for o in outs if o["complete"] and o["good_flakes"] is not None]
    if len(outs) >= int(ex.get("min_runs_for_estimate", 2)):
        chips = sum(o["searched_chips"] for o in outs)
        value = sum(o["good_flakes"] for o in outs) / chips if chips else 0.0
        return {"usable_per_chip": round(max(value, 0.05), 3),
                "source": f"{len(outs)} finished {store.bench} run(s)"}
    return {"usable_per_chip": float(ex.get("default_usable_per_chip", 0.5)),
            "source": "default assumption (fewer than "
                      f"{ex.get('min_runs_for_estimate', 2)} searched runs)"}


def chips_for(flakes_needed: int, per_chip: float, margin: float) -> int:
    if flakes_needed <= 0:
        return 0
    return math.ceil(margin * flakes_needed / max(per_chip, 0.05))
