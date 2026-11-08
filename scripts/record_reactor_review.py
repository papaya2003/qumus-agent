"""Focused offline evidence: immutable Gr/Pd requirements and one tape application.

No chemical product, mica release, completed hBN assembly or model decisions are
claimed. All dialogue is scripted; the heated exfoliation job is actually run
through ToolGateway, JobService and SafetyGate on the ordinary simulated bench.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qumus2.app import build_services  # noqa: E402
from qumus2.config import simulation_profile  # noqa: E402
from qumus2.ui.recording import (  # noqa: E402
    attach_recorder,
    check_recording,
    export_recording,
    source_fingerprint,
)


def review_plan():
    steps = [{"id": "s1", "owner": "eyes", "title": "Inspect and reconcile ordinary simulated stock"}]
    for mat, chip in (("Gr", "chip_001"), ("hBN", "chip_002")):
        for op in ("exfoliate_chips", "search_chip"):
            steps.append({"id": f"s{len(steps)+1}", "owner": "hands", "title": f"{op}: {mat} on {chip}",
                "operation": op, "materials": [mat], "sample_outcomes": [{"sample_id": chip, "in_storage": True}]})
    return [*steps,
        {"id": "s6", "title": "Picker screens photographed inputs and backups", "owner": "picker", "experiment_stage": "screen"},
        {"id": "s7", "title": "Hands checks screened neighbourhoods in source-chip batches", "owner": "hands", "operation": "assess_reactor_inputs"},
        {"id": "s8", "title": "Picker confirms top hBN and reactive Gr", "owner": "picker", "experiment_stage": "confirm"},
        {"id": "s9", "title": "Hands assembles on prepared Pd/silicon after release-route validation", "owner": "hands",
         "operation": "prepare_reactor_pair", "materials": ["hBN", "Gr"]},
        {"id": "s10", "title": "Reactor handles Gr/Pd process after adapter validation", "owner": "reactor", "operation": "reactor_reaction"}]


async def record(folder: Path):
    folder.mkdir(parents=True, exist_ok=False)
    source = source_fingerprint()
    s = build_services(folder / "qumus.db", testbench_config=simulation_profile(), seed=7, use_openai=False)
    recorder = attach_recorder(s)
    checks = {}
    error = None

    async def call(role, name, args=None):
        return (await s.gateway.call(role, name, args or {}))[0]

    def expect(result, status="ok"):
        if result.status != status:
            raise RuntimeError(result.summary)
        return result

    try:
        s.store.emit("notification", {"level": "info", "text": "SCRIPTED REVIEW FIXTURE. No API or physical hardware. "
            "Demonstrates protected Gr/Pd requirements and single heated tape application; not mica/Pd release or reaction acceptance."})
        iid = s.store.add_instruction("SCRIPTED operator fixture: use Gr and Pd with top nonreactive hBN. "
            "Retain hBN/Gr/Pd/silicon, saved images and source provenance. Validate workflow guards; do not claim a chemical result.", project_id=s.project_id)
        req = expect(await call("orchestrator", "establish_reactor_requirements", {"instruction_id": iid,
            "reactive_material": "Gr", "metal": "Pd"})).data
        steps = review_plan()
        expect(await call("orchestrator", "update_plan", {"plan": steps, "note": "Scripted review plan with all obligations", "task_kind": "experiment"}))
        s.gateway.delegation_started("eyes", "eyes", "s1: inspect")
        s.store.emit("agent.delegate", {"to": "eyes", "task": "s1: inspect (scripted)"}, agent="orchestrator")
        expect(await call("eyes", "inspect_bench"))
        expect(await call("eyes", "get_instrument_status"))
        s.gateway.delegation_finished("eyes")
        # Reject scheme drift, then merge current progress via structural revision.
        drift = deepcopy(steps)
        drift[3]["materials"] = drift[4]["materials"] = ["Gr"]
        version = s.store.get_project(s.project_id)["plan_version"]
        refused = await call("orchestrator", "update_plan", {"plan": drift, "expected_version": version, "note": "Adversarial synthetic retry: drop hBN"})
        checks["scheme_drift_rejected"] = refused.status == "error" and refused.data.get("error") == "experiment_requirements"
        expect(await call("advisor", "get_experience", {"material": "Gr"}))
        missing = s.gateway.delegate("hands", task="s2: Exfoliate Gr", require_sample_outcomes=True)
        checks["missing_recipe_refused_before_execution"] = not missing.allow and "recipe" in missing.reason
        recipe = {"material": "Gr", "chip_ids": ["chip_001"], "target_temp_c": 50, "dwell_s": 30,
                  "num_massages": 2, "press_depth_mm": 0.2, "peel_speed": 200, "peel_angle_deg": 30, "purpose": "secure"}
        s.store.emit("agent.message", {"text": "Scripted Advisor fixture supplies a complete unvalidated simulation recipe; no physical calibration claim."}, agent="advisor")
        refined = expect(await call("orchestrator", "refine_plan", {"expected_revision": 1, "updates": [
            {"step_id": "s2", "fields": {"operation_args": recipe}}], "note": "Complete all recipe gaps together and retain latest Eyes progress"}))
        checks["progress_merged"] = refined.data["plan"][0]["status"] == "done"
        allowed = s.gateway.delegate("hands", task="s2: Exfoliate Gr with the complete recipe", require_sample_outcomes=True)
        if not allowed.allow:
            raise RuntimeError(allowed.reason)
        s.gateway.delegation_started("hands", "hands", "s2: Exfoliate Gr")
        s.store.emit("agent.delegate", {"to": "hands", "task": "s2: Exfoliate Gr with the complete agreed simulation recipe (scripted)"}, agent="orchestrator")
        sub = expect(await call("hands", "exfoliate_chips", recipe), "pending")
        rid = sub.data["job_id"]
        result = await call("hands", "wait_job", {"job_id": rid, "timeout_s": 120})
        if result.status not in ("ok", "corrected"):
            raise RuntimeError(result.summary)
        s.gateway.delegation_finished("hands")
        active = None
        applications = []
        for e in s.store.events_after(0, limit=100000):
            if e.run_id != rid:
                continue
            if e.kind == "step.started":
                active = e.payload["name"]
            if e.kind == "device.command" and e.payload["device"] == "exfoliator" and e.payload["command"] in ("apply", "peel"):
                applications.append([active, e.payload["command"], e.id])
        on_chip = [row[:2] for row in applications if row[0] in ("press_tape", "heat_applied_tape", "cool_and_peel")]
        checks["single_application"] = on_chip == [["press_tape", "apply"], ["cool_and_peel", "peel"]]
        checks["massage_count_preserved"] = s.store.get_run(rid)["params"]["num_massages"] == 2
        checks["source_stored"] = s.store.get_sample("chip_001")["location"].startswith("mid_chip_")
        checks["no_false_acceptance"] = (await call("orchestrator", "save_project_status", {"status": "done", "summary": "Adversarial premature completion"})).status == "error"
        expect(await call("orchestrator", "save_project_status", {"status": "blocked", "summary":
            "Focused regression complete: one heated tape application, preserved Gr/Pd and hBN obligations. "
            "hBN preparation/search, mica release onto Pd and material-metal reaction remain unexecuted and unaccepted."}))
        s.store.emit("agent.message", {"text": "Scripted review result: requirement drift and incomplete recipe delegation were rejected. "
            "The actual simulated Gr chip received one tape application, retained two massages, and returned to storage. "
            "Remaining plan steps are deliberately pending. No Gr/Pd product, hBN assembly or mica/Pd release was validated."}, agent="orchestrator")
        checks["requirements_retained"] = req["requirements"]["protective_role"] == "nonreactive_pickup_and_protection"
    except Exception as exc:
        error = str(exc)
    finally:
        await s.runtime.close()
        recorder.stop()
    events = s.store.events_after(0, limit=100000)
    checks["no_safety_violation"] = not any(e.kind == "safety.violation" for e in events)
    checks["no_paid_calls"] = not any(e.kind == "agent.usage" for e in events)
    checks["source_unchanged"] = source == source_fingerprint()
    output = export_recording(folder / "qumus.db", folder / "export", title="Protect the experiment; apply tape once",
        description="Offline scripted review regression on ordinary simulated stock. Protected hBN/Gr/Pd/silicon requirements and single heated tape application. "
            "No mica release, material-metal reaction, chemical product or live model acceptance claim.", layout=s.lab.bench,
        meta={"provider": "scripted", "source": "software_simulation", "experimental": True,
              "execution_source_sha256": source, "purpose": "Owner review R02-R07 workflow guards and single tape application",
              "experiment_kind": "material_metal_workflow_regression", "acceptance_checks": checks})
    checks["media_complete"] = not check_recording(output)
    summary = {"passed": not error and all(checks.values()), "checks": checks, "error": error, "api_calls": 0,
               "scope": "Workflow regression only; not completed material-metal experiment"}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return summary["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(record(args.out)) else 1)
