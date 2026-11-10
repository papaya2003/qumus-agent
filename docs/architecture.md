# Architecture: the brain and the stations

> **中文摘要：** qumus-agent 只做“指挥”（大脑）：想做什么、按什么顺序、出错怎么办。仪器怎么动、动得安不安全，由各个工作站自己管。
> 两边各有自己的数据库，只通过 HTTP（contract v1）说话。模拟实验室 qumus-simlab 和真实实验室走同一套接口，
> 在同一台电脑或不同电脑上都一样，只是 stations.yaml 里的地址不同。

## 1. Who does what

```text
 person ──▶ web page (qumus2/ui) ──▶ agent team (qumus2/agents)
                                         │ tool call, checked by ToolGateway + policy
                                         ▼
                 ctx.jobs = ContractJobService (qumus2/stations/jobs.py)      ctx.store = brain RunStore
                    │ put_records, job commands, job_* controls, stop             ▲
                    │ ContractClient + LeaseKeeper (qumus2/contract)              │ record mirror
                    ▼ HTTP, contract v1                                           │ (qumus2/stations/mirror.py)
        ┌───────────────────┬───────────────────┬──────────────────┐             │
   exfoliation          stacking             reactor        GET /api/records ─────┘
   (sim or real)        (sim or real)        (sim or real)  GET /api/images/<id>
```

- **Brain (this repo)** decides *what* to do: plans, flake choice, device design,
  recovery choices. It asks a station for a whole job by name (`search_chip`,
  `stack_device`, ...) and never sends raw device motion.
- **Stations** decide *how*: drivers, named points, SafetyGate limits, closed-loop
  steps, heater timeouts, and stop motion when the brain's lease lapses (3 s;
  the brain renews every second).

The brain never imports laboratory code; a test (`tests/test_stations.py`) checks it.

## 2. Two stores, one feed

| Owner | Records |
| --- | --- |
| Station (lab) | physical world, runs and steps of jobs, samples it creates or moves, reservations, lab events, pictures |
| Brain | projects, plans, instructions, experience, agent events, budget |
| Both | `sample`, `reservation`, project `state` that jobs read |

- **lab → brain**: `GET /api/records?since=<seq>` on the `records_from` station
  (default stacking). Each change is `{seq, table, key, row}`; `row` is the RunStore
  dict shape, `null` means deleted. Every page carries `lab_id`; a new `lab_id` (a
  fresh simulated lab) clears the mirrored samples/reservations and starts at 0.
  Lab events are re-emitted in the brain store with `lab_event_id`.
- **pictures**: a row refers to a picture as `"qumus-image:<image_id>"`. The mirror
  downloads `GET /api/images/<image_id>` into `<run folder>/images/` and writes the
  local path; pushing maps it back.
- **brain → lab**: command `put_records` (any client, not motion) with the brain's
  changed samples, all projects (`{id, name, state}`) and instructions, sent before
  every job command, retry, resume and `complete_experiment`.
- After a job command answers `run_id`, the brain waits until that run row arrives
  in the feed, so tools can read it at once. `wait_job` reads the mirrored run/step rows.

Exact wire shapes: `notes/agent-brain-20261110/brain-simlab-interface.md` (project notes).

## 3. stations.yaml

```yaml
simlab_dir: ../qumus-simlab      # started by `serve` when local sim stations do not answer
records_from: stacking
modules:
  exfoliation: {url: "http://127.0.0.1:8781/", kind: sim}
  stacking:    {url: "http://127.0.0.1:8782/", kind: sim}
  reactor:     {url: "http://127.0.0.1:8783/", kind: sim}
```

Same computer or another computer: only `url` changes (`http://192.168.x.y:8782/`
for a LAN station, or `http://127.0.0.1:18782/` for the local end of an SSH tunnel).
`kind: real` marks a real station (the page shows it; learning stays off until
recording provenance is verified). Extra keys stay with the module for its adapter
(e.g. the reactor console: `adapter: console, session: qumus`).

## 4. The page

The web page reads the laboratory through the `records_from` station:
`GET /api/sim/layout` (bench + regions), `/api/sim/bench_state` (devices, objects,
twin, stamp, faults), `/api/sim/preview/<camera>`, `/api/sim/faults` (simulated lab
only). The bench description is kept in the brain store (`station_bench`) so tools
and History work while the station is offline. `/api/windows` and `/api/stations`
show each station's URL, sim/real and whether it answers.

## 5. Tests

`qumus2/stations/fake_lab.py` serves three fake stations on loopback that share one
lab-side store and finish every job at once with an outcome the test chooses. The
`services` fixture is the brain on that fake Hall-bar laboratory. Tests that need
simulated physics live with qumus-simlab and the integration run.
