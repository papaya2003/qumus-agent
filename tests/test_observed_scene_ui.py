"""Offline DOM coverage of observed evidence versus diagnostic bench rendering."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_observed_scene_ui():
    node = shutil.which("node")
    if node is None:
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
        node = str(bundled) if bundled.is_file() else None
    if node is None:
        pytest.skip("Node is required for offline observed-scene UI checks")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("observed_scene_ui.mjs"))],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
