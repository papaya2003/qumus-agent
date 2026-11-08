# Per-request input composition

Implemented after the owner's approval on 28 October 2026. The main History
chart remains provider-reported input/output usage and recorded model capacity.
Select a request to see all ten input categories in the right inspector. Each
category can expand to show its measurement basis; the main chart has no
composition stacks, duplicated details or estimates substituted for usage.

| Category | Actual source | Attribution boundary |
| --- | --- | --- |
| System and agent instructions | Agent prompt and runtime rules | Assembly ranges outside reference and laboratory sections |
| Reference skills | Agent-configured reference blocks | Exact ranges captured when the instructions are assembled |
| Current experiment snapshot | Refreshed laboratory-record suffix | Current Task Plan, requirements, project state and linked-job evidence |
| Current user or delegated task | Current Runner / specialist input | Last matching actual user item for the active task |
| Conversation history | Resent prior messages | Earlier user messages and model answer text; excludes structured tool calls/results |
| Tool definitions | Actual outgoing tool schemas | Compact JSON schemas/descriptions once per request |
| Tool call arguments | Resent structured calls | Tool names and arguments, excluding protocol IDs |
| Tool results | Actual tool outputs | Returned text, records, errors and specialist reports; media separate |
| Images and other media | Actual outgoing content parts | Attachments after input filtering, deduplication and conversion |
| Protocol and unassigned input | Opaque/framing or unattributed content | Hidden framing stays unknown; visible unattributed text is counted separately |

## Measurement and privacy contract

`agent.request.input_composition` is an additive version-1 counts-only snapshot
of each actual HTTP send attempt, alongside its existing request/call/session,
agent, model and protocol identity. Attribution runs after SDK filtering and
transport conversion. It does not alter prompts, tool calls, retry settings,
usage settlement, sessions or persisted transcripts. Runtime instruction ranges,
current-task text and generated tool-image evidence labels are ephemeral hints;
none of those strings is copied to the composition event.

Instruction attribution verifies that captured assembly sections reproduce the
actual outgoing instruction text. Unexpected prefixes or missing task provenance
stay unknown/unassigned rather than being guessed from headings. In Chat
Completions, generated tool-image evidence is still tool output, not a new human
task. Laboratory data in the current instruction snapshot belongs to that
snapshot; the same kind of data returned by a tool belongs to tool results.
Every transmitted occurrence is counted once. A repeated occurrence really sent
again remains part of that request; observing it does not deduplicate the prompt.

The ten fixed component IDs are `system_instructions`, `reference_skills`,
`lab_snapshot`, `current_task`, `conversation_history`, `tool_definitions`,
`tool_calls`, `tool_results`, `media`, and `protocol_unassigned`. Rows preserve
`text_bytes`, text/attachment `item_count`, `text_estimated_tokens`, nullable
`estimated_tokens`, and `estimated` / `partial` / `unknown` / `not_present` status.
The envelope includes `version`, `method`, `status`, `text_estimated_tokens`,
`media_count` and `opaque_count`. It retains no extra prompt text, media URLs,
pixels, base64, file IDs, raw tool content or arbitrary exception messages.
An instrumentation failure produces an unavailable diagnostic, not a failed call.

## Estimate interpretation

The formula is **ceil(UTF-8 text bytes / 4), once per category**, identified by
`utf8_bytes_div4_v1`. This lightweight, offline heuristic is not a model tokenizer.
There is no tokenizer installed in the current runtime, and this feature adds
neither heavy dependencies nor token-count API calls. Language, model, JSON,
code, punctuation and component boundaries can produce substantial error. Exact
UTF-8 byte counts do not make the derived token counts exact. The UI marks each
estimated count with an approximation sign and identifies the method.

Any percentage has the explicitly labelled denominator **estimated text subtotal**.
It is not a percentage of actual provider input, used context capacity or billing.
The subtotal is the sum of the per-category text estimates, including identifiable
unassigned text. It excludes media, opaque input and server framing. It is never
scaled to the provider total. No residual is presented as measured image or
protocol tokens; their total token counts remain unknown. Known absent components
may show zero, while unavailable attribution cannot become a measured zero.

Provider-reported input/output, cache and reasoning fields retain their original
meaning. The output total remains separate; a reported reasoning count is marked
as already included in output. Composition estimates never fill missing usage,
change the used/remaining capacity, settle costs or enforce a context limit.
Failed attempts can retain outgoing composition while their response usage stays
unknown. A successful retry reports usage only for its own request ID.

Older recordings without composition show "not recorded". No old category counts
are inferred from characters, totals, newer settings or another model. Existing
export/replay includes the additive event field without rewriting recordings.

## Related request identity and model switching

New request events derive `gateway` from the actual HTTP request URL: normalized
scheme/host/port and deployment path without the API operation, credentials,
query, fragment or trailing slash. Request-usage and completed `agent.usage`
records retain that same request identity. Historical events without gateway
identity remain unknown; the current configuration is never backfilled into them.

Model switching also refuses an unfinished Task Plan during idle gaps between
jobs. Only the existing plan terminal statuses `done` and `skipped` allow switching
when there are no active jobs, turns or pending instructions. Failed, missing or
invalid statuses stay unresolved. This does not add per-agent routing or new
execution policy.

## Local validation

Offline unit tests cover both protocols, Unicode, instruction-range provenance,
repeated task text, tool arguments/results, media exclusion, privacy, diagnostic
failure and sanitized deployment identity. Existing mocked SDK loops cover native
streaming/buffered calls, specialists, refreshed snapshots and retries. Browser
checks use private synthetic replay fixtures, verify all ten right-side categories,
unknown historical/failed usage and model capacity, and exercise mobile layout.
No inference credits or real hardware are used by these checks.
