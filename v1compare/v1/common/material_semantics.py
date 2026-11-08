from __future__ import annotations

from typing import Optional


GRAPHITE_CODE = "Gr"
GRAPHENE_THICKNESS_LABELS = {"1", "1l", "monolayer"}
GRAPHITE_GATE_THICKNESS_LABELS = {"manylayer"}


def normalize_material_code(material: Optional[str]) -> Optional[str]:
    """Normalize operator material input to canonical lab material codes."""
    if material is None:
        return None
    normalized = material.strip()
    if not normalized:
        return None

    lowered = normalized.lower()
    if lowered in {"gr", "graphite", "graphene"}:
        return GRAPHITE_CODE
    if lowered in {"hbn", "boron nitride"}:
        return "hBN"
    if lowered in {"rese2", "re se2", "re-se2"}:
        return "ReSe2"
    return normalized


def is_graphene_flake(
    *,
    type_label: Optional[str],
    thickness_layers: Optional[float],
    thickness_label: Optional[str],
) -> bool:
    """Graphene is defined as Gr + monolayer (1L)."""
    if normalize_material_code(type_label) != GRAPHITE_CODE:
        return False
    if thickness_layers is not None and float(thickness_layers) == 1.0:
        return True
    if thickness_label is not None and thickness_label.strip().lower() in GRAPHENE_THICKNESS_LABELS:
        return True
    return False


def is_graphite_gate_flake(
    *,
    type_label: Optional[str],
    thickness_label: Optional[str],
) -> bool:
    """A graphite gate uses the database's categorical Gr/manylayer label.

    The material database does not store a calibrated graphite thickness in
    nanometers.  Keeping this predicate categorical prevents guidance such as
    ``2-8 nm`` from being compared with ``thickness_layers`` or from excluding
    an otherwise valid inventory row.
    """
    if normalize_material_code(type_label) != GRAPHITE_CODE:
        return False
    if thickness_label is None:
        return False
    return thickness_label.strip().lower() in GRAPHITE_GATE_THICKNESS_LABELS
