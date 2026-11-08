"""Run an unchanged copy of Qumus v1 on the v2 software testbench.

`V1Bench` builds the same simulated lab v2 uses (testbench/lab.py), puts
emulators behind v1's serial ports, swaps v1's two vision models and two
webcams for the sim perception, and points v1's clock at the sim clock. Then
it builds v1's tools with v1's own factory (create_all_workflow_tools) and
v1's lab-manager camera scan (LiveYOLOTool).

One V1Bench per process: v1 keeps module-level state (database engines bound
at import, camera calibration, status service). The comparison runner starts
a fresh process per trial (v1compare/run.py).
"""

from __future__ import annotations

import os
import shutil
import subprocess as _real_subprocess
import sys
import threading
import time as _real_time
import types
from pathlib import Path
from typing import Any

from qumus2.config import load_yaml
from qumus2.core.clock import SimClock
from qumus2.testbench.lab import SimLab, build_sim_lab
from v1compare import emulators, microscope, perception
from v1compare.calibration import ArmMap
from v1compare.emulators import (
    ArmEmu,
    FocusEmu,
    HeaterPower,
    Journal,
    MicroscopeOptics,
    MotorEmu,
    PdvEmu,
    PrinterEmu,
    PsuEmu,
    RelayEmu,
    TurretEmu,
)

V1_ROOT = Path(__file__).resolve().parent / "v1"
V1_PACKAGES = ("agents", "common", "database")
# Keeps real time: its folder watcher (watchdog threads) waits on real files.
REAL_TIME_MODULES = ("agents.processing_agent.tools.observer",)
SEGMENTER_DIR = V1_ROOT / "agents" / "processing_agent" / "segmenter"
# v1's Dracal sensor serials (defaults.py) -> testbench heaters
TEMP_SENSORS = {"E30559": "exfoliation_heater", "E30753": "stamp_heater"}


class SimTime(types.ModuleType):
    """`time` as v1 modules see it: sleep/time/monotonic run on the sim clock."""

    def __init__(self, clock: SimClock) -> None:
        super().__init__("time")
        self._clock = clock

    def __getattr__(self, name: str) -> Any:
        return getattr(_real_time, name)

    def sleep(self, seconds: float) -> None:
        self._clock.sleep(max(0.0, float(seconds)))

    def time(self) -> float:
        return self._clock.now()

    def monotonic(self) -> float:
        return self._clock.now()

    def perf_counter(self) -> float:
        return self._clock.now()


class _TempSubprocess(types.ModuleType):
    """`subprocess` inside temp_controller.py: dracal-usb-get.exe reads a heater."""

    STDOUT = _real_subprocess.STDOUT

    def __init__(self, bench: V1Bench) -> None:
        super().__init__("subprocess")
        self._bench = bench

    def check_output(self, args: list[str], **kwargs: Any) -> str:
        serial = args[args.index("-s") + 1]
        heater = self._bench.lab.devices.get(TEMP_SENSORS[serial])
        self._bench.journal.command(TEMP_SENSORS[serial], "read_temperature")
        t = heater.read_temperature()  # offline -> raises, like a missing USB sensor
        self._bench.lab.devices.get("exfoliator").note_temperature()
        return f"{t:.2f}\n"


class _TransferStub:
    """v1's stacking manipulator (Thorlabs KST/KDC over pythonnet) is not
    wired: the graphene path does not use it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("transfer manipulator is not connected on this testbench")


class V1Bench:
    def __init__(self, workdir: str | Path, seed: int = 1,
                 testbench_config: dict[str, Any] | None = None,
                 build_tools: bool = True, any_thread_is_job: bool = False) -> None:
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self.clock = SimClock()
        cfg = testbench_config if testbench_config is not None else load_yaml("testbench.yaml")
        self.lab: SimLab = build_sim_lab(self.clock, store=None, seed=seed, config=cfg)
        endless_tape(self.lab.world)
        self.journal = Journal(self.clock)
        self.tools: dict[str, Any] = {}
        self.triggers: list[dict[str, Any]] = []
        self.injected: list[dict[str, Any]] = []
        self._dead_threads: set[str] = set()
        self.any_thread_is_job = any_thread_is_job
        self._install()
        self._wrap_fault_injector()
        if build_tools:
            self.build_tools()

    # ----- wiring ------------------------------------------------------------

    def _install(self) -> None:
        db = self.workdir / "database"
        db.mkdir(exist_ok=True)
        for var, name in (("MATERIAL_DB_URL", "material_base.db"),
                          ("PROCESS_DB_URL", "process_base.db"),
                          ("EXPERIMENT_DB_URL", "experiment_base.db")):
            os.environ[var] = f"sqlite:///{db / name}"
        os.environ.setdefault("QUMUS_DISABLE_TRACING", "1")
        os.chdir(self.workdir)  # v1 writes runtime files relative to the cwd
        # v1 prints "Initializing REAL Hardware Controllers" and COM port names;
        # say once what they really are
        print("[v1compare] TESTBENCH: every serial port, camera and stage below is simulated; "
              "no real hardware is connected.", flush=True)
        if str(V1_ROOT) not in sys.path:
            sys.path.insert(0, str(V1_ROOT))
        # v1's agents/ is a namespace package. The installed OpenAI SDK also
        # owns `agents`, and a regular installed package wins over a namespace
        # even with V1_ROOT first. Bind v1 explicitly in this isolated trial
        # process; never modify the frozen source or shadow the SDK in v2.
        if "agents" in sys.modules and not str(getattr(sys.modules["agents"], "__file__", "")).startswith(str(V1_ROOT)):
            raise RuntimeError("v1 comparison needs a fresh process without the OpenAI agents SDK imported")
        package = types.ModuleType("agents")
        package.__path__ = [str(V1_ROOT / "agents")]
        sys.modules["agents"] = package

        sys.modules["serial"] = emulators.fake_serial_module()
        sys.modules["ultralytics"] = perception.fake_ultralytics_module()
        sys.modules["pyboof"] = perception.fake_pyboof_module()
        import cv2
        cv2.VideoCapture = perception.SimVideoCapture
        for gui in ("namedWindow", "resizeWindow", "imshow", "destroyWindow",
                    "destroyAllWindows", "moveWindow"):
            setattr(cv2, gui, lambda *a, **k: None)
        cv2.waitKey = lambda *a, **k: -1
        cv2.imwrite = microscope.make_imwrite(cv2.imwrite, str(self.workdir / ".imwrite_tmp"))

        self._route_ports()
        perception.Context.world = self.lab.world
        perception.Context.maps = {"left": ArmMap("left"), "right": ArmMap("right")}

        microscope.Context.clock = self.clock
        microscope.Context.lab = self.lab
        microscope.Context.optics = self.optics
        microscope.Context.seed = self.seed
        microscope.Context.picks_path = str(self.workdir / "sim_revisits.jsonl")

        import common.hardware as hw
        from common.hardware import camera_controller
        hw.TransferController = _TransferStub
        sim_camera = microscope.make_camera_class(camera_controller.CameraController)
        hw.CameraController = camera_controller.CameraController = sim_camera
        microscope.install_segmenter(str(SEGMENTER_DIR))
        self._redirect_agent_log()
        self._redirect_ui_exports()
        from agents.processing_agent.tools import status_utils
        status_utils.STATUS_DIR = self.workdir / "status"
        status_utils.STATUS_FILE = status_utils.STATUS_DIR / "true_instrument_status.json"
        from agents.lab_manager_agent.tools.cv import glass_slide_detector as gsd
        from common.hardware import temp_controller

        def read_qr(det: Any, img_gray: Any, bbox: Any, idx: int) -> str:
            return perception.read_carrier_qr(det.config.table_side, bbox)

        gsd.GlassSlideDetector._read_micro_qr = read_qr
        gsd.GlassSlideDetector._detect_qr_orientation = lambda self, poly, img: (0.0, 0.9)
        temp_controller.subprocess = _TempSubprocess(self)
        self._patch_time()

    def build_tools(self) -> dict[str, Any]:
        """v1's processing tools (fresh controllers, fresh serial connections)
        and the lab manager's camera scan, as v1 builds them. Called again after
        a simulated crash: that is v1 restarting (its state lives in its
        databases, which stay)."""
        from agents.lab_manager_agent.tools.live_yolo_tool import LiveYOLOTool
        from agents.processing_agent.workflows import create_all_workflow_tools

        self.tools = {tool.name: tool for tool in create_all_workflow_tools()}
        self.tools["live_yolo_capture"] = LiveYOLOTool()
        self._patch_time()  # modules imported while building the tools
        return self.tools

    # ----- faults ------------------------------------------------------------

    def _wrap_fault_injector(self) -> None:
        """Scenario triggers (inject a fault when the world reaches a state)
        and a dead "process" after a simulated crash: v1 code runs `finally:`
        blocks on the way out, a killed process would not, so every later
        device call from the crashed thread dies too.

        With `any_thread_is_job` (agent mode) every thread counts, since the
        agent may call tools from worker threads, and a crash kills them all."""
        wrap_fault_injector(self.lab, self.triggers, self.injected, self._dead_threads,
                            whole_process=self.any_thread_is_job)

    def _redirect_agent_log(self) -> None:
        """v1 writes logs/agent_log next to its repo root (here: inside
        v1compare/); keep them in the run's workdir instead."""
        from common.services import agent_run_log

        logs_root = os.path.join(str(self.workdir), "logs", "agent_log")

        def init(logger: Any) -> None:  # AgentRunLogger.__init__ with another root
            logger.logs_root = logs_root
            logger.sessions_dir = os.path.join(logs_root, "prev_trajs_sessions")
            os.makedirs(logger.sessions_dir, exist_ok=True)
            for attr, name in (("prev_trajs_path", "prev_trajs.jsonl"),
                               ("complete_trajs_path", "complete_trajs.jsonl"),
                               ("error_corr_path", "error_corrections.jsonl"),
                               ("run_log_path", "run.jsonl")):
                setattr(logger, attr, os.path.join(logs_root, name))

        agent_run_log.AgentRunLogger.__init__ = init

    def _redirect_ui_exports(self) -> None:
        """v1 copies its camera overlays for the UI into <v1 repo>/images/ and
        returns that path relative to the repo root, which in the lab is also
        its working directory. Here the working directory is the trial's
        workdir, so the agent got paths that scan_materials could not open
        (and parallel trials shared one folder). Copy them into the workdir,
        under the same relative path, exactly as v1 does otherwise."""
        from agents.lab_manager_agent.tools.cv import capture_controller as cc

        workdir = self.workdir

        def export(ctl: Any, source: Path, rel_dir: Path,
                   filename_override: str | None = None) -> Path | None:
            name = filename_override or Path(source).name  # v1 callers always name it
            if name.startswith("latest_"):
                name = f"{ctl.config.camera_view}_{name}"
            dest = workdir / rel_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, dest)
            except Exception:  # noqa: BLE001 - as v1: no overlay, no path
                return None
            return rel_dir / name

        cc.LiveYOLOController._export_for_ui = export

    def _route_ports(self) -> None:
        from common.hardware.defaults import DEFAULT_HARDWARE_PORTS as P

        d, j = self.lab.devices, self.journal
        emulators.ROUTER.clock = self.clock
        power = HeaterPower({"exfoliation_heater_relay": d.get("exfoliation_heater"),
                             "transfer_heater_relay": d.get("stamp_heater")})
        self.power = power
        self.optics = MicroscopeOptics(d.get("microscope"))
        routes = {
            P["focus_port"]: FocusEmu(j, self.optics),
            P["robot_arm_port_left"]: ArmEmu(j, d.get("arm_left"), "left"),
            P["robot_arm_port_right"]: ArmEmu(j, d.get("arm_right"), "right"),
            P["exfoliation_vacuum_relay_port"]: RelayEmu(j, "exfoliation_vacuum",
                                                         d.get("exfoliation_vacuum").set),
            P["exfoliation_fan_relay_port"]: RelayEmu(j, "exfoliation_fan",
                                                      d.get("exfoliation_fan").set),
            P["sample_stage_vacuum_relay_port"]: RelayEmu(j, "transfer_stage_vacuum",
                                                          d.get("transfer_stage_vacuum").set),
            P["transfer_arm_vacuum_relay_port"]: RelayEmu(j, "transfer_arm_vacuum",
                                                          d.get("transfer_arm_vacuum").set),
            P["exfoliation_heater_relay_port"]: RelayEmu(
                j, "exfoliation_heater_relay",
                lambda on: (d.get("exfoliation_heater")._check_online(),
                            power.set_relay("exfoliation_heater_relay", on))),
            P["transfer_heater_relay_port"]: RelayEmu(
                j, "transfer_heater_relay",
                lambda on: (d.get("stamp_heater")._check_online(),
                            power.set_relay("transfer_heater_relay", on))),
            P["printer_port"]: PrinterEmu(j, d.get("exfoliator")),
            P["motor_port"]: MotorEmu(j, d.get("exfoliator")),
            P["power_port"]: PsuEmu(j, power),
            P["stage_port"]: PdvEmu(j, d.get("transfer_stage"), self.clock),
            P["turret_port"]: TurretEmu(j, d.get("microscope")),
        }
        emulators.ROUTER.ports = routes
        self.emulators = routes

    def _patch_time(self) -> None:
        sim_time = SimTime(self.clock)
        for name, mod in list(sys.modules.items()):
            if mod is None or not name.startswith(tuple(p + "." for p in V1_PACKAGES)):
                continue
            if name in REAL_TIME_MODULES:
                continue
            if getattr(mod, "time", None) is _real_time:
                mod.time = sim_time

    def add_trigger(self, when: Any, fault: str, params: dict[str, Any] | None = None) -> None:
        self.triggers.append({"when": when, "fault": fault, "params": params or {}})

    # ----- running -----------------------------------------------------------

    def call(self, tool: str, **kwargs: Any) -> Any:
        """Run one v1 tool the way v1's agent would (smolagents Tool.__call__),
        on a "job-" thread so the testbench fault injector sees the calls."""
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = self.tools[tool](**kwargs)
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                box["error"] = exc

        th = threading.Thread(target=target, name=f"job-v1-{tool}")
        th.start()
        th.join()
        if "error" in box:
            raise box["error"]
        return box["result"]


def endless_tape(world: Any) -> None:
    """Tape consumption is not what the comparison measures, and v1 counts its
    tape feed in motor degrees, not mm: give both systems a roll that never
    runs out (a scenario can still empty it on purpose)."""
    with world.lock:
        world.tape.roll_left_mm = 1e9


def wrap_fault_injector(lab: Any, triggers: list[dict[str, Any]], injected: list[dict[str, Any]],
                        dead: set[str], whole_process: bool = False) -> None:
    """Shared by the v1 bench and the v2 side of a comparison trial.

    Faults fire only on job threads ("job-..."), or on every thread when
    `whole_process`; then a crash marks the whole process dead ("*")."""
    from qumus2.testbench.faults import SimulatedCrash

    faults = lab.faults
    real = faults.before_call

    def before_call(device_id: str) -> None:
        if whole_process:
            # the testbench counts (and crashes or times out) only device calls
            # made on "job-..." threads; smolagents runs tool calls on its own
            # worker threads, which are all part of the one v1 process here
            cur = threading.current_thread()
            if not cur.name.startswith("job-"):
                cur.name = f"job-{cur.name}"
        th = "*" if whole_process else threading.current_thread().name
        if whole_process or th.startswith("job-"):
            if th in dead:
                raise SimulatedCrash(f"process is dead (call to {device_id})")
            for t in list(triggers):
                if t["when"](lab.world, device_id):
                    triggers.remove(t)
                    faults.inject(t["fault"], t["params"])
                    injected.append({"fault": t["fault"], "params": t["params"],
                                     "device": device_id, "calls": faults.calls})
        try:
            real(device_id)
        except SimulatedCrash:
            dead.add(th)
            raise

    faults.before_call = before_call
