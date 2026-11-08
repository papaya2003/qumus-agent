"""Run pure UI event regressions without browser automation or API calls."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("script", ["ui_review.mjs", "ui_recording_timing.mjs", "token_chart.mjs", "context_composition.mjs", "device_design_ui.mjs"])
def test_ui_event_logic(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for offline UI event regressions")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", f"tests/{script}"], cwd=root,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
