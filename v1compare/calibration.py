"""Map v1's arm coordinates onto the v2 testbench world (and back).

v1 hard-codes its bench in each arm's own frame (robot_arm_controller.py:
exfoliation stage (0, 256), transfer stage (10, 300), massage strip, homes)
and keeps chip slots and carriers in its database. The v2 testbench follows
the real lab's layout (config/bench.yaml): left board, exfoliation stage,
middle board, search & stacking stage, right board, each arm in front of its
stations. v1's frames point the same way (x to the right, y away from the
operator; its chip slots S1..S14 are right of the left arm and left of the
right arm, on the middle board), so every part maps by a translation; the
part sizes still differ, so the map is piecewise:

- zones: a rectangular part of the table moved as a whole (a translation per
  zone). Objects inside a zone keep their relative distances, so v1's own
  perception and pickup math works.
- anchors: the fixed spots v1 hard-codes (stage drop-off points, the massage
  strip, the homes). A point near an anchor maps by translation from it.
  Several v1 anchors may land on the same world spot: v1 puts the carrier
  27 mm below the chip spot on its exfoliation stage, the v2 stage holds one
  object at its centre.

Axes: x_v1 = world_x + dx, y_v1 = world_y + dy. The world poses are then
commanded in the v2 arm's own frame (world - arm base, bench.yaml). Nozzle
rotation (v1 M2101) is not mapped: the testbench assumes v1's nose rotation
does what v1 intends and keeps the object's orientation (placement noise
still applies).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# v1 bench constants (robot_arm_controller.py, seed_chip_slots.py, stage_geometry.py)
V1_EXFOLIATION_STAGE = (0.0, 256.0)
V1_TRANSFER_STAGE = (10.0, 300.0)
ANCHOR_RADIUS_MM = 25.0


@dataclass(frozen=True)
class Zone:
    name: str
    world_x: tuple[float, float]
    world_y: tuple[float, float]
    dx: float  # x_v1 = wx + dx
    dy: float  # y_v1 = wy + dy
    dz: float  # z_world = z_v1 + dz

    def world_contains(self, wx: float, wy: float) -> bool:
        return (self.world_x[0] <= wx <= self.world_x[1]
                and self.world_y[0] <= wy <= self.world_y[1])

    def to_v1(self, wx: float, wy: float) -> tuple[float, float]:
        return wx + self.dx, wy + self.dy

    def to_world(self, x: float, y: float) -> tuple[float, float]:
        return x - self.dx, y - self.dy

    def v1_contains(self, x: float, y: float) -> bool:
        wx, wy = self.to_world(x, y)
        return self.world_contains(wx, wy)


@dataclass(frozen=True)
class Anchor:
    name: str
    v1: tuple[float, float, float]  # x, y, z in the v1 arm frame
    world: tuple[float, float, float]

    def to_world(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        dx, dy = x - self.v1[0], y - self.v1[1]
        return self.world[0] + dx, self.world[1] + dy, z - self.v1[2] + self.world[2]

    def to_v1(self, wx: float, wy: float) -> tuple[float, float]:
        return self.v1[0] + wx - self.world[0], self.v1[1] + wy - self.world[1]


# Shared world parts (bench.yaml stations, world frame). v1 keeps exfoliated
# chips in its chip slots S1..S14 (left frame x 200..320, y 20..160; the right
# frame is the left one shifted by -520 mm in x, seed_chip_slots.py): they land
# on the middle board, which both arms reach.
STORAGE_WORLD = ((460.0, 600.0), (60.0, 220.0))     # middle board (+ v1's slot margin)
LEFT_TABLE_WORLD = ((40.0, 170.0), (55.0, 225.0))   # left board: bare chips + carriers
DEVICE_RACK_WORLD = ((890.0, 1020.0), (55.0, 225.0))  # right board: electrode chips
ZONE_DZ = -13.0  # v1 picks at z15 -> world nozzle z2 (bench.yaml pick_z)

LEFT_ZONES = (
    Zone("left_table", *LEFT_TABLE_WORLD, dx=-420.0, dy=-20.0, dz=ZONE_DZ),
    Zone("storage", *STORAGE_WORLD, dx=-270.0, dy=-50.0, dz=ZONE_DZ),
)
RIGHT_ZONES = (
    Zone("storage", *STORAGE_WORLD, dx=-790.0, dy=-50.0, dz=ZONE_DZ),
    Zone("device_rack", *DEVICE_RACK_WORLD, dx=-795.0, dy=-20.0, dz=ZONE_DZ),
)

_EXF = (307.0, 318.0)  # bench.yaml stations.exfoliation_stage
_TRANSFER = (735.0, 300.0)  # bench.yaml stations.transfer_stage (load position)
LEFT_ANCHORS = (
    Anchor("exfoliation_stage_chip", (10.0, 256.0, 80.0), (*_EXF, 3.0)),
    Anchor("exfoliation_stage_carrier", (0.0, 229.0, 85.0), (*_EXF, 3.0)),
    Anchor("massage", (0.0, 298.0, 74.0), (*_EXF, 1.0)),
    Anchor("home", (0.0, 220.0, 95.0), (307.0, 236.0, 120.0)),  # arm_left home, world
)
RIGHT_ANCHORS = (
    Anchor("transfer_stage", (0.0, 300.0, 132.0), (*_TRANSFER, 3.0)),
    Anchor("home", (0.0, 260.0, 140.0), (765.0, 246.0, 120.0)),  # arm_right home, world
)

# Where each camera shows objects lying on a stage (v1 frame of that side).
STAGE_VIEW = {
    "left": {"chip": "exfoliation_stage_chip", "carrier": "exfoliation_stage_carrier"},
    "right": {},
}


class ArmMap:
    def __init__(self, side: str) -> None:
        self.side = side
        self.zones = LEFT_ZONES if side == "left" else RIGHT_ZONES
        self.anchors = {a.name: a for a in (LEFT_ANCHORS if side == "left" else RIGHT_ANCHORS)}
        self.unmapped: list[tuple[float, float, float]] = []

    def to_world(self, x: float, y: float, z: float) -> tuple[float, float, float, str]:
        """v1 arm command -> world nozzle pose and the name of the part used."""
        best = min(self.anchors.values(), key=lambda a: math.hypot(x - a.v1[0], y - a.v1[1]))
        if math.hypot(x - best.v1[0], y - best.v1[1]) <= ANCHOR_RADIUS_MM:
            return (*best.to_world(x, y, z), best.name)
        for zone in self.zones:
            if zone.v1_contains(x, y):
                return (*zone.to_world(x, y), z + zone.dz, zone.name)
        # Outside everything v1 should ever visit: keep a consistent mapping and
        # let the caller log it.
        self.unmapped.append((x, y, z))
        zone = self.zones[0]
        return (*zone.to_world(x, y), z + zone.dz, "unmapped")

    def world_to_v1(self, wx: float, wy: float, kind: str,
                    on_stage: bool = False) -> tuple[float, float] | None:
        """Where this side's camera sees a world object (None = not in view)."""
        if on_stage:
            anchor = STAGE_VIEW[self.side].get(kind)
            if anchor is None:
                return None
            a = self.anchors[anchor]
            return a.v1[0] + (wx - _EXF[0]), a.v1[1] + (wy - _EXF[1])
        for zone in self.zones:
            if zone.world_contains(wx, wy):
                return zone.to_v1(wx, wy)
        return None


# Desktop camera model: one affine pixel map per camera, in that arm's frame.
# 1920 x 1080 frames, 2.4 px/mm, the centre line u = 960 is x_v1 = 0 (v1's
# _relabel splits bare/exfoliated chips there). The four table QR fiducials of
# stage_geometry.py land inside the frame, so v1 calibrates itself from them.
PX_PER_MM = 2.4
FRAME_W, FRAME_H = 1920, 1080


def v1_to_px(x: float, y: float) -> tuple[float, float]:
    return (x + 400.0) * PX_PER_MM, (250.0 - y) * PX_PER_MM


def px_to_v1(u: float, v: float) -> tuple[float, float]:
    return u / PX_PER_MM - 400.0, 250.0 - v / PX_PER_MM
