"""A seed's exfoliated flakes do not depend on other arm cycles.

Each arm move draws from its own stream per object and move
(SimWorld._move_rng), and the cleave is drawn over the whole chip with only
the pressed cells keeping their flakes. Extra place corrections, revisits or
retries on one chip then leave another chip's flakes unchanged, even when the
press footprint comes out a cell different.
"""
from __future__ import annotations

import json

from conftest import _close_owned_services
from test_imported_hallbar_target import declared_import

from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.testbench.hallbar import load_imported_hallbar_target, with_hallbar_target


def hallbar_bench(root, name):
    bundle = root / f"{name}-bundle"
    bundle.mkdir()
    (bundle / "target.json").write_text(json.dumps(declared_import().model_dump(mode="json")))
    (bundle / "electrode-layout.gds").write_bytes(b"UNIT ONLY; NOT ACTUAL GDS")
    work = root / name
    work.mkdir()
    load_imported_hallbar_target(bundle, workspace=work)
    config = with_hallbar_target(simulation_profile("hallbar-linked"), target=load_imported_hallbar_target(bundle))
    config = {k: v for k, v in config.items() if k != "material_recognition"}  # truth search: no 4K renders
    return build_services(str(work / "qumus.db"), scenario="hallbar_device", seed=8, testbench_config=config)


def run(s, flow, params, timeout=300):
    st = s.jobs.wait(s.jobs.submit(flow, params), timeout)
    assert st.status == "succeeded", st.steps
    return st


def flakes(s, chip_id):
    return [(f.id, f.layers, round(f.x_mm, 9), round(f.y_mm, 9), round(f.area_um2, 6))
            for f in s.lab.world.objects[chip_id].flakes]


def test_extra_arm_cycles_on_one_chip_leave_another_chips_flakes_unchanged(tmp_path):
    plain, busy = hallbar_bench(tmp_path, "plain"), hallbar_bench(tmp_path, "busy")
    try:
        # The same chip_001 move on both benches. On the busy one it is dropped
        # in transit, found, picked up again and re-placed: extra pick, drop,
        # placement and overview-camera draws, as place corrections and revisits add.
        busy.lab.faults.inject("drop_in_transit")
        placed = [run(s, "place_chip_on_transfer_stage", {"chip_id": "chip_001"}) for s in (plain, busy)]
        assert "dropped" in placed[1].result[0]["observations"][0]["message"]
        assert not any("dropped" in o["message"] for o in placed[0].result[0]["observations"])
        for s in (plain, busy):
            run(s, "exfoliate_chips", {"material": "Gr", "chip_ids": ["chip_002"]})
        a, b = flakes(plain, "chip_002"), flakes(busy, "chip_002")
        assert a and a == b
    finally:
        for s in (plain, busy):
            _close_owned_services(s)
