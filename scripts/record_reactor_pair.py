"""Offline two-flake reactor acceptance; never calls a paid model or hardware.

The normal case verifies preprepared pair load, fixed-optics XY observation,
one local product, cooling and recovery. The camera-loss case verifies targets
are disabled and uncertain exposure is parked without inventing a product.
Existing recordings are never overwritten. All inputs and decisions are fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qumus2.app import build_services  # noqa: E402
from qumus2.config import load_yaml  # noqa: E402
from qumus2.ui.recording import (  # noqa: E402
    attach_recorder,
    check_recording,
    export_recording,
    source_fingerprint,
)

CHIP = "reactor_chip_01"


def pair_config():
    cfg = load_yaml("testbench.yaml")
    cfg["reactor"] = {"enabled": True, "experiment_kind": "two_flake_reaction",
                      "synthetic_prepared_pairs": True}
    return cfg


async def record(folder: Path, scenario: str) -> bool:
    execution_source = source_fingerprint()
    folder.mkdir(parents=True, exist_ok=False)
    os.environ.pop("QUMUS2_MICROSCOPE_REPLAY_DIR", None)
    cfg = pair_config()
    s = build_services(folder / "qumus.db", testbench_config=cfg, seed=7, use_openai=False)
    recorder = attach_recorder(s)
    checks, error = {}, None

    async def call(agent, tool, args=None):
        result, _ = await s.gateway.call(agent, tool, args or {})
        return result

    async def job(tool, **params):
        result = await call("reactor", tool, params)
        if result.status != "pending":
            raise RuntimeError(result.summary)
        rid = result.data["job_id"]
        result = await call("reactor", "wait_job", {"job_id": rid, "timeout_s": 120})
        return rid, result

    async def successful_job(tool, **params):
        rid, result = await job(tool, **params)
        if result.status not in ("ok", "corrected"):
            raise RuntimeError(result.summary)
        return rid

    try:
        s.store.emit("user.message", {"kind": "message", "source": "scripted_fixture",
            "text": "Scripted acceptance input: inspect the bench, process the prepared two-flake "
                    "pair and verify one product before recovery." if scenario == "normal" else
                    "Scripted acceptance input: test camera loss during a prepared two-flake "
                    "reaction; stop safely and do not claim an unverified product."})
        s.store.emit("notification", {"level": "info", "text":
            "OFFLINE scripted acceptance. Two preprepared synthetic small flakes; initialization is "
            "not autonomous cleavage or stacking. Real ToolGateway, jobs and SafetyGate; no API calls."})
        await call("orchestrator", "update_plan", {"note": f"Two-flake fixture: {scenario}", "plan": [
            {"id": "s1", "title": "Inspect the virtual reactor bench", "owner": "eyes"},
            {"id": "s2", "title": "React two prepared small flakes, verify and return to cold storage", "owner": "reactor",
             "operation": "reactor_reaction", "sample_outcomes": [{"sample_id": CHIP, "in_storage": True}],
             "reactor_outcome": {"chip_id": CHIP, "disposition": "cold_storage", "temperature_target_c": None,
                                  "flow_target_sccm": 0, "rationale": "Finish in checked cold storage."}}]})
        s.gateway.delegation_started("eyes", "eyes", "s1: inspect")
        s.store.emit("agent.delegate", {"to": "eyes", "task": "s1: inspect (scripted fixture)"}, agent="orchestrator")
        result = await call("eyes", "inspect_bench")
        if result.status != "ok":
            raise RuntimeError(result.summary)
        s.gateway.delegation_finished("eyes")
        s.store.emit("agent.done", {"summary": "Scripted fixture: the bench inspection completed."}, agent="eyes")
        result = await call("orchestrator", "set_reactor_pair_goal", {"chip_id": CHIP})
        if result.status != "ok":
            raise RuntimeError(result.summary)
        s.gateway.delegation_started("reactor", "reactor", "s2: two-flake reaction")
        s.store.emit("agent.delegate", {"to": "reactor", "task": "s2: prepared pair acceptance (scripted fixture)"}, agent="orchestrator")
        await successful_job("load_reactor_chip", chip_id=CHIP)
        acquired = s.gate.stage("reactor_slot").get_pose()
        await successful_job("position_reactor_slot", chip_id=CHIP, x_mm=acquired.x + 0.015, y_mm=acquired.y + 0.01)
        await successful_job("observe_reactor", chip_id=CHIP)
        await successful_job("position_reactor_slot", chip_id=CHIP, x_mm=acquired.x, y_mm=acquired.y)
        if scenario == "camera-loss":
            scope = s.lab.devices.get("reactor_microscope")
            capture = scope.capture

            def fail_after_heating():
                if scope.plant.readings()[0] >= 700:
                    if not scope.offline:
                        s.store.emit("notification", {"level": "warning", "text":
                            "Injected fixture fault: reactor microscope goes offline after heating."})
                    scope.offline = True
                return capture()

            scope.capture = fail_after_heating
            rid, _ = await job("reactor_reaction", chip_id=CHIP, duration_s=80)
            result = await call("reactor", "submit_reactor_pair_result", {"chip_id": CHIP, "reaction_job_id": rid})
            checks["fault_parked"] = s.store.get_run(rid)["status"] in ("parked", "waiting_human")
            checks["fault_cannot_claim_product"] = result.status == "error"
            checks["uncertainty_retained"] = s.store.get_sample(CHIP)["props"].get("reactor_state_uncertain") is True
            checks["no_product_written"] = not any(x["props"].get("source_flake_ids")
                for x in s.store.list_samples(kind="flake"))
        else:
            rid = await successful_job("reactor_reaction", chip_id=CHIP, duration_s=80)
            result = await call("reactor", "submit_reactor_pair_result", {"chip_id": CHIP, "reaction_job_id": rid})
            if result.status != "ok":
                raise RuntimeError(result.summary)
            products = [x for x in s.store.list_samples(kind="flake", location=CHIP)
                        if x["props"].get("source_flake_ids")]
            pair = s.store.get_sample(CHIP)["props"]["reactor_pair"]
            checks["one_small_product"] = len(products) == 1 and 0 < products[0]["props"]["area_um2"] <= 288.001
            checks["both_sources_retained"] = all(s.store.get_sample(fid)["status"] == "reactor_consumed"
                                                 for fid in pair["input_flake_ids"])
            checks["lineage_matches_pair"] = products[0]["props"]["source_flake_ids"] == pair["input_flake_ids"]
            checks["code_checked_acceptance"] = result.data["accepted"] is True
            await successful_job("unload_reactor_chip", chip_id=CHIP)
            checks["chip_recovered"] = s.store.get_sample(CHIP)["location"] == "reactor_chip_slot_1"
            checks["virtual_access_conditions"] = s.gate.heater("reactor_heater").read_temperature() <= 80
            checks["rod_retracted"] = s.gate.stage("reactor_insert").get_pose().y == 0
        s.gateway.delegation_finished("reactor")
        reactor_step = next(x for x in s.store.get_project(s.project_id)["plan"] if x["id"] == "s2")
        checks["completion_matches_evidence"] = (reactor_step["status"] == "done" if scenario == "normal"
                                                  else reactor_step["status"] != "done")
        summary = ("Scripted result: one product accepted; its two sources are retained as consumed "
                   "history. The carrier cooled and returned to storage." if scenario == "normal" else
                   "Scripted result: camera loss parked the reaction. Targets are off, state remains "
                   "uncertain, and no product is accepted. Human review is required.")
        s.store.emit("agent.done", {"summary": summary}, agent="reactor")
        s.store.emit("agent.message", {"text": summary}, agent="orchestrator")
        s.store.emit("agent.done", {"summary": summary}, agent="orchestrator")
    except Exception as exc:
        error = str(exc)
    finally:
        await s.runtime.close()
        recorder.stop()
    checks["targets_disabled"] = s.gate.heater("reactor_heater").get_target() is None \
        and s.gate.gas_flow("reactor_flow").get_target() == 0
    events = s.store.events_after(0, limit=1_000_000)
    checks["no_legacy_growth"] = all(r["flow"] != "reactor_growth" for r in s.store.list_runs())
    checks["no_safety_violation"] = not any(e.kind == "safety.violation" for e in events)
    checks["no_paid_model_calls"] = not any(e.kind == "agent.usage" for e in events) and s.store.spent_usd() == 0
    checks["application_source_unchanged"] = execution_source == source_fingerprint()
    purpose = ("Preprepared pair load, fixed microscope observation, one small product and recovery"
               if scenario == "normal" else "Camera loss during hot processing: disable targets, park and reject false completion")
    export = export_recording(folder / "qumus.db", folder / "export", title=f"Two-flake reactor: {scenario}",
        description=purpose + ". Offline scripted decisions; qualitative synthetic materials, geometry and images. "
            "No autonomous preparation or validated chemistry claim.", layout=s.lab.bench,
        meta={"provider": "scripted", "source": "software_simulation", "experimental": True,
              "execution_source_sha256": execution_source,
              "experiment_kind": "two_flake_reaction", "purpose": purpose,
              "preparation_source": "synthetic_fixture", "placement_fixture": cfg["placement"],
              "noise_profile": "default", "seed": 7,
              "acceptance_checks": checks})
    checks["media_complete"] = not check_recording(export)
    summary = {"passed": error is None and all(checks.values()), "checks": checks, "error": error,
               "estimated_usd": s.store.spent_usd(), "jobs": s.store.list_runs()}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in ("passed", "checks", "error", "estimated_usd")}, indent=2))
    return summary["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("normal", "camera-loss"), default="normal")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(record(args.out, args.scenario)) else 1)
