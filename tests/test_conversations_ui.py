"""The conversation picker's and History's grouping helpers (node --test)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_conversation_picker_and_history_groups():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for UI checks")
    script = Path(__file__).with_name("conversations_ui.mjs")
    run = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, run.stdout + run.stderr
