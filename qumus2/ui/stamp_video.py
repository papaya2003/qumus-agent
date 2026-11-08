"""Stacking replay as video (#205), like v1's StampVideoRecorder (mp4, 15 fps).

While a stamp hangs over the stage, the twin saves the microscope's motion
frames: a still picture during the far approach, every motion step once
white-light rings or a contact are in view. On export, each unbroken run of
such frames becomes one H.264 MP4 in media/, and those preview events point at
their time in the clip instead of at a separate photo.

A frame is shown for one video frame (1/15 s); a frame that stayed on screen
longer in simulated time is held longer, up to one second, so a still far
approach reads as a pause rather than vanishing. Evidence captures are not
touched; they keep their own lossless photos.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

FPS = 15
#: longest hold of one frame in the video (s); simulated time runs on underneath
MAX_HOLD_S = 1.0
VIDEO_SUFFIXES = (".mp4",)


def _ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError:
        return None


def available() -> bool:
    return _ffmpeg() is not None


def stamp_clips(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Unbroken runs of stamp motion frames (the preview payloads), in order.

    A run ends at a motion frame without the stamp, or when the picture size
    changes (an objective turn).
    """
    clips: list[list[dict[str, Any]]] = [[]]

    def size(preview: dict[str, Any]) -> Any:
        return (preview.get("calibration") or {}).get("image_size_px")

    for event in events:
        preview = (event.get("payload") or {}).get("microscope_preview")
        if event.get("kind") != "lab.frame" or not isinstance(preview, dict):
            continue
        if not preview.get("stamp_in_view") or not preview.get("image"):
            clips.append([])
            continue
        if clips[-1] and size(clips[-1][-1]) != size(preview):
            clips.append([])
        clips[-1].append(preview)
    return [clip for clip in clips if len(clip) > 1]


def _holds(clip: list[dict[str, Any]]) -> list[int]:
    """Video frames per picture: 1, or longer for a picture that stayed."""
    times = [float(p.get("timestamp") or 0.0) for p in clip]
    holds = []
    for i in range(len(clip)):
        dt = times[i+1]-times[i] if i+1 < len(clip) else 0.0
        holds.append(int(max(1, min(round(MAX_HOLD_S*FPS), round(dt*2)))))
    return holds


def write_clip(frames: list[Path], holds: list[int], out: Path) -> None:
    """Encode the pictures into one H.264 MP4 (yuv420p plays in every browser)."""
    import subprocess

    exe = _ffmpeg()
    if exe is None:
        raise RuntimeError("imageio-ffmpeg is not installed")
    with Image.open(frames[0]) as first:
        width, height = first.size
    # yuv420p needs even dimensions; pad by one pixel if needed
    pad_w, pad_h = width + width % 2, height + height % 2
    cmd = [exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{pad_w}x{pad_h}", "-r", str(FPS), "-i", "-",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for path, hold in zip(frames, holds, strict=True):
            with Image.open(path) as image:
                rgb = np.asarray(image.convert("RGB"))
            if rgb.shape[:2] != (height, width):
                raise ValueError(f"{path.name} differs in size from the clip")
            if (pad_w, pad_h) != (width, height):
                rgb = np.pad(rgb, ((0, pad_h-height), (0, pad_w-width), (0, 0)), mode="edge")
            data = np.ascontiguousarray(rgb).tobytes()
            for _ in range(hold):
                proc.stdin.write(data)
    finally:
        proc.stdin.close()
        code = proc.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg failed ({code}) writing {out.name}")


def pack_stamp_videos(events: list[dict[str, Any]], resolve: Any, media_dir: Path) -> list[str]:
    """Replace stamp motion photos by times in MP4 clips written to media_dir.

    `resolve(image) -> Path | None` finds a preview's file. Clips whose frames
    are missing stay as photos. Returns the media paths written.
    """
    if not available():
        return []
    written = []
    for clip in stamp_clips(events):
        files = [resolve(p["image"]) for p in clip]
        if any(f is None or not Path(f).is_file() for f in files):
            continue
        holds = _holds(clip)
        name = f"stamp_clip_{len(written)+1:03d}.mp4"
        write_clip([Path(f) for f in files], holds, media_dir / name)
        start = 0
        for preview, hold in zip(clip, holds, strict=True):
            preview.pop("image")
            # the middle of the picture's first video frame (no seek rounding)
            preview["video"] = f"media/{name}"
            preview["video_time_s"] = round((start+.5)/FPS, 4)
            start += hold
        written.append(f"media/{name}")
    return written
