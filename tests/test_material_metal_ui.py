"""Read-only event projection for material-metal simulation results."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_material_metal_evidence_display():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for UI event checks")
    script = Path(__file__).with_name("material_metal_ui.mjs")
    subprocess.run([node, "--test", str(script)], check=True, capture_output=True, text=True)
