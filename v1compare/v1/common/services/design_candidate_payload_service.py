from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import selectinload

from common.material_semantics import (
    GRAPHITE_CODE,
    is_graphene_flake,
    is_graphite_gate_flake,
    normalize_material_code,
)
from database import models
from database.db_utils import init_db, material_session_scope


GRAPHENE_QH_HBN_TARGET_AREA_UM2 = 1200.0


def _guidance_layers(guidance: dict[str, Any]) -> list[dict[str, Any]]:
    layer_stack = guidance.get("layer_stack")
    if isinstance(layer_stack, list):
        return [layer for layer in layer_stack if isinstance(layer, dict)]
    stack_roles = guidance.get("stack_roles")
    if isinstance(stack_roles, dict):
        result: list[dict[str, Any]] = []
        for role, value in stack_roles.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("role", role)
            else:
                item = {"role": role, "material": value}
            result.append(item)
        return result
    return []


def _fallback_layers_for_guidance(guidance: dict[str, Any]) -> list[dict[str, Any]]:
    """Use a device-family fallback without introducing unrelated material roles."""
    text = " ".join(
        str(guidance.get(key) or "")
        for key in (
            "device_name",
            "research_goal",
            "overall_structure",
            "contact_scheme",
            "gate_scheme",
            "layout_constraints",
        )
    ).lower()
    if "graphene" in text and "rese2" not in text:
        return [
            {"role": "graphene_channel", "material": "graphene"},
            {"role": "top_hbn", "material": "hBN"},
            {"role": "bottom_hbn", "material": "hBN"},
            {"role": "graphite_gate", "material": "graphite"},
        ]
    if "rese2" in text:
        return [
            {"role": "rese2_channel", "material": "ReSe2"},
            {"role": "top_hbn", "material": "hBN"},
            {"role": "bottom_hbn", "material": "hBN"},
            {"role": "graphite_gate", "material": "graphite"},
        ]
    return [
        {"role": "graphene_channel", "material": "graphene"},
        {"role": "hbn", "material": "hBN"},
        {"role": "graphite_gate", "material": "graphite"},
        {"role": "rese2_channel", "material": "ReSe2"},
    ]


def _role_key(raw: object, fallback: str) -> str:
    text = str(raw or fallback).strip().lower().replace("-", "_").replace(" ", "_")
    return "".join(ch for ch in text if ch.isalnum() or ch == "_") or fallback


def _role_material(layer: dict[str, Any]) -> tuple[str, str]:
    role = str(layer.get("role") or layer.get("name") or "").strip()
    material = str(layer.get("material") or layer.get("name") or "").strip()
    return role, material


def _required_flake_counts_by_role(
    layers: list[dict[str, Any]], candidate_roles: dict[str, list[dict[str, Any]]]
) -> dict[str, int]:
    """Preserve complete-device cardinality, including repeated/quantity roles."""
    counts = {role: 1 for role in candidate_roles}
    occurrences: dict[str, int] = {}
    explicit: dict[str, int] = {}
    for index, layer in enumerate(layers, start=1):
        role, material = _role_material(layer)
        key = _role_key(role or material, f"role_{index}")
        if key not in counts:
            continue
        occurrences[key] = occurrences.get(key, 0) + 1
        for field in ("required_count", "quantity", "count"):
            if layer.get(field) is None:
                continue
            try:
                value = int(layer[field])
            except (TypeError, ValueError):
                value = 1
            explicit[key] = max(explicit.get(key, 1), value if value > 0 else 1)
            break
    for role in counts:
        counts[role] = max(counts[role], occurrences.get(role, 0), explicit.get(role, 0))
    return counts


def _flake_matches_role(flake: models.Flake, role: str, material: str) -> bool:
    text = f"{role} {material}".lower()
    if "substrate" in text or "silicon chip" in text:
        return False
    if any(word in text for word in ("contact", "electrode", "metal", "source", "drain", "pad")):
        return False

    if "graphene" in text:
        return is_graphene_flake(
            type_label=flake.type_label,
            thickness_layers=flake.thickness_layers,
            thickness_label=flake.thickness_label,
        )
    if "graphite" in text or "gate" in text and normalize_material_code(material) == GRAPHITE_CODE:
        return is_graphite_gate_flake(
            type_label=flake.type_label,
            thickness_label=flake.thickness_label,
        )

    normalized = normalize_material_code(material)
    if normalized:
        return normalize_material_code(flake.type_label) == normalized
    return False


def _flake_record(flake: models.Flake) -> dict[str, Any]:
    image_100x_path = flake.image.path if flake.image is not None else None
    image_20x_path = flake.image_20x.path if flake.image_20x is not None else None
    mask_100x_path = flake.mask_path
    mask_20x_path = getattr(flake, "mask_20x_path", None)
    return {
        "flake_id": flake.id,
        "chip_id": flake.chip_id,
        "type_label": flake.type_label,
        "type_index": flake.type_index,
        "thickness_layers": flake.thickness_layers,
        "thickness_label": flake.thickness_label,
        "size_width_um": flake.size_width_um,
        "size_length_um": flake.size_length_um,
        "size_area_um": flake.size_area_um,
        "cleanliness": flake.cleanliness,
        "location_x": flake.location_x,
        "location_y": flake.location_y,
        # `mask_path` is the effective design path in this payload for
        # backward-compatible consumers; it is deliberately normalized to 20X.
        "mask_path": mask_20x_path,
        "mask_20x_path": mask_20x_path,
        "mask_100x_path": mask_100x_path,
        "image_100x_path": image_100x_path,
        "image_20x_path": image_20x_path,
        "preferred_image_path": image_20x_path or image_100x_path,
        "metadata": flake.metadata_json,
    }


def _readable_path(raw: object) -> bool:
    return bool(raw) and Path(str(raw)).is_file()


def _has_selection_optical_evidence(record: dict[str, Any]) -> bool:
    return _readable_path(record.get("image_100x_path")) and _readable_path(
        record.get("image_20x_path")
    )


def hydrate_candidate_payload_paths(candidate_payload: dict[str, Any]) -> dict[str, Any]:
    """Restore omitted evidence paths for only the physical candidates already supplied.

    Orchestrators sometimes serialize a reduced candidate record.  Selection should not
    treat those dropped fields as evidence that the physical flake is absent.  This helper
    looks up the same flake ids in material_base.db and fills missing or stale path fields;
    it never adds another candidate or widens a role's candidate set.
    """
    hydrated = copy.deepcopy(candidate_payload)
    roles = hydrated.get("candidate_flakes_by_role")
    if not isinstance(roles, dict):
        return hydrated

    flake_ids: set[int] = set()
    for raw_records in roles.values():
        if not isinstance(raw_records, list):
            continue
        for record in raw_records:
            if not isinstance(record, dict):
                continue
            try:
                if record.get("flake_id") is not None:
                    flake_ids.add(int(record["flake_id"]))
            except (TypeError, ValueError):
                continue

    if flake_ids:
        init_db()
        with material_session_scope() as session:
            flakes = (
                session.query(models.Flake)
                .options(selectinload(models.Flake.image), selectinload(models.Flake.image_20x))
                .filter(models.Flake.id.in_(sorted(flake_ids)))
                .all()
            )
            database_records = {int(flake.id): _flake_record(flake) for flake in flakes}
    else:
        database_records = {}

    recovered: list[dict[str, Any]] = []
    for role, raw_records in roles.items():
        if not isinstance(raw_records, list):
            continue
        for record in raw_records:
            if not isinstance(record, dict):
                continue
            try:
                flake_id = int(record["flake_id"])
            except (KeyError, TypeError, ValueError):
                continue
            source = database_records.get(flake_id)
            if not source:
                continue
            supplied_chip = record.get("chip_id")
            if supplied_chip is not None and str(supplied_chip) != str(source.get("chip_id")):
                continue
            recovered_fields: list[str] = []
            for field in (
                "image_100x_path",
                "image_20x_path",
                "mask_20x_path",
                "mask_100x_path",
            ):
                database_path = source.get(field)
                if not _readable_path(record.get(field)) and _readable_path(database_path):
                    record[field] = database_path
                    recovered_fields.append(field)
            if record.get("mask_20x_path") and not _readable_path(record.get("mask_path")):
                record["mask_path"] = record["mask_20x_path"]
            if not record.get("preferred_image_path") or not _readable_path(record.get("preferred_image_path")):
                record["preferred_image_path"] = record.get("image_20x_path") or record.get("image_100x_path")
            if recovered_fields:
                recovered.append(
                    {"role": str(role), "flake_id": flake_id, "fields": recovered_fields}
                )

    hydrated["candidate_mask_paths_by_role"] = {
        str(role): [
            str(record.get("mask_20x_path"))
            for record in records
            if isinstance(record, dict) and record.get("mask_20x_path")
        ]
        for role, records in roles.items()
        if isinstance(records, list)
    }
    hydrated["candidate_image_paths_by_role"] = {
        str(role): [
            str(record.get("image_100x_path"))
            for record in records
            if isinstance(record, dict) and record.get("image_100x_path")
        ]
        for role, records in roles.items()
        if isinstance(records, list)
    }
    hydrated["candidate_image_20x_paths_by_role"] = {
        str(role): [
            str(record.get("image_20x_path"))
            for record in records
            if isinstance(record, dict) and record.get("image_20x_path")
        ]
        for role, records in roles.items()
        if isinstance(records, list)
    }
    if recovered:
        hydrated["candidate_path_hydration"] = {
            "source": "material_base.db",
            "candidate_set_widened": False,
            "recovered": recovered,
        }
    return hydrated


def _target_thickness_range_nm(layer: dict[str, Any]) -> Optional[tuple[float, float]]:
    text = " ".join(
        str(layer.get(key) or "")
        for key in (
            "target_thickness",
            "thickness",
            "thickness_target",
            "requirement",
            "coverage",
            "layout_intent",
        )
    )
    match = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*nm", text, re.IGNORECASE)
    if match:
        a = float(match.group(1))
        b = float(match.group(2))
        return (min(a, b), max(a, b))
    match = re.search(r"(\d+(?:\.\d+)?)\s*nm", text, re.IGNORECASE)
    if match:
        value = float(match.group(1))
        return (value, value)
    return None


def _thickness_distance(value: Optional[float], target: Optional[tuple[float, float]]) -> float:
    if target is None:
        return 0.0
    if value is None:
        return 1_000_000.0
    low, high = target
    if low <= float(value) <= high:
        return 0.0
    return min(abs(float(value) - low), abs(float(value) - high))


def _estimate_confidence(record: dict[str, Any]) -> float:
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return 0.0
    estimate = metadata.get("estimated_thickness")
    if not isinstance(estimate, dict):
        return 0.0
    try:
        return float(estimate.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_graphene_quantum_hall_guidance(guidance: dict[str, Any]) -> bool:
    text = " ".join(
        str(guidance.get(key) or "")
        for key in (
            "device_name",
            "research_goal",
            "overall_structure",
            "layout_constraints",
            "layer_stack",
            "stack_roles",
            "material_requirements",
        )
    ).lower().replace("_", " ").replace("-", " ")
    return "graphene" in text and bool(
        "quantum hall" in text or re.search(r"\bqhe?\b", text)
    )


def _is_hbn_role(role: str, material: str) -> bool:
    text = f"{role} {material}".lower().replace("-", "").replace("_", "").replace(" ", "")
    return "hbn" in text


def _is_top_hbn_role(role: str, material: str) -> bool:
    normalized_role = role.lower().replace("-", "_").replace(" ", "_")
    return _is_hbn_role(role, material) and "top" in normalized_role and (
        "hbn" in normalized_role or "encapsulation" in normalized_role
    )


def _operator_selection_pin_matches(
    record: dict[str, Any],
    *,
    device_family: str,
    role: str,
    material: str,
) -> bool:
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return False
    pin = metadata.get("operator_selection_pin")
    if not isinstance(pin, dict) or pin.get("required") is not True:
        return False
    if str(pin.get("device_family") or "").strip().lower() != device_family.lower():
        return False
    pinned_role = str(pin.get("role") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if pinned_role in {"top_hbn", "top_encapsulation"}:
        return _is_top_hbn_role(role, material)
    return pinned_role == role.lower().replace("-", "_").replace(" ", "_")


def _is_graphite_gate_role(role: str, material: str) -> bool:
    text = f"{role} {material}".lower()
    return "graphite" in text or (
        "gate" in text and normalize_material_code(material) == GRAPHITE_CODE
    )


def _sort_role_candidates(
    records: list[dict[str, Any]],
    layer: dict[str, Any],
    *,
    preferred_area_um2: Optional[float] = None,
) -> list[dict[str, Any]]:
    role, material = _role_material(layer)
    # Graphite inventory is categorical (Gr/manylayer), not nanometer based.
    # Ignore legacy/generated nm wording rather than comparing nm with the
    # database's representative ``thickness_layers`` value.
    target = None if _is_graphite_gate_role(role, material) else _target_thickness_range_nm(layer)

    def sort_key(record: dict[str, Any]) -> tuple[float, float, float, int]:
        thickness_priority = _thickness_distance(record.get("thickness_layers"), target)
        confidence_priority = -_estimate_confidence(record)
        area = record.get("size_area_um")
        if preferred_area_um2 is None:
            return (
                thickness_priority,
                confidence_priority,
                -(float(area or 0.0)),
                int(record.get("flake_id") or 0),
            )
        area_priority = 1_000_000.0 if area is None else abs(float(area) - preferred_area_um2)
        return (
            thickness_priority,
            area_priority,
            confidence_priority,
            int(record.get("flake_id") or 0),
        )

    return sorted(
        records,
        key=sort_key,
    )


def _normalize_role_map(raw: object) -> dict[str, list[str]]:
    """Coerce a {role: path | [paths]} mapping into {role_key: [paths]}."""
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for role, value in raw.items():
        key = _role_key(role, str(role))
        if isinstance(value, (list, tuple, set)):
            items = [str(v).strip() for v in value if v]
        elif value:
            items = [str(value).strip()]
        else:
            items = []
        if items:
            out.setdefault(key, []).extend(items)
    return out


def _artifact_records_by_role(raw: object) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(raw, dict):
        return out
    for role, value in raw.items():
        key = _role_key(role, str(role))
        records = value if isinstance(value, (list, tuple)) else [value]
        for record in records:
            if isinstance(record, dict):
                out.setdefault(key, []).append(dict(record))
    return out


def _role_is_satisfied_by_artifact(missing_role: str, artifact_roles: set[str]) -> bool:
    for artifact_role in artifact_roles:
        if not artifact_role:
            continue
        if missing_role == artifact_role:
            return True
        if missing_role in artifact_role or artifact_role in missing_role:
            return True
    return False


def build_design_candidate_payload(
    *,
    approved_device_guidance: Optional[dict[str, Any]] = None,
    artifact_paths_by_role: Optional[dict[str, Any]] = None,
    artifacts_by_role: Optional[dict[str, Any]] = None,
    limit_per_role: int = 20,
) -> dict[str, Any]:
    """Build role-grouped flake candidates from the live material database.

    The returned keys match the Qumus/design-agent contract:
    `candidate_mask_paths_by_role`, `candidate_image_paths_by_role`, and
    `candidate_flakes_by_role`.

    `artifact_paths_by_role` / `artifacts_by_role` carry human-prepared or
    external-fab artifacts (electrodes, contacts, gates, substrates, masks,
    chips) registered on a resumable project. They are merged in so a resumed
    device workflow can hand a single payload to the design/stacking backend,
    and roles satisfied by such an artifact are removed from
    `missing_candidate_roles`. This is device-family agnostic.
    """
    init_db()
    guidance = approved_device_guidance or {}
    layers = _guidance_layers(guidance)

    if not layers:
        layers = _fallback_layers_for_guidance(guidance)

    candidate_masks: dict[str, list[str]] = {}
    candidate_images: dict[str, list[str]] = {}
    candidate_images_20x: dict[str, list[str]] = {}
    candidate_flakes: dict[str, list[dict[str, Any]]] = {}
    missing_roles: list[dict[str, Any]] = []
    skipped_roles: list[dict[str, Any]] = []
    candidate_prefilters_by_role: dict[str, dict[str, Any]] = {}
    operator_selection_pins_by_role: dict[str, list[int]] = {}
    graphene_quantum_hall = _is_graphene_quantum_hall_guidance(guidance)

    with material_session_scope() as session:
        flakes = (
            session.query(models.Flake)
            .options(selectinload(models.Flake.image), selectinload(models.Flake.image_20x))
            .filter(models.Flake.is_used.is_(False))
            .order_by(models.Flake.size_area_um.desc().nullslast(), models.Flake.id.asc())
            .all()
        )

        for index, layer in enumerate(layers, start=1):
            role, material = _role_material(layer)
            key = _role_key(role or material, f"role_{index}")
            text = f"{role} {material}".lower()
            if "substrate" in text or any(
                word in text for word in ("contact", "electrode", "metal", "source", "drain", "pad")
            ):
                skipped_roles.append({"role": key, "material": material, "reason": "not a Qumus flake role"})
                continue

            matches = [_flake_record(flake) for flake in flakes if _flake_matches_role(flake, role, material)]
            # Preserve physical candidates even when an evidence or segmentation
            # path needs repair. Selection eligibility is decided from the real
            # 100X + 20X optical images; a 20X mask is prepared later for layout.
            preferred_area_um2 = (
                GRAPHENE_QH_HBN_TARGET_AREA_UM2
                if graphene_quantum_hall and _is_hbn_role(role, material)
                else None
            )
            matches = _sort_role_candidates(
                matches,
                layer,
                preferred_area_um2=preferred_area_um2,
            )
            if graphene_quantum_hall and _is_top_hbn_role(role, material):
                pinned_matches = [
                    record
                    for record in matches
                    if _operator_selection_pin_matches(
                        record,
                        device_family="graphene_quantum_hall",
                        role=role,
                        material=material,
                    )
                ]
                if pinned_matches:
                    matches = pinned_matches
                    pinned_ids = [int(record["flake_id"]) for record in matches]
                    operator_selection_pins_by_role[key] = pinned_ids
                    candidate_prefilters_by_role.setdefault(key, {}).update(
                        {
                            "required_flake_ids": pinned_ids,
                            "operator_pinned": True,
                            "operator_pin_policy": (
                                "hard operator selection; do not substitute another candidate"
                            ),
                        }
                    )
            # Never let path-incomplete inventory rows crowd readable optical
            # candidates out of the bounded selector payload.
            matches = sorted(matches, key=lambda record: not _has_selection_optical_evidence(record))
            if preferred_area_um2 is not None:
                candidate_prefilters_by_role.setdefault(key, {}).update({
                    "preferred_area_um2": preferred_area_um2,
                    "policy": (
                        "soft target for graphene quantum Hall hBN after hard coverage/shape checks; "
                        "larger area is not automatically better"
                    ),
                })
            if _is_graphite_gate_role(role, material):
                candidate_prefilters_by_role[key] = {
                    "required_type_label": GRAPHITE_CODE,
                    "required_thickness_label": "manylayer",
                    "policy": (
                        "categorical graphite-gate match from material_base.db; "
                        "do not use or infer a nanometer thickness"
                    ),
                }
            matches = matches[: max(1, int(limit_per_role))]
            candidate_flakes[key] = matches
            candidate_masks[key] = [
                str(item["mask_20x_path"])
                for item in matches
                if item.get("mask_20x_path")
            ]
            candidate_images[key] = [
                str(item["image_100x_path"]) for item in matches if item.get("image_100x_path")
            ]
            candidate_images_20x[key] = [
                str(item["image_20x_path"]) for item in matches if item.get("image_20x_path")
            ]
            if not matches:
                missing_roles.append({"role": key, "material": material or "unspecified"})

    # Merge human-prepared / external-fab artifacts registered on the project.
    artifact_masks = _normalize_role_map(artifact_paths_by_role)
    artifact_records = _artifact_records_by_role(artifacts_by_role)
    for key, records in artifact_records.items():
        for record in records:
            path = str(record.get("path") or "").strip()
            if path:
                artifact_masks.setdefault(key, []).append(path)
    artifact_masks = {key: sorted(set(paths)) for key, paths in artifact_masks.items() if paths}

    artifact_roles = set(artifact_masks) | set(artifact_records)
    roles_satisfied_by_artifacts: list[str] = []
    still_missing: list[dict[str, Any]] = []
    for entry in missing_roles:
        if _role_is_satisfied_by_artifact(entry["role"], artifact_roles):
            roles_satisfied_by_artifacts.append(entry["role"])
        else:
            still_missing.append(entry)
    missing_roles = still_missing

    return {
        "status": "ok" if not missing_roles else "partial",
        "source": "material_base.db",
        "approved_device_name": guidance.get("device_name"),
        "candidate_mask_paths_by_role": candidate_masks,
        "candidate_image_paths_by_role": candidate_images,
        "candidate_image_20x_paths_by_role": candidate_images_20x,
        "candidate_flakes_by_role": candidate_flakes,
        "required_flake_counts_by_role": _required_flake_counts_by_role(layers, candidate_flakes),
        "candidate_prefilters_by_role": candidate_prefilters_by_role,
        "operator_selection_pins_by_role": operator_selection_pins_by_role,
        "artifact_paths_by_role": artifact_masks,
        "artifact_records_by_role": artifact_records,
        "roles_satisfied_by_artifacts": roles_satisfied_by_artifacts,
        "missing_candidate_roles": missing_roles,
        "skipped_nonflake_roles": skipped_roles,
        "pixel_size_um_per_px": 0.183,
        "coordinate_frame_notes": (
            "Candidate mask paths are segmented 20X image-frame references from mask_20x_path. "
            "The 20X calibration is 0.183 um/px; 100X masks are retained only as provenance. "
            "For flake selection, real 100X optical images are primary for local surface and thickness quality, "
            "real 20X optical images are primary for full-flake shape and continuity, and segmented 20X masks "
            "are reference-only for localization, approximate boundaries, and measurement. A mask-only gap is "
            "not evidence of a physical hole or crack. Segmented masks remain the machine-readable geometry input "
            "for downstream real-flake layout, subject to optical-image verification. When approved guidance includes thickness targets in "
            "nm, non-graphite candidates are sorted by target-thickness match before area. Graphite-gate roles "
            "always use the categorical database match type_label=Gr and thickness_label=manylayer; graphite nm "
            "ranges are ignored. For graphene quantum Hall hBN roles, "
            "approximately 1200 um^2 is a soft area target and larger area is not automatically preferred."
        ),
    }
