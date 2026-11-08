"""The lab's experience book (spec 10, self-evolution).

Like a researcher's lab notebook: before an experiment the advisor reads what
the lab knows; after it, a review turns what happened into experience, and
later real runs vote for or against each piece of it.

    trial ──enough support from several batches──▶ confirmed
      └──────────more against than for────────────▶ retired (kept, with the reason)

Hard rules of this module (code, not prompt):
  - Evidence and votes require a physical backend receipt bound at submission
    and agreement with the real store/run origin. A real label alone is not
    physical provenance. Testbench (sim)
    runs never create, vote on or confirm experience. Tests can open a
    sandbox store (`RunStore(experience_sandbox=True)`): there sim runs count,
    and every row is marked sandbox and never shown as lab experience.
  - Status changes follow the vote rule in config/experience.yaml; nobody sets
    "confirmed" by hand. Experience a person teaches (source human) starts
    confirmed; seeds from v1 and the paper (source prior) start on trial.
  - Experience is advice to the agents. It cannot change limits, rules,
    verification tolerances or tool permissions; those live in code.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from qumus2.core.materials import normalize_material_code
from qumus2.records.outcomes import cfg, exfoliation_outcome, same_recipe
from qumus2.runtime.store import RunStore

TOPICS = ("exfoliation", "search", "flake_choice", "stacking", "equipment", "efficiency")
FINISHED = {"succeeded", "failed", "parked", "waiting_human", "cancelled"}
#: words that try to talk an agent out of a check; experience never says that
_UNSAFE = re.compile(r"\b(skip|bypass|ignore|disable|override)\b.{0,40}\b(check|verif\w*|"
                     r"safety|limit|rule|interlock|camera)", re.I)
NO_REAL_EXPERIENCE = ("No real experience yet: experience comes only from experiments on the "
                      "real lab bench, and none have run. Entries below are prior knowledge "
                      "(v1, the paper) or what a person stated; v2 experiments have not "
                      "verified them.")


class ExperienceError(ValueError):
    """A request the book refuses; the message says why (shown to the agent)."""


class LaboratoryOrigin(BaseModel):
    """Backend-authored, submission-bound provenance in the existing event log."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["laboratory_origin_v1"]
    kind: Literal["simulated", "physical", "mixed", "unknown"]
    backend_id: str = Field(min_length=1, max_length=200)
    submission_event_id: int = Field(gt=0, strict=True)


def learning_origin(store: RunStore, run: dict[str, Any]) -> dict[str, Any]:
    """Read origin bound at submission, never mutable run metadata or UI mode.

    Only trusted backend composition may produce ``laboratory.origin``. Its
    receipt must immediately follow the original submission in that run's
    history, so a later import/review cannot manufacture missing provenance.
    No physical adapter currently produces these receipts in this application.
    """
    rows = store._x("SELECT id,kind,payload_json FROM event WHERE run_id=? ORDER BY id LIMIT 2",
                    (run["id"],)).fetchall()
    result: dict[str, Any] = {"kind": "unknown", "eligible": False, "backend_id": None}
    persisted_bench = store.get_meta("bench")
    if store.bench != persisted_bench or persisted_bench != run.get("bench"):
        return {**result, "kind": "mixed", "reason": "Recording origin disagrees with its laboratory store."}
    if store.bench == "sim":
        return {**result, "kind": "simulated", "reason": "Virtual laboratory runs cannot teach experimental experience."}
    if (len(rows) != 2 or rows[0]["kind"] != "job.submitted"
            or rows[1]["kind"] != "laboratory.origin"):
        return {**result, "reason": "Physical backend provenance was not recorded at submission; reviewed import/admission is required."}
    try:
        receipt = LaboratoryOrigin.model_validate_json(rows[1]["payload_json"]).model_dump()
        backend = receipt.get("backend_id")
        if (receipt.get("version") != "laboratory_origin_v1"
                or receipt.get("submission_event_id") != rows[0]["id"]
                or not isinstance(backend, str) or not backend.strip()):
            raise ValueError("invalid backend receipt")
        later = store._x("SELECT payload_json FROM event WHERE run_id=? AND kind='laboratory.origin' AND id>?",
                         (run["id"], rows[1]["id"])).fetchall()
        if any(json.loads(row["payload_json"]) != receipt for row in later):
            return {**result, "kind": "mixed", "reason": "The recording contains conflicting backend provenance."}
    except (ValueError, TypeError, AttributeError):
        return {**result, "reason": "The recorded backend provenance is invalid."}
    kind = receipt.get("kind")
    if kind != "physical":
        return {**result, "kind": kind if kind in ("simulated", "mixed", "unknown") else "unknown",
                "backend_id": backend, "reason": "The recorded backend is not a verified physical laboratory."}
    return {"kind": "physical", "eligible": True, "backend_id": backend,
            "reason": "Physical backend provenance was bound when this job was submitted."}


class ExperienceBook:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.sandbox = bool(store.experience_sandbox)
        self._seed()

    # ----- reading ---------------------------------------------------------

    def entries(self, *, material: str | None = None, topic: str | None = None,
                include_retired: bool = False) -> list[dict[str, Any]]:
        """Lab experience (sandbox rows too, when this store is a sandbox)."""
        rows = self.store.list_experiences(sandbox=False)
        if self.sandbox:
            rows += self.store.list_experiences(sandbox=True)
        mat = normalize_material_code(material)
        out = []
        for e in rows:
            if not include_retired and e["status"] == "retired":
                continue
            if topic and e["topic"] != topic:
                continue
            emat = normalize_material_code(e["scope"].get("material"))
            if mat and emat and emat != mat:
                continue
            out.append(self._view(e))
        return out

    def real_runs(self) -> int:
        return sum(learning_origin(self.store, r)["eligible"] for r in self.store.list_runs())

    def summary(self) -> dict[str, Any]:
        """What the UI shows. Sandbox rows are never part of it."""
        rows = [self._view(e) for e in self.store.list_experiences(sandbox=False)]
        counts = {s: sum(e["status"] == s for e in rows) for s in ("trial", "confirmed",
                                                                  "retired")}
        learned = [e for e in rows if e["source"] == "review"]
        real = self.real_runs()
        note = NO_REAL_EXPERIENCE if not learned else (
            f"{len(learned)} experience(s) learned from {real} real run(s).")
        origins = [learning_origin(self.store, r) for r in self.store.list_runs()]
        unverified = sum(o["kind"] in ("unknown", "mixed") for o in origins)
        if unverified and not learned:
            note = "No verified physical experience yet. Unknown or mixed recording origins cannot teach experience."
        return {"bench": self.store.bench, "real_runs": real, "note": note, "counts": counts,
                "unverified_origin_runs": unverified,
                "learning": {"enabled": self.store.bench == "real" and real > 0,
                    "status": "verified_physical_records_only" if self.store.bench == "real" else "disabled_virtual",
                    "detail": "Only submission-bound physical records can create or vote on experience. Existing advice remains readable." if self.store.bench == "real" else "Learning is disabled in the virtual laboratory. Existing advice remains readable."},
                "learned": len(learned), "baselines": self.baselines(), "experiences": rows,
                "rule": cfg().get("votes", {})}

    def baseline(self, material: str, *, include_sandbox: bool = True) -> dict[str, Any] | None:
        """Best known exfoliation recipe for a material: a confirmed one (code
        holds the secure runs to it), else the best on trial (a suggestion)."""
        recipes = [e for e in self.entries(material=material, topic="exfoliation")
                   if e["recipe"] and normalize_material_code(e["scope"].get("material"))
                   and (include_sandbox or not e["sandbox"])]
        if not recipes:
            return None
        # confirmed first; among those what a person said, then the best supported
        rank = {"confirmed": 0, "trial": 1}
        recipes.sort(key=lambda e: (rank[e["status"]], e["source"] != "human",
                                    -len(e["support"]), len(e["against"]), -e["created_at"]))
        best = recipes[0]
        return {"experience_id": best["id"], "recipe": best["recipe"],
                "status": best["status"], "source": best["source"],
                "enforced": best["status"] == "confirmed"}

    def baselines(self) -> dict[str, Any]:
        mats = {normalize_material_code(e["scope"].get("material"))
                for e in self.store.list_experiences(sandbox=False)} - {None}
        return {m: self.baseline(m, include_sandbox=False) for m in sorted(m for m in mats if m)}

    # ----- writing -----------------------------------------------------------

    def propose(self, *, topic: str, claim: str, scope: dict[str, Any],
                evidence_run_ids: list[str], observation: str, cause_guess: str = "",
                suggestion: str = "", recipe: dict[str, Any] | None = None,
                expect: dict[str, Any] | None = None, author: str = "advisor") -> str:
        """A new experience from a review of real runs. Starts on trial; the
        runs it cites are its first supporting votes."""
        self._check_text(topic, claim)
        if not evidence_run_ids:
            raise ExperienceError("cite at least one run as evidence")
        runs = [self._evidence_run(r) for r in evidence_run_ids]
        scope = self._scope(scope)
        dup = self._duplicate(topic, scope, claim, recipe)
        if dup:
            raise ExperienceError(f"this is already experience {dup['id']} ({dup['status']}): "
                                  "vote on it with vote_experience instead")
        eid = self.store.insert_experience(
            topic=topic, claim=claim.strip(), source="review", status="trial", scope=scope,
            recipe=recipe or None, expect=expect or None, author=author, sandbox=self.sandbox,
            detail={"observation": observation, "cause_guess": cause_guess,
                    "suggestion": suggestion, "evidence_run_ids": evidence_run_ids,
                    "note": "cause_guess is a hypothesis, not a finding"})
        self.store.emit("experience.proposed", {"experience_id": eid, "topic": topic,
                                                "claim": claim, "sandbox": self.sandbox},
                        agent=author)
        for run in runs:
            self.store.add_vote(eid, run["id"], "support", author=author,
                                batch=self._batch(run), note="cited when proposed")
        self._update_status(eid)
        return eid

    def teach(self, *, topic: str, claim: str, scope: dict[str, Any], instruction_id: str,
              recipe: dict[str, Any] | None = None, author: str = "orchestrator") -> str:
        """What a person told the system. It counts at once (confirmed)."""
        self._check_text(topic, claim)
        ins = self._instruction(instruction_id)
        eid = self.store.insert_experience(
            topic=topic, claim=claim.strip(), source="human", status="confirmed",
            scope=self._scope(scope), recipe=recipe or None, author=author,
            sandbox=self.sandbox, detail={"instruction_id": instruction_id,
                                          "user_words": ins["text"]})
        self.store.emit("experience.taught", {"experience_id": eid, "claim": claim,
                                              "instruction_id": instruction_id}, agent=author)
        return eid

    def vote(self, experience_id: str, run_id: str, verdict: str, *, note: str,
             author: str = "advisor") -> dict[str, Any]:
        exp = self._get(experience_id)
        self._writable(exp)
        if exp["status"] == "retired":
            raise ExperienceError(f"{experience_id} is retired")
        run = self._evidence_run(run_id)
        if not self.store.add_vote(experience_id, run_id, verdict, author=author,
                                   batch=self._batch(run), note=note):
            raise ExperienceError(f"run {run_id} already voted on {experience_id}")
        self.store.emit("experience.voted", {"experience_id": experience_id, "run_id": run_id,
                                             "verdict": verdict, "note": note}, agent=author)
        return self._update_status(experience_id)

    def retire(self, experience_id: str, *, reason: str, instruction_id: str,
               author: str = "orchestrator") -> None:
        """A person says an experience is wrong."""
        self._get(experience_id)
        ins = self._instruction(instruction_id)
        self._set_status(experience_id, "retired",
                         f"{reason} (person: {ins['text'][:200]!r})", author)

    def auto_vote(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        """Recipe experience with an expected result is voted by code: a finished,
        fully searched real exfoliation run with exactly that recipe supports it
        if it reached the expected good flakes per chip, else it is against."""
        if run["flow"] != "exfoliate_chips" or not self._counts(run):
            return []
        out = exfoliation_outcome(self.store, run)
        if not out["complete"] or out["good_per_chip"] is None:
            return []
        done = []
        for e in self.entries(material=out["material"], topic="exfoliation"):
            if bool(e["sandbox"]) != self.sandbox:
                continue
            want = (e["expect"] or {}).get("good_per_chip")
            if want is None or not e["recipe"] or not same_recipe(e["recipe"], run):
                continue
            verdict = "support" if out["good_per_chip"] >= float(want) else "against"
            note = f"code: {out['good_per_chip']} good flake(s) per chip, expected {want}"
            if self.store.add_vote(e["id"], run["id"], verdict, author="code",
                                   batch=self._batch(run), note=note):
                self.store.emit("experience.voted", {"experience_id": e["id"],
                                                     "run_id": run["id"], "verdict": verdict,
                                                     "note": note}, agent="code")
                done.append({"experience_id": e["id"], "verdict": verdict,
                             "status": self._update_status(e["id"])["status"]})
        return done

    # ----- internals ---------------------------------------------------------

    def _seed(self) -> None:
        for s in cfg().get("seed", []):
            if self.store.get_experience(s["id"]) is not None:
                continue
            with contextlib.suppress(sqlite3.IntegrityError):  # another process seeded it
                self.store.insert_experience(
                    experience_id=s["id"], topic=s["topic"], claim=s["claim"],
                    source=s["source"],
                    status="confirmed" if s["source"] == "human" else "trial",
                    scope=s.get("scope") or {}, recipe=s.get("recipe"), author="seed",
                    detail={"reference": s.get("reference", ""),
                            "note": "starting knowledge; not verified by v2 experiments"})

    def _counts(self, run: dict[str, Any]) -> bool:
        return self.sandbox or learning_origin(self.store, run)["eligible"]

    def _evidence_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ExperienceError(f"run {run_id} does not exist")
        if not self._counts(run):
            origin = learning_origin(self.store, run)
            raise ExperienceError(f"run {run_id} cannot teach experience: {origin['reason']} "
                                  "The testbench (sim), unknown and mixed origins are never physical evidence.")
        if run["status"] not in FINISHED:
            raise ExperienceError(f"run {run_id} is still {run['status']}")
        return run

    def _instruction(self, instruction_id: str) -> dict[str, Any]:
        ins = self.store.get_instruction(instruction_id)
        if ins is None or ins["source"] != "ui":
            raise ExperienceError(f"{instruction_id} is not a message a person sent; only what "
                                  "a person said can be taught or retire experience")
        return ins

    @staticmethod
    def _batch(run: dict[str, Any]) -> str:
        return run.get("project_id") or run["id"]

    @staticmethod
    def _scope(scope: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in (scope or {}).items() if v not in (None, "")}
        if "material" in out:
            out["material"] = normalize_material_code(out["material"]) or out["material"]
        return out

    @staticmethod
    def _check_text(topic: str, claim: str) -> None:
        if topic not in TOPICS:
            raise ExperienceError(f"topic must be one of {list(TOPICS)}")
        if not claim.strip() or len(claim) > 400:
            raise ExperienceError("claim: one or two sentences, at most 400 characters")
        if _UNSAFE.search(claim):
            raise ExperienceError("experience gives judgement hints only; it never tells an "
                                  "agent to skip, bypass or ignore a check, limit or rule")

    def _duplicate(self, topic: str, scope: dict[str, Any], claim: str,
                   recipe: dict[str, Any] | None) -> dict[str, Any] | None:
        for e in self.entries(topic=topic):
            if e["scope"] != scope:
                continue
            if (recipe and e["recipe"] == recipe) or e["claim"].strip().lower() == \
                    claim.strip().lower():
                return e
        return None

    def _get(self, experience_id: str) -> dict[str, Any]:
        e = self.store.get_experience(experience_id)
        if e is None or (e["sandbox"] and not self.sandbox):
            raise ExperienceError(f"no experience {experience_id}")
        return e

    def _update_status(self, experience_id: str) -> dict[str, Any]:
        e = self._get(experience_id)
        rule = cfg().get("votes", {})
        # Preserve historical votes, but unverified legacy/imported origins may
        # not help a new lesson cross a confirmation or retirement threshold.
        votes = [v for v in e["votes"] if (run := self.store.get_run(v["run_id"]))
                 and run["status"] in FINISHED and self._counts(run)]
        sup = sum(v["verdict"] == "support" for v in votes)
        ag = sum(v["verdict"] == "against" for v in votes)
        batches = {v["batch"] for v in votes if v["verdict"] == "support"}
        new = e["status"]
        if (e["status"] != "retired" and ag >= int(rule.get("retire_min_against", 2))
                and sup <= ag):
            new = "retired"
        elif (e["status"] == "trial" and sup >= int(rule.get("confirm_min_support", 3))
              and len(batches) >= int(rule.get("confirm_min_batches", 2))
              and ag <= int(rule.get("confirm_max_against", 1))):
            new = "confirmed"
        if new != e["status"]:
            reason = (f"{ag} run(s) against, {sup} for" if new == "retired"
                      else f"{sup} supporting run(s) from {len(batches)} batch(es)")
            self._set_status(experience_id, new, reason, "code")
        return self._view(self._get(experience_id))

    def _set_status(self, experience_id: str, status: str, reason: str, author: str) -> None:
        entry = self._get(experience_id)
        self._writable(entry)
        old = entry["status"]
        self.store.set_experience_status(experience_id, status, reason)
        self.store.emit("experience.status", {"experience_id": experience_id, "from": old,
                                              "to": status, "reason": reason}, agent=author)

    def _writable(self, entry: dict[str, Any]) -> None:
        if bool(entry["sandbox"]) != self.sandbox:
            raise ExperienceError("Sandbox votes and status changes cannot alter laboratory experience.")

    def _view(self, e: dict[str, Any]) -> dict[str, Any]:
        runs = [self.store.get_run(r) for r in e["support"] + e["against"]]
        real = sum(1 for r in runs if r and learning_origin(self.store, r)["eligible"])
        review_label = ("sandbox test experience" if e["sandbox"] else
                        "learned from real runs" if runs and real == len(runs) else
                        "recorded experience; physical origin is unverified")
        return {k: v for k, v in e.items() if k != "votes"} | {
            "verified_by_real_runs": real,
            "label": {"prior": "prior knowledge, not verified by v2 experiments",
                      "human": "stated by a person",
                      "review": review_label}[e["source"]]}
