"""Run the sim-lab modules as HTTP services on loopback.

    qumus2 simlab --db runs/checks/simlab/qumus.db --seed 3
        exfoliation  http://127.0.0.1:8781/api/describe
        stacking     http://127.0.0.1:8782/api/describe

`SimLabServer` is also what `qumus2 serve --contract-lab` starts in-process, so
the agent reaches the simulated devices only through the contract. Port 0
picks free ports (tests).
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

from qumus2.simlab.modules import PORTS, SimModule, build_modules
from qumus2.simlab.service import create_app


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


class SimLabServer:
    """Both modules on their own uvicorn servers (threads) over one lab side."""

    def __init__(self, side: Any, *, host: str = "127.0.0.1",
                 ports: dict[str, int] | None = None, motion_delay_s: float = 0.0) -> None:
        self.side, self.host = side, host
        self.modules: dict[str, SimModule] = build_modules(side, motion_delay_s=motion_delay_s)
        wanted = {**PORTS, **(ports or {})}
        self.ports = {m: (wanted[m] or _free_port(host)) for m in self.modules}
        self._servers: dict[str, Any] = {}
        self._threads: list[threading.Thread] = []

    def url(self, module: str) -> str:
        return f"http://{self.host}:{self.ports[module]}/"

    @property
    def urls(self) -> dict[str, str]:
        return {m: self.url(m) for m in self.modules}

    def start(self, timeout_s: float = 10.0) -> SimLabServer:
        import uvicorn

        for module, mod in self.modules.items():
            mod.service.start_watchdog()
            config = uvicorn.Config(create_app(mod.service), host=self.host,
                                    port=self.ports[module], log_level="warning",
                                    access_log=False, lifespan="off")
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            t = threading.Thread(target=server.run, daemon=True, name=f"simlab-{module}")
            t.start()
            self._servers[module], self._threads = server, [*self._threads, t]
        deadline = time.monotonic() + timeout_s
        while not all(s.started for s in self._servers.values()):
            if time.monotonic() > deadline:
                self.close()
                raise RuntimeError(f"sim lab services did not start on {self.urls}")
            time.sleep(0.02)
        return self

    def close(self) -> None:
        for mod in self.modules.values():
            mod.service.close()
        for s in self._servers.values():
            s.should_exit = True
        for t in self._threads:
            t.join(timeout=5)


def main(args: Any) -> None:
    from qumus2.app import build_lab_side
    from qumus2.config import SIM_PROFILE, simulation_profile

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    # The same Hall-bar laboratory as `serve` (never the older test benches).
    side = build_lab_side(db, seed=args.seed, testbench_config=simulation_profile(SIM_PROFILE))
    server = SimLabServer(side, host=args.host, motion_delay_s=args.motion_delay,
                          ports={"exfoliation": args.exfoliation_port,
                                 "stacking": args.stacking_port}).start()
    for module, url in server.urls.items():
        print(f"{module:12s} {url}api/describe")
    print("Simulated lab (demo: true). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        side.lab.devices.all_safe_state()
        side.store.close()
