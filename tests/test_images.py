"""Testbench pictures: rendered overview-camera views and microscope replay
(workstreams.md A2). Files are written only for photos a step keeps."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from conftest import put_sample

from qumus2.testbench.render import Canvas, MicroscopeRenderer, encode_png


def png_size(path: str | Path) -> tuple[int, int]:
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def png_pixel(path: str | Path, x: int, y: int) -> tuple[int, int, int]:
    """Pixel of a PNG written by encode_png (one IDAT, filter 0 rows)."""
    raw = Path(path).read_bytes()
    w, _ = struct.unpack(">II", raw[16:24])
    rows = zlib.decompress(raw[raw.index(b"IDAT") + 4:raw.index(b"IEND") - 8])
    i = y * (1 + 3 * w) + 1 + 3 * x
    return rows[i], rows[i + 1], rows[i + 2]


def test_png_encoder_round_trip():
    c = Canvas(4, 3, (10, 20, 30))
    c.rect(1, 1, 2, 2, (255, 0, 0))
    raw = encode_png(c.w, c.h, c.px)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    idat = raw[raw.index(b"IDAT") + 4:raw.index(b"IEND") - 8]
    rows = zlib.decompress(idat)
    assert len(rows) == 3 * (1 + 4 * 3)  # filter byte + RGB per row


def test_no_images_without_image_dir(services):
    st = services.jobs.wait(services.jobs.submit("place_chip_on_transfer_stage",
                                                 {"chip_id": "chip_001"}), 10)
    assert st.status == "succeeded"
    assert all(not o["images"] for x in st.result for o in x["observations"])


def test_verify_photos_are_saved_and_autofocus_is_not(tmp_path, make_services):
    s = make_services(image_dir=tmp_path / "images")
    st = s.jobs.wait(s.jobs.submit("exfoliate_chips", {"material": "Gr"}), 60)
    assert st.status == "succeeded"
    st = s.jobs.wait(s.jobs.submit("search_chip", {}), 120)
    assert st.status == "succeeded"
    cams = sorted((tmp_path / "images" / "camera").glob("*.png"))
    mics = sorted((tmp_path / "images" / "microscope").glob("*.png"))
    assert cams and mics
    # Directly test autofocus retention rather than imposing an old aggregate
    # photo count, which also includes intentionally saved neighbourhood views.
    from qumus2.flows.library.primitives import autofocus

    ctx = s.jobs.context_factory(st.run_id, {}, s.project_id)
    before = set((tmp_path / "images" / "microscope").glob("*.png"))
    after_id = s.store.last_event_id()
    autofocus(ctx, 20)
    # Motion previews are intentionally retained for honest focus replay. The
    # autofocus probe exposures themselves remain encoded bytes, without files.
    preview_paths = {
        Path(e.payload["microscope_preview"]["image"])
        for e in s.store.events_after(after_id)
        if e.kind == "lab.frame" and e.payload.get("microscope_preview", {}).get("image")
        and e.payload["microscope_preview"].get("purpose") == "motion_preview"
        and e.payload["microscope_preview"].get("verification") is False
    }
    assert set((tmp_path / "images" / "microscope").glob("*.png")) - before <= preview_paths
    w, h = png_size(cams[0])
    assert w > 100 and h > 100
    place = [x for x in st.result if x["step"] == "place_chip_on_transfer_stage"][0]
    img = place["observations"][-1]["images"][0]
    assert Path(img).is_absolute() and Path(img).exists()
    flake = [f for f in s.store.list_samples(kind="flake", location="chip_001")
             if f["status"] == "catalogued"][0]
    assert set(flake["props"]["images"]) == {"20x", "100x", "context"}
    assert all(Path(p).exists() for p in flake["props"]["images"].values())
    assert len(flake["props"]["pickup_assessment"]["images"]) == 3


def test_microscope_replays_real_photos(tmp_path):
    replay = tmp_path / "replay_src" / "graphene"
    replay.mkdir(parents=True)
    for i in range(3):
        (replay / f"img_{i}.jpg").write_bytes(b"\xff\xd8fake jpeg %d" % i)
    r = MicroscopeRenderer(tmp_path / "out", replay.parent)
    meta = {"objective": 100, "sharpness": 9.0,
            "flakes": [{"sim_id": "f1", "material": "Gr", "layers": 1, "dx_mm": 0, "dy_mm": 0,
                        "area_um2": 100.0}]}
    path, src = r.render("microscope", meta)
    assert src is not None and Path(src).parent == replay
    assert Path(path).read_bytes() == Path(src).read_bytes()
    assert Path(path).is_relative_to(tmp_path / "out")  # copied under the data dir
    assert r.render("microscope", meta)[1] == src  # same flake, same photo
    # no photos for hBN: drawn instead
    meta["flakes"][0]["material"] = "hBN"
    path, src = r.render("microscope", meta)
    assert src is None and png_size(path) == (480, 320)


def test_bench_picture_agrees_with_inspect_bench(make_services, tmp_path):
    from PIL import Image, ImageChops

    s = make_services(image_dir=tmp_path)
    look = {"regions": ["transfer_stage"]}

    def shot():
        st = s.jobs.wait(s.jobs.submit("inspect_bench", look), 30)
        obs = st.result[0]["observations"][0]
        return obs["measurements"], obs["images"][0]

    m, img = shot()
    assert m["empty"] == ["transfer_stage"]
    before = Image.open(img).copy()
    assert before.size == (1100, 650)
    # An unchanged physical state has the same immutable camera picture.
    assert shot()[1] == img
    put_sample(s, "chip_001", "mid_chip_1")
    assert s.jobs.wait(s.jobs.submit("place_chip_on_transfer_stage",
                                     {"chip_id": "chip_001"}), 30).status == "succeeded"
    m, img = shot()
    (det,) = m["regions"]["transfer_stage"]
    assert det["label"] == "chip"
    after = Image.open(img)
    # The newly placed chip changes pixels at its position in the 2D map.
    r = s.lab.devices.get("overview_camera").renderer
    x, y = r._p(det["pose"]["x"], det["pose"]["y"])
    crop = (int(x)-12, int(y)-12, int(x)+12, int(y)+12)
    assert ImageChops.difference(before.crop(crop), after.crop(crop)).getbbox()


def test_image_numbers_survive_restart_and_target_photos_reach_tools(tmp_path):

    from qumus2.app import build_services
    from qumus2.testbench.render import _Writer

    writer = _Writer(tmp_path / 'numbered')
    first = Path(writer.write('microscope', b'first'))
    second = Path(_Writer(tmp_path / 'numbered').write('microscope', b'second'))
    assert first != second and first.read_bytes() == b'first'
    s = build_services(tmp_path / 'run' / 'qumus.db')
    target = s.store.get_sample('chip_e01')
    # no display thumbnail of the electrode chip any more (Jialei 2026-11-06)
    assert 'electrodes' not in (target['props'].get('images') or {}) and target['props']['image_source'] == 'simulated'
    # An image is tied to the objective at capture, even after the turret moves.
    mic = s.lab.devices.get('microscope')
    mic.set_objective(20)
    exposure = mic.capture()
    capture_path = Path(exposure.saved_path())
    capture_bytes = capture_path.read_bytes()
    mic.set_objective(100)
    preview = mic.preview()
    assert preview.meta['objective'] == 100
    assert preview.meta['purpose'] == 'motion_preview' and not preview.meta['verification']
    assert exposure.meta['objective'] == 20
    assert exposure.saved_path() == str(capture_path)
    assert capture_path.read_bytes() == capture_bytes
    assert preview.saved_path() != str(capture_path)


def test_electrodes_follow_chip_pose_and_survive_world_restart(make_services, tmp_path):
    from qumus2.testbench.persist import load_lab_state, save_lab_state

    s = make_services(image_dir=tmp_path / 'images')
    chip = s.lab.world.objects['chip_e01']
    mic = s.lab.devices.get('microscope')
    chip.on = 'transfer_stage'
    chip.pose = chip.pose.model_copy(update={'x': mic.center[0], 'y': mic.center[1]})
    at_center = mic.capture().meta['electrodes'][0]['points'][0]
    chip.pose = chip.pose.model_copy(update={'x': mic.center[0] + .02})
    shifted = mic.capture().meta['electrodes'][0]['points'][0]
    assert abs(shifted[0] - at_center[0] - .02) < 1e-9
    state = tmp_path / 'world.pkl'
    save_lab_state(s.lab, s.clock, state)
    other = make_services(image_dir=tmp_path / 'other')
    load_lab_state(other.lab, other.clock, state)
    assert other.lab.world.objects['chip_e01'].electrodes == chip.electrodes


def test_older_saved_bench_gets_optics_without_resetting_physics(tmp_path):
    from qumus2.app import build_lab_side
    from qumus2.core.clock import SimClock
    from qumus2.runtime.store import RunStore
    from qumus2.testbench.lab import build_sim_lab
    from qumus2.testbench.persist import save_lab_state

    clock = SimClock()
    db = tmp_path / 'qumus.db'
    store = RunStore(db, clock=clock)
    lab = build_sim_lab(clock, store=store, seed=2)
    target = lab.world.objects['chip_e01']
    del target.electrodes  # pre-image-fixture pickle
    lab.world.tape.uses = 3
    clock.advance(123)
    rng = lab.world.rng.getstate()
    state = tmp_path / 'world.pkl'
    save_lab_state(lab, clock, state)
    restored = build_lab_side(db, state_path=state)
    chip = restored.lab.world.objects['chip_e01']
    assert chip.electrodes and chip.pose == target.pose
    assert restored.lab.world.tape.uses == 3 and restored.clock.now() == clock.now()
    assert restored.lab.world.rng.getstate() == rng
    row = restored.store.get_sample('chip_e01')
    assert row['status'] == 'patterned' and row['location'] == 'right_chip_1'
    restored.lab.restore_optical_references(restored.store)
    assert 'electrodes' not in (restored.store.get_sample('chip_e01')['props'].get('images') or {})


def test_replay_prefers_photos_of_the_same_thickness(tmp_path):
    src = tmp_path / "replay_src"
    for sub, n in (("graphene/monolayer", 2), ("graphene/manylayer", 2), ("hbn", 1)):
        (src / sub).mkdir(parents=True)
        for i in range(n):
            (src / sub / f"img_{i}.jpg").write_bytes(b"\xff\xd8 %s %d" % (sub.encode(), i))
    (src / "hbn" / "thick_20-30.png").write_bytes(b"\x89PNG 20-30")
    r = MicroscopeRenderer(tmp_path / "out", src)

    def pick(material, layers, sim_id="f1"):
        meta = {"objective": 100, "sharpness": 9.0, "flakes": [
            {"sim_id": sim_id, "material": material, "layers": layers, "dx_mm": 0,
             "dy_mm": 0, "area_um2": 100.0}]}
        return Path(r.render("microscope", meta)[1])

    assert all(pick("Gr", 1, f"f{i}").parent.name == "monolayer" for i in range(5))
    assert all(pick("Gr", 40, f"f{i}").parent.name == "manylayer" for i in range(5))
    assert pick("hBN", 75).name == "thick_20-30.png"   # 75 layers = ~25 nm
    assert pick("hBN", 10).parent.name == "hbn"         # no 0-10 photo: any hBN photo


def test_invalid_camera_region_is_explained_before_job_submission(tmp_path):
    import asyncio

    from qumus2.app import build_services

    s = build_services(tmp_path / 'run.db')
    out, _ = asyncio.run(s.gateway.call('eyes', 'inspect_bench',
                                      {'regions': ['search & stacking stage']}))
    assert out.status == 'error' and 'transfer_stage' in out.data['valid_regions']
    assert not s.store.list_runs()
