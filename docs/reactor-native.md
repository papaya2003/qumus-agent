# The reactor in its own words (decided 2026-11-10)

Jialei decided on 2026-11-10 that there is no translator between the brain and the vdW reactor.

- The brain speaks the reactor's own Lab Console API: reactor ADR 0010, `docs/agent-cli.en.md` and `docs/orchestrator.en.md` in the reactor repo.
- The simulated reactor (qumus-simlab, reactor window 8783, under `/lab/`) imitates that API, so the sim and the real reactor stay symmetric.
- For now a person hands chips over.

The full spec is in the project notes: `notes/vdw-reactor-20261110/reactor-native-spec.md`.

## 中文摘要

- 大脑直接用 reactor 自己的“Lab Console”语言，模拟 reactor 也说这种语言，所以模拟和真机是对称的。
- 九个新动作取代了旧的 13 个 reactor 整段动作：
  - `reactor_status`
  - `reactor_handover_in` / `reactor_handover_out`（人工交接）
  - `reactor_load` / `reactor_unload`（托盘 ↔ 坩埚）
  - `reactor_heat`（温度程序和气体 %）
  - `reactor_snapshot` / `reactor_record`
  - `reactor_stop`
- 真 reactor 默认只读：可以读状态、拍照、停止。装片、取片和加热会先被拦下，要等 Jialei 在 stations.yaml 里写 `allow_actuation: true`。
- 模拟全程一键跑：`python -m qumus2.reactor.roundtrip --url http://127.0.0.1:8783/lab/`。

## Where the brain connects

The reactor entry in `stations.yaml` carries `adapter: console`:

```yaml
reactor: {url: "http://127.0.0.1:8783/lab/", kind: sim, adapter: console, session: qumus}
# real, through scripts/reactor_tunnel.sh:
# reactor: {url: "http://127.0.0.1:18772/lab/", kind: real, adapter: console, session: qumus,
#           microscope_url: "http://127.0.0.1:18770/", allow_actuation: false}
```

What this entry changes:

- A console station has no record feed and no contract jobs. `records_from` must name a contract station.
- `qumus2/app.py` builds a `ReactorStation` for it and hands it to the tools through the gateway (`ctx.extra["stations"]`).
- The brain's stop also stops it.
- The opt-in file `qumus2/config/stations.reactor-console.yaml` has this entry for the sim. The packaged default is unchanged until the Hall-bar paid run is done.

## Tools (`qumus2/reactor/tools.py`)

| Tool | Reactor call (through `qumus2/reactor/console.py`) |
|---|---|
| `reactor_status` | console state and the state of each device |
| `reactor_handover_in` / `_out` | sim: `POST sim/handover`. Real: asks a person, returns `pending`, and is confirmed after the person writes back. In both, recipe `slot-state` checks the slot when motion is allowed |
| `reactor_load` | recipe `crucible-load {from_slot}` |
| `reactor_heat` | furnace `start_sequence {steps, end_gas, end_gas_until_c}`, optional `microscope-record`, then waits until the program ends |
| `reactor_snapshot` | microscope `snapshot` and the frame (real: `microscope_url`) |
| `reactor_record` | recipe `microscope-record` |
| `reactor_unload` | recipe `crucible-unload {to_slot}` (waits until the crucible is cold) |
| `reactor_stop` | console motion stop, the robot/stage/microscope stops, and the furnace stop |

How every call is handled:

- Each call is one run in the brain's store (flow `reactor_<tool>`).
- It is sent once and never retried.
- The chip's place is kept in the sample ledger: `reactor:tray:<slot>`, `reactor:crucible`, `person`, or `reactor:unknown` after an unclear outcome.
- Recipe runs follow the reactor's own protocol:
  - The session takes the recipe's devices and holds the orchestrator lease.
  - It sends `POST api/runs {recipe, params, borrow: true}` and polls `api/state`.
  - A held run is aborted. The orchestrator lease is released at the end.

## Retired

These were removed:

- the sim-era reactor job tools and flows (`load_reactor_chip`, `reactor_growth`, `reactor_reaction`, `reactor_material_metal`, `verify_reactor_state`, `observe_reactor`, `position_reactor_slot`, `shutdown_reactor`, `unload_reactor_chip`, `hold_reactor_observation`, `inspect_reactor_product`, `assess_reactor_inputs`) and their recovery and result tools
- the material-metal requirement tools
- the reactor objective in `delegate_reactor`

The `qumus2/records/material_metal*` and `reactor_requirements` modules remain as unused library code for a later cleanup.

## Check

- `python -m qumus2.reactor.roundtrip --url http://127.0.0.1:8783/lab/ [--record]` (simulated reactor only: it refuses a console without the sim-only `sim/world` route).
- `tests/test_reactor_console.py` and `tests/test_reactor_tools.py` run offline against `qumus2/reactor/console_fake.py`.
