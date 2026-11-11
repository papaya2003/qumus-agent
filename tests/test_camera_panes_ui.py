"""Browser check of the lab column's Camera and Microscope panes (camera_panes_ui.mjs)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_camera_panes_ui():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the camera pane browser check")
    script = Path(__file__).with_name("camera_panes_ui.mjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, timeout=120)
    if "playwright is not installed" in result.stdout:
        pytest.skip("Playwright for Node is required for the camera pane browser check")
    assert result.returncode == 0, result.stdout + result.stderr
