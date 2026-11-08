"""Jialei 2026-11-04: one stable entry. The launcher heals a WAL left by an
unclean close and moves a workspace made by older code aside, intact."""
from __future__ import annotations

import shutil
import sqlite3

from qumus2.testbench.bench_geometry import PROFILE, WORKSPACE_META
from scripts import start_qumus

SCHEMA = ("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE sample (id TEXT);"
          "CREATE TABLE run (id TEXT); CREATE TABLE project (id TEXT);")


def _workspace(tmp_path, geometry):
    workspace = tmp_path / "repo" / start_qumus.WORKSPACE
    (workspace / "targets" / "abc").mkdir(parents=True)
    (workspace / "targets" / "abc" / "target.json").write_text("{}")
    database = workspace / "qumus.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO meta VALUES (?, ?)", (WORKSPACE_META, geometry))
        connection.execute("INSERT INTO sample VALUES ('chip_e01')")
    (workspace / "images").mkdir()
    (workspace / "images" / "a.png").write_bytes(b"png")
    return workspace, database


def test_a_leftover_wal_is_backed_up_and_checkpointed(tmp_path, monkeypatch):
    monkeypatch.setattr(start_qumus, "REPO", tmp_path / "repo")
    source = tmp_path / "live.db"
    live = sqlite3.connect(source)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.executescript("CREATE TABLE t (v INTEGER); INSERT INTO t VALUES (1), (2), (3);")
    live.commit()
    database = tmp_path / "repo" / start_qumus.DATABASE
    database.parent.mkdir(parents=True)
    for suffix in ("", "-wal"):   # what a window closed without Control+C leaves
        shutil.copy2(f"{source}{suffix}", f"{database}{suffix}")
    live.close()
    wal = database.with_name("qumus.db-wal")
    assert wal.stat().st_size > 0
    assert start_qumus.heal_wal(database) is True
    assert not wal.exists() or wal.stat().st_size == 0
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    backups = list((tmp_path / "repo/runs/workspace/backups").iterdir())
    assert len(backups) == 1 and (backups[0] / "qumus.db-wal").stat().st_size > 0
    assert start_qumus.heal_wal(database) is False


def test_a_workspace_from_older_geometry_is_moved_aside_with_its_target_kept(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(start_qumus, "REPO", tmp_path / "repo")
    workspace, database = _workspace(tmp_path, "v1-linked-first-two-sim-arms-v1")
    reason = start_qumus.workspace_mismatch(database, workspace / "targets/abc/target.json")
    assert reason and "v1-linked-first-two-sim-arms-v1" in reason and PROFILE in reason
    archive = start_qumus.archive_workspace(workspace, reason)
    assert sorted(p.name for p in workspace.iterdir()) == ["targets"]
    assert (workspace / "targets/abc/target.json").exists()
    assert (archive / "qumus.db").exists() and (archive / "images/a.png").read_bytes() == b"png"
    assert (archive / "targets/abc/target.json").exists()
    assert "kept unchanged" in capsys.readouterr().out
    assert start_qumus.workspace_mismatch(database, workspace / "targets/abc/target.json") is None


def test_an_occupied_workspace_without_its_saved_lab_state_cannot_open(tmp_path, monkeypatch):
    monkeypatch.setattr(start_qumus, "REPO", tmp_path / "repo")
    workspace, database = _workspace(tmp_path, PROFILE)
    assert start_qumus.workspace_mismatch(database, workspace / "targets/abc/target.json") == \
        "its saved laboratory state is missing"
