"""The reactor's entry in get_lab_setup.

The reactor's tools live in qumus2/reactor/tools.py. They speak the vdW
reactor's own Lab Console API (Jialei, 2026-11-10). The sim-era reactor job
tools (load_reactor_chip, reactor_growth, ...) and the material-metal reactor
chain were retired with that change.
"""

from __future__ import annotations

from qumus2.agents.tooling import ToolContext, ToolResult


async def reactor_setup_details(ctx: ToolContext) -> ToolResult:
    """Device detail provider used by the common laboratory setup query."""
    from qumus2.reactor.tools import REACTOR_TOOLS, station_of

    station = station_of(ctx)
    if station is None:
        return ToolResult(status="ok", summary="No reactor station speaks the Lab Console here.",
                          data={"enabled": False})
    return ToolResult(status="ok", summary=f"vdW reactor ({station.kind}) through its Lab Console.",
                      data={"enabled": True, "kind": station.kind, "adapter": "console",
                            "actuation_allowed": station.console.allow_actuation,
                            "tray_slots": list(station.slots or []),
                            "tools": list(REACTOR_TOOLS),
                            "chip_handover": "by a person (reactor_handover_in / reactor_handover_out)",
                            "gas_unit": "percent of each MFC's full scale, channels '0'..'2'",
                            "limits": "temperature and ramp limits are the reactor host's own",
                            "chip_locations": ["reactor:tray:<slot>", "reactor:crucible", "person"]})
