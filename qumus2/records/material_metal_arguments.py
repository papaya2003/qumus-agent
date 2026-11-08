"""One closed plan-argument contract for save, delegation and job verification.

Normalization is a read-only view. Raw plan fields and historical obligations
are retained; aliases must agree, and unknown fields never disappear silently.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from qumus2.records.material_metal_simulation import SIM_PROTOCOL
from qumus2.records.reactor_requirements import norm, requirements_record
from qumus2.records.stamps import contact_film

OPERATIONS = frozenset({"prepare_material_metal", "reactor_material_metal"})
ALIASES = {"target_chip_id": "chip_id", "reactor_chip_id": "chip_id", "gr_flake_id": "reactive_flake_id",
           "support": "stamp_film", "metal_preparation_id": "preparation_id",
           "preserve_images": "retain_images", "preserve_provenance": "retain_source_provenance"}


class MaterialMetalPlanArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    chip_id: str | None = Field(None, min_length=1)
    flake_ids: list[str] | None = Field(None, min_length=2, max_length=2)
    hbn_flake_id: str | None = Field(None, min_length=1)
    reactive_flake_id: str | None = Field(None, min_length=1)
    stamp_id: str | None = Field(None, min_length=1)
    stamp_film: Literal["PC", "mica"] | None = None
    protocol_id: Literal["qualitative-material-metal-v1", "synthetic-material-metal-workflow-v1"] | None = None
    fixture_instruction_id: str | None = Field(None, min_length=1)
    top_material: str | None = Field(None, min_length=1)
    reactive_material: str | None = Field(None, min_length=1)
    metal: str | None = Field(None, min_length=1)
    substrate: str | None = Field(None, min_length=1)
    preparation_id: str | None = Field(None, min_length=1)
    retain_images: StrictBool | None = None
    retain_source_provenance: StrictBool | None = None
    temperature_c: float | None = Field(None, ge=22, le=950)
    flow_sccm: float | None = Field(None, ge=0, le=400)
    duration_s: float | None = Field(None, gt=0, le=900)


REACTION_FIELDS = frozenset({"temperature_c", "flow_sccm", "duration_s"})


def argument_contract(operation: str) -> dict:
    fields = MaterialMetalPlanArguments.model_json_schema()["properties"]
    if operation == "prepare_material_metal":
        fields = {k: v for k, v in fields.items() if k not in REACTION_FIELDS}
    return {"version": 1, "operation": operation, "fields": fields, "aliases": ALIASES,
        "unknown_fields": "rejected", "conflicting_aliases": "rejected",
        "normalization": "Read-only canonical view; raw plan obligations are preserved.",
        "evidence": {"stamp_film": "Known stamp property before execution; frozen preparation receipt afterwards.",
            "preparation_id": "Operator-reported metal coating preparation_id on the exact carrier.",
            "retain_images": "Original source images and independently observed release images (device protocol).",
            "retain_source_provenance": "Original source-chip IDs and the exact observed placement/receipt."}}


def normalize_arguments(operation: str, raw: dict) -> tuple[dict, list[dict]]:
    if operation not in OPERATIONS:
        return {}, []
    errors, normalized = [], {}
    fields = argument_contract(operation)["fields"]
    for key, value in raw.items():
        canonical = ALIASES.get(key, key)
        if canonical not in fields:
            errors.append({"field": key, "code": "unsupported_argument", "message": f"Unsupported {operation} plan argument {key!r}."})
        elif canonical in normalized and (normalized[canonical] != value or type(normalized[canonical]) is not type(value)):
            errors.append({"field": key, "code": "conflicting_alias", "message": f"{key} conflicts with another value for {canonical}."})
        else:
            normalized[canonical] = value
    try:
        normalized = MaterialMetalPlanArguments.model_validate(normalized).model_dump(exclude_none=True)
    except ValidationError as exc:
        errors.extend({"field": ".".join(map(str, e["loc"])), "code": "invalid_argument_type",
                       "message": e["msg"]} for e in exc.errors(include_url=False, include_input=False))
        return {}, errors
    ids = normalized.get("flake_ids")
    components = [normalized.get("hbn_flake_id"), normalized.get("reactive_flake_id")]
    if ids is not None and (any(not fid for fid in ids) or len(set(ids)) != 2):
        errors.append({"field": "flake_ids", "code": "invalid_flake_pair", "message": "Use two distinct nonempty flake IDs, hBN first."})
    if ids and any(fid is not None and fid != ids[i] for i, fid in enumerate(components)):
        errors.append({"field": "flake_ids", "code": "conflicting_alias", "message": "flake_ids disagrees with the named hBN/reactive flake IDs."})
    if ids is None and all(components):
        if components[0] == components[1]:
            errors.append({"field": "flake_ids", "code": "invalid_flake_pair", "message": "The hBN and reactive flake IDs must be distinct."})
        else:
            normalized["flake_ids"] = components
    return normalized, errors


def _target(store, step: dict, args: dict) -> str | None:
    targets = {c["sample_id"] for c in step.get("sample_outcomes", [])
               if (store.get_sample(c["sample_id"]) or {}).get("props", {}).get("reactor_substrate")}
    if (step.get("reactor_outcome") or {}).get("chip_id"):
        targets.add(step["reactor_outcome"]["chip_id"])
    if args.get("chip_id"):
        targets.add(args["chip_id"])
    return next(iter(targets)) if len(targets) == 1 else None


def argument_errors(store, project_id: str, step: dict, *, phase: str = "plan", run: dict | None = None) -> list[dict]:
    """Same syntax and assertions at every boundary; future evidence is not fabricated."""
    operation = step.get("operation")
    if operation not in OPERATIONS:
        return []
    args, errors = normalize_arguments(operation, step.get("operation_args") or {})
    if errors:
        return [{"step_id": step.get("id"), "rule": "material_metal_arguments", **e} for e in errors]

    def mismatch(field, expected, actual, message=None):
        errors.append({"field": field, "code": "argument_evidence_mismatch" if run else "argument_requirement_mismatch",
            "expected": expected, "actual": actual, "message": message or f"{field} requires {expected!r}; evidence has {actual!r}."})

    record = requirements_record(store, project_id)
    req = (record or {}).get("requirements", {})
    for field, key in (("top_material", "protective_material"), ("reactive_material", "reactive_material"),
                       ("metal", "metal"), ("substrate", "substrate"),
                       ("retain_images", "save_images"), ("retain_source_provenance", "retain_source_provenance")):
        if field in args and key in req:
            left, right = args[field], req[key]
            equal = norm(left) == norm(right) if field in ("top_material", "reactive_material") else left == right
            if not equal:
                mismatch(field, left, right, f"{field} conflicts with the original experiment requirement {right!r}.")
    chip_id = _target(store, step, args)
    if args.get("chip_id") and chip_id != args["chip_id"]:
        mismatch("chip_id", args["chip_id"], chip_id, "Operation arguments and declared carrier outcomes must name the same target.")
    chip = store.get_sample(chip_id) if chip_id else None
    coating = (chip or {}).get("props", {}).get("metal_coating") or {}
    if run is None:
        if "preparation_id" in args and (coating or phase == "delegate") and args["preparation_id"] != coating.get("preparation_id"):
            mismatch("preparation_id", args["preparation_id"], coating.get("preparation_id"))
        if "stamp_film" in args:
            stamp = store.get_sample(args["stamp_id"]) if args.get("stamp_id") else None
            known = contact_film(stamp) if stamp else None
            if known and known != args["stamp_film"] or phase == "delegate" and known is None:
                mismatch("stamp_film", args["stamp_film"], known, "Bind a stamp_id with a matching known PC/mica property before delegation.")
        ids = args.get("flake_ids", [args.get("hbn_flake_id"), args.get("reactive_flake_id")])
        for fid, material in zip(ids, ["hBN", req.get("reactive_material")], strict=True):
            row = store.get_sample(fid) if fid else None
            if row and material and norm(row.get("props", {}).get("material")) != norm(material):
                mismatch("flake_ids", material, row.get("props", {}).get("material"))
    else:
        params = run["params"]
        receipt = (chip or {}).get("props", {}).get("material_metal_preparation") or {}
        layers = receipt.get("layer_order") or []
        preparation = store.get_run(receipt.get("preparation_job_id", ""))
        source_params = params
        if operation == "reactor_material_metal":
            source_params = (preparation or {}).get("params", {})
            if (not preparation or preparation.get("project_id") != project_id
                    or preparation.get("flow") != "prepare_material_metal"
                    or preparation.get("status") != "succeeded"
                    or params.get("goal", {}).get("preparation") != receipt):
                mismatch("preparation_id", "matching original preparation job and frozen receipt", None)
        if run.get("project_id") != project_id or run.get("flow") != operation:
            mismatch("operation", operation, run.get("flow"), "Use the exact same-project operation job.")
        ids = params.get("flake_ids") or receipt.get("top_first_flake_ids") or []
        actual = {"chip_id": params.get("target_chip_id") or params.get("chip_id"), "flake_ids": ids,
            "hbn_flake_id": ids[0] if len(ids) == 2 else None, "reactive_flake_id": ids[1] if len(ids) == 2 else None,
            "stamp_id": params.get("stamp_id") or receipt.get("stamp_id"), "stamp_film": receipt.get("stamp_film"),
            "preparation_id": receipt.get("metal_coating", {}).get("preparation_id"),
            "metal": receipt.get("metal_coating", {}).get("metal"), "substrate": receipt.get("metal_coating", {}).get("substrate"),
            "top_material": layers[0] if layers else None,
            "reactive_material": layers[1] if len(layers) > 1 else None,
            **{k: params.get(k) for k in ("protocol_id", "fixture_instruction_id", *REACTION_FIELDS)}}
        for key, wanted in args.items():
            if key in ("retain_images", "retain_source_provenance"):
                continue
            equal = norm(wanted) == norm(actual.get(key)) if key in ("top_material", "reactive_material") else wanted == actual.get(key)
            if not equal:
                mismatch(key, wanted, actual.get(key))
        for fid in ids:
            props = (store.get_sample(fid) or {}).get("props", {})
            if args.get("retain_images"):
                images = props.get("images")
                saved = receipt.get("source_images", {}).get(fid)
                if receipt.get("protocol_id") != SIM_PROTOCOL:
                    paths = list(images.values()) if isinstance(images, dict) else images or []
                    saved = images if paths and set(paths) <= set(receipt.get("images", [])) else None
                if not images or images != saved or receipt.get("protocol_id") == SIM_PROTOCOL and not receipt.get("release_images"):
                    mismatch("retain_images", True, False, f"{fid} lacks matching retained source/release image evidence.")
            if args.get("retain_source_provenance"):
                source = receipt.get("source_chip_ids", {}).get(fid)
                observed_source = props.get("material_placement", {}).get("flake", {}).get("source_chip_id") if receipt.get("protocol_id") == SIM_PROTOCOL else props.get("chip_id")
                if (not source or source == chip_id or not store.get_sample(source) or props.get("chip_id") != source
                        or observed_source != source or source_params.get("source_chip_ids", {}).get(fid) != source):
                    mismatch("retain_source_provenance", True, False, f"{fid} lacks matching source-chip and observed placement provenance.")
        if (args.get("retain_images") or args.get("retain_source_provenance")) and len(ids) != 2:
            mismatch("flake_ids", "two evidenced source flakes", ids)
    return [{"step_id": step.get("id"), "rule": "material_metal_arguments", **e} for e in errors]


def argument_feedback(errors: list[dict]) -> str:
    return "; ".join(f"operation_args.{e['field']}: {e['message']}" for e in errors)


def submission_errors(store, project_id: str, step: dict, supplied: dict, bench: dict) -> list[dict]:
    """Bind public tool inputs to the delegated plan before any job is submitted."""
    errors = argument_errors(store, project_id, step, phase="delegate")
    expected, syntax = normalize_arguments(step.get("operation"), step.get("operation_args") or {})
    if errors or syntax:
        return errors or syntax
    target = _target(store, step, expected)
    if target:
        expected.setdefault("chip_id", target)
    actual = dict(supplied)
    actual.setdefault("protocol_id", SIM_PROTOCOL)
    ids = actual.get("flake_ids") or []
    if len(ids) == 2:
        actual.update(hbn_flake_id=ids[0], reactive_flake_id=ids[1])
    if step.get("operation") == "reactor_material_metal":
        profile = bench.get("reactor_module", {}).get("material_metal", {})
        for key in REACTION_FIELDS:
            if actual.get(key) is None:
                actual[key] = profile.get(key)
    bound = {"chip_id", "protocol_id", "fixture_instruction_id"}
    bound |= REACTION_FIELDS if step.get("operation") == "reactor_material_metal" else {
        "flake_ids", "hbn_flake_id", "reactive_flake_id", "stamp_id"}
    for key in bound & expected.keys():
        if expected[key] != actual.get(key):
            errors.append({"step_id": step.get("id"), "field": key, "rule": "material_metal_arguments",
                "code": "submission_mismatch", "expected": expected[key], "actual": actual.get(key),
                "message": f"Submitted {key} must match the delegated plan ({expected[key]!r}). No job was submitted."})
    return errors
