"""Serial-port emulators: v1's hardware drivers talk to these instead of COM ports.

v1's controllers (common/hardware/*.py) stay byte-identical. They open
`serial.Serial(port)`, write their usual command strings and parse the
replies; a fake `serial` module routes each port to one emulator below, which
turns the command into a call on the v2 testbench device and answers the way
the real firmware does. A device that is offline or faulted in the testbench
does not answer, so v1 sees what it would see on the real bench: a timeout.

Every command and every oddity (unmapped coordinates, tape torn by an advance
while still applied, ...) goes to `Journal` for the comparison report.
"""

from __future__ import annotations

import re
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from qumus2.core.clock import Clock
from qumus2.core.types import Pose
from v1compare.calibration import ArmMap


@dataclass
class Journal:
    clock: Clock
    commands: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)

    def command(self, device: str, cmd: str, **extra: Any) -> None:
        self.commands.append({"t": self.clock.now(), "device": device, "cmd": cmd, **extra})

    def incident(self, device: str, what: str, **extra: Any) -> None:
        self.incidents.append({"t": self.clock.now(), "device": device, "what": what, **extra})


class Emulator:
    """One physical device behind one port. `handle` gets each complete command."""

    name = "device"

    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    def on_write(self, data: bytes, port: FakeSerial) -> None:
        port._pending += data
        while True:
            text = port._pending.decode(errors="ignore")
            m = re.search(r"\r\n|\n|\r", text)
            if not m:
                return
            line, rest = text[:m.start()], text[m.end():]
            port._pending = rest.encode()
            if line.strip():
                self._dispatch(line.strip(), port)

    def _dispatch(self, line: str, port: FakeSerial) -> None:
        try:
            reply = self.handle(line)
        except Exception as exc:  # device fault: no answer, v1 times out
            self.journal.incident(self.name, f"no reply to {line!r}: {exc}")
            return
        if reply:
            port._rx += reply.encode()

    def handle(self, line: str) -> str | None:
        raise NotImplementedError


class ArmEmu(Emulator):
    """uArm-style G-code arm (robot_arm_controller.py)."""

    def __init__(self, journal: Journal, arm: Any, side: str) -> None:
        super().__init__(journal)
        self.arm, self.map, self.name = arm, ArmMap(side), f"arm_{side}"
        self.side = side
        home = self.map.anchors["home"].v1
        self.pos = {"X": home[0], "Y": home[1], "Z": home[2]}

    def handle(self, line: str) -> str | None:
        words = line.split()
        op = words[0].upper()
        self.journal.command(self.name, line)
        if op in ("G0", "G1"):
            for w in words[1:]:
                if w[0].upper() in "XYZ":
                    self.pos[w[0].upper()] = float(w[1:])
            wx, wy, wz, part = self.map.to_world(self.pos["X"], self.pos["Y"], self.pos["Z"])
            if part == "unmapped":
                self.journal.incident(self.name, "move outside the mapped bench",
                                      v1=dict(self.pos))
            bx, by = getattr(self.arm, "base", (0.0, 0.0))  # world -> this arm's frame
            self.arm.move_to(Pose(x=wx - bx, y=wy - by, z=wz))
        elif op == "M1000":
            self.arm.set_vacuum(True)
        elif op in ("M1002", "M1003"):
            self.arm.set_vacuum(False)
        elif op == "M114":
            self.arm._check_online()
            p = self.pos
            return f"X:{p['X']:.2f} Y:{p['Y']:.2f} Z:{p['Z']:.2f}\nok\n"
        elif op == "M888":
            self.arm._check_online()
            return None  # firmware mode switch, no "ok" (v1 NO_OK_CMDS)
        else:  # G4 G21 G90 M17 M400 M1001 M2100 M2101 ...
            self.arm._check_online()
        return "ok\n"


class RelayEmu(Emulator):
    """USB relay board "AT+CH1=1" (relay_controller.py) driving one switch."""

    def __init__(self, journal: Journal, name: str, on_set: Callable[[bool], None]) -> None:
        super().__init__(journal)
        self.name, self.on_set = name, on_set

    def handle(self, line: str) -> str | None:
        self.journal.command(self.name, line)
        m = re.fullmatch(r"AT\+CH1=([01])", line.strip(), flags=re.IGNORECASE)
        if not m:
            return "ERROR\n"
        self.on_set(m.group(1) == "1")
        return f"CH1={m.group(1)} OK\n"


class HeaterPower:
    """TP3005P supply feeding both stage heaters through their relays.

    Steady state 22 C + 0.25 C/V^2 * V^2 fits v1's approved (target, hold V)
    pairs to within a few degrees (exfoliation_setpoints.py).
    """

    K_C_PER_V2 = 0.25

    def __init__(self, heaters: dict[str, Any]) -> None:
        self.heaters = heaters  # relay name -> SimHeater
        self.voltage = 0.0
        self.output = False
        self.relay = {name: False for name in heaters}

    def update(self) -> None:
        for name, heater in self.heaters.items():
            if self.output and self.relay[name] and self.voltage > 0:
                heater.set_target(heater.ambient + self.K_C_PER_V2 * self.voltage ** 2)
            else:
                heater.off()

    def set_relay(self, name: str, on: bool) -> None:
        self.relay[name] = on
        self.update()


class PsuEmu(Emulator):
    """TP3005P: "VSET1:05.00/n", "OUTPUT1:/n" (literal slash-n, power_controller.py)."""

    name = "power_supply"

    def __init__(self, journal: Journal, power: HeaterPower) -> None:
        super().__init__(journal)
        self.power = power

    def on_write(self, data: bytes, port: FakeSerial) -> None:
        for cmd in data.decode(errors="ignore").split("/n"):
            if cmd.strip():
                self._dispatch(cmd.strip(), port)

    def handle(self, line: str) -> str | None:
        self.journal.command(self.name, line)
        if line.upper().startswith("VSET1:"):
            self.power.voltage = float(line.split(":", 1)[1])
        elif line.upper().startswith("OUTPUT1"):
            self.power.output = True
        elif line.upper().startswith("OUTPUT0"):
            self.power.output = False
        self.power.update()
        return None  # the supply does not answer; v1 waits out its 1 s read


class PrinterEmu(Emulator):
    """3D-printer tape exfoliator (printer_controller.py): X/Z G-code.

    The arc of apply_tape ends at X = -250 (tape down on the stage); the peel
    arc brings it back to X = -140 (tape off). The peel speed is the feedrate
    of that arc, the same 50..500 scale the v2 tape model uses.
    """

    name = "exfoliator"
    APPLIED_BELOW_X = -240.0
    PEELED_ABOVE_X = -150.0

    def __init__(self, journal: Journal, tape: Any) -> None:
        super().__init__(journal)
        self.tape = tape
        self.relative = False
        self.x = self.z = 0.0
        self.feed = 1000.0

    def handle(self, line: str) -> str | None:
        words = line.split()
        op = words[0].upper()
        if op not in ("G1", "G0"):
            self.journal.command(self.name, line)
        if op == "G90":
            self.relative = False
        elif op == "G91":
            self.relative = True
        elif op in ("G0", "G1"):
            for w in words[1:]:
                k, v = w[0].upper(), float(w[1:])
                if k == "F":
                    self.feed = v
                elif k in "XZ":
                    cur = self.x if k == "X" else self.z
                    new = cur + v if self.relative else v
                    if k == "X":
                        self.x = new
                    else:
                        self.z = new
        elif op == "M114":
            self.tape._check_online()
            return f"X:{self.x:.2f} Y:0.00 Z:{self.z:.2f} E:0.00 Count X:0 Y:0 Z:0\nok\n"
        elif op == "M400":
            applied = self.tape.is_applied()
            if not applied and self.x <= self.APPLIED_BELOW_X:
                self.tape.apply()
                self.journal.command(self.name, "tape applied", x=self.x)
            elif applied and self.x >= self.PEELED_ABOVE_X:
                self.tape.peel(max(50.0, min(500.0, self.feed)))
                self.journal.command(self.name, "tape peeled", speed=self.feed)
        self.tape._check_online()
        return "ok\n"


class MotorEmu(Emulator):
    """Arduino tape rolls (motor_controller.py): MOTOR1_CCW_<deg> releases tape."""

    name = "tape_motors"

    def __init__(self, journal: Journal, tape: Any) -> None:
        super().__init__(journal)
        self.tape = tape

    def handle(self, line: str) -> str | None:
        self.journal.command(self.name, line)
        m = re.fullmatch(r"MOTOR(\d)_(CW|CCW)_([-\d.]+)", line.strip())
        if m and m.group(1) == "1" and m.group(2) == "CCW":
            try:
                self.tape.advance(float(m.group(3)))
            except Exception as exc:  # e.g. tape still down: the roll tears it
                self.journal.incident(self.name, f"tape advance failed: {exc}")
        elif m:
            self.journal.incident(self.name, "tape position adjust (not modelled)", cmd=line)
        return "OK\n"


class TurretEmu(Emulator):
    """Objective turret: "cRDC<pos>\\r" (turret_controller.py)."""

    name = "turret"
    MAGNIFICATION = {1: 5, 2: 10, 3: 20, 4: 100}

    def __init__(self, journal: Journal, microscope: Any) -> None:
        super().__init__(journal)
        self.microscope = microscope

    def handle(self, line: str) -> str | None:
        self.journal.command(self.name, line)
        m = re.fullmatch(r"cRDC(\d)", line.strip())
        if not m:
            return "eRDC\n"
        self.microscope.set_objective(self.MAGNIFICATION.get(int(m.group(1)), 10))
        return "oRDC\n"


class FocusEmu(Emulator):
    """Thorlabs MFC1 focus motor (focus_controller.py): 9-byte binary frames,
    instruction 4 = relative move in microsteps (92.5 per um), big-endian two's
    complement; reply 9 bytes with status 100 (ok) in byte 2."""

    name = "focus"
    STEPS_PER_UM = 92.5

    def __init__(self, journal: Journal, optics: Any) -> None:
        super().__init__(journal)
        self.optics = optics  # MicroscopeOptics

    def on_write(self, data: bytes, port: FakeSerial) -> None:
        port._pending += data
        while len(port._pending) >= 9:
            frame, port._pending = port._pending[:9], port._pending[9:]
            try:
                self.optics.microscope._check_online()
                status = self._frame(frame)
            except Exception as exc:
                self.journal.incident(self.name, f"no reply: {exc}")
                continue
            port._rx += bytes([frame[0], 1, status, frame[3], 0, 0, 0, 0, 0])

    def _frame(self, f: bytes) -> int:
        if (sum(f[:8]) & 0xFF) != f[8]:
            return 1
        instr = f[1]
        val = int.from_bytes(f[4:8], "big")
        if val >= 2 ** 31:
            val -= 2 ** 32
        if instr == 4:
            um = val / self.STEPS_PER_UM
            self.journal.command(self.name, f"move {um:+.2f} um")
            self.optics.move_motor_um(um)
        elif instr == 3:
            self.journal.command(self.name, "stop")
        else:
            return 2
        return 100


class MicroscopeOptics:
    """Focus of the v2 sim microscope as v1 drives it: the Thorlabs motor
    (relative um) plus the camera's own lens autofocus position (v1 UVCHAM
    AF, 'afpos'). Best focus differs per chip (testbench focus model)."""

    def __init__(self, microscope: Any) -> None:
        self.microscope = microscope
        self.motor_um = 0.0
        self.lens_mm = 0.0

    def move_motor_um(self, um: float) -> None:
        self.motor_um += um
        self.apply()

    def apply(self) -> None:
        self.microscope.move_focal_plane(self.microscope.nominal_focus
                                         + self.motor_um / 1000.0 + self.lens_mm)


class PdvEmu(Emulator):
    """KZ-100 motor controller of the transfer ("sample") stage (kz100_pdv_api.py).

    The driver writes one character at a time and reads one echo byte (0x0D)
    after each; "U<axis>;" then returns 9 ASCII characters of position. Axes X
    and Y move the chip under the microscope (12800 steps/mm), L carries the
    stage from the arm drop-off point (L = 0) to the microscope (L = 80 mm,
    3280 steps/mm). Distance moves are instant on the sim clock; "S" jogs run
    at speed * 38.1 steps/s until stopped ("PA;" or a new command on the axis).

    v2 transfer_stage pose (mm) = (L / 80) * (52.7, 8.3) + (-Y, -X): v1's
    microscope position (L 80, X -12, Y 8) puts the objective 0.3 mm inside the
    chip corner v1 starts from (its "BL" corner: world -x, +y), where v1's
    snake scan begins (set_home at that position). The v2 bench's microscope
    position (40, 25) instead centres the chip.
    """

    name = "transfer_stage"
    XY_STEPS_PER_MM = 12800.0
    L_STEPS_PER_MM = 3280.0
    JOG_STEPS_PER_S = 38.1  # per unit of speed (5^5 / 2^20 rev/s * 12800)

    def __init__(self, journal: Journal, stage: Any, clock: Clock) -> None:
        super().__init__(journal)
        self.stage, self.clock = stage, clock
        self.steps = {"X": 0.0, "Y": 0.0, "Z": 0.0, "L": 0.0}
        self.jog: dict[str, tuple[float, float]] = {}  # axis -> (speed steps/s, since)

    def _settle(self) -> None:
        now = self.clock.now()
        for axis, (v, t0) in list(self.jog.items()):
            self.steps[axis] += v * (now - t0)
            self.jog[axis] = (v, now)

    def pose(self) -> Pose:
        x_mm = self.steps["X"] / self.XY_STEPS_PER_MM
        y_mm = self.steps["Y"] / self.XY_STEPS_PER_MM
        f = (self.steps["L"] / self.L_STEPS_PER_MM) / 80.0
        return Pose(x=52.7 * f - y_mm, y=8.3 * f - x_mm)

    def _apply(self) -> None:
        self.stage.move_to(self.pose())

    def on_write(self, data: bytes, port: FakeSerial) -> None:
        for ch in data.decode(errors="ignore"):
            port._pending += ch.encode()
            port._rx += b"\r"  # per-character echo
            if ch == ";":
                cmd = port._pending.decode(errors="ignore")[:-1]
                port._pending = b""
                self._dispatch(cmd, port)

    def handle(self, line: str) -> str | None:
        self._settle()
        op, rest = line[:1].upper(), line[1:]
        if op != "U":
            self.journal.command(self.name, line)
        if op == "U":
            axis = rest[:1].upper()
            self.stage._check_online()
            if axis in self.steps:
                self._apply()  # a jogging stage keeps moving between reads
                return f"{int(round(self.steps[axis])):>9d}"[:9]
            return "0"  # status byte (limit / mode / home): nothing set
        if op == "D":
            axis, n = rest[:1].upper(), float(rest[1:])
            self.jog.pop(axis, None)
            self.steps[axis] += n
        elif op == "S":
            axis, v = rest[:1].upper(), float(rest[1:])
            if v == 0:
                self.jog.pop(axis, None)
            else:
                self.jog[axis] = (v * self.JOG_STEPS_PER_S, self.clock.now())
        elif op == "P":  # "PA": stop all axes
            self.jog.clear()
        self._apply()
        return None


class FakeSerial:
    """Enough of pyserial's Serial for v1's drivers."""

    def __init__(self, port: str | None = None, baudrate: int = 9600, timeout: float | None = None,
                 **kwargs: Any) -> None:
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self._rx = b""
        self._pending = b""
        dev = ROUTER.get(port)
        if dev is None:
            raise SerialException(f"could not open port {port!r}: no such device on the testbench")
        self.device = dev
        self.is_open = True

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise SerialException("port closed")
        self.device.on_write(bytes(data), self)
        return len(data)

    def read(self, n: int = 1) -> bytes:
        out, self._rx = self._rx[:n], self._rx[n:]
        if len(out) < n and self.timeout:
            ROUTER.clock.sleep(float(self.timeout))  # a real read blocks until timeout
        return out

    def readline(self) -> bytes:
        i = self._rx.find(b"\n")
        if i < 0:
            out, self._rx = self._rx, b""
            if self.timeout:
                ROUTER.clock.sleep(float(self.timeout))
            return out
        out, self._rx = self._rx[:i + 1], self._rx[i + 1:]
        return out

    def reset_input_buffer(self) -> None:
        self._rx = b""

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    def isOpen(self) -> bool:  # noqa: N802 - pyserial name
        return self.is_open


class SerialException(OSError):
    pass


class _Router:
    def __init__(self) -> None:
        self.ports: dict[str, Emulator] = {}
        self.clock: Any = None

    def get(self, port: str | None) -> Emulator | None:
        return self.ports.get(str(port))


ROUTER = _Router()


def fake_serial_module() -> types.ModuleType:
    mod = types.ModuleType("serial")
    mod.Serial = FakeSerial
    mod.SerialException = SerialException
    mod.SerialTimeoutException = SerialException
    mod.PARITY_NONE, mod.PARITY_EVEN, mod.PARITY_ODD = "N", "E", "O"
    mod.STOPBITS_ONE, mod.STOPBITS_TWO = 1, 2
    mod.EIGHTBITS = 8
    mod.__version__ = "testbench"
    return mod
