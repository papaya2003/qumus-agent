"""The operator's imported Hall-bar target, kept by the brain as well as by the sim lab.

The design tools (get_target_geometry, submit_device_plan, the layout) read the
target from the brain's own run folder, `<run>/targets/<gds sha256>/`, never
from a station's disk: on another computer the station's files are out of
reach. `serve --target-bundle` therefore copies the same bytes there (and passes
the bundle to a qumus-simlab it starts). Same rules as the sim lab's import:
regular sibling files, the GDS hash must match the target's import receipt, and
an existing copy is immutable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from qumus2.records.device_design import (
    ImportedHallBarTarget,
    imported_target_directory,
    parse_hallbar_target,
)

FILES = ("target.json", "electrode-layout.gds")


def import_target_bundle(bundle_dir: str | Path, workspace: str | Path) -> Path:
    """Copy target.json + electrode-layout.gds into `<workspace>/targets/<sha>/`."""
    bundle = Path(bundle_dir).resolve()
    if bundle.is_file():
        bundle = bundle.parent
    paths = {name: bundle / name for name in FILES}
    if any(not p.is_file() or p.is_symlink() or p.resolve().parent != bundle for p in paths.values()):
        raise ValueError(f"{bundle} must hold target.json and electrode-layout.gds as regular files")
    raw = {name: p.read_bytes() for name, p in paths.items()}
    target = parse_hallbar_target(json.loads(raw["target.json"]))
    if not isinstance(target, ImportedHallBarTarget):
        raise ValueError("an imported target needs its explicit versioned contract")
    if hashlib.sha256(raw["electrode-layout.gds"]).hexdigest() != target.import_receipt.source_sha256:
        raise ValueError("electrode-layout.gds does not match the target's import receipt")
    directory = imported_target_directory(Path(workspace), target)
    if directory.exists():
        for name, data in raw.items():
            saved = directory / name
            if saved.is_symlink() or not saved.is_file() or saved.read_bytes() != data:
                raise ValueError(f"{directory} already holds a different target; use a fresh run folder")
        return directory
    directory.mkdir(parents=True)
    for name, data in raw.items():
        with (directory / name).open("xb") as out:
            out.write(data)
    return directory
