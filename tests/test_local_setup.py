"""Bootstrap must preserve a configured workspace when run again."""

import json
import shutil
from pathlib import Path

from scripts import setup_qumus


def test_setup_preserves_private_config_data_and_editor_choices(tmp_path, monkeypatch):
    repo = tmp_path / "a repo"
    repo.mkdir()
    (repo / ".env.example").write_text("PLACEHOLDER=\n", encoding="utf-8")
    (repo / ".env").write_text("LOCAL_TEST_SETTING=keep\n", encoding="utf-8")
    (repo / "runs/workspace").mkdir(parents=True)
    db = repo / "runs/workspace/qumus.db"
    db.write_bytes(b"existing data")
    settings = tmp_path / ".vscode/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"editor.fontSize": 17, "files.exclude": {"**/.venv": False}}), encoding="utf-8")
    templates = repo / "scripts/templates"
    templates.mkdir(parents=True)
    for name in ("Start Qumus.command", "Stop Qumus.command"):
        shutil.copy2(Path(setup_qumus.__file__).parent / "templates" / name, templates)
    monkeypatch.setattr(setup_qumus, "REPO", repo)
    for _ in range(2):
        setup_qumus.prepare_local_files()
    assert (repo / ".env").read_text(encoding="utf-8") == "LOCAL_TEST_SETTING=keep\n"
    assert db.read_bytes() == b"existing data"
    result = json.loads(settings.read_text(encoding="utf-8"))
    assert result["editor.fontSize"] == 17 and result["files.exclude"]["**/.venv"] is False
    assert result["files.exclude"]["**/__pycache__"] is True
    launcher = tmp_path / "Start Qumus.command"
    assert f"QUMUS_REPO='{repo}'" in launcher.read_text(encoding="utf-8")
    assert launcher.stat().st_mode & 0o111
    stop = tmp_path / "Stop Qumus.command"
    assert f"QUMUS_REPO='{repo}'" in stop.read_text(encoding="utf-8") and "--stop" in stop.read_text(encoding="utf-8")
    assert stop.stat().st_mode & 0o111


def test_setup_writes_vscode_start_tasks_and_keeps_the_persons_own(tmp_path, monkeypatch):
    repo = tmp_path / "qumus-agent"
    (repo / "scripts").mkdir(parents=True)
    tasks = tmp_path / ".vscode/tasks.json"
    tasks.parent.mkdir()
    mine = {"label": "my task", "type": "shell", "command": "echo hi"}
    tasks.write_text(json.dumps({"version": "2.0.0", "tasks": [mine, {"label": "Start Qumus"}]}), encoding="utf-8")
    monkeypatch.setattr(setup_qumus, "REPO", repo)
    for _ in range(2):
        setup_qumus.write_editor_tasks(tmp_path)
    result = json.loads(tasks.read_text(encoding="utf-8"))
    labels = [t["label"] for t in result["tasks"]]
    assert labels == ["Start Qumus", "Start Qumus (free, scripted)", "Stop Qumus", "Qumus status", "my task"]
    start, free, stop = result["tasks"][0], result["tasks"][1], result["tasks"][2]
    assert free["args"][:3] == ["-m", "qumus2", "serve"] and "--openai" not in free["args"]
    assert start["args"] == [str(repo / "scripts" / "start_qumus.py")] and start["group"]["isDefault"] is True
    assert stop["args"][-1] == "--stop" and start["options"]["cwd"] == str(repo)
    assert result["tasks"][4] == mine
    tasks.write_text("{ not json", encoding="utf-8")
    setup_qumus.write_editor_tasks(tmp_path)
    assert tasks.read_text(encoding="utf-8") == "{ not json"
