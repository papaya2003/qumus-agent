# Stage cooling diagnosis and completion boundary

Owner review, 28 October 2026. All validation described here is offline simulation.

## Previous model and the recorded 56.7 C

`SimHeater._update` uses
`T(t + dt) = goal + (T(t) - goal) * exp(-dt / tau)`.
The default ambient is 22 C. Heating has `tau = 60 s`; passive cooling has
`tau = 3 * 60 = 180 s`; commanded fan cooling uses the configured synthetic
`fan_tau_s = 40 s`. These are software test assumptions, not measured lab rates.
At 90 C with heat off, passive cooling initially falls at about 0.378 C/s and
fan cooling at 1.7 C/s; the rates decrease as the temperature approaches ambient.
From 170 C to strictly below 40 C takes more than 379.2 s passively or 84.3 s
with that ideal fan, before polling quantization. No thermal constant is changed
by this repair. Source: `devices/sim/devices.py`, `config/testbench.yaml`.

Previously, only `exfoliation_fan` was bound to a heater. `CoolAndPeel` disabled
heat, enabled that fan when needed, waited for a reading below 60 C, then disabled
the fan before peeling. The search/stacking stage (`stamp_heater`) had no fan
binding; `CoolTransferStage` disabled heat and waited passively for a reading
at or below its 60 C handling threshold. Completing these intermediate jobs
did not require either stage below 40 C. Project completion checked physical
plan/jobs and reactor acceptance, but had no independent two-stage cold check.

The immutable recording
`recordings/openai-material-metal-default-20261028-v7/recording.json` shows:

| Evidence | Recorded result |
| --- | --- |
| Events 443 and 3436, `cool_and_peel` | Peel temperature 22.0 C |
| Event 14263, `cool_transfer_stage` | 59.7 C; accepted under the 60 C handling threshold |
| Event 14796, `wait_for_reactor_access` | Reactor 74.506 C, within its independent 80 C access limit |
| Events 14849–14850, final reactor process | Reactor 56.7043856608511 C, heater target off, flow effectively zero, carrier in storage |
| Event 14874, `save_project_status` | Completion saved under the previous contract |
| Final recorded bench frame 14884 | Exfoliation 22.0 C; search/stacking 23.4 C; reactor 56.7 C |

Thus 56.7 C was the **reactor**, not either of the two stages. The old final frame
actually has both stages below 40 C, but it lacks the new explicit final-stage
completion check and must not be relabelled as evidence of that check. Reactor
cooling separately uses its configured 45 s time constant (heating 30 s), no
stage fan, and its existing access/material acceptance rules. This repair does
not silently impose the owner's two-stage requirement on that separate reactor.

`SimClock` advances with device movement and flow waits; model thinking does not
independently advance this simulated physical clock. The heater evaluates elapsed
time on reads/control boundaries rather than teleporting to a target. The lab
process persists before worker device calls, including temperature polls. Replay
uses saved frames/events, combines bounded simulated/wall-clock gaps for display,
and excludes epoch jumps. Waiting at the end of historical playback does not
generate new physics or colder historical readings. Shutdown of an output is
distinct from removal of stored heat.

## Completion and presentation

Final completion requires a normal `cool_experiment_stages` job followed by fresh,
locked readings of both ordinary stages strictly below 40 C, heaters off and fans
commanded off. Intermediate handling retains its separate 60 C threshold.
Failures, unknown feedback and interrupted cooling do not establish completion.
The authoritative execution, locking and recovery contract is in
[Architecture: final experiment cooling](architecture.md#final-experiment-cooling).

The Progress Top view retains one outer card, one heading and Expand; its inner
map frame is removed. This presentation change does not alter historical data.
Local test and screenshot receipts are private workstream evidence; the current
[release report](archive/release-2026-10-28.md) states integrated acceptance status.

## Hardware and measurement limits

- `transfer_stage_fan` is a simulated Switch using the existing configurable
  synthetic fan rate. Existing thermal constants and real-driver settings are
  unchanged. No real fan command protocol or calibrated airflow response is
  invented. A real deployment still needs approved drivers and calibration.
- Switch readback is commanded state, not a tachometer or airflow measurement.
  Simulated stuck actuators use `really_on` in the physical thermal model; lack
  of sufficient cooling produces stalled feedback or timeout. A slow fan can
  still finish safely if independent temperature readings satisfy the threshold.
- A single plausible but falsely low temperature sensor cannot be diagnosed
  from that reading alone. Finite guarded readings are required, with explicit
  missing/offline/nonfinite/stalled-feedback failure paths. Redundant sensing
  and real airflow verification are unresolved hardware capabilities.
- Process exit disables energy outputs through existing shutdown paths. It does
  not run a new unattended cooling experiment, certify residual heat as safe, or
  mark an unfinished cooldown complete. Preserved state is rechecked on recovery.
