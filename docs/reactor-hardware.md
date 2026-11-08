# Reactor mechanics and replaceable hardware interfaces

Updated 2026-10-26 from the owner's mechanical clarification and a read-only
review of the group's GitHub controllers. These adapters are an integration
foundation, not commissioned apparatus drivers. No ports were opened and no
physical commands were executed during this work.

## Stable device identities and coordinates

| Qumus device | Physical function | Adapter boundary |
| --- | --- | --- |
| `reactor_slot` | Bottom X and top Y move the entire chamber and rod mechanism | `MotionStage.move_to(Pose)`, `get_pose()` |
| `reactor_insert` | Insert Y advances/retracts the rod relative to the assembly | Same interface; X, Z and rotation must be zero |
| `reactor_microscope` | Fixed XY microscope; independent focus Z | `Microscope` plus a calibrated optical observation pipeline |
| `reactor_heater` | Furnace temperature request and measured PV | `Heater`; target and feedback remain separate |
| `reactor_flow` | One calibrated gas/MFC channel | `GasFlowController`, in sccm |
| `arm_reactor` | Load and retrieve the narrow substrate | `Arm`; end effector needs its own verified contract |

In the simulation, chip world XY = load XY + assembly XY + (0, insertion Y)
+ measured seating offset. The centred window follows the chamber. The
microscope world XY stays fixed. Firmware CW/CCW labels do not establish signs
in this world frame. Assembly and insertion observations must use their own
relative frames, and timestamps must use the injected Clock's epoch.

Geometry, chip/slot dimensions, storage pitch and motion speeds live in
`qumus2/config/reactor.yaml`; guarded software envelopes live in `limits.yaml`;
observed placement margins live in `verification.yaml`. The confirmed chip
width ratio is 1:4 and its length is unchanged. Absolute millimetres, speeds,
thermal limits and flow limits in the simulation are uncalibrated fixtures.
Hardware profiles must replace these values with reviewed measurements.

## Source review

The reviewed commits are pinned below so upstream updates cannot silently
change the meaning of this integration.

| Repository / version | Reviewed contract and implication |
| --- | --- |
| [Stage-Motor-Controller, 8dda996](https://github.com/WuLabPrinceton/Stage-Motor-Controller/tree/8dda99679b46934f83de0c0d8b4f96f5f66567f7) | `chemistry_controller/protocol.py`: motor selection, CW/CCW, speed 1–1000 STEP pulses/s, finite moves 1–100000 pulses, exact ACK validation, three-channel status, shared microstep and one-second cooldown. Position is a pulse counter; no verified homing or encoder protocol. |
| [FurnaceCode, ebbf84b](https://github.com/WuLabPrinceton/FurnaceCode/tree/ebbf84b0badb55be43539c7d4b438c20615766c2) | `FurnaceService`: measured PV, requested SV, sample timestamp, ramp and stop. MFC commands use percent; require per-gas full-scale calibration to convert sccm. A successful write or cached zero after stop does not prove zero measured flow. |
| [Objective-Control, 3a2dad0](https://github.com/WuLabPrinceton/Objective-Control/tree/3a2dad07960fa08f2ae0e551ad426e0ce48cd482) | Current dual-A4988 GUI uses OBJ/FOCUS commands; older controller files use a different protocol. Autofocus estimates completion by elapsed time. Objective identity can use the side camera's ArUco calibration. Select matching firmware and measured completion before an adapter can claim success. |
| [Gripper-Control, fe32f20](https://github.com/WuLabPrinceton/Gripper-Control/tree/fe32f20b30fd5b7f2fba1cf980689892ac97114d) | PGE2 Modbus RTU: position 0–1000 per mille, force 20–100, speed 1–100, initialization/mode/status checks. Disconnect does not stop or release. This is not interchangeable with `Arm.set_vacuum(bool)`. |
| [dobot_mg400, e6313a1](https://github.com/WuLabPrinceton/dobot_mg400/tree/e6313a17f14cbb828fde848392cddfd6fab72d91) → [MG400 interface, 92d3e2d](https://github.com/shc0507/dobot_mg400/tree/92d3e2d2072901765a3b362fe89938223c719075) | Reviewed protocol, feedback, ownership and robot configuration sources. Exact echoed commands/status, 1440-byte feedback and a process ownership lease; use local receipt freshness, not the controller timer. Camera/world/tool calibration and the actual end effector remain required. |

## Implemented adapters

`qumus2/devices/real/reactor.py` is dependency-injected and has no I/O on import.
The normal launcher and `serve --reactor` still use simulation. The adapters
must be registered behind the existing SafetyGate in a separately authorized
hardware composition. Jobs and ToolGateway tools retain their device names.

- `ReactorMotorBank` owns one injected stage-controller Transport and serializes
  both logical stages with one lock. All three calibrated motor identities,
  pulse/mm factors, signs, reference offsets, microstep and speeds are explicit.
  It refuses a busy controller, incomplete status, changed resolution and
  oversized transactions. ACK acceptance is followed by completion polling.
- `ControllerReactorStage` exposes the assembly or insertion device. It checks
  fresh independent position observations before and after motion. A calibrated
  camera or encoder may supply them; a pulse counter may not. Timeout, mismatch
  or interrupted completion triggers a best-effort controller stop, records
  uncertainty and refuses reuse until an operator establishes a new reference.
  There is no inferred homing, blind retry or automatic microstep change.
- `FurnaceServiceHeater` delegates to an injected FurnaceService, separates PV
  from SV and refuses missing/stale PV. Turning off remains callable when
  feedback is stale. It does not claim immediate cooling or verified isolation.
- `FurnaceServiceGasFlow` requires channel, gas identity and full-scale sccm.
  It obtains measured flow only through fresh `inspect_gas_flow`, rejects a
  write-only/unavailable channel, and never promotes a cached post-stop zero
  to feedback. The returned target is a request, not proof of delivery.

The upstream transport and service remain responsible for finite I/O timeouts
and exclusive process ownership. The motor bank's lock is in-process; it does
not arbitrate an independently running GUI. Commissioning must also confirm
the controller's physical stop response. No software ACK proves de-energization.

## Substitution procedure

1. Keep the six logical device identities and the flow/ToolGateway contracts.
   Implement or inject the vendor transport behind the adapter. Do not put
   vendor serial strings or motor pulse counts in an agent prompt.
2. Supply measured chip, slot, travel and transform data; pulse/mm, direction,
   microstep and a named reference for every motor; the position observer;
   temperature timestamp/ramp settings; gas channel, identity and full scale.
   A different microstep or a controller reboot requires re-referencing.
3. Bind both motion devices to the same owned motor bank. Confirm the gripper
   type explicitly; a PGE2 installation needs a gripper-aware pick/place flow,
   not a boolean vacuum translation. Bind optics only after selecting the
   matching firmware, objective calibration and real completion feedback.
4. Run the adapter contract/fault tests, then separately authorize apparatus
   commissioning with the lab's limits and shutdown procedure. Absolute targets
   must be checked against independent observations, including after restart.

The current tests use fake controllers and the reviewed upstream DemoTransport
with an injected clock. They cover ACK failure, timeout, endpoint mismatch,
busy/invalid status, resolution change, stale feedback and write-only flow.
They do not establish mechanical repeatability, real MFC response, microscope
completion, actual gripping or material quality. Replacing hardware is confined
to adapters and calibration; those physical acceptance checks remain necessary.
