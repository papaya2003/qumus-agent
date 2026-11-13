# Qumus development instructions

- Private lab code. Do not publish source or lab data to public locations.
- All project code, comments, commit messages, UI text, documentation,
  launcher messages and new filenames are in English.
- Read `docs/architecture.md` (brain, stations, record feed, stations.yaml) before changing code. This repository is the brain only: never add laboratory code (flows, devices, safety, simulation) here; that belongs to qumus-simlab or qumus-lab.
  Update the architecture document when changing a contract.
- Use `.venv/bin/python` for the current OpenAI Agents SDK runtime. See
  `docs/runtime.md` for setup, transport, budgets and validation.
- Preserve the UI / agent / testbench boundaries. Devices are accessed only
  through `SafetyGate`; physical error models and safety limits are not agent
  preferences. Flow and runtime code use the provided `Clock`.
- Do not connect to or operate real hardware without the owner's explicit OK.
- Never commit keys, `.env`, run databases, lab photos or model weights.
  Only explicitly scrubbed, simulated recordings belong under `recordings/`.
- Run `ruff check .` and `pytest` before pushing. The live OpenAI checks spend
  API credit and require authorization and configured budget limits.
- GitHub is repository storage only for this phase: no CI, Actions dispatch,
  reruns or remote validation. Run all verification locally. Keep automatic
  workflow triggers disabled when pushing the accepted integrated version.
- All OpenAI tool execution must use `ToolGateway.call`. Lifecycle logging
  callbacks do not replace permission checks. Keep secrets out of prompts and
  preserve existing sessions, recordings and budget ledgers. There is no v2
  Claude SDK execution path; historical playback remains supported.
- Private data lives under `runs/workspace`, `runs/checks` and `runs/archive`.
  Preserve `runs/layout.json` and the budget ledger together: relocation must
  not break old images or reset per-run spending. See `docs/run-data.md`.

## Historical note

A 31 October 2026 "fresh start" note used to stand here (Codex era, in qumus-v2). It is superseded by
the November architecture (separate brain, sim lab, lab and contract repositories); current context
and decisions are in `CLAUDE.md` and the project notes handoff.
