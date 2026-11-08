# Adaptation plan: from the qumus-v2 demo to the Qumus agent system

> **中文摘要（给 Jialei）**
> qumus-agent 以 qumus-v2 0cc7826 为起点，但不是原样复制。保留：AI 团队（各 agent、提示词、技能、工具规则）、
> 网页界面、History 回放、预算与会话记录。要改的核心只有一处：以前 agent 直接在同一个程序里“摸”仿真仪器；
> 以后它只通过“窗口规则”（contract v1）去敲三个窗口：剥离模块、找片+堆叠模块（都在 qumus-lab）、reactor（同学的仓库）。
> 完整仿真（片子、显微镜图像、Hall bar 全流程）留在本仓库，包装成一个“仿真实验室”，它对外也用同样的窗口规则，
> 所以 agent 不接真机也能跑完整仿真。今晚完成：导入、本计划、仿真实验室的两个窗口（读状态/停止/事件/几个动作 + 测试）、
> agent 端经窗口提交任务的适配层、README 与架构说明重写。还没做的步骤见第 5 节。

Status: written 2026-11-08 (night job, thread A). Decisions marked **[default]** were made
without Jialei (asleep) and are listed again in [open-questions.md](open-questions.md).

## 1. Where we start

`main` begins with one commit, "Import qumus-v2 0cc7826 as starting point": the frozen sim
demo without its 305 MB of recordings. In qumus-v2 everything runs in one Python program:

```text
UI ─▶ agents ─▶ ToolGateway ─▶ JobService ─▶ Flow/Step ─▶ SafetyGate ─▶ sim devices (SimWorld)
                                    (optionally in a "lab process" behind a private RPC)
```

The new architecture (Jialei, 2026-11-08) has three repositories:

| Repository | Role | Talks through |
| --- | --- | --- |
| `qumus-v2` | frozen demo; never changed | nothing (stand-alone) |
| `qumus-lab` | lab PC software, rewritten from v1: drivers, SafetyGate, module actions, debug page | serves contract v1: `exfoliation` :8781, `stacking` :8782 |
| `qumus-agent` (this) | the only commander: AI team, UI, History, budget | contract v1 client to every module service |
| reactor (`dobot_mg400`, labmate) | reactor module services | serves contract v1 (ADR 0001), joins as window 3 |

## 2. What the agent system keeps

| Part | Files | Why it stays |
| --- | --- | --- |
| Agent team, prompts, skills | `agents/roster.py`, `agents/prompts/`, `agents/skills/` | the AI part Jialei is rewriting; independent of where devices live |
| Tool rules | `agents/gateway.py`, `agents/policy.py`, `agents/tooling.py` | every tool call is still checked before it runs |
| Model runtimes and budget | `agents/openai_runtime.py`, `agents/scripted.py`, `agents/openai_budget.py`, `ui/billing.py` | unchanged; scripted runtime keeps tests free |
| Projects, plans, conversation, records | `runtime/store.py`, `records/` | the agent's own memory of the experiment |
| Web UI and History | `ui/` | same page; lab panel gets its data from module state (step 3 below) |
| Recordings | `ui/recording.py`, `ui/replay.py` | same format; contract events are added as they arrive |
| Simulation | `testbench/`, `devices/sim/`, `flows/`, `safety/`, `perception/` | wrapped as the **sim lab** (section 4); not used by the agent directly any more |

## 3. What changes

1. **In-process lab → contract clients.** `app.build_services` no longer needs to own devices.
   A new lab mode (`serve --contract-lab`) gives the agent a `ContractJobService` (`qumus2/simlab/agent_link.py`)
   with the same methods as `JobService` (`submit`, `wait`, `status`, `cancel`, `stop_all`, ...).
   It turns `submit(flow, params)` into `POST /api/command {"action": flow, ...params}` on the
   module that owns the flow, keeps the control lease alive while a command runs, and reads the
   result. Stop goes to `POST /api/stop` on every module.
   The private RPC lab process (`runtime/labproc.py`, `rpc.py`, `remote.py`, `supervisor.py`)
   becomes legacy: kept while the sim demo still uses it, deleted once `contract` mode is the default.
2. **Agent tools → module actions.** The tools keep their names and checks; only the last line
   changes (`ctx.jobs.submit` now reaches a module action). Flow → module mapping
   (`simlab/modules.py: FLOW_MODULE`):
   exfoliation = `exfoliate_chips`;
   stacking = `place_chip_on_transfer_stage`, `search_chip`, `revisit_chip`, `stack_device`,
   `cool_experiment_stages`, `inspect_bench`;
   reactor flows stay in-process until the reactor window is connected (thread C, `qumus2/reactor/`).
3. **UI lab panel from `/api/state` + `/api/events`.** Each module's state (devices with units,
   busy, locked, fault, lease, last command) is what the page shows. The bird's-eye "world"
   picture exists only for the sim lab, labelled as simulation (lab-state design v2: the god view
   is never read by programs).
4. **Recordings from events.** Module events (`command accepted/started/done/failed`, `fault`,
   `lock`, `lease`) are appended to the run store as `module.event` records, so History replays
   them like every other event. **[default]** prefix `module.` so old recordings are unchanged.
5. **Reactor as third service.** Same client, own header (`X-MG400-Client`), own actions.
   Gaps (human-approval gateway vs no-approval rule, network, chip hand-over) are in thread C's
   `docs/reactor-gaps.md`.
6. **No real drivers here.** Real hardware code lives in qumus-lab. `devices/real/reactor.py`
   (old in-process reactor vendor adapters) is **retired for the real reactor**: the reactor
   services own those devices (thread C gap G15). It stays only until the sim no longer imports it.

## 4. The sim lab (default decision, Jialei may override)

**[default]** The full simulation stays in this repo and is served as a "sim lab" with the SAME
contract as qumus-lab: two services, `exfoliation` and `stacking`, over one shared simulated bench
(`qumus2/simlab/`). So:

- the agent can run a complete simulated Hall bar without hardware or qumus-lab;
- the agent code cannot tell sim from real except through `state.demo` (always `true` here);
- qumus-lab's own `--demo` is device-level only (positions, switches, temperatures) and is used
  for the lab debug page and L1-L2 scripts, not for whole experiments.

What the sim lab serves tonight (`qumus2/simlab/service.py`, `modules.py`, tests in
`tests/test_simlab_contract.py`):

| Endpoint | Behaviour |
| --- | --- |
| `GET /api/describe` | service, contract `"1"`, module, `demo: true`, `client_header`, devices (kind, fields + units), commands (params schema, `any_client`, `motion`, `local_only`) |
| `GET /api/state` | `seq`, `ts_unix`, `connected`, `busy`, `locked`, `fault`, `lease {holder, remaining_s}`, `last_command {id, action, client, result, message, finished_at}`, `devices` |
| `POST /api/command` | flat body `{"action", ...params}` → 202 `{command_id, state}`; 409 busy/locked/lease; 422 bad params; 403 `local_only` from non-loopback |
| `POST /api/stop` | any client, idempotent, `{wait_s}` → `{stop: confirmed/pending/unconfirmed, state}` |
| `POST /api/heartbeat` / `renew` / `release` | single 3 s control lease; expiry during motion stops it |
| `GET /api/events?since=` | `{events: [{seq, ts_unix, level, message, fields}], next}` |
| `POST /api/unlock` | loopback only; clears `locked` (never automatic) |

Actions: common `read_all`, `home_query`, `capture`, `go_to_point`, `heat`, `heat_off`,
`set_switch` (local only), `jog` (local only, 0.5 mm arm/stage, 0.1 mm stamp caps), plus the
module's flows as whole actions (section 3.2). Every device command still goes through the
existing SafetyGate. A motion command whose outcome is unknown (device timeout) sets `locked`.

## 5. Steps

| # | Step | State (2026-11-08 morning) |
| --- | --- | --- |
| 1 | Import qumus-v2 0cc7826 without recordings | done (`main` 84b85fd) |
| 2 | This plan | done |
| 3 | Sim lab services (contract server, two modules, tests) | done tonight |
| 4 | `ContractJobService` + `serve --contract-lab`: agent tools submit jobs through the contract | done tonight (tested: hands tool → stacking window → job done) |
| 5 | Generic contract client (thread C, `qumus2/contract/`) used by `simlab/agent_link.py` | done (this branch includes thread C's commit) |
| 6 | UI lab panel reads module `/api/state` + `/api/events` | next |
| 7 | Recordings store module events | next |
| 8 | Reactor window (thread C adapter) | gap list from thread C |
| 9 | Point the agent at qumus-lab services (real or `--demo`) for L1-L2 reads, then L4 actions after L3 | after qumus-lab L3 |
| 10 | Retire in-process lab process code (`labproc`, `rpc`, `remote`, `supervisor`) and `devices/real/` | when steps 4-9 are green |

## 6. Old file → new role

| Old (qumus-v2) | New role in qumus-agent |
| --- | --- |
| `app.py build_lab_side` | the sim lab's bench (`simlab/server.py`); legacy demo |
| `app.py build_services` | agent side; lab = in-process (legacy demo) or `contract` |
| `runtime/jobs.py` JobService | inside the sim lab only; the agent uses `ContractJobService` |
| `runtime/labproc.py`, `rpc.py`, `remote.py`, `supervisor.py` | legacy, removed at step 10 |
| `runtime/store.py` RunStore | agent records; in sim-lab mode shared with the sim lab (same SQLite file) |
| `runtime/twin.py` | sim lab only (bench drawing for the sim UI) |
| `safety/` | sim lab only; real SafetyGate lives in qumus-lab |
| `devices/base.py`, `devices/sim/` | sim lab only |
| `devices/real/reactor.py` | retired for the real reactor (gap G15); reactor via contract (`qumus2/reactor`) |
| `flows/` | sim lab only; on real hardware these become qumus-lab module actions |
| `testbench/` | sim lab only |
| `perception/` | sim lab now; real recognition is a qumus-lab module job (contract: actions + recognition per module) |
| `agents/tools/hardware.py` `submit()` | unchanged call, now reaches a module action |
| `ui/app.py` lab panel (`/api/snapshot` world, `/api/camera`) | from module state/events; world picture sim only |
| `ui/recording.py` | adds `module.*` events |
| `scripts/record_*`, `v1compare/` | kept for the sim demo history; not part of the real path |
