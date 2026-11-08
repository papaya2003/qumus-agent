# The Qumus v2 testbench

The testbench is a simulated version of the lab. Qumus v2 runs on it without any
hardware: the agents, tools, flows, job service, safety layer and UI are the
real code, and only the devices underneath are simulated. This document lets
someone outside the project see three things: how the simulated world is
modelled, which faults can be injected, and what the tests and benchmarks
measure.

Current contracts and the owner's apparatus clarification are in
[sim-contracts.md](sim-contracts.md). Earlier implementation decisions are in
the [dated archive](archive/architecture-decisions-through-2026-10-25.md).
This guide describes the mainline implementation. Acceptance of the current
iteration is tracked separately in [workstreams.md](workstreams.md); the
everyday profile includes the material-metal reactor simulation.

The ordinary stage now has coarse travel, fine x/y and in-plane rotation;
microscope focus moves independently in Z. Devices, interlocks, flows and
checkpoint signatures share those semantics. Geometry, dynamic rates and
perception remain synthetic assumptions rather than apparatus calibration.
Mother/daughter tape handling and lifted-chip recovery retain documented gaps.

## 1. What it is, and what it is not

- **It tests the control software, not physics.** Each part of the lab is modelled
  only as far as the software can see it: where things are, what the cameras
  report, what a device does with a command, and how each of those can go wrong.
  The controllable mechanics are simulated (positions, motions, focus, the
  stamp's optical shift and the air gap under the dome). What depends on the
  polymer (tape adhesion, PC film pick-up) is a probability model. For
  exfoliation, the yield and layer mix do **not** depend on the recipe: the
  hidden *best recipe* model (Jialei, 2026-10-22; spec 11.2) was removed by
  Jialei on 2026-11-03, because the simulation runs without the experience
  module and nobody tunes the recipe there.
- **Its numbers are placeholders.** Placement errors, detector rates, flake
  yields and pick-up odds are plausible guesses, a few from v1's code or logs,
  and none of them are measured in this lab. A count from a testbench run says how
  the software copes with this model. It is **not** a lab success rate.
- **It never feeds the experience library.** Self-evolution learns only from real
  experiments (spec 10.1); runs on the testbench are marked as simulated.
- **It approximates the lab layout** using earlier drawings and synthetic
  coordinates. Known differences are listed above; positions and interlocks
  require apparatus verification before hardware integration.
- **It has a ground truth.** The simulated world always knows where every object
  really is. Benchmarks judge a run against that truth, not against what the
  system claims.

## 2. How it is built

```
            agents (OpenAI SDK or scripted)        (qumus2/agents)
                 |  tool calls (checked by hooks)
            hardware tools -> job service -> flows of closed-loop steps   (qumus2/flows, runtime)
                 |  every device command
            SafetyGate: limits, interlocks, logging                    (qumus2/safety)
                 |
   sim devices: arms, stages, heaters, switches, exfoliator,     (qumus2/devices/sim)
   stamp holder, overview camera, microscope
                 |  change / read
            SimWorld: ground truth (objects, flakes, tape, stamps,      (qumus2/testbench/world.py)
            stage heights, collisions)  <- FaultInjector (faults.py)
                 |  frames carry the truth
   sim perception: SimLocator (overview camera), SimMicroscopeAnalyzer  (testbench/locator.py)
   = truth + noise + bias + misses
                 |
   rendered microscope pixels -> autofocus, specimen registration,
   contact-front measurements and bounded transfer control (perception/tracking.py)
```

Overview detection and synthetic flake classification still use the simulated
analyzers. Autofocus and the transfer controller measure captured raster pixels;
they do not obtain the target's current location or contact state from hidden
world metadata. This distinction matters when interpreting robustness results.

- **Flake-search scan (owner decision 2026-11-04, #203).**
  - **Focus.** The microscope autofocuses once per chip, at three corner
    fields, and fits a plane. Every 10x field is then taken on that plane
    without refocusing (`verification.yaml` `scan.focus_policy: chip_plane`).
    Revisits at 20x/100x autofocus again as before.
    *Limitation for the real lab:* this assumes a flat, rigidly held chip. A
    warped chip, a chip that moves, or focus drift during a long scan will
    blur fields. v1 instead used the camera's continuous hardware autofocus
    and re-snapped each field until its blur score reached `min_blur`. Revisit
    this before running on the real camera. `focus_policy: per_tile` keeps the
    old per-field software refocus.
  - **Which fields are kept.** A field without material is recognised in
    memory and then dropped: it is neither saved nor shown. Fields with
    material are saved and shown at their capture time.
  - **No previews.** No moving-scene preview frames are drawn during the scan
    or the revisits.
  - **Photo format.** v1-camera search photos are JPEG q95, as v1 wrote them
    with `cv2.imwrite`. The recognizer reads those same JPEG bytes. Stamp and
    contact frames stay lossless PNG, matching v1 stacking's `step_N.png`.
- **Closed-loop steps.** Every flow is a list of steps. Each step runs act,
  verify, correct (up to a limit), and stops with a reason when it cannot
  succeed (`flows/step.py`). On a stop, a step records in the ledger what its
  last look showed (`on_give_up`).
- **The lab twin.** After every command the gate logs what changed in the lab
  (`lab.frame` events), which the UI animates and recordings replay.
- **Clock.** Everything takes time from a `Clock`. Tests use a simulated clock,
  so arm travel, heating and peeling take simulated seconds.
- **Lab process.** The world can run in its own supervised process
  (`serve --lab-process`). Its state is saved before every job device call, so a
  killed process restarts where it was (`testbench/persist.py`).
- **Layout and numbers.** The layout is in `config/bench.yaml` (one world frame
  with its origin at the table's front-left corner; each arm commands in its own
  frame). Safety limits are in `config/limits.yaml`, verification tolerances in
  `config/verification.yaml`, and the error model in `config/testbench.yaml`.

## 3. The stations

The table layout follows the real lab. The top row holds exfoliation (left),
finding and stacking (middle) and the reactor (right). The bottom row holds a
storage board, arm 1, a storage board, arm 2 and a storage board.

| station | how it is modelled | where |
|---|---|---|
| **Storage boards** (left, middle, right) | Black frames (slots) hold chips, material slides and stamps. QR markers sit at the board corners: the overview camera calibrates from them, and the objects themselves carry no QR code. Left board: material tapes on slides and bare chips. Middle board: exfoliated chips and the hand-over between the arms. Right board: stamps, electrode chips and finished stacks. | `bench.yaml stations, slots, qr_markers` |
| **Left and right arms** | Desktop arms commanded in G-code style, each with a rubber suction nozzle that holds chips. The left arm's nozzle also presses the tape. Moves take distance / speed plus settling time. The SafetyGate refuses moves beyond `reach_mm` and any move of an arm that is not homed. Two arm faults: lost steps (the arm sits off by dx/dy until it homes) and a controller reset (it must home before it moves). A held object keeps its offset on the nozzle; placement adds an error on top. | `devices/sim/devices.py SimArm`, `world.py carry/place` |
| **Exfoliation stage and exfoliator** | Seen from the front: a fixed roll and a movable roll on an x/z cross positioner, with the white tape as the straight line between them. The tape is *laid* on a chip only when it spans the chip in x, overlaps it in y and comes within `press_reach_mm` of its surface. The press pad (the left arm's rubber nozzle) marks 0.5 mm cells as pressed, and only inside the tape band. The nozzle presses `press_depth_mm` below first contact (`press_contact_z` 1.2); the arm is position-controlled, so press "force" is expressed as depth. The yield scales with the pressed fraction, and flakes land only in pressed cells. **No recipe model (since 2026-11-03):** temperature, peel speed, peel angle, press depth and massage rounds stay parameters of the `exfoliate_chips` tool and flow, because the real hardware needs them, and the simulated devices carry them out; they do not change the flake count, the layer mix or whether the chip lifts (with the vacuum on it never does). A peel moves the roll so the tape leaves the chip at the requested angle. The angle is limited by the positioner travel, and the angle actually reached is recorded next to the requested one. A roll driven into the stage is a collision. The stage has a vacuum and a heater (with a fan). The tape roll runs out (3000 mm). | `bench.yaml exfoliator`, `SimTape`, `world.py tape_*` |
| **Finding and stacking stage** (`transfer_stage`) | One stage exposes coarse translation, fine x/y and in-plane rotation. The carried assembly rotates about a named translated pivot, retaining off-centre mounting. Commands are load-origin offsets; world coordinates remain table-relative. Z is mechanical height, fixed at zero in the normal synthetic travel configuration. Independent microscope focus leaves the sample still. Placement, height, tilt and repeatability remain synthetic. | `SimStage`, `world.py Surface`, `bench.yaml` |
| **Microscope** | Independent mechanical focus Z and a switchable objective. Capture freezes pose, objective, optical scene and calibration at exposure. The same projected raster supplies in-memory bytes and lazily saved images; unrelated reference photographs do not replace a moving field. Live previews are current scenes; recorded motion previews are immutable and distinct from verification captures. UI polling consumes no observation randomness. | `SimMicroscope`, `testbench/render.py`, `runtime/twin.py` |
| **Stamp holder** (`transfer_arm`) | XYZ translation carries the stamp under the objective. Spherical dome/surface geometry determines gap and geometric contact radius. Synthetic drift, slip and contact-front relaxation are distinct from apparent optical refraction. Refocusing moves the microscope rather than the holder or sample. The selected process makes one observed first-contact XY adjustment, then uses bounded Z-only descent and fixed-reference image differences. Newton rings do not certify stable alignment. | `SimManipulator`, `world.py`, `flows/library/stacking.py` |
| **Stacking physics** | PC/mica support profiles remain qualitative. The controller observes image coverage and wafer endpoints; physical transfer separately depends on contact history, material, temperature and injected failures. Film replacement is a cited human report; the simulator applies that act on subsequent grip. Bounded raster monitoring preserves its reference and motion intent in existing run metadata; unknown evidence cannot become success. | `world.py`, `records/stamps.py`, `perception/tracking.py` |
| **Cameras** | One overview camera over the whole table plus the microscope camera. | `SimCamera`, `SimLocator` |
| **Reactor** | The everyday integrated profile prepares hBN/reactive material on a human-reported metal/silicon carrier, then runs the qualitative reaction and final disposition through the third arm. Older prepared two-flake fixtures remain historical examples. | `reactor-model.md`, `material_metal.py`, reactor adapters |

**Safety interlocks between the stations** (`limits.yaml`, `safety/gate.py`):
- The exfoliator is locked while the left arm is low over its stage.
- The transfer stage is locked while the right arm is above it.
- A low stamp inhibits lateral sample travel and rotation. Swept mounted-sample and holder envelopes are checked, including pure rotation and intermediate crossings.
- The right arm stays out while the stamp is over the stage.
- The holder has a mechanical z floor; microscope focus has separate stroke and objective/sample/stamp clearances. Synthetic bounds authorize simulation only.
- Heaters have temperature and time limits, and a watchdog.
- Peel angles must stay between 10 and 90°.
- The left arm has a z floor over the exfoliation stage (0.6, i.e. at most 0.6 mm
  below first tape contact), so a press cannot crack the chip.

## 4. Error model

All values are in `config/testbench.yaml` (and the device-level ones in
`bench.yaml`). In the "source" column, *placeholder* means a guess; no value is
measured in this lab.

| what | value | source |
|---|---|---|
| placement error per axis / rotation | σ 0.25 mm / 1.0° | placeholder |
| big placement error ("askew") | 5% of placements, 2 mm / 6° | placeholder (v1 failure mode) |
| pick failure | 0 by default (fault: per pick or next N) | placeholder |
| overview detector noise per frame | σ 0.05 mm / 0.2° | placeholder |
| overview detector bias per object (same every frame until it moves) | σ 0.1 mm / 0.3° | placeholder |
| heavy tail on a detection | 10% of detections, σ 0.3 mm extra | v1 tracking log (`movement_history.jsonl`), meaning unconfirmed |
| missed object / phantom chip / bare-vs-exfoliated confusion | 2% / 0.3% per look / 2% | placeholder |
| transfer-stage repeatability | σ 2 µm on a long move; a jog keeps it | placeholder |
| stamp holder per axis | σ 0.5 µm | placeholder |
| chip height (focus) from chip to chip / tilt | σ 0.3 mm / 0.01 mm per mm | placeholder |
| depth of focus | 0.15 mm at 10x, scales with 10/objective | placeholder |
| flake layer class wrong by one | 25% at 10x, 3% at 100x; the same misreading every time for a given flake and objective | placeholder |
| flake area error | ±10%, the same every time for a given flake and objective | placeholder |
| exfoliation yield | mean `flakes_per_chip` 20 × pressed fraction; −15% per mother-tape use; independent of the recipe (recipe model removed 2026-11-03) | placeholder |
| a peel that leaves no flake | the chip stays bare (its material is not set) | model rule |
| good layer share | the classes inside `good_layers` (graphene 1-3 L, hBN 10-50 nm) are weighted by 0.3 + 0.7·q | placeholder |
| chip lifts with the tape | chips only (a glass slide held by the vacuum stays down); only a peel steeper or faster than the best by more than 0.75 widths; up to 50% far beyond (about 1.5% at v1's F500/30°, about 30% at 90°) | placeholder |
| stamp optical shift / Newton rings | 0.3 ± 0.03 mm, contrast ×0.85 / white-light gap-driven RGB fringe profile, finite 8 µm lookup support; spectral response uncalibrated | synthetic approximation; illumination owner-reported |
| layer numbers | graphene: 1 L 12%, 2 L 10%, 3 L 8%, 4-9 L 30%, thicker 40%; hBN mostly 10-50 nm | placeholder |
| stamp touch height / dome apex offset | 5.3 ± 0.06 mm / σ 0.08 mm | placeholder |
| pick-up chance (bare film) | hBN 0.9, Gr 0.6, WTe2 0.5; onto a stack 0.95; ×0.3 below 60 °C | placeholder |
| PC melt | 165 ± 2 °C | placeholder (v1 recipe ~170 °C) |
| crush depth | 0.4 mm past touch | placeholder |
| arm_left travel height | z 95 (clear of the parked movable roll at 78) | placeholder |
| motion | arms 80 mm/s + 0.3 s settle (v1 F5000); stage 10 mm/s; holder 2 mm/s | arms from v1 G-code, others placeholder |
| heaters | first order, τ 60 s heating, 40 s cooling with the fan (180 s without) | placeholder |

Component tests use `tests/conftest.py quiet_bench` to isolate selected errors;
that helper is not the complete ideal condition.

### Explicit ideal stacking condition

`qumus2.config.ideal_stacking_profile()` constructs a fresh integrated bench
with `simulation_condition="ideal_stacking_v1"`. Ordinary defaults are unchanged.
The current owner-directed baseline uses this condition before disturbance
validation; it is not a calibrated laboratory model or proof of robustness.

| Mechanism | Ideal setting |
| --- | --- |
| Placement and pick | Placement translation/rotation sigmas, large-error probability/magnitudes and ordinary pick-failure probability are zero. |
| Overview observations | Position/angle noise, fixed bias, heavy tails, misses, false positives and material confusion are zero. Random confidence becomes the original interval's midpoint. |
| Stage and holder | Stage and manipulator positioning sigmas are zero. Time/Z lateral drift, drift clip, contact slip and apparent lateral refraction are zero. |
| Sampled static offsets | Chip focus, stamp touch height, apex position, melt temperature and axial-optical-shift spreads are zero. Their nominal values remain. |
| Microscope observations | Layer-class errors, corner/contact/fringe measurement sigmas, residual source/stamp position errors, area errors, random partial-blur omissions and camera pixel grain are zero. |
| Eligible transfers | Known and fallback bare-film pickup, pickup onto a stack, and admitted PC/mica pickup/release probabilities are one. Full typed assumptions retain PC 90/170 C and mica 80/150 C with retained mica support. |

The residual observation switch is `microscope.observation_noise_scale=0`;
its absent/default value is one. Keep illumination falloff, specimen texture,
morphology, actual defocus/PSF, FOV/clipping and hard visibility requirements.
Keep spherical geometry, the circular evolving whole-outline contact front,
normal coordinate transforms, nominal heights, axial optical shift/focus tilt,
finite speeds, thermal lag, cold-pickup factor and safety limits. Explicit faults
remain injectable. Exfoliation and chemistry probabilities are separate laws;
the shared profile does not change chemistry success. A deterministic chemistry
test must declare its additional assumption separately.

Construct the condition before sampling a fresh world. Existing mechanical
checkpoint signatures reject cross-condition restoration before overlaying any
state; do not relabel old noisy checkpoints. Save the effective configuration,
seed and actual admitted per-job transfer assumptions. Those frozen assumptions
remain authoritative: a conflicting old job is not silently rewritten ideal.
Use the same explicit configuration for a fixture and its restored copies.

The existing offline material diagnostic accepts
`--simulation-condition ideal_stacking_v1`; its default remains `standard`.
`simulation-conditions.json` records the actual configuration, selected seed
and admitted transfer laws. Separate private `simulation-evaluation.json`
sidecars preserve the pure evaluator state at preparation and final-read
boundaries, including failures. They never drive the Agent, controls or public
export. Raw job statuses, unfinished work, capture races and observer failures
remain explicit; even `all_jobs_succeeded` does not certify experiment completion,
cold stages or laboratory success.

## 5. Fault catalogue

There are 22 faults. Tests, the UI's Testbench menu (testbench only) and the CLI
all inject them through one entry point, `FaultInjector.inject(name, params)`
(`testbench/faults.py`). "Expected" is what a correct system must do; it is the
same rule the chaos benchmark scores against (`testbench/chaos.py FAULT_CASES`).

| fault | what it does | expected behaviour | covered by |
|---|---|---|---|
| `next_placement` | The next object put down lands off by dx/dy/drot. | **recover**: the camera sees it, re-pick and re-place | test_realistic_bench, test_scripted_and_ui, chaos |
| `pick_fail_next` | The next N picks come up empty. | **recover**: the empty nozzle is seen, pick again | test_faults, test_agent_tools, chaos |
| `pick_fail_prob` | Each pick fails with probability p. | **either**: retries; many misses in a row end in a stop | test_testbench_fixes, chaos |
| `big_error_prob` | Each placement is off by a few mm with probability p. | **either**: each one is corrected; corrections used up → a stop, and the ledger says where the object really is | test_chaos, chaos |
| `drop_in_transit` | The carried object falls off the nozzle on the way. | **either**: found on the bench and moved again, or a stop | test_faults, chaos |
| `pickup_fail_next` | The next N stamp pick-ups leave the flake on the chip. | Selected normal process **holds** when source disappearance is not confirmed; no automatic repress. | test_stacking, chaos |
| `release_fail_next` | The next N drop-downs keep the stack on the stamp. | Selected normal process **holds** when new target appearance is not confirmed; no automatic heat escalation. | test_stacking, chaos |
| `device_offline` | A device stops answering. | **stop**, naming the device | test_faults, chaos |
| `camera_frame_drop` | The overview camera drops its next N frames. | **recover**: transient, look again | test_realistic_bench, chaos |
| `qr_unreadable` | A board's QR markers cannot be read for N looks. | **recover**: transient, look again | test_realistic_bench, chaos |
| `chip_removed` | Someone takes an object away without telling the system. | **stop**, saying it is missing | test_realistic_bench, chaos |
| `arm_step_loss` | An arm sits off by dx/dy until it homes. | **recover**: placements corrected; the same offset twice → home | test_realistic_bench, chaos |
| `arm_reset` | An arm's controller resets and must home. | **recover**: the gate refuses moves until the flow homes it | test_realistic_bench, chaos |
| `stamp_torn` | The stamp's PC film tears (now or on the next stamp gripped). | Selected normal process **holds** for human review and retains source/damage evidence. Partial transfer can be unknown even on the first pickup. | test_stacking, test_chaos, chaos |
| `tape_roll_empty` | The white tape roll is (nearly) empty. | **stop**, asking for a new roll | test_chaos, test_realistic_bench, chaos |
| `perception` | Raises the overview detector's miss, phantom, confusion and tail rates. | **either**: more looks and corrections, maybe a stop | test_realistic_bench, chaos |
| `serial_timeout` | The next N calls to a device time out. | **recover**: transient, retried with backoff | test_faults, chaos |
| `sensor_stuck` | A heater's reading freezes. | **either**: a heating step notices and stops everything (dangerous); a cooling step cannot tell | test_stacking, chaos |
| `vacuum_stuck` | A vacuum switch ignores commands. | **either**: the next check sees the object not held | test_stacking, chaos |
| `stuck_at` | General form of the two above. | as above | test_scripted_and_ui |
| `crash` | The job process dies before the Nth device call (kill -9) and restarts. | **recover**: resume from the step checkpoint, or park for a human when a step cannot be resumed safely | test_faults, test_stacking, test_lab_process, chaos |
| `hang` | The lab process freezes; the supervisor kills and restarts it. | **recover** after restart | test_lab_process (separate lab process only) |

What a fault already did stays done after `clear()`: a dropped chip stays where
it fell, a torn stamp stays torn and used tape stays used. Only the lost steps
are undone, because nothing could see them.

## 6. Tests and benchmarks

The offline tests and scripted/flow commands below make no model requests.
The separately described agent-mode comparison uses paid models.

| what | what it checks | how to run |
|---|---|---|
| **Unit and scenario tests** (`tests/`) | Each flow, step, correction, interlock, fault and recovery path, plus the UI API and the scripted agent scenarios. Run locally. GitHub is storage only for this phase; automatic workflow triggers are disabled and manual dispatch is not authorized. | `pytest` |
| **Stop / kill sweeps** (`test_stop_continue.py`, `test_faults.py`, `test_stacking.py`) | Stop or kill before selected device calls. Safe resumable operations preserve their outcome; interrupted unsafe strokes hold for review. No command or consumed one-shot admission may be replayed blindly. | part of `pytest` |
| **Chaos benchmark** (`qumus2 chaos`) | See below. | `python -m qumus2 chaos` (40 seeds by default; writes `runs/checks/chaos.md`) |
| **Not-verifiable report** (`qumus2 report`) | Runs every flow once and lists the steps whose result cannot be checked with the present sensors (the sensor wish-list, spec 4.2). | `python -m qumus2 report` |
| **v1 vs v2 comparison** (`v1compare/`) | Runs a copy of Qumus v1 on the same testbench, with the same faults and the same ground truth. Flows mode is free; agent mode uses real models and costs money. | see `v1compare/README.md` |
| **Scripted demo / recordings** | The whole stack with scripted agent decisions; recordings replay in the UI. | `python -m qumus2 serve` (scripted, free); recordings in History (free) |

### The chaos benchmark

Each seed runs the stacking task twice: once without faults (the baseline) and
once with them. The task is:
1. Exfoliate graphene onto one chip and hBN onto another, with the recipe the
   hands' tool submits by default (50 °C, 2 massage rounds, 0.2 mm press, F200,
   30°; `chaos.py RECIPE`). Since 2026-11-03 the recipe does not change the
   simulated yield; the task names it only to keep the process explicit.
2. Search both chips under the microscope.
3. Stack hBN/graphene/hBN onto the electrode chip.

This is a generic multilayer fault benchmark, not acceptance of the owner's
current prepatterned-electrode device. Its bottom hBN separates Gr from the
existing electrodes. The current device requires PC transfer of Gr directly
onto the designated contacts, with explicit region/alignment checks and no
unrequested hBN layer; see [current scope](workstreams.md).

The first fault of a run cycles through the 20 faults by seed, so every fault
gets the same number of runs; `--faults N` adds random ones. Each fault is
injected before a random device call within the baseline's call count. A fault
that only matters in one flow is injected during that flow, for example an
empty tape roll during exfoliation; such a fault that waits for its first chance
(the stamp pick-up or release failing) is armed as its flow starts, so it cannot
land after its last chance. Some faults pick their device at the moment
of injection: the device being called, the arm in use, or the board last looked at.

**Run outcome**, judged against the world:

| outcome | meaning |
|---|---|
| ok | The system says done, and the world agrees: every chip and carrier is where the ledger says, nothing is left on a nozzle, no safety violation, no collision. |
| honest_stop | The system stopped and said why, and its records still match the world (or are marked uncertain, or the stop message names the object). A `failed` job caused by a dangerous verdict or a hardware error counts here. |
| bad_stop | It stopped, but its records are wrong, or something collided. **A bug.** |
| false_ok | It says done, but the world disagrees. **A bug.** |
| error | An unexpected exception. **A bug.** |
| no_fault | No injected fault took effect, so the run is not counted. |

**Per injected fault:**
- *fired*: the armed failure was used up, or the faulty device was used afterwards, or (a vacuum stuck at the other state than it was switched to) it changed what is held at once.
- *handled*: the system did what the fault's expected behaviour says (table in section 5).
- *extra cost*: tape mm, stamps and sim minutes compared with the same seed without faults (for runs that ended ok).
- *collisions*: counted from the world's own record, not the safety layer's. This must be 0.

Historical run (60 seeds, 1 fault each, 2026-10-21): 46 ok, 14 honest_stop, 0
bad_stop, 0 false_ok, 0 error, 0 no_fault; every one of the 60 faults fired and
was handled; 0 collisions. The honest stops are the faults whose expected
behaviour is a stop (device_offline, chip_removed, tape_roll_empty,
sensor_stuck) and two of the three stamp_torn runs. These are counts on a
simplified model with placeholder probabilities, not lab success rates.

What was deliberately left out: step timing distributions, CPU time and
per-tool call counts. They say nothing about whether the control software is
right.

`hang` and `stuck_at` are not in the series: `hang` needs a separate lab
process (tests/test_lab_process.py covers it), and `stuck_at` is the general
form of `sensor_stuck` and `vacuum_stuck`.

The new checks have found real bugs. Two examples:
- A move that gave up left the ledger at the old place.
- A first stamp touch that could pick up the flake.

Both fixes are described in the archived architecture's "Testbench rework"
sections; this historical account is not a fresh test result.

## 7. Known simplifications

- **Images.** Microscope pictures are drawn from the world model. Perception is
  mixed: synthetic object/material analyzers read truth and add noise, while
  autofocus, source registration and difference-based pickup use raster pixels.
  The real YOLO weights and segmenter are not in the repository.
- **Physics.** Contact, pick-up and release are probability models, not
  mechanics. Tape tension and adhesion, film wrinkles, bubbles and
  temperature gradients are not modelled.
- **Exfoliation.** Flake positions, sizes and thicknesses are drawn from
  distributions. The renderer draws illustrative polygonal outlines; it is
  not a calibrated model of exfoliation morphology or optical measurements.
- **Arms.** Arms move in straight lines with no dynamics. Collision checks are
  the station interlocks, the world's stamp and roll collisions, and the left
  arm's nozzle path against the two exfoliator rolls (sampled every mm). The arm
  links are not modelled, only the nozzle.
- **Exfoliation recipe.** Temperature, peel speed and angle, press depth and
  massage rounds do not affect the simulated outcome (the best-recipe model was
  removed on 2026-11-03); there is no tape mechanics. They remain tool and flow
  parameters for the real hardware. The flow accepts `dwell_s` as a waiting-time parameter;
  it does not establish a measured dwell-to-yield relationship. A heated recipe peels
  twice (the lift before heating, with the vacuum on, also exfoliates); this is
  how the flow behaves, not a model choice.
- **Placeholders.** Every number in section 4 is a placeholder until it is
  measured in the lab.
- **No sim-to-real switch yet.** The real device drivers (`devices/real/`)
  belong to roadmap stage 3.
- **Scripted mode.** The scripted mode replays agent decisions from scenario
  files. It tests the tools and flows, not model decision-making.
