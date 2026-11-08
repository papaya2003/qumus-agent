> Historical snapshot. Current ownership and instructions are in
> [workstreams](../workstreams.md), [architecture](../architecture.md) and
> [local setup](../local-start.md). Earlier test counts apply only to the
> revisions described below.

# Stage-1 workstreams

Three threads work in parallel, each on its own branch with PRs into `main`.
Each owns some directories; touching another workstream's directory is fine for
small fixes but must keep its tests green and be called out in the PR.
Contracts are listed in [architecture.md](../architecture.md).

Spec §11.5 (stage-1 done) is the shared finish line:
1. one command on the Mac, browser opens the UI, no lab hardware;
2. "give me a graphene flake" / "make a graphene device" runs end to end on the
   testbench with all six agents, visible live;
3. a message mid-run changes the plan;
4. every hardware command logged and safety-checked; hook denials visible;
5. an injected askew placement is caught and corrected; killing the process at
   any step and restarting resumes.

---

## A. Testbench (the simulated lab and everything below the tools)

**Owns:** `qumus2/devices/`, `qumus2/safety/`, `qumus2/perception/`, `qumus2/flows/`,
`qumus2/runtime/jobs.py`, `qumus2/runtime/recovery.py`, `qumus2/testbench/`,
`config/limits.yaml`, `config/bench.yaml`, `config/testbench.yaml`, `config/verification.yaml`.

**Starts from:** working sim arm/camera/locator, `PlaceChipOnTransferStage` as
the reference closed-loop step, thread-based job service, recovery planner.

**Tasks, in order:**
1. Fill out the sim world for the v1 bench: left arm + exfoliation stage, tape
   mechanism, heaters, microscope + stage; regions for each.
2. Port v1 molecule/assembly flows (`agents/processing_agent/workflows/`) as
   closed-loop Steps, starting with the graphene path: exfoliation → chip
   search → (flake picking happens in agents) → stacking. Each step has
   `verify()` or an explicit `NOT_VERIFIABLE("why")`; merge duplicated atoms.
3. Microscope replay from v1 `segmenter/test_images/` so the picker sees real
   flake images; sim flakes appear in the sample ledger after exfoliation.
4. Rendered desktop-camera images (top-down drawing of the sim world) saved as
   files so `Observation.images` and the UI have pictures.
5. Fault injection: pick failure, drop in transit, sensor stuck/offline, serial
   timeout, kill-the-process at any step; scenario tests per fault.
6. Job service as a separate process + supervisor/heartbeat (spec 5.4), same
   `JobService` interface; recovery on start-up.
7. Report of all `NOT_VERIFIABLE` steps (sensor wish-list, spec 4.2).

**Done when:** all hardware flows needed for the graphene device run on the
testbench through hardware tools, faults are detected/corrected or handed back,
kill-and-resume works, and safety violations are zero in the scenario suite.

---

## B. SDK agents (orchestrator + five subagents)

**Owns:** `qumus2/agents/` (roster, prompts, tools, policy, gateway, scripted
and SDK runtimes, credentials), `qumus2/interaction/`, `qumus2/records/`,
`config/agents.yaml`, `qumus2/testbench/scenarios/`.

**Starts from:** tool contract with 11 example tools, policy with three
cross-tool rules, scripted runtime + demo scenario, `sdk_runtime.build_options`
(wired but never run against the API; see the VERIFY comments).

**Tasks, in order:**
1. Port the tools of all five subagents from v1 (one module per agent under
   `agents/tools/`), as thin wrappers: hardware ones submit flows built by
   workstream A (agree flow names/params in the PR); query ones are code-written
   queries (no AI-written SQL). Port v1 `validate_selection_result` and
   `validate_device_guidance_for_layout` into the picker/designer submit tools.
2. Recovery menu tools for hands (re-shoot, re-place, skip chip, use another,
   ask human), each counted against a retry budget (spec 4.3 L1).
3. Prompts: port the role parts of v1 `CORE_IDENTITY` and each agent; move
   SKILL.md files into SDK skills; no hard rules in prompts.
4. Finish `SdkRuntime`: stream assistant/subagent messages into events
   (`parent_tool_use_id` → which agent), images in tool results, save
   `session_id` to the project and resume, `Stop`/`SubagentStop` hooks saving
   status, interrupt. Resolve every VERIFY note once the key works.
5. Scripted scenarios for "give me a graphene flake" and "make a graphene
   device" covering all six agents, used by tests and by the demo without a key.
6. Real-Claude test script (manual trigger, costs money): delegation, picking,
   mid-run instruction.

**Done when:** both demo requests run end to end in scripted mode in tests, and
with the key in real mode on the testbench; every hook denial shows its reason.

---

## C. UI

**Owns:** `qumus2/ui/` (backend endpoints beyond the contract, front end).

**Starts from:** FastAPI app with the full API contract and a plain placeholder
page that already shows events, the world snapshot, a message box and a stop
button.

**Tasks, in order:**
1. Front end as a single-page app with no build step required on the Mac
   (plain modules or a vendored bundle committed to `ui/static/`), so `pip
   install` + `qumus2 serve` stays the only setup.
2. Layout per spec 11.3: chat on the left (who is speaking: orchestrator → hands …),
   lab view on the right (top-down bench drawing from `world.snapshot` events,
   latest camera image, last verification with measured offset and whether it
   was corrected), plan and progress at the bottom (plan book with revisions,
   steps with time, click for records and photos).
3. Light and dark themes; restrained, premium look; motion only on state changes.
4. Testbench controls: fault injection buttons (askew placement, pick failure,
   device offline), simulation speed.
5. First version to Jialei for feedback (spec 11.3).

**Done when:** spec 11.5 items 1–4 are visible in the browser during a scripted
run, and the page works on a laptop screen in both themes.

## D. v1 vs v2 comparison (`v1compare/`)

Owns `v1compare/` and `tests/test_v1compare.py`. Runs a copy of v1
(`v1compare/v1/`, byte-identical to the lab zip, checked by `MANIFEST.sha256`;
never edit it) and v2 on the same testbench task, faults and ground truth.
Flows mode needs no model; agent mode uses real model replies and runs only on
the Mac with a cost estimate and the owner's OK. Depends on testbench
internals (world objects, fault injector, `SimMicroscopeAnalyzer`): a
testbench change that breaks `tests/test_v1compare.py` needs a fix here too.
