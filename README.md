# Qumus agent system

> **中文一句话：** 这是 Qumus 的“指挥中心”——AI 团队、网页界面、History 回放和预算都在这里。
> 它不再自己控制仪器，而是通过统一的“窗口规则”（contract v1）去指挥各个实验室模块。
> 不接真机时，它指挥本仓库里的“仿真实验室”，规则完全一样。

Qumus is an AI agent team that runs a 2D-materials lab for Sanfeng Wu's group at
Princeton: it exfoliates crystals onto chips, finds good flakes under the
microscope, designs a device and stacks it. This repository is the **agent side**.
It started on 8 November 2026 from the qumus-v2 simulation demo (commit 0cc7826)
and is being adapted to command real lab modules.

**Private lab code. Do not push it to any public place.** Never commit API keys,
`.env`, lab run data (images, databases), GDS files or model weights.

## Where it fits: three repositories, one rule

Think of the lab as a building with service windows. The agent never walks into a
room to touch an instrument; it goes to a window, asks for an action by name
("go to the exfoliation stage", "heat to 40 C for 10 minutes", "stop") and reads
the board that shows what the room is doing. Every window follows the same rules
(contract v1): one controller at a time, Stop works for anyone, and a move whose
outcome is unknown locks the window until a person checks it.

```text
                       qumus-agent  (this repository: AI team, web page, History, budget)
                            |   contract v1 (HTTP, loopback / lab network)
        +-------------------+--------------------+---------------------------+
        |                   |                    |                           |
  exfoliation :8781    stacking :8782       reactor services          sim lab (this repo)
  (qumus-lab, lab PC)  (qumus-lab, lab PC)  (labmate's dobot_mg400)   exfoliation + stacking,
                                                                       full simulation
```

| Repository | What it is |
| --- | --- |
| `qumus-v2` | the frozen demo; always runnable, never changed |
| `qumus-lab` | lab-PC software rewritten from v1: drivers, safety checks, module windows, debug page |
| `qumus-agent` (here) | the only commander: agents, page, History, budget, and the sim lab |
| reactor | labmate's services; joins as another window (gaps: [reactor-gaps.md](docs/reactor-gaps.md)) |

What changed compared with qumus-v2 and what comes next:
[adaptation plan](docs/adaptation-plan.md). How the pieces connect:
[architecture](docs/architecture.md). Choices made without the owner:
[open questions](docs/open-questions.md).

## Try it (free, no model calls, no hardware)

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,agents]"

# 1. The simulated lab as two windows; look at http://127.0.0.1:8782/api/state
.venv/bin/qumus2 simlab

# 2. Agents + web page, reaching the simulated lab only through the windows
.venv/bin/qumus2 serve --contract-lab

# 3. The original in-process demo (same as qumus-v2)
.venv/bin/qumus2 serve
```

`serve` uses scripted agents and costs nothing. `--openai` uses the OpenAI Agents
SDK through the project gateway and spends money only while the team works on a
message; see [runtime](docs/runtime.md). Everyday Mac startup:
[local start](docs/local-start.md).

## What is inside

| Part | Folder | Notes |
| --- | --- | --- |
| Agent team | `qumus2/agents/` | orchestrator, advisor, eyes, hands, picker, designer, reactor; prompts and skills |
| Tool checks | `qumus2/agents/gateway.py`, `policy.py` | every tool call is checked before it runs |
| Web page and History | `qumus2/ui/` | live page, recordings, replays |
| Lab windows client | `qumus2/contract/` | contract v1 client and lease keeper ([contract-client.md](docs/contract-client.md)) |
| Reactor window | `qumus2/reactor/` | adapter for the reactor services |
| Sim lab | `qumus2/simlab/` | the simulation served as two windows |
| Simulation | `qumus2/testbench/`, `devices/sim/`, `flows/`, `safety/`, `perception/` | the qumus-v2 simulated lab; used through `simlab` |

Recordings: the qumus-v2 recordings (305 MB) were not copied; they stay playable
in the frozen qumus-v2 repository. Tests that need one of them are skipped here
unless `QUMUS_RECORDINGS_DIR` points to a qumus-v2 `recordings/` folder.

## Checks

```sh
.venv/bin/pip install -e ".[dev,agents,vision]"   # vision: the v1 RGB recognition used by stacking
.venv/bin/ruff check .
.venv/bin/python -m pytest              # default run, skips tests marked slow
.venv/bin/python -m pytest -m slow      # long end-to-end runs (minutes each)
```

Without the `vision` extra, tests that stack a device are skipped or fail in
the same way as in qumus-v2.

GitHub is storage only (no Actions); run all checks locally. The full qumus-v2
README is kept as [docs/archive/qumus-v2-readme-0cc7826.md](docs/archive/qumus-v2-readme-0cc7826.md).
