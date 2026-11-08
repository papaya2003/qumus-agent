"""Experience tools (spec 10, self-evolution): read what the lab knows, review
finished experiments, and keep the experience book.

The book's rules are code (records/experience.py): evidence only from real-lab
runs, status only by the vote rule, a person's word counts at once. Agents
write the words; code decides what counts.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.core.materials import normalize_material_code
from qumus2.records import review as reviewing
from qumus2.records.budget import exploration_budget
from qumus2.records.experience import TOPICS, ExperienceBook, ExperienceError
from qumus2.records.queries import recipe_stats

Topic = Literal["exfoliation", "search", "flake_choice", "stacking", "equipment", "efficiency"]
assert set(Topic.__args__) == set(TOPICS)  # type: ignore[attr-defined]


def mark_checked(ctx: ToolContext, material: str | None) -> None:
    """Remember that the advisor looked up a material for this project (rule
    experience_checked). Other agents may read experience, but it is the
    advisor's look-up that counts."""
    mat = normalize_material_code(material)
    if not (mat and ctx.project_id) or ctx.agent != "advisor":
        return
    st = (ctx.store.get_project(ctx.project_id) or {}).get("state", {})
    seen = list(st.get("experience_checked", []))
    if mat not in seen:
        ctx.store.update_project_state(ctx.project_id, experience_checked=[*seen, mat])


def _bench_note(ctx: ToolContext, book: ExperienceBook) -> str:
    if ctx.store.experience_sandbox:
        return "SANDBOX (tests only): simulated runs count here; nothing is lab experience."
    if ctx.store.bench == "sim":
        return ("This is the testbench (simulated lab). Its runs are not evidence: they never "
                "create or confirm experience, and its statistics are not real-lab rates.")
    return f"Real lab: {book.real_runs()} real run(s) recorded."


class Scope(BaseModel):
    material: str | None = None
    crystal: str | None = Field(None, description="Carrier/crystal id, when it matters")
    flow: str | None = None


class GetExperienceArgs(BaseModel):
    material: str | None = Field(None, max_length=64, description="e.g. Gr or hBN; None = all")
    topic: Topic | None = None
    materials: list[Annotated[str, StringConstraints(max_length=64)]] = Field(default_factory=list, max_length=3,
                                 description="Up to three additional task materials")


EXPERIENCE_CHAR_BUDGET = 16000


def bounded_value(value: Any, depth: int = 0) -> Any:
    """Bound advisory fields without modifying the underlying audit records."""
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:397] + "..."
    if isinstance(value, dict):
        return {str(k)[:80]: bounded_value(v, depth + 1) for k, v in list(value.items())[:12]} if depth < 3 else "[see audit record]"
    if isinstance(value, list):
        return [bounded_value(v, depth + 1) for v in value[:3]] if depth < 3 else "[see audit record]"
    return value


def advisory_entry(e: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "topic", "scope", "claim", "recipe", "expect", "status", "source",
            "label", "verified_by_real_runs", "detail", "support", "against")
    return {**{k: bounded_value(e[k]) for k in keys if k in e},
            "support_count": len(e.get("support", [])),
            "against_count": len(e.get("against", []))}


@qumus_tool("get_experience",
            "What the lab knows before an experiment: experience entries (confirmed, on "
            "trial; sources and conflicting evidence labelled), baseline recipes, bounded "
            "statistical summaries and Exploration chip allowance. At most 16000 characters; "
            "omissions are counted. Advisor's normal entry point before an experiment.",
            agents={"advisor", "orchestrator", "picker", "designer"}, kind="query")
async def get_experience(args: GetExperienceArgs, ctx: ToolContext) -> ToolResult:
    book = ExperienceBook(ctx.store)
    mats = list(dict.fromkeys(normalize_material_code(m)
                             for m in [args.material, *args.materials] if m))
    data: dict[str, Any] = {"note": _bench_note(ctx, book), "materials": {},
                            "response_char_budget": EXPERIENCE_CHAR_BUDGET,
                            "scope": "distilled advice; full records retained for audit"}
    for m in mats or [None]:
        entries = book.entries(material=m, topic=args.topic)
        # Keep contradicting findings visible ahead of high-support repetition.
        entries.sort(key=lambda e: (not bool(e.get("against")), e["status"] != "confirmed",
                                    -e.get("updated_at", 0)))
        entry: dict[str, Any] = {"experience": [advisory_entry(e) for e in entries[:6]],
                                "matching_entries": len(entries),
                                "omitted_entries": max(0, len(entries) - 6),
                                "entries_with_counterevidence": sum(bool(e.get("against")) for e in entries)}
        if m:
            entry["baseline_recipe"] = bounded_value(book.baseline(m))
            stats = recipe_stats(ctx.store, "exfoliate_chips", m)
            entry["recipe_stats"] = [bounded_value(s) for s in stats[:3]]
            entry["omitted_recipe_groups"] = max(0, len(stats) - 3)
            entry["exploration"] = bounded_value(exploration_budget(ctx.store, ctx.project_id, m))
        data["materials"][normalize_material_code(m) or "all"] = entry
    # Reserve room for the ToolResult envelope; never serialize an unbounded field.
    while len(json.dumps(data, ensure_ascii=False)) > EXPERIENCE_CHAR_BUDGET - 600:
        largest = max(data["materials"].values(), key=lambda e: len(json.dumps(e)))
        if largest["experience"]:
            largest["experience"].pop()
            largest["omitted_entries"] += 1
        elif largest.get("recipe_stats"):
            largest["recipe_stats"].pop()
            largest["omitted_recipe_groups"] += 1
        else:
            break
    n = sum(len(e["experience"]) for e in data["materials"].values())
    learned = sum(e["source"] == "review" for v in data["materials"].values()
                  for e in v["experience"])
    result = ToolResult(status="ok", summary=f"{n} experience entr(ies), {learned} learned from "
                        f"runs; {data['note']}", data=data)
    if len(result.to_text()) > EXPERIENCE_CHAR_BUDGET:
        # Legacy/malformed base records may themselves be oversized. The actual
        # transport envelope has a hard limit, including Unicode/JSON escaping.
        return ToolResult(status="needs_attention", summary="Experience summary exceeds the "
                          "response budget even after omitting entries. Narrow the material/topic "
                          "scope; do not infer that evidence or counterevidence is absent.",
                          data={"response_char_budget": EXPERIENCE_CHAR_BUDGET,
                                "omitted": "oversized advisory records; source records retained",
                                "note": data["note"]})
    for m in mats:
        mark_checked(ctx, m)
    return result


class ReviewArgs(BaseModel):
    material: str | None = None


@qumus_tool("review_runs",
            "After experiments: the facts of every finished, not yet reviewed real-lab run "
            "(recipe, purpose, expected vs actual good flakes per chip, corrections, failed "
            "steps), with code votes already applied. Then vote_experience / "
            "propose_experience. Testbench runs are never reviewed.",
            agents={"advisor"}, kind="query")
async def review_runs(args: ReviewArgs, ctx: ToolContext) -> ToolResult:
    res = reviewing.review(ctx.store, args.material)
    mark_checked(ctx, args.material)
    if not res["runs"]:
        return ToolResult(status="ok", summary=res["note"] if res["bench"] != "real"
                          and not res.get("sandbox") else "no finished runs to review",
                          data=res)
    return ToolResult(status="ok", summary=f"{len(res['runs'])} run(s) reviewed, "
                      f"{len(res['auto_votes'])} code vote(s)", data=res)


class ProposeArgs(BaseModel):
    topic: Topic
    scope: Scope = Field(default_factory=Scope)
    claim: str = Field(min_length=1, max_length=400, description="One or two sentences: "
                       "what to do or expect, in which situation")
    evidence_run_ids: list[str] = Field(min_length=1, description="Real runs it rests on")
    observation: str = Field(min_length=1, description="What was seen (facts, with run ids)")
    cause_guess: str = Field("", description="Possible reason; a hypothesis, say so")
    suggestion: str = ""
    recipe: dict[str, float] | None = Field(None, description="exfoliation: the recipe this "
                                            "is about (recipe parameter -> value)")
    expected_good_per_chip: float | None = Field(None, ge=0, description="exfoliation: good "
                                                 "flakes per chip this recipe should give; "
                                                 "lets code vote on later runs")


@qumus_tool("propose_experience",
            "Write a new experience from reviewed real runs. It starts on trial and is "
            "confirmed only after several supporting runs from different batches; enough "
            'contradicting runs retire it. Example: {"topic": "exfoliation", "scope": '
            '{"material": "hBN"}, "claim": "70 C gives more 20-30 nm flakes than 50 C", '
            '"evidence_run_ids": ["run_..."], "observation": "run_... at 70 C: 3 good flakes '
            'per chip; run_... at 50 C: 1", "cause_guess": "...", "recipe": {"target_temp_c": '
            '70}, "expected_good_per_chip": 3}', agents={"advisor"}, kind="state")
async def propose_experience(args: ProposeArgs, ctx: ToolContext) -> ToolResult:
    book = ExperienceBook(ctx.store)
    try:
        eid = book.propose(
            topic=args.topic, claim=args.claim, scope=args.scope.model_dump(),
            evidence_run_ids=args.evidence_run_ids, observation=args.observation,
            cause_guess=args.cause_guess, suggestion=args.suggestion, recipe=args.recipe,
            expect=({"good_per_chip": args.expected_good_per_chip}
                    if args.expected_good_per_chip is not None else None), author=ctx.agent)
    except ExperienceError as e:
        return ToolResult(status="error", summary=str(e))
    e = ctx.store.get_experience(eid) or {}
    return ToolResult(status="ok", summary=f"experience {eid} recorded ({e.get('status')})",
                      data={"experience_id": eid, "status": e.get("status")})


class VoteArgs(BaseModel):
    experience_id: str
    run_id: str
    verdict: Literal["support", "against"]
    note: str = Field(min_length=1, description="What in this run agrees or disagrees")


@qumus_tool("vote_experience",
            "Record that one finished real run supports or contradicts an experience. One "
            "vote per run; status follows the vote rule.", agents={"advisor"}, kind="state")
async def vote_experience(args: VoteArgs, ctx: ToolContext) -> ToolResult:
    try:
        e = ExperienceBook(ctx.store).vote(args.experience_id, args.run_id, args.verdict,
                                           note=args.note, author=ctx.agent)
    except ExperienceError as err:
        return ToolResult(status="error", summary=str(err))
    return ToolResult(status="ok", summary=f"{args.experience_id}: {len(e['support'])} for, "
                      f"{len(e['against'])} against, {e['status']}",
                      data={"experience_id": e["id"], "status": e["status"],
                            "support": e["support"], "against": e["against"]})


class TeachArgs(BaseModel):
    topic: Topic
    scope: Scope = Field(default_factory=Scope)
    claim: str = Field(min_length=1, max_length=400)
    recipe: dict[str, float] | None = None
    instruction_id: str = Field(description="Id of the person's message that said it")


@qumus_tool("teach_experience",
            "Record what a person told you about the lab (\"this crystal is too thick\", "
            "\"use 70 C for hBN\") as experience. It counts at once.",
            agents={"orchestrator"}, kind="state")
async def teach_experience(args: TeachArgs, ctx: ToolContext) -> ToolResult:
    try:
        eid = ExperienceBook(ctx.store).teach(
            topic=args.topic, claim=args.claim, scope=args.scope.model_dump(),
            recipe=args.recipe, instruction_id=args.instruction_id, author=ctx.agent)
    except ExperienceError as e:
        return ToolResult(status="error", summary=str(e))
    return ToolResult(status="ok", summary=f"experience {eid} recorded from the person's "
                      "words (confirmed)", data={"experience_id": eid})


class RetireArgs(BaseModel):
    experience_id: str
    reason: str = Field(min_length=1)
    instruction_id: str = Field(description="Id of the person's message that said it is wrong")


@qumus_tool("retire_experience",
            "A person says an experience is wrong: retire it (kept with the reason).",
            agents={"orchestrator"}, kind="state")
async def retire_experience(args: RetireArgs, ctx: ToolContext) -> ToolResult:
    try:
        ExperienceBook(ctx.store).retire(args.experience_id, reason=args.reason,
                                         instruction_id=args.instruction_id, author=ctx.agent)
    except ExperienceError as e:
        return ToolResult(status="error", summary=str(e))
    return ToolResult(status="ok", summary=f"experience {args.experience_id} retired")


class OutcomeArgs(BaseModel):
    sample_id: str = Field(description="Device, flake or chip the result is about")
    verdict: Literal["good", "bad", "mixed"]
    note: str = Field(min_length=1, description="What the person reported, e.g. measured "
                      "mobility, bubbles, a broken flake")
    instruction_id: str = Field(description="Id of the person's message")


@qumus_tool("record_outcome",
            "Record a result a person reports later, outside the system (a device measured, "
            "a flake that broke). It is kept on the sample and on the flakes of a device, "
            "and the next review sees it.", agents={"orchestrator"}, kind="state")
async def record_outcome(args: OutcomeArgs, ctx: ToolContext) -> ToolResult:
    sample = ctx.store.get_sample(args.sample_id)
    if sample is None:
        return ToolResult(status="error", summary=f"no sample {args.sample_id} in the ledger")
    ins = ctx.store.get_instruction(args.instruction_id)
    if ins is None or ins["source"] != "ui":
        return ToolResult(status="error", summary=f"{args.instruction_id} is not a message a "
                          "person sent")
    entry = {"verdict": args.verdict, "note": args.note, "instruction_id": args.instruction_id,
             "ts": ctx.store.clock.now()}
    related = [args.sample_id, *(sample["props"].get("layers_top_first") or [])]
    for sid in related:
        s = ctx.store.get_sample(sid)
        if s is None:
            continue
        ctx.store.upsert_sample(sid, s["kind"], props={
            "outcomes": [*(s["props"].get("outcomes") or []),
                         {**entry, "about": args.sample_id}]})
    ctx.store.emit("outcome.recorded", {"sample_id": args.sample_id, **entry},
                   agent=ctx.agent)
    return ToolResult(status="ok", summary=f"outcome recorded on {len(related)} sample(s)",
                      data={"samples": related})
