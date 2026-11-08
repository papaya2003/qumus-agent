"""Recording routes for History, and a replay app used by the browser tests.

People watch recordings only in History of the running Qumus (Jialei
2026-11-04/06: one entry); `create_replay_app` is test infrastructure, not a
launcher. No agents, no testbench, no key: the page downloads the recording once and
plays it itself (play, pause, speed, seek, restart). Endpoints:

    GET /api/health        {"ok": true, "mode": "replay"}
    GET /api/recording     the recording (recording.json); ?name=<folder> another one
                           next to it (the page's History switches recordings)
    GET /api/recordings    every recording found next to it [{name, title, ..., outcome}]
    GET /api/recordings/<name>/media/<file>   a photo of that recording
    GET /api/image?path=   a photo from the recording's media/ folder
    GET /                  the same single-page front end as `qumus2 serve`

`qumus2 serve` has the same three recording endpoints (add_recording_routes),
so its History plays the demo recordings in the page.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse

from qumus2.ui.app import IMAGE_SUFFIXES, NO_CACHE, mount_front_end
from qumus2.ui.recording import FORMAT, summarize_events
from qumus2.ui.stamp_video import VIDEO_SUFFIXES

DEFAULT_ROOT = Path("recordings")


def find_recordings(root: str | Path = DEFAULT_ROOT) -> list[Path]:
    """Recording folders under `root`: model-driven ones first, then newest first."""
    out: list[tuple[bool, str, Path]] = []
    for p in sorted(Path(root).glob("*/recording.json")):
        if p.parent.name.endswith(".partial"):  # an export that never finished
            continue
        try:
            rec = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict) or rec.get("format") != FORMAT:
            continue
        meta = rec.get("meta", {})
        out.append((meta.get("agents") == "sdk", str(meta.get("recorded_at") or ""), p.parent))
    return [p for *_, p in sorted(out, key=lambda t: (t[0], t[1]), reverse=True)]


def resolve_recording(path: str | Path | None) -> Path:
    """The folder given; else the one recordings/DEFAULT names; else the first
    of find_recordings()."""
    if path:
        p = Path(path)
        if p.is_file():
            p = p.parent
        if not (p / "recording.json").is_file():
            raise FileNotFoundError(f"no recording.json in {p}")
        return p
    pick = DEFAULT_ROOT / "DEFAULT"  # one line: the recording History lists first
    if pick.is_file():
        name = pick.read_text().strip()
        if name and (DEFAULT_ROOT / name / "recording.json").is_file():
            return DEFAULT_ROOT / name
    found = find_recordings()
    if not found:
        raise FileNotFoundError("no recordings under ./recordings; record one with "
                                "scripts/record_demo.py or pass a folder")
    return found[0]


_ROWS: dict[str, tuple[float, dict[str, Any]]] = {}


def recording_row(folder: Path) -> dict[str, Any]:
    """One recording for the History list: its meta and how the run ended
    (summarize_events), cached until the file changes."""
    f = folder / "recording.json"
    mtime = f.stat().st_mtime
    hit = _ROWS.get(str(f))
    if hit and hit[0] == mtime:
        return dict(hit[1])
    rec = json.loads(f.read_text())
    meta = rec.get("meta", {})
    row = {**summarize_events(rec.get("events") or []), "name": folder.name, **{
        k: meta.get(k) for k in ("title", "description", "agents", "provider", "model", "models", "recorded_at",
                                 "duration_s", "cost_usd", "cost_basis")}}
    row["request"] = meta.get("request") or row["request"]
    _ROWS[str(f)] = (mtime, row)
    return dict(row)


def default_name(root: Path) -> str | None:
    pick = root / "DEFAULT"
    return pick.read_text().strip() or None if pick.is_file() else None


def add_recording_routes(app: FastAPI, root: str | Path, current: Path | None = None) -> None:
    """GET /api/recordings, /api/recording[?name=], /api/recordings/<name>/media/<file>
    for the recordings under `root` (the replay server and `qumus2 serve`)."""
    root = Path(root)

    def folder(name: str) -> Path:
        p = (root / name).resolve()
        if ("/" in name or "\\" in name or name.startswith(".") or not p.is_relative_to(root.resolve())
                or not (p / "recording.json").is_file()):
            raise HTTPException(404, "no such recording")
        return p

    @app.get("/api/recordings")
    def recordings() -> list[dict[str, Any]]:
        items, pick = [], default_name(root)
        for p in find_recordings(root):
            try:
                row = recording_row(p)
            except (OSError, json.JSONDecodeError):
                continue
            items.append({**row, "current": current is not None and p.resolve() == current,
                          "default": p.name == pick})
        return items

    @app.get("/api/recording")
    def recording(name: str | None = None) -> FileResponse:
        if name:
            f = folder(name) / "recording.json"
        elif current is not None:
            f = current / "recording.json"
        else:
            raise HTTPException(404, "no recording is playing")
        return FileResponse(f, media_type="application/json", headers={"cache-control": "no-cache"})

    @app.get("/api/recordings/{name}/media/{path:path}")
    def media(name: str, path: str) -> FileResponse:
        base = folder(name) / "media"
        p = (base / path).resolve()
        if (not p.is_relative_to(base) or p.suffix.lower() not in (*IMAGE_SUFFIXES, *VIDEO_SUFFIXES)
                or not p.is_file()):
            raise HTTPException(404, "no such image")
        return FileResponse(p, headers=NO_CACHE)


def create_replay_app(rec_dir: str | Path) -> FastAPI:
    rec_dir = Path(rec_dir).resolve()
    app = FastAPI(title="Qumus v2 replay")
    app.add_middleware(GZipMiddleware, minimum_size=2048)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "mode": "replay"}

    add_recording_routes(app, rec_dir.parent, current=rec_dir)

    @app.get("/api/image")
    def image(path: str) -> FileResponse:
        p = (rec_dir / path).resolve()
        if (not p.is_relative_to(rec_dir / "media")
                or p.suffix.lower() not in (*IMAGE_SUFFIXES, *VIDEO_SUFFIXES) or not p.is_file()):
            raise HTTPException(404, "no such image")
        # revalidated: another recording on the same port has its own media/ files
        return FileResponse(p, headers=NO_CACHE)

    mount_front_end(app)
    return app
