"""UI backend (spec 11.3). CONTRACT for the UI workstream.

The UI reads the run store and writes human instructions; it never touches
devices or agents directly.

    GET  /api/health                      {"ok": true, "pid", "mode": "scripted"|"sdk", "busy",
                                          "model_status": {state, detail, checked_at}} (state: scripted
                                          | unknown | ok | unreachable | auth_failed | error);
                                          + "cost_usd" (what this serve spent on models so far,
                                          sum of agent.turn_end cost_usd) when the store has it;
                                          + "waiting": jobs that wait for a person (or are parked)
    GET  /api/snapshot                    project, plan, runs, samples, locks, world, agents,
                                          session_start (this run = the events after it)
    GET  /api/events?after=<id>&limit=N   events newer than id (see runtime/events.py)
    GET  /api/runs/{run_id}               run + steps
    POST /api/messages {text, kind?}      kind: message (default) | plan_change | veto | stop | continue
    POST /api/server/shutdown             stop the server like Control+C (header
                                          X-Qumus-Shutdown-Token; scripts/start_qumus.py --stop)
    POST /api/stop                        Stop: interrupt agents, every job pauses at a safe point
    POST /api/continue                    Continue: paused jobs resume, agents go on
    POST /api/faults {fault, params}      testbench only: inject a fault (spec 11.2); 400 if unknown
    GET  /api/faults                      testbench only: the fault menu [{fault, description, params}]
    WS   /ws/events?after=<id>            pushes each new event as JSON (same-origin pages only)
    GET  /                                single-page front end (ui/static/index.html)

UI-only endpoints (not part of the C12 contract; other workstreams don't call them):

    GET  /api/bench                       bench layout, device states, objects, faults, sim speed
    GET  /api/plan/history                every plan revision of the project
    GET  /api/experience                  the lab's experience book (records/experience.py):
                                          {bench, real_runs, note, counts, learned,
                                          baselines, experiences, rule}; sandbox rows never
    GET  /api/laboratory                  actual wired backend, available modes, learning status
    POST /api/laboratory/mode {mode}      backend-bound selection; unavailable/busy returns 409
    POST /api/sim {speed}                 testbench: scripted-agent pacing (0.25x .. 8x)
    POST /api/model/check                check the gateway now (free, no prompt)
    GET  /api/camera/{id}/latest          what a camera shows now: {device, image (an
                                          /api/image link or null), timestamp, meta}
    GET  /api/image?path=<file>           an image file under the run-data directory
    GET  /api/history                     the earlier sessions (runs) in this run store, newest
                                          first: [{id, until, started, ended, agents, request,
                                          outcome, jobs, devices, flakes, lab_s, cost_usd, ...}]
    GET  /api/history/{id}/recording      that session as a recording (ui/recording.py), to
                                          play in the page; photos stay /api/image paths
    GET  /api/recordings, /api/recording?name=, /api/recordings/{name}/media/{file}
                                          the demo recordings under ./recordings (ui/replay.py)
    POST /api/stamps/{id}/film {by?}      a person put a new PC film on a used or torn stamp
                                          (Lab page): checked and recorded (records/stamps.py),
                                          the stamp is fresh again; 409 with the reason if not
    GET  /static/<file>                   front-end assets
"""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from qumus2.agents.roster import ALL_AGENTS, DESCRIPTIONS, UI_LABELS
from qumus2.app import Services
from qumus2.core.bench import positions, regions
from qumus2.core.run_paths import resolve_run_path
from qumus2.runtime.oscompat import interrupt_self
from qumus2.ui.presence import SHUTDOWN_TOKEN_ENV, SHUTDOWN_TOKEN_HEADER

STATIC = Path(__file__).parent / "static"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


class AgentModelChoice(BaseModel):
    model_config = {'extra': 'forbid'}
    model: str = Field(min_length=1, max_length=200)
    input_price: float = Field(ge=0, allow_inf_nan=False)
    output_price: float = Field(ge=0, allow_inf_nan=False)


class ModelChoice(AgentModelChoice):
    context_window_tokens: int | None = Field(default=None, gt=0, le=2**53 - 1, strict=True)
    agent_models: dict[str, AgentModelChoice] = Field(default_factory=dict)


class BillingModel(BaseModel):
    model: str
    usd: float = Field(ge=0, allow_inf_nan=False)
    requests: int = Field(ge=0)
    tokens: int | None = Field(default=None, gt=0)


class BillingRequest(BaseModel):
    """Manually verified, exact request bill; no account totals or credentials."""

    model_config = ConfigDict(extra='forbid')
    scope: Literal['request']
    request_id: str = Field(min_length=1, max_length=200, pattern=r'^[^\s]+$')
    gateway: Literal['https://physics.bytefuture.ai/v1']
    model: str = Field(min_length=1, max_length=200, pattern=r'^[^\s]+$')
    currency: Literal['USD']
    source: Literal['https://physics.bytefuture.ai/dashboard/usage']
    verified_at: AwareDatetime
    usd: float = Field(ge=0, allow_inf_nan=False, strict=True)


class BillingSnapshot(BaseModel):
    source_url: Literal['https://physics.bytefuture.ai/dashboard/usage']
    verified_at: str = Field(min_length=1)
    period: str
    scope: str
    total_usd: float = Field(ge=0, allow_inf_nan=False)
    by_model: list[BillingModel]
    by_request: list[BillingRequest] = Field(default_factory=list)


#: the page and its scripts are checked with the server on every load (an ETag
#: round trip), so a browser never mixes files cached before a `git pull` with
#: new ones (stage-1 review issue 2: a half-old page fell back to live mode)
NO_CACHE = {"cache-control": "no-cache"}


class FrontEndFiles(StaticFiles):
    """/static/*, revalidated on every load (NO_CACHE)."""

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        resp = super().file_response(*args, **kwargs)
        resp.headers.update(NO_CACHE)
        return resp


def mount_front_end(app: FastAPI) -> None:
    """GET / and /static/*: the single-page front end, never served from a stale cache."""

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers=NO_CACHE)

    app.mount("/static", FrontEndFiles(directory=STATIC), name="static")


class MessageIn(BaseModel):
    text: str
    kind: Literal["message", "plan_change", "veto", "stop", "continue"] = "message"


class FaultIn(BaseModel):
    fault: str  # one of the simulated lab's faults (GET /api/faults)
    params: dict[str, Any] = {}


class JobHumanReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    failure_event_id: int = Field(gt=0, strict=True)
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)


class SimIn(BaseModel):
    speed: float = Field(ge=0.25, le=8)


class LaboratoryModeIn(BaseModel):
    mode: Literal["virtual", "real"]


def laboratory_capabilities(s: Services) -> dict[str, Any]:
    """Which laboratory the brain is wired to: the stations in stations.yaml."""
    st = s.stations
    simulated = st.all_sim
    mode = "virtual" if simulated else ("real" if not st.any_sim else "mixed")
    detail = ("Point a module at a real station in stations.yaml (kind: real) and restart; "
              "the brain reaches every station only through contract v1.")
    return {"mode": mode,
            "backend": "stations",
            "stations": [s_.to_dict() for s_ in st.modules.values()],
            "modes": [{"id": "virtual", "label": "Virtual laboratory", "available": st.any_sim},
                      {"id": "real", "label": "Real laboratory", "available": not simulated, "detail": detail}],
            "learning": {"enabled": False, "status": "disabled_virtual" if simulated else "unverified_backend",
                "detail": "Learning is disabled in the virtual laboratory. Existing validated advice remains readable."
                    if simulated else "Learning is disabled until recording provenance of real stations is verified."},
            "switch_available": False, "real_setup_requirement": detail}


class MaterialReviewIn(BaseModel):
    expected_material: str = Field(min_length=1, max_length=80)
    by: str = Field(min_length=1, max_length=80)
    status: str = Field(pattern="^(confirmed|uncertain)$")
    note: str = Field(default="", max_length=500)


class FilmIn(BaseModel):
    by: str = Field("", max_length=80)  # the person's name, if they gave one


def bench_state(s: Services) -> dict[str, Any]:
    """What GET /api/bench shows besides the static layout: device states,
    objects, the held stamp, faults, busy (from the station's /api/sim/bench_state).
    The recorder saves the same dict over time, so a replay shows the bench
    exactly as the live page did."""
    from qumus2.records.lab_geometry import observed_scene

    # Bind evidence before reading any live diagnostics. Recorder snapshots
    # retain this exact prefix; synthetic state never supplies observed poses.
    through = s.store.last_event_id()
    observed = observed_scene(s.store, through_event_id=through)
    remote = s.lab.bench_state()
    objects = [dict(o) for o in remote.get("objects") or []]
    status = {x["id"]: x["status"] for x in s.store.list_samples(kind="stamp")}
    for o in objects:
        if o.get("kind") == "stamp":
            o["status"] = status.get(o["id"], o.get("status") or "fresh")
    out = {
        "devices": remote.get("devices") or [],
        "objects": objects,
        "twin": {**(remote.get("twin") or {}), "observed_scene": observed},
        "stamp": remote.get("stamp"),
        "faults": remote.get("faults") or {},
        "busy": s.runtime.busy,
    }
    if remote.get("error"):
        out["station_error"] = remote["error"]
    return out


def _same_origin(ws: WebSocket) -> bool:
    """Browsers send Origin on websockets; a page from another site must not
    read the event stream. Clients without Origin (tests, scripts) are fine."""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    host = ws.headers.get("host", "")
    return origin.split("://", 1)[-1].rstrip("/") == host


def bench_layout(bench: dict[str, Any]) -> dict[str, Any]:
    """bench.yaml plus every named region and place in the world frame."""
    return {**bench, "regions": regions(bench), "positions": positions(bench)}


def create_app(services: Services, *, start_driver: bool = True,
               data_dir: str | Path | None = None,
               recordings_dir: str | Path = "recordings",
               stop_when_closed_s: float | None = None) -> FastAPI:
    s = services
    if data_dir is None:
        data_dir = Path(s.store.path).parent if s.store.path != ":memory:" else Path("runs")
    data_root = Path(data_dir).resolve()
    base_delay = float(getattr(s.runtime, "delay", 0.0) or 0.0)
    sim = {"speed": 1.0}
    cache: dict[str, Any] = {}

    model_health = None
    catalog = None
    if getattr(s.runtime, "provider", None) == "openai":
        from qumus2.agents.model_settings import ModelCatalog
        from qumus2.agents.openai_health import OpenAIHealth

        model_health = OpenAIHealth(s.runtime.creds)
        catalog = ModelCatalog(s.runtime.creds)

    presence = None
    if stop_when_closed_s is not None:
        from qumus2.ui.presence import IdleStop

        def working() -> bool:
            return bool(s.runtime.busy) or any(r.get("status") in ("queued", "running")
                                               for r in s.store.list_runs())

        presence = IdleStop(stop_when_closed_s, working)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        checker = idle = None
        if start_driver:
            s.driver.start()
            if model_health is not None:
                checker = asyncio.create_task(model_health.run_forever())
        if presence is not None:
            idle = asyncio.create_task(presence.run_forever())
        yield
        for task in (checker, idle):
            if task is not None:
                task.cancel()
        await s.runtime.close()

    app = FastAPI(title="Qumus v2", lifespan=lifespan)
    app.state.presence = presence

    if presence is not None:
        @app.middleware("http")
        async def page_presence(request, call_next):  # type: ignore[no-untyped-def]
            # pages send a Referer on their own requests; the launcher's probe does not
            if request.url.path.startswith("/api/") and request.headers.get("referer"):
                presence.touched()
            return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """Current gateway reachability, independently of model capability."""
        out: dict[str, Any] = {"ok": True, "pid": os.getpid(), "mode": s.runtime.mode,
                              "busy": s.runtime.busy,
                              "laboratory": laboratory_capabilities(s),
                              "model_status": model_health.state if model_health is not None
                              else {"state": "scripted"}}
        out["workspace"] = s.lab.bench.get("name") or "Hall-bar laboratory"
        out["stations"] = {m: {"url": st.url, "kind": st.kind} for m, st in s.stations.modules.items()}
        out["mirror"] = {"online": s.jobs.mirror.online if s.jobs.mirror else False,
                         "error": s.jobs.mirror.last_error if s.jobs.mirror else None}
        if getattr(s.runtime, "provider", None) == "openai":
            out.update(provider="openai", model=s.runtime.creds.model_id)
            settings = getattr(s.runtime, 'model_settings', lambda: {})()
            out['models'] = sorted({choice['model'] for choice in settings.get('effective_models', {}).values()}) or [s.runtime.creds.model_id]
        # Retain recorded spend accounting, including historical runs.
        spent = getattr(s.store, "spent_usd", None)
        if callable(spent):
            out["cost_usd"] = float(spent() or 0.0)
        # a recording plays in the page while this run goes on: it says when the run needs a person
        mine = s.store.run_ids_since(getattr(s, "session_start", 0))
        out["waiting"] = sum(1 for r in s.store.list_runs()
                             if r["id"] in mine and r.get("status") in ("waiting_human", "parked"))
        return out

    @app.get("/api/laboratory")
    def laboratory() -> dict[str, Any]:
        return laboratory_capabilities(s)

    @app.get("/api/windows")
    def windows() -> dict[str, Any]:
        """Laboratory stations (contract v1) the brain is connected to: sim/real,
        online/offline and each window's own state summary."""
        clients = getattr(s.jobs, "clients", None) or {}
        out = []
        for module, client in clients.items():
            station = s.stations.modules.get(module)
            row: dict[str, Any] = {"module": module, "url": client.base_url,
                                   "kind": station.kind if station else "sim"}
            try:
                st = client.state()
                row.update(reachable=True, demo=st.demo, busy=st.busy, locked=st.locked,
                           fault=st.fault, lease=st.raw.get("lease"),
                           last_command=st.raw.get("last_command"), jobs=st.raw.get("jobs", []))
            except Exception as exc:  # noqa: BLE001 - one dead window never hides the others
                row.update(reachable=False, error=str(exc))
            out.append(row)
        return {"mode": "contract", "windows": out}

    @app.get("/api/stations")
    def stations() -> dict[str, Any]:
        mirror = s.jobs.mirror
        return {**s.stations.to_dict(),
                "mirror": {"online": mirror.online, "error": mirror.last_error,
                           "applied": mirror.applied} if mirror else None}

    @app.post("/api/laboratory/mode")
    def choose_laboratory(choice: LaboratoryModeIn) -> dict[str, Any]:
        turn = s.driver._turn
        if (s.runtime.busy or (turn is not None and not turn.done())
                or any(r["status"] in ("queued", "running", "paused") for r in s.store.list_runs())):
            raise HTTPException(409, "Laboratory mode cannot change while a model turn or laboratory job is active.")
        capability = laboratory_capabilities(s)
        if choice.mode != capability["mode"]:
            raise HTTPException(409, capability["real_setup_requirement"] if choice.mode == "real"
                                else "A verified virtual backend must be composed before selecting virtual mode.")
        return capability

    @app.get("/api/camera/{camera_id}/latest")
    def camera_latest(camera_id: str) -> dict[str, Any]:
        """What a camera shows now: the overview camera's live picture, or the
        microscope's last micrograph with its objective, focus and sharp plane.
        `image` is an /api/image link (null: no picture yet)."""
        optional_scope = camera_id == "reactor_microscope" and "reactor_module" in s.lab.bench
        if camera_id not in s.lab.bench.get("cameras", {}) and camera_id != "microscope" \
                and not optional_scope:
            raise HTTPException(status_code=404, detail=f"unknown camera {camera_id}")
        p = s.lab.preview(camera_id)
        if p is None:
            return {"device": camera_id, "image": None, "timestamp": None, "meta": {}}
        image = None
        if p.get("path"):
            try:
                f = Path(p["path"]).resolve()
                # a live preview keeps its file name: the version makes browsers reload it
                image = (f"/api/image?path={f.relative_to(data_root).as_posix()}"
                         f"&v={f.stat().st_mtime_ns}")
            except (ValueError, OSError):
                image = None  # outside the run-data directory (never served) or gone
        return {"device": camera_id, "image": image, "timestamp": p.get("timestamp"),
                "meta": p.get("meta") or {}}

    @app.post("/api/model/check")
    async def model_check() -> dict[str, Any]:
        """Check the gateway now (free: no prompt is sent)."""
        if model_health is None:
            return {"state": "scripted"}
        return await asyncio.to_thread(model_health.check)

    @app.get('/api/models')
    async def models() -> dict[str, Any]:
        if catalog is None:
            # scripted agents make no model calls: say how to start the real model
            return {'available': False, 'reason': 'scripted',
                    'detail': 'This Qumus runs scripted agents (free, no model calls), so there is no model to '
                              'choose. To use the real model, start Qumus with Start Qumus on the desktop, or '
                              'run `python -m qumus2 serve --openai`.'}
        try:
            rows = await asyncio.to_thread(catalog.list)
            return {'available': True, **s.runtime.model_settings(), 'models': rows}
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.post('/api/model')
    async def choose_model(choice: ModelChoice) -> dict[str, Any]:
        if catalog is None:
            raise HTTPException(409, 'Model switching is unavailable in this runtime.')
        try:
            rows = await asyncio.to_thread(catalog.list)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from None
        selected = next((r for r in rows if r['id'] == choice.model), None)
        if selected is None:
            raise HTTPException(400, 'Choose a model listed by the gateway.')
        if selected.get('account_listed') is False or (selected.get('registered') and not selected.get('website_listed') and selected.get('account_listed') is not True):
            raise HTTPException(400, 'This registered model is not listed for the current account. Confirm gateway access first.')
        if selected.get('conversational') is False:
            raise HTTPException(400, 'This model requires a specialized API and cannot run agent conversations.')
        for role, assignment in choice.agent_models.items():
            selected = next((r for r in rows if r['id'] == assignment.model), None)
            if selected is None or selected.get('conversational') is False:
                raise HTTPException(400, f'Choose a conversational gateway model for {role}.')
        # The driver may have scheduled a turn which has not entered send yet.
        if s.driver._turn is not None and not s.driver._turn.done():
            raise HTTPException(409, 'Wait for the current turn to finish.')
        try:
            context = ({'context_window_tokens': choice.context_window_tokens}
                       if 'context_window_tokens' in choice.model_fields_set else {})
            if 'agent_models' in choice.model_fields_set:
                context['agent_models'] = {role: config.model_dump()
                                           for role, config in choice.agent_models.items()}
            await s.runtime.select_model(choice.model, choice.input_price, choice.output_price, **context)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        model_health.creds = s.runtime.creds
        return s.runtime.model_settings()

    @app.get('/api/billing')
    def billing() -> dict[str, Any]:
        creds = getattr(s.runtime, 'creds', None)
        station = creds is not None and urlsplit(creds.base_url or '').hostname == 'physics.bytefuture.ai'
        out = {'status': 'unavailable', 'url': 'https://physics.bytefuture.ai/dashboard/usage' if station else None,
               'detail': 'Gateway billing requires its signed-in dashboard; model API keys cannot read it.'}
        request_bills = None
        if station and str(s.store.path) != ':memory:':
            try:
                saved = BillingSnapshot.model_validate_json(
                    Path(s.store.path).with_name('gateway-billing.json').read_text(encoding="utf-8"))
                from qumus2.ui.billing import calibrated_estimate

                snapshot = saved.model_dump(mode='json', exclude_none=True)
                request_bills = snapshot.pop('by_request', [])
                out.update(status='snapshot', snapshot=snapshot,
                           estimate=calibrated_estimate(s.store, snapshot, gateway=creds.base_url))
            except (OSError, ValueError):
                pass
        if creds is not None:
            from qumus2.ui.billing import request_estimate

            out['request_estimate'] = request_estimate(
                s.store, gateway=creds.base_url, billed_requests=request_bills)
        return out

    @app.get("/api/snapshot")
    def snapshot() -> dict[str, Any]:
        proj = s.store.get_project(s.project_id)
        return {
            "project": proj,
            "runs": s.store.list_runs(),
            "samples": s.store.list_samples(),
            "locks": s.store.locks(),
            "world": s.lab.world.snapshot(),
            "stations": s.stations.to_dict(),
            "agents": [{"name": a, "label": UI_LABELS[a], "description": DESCRIPTIONS[a]}
                       for a in ALL_AGENTS],
            "busy": s.runtime.busy,
            "last_event_id": s.store.last_event_id(),
            "session_start": getattr(s, "session_start", 0),
        }

    @app.get("/api/events")
    def events(after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return [e.model_dump() for e in s.store.events_after(after, limit)]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        from qumus2.runtime.jobs import waiting_human_failure

        r = s.store.get_run(run_id)
        if r is None:
            raise HTTPException(404, "unknown run")
        return {**r, "steps": s.store.get_steps(run_id), "human_review": waiting_human_failure(s.store, run_id)}

    @app.post("/api/runs/{run_id}/human-review")
    async def review_job_hold(run_id: str, report: JobHumanReviewIn) -> dict[str, Any]:
        turn = s.driver._turn
        if s.runtime.busy or (turn is not None and not turn.done()) or s.store.pending_instructions():
            raise HTTPException(409, "Finish the active conversation before reviewing a job hold")
        try:
            return await asyncio.to_thread(s.jobs.review_waiting, run_id, project_id=s.project_id, **report.model_dump())
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/messages")
    def message(m: MessageIn) -> dict[str, str]:
        iid = s.store.add_instruction(m.text, kind=m.kind, project_id=s.project_id)
        return {"instruction_id": iid}

    @app.post("/api/server/shutdown")
    async def shutdown(request: Request) -> dict[str, bool]:
        """Stop this server the way Control+C does. For scripts/start_qumus.py
        --stop: on Windows a signal sent from another process cannot be caught.
        Only a caller that knows the token the launcher started us with (it is in
        runs/qumus-server.lock) may do this; the custom header also keeps other
        web sites out (a cross-site page cannot send it without CORS approval)."""
        token = os.environ.get(SHUTDOWN_TOKEN_ENV, "")
        given = request.headers.get(SHUTDOWN_TOKEN_HEADER, "")
        if not token or not secrets.compare_digest(given, token):
            raise HTTPException(403, "shutdown token missing or wrong")
        asyncio.get_running_loop().call_later(0.2, interrupt_self)  # after this reply
        return {"stopping": True}

    @app.post("/api/stop")
    def stop() -> dict[str, str]:
        iid = s.store.add_instruction("STOP", kind="stop", project_id=s.project_id)
        return {"instruction_id": iid}

    @app.post("/api/continue")
    def go_on() -> dict[str, str]:
        """After Stop: paused jobs resume where they can, the model continues."""
        iid = s.store.add_instruction("CONTINUE", kind="continue", project_id=s.project_id)
        return {"instruction_id": iid}

    @app.post("/api/faults")
    def fault(f: FaultIn) -> dict[str, bool]:
        try:
            s.lab.faults.inject(f.fault, f.params)
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        s.store.emit("fault.injected", {"fault": f.fault, "params": f.params})
        return {"ok": True}

    @app.get("/api/faults")
    def fault_menu() -> list[dict[str, Any]]:
        return s.lab.faults.describe()

    @app.post("/api/carriers/{carrier_id}/material-review")
    def review_carrier_material(carrier_id: str, report: MaterialReviewIn) -> dict[str, Any]:
        """A person checks a mother tape's physical label or preparation record."""
        from qumus2.records.materials import review_material

        if s.runtime.busy:
            raise HTTPException(status_code=409, detail="Finish the active task before reviewing material identity.")
        try:
            return review_material(s.store, s.lab.bench, carrier_id, **report.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/api/stamps/{stamp_id}/film")
    def new_film(stamp_id: str, f: FilmIn) -> dict[str, Any]:
        """The Lab page's "New PC film": a person says they re-filmed a stamp."""
        from qumus2.records.stamps import FilmReportError, report_new_film

        who = f.by.strip() or "a person"
        try:
            return report_new_film(s.store, s.lab.bench, stamp_id, by=who, via="ui:lab")
        except FilmReportError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    def layout() -> dict[str, Any]:
        if not cache.get("layout"):
            cache["layout"] = s.lab.layout()
        return cache["layout"]

    @app.get("/api/bench")
    def bench() -> dict[str, Any]:
        return {"layout": layout(), **bench_state(s), "sim": {**sim, "mode": s.runtime.mode}}

    @app.get("/api/plan/history")
    def plan_history() -> list[dict[str, Any]]:
        return s.store.plan_history(s.project_id)

    @app.get("/api/experience")
    def experience() -> dict[str, Any]:
        from qumus2.records.experience import ExperienceBook

        return {**ExperienceBook(s.store).summary(), "laboratory": laboratory_capabilities(s),
                "learning": laboratory_capabilities(s)["learning"]}

    @app.post("/api/sim")
    def set_sim(p: SimIn) -> dict[str, Any]:
        sim["speed"] = p.speed
        if hasattr(s.runtime, "delay"):
            s.runtime.delay = base_delay / p.speed  # type: ignore[attr-defined]
        return {"ok": True, **sim}

    @app.get("/api/image")
    def image(path: str) -> FileResponse:
        p = Path(path)
        p = resolve_run_path(p if p.is_absolute() else data_root / p)
        if (not p.is_relative_to(data_root) or p.suffix.lower() not in IMAGE_SUFFIXES
                or not p.is_file()):
            raise HTTPException(404, "no such image")
        return FileResponse(p)

    # ----- History: the earlier sessions of this store, and the demo recordings
    from qumus2.ui.recording import (
        build_recording,
        read_timeline,
        recorded_models,
        summarize_events,
        timeline_path,
    )
    from qumus2.ui.replay import add_recording_routes

    add_recording_routes(app, recordings_dir)
    past: dict[tuple[int, int], dict[str, Any] | None] = {}

    def bounds() -> list[tuple[int, int]]:
        cur = getattr(s, "session_start", 0)
        starts = [x for x in s.store.sessions() if x < cur]
        if cur > 0 and (not starts or starts[0] > 0):
            starts = [0, *starts]  # a store from before sessions were kept: one earlier run
        if not starts:
            return []
        return [(a, b) for a, b in zip(starts, [*starts[1:], cur], strict=True) if b > a]

    def timeline(a: int, b: int) -> list[dict[str, Any]]:
        """The wall-time entries the serve of session (a, b] wrote: from its own start
        mark to the next one (a deleted run store leaves older marks with the same ids)."""
        tl = read_timeline(timeline_path(s.store.path)) if s.store.path != ":memory:" else []
        marks = [i for i, t in enumerate(tl) if t.get("start")]
        mine = [i for i in marks if a <= tl[i].get("id", -1) <= b]
        if not mine:
            return []
        i = mine[-1]
        j = next((k for k in marks if k > i), len(tl))
        return tl[i:j]

    @app.get("/api/history")
    def history() -> list[dict[str, Any]]:
        rows = []
        for a, b in bounds():
            if (a, b) not in past:  # an earlier session never changes: summed up once
                events = [e.model_dump() for e in s.store.events_after(a, limit=b - a)]
                row: dict[str, Any] | None = summarize_events(events)
                if row is not None and not row["request"]:
                    row = None  # started and closed without a request
                if row is not None:
                    tl = timeline(a, b)
                    walls = [t["wall"] for t in tl if a < t.get("id", 0) and not t.get("start")
                             and t["id"] <= b]
                    sdk = any(e["kind"] == "agent.turn_end" for e in events)
                    row.update(id=a, until=b, agents="sdk" if sdk else "scripted",
                               models=recorded_models(events),
                               provider=next((e["payload"]["provider"] for e in reversed(events)
                                              if e["kind"] == "agent.turn_end"
                                              and e["payload"].get("provider")),
                                             "claude" if sdk else "scripted"),
                               started=walls[0] if walls else None,
                               ended=walls[-1] if walls else None)
                past[(a, b)] = row
            if past[(a, b)] is not None:
                rows.append(past[(a, b)])
        return rows[::-1]

    @app.get("/api/history/{session_id}/recording")
    def history_recording(session_id: int) -> dict[str, Any]:
        b = next((b for a, b in bounds() if a == session_id), None)
        if b is None:
            raise HTTPException(404, "no such earlier run")
        return build_recording(s.store, timeline(session_id, b), after_id=session_id, before_id=b,
                               layout=layout(), meta={"source": "this computer"})

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket, after: int = 0) -> None:
        if not _same_origin(ws):  # another web site open in the same browser
            await ws.close(code=1008)
            return
        await ws.accept()
        if presence is not None:
            presence.opened()
        last = after
        # the page never writes here; listening still matters: a closed tab, or
        # the server shutting down (Ctrl+C) closes the socket, and a loop that
        # only sends would wait for an event that never comes and hold the exit
        gone = asyncio.create_task(_until_closed(ws))
        try:
            while not gone.done():
                for e in s.store.events_after(last):
                    await ws.send_json(e.model_dump())
                    last = e.id
                await asyncio.wait({gone}, timeout=0.2)
        except (WebSocketDisconnect, RuntimeError):  # RuntimeError: sent after the close
            return
        finally:
            gone.cancel()
            if presence is not None:
                presence.closed()

    mount_front_end(app)
    return app


async def _until_closed(ws: WebSocket) -> None:
    """Returns when the socket closes, from either side."""
    while (await ws.receive())["type"] != "websocket.disconnect":
        pass
