"""Packaged default configuration of the agent system. Override directory: env QUMUS2_CONFIG_DIR.

The laboratory's own configuration (bench layout, limits, simulation profiles)
lives with each station; the brain reads the bench layout from the station.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DIR = Path(__file__).parent


def config_dir() -> Path:
    override = os.environ.get("QUMUS2_CONFIG_DIR")
    return Path(override) if override else DEFAULT_DIR


def load_yaml(name: str) -> dict[str, Any]:
    path = config_dir() / name
    if not path.exists():
        path = DEFAULT_DIR / name
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
