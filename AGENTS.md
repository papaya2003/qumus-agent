# Qumus development instructions

- Private lab code. Do not publish source or lab data to public locations.
- All project code, comments, commit messages, UI text, documentation,
  launcher messages and new filenames are in English.
- Read `docs/architecture.md` (repositories and the lab contract), `docs/sim-contracts.md` (agent and simulation contracts) and `docs/workstreams.md` before changing code.
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
- Keep `v1compare/v1/` byte-identical to its manifest. The legacy agent's
  LiteLLM and original specialist dependencies belong in `.venv-v1compare`:
  `pip install -e ".[dev,v1compare,v1agents]"`, without the `agents` extra.
  Run `.venv-v1compare/bin/python -m pytest tests/test_v1compare.py` there. The modern environment can use
  `.[dev,agents,v1compare]` for the SDK and offline flow comparisons.
- All OpenAI tool execution must use `ToolGateway.call`. Lifecycle logging
  callbacks do not replace permission checks. Keep secrets out of prompts and
  preserve existing sessions, recordings and budget ledgers. There is no v2
  Claude SDK execution path; historical playback remains supported.
- Private data lives under `runs/workspace`, `runs/checks` and `runs/archive`.
  Preserve `runs/layout.json` and the budget ledger together: relocation must
  not break old images or reset per-run spending. See `docs/run-data.md`.

## Owner-directed fresh start — 31 October 2026

The previous optimization round is rejected. Its handoff memory is cleared.
Do not adopt or resume its plans, assignments, issue backlog, acceptance claims
or model campaigns from historical notes or chats. Collect requirements afresh
in `../notes/planning/restart-20261031.md`. Existing architecture describes code,
not the new round's approved scope. Preserve code and raw experimental/financial
evidence unless the owner separately requests changes.
