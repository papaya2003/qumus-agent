# Contract v1 client (`qumus2.contract`)

> **Contract 2.** Stations that offer contract 2 (papaya2003/lab-contract `spec/contract-2.md`;
> qumus-simlab does) are commanded through lab-contract's `labclient` instead: see
> [Contract 2](#contract-2-stationslinkpy) below. This v1 client stays for v1-only stations and
> for the record feed, pictures, `put_records` and the sim pages until the contract 2 records
> profile is in.

The agent talks to every lab window only through instrument service contract v1:
the qumus-lab modules (`exfoliation`, `stacking`), the simulated lab (`qumus2/simlab`)
and the reactor services (`qumus2/reactor`, see [reactor-gaps.md](reactor-gaps.md)).
Standard library only; works on macOS, Windows and Linux.

```python
from qumus2.contract import ContractClient, OutcomeUnknown

stacking = ContractClient("http://127.0.0.1:8782/", "qumus-agent")
print(stacking.describe().commands)          # what the module offers
print(stacking.state().devices)              # current device values
with stacking.lease_keeper():                # take and keep the 3 s control lease
    stacking.run("go_to_point", {"point": "middle_board"}, timeout_s=60)
stacking.stop()                              # any client, no lease, safe to repeat
```

## Calls

| Method | Route | Notes |
|---|---|---|
| `describe()` | GET `api/describe` | cached; refuses a service whose `contract` is not 1 |
| `state()` | GET `api/state` | `ServiceState`: busy, locked, fault, lease, last_command, devices |
| `events(since)` | GET `api/events?since=` | `EventPage(events, next, restarted)`; `restarted` means the service renumbered |
| `heartbeat()` / `renew()` / `release()` | POST `api/heartbeat` / `api/renew` / `api/release` | `renew` never re-acquires |
| `lease_keeper()` | - | thread that renews every third of the lease (max 1 s); on failure sets `lost`, calls `on_lost`, never re-acquires |
| `command(action, params)` | POST `api/command` | sent once; returns the 202 receipt, not completion |
| `wait_for_command(receipt)` | polls `api/state` | until `last_command` for this command is final; does not stop on timeout |
| `run(action, params)` | both | raises `CommandFailed` unless the result is `ok` |
| `stop(wait_s)` | POST `api/stop` | retried on transport errors (idempotent) |

There is deliberately no `unlock`: clearing `locked` is a person's step at the lab PC.

## Wire format

ADR 0001 names are primary (agreed 11-08): `describe.commands {name: {params, any_client, motion?, local_only?}}`,
flat command body `{"action": name, ...params}`, `last_command {id, action, client, result, message, finished_at}`,
`lease {holder, remaining_s}`, events `{events: [{seq, ts_unix, level, message, fields}], next}`.
The client also reads the first Qumus spec draft's spellings (`actions`, nested `params`, `command_id`, `status`,
`expires_unix`, string `"1"`) so the lab modules and the agent can be updated independently.

Client id header: `describe.client_header` if the service names one, else `X-Qumus-Client`. Reactor services use
`X-MG400-Client` (the reactor adapter sets it).

## Errors

| Situation | Exception |
|---|---|
| 400, 413, 415, 422; unknown action (checked before sending) | `BadRequest` |
| 403; `local_only` action from a non-loopback client (checked before sending) | `Forbidden` |
| 404 (e.g. `api/renew` missing) | `NotFound` |
| 409 busy, locked, lease missing or held by another client | `Conflict` |
| 503 device link lost | `DeviceUnavailable` |
| other 5xx | `ServerError` |
| connection refused (nothing reached the service) | `TransportError` |
| command sent, no reply | `OutcomeUnknown`: the service locks itself; read the state; never resend motion |
| command ended failed / rejected / stopped | `CommandFailed` (`.result`, `.last_command`) |
| no final result in time | `CommandTimeout` (the command may still run) |
| lease renewal failed | `LeaseLost` (from `LeaseKeeper.check()`) |

Timeouts: reads 5 s, commands 10 s, stop `wait_s + 5` s; all configurable. Redirects are refused and
environment proxies ignored, because the services are loopback/VPN only.

## Test double

`qumus2.contract.fake_server.FakeService` is a small in-process contract service (no devices) with knobs
for both wire dialects, header rules, the robot's 200-reply style, lease expiry during motion, and a lost
command reply (which locks the service). `tests/test_contract_client.py` shows its use.

## Contract 2 (`stations/link.py`)

The job service (`stations/jobs.py`) commands each module through a link chosen when the brain
connects:

| Link | When | Lease | Commands |
|---|---|---|---|
| `V2Link` | the module URL (or `entry:`) answers `GET api/station` with `contract: 2`, lists a service named like the module (or `service:`), and labclient is found | `POST/DELETE api/lease`, renewed with `renew_only` | `labclient.Service.run`: client command id, nested `params`, wait on `api/commands/<id>` |
| `V1Link` | otherwise, or `contract: 1` on the module in stations.yaml | `ContractClient.lease_keeper()` | as above |

`contract: 2` on a module makes contract 2 required: connecting fails, naming why. With the default
`auto`, a station that offers contract 2 but cannot be used (no labclient, no such service, a
describe that does not follow contract 2) is logged as a warning and used with v1.
contract 2 error codes become the same exceptions as v1 (`bad_params` -> `BadRequest`,
`busy`/`locked`/`lease_*` -> `Conflict`, `not_found` -> `NotFound`, ...), so the job service treats
both paths alike. `ContractJobService.contracts()` says which path each module uses.

labclient is not a package dependency (lab-contract is a private repository). It is found as an
installed package, in the folder `QUMUS_LAB_CONTRACT` names, or in a `lab-contract` checkout next
to this one (`~/Projects/Qumus/lab-contract` on the Mac). Tests: `tests/test_stations_contract2.py`
runs the brain against `FakeLab(contract2=True)` (a labkit station) and is skipped without
lab-contract.
