"""Headless klayout.db backend: real save/reopen in the cloud, bound receipts."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

db = pytest.importorskip("klayout.db")

from test_design_placement import REAL_TARGET, VERIFICATION, _random_convex, _spec  # noqa: E402
from test_device_design_pose_screen import declared_spec  # noqa: E402

from qumus2.agents import klayout  # noqa: E402
from qumus2.records.design_placement import solve_pair_placement  # noqa: E402
from qumus2.records.device_design import (  # noqa: E402
    DeviceDesignV1,
    KLayoutPythonArtifact,
    bind_artifact,
    design_directory,
    imported_target_directory,
    require_design_pose_window,
    validate_design_artifact,
)


def _store(tmp_path):
    return SimpleNamespace(path=str(tmp_path / "qumus.db"), bench="sim")


def _ordered_primitives():
    """Analytic unit primitives in KLayout's read order: polygons, paths, boxes.

    Same contact geometry as declared_import (voltage paths 18-23, drain box 32,
    source box 33); the other parts are distant filler metal.
    """
    parts = []
    for index in range(8):
        x, y = 100000 + index*3000, 100000
        parts.append({"index": index, "kind": "polygon", "points_dbu": [[x, y], [x+1000, y], [x, y+1000]]})
    for index in range(8, 18):
        x, y = 100000 + (index-8)*7000, 150000
        parts.append({"index": index, "kind": "path", "points_dbu": [[x, y], [x+5000, y]], "width_dbu": 1000,
                      "bgn_ext": 0, "end_ext": 0,
                      "outline_dbu": [[x, y-500], [x+5000, y-500], [x+5000, y+500], [x, y+500]]})
    for index, x, y, direction in ((18, 510000, 500000, 1), (19, 510000, 510000, 1), (20, 510000, 490000, 1),
                                   (21, 490000, 500000, -1), (22, 490000, 510000, -1), (23, 490000, 490000, -1)):
        end = x + direction*5000
        parts.append({"index": index, "kind": "path", "points_dbu": [[x, y], [end, y]], "width_dbu": 1000,
                      "bgn_ext": 0, "end_ext": 0,
                      "outline_dbu": [[min(x, end), y-500], [max(x, end), y-500],
                                      [max(x, end), y+500], [min(x, end), y+500]]})
    for index in range(24, 32):
        x, y = 100000 + (index-24)*3000, 200000
        parts.append({"index": index, "kind": "box", "box_dbu": [x, y, x+1000, y+1000]})
    parts.append({"index": 32, "kind": "box", "box_dbu": [499000, 479000, 501000, 481000]})
    parts.append({"index": 33, "kind": "box", "box_dbu": [499000, 519000, 501000, 521000]})
    return parts


def _unit_import(path: Path):
    """Write the unit primitives as an actual GDS, then import-bind those bytes."""
    import hashlib

    from qumus2.testbench.hallbar import build_imported_hallbar_target

    parts = _ordered_primitives()
    layout = db.Layout()
    layout.dbu = .001
    top = layout.create_cell("QUMUS_DEVICE")
    layer = layout.layer(3, 0)
    for part in parts:
        if part["kind"] == "box":
            top.shapes(layer).insert(db.Box(*part["box_dbu"]))
        elif part["kind"] == "path":
            top.shapes(layer).insert(db.Path([db.Point(*p) for p in part["points_dbu"]], part["width_dbu"], 0, 0))
        else:
            top.shapes(layer).insert(db.Polygon([db.Point(*p) for p in part["points_dbu"]]))
    layout.write(str(path))
    raw = path.read_bytes()
    target = build_imported_hallbar_target(primitives=parts, source_sha256=hashlib.sha256(raw).hexdigest(),
        owner_instruction_id="declared-unit-owner-instruction", owner_instruction_sha256="a"*64, size_mm=(1., 1.))
    return target, raw


def _imported_fixture(tmp_path, *, target=None):
    store = _store(tmp_path)
    if target is None:
        target, raw = _unit_import(tmp_path / "source.gds")
    else:
        raw = (REAL_TARGET / "electrode-layout.gds").read_bytes()
    directory = imported_target_directory(tmp_path, target)
    directory.mkdir(parents=True)
    (directory / "electrode-layout.gds").write_bytes(raw)
    (directory / "target.json").write_text(json.dumps(target.model_dump(mode="json")))
    return store, target


def _solved_spec(target, gr_radii=(26, 36), hbn_radii=(50, 60), seed=1):
    gr = {"flake_id": "gr", "outline_mm": _random_convex(seed, *gr_radii)}
    hbn = {"flake_id": "hbn", "outline_mm": _random_convex(100+seed, *hbn_radii)}
    result = solve_pair_placement(target, hbn, gr, verification=VERIFICATION)
    assert result["status"] == "fit"
    return _spec(target, hbn, gr, result)


def test_backend_selection_is_explicit(monkeypatch):
    monkeypatch.delenv("QUMUS_KLAYOUT_BACKEND", raising=False)
    assert klayout.selected_backend() == "mcp"
    monkeypatch.setenv("QUMUS_KLAYOUT_BACKEND", "python")
    assert klayout.selected_backend() == "python"
    monkeypatch.setenv("QUMUS_KLAYOUT_BACKEND", "batch")
    with pytest.raises(klayout.KLayoutError, match="must be"):
        klayout.selected_backend()


def test_legacy_target_round_trips_headlessly(tmp_path):
    store, spec = _store(tmp_path), declared_spec()
    artifact = klayout.create_artifact_python(store, spec)
    assert isinstance(artifact, KLayoutPythonArtifact)
    design = bind_artifact(spec, artifact)
    assert validate_design_artifact(store, design) == design
    directory = design_directory(store, spec.design_id)
    assert {p.name for p in directory.iterdir()} >= {"device.gds", "roundtrip.py", "verified.json", "preview.png"}
    assert not (directory / "native.png").exists()


def test_solved_imported_design_round_trips_and_survives_reparse(tmp_path):
    store, target = _imported_fixture(tmp_path)
    spec = _solved_spec(target)
    require_design_pose_window(spec, verification=VERIFICATION)
    design = bind_artifact(spec, klayout.create_artifact_python(store, spec))
    reparsed = DeviceDesignV1.model_validate(json.loads(json.dumps(design.model_dump(mode="json"))))
    assert isinstance(reparsed.artifact, KLayoutPythonArtifact)
    assert validate_design_artifact(store, reparsed) == design
    saved = db.Layout()
    saved.read(design.artifact.path)
    assert {saved.get_info(i).layer for i in saved.layer_indices()} == {1, 2, 3}


@pytest.mark.parametrize("change", ["vertex", "receipt", "preview"])
def test_tampered_headless_evidence_is_refused(tmp_path, change):
    store, target = _imported_fixture(tmp_path)
    spec = _solved_spec(target)
    design = bind_artifact(spec, klayout.create_artifact_python(store, spec))
    directory = design_directory(store, spec.design_id)
    if change == "vertex":
        layout = db.Layout()
        layout.read(str(directory / "device.gds"))
        top = layout.top_cell()
        shape = next(iter(top.shapes(layout.find_layer(2, 0)).each()))
        polygon = shape.polygon
        points = list(polygon.each_point_hull())
        points[0] = db.Point(points[0].x + 1, points[0].y)
        shape.polygon = db.Polygon(points)
        layout.write(str(directory / "device.gds"))
    elif change == "receipt":
        receipt = json.loads((directory / "verified.json").read_text())
        receipt["zoom_box_um"][0] -= 1
        (directory / "verified.json").write_text(json.dumps(receipt))
    else:
        (directory / "preview.png").write_bytes((directory / "preview.png").read_bytes() + b"\0")
    with pytest.raises(ValueError):
        validate_design_artifact(store, design)


def test_wrong_original_gds_bytes_block_creation(tmp_path):
    store, target = _imported_fixture(tmp_path)
    spec = _solved_spec(target)
    source = imported_target_directory(tmp_path, target) / "electrode-layout.gds"
    source.write_bytes(source.read_bytes() + b"\0")
    with pytest.raises((ValueError, klayout.KLayoutError)):
        klayout.create_artifact_python(store, spec)
    assert not (design_directory(store, spec.design_id) / "verified.json").exists()


@pytest.mark.skipif(not (REAL_TARGET / "target.json").exists(), reason="private lab target not present")
def test_owner_target_solved_design_round_trips(tmp_path):
    from qumus2.testbench.hallbar import load_imported_hallbar_target

    store, target = _imported_fixture(tmp_path, target=load_imported_hallbar_target(REAL_TARGET))
    spec = _solved_spec(target, gr_radii=(23, 27), hbn_radii=(46, 50), seed=7)  # fits at 0 deg (#204)
    require_design_pose_window(spec, verification=VERIFICATION)
    design = bind_artifact(spec, klayout.create_artifact_python(store, spec))
    assert validate_design_artifact(store, design) == design


def test_designer_tools_end_to_end_with_suggested_placement(tmp_path, monkeypatch):
    """Context suggestion -> create_device_design (headless GDS) -> current for stacking."""
    import asyncio

    from test_device_design import design_fixture

    from qumus2.agents.gateway import ToolGateway
    from qumus2.agents.tooling import default_registry
    from qumus2.records.device_design import validate_design_current

    monkeypatch.setenv("QUMUS_KLAYOUT_BACKEND", "python")
    store, project, _, _ = design_fixture(tmp_path, wide=True)
    gateway = ToolGateway(default_registry(), store, None, project_id=project)
    context, _ = asyncio.run(gateway.call("designer", "get_design_context", {}))
    suggestion = context.data["suggested_placement"]
    assert suggestion["status"] == "fit", suggestion["summary"]
    result, _ = asyncio.run(gateway.call("designer", "create_device_design", {
        "placements": suggestion["placements"], "contact_overlap_um": 1}))
    assert result.status == "ok", result.summary
    assert result.data["transport"] == "klayout_python"
    assert result.images and result.images[0].endswith("preview.png")
    # The accepted design states its distance margins, equal to the suggestion's.
    margins = result.data["margins_um"]
    assert margins["gr_headroom_um"] == pytest.approx(suggestion["gr_headroom_um"], abs=1e-6)
    assert margins["hbn_headroom_um"] == pytest.approx(suggestion["hbn_headroom_um"], abs=1e-6)
    assert "Margins: Gr holds every electrode end" in result.summary
    design = validate_design_current(store, project)
    assert isinstance(design.artifact, KLayoutPythonArtifact)
    refused, _ = asyncio.run(gateway.call("designer", "report_design_infeasible", {
        "flake_ids": ["flake_hBN", "flake_Gr"], "failed_roles": ["channel"], "reason": "guess"}))
    assert refused.status == "error" and "fits" in refused.summary


def test_non_fitting_selection_is_handed_back_with_measured_shortfall(tmp_path):
    import asyncio

    from test_device_design import design_fixture

    from qumus2.agents.gateway import ToolGateway
    from qumus2.agents.tooling import default_registry

    store, project, _, _ = design_fixture(tmp_path)  # 20 x 40 um Gr under a 24 x 44 um contact set
    gateway = ToolGateway(default_registry(), store, None, project_id=project)
    context, _ = asyncio.run(gateway.call("designer", "get_design_context", {}))
    suggestion = context.data["suggested_placement"]
    assert suggestion["status"] == "no_fit" and suggestion["limiting"] == "gr_contacts"
    assert suggestion["shortfall_um"] > 2
    result, _ = asyncio.run(gateway.call("designer", "report_design_infeasible", {
        "flake_ids": ["flake_hBN", "flake_Gr"], "failed_roles": ["channel"], "reason": "solver no_fit"}))
    assert result.status == "ok"
    failure = store.get_project(project)["state"]["design_fit_failure"]
    assert failure["measured"]["shortfall_um"] == pytest.approx(suggestion["shortfall_um"])
