> Historical development evidence, superseded by the integrated optional reactor and current workstreams. Old startup/release instructions are not current.

# Experimental vdW reactor model

Status: isolated development in `codex/vdw-reactor-simulation`. The canonical
main checkout, normal launcher and group-meeting report do not include this work.
The owner requested partial progress while laboratory colleagues clarify the
apparatus. No merge or reactor deployment is authorized at this stage.

## Confirmed mechanical structure

Manually cut chips enter a dedicated new Storage Stage. A robotic arm puts a
chip into the 3D-printed carrier called the slot. The owner clarified the three
motors on 2026-10-26: `insert` advances/retracts the rod inside the chamber;
`bottom` moves the whole chamber and rod mechanism left/right; `top` moves that
whole assembly fore/aft. These two fine axes place the chip relative to the
fixed microscope. The carrier and chip do not rotate. The rectangular chamber
has a circular passage and an observation window at its centre. A microscope
stays fixed in the plane and moves vertically to focus. Reactor chips have the
same length as ordinary chips and one quarter of their width.
Temperature and gas flow are controllable during observation.

This establishes proportions and motor functions, not absolute chip dimensions,
encoder availability, clamping, temperature/flow sensors, gas composition, real travel
limits or real stopping procedure. The old abstract `ReactionChamber` door
methods are historical placeholders and do not confirm an actual door.

## Synthetic growth model

`qumus2/testbench/reactor_model.py` provides a spatial, irreversible dose model.
It samples a fixed field of nucleation delays and rate variation from the
bench seed and chip identity. Extra observations do not consume the growth
random stream or change future outcomes.

For each cell, growth dose accumulates with time multiplied by a Gaussian
temperature response and a Gaussian response to log(flow/nominal flow). A small
fixed flux gradient makes different chip regions respond differently. Cells
begin growing after their nucleation dose, then saturate toward full coverage.
Excess accumulated dose penalizes quality through overgrowth. Excess temperature
adds irreversible damage. Returning to nominal conditions cannot erase it.
Outside the chamber, below the activation threshold or without flow, no new
growth dose accumulates. Overheating can still damage a chip without gas flow.

The current chosen demonstration profile is:

| Parameter | Chosen value | Provenance |
| --- | --- | --- |
| Material label | MoS2 (synthetic example) | Software demonstration choice |
| Nominal growth temperature | 820 C | Chosen fixture, not an apparatus recipe |
| Temperature-response width | 65 C | Chosen fixture |
| Nominal carrier-gas flow | 200 sccm | Chosen fixture |
| Log-flow response width | 0.55 | Chosen fixture |
| Nominal growth duration | 600 s | Chosen fixture |
| Growth activation threshold | 350 C | Chosen fixture |
| Spatial grid | 10 by 10 cells | Numerical discretization |

All numbers are editable simulation assumptions. No setpoint, optimum, safe
range or sensor in this profile is a measured fact about the owner's reactor.
The model is intended to exercise observation, control, degradation and record
keeping. It does not calculate chemistry, gas transport or real material yield.

Virtual image analysis returns coverage, spatial uniformity, defect fraction
and a dimensionless quality score. The score is coverage times uniformity times
one minus defect fraction. It is not a calibrated probability of a successful
experiment. Underlying doses and optima are excluded from the observation.
Raw simulator state remains available only to persistence, rendering and tests.

## Literature and the inference boundary

- Lopez-Posadas et al., *Direct observation of the CVD growth of monolayer MoS2
  using in situ optical spectroscopy*, Beilstein J. Nanotechnol. 10, 557–564
  (2019), [doi:10.3762/bjnano.10.57](https://www.beilstein-journals.org/bjnano/articles/10/57).
  This experimental study connects growth and optical changes to temperature
  and precursor supply. Window coating can also change the optical signal.
- *Control of the Nucleation Density of Molybdenum Disulfide in Large-Scale
  Synthesis Using Chemical Vapor Deposition*, Materials 11, 870 (2018),
  [doi:10.3390/ma11060870](https://www.mdpi.com/1996-1944/11/6/870).
  Carrier flow changes nucleation and growth, and high flow can suppress growth
  for the reported setup. More flow therefore need not always improve output.

The operating-window functions, cell grid, incubation distribution, numerical
profile and quality/overgrowth laws are our phenomenological design inspired by
these observations. They are not equations fitted to those papers, and no
single optimum is transferable between apparatuses. The sources do not show
that ordinary image contrast alone establishes material identity or thickness.

## Current verification

The initial model tests cover multiple seeds, temperature/flow/time sensitivity,
no-growth conditions, spatial variation, irreversible history, invalid inputs,
observation independence and JSON checkpoint continuation. Further integration
must preserve those properties when device commands, heater dynamics, stage
motion and recovery advance the shared Clock.

The optional device composition is enabled by setting `reactor.enabled=true`
in the supplied testbench configuration. Its geometry and dynamics are in
`config/reactor.yaml`. The default bench remains disabled. It seeds four
dedicated 2.5 by 10 mm chips representing manual preparation, with explicit
simulation provenance rather than a claim of human identity confirmation.
The ordinary chip fixture is 10 by 10 mm. A 3 by 11 mm carrier slot leaves
clearance around the narrow chip. Both absolute dimensions remain placeholders;
placement verification checks the complete rotated footprint with a 0.05 mm
synthetic edge margin. The storage uses the existing 35 by 40 mm centre pitch
in a smaller two-by-two board. The approved 1440 by 630 mm bench separates
Arm 3, the insertion corridor and storage. All arm bases share y=126 and the
same initial pose; both microscope bodies share a single rendering function.
Regular storage centres and arm bases alternate at x=130, 320, 510, 700, 890
and 1080 mm, giving five equal horizontal gaps of 190 mm (centre distance
190.515 mm with the common 14 mm Y offset). Reactor storage is the exception.
Each arm retains its original relative pose to its working platform. The rod
is 190 mm long; insertion travels 160 mm from (1200, 270) to (1200, 430),
directly under the chamber-centred observation window. These remain synthetic
fixture dimensions, with the existing safety envelope updated accordingly.
The shared reactor frame spans x=1140..1260; its compact 120 mm width clears
the drawn Arm 3 base and leaves 35 mm before dedicated storage at x=1295.
All storage slots remain inside the unchanged 280 mm Arm 3 reach. The optics,
chamber and loading point share the same nominal x=1200 centreline.

The heater and flow feedback evolve separately from their requested setpoints.
Growth integrates those evolving values on a fixed one-second grid. Extra
fractional-time observations do not change future growth. A control change
commits its partial interval before changing the setpoint, preserving history.
The carrier translates the seated chip without adding rotation or Z motion.
Assembly XY and insertion Y are independent device states. The chip's world
position is load + assembly XY + insertion Y + its measured seating offset.
The chamber and guide move with assembly XY; the microscope does not. Insertion
uses 30 mm/s and fine motion 1 mm/s as simulation settings. These moves advance
the shared Clock, giving replay a continuous command-to-completion interval.
Microscope focus changes leave carrier position untouched. Captured frames
retain their original metadata even after a later focus change.

Synthetic interlocks require insertion before heat/flow, prevent coarse
retraction while hot or flowing, prevent motion while the loading arm is low,
and require a cold accessible carrier for low arm access. Flow closure and
lifting the arm clear do not require the other process sensor to remain online.
The simulated shutdown commands heater off and flow target zero while retaining
carrier position; residual temperature and flow decay rather than instantly
becoming zero. No physical shutdown procedure is inferred from this choice.

Device checks also cover invalid flow/axes, ambiguous occupancy, unavailable
devices, independent focus, immutable saved-image metadata, observation
independence and a checkpoint restart with a different requested seed. Related
safety, twin and existing lab-process checks passed with the new optional layer.

## Jobs, agent and remaining laboratory questions

The device layer now composes the dedicated arm, whole-assembly XY stage,
independent insertion stage, independent
focus microscope, heater and gas-flow controller through SafetyGate. Closed-loop
jobs and a Reactor specialist now use the existing ToolGateway, permissions,
typed parameters, reservations, shared Clock, job records and checked delegation.
The fixed goal belongs to Qumus; Reactor cannot supply its own measurements or
change its acceptance thresholds. Success is explicitly field-limited.
Chosen
geometry and operating limits belong to the clearly synthetic configuration.

The developer acceptance driver intentionally starts with 740 C, 50 sccm for
120 s, submits the unsuccessful result, then uses observations and the nominal
profile to run a subsequent bounded window. The scripted and paid-model paths
both rejected the first result, accepted the later synthetic field result and
returned the chip after cooling. This is a single synthetic control scenario,
not optimization research or evidence of a transferable recipe. See
[review and operating instructions](reactor-review-2026-10-26.md).

Laboratory discussion must settle the chip preparation and seating, coordinate
calibration, actual command and feedback capabilities, temperature measurement
location, gas identity and supply, interpretation of optical observations,
reaction end criteria, and insertion/retraction and shutdown procedures. These
questions constrain later physical integration rather than blocking the
authorized virtual prototype.

See [reviewed controller interfaces](../reactor-hardware.md) for the group GitHub
source versions, implemented injection boundaries and remaining calibration.
