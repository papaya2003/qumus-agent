"""v1 accurate refind of the electrode site against the user's own 20x photo (#204)."""
from __future__ import annotations

import json
import math
import random

import pytest
from test_imported_hallbar_target import declared_import

from qumus2.app import build_services
from qumus2.config import simulation_profile
from qumus2.flows.library.stacking import RefindElectrodeSite
from qumus2.records.device_design import design_anchor_mm
from qumus2.testbench import user_reference
from qumus2.testbench.hallbar import load_imported_hallbar_target, with_hallbar_target

cv2 = pytest.importorskip("cv2")


@pytest.fixture(scope="module")
def services(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    bundle = root / "bundle"
    bundle.mkdir()
    target = declared_import()
    (bundle / "target.json").write_text(json.dumps(target.model_dump(mode="json")))
    (bundle / "electrode-layout.gds").write_bytes(b"UNIT ONLY; NOT ACTUAL GDS")
    work = root / "work"
    work.mkdir()
    load_imported_hallbar_target(bundle, workspace=work)
    config = with_hallbar_target(simulation_profile("hallbar-linked"), target=load_imported_hallbar_target(bundle))
    s = build_services(str(work / "qumus.db"), scenario="hallbar_device", seed=8, testbench_config=config)
    chip = next(c for c in s.store.list_samples(kind="chip") if c["props"].get("device_design_target"))
    return s, chip


def test_the_target_chip_has_the_users_20x_photo_of_its_site_and_is_put_back(services):
    s, chip = services
    ref = chip["props"]["user_reference_20x"]
    assert ref["objective"] == 20 and ref["source"] == "simulated_user_photo"
    assert tuple(ref["site_mm"]) == design_anchor_mm(chip["props"]["device_design_target"])
    assert cv2.imread(ref["image"]) is not None
    # the photo is taken off the record: the chip is back in storage, nothing on the stage
    sim = s.lab.world.objects[chip["id"]]
    assert sim.on is None and sim.pose.rotation == 0 and sim.held_by is None
    assert not [o for o in s.lab.world.objects.values() if o.on == "transfer_stage"]


@pytest.mark.parametrize("dx_um,dy_um,rotation", [(4, -3, 1.2), (-10, 6, -.8)])
def test_v1_centre_patch_match_finds_the_site_to_a_pixel(services, monkeypatch, dx_um, dy_um, rotation):
    s, chip = services
    ref = chip["props"]["user_reference_20x"]
    site = ref["site_mm"]

    class Placed(random.Random):
        def gauss(self, mu, sigma):
            return rotation

    monkeypatch.setattr(user_reference.random, "Random", Placed)
    shot = user_reference.photograph_site(s.lab, chip["id"], (site[0]+dx_um/1000, site[1]+dy_um/1000))
    from qumus2.perception.v1_vision import refind_template

    reference = cv2.cvtColor(cv2.imread(ref["image"]), cv2.COLOR_BGR2RGB)
    current = cv2.cvtColor(cv2.imread(shot["image"]), cv2.COLOR_BGR2RGB)
    match = refind_template(reference, current)
    sx, sy = shot["calibration"]["mm_per_pixel"]
    h, w = current.shape[:2]
    r = math.radians(rotation)
    wx = -(dx_um*math.cos(r)-dy_um*math.sin(r))/1000
    wy = -(dx_um*math.sin(r)+dy_um*math.cos(r))/1000
    expected = (w/2+wx/sx, h/2-wy/sy)  # image v is -world_y
    error_um = math.hypot((match.centre_px[0]-expected[0])*sx, (match.centre_px[1]-expected[1])*sy)*1000
    assert error_um < .5  # v1 locates at half size: 2 px of 0.18 um


def test_step_needs_the_users_photo_of_the_designed_site(services):
    s, chip = services
    from qumus2.core.errors import PreconditionFailed

    step = RefindElectrodeSite(chip["id"], (1., 1.))
    ctx = type("C", (), {"store": s.store})()
    with pytest.raises(PreconditionFailed, match="another site"):
        step._reference(ctx)
