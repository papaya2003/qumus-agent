"""A tiny in-process laboratory for tests and offline brain demos.

Three contract v1 stations (exfoliation, stacking, reactor) on loopback that
share one lab-side RunStore, serve the record feed, pictures, put_records and
the [Q-sim] page endpoints, and finish every job at once with an outcome the
test chooses. It simulates no physics: use qumus-simlab for that.

    with FakeLab(bench=hallbar_bench, samples=seed_rows) as lab:
        services = build_services(db, stations=lab.stations())
        lab.outcomes["search_chip"] = lambda run, params: {"status": "succeeded", "result": {...}}

`FakeLab(contract2=True)` also serves the commands as one contract 2 station (lab-contract's
labkit, on its own port; needs a lab-contract checkout) and `stations()` points every module's
`entry:` at it, so the brain commands it through labclient while the record feed, pictures and
put_records stay on the v1 ports (as with qumus-simlab).
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qumus2.contract.fake_server import FakeCommand, FakeService
from qumus2.core.types import new_id
from qumus2.runtime.store import RunStore
from qumus2.stations.catalog import KNOWN_FLOWS
from qumus2.stations.config import Stations, from_urls

CONTROLS = ("job_cancel", "job_retry", "job_reverify", "job_resume", "job_resume_all",
            "job_review_waiting", "job_status", "complete_experiment")
OPEN_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}
#: contract 2 params schemas are closed: the parameters the brain's tests send
CONTRACT2_PARAMS = ("job", "run_id", "project_id", "summary", "chip_id", "chip_ids", "material",
                    "num_chips", "flake_ids", "target_chip_id", "failure_event_id", "reviewer",
                    "reason", "regions", "slot", "samples", "removed_samples", "reservations",
                    "instructions", "project")

Outcome = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class FakeLab:
    def __init__(self, *, bench: dict[str, Any] | None = None,
                 samples: list[dict[str, Any]] | None = None,
                 image_root: str | Path | None = None, modules: tuple[str, ...] = ("exfoliation", "stacking", "reactor"),
                 contract2: bool = False) -> None:
        self.store = RunStore(":memory:")
        self.bench = bench or {"slots": {}, "cameras": {}}
        self.lab_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.changes: list[dict[str, Any]] = []
        self.images: dict[str, bytes] = {}
        self.image_root = Path(image_root) if image_root else None
        self.projects: dict[str, dict[str, Any]] = {}
        self.instructions: dict[str, dict[str, Any]] = {}
        self.put_records: list[dict[str, Any]] = []
        self.commands: list[tuple[str, str, dict[str, Any]]] = []
        self.faults: list[dict[str, Any]] = []
        self.bench_state: dict[str, Any] = {"devices": [], "objects": [], "twin": {}, "stamp": None, "faults": {}}
        self.previews: dict[str, dict[str, Any]] = {}
        #: flow -> hook(run, params) -> {"status", "result", "error", "steps": [...], "samples": [...]}
        self.outcomes: dict[str, Outcome] = {}
        self.services: dict[str, FakeService] = {}
        for module in modules:
            cmds: dict[str, FakeCommand] = {}
            for flow, owner in KNOWN_FLOWS.items():
                if owner == module:
                    cmds[flow] = FakeCommand(params=OPEN_SCHEMA, motion=True, duration_s=0.01,
                                             run=self._job(module, flow), extra={"job": True})
            for name in CONTROLS:
                cmds[name] = FakeCommand(params=OPEN_SCHEMA, motion=False, duration_s=0.0,
                                         any_client=name in ("job_status", "job_cancel", "job_review_waiting"),
                                         run=self._control(name))
            cmds["put_records"] = FakeCommand(params=OPEN_SCHEMA, any_client=True, motion=False,
                                              duration_s=0.0, run=self._put_records)
            svc = FakeService(module, commands=cmds)
            svc.route_hook = self._route
            self.services[module] = svc
        for row in samples or []:
            self.put_sample(row)
        self.station2: Any = self._contract2() if contract2 else None
        self._server2: Any = None

    def _contract2(self) -> Any:
        """The same commands as one contract 2 station (labkit)."""
        from qumus2.stations.link import load_labclient

        if load_labclient() is None:
            raise RuntimeError("FakeLab(contract2=True) needs lab-contract (labclient/labkit)")
        from labkit.service import Failed, Service
        from labkit.station import Station

        station = Station("fake-lab")
        for module, svc in self.services.items():
            s2 = Service(module, lease_s=3.0)
            for name, cmd in svc.commands.items():
                job = bool((cmd.extra or {}).get("job"))

                def fn(_run: Any, params: dict[str, Any], _cmd: Any = cmd) -> dict[str, Any]:
                    try:
                        return _cmd.run(dict(params), None) or {}
                    except (KeyError, ValueError) as e:
                        raise Failed(str(e)) from e

                s2.add(name, fn, summary=name, motion=job, job=job, lease=not cmd.any_client,
                       deadline_s=(3600.0, 3600.0) if job else None)
                s2.commands[name].spec["params"] = {
                    "type": "object", "additionalProperties": False,
                    "properties": {p: {} for p in CONTRACT2_PARAMS}}
            station.add(s2)
        return station

    # ----- lifecycle ----------------------------------------------------------
    def start(self) -> FakeLab:
        for svc in self.services.values():
            svc.start()
        if self.station2 is not None:
            self._server2 = self.station2.serve()
        return self

    def close(self) -> None:
        for svc in self.services.values():
            svc.close()
        if self._server2 is not None:
            self._server2.shutdown()
            self._server2.server_close()
            self.station2.close()

    def __enter__(self) -> FakeLab:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def urls(self) -> dict[str, str]:
        return {m: svc.base_url for m, svc in self.services.items()}

    @property
    def entry(self) -> str | None:
        """The contract 2 station entry (contract2=True, after start())."""
        if self._server2 is None:
            return None
        return f"http://127.0.0.1:{self._server2.server_address[1]}/"

    def stations(self, records_from: str | list[str] = "stacking") -> Stations:
        extra = {m: {"entry": self.entry, "contract": "2"} for m in self.urls} if self.entry else None
        return from_urls(self.urls, records_from=records_from, extra=extra)

    # ----- the change log -----------------------------------------------------
    def _log(self, table: str, key: str, row: dict[str, Any] | None) -> None:
        with self.lock:
            self.changes.append({"seq": len(self.changes) + 1, "table": table, "key": key,
                                 "row": json.loads(json.dumps(row, default=str)) if row is not None else None})

    def put_sample(self, row: dict[str, Any]) -> None:
        self.store._x("INSERT OR REPLACE INTO sample (id, kind, location, status, props_json, updated_at) "
                      "VALUES (?,?,?,?,?,?)", (row["id"], row["kind"], row.get("location"), row.get("status"),
                                               json.dumps(row.get("props") or {}), row.get("updated_at")))
        self._log("sample", row["id"], self.store.get_sample(row["id"]))

    def remove_sample(self, sample_id: str) -> None:
        self.store._x("DELETE FROM sample WHERE id = ?", (sample_id,))
        self._log("sample", sample_id, None)

    def event(self, kind: str, payload: dict[str, Any] | None = None, *, run_id: str | None = None,
              agent: str | None = None) -> int:
        eid = self.store.emit(kind, payload or {}, run_id=run_id, agent=agent)
        e = self.store.events_after(eid - 1, limit=1)[0]
        self._log("event", str(eid), {"id": e.id, "ts": e.ts, "kind": e.kind, "run_id": e.run_id,
                                      "agent": e.agent, "payload": e.payload})
        return eid

    def add_image(self, image_id: str, data: bytes) -> str:
        self.images[image_id] = data
        return "qumus-image:" + image_id

    def set_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        assert run is not None
        status = fields.pop("status", None)
        if status is not None or "result" in fields or "error" in fields:
            self.store.set_run_status(run_id, status or run["status"], result=fields.get("result"),
                                      error=fields.get("error"))
        if fields.get("meta"):
            self.store.update_run_meta(run_id, **fields["meta"])
        row = self.store.get_run(run_id)
        self._log("run", run_id, row)
        return row  # type: ignore[return-value]

    def step(self, run_id: str, seq: int, name: str, status: str = "done", *,
             safe_to_resume: bool = True, measurements: Any = None) -> None:
        self.store.begin_step(run_id, seq, name, safe_to_resume=safe_to_resume)
        if status != "in_progress":
            self.store.finish_step(run_id, seq, status, measurements=measurements)
        row = next(s for s in self.store.get_steps(run_id) if s["seq"] == seq)
        self._log("step", f"{run_id}:{seq}", {**row, "run_id": run_id})

    # ----- commands -----------------------------------------------------------
    def _job(self, module: str, flow: str) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
        def run(params: dict[str, Any], _svc: Any) -> dict[str, Any]:
            job = params.pop("job", {}) or {}
            params.pop("action", None)
            with self.lock:
                self.commands.append((module, flow, dict(params)))
                rid = self.store.create_run(flow, params, project_id=job.get("project_id"),
                                            requested_by=job.get("requested_by"))
                self._log("run", rid, self.store.get_run(rid))
                hook = self.outcomes.get(flow)
                outcome = hook(self.store.get_run(rid), params) if hook else {"status": "succeeded",
                                                                               "result": {"flow": flow}}
                for i, st in enumerate(outcome.get("steps") or []):
                    self.step(rid, i, st["name"], st.get("status", "done"),
                              safe_to_resume=st.get("safe_to_resume", True))
                for row in outcome.get("samples") or []:
                    self.put_sample(row)
                for ev in outcome.get("events") or []:
                    self.event(ev["kind"], ev.get("payload"), run_id=rid)
                self.event("job.status", {"flow": flow, "status": outcome.get("status", "succeeded")}, run_id=rid)
                self.set_run(rid, status=outcome.get("status", "succeeded"), result=outcome.get("result"),
                             error=outcome.get("error"))
            return {"run_id": rid}
        return run

    def _control(self, name: str) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
        def run(params: dict[str, Any], _svc: Any) -> dict[str, Any]:
            with self.lock:
                self.commands.append(("control", name, dict(params)))
                rid = params.get("run_id")
                if name == "job_status":
                    return {"run": self.store.get_run(rid)}
                if name == "job_cancel" and rid:
                    self.set_run(rid, status="cancelled")
                if name in ("job_retry", "job_resume") and rid:
                    self.set_run(rid, status="succeeded")
                    return {"result": {"run_id": rid}}
                if name == "job_resume_all":
                    return {"result": []}
                if name == "complete_experiment":
                    return {"result": {"project_id": params.get("project_id"), "completed": True}}
                return {"result": {}}
        return run

    def _put_records(self, params: dict[str, Any], _svc: Any) -> dict[str, Any]:
        with self.lock:
            self.put_records.append(json.loads(json.dumps(params, default=str)))
            projects = params.get("project") or []
            for p in projects if isinstance(projects, list) else [projects]:
                self.projects[p["id"]] = p
            for row in params.get("samples") or []:
                self.put_sample(row)
            for sid in params.get("removed_samples") or []:
                self.remove_sample(sid)
            for row in params.get("instructions") or []:
                self.instructions[row["id"]] = row
            return {"samples": len(params.get("samples") or [])}

    # ----- extra routes ---------------------------------------------------------
    def _route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any] | None:
        if method == "GET" and path == "/api/records":
            raw = (query.get("since") or ["0"])[0]
            since = int(raw) if raw.isdecimal() else 0
            with self.lock:
                page = [c for c in self.changes if c["seq"] > since][:200]
                nxt = page[-1]["seq"] if page else max(since, 0)
            return 200, {"changes": page, "next": nxt, "lab_id": self.lab_id}
        if method == "GET" and path.startswith("/api/images/"):
            from urllib.parse import unquote

            image_id = unquote(path[len("/api/images/"):])
            data = self.images.get(image_id)
            return (200, data) if data is not None else (404, {"error": "unknown image"})
        if method == "GET" and path == "/api/sim/layout":
            from qumus2.core.bench import positions, regions

            return 200, {"bench": self.bench, "layout": {**self.bench, "regions": regions(self.bench),
                                                        "positions": positions(self.bench)}}
        if method == "GET" and path == "/api/sim/bench_state":
            return 200, self.bench_state
        if method == "GET" and path.startswith("/api/sim/preview/"):
            cam = path.rsplit("/", 1)[-1]
            return 200, self.previews.get(cam, {"image_id": None, "timestamp": None, "meta": {}})
        if path == "/api/sim/faults":
            if method == "GET":
                return 200, [{"fault": "pick_fail_next", "description": "next pick fails", "params": {}}]
            if not isinstance(body, dict) or body.get("fault") != "pick_fail_next":
                return 400, {"error": "unknown fault"}
            self.faults.append(body)
            return 200, {"ok": True}
        return None


def new_sample(kind: str, location: str | None, **props: Any) -> dict[str, Any]:
    """A sample row for FakeLab.put_sample / job outcomes."""
    return {"id": props.pop("id", None) or new_id(kind), "kind": kind, "location": location,
            "status": props.pop("status", None), "props": props}
