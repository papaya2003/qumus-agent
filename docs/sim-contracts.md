# Architecture and contracts

This document describes the current system boundaries and contracts, organized
by responsibility. It is not a release acceptance report. Implementation status,
ownership, execution limits and validation gates are in
[workstreams.md](workstreams.md); changes to these contracts require matching
code and relevant local checks. Historical implementation notes explain prior
choices but do not override the current contracts or known hardware limits.

## System boundaries

```text
Browser UI ──HTTP/WS──▶ ui/app.py ──reads──▶ RunStore (SQLite)
                            │ instructions       ▲ events/state
                            ▼                    │
                     agents/driver.py → AgentRuntime (scripted | OpenAI SDK)
                                             │ every tool call
                                             ▼
                     ToolGateway: policy → tool → event/instruction handling
                                             │ hardware jobs
                                             ▼
                     JobService → FlowExecutor → StepRunner
                                             │ every device command
                                             ▼
                     SafetyGate → device interfaces → simulated / real adapters
```

RunStore owns application records and event history; the UI presents these
records instead of creating a second execution or laboratory state store.
Hardware tools submit jobs; agents never command devices outside SafetyGate.
The JobService supports in-process operation and a supervised laboratory process
(`serve --lab-process`) with restart recovery. Flows and runtime use the provided
`Clock`. Process recovery does not certify real hardware recovery.

An owned in-process service has an explicit disposal lifecycle. `shutdown`
stops admission, uses normal Stop and safety cleanup, and joins workers within
one wall-clock deadline. Unsettled execution or unconfirmed energy shutdown
retains the store and image owners; disposal cannot acknowledge a human hold or
turn it into success. `Services.close_local` releases resources only after this
settlement. Manual executors without managed workers remain explicitly unknown.
Local recovery excludes jobs still owned by active or queued workers; a crash
callback arriving during safety cleanup is deferred until the actual owner exits.
Run-specific recovery and human review also refuse that still-owned interval;
`done` remains false while cleanup is active even if a terminal status has already
been published. Explicit Continue on an ordinary running job retains its existing
same-executor semantics.
The supervised laboratory retains its existing supervisor lifecycle.

Candidate revisits retain an ordered input binding, completed original image and
recognition results, and the current candidate's public entry optical readbacks
in run metadata. Append-only same-job `microscope.revisit_checkpoint` receipts
bind each metadata state. Continue retains completed evidence and reacquires only
the incomplete candidate from its entry anchor through SafetyGate; autofocus,
observation noise and Stop boundaries are unchanged. All-lost give-up and a newly
issued correction permit fresh acquisition, while transient retries of the same
correction retain progress. Sample publication and the published checkpoint are
one existing store transaction; a replay validates its own published outputs
without reobserving. Missing progressed historical journals and changed inputs
are refused rather than treated as fresh observations.

The active repository is `/Users/jialei/Projects/Qumus/v2`. Everyday startup uses
`../Start Qumus.command` and `scripts/start_qumus.py`; History contains the Demo
catalog and each recording's Experience. Modern dependencies use `.venv`;
frozen v1 dependencies use `.venv-v1compare`. Preserve `v1compare/v1/` against its
manifest. Experimental full-page Settings and specialist model overrides remain
in the separate `codex/agent-model-settings` checkout, outside this main runtime.
Do not add another launcher, review site, scheduler or settings store.

### Contract index

The C1–C13 identifiers remain stable for existing documentation and ownership.
Paths in the index are relative to `qumus2/`. Detailed parameter constraints live
in typed schemas and configuration; the sections below describe cross-module
invariants.

The normal stacking process is being migrated to the owner's selected
[version 1 source sequence](stacking-process-v1.md). The previous continuous
dual-plane/XY-servo implementation remains recoverable in Git and private run
history. Its contracts do not govern the revised normal process. Migration
retains the shared safety, state, clock and recording boundaries and retires
superseded active paths after checking their callers.

An opt-in diagnostic profile provides an explicit ideal stacking baseline, with
positioning/observation perturbations and lateral drift/slip disabled through
one shared configuration. It retains normal geometry, contact, transforms,
thermal/safety rules and strict target overlap. Saved noisy worlds are not ideal
inputs. Historical acceptance restrictions do not replace the current Hall-bar
requirements or certify normal-condition execution. See the
[ideal baseline contract](stacking-process-v1.md#ideal-baseline-before-disturbance-validation)
for the retained diagnostic configuration.

| ID | Responsibility and public boundary | Implementation |
| --- | --- | --- |
| C1 | Device interfaces and bundles; mm, degrees, degrees C, seconds; arms command in their own frame | `devices/base.py` |
| C2 | Guarded device access, limits, interlocks, watchdog and `safety.violation` events | `safety/`, `config/limits.yaml` |
| C3 | Checked Step lifecycle, observations, correction ladder and checkpoints | `flows/step.py` |
| C4 | Registered Flow, typed Params, submission resolution, deterministic Steps and optional composition/replanning | `flows/flow.py`, `flows/workflow.py` |
| C5 | Job submission, waiting, cancellation, Stop/Continue, retry, reverify and recovery | `runtime/jobs.py`, `runtime/recovery.py` |
| C6 | Projects, plan revisions, runs, steps, instructions, device locks, samples, experience, metadata and transactions | `runtime/store.py` |
| C7 | Durable event kinds and payloads; additive fields, no historical renames | `runtime/events.py` and event producers |
| C8 | `@qumus_tool`, typed arguments, `ToolContext`, `ToolResult(status, summary, data, images)` | `agents/tooling.py`, `agents/tools/` |
| C9 | Permissions, schema/references, cross-tool policy and delegation checks | `agents/policy.py`, `agents/gateway.py` |
| C10 | `orchestrator`, `advisor`, `eyes`, `hands`, `picker`, `designer`, enabled-bench `reactor` | `agents/roster.py` |
| C11 | `AgentRuntime`: `mode`, `busy`, `send`, `interrupt`, `continue_turn`, `close` | `agents/runtime.py`, `agents/openai_runtime.py` |
| C12 | Live HTTP/WebSocket API and read-only replay projections | `ui/app.py`, `ui/static/` |
| C13 | Object location and microscope analysis from captured Frames | `perception/base.py` |

## Device execution, safety and recovery

Laboratory mode is a backend capability, not a relabeling of a run. The current
composition wires SimLab or its RemoteLab proxy and reports Virtual with
learning disabled. Settings exposes Real as unavailable with the required
physical-adapter setup; switching during active turns/jobs is refused. Replay
retains its original provenance and cannot switch modes.

Experimental learning retains the existing ExperienceBook lifecycle. A run must
have matching persisted real-store/run origin and a strict `laboratory.origin`
receipt immediately following its original `job.submitted`, linked by event ID.
JobService emits this receipt from its actual guarded device bundle in the same
transaction; it never infers physical provenance from a caller parameter. No
physical receipt producer is currently wired. Missing, late, conflicting or
simulated provenance is ineligible. Legacy votes remain historical but cannot
count toward new confirmation thresholds without qualified evidence. Test
sandbox lessons and votes cannot change laboratory entries or summary baselines.

SafetyGate checks every moving/heating device against registered limits and
refuses missing limits, unhomed arm motion, out-of-reach targets and invalid peel
angles. Arm limits, floor regions and interlocks use each arm's frame. Stamp
interlocks relate the holder's x/y region and holder-to-stage z gap. The ordinary
sample stage exposes coarse translation, fine x/y and in-plane rotation through
one MotionStage. Microscope focus is an independent mechanical Z; it does not
move the sample or alter that gap. Swept sample/stamp envelopes, focus clearance
and synthetic-only calibration bounds are checked by the same gate. Guarded commands
can append `lab.frame` snapshots through the frame provider. `Camera.preview()`
and `Microscope.preview()` supply UI pictures, defaulting to unavailable; they
are not experimental commands or new verification evidence.

Manipulator command records retain their precommand public axis readings and
the adapter's declared duration bound, including its configured default speed.
An unknown bound remains unknown. Terminal release transport binds its saved
receipt to that actual command and the corresponding ordered encoder frames;
mutable phase values cannot enlarge the bound or replace the recorded poses.
This adds provenance to the existing command history without changing motion,
timing, physical limits or the authority of a public encoder reading.

A Step is a checked checkpoint, not one atomic command. Its lifecycle is
`preconditions → act → verify → correct`, with `safe_state`, `probe_state`,
`on_success` and `on_give_up` hooks. `Observation` records verdict, measurements,
expected values and saved images; unobservable state is explicitly not verifiable.
`on_give_up` runs before `safe_state` when observation/corrections cannot establish
success. `StepRunner` owns bounded correction and checkpointing. `pause_inside` defaults
to `safe_to_resume`; unsafe thermal/release Steps retain their special recovery
rules. Successful messages and operation labels cannot replace observations.

L0 Step corrections and bounded L1 Hands recovery are distinct. A parked job
may use the existing `reverify_job` or `retry_job` through ToolGateway only while
its per-job/project recovery budget permits; safety failures remain human-only.
For an off-position placement whose ledger already says the destination,
MoveObject requires this same job's latest failed Step, its correctable public
pose and unchanged last-seen evidence, then a fresh unique camera observation.
A completed, stale, uncertain or unrelated placement cannot grant another move.

A parent invoking a child Step directly must preserve a rejected child
observation, delegate its existing failure reconciliation before propagating the
typed error, and retain the structured observation in that error's details.
A handoff count establishes observed stamp presence, not the selected stamp's
identity. Correct only the selected stamp's location from a current, image-bound,
unambiguous sighting tied either to readable identity or to the same run's
verified exclusive placement and uninterrupted public history. Missing, stale or
conflicting evidence retains explicit location uncertainty; it cannot relocate
other stamp records. Unverified grip and holder encoder motion do not establish
attachment. Reconciliation uses the existing Store and already taken observation
without new motion, retry or capture.

A Flow resolves Params once at submission against the composed bench, stores
resolved values, and deterministically builds its ordered Steps. Optional
`build_workflow` exposes Assembly/Molecule composition without another scheduler.
Dry-transfer molecules are reused by material-metal preparation; reactor loading,
unloading, observation and readback share reactor operations. New runs persist
`run.meta.workflow` and `job.workflow` (`submitted` or `replanned`), with stable
molecular IDs, inclusive zero-based Step ranges, descriptions and unsafe-resume
flags. Local/remote job status reads that saved manifest; compact tool replies
show names/levels/ranges, while `full=true` preserves leaves. Legacy jobs without
a manifest retain unknown hierarchy. See [workflow hierarchy](vdw-workflow-hierarchy.md).

Flow-local `on_step_failed` may return a Replan only when the new Params preserve
the Step-name prefix through the failed Step and extend the remaining sequence.
`set_run_params` records `job.params_changed`; a deliberately superseded failed
Step is `skipped` and counts as finished for resume/recovery. Historical
torn-stamp replans remain readable. The selected normal process holds observed
film damage for human review because partial transfer is unknown; it does not
automatically replace the stamp or repeat a pickup.
The checked replan commits JSON-compatible resolved parameters, workflow and
the skipped checkpoint in the existing transaction. Only after that commit does
it update `params` on the same live FlowContext, before the next Step runs.
Clock, gate, cancellation and histories remain attached to that context.
`set_run_params` returns its existing `job.params_changed` event ID; the additive
`params_event_id` on `job.workflow` with reason `replanned` links the checked
workflow to that exact parameter snapshot. Failed persistence cannot publish
new live parameters.

Jobs can be `queued`, `running`, `paused`, `waiting_human`, `parked`, `succeeded`,
`failed` or `cancelled`. Stop pauses rather than cancelling. Retry starts at the
first unfinished Step. Ordinary reverify runs its verification without motion;
only an OK observation can finish that Step and allow subsequent execution.
An operator may review a held job from its live Results record. The dedicated
UI/API action binds the current waiting event and failed checkpoint, checks the
saved workflow, safe-resume rules, motion state and disabled energy outputs,
and atomically appends `job.human_reviewed` with a transition to `parked` only.
Stale or unsafe reviews fail without changing the hold. The action is absent
from agent tools and replay; it does not enqueue work, certify material/process
outcomes, restore heat or reset recovery counts. Subsequent bounded recovery is
a separate action with normal fresh-evidence and SafetyGate checks. Continue
does not acknowledge a human-required hold.
Before execution, heater restoration, probes or reverify, the runtime compares
nonempty saved structural bindings with the current composition and Steps.
A mismatch emits `job.workflow_rejected`, preserves the original manifest and
checkpoints, and parks for human review. It does not invoke the current Step's
motion or holding callbacks. This detects structural changes, not changes to
the bodies of identically named Steps.
When optional replanning is refused after an observed failed Step, the job
retains that original process error, observations and result while recording the
binding refusal separately. Trusted shutdown, lock retention and later
continuation checks still apply; preserving an outcome grants no execution.

Failure shutdown uses trusted registered Heater/GasFlowController interfaces.
It attempts all relevant outputs and confirms disabled targets, even when one
attempt fails. A Flow's `shutdown_devices` distinguishes devices it may change
from devices it only locks for observation; the read-only reactor verification
flow has an empty shutdown scope. Recovery verifies energy shutdown across the
whole Flow scope, including heating started before the interrupted Step.
Unconfirmed outputs are recorded as `safe_shutdown_failed` with
danger diagnostics and retained device locks. Retry, Continue and process restart
must not release those locks or restore targets while shutdown remains unconfirmed.
Explicit reverify in this state verifies energy shutdown only: it cannot mark a
process Step successful. Confirmed shutdown may release the locks, while the
original unfinished failure remains for human recovery. Holding controls and
carrier motion are not inferred from energy-off state. Shutdown does not remove
stored heat or certify a cold experiment.

On an abrupt worker exit, a resumable Flow preserves readable heater targets in
the existing `held_heaters` metadata before shutdown. Restart recovery uses the
same checked resume path as Continue. Unconfirmed shutdown and workflow mismatch
still block restoration; final cooling and read-only reactor verification never
restore heating targets. An unreadable old target is not invented.

### Final experiment cooling

Final experiment completion is distinct from hot intermediate process completion.
`save_project_status(done)` checks physical plan/job/material/reactor obligations
and outstanding work, then submits `cool_experiment_stages` through the existing
JobService and returns pending. Established experiment requirements and heated
process history prevent bypass by changing the task label. The orchestrator waits
and requests completion again; a successful cooling job alone does not save done.

Both `exfoliation_heater` and `stamp_heater` must have finite measured temperatures
**strictly below 40 C**, targets off, and their fan commands confirmed off after
cooling. The Step attempts both heater shutdowns, enables `exfoliation_fan` and
`transfer_stage_fan` through Switch interfaces, and records temperatures, targets,
fan commands and a persisted bounded deadline in `experiment.cooling`. It attempts
both final fan shutdowns even if one fails. Missing interfaces, invalid/stalled
feedback, unconfirmed shutdown and timeout cannot establish completion.

The lab-side completion method reacquires all four thermal device locks, rechecks
original obligations and fresh readings, emits `experiment.stages_verified`, and
writes project completion within a checked store transaction. Occupied devices,
changed obligations or invalid feedback prevent completion; newly hot stages need
another cooldown. A failed/cancelled/parked/waiting-human cooling job retains its
ID and needs-attention result instead of silently starting a replacement.

Stop/restart uses normal recovery and the original deadline. This shutdown flow
never restores prior heater targets, including a stop before its first actuation.
Intermediate peel/transfer handling thresholds remain separate; transfer cooldown
now uses its fan without changing its 60 C handling threshold. The reactor retains
its own access/final-disposition requirements. The historical 56.7 C final value
was the reactor, not either ordinary stage; old recordings are not evidence of
the new two-stage completion check.

Simulated heat follows the existing exponential thermal model. Fan transitions
integrate the preceding interval before changing airflow; stuck actuator state
affects physical cooling. Legacy transfer-heater checkpoints retain heat/time
while gaining the configured fan capability. Switch feedback is a command state,
not measured airflow; temperature readings remain necessary. Synthetic rates are
not laboratory calibration, and one plausible falsely low sensor reading cannot
prove its own accuracy. Process exit does not run an unattended cooling experiment.
See [cooling diagnosis, assumptions and evidence](stage-cooling-20261028.md).

## Records, plans and completion evidence

RunStore is shared by simulated and real benches with explicit bench identity.
Sample upserts merge properties and append bounded `sample.updated` payloads;
removal emits `sample.removed`. Producer fields are additive. Saved events,
images, capture metadata, failed attempts and original sessions remain immutable.
Job completion, material acceptance and the model's final answer are separate.

### Laboratory facts and source evidence

`records/lab_geometry.py` validates immutable `lab.evidence` assertions in the
existing event table. Admitted inspection and human material/support/film
reports append source-linked assertions; they retain their original admission,
instruction, reservation and sample checks. Assertions name bench, subject,
property, value, units/frame, source, observation time/timebase, calibration and
uncertainty. Unknowns remain explicit. Historical constants, synthetic state,
operator reports, estimates and measurements retain distinct authority.

The current view reduces a specified event prefix in pages, retains conflicts
and explicit supersession, and bounds returned properties/assertions with
omission counts. It does not use a capped first page as a complete history.
`get_instrument_status` exposes this stored evidence alongside existing readings;
reading it neither refreshes observations nor establishes readiness. No separate
twin database or motion authority is introduced. Private recovered v1 facts can
be initialized through this same validated API; no private lab constants are
embedded in the shipped simulation configuration.

The same full prefix also constructs `observed_scene`, a pure scene projection
embedded in the existing recorded bench snapshots. Current world-pose camera
assertions form unidentified markers; admitted human reports supply named
attributes. Identity and position are joined only when the assertions establish
that binding. Ledger IDs and simulator object positions cannot fill observation
gaps. Conflicts, stale observations, missing frames and calibration remain
visible as unresolved facts. Freshness is evaluated at the saved prefix time
and is a display policy, not a readiness decision or a fresh measurement.
RunStore atomically saves a companion `event_clock` row for explicitly declared
writer clock bases without changing event envelopes or assertion payloads.
UTC writers share one comparable basis; every independent SimClock has its own
epoch in the simulation domain. The prefix's latest saved reading per domain
selects its current epoch, so parent UTC events cannot age simulation evidence
and a restarted simulation cannot make old observations current. Store-clock
assertions use their saved writer basis; tagged UTC assertions require a saved
UTC reading. Untagged historical rows and undeclared custom clocks have unknown
freshness, with no timestamp-magnitude inference or backfill. Projection reads
only the selected prefix and never calls Clock.now or refreshes an observation.
Stored instrument frames share this prefix clock lookup. Their elapsed age uses
the reader's current Clock only when its basis is comparable and still active;
mixed-clock readers, retired epochs, untagged frames and future timestamps have
unknown age. Even a known age does not establish fresh sensing or readiness.

In virtual mode, Lab's Bench overview defaults to the same simulation drawing
and geometry used by Progress Top View. Physical dimensions use one uniform
scale; chips and tables are not independently enlarged. Observed evidence is
an explicitly selected alternative and remains the default for unknown or
non-simulated origins. Recovered historical coordinates
remain viewable in their original arm frames with unknown age/calibration;
inconsistent transforms cannot silently merge them into world coordinates.
The projection reduces the full history before bounding displayed items and
discloses omissions. Replay excludes a scene whose evidence boundary is newer
than the selected prefix. Observations do not overwrite the simulator's hidden
physical state or introduce a second authoritative state store.

Problem history resolves a supervisor restart warning only with a successful
typed receipt carrying the same restart ID. Exhausted recovery clears command
readiness and remains an active failure. A model error is recorded once through
its verified event ID; a later user-requested successful turn may mark that
same session/project error as historical, preserving the original evidence.
It does not resolve laboratory failures or unrelated sessions.

### Atomic Task Plans and physical obligations

Task Plan writes use `BEGIN IMMEDIATE` before reading and atomically commit
snapshot, revision and `plan.updated`/`plan.step` events. Unique
`(project_id, revision)` prevents duplicate revisions; ambiguous legacy duplicates
are refused rather than deleted. `project.plan_version` and event `version`
advance on structural and progress changes. Existing-plan `update_plan` requires
`expected_version`; only initial creation can omit it. Stale edits fail and progress
merges inside the transaction. Low-level callers may serialize fresh writes without
a version, but edits of an already-read snapshot must supply one.

Typed `sample_outcomes` combine sample ID, status, exact location and/or storage
conditions with execution evidence. Missing/uncertain samples cannot pass.
`in_storage` follows a flake/device's current location to a known carrier chip and
then a configured `holds: chip` slot; both locations must be certain. Source
provenance, name prefixes, wrong-kind carriers, cycles, stages, arms and reactor
ports cannot substitute for storage. Direct chip/stamp checks and explicit
status/location requirements remain independent.

Delegation captures conditions before execution, preventing concurrent edits from
erasing active obligations. Cross-step completion uses the same checks. New physical
`done` declarations through `update_plan` are refused; job/delegation checks establish
completion. Changed conditions cannot inherit done. An unchanged previously completed
Step retains its historical completion after later processing advances a sample;
resumed unfinished Steps must meet current admission and outcome contracts.
Hands requires a named Step with sample conditions before delegation. Reactor also
requires typed final process disposition with fresh measured project/chip-bound
evidence. Legacy unfinished plans must be updated before new hardware work.
These conditions are ledger/evidence checks, not additional sensing.

Unfinished obligations cannot disappear through deletion, renaming, ownership,
skipped labels or weakened conditions. Cited human `physical_changes` may cancel
or supersede work; the old Step keeps its skipped record, instruction and replacement
IDs, and replacements stay pending. Retargeting uses ordinary sample names without
requiring people to supply internal Step IDs. Future condition-free drafts can
substitute one known same-kind sample in an otherwise unchanged title only with
unchanged owner/operation and a direct instruction authorizing those targets.
This does not supply missing delegation conditions. Version conflicts are reported
first; malformed/omitted companion changes return repair structure for the existing
instruction rather than demanding repeated human authorization.

`agent.job_linked` binds distinct run IDs to project/plan Steps. Unresolved linked
jobs survive further delegations and gateway restarts; another successful run of
the same flow cannot erase one. Observations/recovery of one run converge on that
run's current state. Completion also checks outstanding linked work. Safe cancellation
is cancellation, not success; held reservations or uncertain sample/reactor state
prevent retirement.

### Flow outputs and narrow legacy repairs

Known flow output contracts are checked before plan save. Legacy pair preparation
leaves a `reactor_pair_prepared` carrier; modern preparation leaves
`material_metal_prepared`; both leave input flakes `stacked`. Reaction leaves the
carrier `reactor_processed`. Cross-role status repairs retain operation, target
and all other obligations, record their authority and remain pending; submitted
work additionally needs matching successful preparation evidence.

Exfoliation assigns `exfoliated` to its receiving chip, never its raw-material
carrier. New plans reject that role mismatch. An unsubmitted model-authored
legacy mistake may remove only that impossible status while retaining the same
carrier's storage and other obligations. After submission, replacing the mistaken
carrier identity requires the exact step's unique successful single-chip job,
matching recipe/source/material and recorded storage verification. Explicit human
targets, ambiguous outputs, unknown arguments and altered obligations are refused.
The correction records its authority and stays pending until Hands checks the
original job and current outcomes; another job cannot certify it. Unchanged legacy
drafts may coexist with repairs, but cannot execute until their own checks pass.

Carrier status `stored` does not exist. A legacy declaration may be removed only
for known return-to-storage operations when the same condition already required
`in_storage: true`; Step, owner, operation, carrier and remaining obligations stay.
Submitted work needs its matching successful linked job. This does not remove a
real status requirement or rewrite historical completion.

Established material-metal requirements reject legacy pair preparation/reaction
at save and delegation. A model-authored wrong preparation operation/status can
be repaired only against original human requirements and immutable plan history,
retaining Step, carrier, materials and obligations. Submitted work needs one exact
same-project successful preparation job bound to current requirements, sources,
support and optical receipt, with no uncertain/reserved state. The original job
must still be explicitly read and normal completion checked. Semantic arguments
such as image retention need positive evidence. Ambiguous jobs, human requirements,
unknown arguments and abstract receipt fixtures cannot be reinterpreted as successful
device preparation.

`get_lab_setup.flow_storage_contracts` exposes registered destinations.
Exfoliation reserves free middle-storage slots at submission. Search/revisit capture
the actual current location as `home_slot` after preceding work; an earlier left
slot is not their future home. Occupied destinations may select another free chip
slot in that station and persist the change. These flows do not guarantee a
caller-selected exact slot. New/changed plans and strict legacy delegation reject
unsupported exact locations; drafts with unknown operation cannot bind a route
prematurely. Generic goals use status plus `in_storage`. Explicit human fixed-slot
goals stay requirements and receive an unsupported-route explanation rather than
automatic weakening. Admission never removes saved conditions or completes history.

`verify_reactor_state(chip_id)` reads heater, gas and insertion state for a carrier
already in dedicated storage, without motion or target changes. Evidence binds
the successful job, project and chip; later control invalidates it. Fresh cold-storage
evidence cannot certify a failed or interrupted reaction.

### Binding requirements and recovery evidence

Material-metal plan arguments use one closed typed contract at plan save,
delegation, submission and completion. Explicit aliases retain raw fields and
must agree; unknown/conflicting arguments are refused. Submitted carrier, flake,
stamp and process values must match the captured delegation. Completion checks
the exact executed job and its preparation receipt, including support and source
images. Reactor provenance refers to its frozen preparation, not invented source
fields on a reaction job. Malformed legacy plans remain pending without turning
a successful physical job into a sensor failure.

Optional flake identities may be null while sources are unbound. Refinement can
fill only these placeholders in the two modern material-metal operations; existing
non-null IDs, materials, carrier/support/process values and completion conditions
stay protected. Other null values, such as heater targets, are not reinterpreted.

Readiness distinguishes `next_pending_step` executability from `all_steps_ready`
and whole-plan `readiness_errors`. A ready exfoliation may start while a future
search awaits its actual output chip. Failed proposals return the unchanged
plan's `current_execution`. These are planning diagnostics; the gateway still
checks the named Step, conditions, recipe and real execution state.

Plan refinement inserts before/after stable Step IDs in one transaction, allocating
never-used IDs and preserving ordering, progress and original obligations.
Placement arguments do not become persisted Step data. Invalid anchors, duplicate
IDs and incorrect role/layer order fail without partial writes. Appending and
then reordering is not equivalent when the intermediate plan violates prerequisites.

Search completion binds the receiving chip or catalogued flakes, never the raw
material carrier. A mistaken carrier condition can be repaired only using a unique
successful same-project exfoliation output, matching material/provenance and
original search evidence. Ambiguity, human target conflicts and weakened conditions
are refused. Repair leaves completion pending until the original job and current
outcomes pass; it does not repeat search.

Unknown job IDs return same-project/current-delegation `known_jobs` evidence,
not fuzzy matches or newly linked execution. Delegated context carries exact
linked IDs and current plan revision/version. Displayed recovery is read-only:
a project-completion refusal needs later checked completion of that same project;
a search prerequisite refusal needs that Step's admitted delegation. A truncated
lookup needs an explicit read of the uniquely linked original job and checked
completion. Unrelated success and model claims cannot resolve it.

## Inventory, perception and material geometry

### Laboratory observations and source identity

ObjectLocator locates detections in explicit regions/frames; microscope analysis
covers sharpness, chip/corner/flake/stamp detection, contact-film integrity and
Newton-ring gap estimates. Physical positions use mm and degrees. Overview
inspection reports observed and unobserved regions separately: chamber, port and
storage are distinct, and an omitted region is not empty. Detections establish
object kinds/occupied positions; the ledger supplies reconciled sample identity.

Corner acquisition probes symmetric four-direction rings about its fixed
observed nominal point, growing the radius only after a complete ring. Existing
correction counts, stage limits and verification tolerances remain unchanged.
Microscope rasterization intersects the finite rotated chip footprint with the
field of view, including an edge crossing with no visible vertex. The substrate,
texture and edge clip to that footprint; truly disjoint fields remain blank.
The additive `chip_surface` photometry identity invalidates older optical
references. Stored recording pixels are retained; re-rendering affected partial
chip packets intentionally changes, while unaffected historical raster cases
remain stable.

Mother-tape `material_reviews` are append-only and propagated into sample events.
The live Lab review endpoint requires the expected current material, a named
person, confirmed/uncertain status, an unreserved carrier in left storage and an
idle runtime. It records identity evidence without moving or visually identifying
material. Old labels remain unreviewed; changed labels invalidate old confirmation.
A chat source report must cite the person's recorded instruction naming the
carrier and material. It records that explicit report rather than allowing a
model to invent human confirmation; already produced flakes retain their provenance.
This is the local application's operator trust model, not multi-user authentication.

`record_stamp_support` and `record_material_source` require a cited human report
for the stored, unreserved object. PC/mica identity alone cannot refresh a used
or torn stamp. The existing `report_new_film` records an explicit PC/mica replacement;
the Lab UI retains its PC default. In simulation, a cited material-source
replacement represents that external act, without rewriting already produced chips.

`record_metal_coating` requires a metal and positive finite `thickness_nm` consistent
with the cited person's numerical thickness and units (nm or um, including Chinese
unit names). Preparation references and preparer are optional; supplied values must
be cited. Otherwise the instruction is the receipt reference and `user_report` is
the source. Existing coating history is not silently replaced.

An uncertain review prevents source use; conflicting identity cannot qualify for
the simulation exception. Fresh simulated carriers have explicit `simulation_identity` declarations bound to sample ID, kind, material
and configuration source. `list_samples` reports whether the declaration is usable
only on `bench=sim`; it never converts `human_review` to confirmed. On restore,
identities are revalidated against saved world objects in separate metadata;
missing/conflicting objects override stale declarations, and fresh benches clear
the restore map. Labels/default IDs alone are insufficient. Simulation identity
does not establish readiness or quality, and has no real-hardware exception.

### Search and reusable optical evidence

Revisit priority is an observation decision, not source admission. For device
tasks, public 10X observations and the registered contact geometry guide which
flakes receive 20X/100X inspection, including potential hBN/Gr compatibility.
Neither largest-first nor smallest-first is a device-fit criterion. Coarse
geometry screens are advisory: missing geometry or a failed bounded pose screen
does not prove incompatibility and must not erase a candidate from the pending
inspection inventory. Explicit flake IDs retain their normal targeted-revisit
path. Final selection still requires the original 20X mask, 100X thickness and
surface evidence, current pickup inspection and the full coverage checks.

The Picker separates pending optical evidence from admitted candidates. A
role-relevant 10X-only flake must remain visible before pickup admission is
evaluated; lack of a pickup inspection cannot establish exhausted stock.
Existing missing-image repair and design-failure feedback govern targeted
revisits before additional exfoliation. Observed incompatibility, failed
revisits and user exclusions remain separate from missing evidence.

The owner's later 2 November decision restores v1's original20X-mask design
route. Normal RGB revisits publish the saved selected20X mask as geometry;
100X captures supply classification and surface evidence only. The independently
developed RGB boundary refinement and zero-uncertain-pixel requirement no longer
gate this route. Historical native100 derived measurements remain historical
evidence, not relabelled20X sources.

The existing exact pixel-cell adapter preserves the selected mask's components
and holes and transforms its centroid-relative region by observed chip rotation.
This is deliberately not a verbatim copy of v1's external-only simplified contour
export, which omits holes. The original mask is immutable; the adapter does not
fill holes, inflate contours or infer a sharper physical edge. Actual per-capture
calibration is retained (the native20X profile declares0.182um/px); the donor
Designer's hardcoded0.183um/px does not override an exposure's declared pitch.
Public stage readbacks bind the20X centroid, and100X recentering cannot replace
that source origin. The owner-inclusive1um/1degree engineering allowance is
applied once, with full-electrode and whole-Gr coverage unchanged. This policy
is not a measured real-camera boundary accuracy guarantee.

Native captures declare `material_reference_selection` as
`v1-ordinary-objective-references-v2`: thin graphene at 20X uses the ordinary
low-magnification donor anchors, while 100X retains the high table. The donor's
centered 20X recovery classifier is not a forward camera calibration. The
historical reference-selection helper remains unchanged. Native capture profile
`v1-reference-material-capture-v2` uses RGB8 (164, 98, 103), derived from the
frozen hBN donor background, for both substrate fill and contrast normalization.
Native substrate support uses the same background. This avoids the demonstrated
20–30 nm hBN high-magnification gamut clipping against the legacy purple
substrate; it does not establish universal material or camera calibration.
Ordinary survey and legacy neighbourhood frames retain their original substrate.
The renderer photometry marker declares native-domain capability; each capture's
calibration determines whether that domain applies.

This explicit domain revision changes the optical dependency fingerprint.
Historical images, masks and receipts remain immutable evidence, but cannot be
silently relabelled or rehashed into current source admission. Existing candidates
require fresh normal native acquisition and measurement before current design or
pickup. Old capture-profile identifiers are rejected. Recognition tables,
thresholds and boundary algorithms are not changed by this camera correction.

Chip autofocus in this native domain scores the mean gradient energy of the
three observed RGB channels (`raster-focus-rgb-v1`). Equal-luminance interference
colours can lose their optical edge signal when converted to grey. The crop,
Gaussian filter, noise deadband, sweep and peak/plateau policy are unchanged;
ordinary and stamp-plane autofocus retain the grey metric. Focus is selected
from captured pixels and public axis readbacks, never simulator best-focus data.

Owner-directed local pickup inspection uses three 20X `analysis_capture` frames
through SafetyGate. `pickup.local_clearance_mm` defaults to a provisional 0.020 mm
edge-to-edge gap around the target's observed region, not the stamp footprint or
the former 0.85 mm centre-radius exclusion. Region distance preserves holes and
concavity without dilating source contours. The full target and local margin must
fit inside the calibrated exposure; missing targets or unsupported boundaries
remain unknown, distinct from detected neighbouring material. Thresholds and
material classifiers are unchanged. This classifies the requested material, not
every possible contaminant; it is not a physical cleanliness certification.
Receipts name the local policy, required edge gap, distance basis and status.
Historical evidence and recordings are not rewritten or retrospectively passed.

The separate reactor assessment explicitly retains its legacy 5X radius policy
(`local=False`). Its `neighbourhood_capture` uses a 3560 by 2360 SIM raster and
the existing 2.6 by 1.72 mm field, not a verified physical sensor specification.
Ordinary 5X discovery, aperture and release exposures, optical profiles, and
source-measurement provenance remain unchanged.

Standalone experiments use an unrestricted thickness revisit quota; device tasks
retain their Device Guidance role quotas. Explicit revisit IDs still require
eligible stored candidates, bounded capture counts and successful optical refinding.
An explicitly named flake may be photographed again even when it already has
images; order, deduplication and capture bounds still apply.
Revisit admission requires chip/material-bound, verified and completed `scan_chip`
observation events from an actual `search_chip` job. The scan must cover its
declared positive tile count within the blur limit; zero detected candidates is
valid evidence. A later exfoliation submission, specimen replacement/reset or
material change invalidates prior evidence. This check runs during flow parameter
resolution, before a revisit job, reservation, motion or status write. It never
converts a revisit into a full scan. Existing scan events remain usable without
new fields; a legacy `searched` label, coordinates, candidates or successful
empty revisit without scan provenance require an explicit full scan first.

Search records photographed flakes and complete observed outlines separately from
inventory labels. `optical_geometry` uses centre-relative mm in source-chip axes
with saved image, source-chip, source type and job provenance. Simulated optical
geometry is usable only on simulated benches; legacy display shapes cannot
establish containment. Neighbourhood captures record the inspected
radius, objective/FOV, detections, images and job. Pure geometry/selection rules
belong in `records/geometry.py`; shared material pickup admission remains in
`records/materials.py`. Camera/focus/capture orchestration remains in
`flows/library/pickup_evidence.py`. Saved evidence can support selection without
unconditional rescanning; missing or stale evidence requires targeted reacquisition.
A successful job alone must not turn evidence for a different specimen into current
geometry. Moving a flake away from its source invalidates source-bound evidence.

The version1 RGB material adapter is a separately validated recognition backend;
its presence alone does not select it as the default laboratory analyzer. It
uses the modern optional `vision` dependencies, without importing the frozen
version1 SDK environment. It
reads captured RGB bytes and explicit pixel calibration, never simulator flake
annotations. Its paired evidence retains the original integer label mask and a
separate binary mask selecting exactly one original component. Neither mask may
be opened, filled, grown, truncated or replaced by a convex hull. The original
20X image, masks, algorithm/material-policy identities and revisit selection are
bound in `recognition_chain`; 100X thickness evidence cannot replace that geometry.
`rgb_optical_outline` admission reopens all three artifacts, verifies hashes and
exact component equality, then reconstructs calibrated pixel-cell boundaries
with the recorded chip rotation. Material policy, unique revisit selection and
every admitted outline vertex must match. The flow writes an original
`microscope.rgb_recognition` event linked to its actual microscope capture and
prior successful measured chip rotation. Inventory must match that same-job,
pre-completion receipt; coherently rewriting calibration and outline in mutable
sample properties cannot establish a different physical scale. Current polygon admission still
rejects clipped, nonconvex, holed or excessive-vertex outlines explicitly;
retaining their recognition evidence does not make them usable designs. Existing
legacy observations retain their original source contract and cannot disguise
new RGB evidence under a legacy source label. Designer creation and current-design
validation share this admission through `records/geometry.py`.

The shared pickup-admission rule requires current specimen-bound evidence and
an explicit boolean `clear: true`. Evidence must name the exact flake, not
another flake from the same chip/search. Ordinary selection and ranking, direct
stack-flow resolution and material-metal assessment use that same rule before
job creation or reservations. Missing, stale, uncertain or non-boolean clearance
requires fresh inspection or another candidate. Valid unclear assessments
remain available for display; they do not become admissible by default.

### Device Guidance, selection and layout

Advisor owns Device Guidance, Designer owns Layout, and Qumus owns the Task Plan.
Legacy two-contact prepatterned targets expose `electrode_geometry`: design/calibrated-image/simulated
source, reference, micrometre units, contact gap/span/depth and image calibration.
Synthetic geometry cannot establish a real target. Supported geometry is a straight
bridge between opposed contacts; absent or unsupported data remains unresolved.

Named direct-contact requests additionally require identified contact regions:
complete polygons, a distinct contact pair, explicit chip-centre axes in mm and
the same design/calibration provenance. The gate is not a contact. Synthetic
regions derive from the same pattern as the renderer; dimension-only legacy
records cannot establish a named contact. The region participates in target
snapshots and guidance hashes. Normal motion still takes bottom-left chip
coordinates, using the declared chip size for conversion without a nominal-size
fallback or a changed coordinate convention.

Legacy direct-contact campaigns freeze target intent and requirements before finding. A resolved
transfer contract subsequently binds the actually selected source, its complete
observed outline/image provenance and the same intent before hardware dispatch.
It must not preselect a flake merely to construct the campaign. That historical
contract requests sole Gr and PC on a fresh, explicitly unlayered target. Pickup
order is top-first, so the last incoming layer faces the target; lower hBN or
unknown receiving layers cannot establish direct Gr contact.
Execution admission, pickup and release recheck the frozen external image,
geometry and public receiver continuity. Verified own pickup/release records
explain legitimate lifecycle changes; stale facts never refresh the contract.

One shared geometry predicate checks the same connected Gr body against both
designated contacts and the declared bridge/overlap requirement. Release uses
current image-bound observed geometry and qualified pickup/layer provenance;
an intended drop point or renderer overlay is insufficient. Independent private
evaluation checks the actual persisted model geometry and selected identities.
These establish ideal geometric/interface compatibility, not electrical contact
resistance or real three-dimensional metrology. Missing evidence stays unresolved.

`submit_device_plan` binds the target and its geometry snapshot, explicit contact
overlap, channel width, placement margin and rationale. The derived continuous
footprint accompanies each role. Picker supplies evidence for calibrated clean
inscribed width/height; code checks dimensions, not area alone. Designer's simple
rectangle validator uses this footprint when present and the historical 5 × 10 µm
default only without resolved target geometry. These are reported optical judgements
and basic layout checks, not automatic segmentation or KLayout certification.
Changed target facts require renewed guidance/layout binding.

The eight-contact Hall-bar path instead uses `device_design_target` with typed
millimetre polygons in chip bottom-left axes, source/drain and three voltage
contacts per side. It requires the complete observed hBN and Gr outlines, with
hBN covering all Gr and the target's explicit eight-contact coverage policy. The
legacy `hallbar-target-v1` retains its overlap-area policy and serialized defaults.
The additive owner-imported `hallbar-target-gds-v2` requires complete containment
of all eight independently defined ends: entire source/drain terminal boxes and
the first2 micrometres of each full-width side path. It retains all34 original
metal primitives for physical state and GDS, separately from the eight end regions.
The import is deliberately restricted to the selected cell/layer/DBU/primitive
recipe; it is not a generic arbitrary-GDS importer. Shared parsing and coverage
helpers govern design, pose admission, execution endpoints and scoring.

One immutable private bundle contains `target.json` and its original GDS.
Read-only loading verifies typed geometry, instruction/source bindings and file
hashes; it does not decode GDS. Explicit operator preparation uses native original
integer semantics, and native MCP independently checks those semantics and the
saved export before artifact acceptance. Configure the imported target before
bench construction. Registration can only reaffirm that constructed binding;
an agent cannot install different physical metal by changing ledger properties.
Campaign preparation may materialize exact bytes once through the optional
`record_demo` attempt-preparation hook before services. Admission and continuation
only read/verify existing files. The current campaign revision is
`hallbar-linked-owner-import-target-v2`; legacy framework-version3 hooks remain
optional for older campaigns.

Designer’s
`create_device_design` calls the installed KlayoutClaw MCP bridge on loopback
port 8767. Trusted code generated solely from the typed geometry runs through
one atomic new-view operation: create an owned tab, write GDS, reopen it in a
detached layout and compare exact cell/layer inventories and polygon XORs, then
load the saved geometry into that owned tab and capture a native device-scale
viewport. User tabs are not reused or closed. The bridge must advertise and
confirm `atomic-new-layout-v1`, which creates the new view before its usual
current-view handle acquisition. Ordinary current-view save/screenshot calls
cannot provide this isolation. No model-supplied Python, output path or backend
waiver is accepted. Protocol errors, ownership changes, server auto-recovery,
timeouts and missing artifacts refuse acceptance without an automatic retry or
batch fallback; a timeout can leave remote completion unknown.

New `klayout_mcp` artifacts bind a typed receipt hash covering the payload,
generated script, saved GDS, native PNG and owned-view identity. The existing
artifact validator handles this additive variant. Historical `klayout_batch`
records retain their exact serialized form and canonical digest and remain
readable without a live bridge. Native images are visual evidence, not human
approval. Transport capability and an offline mock do not establish actual
native round-trip acceptance; integration evidence is tracked separately.

The backend is an explicit operator setting, `QUMUS_KLAYOUT_BACKEND`, read by
`agents/klayout.create_design_artifact`: `mcp` (default, the KlayoutClaw path
above) or `python` (cloud and free scripted runs; optional extra
`pip install -e '.[design]'`). The `python` backend executes the identical
generated save/reopen/verify script with the `klayout` Python module, using
headless stand-ins only for the GUI view calls, renders `preview.png` instead of
a native viewport and binds a `KLayoutPythonReceipt` to a
`klayout_python` artifact that the same artifact validator checks. An
unavailable selected backend refuses acceptance; nothing switches backends
automatically.

Placement is computed, not guessed. `records/design_placement.py` finds the
largest-clearance rigid poses for the selected pair: Gr over every contact end
with its endpoint pose-window displacement, then hBN over the whole placed Gr
with both displacements, both inside the chip (the same rules the screen
enforces). Convex outers use an exact per-rotation linear programme; non-convex
or holed outers use an erosion search that is re-checked exactly. A non-fitting
pair yields its best attempt, the limiting constraint and the shortfall in
micrometres. `get_design_context` returns this as `suggested_placement`;
`create_device_design` still re-screens and saves whatever placements it is
given. `report_design_infeasible` stores the measured shortfall and refuses to
hand back a pair the solver can fit. The pair-selection stream calls
`solve_gr_placement`/`solve_pair_placement` to rank candidates.

`device_design_v1` binds project, revision, source evidence, target dimensions,
placements/rotations, pickup order, canonical digest and artifact byte hash.
The pose tolerance is frozen from the synthetic verification configuration
(currently 1 micrometre and 1 degree, owner-selected on 2 November), not
supplied by an agent. A proper rigid-body fit
forbids scale, reflection, cropping and changed vertices; containment/contact
checks remain strict. These defaults do not certify real apparatus calibration.
After a designed source-chip rotation, pixel registration uses the difference
between the original and current immutable optical rotation observations as its
nominal orientation. The calibrated pixel aspect ratio and enforced camera-axis
convention determine the projection. Residual rotation, translation, image
quality, ambiguity and field-edge gates remain unchanged; angle metadata alone
never establishes specimen identity. Original/default-zero registration retains
its existing search grid.
Admission revalidates current sources and target; a frozen artifact check alone
cannot authorize execution. The stack flow consumes that binding and verifies
the actual observed final geometry, recording its result on the device. The
required top-first order is hBN/Gr/electrodes/silicon. Geometric success does
not certify electrical performance, fabricate a lithographic Hall-bar outline,
or establish physical calibration from a simulated preset.

Structured `flake_roles[].count` determines quantity; free-text backup suggestions
cannot increase it. A shortage claim when enough readable candidates exist needs
structured assessments of every readable candidate and explicit observed rejection
risks. Unknown, duplicate, unreadable or unviewed IDs are refused. Optional backups
cannot justify another exfoliation when assessed feasible candidates meet the count.
Independent selection, thickness, cleanliness and image checks still apply.

Whole-stack pickup checks share convex-polygon rules in `core/polygons.py`.
The first lifted hBN layer must cover each lower lifted layer; a fixed receiving
substrate is not another pickup layer. Complete observed outlines, offsets and
rotation must be compared in one physical frame. Area or a nominal concentric fit
is only screening: executed pickup must verify the actual stamp/source placement
before contact. Unsupported concavity, partial outlines and unknown geometry fail
closed rather than being silently replaced by a convex hull or fabricated size.
Ordinary device and material-metal paths must retain the same coverage invariant.
Admission, ranking and pickup use the same complete convex-polygon predicate in
an explicit common frame. Without a future positioning budget, planning reports
`nominal_only`; it cannot assume zero uncertainty. A finite budget bound to the
same evidence and its provenance reports required clearance, headroom and
`bound_satisfied`, `bound_failed` or `unresolved`. A fixed-twist translation
candidate preserves the original placement tolerance and full uncertainty. It
certifies a snapshot, not command arrival or future coverage.
Selections snapshot the source images and geometry; changed evidence requires
reselection. Nominal screening cannot replace the final camera-plane check.

A predeposited metal report supplies material/provenance, not an observed shape.
The transfer target is inspected through the microscope before release and its
geometry is saved with image/job identity. Missing calibrated observation cannot
be replaced by a label. Containment of the landing point in the coating is distinct
from full transferred-footprint coverage and from later preparation acceptance.

### Shared simulated morphology and compatibility

Simulated flakes carry persistent centre-relative `outline_mm` and `origin`
(source chip, tape batch, seed, original outline and model version). Microscope
metadata adds camera-centred outlines, visible area and geometry source; rendering,
pickup and drop project the same physical geometry. Fresh targets derive simulated
electrode dimensions from the same optics polygons used for their images. Explicit
scripted footprint fixtures remain labelled synthetic rather than image measurements.
Old area-only state has a labelled deterministic display fallback; images are not
regenerated to make old state satisfy new geometry requirements.

New worlds persist a separate observation RNG so perception does not consume the
mechanical error stream. Old checkpoints retain legacy shared-stream semantics;
`legacy_shared_observation_rng` is only an explicit historical-check option.
Fresh population policy comes from `config/material-morphology.yaml`; restored
worlds retain their saved policy and random state. `LocalReaction` accepts exactly
two distinct placed flakes and produces at most one connected overlap-bounded
product with source lineage. Its kinetics are uncalibrated fixtures. `material_view`
observes this model without writing the ledger or operating devices. See
[material simulation](material-simulation.md) for assumptions, licenses and units.

## Physical workflows and reactor scope

Typed Flow/tool Params are authoritative for limits and defaults. Resolved values
are recorded with the submitted run; live model text does not bypass them.

| Operation | Main contract and ledger effect |
| --- | --- |
| `place_chip_on_transfer_stage` | Loads a known chip; resolves a middle-board handover for left-board sources; records transfer-stage location and world pose. It is not an unload tool. |
| `exfoliate_chips` | Material must match an eligible source; resolves carriers, chips and reserved destination slots; records exfoliated chips and daughter-tape material/use/reel state. |
| `search_chip` | Scans the current chip, photographs bounded candidates, records chip corner/rotation, flake positions/images and candidate/catalogued/lost states, then stores the chip. |
| `revisit_chip` | Requires verified current-specimen scan history; photographs eligible candidates without a new scan; explicit IDs retain capture bounds and optical refinding. |
| `stack_device` | Picks 1–6 catalogued/selected flakes top-first and releases to a distinct target; `target_xy_mm` is measured from the chip's bottom-left corner, defaulting to its centre. Records device/layers, stacked flakes and used/torn stamp history. |
| `inspect_bench` | Reads specified overview regions without motion or device locks; reports world-frame observations and coverage. |
| `prepare_material_metal` / `reactor_material_metal` | Guarded preparation and reactor operation bound to original requirements, exact sources/support/coating, observations and terminal state. |
| `cool_experiment_stages` | Final two-stage measured shutdown/cooldown; project completion requires the separate fresh locked check. |

Sample locations name actual places or carriers, not inferred source provenance.
Ordinary chips progress bare → exfoliated → searched; target chips may start
patterned. Flakes progress candidate/catalogued → on_stamp → stacked. Stamps
progress fresh → in_use → used/torn; only a cited person's film replacement can
restore fresh status. A torn stamp is swapped automatically only when it has not
yet carried a stack in preserved historical workflows. The selected normal
process holds film damage for human review, including a first pickup when
partial transfer is unknown; it does not automatically replace the stamp.

### Current material-metal experiment

The current experiment reacts a user-specified 2D material with a user-specified
predeposited metal on silicon; topmost hBN remains nonreactive. The owner example
is Gr/Pd. Picker screens/confirms sources, Hands performs guarded hBN-first transfer,
and Reactor loads, observes, exposes, shuts down, cools and stores the carrier.
Known PC/mica support remains a property rather than an extra final layer.

The model-visible transfer entry is `stack_device` for both ordinary and metal
targets. A material-metal target retains `prepare_material_metal` as its internal
job/Plan operation and receipt, with the exact confirmed experiment inputs and
carrier guards. Both paths share transfer mechanics and support-dependent release;
this does not expose arbitrary unchecked Python operations to the model.

The default `qualitative-material-metal-v1` protocol uses physical simulated device
operations. Geometry, source/coating provenance, ledger state and saved images
must agree. Transfer and interface-progress assumptions are explicitly uncalibrated
and frozen in receipts. Time and control actions advance the model; taking another
image does not independently advance it. Failed release, unobserved/stalled progress
and interrupted exposure cannot pass acceptance. `reactor_simulation_result` can
close a simulated task only after original requirements, executed jobs, observations
and terminal state pass; `experiment_accepted` and `chemistry_verified` remain false.
The opt-in abstract receipt fixture is not device-experiment evidence. See
[owner workflow contract](reactor-owner-review-20261027.md).

### Reactor geometry, persistence and legacy experiments

Everyday startup uses the integrated three-arm simulation with normal noise and
no preprepared-pair fixtures, in its own workspace. `--previous-workspace` opens
the earlier two-arm checkpoint. The integrated bench has six stations/arms spaced
190 mm apart, a 190 mm rod with 160 mm insertion, gas feedback, whole-assembly XY,
a separate insertion axis, heater and independent reactor microscope focus.
Chamber and rod move rigidly together under fixed XY optics during observation;
insertion is separate. Preview, replay and query context use the same state.
See [reactor model](reactor-model.md) and [hardware boundary](reactor-hardware.md).

Checkpoint restore binds the existing ledger through `reactor_store` without
reseeding samples. It validates device identity and mechanical/layout signatures
before applying any saved fields. Incompatible reactor enablement/layout changes
are refused without overwriting world, ledger or checkpoint; use a compatible
configuration or an explicitly fresh database. Seed, pair state and consumed-source
history survive recovery. There is no automatic layout migration.
The sole signature compatibility exception is the addition of the synthetic
`geometry.metal_patch_outline_mm` optical fixture: the saved signature must match
the current layout with exactly that key removed, with matching reactor devices
and mechanics version. Restore updates only the loaded signature; all prior
geometry, bench layout, experiment and reaction parameters remain guarded.
The original checkpoint is unchanged, and physical state and random streams survive.

Legacy `two_flake_reaction` uses `PairMaterialBackend`, `material_view` and
`LocalReaction`, with one prepared pair and at most one small product. It reserves
inputs/carrier/port and rejects extra, moved or consumed sources. Acceptance locks
SQLite before reading goals/jobs/observations and atomically commits product,
consumption, project result and events. Saved products retain `images.reactor`,
carrier/source IDs and preparation/evidence jobs. Its consumption of both inputs
is incompatible with current nonreactive hBN; it is not a substitute for the modern
material-metal workflow. `synthetic_prepared_pairs` and historical CVD growth are
explicit fixtures/legacy compatibility. See [legacy pair contract](reactor-pair-contract.md).

### Known real-apparatus gaps

Owner laboratory facts supersede older assumptions but are not claims of a
completed hardware migration. Mother tape is the material-bearing left-storage
source; daughter tape is the exfoliation-stage reel. Changing material requires
fresh daughter tape. Repeated contact of the same daughter region is distinct
from the simulator's massage count. A lifted chip currently parks for a person;
the requested vacuum-off/reel-return/vacuum-on/peel recovery needs measured state
and bounded retries before it can be automatic.

The ordinary-stage simulator models coarse translation, fine x/y and in-plane
rotation, independent microscope focus and stamp-holder x/y/z. World coordinates
remain relative to the table front-left; stage commands are offsets from its
load station. Fine translation precedes rotation around the translated stage
pivot, preserving off-centre chip mounting. This nesting, pivot, bounds and
dynamic rates are explicit synthetic assumptions, not recovered calibration.

Capture freezes objective, focus, scene, calibration and sample/stamp poses at
exposure. Pixels are available in memory, with retained files saved on demand.
Execution microscope adapters timestamp a new exposure in the configured Clock
basis, converting device timestamps explicitly when necessary. Guarded capture
requires a finite timestamp inside its call's start/end interval; equal times are
valid while SimClock is unchanged. Old, future, invalid or reversed-clock captures
raise a typed observation rejection, and the affected transfer phase holds for review.
The rejected exposure cannot establish current sensing. Preview and historical
image playback retain their separate, non-verifying contracts.
The calibration includes named synthetic contact and carried-hBN photometry
profiles. Saved reference consumers compare the complete optical binding;
missing or changed profiles require new evidence. These profiles describe
illustrative rendering assumptions and cannot authorize real apparatus control.
Fresh simulated exposures also freeze `observation_noise_scale` and any explicit
`simulation_condition` in the image packet and calibration's
`observation_condition`. Absent/default noise scale remains one. Rendering uses
that saved value, with no live configuration lookup or historical backfill.
Scene-bound specimen texture records chip identity, seed, pose, size and camera
centre. Fixed chip-local procedural tiles project through that exposure's pose
and apparent optical shift; removing a flake or changing objective cannot
regenerate the residue. Stamp texture uses its own stable identity seed.
These are renderer inputs, not observed geometry or control authority. Legacy
packets without a specimen binding retain their previous raster interpretation.
The carried-flake raster also declares `synthetic-stamp-plane-psf-v1` in the
same optical profile. Material opacity stays independent of focus. Ordered
premultiplied color and coverage receive the same positive, normalized finite
Gaussian kernel before composition over the separately defocused chip. The
fixed synthetic width is `sigma_px = 3 * sqrt(1-q)`, where
`q = clamp((sharpness_stamp-0.5)/8, 0, 1)`; its four-sigma support uses 14-pixel
padding before cropping. This is a pixel-space approximation for carried flakes,
not calibrated apparatus optics or a complete PC/PDMS interference model.
Contact fronts and tears retain their existing projection; upstream physical-FOV
culling still omits possible halos from wholly off-field objects.
Old or missing raster profiles cannot authorize reuse of an optical reference.
Historical images and reference metadata are not backfilled.
The separate `synthetic-white-light-air-gap-v1` fringe profile uses a fixed,
illustrative Gaussian RGB response in wavenumber. A bounded lookup table and
subpixel interpolation project colour from the same frozen signed gap, configured
dome radius and physical contact footprint. Positive-gap outer annuli can remain
after contact. The renderer does not advance Clock, choose random ring colours
or change contact mechanics. Its exact optical-geometry preview key preserves
submicron phase changes that spatial-pixel quantization would hide. Illumination
is owner-reported; spectral response, film optics and cap deformation remain
synthetic assumptions. The simulation-only `optical_gap` packet is rendering
input, not observed gap evidence or a motion certificate.
Live preview is a pure current scene; bounded immutable motion previews use the
existing Clock, `lab.frame` and export/replay path. High-precision geometry is
separate from rounded drawing fields. Polling must not change physical state,
observation randomness or outcome. A mechanics signature binds new checkpoints;
incompatible old stage-Z-focus states are refused before overlay and remain
available for read-only export/replay. The single launcher uses a fresh private
`runs/workspace/twin/` workspace rather than overwriting old state.

The simulator retains bounded whole-material contact witnesses in its existing
stamp state. One actually evolved footprint must cover a complete outline; a
union of partial patches or a maximum radius moved to another centre cannot
qualify it. A simultaneous whole-stack witness governs release geometry. The
existing 0.1-second Clock mesh and real control endpoints retain this history;
fractional preview polls do not create witnesses. Present optics still use the
instantaneous front. Detachment keeps the configured probability, temperature,
fault, film and placement rules, including the original candidate draw order.
This is a coarse synthetic adhesion model, not laboratory contact metrology or
fragment/fracture mechanics. Its internal witnesses are not sensing evidence.

The existing checkpoint path persists the episode history. A legacy active or
unknown contact without it is refused before live state is overlaid. Only a
provably empty precontact legacy bench gains empty bookkeeping in its loaded
copy; the original checkpoint remains unchanged. Zero-front new episodes cannot
consume a pending release fault, while genuine earlier whole-contact witnesses
remain valid through recession.

### Selected source-based normal transfer

The migration contract is [stacking-process-v1.md](stacking-process-v1.md);
local process acceptance remains pending. The sequence refinds and centres each
source at 20x, applies an optional declared chip-local design shift, then selects
5x before initializing the holder and applying its fixed synthetic focus offset.
Only the first layer on a loaded stamp acquires a first-contact candidate by
comparing descending captures with the retained initial image. Its near-round
changed region is an image observation, distinct from the PDMS geometric centre
or an interferometric gap. One clear-height XY reaim establishes the working
holder position. Later layers reuse that position with exact stamp/run/profile
binding. A replacement stamp needs its own first-contact observation.

For a new bound Hall-bar design, the first source additionally requires observed
paper-aperture alignment before first contact. The explicit synthetic structure
uses a diamond paper opening around a circular PDMS footprint; neither defines
the observed first-contact centre. Saved pixels, exposure calibration and
encoder receipts establish alignment. Missing edges or invalid clearance hold
for review. Historical optical packets retain their original profile.

The new-design first-contact sampling policy is frozen at submission and in the
phase receipt: at most 0.25 micrometres per approach command, a 0.1-second
observation window and 4096 observations. Existing speed, travel, floor and
phase-time limits remain. The finer cadence applies to the observed approach;
withdrawal keeps the ordinary safe segmentation. The ring cue must be one local
family with consecutive, angularly coherent radial bands in the same original
reference/current exposures as the optical candidate. Native-pixel uncertainty
and closed-boundary compatibility qualify their centres. New-design reaim uses
that admitted ring centre; an earlier cached boolean cannot qualify a later
candidate. This is qualitative optical localization, not a measured gap or
absolute apex accuracy. Legacy phases retain their budgets and centroid behavior.

New-design pickup requires the entire transformed source outline plus margin
inside individually supported contact-interior pixels. The front bounds the
analysis domain but never fills it. The shared immutable synthetic optical laws
define a sufficient contact model and a conservative unchanged-image hypothesis.
The latter includes all carried hBN states permitted by the frozen public
nanometre labels and all fractional mixtures within the one-pixel reference
stencil. Only pixels excluding that entire hypothesis can support coverage;
holes, clipped pixels and ambiguous regions remain unknown. Complete-outline
geometry retains its 175.2 micrometre margin plus a native-pixel registration
guard. A larger source can legitimately remain unobservable.

This classifier is restricted to the declared synthetic camera, contact,
complete-outline tint and PSF profiles. Exact original captures, unchanged
sample/focus/holder-XY context, observed aperture clearance and public source
classification bind its calibration. Registration uses the recorded Clock and
complete command/encoder prefix with declared deterministic drift bounds.
Encoder receipts use the versioned public axes/camera projection, including
original event/run identity, time and device command. Display-only previews and
unused renderer geometry are excluded from that projection; the actual analysis
captures retain their separate strict image-content hashes. Saved coverage must
match the original phase reference and its independently verified endpoint. A
pixel-supported chip domain must exclude the dome-surface branch discontinuity
before its height-slope bound is used. Gaussian detector positions alone cannot
provide that finite geometric certificate. Unsupported context is unknown; no
hidden gap, apex, realized drift, layer count or contact mask enters the classifier.
These optical assumptions cannot qualify real laboratory learning.

A finite Clock dwell follows coverage; then the stamp actually parks laterally clear
before high-magnification source inspection. Same-run immutable dwell, park,
focus and image receipts bind these steps for replay and recovery. The default
one-second dwell is an explicitly synthetic recipe, not a polymer measurement.

Pickup retains one fixed difference reference and uses segmented Z-only descent.
The largest admitted changed region must contain the mapped target point with a
strict physical edge margin. Historical pixel scale, area and offsets are mapped
through each actual image calibration and signed axis convention; old camera
indices and motor signs do not become current calibration. A missing, incomplete
or changed optical binding cannot authorize processing an old reference.
Continuous XY correction, paired-plane tracking and inferred-gap servo control
are not normal-process readiness requirements.

The existing Gate and Clock govern movement, cancellation, exposure, watchdog,
readback and finite cumulative phase budgets. A saved pending intent cannot be
blindly replayed or bypassed by changing a phase name. Capture and model-inference
cadences remain separate: agents do not reason once per Z segment. Saved images,
first-contact evidence, workflow Steps and normal-transfer metadata remain in the
existing RunStore/event/recording path, rather than a second controller store.

If the next historical pickup increment crosses the evidence-bound process
minimum, at most one final partial increment may use the remaining budget.
Its admitted target retains the existing command tolerance above that minimum;
the original request must still satisfy global hardware and focus limits.
The actual admission and consumed reservation are saved together before motion
in the existing Store (`stack.pickup_macro_admission`). Requested and executed
targets remain distinct in the segmented motion history. Coverage, source
confirmation, watchdogs and physical limits are unchanged. An interrupted or
exhausted reservation cannot create another partial increment through retry,
metadata deletion or restart. Command tolerance is not a measured actuator error.
Each settled tail exposure copies its saved intent into the existing
`lab.frame.microscope_preview.transfer_motion_intent`; later lifts cannot erase
that history. The recorded intent must still match actual command/readback and
capture evidence. It does not itself establish execution or transfer.

Coverage alone establishes neither transfer, complete-flake containment, stack
registry nor chemical identity. The owner's 31 October visual process criteria
are source disappearance at the selected source location for pickup, and the
expected material appearing at the intended target location for placement.
Qualified wafer images must establish those observations before ledger
relocation. Stamp autofocus, a positive stamp count or a negative observation
of an empty film are not additional process requirements. Correct location,
native focus, held capture context and before/after evidence still matter;
unavailable views, source remnants and ambiguous preexisting target material
require attention. Another press is not an automatic correction. These process
criteria do not establish full buried-layer identity, stack registry or chemical
success. Independent terminal simulation truth remains evaluator input, not
sensing or controller authority.

The pending `v1-normal-sim-v2` migration binds a fresh native-20x source focus
after final centring and design shift to its actual AF and capture events. Its
named positive-up synthetic mapping transports the first candidate's process
reference to later source heights from that same first anchor. It propagates
observed focus intervals and admitted mapping uncertainty; the conservative
upper reference bound sets the unchanged candidate-relative press limit.
This is an optical process reference, not contact, gap or force metrology.
No World height, accumulated per-layer shift or widened physical limit may
replace missing evidence. Old profile-v1 runs remain available for review;
they cannot acquire these bindings retrospectively or resume as profile-v2.

Profile-v2 evidence uses `qumus-image-content-evidence-v1`: image locators are
normalized to their paired content hashes for digest calculation, while actual
files still require SHA-256 validation. Recording export can relocate unchanged
images without changing the evidence identity. The existing `lab.evidence`,
`lab.frame` and `step.verified` events bind the first candidate, phase baseline
and endpoint observations respectively. Mutable metadata copies or successful
booleans alone cannot replace those historical records. Ordinary, material-device
and reactor release consumers share the same release-commit proof check.
The target-before view's content-bound digest is also retained in the existing
`GoToTarget` verified observation. Its recorded material classification must
match that history before release; editing a preexisting-material count in
mutable metadata cannot establish a blank target.

The declared PC release retains the source order: measured 155 C entry, one
admitted release stroke, measured temperature strictly above 170 C, withdrawal,
then heater-off. Historical withdrawal intent maps to the current existing safe
height, with its adaptation and actual readback disclosed. The provisional
release preset is not a measured laboratory contact height or universal safe
recipe. Mica uses its explicitly admitted existing synthetic support recipe in
the same pipeline. Neither path may widen physical limits or hide an unsupported
stroke to obtain a passing endpoint.

The pending version 2 release policy transports the same first source/candidate
anchor to the verified parked target-before native-20x focus interval. It reuses
the latest required original pickup's verified coverage segment command relative
to that pickup's recomputed upper process reference, then applies that intent
to the target upper reference. Immutable coverage images, the original verified
source-disappearance endpoint and owned command/encoder/capture history qualify
the witness. Missing or inadmissible latest evidence holds without an earlier
fallback. The unchanged press minimum, command margin and global limits still
apply. This is a declared SIM process-memory choice; AF uncertainty does not
cancel or guarantee physical coverage reproduction. The nominal fixed-difference
policy remains historical, and neither policy is recovered v1 penetration
metrology. Shared release handling then parks the withdrawn stamp
after confirmed heater-off, and acquires the target endpoint through the same
unobstructed path as the before view. Its prior focus seeds one bounded fresh AF;
actual focus, calibration, ROI, pose and image evidence still qualify the view.
Missing evidence or endpoint appearance holds without a new press. Fresh policy
bindings and reserved exit/endpoint work are checked before motion/restoration;
old held-target executions remain historical. No additional registration system
or runtime state store is introduced. See the complete rule in
[the source-process contract](stacking-process-v1.md#target-relative-release-policy).

The shared release path requests the existing fixed 0.5 C setpoint headroom
at entry as well as at the final release threshold. Actual measured entry and
release predicates remain unchanged. This avoids waiting for a first-order
heater to reach its exact asymptotic setpoint; it does not relax feedback,
timeout, stall detection or maximum temperature, or escalate heat on retry.

Saved raw procedure/profile bindings are checked before process-context
construction, restoration, probes or current Step callbacks across actual
restart, Continue, retry and reverify entry points. Missing or old bindings keep
old runs available for review rather than reinterpreting them as this process.
Admission uses actual saved checkpoint progress: completed first contact needs
its historical placement, and started release needs its retained phase before
targets can be restored. Future endpoint evidence is required only when the
saved progress claims completion. Missing mid-act evidence cannot create a new
phase during retry.
Release admission also anchors the baseline to the earliest actual own Step
start and its first capture. Later captures or reset counters cannot hide prior
heating or motion and make an interrupted release appear unexecuted.
Unconfirmed energy shutdown retains its separate trusted shutdown-only
verification and device locks.

Autofocus retains sampled raster-sharpness curves, actual focus readbacks,
exposure times and a whitelisted axis/pose binding in existing focus events.
Bracketed sampled passes support its interval estimate; flat, edge, unresolved or
uncalibrated curves do not provide a finite interval. These estimates are not
metrology. Isolated ordinary focus operations may follow a completed difference
phase; changing focus during that phase invalidates its retained baseline.
Synthetic drift, refraction, contact relaxation and slip remain unvalidated
physical approximations. Coloured Newton rings do not certify zero relative
motion, force, registration or an actual air-gap measurement.
Do not infer real travel ranges, motor IDs, fan response or validated chemistry.

## Agent runtime, policy and experience

The modern adapter uses OpenAI Agents SDK (`agents/openai_runtime.py`), with
scripted scenarios behind the same AgentRuntime interface. SDK function tools
always invoke `ToolGateway.call`; agents-as-tools retain delegation checks.
The gateway checks permission, schema, references and cross-tool rules before
execution, and logs results/user instructions afterwards. Lifecycle callbacks
are not permission enforcement. Runtime `mode=sdk`, `provider=openai` names the
adapter, not necessarily the vendor behind a gateway model ID.

Tool image transport preserves original file bytes, including native microscope
PNGs larger than 4 MB. Its local envelope is four images of at most 8 MB each
(about 43 MB of base64 payload), independent of provider limits. Every attachment
is named; unavailable, unsupported, oversized and count-limited images produce
explicit omission text rather than silently leaving only label masks visible.
The outgoing history filter retains the latest four distinct images; raw local
images and recorded tool results are unchanged. Successful local encoding does
not establish gateway acceptance or that the model interpreted the image.

OpenAI session IDs live in project state without replacing legacy `session_id`.
The v2 Claude execution adapter, routes, credentials and proxy are removed;
`/api/model/check` and health `provider`, `model`, `model_status` serve current
runtime status. Historical Claude events/recordings and secret scrubbing remain
supported without installing that old execution SDK. See [runtime setup](runtime.md).

Policy retains Eyes-first real `inspect_bench` evidence, Advisor-only experience
checks, and material-specific Advisor lookup before exfoliation delegation.
A film report must cite an existing same-project human UI instruction naming the
stamp. Delegation captures physical obligations, so concurrent plan edits cannot
erase active checks. Inventory reuse and conditional exfoliation → search →
selection are prompt requirements as well as code-checked admissions; real model
compliance requires fresh evidence, not only prompt inspection.

### Read tools and advisory boundaries

| Tool | Evidence and scope |
| --- | --- |
| `list_samples` | Ledger inventory, identity review and provenance. Hands normally scopes flake reports to one chip; deliberate global reads remain available. |
| `get_instrument_status` | Latest saved `lab.frame` per device, event time and store-clock age; no new observation or inferred readiness. |
| `get_lab_setup` | Registered capabilities, prerequisites, flow storage contracts and external needs, without inventory counts; Advisor/Qumus access. Compatibility reactor setup reuses this common surface. |
| `get_experience` | Bounded advisory summaries: up to four materials, six entries/material, three recipe groups and 16,000 serialized characters; source/counterevidence/omissions explicit. Hands cannot call it. |
| `query_run_records` | Qumus audits/internal use, excluded from ordinary Advisor tools; review/vote ownership unchanged. |
| `get_flake_candidates` | One inventory snapshot with eligible candidates and bounded role rejection/missing-image diagnostics; an empty set is not automatically an image failure. |

Normal reports reuse `wait_job.outcome`. Registered exfoliation defaults are
`validated: false` simulation inputs, not laboratory experience. On simulations
without an enforced confirmed baseline, Advisor may fill omitted historical trial
fields from those defaults while retaining explicit user values and labelling
additions. Oversized legacy experience records return bounded needs-attention.
Production experience rejects simulated runs; test sandboxes and seed recipes keep
their provenance/validation state. Advisor planning/review separation and the
three-chip reassessment policy remain proposals.

Exploration allowance reports free/pending chips, estimated demand, yield source,
margin and remainder; defaults 0.5 and 1.5 are assumptions. Demand is unknown
without Device Guidance unless Qumus explicitly declares a standalone experiment.
The estimate neither reserves chips nor sets an attempt allowance, and cannot
relax recipe/safety checks.

## Models, context and money

### Team model routing

Full-page Settings chooses one default model and optional specialist overrides
inside the endpoint-scoped `openai_model_config`. The default serves Qumus and
inherited roles; the six specialist IDs are unchanged. `POST /api/model` validates
and saves the complete configuration atomically; omitting `agent_models`
preserves overrides and `{}` clears them. All models for one team share its
endpoint, credential, protocol, permissions and budget ledger. Every saved model,
default or override, must pass the same catalog checks: conversational, and
account-listed when the registry is the only source. Guard rates are finite and
non-negative, one pair per model across the team.
The resolved per-role model supplies the SDK Agent, reservation rates, usage cost
and request context-limit snapshot together. Only the default model carries the
configured cached-input tariff; an override with a different model is charged at
its full input rate, so cache discounts are never borrowed across models.
`GET /api/models` exposes explicit overrides and resolved `effective_models`;
health names the distinct effective team models. A save requires a resolved
current plan, no unfinished jobs, no pending instructions and an idle runtime,
then starts fresh conversation context for the next plan. Saved historical
events are unchanged. See [design and acceptance](agent-model-settings.md).

### Catalog and next-plan selection

`GET /api/models` projects the website catalog, account-facing `/v1/models` and
exact-endpoint administrator registry through one ModelCatalog and its existing
five-minute cache. Registered, website-listed, account-listed and access-verified
are separate facts. Registry-only entries are inspectable but cannot be saved
without account listing; known account-excluded entries are refused. Discovery
failure leaves visibility unknown. Listing never proves execution, tool use or
vision support. Known non-conversational image/video/decision models require a
separate adapter; unknown capabilities remain unknown.

The administrator YAML retains all 91 exact model IDs, original Context values
and nominal USD-per-million quotes with source date and attachment hash. It applies
only to its recorded `https://physics.bytefuture.ai/v1` endpoint. Zero Context
values mean unknown. No family/suffix alias borrows another ID's capacity, quote
or access. Qwen in that registry does not prove account visibility or callability.
Read-only discovery is not authorization for bulk inference probes. Dated account
investigation results and raw receipts remain in private workstream notes.

`POST /api/model` applies only to the OpenAI runtime and uses explicit positive
input/output budget guard rates. It rejects unsupported/catalog-excluded IDs,
active turns, pending instructions, unfinished jobs and unfinished Task Plans
between jobs; only plan states `done`/`skipped` permit the next-plan boundary.
Saving does not call inference. It clears SDK objects, starts a fresh SDK
conversation and persists `openai_model_config` while preserving lab state,
history and cumulative budgets. Restore requires the same API base URL.
`model.changed` records configuration; executed model attribution comes from
usage/turn-end events and recording `models`, not from a setting alone.

Context limit resolution is endpoint/model override, then the initial accompanying
`QUMUS_OPENAI_CONTEXT_WINDOW_TOKENS`, then administrator registry. Saving a default
removes its override; manual null is unknown; switching cannot inherit another
model's limit. Settings exposes the resolved value/source. Sources are
`administrator_table`, `configured` or unknown. Only overrides persist, allowing
new defaults after restart without rewriting old request snapshots.

### Request identity, usage and capacity

`agent.request` is recorded immediately before each outgoing HTTP send attempt,
including retries, after SDK filtering/transport conversion. Responses and Chat
Completions, buffered and native streaming, use the same observer. It records
agent/model/session/call/request/attempt identity and counts, not another prompt
copy. The sanitized actual `gateway` contains scheme/host/port/deployment path,
excluding credentials, query, fragment and API operation. Current configuration
never fills unknown historical deployment identity.

`text_chars` counts Unicode code points in textual values and compact JSON tool
definitions, including instructions/history/arguments/results. Roles, IDs, type
metadata, media/base64, opaque reasoning and server templates are excluded. It
is neither tokens nor capacity. Unknown measurements are null, not zero.

`agent.request_usage` joins by exact request/call/session/model/agent identity
and preserves provider field presence: input/output, their sum, optional provider
total/cache/reasoning and `reported`/`partial`/`unavailable`. Buffered raw replies
are observed before SDK defaults. Failed/incomplete terminal Responses retain
reported usage before errors and preserve stream cleanup. Reported zero remains
zero; missing attempts stay gaps, and a successful retry fills only itself.

Each request snapshots `context_window_tokens`, `context_limit_source` and
`max_output_tokens`. Used context is reported input + output; remaining is limit
minus used only when all values are known. Cache is not subtracted; reasoning is
not added twice. Negative remaining displays an exceeded configured limit.
This is response-level monitoring, not a tokenizer, preflight gate or guarantee
about transient internal context.

`agent.request_failure` records sanitized failure kind, exception type, HTTP
status and validated provider identifiers for each failed send. It excludes raw
messages, bodies, URLs and headers. Network observation rethrows the original
exception. Default SDK retry behavior is unchanged; the opt-in demonstration
runner permits at most two SDK retries with separately reserved HTTP attempts.
UI joins exact
identity and links only increasing attempts of the same call/agent/model/session.
Recovery needs a later matching completed-response `agent.usage` without failure;
missing usage or future replay events cannot imply recovery.

### Context display and composition

History's Context tab is the single debugging view for full recordings and
Current run uses the existing live Session. The centre chart displays **one
provider-reported total context bar per request**, with unknown/partial attempts
as gaps. It does not split the bar into input/output. Bars remain 14 px wide with
separate selection targets; original request numbers, agent lanes, fixed axes and
horizontal scroll retain readable long histories. The rounded viewport navigator
tracks touch, keyboard and wheel. Pane updates preserve chart selection/scroll;
scale animation respects reduced motion. Results contains experiment outputs.

Actual usage fits the linear axis to reported totals. Full capacity draws regular
dash segments only over consecutive original requests with identical recorded
model/gateway/session/limit/source; isolated limits use a solid cap. Hidden or
unknown requests break segments. Each request's ratio uses its own snapshot,
including different agent/model capacities; no current/shared/averaged limit is
substituted. One right inspector holds request identity, retry/failure, usage,
capacity and composition; no duplicate table/inspector follows the lanes.

Context composition first shows measured Input and Output, then **Input source
estimates**. The send observer attributes actual outgoing input to ten exclusive
categories: instructions, references, current experiment snapshot, current task,
history, tool definitions, tool arguments, tool results, media, and unassigned
protocol/input. Attribution hints are ephemeral assembly/task/image provenance,
not another transcript store. Instruction ranges must match actual sent text;
unattributed content stays unknown. Every transmitted occurrence counts once.

The additive `input_composition` version-1 event stores counts only. Rough text
estimates use `ceil(UTF-8 bytes / 4)` per category; displayed shares divide by the
estimated text subtotal. Media/opaque/server framing tokens remain unknown;
no residual is relabelled as measured media. Estimates never fill provider usage,
capacity, cost or historical gaps. Cache and reasoning remain subsets. Failed
sends may have input composition and unknown response usage. Historical records
without categories show not recorded; character-only history never becomes tokens.
See [composition wire/measurement contract](context-composition.md).

### Request context and cache observations

Role rules, reference skills and ordered tools remain stable for a selected
configuration. Each actual model call receives one fresh ephemeral developer
application-state item after its complete input/tool group, rather than a
rewritten instruction prefix. The asynchronous filter waits for shared pacing
before reading state; budget reservation uses the resulting full input. The
snapshot stays out of persisted SDK session history. Stop cancels a pending
pacing wait. ToolGateway still checks current obligations at execution.

Flake inventory is an observed catalogue view: material, labels, availability,
geometry bounds/counts and immutable evidence references. Full native contours,
masks and measurement receipts remain in RunStore and artifacts for Picker and
Designer validation. Historical `list_samples` results receive the same pure
projection only on the outgoing model wire, without reassessing past availability
or editing SDK history, tool call identities or original journal records. The
Context diagnostics count these explicit catalogue projections.

The existing HTTP observer keeps bounded runtime-local keyed component digests
and emits counts/categories in `prefix_comparison`; it stores no prompt copy,
digest vector or key. This diagnoses local wire changes, not provider token
alignment or a server cache miss. Cache summaries join exact HTTP attempts and
reported usage, preserve missing/invalid/conflicting identities, and show token-
weighted cached input separately from request hit frequency. Reused request IDs
with conflicting core identity count once as ambiguous. Cached input is a subset
of total input; unknown attempts are never counted as zero-token misses.
Recording Context computes these metrics from the selected event prefix, before
agent filtering, so a reused ID with conflicting role identity remains ambiguous.
It shows measured input and request denominators, unknown/invalid coverage and
the recorded gateway when available. It cannot attribute low hit rates to the
gateway versus local prompt changes without additional evidence.
The billing view explicitly labels input-token hit and complementary miss with
their shared measured-input denominator and workspace scope, alongside coverage
and the distinct request-hit frequency. A numerical complement does not prove
that an external administrator dashboard used the same scope or metric.

### Budget enforcement, nominal quotes and billing

`agent.usage` is the sole response cost event and carries request identity and
measurement markers. `agent.turn_end.cost_accounted_by_usage=true` prevents
`RunStore.spent_usd()` double counting. Token/composition events never settle
money. The persistent private budget ledger retains request reservations,
settlement, per-workspace allowances and unknown-spend history. The stage-total
limit is finite by default; explicit `QUMUS_OPENAI_STAGE_BUDGET_USD=unlimited`
disables only that gate. Moving data or switching models cannot reset allowances.

Administrator registry quotes are dated nominal reference prices. Current-account
applicability, cache rates, multiplier and effective timestamp are not established;
zero quotes do not prove free usage. They are displayed separately from editable
conservative budget guard rates. Importing registry metadata does not replace
saved guard rates, alter reservations/settlement or rewrite historical bills.
Missing prices remain unknown rather than silently authorizing free calls.
Explicit finite zero quotes may be selected as zero; they are not rejected by a
positive-price-only form. Nominal micro-USD per-million quotes are converted once
to USD per million, with exact model/gateway/source identity.

An optional configured cached-input rate applies only to valid measured cache
tokens at response settlement. Pre-send reservations assume uncached input and
maximum output; absent or malformed cache counts do not earn a discount. Saved
model settings bind the cache rate to their model and input/output tariff. Usage
events retain the applicable rates and cache treatment, without repricing prior
events or ledger rows. Buffered responses carry the original provider usage
snapshot through SDK completion events, including absent usage; normalized SDK
defaults cannot supply missing cache measurements. Account-observed rates remain estimates, separate from
the provider's actual bill and the dated administrator catalog.

`GET /api/billing` may read a private dated `gateway-billing.json` beside the
database with source URL, verification time, period, scope and total/model charges.
The model key cannot access the signed-in billing endpoint; the app does not
export browser cookies. Without a snapshot, billing is unavailable and links to
the original dashboard. Per-model observed USD/total-token averages can estimate
matching complete raw-normalized workspace usage only with matching saved gateway
identity. Duplicate response identities are not priced twice. Unknown origin,
usage or calibration is excluded; an entirely unpriced workspace is not zero cost.
Legacy turn-only history is unpriced, not proof that no calls happened. The
projection labels its account-average basis/date, does not invent per-demo bills,
and never rewrites events, recordings, guard rates or the ledger. Settings keeps
Model, Cost & billing and Appearance, with guard/connection/account/team details
collapsed; replay summaries omit local cost estimates.

The same billing endpoint also exposes a per-request evidence projection using
shared `nominal_rates`/`request_cost` rules. Correctly scoped request bills take
precedence when supplied; saved configured rates and dated nominal quotes remain
estimates. Valid cache reads use their own evidenced tariff. Missing cache rates
do not earn an invented discount; unsupported cache-write semantics prevent a
claimed full upper bound. Request bills, rate estimates, account averages and
unsettled reservations remain distinct, with usage and price coverage disclosed.

## UI, recordings and private data

### Evidence projections and navigation

The live API includes health/snapshot/events/run detail, messages, Stop/Continue,
fault controls and `/ws/events`; WebSocket origins are checked. Health reports
runtime busy/model status and recorded spend where available. Laboratory forms
for film/material reports are live-only and preserve their admission checks.

Progress renders one tool row per call, with the originating job nested beneath
its submitting tool and Steps/commands beneath that job. Wait/query calls do not
create duplicate jobs. Top view has one outer card, heading and Expand; the nested
map fill/stroke is removed only in its embedded Progress view. Expanded/Lab maps
retain their geometry. Lab Status uses category navigation, middle item cards and
right details; category changes clear stale selection while siblings remain visible.
The bench map is the explicit Overview category, not a permanent material list.

`PlanBook` retains all accepted guidance, selections and layouts by result event
ID; singular fields are latest aliases. Results category selection opens its entry
list even for one entry, and an entry opens right details. Saved device photos,
layout geometry and layer order reuse thumbnail cards. Job diagnostics stay in
explicit Progress links rather than report/history summary badges. Reactor details
preserve saved observation/source/preparation evidence and consumed-input history;
no objective magnification or new observation is invented.

Overview replay uses the shared SVG LabState renderer labelled simulated saved
state; image-bearing inspections freeze it at their exact observation event and
keep original pixels accessible. Microscope views retain captured pixels.
No guessed timestamp pairs an image with state. Pane changes are immediate;
Problems occupies a full-width row above the status bar with wrapping text.

### Failure/recovery presentation

UI-derived ToolAttempts scopes refusals to agent, active plan Step, tool and target.
Accepted matching submissions resolve tool refusal; physical retry requires its
returned job to succeed. A later accepted plan proposal may retire that agent's
refused proposal, and admitted delegation may retire the same role's prerequisite
refusal, without claiming physical completion. Failed/pending jobs and final done
markers cannot erase unrelated failures.

A successful job retry must begin after failure and match flow/parameters within
project, plan revision, Step and agent. An unknown-region inspection correction
may substitute only supported invalid names while retaining all valid original
regions, camera/options; an all-invalid request is not guessed. Safety violations
and waiting-human jobs are not cleared by this projection. Original failed jobs
stay failed; stopped inner actions are not shown running. Problems separates
unresolved entries from recovered history and links its evidence.

A refused physical-plan alteration may recover without a new edit only when the
unchanged contract passes checked delegation and an exact successful linked job;
owner, operation, arguments and outcomes must still match. Seeking reconstructs
all these projections from the event prefix, never the final outcome. Original
errors, jobs and recovery evidence are retained and no projection executes work.

### Export, time and storage

Replay player, Conversation and History derive lab elapsed time from the same
bounded event gaps: the longer of simulated device and wall time, excluding epoch
jumps. Original session time is displayed separately and timestamps are unchanged.
Preparation/exposure/acceptance/recovery navigation comes from events; seeking
before acceptance preserves the earlier unaccepted state. Waiting on an old replay
does not advance physics or generate colder historical readings.

New exports include `application_source_sha256` over packaged source, prompts and
configuration plus the separate base `git_commit`; this identifies the application
even before its final acceptance commit. Provider/executed model/request/usage/
composition fields are additive within the existing recording format. Historical
exports require no backfill. Top-level discovery excludes `recordings/archive/`;
archived recordings remain accessible through explicit developer paths. Only
scrubbed simulated exports belong in `recordings/`.

Private current work is under `runs/workspace`, diagnostics under `runs/checks`,
and historical raw evidence under `runs/archive`; the budget ledger remains
`runs/openai-budget.db`. Keep private `runs/layout.json` with data and ledger.
It maps old absolute roots and relative moves without rewriting database/events,
image paths, sessions or recording schemas. `core/run_paths.py` resolves relocated
images for serving, export, Picker validation and model attachments while retaining
containment/image-type checks. Reverse mapping preserves original per-run budget
scope. Malformed manifests fail explicitly; fresh clones need none. See
[private run-data contract](run-data.md).

### Current Hall-bar recording contract

Source, physical observations, run databases, keys, account snapshots and budget
ledgers remain private. The current version-3 Hall-bar campaign is implemented
by `scripts/record_hallbar_demos.py` as hooks into `record_three_demos.py`.
It reuses the normal SDK, ToolGateway, JobService, RunStore, recorder, attempt
registry and SpendLedger; there is no second laboratory controller. Historical
version-1/2 campaigns and their evidence remain readable and retain their own
limits. Historical releases do not authorize the current campaign.

The common initial state contains tapes, six blank source chips in existing
board slots and the supplied eight-electrode target, with no prepared flakes or
preselected pair. Seed, full simulator configuration, source, prompts, skills,
protocol, initial truth and advice library are frozen before execution. Only
seeded library creation/update timestamps are omitted from advice comparison;
scientific content, provenance, votes and non-seed timestamps remain bound.
Simulation cannot contribute real laboratory learning.

The fifteen-cell plan contains five models and three scenarios: repeated
recoverable faults, the same initial prefix followed by a human-required device
failure, and a simpler fault with progressively refined operator instructions.
Only `astra-complete` may be released after local acceptance. The remaining
fourteen cannot execute through this adapter and require owner review plus
new budgets. Faults bind to distinct-source search submissions; acceptance
requires actual fault consumption and subsequent same-job recovery, or the
corresponding honest human escalation. Terminal acceptance independently checks
actual lineage, complete source polygons, design pose, all eight electrode
contacts and whole-graphene hBN coverage. A success message is not evidence.

The first Astra attempt has a cumulative estimated USD100 ceiling including
qualification, failed sends and retries. Finite request/token/time guards and
retained unknown-usage reservations apply across attempts; missing ledger rows
block admission. Qualification uses the same normal SDK/ToolGateway and budget,
with at most two sends, USD1 and 300 seconds inside that ceiling. The route is
`openai/gpt-6-astra`; administrator rates are estimates, cached-input discount
and account multiplier are unknown, and provider access remains unverified
until qualification succeeds. These guards do not constitute an invoice guarantee.

Every HTTP admission rechecks the frozen identity and cumulative allowances.
Before the first send, the serialized request allowance can atomically increase
its existing pending ledger reservation; campaign and qualification checks count
only the increase. Retry sends retain separate rows. Recorder pacing and retry
observers attach once to each client, including the client rebuilt after qualification.
Failed attempts remain intact; an explicit replacement uses a new workspace
and never resets spending. Local acceptance must have actual command outcomes
and hashed evidence bound to the source identity; readiness flags alone are
insufficient. All validation runs locally, with automatic workflow triggers
disabled. Only the accepted, scrubbed simulated export enters the current
History delivery catalog; previous recordings remain clearly historical.


### Linked stamp discovery and source restoration

Fresh linked Hall-bar submissions bind a validated copy of the actual composed
bench's discovery policy and require a typed device design. Caller policy cannot
replace bench configuration; retained parameters must match the same bench.
Four fixed low-power observations establish a conditional clear domain around
the actual nominal holder head. The fine approach retains original captures,
precision encoder readings and the complete command interval. Post-candidate
focus, monitored hold and clear withdrawal share one bounded work budget.

Discovery and source restoration are separate checkpoints. The original contact
candidate remains immutable; a parked native20 source revisit supplies its own
focus reference before the clear-height holder returns and reaims once in X/Y.
The initializer's original overview handoff capture is retained as a narrowly
bound engagement receipt. It does not admit unrelated camera or motion commands.
The shared pure authority in `records/discovery.py` is used by live pickup,
retained execution and recorded audit. It reconstructs source return, reaim,
chronology and budgets from original events; cached ready assertions cannot
replace those events. Live adapters additionally verify saved image bytes.
These conditional synthetic observation rules are not measured polymer optics,
real-laboratory experience, or evidence of successful material transfer.

### Conditional observed release (staged implementation)

Fresh explicit PC Hall-bar hBN/Gr and sole-Gr direct-contact contracts use the
versioned observed-loaded-support release policy. Generic legacy three-layer and
mica plans retain their original v2 release behavior; old held runs are not
promoted. One final native20 complete-load observation follows bounded autofocus,
with every exposure charged to the existing 600-observation, 900-second and
8 mm phase budget. The shared
segmented SafetyGate controller descends once within the unchanged upper-reference
minus 0.2 mm floor, stopping only at original conditional support evidence.

The SIM-only public support profile assumes a single filled convex contact and
complete-outline recognition. Alpha 0.001 bounds only the declared Gaussian
centroid translation for Kmax=2/N=1, not identity, class, film integrity or transfer.
Both-frame opaque Gr influence is independently reconstructed, never inverted;
opaque interiors remain unobserved. A fixed 175.2 µm collar and 59.630917558 µm erosion
apply to strict visible cells in the recognized inner-front domain. Profile,
focus/capture/encoder/trajectory/load receipts remain bound to original bytes.
Missing geometry, unsupported profiles, visible damage or exhausted reserves hold.
Mechanical safety and final observed target appearance/geometry are independent;
this staged contract does not establish actual-process acceptance or real optics.

The pure journal validator in `records/release_support.py` is shared by live
release control and exported-record scoring. It derives elapsed time and total
Z variation from original captures and the complete command/encoder interval;
recorded summary values are comparison targets, not independent evidence. Only
explicit display-only attachments are excluded from optical publication checks.
Their encoder packets remain evidence, and every analysis exposure still needs
its immutable image publication and phase binding.


Fresh typed Hall-bar submission also recomputes a separate pose-window admission
sidecar against the configured synthetic endpoint window and shape cap. This does
not alter historical DeviceDesignV1 parsing/canonical digests. The exact typed
sidecar is frozen in original job.submitted and one pre-motion lab.evidence
receipt; saved execution and record scoring revalidate its original binding.
Absent historical sidecars remain unscreened history, never backfilled. A nominal
old diagnostic design is not fresh-submission acceptance or a robustness claim.

On 1 November 2026 the owner changed `refind_flake.centre_tolerance_um` from
6 to 2 micrometres, then explicitly selected 1 micrometre translation and
1 degree rotation through the subsequent owner relay. Fresh design screening
and flake refinding share `centre_tolerance_um`; design rotation uses
`vision_control.residual_rotation_deg`. These are acceptance tolerances,
not changes to simulated stage noise or measured mechanical precision.
RGB boundary measurement uncertainty is an additional, separate budget; a
nominal source pair passing the pose window alone is not RGB acceptance.
Historical designs, records and running contexts retain their original bindings;
the previous 6- and 2-micrometre source-pool results do not certify the new setting.

The opt-in `v1-reference-material-capture-v1` image profile rasterizes the scene
at 3560 by 2360 native pixels, with the v1 reference pitches of 0.365, 0.182
and 0.036 micrometres for 10X, 20X and 100X. The raster dimensions are a declared
SIM choice, not a verified physical v1 sensor size; frozen v1 reads its camera
resolution from the SDK. Pitches follow v1 autosearch and flake repository;
its separate 0.183-micrometre 20X refind constant still requires an explicit
cross-scale integration decision. Focus/preview exposures retain that
physical field at their separately declared smaller raster. The profile does
not resize earlier images. Native recognition checks the actual raster,
objective, signed calibration, field, camera response and observation condition.
Frozen v1 LAB tables remain unchanged; native-only background support and
recovery-ROI guards are explicit adaptations, not real-camera calibration or
byte parity with the entire frozen LiveSegment implementation.

Selected original labels and derived binary masks remain separate immutable
artifacts. Exact horizontal pixel-cell unions preserve concavity, components
and holes; geometry limits refuse excessive inputs without dropping pixels.
The resulting region represents the selected mask, not a certified physical
boundary. Recovery or frame clipping remains unsupported even when that mask
has a valid polygon representation.

`perception/measurement.py` owns the conditional SIM boundary-qualification
disposition. Its current native profile is explicitly unqualified and supplies
no numeric bound. A positive caller-supplied error, hash, status or analytic
fixture cannot qualify it. New region/native-RGB admission requires an actual
qualified method and capture-domain binding; retaining a legacy convex schema
does not bypass that requirement. Raw image/mask/region evidence may still be
preserved and evaluated. Fixed-fixture errors are evaluation evidence only;
they neither certify future sources nor improve mechanical precision.

The R024 boundary-measurement work keeps the original v1 label/selected-mask
chain intact and adds a distinct RGB-derived measurement. Original masks are
material identity/interior seeds, not asserted physical outer boundaries.
The new result must bind the original image, seed, public calibration, method
and optical dependencies, and preserve separate derived/uncertain pixel masks.
Pixel membership must follow image evidence; uniform contour offsets, filling
holes by policy, truth outlines and favourable contour registration are excluded.
Successful raster measurement and continuous physical-boundary qualification
are distinct dispositions. Neither success nor a finite holdout maximum grants
the latter automatically.

This new measurement uses the renderer's corner-origin sampling convention:
pixel index `(column,row)` has centre `(column+0.5,row+0.5)`, and exposure
coordinates `((column+0.5-W/2)*sx, (H/2-row-0.5)*sy)`. Historical original-mask
geometry uses its existing integer-index convention and remains unchanged.
Comparisons report that historical origin bias separately from segmentation
error rather than absorbing it into a larger uncertainty budget.

`measurement.measure_recognition_boundary` is the explicit saved-evidence
entrypoint. It validates the original chain, hashes and decodes the same input
bytes, calls the independent raster estimator, and stores separate immutable
pixel maps and a source/dependency-bound JSON receipt. Its new
`region_exposure_mm` uses the corrected absolute exposure frame. The existing
recognition region, default application analyzer and source/design admission
are unchanged; this evidence operation cannot self-qualify a physical bound.

The separate `seeded-rgb-chroma-interval-raster-v2` method learns dominant
foreground/background chromatic differences from original seed-interior and
frame-border RGB samples. The declared SIM camera's shared additive noise cancels
in those differences; public vignette gain and RGB truncation determine the
compatibility intervals. Only channel pairs observable in both learned classes
are used. A clipped unused channel is not treated as an exact measurement.
Connected foreground must have a closed classified background boundary;
uncertain boundary pixels, clipping and unsupported captures return unknown.
This preserves image-visible holes and permits recovery of classifier holes only
where the original RGB supports foreground. No palette inversion or physical
shape is an input. Uniform-colour, visibility and camera assumptions remain
explicit; real-camera accuracy and arbitrary subpixel geometry are not certified.

The opt-in `hallbar-linked` SIM profile composes one versioned bench geometry
after reactor layout composition and before surfaces, devices and sample seeding.
Both maps, camera, flows, arm transforms and recorded layouts consume that same
bench. It uses the frozen v1 paired 520 mm arm linkage and 14 shared slot positions
(seven autonomous), with explicitly estimated outer-board widths and an
owner-authorized provisional 450 mm SIM reach. Profile-only safety XY anchors
move with their source frames while Z, dimensions, clearances and interlock
semantics remain unchanged. The complete synthetic reactor group moves with the
right storage board; local reactor mechanics and physics stay unchanged.

A profile/layout/safety signature and pre-seeding workspace admission reject
historical checkpoint overlays; previous/integrated/hallbar profiles and recorded
layouts retain their original identities. Unknown donor geometry keys or changed
world anchors are refused before coordinate conversion. The everyday launcher
and first future Hall-bar campaign use a fresh linked workspace/config revision,
preserving all historical data and spending rows. This geometry is not physical
calibration or process acceptance.

Native RGB measurement anchors use `native100_derived_centroid_chip_axes_v1`:
the measured 100x raster area centroid is the origin and the original observed
chip rotation supplies the axes. The 20x mask remains immutable identity
evidence; fitted cross-scale translation never moves the 100x material body.
Changing a geometry origin requires the compensating placement translation,
so the placed physical region is invariant. The native revisit producer records
both measurements and a `microscope.native_source_anchor` event. The record
validator rejoins original same-job rotation/capture events and reconstructs
the anchor from immutable measurement receipts. These events establish an
auditable coordinate binding, not a qualified physical uncertainty bound.
Missing observed rotation or unsupported measurement remains blocked.

Measured-region Designer diagnostics preserve components, holes and all
imported electrode metal through native KLayout export/readback. Their
`diagnostic_only` disposition cannot create an executable DeviceDesign.
The independent recording scorer now supports complete region geometry;
truth is used only for terminal scoring, never source selection or planning.
Geometry, source admission, full-chain acceptance and model recording remain
separate gates.

The owner's subsequent 2 November engineering acceptance explicitly treats
the existing 1 um spatial allowance as inclusive of measurement, registration
and positioning errors, with the existing 1 degree rotation requirement.
`measurement.owner_error_budget_policy` records this assumption separately
from scientific calibration, which remains unqualified. Historical native source
observations remain reconstructible from their original completed capture/anchor
journals as `rgb_native100_measured_region`; masks remain immutable. Following
the later original20-mask decision, active source selection rejects that derived
geometry and requires a fresh normal20X observation instead.
Design screening applies the inclusive allowance once. Shape comparison may
use the same 1 um allowance but cannot add it again to the pose reserve.
Contact support and terminal checks retain actual region components and holes.

The explicitly authorized first Astra demo can launch after short necessary
startup, budget, recorder and replay checks while scientific/full-regression
acceptance stays false. Its campaign records
`execution_scope=owner_authorized_unaccepted_recording`; no scientific check
receipt is fabricated. Actual model calls, faults, recovery and terminal
failure/success remain recorded through the existing engine and shared ledger.
