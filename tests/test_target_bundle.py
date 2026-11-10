"""serve --target-bundle: the brain keeps its own copy of the operator's target."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from qumus2.stations.target_bundle import import_target_bundle

BUNDLE = os.environ.get("QUMUS_TARGET_BUNDLE",
                        "/mnt/project-files/lab-inputs/electrode-wafer-20261102/target-a518-v1chip")


@pytest.mark.skipif(not Path(BUNDLE, "target.json").is_file(), reason="operator target bundle not here")
def test_import_copies_same_bytes_once(tmp_path):
    d = import_target_bundle(BUNDLE, tmp_path)
    gds = (Path(BUNDLE) / "electrode-layout.gds").read_bytes()
    assert d.name == hashlib.sha256(gds).hexdigest() and d.parent == tmp_path / "targets"
    assert (d / "electrode-layout.gds").read_bytes() == gds
    assert import_target_bundle(BUNDLE, tmp_path) == d  # same bytes again: fine


@pytest.mark.skipif(not Path(BUNDLE, "target.json").is_file(), reason="operator target bundle not here")
def test_mismatched_gds_refused(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "electrode-layout.gds").write_bytes(b"not the receipt's bytes")
    (b / "target.json").write_bytes((Path(BUNDLE) / "target.json").read_bytes())
    with pytest.raises(ValueError, match="import receipt"):
        import_target_bundle(b, tmp_path / "run")
