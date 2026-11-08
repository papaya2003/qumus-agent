"""Flakes on electrodes or on other flakes are drawn bottom-up and see-through.

A released hBN/Gr stack used to be painted top-first and opaque: the bottom Gr
covered the hBN above it and the hBN hid the electrodes underneath.
"""
import io

import numpy as np
from PIL import Image

from qumus2.agents.tools.picking import Assessment
from qumus2.testbench.render import MicroscopeRenderer

FOV = (0.4, 0.3)
SIZE = (400, 300)
LEAD = {"kind": "lead", "points": [(-0.2, -0.005), (0.2, -0.005), (0.2, 0.005), (-0.2, 0.005)]}
HBN = [(-0.1, -0.1), (0.1, -0.1), (0.1, 0.1), (-0.1, 0.1)]
GR = [(0.03, 0.03), (0.07, 0.03), (0.07, 0.07), (0.03, 0.07)]


def flake(fid, material, layers, outline, level=None):
    f = {"sim_id": fid, "material": material, "layers": layers, "area_um2": 1.0, "dx_mm": 0, "dy_mm": 0,
         "outline_mm": outline, "visible_area_um2": 1.0, "geometry_source": "test"}
    return {**f, "stack_level": level} if level else f


def render(flakes, electrodes=(LEAD,)):
    meta = {"fov_mm": list(FOV), "flakes": flakes, "chip_corners": [], "on_chip": "chip_e01",
            "sharpness": 10.5, "sharpness_chip": 10.5, "objective": 20, "electrodes": list(electrodes),
            "observation_noise_scale": 0}
    return np.asarray(Image.open(io.BytesIO(MicroscopeRenderer._compose(meta, size=SIZE))).convert("RGB"), float)


def px(image, x_mm, y_mm):
    x = int(SIZE[0] / 2 + x_mm * SIZE[0] / FOV[0])
    y = int(SIZE[1] / 2 - y_mm * SIZE[1] / FOV[1])
    return image[y - 2:y + 3, x - 2:x + 3].reshape(-1, 3).mean(axis=0)


def test_stack_draws_bottom_up_whatever_the_list_order():
    top_first = render([flake("h", "hBN", 75, HBN, 2), flake("g", "Gr", 1, GR, 1)])
    bottom_first = render([flake("g", "Gr", 1, GR, 1), flake("h", "hBN", 75, HBN, 2)])
    assert np.array_equal(top_first, bottom_first)


def test_graphene_shows_faintly_under_hbn_and_electrodes_show_through():
    image = render([flake("h", "hBN", 75, HBN, 2), flake("g", "Gr", 1, GR, 1)])
    bare = render([])
    hbn_only, gr_under = px(image, -0.05, 0.05), px(image, 0.05, 0.05)
    assert 2 < np.abs(hbn_only - gr_under).sum() < 60  # visible, but only faintly
    lead_bare, lead_under = px(bare, 0.15, 0), px(image, -0.08, 0)
    hbn_on_substrate = px(image, -0.05, -0.05)
    # The lead shows through the hBN layer: neither hidden nor untouched.
    assert np.abs(lead_under - hbn_on_substrate).sum() > 40
    assert np.abs(lead_under - lead_bare).sum() > 10


def test_layer_colour_reproduces_the_tint_on_substrate():
    from qumus2.testbench.render import flake_layer, layer_opacity

    substrate = np.array([164., 98., 103.])
    for shown in ([60., 230., 90.], [150., 90., 100.], [250., 250., 250.]):
        alpha, color = flake_layer(shown, substrate, layer_opacity("Gr", 1))
        assert 0 < alpha <= 1 and np.all(color >= -1e-9) and np.all(color <= 255 + 1e-9)
        assert np.allclose(alpha * color + (1 - alpha) * substrate, shown)
    assert layer_opacity("Gr", 1) < layer_opacity("hBN", 75) < layer_opacity("hBN", 300) <= .9


def test_flakes_on_bare_substrate_keep_their_opaque_tint():
    image = render([flake("h", "hBN", 75, HBN)], electrodes=())
    alone = render([flake("h", "hBN", 75, HBN, 3)], electrodes=())
    assert np.array_equal(px(image, -0.05, 0.05), px(alone, -0.05, 0.05))


def test_one_sentence_note_is_kept_as_one_list_item():
    base = dict(flake_id="f", feasible=True, geometry_fitness=1, visible_cleanliness=1,
                continuity_uniformity=1, evidence_confidence=1, inventory_tiebreaker=1,
                overall_score=1, reason="r")
    a = Assessment(**base, observed_strengths="uniform body", observed_risks=" ",
                   segmentation_disagreements=["kept"])
    assert a.observed_strengths == ["uniform body"] and a.observed_risks == []
    assert a.segmentation_disagreements == ["kept"]


def test_a_profile_without_residue_draws_a_clean_chip_and_others_keep_it():
    """Jialei 2026-11-04: hallbar-linked draws no tape residue; the texture
    packet carries residue False only there, so other profiles are unchanged."""
    import io

    import numpy as np
    from PIL import Image

    from qumus2.testbench.render import MicroscopeRenderer

    texture = {"profile": "chip-local-procedural-v1", "seed": "8:chip_002", "chip_id": "chip_002",
               "pose": {"x": 0., "y": 0., "z": 0., "rotation": 0.}, "size_mm": [16., 26.],
               "view_center_world_mm": [1.3, 2.1]}
    meta = {"objective": 10, "fov_mm": [1.3, .86], "on_chip": "chip_002", "sharpness_chip": 10.5,
            "chip_pose": texture["pose"], "flakes": [], "observation_noise_scale": 0}

    def pixels(packet):
        data = MicroscopeRenderer._compose({**meta, "specimen_texture": packet}, size=(480, 320))
        return np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(int)

    with_residue, clean = pixels(texture), pixels({**texture, "residue": False})
    interior = (slice(40, 270), slice(40, 440))
    assert np.ptp(clean[interior], axis=(0, 1)).max() < np.ptp(with_residue[interior], axis=(0, 1)).max()
    assert (abs(with_residue[interior]-clean[interior]).sum(axis=2) > 30).sum() > 0
