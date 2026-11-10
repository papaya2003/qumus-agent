"""Scripted reactor round trip against the SIMULATED reactor console (qumus-simlab 8783 /lab/).

    python -m qumus2.reactor.roundtrip --url http://127.0.0.1:8783/lab/

Runs the brain's reactor tools in order, each once:
handover_in -> load -> heat (short program) -> snapshot -> unload -> handover_out,
with reactor_status before and after. It records every call in a run store
(``--db``, default in memory) and prints one JSON report. Exit 0 when every step
was ok.

Simulation only: it refuses a console that does not answer the sim-only
``GET <url>sim/world`` route, so it never drives the real reactor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from qumus2.agents.tooling import ToolContext
from qumus2.contract.errors import ContractError
from qumus2.reactor import tools as rt
from qumus2.reactor.console import ReactorConsole
from qumus2.runtime.store import RunStore

SIM_URL = "http://127.0.0.1:8783/lab/"
#: [default] short program: 300 C at 100 C/min, 1 min hold, 20 % on gas channel 0,
#: 5 % cooling gas until 150 C
SHORT_PROGRAM = {"steps": [{"temperature_c": 300, "ramp_rate_c_per_min": 100, "hold_minutes": 1,
                            "gas": {"0": 20}}],
                 "end_gas": {"0": 5}, "end_gas_until_c": 150}


def is_simulated(console: ReactorConsole) -> bool:
    try:
        console._get("sim/world")
        return True
    except ContractError:
        return False


async def round_trip(url: str, *, chip_id: str, slot: str, to_slot: str, record: bool,
                     store: RunStore, image_dir: Path) -> dict[str, Any]:
    console = ReactorConsole(url, session="qumus", allow_actuation=True, timeout_s=10.0)
    if not is_simulated(console):
        raise SystemExit(f"{url} is not the simulated reactor (no sim/world route); refusing")
    station = rt.ReactorStation(console, "sim", None, image_dir, rt.SIM_SLOTS)
    project = store.create_project("reactor round trip")
    ctx = ToolContext(store=store, jobs=SimpleNamespace(), agent="reactor", project_id=project,
                      extra={"stations": {"reactor": station}})
    plan = [
        ("reactor_status", rt.reactor_status, rt.NoArgs()),
        ("reactor_handover_in", rt.reactor_handover_in, rt.HandoverArgs(chip_id=chip_id, slot=slot)),
        ("reactor_load", rt.reactor_load, rt.LoadArgs(chip_id=chip_id, from_slot=slot)),
        ("reactor_heat", rt.reactor_heat, rt.HeatArgs(chip_id=chip_id, record=record, **SHORT_PROGRAM)),
        ("reactor_snapshot", rt.reactor_snapshot, rt.ChipArgs(chip_id=chip_id)),
        ("reactor_unload", rt.reactor_unload, rt.UnloadArgs(chip_id=chip_id, to_slot=to_slot)),
        ("reactor_handover_out", rt.reactor_handover_out, rt.HandoverArgs(chip_id=chip_id, slot=to_slot)),
        ("reactor_status", rt.reactor_status, rt.NoArgs()),
    ]
    steps = []
    ok = True
    for name, tool, args in plan:
        result = await tool(args, ctx)
        steps.append({"tool": name, "status": result.status, "summary": result.summary,
                      "run_id": result.data.get("run_id"), "images": result.images})
        if result.status != "ok":
            ok = False
            break
    try:
        console.release_session()
    except ContractError:
        pass
    chip = store.get_sample(chip_id)
    return {"ok": ok, "url": url, "chip_id": chip_id, "final_location": chip and chip["location"],
            "steps": steps,
            "runs": [{"flow": r["flow"], "status": r["status"]} for r in store.list_runs()]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qumus2.reactor.roundtrip",
                                     description="Scripted reactor round trip (simulated reactor only).")
    parser.add_argument("--url", default=SIM_URL, help=f"simulated console base URL (default {SIM_URL})")
    parser.add_argument("--chip", default="reactor_rt_chip", help="chip ID for the round trip")
    parser.add_argument("--slot", default="A1", help="tray slot the person puts the chip into")
    parser.add_argument("--to-slot", default="A2", help="tray slot the chip is unloaded to")
    parser.add_argument("--record", action="store_true", help="also record the microscope while heating")
    parser.add_argument("--db", default=":memory:", help="run store for the records (default in memory)")
    parser.add_argument("--images", default=None, help="where snapshots go (default a temp folder)")
    args = parser.parse_args(argv)
    store = RunStore(args.db, bench="sim")
    image_dir = Path(args.images) if args.images else Path(tempfile.mkdtemp(prefix="reactor-rt-"))
    try:
        report = asyncio.run(round_trip(args.url, chip_id=args.chip, slot=args.slot,
                                        to_slot=args.to_slot, record=args.record, store=store,
                                        image_dir=image_dir))
    finally:
        store.close()
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
