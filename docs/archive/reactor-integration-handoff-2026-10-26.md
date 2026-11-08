> Historical development evidence, superseded by the integrated optional reactor and current workstreams. Old startup/release instructions are not current.

# Reactor integration handoff, 27 October 2026

Branch: `codex/vdw-reactor-simulation`, in the isolated reactor worktree.
This work has not been pushed or merged into main. No GitHub CI, paid model
calls or physical hardware operations were used.

## Integration boundary

- `441620c` preserves the owner-approved mechanical model, equal spacing,
  rigid XY assembly and Arm 3 / storage clearances.
- `8b8c668` adds the two-flake ledger, guarded reaction flow, fixed goal,
  transactional acceptance, optical adapter, fixture and offline recorder.
- Reviewed material dependency: `7cfbcf8`. Merge `094d03c` incorporates it
  and its `3c83d87` baseline. Material algorithms remain owned by that line.
- The final follow-up adds actual lab-process restart coverage, compatibility
  rejection before checkpoint overlay and two versioned acceptance recordings.

Shared-file integration needs deliberate review in `app.py`, `runtime/store.py`,
`agents/gateway.py`, prompts and architecture docs. The `_transaction()` helper
matches the core line's `BEGIN IMMEDIATE` / RLock contract. Keep one definition
when integrating; do not discard the reactor's atomic result transaction or
the core's independent plan-history transaction fixes. Persistence retains both
the reactor state/seed and the material observation random streams.

## Current behavior

An enabled Reactor now defaults to two small, preprepared synthetic flakes on
each narrow carrier. These deterministic rectangular input polygons (500 and
288 square micrometres) are acceptance fixtures, not photorealistic specimens
or an executed autonomous cleavage/stacking chain. The material backend supplies
the overlap, local reaction, one stable product identity and rendered view.
Legacy whole-field growth requires an explicit `legacy_field_growth` config;
its job and goal are refused on a pair bench. Existing recordings are unchanged.

The adapter preserves backend geometry/state/model provenance under
`material_state`. Partial, failed, cropped, unfocused or null product evidence
cannot pass result acceptance. A successful hardware job alone is insufficient.
Acceptance locks before reading goal/job/evidence and atomically writes the
single product, both consumed source rows, project result and events. Independent
SQLite connections cannot insert two competing products; injected write failure
rolls back all of them.

The original 190 mm regular spacing, workcell-relative positions, 190 mm rod,
160 mm insertion, narrow carrier and fixed microscope remain. The common frame
clears Arm 3 by approximately 23.1 mm and dedicated storage by 35 mm at zero XY;
the +/-4 mm adjustment retains clearance. Actual expanded-browser bounds gave
14.53 px / 22.01 px gaps with no overlap at the reviewed viewport.

## Recordings and purpose

| Directory under `recordings/` | Purpose and observed result |
| --- | --- |
| `experimental-reactor-pair-normal-20261027-v1` | Preprepared pair load, insertion, small XY observation, bounded reaction, exactly one lineage-preserving product, cooling, retraction and storage. Seven successful jobs, 175 checked commands, 11 images; 12 acceptance checks including media validation passed. |
| `experimental-reactor-pair-camera-loss-20261027-v1` | Microscope goes offline after heating. Reaction parks, targets turn off, exposure remains uncertain, and a result-submission attempt is rejected. Six jobs, 149 checked commands, five images; nine checks including media validation passed. The hot chip remains inserted awaiting review; this is deliberately not a recovery-success claim. |

Both use `ToolGateway`, jobs and `SafetyGate` with scripted decisions, not LLM
decisions. API usage is zero. Their metadata states the fixture purpose and
provenance. The microscope panel labels its image as a saved observation, so a
pre-fault image is not mistaken for current device feedback.

Reproduce into new, non-existing directories:

```sh
python scripts/record_reactor_pair.py --scenario normal --out runs/checks/pair-normal-new
python scripts/record_reactor_pair.py --scenario camera-loss --out runs/checks/pair-fault-new
python -m qumus2 replay recordings/experimental-reactor-pair-normal-20261027-v1
```

Use the modern `.venv` runtime. The existing launcher and CLI remain the entry
points. For a new live simulation, explicitly choose a fresh database rather
than enabling Reactor on the normal saved workspace:

```sh
python -m qumus2 serve --mock --reactor --lab-process \
  --db runs/checks/reactor-pair-workspace-new/qumus.db
```

Compatible restart binds the existing ledger without reseeding consumed inputs
or the product. Reactor device/layout mismatch is rejected before any object,
device, clock or random state overlay. Tests preserve the old checkpoint bytes,
ledger and newly constructed lab when enabling, disabling or changing the
reactor configuration incompatibly. There is no automatic migration.

## Validation and review

- Pair/material, mechanics, workflow, real-adapter and transactional acceptance
  checks passed after the material merge. Separate lab-process restart retains
  the same product and allows safe unload; consumed inputs cannot react again.
- Offline JavaScript replay suite: 17 passed, including continuous insertion and
  retraction in the new normal recording and all three preserved geometry replays.
- Frozen-v1 comparison suite: 14 passed; the frozen source tree is unchanged.
- Full non-browser Python regression: 476 passed, one skipped in 199.94 seconds,
  recorded in `runs/checks/reactor-pair-final-pytest.log`. The final empty-carrier
  rendering correction then passed all 12 pair-binding/restart tests; an empty
  pair microscope never falls back to a legacy growth image. Ruff and whitespace
  checks passed.
- Actual in-app browser reviewed both recording outcomes and the preserved
  expanded layout. Local screenshots are under
  `runs/checks/reactor-pair-browser-20261027/` (`normal-complete.png`,
  `camera-loss-parked.png`, `approved-layout-preserved.png`).

This does not claim the unrelated full browser-test suite is green. The core
and material lines have reported baseline UI failures; this line verified the
actual reactor replays through the browser. One existing cosmetic issue remains:
scripted tool-only conversation cards can still say "working 0:00" after the
plan/job has completed. The plan and job outcomes themselves are correct.

## Remaining physical scope

The module is qualitative software simulation. Real dimensions, optical
calibration, chemistry, rates and material identity remain unvalidated. The
confirmed bottom-X / top-Y / insert mechanism and injected-controller adapters
are retained, but no physical controller has been opened. Real two-flake
preparation and calibrated material observations require their own verified
hardware workflow and provenance before replacing the explicit fixture.
