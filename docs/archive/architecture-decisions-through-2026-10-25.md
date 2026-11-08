# Architecture decision history through 25 October 2026

This preserves the chronological implementation notes formerly appended to
`docs/architecture.md`. Later decisions can supersede earlier ones. Read the
[current contracts](../architecture.md) and laboratory clarification before
using a historical note as a requirement. Document-relative links use the
new archive location; old `notes/` paths describe the original context.

## Decisions made in the skeleton (not in the spec before)

These are implementation details the spec left open; they are also recorded in
spec §12.4.

1. Package name `qumus2`, Python ≥ 3.11, pydantic v2, FastAPI, SQLite (WAL).
   `claude-agent-sdk==0.2.168` is an optional extra (`.[agents]`) so tests and the
   scripted demo need no SDK and no key.
2. Without a key the team runs on a **scripted runtime** that replays scenario
   YAML through the real gateway, policy, job service, safety layer and
   testbench; only Claude's words and choices are scripted.
3. All tool calls from any runtime go through one `ToolGateway`, so the SDK
   hooks and the scripted runtime share one policy implementation.
4. The UI reads only the run store; events are rows in the `event` table and
   the websocket tails it. This works unchanged when the job service becomes a
   separate process.
5. Time comes from a `Clock`; the testbench uses `SimClock` (fast-forward).
6. Hardware tools return a job id at once; `wait_job` waits at most 600 s per call.
7. API key env var is `QUMUS_ANTHROPIC_API_KEY` (optional
   `QUMUS_ANTHROPIC_BASE_URL`, `QUMUS_ANTHROPIC_AUTH`, `QUMUS_ANTHROPIC_MODEL_PREFIX`),
   passed to the SDK explicitly; never `ANTHROPIC_API_KEY` from the shell. With a
   base URL (lab gateway) auth defaults to Bearer (`ANTHROPIC_AUTH_TOKEN`), and the
   model prefix (e.g. `anthropic/`) is applied to every model id, including the
   CLI's background small model (`small_fast_model` in `config/agents.yaml`).
   The cloud container cannot reach the lab gateway, so real-Claude runs happen on
   the Mac; CI and cloud tests use scripted replies.
8. Jobs run on worker threads (`ThreadJobService`): in the app process by default,
   or in a separate lab process with `serve --lab-process` (decision 24).
9. All limit and tolerance numbers in `config/` are placeholders until the lab
   provides real values (spec 15.3 Q4); they must be replaced before stage 3.
10. UI (workstream C): the front end is plain ES modules in `qumus2/ui/static/`
    (no build step, no CDN). UI-only endpoints beside C12, used by nobody else:
    `GET /api/bench` (bench layout, read-only device states, objects, faults),
    `GET /api/plan/history`, `POST /api/sim {speed}` (scripted-agent pacing) and
    `GET /api/image?path=` (image files under the run-data directory only, so
    `Observation.images` paths should live under `runs/`). Job device commands
    arrive in a burst on the sim clock; the lab view replays them at a watchable
    pace and applies all live events in that order. All UI text is English
    (agent names Orchestrator, Advisor, Eyes, Hands, Picker, Designer) because
    the group has non-Chinese speakers. `websockets` is a runtime
    dependency so `/ws/events` works out of the box; the page falls back to
    polling `/api/events` if the socket fails. `POST /api/faults` also takes
    `stuck_at {device, value|null}` (sim heaters and switches; additive to C12).

## Decisions made in the testbench workstream

11. (Superseded by 29.) One world frame in the testbench (= the arm_right frame); `config/bench.yaml`
    keeps per-arm sections (via YAML anchors) so the real bench can calibrate
    each arm separately. The transfer stage is the motorised v1 "sample stage":
    chips on it move with it between the arm drop-off point and the microscope.
12. Ledger-dependent choices (which chips, which carrier, whether to prepare
    the mother tape) are made once at submit (`Flow.resolve_params`) and stored
    in the run's params, so `build_steps` stays deterministic for resume.
13. The material label of an exfoliated chip comes from the carrier's ledger
    entry (since 29: the slide's slot on the left board; slides carry no QR) via
    the `tape` sample in the ledger, never from an agent (spec 7.1).
14. Heaters are ramped in steps within `max_ramp_c_per_min`; every wait has a
    timeout (needs human) and a stuck-sensor check (dangerous).
15. v1 switched the exfoliation-stage vacuum off before the pre-heat peel; v2
    keeps it on (with it off the chip can come off with the tape). Lab to confirm.

## Decisions made in the SDK agents workstream

Also recorded in spec §12.5.

1. `SdkRuntime` was run against the bundled Claude Code CLI (2.1.301, SDK 0.2.168)
   with a fake Messages API (`tests/fake_anthropic.py`); `tests/test_sdk_runtime.py`
   runs in CI (CI installs `.[dev,agents]`). No key or network needed.
2. Credentials: the CLI subprocess inherits the parent environment, including a
   host-managed Claude login (seen in the cloud container: requests went out with
   that token instead of ours). `scrubbed_env` blanks every inherited
   `ANTHROPIC_*` / `CLAUDE_*` variable and sets only the lab gateway key, so a
   Claude subscription token can never reach the gateway. The CLI gets its own
   config dir `runs/claude/` (session transcripts live there; resume needs them).
3. Subagents run in the foreground (`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`,
   `background=False`): the Agent tool returns the subagent's final report, so
   delegation is synchronous and hardware work stays in plan order.
4. Agent attribution: hooks get `agent_type` (our subagent name). The tool
   function gets no `tool_use_id`, so PreToolUse adds a hidden `_qumus_call`
   argument via `updatedInput`; a call that did not pass PreToolUse is refused.
5. What subagents say: the CLI does not forward subagent text, only tool calls;
   `_Narrator` reads the subagent transcript after each of its tool calls and at
   SubagentStop. `agent.done` is emitted from SubagentStop with the clean final
   report.
6. The orchestrator's tool list shows every `mcp__qumus__` tool (the CLI has no
   per-thread MCP filter that leaves subagents alone); PreToolUse denies the
   ones it may not use.
7. User instructions mid-run: the orchestrator consumes them at its next tool
   result or when a subagent returns; a busy subagent sees each one once as a
   heads-up without consuming it.
8. Skills: SKILL.md files under `agents/skills/skills/` load as a local plugin
   `qumus` and are preloaded per subagent (`skills:` in `config/agents.yaml`).
9. C7 additions: `agent.turn_end` (session_id, is_error, num_turns, cost_usd,
   reason); `agent.delegate` and `agent.done` carry `tool_use_id` where known.
10. Real Claude is opt-in: `qumus2 serve --claude` / `build_services(use_claude=True)`.
    The default is the scripted runtime even when `QUMUS_ANTHROPIC_API_KEY` is set,
    so starting the UI never spends credit by accident.
11. Device plan (state, from the advisor's `submit_device_plan`): layer stack top
    to bottom; each flake layer gives a role, material, thickness labels and
    minimum area. `flake_roles` (with counts) is what the picker must fill. A
    plan without succeeded precedent runs is flagged NEW (notification).
12. Picker and designer hand-offs are code-checked tools, not prompt rules:
    `submit_flake_selection` (ports v1 `validate_selection_result`) and
    `submit_design` (stage 1: rectangles in um; roles covered, channel minimum,
    hBN covers the channel, 3.0 um gate-to-contact clearance). An accepted
    selection or design takes effect at once; a veto (`exclude_flake`) of a
    selected flake resets selection and design. `report_design_infeasible`
    hands the combination back and that exact set cannot be selected again.
13. `require_optical_images` (config/agents.yaml `picking:`) is on: a flake can
    be selected only if its 100X and 20X images exist and the picker viewed
    them. ":memory:" stores write no images, so tests there switch it off.
14. Hands' recovery menu: `reverify_job`, `retry_job`, `replace_chip`,
    `skip_chip`, with a budget (`recovery:` in config/agents.yaml, per job
    including its replacements, and per project) enforced by the
    `recovery_budget` rule; after that only `request_human_help`.
    `stack_device` needs `flakes_selected`, `design_accepted` and
    `stack_matches_selection` (exactly the code-checked set).
15. Scripted scenarios may use `pick_flakes` / `lay_out`: stand-ins for the
    picker's and designer's judgement that call the same tools through the
    gateway. Demos: `place_chip_demo`, `graphene_flake`, `graphene_device`.
16. The CLI cuts an MCP tool call off after 60 s by default; `scrubbed_env` sets
    `MCP_TOOL_TIMEOUT` to 660 s so `wait_job` (up to 600 s) returns its result.
17. A flake selection lives in project state only; the ledger status stays as
    the search left it (`catalogued`), which is what `stack_device` accepts.
18. Spend limit: `max_budget_usd` (config/agents.yaml, lowered by env
    `QUMUS_MAX_BUDGET_USD`) is passed to the CLI, which stops a turn midway once
    a model call crosses it (`agent.turn_end` reason `budget_exhausted`,
    `is_error` true); our own cost events only arrive at turn end, so watching
    them cannot stop a turn. That cap is PER TURN. 2026-10-22 (leader's
    acceptance night): a run budget on top of it. `build_services(...,
    run_budget_usd=)` → `SdkRuntime.run_budget_usd` (the scripts' --budget:
    record_demo, real_claude_check per check, v1compare --budget-usd); the next
    turn's CLI cap is `min(per-turn cap, what is left)` (the client is
    restarted with the lower cap and resumes the session), and once
    `store.spent_usd()` (summed `agent.turn_end` cost_usd; the UI may show it
    as `/api/health` `cost_usd`) reaches the budget `SdkRuntime.budget_exhausted`
    is true: `send` returns without a model call, one danger notification
    ("budget exhausted") is emitted, and `ConversationDriver` drops later
    messages (`user.dropped`, reason). Jobs already submitted finish.
19. Tool calls the CLI refuses before any hook runs (input that is not valid
    JSON) are logged as `tool.denied` with rule `input`.
20. The lab gateway breaks streamed replies with parallel tool calls (only the
    first block gets a start event; every input lands under index 0, glued).
    Plain replies are correct. With a base URL set, `Credentials.unstream` is on
    (env `QUMUS_ANTHROPIC_UNSTREAM=0` turns it off) and the CLI talks to
    `UnstreamProxy` on 127.0.0.1, which sends each request unstreamed and replays
    the reply to the CLI as server-sent events (`qumus2/agents/unstream_proxy.py`).
21. Delegation rules (PreToolUse on the SDK's Agent tool, `policy.check_delegation`):
    no delegation before a plan exists (rule `plan_first`); the hands only after
    the eyes have been sent once in the project (rule `eyes_first`, project state
    `eyes_checked`). Denials are `tool.denied` with tool `delegate`.
22. Loosening a device-plan requirement (a flake role's thickness labels, minimum
    area, material or count, or dropping the role) is refused in
    `submit_device_plan` unless the designer's latest `assess_relaxation` verdict
    for that role accepted at least that much (project state `relaxations`). An
    accepted relaxation is announced as a warning notification. No human gate:
    if the designer says no, more material is exfoliated (Jialei, 2026-10-21).
23. Device-plan thickness labels are normalised to the ledger's words
    (`core.materials.THICKNESS_LABELS`, e.g. "1L" -> "monolayer", "20-30 nm" ->
    "20-30"); unknown labels for Gr and hBN are refused. `get_device_plan`
    (advisor, designer; 2026-10-22 also the orchestrator, it is read-only and
    a real run was denied it) returns the accepted plan.
24. Testbench: `exfoliation.layers_by_material` gives hBN its own thickness
    distribution (mostly 10-50 nm); before, hBN shared graphene's 1-60 layers
    and never exceeded 20 nm.
25. `Step.before_park(ctx)` (additive, default no-op): restart recovery calls it
    before device safe states when it parks a run. Stacking's AlignStamp,
    PickUpFlake and DropDown lift the stamp. Before, a kill while the stamp
    pressed on a chip parked with the stamp still down (seen once hBN got its
    own thickness distribution and the crash test hit drop_down).
26. Plan step status is kept by code, not the prompt. `ToolGateway.delegation_started`
    (SDK: PreToolUse on the Agent tool; scripted: `delegate`) marks running the
    step whose id starts the description ("s3: ..."), else the first unfinished
    step owned by that agent; `delegation_finished` (the Agent tool result;
    scripted: `done`, optional `step_status`) marks it done, or failed on an
    error result; `turn_finished` puts open steps back to pending after a bad
    turn and completes the orchestrator's own steps after a good one.
    `RunStore.set_step_status` changes the latest revision in place and emits
    `plan.step` (additive event: project_id, revision, step_id, status, plan).
    `update_plan` keeps the status of a step with the same id and owner (id given,
    or title unchanged) when the rewrite leaves it at pending. 2026-10-22: when
    one hands delegation runs several flows, each job marks the hands step whose
    title names its flow (`gateway._FLOW_WORDS`: exfoliat / search / stack /
    place ...), and the delegated step is judged by its own flow(s) only; a
    real run showed "Exfoliate Gr" failed although only the search after it
    had parked.
27. The CLI shows the main thread every tool of the MCP server (`disallowed_tools`
    hides a tool from subagents, not from the main thread), so tools that are not
    the orchestrator's start their description with "[<agents> only: delegate to
    it]". `PlanStep` accepts `step`/`agent` for `title`/`owner` and "user" for
    "human" (what every real run wrote first).
28. Scripted runtime: a template that needs data an earlier step did not return
    (failed call, parked job) stops the turn: the agent that owned the parked job
    calls `request_human_help` with it and the orchestrator says why, instead of
    a KeyError.

16. Testbench pictures (C1 addition, additive): `Frame.save` is an optional
    callback and `Frame.saved_path()` writes the picture once, on demand, and
    returns its absolute path. Steps call it only for photos they keep
    (verification photos, flake revisits), so autofocus sweeps write nothing.
    Sim desktop cameras draw a top-down view of the bench; the sim microscope
    copies a real photo from a replay folder (`QUMUS2_MICROSCOPE_REPLAY_DIR`
    or testbench.yaml `microscope.replay_dir`, one sub-folder per material,
    e.g. `graphene/`, `hbn/`) or draws one. Files live under
    `<db dir>/images/{camera,microscope,microscope/replay}`; ":memory:" runs
    write no pictures. `Observation.images` and flake `props.images` carry
    those paths.
17. Fault injection has one entry point, `lab.faults.inject(name, params)`
    (`qumus2/testbench/faults.py`, menu in `FAULTS`, served at `GET /api/faults`;
    `POST /api/faults` now accepts every name there, 400 for unknown ones).
    Faults: misplacement, pick failure, drop in transit, device offline,
    serial timeout, stuck heater sensor, stuck vacuum, and `crash`.
18. `crash` kills the job worker before the Nth device call with a
    BaseException, so no step cleanup runs (like kill -9). The app then calls
    `ThreadJobService.recover()` (additive, not in the C5 Protocol yet): it
    restarts dead workers and runs `plan_recovery`, resuming or parking every
    run left `running`. Tests kill at every device call of
    `place_chip_on_transfer_stage`, every third of `exfoliate_chips` and a
    spread of `search_chip`; all resume to the same end state.
19. Recovery that parks a run now puts the in-progress step's devices in
    their device-level safe state first (heater off; arms are never moved).
    A move step killed with the object on the nozzle (vacuum on, object at
    neither end) resumes by putting it down at the destination instead of
    parking. A move whose object is not at either end looks at the whole
    table (`workspace` region) and re-picks a single stray object (dropped
    on the way).
20. Stacking hardware (C1/C2 additions, additive): `StampManipulator` (v1
    "transfer arm", axes `l x y z` in mm, `move_axis_to / get_positions`) with
    `ManipulatorLimits` in `limits.yaml`, plus `stamp_interlocks`: while the
    stamp hangs low over the stage the transfer stage may not move, and arm_right
    may not go low under the stamp. Sim devices: `transfer_arm`,
    `transfer_arm_vacuum` (holds the stamp; kept on in safe state). Stamps are
    samples of kind `stamp` in `stamp_slot_N`; the arm hands them to the
    manipulator at `stamp_handoff`.
21. Testbench stamp model: a dome of radius 1.5 mm whose contact radius grows
    as sqrt(2 R depth); `touch_z` differs per stamp, so every pick-up and the
    drop-down first touches, measures where the contact lands and re-aims
    (`AlignStamp`). Lifting off below the film's melt temperature picks up the
    flakes inside the contact (probability per material, much lower when
    cold); above it the film and the stack stay on the chip. Faults
    `pickup_fail_next` and `release_fail_next` exercise the corrections
    (press deeper; heat 5 °C more).
22. The microscope measures a chip's rotation from its bottom-left and
    bottom-right corners (`MeasureChipRotation`, now part of `search_chip` and
    of every chip load in `stack_device`): the desktop camera's ~0.2° error
    puts a flake 1 mm from the corner ~70 µm off, too much to find it again
    at 20×. A new placement on the stage clears the stored rotation.
23. `stack_device` is not resumable blind at two points: a pick-up or the
    drop-down killed while the stamp touches the chip waits for a human (the
    stamp is lifted and the heater switched off first); the drop-down is
    `safe_to_resume = False`. Everything else (loads, refinds, aiming) resumes.
    The drop-down is verified by checking nothing is left on the stamp and
    every material is present within 25 µm of the target (decision 26).
24. The lab side can run as its own process (spec 5.3, 5.4): `qumus2 serve
    --lab-process` (or `build_services(..., lab_process=True)`) starts
    `qumus2 lab` under `runtime/supervisor.Supervisor`. The lab process owns the
    devices, safety gate, job service and (testbench) the simulator; the app
    process keeps agents and UI and reaches it through local RPC
    (`runtime/rpc.py`: 127.0.0.1, random per-start auth key, an allow-list of
    methods). `RemoteJobService` keeps the C5 interface (`status` reads the
    shared store, `wait` rides out a restart); `RemoteLab` gives the UI the same
    read-only view (`world.snapshot()`, `devices.devices`, `faults.inject()`,
    `bench`). Heartbeat: new `heartbeat` table (C6, additive), wall-clock time,
    every 0.5 s. The supervisor restarts the process when it exits or its
    heartbeat is older than 5 s (kill -9 first), at most 5 times in 10 min,
    then leaves it stopped with a danger notification; on start it stops a lab
    process an earlier app left running. A lab process that finds its
    predecessor did not stop cleanly puts every device in its safe state, then
    runs `recover()`. The simulator saves its physical state
    (`<db>.testbench.pkl`, e.g. `runs/qumus.testbench.pkl`) before every job device
    command and the new process restores it without re-seeding the sample ledger
    (only when that database already has a ledger), so a restart
    sees the bench the dead process left. In this mode `crash` really exits
    the process and the new fault `hang` freezes it. Event timestamps written
    by the app process use real time, those of the lab process simulated time.
25. NOT_VERIFIABLE report (spec 4.2): `qumus2 report` runs every registered
    flow once on a fresh testbench (`testbench/report.py` `SCENARIOS`; a test
    fails when a new flow is missing there) and writes the steps that logged
    `not_verifiable`, grouped by step class with their reasons, to
    `runs/not_verifiable.md`. `collect_not_verifiable(store)` works on any run
    store, so the same list can be made from real lab runs. Today one step is
    on it: `PressTape` (no force sensor on the exfoliator).
26. Flow code uses only what real perception can give. It never reads
    simulator truth (`frame.meta`, detection `meta["sim_id"]`): whether a chip
    is under the objective is `MicroscopeAnalyzer.chip_in_view(frame)` (C13,
    additive), and detections of several looks are the same flake when closer
    than 3 µm (`SAME_FLAKE_MM`). A test strips the sim ids from every detection
    and stacks a device. Because outlines cannot tell two layers of one
    material apart, the drop-down no longer counts layers per material; it
    checks that the stamp is empty and every material is at the target (every
    layer was confirmed on the stamp at its pick-up). Search still stores
    `sim_id` in flake props, only as test bookkeeping.
27. Microscope replay picks a real photo of the same thickness class when the
    replay folder says it (sub-folder or file name such as
    `graphene/monolayer/`, `hbn/x_20-30.png`, `gt40` for ">40"), else any photo
    of the material. Size and shape of the photographed flake still differ
    from the ledger flake: the picker's choice in the testbench is judged on
    the sim measurements, not on what the photo shows.
28. `v1compare/` (outside the `qumus2` package) runs a byte-identical copy of
    v1 against the testbench for the v1-vs-v2 comparison. It replaces only
    v1's hardware, camera and perception boundaries (see
    `v1compare/README.md`) and drives the testbench sim devices directly, not
    through `SafetyGate` (C2): v1 has no such layer and the comparison must not
    add one. It never touches real hardware. No contract changed; it reads
    testbench internals (`world`, `faults.before_call`) and wraps the fault
    injector per trial process.
29. The testbench follows the real lab's layout (Jialei, 2026-10-21; paper
    Fig. 1d), `config/bench.yaml`: three storage boards (left: material slides
    and bare chips; middle: exfoliated chips and the hand-over between the
    arms; right: stamps and target/finished chips) with a QR marker in each
    corner and black slot frames; top row exfoliation stage, search & stacking
    stage, reactor (placeholder port; its arm is `planned`). World frame: table
    front-left corner, x right, y away. Each arm commands in its own frame
    (pose = world - `base`, a translation; real calibration later) and serves
    only its stations (`reach`, plus `reach_mm` in the SafetyGate):
    arm_left left board / exfoliation / middle board, arm_right middle board /
    transfer stage / stamp hand-off / right board. Helpers in
    `qumus2/flows/bench.py` (`slots`, `positions`, `regions`, `reachable`,
    `arm_positions`, `to_arm`, `to_world`). Steps report poses (expected,
    measured, ledger) in the world frame; `measurements.arm_pose` keeps the arm
    frame for the correction.
30. One overview camera (`overview_camera`) sees the whole table (Jialei: one
    overall camera plus the microscope camera; v1's code opened two, v1compare
    keeps that). Objects carry no QR: a detection has no object id, the flow
    knows an object by the slot it sits in. Detector error model
    (testbench.yaml `locator`): Gaussian jitter with a heavier tail (v1's
    tracking log), misses, phantom chips, bare/exfoliated confusion; board QR
    markers unreadable and dropped frames raise `TransientError`. Flows look
    with 3 frames and take the median of the objects seen in most of them; a
    look that should find an object and does not is repeated with 5 frames
    before the step concludes it is missing.
31. Exfoliator: tape on two cylinders, the upper one on an x-z gantry
    (apply path right, down, left, down); `peel(speed, angle_deg)`; the angle
    is a flow parameter (agent-adjustable within limits.yaml [10, 90]); its
    effect on yield and on lifting the object is a probability model, not
    physics. The white-tape roll is finite (3000 mm): an empty roll raises
    `NeedsHumanError`. Exfoliation outcomes draw from their own random stream
    (seed, chip, tape segment, use), so faults and extra looks do not change
    which flakes a seed puts on a chip.
32. Microscope: the stamp film and the chip are two focal planes
    (`sharpness_chip`, `sharpness_stamp`, `focus_plane()`); flakes, corners and
    contact rings are only seen on the plane in focus. Checking flakes on the
    stamp refocuses up by the stamp's height above its touch point (narrow
    sweep) and back to the chip, as the lab does while aligning.
33. Arms: travel takes clock time (`motion` in testbench.yaml). A controller
    reset leaves an arm un-homed: the SafetyGate refuses moves; every flow arm
    move goes through `move_arm()`, which homes first and comes down from safe
    height (a held object stays on the nozzle), so a reset between two moves
    of one pick-and-place is not a failure. An unclean lab-process restart
    resets the arms (`process.restart_loses_arm_position`). Lost steps
    (`arm_step_loss`) show as the same placement offset twice (within 0.5 mm
    or half the offset); MoveObject then homes the arm before the next
    re-place, and it always homes before its last try.
    A run killed between release and check leaves the object at the
    destination, off target, with the ledger still at the source: the probe
    resumes the step and MoveObject re-places it from where it lies (only
    when no other sample is booked at the destination and the source is empty).
34. New faults: `camera_frame_drop`, `qr_unreadable`, `chip_removed`,
    `arm_step_loss`, `arm_reset`, `stamp_torn`, `tape_roll_empty`,
    `perception` (detector error rates). `qumus2 chaos` runs the graphene task
    on many seeds with random faults at random moments and classifies each run
    against the simulated world (ok / honest stop / false success / error);
    its counts are robustness numbers for the software, not lab rates.

## Decisions made in the self-evolution work (spec 10)

Contract changes, all additive: C6 gains the tables `meta` (store-wide `bench`),
`experience`, `experience_vote`, the run columns `bench` and `meta_json`
(old database files get them on open) and the methods `update_run_meta`,
`get_instruction` and the experience methods; C7 gains `experience.proposed`,
`experience.voted`, `experience.status`, `experience.taught`,
`outcome.recorded`; C9 gains the rules `experience_checked`, `runs_reviewed`,
`exploration_budget` (all on `exfoliate_chips`); the UI gains the UI-only
endpoint `GET /api/experience` and the renderer `static/experience.js`
(`renderExperience(container, data)`, no fetching inside, for replay mode).

29. Experience comes only from real-lab runs. A run store records its bench
    once (`meta.bench`, default `sim`); opening it as another bench raises, so
    testbench and lab records never share a file. Every run row copies the
    bench. `records/experience.py` refuses sim runs as evidence or votes.
    Tests use `RunStore(experience_sandbox=True)`: sim runs then count, every
    row is marked sandbox, and the lab view (`/api/experience`) never shows
    sandbox rows.
30. Status by rule only (`config/experience.yaml` `votes:`): trial ->
    confirmed after 3 supporting real runs from 2 batches (batch = project),
    at most 1 against; retired after 2 or more against that outnumber the
    support. A person's words (`teach_experience`, needs the id of their
    message) start confirmed; seeds from v1 code and prompts (`seed:`) start
    on trial and are labelled "not verified by v2 experiments". Retiring by a
    person also needs their message id.
31. Exfoliation result = good flakes per searched chip of that run's chips.
    Good flake (Jialei, 2026-10-21): thickness label in the run's target,
    area at least its minimum, surface residue not `heavy`. The target is
    recorded at submit (from the device plan's roles of that material, or
    the hands' `target_*` arguments) in `run.meta.target`, with the purpose,
    hypothesis, expected good flakes per chip and the conditions at submit
    (tape material and uses, tape roll left, crystal batch; humidity and room
    temperature stay None until the lab can measure them).
32. Secure first, explore with the rest (Jialei): `exploration_budget`
    allows an `explore` exfoliation only on spare bare chips: bare chips now
    minus ceil(1.5 x good flakes still missing / good flakes per chip) minus
    task chips already exfoliated and not searched (per chip: from 2 or more
    searched runs of this bench, else 0.5). An explore run changes exactly
    one recipe parameter from the baseline, states a hypothesis and uses at
    most 2 chips. `secure` runs must use the baseline recipe only when it is
    confirmed (a prior recipe is a suggestion); `user` runs need the id of
    the person's message.
33. Code votes on recipe experience that carries an expected result
    (`expect.good_per_chip`): a finished, fully searched real run with that
    recipe supports it when it reached the expectation, else it is against.
    `review_runs` builds the facts of every unreviewed real run and applies
    these votes; on the real bench `runs_reviewed` refuses the next
    exfoliation of a material until its finished runs are reviewed.
34. The picker judges `surface_residue` (clean, light, heavy, uncertain) for
    every candidate from the 100X image; a selected flake must have one and
    it must not be heavy. Judgements are stored in the flake's
    `props.residue`; heavy-residue flakes are no longer candidates.
35. `ExfoliateArgs.peel_angle_deg` (default 30) is passed to the flow; a flow
    that does not have the field yet ignores it, and recipe keys include it
    only when the run's params have it.

## Decisions made in the UI workstream (recording and replay)

1. Recording (`qumus2/ui/recording.py`): `qumus2 serve` and
   `scripts/record_demo.py` attach a `Recorder` that tails the run store and
   appends to `<db dir>/timeline.jsonl` the wall-clock time of new events and,
   when it changed, the bench state (`ui.app.bench_state`, the same dict
   `GET /api/bench` returns). It only reads. The run store's clock is the
   testbench's simulated clock, which stands still while Claude thinks, so
   event `ts` cannot pace a replay; the timeline can. Not a contract change:
   nothing outside the UI reads the timeline.
2. C6 addition (additive): `RunStore.last_event_id()`.
3. `qumus2 export --db ... --out recordings/<name>` writes `recording.json`
   (meta, layout, events with `wall`, bench frames, runs with steps, plan
   history) and `media/` (photos, paths rewritten). Exports are scrubbed:
   values of `QUMUS_ANTHROPIC_API_KEY` / `ANTHROPIC_*` keys, `sk-...` and bearer
   tokens are replaced and home-directory paths shortened to `~/`; the export
   refuses to write if anything key-like is left.
4. `qumus2 replay [folder]` serves the same front end in replay mode
   (`/api/health` mode `replay`, `/api/recording`, `/api/recordings`,
   `/api/image` from the recording's `media/` only). The page plays the
   recording itself; no agents, testbench or key. Testbench recordings under
   `recordings/` are committed on purpose as the demo (simulated data only,
   scrubbed); lab run data still never goes into git.

5. Replay time (2026-10-22, Jialei's item 11 "the clock does not run
   evenly"): the player's clock is the original run's wall clock and advances
   `speed` seconds per real second, everywhere. Device bursts are not
   stretched (the first version added 0.12 s per device command and waited
   for the bench animation, so the clock crawled through bursts at ~0.5x and
   jumped between them); the bench eases each frame in at most 0.12 s / speed
   and coalesces frames it cannot show in time. The one exception: a pause
   longer than `MAX_GAP_S` (5 s) plays for 5 s and the rest is skipped, with a
   "skipped N s" line in the conversation and a grey mark on the timeline.
   The timeline is linear in that compressed run time; the clock shows the
   original run time (it ticks between events). Checked by
   `tests/test_ui_browser.py::test_replay_clock_runs_at_speed`. Since decision
   6 this is the `session` axis; `lab` is the default.

6. Lab time (2026-10-22, Jialei: "the replay is too fast to see what the
   instruments did"): decision 5's wall clock gives device actions ~0 s, since
   the testbench's `SimClock` (speed 0) runs them in no wall time (a whole
   `stack_device`, 31 min of lab time, took 1.7 s). The replay now has two
   axes and plays `lab` by default: each gap between events lasts
   max(wall gap, `ts` gap), so instruments take their real time and Claude's
   turns take theirs. Gaps over `MAX_GAP_S` still play for 5 s; a pause is
   skipped as before (feed line, grey tick), a lab wait (heating, cooling, a
   slow lift: the `ts` gap is the larger) is fast-forwarded, said in the
   bench caption, with no feed line. `session` is decision 5 unchanged.
   Default speed 4x. In lab time a device command's lab frame is eased in
   from the command to the frame (the move's own duration), not 0.12 s.
   The page adds, replay only: a caption above the bench (instrument, step,
   step k of n, the step's check), the folded "Hardware jobs" column as one
   row per flow step (click = seek to its start; `actions.js`), and step by
   step (stop before every `step.started`; Step / ← → keys). The bench view
   may offer `highlight(deviceIds)`; the page calls it when present. No
   recording format change. Checked by
   `tests/test_ui_browser.py::test_replay_lab_time_and_steps`.

## Decisions made in the UI rework (2026-10-21, Stop / Continue and Claude status)

Plan: notes/v2-ui/ui-rework-plan.md (Jialei's 15 items). Step 1 is backend only.

1. Stop = "a crash at a safe point", Continue = restart recovery. Stop sets each
   job's cancel event; `FlowContext.check_cancel` (called between device
   commands) raises `StopRequested` (`core/errors.py`). `StepRunner` lets it
   through without running the L0 ladder, so the step stays in progress, as
   after a crash. The executor then pauses the run: the in-progress step's
   `before_park` (stamp lifted; arms are never moved and vacuum stays on),
   the job's heaters switched off with their setpoints saved, run meta
   `paused = {seq, step, in_progress, heaters}`, status `paused` (C5, C6
   `RUN_STATUSES`). Cancel before the locks or between steps pauses the same
   way with `in_progress = false`.
2. Steps that must not be cut (`safe_to_resume = False`, e.g. heating with the
   tape on, the stacking drop-down) run to their end first: `StepRunner.run`
   hides the cancel event inside them and passes it on afterwards. A step can
   override this with `Step.pause_inside` (C3, additive).
3. `resume(run_id)` runs `recovery.decide` (the probe used after a crash:
   done → next step, not done → this step again, unknown → park for a person);
   resumed jobs first set the saved heater setpoints again and wait for the
   temperature, then go on. `resume_all()` resumes every paused job and also
   clears the cancel of jobs that Stop reached but which had not paused yet.
   `retry` of a paused job resumes it; `reverify` refuses a paused job.
   `recovery.plan_recovery` now uses the same `decide` + `park`.
4. `stop_all` no longer calls `all_safe_state` (that cut steps in half and
   left runs `cancelled`/`waiting_human`, not resumable). It sets the cancel
   events and switches off heaters that no job holds. `cancel(run_id)` stops
   the same way but ends the job `cancelled` (not resumable). `RemoteJobService` and
   the lab-process RPC carry `resume` and `resume_all`.
5. Driver (`agents/driver.py`): `stop` marks undelivered messages `dropped`
   (event `user.dropped`), interrupts the runtime, pauses jobs and emits
   `system.stopped`; `continue` (instruction kind, C6) resumes jobs, emits
   `system.continued {resumed: [{run_id, flow, from_step, step, reason}],
   waiting_human: [...]}` and calls `AgentRuntime.continue_turn(text)` (C11)
   with a note saying where each job goes on. A message sent after Stop
   without Continue carries a note listing the paused jobs. An exception in a
   turn becomes `agent.error {error, type}` (scrubbed) instead of vanishing.
   New C7 kinds: `agent.error`, `user.dropped`, `system.stopped`,
   `system.continued`.
6. `wait_job` on a paused job returns `ToolResult(status="paused")` (C8
   `ToolStatus` gains `paused`). The scripted runtime suspends its turn at
   Stop and goes on at Continue; the SDK runtime's `continue_turn` sends the
   note as the next user turn.
7. Claude status (`agents/health.py`): in SDK mode the app checks
   `GET <base>/v1/models` with the same key and auth header at start-up, every
   5 minutes and on `POST /api/claude/check` (UI-only). It sends no prompt and
   costs nothing. States `ok unreachable auth_failed error unknown`; scripted
   mode reports `scripted`. The key never appears in the result. Secret
   scrubbing moved from `ui/recording.py` to `core/scrub.py`.
8. `/ws/events` closes connections whose `Origin` is another site (a page
   from elsewhere open in the same browser could read the event stream).
9. Fix found by the Stop matrix: `AlignStamp` lowers from `approach_z` when the
   stamp is above it (after a pause lifted the stamp, the re-run used to start
   from the lifted height and find no contact).
10. Tests: `tests/test_stop_continue.py` stops before every device call of
    `place_chip_on_transfer_stage`, every third of `exfoliate_chips`, a spread
    of `search_chip` and `stack_device`, then continues; each run must end as
    an uninterrupted run (samples, flake counts, positions), with no safety
    violation, nothing left held and heaters back on their setpoints.

### Step 2 (2026-10-21): bench geometry, lab twin, camera previews

11. Lab twin (`runtime/twin.py`, `LabTwin`): device readings (arm world poses,
    vacuum, homed; stage poses; manipulator axes and head point; exfoliator
    gantry and upper-cylinder x; objective) plus objects from a callable
    (testbench: the sim world; real hardware later: ledger + measured
    positions). `SafetyGate(frames=twin.delta)` logs the changed parts after
    every command as `lab.frame` (C2, C7 additions); commands that change
    nothing (captures) make no frame. Reads only, never temperatures (a reading
    would count as a device call). `GET /api/bench` adds `twin` (full state).
    The page will animate from these frames instead of guessing from commands.
12. Geometry (bench.yaml, placeholders): the exfoliator's fixed cylinder is
    `exfoliator.origin` (right of the stage, at the stage's y); the upper one
    is at world x = origin.x + gantry x, so during apply it moves left past the
    stage and the tape covers the chip. The stamp holder runs on a rail
    (`transfer_arm.rail_dir`, 1 mm of l = 1 mm in the world); l = over_stage_l
    at ref_xy is the microscope centre. The hand-off point is the head at the
    load pose (`flows/bench.py stamp_handoff`, no longer a bench.yaml entry;
    it is above the right storage board). `park_l` and home l are now 30 (the
    holder parks above the right storage; v1 used 160). `parked_xy` is gone:
    the sim head moves continuously with l. Stamps are slide-sized (25 x 40 mm).
13. Camera previews: `GET /api/camera/{id}/latest` (UI-only) returns
    {device, image, timestamp, meta}: the overview camera draws the table now
    to one live file (testbench); the microscope returns its last saved
    micrograph with objective, focus and the sharp plane. Previews are not
    commands: no limits, no log, no fault injection, no random numbers.
    The lab process serves `lab.twin_state` and `lab.preview`.


### Step 3 (2026-10-21): the page

No contract changes; the page reads the events and endpoints above.

14. Front end split into modules under `ui/static/`: `ui.js` (helpers, safe
    Markdown), `feed.js` (conversation), `team.js` (team graph), `stations.js`
    (bench drawing), `labstate.js` (lab state + tweens), `camera.js` (camera
    and last check), `planbook.js` (plan, device plan, flake selection,
    layout, folded job list), `app.js` (wiring, top bar, live link, replay).
    `bench.js` (the pre-station view) is gone: every layout has stations.
15. One event path: history and replay seeks apply events at once; live and
    replayed events go through a queue so the conversation, plan, jobs and
    bench stay in step while the bench eases each `lab.frame` (ms grow with
    distance; a backlog plays faster). Drawing runs in one
    requestAnimationFrame loop that only touches SVG attributes. Recordings
    without `lab.frame` (made before step 2) fall back to working out frames
    from `device.command` events.
16. Conversation: hand-offs are cards (subagent words and tool calls folded
    inside; job results stay visible); Qumus's own tool calls fold into one
    line; tool results pair with calls by `tool_use_id`; within-tolerance
    checks are not listed. The orchestrator is called "Qumus" on the page.
    No message-kind tags: the composer sends plain messages.
17. Top bar: Claude status chip (scripted / ready / working / unreachable /
    key refused / error, from `/api/health` and `agent.error`; click checks
    again), a small server-link dot, Stop and, after Stop or while a job is
    paused, Continue. The fault menu and sim speed sit in a "Testbench" menu
    that appears only when `/api/faults` answers (sim speed only in scripted
    mode). After a reconnect the page reloads if the server has another run
    store (event ids went back or the project changed).
18. Lab: device status lists only safety-relevant items (offline, stuck,
    errors, heaters on, things held, unhomed arms, lowered stamp, tape low,
    tape applied), else "All systems normal". Camera card: Auto / Overview /
    Microscope (Auto = microscope while `search_chip` or `stack_device`
    runs), last check below it.

## Testbench rework (2026-10-21, notes/v2-testbench/testbench-rework-plan.md)

### Step 1: review fixes

1. Lab-process restarts keep each motorised surface's offset
   (`testbench/persist.py`), so a chip on a moved transfer stage still rides it.
2. A held object keeps its pick offset and angle relative to the nozzle
   (`SimObject.rel_xy`); placement adds the drop error on top. Consequence:
   lost steps before a pick shift pick and drop alike and cancel; only steps
   lost while carrying misplace the object (tests inject them mid-carry).
3. The overview detector has a per-object bias that stays the same from frame
   to frame until the object moves (`testbench.yaml locator.bias_sigma_mm /
   _deg`), so multi-frame voting removes only the per-frame jitter.
4. `ContactDetection.film_intact` (C13, additive): a torn stamp film shows as a
   broken contact front; `PickUpFlake` then hands back "film is torn, put on a
   new stamp" instead of pressing harder.
5. `FaultInjector.clear()` also restores the configured pick-failure and
   big-error probabilities; what a fault already did to the bench stays.
6. Simulator ids (`sim_id` in ledger props and detections, kept for scoring
   and tests) are stripped from every tool result Claude reads
   (`ToolResult.to_text`).
7. v1compare: a true success also needs the records to put every exfoliated
   chip where it is (`ledger_ok`), the same rule as `qumus2 chaos`; extra
   chips are reported as a cost column.

### Step 2: world modelling (Jialei's drawings of the exfoliation and stacking stages)

1. Stamp holder (`transfer_arm`) is an x/y/z translation stage, not an arm:
   head = `origin` + (x, y), z = height. It is over the transfer stage when the
   head is within `over_stage_tolerance_mm` of the microscope centre
   (`flows/bench.py over_stage`). `manipulator_head` treats a missing axis as
   the park pose. Holder moves and stage moves take time on the clock
   (`testbench.yaml motion`).
2. The transfer stage has a real z: raising it brings the chip towards the
   objective and the stamp. Contact depth = touch height + stage height -
   holder z; pressing more than `stamp.crush_depth_mm` past touch logs a world
   collision (`SimWorld.collisions`, persisted) and tears the film. The gate
   checks the gap, lets a blocked stage only go down, and refuses a holder z
   that a raised stage makes too low.
3. Exfoliator, seen from the front: a fixed roll and a movable roll on an x/z
   gantry, the tape is the straight line between them
   (`flows/bench.py exfoliator_rolls / tape_height`). The tape is "laid" on a
   chip only when it spans the chip in x, overlaps it in y and is within
   `press_reach_mm` of its surface. The press pad marks 0.5 mm cells inside the
   tape band; exfoliation yield scales with the pressed fraction, and flakes
   land only in pressed cells. A peel moves the roll so the tape leaves the
   chip at the requested angle, clipped to the gantry travel; the angle really
   reached is recorded next to the requested one. A roll driven into the
   stage is a world collision.
4. Twin (`lab.frame` exfoliator): adds `tape {x0, z0, x1, z1}` and
   `pressed_frac`; the manipulator carries `x y z` and `head`.
5. Sim perception: the segmenter's misreading and area error are fixed per
   (flake, objective), so a rescan sees the same catalogue. A stage jog under
   0.05 mm keeps the stage's present position error (plus a fifth of its
   sigma) instead of drawing a new one, so re-centring on a flake converges.
6. Flow fixes found with the new model: the first stamp touch is aimed over
   the stage (it could pick up the flake before); align and pick-up park the
   heater too; pressing the tape never counts as already done after a restart.

### Step 3: the chaos benchmark (`qumus2 chaos`)

1. Task: exfoliate Gr and hBN, search both, stack hBN/Gr/hBN onto the
   electrode chip (was: exfoliate, search, one chip to the transfer stage).
2. 20 of the 22 faults (`testbench/chaos.py FAULT_CASES`; `hang` and
   `stuck_at` left out, reasons in `NOT_IN_CHAOS`). The first fault of a run
   cycles through them by seed; each has an expected behaviour (`recover`,
   `stop` with a reason pattern, or `either`). Devices can be chosen at the
   moment of injection (`@now`, `@arm`, `@board`) and faults that only matter
   in one flow are injected there (`during`), so a fault meets the task.
3. Every seed also runs without faults (baseline: call count, tape, stamps,
   sim time). Per injection: `fired` (the armed failure was used up, or the
   faulty device was used afterwards) and `handled`. A run where no fault
   fired is `no_fault` and not counted.
4. A stop is checked against the world too: records wrong (and not marked
   uncertain, nor named in the stop note) or a world collision = `bad_stop`.
   A job `failed` by a dangerous verdict or a hardware error is a stop with a
   reason; only an unexpected exception is `error`.
5. Fix found by it: a move that gives up left the ledger at the old place.
   `MoveObject.on_give_up` (C3 hook) moves the ledger to the destination when
   the object was seen there off target, and otherwise marks the sample
   `location_uncertain` with `last_seen`; a later success clears both.

### Step 4: a fixed objective (Jialei, 2026-10-21: the objective cannot move)

1. The focal plane is fixed at height 0 (`bench.yaml microscope.focal_plane_z`).
   A chip surface sits at stage z + its own height (`SimObject.focus_offset`,
   chip to chip +-0.3 mm); the stamp film at holder z - its touch height.
   Contact uses the same chip height, so a chip brought into focus always
   meets the dome at the same holder z.
2. `primitives.autofocus(..., plane=)`: "chip" sweeps the stage z (x/y stay;
   a z-only stage move keeps the stage's x/y error), "stamp" sweeps the holder
   z. `_move_stage` keeps the current z unless given one; named stage
   positions carry z.
3. Looking at the stamp (`stacking._stamp_near_centre`): stage down by the
   film's height plus 0.3 mm, stamp down into the focal plane, sharpen with
   the holder, look; back with the stamp first lifted clear of the stage
   (the stage may not rise under a low stamp), then the stage, then the stamp
   to where it was. The way back runs even after Stop or an error (no cancel
   check, timeouts retried).
4. Gate: stage z within `stage_focus_mm` (1.0) is focusing, so the holder z
   floor does not rise with it; the stamp-low gap is 6.2 mm (above any contact
   gap once chip heights differ). Touch search: approach 5.5, at most 0.75 mm
   down, which stops above the 4.7 floor.
5. What the dome meets (`SimWorld.dome_surface`): the highest chip within the
   dome radius of the apex, lowered by the dome's sag d^2/2R when the apex is
   d past the chip's edge; only with no chip that close the bare stage top,
   a chip thickness (`testbench.yaml stamp.chip_thickness_mm`, 0.5) below the
   nominal chip surface. A chip near the apex always wins, even one whose
   drawn height offset is below the bare stage (the offset is mounting and
   thickness spread). Pick-up and release use the same chip.

## Decisions made in the design review fixes (2026-10-21, Jialei approved notes/v2-review/fix-proposal-a1-a4.md)

1. A returned delegation is checked against the records before its plan
   step counts (`ToolGateway.check`, both runtimes): hands - the last job of
   each flow it ran in that delegation succeeded (failed / cancelled /
   parked / waiting_human -> step `failed`; still running or paused -> stays
   `running`; no job -> `pending`); picker - `submit_flake_selection`
   accepted; designer - `submit_design` or `assess_relaxation` accepted;
   advisor - a `submit_device_plan` it tried was accepted (a consultation is
   done after any successful call); eyes - any successful call. Otherwise
   `pending`. A status the runtime passes explicitly (error, scripted
   `step_status`) is kept. The facts go to the orchestrator as
   additionalContext of the Agent tool's PostToolUse ("Code check of the ...
   delegation") and into a new event `agent.delegation_checked` (C7,
   additive).
2. `eyes_first` (C9) now means: a successful `inspect_bench` in this project
   (set by the tool), not "the eyes were delegated to". A hands job that ends
   failed / cancelled / parked / waiting_human clears it, so the eyes look
   again before the next hands delegation. Paused (Stop) does not clear it.
3. `experience_checked` (C9) counts only the advisor's look-ups
   (`get_experience`, `query_run_records`, `review_runs`), as the spec and the
   prompts say; other agents may still read experience.
4. The advisor's WebSearch / WebFetch calls are logged by hooks that never
   block: event `agent.web` (C7, additive) with the query or URL and, after
   the call, its size and a scrubbed 500-character excerpt.
5. The scripted runtime applies `check_delegation` like the SDK's Agent tool
   (a refused delegation stops the scripted turn). The scenarios' eyes call
   `inspect_bench`.
6. Revisit slots follow the device plan (A1): `search_chip` (the tool) turns
   each `flake_roles` entry into a quota of 3 x count slots for its 10x
   thickness labels and minimum area; each role takes its slots first, then
   free slots go to other flakes the roles would take, then to v1's
   PREFERRED labels, largest first, up to `max_revisits` (now 20). Without a
   plan v1's quotas apply: graphene 15 monolayer + 5 manylayer (graphite
   gates), hBN 4 per bin 0-10 / 10-20 / 20-30 / 30-40. The 10x detection
   minimum (50 um2) is unchanged.
7. Stored candidates can be photographed later without a new scan: the
   `search_chip` tool with `revisit_only` and `chip_id` (optionally
   `flake_ids`) submits the new flow `revisit_chip`. The picker's
   `relink_or_capture_optical_images` now has this as its remedy, so the
   orchestrator photographs before it exfoliates more.
8. Restart of plain `serve` (A2): the in-process testbench seeds a fresh world
   and ledger on every start, so `build_services` closes runs the last process
   left queued / running / paused / parked / waiting_human (status
   `cancelled`, locks released, one warn notification) with
   `ThreadJobService.close_leftovers` instead of leaving them "running".
   Resuming after a restart (`recover()`, spec 8.2) is for labs that keep their
   state: `serve --lab-process` and, later, real hardware, which must run that
   way.

## Decisions made in the overnight acceptance sprint (2026-10-22, testbench thread)

1. Stop at the holder's floor, not below it: `SafetyGate.manipulator_z_floor(name)`
   (C2, additive) returns the lowest z the gate accepts for a stamp holder
   now (its `z` limit, raised by a stage lifted beyond `stage_focus_mm`).
   `stack_device`'s touch search and press never ask for less; a press the
   floor cuts short is reported as such (UNCLEAR, "the holder's z floor ...
   stops the press"), never as a safety violation.
2. An object the tape lifts off the exfoliation stage (vacuum lost or stuck)
   ends the job with a note that names the object and the vacuum reading;
   `on_give_up` marks the sample `props.location_uncertain` with
   `props.last_seen = {at: "tape", over: exfoliation_stage, note}`. The
   ledger location stays where the object was last known to sit.
3. A `QumusError` raised outside a step (the watchdog's reads between steps,
   a device gone offline) ends the run like a dangerous verdict: every device
   of the flow to its safe state, a `danger` notification "flow: watchdog:
   ...", status `failed` with the typed error (C5 behaviour, no new API).
4. A job that parks (`parked` / `waiting_human`) switches the flow's heaters
   off like Stop does and keeps the setpoints in the run `meta.held_heaters`;
   `retry` restores them before the first step (C5 behaviour).
5. Reservations (groundwork for the slot bug, used by the flows in the next
   PR): table `reservation(kind, key, run_id, flow, note, created_at)` (C6,
   additive) with `RunStore.reserve / reservations / release_reservations /
   occupied_slots / reserved_samples / update_run_params`; a run's bookings
   lapse when it ends (`set_run_status` succeeded / failed / cancelled).
   `Flow.reserve(params, store, run_id)` (C4, additive, default nothing) is
   called by `submit` right after `create_run`, under one lock with
   `resolve_params`, so two submits in a row never plan on the same free
   slot, bare chip or stamp. `update_run_params(run_id, path, value)` writes
   one resolved field back into the stored params and emits
   `job.params_changed {path, value}` (C7, additive).
6. Chaos: `pickup_fail_next` and `release_fail_next` are injected during the
   stacking flow only, so every run with them counts as fired.
7. The flows book what they plan on (Bug A: two `exfoliate_chips` back to
   back resolved the same free slot and the same bare chip). `Flow.reserve`
   of `exfoliate_chips` books `store_slots` and the chips (and the carrier
   plus its slide slot when the job prepares a mother tape), `search_chip` /
   `revisit_chip` the chip and `home_slot`, `stack_device` the stamp,
   `stamp_slot` and the flake chips' `home_slots`, `place_chip_on_transfer_stage`
   the `handover_slot`. `resolve_params` skips booked slots and samples and
   refuses a named chip another run has booked; a carrier a running
   exfoliation holds on the stage is accepted (its slot from that run's
   params). At run time `MoveObject(fallback=(board, holds), param_path=...)`
   re-targets a destination found taken (something put there by hand) to
   the next free, reachable, camera-empty slot of the same board, re-books
   it, writes the slot into the run params (`job.params_changed`) and
   warns; no free slot is a `PreconditionFailed` naming both. The stored
   run params therefore always carry the slots actually used.
8. Lab twin (additive fields, for the page's bench drawing): `manipulator`
   gains `over_stage`, `gap_mm` (holder z minus transfer-stage z) and `stamp`
   (the stamp it holds, from the objects); `exfoliator` gains `rolls`
   {movable, fixed}{x, z} (the two tape axes, same numbers as `tape`),
   `peel_angle_deg` and `vacuum` (the stage vacuum switch); `microscope`
   gains `focus_plane` ("chip" / "stamp" / None) when the microscope can
   tell. Contact of the stamp with the chip is not a device reading: the
   page sees it in the micrograph.
9. Testbench pictures (item 7 of the overnight list): `MicroscopeRenderer`
   draws a 480x320 micrograph from the frame meta: SiO2/Si substrate,
   interference tints by layer count (`flake_tint`), crystal-shaped flakes
   with terraces and dark edges, tape residue and graphite crumbs at a fixed
   density per mm2, the chip edge beyond a corner, defocus from the focus
   error (drawn small and scaled up), the stamp's veil, contact front, torn
   film and the flakes on the film, camera vignetting and noise, the
   objective and a scale bar. `BenchRenderer` draws the breadboard's hole
   grid, plates with shadows, glass slides, the exfoliator rail and rolls
   with the tape band, and the arms as base-to-nozzle links. File names and
   the pixel contract of tests/test_images.py (flat region colours on the
   bench picture) are unchanged; perception still reads the meta, never the
   pixels.

## Bench modules redrawn (2026-10-22, Jialei's sketch; notes/v2-ui/bench-module-redesign/)

1. Geometry (bench.yaml, placeholders): the search & stacking module has the
   fixed microscope top left (`microscope.center` 740, 420), the stage's
   drop-off point straight below it (`stations.transfer_stage` 740, 340;
   `transfer_stage.microscope` = load + (0, 80): the stage only moves up to
   the objective) and the stamp holder bottom right (`transfer_arm.origin`
   700, 330; `load` = the hand-off under the head at 850, 340; `park` 40 mm
   right of it; `over_stage` (40, 90)). The glass slide lies on the holder,
   held by a vacuum hole in its top face; the stamp hangs free at the slide's
   front (left) end. limits.yaml axis ranges and the arm_right interlock
   regions follow (placeholders as before).
2. Holder travel (flows/library/stacking.py `_travel`): into the microscope
   y first, then x (up, then left); everywhere else x first, then y (right,
   then down). Searching never moves the holder (unchanged).
3. `stations.<id>.module` (drawing only): the box around a whole machine
   with every part at its furthest position, centred on them, 18 mm margin.
   `exfoliator.rail {x, y}` replaces `exfoliator.region` (drawing only): the
   slide rail along the back of the exfoliation module, with both rolls
   hanging from sliders on it. Layouts without these (older recordings) get
   both computed by the page (stations.js `exfoliatorRail`, `holderPath`).
4. Twin: `manipulator.over_stage` is now `flows/bench.over_stage` (head
   within the tolerance of the microscope centre), the same rule the flows
   and the world use (it used to test the stage's drop-off box).
5. Page (stations.js): modules as boxes with no part labels inside except
   the objective (e.g. 10×); the holder draws the slide when a stamp is on
   it; chips and their slot frames are drawn at 2x (drawing only); a planned
   arm (`planned: true`, Arm 3) and the slots of a `placeholder` station
   (the reactor port) are not drawn, on the page or in the overview-camera
   picture. Replaces the dashed Arm 3 ghost of the UI acceptance night.

## Recipe model, press depth, stamp optics, arm path (2026-10-22, simulation workflow review)

Jialei's rule (spec 11.2): simulate every controllable mechanical part; what
depends on the polymer (tape, PC film) stays a probability model.
notes/v2-testbench/sim-flow-details-qa.md has the questions and answers.

1. Exfoliation outcome (testbench.yaml `exfoliation.recipe`, world.py
   `recipe_quality / recipe_lift_prob / layer_weights`): a hidden best recipe
   per material (temperature, peel speed, peel angle, press depth, massage
   rounds) with a width for each. q = exp(−½ Σ((x − best)/width)²) sets the
   flake mean (`flakes_at_best` × (floor + (1 − floor)·q) × wear × pressed
   fraction) and the weight of the good layer classes. The chip lifts with the
   tape only for a peel steeper or faster than the best by more than
   `lift_margin` widths. No tape mechanics; the old per-parameter gains
   (`flakes_mean`, `temp_gain_per_c`, `massage_gain`, `peel_angle`) are gone.
2. Press depth: the left arm is position-controlled, so press "force" is the
   depth below first contact (`press_contact_z` in bench.yaml and
   testbench.yaml). New flow/tool parameter `press_depth_mm` (C4 additive:
   default 0.2 keeps v1's massage height). Gate floor: limits.yaml
   `arm_left.floors` min z 0.6 over the exfoliation stage (C2).
3. Stamp optics: a stamp over the stage shifts the chip's focus by its
   `optic_shift` (0.3 ± 0.03 mm). New step `RefocusThroughStamp` after each
   `stamp_over_*` (lifts the holder clear, refocuses the chip around the
   expected shift, stores `optical_shift_mm` on the stamp). `AlignStamp` now
   predicts contact from the film focus (stage dropped `gap_probe_mm`), moves
   fast to just above it, then steps down, in 1 µm steps once Newton rings
   show (C13 `fringes`). Step measurements add `predicted_touch_z`,
   `first_touch_z`, `fringes_seen`. Settings in verification.yaml `touch` and
   `stamp_optics`.
4. Arm path: `World.carry` checks the nozzle's straight path (every mm)
   against both exfoliator rolls; a hit is a world collision
   (`exfoliator_<roll>_roll`). The arm links are not modelled.
5. Consequences found by the chaos runs: a glass slide (the mother tape's
   carrier) held by the vacuum never lifts with the tape (the recipe lift risk
   is for chips); a peel that leaves no flake leaves the chip bare (no
   material), so the ledger and the world agree when the camera sees nothing;
   the chaos task names its exfoliation recipe (`chaos.py RECIPE`, the hands'
   tool defaults) instead of the flow's v1 defaults. `tests/conftest.py
   quiet_bench` turns the lift risk off (`lift_max_prob` 0) like the other
   random errors.
6. Two older inconsistencies the new checks exposed: the dome met a chip at its
   untilted height while the microscope saw the tilt (`focus_tilt_per_mm`), so
   the two-plane estimate was off by up to ~80 µm on a flake near a chip
   corner; `dome_surface` now uses the same tilt (estimate within ~11 µm,
   10 chaos baselines). And arm_left travelled at z 80 through the parked
   movable roll (axis at 78): bench.yaml `arm_left.safe_z` is 95
   (placeholder). The scan merges detections closer than
   verification.yaml `scan.merge_mm` (0.03; was a fixed 0.01, which counted a
   flake twice when two stage moves landed ~11 µm apart).

## Workbench shell (2026-10-22, UI redesign step 1; notes/v2-ui/workbench-design/)

The page is laid out like a code editor (VS Code's workbench): every kind of
information has one home, and the layout is the same in live and replay mode.
Design approved item by item by Jialei on the workbench design page; this is
the first of four PRs (shell → conversation → progress tree and Problems
panel → lab column).

1. Grid (`.app`, style.css): title bar / activity bar / sidebar / editor
   (conversation) / lab column / status bar. Widths come from `--side-w` and
   `--lab-w`; the sidebar folds with `.no-side`, the lab column with
   `.no-lab`; shell.js remembers both and the open page in localStorage
   (`qumus.shell`).
2. Title bar = the one "Now" sentence (`#now`, app.js `renderNow`): the plan
   step, who is doing what for how long, the last check. In replay the same
   slot holds the device caption (actions.js) that used to sit in the replay
   bar. It turns amber (`.now.wait`) when a person is needed and red when
   Claude stopped with an error.
3. Activity bar (`nav.act`): Progress, Lab, Records, Experience, Tools (only
   with testbench tools), Settings. Clicking the open page folds the
   sidebar. Experience moved from a drawer to a sidebar page; the lab sidebar
   page lists devices and stock (`/api/snapshot` samples, polled every 5 s).
4. Status bar (`footer.sb`): left = where we are (mode pill, safety line,
   "waiting for you"); right = the current contents (Claude chip with cost,
   run time; the replay shows the recorded cost).
5. "Waiting for a person" (`S.waiting`) has four homes at once: the Now
   sentence, an amber badge on the Progress item, the status bar, and the
   conversation. Set by `job.status` = waiting_human and `notification`
   level needs_human; cleared when the run goes on.
6. Phone (≤ 860 px): the activity bar becomes four bottom tabs Chat,
   Progress, Lab, More (More = Records / Experience / Tools / Settings as
   chips), one column at a time (`.app[data-view=…]`). The safety line and
   cost are hidden in the status bar.
7. Bottom panel (Problems) is always collapsed, also in replay (Jialei's
   decision); it arrives with the progress tree PR.

### Step 2 (2026-10-22): the conversation as a group chat (feed.js)

1. You are on the right in a dark bubble (`.msg.me`); every team member is on
   the left with its avatar and name (`.msg.member[data-a]`). A hand-off is
   Qumus's message to the member ("Qumus → Hands" + the task, `.handoff`);
   the member's report (`agent.done` summary) is the member's own message.
2. Everything a member does on the way sits at the bottom of that member's
   bubble: tool calls as two-line rows (⏺ tool · args / ⎿ result, click for
   the details), interim notes, hardware job results and corrections. While
   the member works (`.busy`) the last three rows show; once it has reported
   the rows fold to one line "N tool calls · duration · M blocked" that opens
   on click. Qumus's own tool calls fold the same way under Qumus's next
   message (or the next hand-off / turn end). The `delegate` tool call itself
   is not listed: the hand-off bubble is that call.
3. Times come from the event's wall clock (`wall`, falling back to `ts`):
   time of day live, elapsed since the first message in a replay.
4. The working line is Claude Code's: "✻ Picker is working… · 2 min 10 s ·
   8 tool calls · Esc to stop" (no time and no Esc in a replay).
5. Full / Focus (`#feed-mode`, remembered in localStorage `qumus.feed`):
   Focus hides tool rows, skip lines, system lines, turn ends and plan lines,
   and a member's bubble that has no text yet.
6. The jump button counts what arrived while scrolled up ("↓ N new").

### Step 3 (2026-10-22): the progress tree and the Problems panel

1. The Progress page is one tree (tree.js `ProgressTree`), four levels deep:
   plan step → hardware job → lab action (a flow step) → device command. It
   replaces the plan's step rows and the "Hardware jobs" list; the plan's
   revisions (v1, v2, …) and the Device / Flakes / Layout tabs stay above it
   (planbook.js). A job hangs under the plan step that was running for its
   agent when it was submitted (`stepFor`); a member's tool calls and what it
   handed in (device plan, flakes, layout: a row that opens the tab) hang
   under its plan step. Corrections and failed checks bubble up as the icon
   of the job and the step; a job waiting for a person shows ⏸.
2. Device commands are built only for the action that runs (or that was
   opened by a click, live). Refused commands (safety.violation) are listed
   in red at level 4.
3. A replay scans the recording once (`preload`) so every job and action is
   listed as "to come" from the start; a click on an action seeks the replay
   to its start, a click on a device command to that command (the old action
   list of the replay bar is gone; `actions.js` keeps the caption and the
   check wording). In a replay everything is open unless the person closes
   it; live, steps that are done or running are open and finished jobs are
   folded.
4. The bottom panel (panel.js) is Problems only, under the conversation and
   always collapsed at first (also in a replay): "Problems N · M commands
   allowed". Rows are exceptions: refusals by the safety layer, blocked tool
   calls, checks whose verdict is not ok / correctable / not_verifiable,
   jobs that failed, parked or wait for a person, warn / danger /
   needs_human notifications, Claude errors. "↗ step" opens the progress
   page and reveals the step's row in the tree. A new problem while the
   panel is collapsed paints its top edge red until it is opened. The Events
   tab (testbench faults) shows only with the testbench tools.

## Stage-1 wrap-up (2026-10-22, notes/v2-review/stage1-wrapup-2026-10-22.md)

Small fixes left by the overnight acceptance report (its section 7):

1. **Open delegation rules are announced, not only refused.** `policy.open_gates(project)`
   lists the delegation rules a project has not met yet (plan_first, eyes_first,
   advisor_first) as one line each. `get_project_context` puts them in its summary and
   in `data.before_delegating`; `update_plan` appends them after "Next:". The rules
   themselves (`check_delegation`) are unchanged. Every real run of the overnight
   acceptance met two or three of them as refusals first (one round trip each).
2. **Tool text names image files, not paths.** `ToolResult.to_text()` (what Claude
   reads) shows `images` and any absolute image path inside `data` by file name only;
   the images are attached as image blocks anyway. `ToolResult.images`, the events and
   the ledger keep full paths. An absolute path into another run directory made the
   orchestrator doubt correct results (E10, E3b).
3. **chaos: stacking-only "next N" faults are armed when stacking starts**
   (`pickup_fail_next`, `release_fail_next`: a case with both `during` and `pending`).
   Armed at a random call they could land after the last pick-up and never fire
   (no_fault runs, not counted).
4. **`scripts/record_demo.py --fault NAME[=JSON]`** (repeatable) injects testbench faults
   at the start, so the fault demos are reproducible from the repo (the overnight
   stacking-fault recordings were made with a one-off script).
5. **Wall time in `runtime/jobs.py` is deliberate**: the worker's back-off for a job
   queued behind another (`time.sleep(poll_s)`) and `JobService.wait` (how long a
   `wait_job` call holds the agent's turn) are scheduling, not lab time. A `SimClock`
   at speed 0 would return at once and spin. All lab time still comes from the Clock.
6. **A deeper press stops at the holder's z floor** (`stacking._press_depth`): the
   pick-up's "press a little deeper" correction and the release's extra press went to
   `touch_z - depth` unclamped; with the new stamp optics (PR #72) chaos seed 71's
   third pick-up asked for z 4.683 under the 4.7 floor and the gate stopped the job
   (stopped_danger). Now the press goes as deep as the floor allows, and "the flake
   stayed on the chip" names the floor when it was the limit.
7. **chaos: a vacuum stuck at the other state counts as fired at once**
   (`_takes_effect_at_once`): stuck off under a held chip changes what is held with no
   further command (seed 98 was counted no_fault although the chip came off).
8. `tests/test_v1compare.py::test_v1_exfoliates_then_reports_an_arm_timeout` runs seed 2:
   since the recipe model v1's F500 peel lifts the chip 1.5% of the time and seed 1
   draws it. The test failed on main after PR #72 (CI was not running).

### Step 4 (2026-10-22): the lab column

1. The lab column (`index.html` `#lab`, camera.js `CameraCard`) is three
   pictures stacked, always on screen, no camera switch: the bench top view
   (stations.js, unchanged; its frame takes the table's own shape), the
   overview camera and the microscope. Live, the page polls both cameras
   every 1.5 s; in a replay each frame shows the newest recorded picture of
   that camera so far. The frame a job looks through (search, stacking →
   microscope; otherwise the overview) is marked "a job works here".
2. The pictures share the column's height (flex, each with its camera's
   aspect ratio) and shrink together on a short window instead of
   scrolling; on a phone the column scrolls and each keeps its shape.
3. The last check is one line under the pictures: verdict icon, step, how
   far off (mm / °), "in tolerance" or not, "after N fixes". Only checks
   that measured a position are shown there (the others are a ✓ in the
   progress tree); "history" unfolds the measurements with their tolerance
   bars and every attempt. `#cam` still holds the `img` elements of both
   frames (`.cam[data-cam=…]`) for the browser tests.

## Stamp swap, stamp states, inventory (2026-10-22, review items 10, 15, 16)

1. **A torn PC film with nothing on it yet is swapped by the job itself.** When a
   pick-up of `stack_device` fails because the film is torn and the stamp carries
   no flake of this stack, `StackDevice.on_step_failed` returns a `Replan`: the
   torn stamp goes back to its own slot with status `torn`, the next fresh stamp
   (`stacking.fresh_stamps`: fresh, in a stamp slot, not booked by another job,
   lowest id) is loaded and gripped, brought over the stage, refocused, aligned,
   and the same flake is picked up again. Steps after the swap use the new
   stamp (`stamp_id`/`stamp_slot` are always the stamp in use now). No fresh
   stamp left: the job parks and says a person must put a new PC
   film on a stamp and report it. Flakes already on the stamp (a later pick-up,
   or the drop-down): the job stops and names them; nothing is swapped.
2. **How resume stays correct.** The change is in the run's params
   (`stamp_swaps`, the new `stamp_id`/`stamp_slot`), stored in one write
   (`store.set_run_params`) before anything else happens. `build_steps` stays a
   pure function of the params: each swap adds its steps right after the failed
   pick-up (`manipulator_to_load_for_<torn>`, `return_torn_<torn>`,
   `load_<new>`, `grip_<new>`, `refind_<flake>_<new>`, `stamp_over_<flake>_<new>`,
   `refocus_through_stamp_<flake>_<new>`, `align_stamp_<flake>_<new>`,
   `pick_up_<flake>_<new>`), and the steps up to the failed one keep their names
   and seqs. The failed pick-up is marked `skipped`; checkpoints are never
   reused for another step. A crash after the params write but before the
   `skipped` mark re-runs that pick-up: its precondition refuses a torn stamp,
   and `on_step_failed` sees the swap is already planned (the step's stamp is no
   longer the current one) and the job goes on. Each swap step is an ordinary
   checkpointed step with its own `probe_state`. The new stamp is booked
   (reserved) when the swap is planned; the torn one's booking is released.
3. **Stamp states are explicit**: `fresh` (new PC film), `in_use` (on the holder),
   `used` (its film stayed on a device), `torn`. Only a person makes a stamp fresh
   again: `records/stamps.report_new_film` checks the stamp (it exists, sits in a
   stamp slot, no open job has booked it, it is `used` or `torn`, the same
   message was not used for it before), sets it `fresh` (film PC, empty stack)
   and records who, when (Clock time), what and the previous status in
   `props.film_reports` and as event `stamp.film_replaced`, plus the
   `sample.updated` of the ledger write. Two ways in: the Lab page's "New PC
   film…" (`POST /api/stamps/{id}/film`, live only; `via` `ui:lab`), and the
   orchestrator's tool `report_new_film(stamp_id, instruction_id)` (`via` chat),
   which the policy rule `person_reported_film` lets through only when it cites
   a person's message that names the stamp. Messages reach the agents with
   `[message id: …]`; the scripted runtime can cite it as `{{turn.message_id}}`.
   On the testbench the report stands for the act: the sim stamp gets an intact
   film the next time the holder grips it (`SimWorld.film_reports`).
4. **The Lab page shows the inventory as lab state** (`ui/static/inventory.js`):
   material slides and tape, bare chips, exfoliated chips (with the flakes still
   on them), target chips, finished devices and stamps (new / on the holder /
   used / torn, slot, film, last film report), with counts. Live it is seeded
   from `/api/snapshot` and kept current by `sample.updated`; a replay rebuilds it
   from the recorded events alone (recordings made before this have no ledger
   events and say so). The bench top view's tooltip names a stamp's status and
   draws a torn stamp faded with a red dashed edge.
5. **chaos**: `stamp_torn` is judged by what the tear met (`FaultCase.expect_at`):
   a held stamp that carries flakes must stop; otherwise it must recover when a
   fresh stamp is left for the swap, and stop when none is. `qumus2 chaos
   --fault NAME` runs one fault's seeds only.

## Bench review fixes (2026-10-22, owner's replay review items 6, 7, 8, 9, 11)

1. **Continuous motion in the top view (items 6, 11).** Cause, from
   `recordings/v2-stack-stamp_torn-20261022c`: event 122 is Arm 1's
   `device.command move_to` (ts +12.21 s), 123 a `tool.call` that Claude's
   thread wrote meanwhile, 124 the arm's `lab.frame` (ts +15.85 s, 267 mm
   away). The replay paired a command only with the very next event, so this
   frame was eased in 0.12 s / speed (30 ms at 4x): a jump. The same happens
   whenever another thread's event (tool call, another job's submit) lands
   between a command and its frame. The roll: the page placed it from the
   twin's `exfoliator.rolls` / `tape` (overnight sprint item 8), which the
   tween did not ease, so every `apply` / `peel` (70 mm, 13.5 s of lab time)
   snapped. Fixes, page only (no recording or event change, old recordings
   play the same way): `Replay.frameAfter` finds the frame of the same device
   and command before that device's next command (up to 200 events on);
   `LabState.step` eases `rolls` and `tape`; `LabState.push` no longer snaps
   a move under way to its end when another frame arrives (a frame that moves
   nothing joins it, one that moves something takes over from where the view
   is). Lab frames stay one per command (command `ts` to frame `ts` is the
   move's duration). Checked by
   `tests/test_ui_browser.py::test_replay_moves_are_continuous`.
2. **Pressing order (item 7, a flow change for the real robot too).**
   `primitives.massage` presses column by column from right to left: columns
   at x = centre + span down to centre - span, each one low stroke along y
   (front to back), the nozzle at safe_z between columns. bench.yaml
   `arm_left.massage_rows` is renamed `massage_columns` (3). The square
   pressed, the stroke count per round (testbench.yaml `strokes_per_round` 6),
   the press depth and floor, and the nozzle's clearance from both rolls are
   unchanged.
3. **One stamp orientation, a continuous hand-off (item 8).** A stamp's
   slide (bench.yaml `stamp_slide`: 40 x 25 mm, dome 7 mm from its left end)
   lies along world x everywhere: in its storage frame, on Arm 2's nozzle and
   on the holder. The world gives a stamp's position as its slide's centre
   (where an arm holds it) everywhere, the holder included. New in
   `flows/bench.py`: `stamp_dome_offset(bench)` (dome minus slide centre,
   (-13, 0); (0, 0) for a bench without `stamp_slide`) and
   `stamp_seat(bench, pos)` (slide centre of a stamp on the holder = head
   point minus that offset). `stamp_handoff` (positions / regions
   `stamp_handoff`, arm_right's target) is now the seat at the load pose,
   (863, 340), so the dome lands exactly under the head (850, 340). The
   world grips the stamp whose slide centre lies within 2 mm of the seat
   (`SimWorld.seat_xy`) and carries it there; contact and the dome apex
   still come from the head point. Storage: lying slides do not fit three in
   a row on the right board, so `right_stamp_1..3` are frames of [40, 25] at
   (930, 196), (980, 196), (930, 164) (inside the board and its QR markers,
   clear of the chip frames, within arm_right's 280 mm and its limits; checked
   by `tests/test_twin.py`). stations.js draws the stamp as the same object
   everywhere (on top of the holder body while on it or carried, at the seat
   from the holder's tween once gripped), and no longer draws a separate slide
   on the holder; `testbench/render.py` draws the dome near the left end.
   The real orientation of the slides in storage is not known (ask the lab):
   this keeps storage, grip and holder consistent. What remains at the
   hand-off is the arm's placement error (the world puts the gripped slide
   onto the seat: here 0.8 mm).
4. **The holder stays at the hand-off (item 9).** bench.yaml
   `transfer_arm.park` and `home` = `load` (x 150; PR #70 had park 40 mm to the
   right). No interlock in limits.yaml used the 40 mm (the arm_right regions
   are under the objective and over the stage's lane; arm_right comes down at
   the hand-off only to hand a stamp over). `GripStamp` now only switches the
   holder's vacuum on and is `NOT_VERIFIABLE` (no sensor reads that vacuum;
   the switch reports what was commanded); the grip is checked on the holder's
   next trip: every `ManipulatorTo("over_stage")` looks at the hand-off and, if
   the stamp stayed there, goes back, grips again and retries (its
   corrections), and on giving up puts the stamp back at `stamp_handoff` in
   the ledger. verification.yaml `grip_stamp` is gone. A stuck holder vacuum
   now parks at `stamp_over_<flake>` (with the flake chip already under the
   objective), not at `grip_stamp`.
5. **Found on the way: Stop inside `RefocusThroughStamp`.** The step took the
   chip's focus before the stamp from the stage z at its start, so a Stop after
   it had lowered or swept the stage resumed one expected shift lower again and
   found nothing sharp (parked; on main too, at stop points the Stop sweep did
   not sample, e.g. k = 780 of `test_stop_during_stacking_then_continue`). The
   step now keeps that z in the run's meta (`refocus_from_z` {step name: z},
   C6 additive use of `update_run_meta`) and resumes around the same window.
   The moved camera look shifts the random stream: `graphene_device` seed 1
   now parks on five electrode-chip placements over 0.5 mm in a row (a draw
   of the placeholder error model; seeds 2-8 succeed), so its two tests use
   seeds 2 and 3.

## Flake photos, flake library, readable records (2026-10-23, notes/v2-ui/usability-redesign/)

Jialei's usability review ("I cannot see the inventory flakes' pictures"),
items 1-8 of the redesign page, approved as a whole.

1. **Flake data in the events (C6/C7, additive).** `sample.updated` carried
   only what the old inventory showed, so the page (and every recording) knew
   a flake's id and thickness word but not its photos, area or position; the
   photos were only in the Picker's folded tool calls. `SAMPLE_EVENT_PROPS`
   now also lists `chip_id labels area_um2 confidence stage_xy rel_corner_mm
   images residue`. Recordings copy the photo files into `media/` like any
   other picture path (`recording.py` walks every string). `sim_id` (the
   testbench's ground truth) stays out. Recordings made before this have no
   flake photos; the page says so.
2. **Records page (bug).** Since PR #75 the page called `book.runCard`, which
   PR #75 removed, and threw; no test opened the page. The rows are built in
   app.js again and a browser test opens the page.
3. **Replay seek to the end.** The right end of the track is covered by the
   last chapter mark; a click there sought to that chapter's start (before
   the final message) instead of the end. A chapter mark within 0.5 % of the
   end now seeks to the end, and a seek that lands on the end clears the
   "working" state like playing to the end does.
4. **Wording.** The Problems tab says "Safety: all N device commands checked
   and allowed" (the count was read as a number of problems). Storage slots
   read "middle board, slot 1" with the id in small print.

5. **Lab page: Samples and Devices (item 1).** The Lab page has two tabs.
   Samples: one line summing up the instruments (only exceptions named; it
   opens the Devices tab), a search box (flake, chip or stamp id), exfoliated
   chips as cards (the four largest photographed flakes, a thickness bar with
   counts per label, how many were photographed), finished devices with each
   layer's photo and role, stamps as pills (the "New PC film…" report stays on
   used and torn stamps, live only), then target chips, material slides and
   bare chips as short rows. Devices: the instrument list as before.
6. **Flake library (items 2 and 3, `static/flakes.js`).** A window over the
   page (Esc closes it; it opens from the Lab page, the lab column's "Flakes"
   button, any flake id, the microscope caption, a job record, the Picker's
   selection). Filters: material, thickness label, status (on a chip / on a
   stamp or in a device / lost or excluded), chip; sort by area, Picker score
   or name; photographed flakes first, the others marked "10× scan only".
   The detail: 20×/100× photo (the 10× scan keeps no photo per flake), the
   label each objective gave, area and detector confidence, residue (the
   Picker's judgement), where it came from (chip, slot, mm from the chip
   corner) with a map of every flake found on that chip, and a history built
   from the events (found, photographed, looked at, chosen or not and why,
   refound, picked up, stacked). Live only: "Don't use this flake" sends a
   `veto` message after a confirmation (the orchestrator records it with
   `exclude_flake`); "Ask Qumus about it" starts a message.
7. **Picker selection, microscope, job record, links (items 4-7).** The Flakes
   tab of the progress page shows each chosen flake with its photo, thickness,
   area, residue, score and reason. The microscope frame names the flake it
   looks at ("Looking at chip_001 · f014", click opens it): the flake whose
   photo the frame shows, else the flake a running microscope job's current
   step is named after; in a replay the frame shows each new revisit photo as
   the search takes it. The job record opens with one sentence (for a search:
   tiles scanned, candidates, revisited, found again), the flakes a search
   found, one line per step with "measured / allowed" where the step has a
   tolerance, the photos; raw measurements and the device commands are folded
   (the commands are built only when opened). Sample ids in the conversation
   and the job summary (`chip_001_f014`, `chip_e01`, `device_001`, `stamp_02`)
   are links when the ledger knows them: a flake or a searched chip opens the
   library, anything else shows its card on the Lab page.
8. **Records rows and live inventory.** A record row says what the job works
   on ("Search chip_001 for flakes", "Stack a device on chip_e01"), its lab
   time (the lab's clock; the session clock of a testbench run is much
   faster), and for a search how many flakes it found and photographed. The
   live page seeds the ledger from `/api/snapshot` at start (it used to wait
   for the Lab page), so links and the microscope caption work at once.

## Scenario redesign (2026-10-23, UI structure review; notes/v2-ui/structure-redesign/)

A classmate found the page hard to use: every feature was there, but in 12
zones at once. Jialei and Claude redrew it scenario by scenario (page v7 of
the proposal). The page now shows one scenario at a time; the activity bar
(icons with labels) picks it, and every scenario fills the same three columns:
the left says what to look at, the centre shows it, the right goes with it.

1. **Progress** (what the team does now): left = the progress tree, open to
   level 2 by itself (plan steps and their jobs; a running job names its
   current action on its row; a click unfolds a job's lab actions; a replay
   also unfolds the job being played and lists the jobs still to come,
   folded, under "Later in this recording"). Centre = the conversation.
   Right = the bench (top view, overview camera, microscope, last check). A
   sample id or a plan revision clicked in the conversation shows its details
   in the right column with "← Bench".
2. **Results** (what this run made, its evidence and its problems; review of
   problems is part of it): left = an index generated from the records only,
   empty groups hidden: Overview; Device (each device, the Advisor's device
   plan, the Designer's layout); Materials (flakes used, all flakes found,
   exfoliated chips); Problems. Centre = the overview: the verdict (the
   request in the person's words, outcome, lab time, jobs, problems, cost,
   testbench faults, "Qumus said (AI)"), device blocks with each layer's
   photo, flake strips, one evidence row per job (last check measured /
   allowed, photos, "record ›" opens the job record in the centre) and "Left
   for a person" (Qumus's own words labelled AI). Right = the details of what
   was clicked; a problem says what happened, the job and action, measured
   against allowed, how it was handled, photos and links. The old Records
   page is gone (its rows are the evidence rows).
3. **Lab** (can the lab run, what is in it): one scenario with a Status |
   Stock switch at the top of the left column. Status: instruments and
   supplies on the left, the lab map with status dots, numbered pins and the
   "Needs a person" list in the centre, the cameras (or the clicked
   instrument's details, "← Cameras") on the right. Stock: categories with
   counts on the left, a card wall with search and material filter in the
   centre, the item's details on the right, where it is in words and its
   history, no picture of the lab (Jialei). "New PC film" sits in a used or
   torn stamp's details (live only). The flake library window is gone: the
   Flakes category is the library, a flake's details take the right column.
4. **History** (earlier runs, demo recordings, experience): a Runs |
   Experience switch at the top of the left column. Runs: "This computer"
   (the earlier sessions of this run store, live only) and "Demo recordings"
   (./recordings). The centre shows the picked run read-only (Results,
   Progress, Conversation or its saved Experience) with "▶ Play recording"; the right lists its key
   moments, a click plays from there. Experience: "No real experience yet",
   the confirm / retire rule, filters by status and topic, cards, and an
   entry's provenance and votes on the right.
5. **Demo**: a recording plays in the page (`?rec=<name>` or `?run=<id>`,
   `&at=<event index>`), with a REPLAY badge and "Back to live" while the live
   run goes on (it says when that run needs a person). `qumus2 replay` stays.
6. **General.** The mode is stated once, in the title bar. Phone (≤ 860 px):
   tabs Chat / Progress / Lab / More, one column at a time, ‹ goes back a
   column, "Bench ›" and "Cameras ›" reach the right column. Fixes: the
   microscope caption names the objective of the photo it shows; the empty
   tree says Qumus has not written a plan yet; skipped pauses under 5 s get no
   mark.

Contract changes, all additive:

- **C6 (run store).** `meta.sessions`: where each session (one `serve` or
  any `build_services`) began, a JSON list of event ids, oldest first; a
  session is the events after its id up to the next one's. Methods
  `set_meta`, `sessions()`, `begin_session(after_id)` and
  `run_ids_since(after_id)` (the jobs that wrote an event after it).
  `Services.session_start` is the id the current session began after
  (taken before the bench seeds the ledger); `build_services` records it.
- **C12 (UI API).** `/api/health` adds `waiting` (this session's jobs that
  wait for a person or are parked); `/api/snapshot` adds `session_start`.
  UI-only endpoints: `GET /api/history` (the earlier sessions with a
  request, newest first, summed up once: request, outcome, jobs, devices,
  flakes, lab time, cost, agents, wall start / end), `GET
  /api/history/{id}/recording` (that session as a recording, photos stay
  `/api/image` paths), and the replay server's `/api/recordings`,
  `/api/recording?name=`, `/api/recordings/{name}/media/{file}` on the live
  server too.
- **Recordings (`ui/recording.py`).** `summarize_events` sums a run up from
  its events alone, by the same rules the page's Results use: only what
  happened after the request counts (the ledger seeding does not); outcome
  is "needs a person", "stopped", "error", "done" (every plan step done or
   skipped), "awaiting review", "failed", "answered" or "unfinished".
   "Awaiting review" requires an explicitly active human plan step, all
   non-human steps done/skipped, and no active jobs. It takes precedence over
   historical failed jobs, which remain in Evidence, but not parked jobs,
   stops or agent errors. Python and JavaScript summaries use the same rule.
   The timeline file
  (`runs/timeline.jsonl`) is read per session: from the session's own start
  mark to the next one, so a deleted and restarted run store (which reuses
  event ids) never borrows an older serve's wall times.

## First real-Claude serve on the Mac (2026-10-23, same thread)

Five faults the owner found opening `serve --claude` on a run store kept
since 2026-10-21:

1. **Claude check said "unreachable" (`agents/health.py`).** Python from
   python.org on a Mac has no system CA store, so the free `GET /v1/models`
   failed with `CERTIFICATE_VERIFY_FAILED` while real turns (httpx, certifi's
   bundle) worked. The check now uses certifi's bundle when installed
   (`tls_context()`), the same trust as the turns.
2. **"2070 h lab time" in History (`ui/recording.py`, `static/session.js`).**
   A store older than the session marks holds several serves in its first
   session: wall-time stamps (about 1.79e9) and a testbench clock from 1.8e9.
   Lab time was max(ts) - min(ts). It is now the sum of the clock's forward
   steps between events, without jumps longer than `LAB_GAP_MAX_S` (1 h) or
   backwards. Every committed recording keeps its lab time (largest step in
   any of them: 245 s).
3. **"No free target chip" on a new bench (C6/C7, additive).** Each serve
   without a saved lab-process state seeds a new testbench, but the ledger
   kept what the earlier bench made: device_001 still sat on the electrode
   chip, and flakes the new bench does not have stayed on chips the agents
   could pick. `build_sim_lab` with a store now takes every sample out of the
   ledger before seeding (new store method `remove_sample(id, reason, emit)`;
   event `sample.removed` {id, kind, reason} for the ones the seed does not
   bring back), so the seeded samples start without their old props. The
   events keep the history; History and replays are unchanged. The Lab
   page's inventory drops a sample on `sample.removed`. A `--lab-process`
   serve that restores its saved world keeps its ledger as before.
4. **Ctrl+C did not stop `serve` with the page open (`ui/app.py`).** The
   `/ws/events` loop only sent; it never read, so it never saw the socket
   close when uvicorn shut down, and uvicorn waited for it (a second Ctrl+C
   then printed tracebacks). The loop now also listens (`_until_closed`) and
   ends on the close; uvicorn's `timeout_graceful_shutdown=3` is the backstop.
   Test: `tests/test_serve_shutdown.py` (a real serve, a socket, SIGINT).
5. **`serve` did not open the browser.** It does now, as `replay` does
   (`--no-browser` to skip).

## Review corrections (2026-10-25)

The UI, agent system and software testbench remain separate. These changes
repair presentation and image delivery; they do not add physical capability.

- `scripts/start_qumus.py` is the tracked launcher implementation. The project
  home's one `Start Qumus.command` invokes it. Live work, History, Demo replay
  and each recording's Experience use the same app on port 8765. Retired
  standalone review launchers and duplicate exports are archived outside the
  active repository. Published demos live only in `recordings/`; private
  source databases and diagnostics remain in `runs/`.
- `BenchCameraRenderer` produces a simple 2D diagram from world objects, tape
  and arm positions. On owner review, the purpose is to demonstrate camera
  frame delivery, saving and display before connecting real cameras; visual
  realism is not an acceptance criterion. Regions, flat sample markers and
  arm-position crosshairs replace perspective, machine bodies, shading and
  sensor texture. The annotated UI top view remains separate.
  The renderer reuses an immutable file for unchanged state and never draws
  random physical outcomes. These pictures are illustrative, not calibrated
  photographs or a validated optical model. Existing recordings retain their
  original captured images; new captures use the simpler renderer.
  Simulated verification still
  uses the existing observation models, not computer vision on these pixels.
- Electrode geometry belongs to the testbench chip (`SimObject.electrodes`),
  in chip-local millimetres. Both the inventory reference image and later
  microscope captures use this geometry, transformed by the actual chip pose.
  The pattern is a synthetic fixture, not a real fabrication mask. Optical
  images carry a SIMULATED mark. Sample props may include `image_source` and
  `image_description`; those fields are included in `sample.updated` events.
- Target-chip and finished-device images are attached to query tools and
  rendered in Lab/Results/details. OpenAI receives image content from the same
  saved files the UI displays. Files receive increasing names across restarts
  so a new session cannot overwrite historical photos. Microscope preview
  metadata belongs to the saved capture, not the current turret setting.
  Restoring an older software bench adds missing electrode geometry and a
  clearly labelled reference image without resetting poses, flakes or RNG.
  Existing image evidence remains unchanged.
- The SDK input filter keeps up to four recent distinct images per model
  request. Repeated pixels are referenced once; older omitted pixels are
  explicitly identified as omitted and can be requested again with image
  tools. Repeated full project snapshots are replaced by references to the
  latest snapshot; user messages, tool IDs and all other text are retained.
  Saved history is never edited by this input filter.
  This bounds visual context and request reservations without relaxing the
  persistent dollar caps. A specialist's laboratory summary is refreshed
  before every delegation, including repeated delegations in one turn.
- A user-requested review point takes precedence over default autonomous
  continuation. Acceptance recordings include short operator messages sent
  through the normal instruction inbox; their metadata identifies those
  messages as scripted acceptance inputs. Model replies and laboratory jobs
  are executed, not pre-recorded answers or prepared-device fixtures.
- History uses recording provider metadata rather than assuming every SDK run
  is Claude. Each selected demo has its own read-only Experience snapshot.
  Token-rate estimates are labelled `est.` and are not gateway invoices.
  User interruption is shown as a pause, not a model failure.
- Replay integrity checks validate nested material, target and device image
  references as well as top-level tool photos. Camera loading retains the
  last good frame and rejects stale loads after seeking. Tool status uses one
  labelled glyph per call instead of a CSS circle plus a circle character.
  Playback uses the same one-hour clock-jump cutoff as History summaries:
  switching between agent wall time and a separate lab's simulated epoch must
  not add months to the displayed experiment duration.
- The agent-facing `StackDeviceArgs.flake_ids` now matches the existing
  `StackParams` contract (one to six distinct physical flakes). A single
  graphene flake on pre-patterned metal is a valid transfer. Existing metal is
  part of the target chip, not an invented second flake. Physical uniqueness,
  selection, design and SafetyGate checks are unchanged.
