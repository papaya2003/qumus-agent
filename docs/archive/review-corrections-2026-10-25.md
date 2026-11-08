# Review corrections — 2026-10-25

Archived on 2026-10-26. Paths and outcomes below describe their original
revisions. Private evidence moved under `runs/archive/2026-10-26/`;
`runs/layout.json` records the exact moves. See [run data](../run-data.md).

This is a chronological evidence log, including superseded intermediate states
and dated test results. Use [local-start.md](../local-start.md) and
[architecture.md](../architecture.md) for current behavior. Earlier failures and
test counts remain evidence for their own revisions, not the latest baseline.

This change addresses the owner's five review findings. It covers the web UI,
agent integration and testbench image output. No real laboratory hardware is
connected. The original migration report remains historical evidence.

## Open and review

Use `~/Projects/Qumus/Start Qumus.command`, then **History → Demo recordings**.
Do not start a separate review website. Each recording contains Results,
Progress, Conversation and Experience, with **Play recording** and key moments.
See [local-start.md](../local-start.md) for Finder and VS Code instructions.

Start with **Graphene device: review the layout, then approve transfer**
(`openai-device-approval-20261025`, the default demo). The initial request is
“Make a graphene device on chip_e01. Let me review the layout before stacking.”
The model searched three spare chips, found a qualifying monolayer, submitted
a checked layout, and waited. The **5:49** key moment is the operator's approval;
the subsequent transfer produced `device_001` on `chip_e01`. All ten jobs
succeeded. Results contains the final 20X device image, alongside the selected
flake's 20X/100X images. Lab → Target chips contains the electrode reference.
The recording includes a recovered tool-policy refusal and a corrected layout
submission; it is not an edited, error-free narrative.

The short-goal collaboration recording is **Graphene flakes: change the chip
while Qumus works** (`openai-collaboration-20261025`). It starts with “Find
graphene flakes for a device. Show me the microscope images.” The acceptance
driver then sends “Keep chip_001 untouched. Use another chip and save the
flakes for me.” Qumus uses chip_002; chip_001 remains bare. Four candidates
have both 20X and 100X images. A fifth is a scan candidate without those photos.
The recording retains a recovered inspection error and an inaccurate model
sentence suggesting that candidate has a saved 10X picture. The ledger is the
source of truth; no such per-candidate photo is claimed by the code.

Operator messages in these acceptance runs are scripted test inputs delivered
through the normal inbox. All model decisions and laboratory jobs execute on
the testbench. These are event replays, not edited videos or canned replies.

## What changed

| Owner finding | Change | Main code |
| --- | --- | --- |
| Duplicate launchers and recording folders | One root launcher; one in-app Demo catalog; older exports, handoffs and chat launchers moved into the dated archive with a move manifest | `scripts/start_qumus.py`, `docs/local-start.md` |
| Review split across separate sites | Removed the review launcher; Demo entries have their own Experience snapshot inside History | `ui/static/history.js`, `ui/replay.py` |
| Long initial request, no collaboration | Short requests, mid-turn constraint and a requested review point; fresh specialist state after reopening a plan step | `agents/prompts/orchestrator.md`, `agents/openai_runtime.py`, `scripts/record_acceptance.py` |
| Missing camera/material/electrode/device images | State-driven overview render, optical electrode fixture, shared saved image evidence for tools and UI, device photo in Results and Lab | `testbench/camera_render.py`, `testbench/optics.py`, `testbench/render.py`, `agents/tools/design.py`, `ui/static/camera.js`, `ui/static/results.js` |
| Two unexplained tool dots | One labelled status glyph: ellipsis, check, correction or warning | `ui/static/feed.js`, `ui/static/style.css` |

Additional defects found during validation:

- History treated every SDK recording as Claude; it now respects provider metadata.
- Configured token-rate estimates looked like provider invoices; the UI now says `est.`.
- Restarted camera writers could overwrite earlier photos; numbering now continues.
- Preview magnification could describe the current turret instead of the displayed photo.
- Camera seeking could accept a stale image load; only the latest request is shown.
- Invalid display names used as camera region IDs caused failed jobs; the query now
  returns the valid IDs before submitting a job.
- A reopened delegation could receive its old “done” state; the summary is now
  refreshed after the step is marked running.
- Repeated images accumulated in model requests and consumed conservative request
  reservations. Requests retain four recent distinct images, preserve user
  instructions and saved history, and explicitly tell the model how to retrieve
  omitted images. Only the latest full project snapshot is sent when that
  read-only query has been repeated; old copies are marked as superseded.
- The agent tool required two flakes although the physical transfer flow accepts
  one. The schema now matches the flow, allowing graphene on pre-patterned metal.
- Successful job results carried nested photo paths but did not attach pixels.
  They now return the final device image first, followed by recent distinct
  observation images. The live recordings precede this final attachment fix;
  the tool-to-image regression verifies the corrected code without another paid run.
- Older saved benches may lack electrode geometry or reference photos. Restore
  now fills only those missing optical fixtures, preserving physical state,
  existing photos and RNG. Saved `renderer=None` cannot disable today's renderer.
- Replay treated jumps between agent and testbench clock epochs as experiment
  duration. Playback now uses the same clock-jump cutoff as History summaries.

## Boundaries

Camera images are synthetic renders of the software bench, marked SIMULATED.
The overview hardware geometry and electrode pattern are illustrative, not
calibrated photographs or manufacturing masks. The existing observation models
still provide simulated detections; this change does not establish pixel-based
computer-vision performance or electrical device performance. A rendered
post-transfer photo establishes the simulated transfer outcome only.

The original 11 demos are retained alongside the two new acceptance recordings.
New code does not fabricate missing photos
inside old recordings. Simulated runs remain excluded from real-lab Experience.

## Validation status

The non-browser suite passed **350 checks with two legacy-environment skips**.
The isolated legacy suite passed all 14 checks. After the saved-bench migration
fix, all 20 image/lab-process/restart checks passed. Coverage includes refreshed
specialist state, one-flake transfer, final device pixels delivered to the
agent, old image-free snapshots, and preserved physical state on restoration.
Python lint, all 22 JavaScript modules' syntax and both environments' dependency
checks passed. A pure JavaScript replay-timing check passed for mixed epochs
and both new exports; this is not a browser or visual test.
All **13** published Demo exports have complete referenced media. All 89 new
PNG files decode, and both new exports pass secret and local-home-path checks.
The launcher was restarted on port 8765 without submitting a model task.

The earlier conversation could not access the browser because its permission
check was denied. The continuation obtained authorized access through its own
Chrome tab and completed the following visual review on the canonical app.

## Browser acceptance follow-up

Reviewed History → Demo recordings in Chrome at 1864 × 1000 and 390 × 844,
including dark and light themes, both new recordings and the older microscope
offline demo. This review used saved events and images only.

| Original finding | Browser result |
| --- | --- |
| Folder and launcher clutter | One active root launcher and one 13-entry in-app catalog; historical files remain archived. |
| Integrated review and Experience | Results, Progress, Conversation and Experience open in History. Experience shows the saved provenance and zero real-lab votes. |
| Short requests and collaboration | Both short initial requests and the mid-turn constraint / approval are visible. The final inventory leaves chip_001 bare and stores the five flakes on chip_002 in the collaboration recording. Operator inputs remain explicitly labelled as scripted. |
| Image delivery | Overview, 20X/100X flake images, electrode reference and final device image load and enlarge. The scan-only candidate correctly has no per-candidate photo. The owner subsequently clarified that demonstrating the image interface is sufficient; realistic bench modelling is not required. |
| Duplicate tool status dots | Expanded tool rows contain one labelled status glyph each; no second grey dot. |

The browser exposed additional defects, now repaired:

- History conversations now have their own Full / Focus control.
- Identical final specialist text emitted twice by the SDK appears once;
  distinct interim notes and the original recorded events remain intact.
- Long reports can expand even when their feed was constructed off screen.
- Results preserves sample IDs and filenames, including underscores, in the
  AI summary. Markdown block boundaries retain spaces.
- Play recording enters Progress at the beginning; key moments enter Progress
  at their recorded position. Reload still restores the playback position.
  Home / End seek to the beginning / end of the timeline.
- An active human review after completed agent work is labelled awaiting review
  and listed under Left for a person. Earlier failed jobs remain in Evidence;
  errors, parked jobs and stops retain priority.
- Experience has a useful empty detail pane and a recorded-snapshot label;
  narrow History views have a Key moments button and working back navigation.
- Electrode cards use the app styling. Images returned by query tools no longer
  inherit a possibly unrelated current microscope objective. Replay copy uses
  Qumus / agent terminology rather than assuming Claude.

Follow-up checks: **352 non-browser tests passed, two legacy-environment skips**;
the isolated legacy suite passed **14 tests**. Ruff and all 22 JavaScript module
syntax checks passed. A browser regression was added, but the automated
Playwright suite was not run in this session; its affected interactions were
checked through the authorized Chrome UI. No application console errors were
observed; two unrelated Zotero extension connection errors appeared on reload.
The original recording exports were not edited.

Screenshots and the offline test log are private, ignored files in
`runs/visual-review-20261025/`. The final live store had zero jobs, instructions
and model-usage events. The phase ledger remained **$88.67538**, unchanged by
this visual review. The local app was left running with the default recording
paused at its final Results page.

## Simpler camera demonstration

The owner clarified that real cameras will eventually supply the images.
The simulation should prove that the camera interface works, without trying
to reproduce the real laboratory's appearance. Realism is therefore not an
outstanding requirement or a reason for additional model spending.

The overview renderer now draws a plain 2D diagram: named station regions,
colored sample markers, arm-position crosshairs, vacuum state and tape state.
Perspective, shaded machine geometry, shadows, texture and sensor noise were
removed. Frames still follow the simulated state and use the existing capture,
preview, saved-image and UI paths. The image is labelled SIMULATED / 2D DIAGRAM.
Microscope images and all historical recording exports remain unchanged.
History shows the original saved pixels; the current live overview shows the
new diagram. No new model-driven recording is needed for this change.

The canonical app was restarted with an empty live task queue. Authorized
Chrome inspection confirmed that Lab's camera preview and enlarged image use
the new diagram. Private evidence is in `runs/simple-camera-20261025/`.
The live store still contains zero jobs, instructions and model-usage events;
the phase ledger is unchanged at $88.67538.
The full non-browser regression suite passed 352 tests with two environment
skips, the isolated legacy comparison passed 14 tests, and Ruff plus the
modern environment's dependency check passed. The existing image regression
verifies that moving a simulated chip changes pixels at its 2D position and
that unchanged state reuses the saved frame.

## Private diagnostics and cost

Raw databases, model usage and images remain in ignored `runs/acceptance/`.
Retired standalone diagnostic exports are in the project-root
`archive/2026-10-25-review/diagnostic-exports/`, with `moves.json` recording moves.

- `openai-collaboration-20261025`: passed programmatic checks, $6.02448 estimated.
- `openai-device-review-20261025`: incomplete hBN/graphene/hBN build; preserved
  budget stops at $15.16638 estimated, never published as a successful demo.
- `openai-device-collaboration-20261025`: reached human review and approval,
  then exposed the single-flake tool mismatch. Including continuation, $16.97590
  estimated; the same $20 scope prevented another request. Never published.
- `openai-device-approval-20261025`: passed the full device acceptance checks,
  $17.14500 estimated; no request exceeded the run's $20 budget.

This correction round totals **$55.31176 estimated** across these four runs.
The shared stage ledger totals **$88.67538**, including earlier migration tests
and retained reservations for two requests without reported usage, below $200.

These are configured token-rate estimates, not Token Station charges. Failed
attempts count toward the same $200 stage ledger; a resumed test does not reset
its $20 limit. Actual billing must be read from Token Station.

## Presentation review: catalog, delegation graph, models and billing

History now recommends two recordings in presentation order: **Build a device
with your approval**, then **Change the plan while Qumus works**. Eleven older
examples remain under **More examples**, each with a complete title and a short
explanation. The launch guide explains when to use each one. Default-selection
metadata is preserved; original recordings and media are unchanged.

The team graph previously drew shared stems and rails once per specialist.
Later neutral paths overpainted parts of an earlier active path. Neutral paths
now occupy the bottom SVG group, active paths/dots the next group, and nodes
the top group. Active paths are opaque and slightly wider. Chrome inspection
confirmed the full Qumus-to-Eyes curve in light and dark themes.

Settings now reads the gateway's conversational model catalog and can persist
a model/rate selection for the next task without inference. An active turn,
pending instruction or unfinished job blocks switching. Positive budget rates
are required; zero metadata for a non-free model is shown as unknown. The next
turn uses a new SDK conversation while preserving accumulated costs, histories
and the bench. An offline SDK test verifies dispatch to a different selected
model, fresh context, persistence and retained spend. This is not a paid model
capability comparison; access and tool/image compatibility remain untested for
the newly listed models.

The gateway's signed-in Usage page was checked directly. Its model key cannot
read billing, so Settings displays a **dated account snapshot** stored privately
beside the live database, with period, scope and a source link. It does not
claim live refresh, balance or per-recording attribution. Actual account totals
are deliberately absent from this repository. Local estimates are retained in
Settings and the ledger, and removed from the footer, demo list, result badges
and turn-end summaries. Replay explicitly says **Playback is free**.

The owner's new mother/daughter tape, repeated-contact, lifted-chip recovery,
stage-axis and microscope-focus facts are documented in architecture.md. They
supersede the older incompatible device assumptions; this presentation change
does not claim that the physical model/flows have already been migrated.

Private screenshots and test output are in `runs/presentation-review-20261025/`.
The live configuration was saved with its original model; no task was sent.
The separate browser regression was updated, but not executed through an
automated browser runner in this review.

Validation: the full non-browser run passed **355 tests with two environment
skips**. After adding model provenance to History/exports, all **16 focused
recording, History and model-selection tests** passed (including one new mixed
model regression). The isolated legacy suite passed **14 tests**. Ruff, the
modern dependency check and all **24 JavaScript module syntax checks** passed.
Chrome verified both recommended entries, the eleven expanded examples, model
form validation/save/restart persistence, read-only settings during replay,
the dated billing snapshot, free-playback footer and the full active graph
curve. The only console errors were unrelated Zotero extension connections.
The live store still had zero jobs, instructions and usage events; the phase
estimate remained **$88.67538**, unchanged. No inference or real hardware calls
were made for this review.


## Default replay review: resolved history, result lists and camera (2026-10-25)

The owner reviewed the first default recording and reported three crosses in
Progress, a persistent Problem, the old 3D overview image, singleton Results
categories bypassing the middle column, job evidence clutter, redundant demos,
and abrupt scenario switches. This follow-up changes presentation/derived state;
it does not rerun models or hardware or rewrite recorded evidence.

- The three rejected submissions were events **98** (Advisor: unsupported
  thickness label), **2655** (Picker: incorrect missing-image explanation), and
  **7356** (Designer: missing contact-metal layer). Successful corrected
  submissions are **102**, **2676** and **7359**. They now display as recovered.
- The policy refusal **5282** cited a plan-step ID as an operator instruction.
  Its accepted retry **5363** submitted `run_8e2ca09c4480`; only that job's
  success at **5499** resolves it. Chrome showed **Problems 1** before that
  success and **Problems 0** at completion, with all four original rejections
  retained under **Recovered earlier**. No final-outcome shortcut clears them.
- Results categories consistently open middle-column entry lists, even with
  one entry. Clicking an entry alone opens right details; switching categories
  clears the previous detail. Device plans and layouts retain every accepted
  record, identified by event ID, rather than replacing the earlier one.
  The overview and History summary no longer show hardware-job evidence/counts.
  Explicit diagnostic links from Progress remain available.
- The replay overview is now a simple SVG diagram using the saved animated lab
  state. Seeking resets it correctly and its enlarged view is readable. The
  microscope resets at the beginning and keeps its recorded final image.
  Original overview evidence remains in the conversation, unchanged.
- History now lists two recommended demos plus five distinct recovery/failure
  examples. Six overlapping or internal demos moved byte-for-byte to
  `recordings/archive/`; see the full decision table in `docs/local-start.md`.
- Scenario/pane changes initially used a cancellable 160 ms slide/fade.
  The owner rejected that animation; the follow-up below removes it.

Validation: **357 passed, 2 environment skips** in the full non-browser suite
(359 collected), including an offline Node test covering eight recovery,
negative-attribution, multiple-submission and navigation cases. The isolated
legacy suite passed **14 tests**. Ruff, diff whitespace checks and all **26**
JavaScript module syntax checks passed. All **13** active/archived recordings
passed referenced-media validation, and discovery returned exactly **7**.
The browser regression definitions were updated for these new interactions;
the automated Playwright suite was not run. Manual Chrome checks covered the
default replay, singleton devices/plans/layouts, clearing stale details,
original recovered evidence, unresolved-before-retry state, seek resets,
enlarged overview, rapid scene switches and the seven-demo catalog. No local
app console errors were captured. Private screenshots are in
`runs/presentation-review-20261025/` and are not committed.

The live store still contains **zero jobs, zero instructions, zero model-usage
and zero model-turn events**. No inference requests or real hardware operations
were made during this follow-up. This is a visual acceptance of the first demo;
the other six retained demos have not received a new full visual walkthrough.


## Thumbnail consistency, global Problems and material identity

The owner rejected the scene animation and oversized text-only report cards.
Scene/pane switching is immediate again. Devices, accepted device plans and
layouts now reuse the same compact thumbnail grid as flake cards: a saved final
photo, the planned material layers, or the accepted layout geometry. Selected
flakes reuse the existing flake card itself. Every category opens the middle
list first, including a singleton; only a card selection opens right details.

Problems now occupies the full-width bottom row in every scenario. Expanded
messages wrap, and recovered history remains inspectable. Its resolution logic
is shared by live sessions and every recording; no production recording ID,
event number or prompt text controls recovery. The existing synthetic tests
prove that unrelated agents, chips, plan steps, pending/failed retry jobs and
final done markers cannot clear an error. A matched accepted retry, or that
retry's successful job completion, is required. This fixes status attribution;
it does not claim that a future model cannot make a new mistake.

Lab Status and Stock now identify mother tapes separately from the daughter
tape. Current seeded labels are Gr in left slide slot 1 and hBN in slot 2.
These are inventory declarations, not newly verified physical facts. Every
card shows material, carrier ID, storage location and human-review status.
The live detail form records a person's name, optional preparation-record note
and confirmation or uncertainty. Replay remains read-only. No agent tool can
submit this confirmation; Eyes and Advisor receive provenance and are told
not to infer material identity from tape appearance. An uncertain mother tape
is excluded by exfoliation parameter resolution, including explicit selection.
Reviews persist in sample events and snapshots; changing the inventory label
invalidates an older confirmation. No confirmation was submitted in the actual
live inventory during review. Endpoint tests use temporary simulated stores.

Manual Chrome review covered all six Results categories, the default replay,
category/detail navigation, full-width expanded Problems, replay read-only
material inventory and the empty live human-review form. At 1864 × 943, the
Problems row spans all 1864 pixels and ends at the viewport bottom; the layout
card is approximately 192 pixels wide in the 1042-pixel middle column. A
760 × 900 check verified the bottom row above mobile navigation and the material
cards, after which the original viewport was restored. The review caught and
fixed a long plan-label overflow, low-contrast layer labels, thumbnail SVG
borders and an empty history message squeezed into a timeline column.

Validation: the full non-browser run had 360 passes, two legacy-environment
skips and one existing assertion affected by changed error wording. The message
prefix was restored; all 18 affected graphene-flow/material-review tests then
passed. The four new material checks cover persistence/provenance, blank or
stale reviews and carriers in use, uncertain identity exclusion, and review
invalidation after relabelling. All eight offline Node event/navigation checks,
26 JavaScript module syntax checks, Ruff and diff whitespace checks passed.
The isolated legacy suite passed all 14 tests. Browser regression definitions
were updated, but automated Playwright was not run; visual acceptance used the
normal Chrome app. Private screenshots and test logs remain in ignored
`runs/presentation-review-20261025/`. No new model task or real hardware action
was performed.

## Second presentation replay: failed inspection recovered by a new job

In **Change the plan while Qumus works**, event 147 is a real failed inspection:
Eyes requested the unsupported region `search & stacking stage`. The next
inspection preserves the other requested regions and uses `transfer_stage`
and `microscope`; it succeeds at event 180. The failure should remain historical
evidence, but should no longer count as unresolved. The earlier tool-rejection
matcher did not cover a failed job followed by a successful replacement job.

The shared event logic now handles later matching jobs, with conservative
region-correction matching for this read-only inspection contract. No recording
events, IDs or names are hard-coded in the production matcher. Progress uses
the same recovery and no longer displays the stopped attempt as still running.
The original failed job remains failed when its diagnostic record is opened.

Chrome verification on the normal app showed both problems at session time
1:24, then only the unrelated tool refusal at 1:27 after the inspection retry.
At the end: **Problems 0**, **Recovered earlier 2**, and **awaiting review** for
the intended human review of candidate flakes. The first default recording
still ends with zero unresolved problems and four recovered tool rejections.
An offline audit of all seven active recordings found no other job failure
newly classified as recovered. All 13 Node event/navigation tests, five focused
pytest checks, 26 JavaScript syntax checks, Ruff and whitespace checks passed.
The negative cases cover changed targets, cameras, agents, plan revisions and
steps, incomplete retries, safety violations and outstanding human work.
Original exports are unchanged. This review used no model inference or real
hardware. Screenshots remain private under `runs/presentation-review-20261025/`.
