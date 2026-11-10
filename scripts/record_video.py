"""Record a Qumus recording's replay as a video (headless Chromium, screen capture).

    qumus2 replay <recording dir> --port 8792          # serve the replay (qumus-agent venv)
    python scripts/record_video.py --url http://127.0.0.1:8792/ --out run.mp4 --speed 16

Needs only Python Playwright, a Chromium it can launch (--chromium PATH when its own
download is missing) and an ffmpeg (--ffmpeg PATH; Playwright's bundled ffmpeg works
for WebM). It imports nothing from qumus2, so any venv with Playwright can run it.

The page plays the recording at --speed (lab seconds per real second; the player
already shortens long idle gaps). Frames come from the Chrome DevTools screencast
(sent when the screen changes) and are written at a constant --fps, repeating the
latest frame. Output: H.264 MP4 when the ffmpeg has libx264 and --out ends in .mp4,
otherwise VP8 WebM (the extension is changed to .webm).
"""

from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOOK = "\n;window.__replay = () => REPLAY;\n"


def _encoders(ffmpeg: str) -> str:
    try:
        return subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                              text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8792/")
    ap.add_argument("--out", required=True, help="output .mp4 (or .webm)")
    ap.add_argument("--speed", type=float, default=16.0)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--max-minutes", type=float, default=120.0, help="stop recording after this")
    ap.add_argument("--chromium", default=None, help="Chromium executable (default: Playwright's)")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg executable (default: on PATH)")
    args = ap.parse_args(argv)

    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("no ffmpeg: pass --ffmpeg (Playwright's bundled one works for WebM)")
    out = Path(args.out).resolve()
    encoders = _encoders(ffmpeg)
    if out.suffix == ".mp4" and "libx264" in encoders:
        codec = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-movflags", "+faststart"]
    else:
        out = out.with_suffix(".webm")
        codec = ["-c:v", "libvpx" if "libvpx " in encoders else "vp8", "-b:v", "4M", "-deadline", "realtime",
                 "-cpu-used", "8"]
    out.parent.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    w, h = (int(v) for v in args.size.lower().split("x"))
    frames: list[tuple[float, bytes]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**({"executable_path": args.chromium} if args.chromium else {}))
        ctx = browser.new_context(viewport={"width": w, "height": h})
        page = ctx.new_page()
        page.on("pageerror", lambda e: print("page error:", e, file=sys.stderr))

        def patch(route):
            r = route.fetch()
            route.fulfill(response=r, body=r.text() + HOOK,
                          headers={**r.headers, "content-type": "application/javascript"})

        page.route("**/static/app.js", patch)
        page.goto(args.url, wait_until="load")
        page.wait_for_function("() => window.__replay && window.__replay()", timeout=600_000)
        page.evaluate("() => { const R = window.__replay(); R.pause(); R.seekFraction(0); }")
        page.wait_for_timeout(2000)
        cdp = ctx.new_cdp_session(page)

        def on_frame(ev):
            frames.append((time.monotonic(), base64.b64decode(ev["data"])))
            try:
                cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
            except Exception:  # noqa: BLE001 - closing
                pass

        cdp.on("Page.screencastFrame", on_frame)
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 85, "maxWidth": w, "maxHeight": h})
        page.wait_for_timeout(1500)
        page.evaluate("s => { const R = window.__replay(); R.setSpeed(s); R.play(); }", args.speed)
        total = page.evaluate("() => window.__replay().total")
        print(f"recording {total:.0f} s of replay time at {args.speed:g}x "
              f"(about {total / args.speed / 60:.1f} min of video)", flush=True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < args.max_minutes * 60:
            if page.evaluate("() => { const R = window.__replay(); return R.now >= R.total - 0.5; }"):
                break
            page.wait_for_timeout(2000)
        page.wait_for_timeout(3000)  # hold the final frame
        end = time.monotonic()
        try:
            cdp.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001
            pass
        ctx.close()
        browser.close()
    if not frames:
        raise SystemExit("no frames captured")
    start = frames[0][0]
    n_out = int((end - start) * args.fps) + 1
    proc = subprocess.Popen([ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(args.fps),
                             "-c:v", "mjpeg", "-i", "-", *codec, str(out)], stdin=subprocess.PIPE)
    i = 0
    assert proc.stdin is not None
    for k in range(n_out):
        t = start + k / args.fps
        while i + 1 < len(frames) and frames[i + 1][0] <= t:
            i += 1
        proc.stdin.write(frames[i][1])
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print(f"wrote {out} ({len(frames)} screen changes, {n_out} frames, {n_out / args.fps / 60:.1f} min)",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
