> Historical snapshot. Current ownership and instructions are in
> [workstreams](../workstreams.md), [architecture](../architecture.md) and
> [local setup](../local-start.md). Earlier test counts apply only to the
> revisions described below.

# OpenAI migration validation — 2026-10-25

This is the original migration report. The subsequent owner-review corrections
are documented in [dated review corrections](review-corrections-2026-10-25.md). Its earlier
test counts and spending are historical, not the latest totals.

## Configuration and scope

Migration base: `9b3e116`. Real-model requests used the owner's Token Station
endpoint `https://physics.bytefuture.ai/v1`, model `openai/gpt-6-astra`.
All laboratory actions used the software testbench. No real hardware was used.
The UI layout, testbench physics, existing flows and SafetyGate are preserved.

The final local connection uses Responses with buffered replies and opt-in
hosted web search. Chat Completions was also tested. Both use the same new
OpenAI Agents SDK runtime. Complete replies are adapted locally into SDK
events; no streamed JSON repair or guessed tool calls are executed.

## Automated checks

- Main environment: **369 passed, 2 skipped** (`pytest -n 3`, 252.97 seconds).
  This includes 17 new SDK integration tests and the browser tests.
- Frozen v1 environment: **14 passed**. The main environment's two skips
  require legacy LiteLLM dependencies; they execute in this isolated run.
- `ruff check .`, `git diff --check`, and dependency checks in both environments
  passed. Frozen v1 files retain their original manifest bytes.

The SDK integration tests exercise the real SDK loop with deterministic model
responses: delegation permissions, checked results, images, malformed tool
arguments, user-message delivery, separate session persistence, interrupt and
continue, persistent budget reservations and provider labels. They do not
establish scientific quality or reliability on every possible lab task.

## Real-model evidence

Local evidence stays under ignored `runs/`; simulated images, databases and
replays are not uploaded with the source changes. Costs below are estimates.

| Check | Result | Estimated USD | Local evidence |
| --- | --- | ---: | --- |
| Minimal model connection | Passed | 0.08 | `runs/openai-migration/` |
| Misaligned chip placement, Chat Completions | Passed; one corrective placement, two jobs succeeded | 1.13 | `runs/openai-check/place-askew-v1/summary.json` |
| Complete device build, buffered Chat Completions | Passed; nine jobs succeeded, accepted design and recorded device on `chip_e01` | 11.63 | `runs/openai-check/device-buffered-v2/summary.json` |
| Restart and resume the completed device | Passed; original device/history restored, **no new hardware jobs** | 0.68 additional | `runs/openai-check/device-buffered-v2/summary-resume-*.json` |
| Torn-stamp stacking diagnostic | Passed; `stamp_01` torn, switched to `stamp_02`, device completed | 2.90 | `runs/openai-check/stack-stamp-fault/summary.json` |
| Responses image/tool path | Passed; inspection and placement succeeded, image outputs present | 1.44 | `runs/openai-migration/responses-place.json` |
| Responses hosted web research | Passed; four actual search events and advisor/orchestrator messages | 1.07 | `runs/openai-migration/responses-research.json` |

The torn-stamp diagnostic starts from **scripted preparation through accepted
selection and layout**, then uses the real model for inspection and stacking.
The existing stacking flow performs physical fault recovery. This validates
the new agent's integration with that recovery; it is not a second complete
model-planned device build from an empty inventory. Both the full build and
the focused fault test recorded zero SafetyGate violations, and their replay
integrity checks found no problems.

During detailed review, the full-build recording's final report was found to
call simulated operations a "Real hardware job" and "recorded experimental
results". Those evidence labels are incorrect. Workflow completion passed,
but that report fails the simulated-versus-real wording requirement. The
current runtime includes an explicit testbench instruction; a new complete
run has not established that this instruction is sufficient to prevent the
wording error. The original recording is retained for review.

Failed attempts remain available and are included in phase spending:

- An early placement call encountered gateway HTTP 429 after physical work
  (estimated $1.86). Shared request pacing and bounded retries were added.
- The first full-device attempt stopped at the original 200,000-token turn
  guard ($4.51), without completing the device. The turn guard was raised to
  1,000,000 while keeping the $20 test and $200 phase estimated-dollar caps.
- A full-device attempt with a requested stamp fault stopped before reaching
  stacking ($6.23): the model exhausted all four bare chips on hBN and had no
  graphene. The stamp fault never fired. This is **not** counted as successful
  recovery. Resource planning remains a limitation of the model's strategy.

## Gateway streaming observation

Three tiny Chat Completions probes (streamed parallel, complete parallel,
streamed single) each returned one valid mock tool call. These results neither
reproduced nor ruled out corruption of multiple tool calls.

An additional pair using the gateway's Messages API and the same selected
model returned **one** valid tool call when streamed and **three distinct**
valid calls when not streamed. The diagnostic never executed those mock tools.
This is evidence of a completeness discrepancy in that pair, not proof of the
gateway's internal cause: generation can vary, and concatenated JSON was not
observed in this sample. Evidence: `runs/openai-stream-probe/`.

The final runtime therefore avoids upstream streaming by default and disables
parallel function execution. It preserves distinct complete-response tool
items and rejects malformed arguments before ToolGateway execution. Native
streaming remains an explicit option after endpoint-specific validation.

## Spending and remaining limits

The shared persistent ledger contains **$31.57770** of token-rate estimates
plus **$1.78592** reserved for requests without confirmed usage, totaling
**$33.36362** of conservative phase accounting. Every individual test remains
below $20; the authorized phase limit is $200. The total includes diagnostic
and failed requests, not just successful checks.

The configured estimates use $20 input / $100 output per million tokens,
counting cached input at full price. These are twice the checked OpenAI
standard short-context rates, **not verified Token Station prices**. Hosted
web search may incur separate fees. Only the gateway's billing records and
account-side limits can establish and enforce actual dollars charged.

A later read of the owner's logged-in Token Station Usage page showed the
30-day `openai/gpt-6-astra` aggregate as **251 requests, 1,440,221 tokens,
$6.46**. This is gateway-reported spending, distinct from the local $33.36
budget estimate. That model-level aggregate can include other uses of the
same account; it does not by itself reconcile individual test invoices.

Long-context pricing, arbitrary scientific tasks, all fault combinations and
real hardware have not been certified by these checks. The successful runs
establish a working migration and a reviewable baseline; the failed resource
allocation attempt remains an explicit target for future agent improvements.

## Original independent-use checks (superseded)

The original English launchers let the owner open the project without a chat
assistant. Cold-start checks started
and verified three temporary replay servers. A separate temporary live UI
started with OpenAI Agents SDK and its lab supervisor, served health data,
and shut down cleanly without sending a model task. The review launcher's
output and generated index, and the live launcher's command preview, were
also checked from the project-root shortcuts.

The separate review launcher and index have since been retired. Use the
single launcher and History → Demo workflow in [everyday instructions](../local-start.md).
