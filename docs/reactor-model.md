# vdW reactor simulation

The everyday launcher opens the integrated three-arm simulation, including this
reactor, in its own saved workspace. The previous two-arm workspace and old
prepared-pair fixtures remain separately accessible. See [startup](local-start.md)
and [current workflow contract](reactor-owner-review-20261027.md).

## Apparatus and motion

S1/Arm 1/S2/Arm 2/S3/Arm 3 use five equal 190 mm horizontal gaps. Each workcell
and its existing arm/auxiliary mechanisms retain their relative positions.
The chamber and 190 mm rod form one rigid XY assembly. Bottom and top motors
move that whole assembly relative to fixed microscope XY; a separate insert
motor advances the rod by 160 mm for loading and unloading. The carrier does
not rotate. Microscope Z adjusts focus. The narrowed chamber clears the arm
and dedicated storage throughout the configured observation range.

These dimensions, thermal/flow responses, interlocks and images are simulation
fixtures. They are not calibration or certification of physical apparatus.
[Hardware interfaces](reactor-hardware.md) document the remaining real-device
contracts; normal startup does not connect to hardware.

## Material process

The owner specifies a reactive 2D material and a metal deposited by a person on
silicon. The current example is Gr/Pd, not a default chemistry. Ordinary source
preparation and search create catalogued hBN and reactive-material flakes.
Picker screens candidates; Hands checks them locally; Picker confirms the exact
pair; Hands picks hBN first, then the reactive material, and places them onto the
prepared metal. The final order is hBN / reactive material / metal / silicon.
hBN remains topmost and nonreactive. Known PC or mica support is a stamp property,
not another final layer. Source and coating provenance, geometry and images persist.

`prepare_material_metal` and `reactor_material_metal` execute guarded device
simulation with the ordinary `qualitative-material-metal-v1` protocol. Reactor
loads, observes, exposes, shuts down, cools and stores the prepared carrier. The
model advances with the supplied Clock and active exposure, never from taking
another image. Incompatible bench/checkpoint layouts are refused before modifying
saved state. Release and interface-progress assumptions are configurable,
explicitly uncalibrated, and saved with the result.

Simulation acceptance requires original requirements, successful device jobs,
observed qualitative progress, preserved top hBN and checked terminal conditions.
Failed release, camera loss, interrupted exposure and unmet progress do not become
successful results. `simulation_accepted` is separate from `experiment_accepted`;
real chemistry and physical motion remain unverified. No named chemical product,
yield, stoichiometry, product thickness or electrical performance is inferred.

## Historical two-flake model

The `two_flake_reaction` compatibility composition and `PairMaterialBackend`
remain for old recordings and explicit tests. That model consumes both reactive
flakes into at most one small overlap product; it cannot represent protective
nonreactive hBN. See the [legacy pair contract](reactor-pair-contract.md).
The opt-in abstract material-metal receipt fixture is also retained as a contract
diagnostic, not proof of completed device preparation or reaction.

## Historical field model

The earlier CVD-like population/coverage model is retained only as explicit
`legacy_field_growth` compatibility. It is not the current vdW experiment.
Its assumptions and source references are preserved in the
[dated model record](archive/reactor-model-2026-10-26.md). Older recordings live
under `recordings/archive/` and remain immutable.
