"""Original-goal repairs of model-authored material-metal plan mechanics.

These checks never execute work, rewrite jobs, revise chemistry, or certify a
step. A repaired obligation keeps its identity and needs normal job/outcome
verification. Completed preparation is reused only through its exact evidence.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy

from qumus2.records.material_metal_arguments import (
    argument_errors,
    argument_feedback,
    normalize_arguments,
)
from qumus2.records.material_metal_simulation import SIM_PROTOCOL, device_preparation_errors
from qumus2.records.reactor_requirements import norm, requirements_record

PREPARATION_FLOWS = frozenset({"prepare_reactor_pair", "prepare_material_metal"})
LEGACY_OPERATIONS = {"prepare_reactor_pair": "prepare_material_metal", "reactor_reaction": "reactor_material_metal"}


def operation_errors(store, project_id: str, step: dict) -> list[dict]:
    if step.get("operation") not in LEGACY_OPERATIONS or not requirements_record(store, project_id):
        return []
    operation = step["operation"]
    return [{"step_id": step.get("id"), "rule": "material_metal_operation", "operation": operation,
        "required_operation": LEGACY_OPERATIONS[operation],
        "message": f"Established material-metal requirements exclude {operation}; use {LEGACY_OPERATIONS[operation]}. "
            "Retain the original materials, carrier, source provenance and final disposition. "
            "A model-authored legacy binding needs an evidenced plan correction before delegation."}]


def _carrier(store, step: dict) -> str | None:
    ids = {c["sample_id"] for c in step.get("sample_outcomes", [])
           if (store.get_sample(c["sample_id"]) or {}).get("props", {}).get("reactor_substrate")}
    args = step.get("operation_args") or {}
    for key in ("chip_id", "target_chip_id", "reactor_chip_id"):
        if args.get(key) is not None:
            if not isinstance(args[key], str) or not args[key]:
                return None
            ids.add(args[key])
    return next(iter(ids)) if len(ids) == 1 else None


def _related_runs(store, project_id: str, chip_id: str) -> list[dict]:
    return [r for r in store.list_runs() if r["project_id"] == project_id
            and r["flow"] in PREPARATION_FLOWS and r["params"].get("target_chip_id") == chip_id]


def _args_match(store, step: dict, run: dict, receipt: dict) -> bool:
    return not argument_errors(store, run["project_id"], step, phase="complete", run=run)


def _human_and_material_bindings_match(store, project_id: str, step: dict, record: dict, chip_id: str) -> bool:
    args, errors = normalize_arguments(step.get("operation"), step.get("operation_args") or {})
    if errors or argument_errors(store, project_id, step):
        return False
    ids = args.get("flake_ids") or [args.get("hbn_flake_id"), args.get("reactive_flake_id")]
    # Adding previously unbound implementation parameters still must respect the
    # person's named carrier, stamp and flakes. Ambiguous human bindings stay put.
    samples = [s for s in store.list_samples() if s["kind"] in ("stamp", "flake")
               or s.get("props", {}).get("reactor_substrate")]
    for row in store._x("SELECT text FROM instruction WHERE source='ui' AND kind IN ('message','plan_change') "
                        "AND (project_id=? OR project_id IS NULL)", (project_id,)):
        for sample in samples:
            if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(sample["id"]) + r"(?![A-Za-z0-9_])", row["text"]):
                continue
            if (sample.get("props", {}).get("reactor_substrate") and sample["id"] != chip_id
                    or sample["kind"] == "stamp" and args.get("stamp_id") and sample["id"] != args["stamp_id"]
                    or sample["kind"] == "flake" and any(ids) and sample["id"] not in ids):
                return False
    return True


def preparation_evidence(store, project_id: str, step: dict) -> dict | None:
    """Unique, linked, current device preparation matching this exact obligation."""
    chip_id = _carrier(store, step)
    if not chip_id or step.get("operation") != "prepare_material_metal":
        return None
    runs = _related_runs(store, project_id, chip_id)
    if len(runs) != 1:
        return None
    run = runs[0]
    record = requirements_record(store, project_id)
    chip = store.get_sample(chip_id) or {}
    receipt = chip.get("props", {}).get("material_metal_preparation") or {}
    if (not record or run["flow"] != "prepare_material_metal" or run["status"] != "succeeded"
            or run["params"].get("protocol_id") != SIM_PROTOCOL
            or receipt.get("preparation_job_id") != run["id"]
            or run["params"].get("requirements_event_id") != record["event_id"]
            or receipt.get("top_first_flake_ids") != run["params"].get("flake_ids")
            or receipt.get("stamp_id") != run["params"].get("stamp_id")
            or not run["params"].get("receipt_inputs")
            or any(receipt.get(k) != v for k, v in run["params"]["receipt_inputs"].items())
            or device_preparation_errors(store, project_id, chip_id) or not _args_match(store, step, run, receipt)):
        return None
    materials = [norm(m) for m in step.get("materials", [])]
    if materials and materials != ["hBN", norm(record["requirements"]["reactive_material"])]:
        return None
    bound = {**step, "operation_args": {"stamp_id": receipt["stamp_id"],
        "flake_ids": receipt["top_first_flake_ids"], **(step.get("operation_args") or {})}}
    if not _human_and_material_bindings_match(store, project_id, bound, record, chip_id):
        return None
    samples = [chip_id, receipt["stamp_id"], *receipt["top_first_flake_ids"]]
    if set(samples) & store.reserved_samples():
        return None
    if any((store.get_sample(sid) or {}).get("props", {}).get(key)
           for sid in samples for key in ("location_uncertain", "reactor_state_uncertain")):
        return None
    rows = store._x("SELECT id,payload_json FROM event WHERE kind='agent.job_linked' AND run_id=? "
                    "AND json_extract(payload_json,'$.project_id')=? ORDER BY id", (run["id"], project_id)).fetchall()
    links = [r["id"] for r in rows if json.loads(r["payload_json"]).get("job_id") == run["id"]
             and json.loads(r["payload_json"]).get("step_id")]
    if not links:
        return None
    observed = store._x("SELECT id FROM event WHERE kind='reactor.prepared' AND run_id=? ORDER BY id DESC LIMIT 1",
                        (run["id"],)).fetchone()
    if not observed:
        return None
    return {"job_id": run["id"], "flow": run["flow"], "project_id": project_id,
        "requirements_event_id": record["event_id"], "chip_id": chip_id,
        "top_first_flake_ids": receipt["top_first_flake_ids"], "stamp_id": receipt["stamp_id"],
        "preparation_event_id": observed["id"], "linked_event_ids": links,
        "next_action": "Read this exact original job with Hands.wait_job, then run the normal delegation/outcome check. "
            "Do not repeat preparation, reassess stacked source flakes, or change their ledger state."}


def preparation_job_error(store, project_id: str, step: dict, run: dict) -> str | None:
    """Completion boundary: an unrelated success cannot close this preparation."""
    if step.get("operation") != "prepare_material_metal":
        return None
    errors = argument_errors(store, project_id, step, phase="complete", run=run)
    if errors:
        return argument_feedback(errors)
    current = next((s for s in store.get_project(project_id)["plan"] if s.get("id") == step.get("id")), {})
    declared_protocol = (step.get("operation_args") or {}).get("protocol_id")
    if declared_protocol and run["params"].get("protocol_id") != declared_protocol:
        return "The preparation job uses a different protocol from the declared operation."
    if run["params"].get("protocol_id") != SIM_PROTOCOL:
        from qumus2.records.material_metal import FIXTURE_PROTOCOL, preparation_errors

        chip_id = _carrier(store, step)
        chip = store.get_sample(chip_id) if chip_id else None
        receipt = (chip or {}).get("props", {}).get("material_metal_preparation") or {}
        # Preserve the separately human-authorized historical software fixture.
        # It cannot discharge a repaired device obligation or a physical status.
        if (run["params"].get("protocol_id") != FIXTURE_PROTOCOL or run["project_id"] != project_id
                or run["flow"] != "prepare_material_metal" or not chip_id
                or receipt.get("scope") != "synthetic_workflow" or receipt.get("preparation_job_id") != run["id"]
                or receipt.get("top_first_flake_ids") != run["params"].get("flake_ids")
                or any(c.get("correction_kind") == "material_metal_mechanics" for c in current.get("outcome_corrections", []))
                or any(c.get("status") in ("material_metal_prepared", "stacked") for c in step.get("sample_outcomes", []))
                or not _args_match(store, step, run, receipt) or preparation_errors(store, project_id, chip_id)):
            return "Abstract fixture evidence cannot certify this device preparation or an unrelated target."
        return None
    evidence = preparation_evidence(store, project_id, step)
    if not evidence or evidence["job_id"] != run["id"]:
        return "Preparation completion requires its unique same-project, current-requirement, exact-target original job and optical receipt."
    for correction in current.get("outcome_corrections", []):
        if correction.get("correction_kind") != "material_metal_mechanics":
            continue
        original = correction.get("execution_evidence")
        if original and original["job_id"] != run["id"]:
            return "The corrected step must recheck its evidenced original preparation job, not another successful run."
    return None


def correct_material_metal_mechanics(store, project_id: str, previous: dict, current: dict) -> list[dict]:
    """Correct only legacy-operation and bare-output guesses, never human goals."""
    from qumus2.records.plan_completion import (
        completion_contract,
        linked_physical_jobs,
        model_written_contract_events,
        preserves_obligation,
    )

    operation = previous.get("operation")
    if (operation not in PREPARATION_FLOWS or previous.get("owner") != "hands"
            or previous.get("status") not in ("pending", "failed") or current.get("status") != "pending"
            or previous.get("id") != current.get("id") or current.get("operation") != "prepare_material_metal"
            or (current.get("operation_args") or {}).get("protocol_id", SIM_PROTOCOL) != SIM_PROTOCOL):
        return []
    record = requirements_record(store, project_id)
    origins = model_written_contract_events(store, project_id, previous)
    if not record or not origins:
        return []
    instruction = store.get_instruction(record.get("instruction_id", ""))
    if (not instruction or instruction["source"] != "ui" or instruction["kind"] != "message"
            or instruction["project_id"] not in (None, project_id)
            or instruction["text"] != record.get("original_request")):
        return []
    chip_id = _carrier(store, previous)
    if not chip_id or _carrier(store, current) != chip_id:
        return []
    expected_materials = ["hBN", norm(record["requirements"]["reactive_material"])]
    if [norm(m) for m in previous.get("materials", [])] != expected_materials:
        return []
    if not _human_and_material_bindings_match(store, project_id, current, record, chip_id):
        return []
    repaired = deepcopy(previous)
    repaired["operation"] = "prepare_material_metal"
    changed_statuses = set()
    for condition in repaired.get("sample_outcomes", []):
        if condition["sample_id"] == chip_id and condition.get("status") in ("bare", "reactor_pair_prepared"):
            changed_statuses.add(condition["status"])
            condition["status"] = "material_metal_prepared"
    if not changed_statuses and operation == repaired["operation"]:
        return []
    if not preserves_obligation(repaired, current):
        return []
    # A direct human status/operation/structured step binding is never reclassified
    # as a model typo. Conservatively retain ambiguous free-text instructions too.
    forbidden = {*changed_statuses, previous["id"], "prepare_reactor_pair", "two-flake", "裸片", "空载片"}
    for row in store._x("SELECT text FROM instruction WHERE source='ui' AND kind IN ('message','plan_change') "
                        "AND (project_id=? OR project_id IS NULL)", (project_id,)):
        if any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", row["text"], re.I)
               for word in forbidden):
            return []
    evidence = preparation_evidence(store, project_id, {**current, "operation": "prepare_material_metal"})
    related = _related_runs(store, project_id, chip_id)
    linked = linked_physical_jobs(store, project_id, previous["id"])
    if (related or linked or (store.get_sample(chip_id) or {}).get("props", {}).get("material_metal_preparation")):
        if not evidence or any(r["id"] != evidence["job_id"] for r in linked):
            return []
    elif chip_id in store.reserved_samples() or (store.get_sample(chip_id) or {}).get("props", {}).get("location_uncertain"):
        return []
    return [{"step_id": previous["id"], "correction_kind": "material_metal_mechanics", "contract_version": 1,
        "source": "original_human_requirements_and_registered_device_contract", "instruction_id": instruction["id"],
        "requirements_event_id": record["event_id"], "model_plan_event_ids": origins,
        "original_contract": completion_contract(previous), "corrected_contract": completion_contract(current),
        "execution_evidence": evidence, "reason": "Repair model-authored preparation mechanics to implement the unchanged material-metal goal.",
        "next_action": evidence["next_action"] if evidence else "Keep pending; execute the corrected operation once and verify its outcomes."}]
