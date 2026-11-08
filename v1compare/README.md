# v1compare: Qumus v1 on the v2 testbench

This package runs a copy of Qumus v1 (`v1compare/v1/`, see `MANIFEST.sha256`)
against the same simulated lab as v2 (`qumus2.testbench`), so both systems can
be compared on the same task, the same faults and the same ground truth.

It compares control logic and error handling. It does not measure real lab
yield: the testbench's physics and perception are simplified (limits below).

## Current execution scope and proposed common protocol

The current delivery authorizes only three all-Astra demonstrations. **Do not run
the proposed 78-trial benchmark, controls, pilots or alternative-model probes.**
The owner explicitly permits re-recording failed demonstrations to obtain the
three deliverables; failed evidence and cumulative spending must remain visible.
The commands below describe the existing harness;
they are not an instruction to execute it in this delivery.

The next common benchmark is a design awaiting separate approval and budgeting:

| Comparison | What it can establish |
| --- | --- |
| Frozen v1 with its native specialist configuration versus all-Astra v2 | A complete-system comparison on mutually supported tasks; v1 is not assumed to be a homogeneous Astra team. |
| All-Astra v2 versus another verified model in every v2 role | A model-choice comparison within v2, conditional on account access, image/tool support and protocol compatibility. |

Match initial laboratory state, task obligations, fault triggers, operator-message
schedules and simulator seeds (proposed exploratory seeds: 7, 19 and 41). Save
source/configuration/scorer hashes and the actual per-role model manifest. Provider
responses are not made deterministic by a simulator seed. The three demonstrations
have different purposes and cannot supply these comparative statistics.

The proposed independent scorer uses executed operations, simulator state, observed
evidence and sample identities. It must report:

- Autonomous completion, triggered-fault exposure and verified recovery, separating
  Step correction from model replanning and operator assistance.
- Goal retention at every revision, false completion/autonomy claims, unnecessary
  repeated work, lost provenance and human dependence.
- Safety as separate outcomes: energy off, stable support, known storage/location
  and fresh ordinary-stage temperatures strictly below 40 C. Safety failures are
  not offset by a high aggregate score.
- Every started trial, including failures and caps; complete usage coverage, token
  totals, cost basis, wall time, model time, operator wait and simulated duration.

Report raw n/N, uncertainty intervals and paired seed differences. Missing
measurements stay unknown; an unstarted cell is not a failed or successful trial.
The legacy scorer described below does **not** yet implement this complete protocol.
Preserve unsuccessful replays and independently review ambiguous narrative claims.

## What is replaced and what is not

v1's agent code, prompts, flows ("assemblies") and tools run unchanged. Only
the lowest layer, the one that talks to hardware, cameras and trained models,
is swapped:

| v1 boundary | replaced by |
|---|---|
| `serial` G-code arms (`common/hardware`) | `emulators.py`: G-code arm emulators driving the testbench arms (`calibration.py` maps v1 coordinates to the world) |
| Thorlabs stages, heaters, vacuum, PDV transfer stage | `emulators.py`, backed by the testbench devices |
| desktop cameras (`cv2.VideoCapture`) and YOLO OBB (`ultralytics`) | `perception.py`: frames and detections from the testbench world, same locator noise as v2 |
| microscope camera and autofocus lens | `microscope.py`: `SimCameraController`, renders the testbench chip and flakes |
| flake segmentation (`LiveSegment._run_with_consensus`) | `microscope.py`: v2's `SimMicroscopeAnalyzer`, then v1's own area and label filters |
| wall-clock time in v1 modules | testbench `SimClock` (`bench.py`), except the image-folder watchdog |
| SQLite databases, logs, status files, camera overlays for the UI | redirected into the trial's work directory (v1's working directory in the lab is its repo root) |

`bench.V1Bench` installs all of this in one process (one bench per process,
because v1 binds its databases at import). The harness drives sim devices
directly, not through v2's `SafetyGate`; it never touches real hardware.

## Two modes

**Flows mode** (no model, free): each system runs its own procedures for the
graphene task. v1: `assembly_exfoliation` then `search_chip_assembly`. v2: the
`exfoliate_chips` then `search_chip` flows. This isolates the
verify-and-correct logic.

    python -m v1compare.run --out runs/checks/v1compare/flows --seeds 1-3 --jobs 3
    python -m v1compare.trial --system v1 --scenario askew_on_stage --seed 1 \
        --workdir /tmp/t --out /tmp/t.json

**Agent mode** (real model replies, costs money, Mac only): the whole system
gets one plain-language task. v1 runs `create_qumus_agent` (smolagents +
LiteLLM, non-streaming); v2 runs its SDK team. Spend caps are enforced in
code (`--budget-usd`).

    python -m v1compare.gateway_probe          # which models the gateway serves
    python -m v1compare.agent --system v1 --scenario baseline --seed 1 \
        --workdir runs/checks/v1compare/agent/v1_baseline_s1 --budget-usd 4

v1 reaches Claude through the gateway's OpenAI-compatible endpoint by
default; set `QUMUS_V1_ROUTE=anthropic` to use the Messages endpoint instead
(the harness adds the Bearer header the gateway requires). Use whichever
route `gateway_probe` reports OK.

For shared comparison helpers and v2-side flow checks, install
`pip install -e ".[dev,v1compare]"`. To execute frozen v1 in either mode,
use `.venv-v1compare` with `.[dev,v1compare,v1agents]`.
LiteLLM's `openai<3` requirement conflicts with OpenAI Agents SDK 0.24's
`openai>=3`; never combine `v1agents` and `agents`. The current OpenAI runtime
has its own live checks in `scripts/check_model.py`; the agent-mode
comparison command for `--system v2` now uses OpenAI Agents SDK, its
`QUMUS_OPENAI_*` configuration and the persistent budget ledger. It must run
from `.venv`. Run frozen v1 from `.venv-v1compare`; that environment retains
the original Claude specialist SDK dependency used by the immutable baseline.

## Scenarios

Each scenario injects one testbench fault when the world reaches a state,
checked before every device command, so the same trigger works for flows and
agents: `baseline`, `askew_on_stage`, `pick_fails_once`, `drop_in_transit`,
`arm_timeout`, `microscope_offline`, `crash_holding_chip`,
`crash_during_scan` (see `trial.SCENARIOS`).

## Scoring (`scoring.py`)

A trial really succeeds when a chip carries flakes, at least one real,
distinct flake is catalogued, the chip is back in that system's storage area,
and nothing is left held or on the transfer stage. A **false success** is a
reported success the world contradicts. Also counted: human interventions (a
restart after a crash, a parked job, an answer to a question), catalogued
flakes that match real ones, monolayers found, whether the system's records
put the chip within 3 mm of where it really is, and chips exfoliated beyond
the one the task asks for (`extra_chips_used`: a repeated physical step costs
a chip and tape on real hardware).

## Known limits

- Desktop detection never misses or invents objects; only position noise.
- Autofocus always converges; segmentation never fails consensus.
- v2's sim devices take almost no sim time, so durations are not comparable.
- v1 has no supervisor: after a crash the harness rebuilds v1's tools in the
  same process and reruns the phase (counted as one human intervention). v2's
  supervisor resumes on its own.
- The tape-release step uses the testbench's model of exfoliation, not v1's
  assumptions about tape and temperature. v1's heated recipe peels once with
  the vacuum off, so the chip rides on the tape while the stage heats; the
  testbench lays the chip back on the stage when the tape is applied again.
- Real-hardware questions (e.g. vacuum during the tape lift) are not settled
  by this harness.
