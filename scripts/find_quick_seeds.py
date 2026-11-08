"""Find quick-test seeds: worlds whose first hBN chip and first Gr chip already hold a pair (free).

The full hallbar_device run usually needs five or more chips before the
placement solver finds an hBN/Gr pair that fits all eight contacts (about
30-60 min of 10x search per chip). Some seeds are luckier: the first hBN chip
and the first Gr chip already carry a fitting pair, so the same unchanged
scenario reaches design and stacking after two chips (about an hour).

This script only exfoliates (no search, seconds per seed) and asks the same
placement solver about the simulator's true flakes. It changes nothing in the
simulated lab: a quick seed is an ordinary seed of the same world model.

    python scripts/find_quick_seeds.py --bundle <target dir> --seeds 1-40

Candidates must also be clear of every other flake by the picker's pickup
clearance (seed 28 was first predicted from an hBN flake sitting in a cluster;
the picker rightly refused it). Prints, per seed, the best pair's headroom (um). Prefer seeds with a margin of
a few um: recognition sees outlines a little smaller or larger than the truth,
and flakes it cannot see well are missed. A quick-seed run tests search ->
pairing -> design -> stacking end to end; it does not test the "one more chip"
loop or a realistic flake yield, so the final acceptance stays a normal seed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# hBN thickness the scenario's device plan asks for (10-40 nm); 0.333 nm per layer
HBN_LAYERS = (30, 120)
#: smallest area recognition keeps for a candidate (search min_area, um2)
MIN_AREA_UM2 = 50.0


def _seeds(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def _source(flake) -> dict:
    return {"flake_id": flake.id,
            "outline_mm": [[flake.x_mm + x, flake.y_mm + y] for x, y in flake.outline_mm]}


def _shape(flake):
    from shapely.geometry import Polygon

    poly = Polygon([(flake.x_mm + x, flake.y_mm + y) for x, y in flake.outline_mm])
    return poly if poly.is_valid else poly.buffer(0)


def _clear(chip, flake, gap_mm: float) -> bool:
    """The picker only takes a flake whose 20x view shows no other flake within
    the pickup clearance (edge to edge). Ground truth stands in for that view:
    any other flake on the chip, of any material, counts as a neighbour."""
    me = _shape(flake)
    return all(other is flake or me.distance(_shape(other)) > gap_mm for other in chip.flakes)


def _edge_ok(chip, flake, edge_mm: float) -> bool:
    hw, hh = chip.size_mm[0] / 2, chip.size_mm[1] / 2
    return all(abs(flake.x_mm + x) <= hw - edge_mm and abs(flake.y_mm + y) <= hh - edge_mm
               for x, y in flake.outline_mm)


def check_seed(seed: int, bundle: Path, work_root: Path) -> dict:
    from qumus2.app import build_services
    from qumus2.config import load_yaml, simulation_profile
    from qumus2.records import design_placement as solver
    from qumus2.testbench.hallbar import load_imported_hallbar_target, with_hallbar_target

    work = Path(tempfile.mkdtemp(prefix=f"seed{seed}-", dir=work_root))
    target = load_imported_hallbar_target(bundle)
    load_imported_hallbar_target(bundle, workspace=work)
    cfg = with_hallbar_target(simulation_profile("hallbar-linked"), target=target)
    s = build_services(str(work / "qumus.db"), scenario="hallbar_device", seed=seed,
                       testbench_config=cfg)
    verification = load_yaml("verification.yaml")
    pickup = verification.get("pickup", {})
    edge_mm = float(pickup.get("source_chip_edge_clearance_mm", 1.0))
    gap_mm = float(pickup.get("local_clearance_mm", 0.020))

    async def exfoliate(material: str) -> str:
        got = await s.gateway.execute("hands", "exfoliate_chips", {"material": material, "num_chips": 1})
        job = got.data.get("job_id")
        status = s.jobs.wait(job, 3600)
        return getattr(status, "status", str(status))

    # the scenario's order: one hBN chip, then one Gr chip
    states = [asyncio.run(exfoliate("hBN")), asyncio.run(exfoliate("Gr"))]
    chips = [o for o in s.lab.world.objects.values() if o.kind == "chip" and o.flakes]
    hbn = [(c, f) for c in chips for f in c.flakes if f.material == "hBN" and f.layers
           and HBN_LAYERS[0] <= f.layers <= HBN_LAYERS[1] and f.area_um2 >= MIN_AREA_UM2
           and _edge_ok(c, f, edge_mm) and _clear(c, f, gap_mm)]
    gr = [(c, f) for c in chips for f in c.flakes if f.material == "Gr" and f.layers == 1
          and f.area_um2 >= MIN_AREA_UM2 and _edge_ok(c, f, edge_mm) and _clear(c, f, gap_mm)]
    best = None
    for _, g in gr:
        gs = solver.solve_gr_placement(target, _source(g), verification=verification)
        if gs["status"] != "fit":
            continue
        for _, h in hbn:
            r = solver.solve_pair_placement(target, _source(h), _source(g), verification=verification,
                                            gr_solution=gs)
            if r["status"] == "fit":
                head = min(r["gr_headroom_um"], r["hbn_headroom_um"])
                if best is None or head > best["headroom_um"]:
                    best = {"hbn": h.id, "gr": g.id, "headroom_um": round(head, 2),
                            "gr_headroom_um": round(r["gr_headroom_um"], 2),
                            "hbn_headroom_um": round(r["hbn_headroom_um"], 2)}
    s.store.close()
    return {"seed": seed, "jobs": states, "hbn_candidates": len(hbn), "gr_monolayers": len(gr),
            "best_pair": best}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--seeds", default="1-30", help="e.g. 1-40 or 3,8,12-20")
    args = ap.parse_args(argv)
    os.environ.setdefault("QUMUS_KLAYOUT_BACKEND", "python")
    root = Path(tempfile.mkdtemp(prefix="quick-seeds-"))
    for seed in _seeds(args.seeds):
        print(json.dumps(check_seed(seed, Path(args.bundle).resolve(), root)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
