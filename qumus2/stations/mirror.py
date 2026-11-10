"""The brain's copy of the laboratory's records, and the way back.

The laboratory (simulated or real) owns the physical world: runs and steps of
jobs, the samples it creates or moves, reservations, lab events and pictures.
The brain owns projects, plans, instructions, experience and agent events. Each
side keeps its OWN SQLite store; they meet only over contract v1 HTTP:

    lab -> brain   GET /api/records?since=<seq>   (one module, stations.yaml records_from)
                   {"changes": [{"seq", "table", "key", "row"|null}], "next": <seq>}
                   table: sample | reservation | run | step | event
    brain -> lab   command put_records {"project": [...], "samples": [...],
                   "instructions": [...], "reservations": []}  (before every job command
                   and after brain tools write samples)
    pictures       GET /api/images/<image_id>; in records a picture is the string
                   "qumus-image:<image_id>". The mirror downloads it into the brain's
                   image folder and writes the local path; put_records maps it back.

Agent tools keep reading `store.get_sample`, `store.get_run`, `store.get_steps`,
... from the brain's store, which this mirror keeps up to date. Lab events are
re-emitted in the brain's store with the brain's ids and `lab_event_id` in the
payload (the UI tails one timeline).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qumus2.contract import ContractClient, ContractError

log = logging.getLogger(__name__)

IMAGE_PREFIX = "qumus-image:"
TABLES = ("sample", "reservation", "run", "step", "event")
CURSOR_META = "mirror_records_cursor"
LAB_ID_META = "mirror_records_lab_id"
#: brain events that mean "the brain changed a sample": pushed with put_records
SAMPLE_EVENTS = ("sample.updated", "sample.removed")


def _j(value: Any) -> str:
    return json.dumps(value, default=str)


class ImageCache:
    """Pictures fetched from the stations, kept under the brain's image folder."""

    def __init__(self, root: str | Path | None, client: ContractClient | None) -> None:
        self.root = Path(root).resolve() if root is not None else None
        self.client = client
        self._lock = threading.Lock()

    def local_path(self, image_id: str) -> Path | None:
        if self.root is None:
            return None
        clean = Path(*[p for p in Path(image_id).parts if p not in ("", ".", "..", "/")])
        return self.root / clean

    def fetch(self, image_id: str) -> str:
        """Local path of the picture (downloaded once); the reference itself when offline."""
        path = self.local_path(image_id)
        if path is None or self.client is None:
            return IMAGE_PREFIX + image_id
        if path.exists():
            return str(path)
        url = self.client.url("api/images/" + urllib.parse.quote(image_id))
        try:
            with self.client._opener.open(urllib.request.Request(url), timeout=self.client.timeout_s) as r:
                data = r.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log.warning("image %s not fetched: %s", image_id, exc)
            return IMAGE_PREFIX + image_id
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        return str(path)

    def to_local(self, value: Any) -> Any:
        """Every "qumus-image:<id>" string inside `value` -> local path."""
        if isinstance(value, str):
            return self.fetch(value[len(IMAGE_PREFIX):]) if value.startswith(IMAGE_PREFIX) else value
        if isinstance(value, dict):
            if set(value) >= {"image_id"} and isinstance(value.get("image_id"), str) and "path" not in value:
                # {"image_id": ...} form (interface doc v1): keep the id, add the local path
                return {**{k: self.to_local(v) for k, v in value.items()},
                        "path": self.fetch(value["image_id"])}
            return {k: self.to_local(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.to_local(v) for v in value]
        return value

    def to_remote(self, value: Any) -> Any:
        """Local picture paths under the image folder -> "qumus-image:<id>"."""
        if self.root is None:
            return value
        if isinstance(value, str):
            try:
                p = Path(value)
                if p.is_absolute() and p.resolve().is_relative_to(self.root):
                    return IMAGE_PREFIX + p.resolve().relative_to(self.root).as_posix()
            except (OSError, ValueError):
                return value
            return value
        if isinstance(value, dict):
            return {k: self.to_remote(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.to_remote(v) for v in value]
        return value


class RecordMirror:
    """Pull the station's record feed into the brain store; push brain writes back."""

    def __init__(self, store: Any, client: ContractClient, *, image_dir: str | Path | None = None,
                 poll_s: float = 0.5, push_client: ContractClient | None = None,
                 on_error: Callable[[str], None] | None = None) -> None:
        self.store = store
        self.client = client
        self.push_client = push_client or client
        self.images = ImageCache(image_dir, client)
        self.poll_s = poll_s
        self.on_error = on_error
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cursor = self._load_cursor()
        self._pushed_through = store.last_event_id()  # brain events already pushed
        self.online = False
        self.last_error: str | None = None
        self.applied = 0

    # ----- cursor -------------------------------------------------------------
    def _load_cursor(self) -> Any:
        raw = self.store.get_meta(CURSOR_META)
        if raw is None:
            return 0
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    def _save_cursor(self) -> None:
        self.store.set_meta(CURSOR_META, _j(self._cursor))

    # ----- pull ---------------------------------------------------------------
    def sync(self, *, max_pages: int = 50) -> int:
        """Apply every change the station has; returns how many were applied."""
        total = 0
        with self._sync_lock:
            for _ in range(max_pages):
                try:
                    page = self.client._request(
                        "GET", "api/records?since=" + urllib.parse.quote(str(self._cursor)))
                except ContractError as exc:
                    self._fail(f"records feed: {exc}")
                    return total
                if not isinstance(page, dict):
                    self._fail("records feed: reply is not an object")
                    return total
                if self._stop.is_set() and threading.current_thread() is self._thread:
                    return total  # closing: leave the store alone
                lab_id = page.get("lab_id")
                if lab_id is not None and str(lab_id) != (self.store.get_meta(LAB_ID_META) or str(lab_id)):
                    # a different laboratory store (e.g. a fresh simulated lab): start over;
                    # the old lab's ledger no longer describes the bench
                    self._cursor = 0
                    self.store._x("DELETE FROM sample")
                    self.store._x("DELETE FROM reservation")
                    self.store.set_meta(LAB_ID_META, str(lab_id))
                    self._save_cursor()
                    continue
                if lab_id is not None:
                    self.store.set_meta(LAB_ID_META, str(lab_id))
                changes = page.get("changes") or []
                for change in changes:
                    self._apply(change)
                total += len(changes)
                nxt = page.get("next", self._cursor)
                if changes or nxt != self._cursor:
                    self._cursor = nxt
                    self._save_cursor()
                self.online, self.last_error = True, None
                if not changes:
                    break
        self.applied += total
        return total

    def _fail(self, message: str) -> None:
        if self.last_error != message:
            log.warning("mirror: %s", message)
            if self.on_error is not None:
                self.on_error(message)
        self.online, self.last_error = False, message

    def _apply(self, change: dict[str, Any]) -> None:
        table, key, row = change.get("table"), change.get("key"), change.get("row")
        if table not in TABLES:
            return
        if row is not None:
            row = self.images.to_local(row)
        getattr(self, f"_apply_{table}")(str(key) if key is not None else "", row)

    def _apply_sample(self, key: str, row: dict[str, Any] | None) -> None:
        if row is None:
            self.store._x("DELETE FROM sample WHERE id = ?", (key,))
            return
        self.store._x(
            "INSERT INTO sample (id, kind, location, status, props_json, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, location=excluded.location, "
            "status=excluded.status, props_json=excluded.props_json, updated_at=excluded.updated_at",
            (row.get("id") or key, row["kind"], row.get("location"), row.get("status"),
             _j(row.get("props") or {}), row.get("updated_at")))

    def _apply_reservation(self, key: str, row: dict[str, Any] | None) -> None:
        if row is None:
            kind, _, rkey = key.partition(":")
            if rkey:
                self.store._x("DELETE FROM reservation WHERE kind = ? AND key = ?", (kind, rkey))
            else:
                self.store._x("DELETE FROM reservation WHERE key = ?", (key,))
            return
        rkey = row.get("key") or key.partition(":")[2] or key
        self.store._x(
            "INSERT INTO reservation (kind, key, run_id, flow, note, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(kind, key) DO UPDATE SET run_id=excluded.run_id, flow=excluded.flow, "
            "note=excluded.note, created_at=excluded.created_at",
            (row["kind"], rkey, row["run_id"], row.get("flow"), row.get("note"), row.get("created_at")))

    def _apply_run(self, key: str, row: dict[str, Any] | None) -> None:
        run_id = (row or {}).get("id") or key
        if row is None:
            self.store._x("DELETE FROM run WHERE id = ?", (run_id,))
            return
        old = self.store.get_run(run_id)
        # brain-written meta (purpose, target, review) survives lab updates of the same run
        meta = {**((old or {}).get("meta") or {}), **(row.get("meta") or {})}
        self.store._x(
            "INSERT INTO run (id, project_id, flow, params_json, status, created_at, updated_at, "
            "result_json, error_json, requested_by, bench, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, flow=excluded.flow, "
            "params_json=excluded.params_json, status=excluded.status, updated_at=excluded.updated_at, "
            "result_json=excluded.result_json, error_json=excluded.error_json, "
            "requested_by=excluded.requested_by, bench=excluded.bench, meta_json=excluded.meta_json",
            (run_id, row.get("project_id"), row["flow"], _j(row.get("params") or {}), row["status"],
             row.get("created_at"), row.get("updated_at"), _j(row.get("result")), _j(row.get("error")),
             row.get("requested_by"), row.get("bench") or "sim", _j(meta)))

    def _apply_step(self, key: str, row: dict[str, Any] | None) -> None:
        run_id, _, seq = key.rpartition(":")
        if row is not None:
            run_id, seq = row.get("run_id") or run_id, row.get("seq", seq)
        if row is None:
            self.store._x("DELETE FROM step WHERE run_id = ? AND seq = ?", (run_id, int(seq)))
            return
        self.store._x(
            "INSERT INTO step (run_id, seq, name, status, expected_json, measurements_json, corrections, "
            "safe_to_resume, verifiable, note, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id, seq) DO UPDATE SET name=excluded.name, status=excluded.status, "
            "expected_json=excluded.expected_json, measurements_json=excluded.measurements_json, "
            "corrections=excluded.corrections, safe_to_resume=excluded.safe_to_resume, "
            "verifiable=excluded.verifiable, note=excluded.note, started_at=excluded.started_at, "
            "finished_at=excluded.finished_at",
            (run_id, int(seq), row["name"], row["status"], _j(row.get("expected")),
             _j(row.get("measurements")), int(row.get("corrections") or 0),
             int(bool(row.get("safe_to_resume", True))), int(bool(row.get("verifiable", True))),
             row.get("note"), row.get("started_at"), row.get("finished_at")))

    def _apply_event(self, key: str, row: dict[str, Any] | None) -> None:
        if row is None:
            return  # the timeline is append-only
        payload = dict(row.get("payload") or {})
        payload["lab_event_id"] = row.get("id", key)
        if row.get("ts") is not None:
            payload.setdefault("lab_ts", row.get("ts"))
        self.store.emit(row["kind"], payload, run_id=row.get("run_id"), agent=row.get("agent"))

    # ----- push ---------------------------------------------------------------
    def _changed_samples(self) -> tuple[list[dict[str, Any]], list[str], int]:
        """Samples the brain itself changed since the last push (its own sample events)."""
        last = self._pushed_through
        changed: dict[str, bool] = {}
        through = last
        while True:
            events = self.store.events_after(through, limit=500)
            if not events:
                break
            for e in events:
                through = e.id
                if e.kind in SAMPLE_EVENTS and "lab_event_id" not in e.payload and e.payload.get("id"):
                    changed[str(e.payload["id"])] = e.kind == "sample.updated"
        rows, removed = [], []
        for sid, updated in changed.items():
            row = self.store.get_sample(sid)
            if row is not None:
                rows.append(self.images.to_remote(row))
            elif not updated:
                removed.append(sid)
        return rows, removed, through

    def push(self, *, projects: bool = True) -> dict[str, Any]:
        """Send the brain's records the laboratory reads (put_records)."""
        with self._sync_lock:
            samples, removed, through = self._changed_samples()
            body: dict[str, Any] = {"samples": samples, "reservations": []}
            if removed:
                body["removed_samples"] = removed
            if projects:
                body["project"] = [self.images.to_remote({"id": p["id"], "name": p["name"], "state": p["state"]})
                                   for p in self.store.list_projects()]
                body["instructions"] = [self._instruction_row(r) for r in self._instructions()]
            try:
                receipt = self.push_client.command("put_records", body)
            except ContractError as exc:
                self._fail(f"put_records: {exc}")
                raise
            self._pushed_through = through
            return {"command_id": receipt.command_id, "samples": len(samples)}

    def _instructions(self) -> list[Any]:
        return self.store._x("SELECT * FROM instruction ORDER BY ts").fetchall()

    @staticmethod
    def _instruction_row(r: Any) -> dict[str, Any]:
        return {"id": r["id"], "ts": r["ts"], "project_id": r["project_id"], "source": r["source"],
                "kind": r["kind"], "text": r["text"], "status": r["status"], "note": r["note"]}

    # ----- background ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> RecordMirror:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="qumus-record-mirror", daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception as exc:  # noqa: BLE001 - the mirror must keep running
                self._fail(f"{type(exc).__name__}: {exc}")
            self._stop.wait(self.poll_s)

    def wait_for(self, predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
        """Sync until `predicate()` holds (e.g. a new run row arrived)."""
        deadline = time.monotonic() + timeout_s
        while True:
            self.sync()
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def close(self) -> None:
        """Stop the background thread; returns once no sync touches the store any more."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.client.timeout_s + 2.0)
        with self._sync_lock:  # a sync from another thread finishes first
            pass
