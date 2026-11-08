"""Qumus v2.

Layering (top to bottom, each layer only calls the one below it):

    ui            web UI; reads the run store, writes user messages to the inbox
    agents        OpenAI Agents SDK team (orchestrator + subagents), tools, hooks
    runtime       job service, run store (SQLite), events, recovery
    flows         closed-loop Steps grouped into flows (molecule / assembly)
    safety        SafetyGate: every device command is checked against limits
    devices       device interfaces; sim/ (testbench) and real/ (lab drivers)

See docs/architecture.md for the contracts between layers.
"""

__version__ = "0.1.0"
