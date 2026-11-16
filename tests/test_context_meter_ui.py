"""The context meter's words (contextmeter.js) under node --test."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_context_meter_text():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for UI checks")
    script = Path(__file__).with_name("context_meter_ui.mjs")
    run = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, run.stdout + run.stderr
