from __future__ import annotations

import os
import re

_CURRENT_STACKING_RUN_DIR: str | None = None


def get_stacking_run_dirs(
    create_new: bool = False,
    run_number: int | None = None,
    sample_label: str | None = None,
) -> dict[str, str]:
    global _CURRENT_STACKING_RUN_DIR

    current = os.getcwd()
    base_dir = os.path.join(current, "images", "stacking")
    os.makedirs(base_dir, exist_ok=True)

    if run_number is not None:
        run_idx = int(run_number)
        if run_idx <= 0:
            raise ValueError("run_number must be a positive integer.")
        _CURRENT_STACKING_RUN_DIR = os.path.join(base_dir, f"run_{run_idx}")
    elif create_new or not _CURRENT_STACKING_RUN_DIR:
        existing: list[int] = []
        for name in os.listdir(base_dir):
            match = re.fullmatch(r"run_(\d+)", name)
            if match:
                existing.append(int(match.group(1)))
        next_run = max(existing, default=0) + 1
        # Reserve the directory atomically.  A long-lived server must never
        # reuse _CURRENT_STACKING_RUN_DIR for a new physical workflow, and two
        # nearly simultaneous requests must not receive the same run number.
        while True:
            candidate = os.path.join(base_dir, f"run_{next_run}")
            try:
                os.mkdir(candidate)
            except FileExistsError:
                next_run += 1
                continue
            _CURRENT_STACKING_RUN_DIR = candidate
            break

    run_dir = _CURRENT_STACKING_RUN_DIR
    if run_dir is None:
        raise RuntimeError("Failed to initialize stacking run directory.")

    sample_dir = run_dir
    if sample_label:
        sample_dir = os.path.join(run_dir, str(sample_label))

    dirs = {
        "base_dir": base_dir,
        "run_dir": run_dir,
        "sample_dir": sample_dir,
        "corner_dir": os.path.join(sample_dir, "corner"),
        "alignment_dir": os.path.join(sample_dir, "alignment"),
        "output_dir": os.path.join(sample_dir, "output"),
        "stacking_log_dir": os.path.join(sample_dir, "stacking_log"),
        "drop_log_dir": os.path.join(sample_dir, "drop_log"),
        "tp_init_dir": os.path.join(sample_dir, "tp_init"),
    }
    for path in dirs.values():
        os.makedirs(path, exist_ok=True)
    return dirs
