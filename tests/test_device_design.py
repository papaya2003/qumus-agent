"""Eight-contact binding negative controls and bounded KLayout transport checks."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from qumus2.agents import klayout
from qumus2.agents.gateway import ToolGateway
from qumus2.agents.tooling import default_registry
from qumus2.core.polygons import rectangle
from qumus2.records.device_design import (
    DesignArtifact,
    DeviceDesignV1,
    FlakePlacement,
    HallBarTarget,
    bind_artifact,
    build_design_spec,
    design_directory,
    layout_payload,
    validate_design_artifact,
    validate_design_current,
)
from qumus2.records.geometry import geometry_digest, selection_source_snapshot
from qumus2.runtime.store import RunStore


def design_fixture(root, *, wide=False):
    """The wide analytic option passes the full pose window, not a source-preparation result."""
    store = RunStore(root / "qumus.db")
    project = store.create_project("Eight-contact test")
    roles = ["source", "drain", "voltage_left_1", "voltage_left_2", "voltage_left_3",
             "voltage_right_1", "voltage_right_2", "voltage_right_3"]
    centers = [(0, .020), (0, -.020), *[(-.010, y) for y in (-.01, 0, .01)],
               *[(.010, y) for y in (-.01, 0, .01)]]
    target = HallBarTarget(source="simulated_design", reference="Explicit synthetic test geometry",
        size_mm=(1, 1), contacts=[{"id": role, "role": role,
            "points_mm": rectangle(.004, .004, (.5+x, .5+y))}
            for role, (x, y) in zip(roles, centers, strict=True)])
    store.upsert_sample("target", "chip", status="patterned", props={
        "size_mm": [1, 1], "layers_top_first": [], "device_design_target": target.model_dump(mode="json")})
    sizes = (("hBN", (.100, .120)), ("Gr", (.060, .080))) if wide else (
        ("hBN", (.036, .056)), ("Gr", (.024, .044)))
    for material, size in sizes:
        chip, fid = "source_" + material, "flake_" + material
        store.upsert_sample(chip, "chip", status="exfoliated", props={"material": material})
        job = store.create_run("search_chip", {"chip_id": chip}, project_id=project)
        store.set_run_status(job, "succeeded")
        image = root / (material + ".png")
        image.write_bytes(b"synthetic image evidence for binding test")
        store.upsert_sample(fid, "flake", location=chip, props={"material": material,
            "images": {"20x": str(image)}, "optical_geometry": {
                "outline_mm": rectangle(*size), "units": "mm", "frame": "flake_center_chip_axes",
                "source": "simulated_optical_outline", "source_chip_id": chip,
                "image": str(image), "job_id": job}})
    ids = ["flake_hBN", "flake_Gr"]
    store.update_project_state(project, selected_flakes=ids,
        selection_source_evidence=selection_source_snapshot(store, ids),
        device_plan={"target_chip_id": "target", "flake_roles": [
            {"role": "top_hbn", "material": "hBN"}, {"role": "channel", "material": "Gr"}]})
    placements = [FlakePlacement(flake_id=fid, translation_mm=(.5, .5), rotation_deg=0) for fid in ids]
    spec = build_design_spec(store, project, placements, 1, design_id="design_" + uuid4().hex, revision=1)
    return store, project, placements, spec


def fake_artifact(store, spec):
    """Explicit transport test double, never reported as live KLayout acceptance."""
    directory = design_directory(store, spec.design_id)
    directory.mkdir(parents=True)
    path = directory / "device.gds"
    path.write_bytes(b"mock artifact for immutable binding negative controls")
    return DesignArtifact(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          geometry_sha256=geometry_digest(layout_payload(spec)))


def save_design(store, project, design):
    store.update_project_state(project, device_design=design.model_dump(mode="json"), design_accepted=True)


def test_complete_geometry_and_exact_eight_contacts(tmp_path):
    store, project, _, spec = design_fixture(tmp_path)
    assert spec.pickup_order == ("flake_hBN", "flake_Gr")
    assert len(layout_payload(spec)["layers"]) == 10
    design = bind_artifact(spec, fake_artifact(store, spec))
    save_design(store, project, design)
    assert validate_design_current(store, project) == design
    assert len(design.sources[0].evidence_event_ids) == 1


@pytest.mark.parametrize("change", ["duplicate", "missing", "receiving_layer"])
def test_target_missing_ambiguous_or_layered_rejected(tmp_path, change):
    _, _, _, spec = design_fixture(tmp_path)
    body = spec.target.model_dump(mode="json")
    if change == "duplicate":
        body["contacts"][-1] = body["contacts"][0]
    elif change == "missing":
        body["contacts"].pop()
    else:
        body["receiving_layers_top_first"] = ["hBN"]
    with pytest.raises(ValueError):
        HallBarTarget.model_validate(body)


def test_whole_flake_coverage_and_all_contacts_not_channel_crop(tmp_path):
    store, project, placements, spec = design_fixture(tmp_path)
    changed = [placements[0].model_copy(update={"translation_mm": (.5101, .5)}), placements[1]]
    with pytest.raises(ValueError, match="complete Gr"):
        build_design_spec(store, project, changed, 1, design_id=spec.design_id, revision=1)
    shifted = [p.model_copy(update={"translation_mm": (.51, .5)}) for p in placements]
    with pytest.raises(ValueError, match="contact"):
        build_design_spec(store, project, shifted, 1, design_id=spec.design_id, revision=1)


@pytest.mark.parametrize("change", ["guidance", "target", "image", "artifact", "selection", "digest"])
def test_stale_or_tampered_design_cannot_resolve(tmp_path, change):
    store, project, _, spec = design_fixture(tmp_path)
    design = bind_artifact(spec, fake_artifact(store, spec))
    save_design(store, project, design)
    if change == "guidance":
        store.update_project_state(project, device_plan={"target_chip_id": "target", "changed": True})
    elif change == "target":
        target = spec.target.model_dump(mode="json")
        target["reference"] = "changed target source"
        store.upsert_sample("target", "chip", props={"device_design_target": target})
    elif change == "image":
        Path(spec.sources[0].observation["image"]).write_bytes(b"replacement image")
    elif change == "artifact":
        Path(design.artifact.path).write_bytes(b"replacement GDS")
    elif change == "selection":
        store.update_project_state(project, selected_flakes=["flake_hBN"])
    else:
        body = design.model_dump(mode="json")
        body["sources"][0]["placement"]["rotation_deg"] = 180
        store.update_project_state(project, device_design=body)
    with pytest.raises((ValueError, OSError)):
        validate_design_current(store, project)


def test_external_artifact_path_even_with_valid_digest_is_refused(tmp_path):
    store, project, _, spec = design_fixture(tmp_path)
    artifact = fake_artifact(store, spec)
    artifact = artifact.model_copy(update={"path": str(tmp_path / "untrusted.gds")})
    design = bind_artifact(spec, artifact)
    save_design(store, project, design)
    with pytest.raises(ValueError, match="outside"):
        validate_design_current(store, project)


def test_legacy_record_is_not_new_executable_binding(tmp_path):
    store, project, _, _ = design_fixture(tmp_path)
    store.update_project_state(project, design_accepted=True, design={"gds_path": "/any/file.gds"})
    with pytest.raises(ValueError):
        validate_design_current(store, project)


def test_saved_artifact_check_does_not_refresh_execution_source_history(tmp_path):
    store, project, _, spec = design_fixture(tmp_path)
    design = bind_artifact(spec, fake_artifact(store, spec))
    save_design(store, project, design)
    store.upsert_sample("flake_hBN", "flake", location="stamp", status="picked_up")
    assert validate_design_artifact(store, design) == design
    with pytest.raises(ValueError):
        validate_design_current(store, project)


def test_saved_sim_design_cannot_validate_in_real_store(tmp_path):
    store, _, _, spec = design_fixture(tmp_path)
    design = bind_artifact(spec, fake_artifact(store, spec))
    real = RunStore(tmp_path / "real.db", bench="real")
    with pytest.raises(ValueError, match="provenance"):
        validate_design_artifact(real, design)


def test_contact_roles_must_describe_two_ends_and_three_per_side(tmp_path):
    _, _, _, spec = design_fixture(tmp_path)
    body = spec.target.model_dump(mode="json")
    body["contacts"][0]["role"], body["contacts"][1]["role"] = "drain", "source"
    with pytest.raises(ValueError, match="top/bottom"):
        HallBarTarget.model_validate(body)


def test_registered_geometry_cannot_override_current_chip_size(tmp_path):
    store, project, placements, spec = design_fixture(tmp_path)
    store.upsert_sample("target", "chip", props={"size_mm": [.5, .5]})
    with pytest.raises(ValueError, match="current declared target chip size"):
        build_design_spec(store, project, placements, 1, design_id=spec.design_id, revision=1)


def test_current_chip_size_change_invalidates_accepted_design(tmp_path):
    store, project, _, spec = design_fixture(tmp_path)
    design = bind_artifact(spec, fake_artifact(store, spec))
    save_design(store, project, design)
    store.upsert_sample("target", "chip", props={"size_mm": [.5, .5]})
    with pytest.raises(ValueError, match="current declared target chip size"):
        validate_design_current(store, project)


def test_gateway_registers_new_tool_and_preserves_legacy_refusal(tmp_path, monkeypatch):
    store, project, placements, _ = design_fixture(tmp_path, wide=True)
    gateway = ToolGateway(default_registry(), store, None, project_id=project)
    monkeypatch.setattr(klayout, "create_artifact", fake_artifact)
    args = {"placements": [p.model_dump(mode="json") for p in placements], "contact_overlap_um": 1}
    denied, _ = asyncio.run(gateway.call("hands", "create_device_design", args))
    assert denied.status == "denied"
    result, _ = asyncio.run(gateway.call("designer", "create_device_design", args))
    assert result.status == "ok", result.summary
    assert DeviceDesignV1.model_validate(result.data["device_design"])
    old, _ = asyncio.run(gateway.call("designer", "submit_design", {"flake_ids": [p.flake_id for p in placements],
        "shapes": [{"layer": "channel", "x_um": 0, "y_um": 0, "w_um": 10, "h_um": 10}]}))
    assert old.status == "error" and "create_device_design" in old.summary


def test_missing_or_unsuccessful_klayout_does_not_create_acceptance(tmp_path, monkeypatch):
    from test_klayout_mcp import MockBridge

    store, project, placements, _ = design_fixture(tmp_path)
    MockBridge(monkeypatch, change="unpatched")
    gateway = ToolGateway(default_registry(), store, None, project_id=project)
    result, _ = asyncio.run(gateway.call("designer", "create_device_design", {
        "placements": [p.model_dump(mode="json") for p in placements], "contact_overlap_um": 1}))
    assert result.status == "error" and not store.get_project(project)["state"].get("design_accepted")
    with pytest.raises(klayout.KLayoutError, match="lacks atomic"):
        klayout.create_artifact(store, design_fixture(tmp_path / "other")[3])


def test_klayout_timeout_preserves_unaccepted_attempt(tmp_path, monkeypatch):
    from test_klayout_mcp import MockBridge

    store, _, _, spec = design_fixture(tmp_path)
    MockBridge(monkeypatch, change="timeout")
    with pytest.raises(klayout.KLayoutError, match="could not finish"):
        klayout.create_artifact(store, spec)
    assert (design_directory(store, spec.design_id) / "roundtrip.py").is_file()
