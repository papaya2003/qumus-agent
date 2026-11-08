"""After-experiment review (spec 10): the facts of each finished real run, put
together by code, for the advisor to turn into experience.

Code does the arithmetic (expected vs actual, good flakes, corrections, what
went wrong); the advisor writes what it means (propose_experience) and which
existing experience the run supports or contradicts (vote_experience). Recipe
experience with an expected result is voted by code (ExperienceBook.auto_vote).

Testbench (sim) runs are never reviewed: they are not evidence (sandbox stores
in tests excepted, see records/experience.py).
"""

from __future__ import annotations

from typing import Any

from qumus2.core.materials import normalize_material_code
from qumus2.records.experience import FINISHED, ExperienceBook, learning_origin
from qumus2.records.outcomes import exfoliation_outcome, recipe_of
from qumus2.runtime.store import RunStore

REVIEWED_FLOWS = ("exfoliate_chips", "search_chip", "stack_device",
                  "place_chip_on_transfer_stage")


def counts_as_evidence(store: RunStore, run: dict[str, Any]) -> bool:
    return store.experience_sandbox or learning_origin(store, run)["eligible"]


def unreviewed_runs(store: RunStore, material: str | None = None) -> list[dict[str, Any]]:
    """Finished runs that count as evidence and have no review yet. An
    exfoliation waits until all its chips are searched (its result is known)."""
    mat = normalize_material_code(material)
    out = []
    for r in store.list_runs():
        if (r["flow"] not in REVIEWED_FLOWS or r["status"] not in FINISHED
                or (r["meta"] or {}).get("reviewed") or not counts_as_evidence(store, r)):
            continue
        if mat and normalize_material_code((r["params"] or {}).get("material")) != mat:
            continue
        if r["flow"] == "exfoliate_chips" and r["status"] == "succeeded" \
                and not exfoliation_outcome(store, r)["complete"]:
            continue
        out.append(r)
    return out


def packet(store: RunStore, run: dict[str, Any]) -> dict[str, Any]:
    """Everything a reviewer needs about one run, numbers computed by code."""
    meta = run["meta"] or {}
    steps = store.get_steps(run["id"])
    out: dict[str, Any] = {
        "run_id": run["id"], "flow": run["flow"], "status": run["status"],
        "project_id": run["project_id"], "recipe": recipe_of(run),
        "laboratory_origin": learning_origin(store, run),
        "purpose": meta.get("purpose"), "hypothesis": meta.get("hypothesis"),
        "changed": meta.get("changed"), "conditions": meta.get("conditions"),
        "corrections": sum(s["corrections"] for s in steps),
        "problem_steps": [{"step": s["name"], "status": s["status"],
                           "corrections": s["corrections"], "note": s["note"]}
                          for s in steps if s["status"] != "done" or s["corrections"]],
        "error": run["error"]}
    if run["flow"] == "exfoliate_chips":
        res = exfoliation_outcome(store, run)
        out["outcome"] = {k: res[k] for k in ("material", "target", "searched_chips",
                                              "good_flakes", "good_per_chip", "chips")}
        want = meta.get("expected_good_per_chip")
        if want is not None and res["good_per_chip"] is not None:
            out["expected_vs_actual"] = {"expected_good_per_chip": want,
                                         "actual_good_per_chip": res["good_per_chip"],
                                         "difference": round(res["good_per_chip"] - want, 3)}
    return out


def review(store: RunStore, material: str | None = None) -> dict[str, Any]:
    """Build the review of every unreviewed run, apply code votes, mark them
    reviewed. The advisor then proposes or votes on experience."""
    if store.bench != "real" and not store.experience_sandbox:
        return {"bench": store.bench, "runs": [], "auto_votes": [],
                "note": "Testbench runs are simulated: they are never reviewed into "
                        "experience. Experience comes only from real-lab experiments."}
    book = ExperienceBook(store)
    runs = unreviewed_runs(store, material)
    packets, votes = [], []
    for r in runs:
        packets.append(packet(store, r))
        votes += [{"run_id": r["id"], **v} for v in book.auto_vote(r)]
        store.update_run_meta(r["id"], reviewed=True, reviewed_at=store.clock.now())
    return {"bench": store.bench, "sandbox": store.experience_sandbox, "runs": packets,
            "auto_votes": votes,
            "experience": book.entries(material=material),
            "note": "For each run: does it support or contradict an experience above "
                    "(vote_experience)? Did it show something new worth keeping "
                    "(propose_experience, citing the runs)? Say what is fact and what "
                    "is a guess."}
