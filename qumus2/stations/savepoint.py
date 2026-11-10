"""Save points across the brain and a simulated lab (save once after the search, resume later).

A full Hall-bar run spends most of its wall time exfoliating and searching
chips. When a fix only touches what happens after the search (pairing, design,
stacking), that part is the same every time, so a run can start from a save
point taken when the search is over.

Since the brain and the sim lab are separate programs, a save point has two
halves taken at the same quiet moment (no job open), just before the picker
reads the flake candidates (`get_flake_candidates`):

    <savepoint>/brain/qumus.db                 brain run store (SQLite backup)
    <savepoint>/brain/qumus.openai-sessions.db model conversation, if any
    <savepoint>/brain/timeline.jsonl           recorder timeline up to the save
    <savepoint>/lab/                           written by qumus-simlab itself
                                               (POST /api/sim/checkpoint)
    <savepoint>/savepoint.json                 what, when, seed, target, code

`restore_savepoint` copies the brain half into a fresh run folder; the sim lab
half is handed to `qumus_simlab serve --resume <savepoint>/lab`. The resumed
run calls the same tools in the same order as an unbroken run from that point.
Pictures taken before the save are hard-linked into the resumed run folder
(records may point at the original folder, so keep it while a resumed run is
in use); pictures taken after the resume go only into the new folder.

Only for simulated stations on this computer: a real station cannot be saved
and restored, and the sim lab writes its half into a folder on its own disk.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

VERSION = 1
SAVE_BEFORE = frozenset({"get_flake_candidates"})
OPEN_JOB = frozenset({"queued", "running", "paused", "parked", "waiting_human"})
SESSIONS_SUFFIX = ".openai-sessions.db"
TIMELINE = "timeline.jsonl"
REPO = Path(__file__).resolve().parents[2]

#: what the operator says when a resumed live run starts (the scripted scenario
#: is cut to start at the picker instead, see resume_scenario)
RESUME_MESSAGE = ("Make an hBN/graphene Hall bar on the eight-electrode chip chip_e01. The tapes are "
                  "already exfoliated and the chips searched: the laboratory record lists every chip "
                  "and flake found so far. Continue from flake selection until the device is stacked "
                  "on chip_e01 and checked.")
RESUME_SAY = ("Resuming from a save point: the exfoliated chips and the flakes found on them "
              "are in the laboratory record, so I continue with the pair selection.")


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5, cwd=REPO).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _backup(src: sqlite3.Connection, dst_path: Path) -> None:
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()


def open_jobs(store: Any) -> list[str]:
    return [r["id"] for r in store.list_runs() if r["status"] in OPEN_JOB]


def _local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


def savable(services: Any) -> str | None:
    """Why this run cannot be saved, or None when it can."""
    st = services.stations
    if services.store.path == ":memory:":
        return "the run store is in memory"
    if any(m.kind != "sim" for m in st.modules.values()):
        return "a station is real"
    if not all(_local(m.url) for m in st.modules.values()):
        return "a simulated station runs on another computer"
    return None


def save_savepoint(services: Any, dest: str | Path, *, label: str = "",
                   extra: dict[str, Any] | None = None, timeout_s: float = 300.0) -> Path:
    """Write one save point of an idle run (both halves) to `dest`."""
    if why := savable(services):
        raise RuntimeError(f"no save point: {why}")
    store = services.store
    jobs = services.jobs
    mirror = getattr(jobs, "mirror", None)
    if mirror is not None:
        mirror.sync()
    if busy := open_jobs(store):
        raise RuntimeError(f"jobs still open: {', '.join(busy)}")
    dest = Path(dest).resolve()
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "brain").mkdir(parents=True)
    client = jobs.clients[services.stations.records_from]
    lab = client._request("POST", "api/sim/checkpoint",
                         {"dir": str(tmp / "lab"), "label": label}, timeout_s=timeout_s)
    if mirror is not None:
        mirror.sync()  # nothing should have changed; keep the cursor at the lab's save
    work = Path(store.path).resolve().parent
    with store._lock:
        last_id = store.last_event_id()
        _backup(store._conn, tmp / "brain" / "qumus.db")
    sessions = Path(store.path).with_suffix(SESSIONS_SUFFIX)
    if sessions.exists():
        src = sqlite3.connect(sessions)
        try:
            _backup(src, tmp / "brain" / f"qumus{SESSIONS_SUFFIX}")
        finally:
            src.close()
    timeline = work / TIMELINE
    if timeline.exists():
        _copy_timeline(timeline, tmp / "brain" / TIMELINE, timeline.stat().st_size, last_id)
    meta = {"version": VERSION, "label": label, "workspace": str(work), "last_event_id": last_id,
            "saved_wall": time.time(), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "git_commit": _git_commit(), "agents": getattr(services.runtime, "mode", None),
            "stations": services.stations.to_dict(), "lab": lab if isinstance(lab, dict) else {},
            **(extra or {})}
    (tmp / "savepoint.json").write_text(json.dumps(meta, indent=1, default=str))
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return dest


class SavePointWriter:
    """Save a point before each `SAVE_BEFORE` tool call while no job is open.

    Keeps the latest `keep` save points in `root`. Never raises into the run:
    a failed save is logged and the run goes on.
    """

    def __init__(self, services: Any, root: str | Path, *, keep: int = 1,
                 extra: dict[str, Any] | None = None, log: Any = print) -> None:
        self.services, self.root, self.keep = services, Path(root), keep
        self.extra, self.log = extra or {}, log
        self.saved: list[Path] = []
        gateway = services.gateway
        inner = gateway.call

        async def call(agent: str, tool: str, args: dict[str, Any],
                       tool_use_id: str | None = None) -> Any:
            if tool in SAVE_BEFORE:
                import asyncio

                await asyncio.to_thread(self.maybe_save, f"before {agent}:{tool}")
            return await inner(agent, tool, args, tool_use_id)

        gateway.call = call

    def maybe_save(self, label: str) -> Path | None:
        store = self.services.store
        if open_jobs(store):
            return None
        name = f"sp{len(self.saved) + 1:02d}-ev{store.last_event_id()}"
        try:
            t0 = time.time()
            path = save_savepoint(self.services, self.root / name, label=label, extra=self.extra)
        except Exception as e:  # noqa: BLE001 - a save point must never stop the run
            self.log(f"save point {name} failed: {type(e).__name__}: {e}")
            store.emit("notification", {"level": "info", "text": f"save point failed: {e}"})
            return None
        self.saved.append(path)
        self.log(f"save point {path} ({time.time() - t0:.1f} s)")
        store.emit("notification", {"level": "info", "text": f"save point written: {path.name}",
                                    "savepoint": str(path)})
        for old in self.saved[:-self.keep] if self.keep else []:
            shutil.rmtree(old, ignore_errors=True)
        self.saved = self.saved[-self.keep:] if self.keep else self.saved
        return path


def find_savepoint(path: str | Path) -> Path:
    """A save point folder, or the newest one of a run folder (its `savepoints/`)."""
    path = Path(path)
    if (path / "savepoint.json").exists():
        return path
    found = sorted((p for p in (path / "savepoints").glob("sp*") if (p / "savepoint.json").exists()),
                   key=lambda p: json.loads((p / "savepoint.json").read_text())["last_event_id"])
    if not found:
        raise FileNotFoundError(f"no save point in {path}")
    return found[-1]


def _copy_timeline(src: Path, dst: Path, size: int, last_id: int) -> None:
    """The source timeline's lines up to the save point (its first `size` bytes)."""
    if not size or not src.exists():
        return
    with src.open("rb") as f, dst.open("wb") as out:
        for line in f.read(size).splitlines(keepends=True):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:  # a line cut at the save moment
                continue
            if row.get("id", 0) <= last_id:
                out.write(line if line.endswith(b"\n") else line + b"\n")


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def restore_savepoint(savepoint: str | Path, db_path: str | Path) -> dict[str, Any]:
    """Copy the brain half into the fresh run folder of `db_path`.

    Returns the save point's metadata plus `lab_dir` (pass it to
    `qumus_simlab serve --resume`) and `photo_root` (the original brain run
    folder; pass it to export_recording as an extra root)."""
    sp = find_savepoint(savepoint).resolve()
    meta = json.loads((sp / "savepoint.json").read_text())
    if meta.get("version") != VERSION:
        raise RuntimeError(f"save point version {meta.get('version')} is not supported")
    photo_root = Path(meta["workspace"])
    if not photo_root.is_dir():
        raise FileNotFoundError(f"the save point's pictures are in {photo_root}, which is gone")
    db = Path(db_path).resolve()
    if db.exists():
        raise RuntimeError(f"{db} exists; a resumed run needs a fresh --db path")
    db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sp / "brain" / "qumus.db", db)
    if (sp / "brain" / f"qumus{SESSIONS_SUFFIX}").exists():
        shutil.copy2(sp / "brain" / f"qumus{SESSIONS_SUFFIX}", db.with_suffix(SESSIONS_SUFFIX))
    if (sp / "brain" / TIMELINE).exists():
        shutil.copy2(sp / "brain" / TIMELINE, db.parent / TIMELINE)
    targets = photo_root / "targets"
    if targets.is_dir() and not (db.parent / "targets").exists():
        # imported target files: copied, the design tools refuse links
        shutil.copytree(targets, db.parent / "targets", symlinks=False)
    images = photo_root / "images"
    if images.is_dir() and not (db.parent / "images").exists():
        # mirrored pictures taken before the save: hard links (copies across disks), so the
        # resumed run writes its NEW pictures into its own folder, never the original's
        shutil.copytree(images, db.parent / "images", symlinks=False, copy_function=_link_or_copy)
    resumed = {"savepoint": str(sp), **{k: meta.get(k) for k in (
        "workspace", "last_event_id", "saved_at", "git_commit", "label")}}
    (db.parent / "resumed_from.json").write_text(json.dumps(resumed, indent=1))
    return {**meta, "resumed": resumed, "lab_dir": sp / "lab", "photo_root": photo_root}


def _delegation(actions: list[Any], agent: str) -> int | None:
    return next((i for i, a in enumerate(actions)
                 if isinstance(a, dict) and (a.get("delegate") or {}).get("to") == agent), None)


def resume_scenario(scenario: dict[str, Any], *, from_agent: str = "picker",
                    say: str = RESUME_SAY, redo_plan: bool = False) -> dict[str, Any]:
    """The scripted scenario from its first delegation to `from_agent` on.

    `redo_plan`: the save point has no accepted device plan (e.g. it failed
    before the save), so the advisor's delegation (up to its `done`) runs first."""
    turns = scenario.get("turns") or []
    if not turns:
        raise ValueError("scenario has no turns")
    actions = turns[0]["actions"]
    start = _delegation(actions, from_agent)
    if start is None:
        raise ValueError(f"scenario never delegates to {from_agent}")
    head = [{"say": {"agent": "orchestrator", "text": say}}] if say else []
    if redo_plan:
        a = _delegation(actions, "advisor")
        if a is None:
            raise ValueError("scenario never delegates to advisor")
        end = next(i for i in range(a, len(actions))
                   if isinstance(actions[i], dict) and (actions[i].get("done") or {}).get("agent") == "advisor")
        head += actions[a:end + 1]
    first = {**turns[0], "actions": head + actions[start:]}
    return {**scenario, "name": f"{scenario.get('name', 'scenario')}_resumed",
            "turns": [first, *turns[1:]]}
