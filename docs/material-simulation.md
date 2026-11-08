# Qualitative material simulation

Reactor evidence images reserve margins for titles and scale bars outside the
scientific field. `outline_px` remains the calibrated sensor coordinate used by
acquisition; `display_outline_px` and `sensor_rect_px` describe the annotated
image layout. Rendering cannot change geometry, visibility acceptance or motion
calibration. Original captures remain immutable.

Integrated into main from material commit `7cfbcf8`. The shared model implements persistent cleavage geometry,
explicit two-flake placement, and an overlap-bounded single-product model.
All examples are **synthetic**, with no experimental calibration. No chemical
reaction, kinetic law, thickness of the product, mass balance or yield is inferred.

## What changed

Previously, `SimFlake` stored centre, layers and area only. The renderer seeded
an outline from the character sum of its ID, in pixel coordinates. That was
repeatable but could collide between IDs, ignore chip rotation, add external
tails and inflate tiny objects to a pixel minimum. The reactor branch's older
substrate-wide grid growth is a separate model and is not used by this module.

New deposits store a polygon in physical mm, exact polygon area and source
provenance. The microscope transforms that polygon through chip pose, viewport
and magnification. Pickup transforms offsets into the stamp/world frame; release
transforms them into the destination chip frame. Repeated observations do not
sample new candidates. Legacy saved area-only flakes have a deterministic display
fallback, explicitly labelled `legacy_display`, without invented source provenance.
Original images and historical runs are never rewritten.

## Model, inputs and outputs

1. **Cleavage:** `material_flakes.cleave` generates candidates once per physical
   deposition. Inputs: seed, source chip/batch/material, requested count, chip
   size, contact polygons and `CleavageProfile`. Bounded log-normal area,
   configurable layer classes, a thick-flake area multiplier, angular convex
   shapes, and a mixture of contact clusters/uniform positions are illustrative
   controls. A candidate must fit wholly inside a contact region and chip;
   rejection may produce fewer candidates. Count is not measured yield.
2. **Selection/transfer:** `Placement` retains a complete immutable source
   `Flake`, target centre and rotation. `PairAssembly` requires exactly two
   distinct identities, bottom then top, wholly on a named carrier. Selection
   does not happen implicitly when the camera looks at a population.
3. **Local reaction:** `LocalReaction` accepts the assembly and a fixture profile.
   `advance(seconds, active=...)` accumulates explicitly active exposure.
   Incubation, completion exposure, conversion ceiling and seeded failure chance
   are configurable synthetic controls. Defaults (0 s incubation, 60 s exposure,
   ceiling 1) are demonstration values, not advice for any material. No temperature
   or gas recipe is supplied: that requires apparatus/material knowledge.
4. **Geometry:** convex intersection defines the only reactive region. One seeded
   interior anchor and a homothetic contraction give a connected product with
   area `overlap_area * conversion`. It remains inside both inputs at every
   exposure, never spreads across the chip, and has one stable ID. No overlap
   means no product. `partial`, `failed`, `unreacted`, `no_overlap`, `complete`
   are retained; failure freezes the current partial morphology.
5. **Observation:** `observe()` is a pure geometry/state read. `observe_pair()`
   adds viewport/focus and the reactor contract. `render_product` retains partial
   or failed morphology; `product` is offered for acceptance only when complete,
   in focus and fully in view. This is synthetic truth, not pixel-based detection
   or experimental certification. Completed images contain only the single
   product; source outlines survive as provenance, not extra final flakes.
6. **Persistence:** versioned JSON `snapshot()` / `restore()` retains preparation,
   inputs, profile, exposure and failure. Invalid/nonfinite geometry and state are
   refused. A different preparation needs a new model. Ordinary SimWorld pickle
   persistence already carries the new flake fields.

## Integration boundary

Implemented microscope hooks are small changes in `world.py`,
`devices/sim/devices.py` and `render.py`. No changes to `lab.py`, `optics.py`,
agent/UI files, reactor devices/flows/safety or mechanical loading are needed.
`material_adapter.py` translates existing recipe/count/layer controls.
`exfoliation.morphology` keys are `CleavageProfile` fields. New everyday worlds
enable the clustered/contact-contained population in `testbench.yaml`; the
checked-in `config/material-morphology.yaml` provides explicit profile controls.
Restored worlds retain their saved morphology; older checkpoints without a
profile keep the prior distribution rather than silently changing on restart.
The example script merges that overlay into the normal testbench, runs the actual
SafetyGate-backed `exfoliate_chips` flow with default mechanical noise, and renders
its deposited population. `example.json.runtime_fixture` records the recipe and
outcome; it is an offline fixture receipt, not an end-to-end reactor recording.
With no morphology key, existing scripted fixtures retain their scalar layer,
area and centre draw order and sequential IDs, with area bounded to 12..12000
um² and new persistent geometry. That compatibility path keeps the old uniform,
edge-clamped positions; it does not claim the improved spatial distribution.
The Poisson count remains qualitative and retains its own seed stream (the
recipe score was removed on 2026-11-03). Explicit morphology changes the population and therefore may change
later search paths; fixed-seed old demos are not guaranteed successful under a
different population. In every new run, overview/microscope measurement noise has its own persisted
RNG, independent of morphology choice. Repeated read-only analysis cannot consume
mechanical randomness. An explicit top-level `legacy_shared_observation_rng: true`
switch is available only for historical fixture comparisons. Loading a checkpoint
without the new observation stream records `_legacy_observation_rng=true` in memory
and preserves that old continuation's shared-stream semantics without rewriting it.
Fresh runs always default to separate streams. Existing recordings remain unchanged.
New clustered-population IDs include source chip, tape segment/use and seed.

The reactor line owns ledger validation, reservations, consumption and idempotent
product creation, as specified in its `docs/reactor-pair-contract.md`. The minimal
Leader integration is:

```python
# After a known selection/transfer: never guess a catalog ID from image order.
placement = catalog_placement(sim_flake, catalog_id)
# Store placement.model_dump(mode="json") in input.props.material_placement.
assembly = assembly_from_manifest(chip["props"]["reactor_pair"], input_samples,
                                  chip_size_mm=physical_chip.size_mm)
model = LocalReaction(assembly, seed=world.seed, profile=explicit_fixture_profile)
# ReactorPlant owns the supplied Clock and persistent model, keyed by preparation_id.
model.advance(dt, active=fixture_exposure_enabled)
view = observe_pair(model, center_mm=chip_local_view_center, fov_mm=fov,
                    in_focus=focus_is_verified)
png = render_pair(view)
```

`catalog_placement` requires recorded source geometry, maps the physical flake
to the caller's catalog ID, and checks rigid transfer/area agreement. Unknown
legacy provenance is refused. `assembly_from_manifest` consumes reactor_pair
schema 1, the two ordered IDs and each input's explicit placement. It validates
IDs, material labels and polygon areas, without writing samples. The reactor
must additionally check kind, carrier location, extra inputs and consumed state.

Returned fields include `experiment_kind=two_flake_reaction`, `preparation_id`,
`input_flake_ids`, `visible`, `reaction_fraction`, and `product=None` or one product
with `id`, `source_flake_ids`, `material`, positive `area_um2`, `geometry`, `state`.
Areas are absolute square micrometres; conversion uses overlap as denominator.
The viewport reports visible area separately from full area. Reuse of the old
whole-chip `coverage` goal or grid renderer would violate this contract.

The integrated `PairMaterialBackend` binds this API to reactor Clock/exposure,
observations and ledger acceptance. Normal and interrupted-exposure scripted
recordings exercise that path. Prepared pairs remain synthetic fixtures; this
is not a chemistry-specific activation law or autonomous preparation claim.

## Regression decision for Leader review

The first full run with clustered morphology as the unconditional default had
one new non-UI failure: `test_make_a_graphene_device[3]`. Selection and design
passed; the final `place_chip_e01_on_transfer_stage` step parked after four
corrections at 0.52 mm / -2.0 deg, against the unchanged 0.5 mm tolerance. The
geometry model did not use `world.rng`; changed search observations consumed that
same stream through `SimLocator` and `SimMicroscopeAnalyzer`, shifting later
placement errors. This was an honest mechanical verification failure, not an
invalid overlap or a missing flake. No safety threshold or success assertion was
relaxed. The parked state was not turned into a success.

This branch isolates observation noise for **all new default runs**, including
the compatible scalar population, and persists its stream. A guarded-flow test proves actual new deposits and shows
20 additional analyses leave mechanical RNG state unchanged. Default legacy
recipe/yield/centre sampling is retained for existing scripted fixtures; stable
physical polygons, bounded area, source identity, rotation and transfer fixes
remain enabled by default. The initial compatibility adjustment passed device-demo seeds 2 and 3 but still
aliased the default observation stream; that was insufficient and is superseded.
The final invariant test makes 20 extra microscope analyses, then releases the
same chip in two default worlds and proves identical resulting physical poses.
A separate old-checkpoint test proves old files remain byte-identical and retain
their prior noise semantics; the historical switch is never enabled by default. The new distribution is exercised by the checked-in overlay,
actual guarded-flow test and reproducible example, not only the standalone API.
Leader should review whether to promote that overlay for the next default demo;
this branch does not claim to preserve old fixed-seed success after such a switch.

## Sources and reuse audit (reviewed 27 October 2026)

| Source | License checked | Useful idea / decision | Code or data reused |
| --- | --- | --- | --- |
| [MaskTerial Synthetic Data](https://github.com/Jaluus/MaskTerial-Synthetic-Data), [generator.py](https://github.com/Jaluus/MaskTerial-Synthetic-Data/blob/main/generator.py), [contour extraction](https://github.com/Jaluus/MaskTerial-Synthetic-Data/blob/main/extract_flake_contours.py), [license](https://github.com/Jaluus/MaskTerial-Synthetic-Data/blob/main/LICENSE) | MIT, Jan-Lucas Uslu | Separate normalized contours, placement, thickness and appearance; its measured contour/optical-data pipeline is a future calibration option. This task generates new angular polygons and retains them in physical coordinates. | None. No models, contour libraries, images, optical constants or source copied. |
| [Shapely](https://github.com/shapely/shapely), [intersection documentation](https://shapely.readthedocs.io/en/2.1.2/reference/shapely.intersection.html), [license](https://github.com/shapely/shapely/blob/main/LICENSE.txt) | BSD-3-Clause; underlying GEOS LGPL-2.1 | Explicit polygon intersection and validity are useful. Its general geometry engine is unnecessary for the restricted convex MVP. Independently implemented half-plane clipping; reject unsupported outlines. | None. No new dependency or vendored code. |
| [2DFlakeSemSeg](https://github.com/chenller/2DFlakeSemSeg), [license](https://github.com/chenller/2DFlakeSemSeg/blob/main/LICENSE) | Repository Apache-2.0; separate dataset rights not assessed | Segmentation masks could later supply empirical contours. Segmentation accuracy does not establish reaction kinetics; training pipeline is out of scope. | None. No dataset or weights downloaded. |

The [MaskTerial paper](https://arxiv.org/abs/2412.09333) motivates separating
synthetic image generation from detection/classification. It supplies no
calibration for this two-flake reaction. Existing Qumus recipe controls and
Pillow/standard-library rendering are the actual code/dependencies reused.
No private project source or lab data was sent to these projects.

## Limits and acceptance

Convex hole-free outlines cannot represent cracks, holes, concave shapes or
multiple reaction islands. One connected product and overlap-only support are
explicit finite assumptions, not a conclusion about an unknown mechanism.
Outside-overlap material accounting, chemistry, product thickness, adhesion,
strain and atomistic processes are unmodelled. The completion view is a single
product visualization, not a mass-balance claim. Layer/size/distribution controls
are uncalibrated. Source candidates can overlap. Each pressed cell is treated as
a separate conservative contact region, which may reject candidates that would
span adjacent pressed cells. Background residue/camera appearance still uses the
existing illustrative renderer; only identified flake geometry is invariant.
The standalone geometry API uses chip-local viewport axes. The integrated
reactor binding applies camera/carrier rotation before clipping and rendering;
the transfer microscope also handles chip rotation. Out-of-focus reactor
observations withhold unavailable geometry and conversion detail. Arbitrary
real-photo substitution cannot reproduce current scene geometry. Live lab
construction now refuses nonempty `replay_dir` or
`QUMUS2_MICROSCOPE_REPLAY_DIR` settings; offline renderer fixtures retain their
separate photo-replay input.

Offline checks cover exact cardinality/identity, analytic square/rotated overlaps,
invalid geometry, 100 seeded intersection cases, area/containment/nesting,
seed replay, restart, camera-frequency independence, real microscope 10/20/100x
observations, partial FOVs, pickup/drop rotation, catalog mapping, and persistent
failure/incomplete states. Run from the canonical `v2` repository:

```sh
.venv/bin/python -m pytest tests/test_material_simulation.py tests/test_material_integration.py
.venv/bin/python -m ruff check .
.venv/bin/python scripts/material_simulation_example.py --out docs/assets/material-simulation --seed 27
```

Examples: [microscope comparison](assets/material-simulation/microscope-comparison.png),
[cleavage population](assets/material-simulation/cleavage-population.png),
[reaction states](assets/material-simulation/reaction-states.png), and
[seeded geometry/state](assets/material-simulation/example.json). The comparison
uses the retained legacy renderer with the same IDs/areas, not an edited historical
image. The JSON and PNGs can be reproduced without any API call.

The accepted `openai-integrated-reactor-20261027-v5` recording demonstrates
ordinary-stock preparation, optical input selection, physical two-flake stacking,
one local product, cold storage and ordinary-microscope reinspection. It retains
its visible budget stop and continuation. The current camera-loss fixture
demonstrates a failed observation with no accepted product. Local regressions
also cover incomplete conversion, contact loss, no overlap and lost focus.
These recordings preserve source identities, preparation provenance and the
synthetic boundary; see the [catalog](../recordings/README.md).
Do not regenerate historical evidence or enable paid/remote CI for these checks.

## Historical workstream validation

The following records the material worktree handoff on 27 October 2026, before
Leader integration repairs. Current integrated results and closure of the UI
failures below are in the [integration audit](archive/integration-audit-2026-10-27.md).

- Ruff and `git diff --check`: passed.
- Final non-UI regression with default observation/mechanics isolation:
  **384 passed, 3 skipped** (`pytest --ignore=tests/test_ui_browser.py`).
- This includes **35 material-specific cases** and existing persistence/restart,
  mechanical, flow and fixed-seed scripted-demo checks. No existing test assertion,
  mechanical tolerance or default fault probability was changed.
- Guarded clustered cleavage with the checked-in overlay, seed 27 and normal
  testbench mechanical noise: succeeded, 31 candidates. All three PNGs and the
  structured JSON reproduced byte-for-byte on a second run.
- An earlier whole-suite run was **403 passed, 3 skipped, 6 failed**. Its one
  material-triggered scenario failure is analyzed above; both seeds 2 and 3 pass
  in the final regression with the independent default observation stream. Five UI failures were left for the Leader:
  `test_lab_inventory_and_film_report`, `test_job_record_picker_microscope_and_links`,
  `test_demo_contains_its_own_experience_and_single_tool_markers`,
  `test_openai_history_review_navigation_and_reports`, and
  `test_results_singleton_categories_keep_three_columns`. This branch changes
  no UI source and does not claim a completely green whole-suite result or an
  independent clean-baseline reproduction of those UI failures.

That workstream handoff used no paid model calls, GitHub CI/Actions, remote
validation, push, pull request, hardware operation or historical evidence edits.
