"""Microscope camera and flake segmenter stand-ins for v1.

v1's microscope camera is a Touptek/UVCHAM USB camera with its own lens
autofocus (camera_controller.py); its flake finder is a colour-clustering
segmenter (segmenter/livesegment.py) tuned on real images. Neither exists on
the testbench, so:

- `SimCameraController` subclasses v1's CameraController and replaces only the
  device part: frames are drawn on demand from the v2 sim microscope (chip
  outline and flakes on the transfer stage, at the current objective, stage
  position and focus), the lens autofocus is a small model, and the focus
  score is the v2 sim sharpness. v1's own snap / take_photo / focus-sweep
  logic runs unchanged on top. A frame costs one frame period (1/fps) of sim
  time, like waiting for the next frame from the camera.
- `install_segmenter` patches v1's LiveSegment in place: only the model step
  (`_run_with_consensus`, which ran Segmenter on the pixels) is replaced by
  v2's SimMicroscopeAnalyzer on the ground truth of the frame that was saved
  under that file name. Candidate filtering, revisit-target choice, the
  result text/JSONL formats and every file v1 writes stay v1's code.

Image geometry (both directions follow from v1's own pixel -> stage math in
molecule_autosearch._pixel_to_stage and molecule_corner_detection): image
column grows with world +x, image row with world +y, the optical axis is the
image centre (1780, 1180), pixel size per objective as v1 calibrated it.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import threading
from typing import Any

import numpy as np

from qumus2.devices.base import Frame
from qumus2.testbench.locator import SimMicroscopeAnalyzer

IMG_W, IMG_H = 3560, 2360
AXIS_COL, AXIS_ROW = IMG_W / 2, IMG_H / 2
PIXEL_UM = {5: 0.73, 10: 0.365, 20: 0.182, 50: 0.073, 100: 0.036}
# Lens autofocus position (UVCHAM AFPOSITION, 0..854) -> focal plane shift.
# 10X/20X from v1's own focus-motor correction (molecule_autosearch a1/a2, um
# per AF unit); 5X/50X/100X scaled by 1/M^2.
AF_UM_PER_UNIT = {5: 72.5, 10: 18.1367, 20: 4.4029, 50: 0.725, 100: 0.181}
AF_REF, AF_MIN, AF_MAX = 330, 0, 854
AF_SETTLE_S = 0.4  # lens travel + contrast search before the position holds
AF_NOISE_UNITS = 1.0
BLUR_PER_SHARPNESS = 0.8  # v1 Tenengrad log-score ~ 0.8 x v2 sharpness (0.5..10.5)

BG_BGR = (60, 60, 60)
CHIP_BGR = (200, 120, 110)
FLAKE_BGR = {1: (192, 114, 104), 2: (184, 108, 98), 3: (176, 102, 92)}
THICK_BGR = (150, 150, 170)

HBN_V1_LABEL = {">40": "40+"}


class Context:
    """Set by the bench."""

    clock: Any = None
    lab: Any = None
    optics: Any = None  # emulators.MicroscopeOptics
    seed: int = 1
    picks_path: str | None = None  # jsonl: what each revisit image really showed
    images: dict[str, dict[str, Any]] = {}  # abs path -> sim meta of the saved frame
    lock = threading.Lock()


class SimFrame(np.ndarray):
    """A BGR frame that remembers what the sim microscope saw."""

    sim_meta: dict[str, Any] | None

    def __new__(cls, arr: np.ndarray, meta: dict[str, Any]) -> SimFrame:
        obj = np.asarray(arr).view(cls)
        obj.sim_meta = meta
        return obj

    def __array_finalize__(self, obj: Any) -> None:
        self.sim_meta = getattr(obj, "sim_meta", None)


# ----- rendering --------------------------------------------------------------

def _objective() -> int:
    return int(Context.optics.microscope._objective)


def _to_px(dx_mm: float, dy_mm: float, pixel_um: float) -> tuple[float, float]:
    return AXIS_COL + dx_mm * 1000.0 / pixel_um, AXIS_ROW + dy_mm * 1000.0 / pixel_um


def _chips_on_stage() -> list[tuple[str, tuple[float, ...]]]:
    world = Context.lab.world
    out = []
    with world.lock:
        for o in world.objects.values():
            if o.kind == "chip" and o.held_by is None and o.on == "transfer_stage":
                corners = []
                hw, hh = o.size_mm[0] / 2, o.size_mm[1] / 2
                for kx, ky in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
                    corners.extend(o.local_to_world(kx, ky))
                out.append((o.id, tuple(round(c, 5) for c in corners)))
    return out


_cache: dict[str, Any] = {"key": None, "img": None}


def render(meta: dict[str, Any]) -> np.ndarray:
    import cv2

    objective = int(meta["objective"])
    pixel_um = PIXEL_UM.get(objective, 3.65 / objective)
    chips = _chips_on_stage()
    flakes = tuple((f["sim_id"], round(f["dx_mm"], 5), round(f["dy_mm"], 5))
                   for f in meta.get("flakes", []))
    key = (objective, tuple(chips), flakes)
    if _cache["key"] == key:
        return _cache["img"]
    img = np.empty((IMG_H, IMG_W, 3), dtype=np.uint8)
    img[:] = BG_BGR
    cx, cy = Context.optics.microscope.center
    for _, c in chips:
        pts = [_to_px(c[i] - cx, c[i + 1] - cy, pixel_um) for i in range(0, 8, 2)]
        if all(u < 0 for u, _ in pts) or all(u >= IMG_W for u, _ in pts) \
                or all(v < 0 for _, v in pts) or all(v >= IMG_H for _, v in pts):
            continue
        poly = np.array([[max(-1e6, min(1e6, u)), max(-1e6, min(1e6, v))] for u, v in pts],
                        dtype=np.int32)
        cv2.fillPoly(img, [poly], CHIP_BGR)
    for f in meta.get("flakes", []):
        u, v = _to_px(f["dx_mm"], f["dy_mm"], pixel_um)
        r = max(1, int(round(math.sqrt(f["area_um2"] / math.pi) / pixel_um)))
        colour = FLAKE_BGR.get(f["layers"], THICK_BGR if f["layers"] >= 10 else (165, 110, 95))
        cv2.circle(img, (int(round(u)), int(round(v))), r, colour, -1)
    _cache["key"], _cache["img"] = key, img
    return img


# ----- camera -----------------------------------------------------------------

def make_camera_class(base: type) -> type:
    """SimCameraController as a subclass of v1's own CameraController."""

    class SimCameraController(base):  # type: ignore[misc, valid-type]
        def __init__(self, index: int = 0, fps: int = 15, window_name: str = "Live View") -> None:
            super().__init__(index=index, fps=fps, window_name=window_name)
            self._af_mode = 0
            self._af_since: float | None = None
            self._af_done = False
            self._afpos = AF_REF
            self._auto_exposure = 1
            self._exposure_time = 10000
            self._af_rng = random.Random(f"af:{Context.seed}")

        # --- stream: frames on demand, no grab thread ---
        def start(self) -> None:
            self._running = True
            self._width, self._height = IMG_W, IMG_H

        def stop(self) -> None:
            self._running = False
            self._latest = None

        def open_live_view(self) -> None:
            pass

        def close_live_view(self) -> None:
            pass

        def _get_latest_frame(self) -> np.ndarray | None:
            Context.clock.sleep(1.0 / self.fps)  # the next frame from the camera
            if not self._running:
                return None
            return self._frame()

        def get_latest_frame_with_timestamp(self) -> tuple[np.ndarray, float, float] | None:
            frame = self._get_latest_frame()
            if frame is None:
                return None
            now = Context.clock.now()
            return frame, now, now

        def _frame(self) -> np.ndarray | None:
            self._af_update()
            try:
                meta = Context.optics.microscope.capture().meta
            except Exception:  # noqa: BLE001 - camera offline: the stream delivers nothing
                return None
            meta = dict(meta, afpos=self._afpos, t=Context.clock.now())
            return SimFrame(render(meta).copy(), meta)

        # --- lens autofocus model ---
        def _apply_lens(self) -> None:
            k_mm = AF_UM_PER_UNIT.get(_objective(), 18.1367) / 1000.0
            Context.optics.lens_mm = k_mm * (self._afpos - AF_REF)
            try:
                Context.optics.apply()
            except Exception:  # noqa: BLE001 - microscope offline: no frames anyway
                pass

        def _af_update(self) -> None:
            self._apply_lens()
            if self._af_mode not in (1, 2) or self._af_since is None:
                return
            if self._af_mode == 2 and self._af_done:
                return
            if Context.clock.now() - self._af_since < AF_SETTLE_S:
                return
            try:
                meta = Context.optics.microscope.capture().meta
            except Exception:  # noqa: BLE001 - no image, no contrast to focus on
                return
            best = meta.get("best_focus")
            if best is None:
                return  # nothing in view to focus on: the lens stays where it is
            k_mm = AF_UM_PER_UNIT.get(_objective(), 18.1367) / 1000.0
            base = Context.optics.microscope.nominal_focus + Context.optics.motor_um / 1000.0
            pos = AF_REF + (best - base) / k_mm + self._af_rng.gauss(0, AF_NOISE_UNITS)
            self._afpos = int(round(max(AF_MIN, min(AF_MAX, pos))))
            self._af_done = True
            self._apply_lens()

        def _set_autofocus_mode(self, mode: int) -> None:
            if not self._running:
                raise RuntimeError("Camera not started. Call start() first.")
            self._af_mode = int(mode)
            self._af_since = Context.clock.now()
            self._af_done = False

        def get_af_zone_info(self) -> dict:
            return {"min": 0, "max": (5 << 8) | 5, "default": 12, "w": 5, "h": 5, "current": 12}

        def set_af_zone(self, zone: int | None = None, row: int | None = None,
                        col: int | None = None, where: str | None = None) -> dict:
            return {"w": 5, "h": 5, "set": zone if zone is not None else 12, "current": 12}

        def get_af_status_code(self) -> int:
            self._af_update()
            return 1 if self._af_mode == 0 or self._af_done else 2

        def get_focus_position(self) -> int:
            self._af_update()
            return int(self._afpos)

        def set_focus_position(self, pos: int, clamp: bool = True) -> dict:
            pos = int(pos)
            self._af_mode, self._af_since = 0, None
            if clamp:
                pos = max(AF_MIN, min(AF_MAX, pos))
            self._afpos = pos
            self._apply_lens()
            return {"position": pos}

        # --- exposure: no effect on the drawn frames ---
        def auto_exposure_on(self) -> None:
            self._auto_exposure = 1

        def auto_exposure_off(self) -> None:
            self._auto_exposure = 0

        def get_auto_exposure(self) -> int:
            return self._auto_exposure

        def set_exposure_time(self, value: int) -> None:
            self._auto_exposure, self._exposure_time = 0, int(value)

        def get_exposure_time(self) -> int:
            return self._exposure_time

        def set_auto_exposure_target(self, value: int) -> None:
            pass

        # --- focus score: the sim sharpness of the frame ---
        def blur_score_bgr(self, bgr: np.ndarray, shrink: int = 2) -> float:
            meta = getattr(bgr, "sim_meta", None)
            if meta is None:
                return super().blur_score_bgr(bgr, shrink=shrink)
            return BLUR_PER_SHARPNESS * float(meta.get("sharpness", 0.0))

        def blur_score_bgr_laplacian(self, bgr: np.ndarray, shrink: int = 2) -> float:
            return self.blur_score_bgr(bgr, shrink=shrink)

    return SimCameraController


# ----- saved images -------------------------------------------------------------

def make_imwrite(real_imwrite: Any, tmp_dir: str) -> Any:
    """cv2.imwrite that remembers which sim frame went into which file, and
    writes atomically (v1's folder watcher reads a file as soon as it appears)."""

    os.makedirs(tmp_dir, exist_ok=True)
    counter = [0]

    def imwrite(path: str, img: Any, *args: Any) -> bool:
        meta = getattr(img, "sim_meta", None)
        counter[0] += 1
        tmp = os.path.join(tmp_dir, f"w{counter[0]}{os.path.splitext(str(path))[1] or '.png'}")
        ok = real_imwrite(tmp, np.asarray(img), *args)
        if not ok:
            return False
        if meta is not None:
            with Context.lock:
                Context.images[os.path.abspath(path)] = meta
        os.replace(tmp, path)
        return True

    return imwrite


# ----- segmenter ------------------------------------------------------------------

class _SegResult:
    """What LiveSegment uses of a Segmenter after the model step."""

    def __init__(self, h: int, w: int, colors: dict[str, np.ndarray]) -> None:
        self.masks = np.zeros((h, w), dtype=np.int32)
        self._colors = colors
        self._labels: dict[int, str] = {}

    def add(self, mask_id: int, label: str, row: float, col: float, radius_px: float) -> None:
        import cv2

        cv2.circle(self.masks, (int(round(col)), int(round(row))), max(1, int(round(radius_px))),
                   int(mask_id), -1)
        self._labels[mask_id] = label

    def prettify(self) -> np.ndarray:
        out = np.zeros((*self.masks.shape, 3), dtype=np.float32)
        for mask_id, label in self._labels.items():
            out[self.masks == mask_id] = self._colors.get(label, np.zeros(3))
        return out


class _Analyzer(SimMicroscopeAnalyzer):
    """v2's analyzer with its own random stream per image (the folder watcher
    runs on its own thread; sharing the world's stream would make runs
    depend on thread timing)."""

    def __init__(self, world: Any, rng: random.Random) -> None:
        super().__init__(world)
        self._rng = rng

    @property
    def rng(self) -> random.Random:
        return self._rng


_current = threading.local()


def _material(material_type: str) -> str:
    return "hBN" if str(material_type).strip().lower() in {"hbn", "boron nitride"} else "Gr"


def _run_with_consensus(self: Any, img_rgb: np.ndarray, max_area: float | None = None) -> Any:
    effective_max_area = self.max_area if max_area is None else max_area
    h, w = img_rgb.shape[:2]
    seg = _SegResult(h, w, self._colors)
    path = getattr(_current, "path", None)
    with Context.lock:
        meta = Context.images.get(path) if path else None
    if meta is None:
        return seg, [], True  # not a testbench frame: nothing to find
    rng = random.Random(f"seg:{Context.seed}:{os.path.basename(path)}:{meta.get('t')}")
    analyzer = _Analyzer(Context.lab.world, rng)
    frame = Frame(device_id="microscope", timestamp=float(meta.get("t", 0.0)), meta=meta)
    objective = int(meta.get("objective", self.magnification))
    pixel_um = PIXEL_UM.get(objective, 3.65 / objective)
    candidates = []
    for i, fl in enumerate(analyzer.find_flakes(frame, _material(self.material_type)), start=1):
        col, row = _to_px(fl.dx_mm, fl.dy_mm, pixel_um)
        if not (0 <= row < h and 0 <= col < w):
            continue
        area = float(fl.area_um2) / (pixel_um * pixel_um)
        label = HBN_V1_LABEL.get(fl.thickness_label, fl.thickness_label)
        seg.add(i, label, row, col, math.sqrt(area / math.pi))
        # v1 _collect_candidates filters
        if not (self.min_area < area < effective_max_area):
            continue
        if label in {"dirt", "bg", "black", None, ""}:
            continue
        candidates.append({"mask_id": i, "label": label, "area": area,
                           "centroid": np.asarray([row, col], dtype=float),
                           "sim_id": fl.meta.get("sim_id")})
    candidates.sort(key=lambda c: (c["label"], -c["area"], c["centroid"][0], c["centroid"][1]))
    if self._is_revisit_filename(path) and Context.picks_path:
        # the target v1 will keep (its own centre-priority rule), for scoring
        pick = self._center_priority_candidate(candidates, frame_size=(h, w))
        row = {"path": path, "magnification": self.magnification,
               "pick": pick.get("sim_id") if pick else None,
               "pick_label": pick.get("label") if pick else None,
               "candidates": [[c["sim_id"], c["label"], round(c["area"])] for c in candidates]}
        with Context.lock, open(Context.picks_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return seg, candidates, True


def install_segmenter(segmenter_dir: str) -> list[type]:
    """Patch every loaded copy of v1's LiveSegment class (observer.py imports it
    as top-level `livesegment`, the refind flow as a package module)."""

    if segmenter_dir not in sys.path:
        sys.path.append(segmenter_dir)
    import importlib

    classes = []
    for name in ("livesegment", "agents.processing_agent.segmenter.livesegment"):
        mod = importlib.import_module(name)
        cls = mod.LiveSegment
        if getattr(cls, "_testbench_patched", False):
            continue
        real_call = cls.__call__

        def call(self: Any, filename: str, _real: Any = real_call) -> Any:
            _current.path = os.path.abspath(os.path.join(self.input_dir,
                                                         os.path.basename(filename)))
            return _real(self, filename)

        cls.__call__ = call
        cls._run_with_consensus = _run_with_consensus
        # LAB-colour recovery of a revisit flake reads real pixel colours
        cls._center_color_fallback = lambda self, img_rgb, accepted_labels=None: None
        cls._testbench_patched = True
        classes.append(cls)
    return classes
