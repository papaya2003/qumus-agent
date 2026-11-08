"""Record purposeful release demonstrations with real model decisions.

Paid calls require --live and a <= $40 run cap. All devices are simulated.
Operator messages are scripted test inputs delivered through the normal inbox.
Only recordings whose evidence checks pass enter the public-in-repository catalog;
failed attempts and their costs remain in the private runs/checks/release folder.
A reactor run stopped only by its budget can resume in the same spend scope;
the export preserves and labels the interruption and each source segment.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qumus2.app import build_services  # noqa: E402
from qumus2.config import load_yaml, simulation_profile  # noqa: E402
from qumus2.core.scrub import scrub_text  # noqa: E402
from qumus2.ui.recording import (  # noqa: E402
    attach_recorder,
    check_recording,
    export_recording,
    source_fingerprint,
)

STORIES = {
    "collaboration": {
        "title": "Change the specimen, then change the material",
        "purpose": "Follow a mid-turn specimen constraint, extend the task to a second "
                   "material, and stop at the requested boundary while preserving both results.",
        "request": "This is a standalone material preparation experiment, not a device build. "
                   "Find graphene flakes on one bare chip; chip_001 is my initial choice. "
                   "Inspect first, get the Advisor's recipe, exfoliate and search under the "
                   "microscope, save the images and return the chip to storage. Prepare only "
                   "one chip, then stop for my review. Do not stack anything.",
    },
    "recovery": {
        "title": "Recover from three faults in one transfer",
        "purpose": "An empty pickup, misplaced chip and serial timeout actually occur; "
                   "bounded correction and fresh verification complete the same transfer.",
        "request": "Place chip_001 on the transfer stage. Inspect the bench first and "
                   "delegate to Hands. Handle recoverable errors with the available bounded "
                   "recovery tools, verify the final position and show its observation. "
                   "Stop and ask for help if recovery cannot establish the state. "
                   "Do not exfoliate, search for flakes or stack a device.",
    },
    "reactor": {
        "title": "From ordinary stock to one small reactor product",
        "purpose": "Prepare material from ordinary stock, choose two imaged flakes, transfer "
                   "them onto one narrow carrier, react locally and return one verified product "
                   "to cold storage, then recheck that same product on the ordinary microscope "
                   "with both source identities retained.",
        "request": "Run a standalone two-flake reactor simulation experiment from the current "
                   "ordinary material stock, with no preprepared pair. Inspect the bench and "
                   "read the reactor setup. Obtain the material recipe, prepare and microscope-search "
                   "ordinary chips as needed. Choose exactly two actual catalogued, imaged small "
                   "flakes that can overlap, and physically stack them onto reactor_chip_01. "
                   "This is a qualitative reaction test; any supported pair is acceptable and "
                   "no particular layer count or chemistry is claimed. Then set a two-flake goal, "
                   "use the nominal simulated reactor recipe, observe and accept at most one "
                   "small verified product. Finally switch off the heater and gas, cool to the "
                   "checked access limits, and return the carrier to storage. Keep both source IDs, "
                   "the product image and provenance. Move the cold carrier through the available "
                   "product inspection route to the ordinary microscope, verify that same product "
                   "and return it to storage again. Make the final storage and process conditions "
                   "explicit in the Task Plan. If any step cannot be verified, report the actual "
                   "state and use available bounded recovery without inventing completion.",
    },
}
STORIES["collaboration-recovery"] = {
    **STORIES["collaboration"],
    "title": "Changed instructions and three recoverable faults",
    "purpose": "Preserve a reserved chip during a mid-turn change, prepare and photograph "
               "graphene and hBN, and recover from an empty pickup, misplaced chip and "
               "serial timeout during the first microscope search. Stop after the final review.",
}


def inject_transfer_faults(services):
    for name, params in [("serial_timeout", {"device": "arm_right", "count": 1}),
                         ("pick_fail_next", {"count": 1}),
                         ("next_placement", {"dx": 2.2, "dy": -1.2, "drot": 2.0})]:
        services.lab.faults.inject(name, params)
        services.store.emit("fault.injected", {"fault": name, "params": params})


def audit_search_recovery(events, runs):
    """Faults must occur and be corrected in one completed search, not just be armed."""
    evidence = {}
    successful = {r['id'] for r in runs if r['flow'] == 'search_chip'
                  and r['status'] == 'succeeded' and r['params'].get('chip_id') == 'chip_002'}
    categories = {
        'serial_timeout_observed': lambda e: e['kind'] == 'notification'
        and 'serial timeout' in e['payload'].get('text', '').lower(),
        'empty_pickup_observed': lambda e: e['kind'] == 'notification'
        and 'pick came up empty' in e['payload'].get('text', '').lower(),
        'placement_correction_observed': lambda e: e['kind'] == 'step.corrected',
    }
    matching_runs = set(successful)
    checks = {}
    for name, matches in categories.items():
        found = [e for e in events if matches(e) and e.get('run_id') in successful]
        evidence[name] = [e['id'] for e in found]
        checks[name] = bool(found)
        matching_runs &= {e.get('run_id') for e in found}
    checks['three_faults_recovered_in_one_search'] = bool(matching_runs)
    evidence['recovered_search_job_ids'] = sorted(matching_runs)
    return checks, evidence


def require_prepared_material(services, chip_id, material):
    """Do not send an operator review that presupposes work which never happened."""
    from qumus2.records.plan_completion import unmet_sample_outcomes

    problems = unmet_sample_outcomes(services.store, services.lab.bench, [{
        'sample_id': chip_id, 'status': 'searched', 'in_storage': True}])
    photos = [f for f in services.store.list_samples(kind='flake', location=chip_id)
              if f['props'].get('material') == material and f['props'].get('images')]
    project = services.store.get_project(services.project_id) or {}
    unfinished = [step for step in project.get('plan', []) if step.get('owner') in ('hands', 'reactor')
                  and step.get('status') not in ('done', 'skipped')]
    if problems or not photos or unfinished:
        raise RuntimeError(f'Material preparation did not finish for {chip_id} ({material}); '
                           'stop this acceptance attempt before sending the next scripted review.')


def audit_story(case, events, runs, samples, operator_inputs):
    """Require execution evidence; injected faults and model prose do not prove recovery."""
    requests = [e for e in events if e["kind"] == "agent.request"]
    turns = [e for e in events if e["kind"] == "agent.turn_end"]
    checks = {
        "model_requests_measured": len(requests) >= 3 and all(
            type(e["payload"].get("text_chars")) is int and e["payload"]["text_chars"] >= 0
            for e in requests),
        "multiple_agents_executed": len({e.get("agent") for e in requests} - {None}) >= 2,
        "model_turns_completed": bool(turns) and all(not e["payload"].get("is_error") for e in turns),
        "no_model_error": not any(e["kind"] == "agent.error" for e in events),
        "no_safety_violation": not any(e["kind"] == "safety.violation" for e in events),
        "no_unfinished_job": bool(runs) and all(r["status"] == "succeeded" for r in runs),
    }
    evidence = {"request_event_ids": [e["id"] for e in requests],
                "plan_event_ids": [e["id"] for e in events if e["kind"] == "plan.updated"]}
    if case == "recovery":
        notifications = [e for e in events if e["kind"] == "notification"]
        for name, phrase in [("serial_timeout_observed", "serial timeout"),
                             ("empty_pickup_observed", "pick came up empty")]:
            ids = [e["id"] for e in notifications if phrase in e["payload"].get("text", "").lower()]
            checks[name] = bool(ids)
            evidence[name] = ids
        corrected = [e["id"] for e in events if e["kind"] == "step.corrected"]
        checks["placement_correction_observed"] = bool(corrected)
        evidence["placement_correction_observed"] = corrected
        checks["verified_transfer"] = any(r["flow"] == "place_chip_on_transfer_stage"
            and r["status"] == "succeeded" and r["params"].get("chip_id") == "chip_001" for r in runs)
        checks["chip_on_stage"] = any(s["id"] == "chip_001" and s["location"] == "transfer_stage" for s in samples)
    elif case in ("collaboration", "collaboration-recovery"):
        checks["mid_turn_constraint"] = any(x.get("sent_while_busy") and x["kind"] == "plan_change"
                                              for x in operator_inputs)
        checks["multiple_operator_changes"] = len(operator_inputs) >= 4 and sum(
            x["kind"] == "plan_change" for x in operator_inputs) >= 2
        checks["reserved_chip_untouched"] = any(s["id"] == "chip_001" and s["status"] == "bare"
                                                   and s["location"] == "left_chip_1" for s in samples)
        for material in ("Gr", "hBN"):
            chips = [s for s in samples if s["kind"] == "chip" and s["status"] == "searched"
                     and s["props"].get("material") == material and "chip_" in s["location"]]
            photographed = [s["id"] for s in samples if s["kind"] == "flake" and s["props"].get("images")
                            and s["props"].get("material") == material
                            and s["location"] in {c["id"] for c in chips}]
            checks[f"{material}_saved_and_photographed"] = bool(chips) and bool(photographed)
            evidence[f"{material}_flake_ids"] = photographed
        exfoliations = [r for r in runs if r["flow"] == "exfoliate_chips"]
        checks["exactly_two_prepared_chips"] = len({c for r in exfoliations for c in r["params"].get("chip_ids", [])}) == 2
        checks["task_revisions_recorded"] = len(evidence["plan_event_ids"]) >= 3
        conditions = [condition for e in events if e["kind"] == "plan.updated"
                      for step in e["payload"].get("plan", [])
                      for condition in step.get("sample_outcomes", [])]
        checks["sample_completion_conditions_recorded"] = all(any(
            c.get("sample_id") == cid and c.get("status") == "searched" and c.get("in_storage") is True
            for c in conditions) for cid in ("chip_002", "chip_003"))
        last = operator_inputs[-1]["event_id"] if operator_inputs else 0
        checks["final_review_caused_no_new_experiment"] = bool(last) and not any(
            e["id"] > last and e["kind"] == "job.submitted" and e["payload"].get("flow") != "inspect_bench"
            for e in events)
        checks["no_stacking"] = not any(r["flow"] == "stack_device" for r in runs)
        if case == 'collaboration-recovery':
            fault_checks, fault_evidence = audit_search_recovery(events, runs)
            checks.update(fault_checks)
            evidence.update(fault_evidence)
    else:
        by_id = {s["id"]: s for s in samples}
        carrier = by_id.get("reactor_chip_01", {})
        pair = carrier.get("props", {}).get("reactor_pair", {})
        inputs = pair.get("input_flake_ids", [])
        products = [s for s in samples if s.get("status") == "reactor_product"]
        checks["ordinary_material_prepared_and_searched"] = all(any(
            r["flow"] == name and r["status"] == "succeeded" for r in runs)
            for name in ("exfoliate_chips", "search_chip"))
        checks["physical_pair_preparation"] = pair.get("preparation_source") == "stacking_job" and any(
            r["flow"] == "prepare_reactor_pair" and r["status"] == "succeeded" for r in runs)
        checks["two_actual_catalogued_inputs"] = len(set(inputs)) == 2 and all(
            by_id.get(fid, {}).get("props", {}).get("images") and
            by_id.get(fid, {}).get("props", {}).get("material_placement") for fid in inputs)
        checks["one_local_product"] = len(products) == 1 and set(
            products[0].get("props", {}).get("source_flake_ids", [])) == set(inputs)
        checks["product_has_saved_image"] = len(products) == 1 and bool(
            products[0].get("props", {}).get("images", {}).get("reactor"))
        checks["same_product_rechecked_on_ordinary_microscope"] = len(products) == 1 and bool(
            products[0].get("props", {}).get("images", {}).get("ordinary_microscope")) and any(
                r["flow"] == "inspect_reactor_product" and r["status"] == "succeeded" for r in runs)
        checks["sources_consumed"] = bool(inputs) and all(
            by_id.get(fid, {}).get("status") == "reactor_consumed" for fid in inputs)
        checks["reaction_executed"] = any(r["flow"] == "reactor_reaction" and r["status"] == "succeeded" for r in runs)
        checks["carrier_returned_to_storage"] = carrier.get("location", "").startswith("reactor_chip_slot_")
        process = [e for e in events if e["kind"] == "reactor.process"
                   and e["payload"].get("chip_id") == "reactor_chip_01"]
        checks["process_targets_disabled"] = bool(process) and process[-1]["payload"].get("temperature_target_c", 1) is None \
            and process[-1]["payload"].get("flow_target_sccm", -1) == 0
        evidence.update(input_flake_ids=inputs, product_ids=[p["id"] for p in products],
                        final_process_event_id=process[-1]["id"] if process else None)
    return checks, evidence


def validate_budget_resume(previous, meta, events, *, case, seed, morphology):
    """Only continue an honestly recorded budget stop; never relabel another failure."""
    errors = [e for e in events if e['kind'] == 'agent.turn_end' and e['payload'].get('is_error')]
    allowed = {'model_turns_completed', 'physical_plan_closed', 'task_plan_closed', 'resumed_turn_completed'}
    failed = {key for key, passed in previous['checks'].items() if not passed}
    if (case != 'reactor' or previous.get('case') != case or previous.get('passed')
            or previous.get('error') or not errors
            or any(e['payload'].get('reason') != 'budget_exhausted' for e in errors)
            or not failed or not failed <= allowed
            or meta.get('seed') != seed or meta.get('simulation_profile') != 'integrated'
            or meta.get('material_profile') != ('material-morphology.yaml' if morphology else 'default')
            or not meta.get('execution_source_sha256')):
        raise ValueError('Resume requires a matching reactor recording stopped only by its budget, '
                         'with all physical evidence checks already passed.')


def validate_preparation_repair_resume(previous, meta, events, *, case, seed, morphology):
    """Continue the saved, unstarted preparation after its recorded plan-admission repair."""
    inputs = previous.get('operator_inputs', [])
    refused = any(e['kind'] == 'tool.result' and e['payload'].get('tool') == 'update_plan'
                  and e['payload'].get('status') == 'error'
                  and 'Physical cancellation/replacement' in e['payload'].get('summary', '')
                  for e in events)
    jobs = previous.get('jobs', [])
    if (case != 'collaboration-recovery' or previous.get('case') != case
            or previous.get('passed') or not refused or not jobs
            or any(r['flow'] != 'inspect_bench' or r['status'] != 'succeeded' for r in jobs)
            or not str(previous.get('error', '')).startswith('Material preparation did not finish for chip_002 (Gr)')
            or len(inputs) != 2 or inputs[-1].get('kind') != 'plan_change'
            or not inputs[-1].get('sent_while_busy')
            or any(e['kind'] in ('agent.error', 'fault.injected')
                   or e['kind'] == 'agent.turn_end' and e['payload'].get('is_error') for e in events)
            or meta.get('seed') != seed or meta.get('simulation_profile') != 'integrated'
            or meta.get('material_profile') != ('material-morphology.yaml' if morphology else 'default')
            or not meta.get('execution_source_sha256')):
        raise ValueError('Repair continuation requires the recorded unstarted collaboration preparation '
                         'blocked by plan admission, with its original setup and events intact.')


async def record(args):
    execution_source = source_fingerprint()
    resuming = args.resume or args.resume_repair
    # Per-process overrides: no key changes, hosted fees or real-photo playback.
    os.environ["QUMUS_OPENAI_WEB_SEARCH"] = "0"
    os.environ.pop("QUMUS2_MICROSCOPE_REPLAY_DIR", None)
    folder = Path("runs/checks/release") / args.name
    previous, previous_meta, prefix_rows, first_event = None, None, [], 0
    if resuming:
        previous = json.loads((folder / 'summary.json').read_text())
        original = json.loads((folder / 'export' / 'recording.json').read_text())
        previous_meta = original['meta']
        validator = validate_budget_resume if args.resume else validate_preparation_repair_resume
        validator(previous, previous_meta, original['events'],
                  case=args.case, seed=args.seed, morphology=args.morphology)
        with sqlite3.connect(f'file:{folder / "qumus.db"}?mode=ro', uri=True) as db:
            prefix_rows = db.execute('SELECT * FROM event ORDER BY id').fetchall()
        first_event = prefix_rows[-1][0]
        (folder / 'summary.json').rename(folder / f'summary-before-resume-{first_event}.json')
        (folder / 'export').rename(folder / f'export-before-resume-{first_event}')
    else:
        folder.mkdir(parents=True, exist_ok=False)
    config = simulation_profile()
    if args.morphology:
        config["exfoliation"].update(load_yaml("material-morphology.yaml")["exfoliation"])
    s = build_services(folder / "qumus.db", use_openai=True, lab_process=True,
                       seed=args.seed, run_budget_usd=args.budget, testbench_config=config)
    bench = s.lab.bench
    rec = attach_recorder(s)
    operator_inputs = list(previous['operator_inputs']) if previous else []
    pending = None
    fault_monitor = None
    error = None

    def message(text, kind="message"):
        busy = s.runtime.busy
        ident = s.store.add_instruction(text, kind=kind, project_id=s.project_id)
        event_id = next(e.id for e in reversed(s.store.events_after(0, limit=1000000))
                        if e.kind == "user.message" and e.payload.get("instruction_id") == ident)
        operator_inputs.append({"id": ident, "event_id": event_id, "text": text,
                                "kind": kind, "sent_while_busy": busy})
        print("Operator:", text, flush=True)

    async def turn():
        await s.driver.wait_idle(timeout=args.timeout)

    async def arm_search_faults():
        # The first exfoliation is complete before faults are armed. The next
        # physical job must be the normal full-chip search, not a seeded transfer.
        while True:
            completed = [r for r in s.store.list_runs() if r['flow'] == 'exfoliate_chips'
                         and r['status'] == 'succeeded' and 'chip_002' in r['params'].get('chip_ids', [])]
            if completed:
                inject_transfer_faults(s)
                return
            await asyncio.sleep(0.05)

    try:
        if args.case == "recovery":
            inject_transfer_faults(s)
        if args.case == 'collaboration-recovery':
            fault_monitor = asyncio.create_task(arm_search_faults())
        resume_message = ('Continue my saved one-chip graphene preparation. Keep chip_001 untouched. '
                'Use chip_002 for graphene instead. My earlier retarget instruction still applies. Retain the '
                'completed inspection and recipe advice, update the Task Plan, exfoliate and '
                'perform the full microscope search, save the images and return chip_002 to '
                'storage, then stop for my review. Do not stack anything.' if args.resume_repair else
                'Continue the saved experiment after the budget stop. Inspect the current '
                'Task Plan and job evidence, verify any remaining final conditions, close '
                'only the steps supported by that evidence, and report the product and '
                'its saved images. Preserve the completed preparation and reaction.')
        message(resume_message if resuming else STORIES[args.case]["request"])
        pending = asyncio.create_task(turn())
        if args.case in ("collaboration", "collaboration-recovery") and not resuming:
            seen = 0
            while not pending.done():
                batch = s.store.events_after(seen, limit=10000)
                if batch:
                    seen = batch[-1].id
                if any(e.kind == "tool.call" and e.agent == "eyes" for e in batch):
                    message("Change of plan: keep chip_001 untouched for another experiment. "
                            "Use chip_002 for graphene instead. Update the task before moving "
                            "anything, and still prepare just one chip.", "plan_change")
                    break
                await asyncio.sleep(0.05)
        await pending
        if args.case in ("collaboration", "collaboration-recovery"):
            require_prepared_material(s, 'chip_002', 'Gr')
            message("I now also need hBN flakes. Preserve the graphene chip and keep chip_001 "
                    "untouched. On chip_003 only, obtain the hBN recipe, exfoliate and search, "
                    "save the images and return it to storage. This is still standalone "
                    "material preparation, not stacking. Update the Task Plan first.", "plan_change")
            await turn()
            require_prepared_material(s, 'chip_003', 'hBN')
            message("That is enough preparation. Do not move or process any more samples. "
                    "Review the two materials we saved: give their chip IDs, storage locations "
                    "and candidate flake IDs with microscope images. Distinguish measured "
                    "observations from uncertain thickness estimates. Leave everything for "
                    "my next experiment; do not invent a completed device.")
            await turn()
    except Exception as exc:
        error = scrub_text(str(exc))[:2000]
    finally:
        if fault_monitor:
            fault_monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await fault_monitor
        await s.runtime.close()
        if pending and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        if rec:
            rec.stop()
        if s.supervisor:
            s.supervisor.stop()

    events = [e.model_dump() for e in s.store.events_after(0, limit=1000000)]
    checks, evidence = audit_story(args.case, events, s.store.list_runs(),
                                   s.store.list_samples(), operator_inputs)
    source_segments = [{'source_sha256': execution_source, 'after_event_id': first_event,
                        'through_event_id': events[-1]['id']}]
    if resuming:
        prior_segments = previous_meta.get('source_segments') or [{
            'source_sha256': previous_meta['execution_source_sha256'],
            'after_event_id': 0, 'through_event_id': first_event}]
        source_segments = prior_segments + source_segments
        if args.resume:
            del checks['model_turns_completed']
        new_turns = [e for e in events if e['kind'] == 'agent.turn_end' and e['id'] > first_event]
        if args.resume:
            checks['previous_stop_was_budget_only'] = all(
                e['payload'].get('reason') == 'budget_exhausted' for e in events
                if e['kind'] == 'agent.turn_end' and e['id'] <= first_event and e['payload'].get('is_error'))
        else:
            checks['previous_stop_was_plan_admission'] = True  # strict validator above, before any append
        checks['resumed_turn_completed'] = bool(new_turns) and all(
            not e['payload'].get('is_error') for e in new_turns)
        checks['resumed_model_requests'] = any(e['kind'] == 'agent.request' and e['id'] > first_event for e in events)
        checks['original_events_preserved'] = prefix_rows == [tuple(r) for r in s.store._x(
            'SELECT * FROM event WHERE id<=? ORDER BY id', (first_event,))]
    checks["driver_completed"] = error is None
    checks["application_source_unchanged"] = execution_source == source_fingerprint()
    checks["all_operator_inputs_delivered"] = not s.store.pending_instructions() and not any(
        e["kind"] == "user.dropped" for e in events)
    if args.case == "reactor":
        from qumus2.records.plan_completion import unmet_reactor_outcome

        checks["cold_storage_verified"] = not unmet_reactor_outcome(s.store, bench, {
            "chip_id": "reactor_chip_01", "disposition": "cold_storage", "temperature_target_c": None,
            "flow_target_sccm": 0, "rationale": "Acceptance requires a cold stored product."},
            project_id=s.project_id)
    project = s.store.get_project(s.project_id) or {}
    physical_steps = [step for step in project.get("plan", []) if step.get("owner") in ("hands", "reactor")]
    checks["physical_plan_closed"] = bool(physical_steps) and all(
        step.get("status") in ("done", "skipped") for step in physical_steps)
    checks['task_plan_closed'] = bool(project.get('plan')) and all(
        step.get('status') in ('done', 'skipped') for step in project['plan'])
    story = STORIES[args.case]
    export = export_recording(folder / "qumus.db", folder / "export", title=story["title"],
        description=story["purpose"] + (" Continued in the same saved workspace after a visible "
                    + ("plan-admission failure and its repair" if args.resume_repair else "budget stop")
                    + "; earlier events and spending are retained. " if resuming else " ") +
                    "Live model decisions on a simulated bench. "
                    "Operator inputs are scripted acceptance inputs. This is not physical "
                    "hardware or calibrated material-performance evidence.",
        meta={"provider": "openai", "model": s.runtime.creds.model_id,
              "execution_source_sha256": execution_source,
              "source_segments": source_segments, "resumed_after_budget_stop": bool(args.resume),
              "resumed_after_plan_repair": bool(args.resume_repair),
              "run_budget_usd": min(args.budget, s.runtime.creds.test_budget_usd),
              "seed": args.seed, "purpose": story["purpose"],
              "simulation_profile": "integrated",
              "material_profile": "material-morphology.yaml" if args.morphology else "default",
              "operator_inputs": "scripted acceptance inputs", "cost_basis": "configured_token_rates",
              "acceptance_checks": checks, "acceptance_evidence": evidence})
    checks["media_complete"] = not check_recording(export)
    passed = all(checks.values())
    summary = {"case": args.case, "passed": passed, "error": error, "checks": checks,
               "evidence": evidence, "operator_inputs": operator_inputs,
               "estimated_usd": s.store.spent_usd(), "cost_basis": "configured_token_rates",
               "jobs": [{"id": r["id"], "flow": r["flow"], "status": r["status"]} for r in s.store.list_runs()]}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    if passed:
        target = Path("recordings") / args.name
        if target.exists():
            raise FileExistsError(f"Recording already exists: {target}")
        export.rename(target)
        print("Accepted recording:", target, flush=True)
    s.store.close()
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=STORIES)
    parser.add_argument("--live", action="store_true")
    continuation = parser.add_mutually_exclusive_group()
    continuation.add_argument("--resume", action="store_true", help="Continue the same saved reactor run after a budget-only stop")
    continuation.add_argument("--resume-repair", action="store_true",
                              help="Continue the saved collaboration preparation after a recorded plan-admission repair")
    parser.add_argument("--name", required=True)
    parser.add_argument("--budget", type=float, default=40)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--morphology", action="store_true",
                        help="exercise the explicit clustered material morphology profile")
    parser.add_argument("--timeout", type=float, default=1500)
    args = parser.parse_args()
    if not args.live or not math.isfinite(args.budget) or not 0 < args.budget <= 40:
        parser.error("Paid recording requires --live and a budget in (0, 40] USD.")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    if Path(args.name).name != args.name or args.name.startswith("."):
        parser.error("Use a simple recording folder name.")
    if (Path("recordings") / args.name).exists():
        parser.error("The recording already exists; never overwrite historical evidence.")
    raise SystemExit(0 if asyncio.run(record(args)) else 1)


if __name__ == "__main__":
    main()
