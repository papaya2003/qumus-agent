"""Exporting a resumed run: earlier run folders and mixed picture sizes."""

from __future__ import annotations

import json

from PIL import Image

from qumus2.stations.mirror import _earlier_image_roots, earlier_workspaces
from qumus2.ui.stamp_video import write_clip


def test_resume_chain_is_followed(tmp_path):
    a, b, c = (tmp_path / n for n in "abc")
    for f in (a, b, c):
        f.mkdir()
    (c / "resumed_from.json").write_text(json.dumps({"workspace": str(b)}), encoding="utf-8")
    (b / "resumed_from.json").write_text(json.dumps({"workspace": str(a)}), encoding="utf-8")
    assert earlier_workspaces(c) == (b.resolve(), a.resolve())
    assert _earlier_image_roots(c / "images") == (b.resolve() / "images", a.resolve() / "images")
    assert earlier_workspaces(a) == ()


def test_clip_fits_frames_of_another_size(tmp_path):
    frames = []
    for i, size in enumerate([(64, 48), (128, 96), (64, 48)]):
        p = tmp_path / f"f{i}.png"
        Image.new("RGB", size, (i * 80, 20, 20)).save(p)
        frames.append(p)
    out = tmp_path / "clip.mp4"
    write_clip(frames, [2, 2, 2], out)
    assert out.stat().st_size > 0
