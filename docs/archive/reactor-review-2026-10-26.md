> Historical development evidence, superseded by the integrated optional reactor and current workstreams. Old startup/release instructions are not current.

# Experimental reactor: review and handoff

The owner requested two independent outputs for 26 October 2026: a complete
report on stable v2, and partial reactor development in an isolated branch.
This prototype is the second output. It is excluded from the group report.
No merge, release, physical operation or apparatus certification is implied.

## Isolation and return to the working version

- Stable checkout: `/Users/jialei/Projects/Qumus/v2`; it advances independently.
- Experimental checkout: `/Users/jialei/Projects/Qumus/worktrees/vdw-reactor-simulation`,
  branch `codex/vdw-reactor-simulation`.
- The normal `/Users/jialei/Projects/Qumus/Start Qumus.command` still starts stable v2.
- Original recommended demos and media are preserved. Reactor exports use new
  names in the experimental checkout. They are not the stable demo default.

To return from an experimental preview, stop that preview and use the normal
launcher. No reset, file replacement, recording deletion or branch change in
the stable checkout is required. Preserve private `runs/reactor-experiment/`
if later work needs the raw acceptance evidence; it is not committed.

## Implemented scope

The opt-in module adds dedicated manually prepared substrate stock and a third
robot arm, whole-assembly XY positioning and independent insertion Y, a rectangular
chamber/circular passage/centred top window, independent microscope Z focus,
lagged temperature and gas feedback, and a seeded spatial growth model.
The owner clarified the three-motor mechanism on 2026-10-26; this supersedes
the first prototype's combined insertion/fine-Y axis. Field changes follow the chip.
Geometry, envelopes, kinetics and optical measurements are synthetic fixtures.

The Reactor specialist uses typed ToolGateway calls and guarded jobs. Eyes
inspects first. Chip/carrier reservations prevent conflicting submissions.
Hardware execution and measured material-quality acceptance are separate.
The result checker uses the latest successful project/chip growth attempt and
stored finite field metrics against the current goal. A later failed attempt,
changed goal or uncertain exposure history prevents reuse of old acceptance.
An interrupted exposure parks for review rather than repeating blindly.

The UI shows Reactor, the moving carrier, measured process feedback and the
separate microscope. Image metadata is frozen when captured. Extra observations
do not resample growth. The synthetic micrographs are qualitative renderings;
there is no independently trained computer-vision measurement pipeline.

## Reactor assembly clearance update (2026-10-26)

The owner requested visible gaps on both sides of the new common assembly
frame. The synthetic chamber/frame is now 120 mm wide at x=1140..1260, with
the loading point, rod and nominal microscope centreline shifted 20 mm right
to x=1200. Dedicated storage moves 10 mm right to x=1295..1380. Arm bases,
the five 190 mm regular-row gaps, exfoliation/search workcells, chip spacing,
rod length and insertion travel remain unchanged. The Arm 3 loading interlock
follows its new local x=120 loading point. Its caption now sits to the left
of the nozzle so it cannot run into the adjacent reactor frame when parked.

Nominal displayed base-to-frame clearance is 23.1 mm; frame-to-storage
clearance is 35 mm. Both stay positive throughout the +/-4 mm assembly
travel (19.1 and 31 mm minimum). The furthest dedicated storage slot is
276.23 mm from Arm 3, inside its unchanged 280 mm reach. This compact body
width is a simulation fixture, not a measured apparatus dimension.

`recordings/experimental-reactor-clearance-20261026` is the new replay;
private source evidence is at
`runs/checks/reactor-assembly-clearance-20261026/summary.json`. Eight jobs
and all 11 acceptance checks passed using the documented nominal placement
fixture, with $0 model cost. Prior recordings remain unchanged.

## Approved equal-spacing layout (2026-10-26)

The implemented geometry matches proposal revision 2 under the owner's local
`notes/reactor-layout-preview-20261026/`: five 190 mm horizontal gaps in the
S1/A1/S2/A2/S3/A3 row, with reactor storage excepted. Arm 1 and exfoliation
translate together by +13 mm; Arm 2 and search/stacking translate together by
-65 mm. Their relative frames, local motion paths and platform Y positions
are unchanged. Slots, fiducials, named camera regions, tape rail, fixed optics
and stamp-holder origin follow their complete modules. The table is 1440 by
630 mm. The reactor rod is 190 mm long with 160 mm travel, finishing at the
central optics at (1180, 430). Insertion/access envelopes match the new load
point. Physical checkpoints with different geometry are refused for resume;
all prior recordings retain their original layout for playback.

The new recording is `recordings/experimental-reactor-equal-spacing-20261026`.
All eight jobs and 11 acceptance checks passed: 209 guarded device commands,
44 saved images, continuous insertion and retraction, and the chip returned
to storage. The private evidence is
`runs/checks/reactor-equal-spacing-nominal-20261026/summary.json`. Model cost
was $0. The deliberately poor growth window was rejected; the later field
reached coverage 0.893546, uniformity 0.981210 and defect fraction 0.027250.

This recording explicitly uses `--nominal-placement` (synthetic standard
deviations 0.05 mm / 0.2 degrees); its exact placement fixture is saved in
recording metadata. The earlier attempt with default 0.25 mm / 1 degree
placement stopped after three unsuccessful narrow-slot corrections and is
preserved at `runs/checks/reactor-equal-spacing-20261026`. Neither the slot
dimensions nor the acceptance margin were relaxed. These are visualization
and software acceptance fixtures, not measured hardware accuracy.

The complete exfoliation, search and three-layer stacking path also passed
on the translated reactor-enabled bench. Offline replay checks cover both the
old and new recorded insertion/retraction durations and intermediate chip/rod
positions. Browser inspection observed distinct intermediate rod transforms.
The complete modern non-browser suite passed (expected skips remain), as did
all 15 offline JavaScript checks and all 14 frozen-v1 comparison tests in the
separate legacy environment. Ruff and diff whitespace checks passed. The
historical browser-inclusive baseline failures below were not rerun for this
geometry change. The new replay was visually inspected in the browser at 1x,
including continuous insertion, and left expanded at the initial layout.

The owner's subsequent clarification is reflected by a common rectangular
assembly frame around the chamber and the complete insertion mechanism. The
rod inherits the assembly's XY transform; its only independent transform is
the loading/unloading insertion. A fixed microscope label belongs to the
stationary optics layer. No physical geometry, equal-spacing coordinates or
recorded commands changed for this display update. Both recorded XY moves
were checked at five points: chip/chamber relative position and insertion
remain constant. Browser inspection observed 31 intermediate common-frame
transforms and confirmed fixed microscope coordinates before/after the move.
The 15 offline replay checks and 17 reactor device checks passed.

## Earlier three-motor redesign evidence (2026-10-26)

The bench is widened to 1500 mm, with independent clearance for Arm 3, the rod
and reactor storage. All three arm bases and initial poses align. Both
microscopes reuse the same body/crosshair renderer. The chamber window is
centred. Chips are 2.5 by 10 mm against the ordinary 10 by 10 mm fixture,
matching the owner-confirmed quarter width and unchanged length. Reactor
storage is a smaller two-by-two board with the existing 35 by 40 mm pitch.

`reactor_slot` now moves the entire chamber/rod assembly through bottom X and
top Y. The new `reactor_insert` advances the rod separately. Narrow-slot
placement checks the rotated chip footprint with an edge margin; ordinary
centre tolerances alone cannot accept an overhanging chip. Moves advance the
Clock, preserve exposure along the path and provide continuous replay timing.
The Lab instrument details group all three motors. **Expand** opens a larger
animated bench with replay controls. Old recordings remain unchanged and
replayable; old mechanics checkpoints cannot silently resume as three motors.

The group controller review and opt-in injection adapters are documented in
[reactor hardware interfaces](../reactor-hardware.md). Changing a motor board or
furnace controller is confined to the transport/adapter and calibration. Real
microscope completion and the arm's actual gripper still need explicit matching
contracts and physical commissioning; no hardware was operated.

The new self-contained recording is
`recordings/experimental-reactor-mechanics-20261026`. Its source evidence is
`runs/reactor-three-motor-release-20261026/summary.json` (private, uncommitted).
All 11 acceptance checks passed with **$0 model cost**. All eight jobs
succeeded. The narrow-slot check required three observed placement corrections
before insertion. The poor growth result was rejected; the later synthetic
field reached coverage 0.893546, uniformity 0.981210 and defect fraction 0.027250.
Targets were disabled, feedback fell into the virtual access envelope, and the
chip returned to its original slot. These measurements describe a synthetic
field, not physical growth or whole-chip quality.

Current verification:

- Modern non-browser suite: **402 passed, 4 skipped**, 201.65 s. Reactor model,
  device, flow and adapter subset: **68 passed**. Additional affected safety
  checks passed after the shutdown-readback fix.
- Offline JavaScript suite: **14 passed**, including the actual new recording's
  insertion/retraction duration and intermediate rod/chip positions.
- Separate frozen-v1 comparison environment: **14 passed**. Ruff and diff
  whitespace checks passed. Original recordings and frozen-v1 sources have no
  tracked differences.
- The earlier full browser-inclusive run found the same four UI failures
  reproduced on the untouched baseline in
  `/Users/jialei/Projects/Qumus/notes/planning/2026-10-26-phase-coordination.md`:
  inventory wording, History tool markers, recommendation wording, and singleton
  result selection. They remain outside this reactor redesign; the entire
  browser suite is not claimed green.
- In-app browser review sampled 53 distinct intermediate retraction transforms
  and continuous insertion transforms. During assembly adjustment, 44 different
  chamber positions shared one fixed microscope position. The enlarged initial
  view shows aligned arms, narrow chips, smaller storage and a clear insertion
  corridor. No browser warning/error was captured. The local review screenshot
  is `notes/reactor-integration-20261026/reactor-bench-review.png` under the
  owner-managed Qumus workspace.

The ordinary main checkout is undergoing separate work and was not edited by
this redesign. Integration still requires reconciling its newer runtime and
documentation changes; the experimental branch has not been merged or pushed.

## Initial prototype evidence (historical, before three-motor correction)

Both scripted decisions through the real gateway and a paid model trial passed
the deliberately suboptimal-to-nominal scenario. The paid trial used the existing
configured `openai/gpt-6-astra` gateway model, a $6 estimated run cap and the
existing shared phase ledger. Estimated token-rate cost was $3.63948, not a
provider invoice. No additional model calls are needed for playback.

The model trial ran one 120 s window at 740 C / 50 sccm, correctly rejected its
zero-coverage result, then ran 600 s at 820 C / 200 sccm. Saved synthetic field
metrics were coverage 0.894170, uniformity 0.979958 and defect fraction 0.029389.
The unchanged thresholds were 0.75 / 0.80 / maximum 0.20. Shutdown/unload
disabled targets, waited for measured virtual access conditions, retracted and
returned `reactor_chip_01` to `reactor_chip_slot_1`. All six recorded jobs
succeeded; no safety violation or model error occurred. This one trial is not
a robustness estimate or a real growth experiment.

Targeted checks cover invalid axes/flow/schema/roles, default-disabled behavior,
wrong inventory, hot motion, unavailable observations, chip/slot reservations,
lag and independent focus, immutable image metadata, history and random-seed
restore, extra observation independence, honest quality rejection, project and
result freshness, changed goals, and separate-process jobs/feedback/image access.
Final branch regression: **391 passed, 1 skipped** (non-browser modern-runtime
suite, 340.34 s). The separate legacy environment passed all **14** frozen-v1
comparison tests. Ruff, diff whitespace checks and syntax checks of all **26**
browser JavaScript modules passed. The targeted reactor/recording checks also
passed. These are executed software checks; they do not warrant a zero-bug claim.

Chromium review of the model replay checked the final recorded state, the
640 by 420 reactor image and its 820 C / 200 sccm / 89.4% field caption, plus
seeking back to the beginning without stale reactor captions. No page error
occurred. These checks do not mean every historical browser fixture passes.
The stable baseline already has three unrelated browser-test failures documented
with the presentation audit; its recommended actual replays were inspected.
The final experimental UI also replayed both original recommended device and
collaboration recordings, with loaded microscope pixels, no reactor panel on
the original benches and no page errors. All 1,566 original recording files
match their saved hashes in both checkouts, all 212 frozen-v1 files match the
manifest, and the stable checkout remains clean at `27e0566`.

## Reproduce without paid calls

Run from this experimental checkout, using the modern environment:

```sh
/Users/jialei/Projects/Qumus/v2/.venv/bin/python scripts/record_reactor_experiment.py --nominal-placement
/Users/jialei/Projects/Qumus/v2/.venv/bin/python -m pytest tests/test_reactor_model.py tests/test_reactor_devices.py tests/test_reactor_workflow.py tests/test_reactor_adapters.py
```

The first command creates a new private run and self-contained simulated export;
it refuses to overwrite an existing output directory. `--live` is a paid opt-in,
uses project-scoped credentials and the persistent configured ledger, and obeys
its explicit budget. Do not copy environment files or reset the spend ledger.

Use the same application on an unused port to play the new recording without
interrupting the normal application:

```sh
/Users/jialei/Projects/Qumus/v2/.venv/bin/python -m qumus2 replay recordings/experimental-reactor-clearance-20261026 --port 8767
```

`serve --reactor` enables the synthetic devices in this branch; without the
flag the default bench remains disabled. Typed chat in scripted mode still
follows its selected scripted scenario. For the reactor scripted trial, use
the acceptance driver above rather than the ordinary graphene scenario.

## Work deliberately left for laboratory discussion

Confirm actual chip dimensions/preparation and carrier seating; travel, loading
clearance and coordinate calibration; drive/encoder/controller protocols;
temperature-sensor position and calibration; gas identities, channels and flow
feedback; image interpretation and endpoint criteria; and the actual sequence
for insertion, retraction, cooling and shutdown. The current virtual envelopes
must not be treated as physical limits. Additional profiles, broader fault/seed
evaluation and a calibrated image-analysis model remain future work.

The useful next step is to review those questions with the experimental team,
then revise the experimental branch before separately authorizing integration.
