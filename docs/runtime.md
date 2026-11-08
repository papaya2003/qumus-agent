# Runtime setup and validation

## Scope and boundaries

The primary model runtime is `OpenAIRuntime` (`qumus2/agents/openai_runtime.py`),
using `openai-agents==0.24.0`. It implements the existing `AgentRuntime`
interface: `send`, `busy`, `interrupt`, `continue_turn`, `close`.

The current model adapter is OpenAI Agents SDK. It does not change the simulated
world, physical error model, hardware interfaces, closed-loop flows or
SafetyGate rules. The browser keeps its existing layout and event-driven
architecture. Small UI changes label OpenAI correctly while retaining the
original Claude labels on historical recordings.

## Run locally

For Finder launchers and everyday instructions, see
[Open Qumus without an assistant](local-start.md).

```sh
python3 scripts/setup_qumus.py
# Fill the local .env with the project key, full model ID and token rates.
../"Start Qumus.command"
```

The setup script creates `.venv`, installs the modern dependencies, copies
`.env.example` only if `.env` is absent, and generates the single root launcher
from its tracked template. It also hides generated Python/editor clutter while
preserving existing editor preferences. It never starts Qumus or calls a model.
Use `--ui-tests` to install browser-test dependencies and Chromium. Use
`--with-v1` only when the isolated frozen-v1 environment is needed.

On an already configured workstation, regenerate support files without
reinstalling anything:

```sh
.venv/bin/python scripts/setup_qumus.py --local-only --refresh-launcher
```

The normal launcher uses the named Hall-bar profile and
`runs/workspace/hallbar-20261101/qumus.db`. The earlier twin, integrated and
original workspaces remain unchanged. `--integrated-workspace` selects the
former twin path/profile; `--previous-workspace` retains the
original path/profile but cannot convert an incompatible old checkpoint; use
the existing exporter/replay for historical review. See [local startup](local-start.md).
Direct CLI `serve` commands use `runs/checks/manual/qumus.db` unless `--db`
is supplied; this keeps development experiments separate from everyday work.

Plain `serve` remains scripted and sends no model requests. `--openai` is an
explicit paid opt-in. The old `--claude` option was removed. A missing key or
model is an error, never a silent switch to scripted replies.

For the sponsor's Token Station endpoint:

```text
QUMUS_OPENAI_API_KEY=<your local key>
QUMUS_OPENAI_BASE_URL=https://physics.bytefuture.ai/v1
QUMUS_OPENAI_API=responses
QUMUS_OPENAI_STREAM=0
QUMUS_OPENAI_MODEL=openai/gpt-6-astra
QUMUS_OPENAI_WEB_SEARCH=1
QUMUS_OPENAI_INPUT_USD_PER_MILLION=<gateway input rate>
QUMUS_OPENAI_OUTPUT_USD_PER_MILLION=<gateway output rate>
# Optional; leave blank when the model's cache-read rate is unverified.
QUMUS_OPENAI_CACHED_INPUT_USD_PER_MILLION=
QUMUS_OPENAI_STAGE_BUDGET_USD=200
QUMUS_OPENAI_TEST_BUDGET_USD=40
QUMUS_OPENAI_BUDGET_DB=runs/openai-budget.db
```

Copy the complete model ID from the gateway; do not remove `openai/`. The
base URL includes `/v1` but not `/chat/completions` or `/responses`. Qumus does
not inherit global `OPENAI_API_KEY`, Codex settings, or Anthropic credentials.

Responses tools, images and hosted web search were verified on this endpoint
with the selected model on 2026-10-25. Chat Completions was also validated,
including a complete device build. These are transport options within the
same OpenAI Agents SDK, not different agent implementations. Native web
search is opt-in through `QUMUS_OPENAI_WEB_SEARCH=1` and requires Responses.
In Chat Completions mode leave it off; the advisor then explicitly discloses
that external web evidence was not fetched. There is no automatic protocol
fallback that could silently retry side effects.

Custom gateways default to buffered model replies (`QUMUS_OPENAI_STREAM=0`).
The SDK and browser still receive progress events; text appears per completed
model call. `agents/openai_models.py` calls the SDK model's non-streamed
method and emits a complete response locally, preserving every tool's ID,
name and arguments. It never tries to split corrupted JSON into guessed
commands. Set `QUMUS_OPENAI_STREAM=1` for native upstream streaming after
validating that endpoint. `scripts/probe_model_stream.py` compares raw
streamed/non-streamed tool responses without executing the mock tools.

## Team, tools and enforcement

Full-page Settings supports
one default model and per-specialist overrides. All roles still share the same
gateway and protocol. Save the whole team after the current Task Plan and jobs
are completed or resolved; the next plan starts fresh conversation context.
Reservations, usage cost and request capacity use the actual role's selected
model. Old single-model project configurations continue to inherit one default.
See [the prototype contract](agent-model-settings.md); this is not merged to main.

The orchestrator calls six specialists as tools (`Agent.as_tool`):
`delegate_advisor`, `delegate_eyes`, `delegate_hands`, `delegate_picker`,
`delegate_designer`, and `delegate_reactor`. The input is a string beginning with the plan-step ID,
then the task, sample IDs, recipe and any required context. Specialists return
control to the orchestrator; they do not take over the user's conversation.

Each specialist receives only its registered tools. Each domain tool calls
`ToolGateway.call` before execution, enforcing the same role, schema, sample
references and cross-tool rules as the scripted runtime. Delegations go
through `ToolGateway.delegate` and their deliverables are checked against run
records. SDK lifecycle hooks record usage; permission checks remain in Qumus'
execution path. Both provider-level parallel tool calls and SDK function-tool
concurrency are restricted, and all specialists share request pacing.

The existing role prompts and reference skills are reused. Skill text is
loaded for its owning agent. The old v2 Claude SDK adapter, CLI option,
connection probe, credential handler and plugin manifest have been removed.
`AgentRuntime` remains the common interface for OpenAI and scripted execution.

## Images, messages, stop and restart

Images returned by tools are converted to SDK image outputs with pixels.
Host paths alone are not offered as image evidence. All model and specialist
messages, tool results and delegation checks use the existing event schema.
Pending user messages still appear in expert tool results and are delivered
to the orchestrator when the specialist returns.

OpenAI conversation history is in `<run-db-stem>.openai-sessions.db`; its ID
is stored separately in project state (`openai_session_id`) so legacy Claude
sessions are not overwritten. Each turn refreshes the authoritative project
summary, including existing jobs, in every agent's instructions.

Stop cancels model work and prevents late tool execution; ConversationDriver
then pauses jobs at their existing safe points. Continue uses the original
job recovery logic and gives the model the resulting state. Conversation
history is never a substitute for checking actual job/device state.
Use `serve --openai --lab-process` for a bench that persists across server
restarts. Plain `serve` still creates a fresh simulated bench by design.

## Request tokens and context capacity

Open **History → a run → Context** for the complete curve. Context diagnostics
are excluded from experimental Results. Input and output
token curves use the service's per-request usage report, across agents and models.
No local tokenizer or character-to-token conversion is used. Both supported API
protocols and buffered/native streaming modes use the same normalization.

Select a point to see its agent, model, input/output tokens, used tokens
(input + output), context limit and remaining tokens (limit - used).
The administrator table supplied on 27 October is packaged in
`qumus2/config/model_context_limits.yaml`: all 91 exact model IDs are retained,
including 20 zero entries treated as unknown. It applies only to
`https://physics.bytefuture.ai/v1`. Switching automatically selects the matching
limit; it does not add gateway permissions or guess aliases between providers.

**Settings → Model → Context limit** shows the read-only automatic value and its
gateway catalog source. Choose a model and **Save settings**; changes take effect
from the next plan. An optional
`QUMUS_OPENAI_CONTEXT_WINDOW_TOKENS` overrides the table only for its accompanying
initial model ID. Saved per-model/per-gateway settings take precedence over
that environment setting. Only overrides are persisted; automatic values follow
the packaged table on restart. Unlisted models never borrow the preceding limit.
Each recorded request retains its original limit even after later changes.
At 90% of configured capacity the detail displays a warning. Usage above the
configured limit is shown explicitly so an incorrect deployment setting can
be corrected. This does not validate separate input/output caps.

Reports are joined to individual send attempts. Failed retries and responses
without usage remain unknown, not zero. The raw provider fields are preserved
through SDK conversion, including buffered replies. Cache tokens remain part
of input. Incomplete or failed Responses streams also retain usage when the
terminal response includes it. Reasoning tokens are already part of reported
output. The extra `agent.request_usage` records do not alter cost events or the
budget ledger.
The existing character counters remain diagnostic data only.

Older recordings with saved per-response tokens show those reports with unknown
context limits and no inferred retry linkage; ambiguous old zero defaults remain
unknown. Recordings with only characters have no token curve. Original records
are never rewritten. Remaining capacity describes the returned report; it is
not an exact preflight measurement or an automatic history-compaction policy.

## Budget and data

Before a paid request, a persistent SQLite ledger reserves a conservative
estimated allowance for its input and maximum output. It checks both the
stage-total cap ($200 by default) and the per-project run cap ($40 by default).
An explicitly authorized `QUMUS_OPENAI_STAGE_BUDGET_USD=unlimited` disables only
the stage-total gate; per-project limits, reservations and settlement remain.
Different test databases
share `runs/openai-budget.db`; restarting does not reset this ledger. A call
with no returned usage retains its reservation as unknown spend. Do not
delete the ledger to get around a cap. Each model response also writes local
`agent.usage` events; turn totals are not counted twice.

For the 27 October repair acceptance, the owner explicitly removed the
phase-total spending ceiling after correcting our cost interpretation against
their provider dashboard. The local stage-total setting is now `unlimited`;
no historical rows or reservations are reset. The owner reports about $140
total charges and estimates about $30 for the phase, which have not been
independently verified. The normal aggregate token guard is 2,000,000 per
turn; it is not a model context-window limit.

The conservative budget guard reserves input at the configured uncached rate,
including image allowances, and allows the maximum configured output. Returned
usage may settle cached input at an explicitly configured
`QUMUS_OPENAI_CACHED_INPUT_USD_PER_MILLION` rate. Only valid reported cache counts
receive that rate; missing or invalid counts use the uncached rate. A model or
tariff change cannot inherit another model's cache quote. Unknown failed sends
retain their original reservations. Hosted-tool fees may be billed separately.
These estimates cannot guarantee the provider's invoice.

The migration used $20 input / $100 output per million tokens and counted cached
input at full price. That was an oversized planning allowance, not the account's
bill. On 29 October, the coordinator reconciled three exact dashboard charges
against saved input, output and cache counts: each matched $10 uncached input,
$1 cached input and $50 output per million tokens. The dated account's Astra
increase also matched the campaign's 255 known responses and 3,993,513 tokens;
the displayed model-charge increase was $16.14. This supports the account-scoped
forward estimate, not a universal published tariff or a claim that unknown failed
sends were free. Historical events, exports and ledger amounts are unchanged.
Set account-side limits in Token Station for a provider-enforced dollar cap.

Those historical billing observations do not qualify the current Hall-bar
campaign. Its first Astra recording has an inclusive estimated USD100 cap,
including qualification and failed/unknown requests, with input/output rates
of USD10/USD50 per million and no assumed cached-input discount. Its separate
finite admission checks are described in [sim-contracts.md](sim-contracts.md#current-hall-bar-recording-contract).
The remaining fourteen recordings have no execution authorization or budget.

Settings displays a separate cost estimate calibrated from a private
`gateway-billing.json` snapshot beside the workspace database. Optional per-model
`tokens` counts allow a blended rate of billed USD / reported total tokens, applied
only to matching OpenAI-runtime response usage with the same saved gateway identity.
Failed attempts without usage and historical records with unknown gateway identity
are excluded explicitly. When none can be calibrated, the UI shows unavailable,
not a zero-dollar estimate. This is an account-average estimate:
cache hit rates and output mix may differ for a particular task. Missing usage,
legacy ambiguous counts and uncalibrated models are excluded explicitly. Account
totals and timestamps are shown in a collapsed snapshot, with a link to the bill.
Snapshots, historical events, budget ledger settlement and reservations remain
separate; this display projection never rewrites them. Playback makes no requests.

External SDK tracing is disabled by default and includes no lab data. Local
events remain the audit source. No keys, `.env`, databases or live test
recordings are committed. Model requests deliberately include the prompt,
tool context and images needed for the test and are sent to the configured
gateway only.

## Validation and review

See [the archived validation report](archive/openai-migration-validation-2026-10-25.md)
for the original migration results, failed attempts, streaming observations
and cost-estimate limitations. Later UI corrections have their own
[dated evidence](archive/review-corrections-2026-10-25.md); historical counts are not current
regression results.

```sh
ruff check .
pytest tests/test_openai_runtime.py
pytest                         # full offline suite, no paid calls
```

The following acceptance commands spend API credit and require the owner's
authorization and configured budget limits:

```sh
python scripts/check_model.py connect
python scripts/check_model.py place --askew
python scripts/check_model.py flake
python scripts/check_model.py device
python scripts/check_model.py device --fault stamp_torn
```

Live checks are opt-in, capped, and write `summary.json` plus a replay under
`runs/checks/model/`. Review the summary's explicit pass/fail flag and actual
sample/job records. A model's success sentence is not an acceptance test.
Offline tests run the real SDK with deterministic model responses; they
validate the integration, not live scientific judgement.

The v1 comparison source stays byte-identical. Its isolated-process wrapper
binds v1's `agents` namespace explicitly, avoiding a name collision with the
installed OpenAI SDK's Python package of the same name.

The legacy v1 agent's LiteLLM currently requires `openai<3`, while OpenAI
Agents SDK 0.24.0 requires `openai>=3`. `.[v1compare]` supplies shared comparison
helpers and the v2-side flow checks and can coexist with the new SDK.
Executing frozen v1 (including flows, whose package initializers import
LiteLLM) additionally needs `.[v1agents]` in a separate environment:

```sh
python3 -m venv .venv-v1compare
.venv-v1compare/bin/pip install -e ".[dev,v1compare,v1agents]"
.venv-v1compare/bin/python -m pytest tests/test_v1compare.py
```

Use `.venv-v1compare` only for frozen v1 and comparison checks; never combine
`agents` and `v1agents`. Frozen v1 includes its own original Claude specialist
integrations, so `v1agents` keeps their SDK dependency in that isolated
environment. This does not enable a Claude runtime in the current application.
Validate the environments separately locally, including v1 cost-meter tests. GitHub CI is disabled for this phase.

## Preserved data and historical playback

The normal launcher opens `runs/workspace/hallbar-20261101/qumus.db`. Earlier events, session
IDs, images, simulated state and budget amounts are preserved. The budget
ledger stays at `runs/openai-budget.db`; the local `runs/layout.json` maps
relocated data and keeps the old per-run budget scope. Keep both files.
See [run data](run-data.md) for the directory policy and archive locations.
Historical Claude recordings play without the old SDK. Only the isolated
frozen-v1 environment retains its original specialist dependency.

The live UI now uses `model_status` and `/api/model/check`; the old `claude`
health field and `/api/claude/check` endpoint have been removed together.
Reload the browser after restarting the application.

## Official references

- [Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk)
- [Migration from Claude Agent SDK](https://developers.openai.com/cookbook/examples/agents_sdk/migrate-from-claude-agent-sdk/readme)
- [Agents as tools](https://developers.openai.com/api/docs/guides/agents/orchestration)
- [Session state](https://developers.openai.com/api/docs/guides/agents/running-agents)

## Tool images and request measurement

Chat Completions sends tool-returned images as labelled image messages after all
text replies in their tool-call group. The labels preserve the source tool and
call ID and identify the content as tool evidence. Responses retains native
image tool outputs. Saved SDK history and ToolGateway execution are unchanged.
Ambiguous tool identities and file-ID images fail explicitly in Chat mode;
a provider's image rejection is surfaced without a silent text-only fallback.
The selected model/deployment must support image input.

Request lengths are measured at the final HTTP-send boundary after image
filtering, transport adaptation and SDK conversion, including added text labels
and excluding image pixels/base64. Retries produce separate measured attempts.
History → Context describes the complete selected recording; Current run → Context
uses the live session. Results contains experiment outputs only. Scripted execution
and old recordings missing measurements have separate empty states. The detailed
right inspector uses [actual-source composition](context-composition.md), clearly
labelled rough text estimates, and unknown media/protocol totals. Those estimates
never change reported token usage, capacity or billing.


### Request failures and retries

The request inspector distinguishes response received, failed/interrupted,
retried and recovered attempts. Sanitized `agent.request_failure` events record
HTTP categories or transport/model errors without storing exception messages,
response bodies, URLs or credentials. The SDK still controls retry count, backoff
and timeouts; this instrumentation does not eliminate provider outages.

A successful retry keeps its own token report. Earlier unknown usage stays
unknown, and failure events never add estimated spend. Old recordings can link
recorded attempts of the same agent/call/model/session, but cannot reconstruct
unrecorded failure causes. Prefix playback cannot see later recovery evidence.

The modern base runtime includes NumPy>=1.26 for bounded native RGB optical-evidence arrays. It does not require the frozen v1 comparison extra. Install the current project dependencies in `.venv`; keep `.venv-v1compare` separate.
