"""Offline diagnostic of ordinary material-metal device simulation.

Scripted agent decisions, ordinary noise and source preparation, guarded physical
simulation, explicitly uncalibrated progress. No model API or real hardware.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from record_reactor_review import review_plan  # noqa: E402

import qumus2.config as configuration  # noqa: E402
from qumus2.app import build_services  # noqa: E402
from qumus2.config import simulation_profile  # noqa: E402
from qumus2.records.geometry import current_pickup_evidence, ranked_pickup_combinations  # noqa: E402
from qumus2.records.material_metal_simulation import SIM_PROTOCOL, MaterialMetalSimulation  # noqa: E402
from qumus2.ui.recording import (  # noqa: E402
    attach_recorder,
    check_recording,
    export_recording,
    source_fingerprint,
)

CHIP = "reactor_chip_01"
JOB_WAIT_WALL_S = 900


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _material_assumptions(values, base=None):
    complete = deepcopy(base) if base is not None else MaterialMetalSimulation().model_dump(mode="json")
    for key, value in values.items():
        if key in ("PC", "mica"):
            complete[key].update(value)
        else:
            complete[key] = value
    return MaterialMetalSimulation.model_validate(complete).model_dump(mode="json")


def diagnostic_configuration(simulation_condition="standard", overrides=None, *, seed=None):
    """Consume the central condition; derive its changes without another setting map."""
    if simulation_condition not in ("standard", "ideal_stacking_v1"):
        raise ValueError(f"Unknown simulation condition: {simulation_condition}")
    standard = simulation_profile()
    cfg = configuration.ideal_stacking_profile() if simulation_condition == "ideal_stacking_v1" else deepcopy(standard)
    configured_seed = cfg.get("seed")
    if cfg.get("simulation_condition", "standard") != simulation_condition:
        raise ValueError("Central simulation condition marker does not match the requested condition")

    def normalized(value):
        value = deepcopy(value)
        reactor = value.setdefault("reactor", {})
        reactor["material_metal"] = _material_assumptions(reactor.get("material_metal", {}))
        return value

    def changes(before, after, prefix=()):
        rows = []
        for key in sorted(before.keys() | after.keys()):
            path = (*prefix, key)
            if key in before and key in after and isinstance(before[key], dict) and isinstance(after[key], dict):
                rows.extend(changes(before[key], after[key], path))
            elif key not in before or key not in after or before[key] != after[key]:
                kind = "changed" if key in before and key in after else "added" if key in after else "removed"
                rows.append({"path": list(path), "kind": "condition_marker" if path == ("simulation_condition",) else kind,
                             "before_present": key in before, "after_present": key in after,
                             "before": before.get(key), "after": after.get(key)})
        return rows

    central = normalized(cfg)
    central_changes = changes(normalized(standard), central)
    if overrides is not None:
        cfg.setdefault("reactor", {})["material_metal"] = _material_assumptions(
            overrides, central["reactor"]["material_metal"])
    if seed is not None:
        cfg["seed"] = seed
    effective = normalized(cfg)
    if simulation_condition == "ideal_stacking_v1":
        for change in central_changes:
            value = effective
            for key in change["path"]:
                value = value[key]
            if value != change["after"]:
                raise ValueError(f"Override conflicts with central ideal condition: {'.'.join(change['path'])}")
        if effective["reactor"]["material_metal"]["PC"] != central["reactor"]["material_metal"]["PC"] \
                or effective["reactor"]["material_metal"]["mica"] != central["reactor"]["material_metal"]["mica"]:
            raise ValueError("Overrides cannot change the ideal condition's transfer recipes or support assumptions")
    conditions = {"schema_version": 1, "simulation_condition": simulation_condition,
                  "configured_seed": configured_seed, "effective_seed": cfg.get("seed"),
                  "constructor_seed_override": seed,
                  "scope": "Explicit simulator conditions; no laboratory calibration or experimental acceptance",
                  "effective_configuration": deepcopy(cfg), "normalized_effective_configuration": effective,
                  "configuration_sha256": hashlib.sha256(_json_bytes(cfg)).hexdigest(),
                  "central_condition_changes": central_changes,
                  "offlist": [row for row in central_changes if row["kind"] != "condition_marker"],
                  "declared_material_overrides": deepcopy(overrides),
                  "effective_changes_from_standard": changes(normalized(standard), effective)}
    return cfg, conditions


def save_private_evaluation(s, folder, source, conditions, *, recorder_final_read_completed,
                            capture_phase="after_recorder_final_read"):
    """Best-effort private copy only; never a control input or a success criterion."""
    payload = {"schema_version": 1, "scope": "private_simulation_evaluation_only", "verification": False,
               "execution_source_sha256": source, "simulation_condition": conditions["simulation_condition"],
               "configured_seed": conditions["configured_seed"], "effective_seed": conditions["effective_seed"],
               "configuration_sha256": conditions["configuration_sha256"],
               "recorder_final_read_completed": recorder_final_read_completed,
               "capture_phase": capture_phase, "diagnostics": []}

    def binding():
        runs = [{key: run[key] for key in ("id", "flow", "status")} for run in s.store.list_runs()]
        return {"clock_time": s.clock.now(), "clock_basis": getattr(s.clock, "event_clock_basis", None),
                "clock_domain": getattr(s.clock, "event_clock_domain", None),
                "event_prefix_id": s.store.last_event_id(), "runs": runs,
                "unfinished_or_unsuccessful_run_ids": [run["id"] for run in runs if run["status"] != "succeeded"]}

    def capture_source():
        return {"application_sha256": source_fingerprint(),
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    for key, reader in (("capture_source", capture_source), ("binding_before", binding), ("state", lambda: s.lab.world.evaluation_snapshot()),
                        ("binding_after", binding)):
        try:
            payload[key] = reader()
        except Exception as exc:
            payload["diagnostics"].append({"stage": key, "type": type(exc).__name__, "message": str(exc)})
    payload["capture_status"] = "captured" if not payload["diagnostics"] else "incomplete"
    if "capture_source" in payload:
        payload["application_source_matches_start"] = payload["capture_source"]["application_sha256"] == source
    payload["atomic_terminal_claim"] = False
    if "binding_before" in payload and "binding_after" in payload:
        payload["binding_changed"] = payload["binding_before"] != payload["binding_after"]
        if (payload["binding_before"]["unfinished_or_unsuccessful_run_ids"]
                or payload["binding_after"]["unfinished_or_unsuccessful_run_ids"]):
            payload["job_state_classification"] = "unfinished_or_unsuccessful"
        elif payload["binding_before"]["runs"] or payload["binding_after"]["runs"]:
            payload["job_state_classification"] = "all_jobs_succeeded"
        else:
            payload["job_state_classification"] = "no_jobs"
    try:
        try:
            encoded = _json_bytes(payload)
        except Exception as exc:
            payload.pop("state", None)
            payload["capture_status"] = "serialization_failed"
            payload["diagnostics"].append({"stage": "serialization", "type": type(exc).__name__, "message": str(exc)})
            encoded = _json_bytes(payload)
        (folder / "simulation-evaluation.json").write_bytes(encoded)
    except Exception as exc:
        return {"status": "write_failed", "type": type(exc).__name__, "message": str(exc)}
    return {"status": payload["capture_status"], "diagnostics": payload["diagnostics"]}


def record_admitted_transfer(s, folder, conditions, job_id):
    """Copy admitted assumptions, refusing an ideal label over different actual laws."""
    run = s.store.get_run(job_id)
    params = run["params"]
    expected = conditions["normalized_effective_configuration"]["reactor"]["material_metal"]
    if run["flow"] == "prepare_material_metal":
        field = "receipt_inputs.model_assumptions"
        actual = params.get("receipt_inputs", {}).get("model_assumptions")
    elif run["flow"] == "stack_device" and params.get("stamp_film") == "mica":
        field = "support_assumptions"
        actual, expected = params.get("support_assumptions"), expected["mica"]
    else:
        return
    matches = actual == expected
    mismatch = conditions["simulation_condition"] == "ideal_stacking_v1" and not matches
    if mismatch:
        s.jobs.stop_all()
    conditions.setdefault("admitted_transfer_assumptions", []).append({
        "job_id": job_id, "flow": run["flow"], "status": run["status"], "field": field,
        "params_sha256": hashlib.sha256(_json_bytes(params)).hexdigest(),
        "actual": deepcopy(actual), "matches_declared_configuration": matches})
    (folder / "simulation-conditions.json").write_bytes(_json_bytes(conditions))
    if mismatch:
        raise ValueError(f"Admitted {field} for {job_id} conflicts with the ideal condition; Stop requested")


async def wait_for_job(call, jobs, role, job_id, deadline, *, recovery=None):
    """Poll one job within one wall deadline, including its existing recovery budget."""
    def remaining(*, new_wait=False):
        budget = deadline - monotonic()
        # Only a new wait_job call needs its public minimum of one second.
        if budget <= 0 or (new_wait and budget < 1):
            jobs.stop_all()
            status = jobs.status(job_id).status
            raise TimeoutError(f"job {job_id} reached the diagnostic wall deadline; "
                               f"Stop requested; latest public status={status}; "
                               "completion, cold state and safe terminal state are unverified")
        return budget

    recoveries = 0
    while True:
        result = await call(role, "wait_job", {"job_id": job_id, "timeout_s": min(120, remaining(new_wait=True))}, None)
        remaining()
        if result.status == "pending":
            continue
        if result.data.get("run_id") != job_id or result.data.get("done") is not True:
            raise RuntimeError(f"wait_job did not establish a terminal state for {job_id}: {result.summary}")
        if result.status in ("ok", "corrected") or recovery is None or recoveries == 2:
            return result
        await call("eyes", "inspect_bench")
        remaining()
        extra = {"action": "retry"} if recovery == "recover_material_metal_job" else {}
        await call(role, recovery, {"job_id": job_id,
            "reason": "Bounded recovery after fresh bench inspection", **extra}, "pending")
        recoveries += 1


async def record(folder: Path, film="mica", overrides=None, reaction_args=None, seed=7, expected_success=True, after_preparation=None, before_preparation=None, simulation_condition="standard"):
    cfg, conditions = diagnostic_configuration(simulation_condition, overrides, seed=seed)
    folder.mkdir(parents=True, exist_ok=False)
    source = source_fingerprint()
    (folder / "simulation-conditions.json").write_bytes(_json_bytes(conditions))
    condition_label = "ideal stacking simulator" if simulation_condition == "ideal_stacking_v1" else "ordinary simulator"
    s = build_services(folder / "qumus.db", testbench_config=cfg, seed=seed, use_openai=False)
    recorder = attach_recorder(s)
    checks, error = {}, None
    evaluation_capture = {"status": "not_attempted"}

    async def call(role, tool, args=None, expected="ok"):
        result = (await s.gateway.call(role, tool, args or {}))[0]
        if result.status == "pending" and tool in (
                "stack_device", "prepare_material_metal", "recover_material_metal_job", "retry_job"):
            record_admitted_transfer(s, folder, conditions, result.data["job_id"])
        if expected and result.status != expected:
            raise RuntimeError(f"{tool}: {result.summary}")
        return result

    async def execute(role, tool, args):
        deadline = monotonic() + JOB_WAIT_WALL_S
        job_id = (await call(role, tool, args, "pending")).data["job_id"]
        recovery = "recover_material_metal_job" if tool in ("stack_device", "prepare_material_metal", "reactor_material_metal") else "retry_job"
        result = await wait_for_job(call, s.jobs, role, job_id, deadline, recovery=recovery)
        if result.status not in ("ok", "corrected"):
            raise RuntimeError(result.summary)
        return job_id

    def start(role, sid, task, objective=None):
        decision = s.gateway.delegate(role, task=f"{sid}: {task}", require_sample_outcomes=True, objective=objective)
        if not decision.allow:
            raise RuntimeError(decision.reason)
        s.gateway.delegation_started(sid, role, f"{sid}: {task}")
        s.store.emit("agent.delegate", {"to": role, "task": f"{sid}: {task} (scripted)"}, agent="orchestrator")

    try:
        s.store.emit("notification", {"level": "info", "text": "SCRIPTED SOFTWARE REVIEW. "
            f"Source preparation, microscopy, transport, pickup, release and Reactor execution run on the {condition_label}. "
            "Material-metal interface progress uses recorded qualitative assumptions, not calibrated chemistry."})
        iid = s.store.add_instruction("SCRIPTED operator fixture: use Gr and Pd with top nonreactive hBN; preserve source images and provenance.", project_id=s.project_id)
        req = (await call("orchestrator", "establish_reactor_requirements", {"instruction_id": iid, "reactive_material": "Gr", "metal": "Pd"})).data
        stamp = s.store.add_instruction(f"Known support: stamp_01 carries {film}.", project_id=s.project_id)
        await call("orchestrator", "record_stamp_support", {"stamp_id": "stamp_01", "film_type": film, "instruction_id": stamp})
        coating = s.store.add_instruction(f"SCRIPTED preparation input: {CHIP} has 20 nm Pd on silicon, preparation synthetic-pd-01 by synthetic-operator.", project_id=s.project_id)
        await call("orchestrator", "record_metal_coating", {"chip_id": CHIP, "metal": "Pd", "thickness_nm": 20, "preparation_id": "synthetic-pd-01",
            "prepared_by": "synthetic-operator", "instruction_id": coating})
        steps = review_plan()
        recipes = {}
        for sid, material, chip, temperature in (("s2", "Gr", "chip_001", 50), ("s4", "hBN", "chip_002", 90)):
            recipes[sid] = {"material": material, "chip_ids": [chip], "target_temp_c": temperature, "dwell_s": 30,
                "num_massages": 2, "press_depth_mm": 0.2, "peel_speed": 200, "peel_angle_deg": 30, "purpose": "secure"}
            steps[int(sid[1:])-1]["operation_args"] = recipes[sid]
        steps[8].update(title="Assemble hBN/Gr on Pd/Si with checked simulated device motion",
                        operation="prepare_material_metal", sample_outcomes=[{"sample_id": CHIP, "in_storage": True}])
        steps[9].update(title="Observe qualitative material-metal simulation and safely return carrier", operation="reactor_material_metal",
            reactor_outcome={"chip_id": CHIP, "disposition": "cold_storage", "temperature_target_c": None, "flow_target_sccm": 0,
                "rationale": "Turn targets off, cool and return the simulated carrier to storage"})
        # Initial search now retains neighbourhood evidence. This diagnostic
        # exercises its reuse, without an unconditional second assessment job.
        steps = [step for step in steps if step["id"] != "s7"]
        await call("orchestrator", "update_plan", {"plan": steps, "note": "Ordinary material-metal device simulation with independent evidence and acceptance", "task_kind": "experiment"})
        start("eyes", "s1", "Inspect the ordinary simulated bench")
        await call("eyes", "inspect_bench")
        await call("eyes", "get_instrument_status")
        s.gateway.delegation_finished("s1")
        for sid, material, chip in (("s2", "Gr", "chip_001"), ("s4", "hBN", "chip_002")):
            await call("advisor", "get_experience", {"material": material})
            start("hands", sid, f"Exfoliate {material} using the complete simulation recipe")
            await execute("hands", "exfoliate_chips", recipes[sid])
            s.gateway.delegation_finished(sid)
            search_sid = f"s{int(sid[1:])+1}"
            start("hands", search_sid, f"Search and photograph {material} on {chip}")
            await execute("hands", "search_chip", {"chip_id": chip, "material": material, "min_area_um2": 50, "max_revisits": 8})
            s.gateway.delegation_finished(search_sid)
        candidates = (await call("picker", "get_reactor_pair_candidates")).data["candidates"]
        ids = [row["flake_id"] for row in candidates if row["eligible_for_assessment"]][:20]
        start("picker", "s6", "Screen photographed hBN and Gr candidates with backups")
        await call("picker", "submit_reactor_selection", {"chip_id": CHIP, "flake_ids": ids, "phase": "screen", "rationale": "Screen actual simulated catalogue images for both required materials"})
        s.gateway.delegation_finished("s6")
        rows = (await call("picker", "get_reactor_pair_candidates")).data["candidates"]
        ranked = ranked_pickup_combinations(s.store, [
            [r["flake_id"] for r in rows if r["material"] == material and r["pickup_clear"]]
            for material in ("hBN", "Gr")])
        if not ranked:
            raise RuntimeError("No observed isolated hBN/Gr combination satisfies full pickup coverage")
        chosen = ranked[0]["flake_ids"]
        s.store.emit("scripted.selection", {"method": "maximum minimum observed edge clearance",
            "ranked_pairs": ranked, "chosen": chosen, "units": "mm",
            "scope": "Nominal scripted preference; actual pickup requires fresh optical containment"}, agent="picker")
        checks["initial_search_evidence_reused"] = all(
            (evidence := current_pickup_evidence(s.store, s.store.get_sample(fid))) is not None
            and s.store.get_run(evidence["job_id"])["flow"] == "search_chip"
            for fid in chosen)
        start("picker", "s8", "Confirm top hBN and reactive Gr from returned local evidence")
        assessments = []
        for fid in chosen:
            await call("picker", "view_flake_images", {"flake_id": fid})
            item = {"flake_id": fid, "feasible": True, "reason": "SCRIPTED synthetic optical fixture with measured complete containment",
                "geometry_fitness": 80, "visible_cleanliness": 80, "continuity_uniformity": 80,
                "evidence_confidence": 80, "inventory_tiebreaker": 50, "overall_score": 80,
                "surface_residue": "clean", "residue_evidence": "SCRIPTED synthetic clean-region fixture; not a model judgement"}
            if s.store.get_sample(fid)["props"]["material"] == "hBN":
                item["hbn_color_judgement"] = {"relative_body_color": "synthetic", "background_comparison": "synthetic", "thick_edge_visible": False,
                    "classification": "usable_color_family", "evidence": "SCRIPTED optical fixture only"}
            assessments.append(item)
        await call("picker", "submit_reactor_selection", {"chip_id": CHIP, "flake_ids": chosen, "phase": "confirm", "assessments": assessments, "rationale": "Both required materials have successful local pickup-neighbourhood evidence"})
        s.gateway.delegation_finished("s8")
        source_ids = {fid: s.store.get_sample(fid)["location"] for fid in chosen}
        sim_ids = [s.store.get_sample(fid)["props"]["sim_id"] for fid in chosen]
        if before_preparation is not None:
            before_preparation(s)
        start("hands", "s9", "Pick up hBN then Gr and release onto reported Pd/Si")
        prepared = await execute("hands", "stack_device", {"target_chip_id": CHIP, "flake_ids": chosen, "stamp_id": "stamp_01"})
        s.gateway.delegation_finished("s9")
        if after_preparation is not None:
            after_preparation(s)
        objective = {"chip_id": CHIP, "requirements_event_id": req["event_id"],
                     "objective": "Meet the recorded qualitative interface-progress criterion in device simulation; preserve hBN and return cold",
                     "final_disposition": "cold_storage"}
        start("reactor", "s10", "Run the qualitative material-metal simulation and safe storage sequence", objective)
        observed = await execute("reactor", "reactor_material_metal", {"chip_id": CHIP, **(reaction_args or {})})
        result = await call("reactor", "submit_material_metal_result", {"chip_id": CHIP, "reaction_job_id": observed}, None)
        s.gateway.delegation_finished("s10")
        checks.update(device_jobs_succeeded=all(s.store.get_run(job)["status"] == "succeeded" for job in (prepared, observed)),
            exact_physical_layers={f.id for f in s.lab.world.objects[CHIP].flakes} == set(sim_ids),
            sources_relocated=all(s.store.get_sample(fid)["location"] == CHIP and s.store.get_sample(fid)["status"] == "stacked" for fid in chosen),
            physical_sources_removed=all(sid not in {f.id for f in s.lab.world.objects[source_ids[fid]].flakes} for fid, sid in zip(chosen, sim_ids, strict=True)),
            known_film_retained=s.store.get_sample(CHIP)["props"]["material_metal_preparation"]["stamp_film"] == film,
            carrier_stored=s.lab.world.slot_of(s.lab.world.objects[CHIP]) == s.store.get_sample(CHIP)["location"] == "reactor_chip_slot_1",
            simulation_goal_matches_expectation=(result.status == "ok" and result.data.get("simulation_accepted") is True) == expected_success,
            no_chemical_claim=result.data.get("experiment_accepted") is False if result.status == "ok" else True,
            hbn_not_consumed=not s.store.get_sample(chosen[0])["props"].get("reactor_consumed_by"),
            no_invented_product=not any(row["status"] == "reactor_product" for row in s.store.list_samples()),
            plan_matches_outcome=all(step["status"] == "done" for step in s.store.get_project(s.project_id)["plan"]) == expected_success)
        final_args = {"status": "done", "summary": "Qualitative device simulation met its observed goal and cold-storage disposition. Real chemistry is not verified."}
        final = await call("orchestrator", "save_project_status", final_args, None)
        if final.status == "pending":
            cooled = await wait_for_job(call, s.jobs, "orchestrator", final.data["job_id"],
                                       monotonic() + JOB_WAIT_WALL_S)
            if cooled.status in ("ok", "corrected"):
                final = await call("orchestrator", "save_project_status", final_args, None)
        checks["simulation_task_matches_outcome"] = (final.status == "ok") == expected_success
        s.store.emit("agent.message", {"text": f"Explicit {condition_label} Gr/hBN preparation, source screening and local checks, "
            "hBN-first pickup and Gr pickup, release onto reported Pd/Si, reactor loading, qualitative interface observation "
            "and safe recovery ran through the guarded device chain. Both input flakes remain on the carrier. "
            "Simulation target acceptance is independent of real chemical validation."}, agent="orchestrator")
    except Exception as exc:
        error = str(exc)
    finally:
        try:
            await s.runtime.close()
        finally:
            final_read_completed = False
            try:
                recorder.stop()
                final_read_completed = True
            finally:
                try:
                    evaluation_capture = save_private_evaluation(s, folder, source, conditions,
                        recorder_final_read_completed=final_read_completed)
                except Exception as exc:
                    evaluation_capture = {"status": "capture_failed", "type": type(exc).__name__, "message": str(exc)}
    events = s.store.events_after(0, limit=100000)
    checks["no_api_calls"] = not any(event.kind == "agent.usage" for event in events)
    checks["no_safety_violation"] = not any(event.kind == "safety.violation" for event in events)
    checks["source_unchanged"] = source == source_fingerprint()
    export = export_recording(folder / "qumus.db", folder / "export", title="Verify guarded material-metal device simulation",
        description=f"Explicit {condition_label} source preparation, local optics, device transfer and Reactor processing. Uncalibrated qualitative interface model; no real chemical validation.",
        layout=s.lab.bench, meta={"provider": "scripted", "source": "qualitative_device_simulation",
            "experimental": True, "execution_source_sha256": source, "purpose": "Guarded material-metal simulation diagnostics",
            "acceptance_checks": checks, "physical_transfer_verified": False, "chemistry_verified": False,
            "protocol_id": SIM_PROTOCOL, "simulation_condition": simulation_condition,
            "simulation_conditions": conditions})
    checks["media_complete"] = not check_recording(export)
    summary = {"passed": not error and all(checks.values()), "checks": checks, "error": error, "api_calls": 0,
        "simulation_condition": simulation_condition, "configuration_sha256": conditions["configuration_sha256"],
        "private_evaluation_capture": evaluation_capture,
        "scope": "Guarded device simulation with uncalibrated material-metal progress; no real hardware or chemical acceptance"}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary, indent=2))
    return summary["passed"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--film", choices=("PC", "mica"), default="mica")
    parser.add_argument("--seed", type=int, default=8)
    parser.add_argument("--expect-unmet", action="store_true")
    parser.add_argument("--simulation-condition", choices=("standard", "ideal_stacking_v1"), default="standard")
    args = parser.parse_args(argv)
    return 0 if asyncio.run(record(args.out, args.film, seed=args.seed,
        expected_success=not args.expect_unmet, simulation_condition=args.simulation_condition)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
