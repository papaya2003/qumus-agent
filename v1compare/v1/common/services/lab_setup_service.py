"""Assemble a snapshot of the lab's *current setup* for reasoning agents.

"Current setup" = what this lab can actually do right now:
- which raw crystals / carriers are physically staged for exfoliation,
- what flake material is already in inventory,
- chip-slot occupancy and instrument state,
- the fixed capability envelope (what Qumus hardware executes vs. what needs a
  human / external fab).

This is deliberately material-agnostic. It never hard-codes a device family; it
reports the codes that happen to be present plus the generic capability model.
The physics expert / project manager read this before recommending materials or
device stacks so their advice is grounded in the real bench, not assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- fixed capability envelope --------------------------------------------------

QUMUS_EXECUTABLE_WORKFLOWS: list[str] = [
    "mechanical exfoliation of a staged raw crystal onto a bare Si/SiO2 chip",
    "automated optical microscope search / classification of exfoliated flakes",
    "deterministic dry-transfer stacking of catalogued flakes (transfer stage, no lithography)",
    "database-backed material / chip / device inventory operations",
]

HUMAN_OR_EXTERNAL_STEPS: list[str] = [
    "metal electrode / contact / gate lithography and deposition (EBL, evaporation, sputter)",
    "reactive-ion / plasma etching, edge-contact etching",
    "annealing, vacuum bake, surface activation",
    "CVD / ALD film growth",
    "externally fabricated contact chips or pre-patterned substrates",
]

# Material codes the hardware workflow understands as raw-carrier / search labels.
KNOWN_HARDWARE_MATERIAL_CODES: list[str] = ["Gr", "hBN", "ReSe2"]


@dataclass
class LabSetupSnapshot:
    raw_material_carriers: list[dict[str, Any]] = field(default_factory=list)
    stageable_material_codes: list[str] = field(default_factory=list)
    flake_inventory_by_material: dict[str, dict[str, Any]] = field(default_factory=dict)
    chip_slots: dict[str, Any] = field(default_factory=dict)
    instrument_status_summary: str = ""
    qumus_executable_workflows: list[str] = field(default_factory=lambda: list(QUMUS_EXECUTABLE_WORKFLOWS))
    human_or_external_steps: list[str] = field(default_factory=lambda: list(HUMAN_OR_EXTERNAL_STEPS))
    known_hardware_material_codes: list[str] = field(default_factory=lambda: list(KNOWN_HARDWARE_MATERIAL_CODES))
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_material_carriers": self.raw_material_carriers,
            "stageable_material_codes": self.stageable_material_codes,
            "flake_inventory_by_material": self.flake_inventory_by_material,
            "chip_slots": self.chip_slots,
            "instrument_status_summary": self.instrument_status_summary,
            "capability_envelope": {
                "qumus_executable_workflows": self.qumus_executable_workflows,
                "human_or_external_steps": self.human_or_external_steps,
                "known_hardware_material_codes": self.known_hardware_material_codes,
            },
            "warnings": self.warnings,
            "usage_note": (
                "This is the live bench state. Recommend materials/devices that are either "
                "already staged/in-inventory here or acquirable with the listed Qumus workflows; "
                "anything needing a step under human_or_external_steps must be tagged as a "
                "human/external dependency, not a Qumus hardware command."
            ),
        }


class LabSetupService:
    """Best-effort aggregator. Every data source is optional and failure-isolated."""

    def build_snapshot(self, *, flake_limit_per_material: int = 5) -> LabSetupSnapshot:
        snapshot = LabSetupSnapshot()

        self._collect_raw_carriers(snapshot)
        self._collect_flake_inventory(snapshot, flake_limit_per_material)
        self._collect_chip_slots(snapshot)
        self._collect_instrument_status(snapshot)

        return snapshot

    def build_setup_text(self, *, max_length: int = 4000, capability_only: bool = False) -> str:
        """Compact human/LLM-readable rendering for prompt injection.

        When ``capability_only`` is True, emit ONLY the fixed capability envelope
        (Qumus-executable workflows vs. human/external steps) and omit every
        inventory-flavoured detail — staged raw crystals, flake counts, chip
        slots, hardware material codes. Use this for "what materials could we
        explore" surveys, where inventory/DB state must not colour the answer.
        """
        snap = self.build_snapshot()

        if capability_only:
            lines = ["CURRENT LAB CAPABILITY ENVELOPE", ""]
            lines.append("Qumus can directly execute (autonomous):")
            lines.extend(f"  - {w}" for w in snap.qumus_executable_workflows)
            lines.append("Needs a human operator or external fab:")
            lines.extend(f"  - {s}" for s in snap.human_or_external_steps)
            lines.append("")
            lines.append(
                "Use this ONLY to tag whether a fabrication route is Qumus-executable "
                "or needs a human/external step. It says nothing about which materials "
                "are staged or in inventory; do not treat any material as preferred "
                "because of inventory or database state."
            )
            return "\n".join(lines)[:max_length]

        lines: list[str] = ["CURRENT LAB SETUP", ""]

        carriers = snap.raw_material_carriers
        if carriers:
            lines.append("Raw crystals staged for exfoliation (by QR message):")
            for c in carriers:
                lines.append(
                    f"  - {c.get('qr_message') or 'unknown'} "
                    f"(table {c.get('table_coordinate') or '?'})"
                )
        else:
            lines.append("Raw crystals staged for exfoliation: none detected on the table.")
        if snap.stageable_material_codes:
            lines.append(f"Stageable material codes: {', '.join(snap.stageable_material_codes)}")

        lines.append("")
        if snap.flake_inventory_by_material:
            lines.append("Flake inventory (unused) by material:")
            for material, info in snap.flake_inventory_by_material.items():
                lines.append(
                    f"  - {material}: {info.get('count', 0)} flake(s), "
                    f"largest ~{info.get('max_area_um') or '?'} um^2"
                )
        else:
            lines.append("Flake inventory: no catalogued unused flakes.")

        lines.append("")
        if snap.instrument_status_summary:
            lines.append(f"Instrument status: {snap.instrument_status_summary}")
        if snap.chip_slots:
            lines.append(
                f"Chip slots: {snap.chip_slots.get('occupied', 0)} occupied / "
                f"{snap.chip_slots.get('total', 0)} total"
            )

        lines.append("")
        lines.append("Qumus can directly execute:")
        lines.extend(f"  - {w}" for w in snap.qumus_executable_workflows)
        lines.append("Needs a human operator or external fab:")
        lines.extend(f"  - {s}" for s in snap.human_or_external_steps)

        if snap.warnings:
            lines.append("")
            lines.append("Warnings: " + "; ".join(snap.warnings))

        return "\n".join(lines)[:max_length]

    # -- collectors -----------------------------------------------------------

    def _collect_raw_carriers(self, snapshot: LabSetupSnapshot) -> None:
        try:
            from common.material_semantics import normalize_material_code
            from common.services.raw_material_carrier_service import RawMaterialCarrierService

            carriers = RawMaterialCarrierService().list_carriers(limit=50)
            codes: list[str] = []
            for carrier in carriers:
                qr = (carrier.qr_message or "").strip()
                snapshot.raw_material_carriers.append(
                    {
                        "id": carrier.id,
                        "qr_message": qr or None,
                        "table_coordinate": carrier.table_coordinate,
                        "qr_confidence": carrier.qr_confidence,
                    }
                )
                normalized = normalize_material_code(qr) if qr else None
                if normalized and normalized not in codes:
                    codes.append(normalized)
            snapshot.stageable_material_codes = codes
        except Exception as exc:  # pragma: no cover - depends on live DB
            snapshot.warnings.append(f"raw carriers unavailable: {exc}")

    def _collect_flake_inventory(self, snapshot: LabSetupSnapshot, limit_per_material: int) -> None:
        try:
            from common.services.flake_inventory_service import FlakeInventoryService

            flakes = FlakeInventoryService().list_flakes()
            by_material: dict[str, dict[str, Any]] = {}
            for flake in flakes:
                if getattr(flake, "is_used", False):
                    continue
                material = str(getattr(flake, "type_label", "") or "unknown")
                bucket = by_material.setdefault(
                    material, {"count": 0, "max_area_um": None, "examples": []}
                )
                bucket["count"] += 1
                area = getattr(flake, "size_area_um", None)
                if area is not None:
                    if bucket["max_area_um"] is None or area > bucket["max_area_um"]:
                        bucket["max_area_um"] = area
                if len(bucket["examples"]) < limit_per_material:
                    bucket["examples"].append(
                        {
                            "flake_id": getattr(flake, "flake_id", None) or getattr(flake, "id", None),
                            "size_area_um": area,
                            "thickness_label": getattr(flake, "thickness_label", None),
                        }
                    )
            snapshot.flake_inventory_by_material = by_material
        except Exception as exc:  # pragma: no cover
            snapshot.warnings.append(f"flake inventory unavailable: {exc}")

    def _collect_chip_slots(self, snapshot: LabSetupSnapshot) -> None:
        try:
            from common.services.chip_inventory_service import ChipInventoryService

            slots = ChipInventoryService().list_slots()
            occupied = sum(1 for slot in slots if getattr(slot, "is_occupied", False))
            snapshot.chip_slots = {"total": len(slots), "occupied": occupied}
        except Exception as exc:  # pragma: no cover
            snapshot.warnings.append(f"chip slots unavailable: {exc}")

    def _collect_instrument_status(self, snapshot: LabSetupSnapshot) -> None:
        try:
            from common.services.instrument_status_service import build_status_summary

            snapshot.instrument_status_summary = build_status_summary()
        except Exception as exc:  # pragma: no cover
            snapshot.warnings.append(f"instrument status unavailable: {exc}")
