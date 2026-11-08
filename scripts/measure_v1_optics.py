"""Measure the v1 search camera from v1's own photographs (#182).

Reproduces the numbers behind V1_SEARCH_CAMERA and V1_NATIVE_SUBSTRATE_RGB in
qumus2/core/optical_profiles.py from the read-only v1 copy:

  python scripts/measure_v1_optics.py /mnt/project-files/qumus/v1-original/Qumus-lihan-graphene

Needs the "vision" extra (opencv). Prints numbers only; writes nothing.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

HBN = "agents/processing_agent/segmenter/test_images/hBN"
CANON_UM_PER_PX_20X = 5.67/20  # Canon EOS 6D Mark II pixel / objective (assumed 1x relay)


def rgb(path: Path) -> np.ndarray:
    return cv2.imread(str(path))[:, :, ::-1].astype(np.float32)


def separable_vignette(image: np.ndarray) -> tuple[float, float]:
    """Background-only fit of brightness = a(1 - kx u^2 - ky v^2) + tilt."""
    h, w, _ = image.shape
    k = max(1, w//624)
    small = cv2.resize(image, (w//k, h//k), interpolation=cv2.INTER_AREA)
    sh, sw, _ = small.shape
    median = np.median(small.reshape(-1, 3), axis=0)
    chroma = small/np.maximum(small.sum(-1, keepdims=True), 1)
    background = np.linalg.norm(chroma-median/median.sum(), axis=-1) < .02
    y, x = np.mgrid[0:sh, 0:sw]
    u, v = (x-(sw-1)/2)/((sw-1)/2), (y-(sh-1)/2)/((sh-1)/2)
    design = np.c_[np.ones(background.sum()), u[background], v[background],
                   u[background]**2, v[background]**2]
    c, *_ = np.linalg.lstsq(design, small.sum(-1)[background], rcond=None)
    return -c[3]/c[0], -c[4]/c[0]


def radial_vignette(image: np.ndarray) -> float:
    """Radial falloff (normalised by the half-width) of the off-chip region."""
    h, w, _ = image.shape
    small = cv2.resize(image, (w//8, h//8), interpolation=cv2.INTER_AREA)
    sh, sw, _ = small.shape
    median = np.median(small[:, :sw//4].reshape(-1, 3), axis=0)
    background = np.linalg.norm(small-median, axis=-1) < 12
    y, x = np.mgrid[0:sh, 0:sw]
    u, v = (x-(sw-1)/2)/((sw-1)/2), (y-(sh-1)/2)/((sw-1)/2)
    design = np.c_[np.ones(background.sum()), u[background]**2+v[background]**2,
                   u[background], v[background]]
    c, *_ = np.linalg.lstsq(design, small.mean(-1)[background], rcond=None)
    return -c[1]/c[0]


def centre_background(image: np.ndarray) -> np.ndarray:
    h, w, _ = image.shape
    centre = image[int(h*.4):int(h*.6), int(w*.4):int(w*.6)]
    median = np.median(centre.reshape(-1, 3), axis=0)
    return np.median(centre[np.linalg.norm(centre-median, axis=-1) < 15], axis=0)


def texture_sd(image: np.ndarray, block: int) -> float:
    """Background brightness sd after removing the smooth field, at one scale."""
    h, w, _ = image.shape
    centre = image[h//2-1000:h//2+1000, w//2-1000:w//2+1000]
    median = np.median(centre.reshape(-1, 3), axis=0)
    mask = cv2.erode((np.linalg.norm(centre-median, axis=-1) < 18).astype(np.uint8), np.ones((15, 15))) > 0
    gray = centre.mean(-1)
    small = cv2.resize(gray, (gray.shape[1]//block, gray.shape[0]//block), interpolation=cv2.INTER_AREA)
    keep = cv2.resize(mask.astype(np.uint8), small.shape[::-1], interpolation=cv2.INTER_NEAREST) > 0
    return float((small-cv2.GaussianBlur(small, (0, 0), 8))[keep].std())


def debris_per_mm2(image: np.ndarray) -> dict[str, float]:
    h, w, _ = image.shape
    centre = image[h//4:3*h//4, w//4:3*w//4]
    residual = np.linalg.norm(centre-cv2.GaussianBlur(centre, (0, 0), 25), axis=-1)
    count, _, stats, _ = cv2.connectedComponentsWithStats((residual > 12).astype(np.uint8), connectivity=8)
    diameter = 2*np.sqrt(stats[1:, 4]*CANON_UM_PER_PX_20X**2/np.pi)
    area_mm2 = centre.shape[0]*centre.shape[1]*CANON_UM_PER_PX_20X**2/1e6
    return {f"{a}-{b}um": float(((diameter >= a) & (diameter < b)).sum()/area_mm2)
            for a, b in ((.5, 1), (1, 2), (2, 3))}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("v1_root", type=Path)
    root = ap.parse_args(argv).v1_root
    frames = sorted((root/HBN).glob("*_M20_*.JPG"))
    rows = []
    for path in frames:
        image = rgb(path)
        kx, ky = separable_vignette(image)
        rows.append((path.name[:28], kx, ky, centre_background(image), texture_sd(image, 4),
                     texture_sd(image, 10), debris_per_mm2(image)))
    print("Canon 20x frames (6240x4160):")
    for name, kx, ky, bg, t4, t10, debris in rows:
        print(f"  {name}  kx {kx:.3f} ky {ky:.3f}  centre RGB {np.rint(bg).astype(int)}"
              f"  texture sd {t4:.2f} (1.1um) {t10:.2f} (2.8um)  debris/mm2 "
              + " ".join(f"{k}:{v:.0f}" for k, v in debris.items()))
    print("  median centre RGB", np.rint(np.median([r[3] for r in rows], axis=0)).astype(int))
    kx, ky = np.median([r[1] for r in rows]), np.median([r[2] for r in rows])
    print(f"  median kx {kx:.3f} ky {ky:.3f}")
    for key in rows[0][6]:
        print(f"  median debris {key}: {np.median([r[6][key] for r in rows]):.0f}/mm2")
    print("v1 search camera at 5x (corner/CORNER_*.jpg, off-chip background):")
    for path in sorted((root/"corner").glob("CORNER_*.jpg")):
        image = rgb(path)
        print(f"  {path.name} {image.shape[1]}x{image.shape[0]} radial k {radial_vignette(image):.3f}")


if __name__ == "__main__":
    main()
