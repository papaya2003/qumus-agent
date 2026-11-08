"""Stacking display (#205): release ring, segmented frames, video replay."""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from qumus2.testbench.render import MicroscopeRenderer
from tests.test_white_light_optics import optical_lab


def _contact_frame(radius_mm, pinned_mm=None):
    contact = {"dx_mm": 0., "dy_mm": 0., "radius_mm": radius_mm, "film": True}
    if pinned_mm is not None:
        contact["pinned"] = {"dx_mm": 0., "dy_mm": 0., "radius_mm": pinned_mm}
    meta = {"objective": 5, "fov_mm": [2.6, 1.72], "on_chip": "chip_001", "sharpness": 10.5,
            "sharpness_chip": 10.5, "stamp": {"film": True}, "contact": contact,
            "observation_noise_scale": 0}
    return np.asarray(Image.open(io.BytesIO(MicroscopeRenderer._compose(meta, size=(480, 320)))))


def test_release_separation_is_a_thin_ring_not_a_bright_disc():
    bonded = _contact_frame(.2, pinned_mm=.2)
    peeling = _contact_frame(.19, pinned_mm=.2)
    y, x = np.mgrid[:320, :480]
    r = np.hypot(x+.5-240, y+.5-160)*2.6/480
    inside = r < .19*.8
    # the still bonded interior keeps its colour when the PDMS starts to peel
    assert abs(peeling[inside].mean()-bonded[inside].mean()) < 1
    # the separation front itself is bright
    front = abs(r-.19) < .002
    assert peeling[front].mean() > peeling[inside].mean()+10


def test_far_approach_holds_one_picture_and_rings_are_frame_by_frame(tmp_path):
    lab, _, gate, scope = optical_lab(tmp_path)
    holder = gate.manipulator("transfer_arm")
    holder.move_axis_to("z", 5.8)
    far, far_detailed = scope.preview_state()
    holder.move_axis_to("z", 5.7)
    assert scope.preview_state() == (far, False) and not far_detailed
    holder.move_axis_to("z", 5.3005)  # 0.5 um: white-light rings
    ring, detailed = scope.preview_state()
    assert detailed
    holder.move_axis_to("z", 5.3003)
    assert scope.preview_state()[0] != ring


def test_stamp_frames_become_one_h264_clip(tmp_path):
    pytest.importorskip("imageio_ffmpeg")
    from qumus2.ui.stamp_video import FPS, pack_stamp_videos, stamp_clips

    frames = []
    for i in range(4):
        path = tmp_path / f"microscope_{i:06d}.jpg"
        Image.new("RGB", (480, 320), (40*i, 80, 120)).save(path)
        frames.append(path)

    def preview(i, stamp=True):
        return {"kind": "lab.frame", "payload": {"microscope_preview": {
            "image": str(frames[i]), "timestamp": [0., 10., 10.25, 10.5][i], "stamp_in_view": stamp,
            "calibration": {"image_size_px": [480, 320]}}}}

    events = [preview(0), preview(1), {"kind": "device.command", "payload": {}}, preview(2), preview(3, False)]
    assert [len(c) for c in stamp_clips(events)] == [3]
    media = tmp_path / "media"
    media.mkdir()
    assert pack_stamp_videos(events, lambda image: image and __import__("pathlib").Path(image), media) == [
        "media/stamp_clip_001.mp4"]
    shown = [e["payload"]["microscope_preview"] for e in events if e["kind"] == "lab.frame"]
    # the still far frame is held for a second; then one video frame per step
    assert [p.get("video_time_s") for p in shown[:3]] == [round(.5/FPS, 4), round(15.5/FPS, 4),
                                                          round(16.5/FPS, 4)]
    assert all("image" not in p and p["video"] == "media/stamp_clip_001.mp4" for p in shown[:3])
    assert shown[3]["image"] == str(frames[3])  # not over the stage: stays a photo
    assert (media / "stamp_clip_001.mp4").read_bytes()[4:8] == b"ftyp"


def test_stacking_views_use_the_v1_camera_at_5x(tmp_path):
    from qumus2.core.optical_profiles import V1_MATERIAL_CAPTURE_PROFILE, V1_NATIVE_SUBSTRATE_RGB

    lab, _, gate, scope = optical_lab(tmp_path)
    scope.renderer = MicroscopeRenderer(tmp_path, material_capture_profile=V1_MATERIAL_CAPTURE_PROFILE)
    gate.microscope("microscope").set_objective(5)
    frame, display = scope.capture(), scope.preview_snapshot()
    assert (frame.width, frame.height) == (960, 540) and (display.width, display.height) == (3840, 2160)
    assert frame.meta["fov_mm"] == display.meta["fov_mm"] == pytest.approx([2.8032, 1.5768])
    pixels = np.asarray(Image.open(io.BytesIO(frame.data)).convert("RGB"), dtype=float)
    centre = pixels[200:340, 380:580].reshape(-1, 3).mean(axis=0)
    # v1 substrate (164, 98, 103) under the stamp's faint veil, not the synthetic purple
    assert centre[0] > centre[2] and abs(centre[0]-V1_NATIVE_SUBSTRATE_RGB[0]) < 25
    shown = np.asarray(Image.open(display.saved_path()).convert("RGB").resize((960, 540)), dtype=float)
    assert abs(shown[200:340, 380:580].mean()-pixels[200:340, 380:580].mean()) < 4
