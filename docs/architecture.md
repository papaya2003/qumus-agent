# Architecture: the agent and the lab windows

> **中文摘要：** agent 只做“指挥”：想做什么、按什么顺序、出错怎么办。仪器怎么动、动得安不安全，由各模块自己管。
> 两者之间只有一种对话方式：contract v1 窗口规则（读说明、读状态、下命令、停止、租约、事件）。
> 仿真实验室也走同一个窗口，所以从仿真换到真机，agent 这边不用改。

This page is the short map. The detailed agent and simulation contracts inherited
from qumus-v2 (tools, jobs, steps, events, UI) are in [sim-contracts.md](sim-contracts.md).

## 1. Who does what

```text
 person ──▶ web page (qumus2/ui) ──▶ agent team (qumus2/agents)
                                         │ tool call, checked by ToolGateway + policy
                                         ▼
                              JobService interface (submit / wait / stop ...)
                    ┌────────────────────┴────────────────────┐
         in-process testbench (legacy demo)        ContractJobService (qumus2/simlab/agent_link.py)
                                                          │ ContractClient + LeaseKeeper (qumus2/contract)
                                                          ▼ HTTP, contract v1
                              ┌────────────────┬──────────┴───────┬─────────────────┐
                        exfoliation       stacking            reactor services   (more windows)
                     (qumus-lab or sim) (qumus-lab or sim)   (qumus2/reactor)
```

- **Agent side (this repo)** decides *what* to do: plans, flake choice, device design,
  recovery choices. It never sends raw coordinates; it asks a module for a named
  action (`go_to_point {point}`, `heat {device, target_c, timeout_s}`, a whole job).
- **Module side** decides *how*: drivers, named points from config, SafetyGate limits,
  closed-loop steps, heater timeouts. On real hardware that is qumus-lab on the lab PC;
  for simulation it is `qumus2/simlab` over the qumus-v2 testbench.

## 2. The window rules (contract v1)

Shared spec: project notes `night-20261108/contract-v1-qumus.md` (wire format of the
reactor's ADR 0001 plus Qumus rules). Every window offers:

| Call | Meaning |
| --- | --- |
| `GET /api/describe` | who am I, which devices (fields + units), which commands (`any_client`, `motion`, `local_only`) |
| `GET /api/state` | busy, locked, fault, lease, last command, device values |
| `POST /api/command` | `{"action": name, ...params}` → 202; motion needs the lease |
| `POST /api/stop` | anyone, no lease, safe to repeat |
| `POST /api/heartbeat` / `renew` / `release` | one controller at a time; 3 s lease; lease lost during motion = stop |
| `GET /api/events?since=` | ordered history of what the window did |
| `POST /api/unlock` | a person at the lab computer clears `locked` (Qumus rule) |

Rules the agent relies on: a motion command with no answer locks the window and is
never retried automatically; heaters always carry a timeout; `local_only` actions
(jog, raw switches) exist only for the debug page and probe scripts on the lab PC;
there is no human-approval gate per run.

## 3. The sim lab

`qumus2 simlab` (or `serve --contract-lab`) builds the qumus-v2 simulated bench once and
serves it as two windows, `exfoliation` (:8781) and `stacking` (:8782). Each window lists
only its own devices; the simulated table has one overview camera, listed by both.
Besides the basic actions, each window offers its module's whole jobs as actions
(`exfoliate_chips`; `place_chip_on_transfer_stage`, `search_chip`, `revisit_chip`,
`stack_device`, `cool_experiment_stages`, `inspect_bench`) and job controls
(`job_cancel`, `job_retry`, `job_resume`, ...). On real hardware these jobs become
qumus-lab level-4 actions after the L3 measurements.

Sim-only shortcut: the agent and the sim lab share one run-store file, so the agent
reads job progress, samples and images from it. With qumus-lab the agent will read the
same facts from module state and events instead ([adaptation plan](adaptation-plan.md), step 6).

## 4. Where things are

| Path | Role |
| --- | --- |
| `qumus2/agents/` | team, prompts, skills, tools, ToolGateway, model runtimes, budget |
| `qumus2/ui/` | live page, History, recordings |
| `qumus2/runtime/store.py` | projects, plans, runs, events (SQLite) |
| `qumus2/contract/` | contract v1 client ([contract-client.md](contract-client.md)) |
| `qumus2/reactor/` | reactor window adapter ([reactor-gaps.md](reactor-gaps.md)) |
| `qumus2/simlab/` | sim lab windows and the agent's `ContractJobService` |
| `qumus2/testbench/`, `devices/sim/`, `flows/`, `safety/`, `perception/` | the simulation behind the sim lab |
| `qumus2/runtime/labproc.py`, `rpc.py`, `remote.py`, `supervisor.py` | legacy private lab process; retired when the windows cover it |
| `qumus2/devices/real/` | legacy in-process reactor adapters; retired, the reactor services own those devices |
