# Reactor owner review: local workflow repair

Current follow-up: the ordinary device-simulation pipeline is now implemented.
See **Ordinary material-metal device simulation** below. Earlier abstract-only
limitations and frozen handoffs describe their historical snapshots. Physical
hardware and chemical calibration remain outside this software acceptance.

This work addresses R01–R10 from the private owner review. Historical recordings,
run databases and spending ledgers are retained. The current owner-selected
example is **Gr/Pd**, assembled as hBN / Gr / Pd / silicon. It is an example for
this experiment, never a general material default.

## Implemented contracts

- Eyes reads unified stored instrument observations, including reactor readings
  and explicit unknown readiness. Capability/operation rules belong to planning
  and Reactor; Eyes cannot call `get_reactor_setup`.
- `refine_plan` edits existing stable IDs, allocates new IDs for additions and
  merges current progress under an immediate transaction. Structural revision
  conflicts remain errors. Full `update_plan` retains its version check. Nested
  checked writes use savepoints without committing the outer operation.
- `experiment.requirements` is append-only evidence outside mutable project
  execution state. It preserves the direct user request and explicitly adopted
  Advisor response. Ordinary plan changes cannot replace it. Goal revisions cite
  a later user instruction, reason and previous requirement event; history remains.
- Plan coverage retains preparation/search (or photographed existing inputs),
  Picker screening, Hands local checks, Picker confirmation, Hands assembly and
  Reactor reaction. Input validation checks actual material identities, top-first
  order, saved images, source chips and human-prepared metal-coating provenance.
  An all-done or empty plan cannot certify a legacy Gr/Gr result as Gr/Pd.
- Plan saves and strict delegations share a prerequisite report. Exfoliation
  needs all six recipe values and material in `operation_args` before Hands is
  admitted. Active execution cannot silently replace that recipe.
- The recorded duplicate tape application was not caused by `num_massages`.
  Original device events 407/462/465 applied, peeled and reapplied on chip_001;
  268/320 concerned mother-tape loading. New heated exfoliation keeps the single
  application through controlled heating, then cools and peels once. Massage
  count and safety/temperature verification remain.
- Picker has a standalone screen/confirm tool independent of Device Guidance.
  Hands owns local assessment and preparation tools. Candidate queries expose
  saved images and source-chip batches; the existing assessment flow handles
  all candidates on a chip before returning it, including screened backups.
- `delegate_reactor` accepts the structured objective as part of its task dialogue.
  The gateway binds it to the current requirement event, verified preparation,
  source identities, coating provenance and final disposition. The standalone
  `set_reactor_pair_goal` is hidden from the OpenAI tool roster and retained only
  for historical scripted fixtures; it rejects established material-metal tasks.
- Problem recovery matches prerequisite refusals to the same project/step/role.
  Budget recovery requires the same SDK session/project, a subsequent user turn,
  successful execution evidence and a successful resumed turn. Original errors
  and evidence links remain. Playback only uses the current event prefix.

## Stamp identity and literature boundary

A person's PC/mica report is saved as `stamp.props.support.film_type` with source
instruction and history. The temporary support is separate from the final stack.
Identity recording does not make a used/torn support fresh. Existing explicit PC
film data remains readable; unknown or mica support cannot silently enter PC
melting. Coating records similarly identify human-prepared metal on silicon,
its preparation reference and preparer; Qumus does not deposit metal.

Babich et al., *Polymer-free van der Waals assembly of 2D material heterostructures
using muscovite crystals*, Nature Communications 17, 6017 (2026),
[DOI 10.1038/s41467-026-72554-x](https://www.nature.com/articles/s41467-026-72554-x),
was checked using the full indexed 10-page Zotero PDF (PYZCHSPR / IHFTDQ7M).
The paper replaces PC with PDMS-supported mica or uses free-standing cantilevers.
Its standard hBN-based pickup uses 50–90 °C; standard release uses 120–180 °C and
a bottom hBN flake with stronger adhesion. An alternative flips and breaks the
cantilever, leaving mica as a substrate. Reported low-humidity adhesion decreases
with increasing temperature. These are source-specific protocols, not a universal
adhesion law or a calibrated recipe for this bench.

Neither published route establishes direct release onto Pd while preserving the
owner's four-layer order. The implementation does not add bottom hBN, retain mica
in the final sample, claim a calibrated Pd release temperature, or add PC cleaning
to mica. The ordinary simulator now uses explicitly uncalibrated, configurable
transfer assumptions to exercise this order. The old reaction model consumes two
flakes and therefore cannot model the owner's nonreactive hBN; it remains for
legacy tests/playback. Real material-metal execution and chemical acceptance need
calibrated transfer and chemical-observation adapters. The normal device simulator
and earlier abstract fixture each preserve their separate evidence scope. This repair does **not** claim Gr/Pd chemistry,
physical mica release, or completed end-to-end material-metal acceptance.

## Local evidence

`tests/test_reactor_review.py` covers immutable requirements, stale structural
edits versus progress merging, goal revisions, all-done false acceptance, recipe
readiness, role boundaries, Picker handoffs, provenance-bound objective capture,
known stamp support, transaction rollback, single tape application and batched
local checks. `tests/ui_review.mjs` replays the original R10 events and checks
unrelated successes and earlier playback prefixes. The synthetic objective test
uses explicitly constructed ledger evidence; it does not claim physical assembly.

The focused recording is [Protect the experiment; apply tape once](http://127.0.0.1:8765/?rec=reactor-review-20261027-v2),
exported to `recordings/reactor-review-20261027-v2`. Its 422 events include actual
simulated bench inspection and heated Gr exfoliation through the ordinary gateway,
job service and SafetyGate. All 12 recording checks passed, including one tape
application, two preserved massages, stored source chip, rejected scheme drift,
merged progress, refused premature completion, intact media and no API usage.
The simulator reported no safety violation across 221 allowed device commands.
The deliberate hBN/search/assembly/reaction omissions remain pending.

Browser review at the final event showed one active problem (premature project
completion) and two recovered refusals, with original and recovery entries
expandable. A private screenshot is retained at
`runs/checks/reactor-review-20261027-v2/browser-final.png`. The export contains no
absolute private paths. Its execution and export source fingerprint match:
`d761182f1ab9ddd1b714c2743a0a899b8922c5945560c1d532d2a44d8d746202`.
This binds evidence to its execution state, not to later concurrent edits.

Local validation:

- Ruff and `git diff --check`: passed.
- Initial existing SDK checks: 24 passed. Final SDK, stamp, owner-review and UI
  wrapper checks: 48 passed in 3.94 seconds; includes the previously failing
  request-chart wrapper after its owner updated the shared expectations.
- UI event tests: 25 passed, including original R10 budget/delegation recovery,
  unrelated-event exclusions and refinement recovery.
- Frozen v1: 14 passed in 6.15 seconds using `.venv-v1compare`.
- Full-suite run during concurrent context edits: 1020 passing markers, one
  request-chart failure and two skips. The chart failure subsequently passed in
  the focused wrapper run above. This is not a completed full-suite acceptance
  of the final combined worktree.

Private logs and `runs/checks/reactor-review-handoff.json` retain exact scopes.
Shared `openai_runtime.py`, architecture/workstreams and `tests/ui_review.mjs`
were released to the context workstream; that workstream owns subsequent edits.
The leader must validate the final integrated state. No API calls, remote
validation, push or physical hardware operation were used.

Final broader regression: `reactor-review-final-targeted.log` records 269 passed
and one Node/PATH skip across eight affected test modules. The skipped inventory
replay test was rerun with bundled Node and passed (one passed in 0.11 seconds;
`reactor-review-ui-material-replay.log`). All owned source edits are finished;
`reactor-review-final-evidence.json` records local artifact hashes and limitations.

## Positive material-metal adapter pipeline

The follow-up separates a positive software path from unvalidated transfer and
chemistry. `records/material_metal.py` defines typed preparation receipts and
validates job/project/requirement/coating/source identity. The registered
`prepare_material_metal` and `reactor_material_metal` jobs execute the adapter
through ordinary job reservations, checkpoints, observations and recovery.
Reactor task dialogue binds the objective automatically; its result tool records
`reactor_workflow_result` separately from chemical experiment results. A revised
requirement invalidates current goals/results while retaining all audit events.

The available `synthetic-material-metal-workflow-v1` adapter is an **abstract
software fixture**. It maintains its own persisted model state and is restricted
to simulated benches and an explicit project instruction authorizing that exact
fixture. It never relocates sample rows or SimWorld objects, marks a stamp used,
consumes hBN, chooses a Pd recipe, or infers product chemistry. Original catalogue
images retain their source role; they are not relabelled as release evidence.
Reactor cold-storage conditions are measured separately through SafetyGate.

[Verify the material-metal software handoff](http://127.0.0.1:8765/?rec=material-metal-workflow-20261027-v1)
contains 10,494 events and passes 14 checks. Its Gr/hBN exfoliation, catalogue
search, source photographs, batched local assessments and Picker confirmation
actually run on the ordinary simulator. The subsequent adapter jobs exercise
abstract pickup/release state, goal capture and result acceptance. They verify
workflow success while preserving all physical sample locations and denying
chemical experiment completion even with all declared software steps done.
Execution/export source fingerprint:
`e881a709608f61c56534f2bc241302cfa379a4319ce66ad21692f2ffa153fee3`.
The export is `recordings/material-metal-workflow-20261027-v1`; no API or physical
hardware calls were made. This is positive software evidence, not a physical
material-metal end-to-end recording.

Five new adapter test cases cover PC/mica, complete positive job/dialogue/result
flow, unavailable physical/unauthorized adapters, mismatched provenance and an
interrupted release resumed under the same job without early preparation claims.
The final affected contract/SDK/report suite passed 262 tests; the final adapter
and owner-review suite passed 20. The earlier request-chart failure has been
corrected by its owner. Full integrated acceptance is still the leader's task.

### Remaining software and process work

The generic software pipeline and abstract adapter are complete. A device-backed
transfer adapter and material-metal optical/other observation backend are still
unimplemented; the positive fixture does not substitute for their physical or
simulated-device integration. Existing SafetyGate, transport and stacking
mechanics provide reusable parts once the following operation contract is set.

The scientific gaps are narrower: (1) a validated direct-to-Pd release procedure
with contact, temperature, retraction and release-success criteria while keeping
hBN/Gr/Pd/Si order; (2) the intended measurable Gr/Pd endpoint and acceptance
criterion. The cited mica paper and the owner's current Gr/Pd selection do not
supply those. No Pd recipe, product identity or conversion value was invented.
These are the minimum process questions for a device-backed implementation;
they do not block the completed abstract software regression.

The four released shared files remain frozen in this workstream. The leader
should update their architecture/workstream summary to distinguish the now
available positive software fixture from the still unavailable physical and
chemical validation, then run final combined-worktree checks.

Final browser verification showed all ten declared software steps complete, the
accepted abstract workflow receipt, and one active problem for the deliberately
refused chemical completion. The source chips remained in storage; the Reactor
microscope had no fabricated micrograph. The private screenshot is
`runs/checks/material-metal-workflow-20261027-v1/browser-final.png`. Both scrubbed
recordings pass `check_recording`; final Ruff and whitespace checks pass. The
temporary replay server was stopped. All files owned by this workstream are now
frozen for integrated acceptance.


## Ordinary material-metal device simulation

The normal `prepare_material_metal` and `reactor_material_metal` tools now default
to `qualitative-material-metal-v1`; no internal fixture marker is required. The
older explicit abstract protocol remains supported and cannot claim device motion.
The normal path requires the current immutable material/metal requirements,
reported metal-on-silicon provenance, confirmed top-first hBN/reactive inputs,
actual local optical assessment and known PC/mica support.

Hands routes the narrow carrier through the Arm3/Arm2 handover, loads the stamp,
refinds and inspects each source, picks hBN then Gr, releases on Pd/Si, verifies
both full outlines with a new microscope image, cools, returns the carrier and
stores the used stamp. The world model responds to guarded manipulator contact;
flows never move simulator objects directly. Samples change ledger location only
after the matching optical postconditions. Mica stays on its stamp in the default
qualitative model; it does not melt or become an added final layer. PC and mica
have separate recorded transfer assumptions. They apply only to the admitted
material-metal job, preserving historical device-stack behavior.

Reactor task dialogue freezes preparation, original requirements, final disposition
and the quantitative **software** progress criterion. One Reactor job performs
load, insertion, bounded optical acquisition, controlled exposure, saved images,
shutdown, cooldown and return to storage (or a declared cooled observation hold).
Only guarded device commands move the chamber/rod; the existing SafetyGate limits
and Clock remain authoritative. Failed observations disable targets. Interrupted
exposure retains uncertainty and cannot be replayed by the bounded recovery tool.
Failed release retains source flakes on the stamp and cannot create preparation.
Reversible failed placements retain their original source for measured re-picking.

`MaterialMetalSimulation` records all uncalibrated assumptions: distinct PC/mica
pickup/release setpoints and probabilities, release control band, support retention,
activation controls, characteristic exposure time, success probability, stalled
progress ceiling, optical availability and acceptance threshold. Overrides live
under `reactor.material_metal` in the testbench profile. Profiles are validated,
snapshotted in preparation and results, and exposed by `get_reactor_setup`.
The defaults are chosen for software exercise; they are not a Pd recipe or
values established by the mica paper. A stable seeded trajectory can progress or
stall. Exposure time comes from actual simulated controls and elapsed Clock time.
Repeated camera captures do not advance a reaction independently of that time.

The material backend preserves both input identities and geometry, including
nonreactive top hBN. It renders a labelled qualitative interface contrast change
without inventing product composition, conversion, electrical quality or yield.
Out-of-focus, clipped and unavailable observations do not provide measurable
progress. The result checks the original requirement revision, successful jobs,
source/coating/geometry provenance, frozen goal and profile, actual observed image,
progress and fresh terminal device feedback. `reactor_simulation_result` is
separate from `reactor_result` and the older `reactor_workflow_result`.
`simulation_accepted=true` may complete a simulated task while
`experiment_accepted=false` and `chemistry_verified=false` remain explicit.
Real benches cannot use this model.

The UI has a separate Material-metal simulations report showing the recorded
layer order, known support, release image and current observed progress. It does
not present those carriers as old two-flake products. Existing token-usage views
are unchanged. New sample properties are exported for replay; original recordings
remain intact.

Two private diagnostics exercise ordinary stock, default parameters and noise:
the default seed 7 stalls, finishes safe storage and is correctly refused
completion; explicitly selected seed 8 meets the simulation goal. Each diagnostic
passes 17 checks. Seed 8 is not presented as default-seed success.
These are scripted software evidence, not paid/live-model or hardware acceptance.
The final test/artifact scopes and hashes are maintained in the handoff JSON;
the leader owns final combined-worktree/default-model acceptance and recording.

### Normal tool path and integration inputs

A fresh `simulation_profile()` includes this protocol. The model success
probability remains 0.9; the diagnostic does not alter default noise, transfer
probabilities or reaction failure probability to obtain a passing recording.
Seed 8 is a reproducible positive diagnostic, and seed 7 a reproducible stalled
diagnostic. These seeds identify evidence, not a guarantee that arbitrary runs
will succeed. Separate deterministic test profiles explicitly set success to 1
only where a test needs to inspect downstream acceptance and persistence.

An ordinary starting request for the leader's live-model acceptance is:

> In the simulated lab, prepare and run hBN/Gr/Pd/silicon. hBN must stay topmost
> and nonreactive. stamp_01 carries mica. reactor_chip_01 already has Pd deposited
> on silicon, preparation pd-sim-01 by simulation-operator, and is in reactor
> storage. Use ordinary source stock, retain images and provenance, meet the
> configured qualitative simulation goal, and finish cold in reactor storage.
> This is simulated workflow acceptance; real chemistry remains unverified.

PC can replace mica in the reported stamp attribute. The preparer and preparation
reference are explicit human inventory inputs. No fixture-authorization phrase,
magic marker, source-flake seed or manufactured preparation receipt is required.

The ordinary role/tool sequence is:

1. Qumus establishes the requirement and saves the reported stamp support and
   metal coating. Eyes inspects the bench; Advisor supplies complete recipes;
   Qumus records recipe arguments and stable task outcomes before delegation.
2. Hands prepares/searches source chips. Picker screens photographed candidates
   with backups. Hands performs source-chip-batched `assess_reactor_inputs`;
   Picker confirms exactly two actual candidates, hBN first.
3. Hands calls `prepare_material_metal(chip_id, flake_ids, stamp_id)` and waits
   for verified release and safe carrier return. Omitted protocol arguments
   select the ordinary guarded simulation.
4. Qumus conveys the current requirement revision, carrier and cold-storage
   disposition in the `delegate_reactor` objective. Reactor inspects
   `get_reactor_setup`, runs `reactor_material_metal(chip_id)`, waits, and calls
   `submit_material_metal_result(chip_id, reaction_job_id)`.
5. Successful simulation acceptance permits final project completion. A
   completed but stalled job remains an unmet result. A parked reversible step
   can use `recover_material_metal_job` with its original job ID and observed
   reason; interrupted exposure and uncertain release cannot be replayed.

The diagnostic's explicit source recipes (Gr 50 C, hBN 90 C, dwell 30 s, two
massages, depth 0.2 mm, peel speed 200 and angle 30 degrees) are scripted simulation
inputs, not new literature claims or required live-agent choices. The normal
Reactor defaults are 300 C / 50 sccm / 180 s under the labelled qualitative profile;
they are uncalibrated software controls, not a validated Gr/Pd process recipe.

### Final workstream evidence

`material-metal-device-contracts-final.log` records 371 passing affected tests
and two failures in a newly added test that incorrectly expected camera
reverification to perform focusing. The production behavior was correct: a fresh
unfocused image keeps the job parked. The test now verifies that refusal, then
uses the same original job's reversible optical retry. The final focused run
passes all 14 PC/mica device cases and the UI wrapper (15 passed, 31.99 s;
`material-metal-devices-final.log`). This covers actual assembly, accepted and
unmet goals, preserved geometry after restart, pre-exposure recovery, interrupted
exposure refusal, failed release, evidence tampering and camera-independent
progress. The UI wrapper runs three event-scope tests.

The earlier 292/1 prompt-role failure was fixed by identifying the delegated
operation as `Hands.prepare_material_metal` in the orchestrator prompt. Role
permissions, roster and the existing assertion were not weakened. The broad
run and a final isolated role-contract check both pass that test
(`material-metal-role-contract-final.log`, one passed).

Fresh positive and stalled exports each pass 17 checks. They contain 13,861 and
13,533 events respectively, with matching execution/export application hash
`bdcb7644ecea9eae77b36571c694f83e268103c0b07d05cecd1a4e1c4b2f6e17`.
Both live privately under `runs/checks/material-metal-devices-final-*`; neither
replaces the leader's final live-model/default-seed recording. Browser review of
the final positive export confirms the layer order, known mica, source IDs,
saved release and Reactor images, 96% qualitative progress, safe carrier storage
and zero active problems. No percentage is claimed as chemical conversion.

Ruff and whitespace checks pass. All owned source files are frozen for leader
integration; no workstream test or replay process remains active. The four
released shared files remain untouched. The authorized material-metal sections
of `results.js` preserve token views. The handoff lists exact files, normal tool
inputs and outstanding leader-only combined checks. No paid model calls,
hardware operation, remote validation, commit or push occurred.

### Live-run search-plan repair and stamp display follow-up

This follow-up supersedes the earlier source freeze. The first leader-run live
default-seed recording completed all eight device jobs and accepted 95.9%
qualitative interface progress, but its project remained unfinished: s8 and s10
incorrectly required the hBN/Gr raw carriers to have chip status `searched`.
The actual receiving chips were chip_001 and chip_002. This live choice of flakes
differs from the earlier scripted seed-7 stalled diagnostic; neither recording
has been relabelled or overwritten.

The plan/tool changes address the observed failures:

- `refine_plan` reports existing, unknown, missing and duplicate order IDs plus
  invalid explicit addition IDs together. Its earlier add-first/reorder guidance
  is superseded by the atomic insertion follow-up below.
- New or changed search outcomes reject carrier-as-chip roles before saving;
  delegation and search execution also check the actual target. An unchanged
  legacy error may remain as a non-executable draft while another step is fixed.
- A narrowly evidenced correction preserves the step ID and every other
  completion condition. It requires one successful same-project exfoliation
  output, matching material and `from_carrier`, certain unreserved inventory,
  and matching original linked search jobs. It refuses missing or ambiguous
  evidence, fixed locations/operation targets and human-target conflicts. The
  free-text check is intentionally conservative: a direct human mention of the
  old source ID also prevents automatic reinterpretation.
- Corrections retain the original condition/contract, source and target IDs,
  preparation and original search job IDs, reason and prior plan version.
  They remain pending. `Hands.wait_job` observes the original successful search;
  the delegation check, not a model-written status, establishes completion.
  Wrong-chip and foreign-project search jobs cannot certify the step.
- Delegation facts and events return the current structural revision and
  execution version after updating progress. Hands instructions and legacy tool
  descriptions point directly to `prepare_material_metal` for this experiment.

The copied-ledger diagnostic in
`runs/checks/search-outcome-live-ledger-recheck/summary.json` uses the original
production ToolGateway and job-status implementation, with zero workers and no
lab. It corrects s8 then s10 separately and verifies original jobs
`run_734f4678db1a` and `run_dad540c9d7f3`. Revision/version move from 3/25 to 5/31;
16 audit/tool/plan events are added only to the private copy. All original job
records are identical, with zero new jobs, device commands or API calls. The
source database/WAL hashes are unchanged. The final reporting step s6 remains
pending and no project completion is requested. This is a bookkeeping recheck,
not a new physical run or complete integrated acceptance.

Stamp availability (`used`) is now independent of support presence. Bench labels
read current ledger `props.film`, including old replay frames that omitted film,
and show `used (support retained)` for retained mica. Explicit absence shows
`used (no film)`; unknown film stays `used`. Render caching includes film changes.
The ordinary bench, enlarged bench and Lab map have exactly three authorized
ledger lookup connections. Historical status text no longer infers film removal
from `used`. No recording events or token/model-settings logic were rewritten.

Validation: the first affected run had 296 passes and one outdated assertion
expecting the old legacy-tool wording. The assertion now checks the direct
correct-tool guidance while retaining its refusal checks. The final focused run
passes 59 tests in 4.21 s, including 24 search-repair cases and the wrapper for
five Node UI tests. Real simulated hBN and Gr source/search chains cover
correction both before search and after its original successful job. Negative
fixtures cover ambiguity, provenance, human targets, condition weakening and
cross-project/target job reuse. The earlier broad run also passed both PC/mica
device paths and SDK/flow/integration contracts.

Browser verification of the unchanged original replay confirms the final stamp
label `used (support retained)` and its original six Problems. The saved
accessibility record and screenshot are under the copied-ledger diagnostic
folder. The temporary tab is closed; the leader's replay server is untouched.
Targeted Ruff and whitespace checks pass. Current hashes and freeze timestamp
are in `runs/checks/reactor-review-handoff.json`. Leader still owns integrated
closure and final recording; this follow-up makes no chemical-acceptance claim.

### Exact job-reference recovery and closure3 evidence

The subsequent live closure exposed a different failure: Qumus read the correct
successful hBN job `run_734f4678db1a` at events 14997/14998, but omitted the last
character in its Hands delegation at 15003. Hands queried the truncated ID at
15007 and correctly received an unknown-job error at 15008. The earlier lookup
at 14955 failed in the same way. JobService had the original job; these were
model transcription errors. The old software response, however, incorrectly
suggested submitting a job whenever an exact lookup failed.

`unknown_job` now preserves an error result with `requested_job_id`, and supplies
only nested `known_jobs` from the current real delegation's step and project.
Each reference includes its exact ID, flow, current status, target parameters,
original link event IDs and last status event. No active or uniquely identified
delegation means no inferred candidates. The run's own project is checked in
addition to its link. No fuzzy matching, automatic retargeting, submission or
completion is performed. Candidate IDs never appear as top-level `job_id` or
`run_id`, which the existing gateway interprets as observed execution evidence.
The model must explicitly use an exact ID for a separate read and pass the
normal completion check.

The gateway's existing read-only `project_summary` adds exact linked records for
the unique current delegation, including successful original jobs. The unchanged
SDK wrapper already refreshes that summary before starting a specialist. This
fix needs no shared runtime, token UI or model-settings changes.

Closure3 is explicitly operator-assisted: a scripted operator supplied the
correct exact ID, and normal reads/checks completed all twelve steps and the
project without new physical jobs or device actions. That proves the assisted
closure, not fresh autonomous recovery. The earlier events remain preserved.

The Problems projection had four stale entries after closure3. Its narrow
recovery rules now establish the following links:

| Original event | Required later evidence |
| --- | --- |
| 14918, refused project completion | Same-project `save_project_status(done)` call/result 15066/15067 |
| 14939, search-outcome prerequisite | Same-step delegation 15044 and successful completion check 15055 |
| 14955, first truncated lookup | Original job link 525, corresponding failed delegation 14971, later delegation 15044, exact read 15048/15049 and completion check 15055 |
| 15008, second truncated lookup | Original job link 525, corresponding failed delegation 15014, then the same later exact read/check evidence |

Truncated-reference association is a display-only inference: the full original
ID must be the unique prefix extension already linked to that project/step at
the failed lookup, with matching flow. The completion contract must remain the
same, the failed delegation must be recorded, and the later exact read must be
successful and explicitly cited by that step's successful code check. A matching
string, successful unrelated job, project done marker, new same-name job, or
successful read alone cannot clear an unknown-reference issue. All original and
supporting event payloads remain expandable in the Problems entry.

The private read-only replay check records four active / eleven recovered before
the fix and zero active / fifteen recovered afterward. At event 15049 all four
issues still remain; at 15055 only project completion remains; at 15067 all four
have their own evidence. The original closure3 JSON SHA-256 remains
`0c5d8579d575ee5c546a3bc646ace94a47695c74458ef490614dfa170d1c8d7a`.
See `runs/checks/job-reference-r10-evidence.json`.

Final targeted regression: **80 passed in 4.01 s**, covering new exact-reference
tests, prior search correction, agent tools, SDK integration and existing UI
event suites. The new independent Node suite contains 33 cases, including
ambiguous/missing links, cross-project/step/role reuse, changed conditions,
wrong or failed reads, absent/failed completion checks, and preservation of
unrelated errors. The first run's three failures were test fixtures treating an
integer event ID as an event object; those expectations were corrected without
changing production behavior. Targeted Ruff and whitespace checks pass.

This follow-up edits only gateway/job evidence, unknown-job guidance, the two
Problems projection files, independent tests, and this report/private handoff.
No full suite, API call, hardware operation, commit, push or recording mutation
was performed here. Leader reported 1104 passes/two dependency skips and 14 v1
passes before this follow-up; those results predate these edits. The latest
source is frozen for Leader's next integrated/default-run acceptance.

### Atomic prerequisite insertion after the successful fresh v2 run

Leader's fresh default real-model v2 run completed all eight jobs and twelve
steps autonomously, with the project done and no operator correction. It remains
valid simulated-experiment evidence. Its six planning refusals at events 166,
171, 176, 181, 186 and 191 revealed a remaining R02/R07 interface gap before the
full-plan fallback saved revision 2 at event 201.

The earlier advice to add steps, obtain their allocated IDs, then reorder them
could not construct this valid plan. `refine_plan` appended all additions after
the existing Reactor step, while the unchanged experiment sequence guard
required local assessment and confirmation before assembly and Reactor. Attempts
175/176 and 190/191 isolate this defect: the required operations were present and
the sole coverage error was their order. Other attempts also contained genuine
recipe, outcome or missing-operation errors; those are not all attributed to
insertion. The original v2 recording is preserved with its planning history.

New additions now accept either `before_step_id` or `after_step_id`, referencing
an existing stable ID. Existing steps first follow the optional full `order`,
then additions are inserted at their requested anchors. Additions sharing one
anchor/side retain input order; unanchored additions still append. Both placement
fields together, unknown/empty anchors, references to predicted new IDs and
explicit addition IDs are rejected with indexed errors and current valid IDs.
There is no semantic auto-reordering. New IDs are allocated only within the
existing atomic validation/save path, and successful responses expose
`added_steps` alongside the full plan. Placement metadata is not persisted as
part of a step's physical contract.

For the recorded plan, source preparation/search additions can use
`before_step_id: "s5"`; local assessment and confirmation can both use
`before_step_id: "s6"`, in that order. This constructs the valid sequence in one
call without predicting a new ID. All edits and insertions must be supplied
together: rejected submissions save none of their changes. Error guidance and
the orchestrator prompt now state this explicitly. Registered tool schemas
expose the fields automatically; no runtime change is needed.

The regression fixture retains only the recorded initial plan at event 159 and
the exact refinement arguments at 175 and 190. It does not import run databases,
images or the full conversation. Each regression first reproduces the unchanged
unanchored sequence refusal, then adds only placement fields and saves once.
Existing IDs and newly advanced progress survive, new IDs are unique and never
reuse retired history, and all original requested step fields and immutable
requirements remain intact. Future search outputs remain unbound and therefore
non-executable until real receiving chips are known. No job or device action is
performed by these plan tests.

The final focused run passes **95 tests in 5.96 s**, including 13 new insertion
cases plus search repair, owner review, agent tools and SDK regressions. Negative
cases retain sequence, layer-order, required-operation and physical-obligation
guards, while additional cases cover invalid anchors, group order, existing-step
reordering, structural conflicts, progress merging, historical ID allocation and
schema exposure. Targeted Ruff and whitespace checks pass. Evidence and the
unchanged source recording hash are in
`runs/checks/plan-atomic-insertion-evidence.json`.

Only `agents/tools/project.py`, the orchestrator prompt, new insertion tests and
their minimal fixture changed in this follow-up. Shared runtime, token UI,
sequence/chemistry guards, physical flows and recordings were untouched. No full
suite or API run was started here; Leader owns the running baseline and subsequent
integrated acceptance. Current source is frozen and handed off.

## Recorded v3 plan mechanics and original-job completion

The subsequent autonomous v3 run did not reach Reactor. Its eight hardware jobs
all succeeded, including preparation `run_7eb25db35985`, but the plan still
required a `bare` carrier after material-metal assembly. Event 15185 refused
the model's attempt to correct that condition; event 18202 then correctly
reported that the successful job could not meet the erroneous condition.
The carrier was stored, both selected flakes were stacked on it, the used
stamp retained mica, and no reservations remained. The later preparation and
assessment refusals concerned those already-transferred flakes, not an
unfinished preparation job. New assessment steps did not solve that mismatch.
V3 stopped at the request-reservation budget gate after 102 requests and an
estimated USD 32.12324, with no Reactor job or accepted simulation result.
The original failed run and export remain unchanged.

The initial s7 was a generic material-metal preparation draft before the
requirements event. The model assigned its incompatible `prepare_reactor_pair`
operation later, after the requirements were established, while also adding
s12 with the correct operation and incorrect `bare` outcome. Correction
authority therefore comes from original requirements plus immutable model plan
history, not an assumption that the legacy operation itself predates requirements.

Plan saving and delegation now reject legacy pair preparation/reaction for an
established material-metal experiment. Modern preparation requires carrier
`material_metal_prepared` and input flakes `stacked`. Modern reaction requires
carrier `reactor_processed`, assigned by the inherited `GrowthWindow.on_success`,
while both flakes remain `stacked`. Process status alone still cannot certify
the separate qualitative goal, observed result, or safe terminal disposition.
The explicitly authorized abstract fixture retains its distinct status semantics.

`records/material_metal_plan.py` permits only evidenced model-authored
preparation-operation and output-status repairs. It preserves the stable ID,
owner, materials, carrier, existing operation arguments, storage/location
conditions and other obligations. Direct human status/operation/target bindings,
ambiguous history, uncertain state and conflicting jobs refuse the correction.
It records the original/corrected contracts, requirements event, direct original
instruction, model plan events and any exact execution evidence. A correction
never marks the step done or changes an executed job or sample.

After execution, the correction requires a unique successful same-project
preparation, current immutable requirements, matching carrier and ordered flakes,
recorded stamp/coating inputs, an original job link, and the independent optical
receipt with preserved placements and source images. Qumus receives this exact
job reference in correction/context results. Hands reads it through `wait_job`;
the normal gateway checks the job identity and current outcomes before completion.
Two corrected duplicate internal entries can each pass that check against the
same original preparation without another device command. There is no generic
merge, cancellation, skipped label, automatic completion, or fixture substitution.

The successful v2 contract also remains supported. Its semantic arguments such
as `top_material`, `reactive_material`, `substrate`, `retain_images` and
`retain_source_provenance` are checked against original requirements and actual
receipt/image/provenance evidence. Unknown arguments are not silently ignored.
An earlier completed preparation remains historical after Reactor advances the
carrier status; reading its job does not reopen it or revert sample state.

The final focused run passed **199 tests with one Node-path skip in 34.36 s**.
The skipped wrapper then passed using bundled Node, including its 33 offline
recovery-event cases. New recorded-plan tests cover v3 rejection/correction,
exact-job reuse, human/target/ambiguity negatives, missing optical evidence and
v2 compatibility. Existing PC and mica device tests now also declare and verify
the actual Reactor terminal status through normal execution and completion.
Targeted Ruff and whitespace checks pass. Evidence is in
`runs/checks/material-metal-plan-evidence.json`; no paid API, full suite, real
hardware, commit or push was run by this follow-up. Leader owns fresh combined
acceptance and the shared architecture/workstream documentation update.

## Leader integration acceptance (28 October, UTC)

V4 exposed a second contract mismatch: the plan admitted semantic aliases for
mica support, metal-preparation identity, image retention and provenance, but
completion rejected them after a genuinely successful preparation job. The
leader completed a shared closed argument schema after the workstream froze.
Plan saving, delegation, public command submission and exact-job completion now
use the same contract. Conflicting, unknown or mistyped arguments are refused
before execution; declared process parameters cannot change at tool submission.
Actual source images, support, coating and original preparation-job evidence
remain required. A scrubbed v4 regression proves the original completed job can
close without repeating any device operation; conflicting and ambiguous cases
remain negative. Reactor setup advertises the same modern terminal statuses as
lab setup. Inspection distinguishes explicitly unobserved storage from an empty
chamber, preserving the requested camera regions.

Fresh live-model v5 completed eight device jobs and eleven plan steps in 340.6 s
on the ordinary seed-7 bench, with one initial operator inventory statement and
no later assistance. All 78 requests retain exact provider-reported usage and
model/limit snapshots. Independent evidence checks confirm hBN/Gr/Pd/silicon,
nonreactive top hBN, mica, source images and provenance, the qualitative target,
heater/gas targets off, and cold storage. The full stable-source suite passed
1,188 cases with two optional dependency skips; isolated frozen v1 passed 14.

The default recording `openai-material-metal-default-20261028-v5` keeps all seven
rejected plan proposals. Five recover through accepted plan corrections; two
attempted changes to s6 recover only when the unchanged obligation completes
with its exact linked successful job and code-written completion check. The
read-only Problems projection retains original errors and recovery evidence.
Cross-project, changed-contract, missing-job-proof and failed-check variants stay
unresolved. This final display correction does not alter the recorded experiment
or require replaying physical jobs or paid model calls.

This is uncalibrated software device simulation, not experimental chemistry or
real-hardware validation. Failed v3/v4 attempts, the assisted original closure,
and earlier recordings remain preserved with their original evidence boundaries.
GitHub is storage only; all validation and browser review run locally.

Review of v5's last two refusals also isolated a preflight false rejection:
optional source IDs were explicitly null, so filling them with the confirmed
Picker pair was treated as replacement of a fixed value. The final correction
treats only those typed material-metal flake identities as unbound, equivalent
to absent fields. It does not relax non-null bindings, chemistry, carrier, support
or process targets. Both exact recorded requests (11677 and11682) now save in
an isolated ledger without new jobs, device actions or sample changes; non-null
retargeting, other operations and heater-off changes remain rejected. The53-case
material-metal plan module passes. Original v5 errors remain in its recording,
which predates this refinement fix and the later retry-diagnostics instrument.
Its completed physical simulation remains the live evidence; recorded-input
regressions verify these final corrections without replaying completed work.

### Follow-up: future outputs and current readiness

Fresh live v6 stopped after bench inspection: the model read whole-plan
`executable=false` as a reason it could not exfoliate, although only later search
steps lacked their future receiving-chip identities. This run is retained
privately as failed acceptance. Planning feedback now scopes `executable` to the
next pending step and retains all future errors separately. Rejected proposals
return the saved plan's current execution readiness. Recorded v6 regressions
preserve missing-recipe/outcome/inspection blocks and required searches.
This change needs fresh full-workflow acceptance; v5 remains earlier evidence.


## Final integrated acceptance: 28 October 2026 UTC

The fresh `openai-material-metal-default-20261028-v7` recording completes all eight
simulated device jobs and eleven plan steps autonomously in 340.9 seconds, using
77 real model requests with 77 exactly associated provider usage reports. All
27 evidence checks pass, with no tool errors or mid-run operator corrections.
The single initial operator statement supplies the known mica support and
predeposited Pd/silicon input; no reaction chemistry is invented.

Evidence preserves top-first hBN/Gr/Pd/silicon, nonreactive hBN, both source IDs,
source/release/reactor images and metal preparation provenance. The configured
qualitative progress reaches 95.9%; measured terminal state is certain, targets
are off and the carrier is cold enough for storage in `reactor_chip_slot_1`.
This verifies the software/device simulation, not real chemistry or hardware.

The actual execution source is
`62ca46bf6df7d99c7a2a4bb3931caa2fa87402beef6095efc87d4df90aa79864`.
The final application source is
`452843b2b0e9cf1957101e14cf04fc5b795f775f20a24f532a0bef89c6bf2b35`;
only the presentation catalog changed after execution to feature this recording.
Original event/media data and source metadata are retained. Earlier successful,
assisted and failed attempts remain separately labelled; none was rewritten.

Final local validation passed **1,223 tests, two optional dependency skips** in
465.27 seconds with the bundled Node runtime. Isolated frozen v1 passed all
14 cases; its source is unchanged. Ruff and diff whitespace checks pass.
The normal root launcher serves the current source. Browser review confirms
11 completed steps, Problems 0, result imagery/layers/support/storage, 77 token
points, five Reactor points, correct recorded used/remaining capacity, Settings
navigation and no console errors. Historical retry evidence remains explicit,
including unknown original causes and usage. No GitHub validation was run.
