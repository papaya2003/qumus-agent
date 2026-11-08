"""Score one trial of the graphene task against the testbench's ground truth.

Both systems leave two things behind: the simulated world (where every object
really is, which flakes really exist) and their own records (v1: its SQLite
inventory; v2: the sample ledger). A trial is a real success only if the
world shows the job done, the records point at real flakes and the records
put every exfoliated chip where it really is (the same rule as `qumus2 chaos`:
a wrong ledger is a failure, not a detail). A system that says it succeeded
when the world disagrees is a false success report. Chips exfoliated beyond
what the task asked for are a cost (`extra_chips_used`), not a failure.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from qumus2.testbench.locator import thickness_label

# Where each system keeps exfoliated chips (world frame, mm).
STORAGE = {
    "v1": {"x": (460.0, 600.0), "y": (60.0, 220.0)},  # v1compare/calibration.py STORAGE_WORLD
    "v2": {"x": (470.0, 590.0), "y": (60.0, 220.0)},  # bench.yaml stations.middle_storage
}
LEDGER_TOL_MM = 3.0
CHIPS_ASKED = 1  # the task asks for one exfoliated chip


@dataclass
class Score:
    system: str
    claimed_success: bool
    truth_success: bool = False
    false_success: bool = False
    exfoliated_chips: int = 0
    extra_chips_used: int = 0  # exfoliated beyond what the task asked for
    true_flakes: int = 0
    true_monolayers: int = 0
    recorded_flakes: int = 0
    recorded_real: int = 0
    recorded_label_correct: int = 0
    recorded_monolayers_real: int = 0
    chip_stored: bool = False
    ledger_ok: bool | None = None
    stray_objects: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _inside(box: dict[str, tuple[float, float]], x: float, y: float) -> bool:
    return box["x"][0] <= x <= box["x"][1] and box["y"][0] <= y <= box["y"][1]


def score(system: str, world: Any, *, claimed_success: bool, records: list[dict[str, Any]],
          ledger: dict[str, tuple[float, float] | None]) -> Score:
    """`records`: flakes the system catalogued, each {"sim_id", "label", "chip"}
    (sim_id None when the record matches no real flake). `ledger`: where the
    system believes each exfoliated chip is (world x, y; None = unknown)."""
    s = Score(system=system, claimed_success=claimed_success)
    with world.lock:
        objects = list(world.objects.values())
    chips = [o for o in objects if o.kind == "chip" and o.flakes]
    s.exfoliated_chips = len(chips)
    s.extra_chips_used = max(0, len(chips) - CHIPS_ASKED)
    truth = {f.id: (o.id, f) for o in chips for f in o.flakes}
    s.true_flakes = len(truth)
    s.true_monolayers = sum(1 for _, f in truth.values() if f.layers == 1)

    s.recorded_flakes = len(records)
    seen: set[str] = set()
    for r in records:
        sid = r.get("sim_id")
        if sid not in truth or sid in seen:
            continue  # not a real flake, or the same flake catalogued twice
        seen.add(sid)
        s.recorded_real += 1
        _, f = truth[sid]
        if r.get("label") == thickness_label(f.material, f.layers):
            s.recorded_label_correct += 1
        if f.layers == 1 and r.get("label") == "monolayer":
            s.recorded_monolayers_real += 1

    for o in objects:
        if o.held_by is not None and o.kind in ("chip", "carrier"):
            s.stray_objects.append(f"{o.id} still held by {o.held_by}")
        elif o.on == "transfer_stage":
            s.stray_objects.append(f"{o.id} left on the transfer stage")
    stored = [o for o in chips if o.held_by is None and o.on is None
              and _inside(STORAGE[system], o.pose.x, o.pose.y)]
    s.chip_stored = bool(chips) and len(stored) == len(chips)

    checks = []
    for o in chips:
        believed = ledger.get(o.id)
        if believed is None:
            checks.append(False)
            s.problems.append(f"{o.id}: the system has no position for it")
            continue
        d = ((believed[0] - o.pose.x) ** 2 + (believed[1] - o.pose.y) ** 2) ** 0.5
        checks.append(d <= LEDGER_TOL_MM)
        if d > LEDGER_TOL_MM:
            s.problems.append(f"{o.id}: records say ({believed[0]:.1f}, {believed[1]:.1f}), "
                              f"really at ({o.pose.x:.1f}, {o.pose.y:.1f})")
    s.ledger_ok = all(checks) if checks else None

    if not chips:
        s.problems.append("no chip carries flakes: exfoliation did not happen")
    if s.extra_chips_used:
        s.problems.append(f"{len(chips)} chips exfoliated, the task asked for {CHIPS_ASKED}")
    if s.recorded_real == 0:
        s.problems.append("no real flake catalogued")
    if s.recorded_flakes > s.recorded_real:
        s.problems.append(f"{s.recorded_flakes - s.recorded_real} catalogued flake(s) "
                          "do not match a real, distinct flake")
    if chips and not s.chip_stored:
        s.problems.append("exfoliated chip is not back in storage")
    s.problems.extend(s.stray_objects)
    s.truth_success = bool(chips) and s.recorded_real > 0 and s.chip_stored \
        and not s.stray_objects and s.ledger_ok is True
    s.false_success = s.claimed_success and not s.truth_success
    return s
