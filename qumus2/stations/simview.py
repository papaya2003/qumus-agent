"""What the web page shows of the laboratory, read from the stations.

The brain's page draws the bench top view, the device panel, camera pictures
and (simulated lab only) the fault menu. It reads them over HTTP:

    GET  /api/sim/layout              {bench, layout}            [Q-sim]
    GET  /api/sim/bench_state         {devices, objects, twin, stamp, faults}
    GET  /api/sim/preview/<camera>    {image_id|null, timestamp, meta}
    GET  /api/sim/faults              fault menu
    POST /api/sim/faults              {fault, params}            (loopback only)

The bench description (`bench`) is also what agent tools read as
ctx.extra["bench"] (layout names, slots, cameras). The last one a station gave
is kept in the brain's store (meta `station_bench`), so History and tools still
work while the laboratory is offline.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from typing import Any

from qumus2.contract import ContractClient, ContractError
from qumus2.core.bench import positions, regions

log = logging.getLogger(__name__)
BENCH_META = "station_bench"


class StationFaults:
    def __init__(self, view: StationLab) -> None:
        self.view = view

    def describe(self) -> list[dict[str, Any]]:
        if not self.view.simulated:
            return []
        try:
            menu = self.view.client._request("GET", "api/sim/faults")
        except ContractError:
            return []
        return menu.get("faults", menu) if isinstance(menu, dict) else list(menu or [])

    def inject(self, fault: str, params: dict[str, Any] | None = None) -> None:
        if not self.view.simulated:
            raise ValueError("faults can only be injected into the simulated laboratory")
        try:
            self.view.client._request("POST", "api/sim/faults", {"fault": fault, "params": params or {}})
        except ContractError as exc:
            raise ValueError(str(exc)) from exc


class _World:
    def __init__(self, view: StationLab) -> None:
        self.view = view

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self.view.bench_state().get("objects") or [])


class StationLab:
    """The page's and tools' read-only view of the laboratory (stations)."""

    def __init__(self, store: Any, client: ContractClient | None, *, simulated: bool = True,
                 images: Any = None, state_ttl_s: float = 0.25) -> None:
        self.store = store
        self.client = client
        self.simulated = simulated
        self.images = images
        self.faults = StationFaults(self)
        self.world = _World(self)
        self._state: dict[str, Any] = {}
        self._state_at = 0.0
        self._ttl = state_ttl_s
        self._lock = threading.Lock()
        self._layout: dict[str, Any] | None = None
        self._seen: dict[str, Any] = {}
        raw = store.get_meta(BENCH_META)
        self._bench: dict[str, Any] = json.loads(raw) if raw else {}

    # ----- bench description --------------------------------------------------
    def refresh_bench(self) -> bool:
        """Fetch the bench description from the station; False when offline."""
        if self.client is None:
            return False
        try:
            reply = self.client._request("GET", "api/sim/layout")
        except ContractError as exc:
            log.info("bench layout not available: %s", exc)
            return False
        bench = reply.get("bench") if isinstance(reply, dict) else None
        if not isinstance(bench, dict):
            return False
        self._bench = bench
        self._layout = reply.get("layout") if isinstance(reply.get("layout"), dict) else None
        self.store.set_meta(BENCH_META, json.dumps(bench, default=str))
        return True

    @property
    def bench(self) -> dict[str, Any]:
        if not self._bench:
            self.refresh_bench()
        return self._bench

    def layout(self) -> dict[str, Any]:
        if self._layout is None:
            self.refresh_bench()
        if self._layout is not None:
            return self._layout
        bench = self.bench
        return {**bench, "regions": regions(bench), "positions": positions(bench)} if bench else {}

    # ----- live state ---------------------------------------------------------
    def bench_state(self) -> dict[str, Any]:
        with self._lock:
            if time.monotonic() - self._state_at < self._ttl:
                return self._state
            state: dict[str, Any] = {}
            if self.client is not None:
                try:
                    reply = self.client._request("GET", "api/sim/bench_state")
                    state = reply if isinstance(reply, dict) else {}
                    if self.images is not None:
                        state = self.images.to_local(state)
                except ContractError as exc:
                    state = {"error": str(exc)}
            self._state, self._state_at = state, time.monotonic()
            return state

    def preview(self, camera_id: str) -> dict[str, Any] | None:
        if self.client is None:
            return None
        try:
            reply = self.client._request("GET", "api/sim/preview/" + urllib.parse.quote(camera_id))
        except ContractError:
            return None
        if not isinstance(reply, dict):
            return None
        out = {"timestamp": reply.get("timestamp"), "meta": reply.get("meta") or {}, "path": None}
        image_id = reply.get("image_id")
        if image_id and self.images is not None:
            # a live preview keeps its id while the picture changes: fetch it fresh
            path = self.images.local_path(image_id)
            if path is not None and path.exists() and reply.get("timestamp") != self._preview_seen(camera_id):
                path.unlink(missing_ok=True)
            local = self.images.fetch(image_id)
            out["path"] = None if local.startswith("qumus-image:") else local
            self._seen[camera_id] = reply.get("timestamp")
        return out

    def _preview_seen(self, camera_id: str) -> Any:
        return self._seen.get(camera_id)
