# Qumus agent system (the brain)

**Qumus v2.2 · stage 2 · brain.** Works with [qumus-simlab](https://github.com/papaya2003/qumus-simlab) (simulated lab), [qumus-lab](https://github.com/papaya2003/qumus-lab) (real-lab drivers) and [lab-contract](https://github.com/papaya2003/lab-contract) (station contract); keep the four checkouts side by side. Stage 1 (v2.1) is the frozen demo [Qumus-v2](https://github.com/papaya2003/Qumus-v2), tag `v2.1`.

> **中文一句话：** 这是 Qumus 的“大脑”——AI 团队、网页界面、History 回放和预算都在这里。
> 它不包含任何实验室代码，只通过统一的窗口规则（contract v1，HTTP）去指挥各个实验室模块。
> 默认连接模拟实验室（独立程序 qumus-simlab）；以后把 stations.yaml 里某个模块的地址换成真实实验室即可。

Qumus is an AI agent team that runs a 2D-materials lab for Sanfeng Wu's group at
Princeton: it exfoliates crystals onto chips, finds good flakes under the
microscope, designs a device and stacks it. This repository is the **brain**:
agents, web page, History, budget, its own run store and the contract client.
It started on 8 November 2026 from the qumus-v2 demo (commit 0cc7826); on
10 November the simulated laboratory moved out into `qumus-simlab`.

Never commit API keys, `.env`, lab run data (images, databases), GDS files or model weights.

## Where it fits: one brain, several stations

```text
                 qumus-agent  (this repository: AI team, web page, History, budget, run store)
                      |   contract v1 over HTTP (same computer or another computer)
        +-------------+---------------+---------------------------+
        |                             |                           |
  exfoliation                    stacking                       reactor
  sim: qumus-simlab :8781        sim: qumus-simlab :8782        sim: qumus-simlab :8783
  real: qumus-lab (lab PC)       real: qumus-lab (lab PC)       real: reactor console (SSH tunnel)
```

| Repository | What it is |
| --- | --- |
| `qumus-agent` (here) | the brain; never drives an instrument, never imports laboratory code |
| `qumus-simlab` | the simulated Hall-bar laboratory as its own program: three windows 8781/8782/8783 |
| `qumus-lab` | lab-PC software: drivers, safety checks, module windows, debug page |
| reactor | labmate's services; reached through its console ([reactor-gaps.md](docs/reactor-gaps.md)) |
| `qumus-v2` | the frozen demo |

Which station serves which module is set in `stations.yaml` (copy
[stations.example.yaml](stations.example.yaml)); see [architecture](docs/architecture.md).

## Try it (free, no model calls, no hardware)

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,agents,vision]"
# qumus-simlab checked out next to this folder (../qumus-simlab), with its own setup

.venv/bin/qumus2 stations                                   # which stations, online or not
.venv/bin/qumus2 serve --db runs/sim-lab/try/qumus.db --seed 3
```

`serve` reads `stations.yaml` (or `--stations PATH`, or `$QUMUS_STATIONS`, else the
packaged default: all three modules simulated on 127.0.0.1). When a simulated
station on this computer does not answer, it starts `qumus-simlab` from
`simlab_dir` (`python -m qumus_simlab serve --seed <seed> --db <run folder>/simlab/lab-<time>.db`)
and stops it again on exit; `--no-simlab` never starts one. A station on another
computer is reached the same way, only its URL differs (LAN address, or a local
SSH-tunnel port).

`serve` uses scripted agents (scenario `hallbar_device`) and costs nothing.
`--openai --budget 15` uses the OpenAI Agents SDK through the project gateway and
spends money only while the team works on a message; see [runtime](docs/runtime.md).
Headless: `POST /api/messages {"text": ...}`, `GET /api/health`, `GET /api/events?after=N`.

## What is inside

| Part | Folder | Notes |
| --- | --- | --- |
| Agent team | `qumus2/agents/` | orchestrator, advisor, eyes, hands, picker, designer, reactor; prompts, skills, scripted scenarios |
| Tool checks | `qumus2/agents/gateway.py`, `policy.py` | every tool call is checked before it runs |
| Records | `qumus2/records/`, `qumus2/runtime/store.py` | the brain's own run store: projects, plans, experience, mirrored lab records |
| Stations | `qumus2/stations/` | stations.yaml, job service, record mirror, page view, sim-lab launcher, fake lab for tests |
| Contract client | `qumus2/contract/` | contract v1 client and lease keeper ([contract-client.md](docs/contract-client.md)) |
| Reactor | `qumus2/reactor/` | adapter and console client for the real reactor |
| Web page and History | `qumus2/ui/` | live page, recordings, replays |

Recordings: the qumus-v2 recordings (305 MB) were not copied; they stay playable
in the frozen qumus-v2 repository. Tests that need one of them are skipped here
unless `QUMUS_RECORDINGS_DIR` points to a qumus-v2 `recordings/` folder.

## Checks

```sh
.venv/bin/pip install -e ".[dev,agents,vision]"   # vision: the v1 RGB recognition used by stacking
.venv/bin/ruff check .
.venv/bin/python -m pytest              # default run, skips tests marked slow
```

Tests run the brain against a fake laboratory on loopback (`qumus2/stations/fake_lab.py`).
Tests that need simulated physics are skipped here (list in `tests/conftest.py`);
they belong to qumus-simlab and the brain + qumus-simlab integration run.

GitHub is storage only (no Actions); run all checks locally.
