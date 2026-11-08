"""Moving private evidence must preserve pixels, containment and spend limits."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from qumus2.agents.openai_budget import SpendLedger
from qumus2.agents.openai_runtime import tool_output
from qumus2.agents.tools.picking import has_images
from qumus2.app import build_services
from qumus2.core import run_paths
from qumus2.ui.app import create_app
from qumus2.ui.recording import export_recording, load_recording


@pytest.fixture
def relocated(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    workspace = runs / "workspace"
    workspace.mkdir(parents=True)
    (runs / "layout.json").write_text(json.dumps({
        "version": 1, "original_root": str(runs),
        "moves": {"desktop-openai": "workspace", "old-check": "checks/model"}}))
    monkeypatch.setattr(run_paths, "RUNS_ROOT", runs)
    return runs, workspace


def test_old_images_still_view_export_and_reach_model(relocated, tmp_path):
    runs, workspace = relocated
    old_image = str(runs / "desktop-openai" / "images" / "flake.png")
    image = workspace / "images" / "flake.png"
    image.parent.mkdir()
    Image.new("RGB", (3, 3), "red").save(image)
    s = build_services(workspace / "qumus.db")
    s.store.emit("tool.result", {"images": [old_image]}, agent="eyes")
    before = s.store.events_after(0)[-1].payload
    # A relocated foreign image must not expand the serving/export boundary.
    other = runs / "checks/model/foreign.png"
    other.parent.mkdir(parents=True)
    Image.new("RGB", (3, 3)).save(other)
    foreign = str(runs / "old-check/foreign.png")
    with TestClient(create_app(s, start_driver=False)) as client:
        assert client.get("/api/image", params={"path": old_image}).content == image.read_bytes()
        for value in (foreign, str(workspace / "../checks/model/foreign.png"), "/etc/passwd"):
            assert client.get("/api/image", params={"path": value}).status_code == 404
    out = export_recording(s.store.path, tmp_path / "export")
    rec = load_recording(out)
    saved = next(e["payload"]["images"][0] for e in rec["events"] if e["kind"] == "tool.result")
    assert saved.startswith("media/") and (out / saved).read_bytes() == image.read_bytes()
    assert has_images({"props": {"images": {"20x": old_image, "100x": old_image}}})
    outputs = tool_output(SimpleNamespace(images=[old_image], to_text=lambda: "saved photo"))
    assert len(outputs) == 2 and outputs[1].image_url.startswith("data:image/png;base64,")
    assert s.store.events_after(0)[-1].payload == before
    s.store.close()


def test_moved_scope_cannot_reset_budget(relocated):
    runs, workspace = relocated
    original = str(runs / "desktop-openai/qumus.db")
    ledger = SpendLedger(runs / "budget.db", 200)
    reservation = ledger.reserve(original, 19, 20)
    ledger.settle(reservation, 19)
    scope = str(run_paths.original_run_path(workspace / "qumus.db"))
    assert scope == original and ledger.total(scope) == 19
    with pytest.raises(ValueError, match="budget"):
        ledger.reserve(scope, 2, 20)
    assert ledger.total() == 19


def test_manifest_rejects_escape_and_corruption(relocated):
    runs, workspace = relocated
    (runs / "layout.json").write_text(json.dumps({
        "version": 1, "original_root": str(runs), "moves": {"old": "../elsewhere"}}))
    with pytest.raises(ValueError, match="inside"):
        run_paths.resolve_run_path(workspace / "x.png")
    (runs / "layout.json").write_text("{broken")
    with pytest.raises(ValueError):
        run_paths.original_run_path(workspace / "qumus.db")
