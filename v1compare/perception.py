"""Desktop-camera stand-ins for v1: a fake `ultralytics.YOLO` and a fake webcam.

v1's YOLO weights are not in the zip, and the v2 testbench has no real
pixels to run them on. As in v2 (testbench/locator.py), perception reads the
ground truth of the sim world and adds the locator noise. What v1 gets is in
v1's own format: one result per frame with segmentation polygons, class ids
and names exactly as its two models report them. Everything after that
(pixel -> mm calibration from the table QR markers, bare/exfoliated split at
the centre line, pickup-pose math, glass-slide pairing, database writes) is
v1's own code.

Not modelled: missed or false detections, held objects (hidden by the
nozzle), objects on the transfer stage (out of the right camera's view in
this layout). QR text on carriers is read perfectly (v1 used pyboof).
"""

from __future__ import annotations

import math
import types
from typing import Any

import numpy as np

from qumus2.testbench.world import TAPE_HOLDER
from v1compare.calibration import FRAME_H, FRAME_W, PX_PER_MM, ArmMap, v1_to_px

LEFT_NAMES = {0: "exfoliation stage", 1: "glass slide micro qr", 2: "orange square",
              3: "scotch tape crystal", 4: "silicon chip", 5: "table micro qr"}
RIGHT_NAMES = {0: "glass slide micro qr", 1: "orange square", 2: "transfer stage",
               3: "silicon chip", 5: "table micro qr", 6: "stamp holder"}
CLASS = {
    "left": {"stage": 0, "qr": 1, "pickup": 2, "crystal": 3, "chip": 4, "table_qr": 5},
    "right": {"qr": 0, "pickup": 1, "stage": 2, "chip": 3, "table_qr": 5},
}
# stage_geometry.py fiducials (arm frame, mm)
FIDUCIALS = {
    "left": [(-320.0, 214.0), (-320.0, 18.0), (328.0, 15.0), (328.0, 210.0)],
    "right": [(-328.0, 210.0), (-328.0, 15.0), (326.0, 14.0), (326.0, 209.0)],
}
CARRIER_QR_OFFSET_MM = 61.0  # glass slide: QR 146 px (61 mm) left of the pickup square


class Context:
    """Set by the bench: the sim world and where v1's cameras look."""

    world: Any = None
    maps: dict[str, ArmMap] = {}
    last_qr: dict[str, list[tuple[float, float, str, float]]] = {"left": [], "right": []}


def _square(cx: float, cy: float, half_mm: float, rot_deg: float = 0.0) -> np.ndarray:
    """Polygon (px) of a square centred at v1 (cx, cy) mm."""
    r = math.radians(rot_deg)
    pts = []
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        x = cx + half_mm * (sx * math.cos(r) - sy * math.sin(r))
        y = cy + half_mm * (sx * math.sin(r) + sy * math.cos(r))
        pts.append(v1_to_px(x, y))
    return np.array(pts, dtype=np.float32)


def detect(side: str) -> list[tuple[int, np.ndarray, float, dict[str, Any]]]:
    """(class id, polygon px, confidence, info) for everything this camera sees."""
    world, amap, cls = Context.world, Context.maps[side], CLASS[side]
    out: list[tuple[int, np.ndarray, float, dict[str, Any]]] = []
    for fx, fy in FIDUCIALS[side]:
        out.append((cls["table_qr"], _square(fx, fy, 4.0), 0.95, {}))
    if side == "left":
        out.append((cls["stage"], _square(0.0, 262.0, 25.0), 0.95, {}))
    f, r = world.faults, world.rng
    qrs: list[tuple[float, float, str, float]] = []
    with world.lock:
        tape = world.tape
        if side == "left" and tape.material:
            out.append((cls["crystal"], _square(0.0, 262.0, 8.0), 0.9, {}))
        stage = world.surfaces.get("exfoliation_stage")
        for o in world.objects.values():
            if o.kind not in ("chip", "carrier"):
                continue
            if o.held_by not in (None, TAPE_HOLDER):
                continue  # in the nozzle (hidden) or on the stamp manipulator
            on_stage = o.held_by == TAPE_HOLDER or (
                o.held_by is None and stage is not None and stage.contains(o.pose.x, o.pose.y))
            if o.on == "transfer_stage":
                continue
            wx = o.pose.x + r.gauss(0, f.locator_sigma_mm)
            wy = o.pose.y + r.gauss(0, f.locator_sigma_mm)
            v = amap.world_to_v1(wx, wy, o.kind, on_stage=on_stage)
            if v is None:
                continue
            # world rotation -> v1 frame (a quarter turn, x_v1 = wy)
            rot = o.pose.rotation + r.gauss(0, f.locator_sigma_deg)
            if o.kind == "chip":
                out.append((cls["chip"], _square(v[0], v[1], o.size_mm[0] / 2, rot), 0.93,
                            {"id": o.id}))
            else:
                qx, qy = v[0] - CARRIER_QR_OFFSET_MM, v[1]
                out.append((cls["pickup"], _square(v[0], v[1], 4.0), 0.92, {"id": o.id}))
                out.append((cls["qr"], _square(qx, qy, 4.0), 0.92, {"id": o.id}))
                # the lab writes the material code on the QR (testbench.yaml qr: "Gr")
                qrs.append((*v1_to_px(qx, qy), o.material or "", 0.0))
    Context.last_qr[side] = qrs
    return out


class _Masks:
    def __init__(self, polys: list[np.ndarray], cls: list[int], conf: list[float]) -> None:
        self.xy = polys
        self.cls = np.array(cls, dtype=float)
        self.conf = np.array(conf, dtype=float)
        self.scores = None


class _Result:
    def __init__(self, side: str) -> None:
        dets = detect(side)
        self.names = LEFT_NAMES if side == "left" else RIGHT_NAMES
        self.masks = _Masks([d[1] for d in dets], [d[0] for d in dets], [d[2] for d in dets]) \
            if dets else None
        self.boxes = None


class FakeYOLO:
    """ultralytics.YOLO(model_path): left_yolo.pt / right_yolo.pt pick the camera."""

    def __init__(self, model_path: str, *args: Any, **kwargs: Any) -> None:
        self.side = "right" if "right" in str(model_path).lower() else "left"

    def predict(self, frame: Any, conf: float = 0.5, verbose: bool = False,
                **kwargs: Any) -> list[_Result]:
        return [_Result(self.side)]

    __call__ = predict


def fake_pyboof_module() -> types.ModuleType:
    """pyboof (micro-QR decoding) is not installed: v1's own fallbacks run
    (table QR order from positions; carrier QR text via read_carrier_qr)."""

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("pyboof is not available on the testbench")

    mod = types.ModuleType("pyboof")
    mod.FactoryFiducial = unavailable
    mod.ndarray_to_boof = unavailable
    mod.fiducial = types.SimpleNamespace(MicroQR=object)
    return mod


def fake_ultralytics_module() -> types.ModuleType:
    mod = types.ModuleType("ultralytics")
    mod.YOLO = FakeYOLO
    return mod


class SimVideoCapture:
    """cv2.VideoCapture for the two desktop cameras: a plain grey frame (the
    fake YOLO reads the world, not the pixels)."""

    def __init__(self, index: Any = 0, *args: Any) -> None:
        self.index = index
        self._open = True
        self._frame = np.full((FRAME_H, FRAME_W, 3), 128, dtype=np.uint8)

    def isOpened(self) -> bool:  # noqa: N802 - cv2 name
        return self._open

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._open:
            return False, None
        return True, self._frame.copy()

    def set(self, prop: int, value: float) -> bool:
        return True

    def get(self, prop: int) -> float:
        return 0.0

    def release(self) -> None:
        self._open = False


def read_carrier_qr(side: str, bbox: tuple[int, int, int, int]) -> str:
    """Stand-in for v1's pyboof micro-QR decoder on a glass slide: the text of
    the carrier whose QR square is at this bounding box."""
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    best = min(Context.last_qr.get(side, []), default=None,
               key=lambda q: math.hypot(q[0] - cx, q[1] - cy))
    if best is None or math.hypot(best[0] - cx, best[1] - cy) > 10 * PX_PER_MM:
        return "Not found"
    return best[2] or "Not found"
