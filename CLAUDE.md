# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is `qumus-agent`, the Qumus brain (Python package still named `qumus2`). Use [AGENTS.md](AGENTS.md)
for the shared development rules and [docs/README.md](docs/README.md) for current documentation; read
[docs/architecture.md](docs/architecture.md) before changing code. This file does not enable a Claude SDK
runtime: the model runtime is the OpenAI Agents SDK.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,agents,vision]"   # vision: v1 RGB recognition
.venv/bin/ruff check .
.venv/bin/python -m pytest                                  # default: skips tests marked slow
.venv/bin/python -m pytest tests/test_stations.py           # one file
.venv/bin/python -m pytest tests/test_agents.py -k <name>   # one test
.venv/bin/python -m pytest -m slow                          # long end-to-end runs (minutes)

.venv/bin/qumus2 stations                                   # configured stations, online or not
.venv/bin/qumus2 serve --db runs/sim-lab/try/qumus.db --seed 3   # free: scripted agents, web UI on 8765
```

- `serve` reads `--stations PATH`, `$QUMUS_STATIONS`, a local `stations.yaml` (copy `stations.example.yaml`),
  else the packaged `qumus2/config/stations.yaml`. If a local sim station does not answer it starts
  `../qumus-simlab` itself (`--no-simlab` disables that). Headless: `POST /api/messages`, `GET /api/events?after=N`.
- Paid: `serve --openai --budget N`; the model gateway is reachable only from Jialei's Mac. Ask before any
  paid run. Override the model with `QUMUS_OPENAI_MODEL`; never edit, print or commit `.env`.
- Tests that need real simulated physics are auto-skipped (`NEEDS_SIMLAB` in `tests/conftest.py`); they belong
  to qumus-simlab or a brain + simlab integration run. Tests needing qumus-v2 recordings skip unless
  `QUMUS_RECORDINGS_DIR` points to a qumus-v2 `recordings/`. The `*_ui.py` tests run `*.mjs` with `node --test`
  and skip without node.
- Ruff enforces `PLW1514`: every `open`/`read_text`/`write_text` must pass `encoding="utf-8"` (Windows lab PC).

## Architecture (big picture)

```text
web page (qumus2/ui) -> ConversationDriver (agents/driver.py) -> AgentRuntime
   AgentRuntime = OpenAIRuntime (agents/openai_runtime.py, --openai) or ScriptedRuntime (agents/scripted.py,
                  free; replays a scenario from agents/scenarios/*.yaml)
   every tool call -> ToolGateway (agents/gateway.py: policy pre-check, execute, post log) -> agents/tools/*
   tools -> ctx.jobs = ContractJobService (stations/jobs.py) -> one link per module (stations/link.py)
            -> HTTP to the station (contract 2 via labclient, or contract v1 via qumus2/contract)
   station records -> record mirror (stations/mirror.py) -> brain RunStore (runtime/store.py)
```

- `qumus2/app.py:build_services()` wires one run: store, jobs, gateway, runtime, driver, lab view. Read it first.
- Agent team (`agents/roster.py`): `orchestrator` plus subagents `advisor`, `eyes`, `hands`, `picker`, `designer`,
  `reactor`. Names are stable identifiers used in tool permissions (`agents/policy.py`), events and the UI.
  Prompts in `agents/prompts/*.md`, skills in `agents/skills/`, per-agent models in `config/agents.yaml`.
- All tool execution goes through `ToolGateway.call` (or its pre/execute/post hooks from the SDK runtime);
  logging callbacks never replace the permission check.
- The brain asks stations for whole named jobs (`search_chip`, `stack_device`, ...), never raw motion. Safety,
  leases (3 s, renewed every second) and stop-on-lapse live in the station.
- Two stores, one feed: the station owns the physical world, runs, steps and pictures; the brain owns projects,
  plans, instructions, experience, agent events and budget. Lab -> brain via `GET /api/records?since=` and
  `GET /api/images/<id>` (pictures appear as `qumus-image:<id>`); brain -> lab via the `put_records` command
  before every job command. Job commands use contract 2 where the station offers it, but the feed, pictures,
  `put_records` and the `/api/sim/*` page views still use the v1 extension routes (see `stations/link.py` docstring).
- The reactor is driven in its own Lab Console vocabulary (`qumus2/reactor/`, `adapter: console` in
  stations.yaml), not translated into station jobs.
- The brain must never import lab code: `tests/test_stations.py` fails if `qumus_simlab`/lab packages are imported.
- Tests run the brain against `stations/fake_lab.py`: three fake stations on loopback that share one lab-side
  store and finish each job at once with an outcome the test chooses (the `services` fixture in `tests/conftest.py`).
- Run data (`runs/workspace`, `runs/checks`, `runs/archive`, `runs/layout.json`, budget ledger) is private; keep
  the layout file and ledger together when relocating (`docs/run-data.md`). Budget and gateway setup: `docs/runtime.md`.

Some older docs and the package description still say "contract v1" everywhere; the contract 2 client in
`stations/link.py` loads `labclient` from a sibling `../lab-contract` checkout or `$QUMUS_LAB_CONTRACT`.

## Qumus project map (shared by all Qumus repositories, 2026-11-12)

Owner: Jialei Chen (GitHub `papaya2003`), Sanfeng Wu lab, Princeton. Talk to Jialei in Chinese;
code, comments, commits and docs in English. Jialei has no software background: explain simply.

| Repository | Role | main (2026-11-12) |
| --- | --- | --- |
| `qumus-agent` | The brain: AI agents (OpenAI Agents SDK), web UI on 8765, History, budget, run store, contract client. Never contains laboratory code. | 1792a5e |
| `qumus-simlab` | Simulated Hall-bar lab as its own program: exfoliation 8781, stacking 8782, vdW reactor 8783. Never imports qumus-agent. | 80dfe3f |
| `qumus-lab` | Windows lab-PC software: drivers, SafetyGate, module services 8781/8782, debug page 8780 (never tunnel it). | 5f76aa1 |
| `lab-contract` | Contract 2 spec, schemas, `conformance`, `labkit` (stations) and `labclient` (the brain's only client). Private. | 917e41c |
| `qumus-v2` | Frozen phase-1 demo; main 0cc7826 must not change. | 0cc7826 |

- The brain talks to every station only over HTTP (contract 2; older services still speak v1 and are
  auto-detected). Same computer or another computer differs only by URL; no shared files or databases.
  `stations.yaml` in qumus-agent picks sim or real per module.
- Standing permission: code, commit, push branches, open PRs, merge to main, install deps, run tests in these
  repositories (not qumus-v2 main). Ask Jialei first before: driving real hardware; pushing anywhere else or
  changing repository visibility; deleting files outside the repositories and project notes; changing an
  already decided design decision; any paid model call (state the cap).
- Never commit keys, `.env`, lab data or photos, GDS files, target bundles, run databases or model weights.
- GitHub Actions has no minutes: run `ruff` and `pytest` locally and say so in the PR.
- Contract changes need Jialei's approval (2.x is additive only). Simulated operations must match Qumus v1
  exactly; when the sim cannot support a v1 operation, change the sim, not the flow.
- Full context, history, decisions and next steps live in the project notes
  (`notes/handoff-20261112/HANDOFF.md` and `CLAUDE.md` there), not in this repository.
