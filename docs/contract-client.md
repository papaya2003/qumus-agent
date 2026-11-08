# Contract v1 client (`qumus2.contract`)

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
