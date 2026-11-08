"""The stack aims at the electrode site, not the chip centre (#206)."""
from __future__ import annotations

from test_imported_hallbar_target import declared_import

from qumus2.records.device_design import design_anchor_mm
from qumus2.testbench.hallbar import build_imported_hallbar_target, hallbar_target


def test_a_centred_pattern_keeps_the_chip_centre():
    assert design_anchor_mm(hallbar_target()) == (5., 5.)
    # the declared unit import: contacts centred at (0.5, 0.5) on a 1 x 1 chip
    assert design_anchor_mm(declared_import()) == (.5, .5)


def test_the_anchor_follows_the_metal_not_the_declared_chip_size():
    # v1's 17.3 x 26.5 mm electrode chip with the metal where its GDS puts it
    small = declared_import()
    big = build_imported_hallbar_target(primitives=small.metal_primitives,
        source_sha256=small.import_receipt.source_sha256,
        owner_instruction_id=small.import_receipt.owner_instruction_id,
        owner_instruction_sha256=small.import_receipt.owner_instruction_sha256, size_mm=(17.3, 26.5))
    assert design_anchor_mm(big) == design_anchor_mm(small) == (.5, .5)
    assert design_anchor_mm(big) != tuple(v/2 for v in big.size_mm)
