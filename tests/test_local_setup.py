"""Bootstrap must preserve a configured workspace when run again."""

import json
import shutil
from pathlib import Path

from scripts import setup_qumus


def test_setup_preserves_private_config_data_and_editor_choices(tmp_path, monkeypatch):
    repo = tmp_path / "a repo"
    repo.mkdir()
    (repo / ".env.example").write_text("PLACEHOLDER=\n")
    (repo / ".env").write_text("LOCAL_TEST_SETTING=keep\n")
    (repo / "runs/workspace").mkdir(parents=True)
    db = repo / "runs/workspace/qumus.db"
    db.write_bytes(b"existing data")
    settings = tmp_path / ".vscode/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"editor.fontSize": 17, "files.exclude": {"**/.venv": False}}))
    templates = repo / "scripts/templates"
    templates.mkdir(parents=True)
    for name in ("Start Qumus.command", "Stop Qumus.command"):
        shutil.copy2(Path(setup_qumus.__file__).parent / "templates" / name, templates)
    monkeypatch.setattr(setup_qumus, "REPO", repo)
    for _ in range(2):
        setup_qumus.prepare_local_files()
    assert (repo / ".env").read_text() == "LOCAL_TEST_SETTING=keep\n"
    assert db.read_bytes() == b"existing data"
    result = json.loads(settings.read_text())
    assert result["editor.fontSize"] == 17 and result["files.exclude"]["**/.venv"] is False
    assert result["files.exclude"]["**/__pycache__"] is True
    launcher = tmp_path / "Start Qumus.command"
    assert f"QUMUS_REPO='{repo}'" in launcher.read_text()
    assert launcher.stat().st_mode & 0o111
    stop = tmp_path / "Stop Qumus.command"
    assert f"QUMUS_REPO='{repo}'" in stop.read_text() and "--stop" in stop.read_text()
    assert stop.stat().st_mode & 0o111
