from __future__ import annotations

MICRON_PER_PIXEL_5X = 0.730
MM_PER_PIXEL_5X = MICRON_PER_PIXEL_5X / 1000.0
MM_PER_PIXEL_20X = MM_PER_PIXEL_5X / 4.0
MM_PER_PIXEL_100X = MM_PER_PIXEL_5X / 20.0


def convert_pt_unit(pt, view: str = "5x"):
    x, y = pt
    if view == "5x":
        return (x * MM_PER_PIXEL_5X, y * MM_PER_PIXEL_5X)
    if view == "20x":
        return (x * MM_PER_PIXEL_20X, y * MM_PER_PIXEL_20X)
    return (x * MM_PER_PIXEL_100X, y * MM_PER_PIXEL_100X)


def convert_pt_unit_back(pt, view: str = "5x"):
    x, y = pt
    if view == "5x":
        return (int(x / MM_PER_PIXEL_5X), int(y / MM_PER_PIXEL_5X))
    if view == "20x":
        return (int(x / MM_PER_PIXEL_20X), int(y / MM_PER_PIXEL_20X))
    return (int(x / MM_PER_PIXEL_100X), int(y / MM_PER_PIXEL_100X))


def convert_dist_unit(dist, view: str = "5x"):
    if view == "5x":
        return dist * MM_PER_PIXEL_5X
    if view == "20x":
        return dist * MM_PER_PIXEL_20X
    return dist * MM_PER_PIXEL_100X


def convert_dist_unit_back(dist, view: str = "5x"):
    if view == "5x":
        return int(dist / MM_PER_PIXEL_5X)
    if view == "20x":
        return int(dist / MM_PER_PIXEL_20X)
    return int(dist / MM_PER_PIXEL_100X)
