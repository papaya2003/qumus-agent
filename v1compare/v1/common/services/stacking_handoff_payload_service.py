from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text

from database.db_utils import init_db, material_session_scope


def _norm_path(value: object) -> str:
    return str(value or "").strip().replace("\\", "/")


def _as_xy(value: object) -> Optional[list[float]]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return [float(value[0]), float(value[1])]
    except (TypeError, ValueError):
        return None


def _role_key(raw: object) -> str:
    text = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    return "".join(ch for ch in text if ch.isalnum() or ch == "_")


class StackingHandoffPayloadService:
    """Resolve design-agent real-flake handoff JSON into processing-agent args."""

    # molecule_flake_refind_accurate accepts shift_x/shift_y in the calibrated
    # 20X image-pixel frame.  The design handoff target centers are in real
    # microns, so convert relative target offsets directly to 20X pixels here.
    _PROCESSING_SHIFT_PIXEL_SIZE_UM = 0.183

    _WORKFLOW_FLAKE_COUNT = {
        "assembly_device": 2,
        "assembly_quantum_hall_device": 4,
    }

    _ROLE_FIELD_BY_INDEX = ("a", "b", "c", "d")

    def load_handoff(
        self,
        *,
        stacking_handoff_path: str = "",
        stacking_handoff_json: str = "",
    ) -> dict[str, Any]:
        if stacking_handoff_json.strip():
            payload = json.loads(stacking_handoff_json)
        elif stacking_handoff_path.strip():
            payload = json.loads(Path(stacking_handoff_path).read_text(encoding="utf-8"))
        else:
            raise ValueError("Provide stacking_handoff_path or stacking_handoff_json.")
        if not isinstance(payload, dict):
            raise ValueError("Stacking handoff must be a JSON object.")
        return payload

    def resolve(
        self,
        *,
        workflow_name: str,
        stacking_handoff_path: str = "",
        stacking_handoff_json: str = "",
        bottom_electrode_id: Optional[int] = None,
        role_to_slot: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        init_db()
        workflow = workflow_name.strip()
        if workflow not in self._WORKFLOW_FLAKE_COUNT:
            raise ValueError(
                f"Unsupported workflow_name {workflow_name!r}. "
                f"Use one of: {sorted(self._WORKFLOW_FLAKE_COUNT)}."
            )

        handoff = self.load_handoff(
            stacking_handoff_path=stacking_handoff_path,
            stacking_handoff_json=stacking_handoff_json,
        )
        role_to_slot = {
            _role_key(role): str(slot).strip().lower()
            for role, slot in (role_to_slot or {}).items()
            if str(role).strip() and str(slot).strip()
        }
        required_count = self._WORKFLOW_FLAKE_COUNT[workflow]
        movements = handoff.get("flake_stacking_movements")
        if not isinstance(movements, list):
            movements = []

        missing: list[str] = []
        warnings: list[str] = []
        payload: dict[str, Any] = {}
        resolved_flakes: list[dict[str, Any]] = []

        selected = self._select_movements(
            movements,
            required_count,
            role_to_slot,
            stack_order_direction=handoff.get("stack_order_direction"),
        )
        if len(selected) < required_count:
            missing.append(
                f"flake_stacking_movements: expected {required_count}, got {len(selected)}"
            )
        relative_shifts = self._relative_target_shifts_px(
            selected[:required_count],
            warnings=warnings,
        )

        for index, movement in enumerate(selected[:required_count]):
            slot = self._ROLE_FIELD_BY_INDEX[index]
            flake_record = self._resolve_flake(movement)
            if flake_record is None:
                missing.append(f"flake_{slot}: cannot resolve flake by flake_id or mask_path")
                continue
            self._add_flake_fields(
                payload=payload,
                slot=slot,
                movement=movement,
                flake=flake_record,
                handoff=handoff,
                shift_override=relative_shifts.get(index),
                missing=missing,
                warnings=warnings,
            )
            resolved_flakes.append(
                {
                    "slot": slot,
                    "role": movement.get("role"),
                    "material": movement.get("material"),
                    "flake_id": flake_record["flake_id"],
                    "chip_id": flake_record["chip_id"],
                    "mask_path": flake_record["mask_20x_path"],
                }
            )

        electrode = self._resolve_bottom_electrode(bottom_electrode_id)
        if electrode is None:
            missing.extend(
                [
                    "bottom_electrode_coord",
                    "bottom_electrode_image_path",
                    "electrode_chip_corner_coord",
                    "electrode_chip_corner_image_path",
                    "electrode_chip_id",
                ]
            )
        else:
            self._add_bottom_electrode_fields(payload, electrode, missing)
            if bottom_electrode_id is None:
                warnings.append(
                    "bottom_electrode_id was not provided; automatically selected "
                    f"the lowest available id {electrode['bottom_electrode_id']}"
                )

        source_missing = handoff.get("missing_for_stacking")
        if isinstance(source_missing, list) and source_missing:
            resolved_flake_ids = {
                int(item["flake_id"])
                for item in resolved_flakes
                if item.get("flake_id") is not None
            }
            unresolved_source_items = self._unresolved_design_handoff_items(
                source_missing,
                resolved_flake_ids=resolved_flake_ids,
                warnings=warnings,
            )
            missing.extend(f"design_handoff.{item}" for item in unresolved_source_items)

        duplicate_paths = self._duplicate_selected_masks(selected[:required_count])
        if duplicate_paths:
            warnings.append(
                "same mask selected for multiple stack roles: " + ", ".join(duplicate_paths)
            )

        required_fields = self._required_fields_for_workflow(workflow)
        for field in required_fields:
            if field not in payload or payload[field] in (None, ""):
                missing.append(field)

        missing = sorted(set(missing))
        return {
            "status": "ok" if not missing else "partial",
            "workflow_name": workflow,
            "additional_args": payload,
            "resolved_flakes": resolved_flakes,
            "bottom_electrode": electrode,
            "missing_fields": missing,
            "warnings": warnings,
            "source_handoff_machine_ready": handoff.get("machine_ready"),
            "source_stack_order_direction": handoff.get("stack_order_direction"),
            "pickup_order_direction": "top_to_bottom",
            "handoff_path": stacking_handoff_path or None,
        }

    @staticmethod
    def _unresolved_design_handoff_items(
        items: list[Any],
        *,
        resolved_flake_ids: set[int],
        warnings: list[str],
    ) -> list[str]:
        """Discard source-center warnings once the inventory DB supplied them.

        The design handoff intentionally may omit machine coordinates.  Those
        values are resolved from material_base.db here, so carrying the stale
        warning forward would incorrectly block an otherwise complete payload.
        Other design-level missing requirements remain blocking.
        """
        unresolved: list[str] = []
        for raw_item in items:
            item = str(raw_item)
            match = re.search(r"source[_ ]center.*flake[_ ]id\s*(\d+)", item, re.IGNORECASE)
            if match and int(match.group(1)) in resolved_flake_ids:
                warnings.append(f"resolved from material_base.db: {item}")
                continue
            unresolved.append(item)
        return unresolved

    def _select_movements(
        self,
        movements: list[Any],
        required_count: int,
        role_to_slot: dict[str, str],
        *,
        stack_order_direction: object = "",
    ) -> list[dict[str, Any]]:
        clean = [dict(item) for item in movements if isinstance(item, dict)]
        if role_to_slot:
            by_slot: dict[str, dict[str, Any]] = {}
            for movement in clean:
                mapped = role_to_slot.get(_role_key(movement.get("role")))
                if mapped:
                    by_slot[mapped.lower()] = movement
            selected = []
            for slot in self._ROLE_FIELD_BY_INDEX[:required_count]:
                if slot in by_slot:
                    selected.append(by_slot[slot])
            return selected
        selected = sorted(clean, key=lambda item: int(item.get("order") or 0))[:required_count]
        direction = str(stack_order_direction or "").strip().lower().replace("-", "_")
        if direction == "bottom_to_top":
            return list(reversed(selected))
        return selected

    def _resolve_flake(self, movement: dict[str, Any]) -> Optional[dict[str, Any]]:
        flake_id = movement.get("flake_id")
        # The design handoff's effective mask is the segmented 20X mask.  A
        # legacy 100X mask_path may remain in old provenance records, but it
        # must not be used to resolve a real-processing candidate.
        mask_path = _norm_path(movement.get("mask_20x_path") or movement.get("mask_path"))
        with material_session_scope() as session:
            rows = session.execute(
                text(
                """
                SELECT
                  f.id AS flake_id,
                  f.chip_id AS chip_id,
                  f.location_x AS location_x,
                  f.location_y AS location_y,
                  f.mask_path AS mask_path,
                  f.mask_20x_path AS mask_20x_path,
                  i20.path AS image_20x_path,
                  i100.path AS image_100x_path,
                  c.corner_location_x AS chip_corner_x,
                  c.corner_location_y AS chip_corner_y,
                  ci.path AS chip_corner_image_path
                FROM flake f
                JOIN chip c ON c.id = f.chip_id
                LEFT JOIN image i20 ON i20.id = f.image_20x_id
                LEFT JOIN image i100 ON i100.id = f.image_id
                LEFT JOIN image ci ON ci.id = c.corner_image_id
                """
                )
            ).mappings().all()
        records = [dict(row) for row in rows]
        if flake_id not in (None, ""):
            try:
                target_id = int(flake_id)
            except (TypeError, ValueError):
                target_id = None
            if target_id is not None:
                for record in records:
                    if int(record["flake_id"]) == target_id:
                        return record if record.get("mask_20x_path") else None
        if mask_path:
            for record in records:
                if _norm_path(record.get("mask_20x_path")) == mask_path:
                    return record
        return None

    def _add_flake_fields(
        self,
        *,
        payload: dict[str, Any],
        slot: str,
        movement: dict[str, Any],
        flake: dict[str, Any],
        handoff: dict[str, Any],
        shift_override: Optional[list[float]],
        missing: list[str],
        warnings: list[str],
    ) -> None:
        prefix = f"flake_{slot}"
        chip_prefix = f"chip_{slot}"

        flake_coord = _as_xy([flake.get("location_x"), flake.get("location_y")])
        chip_corner = _as_xy([flake.get("chip_corner_x"), flake.get("chip_corner_y")])
        payload[f"{prefix}_coord"] = flake_coord
        payload[f"{prefix}_image_path"] = flake.get("image_20x_path") or flake.get("image_100x_path")
        payload[f"{chip_prefix}_corner_coord"] = chip_corner
        payload[f"{chip_prefix}_corner_image_path"] = flake.get("chip_corner_image_path")
        payload[f"{chip_prefix}_id"] = flake.get("chip_id")

        if flake_coord is None:
            missing.append(f"{prefix}_coord")
        if not payload[f"{prefix}_image_path"]:
            missing.append(f"{prefix}_image_path")
        if chip_corner is None:
            missing.append(f"{chip_prefix}_corner_coord")
        if not payload[f"{chip_prefix}_corner_image_path"]:
            missing.append(f"{chip_prefix}_corner_image_path")

        shift = shift_override or self._movement_shift_px(movement, handoff)
        if shift is None:
            missing.extend([f"shift_x_{slot}", f"shift_y_{slot}"])
            shift = [0.0, 0.0]
        payload[f"shift_x_{slot}"] = float(shift[0])
        payload[f"shift_y_{slot}"] = float(shift[1])

        try:
            rotation_deg = float(movement.get("rotation_deg") or 0.0)
        except (TypeError, ValueError):
            rotation_deg = 0.0
        if abs(rotation_deg) > 1e-9:
            missing.append(f"{prefix}_rotation_must_be_zero")
        if movement.get("rotation_allowed") is True:
            warnings.append(f"{prefix}: rotation_allowed=true in handoff; processing ignores rotations")

    @classmethod
    def _relative_target_shifts_px(
        cls,
        movements: list[dict[str, Any]],
        *,
        warnings: list[str],
    ) -> dict[int, list[float]]:
        if not movements:
            return {}

        origin = cls._target_center_um(movements[0])
        if origin is None:
            warnings.append(
                "slot a target_center_um missing; falling back to design translation_px/translation_um"
            )
            return {}

        shifts: dict[int, list[float]] = {0: [0.0, 0.0]}
        for index, movement in enumerate(movements[1:], start=1):
            target = cls._target_center_um(movement)
            if target is None:
                role = movement.get("role") or f"slot {cls._ROLE_FIELD_BY_INDEX[index]}"
                warnings.append(
                    f"{role}: target_center_um missing; falling back to design translation_px/translation_um"
                )
                continue
            relative_um = [target[0] - origin[0], target[1] - origin[1]]
            shifts[index] = cls._processing_shift_input_from_um(relative_um)

        warnings.append(
            "processing shifts are relative to slot a target_center_um; "
            "slot a shift is forced to (0, 0), and absolute design translation_px is ignored"
        )
        return shifts

    @staticmethod
    def _target_center_um(movement: dict[str, Any]) -> Optional[list[float]]:
        for key in ("target_center_um", "target_center_gds", "target_center"):
            value = _as_xy(movement.get(key))
            if value is not None:
                return value
        return None

    @classmethod
    def _processing_shift_input_from_um(cls, relative_um: list[float]) -> list[float]:
        # Design/KLayout uses Cartesian coordinates (+Y upward).  The accurate
        # refind shift inputs are calibrated 20X image pixels (+Y downward).
        # Convert x directly and flip y.
        return [
            relative_um[0] / cls._PROCESSING_SHIFT_PIXEL_SIZE_UM,
            -relative_um[1] / cls._PROCESSING_SHIFT_PIXEL_SIZE_UM,
        ]

    def _movement_shift_px(
        self,
        movement: dict[str, Any],
        handoff: dict[str, Any],
    ) -> Optional[list[float]]:
        translation_um = _as_xy(movement.get("translation_um"))
        if translation_um is not None:
            return self._processing_shift_input_from_um(translation_um)

        translation_px = _as_xy(movement.get("translation_px"))
        if translation_px is None:
            return None

        source_pixel_size = self._pixel_size_um_per_px(handoff)
        if not source_pixel_size:
            return translation_px
        translation_um = [
            translation_px[0] * source_pixel_size,
            translation_px[1] * source_pixel_size,
        ]
        return self._processing_shift_input_from_um(translation_um)

    @staticmethod
    def _pixel_size_um_per_px(handoff: dict[str, Any]) -> Optional[float]:
        frames = handoff.get("coordinate_frames")
        if isinstance(frames, dict):
            value = frames.get("mask_pixel_size_um_per_px")
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
        try:
            return float(handoff.get("mask_pixel_size_um_per_px"))
        except (TypeError, ValueError):
            return None

    def _resolve_bottom_electrode(self, bottom_electrode_id: Optional[int]) -> Optional[dict[str, Any]]:
        with material_session_scope() as session:
            rows = session.execute(
                text(
                """
                SELECT
                  b.id AS bottom_electrode_id,
                  b.chip_id AS electrode_chip_id,
                  b.location_x AS location_x,
                  b.location_y AS location_y,
                  i20.path AS image_20x_path,
                  i100.path AS image_100x_path,
                  c.corner_location_x AS chip_corner_x,
                  c.corner_location_y AS chip_corner_y,
                  ci.path AS chip_corner_image_path
                FROM bottom_electrode b
                JOIN chip c ON c.id = b.chip_id
                LEFT JOIN image i20 ON i20.id = b.image_20x_id
                LEFT JOIN image i100 ON i100.id = b.image_id
                LEFT JOIN image ci ON ci.id = c.corner_image_id
                ORDER BY b.id ASC
                """
                )
            ).mappings().all()
        records = [dict(row) for row in rows]
        return self._choose_bottom_electrode(records, bottom_electrode_id)

    @staticmethod
    def _choose_bottom_electrode(
        records: list[dict[str, Any]],
        bottom_electrode_id: Optional[int],
    ) -> Optional[dict[str, Any]]:
        """Select an explicit electrode, or deterministically pick the lowest id."""
        if bottom_electrode_id is not None:
            for record in records:
                if int(record["bottom_electrode_id"]) == int(bottom_electrode_id):
                    return record
            return None
        if not records:
            return None
        return min(records, key=lambda record: int(record["bottom_electrode_id"]))

    @staticmethod
    def _add_bottom_electrode_fields(
        payload: dict[str, Any],
        electrode: dict[str, Any],
        missing: list[str],
    ) -> None:
        electrode_coord = _as_xy([electrode.get("location_x"), electrode.get("location_y")])
        electrode_corner = _as_xy([electrode.get("chip_corner_x"), electrode.get("chip_corner_y")])
        payload["bottom_electrode_coord"] = electrode_coord
        payload["bottom_electrode_image_path"] = electrode.get("image_20x_path") or electrode.get("image_100x_path")
        payload["electrode_chip_corner_coord"] = electrode_corner
        payload["electrode_chip_corner_image_path"] = electrode.get("chip_corner_image_path")
        payload["electrode_chip_id"] = electrode.get("electrode_chip_id")
        if electrode_coord is None:
            missing.append("bottom_electrode_coord")
        if not payload["bottom_electrode_image_path"]:
            missing.append("bottom_electrode_image_path")
        if electrode_corner is None:
            missing.append("electrode_chip_corner_coord")
        if not payload["electrode_chip_corner_image_path"]:
            missing.append("electrode_chip_corner_image_path")

    @staticmethod
    def _duplicate_selected_masks(movements: list[dict[str, Any]]) -> list[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for movement in movements:
            path = _norm_path(movement.get("mask_path"))
            if not path:
                continue
            if path in seen:
                duplicates.add(path)
            seen.add(path)
        return sorted(duplicates)

    @classmethod
    def _required_fields_for_workflow(cls, workflow_name: str) -> list[str]:
        count = cls._WORKFLOW_FLAKE_COUNT[workflow_name]
        fields: list[str] = []
        for slot in cls._ROLE_FIELD_BY_INDEX[:count]:
            fields.extend(
                [
                    f"flake_{slot}_coord",
                    f"flake_{slot}_image_path",
                    f"shift_x_{slot}",
                    f"shift_y_{slot}",
                    f"chip_{slot}_corner_coord",
                    f"chip_{slot}_corner_image_path",
                    f"chip_{slot}_id",
                ]
            )
        fields.extend(
            [
                "bottom_electrode_coord",
                "bottom_electrode_image_path",
                "electrode_chip_corner_coord",
                "electrode_chip_corner_image_path",
                "electrode_chip_id",
            ]
        )
        return fields
