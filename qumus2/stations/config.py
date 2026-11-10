"""stations.yaml: where each laboratory module is served.

    simlab_dir: ../qumus-simlab        # checkout the launcher starts when stations are sim
    records_from: stacking             # module(s) whose /api/records feed the brain mirrors:
                                       # one name, or a list when stations keep separate
                                       # stores (e.g. [stacking, reactor] with a reactor bridge)
    modules:
      exfoliation: {url: "http://127.0.0.1:8781/", kind: sim}
      stacking:    {url: "http://127.0.0.1:8782/", kind: sim}
      reactor:     {url: "http://127.0.0.1:8783/lab/", kind: sim, adapter: console, session: qumus}

`kind` is `sim` (qumus-simlab window) or `real` (a laboratory computer, usually
through an SSH tunnel). Extra keys of a module entry are kept with it.
`adapter: console` marks a station that speaks the vdW reactor's own Lab Console
API instead of contract v1 (qumus2/reactor/console.py; Jialei 2026-11-10), as the
packaged default reactor does. Such a station has no record feed and no contract jobs: the brain's reactor tools
(qumus2/reactor/tools.py) drive it. Relative `simlab_dir` is relative to the
stations.yaml file.

Search order: --stations PATH, env QUMUS_STATIONS, ./stations.yaml, the packaged
default (all three modules simulated on loopback).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MODULES = ("exfoliation", "stacking", "reactor")
KINDS = ("sim", "real")
ADAPTERS = ("contract", "console")
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "stations.yaml"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Station:
    module: str
    url: str
    kind: str = "sim"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def adapter(self) -> str:
        return str(self.extra.get("adapter") or "contract")

    def to_dict(self) -> dict[str, Any]:
        return {"module": self.module, "url": self.url, "kind": self.kind, **self.extra}


@dataclass(frozen=True)
class Stations:
    modules: dict[str, Station]
    feeds: tuple[str, ...] = ("stacking",)
    simlab_dir: Path | None = None
    path: Path | None = None

    @property
    def records_from(self) -> str:
        """The primary feed: put_records, the bench drawing and the shared ledger."""
        return self.feeds[0]

    @property
    def urls(self) -> dict[str, str]:
        return {m: s.url for m, s in self.modules.items()}

    @property
    def contract_urls(self) -> dict[str, str]:
        """Stations that speak contract v1 (job service, record feeds)."""
        return {m: s.url for m, s in self.modules.items() if s.adapter == "contract"}

    @property
    def consoles(self) -> dict[str, Station]:
        """Stations that speak the reactor Lab Console API (adapter: console)."""
        return {m: s for m, s in self.modules.items() if s.adapter == "console"}

    @property
    def all_sim(self) -> bool:
        return all(s.kind == "sim" for s in self.modules.values())

    @property
    def any_sim(self) -> bool:
        return any(s.kind == "sim" for s in self.modules.values())

    @property
    def bench(self) -> str:
        """The run store's bench label: "sim" unless every module is real."""
        return "real" if not self.any_sim else "sim"

    def to_dict(self) -> dict[str, Any]:
        return {"records_from": self.records_from, "feeds": list(self.feeds),
                "simlab_dir": str(self.simlab_dir) if self.simlab_dir else None,
                "path": str(self.path) if self.path else None,
                "modules": {m: s.to_dict() for m, s in self.modules.items()}}


def _normalise_url(url: Any, module: str) -> str:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError(f"stations.yaml: {module}.url must be an http(s) URL, got {url!r}")
    return url if url.endswith("/") else url + "/"


def parse(raw: dict[str, Any], *, path: Path | None = None) -> Stations:
    if not isinstance(raw, dict) or not isinstance(raw.get("modules"), dict) or not raw["modules"]:
        raise ValueError("stations.yaml needs a `modules:` mapping")
    modules: dict[str, Station] = {}
    for name, entry in raw["modules"].items():
        if name not in MODULES:
            raise ValueError(f"stations.yaml: unknown module {name!r}; known: {', '.join(MODULES)}")
        if entry is None:  # module switched off
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"stations.yaml: {name} must be a mapping with url and kind")
        kind = entry.get("kind", "sim")
        if kind not in KINDS:
            raise ValueError(f"stations.yaml: {name}.kind must be sim or real, got {kind!r}")
        extra = {k: v for k, v in entry.items() if k not in ("url", "kind")}
        if extra.get("adapter", "contract") not in ADAPTERS:
            raise ValueError(f"stations.yaml: {name}.adapter must be one of {', '.join(ADAPTERS)}")
        modules[name] = Station(name, _normalise_url(entry.get("url"), name), kind, extra)
    records_from = raw.get("records_from", "stacking")
    feeds = (records_from,) if isinstance(records_from, str) else tuple(records_from or ())
    if not feeds:
        raise ValueError("stations.yaml: records_from needs at least one module")
    for feed in feeds:
        if feed not in modules:
            raise ValueError(f"stations.yaml: records_from {feed!r} is not a configured module")
        if modules[feed].adapter != "contract":
            raise ValueError(f"stations.yaml: records_from {feed!r} has no record feed "
                             "(adapter: console); the brain records its jobs itself")
    simlab_dir = raw.get("simlab_dir")
    base = path.parent if path is not None else REPO_ROOT
    resolved = (base / simlab_dir).resolve() if simlab_dir else None
    return Stations(modules, feeds, resolved, path)


def load(path: str | Path | None = None) -> Stations:
    candidates = [path, os.environ.get("QUMUS_STATIONS"), Path.cwd() / "stations.yaml"]
    for c in candidates:
        if c and Path(c).is_file():
            p = Path(c).resolve()
            return parse(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, path=p)
        if c is path and path is not None:
            raise FileNotFoundError(f"stations file not found: {path}")
    raw = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8")) or {}
    # the packaged default: qumus-simlab checked out beside this repository
    stations = parse(raw, path=None)
    return stations


def from_urls(urls: dict[str, str], *, kind: str = "sim",
              records_from: str | list[str] = "stacking") -> Stations:
    """Stations for tests and scripts: {module: url}."""
    feeds = [records_from] if isinstance(records_from, str) else list(records_from)
    feeds = [f for f in feeds if f in urls] or [next(iter(urls))]
    return parse({"modules": {m: {"url": u, "kind": kind} for m, u in urls.items()},
                  "records_from": feeds})
