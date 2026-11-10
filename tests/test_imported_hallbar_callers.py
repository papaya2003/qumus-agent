"""Portable caller controls: declared data, SQLite and doubles; no World or APIs."""
from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_device_design_pose_screen import bound_design
from test_imported_hallbar_target import declared_import, imported_spec

from qumus2 import cli
from qumus2.agents.tools import advisor, picking
from qumus2.config import simulation_profile
from qumus2.records.device_design import imported_target_directory, target_import_binding
from qumus2.runtime.store import RunStore
from qumus2.testbench.hallbar import electrode_pattern_from_target, load_imported_hallbar_target
from scripts import record_hallbar_demos as hallbar
from scripts import start_qumus


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "operator-bundle"
    directory.mkdir()
    target = declared_import()
    (directory / "target.json").write_text(json.dumps(target.model_dump(mode="json")))
    (directory / "electrode-layout.gds").write_bytes(b"UNIT ONLY; NOT ACTUAL GDS")
    return directory, target


@pytest.fixture
def campaign(tmp_path, bundle):
    directory, _ = bundle
    initial, experience = tmp_path / "initial.json", tmp_path / "experience.json"
    initial.write_text(json.dumps({"objects": [], "stamps": {}}))
    experience.write_text(json.dumps({"unit_only": True}))
    config = hallbar.simulation_config(target_bundle=directory)
    identity = hallbar.source_identity(initial_state=initial, experience=experience,
        config=config, target_bundle=directory)
    return hallbar.campaign_template(identity, campaign_path=tmp_path / "attempts",
        initial_state=initial, experience=experience, config=config, target_bundle=directory)


def bytes_under(directory):
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def test_campaign_funds_only_first_and_freezes_imported_config(campaign, bundle):
    directory, target = bundle
    before = bytes_under(directory)
    hallbar.validate_campaign(campaign)
    assert campaign["version"] == 3 and campaign["protocol_revision"] == hallbar.PROTOCOL_REVISION
    assert campaign["campaign_id"] == hallbar.CAMPAIGN_ID
    assert campaign["target_bundle"]["binding"] == target_import_binding(target)
    assert campaign["seed"] == 8 and len(campaign["cells"]) == 15
    assert [c["id"] for c in campaign["cells"] if c["limits"] is not None] == [hallbar.FIRST_CELL]
    assert campaign["limits"]["estimated_usd"] == 100
    config = hallbar.campaign_config(campaign)
    raw = [s for s in config["initial_samples"] if s["kind"] == "chip" and s["location"].startswith("left_chip_")]
    assert len(raw) == 9 and all(s["status"] == "bare" for s in raw)
    assert config["placement"] == simulation_profile()["placement"]
    result = hallbar.preflight(campaign)
    assert not result["ready_for_qualification"] and not result["other_cells_executable"]
    for cell in campaign["cells"][1:]:
        assert "remaining fourteen" in " ".join(hallbar.preflight(campaign, cell_id=cell["id"])["blockers"])
    assert bytes_under(directory) == before and not Path(campaign["campaign_path"]).exists()


@pytest.mark.parametrize("change", ["source", "definition", "instruction", "config", "old_protocol", "old_campaign"])
def test_campaign_target_provenance_and_old_identity_refuse(campaign, bundle, change):
    directory, _ = bundle
    if change == "source":
        (directory / "electrode-layout.gds").write_bytes(b"changed source")
    elif change == "definition":
        with (directory / "target.json").open("a") as output:
            output.write("\n")
    elif change == "instruction":
        campaign["target_bundle"]["binding"]["owner_instruction_id"] = "changed"
    elif change == "config":
        campaign["simulation_config_sha256"] = "0" * 64
    elif change == "old_protocol":
        campaign["protocol_revision"] = "hallbar-linked-geometry-v1"
    else:
        campaign["campaign_id"] = "hallbar-linked-restart-20261101"
    with pytest.raises(ValueError):
        hallbar.preflight(campaign)
    assert not Path(campaign["campaign_path"]).exists()


def test_explicit_attempt_preparation_and_read_only_resume(campaign, bundle, tmp_path, monkeypatch):
    _, target = bundle
    adapter = hallbar.HallbarCampaignAdapter(campaign)
    monkeypatch.setattr(adapter, "check_ready", lambda: None)
    workspace = tmp_path / "attempt-workspace"
    manifest = {"workspace": str(workspace), "source": adapter.expected_source,
        "campaign_sha256": hallbar.digest(campaign), "simulation_config_sha256": hallbar.digest(adapter.config)}
    with pytest.raises(OSError):
        adapter.prepare_attempt(manifest, continuing=True)
    assert not workspace.exists()
    adapter.prepare_attempt(manifest, continuing=False)
    saved = imported_target_directory(workspace, target)
    before = bytes_under(workspace)
    adapter.prepare_attempt(manifest, continuing=True)
    assert bytes_under(workspace) == before
    (saved / "electrode-layout.gds").unlink()
    with pytest.raises(OSError):
        adapter.prepare_attempt(manifest, continuing=True)
    assert not (saved / "electrode-layout.gds").exists()


def test_accounting_database_reads_closed_and_active_wal_without_materializing(tmp_path):
    path = tmp_path / "qumus.db"
    store = RunStore(path)
    store.emit("declared.first", {})
    store.close()
    before = bytes_under(tmp_path)
    with hallbar._read_only_database(path) as connection:
        assert connection.execute("SELECT kind FROM event").fetchall() == [("declared.first",)]
    assert bytes_under(tmp_path) == before
    store = RunStore(path)
    try:
        store.emit("declared.second", {})
        paths = set(bytes_under(tmp_path))
        with hallbar._read_only_database(path) as connection:
            assert connection.execute("SELECT kind FROM event ORDER BY id").fetchall() == [("declared.first",), ("declared.second",)]
        assert set(bytes_under(tmp_path)) == paths
    finally:
        store.close()


def test_guard_missing_registry_refuses_without_recreating_it(campaign, tmp_path, monkeypatch):
    adapter = hallbar.HallbarCampaignAdapter(campaign)
    monkeypatch.setattr(adapter, "check_ready", lambda: None)
    missing = tmp_path / "missing-registry" / "attempts.sqlite3"
    registry = SimpleNamespace(db=missing, connect=lambda: pytest.fail("Guard used writable registry connection"))
    manifest = {"cell_id": hallbar.FIRST_CELL, "model": campaign["first_cell_model"],
        "source": adapter.expected_source, "campaign_sha256": hallbar.digest(campaign),
        "simulation_config_sha256": hallbar.digest(adapter.config)}
    with pytest.raises(FileNotFoundError):
        adapter.guard(registry, manifest)
    assert not missing.parent.exists()


def serve_args(database, bundle=None):
    return argparse.Namespace(db=str(database),
        target_bundle=str(bundle) if bundle else None)


def test_cli_resolves_and_copies_target_before_services(bundle, tmp_path, monkeypatch):
    from qumus2 import app

    directory, target = bundle
    database = tmp_path / "fresh" / "qumus.db"
    seen = []
    class BeforeServices(Exception):
        pass
    def capture(path, **kwargs):
        seen.append(kwargs["testbench_config"])
        assert load_imported_hallbar_target(imported_target_directory(database.parent, target)) == target
        assert not database.exists()
        raise BeforeServices
    monkeypatch.setattr(app, "build_services", capture)
    with pytest.raises(BeforeServices):
        cli.main(["serve", "--mock", "--db", str(database),
                  "--target-bundle", str(directory), "--no-browser"])
    chip = next(s for s in seen[0]["initial_samples"] if s["id"] == "chip_e01")
    assert chip["props"]["device_design_target"] == target.model_dump(mode="json")
    assert chip["props"]["imported_target_binding"] == target_import_binding(target)


def test_cli_read_only_plan_and_old_workspace_refusal(bundle, tmp_path):
    directory, target = bundle
    database = tmp_path / "fresh" / "qumus.db"
    config = cli._serve_config(serve_args(database, directory), prepare=False)
    assert config and not database.parent.exists()
    store = RunStore(database)
    legacy = next(s for s in simulation_profile("hallbar")["initial_samples"] if s["id"] == "chip_e01")
    store.upsert_sample("chip_e01", "chip", props=legacy["props"])
    store.close()
    before = bytes_under(database.parent)
    with pytest.raises(ValueError):
        cli._serve_config(serve_args(database, directory))
    assert bytes_under(database.parent) == before
    assert not imported_target_directory(database.parent, target).exists()
    with pytest.raises(ValueError, match="explicit imported"):
        cli._serve_config(serve_args(tmp_path / "hallbar-linked-owner-20261101" / "qumus.db"))


@pytest.mark.parametrize("count", [0, 2])
def test_launcher_missing_or_ambiguous_refuses_before_socket(tmp_path, monkeypatch, count):
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(count):
        path = repo / "runs/workspace/hallbar-linked-owner-20261101/targets" / str(index) / "target.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    before = bytes_under(repo)
    monkeypatch.setattr(start_qumus, "REPO", repo)
    monkeypatch.setattr(start_qumus.sys, "argv", ["start_qumus.py", "--check-only"])
    monkeypatch.setattr(start_qumus.socket, "socket", lambda *a: pytest.fail("Opened socket"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        start_qumus.main()
    assert exc.value.code == 2 and bytes_under(repo) == before


def test_launcher_valid_check_is_read_only(bundle, tmp_path, monkeypatch, capsys):
    directory, _ = bundle
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(start_qumus, "REPO", repo)
    monkeypatch.setattr(start_qumus.sys, "argv", ["start_qumus.py", "--check-only", "--target-bundle", str(directory / "target.json")])
    monkeypatch.setattr(start_qumus.socket, "socket", lambda *a: pytest.fail("Opened socket"))
    monkeypatch.chdir(tmp_path)
    start_qumus.main()
    output = capsys.readouterr().out
    assert "hallbar-linked-owner-20261101/qumus.db" in output and "Read-only startup plan" in output
    assert not (repo / "runs").exists()


@pytest.mark.parametrize("change", [None, "create", "replace", "downgrade", "binding", "source"])
def test_registration_only_reaffirms_constructed_import(bundle, tmp_path, change):
    directory, target = bundle
    workspace = tmp_path / "registered"
    load_imported_hallbar_target(directory, workspace=workspace)
    store = RunStore(workspace / "qumus.db")
    try:
        props = {"size_mm": list(target.size_mm), "device_design_target": target.model_dump(mode="json"),
                 "imported_target_binding": target_import_binding(target)}
        if change == "create":
            props.pop("device_design_target")
        if change == "binding":
            props["imported_target_binding"]["target_sha256"] = "0" * 64
        if change == "source":
            (imported_target_directory(workspace, target) / "electrode-layout.gds").write_bytes(b"changed")
        store.upsert_sample("target", "chip", props=props)
        submitted = target.model_dump(mode="json")
        if change == "replace":
            submitted = declared_import(source_bytes=b"different").model_dump(mode="json")
        if change == "downgrade":
            submitted = next(s for s in simulation_profile("hallbar")["initial_samples"] if s["id"] == "chip_e01")["props"]["device_design_target"]
        submitted["chip_id"] = "target"
        instruction = store.add_instruction(json.dumps({"device_design_target": submitted}))
        before = deepcopy(store.get_sample("target"))
        result = asyncio.run(advisor.register_target_geometry(advisor.RegisterGeometryArgs(chip_id="target", instruction_id=instruction),
            SimpleNamespace(store=store, project_id=None)))
        assert result.status == ("ok" if change is None else "error")
        if change is not None:
            assert store.get_sample("target") == before
    finally:
        store.close()


def test_advisor_and_picker_expose_full_import_and_refuse_missing_source(bundle, tmp_path):
    directory, target = bundle
    workspace = tmp_path / "context"
    load_imported_hallbar_target(directory, workspace=workspace)
    store = RunStore(workspace / "qumus.db")
    try:
        project = store.create_project("Declared imported context")
        store.upsert_sample("target", "chip", props={"size_mm": list(target.size_mm),
            "device_design_target": target.model_dump(mode="json"), "imported_target_binding": target_import_binding(target)})
        store.update_project_state(project, device_plan={"design_contract": "device_design_v1", "target_chip_id": "target",
            "flake_roles": [{"role": "top_hbn", "material": "hBN", "count": 1}]})
        ctx = SimpleNamespace(store=store, project_id=project)
        result = asyncio.run(picking.get_flake_candidates(picking.NoArgs(), ctx))
        assert result.data["device_design_target"]["coverage_policy"] == "complete_contact_region_containment"
        assert len(result.data["device_design_target"]["metal_primitives"]) == 34
        assert advisor._current_hallbar_target(ctx, store.get_sample("target")) == target
        (imported_target_directory(workspace, target) / "electrode-layout.gds").unlink()
        result = asyncio.run(picking.get_flake_candidates(picking.NoArgs(), ctx))
        assert result.data["device_design_target"]["status"] == "unresolved"
        with pytest.raises(OSError):
            advisor._current_hallbar_target(ctx, store.get_sample("target"))
    finally:
        store.close()


@pytest.mark.parametrize("change", [None, "missing_metal", "changed_lead", "wrong_primitive", "binding", "cropped_gr", "hbn_gap"])
def test_terminal_score_checks_full_metal_and_whole_ends(change):
    design = bound_design(imported_spec())
    target = {"id": design.target_chip_id, "kind": "chip", "size_mm": list(design.target.size_mm),
        "electrode_geometry": {"polygons": electrode_pattern_from_target(design.target), "source_binding": target_import_binding(design.target)},
        "flakes": [{"id": s.flake_id, "material": s.material, "outline_mm": list(s.outline_mm),
                    "x_mm": 0., "y_mm": 0.} for s in design.sources]}
    if change == "missing_metal":
        target["electrode_geometry"]["polygons"].pop(0)
    elif change == "changed_lead":
        target["electrode_geometry"]["polygons"][0]["points"][0][0] += .001
    elif change == "wrong_primitive":
        target["electrode_geometry"]["polygons"][0]["source_primitive_index"] = 99
    elif change == "binding":
        target["electrode_geometry"]["source_binding"]["target_sha256"] = "0" * 64
    elif change in ("cropped_gr", "hbn_gap"):
        index = 1 if change == "cropped_gr" else 0
        target["flakes"][index]["outline_mm"] = [[x * .1, y * .1] for x, y in target["flakes"][index]["outline_mm"]]
    result = hallbar.truth_geometry({"objects": [target], "stamps": {}}, design, placement_tolerance_mm=.006)
    assert result["verified"] is (change is None)
    if change is None:
        assert len(result["contact_coverage"]["contacts"]) == 8
        assert all(v["contained"] for v in result["contact_coverage"]["contacts"].values())
