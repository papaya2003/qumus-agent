"""Run pure UI event regressions without browser automation or API calls."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import v2_recording

#: qumus-v2 recordings a script reads (skipped when they are not available)
NEEDS = {
    "ui_review.mjs": ["openai-device-approval-20261025", "openai-collaboration-20261025",
                      "archive/experimental-reactor-mechanics-20261026",
                      "archive/experimental-reactor-equal-spacing-20261026",
                      "archive/experimental-reactor-clearance-20261026",
                      "experimental-reactor-pair-normal-20261027-v3",
                      "openai-integrated-reactor-20261027-v5",
                      "openai-material-metal-default-20261028-v5"],
    "ui_recording_timing.mjs": ["experimental-reactor-pair-normal-20261027-v3"],
}


@pytest.mark.parametrize("script", ["ui_review.mjs", "ui_recording_timing.mjs", "token_chart.mjs", "context_composition.mjs", "device_design_ui.mjs"])
def test_ui_event_logic(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for offline UI event regressions")
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    for name in NEEDS.get(script, []):
        found = v2_recording(name)
        env["QUMUS_RECORDINGS_DIR"] = str(found.parents[len(Path(name).parts) - 1])
    result = subprocess.run(
        [node, "--test", f"tests/{script}"], cwd=root, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
