"""stations.yaml: where each laboratory module is served.

    simlab_dir: ../qumus-simlab        # checkout the launcher starts when stations are sim
    records_from: stacking             # module whose /api/records feed the brain mirrors
    modules:
      exfoliation: {url: "http://127.0.0.1:8781/", kind: sim}
      stacking:    {url: "http://127.0.0.1:8782/", kind: sim}
      reactor:     {url: "http://127.0.0.1:8783/", kind: sim}

`kind` is `sim` (qumus-simlab window) or `real` (a laboratory computer, usually
through an SSH tunnel). Extra keys of a module entry are kept for its adapter,
e.g. the real reactor console: `{kind: real, url: http://100.64.0.1:8080/lab/,
adapter: console, session: <name>}`. Relative `simlab_dir` is relative to the
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
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "stations.yaml"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Station:
    module: str
    url: str
    kind: str = "sim"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"module": self.module, "url": self.url, "kind": self.kind, **self.extra}


@dataclass(frozen=True)
class Stations:
    modules: dict[str, Station]
    records_from: str = "stacking"
    simlab_dir: Path | None = None
    path: Path | None = None

    @property
    def urls(self) -> dict[str, str]:
        return {m: s.url for m, s in self.modules.items()}

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
        return {"records_from": self.records_from,
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
        modules[name] = Station(name, _normalise_url(entry.get("url"), name), kind, extra)
    records_from = raw.get("records_from", "stacking")
    if records_from not in modules:
        raise ValueError(f"stations.yaml: records_from {records_from!r} is not a configured module")
    simlab_dir = raw.get("simlab_dir")
    base = path.parent if path is not None else REPO_ROOT
    resolved = (base / simlab_dir).resolve() if simlab_dir else None
    return Stations(modules, records_from, resolved, path)


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


def from_urls(urls: dict[str, str], *, kind: str = "sim", records_from: str = "stacking") -> Stations:
    """Stations for tests and scripts: {module: url}."""
    return parse({"modules": {m: {"url": u, "kind": kind} for m, u in urls.items()},
                  "records_from": records_from if records_from in urls else next(iter(urls))})
