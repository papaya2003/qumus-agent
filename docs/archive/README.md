# Historical documentation

These records preserve earlier plans and implementation decisions. Their
framework choices, mechanics assumptions and test counts apply to the dated
context in each document. Current instructions and contracts remain in the
[main document index](../README.md).

- [Architecture decision history](architecture-decisions-through-2026-10-25.md)
  retains the chronological notes formerly appended to architecture.md.
  Some mechanisms remain implemented; later decisions supersede conflicting
  earlier assumptions. Read the current contracts before applying one.
- [Stage-1 workstreams](stage-1-workstreams.md) preserves the earlier
  testbench/SDK/UI work allocation and acceptance goals.
- [Original migration validation](openai-migration-validation-2026-10-25.md)
  records the checks and limitations of the initial SDK migration.

- [Presentation review corrections](review-corrections-2026-10-25.md) records
  chronological fixes, intermediate states and dated evidence. Current usage
  and final behavior are documented in local-start.md and architecture.md.

## Dated reports, 27 to 29 October 2026

The owner rejected this round on 31 October 2026 and restarted with the
Hall-bar workflow. These reports describe that earlier round only.

- [Initial 27 October delivery](release-2026-10-27.md): acceptance withdrawn
  after the integration audit.
- [27 October integration audit](integration-audit-2026-10-27.md): findings,
  repairs and evidence boundaries of that round.
- [Reactor acceptance, 27 October](reactor-acceptance-2026-10-27.md) and the
  [reactor integration handoff](reactor-integration-handoff-2026-10-27.md).
- [28 October integration](release-2026-10-28.md): scope, structure review
  and local acceptance status.
- [29 October iteration](iteration-2026-10-29.md): the reset notice.

The owner workspace separately preserves old planning notes and browser
handoffs under `archive/2026-10-26-cleanup/notes/`, and the original full
specification under `archive/2026-10-25-review/leader-handoff/`. These local
archives are outside this Git repository.
