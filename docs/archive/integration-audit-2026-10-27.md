# Integration audit — 27 October 2026

Audited application revision: `86d11804a2b0ded2fa588ffc10a3457bc205b29d`.
**The initial release failed acceptance.** Merging the four workstreams and passing
the previous regression suite did not establish an operational integrated
application. This audit confirms 15 distinct issues: seven P1 correctness or
workflow blockers, seven P2 integration/presentation issues, and one P3 wording
issue. Three further boundaries/evidence gaps are listed separately, not counted
as proven runtime bugs. This is a bounded audit, not a guarantee of no other bugs.

**Final repair status: accepted locally.** All 15 findings and three integration
boundaries have code changes and evidence below. Device preparation/approval,
ordinary-stock two-flake reactor processing and the complete changed-instruction
plus fault-recovery workflow passed live-model acceptance on the normal integrated
simulation. A separate camera-loss fixture passed its expected safe-stop gates.
The final suite passed 1010 tests, with 14 preserved-v1 checks separately passing.
The owner authorized completion without a phase-total spending ceiling; local
estimates are not actual provider charges.

The Leader inspected the actual daily server, launchers, source contracts and
recordings, and independently reproduced the old-target and plan-rewrite issues.
The four workstream chats audited the same revision with local diagnostics.
The initial audit performed no application fixes, paid inference, hardware
operations or GitHub validation. The owner subsequently authorized repair of
all 15 issues and completion of the integration boundaries. The findings below
describe the audited revision; repair evidence is tracked separately until
final reacceptance. All original runs, records and spending history are retained.

## Confirmed issues and closure criteria

| ID | Priority | Confirmed behavior and impact | Required closure evidence |
| --- | --- | --- | --- |
| A01 | P1 | The actual restored daily `chip_e01` lacks `electrode_geometry`. The new Device Guidance gate refuses it, while fresh test fixtures have the field. Restore repairs photos but supplies no trusted-geometry upgrade/input path. | Restore a copy of the actual old workspace; resolve dimensions from verified matching simulation design or explicit trusted input. Preserve photos, records and RNG state; prove guidance works after reopening. |
| A02 | P1 | Describing the same target as “existing Au electrodes” instead of “prepatterned” and omitting target fields bypasses geometry. A revision can erase an accepted continuous footprint without approval. | Bind geometry to a structured target and ledger facts, independent of wording. Preserve/revalidate requirements on revisions, including omitted fields and equal-area incompatible shapes. |
| A03 | P1 | A layout approved against `chip_e01` can authorize `stack_device` targeting `chip_002`. Policy and parameter resolution do not enforce target continuity. | Reject target mismatch before any job/reservation. Retargeting must revalidate guidance, selection and layout. The diagnostic checks admission/resolution; it did not execute a wrong transfer. |
| A04 | P1 | `update_plan` can replace a new pending physical step with `done` and an empty outcome list. The production gateway accepts it while the chip remains bare and no job exists. | Require the established obligation and execution evidence for new completion; dropping conditions cannot certify the old task. Preserve legitimate historical completion separately. |
| A05 | P1 | Mixed job outcomes can return `ok=false, status=done`; delegation finalization then writes `done` without checking sample outcomes. A job-service fixture reproduces an uncertain chip on the transfer stage despite a required storage outcome. | Gate every transition to done on captured/current outcomes, including mixed succeeded/parked/running jobs. Add actual flow acceptance after the isolated branch regression. |
| A06 | P1 | Reactor can complete “react, cool and return to storage” after product acceptance alone. The valid-plan probe finishes with the chip in `reactor_port`, heater target 820 °C and flow target 200 sccm. | Require declared final sample/process conditions, or an explicit intentional-hold state. A recovery task must remain incomplete until targets are off, access conditions met and the carrier stored. |
| A07 | P1 | Ordinary reactor loading parks for all eight tested seeds 0–7, including default seed 7. Three fail slot seating; five insert but fail optical localization. Focus searches Z only, and Reactor cannot recover the reserved cold job with its exposed tools. | Bounded XY localization and a checked specialist recovery route under the actual default noise profile. Preserve slot/visibility guards; demonstrate both successful correction and an honest unrecoverable stop. Eight deterministic cases are not a population failure-rate estimate. |
| A08 | P2 | The everyday launcher starts the original two-arm bench, with no UI route to the reactor workspace. The visible bench says “not built yet.” Opening this as the integrated release was a delivery/discoverability failure. | Provide a clear supported everyday entry/selection and workspace identity; preserve the existing compatible database. Do not silently apply a new layout to its old checkpoint. The optional composition itself is deliberate, not a missing code merge. |
| A09 | P2 | Adding “Do not exfoliate” to a placement-only delegation triggers `advisor_first`, causing unrelated advice queries. The accepted recovery recording contains this behavior. | Determine the intended operation rather than matching prohibited words. Negative instructions must not add exfoliation prerequisites; actual exfoliation must retain its recipe checks. |
| A10 | P2 | The supported Chat Completions transport drops tool-returned images during SDK conversion. Actual outgoing mock-HTTP requests contain text only; Responses includes the image. | Preserve correctly associated images in supported messages or explicitly refuse/label image-dependent Chat mode. Test buffered/streamed final payloads. Current daily Responses operation is unaffected; no Qwen deployment was tested. |
| A11 | P2 | Rotating a seated reactor carrier by 1° leaves input pixel coordinates and the PNG unchanged. The camera binding rotates the center but not the polygons/view axes. | Apply the camera-frame transform to inputs, overlap and product before clipping/rendering. Preserve physical area and source geometry; test off-center anisotropic fields. |
| A12 | P2 | An out-of-focus reactor frame retains crisp polygons and exact synthetic conversion. Normalized observations correctly withhold the product, but the saved image still presents unavailable detail. | Make lost focus visibly unavailable/degraded, or explicitly separate a truth-model diagnostic from measured camera evidence. Preserve rejection of unobservable products. No false ledger acceptance was reproduced here. |
| A13 | P2 | Reactor replay combines a 12-minute lab timeline with conversation/history timestamps of `0:00`; its successful reaction and product acceptance have no key-moment navigation entries. | Explain/use the appropriate clock consistently and add reaction/product/recovery navigation from saved events. Preserve original timestamps and images; test seeking before and after acceptance. |
| A14 | P2 | The audited README says both “integrated” and “not merged,” cites an old reactor revision and a standby instruction. Current-state documentation is contradictory. | Reconcile current guides and local entry instructions against actual capability/status; preserve dated handoffs as history. An audit-status banner alone does not close the full documentation sweep. |
| A15 | P3 | Seeking a measured recording before its first model request says “No request lengths were recorded for this run.” History still contains its 69 points. | Distinguish no request yet, no model calls, and genuinely missing historical measurements. Verify Home, midpoint, end and complete History. |

P1 means a release blocker for reliable workflow/completion claims. P2 covers
supported-path failures or misleading integration/presentation. P3 is a limited
wording defect. A04 and A05 are separate transitions with distinct reproductions;
A06 concerns Reactor's missing required final process contract. Duplicate reports
of the same old-target, loading or recording limitation are counted only once.

## Three boundaries requiring an explicit disposition

1. **Preparation and shared material state.** The current reactor starts from
   seeded prepared pairs. There is no production selection/stacking-to-reactor
   manifest writer or human preparation-report tool. The pair/product exists in
   the ledger/backend but not in the ordinary physical flake list. An adapter
   probe sees no flakes through the ordinary microscope after direct simulated
   relocation; it does not prove an available user transfer route fails, because
   that route is not implemented. The Leader authorized a labelled fixture for
   demonstration, but this audit found no direct owner decision permanently
   excluding broader preparation. Treat it as an unresolved integration boundary,
   not as proof that the owner requested an autonomous preparation pipeline.
2. **Default material profile.** Persistent geometry and independent observation
   RNG are enabled for new worlds, while the improved clustered/contact population
   remains opt-in. The new collaboration recording enables that profile; the daily
   launcher does not. Decide and expose the intended default explicitly, keeping
   historical population/checkpoint compatibility distinct. This is not evidence
   that all material improvements are absent.
3. **Final model-driven acceptance.** No successful new device approval/stacking
   run after the final geometry/shortage fixes is established. The failed paid
   attempt remains valid failure evidence. The new two-flake reactor recordings
   are offline scripted reduced-noise fixtures, with zero model requests. They
   cannot certify model-driven Reactor work, its context chart, or default-noise
   reliability. After fixing blockers, run bounded current-revision scenarios
   with the shared ledger and publish new recordings only if their checks pass.

The collaboration recording also contains a residual scoped-query mistake:
after exfoliation, Hands asks for flakes at a storage-slot ID rather than the
chip ID and reports zero. Its later post-search queries are correctly chip-scoped.
This is observed model adherence/evidence debt within boundary 3, not a seventh
core implementation defect inferred from every optional inventory query.

## Verified behavior and intentional exclusions

- Request length measurement matched independent final-payload calculations in
  20 synthetic HTTP sends across Responses/Chat and buffered/streamed transport,
  with additional retry/session/model-switch checks. The 69 collaboration and
  26 recovery measurements match their original database events exactly. Browser
  replay prefixes and full History are distinct and work for the exercised cases.
- Atomic plan writes/conflicts, role permission boundaries, bounded experience,
  the reviewed three-column Lab interaction and frozen inspection previews have
  targeted passing evidence. These do not override A01–A06 counterexamples.
- Two-input/one-product acceptance, source retention, transaction rollback,
  duplicate prevention, reactor restart, incompatible-layout rejection and
  independent insertion/assembly motion have targeted passing evidence.
- The actual browser shows the saved reactor product image and two source IDs.
  Camera-loss replay displays an offline microscope, hot residual state and
  unresolved human action, without accepting a product.
- Qwen tokenization/window enforcement was outside the owner's later narrowed
  character-chart request. Designer/KLayout was explicitly deferred. Presentation
  brainstorming was intentionally not implemented. Real hardware and calibrated
  chemistry/yield are not claimed or added as missing requirements here.

## Local evidence

Private detailed reports and scripts are under the project home's
`notes/planning/integration-audit-20261027/`:

- `core-structure/core-structure-audit.txt` and its gateway traces, reproduction
  scripts and geometry/target-policy results: A01–A05 and A09.
- `reactor.md`; raw receipts in
  `v2/runs/checks/reactor-audit-86d1180-1tpisd0d/`: A06–A07. Use the
  `completion-valid-plan/receipt.json`; an earlier invalid no-plan probe is
  retained but is not supporting evidence.
- `materials/audit.md`, `materials/reproduce.py` and `materials/run-gor8yg79/`:
  A11–A12 and the explicitly adapter-only shared-state probe.
- `request-length.md` and `request-length-evidence/`: A10 and A15, wire-level
  measurements, recording/database equality and chart browser checks.
- `v2/runs/checks/integration-audit-20261027/`: Leader's daily target, independent
  no-job plan-rewrite and browser observation receipts. The prior default seed-1
  failure is retained in `reactor-default-profile-audit-20261027/`.

Fresh local checks in this audit: 94 core Python cases, two core browser cases,
40 reactor Python cases and 20 offline JavaScript cases passed; independent
diagnostics reproduced the failures above. These sets may overlap and are not
summed into a new full-suite claim. The modern environment's dependency check
passed. No full-suite rerun is presented as proof of integration. Additional API
spend was **USD 0**; GitHub CI/Actions were not used.

## Order of repair and reacceptance

First close completion and target-binding defects A01–A06. Then close ordinary
reactor localization/recovery A07 and expose the intended everyday workspace A08.
Resolve transport and observation correctness before new evidence recording;
finish presentation/documentation corrections alongside their affected paths.
The three boundaries above need an explicit delivered/deferred disposition.

Reaccept against an identified final source revision using both a fresh workspace
and a preserved old-workspace copy. Include actual model-facing tool sequences,
negative cases, default profiles, restart and UI evidence. Maintain one checklist
linking each ID to its code change, reproduction result and recording where
applicable. The final outcome and evidence are recorded below.

## Repair implementation and acceptance tracking

The owner authorized all repairs after this audit. The table maps the delivered
changes to local evidence; the final workflow results are recorded below.
Private repair receipts are under `runs/checks/integration-repair-20261027/`.

| Issues | Implemented change | Local regression and acceptance evidence |
| --- | --- | --- |
| A01 | Restore only verified original simulation geometry; preserve a marker on display-only repairs; support cited user geometry input. | `test_integration_core_contracts.py`; preserved daily-workspace copy restores and reopens without changing photos, RNG or historical events. The missing-field case was reproduced on the copy because the source already had geometry by the follow-up check. |
| A02–A03 | Bind Device Guidance and Layout to the structured target and geometry; reject mismatched stacking before submission. | Guidance omission/wording and target mismatch cases in `test_integration_core_contracts.py`; accepted device v5 completes guidance, approval, stacking and storage. |
| A04 | Preserve unfinished physical obligations across plan replacement; code owns completion; user-cited cancellation/retargeting retains history. | Deletion, new ID, owner change, weakening, repeated rewrite and natural-language retarget regressions in `test_integration_core_contracts.py`. |
| A05 | Check captured completion conditions and every distinct unresolved run; persist step/job links across delegations and restarts. | Independent follow-up originally found same-flow masking. Leader's separate 15-case recheck now refuses parked/running/failed masking and preserves genuine same-run camera recovery. |
| A06 | Typed final reactor disposition requires fresh measured targets/readbacks, successful chip/project-bound evidence and actual storage or a deliberate stable hold. | Hot/foreign/stale/uncertain evidence and cross-step false completion regressions; accepted reactor v5 verifies cold storage and ordinary-microscope reinspection. |
| A07 | Bounded seating corrections, measured XY acquisition and same-job recovery. | Eight default-noise load seeds plus camera recovery and bounded invisible-input refusal in `test_reactor_acquisition.py`; no universal yield claim. |
| A08 | Everyday integrated profile, separate saved workspace, explicit workspace title and preserved previous-workspace launcher option. | `test_daily_profile.py`; actual daily server switched to the integrated workspace and restarted with unchanged samples/world, no model calls and restored simulation identity. |
| A09 | Explicit operation and positive-intent fallback distinguish prohibited exfoliation from requested work. | Placement-only negative-instruction regressions; all three faults recovered in one default-profile search in accepted collaboration/recovery v6. Earlier v5 failure is retained below. |
| A10 | Associate tool images correctly in Chat requests; measure the final converted transport payload. | Buffered/streamed wire fixtures in `test_request_metrics.py` and `test_chat_tool_images.py`; no Qwen deployment claim. |
| A11–A12 | Apply camera-frame rotation; hide unavailable focused geometry/conversion; preserve physical source/product identity. | `test_material_state.py`, rendered focus/rotation examples and default physical preparation/product reinspection. |
| A13 | Consistent lab/session clocks and reaction, product, process and recovery navigation. | `ui_recording_timing.mjs`, browser seek/history checks and visible reactor key moments. Original recordings are unchanged. |
| A14 | Reconcile current launch, architecture, reactor, material and workstream guides; retain dated reports with withdrawn-acceptance labels. | Current guides and the catalog distinguish four accepted current recordings from historical exports; the final collaboration gate passed in v6. |
| A15 | Distinguish no request yet, no model execution and absent old measurements. | `test_request_chart_browser.py`: start, midpoint, end and complete History. |

Boundary 1 now has guarded ordinary-stock preparation, pickup-neighbourhood
assessment, exact two-input stacking, a shared physical reaction product and an
ordinary-microscope return route. Default seed 7 passes the scripted complete
pipeline (`reactor-integrated-preparation-1i64w8aa/receipt.json`). Successful offline
receipts cover seeds 0–7; seed 2 needed more candidate photography and a checked
cold-load recovery. The exact receipt map is in the private
`reactor-implementation/default-seeds.json` handoff. Boundary 2 is
implemented for fresh worlds with persisted population-policy compatibility.
Boundary 3 has accepted real-model device, ordinary-stock reactor and complete
collaboration/recovery recordings under the default integrated profile. Failed
earlier attempts remain separate historical evidence.

## Findings from real-model repair acceptance

Unsuccessful paid integrated attempts remain private failure evidence under
`runs/checks/release/`; they are not featured or accepted recordings. All USD
amounts in this historical attempt log are configured-rate estimates or
reservations, not provider charges.

- `openai-integrated-reactor-20261027-v1`: USD 16.32668. Explicit few-layer
  revisit IDs were filtered by legacy device thickness quotas, returning no
  photos despite a successful revisit job. Explicit eligible IDs now bypass
  automatic quotas; standalone experiments cover all thickness categories.
  A later chip detached from the tape and remained honestly parked.
- `openai-integrated-reactor-20261027-v2`: USD 13.33374. One source chip produced
  nine photographed candidates; optical neighbourhood assessment and guarded
  two-flake preparation succeeded under normal seed-7 conditions. Completion
  correctly refused a plan that confused carrier `reactor_pair_prepared` with
  input-flake `stacked`. An unnecessary shutdown then invalidated earlier
  process readings. Known-flow plan admission, narrow legacy role/location corrections and fresh
  guarded verification of a stored carrier are implemented. Independent review
  checks that future reaction and inspection obligations remain incomplete;
  another fresh autonomous run is still required.

Both attempts stopped within the normal USD 20 run cap, including conservative
next-request reservations. The same persistent ledger retains their costs.
These failures extend the integration work; passing unit tests alone does not
close final autonomous workflow acceptance.

- `openai-integrated-reactor-20261027-v3`: USD 15.19470. The real model
  prepared and stacked two ordinary flakes, completed the nominal 60-second
  reaction, accepted one product and returned the carrier to cold storage.
  The final ordinary-microscope inspection was not performed: the next request's
  USD 5.05 reservation exceeded the remaining USD 20 run allowance. Invented
  physical operation names also left redundant plan obligations. This run is
  preserved as incomplete evidence, not an accepted demonstration. At that point the normal
  per-run cap became USD 30 (later USD 40, as recorded below); the combined incremental delivery cap at that time remained
  USD 200, including all failed attempts and unresolved spend reservations.

At that earlier stage, the shared cumulative ledger cap was USD 292.31486: the historical USD 92.31486
baseline plus this delivery's USD 200 allowance. The old rows are retained.
The aggregate per-run token guard is 2,000,000; the USD cap still controls
spend. This aggregate guard is unrelated to a single request's context window.

- `openai-integrated-reactor-20261027-v4`: interrupted by Leader after
  repeated empty revisits on unscanned chips. Recorded model usage had an estimated cost of USD 10.11726;
  the retained in-flight reservation makes the ledger commitment USD 10.89294.
  A revisit-only call accepted an exfoliated chip with no scan or candidates,
  marked it searched and misled the model into preparing another chip. This
  is a flow-admission defect, not evidence of missing material. The remaining
  paid cases did not start. Original events, checkpoint and reservations remain
  private and unchanged; a scan-provenance admission guard is required before
  another paid acceptance attempt.

- `openai-integrated-reactor-20261027-v5`: all physical preparation, reaction,
  acceptance, cooling, storage and ordinary microscope inspection passed under
  normal integrated settings. The final report/plan closure stopped at the
  USD 30 reservation gate after USD 25.90354 actual estimated use. The default
  workspace cap is now USD 40. Only the three budget-default package files
  changed; physical code, prompts and completion rules did not. The same saved
  run resumed, rechecked the product, completed its plan and passed every
  recording gate at a cumulative USD 29.05824. The recording explicitly retains
  the budget stop, operator continuation and both source fingerprints. All
  10,986 earlier events and 120 earlier media files match the pre-resume export.

The resume route accepts only a budget-only stopped reactor run whose physical
evidence checks already pass. It rejects parked work, incomplete physical
conditions, model/driver failures, changed setup and missing source identity.
It appends to the original database/timeline and budget scope; it never resets
spending or rewrites earlier events. Per-segment source fingerprints distinguish
the tested physical implementation from the later budget configuration.

- `openai-integrated-device-20261027-v4`: USD 15.33560. The default integrated
  run completed guidance, three-chip exfoliation/search, valid selection, layout
  review, operator approval, stacking on `chip_e01`, final microscopy and storage.
  All model turns completed. Acceptance still failed because `in_storage=true`
  on the flake itself was checked only against direct storage slots, ignoring
  the real containment chain `flake -> chip_e01 -> right_chip_1`. The plan
  correctly remained pending under that old checker. The repair must verify
  typed, certain carrier containment without deleting the original condition;
  a new fresh device run will establish the final integrated result.

- `openai-integrated-device-20261027-v5`: **accepted**, USD 16.53534,
  87 outgoing requests. Fresh ordinary integrated stock, seed 7/default noise,
  real model guidance/selection/layout, scripted operator approval, successful
  stacking and saved final microscopy. All ten plan steps are closed. The
  flake/device storage check follows a known certain chip carrier to its
  configured slot, while preserving the original condition. Browser inspection
  confirms the device, source flake, target, photo and no outstanding human work.

- `openai-integrated-collaboration-20261027-v4`: USD 12.14138, **not accepted**.
  Eyes and Advisor treated an unreviewed simulated seed label as requiring a
  physical human confirmation before any simulated preparation. Only two bench
  inspections ran; no materials or images were fabricated. This exposed missing
  simulation-identity provenance and ambiguous simulation-versus-real wording.
  The failed run remains private. Its repair must keep unreviewed human labels,
  require trusted simulation provenance and reject missing/conflicting identity;
  it must not turn simulator defaults into laboratory certification.

- `openai-integrated-collaboration-recovery-20261027-v5`, first segment:
  USD 3.42986. The simulation identity repair worked, but a draft physical step
  had sample IDs only in its title, before structured completion conditions
  were added. The retarget authorization checker overlooked those IDs and
  rejected the explicit instruction to preserve `chip_001` and use `chip_002`.
  Its generic error also confused stale plan versions and malformed replacement
  arguments with missing user authorization. The recorder now stops immediately
  if the first material is not actually prepared, photographed and stored,
  preventing follow-up scripted reviews from presupposing nonexistent results.
  This prefix is retained for a same-workspace continuation after the specific
  repair; it is not evidence of successful preparation by itself.

The continuation of that same v5 run remained **not accepted**. Cumulative
estimated cost was USD 14.02242 across 63 outgoing requests. Graphene preparation
and full search succeeded, and the empty pickup, serial timeout and placement
correction all occurred in the same successfully completed search job. Its
original events remained unchanged. However, the model had added an impossible
`location=left_chip_2` completion condition before exfoliation, whose flow
allocates a middle-storage slot. The immutable-obligation guard correctly
refused changing it after execution. The search advanced the sample status to
`searched`, so neither changing the location nor retroactively marking the old
exfoliation step done would be valid repair. hBN preparation then stopped at
the shared budget reservation gate. The failed run and both source segments
remain private; they are not promoted as a successful collaboration recording.

The final repair therefore belongs at plan/strict-delegation admission: expose
the real storage endpoint contracts, reject incompatible fixed locations before
execution, and retain any genuine user-specified fixed-slot requirement. The
recorder also requires all physical plan steps to be closed before advancing
to the next scripted material request. No old conditions or physical history
are rewritten to make this attempt pass.

At the earlier pause, the local ledger calculated USD 196.56848 for this
delivery, including an unsettled reservation. That number uses configured
token rates, counts cached input at full price and is **not actual provider
spending**. The owner corrected the cost interpretation, reporting about
USD 140 total charges and estimating about USD 30 for this phase, and explicitly
removed the phase-total ceiling to finish acceptance. Those provider figures
are owner-reported; this audit has no independent billing access. Historical
ledger rows and failed-run estimates remain unchanged. Only the stage-total
gate is disabled locally; ordinary per-workspace, request and safety guards
remain. Repository commit and push remain gated on complete acceptance.

## Final local repair boundary

The final storage contract exposes dynamic allocation and same-station fallback
for exfoliation, search and revisit. It rejects unsupported exact chip slots
before a new plan is saved and blocks strict delegation from incompatible old
plans. A physical draft cannot freeze an exact destination before choosing its
operation. Ordinary `in_storage` goals still execute, including actual slot
obstruction, camera failure and same-job recovery. Explicit user fixed-slot
requirements remain requirements: the system reports that the current flow
cannot promise them. It does not delete them or rewrite earlier completion.

The actual failed v5 arguments were checked on private database copies; the
original database and failure history remain unchanged. The storage repair's
345 related local tests passed, followed by 34 independent admission checks.
Before the stage-only budget option was added, the full suite passed **984
tests**, with two v1 dependency skips in the
modern environment, in 419.03 seconds. The separate preserved-v1 environment
passed all 14 v1 checks. After the full-suite source was frozen, only two
explanation strings changed to accurately describe search storage behavior;
65 focused checks passed on that final source. No execution or safety logic
changed after the full-suite snapshot. Source manifests and all verification
receipts are retained under `runs/checks/integration-repair-20261027/`.

The everyday server uses the integrated profile and the separate saved workspace.
Actual restarts preserve its samples, world and project content, apart from the
ordinary reopening timestamp. Saved simulation identity is recovered from the
matching saved world, without inventing human confirmation. The original daily
169-event history, original 943 budget rows and `runs/layout.json` are preserved.
All checks run locally; GitHub Actions and remote validation were not used.


## Final integrated acceptance

`openai-integrated-collaboration-recovery-20261027-v6` passed all **26 evidence
gates** from a fresh ordinary integrated workspace, with seed 7, default noise
and morphology, no prepared-pair fixture, and the ordinary USD 40 workspace
protection. The explicitly authorized stage-total gate was disabled; no ledger
history was cleared. The run made 56 measured live-model requests. Four operator
messages were scripted test inputs through the normal inbox.

- The mid-turn instruction preserved `chip_001` and retargeted preparation to
  `chip_002`. Superseded plan history remained recorded.
- Graphene on `chip_002` and hBN on `chip_003` were actually exfoliated, searched,
  photographed and returned to `mid_chip_1` and `mid_chip_2`. Nine flakes have
  saved images; exactly two chips were prepared.
- Serial timeout (event 450), empty pickup (464), and placement correction (489)
  occurred in the same successful search job `run_14708d78d4d9`, with subsequent
  physical recovery and verification. Fault injection alone was not accepted
  as evidence of recovery.
- All ten final Task Plan steps are done. The final review instruction produced
  no new experiment, no stacking and no fabricated device. No model errors,
  unfinished jobs or safety violations remained.
- Manual browser review verified the saved hBN image, two stored chips, nine
  flakes, ten completed steps, zero unresolved problems, all 56 request-length
  measurements, and the empty chart at the playback start. History presents four
  current demonstrations separately from eleven historical examples. Browser
  error/warning logs were empty.

Independent read-only review also passed with no blockers. It compared actual
events and private originals: nine candidates and eighteen microscope images
match, both prepared chips are stored, all three injected faults recover in the
same job, and no tool/device activity follows the final stop instruction.

The execution source fingerprint is
`5b6e0eddc14a8fd23a7d21b3848047aa777e42d405edd60f72e7d325428fd1a7`.
The later source differs only in the presentation catalog entry for this accepted
recording; no execution, prompt, physics, plan or safety logic changed. Earlier
accepted device/reactor recordings retain their actual execution fingerprints
and continuation segments; they are not relabelled as fresh runs of this source.

Final local verification: **1010 passed, two modern-environment v1 dependency
skips**, 420.12 seconds; **14 passed** in the preserved-v1 environment. Ruff,
dependency checks, recording/media checks, source preservation and browser
acceptance passed locally. The daily launcher was restarted on the accepted
version: samples, world and project content were unchanged apart from the normal
reopening timestamp. The original 169 events and 943 budget rows were checked
against their pre-repair copies. No GitHub CI/Actions or remote tests were run.

The new run's configured-rate estimate is USD 11.339; this is a diagnostic
estimate, not a provider charge. Owner-reported provider spending and the earlier
cost-interpretation correction remain recorded above. Raw databases, failed
attempts, credentials and the shared ledger stay private; only scrubbed simulated
recording exports are included in the repository.
