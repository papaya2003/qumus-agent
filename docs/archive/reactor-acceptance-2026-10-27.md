# Reactor acceptance

The optional reactor and shared two-flake material model are integrated into main.
The approved apparatus geometry and rigid observation motion are retained.
The normal launcher supports a separate reactor workspace with `--reactor`.

## Local evidence

- Pair cardinality, source occupancy, finite area, one-product lineage, current
  goal/job and uncertainty checks run before acceptance.
- Acceptance holds one SQLite transaction across evidence reads, product/source
  updates, project state and events. Concurrent connections and rollback are tested.
- Actual lab-process restart preserves product identity and consumed sources;
  it does not reseed a fresh prepared pair. Incompatible layouts are refused.
- The normal scripted recording loads, moves under fixed optics, reacts two
  prepared flakes, accepts one small product, then cools/retracts/stores the carrier.
- The camera-loss recording parks after hot exposure, disables targets, preserves
  uncertainty and refuses a false product. It does not pretend automatic recovery.

See the [recording catalog](../../recordings/README.md) for current exports and the
[historical review](reactor-review-2026-10-26.md) for earlier failed attempts
and mechanical iterations. All checks run locally; GitHub is storage only.

## Limits

Prepared pairs, recipes, mechanical errors and camera images are synthetic.
The recordings use scripted operator decisions through the actual ToolGateway,
jobs and SafetyGate; they are not model-autonomy evidence or real chemistry.
Physical hardware operation remains outside this delivery.
