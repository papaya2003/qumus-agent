"""Create new synthetic material examples; no credentials, services or lab data.

Run from the repository root with its modern Python environment:
  .venv/bin/python scripts/material_simulation_example.py --out docs/assets/material-simulation
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from qumus2.app import build_services  # noqa: E402
from qumus2.config import load_yaml  # noqa: E402
from qumus2.core.polygons import transform  # noqa: E402
from qumus2.testbench.material_adapter import microscope_flake  # noqa: E402
from qumus2.testbench.material_flakes import (  # noqa: E402
    Flake,
    PairAssembly,
    Placement,
    flake_outline,
)
from qumus2.testbench.material_reaction import LocalReaction, ReactionProfile  # noqa: E402
from qumus2.testbench.material_view import observe_pair, render_pair  # noqa: E402
from qumus2.testbench.render import MicroscopeRenderer  # noqa: E402
from qumus2.testbench.world import SimFlake  # noqa: E402


def sheet(panels: list[tuple[str, Image.Image]], path: Path, title: str) -> None:
    out = Image.new("RGB", (1280, 60 + 3 * 452), "#111b2d")
    draw = ImageDraw.Draw(out)
    font = ImageFont.load_default(size=19)
    draw.text((18, 18), title, fill="white", font=font)
    for i, (label, im) in enumerate(panels):
        x, y = i % 2 * 640, 60 + i // 2 * 452
        draw.text((x + 12, y + 5), label, fill="#d4e0ee", font=font)
        out.paste(im, (x, y + 32))
    out.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=27)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    # Reuse the production renderer for the same ID/area under both paths.
    # Removing outline_mm invokes its retained legacy area-based fallback.
    temporary = tempfile.TemporaryDirectory(prefix="qumus-material-example-")
    renderer = MicroscopeRenderer(Path(temporary.name), size=(640, 420))
    fl = SimFlake("synthetic_example_A", "Gr", 3, 0, 0, 1600,
                   flake_outline(f"example:{args.seed}", 1600))
    panels = []
    for angle, mag, fov in ((0, 20, (0.13, 0.086)), (65, 20, (0.13, 0.086)), (65, 100, (0.026, 0.0172))):
        geometry = microscope_flake(fl, (0, 0), fov, rotation_deg=angle)
        for label in ("Legacy area-only", "Stored polygon"):
            f = dict(geometry)
            if label.startswith("Legacy"):
                del f["outline_mm"]
            meta = {"on_chip": "synthetic_example", "flakes": [f], "objective": mag,
                    "fov_mm": fov, "sharpness_chip": 10.5}
            path, _ = renderer.render("microscope", meta)
            panels.append((f"{label} | chip rotation {angle} deg | {mag}x", Image.open(path).copy()))
    sheet(panels, args.out / "microscope-comparison.png",
          "SYNTHETIC | Same identity and area; stored geometry follows chip rotation")
    config = load_yaml("testbench.yaml")
    config["exfoliation"].update(load_yaml("material-morphology.yaml")["exfoliation"])
    services = build_services(":memory:", seed=args.seed, testbench_config=config)
    recipe = {"material": "Gr", "chip_ids": ["chip_001"], "target_temp_c": 70,
              "num_massages": 3, "peel_speed": 150, "peel_angle_deg": 25, "press_depth_mm": 0.3}
    job = services.jobs.wait(services.jobs.submit("exfoliate_chips", recipe), 120)
    if job.status != "succeeded":
        raise RuntimeError(f"Seeded guarded cleavage did not succeed: {job.status}")
    physical = services.lab.world.objects["chip_001"].flakes
    if not physical:
        raise RuntimeError("Guarded cleavage deposited no candidates for this seed")
    source = [Flake(id=f.id, material=f.material, layers=f.layers,
                    source_chip_id=f.origin["source_chip_id"], batch_id=f.origin["batch_id"],
                    outline_mm=f.origin["source_outline_mm"]) for f in physical]
    a = Flake(id="catalog_A", material="Unspecified A", layers=3, source_chip_id="source_A",
              batch_id="cleave_A", outline_mm=transform(fl.outline_mm, xy=(1, 2)))
    b = Flake(id="catalog_B", material="Unspecified B", layers=5, source_chip_id="source_B",
              batch_id="cleave_B", outline_mm=transform(flake_outline("B", 1300), xy=(-1, 1)))
    assembly = PairAssembly(id="synthetic_preparation_27", target_chip_id="target_chip", inputs=(
        Placement(flake=a, target_center_mm=(0, 0)),
        Placement(flake=b, target_center_mm=(0.009, 0), rotation_deg=23)))
    reaction = LocalReaction(assembly, seed=args.seed)
    reactions = [("Prepared: exactly two inputs", observe_pair(reaction, fov_mm=(0.16, 0.105)))]
    reaction.advance(18, active=True)
    reactions.append(("Partial: one connected product region", observe_pair(reaction, fov_mm=(0.16, 0.105))))
    failed = LocalReaction.restore(reaction.snapshot())
    failed.fail("synthetic_contact_lost")
    reaction.advance(42, active=True)
    reactions.append(("Complete: product restricted to overlap", observe_pair(reaction, fov_mm=(0.16, 0.105))))
    reactions.append(("Interrupted: partial morphology retained", observe_pair(failed, fov_mm=(0.16, 0.105))))
    limited = LocalReaction(assembly, seed=args.seed, profile=ReactionProfile(max_conversion=0.35))
    limited.advance(120, active=True)
    reactions.append(("Incomplete: explicit conversion ceiling", observe_pair(limited, fov_mm=(0.16, 0.105))))
    separated = PairAssembly(id="separated", target_chip_id="target_chip", inputs=(assembly.inputs[0],
        Placement(flake=b, target_center_mm=(0.045, 0), rotation_deg=23)))
    no_overlap = LocalReaction(separated, seed=args.seed)
    no_overlap.advance(120, active=True)
    reactions.append(("No overlap: no product", observe_pair(no_overlap, fov_mm=(0.16, 0.105))))
    sheet([(label, Image.open(io.BytesIO(render_pair(view)))) for label, view in reactions],
          args.out / "reaction-states.png", "SYNTHETIC | Blue / purple inputs; gold local product")
    # Wider cleavage population with a fixed-geometry close-up; new synthetic data only.
    converted = [SimFlake(f.id, "Gr", f.layers, *f.center_mm, f.area_um2,
                         transform(f.outline_mm, xy=(-f.center_mm[0], -f.center_mm[1]))) for f in source]
    frames = []
    for fov, center in (((0.8, 0.525), source[0].center_mm), ((0.08, 0.0525), source[0].center_mm)):
        flakes = [microscope_flake(replace(f, x_mm=f.x_mm-center[0], y_mm=f.y_mm-center[1]),
                                   (f.x_mm-center[0], f.y_mm-center[1]), fov) for f in converted]
        path, _ = renderer.render("cleavage", {"on_chip": "source", "flakes": [f for f in flakes if f],
             "fov_mm": fov, "objective": 10 if fov[0] > 0.5 else 100, "sharpness_chip": 10.5})
        frames.append(Image.open(path).copy())
    wide = Image.new("RGB", (1280, 464), "#111b2d")
    ImageDraw.Draw(wide).text((16, 12), "SYNTHETIC guarded cleavage | material-morphology.yaml | same deposited cluster at two fields",
                              fill="white", font=ImageFont.load_default(size=18))
    for i, im in enumerate(frames):
        wide.paste(im, (i * 640, 44))
    wide.save(args.out / "cleavage-population.png")
    payload = {"source": "qualitative_synthetic_model", "seed": args.seed,
               "runtime_fixture": {"config_overlay": "material-morphology.yaml",
                                   "flow": "exfoliate_chips", "status": job.status, "recipe": recipe,
                                   "mechanical_noise": "default testbench"},
               "cleavage": [f.model_dump(mode="json") for f in source],
               "assembly": assembly.model_dump(mode="json"), "reaction_snapshot": reaction.snapshot(),
               "observations": {label: view for label, view in reactions}}
    (args.out / "example.json").write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n")
    temporary.cleanup()
    print(f"Wrote synthetic examples to {args.out.resolve()}")


if __name__ == "__main__":
    main()
