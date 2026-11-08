# Recording catalog

Each recording has a specific audience-facing purpose. Playback is local and
free; it makes no model call. Start the normal application, open **History →
Demo recordings**, and choose a story. Each has its own Conversation, Progress,
Results and Experience. Current recordings with request measurements also have
a Context chart; older recordings show that measurements are unavailable.

Only scrubbed simulated data belongs here. Images and material outcomes are
synthetic, not evidence of measured material quality or physical hardware.
Original exports are immutable; a changed implementation gets a new recording.
Execution fingerprints and any continuation segments stay in the original
metadata. The [integration audit](../docs/archive/integration-audit-2026-10-27.md)
records each accepted workflow, source revision and local validation boundary.

The recordings listed below are earlier workflows, preserved unchanged. They do
not show the current Hall-bar workflow. Current Hall-bar recordings
(`hallbar-*`) embed the owner's electrode design, so they stay on the owner's
Mac and are excluded from Git; History lists them with the others. Context
metrics use only each recording's available measurements; missing usage is unknown.

## Historical demonstrations

| Recording | What it demonstrates | Evidence boundary |
| --- | --- | --- |
| `astra-complete-recovery-20261029` | Short initial task, complete material-metal workflow and recovery from three injected faults | Live Astra; simulated device actions and guarded automatic corrections. Original budget stop and explicit same-session closeout retained. Both ordinary stages freshly measured below 40 C; no chemical-product claim |
| `astra-collaborative-refinement-20261029` | Four scripted operator changes: reserve a chip, change targets, add material and stop after independent largest-flake review | Live Astra; both sources stored, chip_001 untouched, original images retained, no pair/design/transfer/reaction. Budget/network interruptions remain visible; final stages freshly measured at 22 C |
| `astra-honest-stop-20261029` | Recognize a permanent microscope outage and stop truthfully | Live Astra; bounded recovery, energy outputs off, incomplete imaging/storage stated explicitly. This story succeeds by reporting the real blocked outcome |
| `openai-material-metal-default-20261028-v7` | Complete hBN/Gr/Pd/silicon preparation, reaction, safe storage and request-token monitoring | Earlier default; live model, normal seed 7, 77 provider usage reports, eight jobs and eleven steps done; no tool errors or mid-run operator correction. Qualitative simulation only |
| `openai-material-metal-default-20261028-v5` | Complete the corrected hBN/Gr/Pd/silicon workflow and inspect guard recovery plus request tokens | Earlier guarded execution; live model, normal seed 7, 78 provider usage reports, eight jobs and eleven steps done without operator correction. Seven rejected plan submissions retained; two null-to-confirmed-source refusals were corrected in code after capture. Qualitative simulation only |
| `openai-material-metal-default-20261027-v2` | Prepare hBN/Gr from ordinary stock, place on reported Pd/silicon, run the reactor simulation and finish cold in storage | Earlier autonomous evidence; live model, normal seed 7, 74 requests with provider usage, eight jobs and twelve plan steps done without operator correction. Six recovered planning refusals retained; predates the final shared argument-contract repairs |
| `openai-material-metal-recovery-20261027-v1` | Close a completed experiment using original job evidence without repeating device work | Live model plus explicit scripted operator correction of a truncated job ID; all original failures, budget stop and continuation source segments retained. Assisted recovery, not autonomous recovery |
| `material-metal-workflow-20261027-v1` | Inspect the early material-metal contract handoff | Scripted abstract receipt fixture; not device-experiment acceptance |
| `reactor-review-20261027-v2` | Inspect owner-review guards and recovered errors | Scripted workflow diagnostic, not a live-model full experiment |
| `openai-integrated-collaboration-recovery-20261027-v6` | Preserve a reserved chip during a mid-turn change, prepare graphene and hBN, recover from three faults within the same search, then stop | Live model; default integrated bench; four scripted operator inputs; 56 requests; all 10 Task Plan steps done and all 26 evidence gates passed |
| `openai-integrated-reactor-20261027-v5` | Historical two-flake interpretation, preserved for the owner's original review | Superseded by the material-metal process; live model, 110 requests, visible budget stop and continuation retained unchanged |
| `openai-integrated-device-20261027-v5` | Resolve target geometry, prepare and select graphene, review/approve a layout, stack and save the device | Live model; scripted operator approval; 87 requests; all 10 Task Plan steps closed; final image and typed storage verified |
| `experimental-reactor-pair-camera-loss-20261027-v9` | Camera fails after heating; targets turn off; residual heat and uncertainty remain; no false product | Current offline scripted prepared-pair fixture; default noise; human review is intentionally still needed |
| `openai-collaboration-20261027-v3` | Change specimens and materials during preparation | Historical pre-repair recording; retained unchanged, not current acceptance evidence |
| `openai-recovery-20261027-v2` | Recover from three transfer faults | Historical pre-repair recording on the previous bench |
| `experimental-reactor-pair-normal-20261027-v3` | React two seeded prepared flakes | Historical pre-repair scripted fixture; no autonomous preparation |
| `experimental-reactor-pair-camera-loss-20261027-v3` | Stop after camera failure | Historical pre-repair scripted fixture; preserved unchanged |
| `openai-device-approval-20261025` | Review a layout, approve transfer, inspect the finished device | Historical live model baseline; scripted operator approval; predates the 27 October changes |
| `openai-collaboration-20261025` | Reserve chip_001 while Qumus is working; continue on chip_002 | Live model; one scripted mid-turn change, not a long multi-turn collaboration |
| `v2-device-pickup_fail-20261023` | Retry after a flake stays on its source chip | Historical live model; one stacking fault |
| `v2-device-stamp_torn-20261023` | Replace a torn stamp and complete the device | Historical live model; consumable recovery |
| `v2-arm_timeout-seed1` | Recover from a serial timeout | Historical live model; communication recovery |
| `v2-microscope_offline-seed1` | Exhaust bounded retries and ask a person | Historical live model; honest failure boundary |
| `v2-crash_holding_chip-seed1` | Resume after a process crash while holding a chip | Historical live model; persisted-state recovery |

The five historical fault examples are separate runs. They do not demonstrate
five faults recovered within one experiment. Model, seed and scenario remain in
each recording's metadata. The older recordings retain their historical provider
labels and require no old execution SDK to play.

## Local playback and verification

Start Qumus (`Start Qumus.command`) and open **History → Demo recordings**.
There is no separate command-line playback (removed 2026-11-06). The catalog
discovers active recording folders automatically and excludes `archive/`;
`DEFAULT` names the recording shown first. Verification is local; do not run
GitHub Actions or other remote checks in this phase.

## Creating evidence

`scripts/record_hallbar_night.py` records the current Hall-bar workflow (see
[the workflow page](../docs/hallbar-workflow.md)). For the historical stories,
`scripts/record_release.py` records multi-turn material preparation and a
single transfer with three injected faults. Its `collaboration-recovery` case
combines both materials with three observed faults in the first full-chip search;
all collaboration and recovery gates must pass. It requires explicit `--live` and
a bounded run budget. It keeps original databases, failed attempts and estimated
spend privately under `runs/checks/`; only an export with passing evidence checks
is promoted into this catalog. Script availability is not proof that a new run
has passed. A recording is listed here only after acceptance.

Operator messages in these acceptance scripts are scripted test inputs through
the normal inbox. Agent decisions are real model calls on simulated devices.
Do not describe a deterministic fixture or scripted reply as model autonomy.
Configured-rate token costs are estimates, not a provider invoice.

Historical reproduction commands, original cost estimates and the archived
comparison examples are in [recording history](archive/README.md). Earlier
presentation limitations are in the [dated review](../docs/archive/review-corrections-2026-10-25.md).
