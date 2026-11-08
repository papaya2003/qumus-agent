# Qumus v2

> **Current status, 6 November 2026: the Hall-bar workflow runs end to end in
> the simulated lab.** From tapes to graphene and hBN released onto the
> eight-electrode chip, following the version 1 operations. Seeds 3 and 40
> stacked with scripted agents, and a live `gpt-6.1-sol` run stacked seed 3 on
> 6 November (resumed from the save point before flake picking). Open items are
> listed in issue #255. Nothing here operates real hardware.

The current workflow is the hBN/graphene Hall bar on the owner's eight-electrode
chip: exfoliate hBN and graphene from tapes, search the chips with version 1
recognition, pick a pair, lay out the device in KLayout, then pick up hBN, pick
up graphene with it and release the stack onto the pre-made electrodes. See the
[Hall-bar workflow](docs/hallbar-workflow.md) for the steps, the owner's
decisions and save points. The reactor and material-metal workflows remain in
the code with their own documentation but are not the current target. GitHub
is repository storage only; all checks run locally.

**Current runtime:** model-backed work uses **OpenAI Agents SDK**.
The launcher `scripts/start_qumus.py` starts it in live mode after you fill
the project `.env` (Token Station endpoint, full model ID, key and token rates);
it spends money only when you send the team a task. Plain `serve` is scripted.
The old v2 Claude SDK execution path was removed on 26 October 2026.
Historical Claude recordings remain playable; frozen v1 stays isolated.
See [Runtime setup, budgets and validation](docs/runtime.md).

For everyday use without an assistant, see
[Open Qumus without an assistant](docs/local-start.md). On this Mac, the
`~/Projects/Qumus` folder has one entry point, `Start Qumus.command`.
Watch recordings inside **History → Demo recordings**, including each demo's
Results, Conversation and Experience. Send new tasks from the same app.
The tracked implementation is `scripts/start_qumus.py`. Dated reports from
earlier rounds are under [docs/archive](docs/archive/README.md).

Qumus is an AI agent team that runs a 2D-materials lab: it exfoliates crystals
onto chips, searches the chips for good flakes under the microscope, picks the
flakes for a device, designs the device and stacks it with a stamp. The current
v2 implementation demonstrates these workflows on a software testbench;
multi-day physical operation remains a project goal. It is built for Sanfeng
Wu's lab at Princeton. v2 is a
rewrite of the agent part of Qumus v1, now on the OpenAI Agents SDK, with a simulated
lab (the testbench) to develop and test on, and a web page to watch and talk to
the team.

**Private lab code. Do not push it to any public place.** Never commit API
keys, `.env`, lab run data (images, databases) or model weights.

Contents: [What it is](#what-it-is) · [What changed from v1](#what-changed-from-v1) ·
[The team](#the-team) · [Safety](#how-safety-is-kept) · [The testbench](#the-testbench) ·
[The page](#the-page) · [Where it stands](#where-it-stands) ·
[Install](#install-on-a-mac) · [Run modes](#run-modes) ·
[Recordings](#recordings-demo-free) · [Benchmarks](#chaos-benchmark-and-the-v1v2-comparison) ·
[FAQ](#faq) · [Code layout](#layout)

## What it is

A person types a request ("make an hBN-encapsulated graphene Hall bar") into
the page. Qumus, the orchestrator, writes a plan and hands the work out to
six team members. Every physical action is done by code that checks its own
result with a camera or sensor and corrects it; the model decides *what* to do
next, the code decides *how* and keeps the rules. A person can write to the
team at any time, change the plan, or press Stop. Work is autonomous by default;
Qumus waits at review points requested by the person.

Start with the [current document index](docs/README.md) and the
[Hall-bar workflow](docs/hallbar-workflow.md), then the
[implementation contracts](docs/architecture.md) and the
[testbench guide](docs/testbench.md). Project notes and the electrode design
files are kept outside this Git repository.

## What changed from v1

| | v1 | v2 |
|---|---|---|
| Agent framework | smolagents, GPT-4o plus Claude connected by relay agents | OpenAI Agents SDK; flake picking and device design are members of the team, no relay agents |
| Hard rules | written into ~71,000 characters of prompts | in code: tool checks, hooks on every tool call, a safety layer under every device command; prompts only describe roles |
| Physical steps | run open loop; an error often stopped the run | every step is *precondition, act, verify, correct*; only what cannot be corrected goes back to the agent, with photos and measurements |
| Resume | only the four-flake stacking program | every step of every job is checkpointed; after a crash the machine state is checked before going on |
| Learning | `flow_*` tables never filled | run records and an experience library the advisor reads before proposing a recipe (learns only from real experiments) |
| Development | on the real bench | on a simulated lab with the same layout, faults on demand, and replays of whole runs |

## The team

```
                   person (web page): talk, change the plan, stop
                                  |
                       Qumus (orchestrator, selected model)
         -----------+-----------+---------+-----------+-----------
         |           |           |         |           |
      Advisor       Eyes       Hands     Picker     Designer      Reactor
   recipe, plan   looks at    runs the   chooses    lays out
   experience     the bench   hardware   flakes     the device
                               jobs
```

- **Qumus** (orchestrator, the SDK main session) plans, delegates, reports.
- **Advisor**: device plan and exfoliation recipe, reads the experience library.
- **Eyes**: inspects the bench with the cameras before hardware work starts.
- **Hands**: submits hardware jobs (exfoliate, search a chip, stack a device),
  watches them, and chooses from a fixed recovery menu when one stops.
- **Picker**: chooses flakes from the catalogue and their microscope photos.
- **Designer**: draws the device layout on the chosen flakes; code checks it.
- **Reactor**: runs guarded reactor processing on benches with the reactor
  enabled. It is not used in the Hall-bar workflow.

Each member has only its own tools. Roles are in `qumus2/agents/roster.py`,
prompts in `qumus2/agents/prompts/`, tools in `qumus2/agents/tools/`.

## How safety is kept

- **Every device command goes through `SafetyGate`** (`qumus2/safety/`): travel
  ranges, height floors, interlocks between devices (the stage may not rise
  under a low stamp, an arm may not go down where the stamp hangs), heater ramp
  limits, a watchdog. A refused command raises an error; it is never sent.
- **ToolGateway checks every tool call** before it runs (`qumus2/agents/policy.py`):
  who may call what, in which order (e.g. look at the bench before moving
  anything, a device can be stacked only from accepted flakes and an accepted
  layout), how many retries are left.
- **Hardware jobs are closed loops** (`qumus2/flows/`): each step verifies its
  result and corrects within tolerances from `config/verification.yaml`; a step
  that cannot verify itself says so and is listed in `qumus2 report`.
- **Honest stops**: when something cannot be fixed, the job parks, the team
  says why, and the person is asked. Faking success is what the benchmarks look
  for first.
- **Money**: model turns have token and estimated USD budgets. OpenAI runs
  share a persistent phase ledger and a per-workspace cap (default $40).
  A historical shared phase-ceiling setting does not override the current first
  Astra campaign's inclusive $100 cap, including qualification and retries.
  The everyday workspace default remains $40; estimates are not bills.
  Configure gateway rates in `.env`; see [budget details](docs/runtime.md#budget-and-data).
- Real hardware is not connected yet; it will be stage 3, and only with the
  lab's OK.

## The testbench

A software copy of the lab: exfoliation, finding and stacking (microscope,
heated x/y/z stage, stamp holder), and a material-metal reactor with its own fixed
microscope, heater, gas feedback and insertion mechanism. The everyday profile
has three arms and the corresponding storage stations. The preserved previous
workspace has the original two-arm layout. The agents, tools, job service,
safety layer and page are application code; the devices underneath are simulated.

- Mechanics are simulated for real: arm and stage moves, the tape rolls, focus,
  the stamp's contact geometry. What depends on polymers (tape adhesion, PC film
  pick-up) is a probability model; the exfoliation yield does not depend on
  the recipe (the hidden best-recipe model was removed on 2026-11-03).
- **All numbers are placeholders**, not lab success rates. Testbench runs never
  feed the experience library.
- 22 faults can be injected (from the page or from tests): failed picks, chips
  dropped in transit, placements off target, torn stamp films, stuck vacuum,
  offline devices, serial timeouts, crashes of the whole process, and more.
- The world always knows the truth, so benchmarks judge what really happened,
  not what the team claims.

Details: [docs/testbench.md](docs/testbench.md).

## The page

One page in the browser. The activity bar on the left picks one of four
scenarios, and each one fills the same three columns (the left says what to
look at, the centre shows it, the right goes with it):

| Scenario | Left | Centre | Right |
|---|---|---|---|
| **Progress** (what the team does now) | the progress tree: plan steps and their hardware jobs; a click opens a job's lab actions | the conversation, as a group chat (you on the right, each team member on the left, tool calls folded) | the bench: top view, overview camera, microscope, the last check; a clicked sample or plan shows here |
| **Results** (what this run made, the evidence, the problems) | Overview, Device, Materials, Problems (only the groups this run has) | the verdict, the device and its layers, the flakes, one evidence row per job, what is left for a person | the details of what was clicked |
| **Lab** (switch Status / Stock) | Status: overview, instrument and supply categories. Stock: categories with counts | Status: cards for the selected category, or the bench map under Overview. Stock: cards with search | Details of the selected instrument, camera or stock item; "New PC film" on a used or torn stamp |
| **History** (switch Runs / Experience) | earlier runs on this computer and the demo recordings; or the experience filters | one run read-only, with "▶ Play recording"; or the experience cards | its key moments (a click plays from there); or where an entry came from |

The mode (live or a recording) is said once, in the title bar, with the
one-line "Now" sentence beside it. A recording plays in the same page with
"Back to live". The Problems panel above the status bar lists refusals,
failed checks and jobs waiting for a person. On a phone the bar becomes four
tabs (Chat, Progress, Lab, More). Light and dark themes; English only.

## Where it stands

As of 6 November 2026, on the integration branch `claude/project-thread-8oos2c`
(`main` still holds the 29 October version):

- **Done in simulation:** exfoliation and 10X search with version 1 recognition,
  20X/100X revisits, pair selection, the KLayout design with a 3 degree angle
  margin, and stacking that follows the version 1 steps with the four owner
  exceptions (see the workflow page). Save points let a run resume before flake
  picking instead of exfoliating and searching again.
- **Evidence:** scripted runs stacked seed 3 and seed 40; the live
  `gpt-6.1-sol` run stacked seed 3 in 18 minutes for about $4.26 (estimate).
  Placement numbers come from the simulator's ground truth. As in version 1,
  the program itself archives photos after the drop and makes no automatic
  final geometric check.
- **Open (issue #255):** most seeds find no pair under the 3 degree margin;
  the stacking success rate over many seeds is not measured; about 98 inherited
  test failures; some perception steps still read simulator truth; the UI does
  not retry gateway 503 errors; a parked revisit leaves the chip on the transfer
  stage (#223).

Real hardware adapters, lab calibration and measured polymer properties are not
available yet. Simulated runs never feed the experience library. No real
apparatus is operated.

## Install on a Mac

Needs git and Python 3.11 or newer.

```bash
cd ~/Projects/Qumus
git clone https://github.com/papaya2003/qumus-v2.git v2
cd v2
python3 scripts/setup_qumus.py  # add --ui-tests for browser tests
source .venv/bin/activate
```

These are fresh-install instructions. On the existing workstation, use the
normal launcher or `.venv`; keep `.venv-v1compare` isolated for
legacy v1 dependencies. Never install `agents` and `v1agents` together.

## Run modes

| Mode | What it is for | Command | Costs money? |
|---|---|---|---|
| **Scripted** (default) | Try the page and the whole software stack. Tools, hooks, job service, safety layer and testbench are real; what the agents say and choose comes from a scenario file (`qumus2/testbench/scenarios/*.yaml`), so typed messages do not change what happens. | `python -m qumus2 serve` (optionally `--scenario graphene_flake` / `graphene_device`) | No model requests, even if a key is set |
| **Live** (everyday) | OpenAI Agents SDK with the selected model on the Hall-bar bench. Configure project credentials and rates in `.env`. | `Start Qumus.command` (runs `serve --openai --lab-process --budget 40 --profile hallbar-linked`) | Yes, only when you send a task: estimated caps and a persistent ledger |
| **Recordings** (demo) | Show a recorded run to someone. | Start Qumus (`scripts/start_qumus.py`), open **History** | No: watching sends nothing |

The page is http://127.0.0.1:8765. The status bar says which mode is running.
The launcher runs Qumus in the background: stop it with `Stop Qumus.command`, or
close every Qumus page (it stops 15 s later unless work is still running). A
`serve` started by hand stops with Ctrl+C.

Restarts: plain `serve` builds a fresh testbench every time it starts (jobs the
last session left open are closed with a notice; what the earlier bench made,
its flakes and devices, leaves the inventory, and History keeps it). `serve --lab-process` runs
the lab in its own process, keeps the bench across restarts and resumes jobs
where the records say they stopped; the real lab will always run that way.

Other commands: `python -m qumus2 demo --askew` (headless scripted demo: a
placement goes askew and is corrected), `pytest` (all tests, no hardware, no
key), `python -m qumus2 report` (steps that cannot verify themselves).

## Recordings (local and free)

Open **History → Demo recordings** in the same application. Choose a recording
by its purpose; Results, Progress, Conversation, Experience and the token-usage
Context chart remain in one interface. The administrator catalog automatically
matches context limits for 91 exact model IDs on the Token Station gateway;
Settings supports manual overrides and restoring defaults. The chart shows used
and remaining tokens. Its default scale fits actual usage; Full capacity is an
optional view. Aligned agent rows, an agent filter and a request table identify
who made each call. Selecting a request opens its model, usage, capacity and
retry identity in the right panel. Zero/unlisted limits stay unknown. Older per-response token reports
remain visible; missing usage and limits stay unknown. Playback sends no model request.

Hall-bar recordings (`recordings/hallbar-*`) embed the owner's electrode design,
so they stay on the Mac and out of Git; History lists the 6 November live run
first. `scripts/record_hallbar_night.py` makes new ones (free without
`--openai`). The [recording catalog](recordings/README.md) describes the
recordings kept in Git, which are earlier workflows preserved unchanged.

Historical comparisons and earlier reactor mechanics/field-growth demonstrations
are preserved under `recordings/archive/`, outside the daily catalog. Raw run
records, failed attempts and the budget ledger remain private under `runs/`.

## Chaos benchmark and the v1/v2 comparison

- `python -m qumus2 chaos --seeds 40` (free, no model calls): many seeded runs of the
  stacking task, each with an injected fault, each judged against the simulated
  world: ok, honest stop, or bad (a false success, a collision, a lost sample).
  Writes `runs/checks/chaos.md`. `--fault NAME` repeats one fault, `--faults 2` injects
  two per run. It tests the flows and the safety layer, not model judgement.
- `v1compare/`: the unchanged v1 code runs on the same testbench through
  emulated hardware, so v1 and v2 can be compared on the same task, faults and
  ground truth. Flows mode is free; paid agent mode uses frozen v1 in its
  isolated environment or the current OpenAI SDK team for v2. See
  [v1compare/README.md](v1compare/README.md).
  Historical Claude comparison (2026-10-23, that revision's bench, seed 1, one run per
  scenario): v2 succeeded in 6 of 7 scenarios (the microscope-offline one stops
  honestly, as it must), for $3.33 in total; v1 succeeded in 0 of 2 valid runs
  (a third, askew placement, never reached the fault and does not count), for
  $12.02. Neither system claimed a success it had not achieved. One run per
  scenario shows what can happen, not how often.

## FAQ

- **The page says "Connecting…" or "Server link lost".** Qumus stops by itself
  about 15 s after its last page closed. Start it again with
  `Start Qumus.command`; if it is running, reload the page. Since 2026-10-22 the
  server tells the browser to check for new files on every load, so an old
  cached page after `git pull` should no longer happen; if it does, Cmd+Shift+R.
- **I typed a message and nothing changed.** Plain `serve` is scripted: the
  scenario decides. Configure the project `.env` and use `serve --openai` for model-backed tasks.
- **Port 8765 is in use.** Run `Start Qumus.command` again: it opens a running
  Qumus, offers to restart an older one, and names any other program on the port.
  `Stop Qumus.command` stops Qumus from anywhere.
- **Model connection unavailable.** Open Settings or hover the model status for
  the reported reason. A reachable gateway does not prove model/tool support.
- **Which key, and where?** The current application reads `QUMUS_OPENAI_API_KEY`
  and the other `QUMUS_OPENAI_*` settings from the project `.env` or environment.
  It does not fall back to another application's credentials. Never commit keys.
- **Does any of this touch the real lab?** No. Nothing in this repo drives
  real hardware yet.

## Model configuration

Use [the runtime guide](docs/runtime.md) for credentials, gateway
transport, token rates, budgets and opt-in live checks. The main environment
is `.venv`; `.venv-v1compare` is reserved for the frozen v1 comparison.

Other scripted demos: `python -m qumus2 serve --scenario graphene_flake` or
`--scenario graphene_device`.

Pictures from the simulated cameras and microscope are written to
`<run-directory>/images/` (git-ignored). The overview camera uses a simple, labelled 2D
diagram to demonstrate image capture and display; it does not model the real
bench's appearance. Existing recordings keep their original saved images.
Live microscope captures render the current scene with their captured pose and
optical calibration. Nonempty `QUMUS2_MICROSCOPE_REPLAY_DIR` or
`microscope.replay_dir` settings are refused for live sensing; arbitrary photos
remain available only to offline renderer fixtures. They cannot establish the
current experiment's geometry or readiness.

## Layout

```
qumus2/
  core/         shared types, structured errors, clock (sim time), materials
  devices/      device interfaces; sim/ = testbench devices, real/ = lab drivers (stage 3)
  safety/       SafetyGate + limits: every device command is checked and logged
  perception/   image analysis and bounded registration used by step verification
  flows/        closed-loop Step + StepRunner (correction ladder); flows in library/
  runtime/      run store (SQLite), events, job service, restart recovery
  agents/       agent roster, prompts, tools, PreToolUse policy, gateway,
                scripted runtime (no key) and OpenAI Agents SDK runtime
  interaction/  human instructions from the UI to the orchestrator
  testbench/    simulated world, sim locator, lab builder, scenarios
  ui/           FastAPI backend + static front end
  config/       limits, verification tolerances, bench positions, agents, testbench
  records/      samples, lab evidence, recipes, experience and scientific record helpers
tests/          unit + testbench scenario tests (local acceptance; GitHub CI disabled)
scripts/        setup, single-launcher implementation, model checks and recording tools
docs/           current guides; archive/ holds dated evidence
recordings/     scrubbed simulation demos; archive/ holds older comparisons
runs/           private workspace/, checks/, archive/ and shared budget ledger
v1compare/      comparison harness; v1/ is the frozen original source
```

See [run-data management](docs/run-data.md) for the private directory layout,
relocation compatibility and preservation rules.
