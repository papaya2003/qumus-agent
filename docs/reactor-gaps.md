# Reactor connection: what works and what is missing (2026-11-08)

## 中文摘要（给 Jialei）

**能连上的（今天）**
- qumus-agent 里有了通用的 contract v1 客户端（`qumus2/contract/`）和 reactor 适配器（`qumus2/reactor/`）。
  客户端能读 reactor 7 个服务的 describe / state / events，能在不拿控制权的情况下 stop，能拿/续/还控制权（lease），
  能发命令并等结果；没有回复时会报“结果未知”，绝不自动重发动作命令。
- 按 reactor 文档做了 7 个“假 reactor 服务”，适配器对它们的测试全部通过（19 个）；通用客户端对假服务器的测试也全部通过（38 个）。
- agent 目前**只允许**做不动、不加热的事：读所有状态、全部停止、热像仪 connect/shutter、显微镜拍照、
  vision 识别芯片/读标签/看托盘槽位/检查标定、读 orchestrator 的 recipe 和运行记录。其他命令在发出前就被拒绝。

**还没连上的**
- 所有会动或加热的操作（机械臂、三电机台、炉子/气体、显微镜对焦）、芯片装入/取出 reactor、完整反应流程。
- 今晚**没有**真正跑 reactor 的 `--demo` 服务：云端沙盒拒绝运行外部仓库的代码。测试已写好，早上一条命令即可跑（见文末）。

**最关键的 3 个差距（需要你和 reactor 负责人决定）**
1. **审批规则冲突**：reactor 规定 agent 只能“提议” recipe，必须由实验室里的人在网页上批准，而且运行时要有人在场（ADR 0008）；
   Qumus 规定每次运行不需要人工审批。→ 要么 reactor 那步接受人工批准，要么请 reactor 负责人为 Qumus 开一个无人值守的自动化通道（他们文档里叫 H-24）。
2. **网络到不了**：reactor 所有仪器服务只听本机（127.0.0.1），VPN（LarePass）代理只转发网页控制台和相机画面，不转发仪器接口。
   → 需要 reactor 负责人给一个 SSH 隧道账号，或者把 Qumus 的 reactor 连接程序放到 reactor 主机上运行。
3. **芯片交接没有定义**：Qumus 模拟里有第三条机械臂和共享存储位；真实 reactor 只有自己的 MG400 和托盘，
   双方没有共同的交接位置，也还没有示教好的站点（reactor 文档说站点示教和转移“推迟”）。→ 需要定一个交接位，两边各自示教。

其余差距（请求头、机械臂控制权方式、字段名、加热超时规则、样品编号、Linux 部署等）见下表，大部分我们这边已经处理或只需要小改动。

## How this was checked

- Source: the reactor repo's public docs at commit `c44d746` (2026-11-07): ADR 0001 (contract v1), 0003 (one host,
  ports), 0005 (furnace lifecycle), 0008 (agent approvals), `docs/development.md`, `furnace.md`, `microscope.md`,
  `thermal.md`, `vision.md`, `orchestrator.md`, `agent-gateway.md`, `olares.md`, `deploy.md`. Paths below are in
  that repo. The repo is AGPL-3.0; nothing was copied, the Qumus code is written from these docs.
- Offline tests (run tonight, all green): `tests/test_contract_client.py` (generic client vs `FakeService`,
  both wire dialects) and `tests/test_reactor_services.py` (adapter vs doc-faithful fakes in
  `qumus2/reactor/fakes.py`).
- Live test against the real reactor `--demo` services: `tests/test_reactor_demo_live.py`, **not run tonight**
  because the cloud sandbox refused to execute the reactor repo's code. It skips cleanly without
  `QUMUS_REACTOR_REPO`. See "Morning check" below.

## What connects today (through `ReactorLab`)

| Service (port) | Read describe/state/events | Stop (no lease) | Agent may send | Refused tonight |
|---|---|---|---|---|
| robot (8766) | yes | yes | nothing | `execute` (raw coordinates), `vacuum`, `grip`, `home`, `enable`, `start_sync`, `keyboard` (ui_only), `clear_alarm` |
| stage (8767, `/contract/`) | yes | yes | nothing | `move`, `hold`, `keepalive`, `release_hold`, `set_microstep` (raw pulses) |
| thermal (8768) | yes | yes (no-op) | `connect`, `shutter` | none |
| furnace (8769) | yes | yes = heat and gas off | nothing | `apply_setpoint`, `set_sequence`, `start_sequence`, gas commands, `connect*` |
| microscope (8770) | yes | yes | `snapshot` | `focus_move`, `objective_nudge` (raw steps), `select_objective`, `autofocus`, `home_focus`, recording |
| vision (8771) | yes | yes | `detect`, `read_labels`, `read_markers`, `observe_slots`, `check_calibration` | `calibrate_camera` (operator step) |
| orchestrator (8772) | yes, plus GET `api/recipes`, `api/runs` | yes (holds the active run) | nothing | starting/resuming/aborting runs (see G1) |
| agent gateway (8773, MCP) | not used | - | - | Qumus does not go through MCP (see G1) |

The allow-list is a Qumus-side default (`qumus2/reactor/adapter.py`, `REACTOR_SERVICES`), chosen tonight without
Jialei: "moves nothing, heats nothing, writes no calibration". Owner tools can turn the guard off
(`ReactorLab(enforce_policy=False)`), the agent cannot.

## Gap list

Who acts: **Q** = us (Qumus), **R** = reactor owner, **D** = decision for Jialei together with the reactor owner.

| # | Gap | Evidence (reactor repo) | Impact | Who | Proposed next step |
|---|---|---|---|---|---|
| G1 | **Approval model.** Agents may only read and `propose_run`; a person at the dashboard approves each run, must hold the dashboard lease and be present until H-24 approves unattended runs. The orchestrator doc also says "Agents must use rehearsal only". Qumus rule: no per-run human approval gate. | ADR 0008 table + "Approval policy (H-23)"; `docs/agent-gateway.md` "Approval policy"; `docs/orchestrator.md` "Commands" | The agent cannot start any reactor action that moves or heats, by the owner's rules. Calling the orchestrator's `POST api/runs` directly would bypass their approval, so Qumus does not do it. | D | Choose: (a) Qumus accepts a human approval for reactor steps only (agent proposes, person approves); (b) reactor owner adds an unattended automation policy for one named Qumus client (their H-24); (c) reactor stays manual, Qumus only reads. Default until decided: (c). |
| G2 | **Network path.** All instrument services bind 127.0.0.1; the VPN (LarePass, Olares at 100.64.0.1) is the boundary; the single-port proxy forwards only the Lab Console `/lab/` and camera streams, "does not forward the robot, stage, thermal, furnace, microscope, or vision services". | `docs/olares.md` "Network" and "Single-port proxy"; ADR 0003 | Neither the Mac nor the Qumus lab PC can reach ports 8766-8771, even on the VPN. | R + D | Options: (a) SSH account on Olares and one tunnel per port (`ReactorLab(endpoints=...)` already supports remapped ports); (b) run the Qumus reactor bridge on the Olares host; (c) owner adds authenticated proxy routes for `*/api/*`. Also: the lab PC needs the LarePass client (check with lab IT). |
| G3 | **Client header and auth.** Reactor services read `X-MG400-Client`; the robot also requires `X-MG400-Panel: 1`; no token (VPN boundary); the MCP gateway needs a bearer token. Qumus modules use `X-Qumus-Client`. | ADR 0001; `docs/development.md` "HTTP interface" | Wrong header -> 400/403. | Q (done) | Adapter sends the right headers per service. Optional for R: add `client_header` to describe so clients need no table. |
| G4 | **Robot control model differs.** `start_sync` (a command) takes control; `api/heartbeat` only renews; no `api/renew`/`api/release`; `api/command` answers 200 with the bare state and no `command_id`. | `docs/development.md` "Robot service interface", "Contract discovery, stop, and events" | Our `LeaseKeeper` cannot take the robot lease; completion is matched by action + client + time instead of an id. | Q (partly done) + R | Client already waits without an id. A robot session (start_sync/stop_sync) will be added only after G1 allows robot motion. Ask R whether the robot will move to the labkit routes (`renew`, `release`, 202 + `command_id`) like the other services. |
| G5 | **Wire names.** ADR 0001: flat command body `{"action", ...params}`, `commands` map, `last_command.id/result`, `lease.remaining_s`, `events {events, next}`, integer `contract: 1`. The first Qumus spec draft used `actions`, nested `params`, `command_id/status`, `expires_unix`, string `"1"`. | ADR 0001 table; `notes/night-20261108/contract-v1-qumus.md` | Mismatched names silently break parsing. | Q (done) | Lead adopted ADR 0001 names for qumus-lab. The client accepts both spellings (`qumus2/contract/models.py`), so nothing breaks while qumus-lab catches up. |
| G6 | **Qumus contract extras missing on the reactor side.** No `motion` / `local_only` flags in describe; no `api/unlock` (unlock is per service: furnace `api/furnace/acknowledge`, stage needs `mg400ctl restart stage`, robot `clear_alarm`). | ADR 0001; `docs/furnace.md` "Client loss, recovery"; `docs/development.md` stage "dedicated stop interface" | The agent cannot tell motion commands from reads by describe alone; unlock is not uniform. | Q (done) / R (optional) | Adapter keeps a per-service table instead of the flags. Unlock stays a human step on both sides (same rule), so no code needed. Optional R: add `motion` to describe. |
| G7 | **Heater rule.** Qumus: every heat command has a mandatory `timeout_s`, heater orphan timeout 10 min. Reactor furnace: `apply_setpoint {target_c, ramp_c_per_min}` has no timeout; an open setpoint without a client gets a 15 min orphan grace; a bounded sequence continues after client loss. | `docs/furnace.md` "Client loss, recovery, and shutdown"; ADR 0005 | Qumus' "heat always ends by itself" guarantee does not hold for an open furnace setpoint. | D | Either accept the furnace's own lifecycle (sequences are bounded; 15 min orphan grace), or Qumus only ever uses bounded `start_sequence`. Recommended: sequences only. |
| G8 | **Actions offered vs what the Qumus reactor flow needs.** Mapping below. Load/unload exist only as orchestrator recipe skills (`crucible.load`, `crucible.unload`), and station teaching + transfers are "postponed work ... refuse on the host". The recipe registry has no furnace setpoint/sequence/gas skills. | `docs/orchestrator.md` "Skills", "Teaching stations", "Crucible load and unload"; `docs/agent-gateway.md` "Approval policy" | No end-to-end reaction can run from Qumus yet, even with approval. | R (stations, furnace skills) + Q (mapping) | See the action table below. |
| G9 | **Chip hand-over and the third arm.** Qumus sim: Arm3 (`arm_reactor`) with a shared Arm2/Arm3 storage slot. Real reactor: one MG400 with its own tray watched by vision cameras; no station is defined where a Qumus chip enters the reactor tray. | Qumus `docs/reactor-model.md`; reactor `docs/vision.md` "Vision-guided tray transfer", `docs/orchestrator.md` "Station transfers" | No physical path from the stacking module to the reactor. | D | Pick one hand-over place (e.g. one reactor tray slot reachable by the Qumus right arm, or a person carries the chip at first). Both sides teach a named station there. |
| G10 | **Coordinate frames.** Qumus world frame = table front-left corner, per-arm frames. Reactor robot uses controller flange coordinates (`User=0, Tool=0`), taught stations, and vision maps tray pixels to robot XY with its own calibration. | `docs/orchestrator.md` "Teaching stations and tool offsets"; `docs/vision.md` calibration | No shared frame. | Q + R | Not needed if both sides only use **named stations** (Qumus rule: actions, not coordinates). Do not build a shared frame unless a later step needs it. |
| G11 | **Sample identity.** Qumus: sample rows in `qumus.db` (chip/flake ids). Reactor: "Chip identity comes from its slot and run record"; every sample has a `chip_id`; chip passport pages; QR / Micro QR readable on the wafer side. | `docs/vision.md` intro; `docs/orchestrator.md` "Run records"; `docs/dashboard.md` "Chip passport" | The same chip would get two unrelated ids. | Q + R | Proposal: Qumus passes its chip id as the recipe `chip_id` parameter; Qumus stores the reactor run id on its sample (like the existing `reactor_evidence_job_id`). If chips carry QR labels, both sides read the same label. |
| G12 | **Units.** Qumus sim gas flow in sccm; reactor gas is "percent of that controller's full-scale flow". Stage position is a pulse counter with no encoder; Qumus `position_reactor_slot` uses absolute mm. | `docs/furnace.md` "Configuration"; Qumus `docs/reactor-hardware.md` "Source review" | Wrong units/positions if mapped naively. | R (full-scale per gas, stage calibration) + Q (convert) | Get full-scale sccm per channel and stage mm/pulse + reference from the owner; until then no mapping. |
| G13 | **Deployment.** Reactor: Linux + systemd on one Olares host. Qumus: agent on Mac, lab modules on a Windows PC. | `docs/deploy.md` "Prerequisites"; ADR 0003 | Not a blocker: our client is standard-library Python and talks HTTP; reactor code never runs on our machines. Only the optional live demo test runs their code locally. | - | none |
| G14 | **Events restart.** Reactor services keep the last 1000 events and renumber from 0 after a restart. | `docs/development.md` "Contract discovery, stop, and events" | A stored cursor would silently miss events. | Q (done) | `EventPage.restarted` is set when `next < since`; the UI/recorder must re-read from 0. |
| G15 | **Old in-process reactor adapters in Qumus.** `qumus2/devices/real/reactor.py` wraps the vendor repos (Stage-Motor-Controller, FurnaceCode) directly in the Qumus process. The reactor now owns those devices through its services ("each device has 1 service owner"). | Qumus `docs/reactor-hardware.md`; reactor ADR 0003 | Two owners of one device would fight. | Q | Do not use `devices/real/reactor.py` on the real reactor; the agent goes through `ReactorLab`. Thread A's adaptation plan should mark it retired. |
| G16 | **Live demo not exercised.** The real `--demo` services were not started tonight. | this run | Field-level surprises (e.g. exact describe content) not yet seen. | Q (Jialei runs one command) | Morning check below. |

### Qumus reactor tools vs reactor services (G8 detail)

| Qumus agent tool (sim today) | What it does in the sim | Reactor equivalent | Status |
|---|---|---|---|
| `verify_reactor_state` | read temperature, gas, targets, stage positions | GET `api/state` of furnace, stage, thermal | **works** (read only) |
| `shutdown_reactor` | turn heat/flow targets off | furnace `POST api/stop` (heat and gas off; confirmed only when verified) | **works** (stop needs no lease) |
| `observe_reactor` | fresh image, optional autofocus | microscope `snapshot`; image via `GET api/camera/microscope/still.png`; `autofocus` | snapshot **works**; autofocus refused (motion) |
| `load_reactor_chip` | arm loads carrier, insert rod advances, focus | orchestrator recipe skill `crucible.load` (robot + stage + furnace) | **missing**: stations not taught on host (G8), needs approval (G1), hand-over (G9) |
| `unload_reactor_chip` | wait for cool, retract, store | orchestrator skill `crucible.unload` (+ thermal cold-region check) | **missing**: same as load |
| `position_reactor_slot` | absolute mm XY of the assembly | stage `move` in raw pulses only, no encoder | **missing**: units/calibration (G12), raw moves refused by both policies |
| `reactor_growth`, `reactor_reaction`, `reactor_material_metal` | heat + gas profile, bounded window, observe | furnace `start_sequence` (+ gas %), microscope recording | **missing**: no furnace recipe skill (G8), approval (G1), heater rule (G7), units (G12) |
| `inspect_reactor_product` | carry product to the ordinary microscope | robot transfer to a hand-over station, then Qumus stacking microscope | **missing**: hand-over (G9) |
| Qumus contract `capture` | camera image id | microscope `snapshot`; vision `detect` for tray cameras | **works** |
| Qumus contract `read_all` | all device values | GET `api/state` per service (`ReactorLab.discover()`) | **works** |
| Qumus contract `stop` | stop everything | `ReactorLab.stop_all()` | **works** |
| Qumus contract `go_to_point {point}` | named point | robot stations (`robot.transfer from/to`) via recipes only | **missing** (G1, G8) |
| Qumus contract `set_switch` | vacuum etc. | robot `vacuum {operation}` | refused (G1) |

## Questions for the reactor owner (copy-paste ready)

1. Will you allow one named automation client (Qumus) to start catalog recipes without a per-run dashboard
   approval, once H-24 is decided? If not, is "agent proposes, person approves" the long-term path?
2. Can we get SSH access to Olares for loopback tunnels to 8766-8772, or would you rather we run a small Qumus
   bridge on Olares?
3. Will the robot service adopt the labkit routes (`api/renew`, `api/release`, 202 + `command_id`)?
4. When will station teaching and `crucible.load/unload` be enabled on the host, and can we agree on one tray
   slot as the Qumus hand-over station?
5. Full-scale sccm for each MFC channel, and the stage's mm-per-pulse and reference procedure.
6. Is a recipe skill for bounded furnace sequences (with gas) planned?

## Morning check (one command, about 2 minutes, no hardware, no paid calls)

On the Mac (needs `uv` and Python 3.12+; the reactor services are written for Linux, demo mode is expected to
work elsewhere but is unverified; a service that fails to start is skipped with its log path):

```sh
git clone https://github.com/shc0507/dobot_mg400 ~/src/dobot_mg400
cd ~/src/dobot_mg400 && uv sync
cd <qumus-agent checkout>
QUMUS_REACTOR_REPO=~/src/dobot_mg400 QUMUS_REACTOR_REPORT=/tmp/reactor-caps.json \
  .venv/bin/python -m pytest tests/test_reactor_demo_live.py -v
```

It starts thermal, furnace, microscope, vision, stage and orchestrator in `--demo` on free loopback ports,
reads describe/state/events, stops each service, runs thermal `connect`, microscope `snapshot`, vision `detect`,
checks that heat and raw motion are refused before sending, reads the recipe list, and writes the capability
table to `/tmp/reactor-caps.json`. The robot needs its separate controller simulator and is not included.
