"""Hands' hardware and job tools (spec 6.4).

Hardware tools submit a flow to the job service and return the job id at once.
The agent then waits with wait_job. Results are structured: status ok /
corrected / needs_attention with measurements and photos.

Flow names and parameters are agreed with the testbench workstream (flows are
theirs, in qumus2/flows/library/). A flow that is not registered yet gives an
error result, not a crash.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from qumus2.agents.tooling import ToolContext, ToolResult, qumus_tool
from qumus2.core.materials import normalize_thickness_label
from qumus2.records.budget import plan_target
from qumus2.records.geometry import BridgeRequirement
from qumus2.records.outcomes import conditions_now
from qumus2.runtime.jobs import JobStatus


class PlaceChipArgs(BaseModel):
    chip_id: str = Field(description="Chip id from the sample ledger")


class ExfoliateArgs(BaseModel):
    material: str = Field(description="Material code wanted, e.g. Gr or hBN. The flow picks a "
                          "crystal carrier whose QR code says this material.")
    num_chips: int = Field(1, ge=1, le=6)
    chip_ids: list[str] = Field(default_factory=list,
                                description="Bare chips to use; empty = the flow picks")
    prepare_mother_tape: Literal["auto", "always", "never"] = "auto"
    target_temp_c: Literal[22, 30, 50, 70, 90] = Field(
        50, description="Exfoliation stage temperature (v1 whitelist)")
    dwell_s: int = Field(30, ge=10, le=90)
    num_massages: int = Field(2, ge=1, le=5)
    press_depth_mm: float = Field(0.2, ge=0.05, le=0.6, description="How hard the nozzle "
                                  "presses the tape (mm below first contact)")
    peel_speed: int = Field(200, ge=50, le=500)
    peel_angle_deg: float = Field(30.0, ge=10, le=90, description="Tape peel angle "
                                  "(limits.yaml exfoliator.peel_angle_deg)")
    # Self-evolution (spec 10): why these chips are exfoliated. Recorded with the run.
    purpose: Literal["secure", "explore", "user"] = Field(
        "secure", description="secure = chips the task needs, with the best known recipe; "
        "explore = try one change on spare bare chips; user = parameters a person asked for")
    changed: list[str] = Field(default_factory=list, description="explore: the one recipe "
                               "parameter changed from the baseline recipe")
    hypothesis: str = Field("", description="explore: what you expect and why")
    expected_good_per_chip: float | None = Field(None, ge=0, description="Your prediction: "
                                                 "good flakes per chip")
    instruction_id: str | None = Field(None, description="user: id of the person's message "
                                       "that asked for these parameters")
    target_thickness_labels: list[str] = Field(
        default_factory=list, description="Good flake thickness labels; empty = from the "
        "device plan")
    target_min_area_um2: float | None = Field(None, ge=0, description="Good flake minimum "
                                              "area; None = from the device plan")


#: ExfoliateArgs fields that describe the run (stored in its meta), not the flow
EXFOLIATION_META = ("purpose", "changed", "hypothesis", "expected_good_per_chip",
                    "instruction_id", "target_thickness_labels", "target_min_area_um2")


class SearchChipArgs(BaseModel):
    chip_id: str | None = Field(None, description="Chip to search; None = latest exfoliated")
    material: str | None = Field(None, description="Leave empty: taken from the ledger")
    min_area_um2: float = Field(50, ge=1)
    max_revisits: int = Field(20, ge=0, le=50, description="Candidates photographed at "
                              "20X/100X; device tasks use role quotas, standalone experiments allow any thickness")
    revisit_only: bool = Field(False, description="No new scan: photograph candidates of an "
                               "already scanned current specimen that have no 20X/100X images yet "
                               "(the picker reported relink_or_capture_optical_images). "
                               "Needs chip_id and verified scan history; use false immediately after exfoliation.")
    flake_ids: list[str] = Field(default_factory=list, description="revisit_only: just these "
                                 "eligible candidates, in order, up to max_revisits; explicit IDs override automatic "
                                 "thickness quotas. Empty = by task/device roles")


#: revisit slots per flake the device plan needs (spares for photos that come
#: out badly and flakes the picker turns down)
REVISIT_SLOTS_PER_FLAKE = 3


def revisit_quota(ctx: ToolContext, max_revisits: int = 20) -> list[dict[str, Any]] | None:
    """Use broad experiment coverage, device roles, or the legacy device default."""
    state = ((ctx.store.get_project(ctx.project_id) or {}).get("state", {})
             if ctx.project_id else {})
    if state.get("task_kind") == "experiment":
        return [{"role": "experiment_source", "thickness_labels": [], "min_area_um2": 0,
                 "slots": max_revisits}]
    roles = (state.get("device_plan") or {}).get("flake_roles") or []
    if not roles:
        return None
    return [{"role": r.get("role", ""), "material": r.get("material"),
             "thickness_labels": [n for lab in r.get("thickness_labels") or []
                                  if (n := normalize_thickness_label(r.get("material"), lab))],
             "min_area_um2": float(r.get("min_area_um2") or 0),
             "slots": REVISIT_SLOTS_PER_FLAKE * int(r.get("count", 1))} for r in roles]


class StackDeviceArgs(BaseModel):
    stamp_id: str | None = None

    flake_ids: list[str] = Field(min_length=1, max_length=6, description="Selected flakes in pickup order; "
                                 "the first is the top layer")
    target_chip_id: str = Field(description="Chip to land the stack on, e.g. a patterned "
                                "bottom-electrode chip (list_samples status patterned)")
    target_xy_mm: tuple[float, float] | None = Field(
        None, description="Drop point on the target chip in mm; None = chip centre")
    design_path: str | None = None
    design_digest: str | None = Field(None, description="Optional check: the accepted design's canonical_digest or design_id from the Designer result. Omit it to stack the current accepted design; the trusted tool resolves the current saved revision either way.")
    contact_dwell_s: float | None = Field(None, gt=0, le=60, description="New-design finite pickup contact hold. Omission resolves to an explicitly synthetic 1 second recipe default, not a calibrated adhesion value.")
    target_contact_region_id: str | None = None
    direct_contact_material: Literal["Gr"] | None = None
    bridge_requirement: BridgeRequirement | None = None
    pickup_temp_c: float = Field(90.0, ge=40, le=130)
    drop_temp_c: float = Field(155.0, ge=100, le=170)
    melt_temp_c: float = Field(170.0, ge=150, le=185, description="Above drop_temp_c")


class JobArgs(BaseModel):
    job_id: str
    full: bool = Field(False, description="True = every step's expected values, measurements "
                       "and observations (long); False = the compact result")


class WaitJobArgs(BaseModel):
    job_id: str
    timeout_s: float = Field(120, ge=1, le=600, description="Max seconds to wait")


class HelpArgs(BaseModel):
    reason: str
    job_id: str | None = None


def recoveries(store: Any, run_id: str) -> list[str]:
    """Times the job service restarted this run after the process died (crash
    recovery, spec 8.2), as the reasons it recorded."""
    return [e.payload.get("message", "") for e in store.events_after(0, limit=100_000,
                                                                       run_id=run_id)
            if e.kind == "job.status"
            and str(e.payload.get("message", "")).startswith("recovered")]


#: step record fields Claude gets in a compact job result (the rest - expected
#: values, every measurement, timestamps - is in the run store and in `full`)
_STEP_FIELDS = ("seq", "name", "status", "corrections", "note")


def compact_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: s[k] for k in _STEP_FIELDS if s.get(k) not in (None, "", 0) or k in
             ("seq", "name", "status")} for s in steps]


def _needs_a_look(outcome: dict[str, Any]) -> bool:
    """A step outcome worth the model's attention: not plainly ok, or corrected."""
    return outcome.get("status") != "succeeded" or bool(outcome.get("corrections"))


def compact_result(result: Any) -> Any:
    """Only the step outcomes that were corrected or did not succeed, with
    their observations (measurements, photos); plainly ok steps are counted."""
    if not isinstance(result, list):
        return result
    kept = [o for o in result if isinstance(o, dict) and _needs_a_look(o)]
    return {"steps_ok": sum(1 for o in result if isinstance(o, dict) and not _needs_a_look(o)),
            "steps_to_look_at": kept}


def _chip_summary(store: Any, chip_id: str | None) -> dict[str, Any]:
    c = store.get_sample(chip_id) if chip_id else None
    c = c or {"props": {}}
    return {"chip": chip_id, "status": c.get("status"), "location": c.get("location"),
            "material": c["props"].get("material")}


def ledger_outcome(store: Any, run: dict[str, Any]) -> dict[str, Any] | None:
    """What the sample ledger says after this job: the ids and places the
    agents must report and use next (written by code, not by the flow's words)."""
    p = run.get("params") or {}
    flow = run["flow"]
    if "reactor" in flow:
        chip = store.get_sample(p.get("chip_id")) if p.get("chip_id") else None
        return {"chip": p.get("chip_id"), "location": chip["location"] if chip else None,
                "observation": chip["props"].get("reactor_result") if chip else None,
                "process_checks": [{"step": step["name"], "measured_at": step["finished_at"],
                                    "measurements": step["measurements"]}
                                   for step in store.get_steps(run["id"])
                                   if step["name"] in ("set_reactor_conditions", "disable_reactor_targets",
                                                       "wait_for_reactor_access")
                                   and step["status"] == "done"],
                "state_uncertain": chip["props"].get("reactor_state_uncertain", False) if chip else None,
                "scope": "synthetic reactor; job success is not a growth-quality verdict"}
    if flow == "place_chip_on_transfer_stage":
        return _chip_summary(store, p.get("chip_id"))
    if flow == "exfoliate_chips":
        tape = (store.get_sample("tape") or {"props": {}})["props"]
        return {"material": p.get("material"), "carrier": p.get("carrier_id"),
                "chips": [_chip_summary(store, c) for c in p.get("chip_ids") or []],
                "tape_uses": tape.get("uses"), "tape_roll_left_mm": tape.get("roll_left_mm")}
    if flow in ("search_chip", "revisit_chip"):
        from qumus2.agents.tools.picking import has_images

        cid = p.get("chip_id")
        flakes = [f for f in store.list_samples(kind="flake")
                  if (f["props"].get("chip_id") or f["location"]) == cid]
        by_label: dict[str, int] = {}
        for f in flakes:
            lab = str(f["props"].get("thickness_label"))
            by_label[lab] = by_label.get(lab, 0) + 1
        shot = sorted(f["id"] for f in flakes if has_images(f) and f["status"] != "lost")
        return {**_chip_summary(store, cid), "flakes_in_ledger": len(flakes),
                "by_thickness_label": dict(sorted(by_label.items())),
                "with_20x_100x_images": shot,
                "without_images": sum(1 for f in flakes if f["id"] not in shot
                                      and f["status"] != "lost"),
                "lost": sorted(f["id"] for f in flakes if f["status"] == "lost")}
    if flow == "stack_device":
        ids = list(p.get("flake_ids") or [])
        devices = [d for d in store.list_samples(kind="device")
                   if d["props"].get("layers_top_first") == ids]
        dev = devices[-1] if devices else None
        stamp = store.get_sample(p.get("stamp_id")) if p.get("stamp_id") else None
        return {"device": dev["id"] if dev else None,
                "device_location": dev["location"] if dev else None,
                "layers_top_first": [{"flake": f, **{k: v for k, v in (
                    store.get_sample(f) or {"props": {}})["props"].items()
                    if k in ("material", "thickness_label")},
                    "status": (store.get_sample(f) or {}).get("status"),
                    "location": (store.get_sample(f) or {}).get("location")} for f in ids],
                "target_chip": _chip_summary(store, p.get("target_chip_id")),
                "stamp": p.get("stamp_id"), "stamp_status": stamp["status"] if stamp else None,
                "stamp_location": stamp["location"] if stamp else None,
                # torn stamps the flow changed on its own (back in their slots, torn)
                "torn_stamps_changed": [sw.get("torn") for sw in p.get("stamp_swaps") or []]}
    return None


def correction_lines(store: Any, run_id: str) -> list[str]:
    """One line per automatic correction of this job, from the step events:
    which step, which attempt, what was measured and what the code did."""
    seen: dict[tuple[int, int], str] = {}  # (seq, attempt) -> what verify saw
    lines = []
    for e in store.events_after(0, limit=100_000, run_id=run_id):
        p = e.payload
        if e.kind == "step.verified" and p.get("attempt") is not None:
            seen[(p.get("seq"), p["attempt"])] = p.get("message") or p.get("verdict") or ""
        elif e.kind == "step.corrected":
            saw = seen.get((p.get("seq"), p.get("attempt")), "")
            lines.append(f"{p.get('name')} attempt {p.get('attempt')}: "
                         + (f"{saw}; " if saw else "") + f"{p.get('action')}"
                         + (f" {p.get('params')}" if p.get("params") else ""))
    return lines


def job_result(st: JobStatus, recovered: list[str] | None = None, *, store: Any = None,
               compact: bool = True) -> ToolResult:
    """The hands' view of a job. Compact (default): step names and statuses,
    the outcomes that need a look (corrected or not ok, with measurements and
    photos) and what the ledger says now (`outcome`). A full stack_device
    result is ~50 KB; the compact one is a few KB. `full=true` on
    get_job_status gives everything."""
    data = st.to_dict()
    images = [img for o in (st.result or []) if isinstance(o, dict)
              for obs in o.get("observations", []) for img in obs.get("images", [])]
    if compact:
        data["steps"] = compact_steps(st.steps)
        data["result"] = compact_result(st.result)
        if st.workflow is not None:
            data["workflow"] = compact_manifest(st.workflow)
    if store is not None and (run := store.get_run(st.run_id)) is not None:
        data["outcome"] = ledger_outcome(store, run)
        data["corrections"] = correction_lines(store, st.run_id)
    if recovered:  # the agents must be able to report a crash, not only its fix
        data["recovered_after_crash"] = recovered
    if not st.done:
        return ToolResult(status="pending", summary=f"job {st.run_id} still {st.status}",
                          data=data)
    if st.status == "succeeded":
        corrected = any(s["corrections"] for s in st.steps)
        # Successful jobs must return pixels too. In particular, the finished
        # device image must not be hidden behind nested paths in a job record.
        final_photos = []
        device_id = (data.get("outcome") or {}).get("device")
        if store is not None and device_id:
            device = store.get_sample(device_id)
            if device:
                photos = device["props"].get("images") or []
                final_photos = list(photos.values()) if isinstance(photos, dict) else list(photos)
        shown = list(dict.fromkeys([*final_photos, *reversed(images)]))[:4]
        notes = (["after corrections"] if corrected else []) + (
            [f"after the lab process crashed and the job resumed automatically "
             f"({len(recovered)}x)"] if recovered else [])
        return ToolResult(status="corrected" if notes else "ok",
                          summary=f"job {st.run_id} succeeded" +
                          (" " + "; ".join(notes) if notes else ""), data=data, images=shown)
    if st.status == "paused":
        return ToolResult(status="paused", summary=f"job {st.run_id} paused: the user pressed "
                          "Stop, or this brain lost control of the station (its lease ran "
                          "out). The job may still hold a chip on the stage. Resume it with "
                          "retry_job before sending another job to that module; nothing "
                          "failed.", data=data)
    return ToolResult(status="needs_attention", summary=f"job {st.run_id} {st.status}"
                      + (" (the lab process crashed during it)" if recovered else ""),
                      data=data, images=images)


def unknown_job(ctx: ToolContext, job_id: str) -> ToolResult | None:
    """Reject an unknown exact ID without turning a lookup typo into new work."""
    if ctx.store.get_run(job_id) is not None:
        return None
    from qumus2.records.plan_completion import linked_job_evidence

    step_id = ctx.extra.get("delegation_step_id")
    known = linked_job_evidence(ctx.store, ctx.project_id, step_id)
    guidance = ("Compare the current step's known_jobs and their target/link evidence, copy the exact full ID, "
                "then explicitly call get_job_status or wait_job. These records do not select a replacement or certify completion."
                if known else "No linked job evidence is available for a uniquely identified current delegation. "
                "Return to the orchestrator to establish the exact job and step from saved records.")
    return ToolResult(status="error", summary=f"no job {job_id!r}: this exact ID was not found. "
                      "An unknown ID does not establish that work is missing; do not submit or repeat a job to fix a lookup. "
                      + guidance,
                      # Candidate IDs must stay nested: top-level job_id/run_id
                      # is execution evidence consumed by the delegation checker.
                      data={"error": "unknown_job", "requested_job_id": job_id,
                            "project_id": ctx.project_id, "step_id": step_id,
                            "known_jobs": known, "next_action": "verify_exact_job_reference"})


def queued_behind(ctx: ToolContext, flow: str, run_id: str) -> str | None:
    """The job of the same flow this one waits for (the job service runs jobs
    that share devices one after the other). A real run submitted two
    exfoliations back to back and the second failed on a slot the first had
    filled; the flows now book their slots and chips at submit (the run
    store's `reservation` table, testbench workstream), so queuing two
    hardware jobs is fine and the result says so."""
    for r in ctx.store.list_runs():
        if r["id"] != run_id and r["flow"] == flow and r["status"] in ("queued", "running"):
            return r["id"]
    return None


def reserved_by(ctx: ToolContext, run_id: str) -> list[str]:
    """Slots and samples the flow booked for this job at submit, if the run
    store has reservations (added with the slot-reservation work)."""
    read = getattr(ctx.store, "reservations", None)
    if read is None:
        return []
    try:
        return sorted(k for k, v in read().items() if v.get("run_id") == run_id)
    except Exception:  # noqa: BLE001 - a nicer message only
        return []


def submit(ctx: ToolContext, flow: str, params: dict[str, Any],
           meta: dict[str, Any] | None = None) -> ToolResult:
    try:
        rid = ctx.jobs.submit(flow, params, project_id=ctx.project_id, requested_by=ctx.agent)
    except KeyError:
        return ToolResult(status="error", summary=f"flow {flow!r} is not available in this lab "
                          "yet", data={"flow": flow})
    except ValueError as e:  # the flow refused the parameters (e.g. no matching carrier)
        return ToolResult(status="error", summary=str(e), data={"flow": flow})
    if meta:
        ctx.store.update_run_meta(rid, **meta)
    behind, booked = queued_behind(ctx, flow, rid), reserved_by(ctx, rid)
    note = (f", queued behind {behind}" if behind else "") + (
        f"; reserved for it: {', '.join(booked)}" if booked else "")
    return ToolResult(status="pending", summary=f"job {rid} submitted ({flow}){note}; call "
                      "wait_job", data={"job_id": rid, "flow": flow, "queued_behind": behind,
                                        "reserved": booked})


@qumus_tool("place_chip_on_transfer_stage",
            "Move a chip FROM its storage slot TO the transfer stage; this cannot store or "
            "unload a chip. Verified by camera and "
            "re-placed automatically if off. Returns a job id; then call wait_job.",
            agents={"hands"}, kind="hardware", refs={"chip_id": "chip"})
async def place_chip_on_transfer_stage(args: PlaceChipArgs, ctx: ToolContext) -> ToolResult:
    return submit(ctx, "place_chip_on_transfer_stage", args.model_dump())


def exfoliation_meta(args: ExfoliateArgs, ctx: ToolContext) -> dict[str, Any]:
    """Purpose, target and conditions of an exfoliation, recorded with the run so
    its real result can later be judged against what it was for."""
    state = ((ctx.store.get_project(ctx.project_id) or {}).get("state", {})
             if ctx.project_id else {})
    target = plan_target(state, args.material) or {}
    if args.target_thickness_labels:  # in the ledger's words, as the search writes them
        target["thickness_labels"] = [
            n for lab in args.target_thickness_labels
            if (n := normalize_thickness_label(args.material, lab)) is not None]
    if args.target_min_area_um2 is not None:
        target["min_area_um2"] = args.target_min_area_um2
    if target:
        target.setdefault("thickness_labels", [])
        target.setdefault("min_area_um2", 0.0)
    return {"purpose": args.purpose, "changed": args.changed, "hypothesis": args.hypothesis,
            "expected_good_per_chip": args.expected_good_per_chip,
            "instruction_id": args.instruction_id, "target": target or None,
            "conditions": conditions_now(ctx.store, args.material)}


@qumus_tool("exfoliate_chips",
            "Exfoliate a crystal onto bare chips (v1 assembly_exfoliation). Each chip is "
            "checked after peeling; the material label comes from the carrier QR code, not "
            "from you. Chips the task needs use the best known recipe (purpose secure); only "
            "spare bare chips may try one change (purpose explore, with a hypothesis). The "
            "advisor must have looked up the material's experience first. Returns a job id; "
            "then call wait_job.",
            agents={"hands"}, kind="hardware", refs={"chip_ids": "chip"},
            requires=("experience_checked", "runs_reviewed", "exploration_budget"))
async def exfoliate_chips(args: ExfoliateArgs, ctx: ToolContext) -> ToolResult:
    meta = exfoliation_meta(args, ctx)
    params = args.model_dump(exclude=set(EXFOLIATION_META))
    return submit(ctx, "exfoliate_chips", params, meta)


@qumus_tool("search_chip",
            "Search an exfoliated chip under the microscope (v1 search_chip_assembly): 10X "
            "scan, then the best candidates for each role of the device plan are photographed "
            "at 20X/100X, then return it to storage. home_slot is its location at submission, "
            "after earlier work finishes; an occupied slot can fall back to another free slot "
            "in the same storage station. This flow does not promise a fixed final slot. "
            "Do not schedule a separate storage move after success; read wait_job.outcome. "
            "All found flakes go to the sample ledger. With revisit_only, no "
            "scan: photograph stored candidates that have no 20X/100X images yet (cheaper "
            "than exfoliating more). revisit_only requires a verified prior scan of this chip's current "
            "specimen, including a legitimate zero-candidate scan. For a newly exfoliated chip use "
            "revisit_only=false first; an empty revisit is not a search. Returns a job id.",
            agents={"hands"}, kind="hardware", refs={"chip_id": "chip", "flake_ids": "flake"})
async def search_chip(args: SearchChipArgs, ctx: ToolContext) -> ToolResult:
    params = args.model_dump(exclude={"revisit_only", "flake_ids"})
    params["revisit_quota"] = revisit_quota(ctx, args.max_revisits)
    if not args.revisit_only:
        return submit(ctx, "search_chip", params)
    if not args.chip_id:
        return ToolResult(status="error", summary="revisit_only needs the chip_id of a "
                          "searched chip (the candidates' location in list_samples)")
    return submit(ctx, "revisit_chip", {**params, "flake_ids": args.flake_ids})


@qumus_tool("stack_device",
            "Transfer the accepted top-first stack onto its observed target (electrodes or predeposited metal). "
            "Uses the shared guarded transport/pickup/release composition, the known stamp property, and the target task contract. Returns a job id.",
            agents={"hands"}, kind="hardware",
            refs={"flake_ids": "flake", "target_chip_id": "chip"},
            requires=("stack_authorized",))
async def stack_device(args: StackDeviceArgs, ctx: ToolContext) -> ToolResult:
    from qumus2.records.reactor_requirements import established_requirements

    if established_requirements(ctx.store, ctx.project_id):
        # Route by the accepted task, preserving the existing checked job/receipt.
        # No second scheduler or unguarded internal helper is exposed to the agent.
        return submit(ctx, "prepare_material_metal", {"project_id": ctx.project_id,
            "target_chip_id": args.target_chip_id, "flake_ids": args.flake_ids,
            "stamp_id": args.stamp_id, "target_xy_mm": args.target_xy_mm})
    params = args.model_dump()
    state = ((ctx.store.get_project(ctx.project_id) or {}).get("state", {}) if ctx.project_id else {})
    plan = state.get("device_plan") or {}
    if state.get("device_design"):
        from qumus2.records.device_design import validate_design_current

        try:
            design = validate_design_current(ctx.store, ctx.project_id)
            if args.design_digest is not None and args.design_digest not in (
                    design.canonical_digest, design.design_id):
                raise ValueError(
                    f"design_digest {args.design_digest!r} is not the current accepted design "
                    f"(design_id {design.design_id}, revision {design.revision}, canonical_digest "
                    f"{design.canonical_digest}). Omit design_digest to stack the current accepted design.")
            if args.flake_ids != list(design.pickup_order) or args.target_chip_id != design.target_chip_id:
                raise ValueError("Stack order/target must match the accepted device design")
            params.pop("design_digest", None)
            params["device_design"] = design.model_dump(mode="json")
            # v1 has no paper-window alignment: the preset stamp position is
            # assumed to keep the contact point in view (owner, 2026-11-05).
            if getattr(ctx.store, "bench", None) == "sim":
                params["skip_aperture_alignment"] = True
        except (ValueError, OSError) as exc:
            return ToolResult(status="error", summary=str(exc))
        return submit(ctx, "stack_device", params)
    params.pop("design_digest", None)
    if plan.get("direct_contact_material"):
        for key in ("target_contact_region_id", "direct_contact_material", "bridge_requirement"):
            if params.get(key) is not None and params[key] != plan[key]:
                return ToolResult(status="error", summary="Stack direct-contact parameters differ from accepted Device Guidance")
            params[key] = plan[key]
        params["direct_contact_intent"] = plan["direct_contact_intent"]
    return submit(ctx, "stack_device", params)


@qumus_tool("wait_job", "Wait up to timeout_s for a job; returns progress if not done. A "
            "finished job returns its steps, one line per automatic correction, the steps "
            "that need a look, and `outcome`: what the sample ledger says now (ids and "
            "places to report).",
            agents={"hands", "orchestrator", "reactor"}, kind="job")
async def wait_job(args: WaitJobArgs, ctx: ToolContext) -> ToolResult:
    import asyncio

    if (unknown := unknown_job(ctx, args.job_id)) is not None:
        return unknown
    st = await asyncio.to_thread(ctx.jobs.wait, args.job_id, args.timeout_s)
    return job_result(st, recoveries(ctx.store, args.job_id), store=ctx.store)


@qumus_tool("get_job_status", "Current status of a job (full=true: every step's expected "
            "values, measurements and observations).",
            agents={"hands", "orchestrator", "eyes", "reactor"}, kind="job")
async def get_job_status(args: JobArgs, ctx: ToolContext) -> ToolResult:
    if (unknown := unknown_job(ctx, args.job_id)) is not None:
        return unknown
    return job_result(ctx.jobs.status(args.job_id), recoveries(ctx.store, args.job_id),
                      store=ctx.store, compact=not args.full)


#: denials after which a person is the right next step (the recovery budget is
#: spent); every other rule means another agent must act first
_HUMAN_AFTER = frozenset({"recovery_budget"})


def denial_before_help(ctx: ToolContext) -> dict[str, Any] | None:
    """The denial this agent just got, when its last tool answer was a rule
    denial other than the recovery budget. Two real runs (2026-10-22) called a
    human right after exfoliate_chips was refused by experience_checked, and
    left a needs_human request open although the orchestrator had already
    sent the advisor."""
    last = None
    for e in reversed(ctx.store.events_after(0, limit=100_000)):
        if e.agent != ctx.agent or e.kind not in ("tool.denied", "tool.result"):
            continue
        last = e
        break
    if last is None or last.kind != "tool.denied":
        return None
    rule = last.payload.get("rule")
    if rule in _HUMAN_AFTER:
        return None
    return {"tool": last.payload.get("tool"), "rule": rule, "reason": last.payload.get("reason")}


@qumus_tool("request_human_help", "Ask a human for help (L2). Use only when no recovery tool "
            "fits. A tool refused by a lab rule (a 'denied' answer naming the rule) is not a "
            "reason: return that reason to the orchestrator, which sends the right agent.",
            agents={"hands", "orchestrator", "reactor"}, kind="notify")
async def request_human_help(args: HelpArgs, ctx: ToolContext) -> ToolResult:
    if (d := denial_before_help(ctx)) is not None:
        return ToolResult(status="error", summary=(
            f"not a problem for a person: {d['tool']} was refused by the lab rule "
            f"{d['rule']} ({d['reason']}). End your turn and return that reason to the "
            "orchestrator; it sends the right agent (the advisor for experience, the eyes "
            "for the bench)."), data=d)
    ctx.store.emit("notification", {"level": "needs_human", "text": args.reason},
                   run_id=args.job_id, agent=ctx.agent)
    return ToolResult(status="ok", summary="human notified")


def compact_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Keep molecular identities/ranges in routine tool replies; full=true retains leaves."""
    return {key: [compact_manifest(child) for child in value] if key == "children" else value
            for key, value in manifest.items() if key not in ("checkpoints", "description")}
