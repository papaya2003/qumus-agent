# Source-based stacking process

This document preserves the version 1 source sequence and earlier diagnostic
contracts. The restarted Hall-bar delivery follows the current contracts in
[simulation contracts](sim-contracts.md) and the ordered acceptance gates in
[workstreams](workstreams.md). Earlier ideal-baseline or campaign instructions
below do not authorize the restarted delivery or establish its acceptance.
Continuous dual-plane tracking and descent-time XY correction are not required
by the selected version 1 sequence.

The original source and `v1compare/v1/` copies of the eight files below were
independently checked byte for byte. Preserve the frozen copy. Its implementation
establishes historical software behavior, not current laboratory calibration or
proof that the physical transfers succeeded.

## Preserved version 1 sequence

The table describes the legacy source/migration behavior. For the current
eight-contact hBN/Gr design, the architecture adds observed aperture alignment,
a coherent Newton-ring family bound to the same first-contact exposure,
complete-source contact-interior coverage with retained unknown pixels, finite
dwell, and lateral stamp clearance before source-disappearance inspection.
Legacy point-contour coverage alone cannot authorize that design's pickup.

| Stage | Version 1 behavior | Required simulation behavior |
| --- | --- | --- |
| Locate a source | Detect a chip corner, roughly refind, then match at 20x. Move the flake to image centre and apply the optional design shift. | Use captured source evidence, retain corner/refind history, and apply the selected design shift in its declared frame. Do not give every layer an invented fixed offset. |
| Initialize the stamp | Select the low-power turret position, move L/Z to presets and apply a fixed mechanical focus offset. | Model the ordered mechanical actions through the existing axes and gate. Do not invent visual identification of the stamp centre, PC underside or a carried flake. |
| Adjust first contact | Before the first layer only, compare each descending frame with the initial frame. Threshold the difference and locate a sufficiently large, approximately round changed component. Lift and adjust XY once. | Record this as an observed first-contact candidate. It is distinct from the PDMS geometric centre and is not an interferometric gap measurement. Bound descent and refuse ambiguous or unavailable evidence. |
| Pick up | Descend in Z, compare with the retained reference, select the largest admitted changed-region contour, and check that the target point has the specified interior edge margin. Lift after coverage. | Follow this Z/observation/coverage order without continuous XY correction or required dual-plane tracking. Save the reference, changed region, target point, margin and motion results. |
| Continue with another layer | Refind the next source, apply its design shift, initialize the stamp, and reuse its lateral placement. Only layer A has first-contact adjustment in the four-layer assembly. | Preserve holder lateral placement across layers. Do not run a new first-contact adjustment or introduce a per-layer stamp-material alignment servo. |
| Release | Heat to the historical drop threshold, move Z to a preset, continue heating to the release threshold, then lift and turn outputs off. | Preserve the stage order with finite temperature/motion bounds, measured feedback and safe shutdown. Historical temperatures and encoder positions are not universal recipes. |

Coverage is a process observation. It does not alone prove complete-flake
containment, transfer identity, stack registry, film integrity or chemical
success. Endpoint observation and independent simulated relocation checks must
report those outcomes separately.

The owner clarified the practical visual endpoint criteria on 31 October:

- Pickup is confirmed when material disappears from the recorded selected
  source location on the wafer.
- Placement is confirmed when the expected material appears at the intended
  location on the target wafer.

Use qualified native-20x wafer images at the correct recorded ROI, with held
stage/holder context, focus, calibration, capture time and retained image evidence.
For placement, retain a before view as well as the after view; material already
present in the before view cannot establish new appearance. Ambiguous or
unavailable observations remain unverified, with no ledger relocation. Material
counts are diagnostics, not proof of every buried layer's identity. Do not add
mandatory positive stamp autofocus or proof that a featureless film is empty.
These owner-defined process criteria do not supply new texture, calibration or
an attestation that any particular recorded transfer succeeded.

## Ideal baseline before disturbance validation

This section retains an earlier, opt-in diagnostic contract. It is not the
current Hall-bar acceptance gate, which uses the declared normal profile.
For an explicitly selected ideal diagnostic, disable time- and Z-dependent actual/apparent lateral
drift, slip, positioning errors and random observation errors, and make admitted
pickup/release probabilities deterministic. Publish the effective configuration
and complete disabled-mechanism list with each result. Ordinary saved worlds and
earlier noisy trials remain separate evidence; they cannot be relabelled ideal.

Retain spherical stamp geometry, the circular contact patch and its evolution,
normal coordinate transforms, temperature requirements, physical transfer rules,
safety limits and existing endpoint/overlap criteria. Source and target wafer
focus remain observed; focusing the stamp underside is not a prerequisite.
Retain first-contact adjustment and subsequent working-XY memory. Do not add
per-layer compensation, manual correction offsets, rendered snapping or edits to
material coordinates to make the baseline succeed.

Verify source-local to world to stamp-relative coordinates, persistence and
release reconstruction into target-local coordinates against the actual images.
Material appearing somewhere on the target does not prove the intended layer
overlap. If ideal stacking is misregistered, preserve the first failure and
diagnose coordinate memory, transforms and image/state consistency before adding
another alignment operation. Disturbance and robustness validation follow as a
separate stage; an ideal result alone does not establish those properties.

A torn film requires attention when partial transfer cannot be excluded. An
identifiable remnant on the source is not proof that nothing transferred, and
an initially empty ledger cannot authorize automatic stamp replacement after
that stroke. Preserve the damage and source-presence observations as evidence;
do not promote them to an intact-material or no-transfer certificate.

## Units and bounded adaptation

Version 1 uses a historical 5x scale of 0.730 micrometres per pixel. Its
first-contact target is `(1920, 1080 - 300)`; pickup uses `(1900, 1100)` and an
interior margin greater than 240 pixels. Relative to the code's reference point
`(1920, 1080)`, the corresponding source-scale distances are 219 micrometres,
14.6 micrometres per pickup-point axis, and 175.2 micrometres of margin.
These are source-derived quantities, not a new measurement of today's camera.
The hardcoded reference point does not by itself prove an actual image size.

Map reference-plane intent through an explicitly identified captured pixel
calibration and axis convention. Do not copy old pixel indices, motor signs,
connected-component areas or blur-kernel widths into a differently sized raster.
If the historical scale cannot be mapped faithfully, disclose that limit instead
of relabelling it as calibrated. Current device coordinates remain separate
from the historical L/Z presets and focus-controller units.

In the declared SIM profile, initialization and withdrawal use the existing
6.5 mm holder safe height. The historical release command adds 5 mm to its old
encoder position; copying that delta after the adapted 5.7 mm release preset
would request 10.7 mm and can exceed objective clearance or the endpoint focus
stroke. Map that withdrawal intent to the current safe height, recording the
historical command, requested SIM target and actual readback. This is a disclosed
mechanical adaptation, not a calibrated conversion or a silently clamped move.
Preserve release temperature, lift, heater-off and endpoint order. A current
floor/envelope refusal still holds the process; observation cannot bypass it.

The historical first-contact increment is -0.01 mm and the pickup command intent
is -0.1 mm. Their unbounded or very long loops are not copied into the runtime.
Keep the existing physical floors, lateral interlocks, watchdogs, travel limits,
finite observation/time budgets and Stop/recovery contracts. Subdivide an old
command intent when the existing gate or exposure deadline requires it, with
observations between admitted segments. This adaptation does not authorize
continuous XY correction, inferred gap control or a second execution path.

When a whole pickup increment would cross the evidence-bound process minimum
`M`, a single final partial increment may end at
`T=nextafter(M+tolerance_mm,+infinity)`. Require actual current Z above `T`,
meaningful progress, `T-tolerance_mm>=M`, qualified unchanged observations and
global admission of both the original request and `T`. Reserve this opportunity
and append its actual admission event before motion in the existing Store.
Execute `T` through the same segmented controller while retaining the original
request separately. No coverage at the end, interrupted motion or uncertain
evidence holds for review; there is no repeated tail, automatic repress or new
XY adjustment. This uses the existing command tolerance as headroom, not a
new calibration or a wider press budget.

The shared release operation requests the fixed existing 0.5 C headroom for
entry too: 155.5 C for the declared PC entry and 135.5 C for the admitted MICA
entry. Actual entry readings must still reach 155 C and 135 C respectively;
the subsequent release predicates are unchanged. An illegal setpoint is refused.
The heater model, generic ramp/wait logic, stuck detection and finite deadlines
remain in force.

The pending `v1-normal-sim-v2` profile additionally requires a fresh native-20x
source autofocus after final recentering/design shift, with the holder clear
and parked. Retain the observed interval, capture-time axis/optical/pose binding,
AF event, exposure event and image hashes in the existing Store. An earlier
focus before movement, a current calibration query or a World height cannot
be substituted for that history.

For later layers, keep the first source and first candidate as the same anchor.
The named SIM positive-up mapping uses scale `s`, first interval `[L0,U0]`,
current interval `[Lk,Uk]` and admitted transport uncertainty `E`:
`D=[s*(Lk-U0)-E, s*(Uk-L0)+E]`, then `R=candidate_z+D`.
The minimum permitted holder Z is `R.upper-max_press_mm`; the existing
`max_press_mm=0.2` remains unchanged. The first source uses zero displacement.
AF intervals already include readback error, which must not be counted again.
Missing, incompatible or unobservable evidence holds the process. This is a
conservative optical process reference, not a contact-height or force claim,
and does not authorize real hardware or generic signed-axis support.

The physical simulator separately retains actual whole-material contact history
through withdrawal. Image coverage cannot write that state. A single evolved
footprint must contain the whole outline; one whole-stack witness is required
for release geometry. The final shrinking footprint does not erase earlier
contact, and a moved maximum disc cannot invent it. Temperature, film and
whole-outline contact requirements remain in force in every profile. Ordinary
stochastic and placement laws are disabled only by the explicit ideal profile;
fault-injection tests must still exercise their named faults. This disclosed
synthetic model does not resolve partial fragmentation or establish laboratory
adhesion parameters.

## Target-relative release policy

The release reference transports the same first-anchor native-20x process
reference to the parked target-before observation. With first interval
`[L0,U0]`, target interval `[Lt,Ut]`, first candidate `C`, admitted scale `s`
and mapping error `e`, use `Rt=C+[s*(Lt-U0)-2e,s*(Ut-L0)+2e]`.
AF intervals already contain readback uncertainty. The pending version 2 policy
uses the latest required original pickup in the submitted flake order, on the
same continuously loaded stamp and first anchor. Let `c_i` be its dispatched
covered segment target and `Ri` its recomputed source process reference. Use
`d=c_i-Ri.upper` and request `Rt.upper+d`. Require the original verified coverage,
source disappearance, reference/current image bytes, settled capture and actual
command/encoder history. Recompute the unchanged raster point-interior-margin
predicate; source disappearance alone does not prove that linkage.
The minimum remains `Rt.upper-max_press_mm`; require the requested target minus
the existing command tolerance to remain above it, as well as all global limits.
The intent must satisfy `tolerance-max_press_mm <= d < 0`. Missing or inadmissible
proof for that required latest pickup holds. Do not choose a different earlier
witness, a deepest offset, an unexecuted macro target, a case-specific height,
clamping, an extra press or an accumulated per-layer offset.

This is an explicitly declared SIM process policy. Version 1 supplies an
absolute release preset, not a measured penetration relative to first contact.
The retained nominal 5.8/5.7 pair and its earlier fixed-difference policy remain
historical SIM provenance. The new rule is a declared process-memory point
intent: independent AF uncertainty does not cancel, and it cannot guarantee
reproduction of physical contact or full-material coverage. Neither the
transported reference nor the witnessed command establishes contact, gap, force
or a laboratory recipe. Only a fresh target-before AF with
the same admitted native objective/axis mapping can support this conversion;
held-target after-focus and synthetic World geometry cannot authorize it.

After the single planned stroke, measured release threshold, clear withdrawal
and confirmed heater-off, park the stamp through the existing guarded handling
path before observing the target. Hold the target stage/ROI and use its verified
before-focus as the seed for one bounded fresh native-20x AF and three target
views. A seed or plane label is not successful focus evidence. Missing optical
observability, changed context or ambiguous appearance holds the process.
There is no new texture, fiducial or generic registration requirement.

Reserve mandatory exit and endpoint work within the existing observation,
travel and time limits. Heat-only captures may follow the remaining observation
deadline, including capture reserve; Z motion keeps its settled segment exposure.
Temperature predicates, stall protection, watchdogs and Stop remain unchanged.
Confirmed-off terminal park uses declared transport timing and public encoder
evidence at clear height. Its whole time and captures still consume the same
budgets; it is not unobserved contact or a continuation of the press.
Version the release policy in fresh resolved parameters and saved evidence;
raw recovery admission rejects missing/old policy before context or energy
restoration. Preserved stopped runs do not acquire it retrospectively.

## Stamp structure and evidence

The owner's drawing reports a rigid glass support, a deformable PDMS cap below
it, and a PC working film on the face toward the silicon. A paper mask covers
part of the slide in plan view, with an aperture corresponding to the PDMS
projection. This report supplies topology, not dimensions, stiffness, a force
law or an optical calibration. Keep the original drawing private.

Represent the PDMS geometric centre, first observed contact point and positions
of material carried by PC as distinct quantities. Do not default them to the
same position. The mask/PC attachment order and film-edge fixation are unknown;
do not fill them from a drawing convention. Use the existing evidence/store and
physical model boundaries rather than introduce another laboratory-state store.

The owner also reports white-light microscope illumination. Newton-ring visuals
must show colour gradients that change continuously with the same recorded gap,
geometry and Clock state. Replace the old fixed alternating palette in the
existing rendering path; do not animate rings independently or randomize their
colour per frame. A bounded synthetic spectral/camera approximation is allowed
and must be identified as such. Source spectrum, camera response and film optical
thickness remain unmeasured. The owner has not reported that all outer rings
disappear after contact; do not encode that claim as an experimental fact.
The current synthetic profile uses the usual air-gap phase relationship and
fixed illustrative broad RGB responses, not a fitted PC/PDMS/silicon optical
stack. White-light coloured rings are supported by the
[Harvard lecture demonstration](https://sciencedemonstrations.fas.harvard.edu/presentations/newtons-rings);
that reference does not calibrate this apparatus or validate its deforming cap.

## Source paths

All paths below are relative to `v1compare/v1/`:

- `agents/processing_agent/workflows/search/molecule_flows/molecule_flake_refind_accurate.py`
- `agents/processing_agent/workflows/stacking/molecule_flows/molecule_initialize_stamp.py`
- `agents/processing_agent/workflows/stacking/molecule_flows/molecule_adjust_touching_point.py`
- `agents/processing_agent/workflows/stacking/molecule_flows/molecule_pick_up.py`
- `agents/processing_agent/workflows/stacking/molecule_flows/utils/touching_detection.py`
- `agents/processing_agent/workflows/stacking/molecule_flows/utils/tools.py`
- `agents/processing_agent/workflows/stacking/molecule_flows/molecule_drop_down.py`
- `agents/processing_agent/workflows/stacking/assembly_flows/assembly_quantum_hall_device.py`

## Migration and acceptance

Keep the previous implementation, failed attempts and review receipts recoverable
in Git/private history. Retire superseded servo-only code and configuration after
checking actual callers; do not retain an extra default process, launcher or
controller solely for compatibility. Preserve useful independent source imaging,
tracking, axes, physics, event/replay, billing and safety work.

Existing unfinished runs with the old workflow binding require review; do not
reinterpret them as execution of this process. Rebind normal transfer checks,
workflow manifests, prompts and the final demonstration source freeze. Validate
the actual stage order and observations, first-contact adjustment count, lateral
placement reuse, bounded Z execution, coverage/transfer distinction, Stop/recovery
and preserved old records. Current release is governed by F-1 local integration
acceptance, then G-1: only the first Astra recoverable-fault recording. Its
inclusive USD100 authorization covers qualification, retries and unresolved
charge reservations; configured token charges remain estimates, not verified
bills. Deliver replay, recovery findings and cache-aware accounting. The other
fourteen recordings require the owner's review of the first and funded limits.
Failed or unsupported execution does not pass this gate. All recordings use
the virtual laboratory with learning disabled. No real hardware or CI/Actions
execution is authorized.
