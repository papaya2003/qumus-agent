# Experimental model team settings

Originally built on `codex/agent-model-settings` (commit `e742824`, 28 October 2026)
and ported into the integration branch on 4 November 2026. The port keeps the
integration branch's Laboratory settings, catalog access checks, administrator
quotes, non-negative guard rates and cached-input pricing; only the default model
carries a cached-input tariff.

## Interaction

Settings takes the complete workspace area beside the activity bar. Conversation
and the bench are hidden while this page is open; running work is not stopped.
The layout uses one Models & team card, with Cost & billing and Workspace cards
below it. The same page becomes one column on a phone.

The default model serves Qumus and every specialist whose selector says
**Use default model**. Advisor, Eyes, Hands, Picker, Designer and Reactor each
have one row. A row shows its role, selection, resolved model, Default/Custom
status and exact deployment context capacity. Missing limits remain unknown.
Changing the default updates only inherited rows. No quality or cost ranking
is inferred from model names.

**Use default for everyone** changes the draft only. **Discard changes** restores
the last saved configuration. **Save settings** submits the whole team together;
it is disabled when nothing changed. Unsaved choices survive theme changes,
health polling and billing refresh. Saving itself makes no inference request.

Specialized image/video/decision models can be inspected as default-model catalog
entries but cannot be saved as conversational models. Specialist selectors disable
these entries. Known text-only models show a warning for camera-dependent roles.
Catalog inclusion is not proof of gateway access, image support or tool support.

Budget guard rates appear once per selected model under a collapsed section.
Unpublished rates must be supplied before saving; prices are never guessed from
model names or copied from a different model. The separate billed-average cost
display and existing conservative ledger remain separate. This branch also
includes the stable billing correction: calibration requires the actual matching
gateway, failed attempts without usage are counted as excluded, duplicate response
IDs are not priced twice, and all-unknown data does not become a zero-dollar bill.
Only complete provider-reported `agent.request_usage` measurements are priced;
SDK-filled defaults in cost events cannot establish complete token usage. Real
reported zeros remain valid zero usage.

## Runtime contract

The existing `/api/model` request gains optional `agent_models`, a closed map from
specialist ID to `{model, input_price, output_price}`. An omitted map preserves
existing overrides; an empty map removes them. Unknown roles, unsupported models,
invalid rates and conflicting rates for the same model reject the entire save.
The endpoint, key, protocol and transport remain shared by the team.

Overrides are stored inside the existing project `openai_model_config`, scoped
to its exact endpoint. Historical records without overrides inherit the default.
`/api/models` returns both explicit overrides and `effective_models` for all seven
roles. These are projections of the same configuration, not a second registry.

Saving is refused during a turn, pending instructions, unfinished jobs or an
unfinished Task Plan, including idle gaps between plan steps. After the current
plan is completed or resolved, a save starts fresh SDK conversation context for
the next plan. Model history, lab state and budget records remain intact.

Each SDK Agent receives its resolved model. The same resolution supplies request
allowances, usage settlement and request context-limit snapshots. Usage events
record the actual calling agent's model and rates; HTTP request/retry observers
retain their existing attempt identity and transport behavior. Recording metadata
derives model IDs from actual send and usage events, including failed specialist
attempts, not unexecuted selections. Failed sends still have unknown usage/cost.
The official SDK supports explicit
[per-agent model selection](https://developers.openai.com/api/docs/guides/agents/models).

The prototype includes the frozen [input-composition contract](context-composition.md).
Instruction ranges, current user/delegated task and generated tool-image evidence
hints are captured for the actual calling role. These counts-only diagnostics
remain separate from its model capacity, reported usage and cost. Both API
protocols and native/buffered modes retain the same per-role routing.

## Verification and rollback

Offline acceptance uses real SDK delegation with deterministic model responses,
checks per-model costs and context snapshots, persistence, endpoint isolation,
atomic rejection and the unfinished-plan boundary. Browser checks cover draft,
save, discard, inheritance, unsupported entries, warnings and three viewport sizes.
Private screenshots and test XML are under
`runs/checks/agent-model-settings-evidence/` in this checkout.

No paid provider compatibility run has been made for this prototype. Real hardware
and scientific validation are unchanged. To leave the prototype, use the normal
main checkout and launcher. Main uses its own existing workspace and receives no
prototype settings. Nothing needs to be migrated back or deleted.
