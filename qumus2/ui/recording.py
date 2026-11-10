"""Recording a run for the replay page (UI workstream).

The run store already keeps every event, but two things a replay needs are not
in it: what the bench looked like (GET /api/bench reads the sim world, which is
gone after the run) and when each event really happened (the store's clock is
the testbench's simulated clock, which stands still while the model thinks).

`Recorder` fills both gaps without touching anything else: a thread tails the
event table and appends to `timeline.jsonl` next to the database

    {"id": <last event id>, "wall": <unix time>}                     each time new events appear
    {"id": ..., "wall": ..., "bench": {devices, objects, ...}}       when the bench changed

It only reads (the same reads as GET /api/bench), never commands a device.

`export_recording()` turns a run store + its timeline into a self-contained
folder that History plays:

    recording.json   meta, bench layout, events (each with its wall time),
                     bench frames, runs with steps, plan history
    media/           the photos the events refer to (paths rewritten)

Exports are scrubbed: API keys, bearer tokens and home-directory paths are
removed, and the export fails if anything key-like is left.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qumus2.core.clock import Clock, RealClock
from qumus2.core.run_paths import resolve_run_path
from qumus2.core.scrub import SECRET_ENV, SECRET_RES, scrub_text
from qumus2.ui.stamp_video import VIDEO_SUFFIXES

FORMAT = "qumus2-recording"
VERSION = 1
TIMELINE = "timeline.jsonl"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def recorded_models(events: list[dict[str, Any]]) -> list[str]:
    """Models actually requested, including failed sends, never just selections."""
    return sorted({e['payload']['model'] for e in events
                   if e['kind'] in ('agent.request', 'agent.usage', 'agent.turn_end')
                   and isinstance(e['payload'].get('model'), str)})


def timeline_path(db_path: str | Path) -> Path:
    return Path(db_path).parent / TIMELINE


class Recorder:
    """Tails the run store and records wall time and bench state (see module doc).

    `bench` is a callable returning the bench state dict (ui.app.bench_state);
    a failing read is skipped, never raised: recording must not break a run."""

    def __init__(self, store: Any, bench: Any, path: str | Path, *, poll_s: float = 0.2,
                 bench_every_s: float = 0.5, wall: Clock | None = None,
                 layout: dict[str, Any] | None = None) -> None:
        self.store, self.bench, self.path = store, bench, Path(path)
        self.layout = layout
        self.poll_s, self.bench_every_s = poll_s, bench_every_s
        self.wall = wall or RealClock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_id = -1
        self._last_bench = ""
        self._last_bench_t = 0.0
        self._dirty = False

    def start(self) -> Recorder:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        start: dict[str, Any] = {"id": self.store.last_event_id(),
                                 "wall": round(self.wall.now(), 3), "start": True}
        if self.layout is not None:
            start["layout"] = self.layout
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(start, default=str) + "\n")
        # A fast scripted job may finish before the polling thread's first
        # tick. Record the initial bench now so replay never starts in its
        # first later (possibly hot or occupied) state.
        self.tick(force_bench=True)
        self._thread = threading.Thread(target=self._loop, name="qumus-recorder", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.tick(force_bench=True)  # the final state

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - never take the run down
                continue

    def tick(self, force_bench: bool = False) -> None:
        now = round(self.wall.now(), 3)
        last = self.store.last_event_id()
        lines: list[dict[str, Any]] = []
        quiet = last == self._last_id
        if not quiet:
            self._dirty = True
            self._last_id = last
            lines.append({"id": last, "wall": now})
        # bench: at most every bench_every_s while events flow, and once more
        # when they stop, so the last frame after a burst is the settled state
        if force_bench or (self._dirty and (quiet or now - self._last_bench_t >= self.bench_every_s)):
            try:
                state = self.bench()
            except Exception:  # noqa: BLE001 - lab restarting: try again next tick
                state = None
            if state is not None:
                after = self.store.last_event_id()  # the state is at least as new as `last`
                text = json.dumps(state, sort_keys=True, default=str)
                if text != self._last_bench:
                    lines.append({"id": after, "wall": now, "bench": state})
                    self._last_bench = text
                elif after != self._last_id:
                    lines.append({"id": after, "wall": now})
                self._last_id = after
                self._last_bench_t = now
                self._dirty = not quiet
        if lines:
            with self.path.open("a", encoding="utf-8") as f:
                for line in lines:
                    f.write(json.dumps(line, default=str) + "\n")


def attach_recorder(services: Any, **kw: Any) -> Recorder | None:
    """Start a recorder for a file-backed run store; None for ":memory:"."""
    from qumus2.ui.app import bench_state

    if services.store.path == ":memory:":
        return None
    try:
        layout = services.lab.layout() or None
    except Exception:  # noqa: BLE001 - station not up yet: export falls back to the stored bench
        layout = None
    return Recorder(services.store, lambda: bench_state(services),
                    timeline_path(services.store.path), layout=layout, **kw).start()


# ----- export ---------------------------------------------------------------

def _walk(v: Any, fn: Any) -> Any:
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, list):
        return [_walk(x, fn) for x in v]
    if isinstance(v, dict):
        return {k: _walk(x, fn) for k, x in v.items()}
    return v


def read_timeline(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:  # a line cut off by a crash
            continue
    return out


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5, cwd=Path(__file__).parent).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def source_fingerprint() -> str:
    """Identify the tested application source even before its acceptance commit."""
    import hashlib

    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".js", ".css", ".html", ".yaml", ".md"}:
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _experience_snapshot(store: Any) -> dict[str, Any] | None:
    """The experience book as GET /api/experience shows it at the end of the run."""
    try:
        from qumus2.records.experience import ExperienceBook
    except ImportError:
        return None
    try:
        return ExperienceBook(store).summary()
    except Exception:  # noqa: BLE001 - a recording without the page beats no recording
        return None


def build_recording(store: Any, timeline: list[dict[str, Any]], *, after_id: int = 0,
                    before_id: int | None = None, title: str = "", description: str = "",
                    meta: dict[str, Any] | None = None, layout: dict[str, Any] | None = None,
                    experience: dict[str, Any] | None = None) -> dict[str, Any]:
    """The recording of the events after `after_id` (up to `before_id`), as a
    dict: meta, layout, events with their wall times, bench frames, the jobs
    they ran, the plan history. Paths stay as the store has them (export_recording
    copies the photos and scrubs; the live page's History plays it as is)."""
    limit = 10_000_000 if before_id is None else max(0, before_id - after_id)
    events = [e.model_dump() for e in store.events_after(after_id, limit=limit)]
    if before_id is not None:
        events = [e for e in events if e["id"] <= before_id]
    used = {e["run_id"] for e in events if e.get("run_id")}
    runs = {}
    for r in store.list_runs():
        if r["id"] in used or before_id is None:
            runs[r["id"]] = {**r, "steps": store.get_steps(r["id"])}
    projects = store.list_projects()
    plans = store.plan_history(projects[0]["id"]) if projects else []
    if experience is None:
        experience = _experience_snapshot(store)

    # wall time of each event: the first timeline entry that covers its id
    if layout is None:
        layout = next((t["layout"] for t in reversed(timeline) if "layout" in t), None)
    if layout is None:
        layout = {}  # no station layout was recorded with this run
    top = before_id if before_id is not None else float("inf")
    tl = [t for t in timeline if t.get("id", 0) >= after_id and not t.get("start")]
    frames = [{"id": t["id"], "bench": t["bench"]} for t in tl if "bench" in t and t["id"] <= top]
    walls = sorted((t["id"], t["wall"]) for t in tl)
    j = 0
    for e in events:
        while j < len(walls) and walls[j][0] < e["id"]:
            j += 1
        e["wall"] = walls[j][1] if j < len(walls) else (walls[-1][1] if walls else None)
    # an event without timeline data (old store): keep order, no timing
    known = [e["wall"] for e in events if e["wall"] is not None]

    costs = [float(e["payload"].get("cost_usd") or 0) for e in events
             if e["kind"] == "agent.turn_end"]
    # only the SDK runtime ends turns with agent.turn_end; else the startup notice says
    mode = "scripted"
    for e in events:
        if e["kind"] == "notification" and str(e["payload"].get("text", "")).startswith("agents:"):
            mode = str(e["payload"]["text"]).split(":", 1)[1].split()[0]
    if any(e["kind"] == "agent.turn_end" for e in events):
        mode = "sdk"
    first_user = next((e["payload"].get("text", "") for e in events
                       if e["kind"] == "user.message"), "")
    rec = {
        "format": FORMAT, "version": VERSION,
        "meta": {
            "title": title or first_user[:80] or "Recorded run",
            "description": description,
            "agents": mode,
            "models": recorded_models(events),
            "provider": next((e["payload"]["provider"] for e in reversed(events)
                              if e["kind"] == "agent.turn_end"
                              and e["payload"].get("provider")),
                             "claude" if mode == "sdk" else "scripted"),
            "recorded_at": (datetime.fromtimestamp(known[0], UTC).isoformat(timespec="seconds")
                            if known else None),
            "duration_s": round(known[-1] - known[0], 1) if len(known) > 1 else None,
            "cost_usd": round(sum(costs), 2) if costs else 0.0,
            "cost_basis": next((e["payload"].get("cost_basis") for e in events
                                 if e["kind"] == "agent.turn_end"), None),
            "n_events": len(events),
            "git_commit": _git_commit(),
            "application_source_sha256": source_fingerprint(),
            **(meta or {}),
        },
        "layout": layout,
        "events": events,
        "frames": frames,
        "runs": runs,
        "plans": plans,
    }
    if experience is not None:
        rec["experience"] = experience
    return rec


def clone_file(src: Path, dst: Path) -> str:
    """Put `src` at `dst` without a second copy of its bytes where possible.

    macOS APFS: clonefile (copy-on-write, like Finder's duplicate). Elsewhere
    a hard link on the same volume. Plain copy only as the last resort. Run
    photos are never rewritten, so a shared copy cannot change later (#203).
    Returns "clone", "link" or "copy".
    """
    src, dst = Path(src), Path(dst)
    if sys.platform == "darwin":
        import ctypes

        try:
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            if libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
                return "clone"
        except (OSError, AttributeError):
            pass
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def export_recording(db_path: str | Path, out_dir: str | Path, *, title: str = "",
                     description: str = "", meta: dict[str, Any] | None = None,
                     layout: dict[str, Any] | None = None,
                     experience: dict[str, Any] | None = None,
                     after_id: int = 0, allow_lab_photos: bool = False,
                     extra_roots: tuple[str | Path, ...] | list[str | Path] = ()) -> Path:
    """Write `out_dir/recording.json` + `out_dir/media/` (see module doc).

    `layout` is the bench layout (lab.bench); `after_id` skips earlier events
    (e.g. a previous session in the same database). Real microscope photos the
    testbench replayed (`images/microscope/replay/`, v1 lab photos) are lab
    data: the export refuses them unless `allow_lab_photos`. `extra_roots`:
    other run folders whose photos the records may name (a run resumed from a
    save point keeps the earlier photos in the original workspace); the
    resumed_from.json chain next to the database is followed automatically."""
    from qumus2.runtime.store import RunStore

    db_path = Path(db_path)
    data_root = db_path.parent.resolve()
    from qumus2.stations.mirror import earlier_workspaces

    roots = [data_root, *(Path(r).resolve() for r in extra_roots)]
    # a resumed run names photos that stay in the runs it was resumed from
    roots += [w for w in earlier_workspaces(data_root) if w not in roots]

    def root_of(p: Path) -> Path | None:
        return next((r for r in roots if p.is_relative_to(r)), None)

    out = Path(out_dir)
    tmp = out.with_name(out.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "media").mkdir(parents=True)
    final, out = out, tmp
    store = RunStore(db_path)
    try:
        rec = build_recording(store, read_timeline(timeline_path(db_path)), after_id=after_id,
                              title=title, description=description, meta=meta,
                              layout=layout, experience=experience)
    finally:
        store.close()

    # New microscope fields must keep their own pixels. A live polling asset
    # is overwritten and would leak the final field into every earlier event.
    for event in rec["events"]:
        preview = event.get("payload", {}).get("microscope_preview")
        if event.get("kind") == "lab.frame" and isinstance(preview, dict):
            image = preview.get("image")
            if image and Path(str(image)).stem.endswith("_live"):
                raise RuntimeError("microscope preview events require immutable image snapshots")

    # stacking motion frames: one H.264 clip per stamp pass instead of photos
    def run_file(image: str) -> Path | None:
        p = Path(str(image))
        try:
            p = resolve_run_path(p if p.is_absolute() else data_root / p)
        except OSError:
            return None
        return p if p.is_file() and root_of(p) is not None else None

    from qumus2.ui.stamp_video import pack_stamp_videos

    pack_stamp_videos(rec["events"], run_file, out / "media")

    # photos: copy every referenced image file into media/ and point there
    copied: dict[str, str] = {}

    def media(s: str) -> str:
        if not s.lower().endswith(IMAGE_SUFFIXES) or "\n" in s or len(s) > 400:
            return s
        p = Path(s)
        p = (p if p.is_absolute() else data_root / p)
        try:
            p = resolve_run_path(p)
        except OSError:
            return s
        root = root_of(p)
        if not p.is_file() or root is None:
            return s
        if "replay" in p.relative_to(root).parts[:-1] and not allow_lab_photos:
            raise RuntimeError(f"{p.name} is a real lab photo (microscope replay); recordings "
                               "may contain simulated pictures only")
        if str(p) not in copied:
            name = p.name
            if (out / "media" / name).exists():
                name = f"{len(copied)}_{name}"
            clone_file(p, out / "media" / name)
            copied[str(p)] = f"media/{name}"
        return copied[str(p)]

    secrets = [v for k in SECRET_ENV if len(v := os.environ.get(k, "")) >= 8]

    def clean(v: Any) -> Any:
        return _walk(_walk(v, media), lambda s: scrub_text(s, secrets))

    rec = clean(rec)
    for event in rec["events"]:
        preview = event.get("payload", {}).get("microscope_preview")
        if event.get("kind") == "lab.frame" and isinstance(preview, dict):
            image = preview.get("image")
            if image and not str(image).startswith("media/"):
                raise RuntimeError("microscope preview snapshot is missing or outside run data")
    text = json.dumps(rec, separators=(",", ":"), default=str)
    for v in secrets:
        if v in text:
            raise RuntimeError("export still contains a key; not written")
    for r in SECRET_RES:
        if r.search(text):
            raise RuntimeError(f"export still contains something key-like ({r.pattern}); "
                               "not written")
    (out / "recording.json").write_text(text, encoding="utf-8")
    if final.exists():
        shutil.rmtree(final)
    out.rename(final)
    return final


# ----- one line per run (the History page) ---------------------------------

#: a job that ended like this needs a person (or stopped the run)
_WAITS = {"waiting_human", "parked"}


#: a jump of the lab clock longer than this between two events is not lab work:
#: a run store kept across serves (or one older than the session marks) mixes
#: clocks (each testbench serve starts its own, older stores stamped wall time)
LAB_GAP_MAX_S = 3600.0


def lab_seconds(ts: list[float]) -> float:
    """Lab time of events in order: the sum of the forward steps of their clock,
    without the jumps between clocks (backwards, or longer than LAB_GAP_MAX_S)."""
    return round(sum(d for a, b in zip(ts, ts[1:], strict=False) if 0 < (d := b - a) <= LAB_GAP_MAX_S), 1)


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """What a run asked for and how it ended, from its events alone (the
    History list): the request, the outcome, what it made, its lab time and
    cost. The page's Results use the same rules (static/results.js)."""
    request, plan, jobs = "", [], {}
    stopped, error, asked = False, False, False
    devices, flakes, chips, faults = set(), set(), set(), []
    cost = 0.0
    cost_basis = None
    ts = [e["ts"] for e in events if isinstance(e.get("ts"), (int, float))]
    for e in events:
        k, p = e["kind"], e.get("payload") or {}
        if k == "user.message" and p.get("kind", "message") == "message":
            request = request or str(p.get("text") or "")
            asked, error = True, False
        elif k in ("plan.updated", "plan.step"):
            plan = p.get("plan") or plan
        elif k == "job.submitted" and e.get("run_id"):
            jobs.setdefault(e["run_id"], "queued")
        elif k == "job.status" and e.get("run_id"):
            jobs[e["run_id"]] = p.get("status")
        elif k == "system.stopped":
            stopped = True
        elif k == "system.continued":
            stopped = False
        elif k == "agent.error":
            error = True
        elif k == "agent.turn_end":
            cost += float(p.get("cost_usd") or 0)
            error = bool(p.get("is_error")) and p.get("reason") != "interrupted"
            cost_basis = cost_basis or p.get("cost_basis")
        elif k == "fault.injected":
            faults.append(str(p.get("fault") or ""))
        elif k == "sample.updated" and asked:
            # only what the run did: the ledger's seeding at startup comes before any request
            kind, status = p.get("kind"), p.get("status")
            if kind == "device":
                devices.add(p.get("id"))
            elif kind == "flake":
                flakes.add(p.get("id"))
            elif kind == "chip" and status not in (None, "bare", "patterned"):
                chips.add(p.get("id"))
    ends = list(jobs.values())
    if any(s in _WAITS for s in ends):
        outcome = "needs a person"
    elif stopped:
        outcome = "stopped"
    elif error:
        outcome = "error"
    elif plan and all((st.get("status") or "pending") in ("done", "skipped") for st in plan):
        outcome = "done"
    elif (any(st.get("owner") == "human" and st.get("status") in ("running", "in_progress") for st in plan)
          and all(st.get("status") in ("done", "skipped") for st in plan if st.get("owner") != "human")
          and not any(s in ("queued", "running", "paused") for s in ends)):
        outcome = "awaiting review"
    elif "failed" in ends:
        outcome = "failed"
    elif not plan and not jobs:
        outcome = "answered" if request else "nothing asked"
    else:
        outcome = "unfinished"
    return {
        "request": request, "outcome": outcome, "steps": len(plan),
        "jobs": len(jobs), "devices": sorted(devices),
        "flakes": len(flakes), "chips": len(chips), "faults": faults,
        "lab_s": lab_seconds(ts),
        "cost_usd": round(cost, 2),
        "cost_basis": cost_basis,
    }


def load_recording(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if p.is_dir():
        p = p / "recording.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    if rec.get("format") != FORMAT:
        raise ValueError(f"{p} is not a Qumus recording")
    return rec


def check_recording(path: str | Path) -> list[str]:
    """Problems that would break or mislead the replay page; [] when fine.
    Used by the recording scripts and tests on machines without a browser."""
    p = Path(path)
    try:
        rec = load_recording(p)
    except (OSError, ValueError) as e:
        return [str(e)]
    folder = p if p.is_dir() else p.parent
    out: list[str] = []
    events = rec.get("events") or []
    if not events:
        out.append("no events")
    ids = [e.get("id") for e in events]
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        out.append("event ids are not unique and increasing")
    walls = [e.get("wall") for e in events]
    if any(w is None for w in walls):
        out.append(f"{sum(w is None for w in walls)} event(s) without wall time (no timeline)")
    elif walls != sorted(walls):
        out.append("wall times go backwards")
    if not rec.get("layout"):
        out.append("no bench layout")
    frames = rec.get("frames") or []
    if not frames:
        out.append("no bench frames (the bench will look empty)")
    for f in frames:
        b = f.get("bench") or {}
        if not isinstance(b.get("objects"), list) or not isinstance(b.get("devices"), list):
            out.append(f"bench frame after event {f.get('id')} lacks objects/devices")
            break
    # Material, target and device photos can be nested in sample props or run
    # results. Checking only payload.images misses precisely those UI pictures.
    missing: set[str] = set()

    def check_media(value: str) -> str:
        if value.startswith("media/") and value.lower().endswith(IMAGE_SUFFIXES + VIDEO_SUFFIXES):
            file = (folder / value).resolve()
            if not file.is_relative_to((folder / "media").resolve()) or not file.is_file():
                missing.add(value)
        return value

    _walk(rec, check_media)
    for e in events:
        for img in (e.get("payload") or {}).get("images") or []:
            if not str(img).startswith("media/"):
                missing.add(str(img))
    if missing:
        out.append(f"{len(missing)} photo(s) missing, e.g. {sorted(missing)[0]}")
    for run_id in {e.get("run_id") for e in events if e.get("run_id")}:
        if run_id not in (rec.get("runs") or {}):
            out.append(f"job {run_id} has events but no run record")
            break
    return out
