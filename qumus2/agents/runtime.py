"""AgentRuntime: what the UI/driver talks to. CONTRACT.

Two implementations:
  ScriptedRuntime (agents/scripted.py)  no API key; replays a scenario YAML
                                        through the same ToolGateway and hooks
  OpenAIRuntime   (agents/openai_runtime.py) OpenAI Agents SDK (primary)

Both write everything they do as events in the run store; the UI only reads
those events.
"""

from __future__ import annotations

from typing import Protocol


class ReportedAgentError(RuntimeError):
    """A terminal failure already saved by the runtime; the driver must not duplicate it."""

    def __init__(self, message: str, event_id: int, *, outage: dict | None = None) -> None:
        super().__init__(message)
        self.event_id = event_id
        #: set when the model service was unavailable (HTTP 429/5xx or no
        #: connection): {"status": int | None, "retry_after_s": float | None}.
        #: The driver then waits and continues the turn (agents/driver.py).
        self.outage = outage


class AgentRuntime(Protocol):
    mode: str  # "scripted" | "sdk"

    @property
    def busy(self) -> bool: ...

    async def send(self, text: str) -> None:
        """Deliver one user message to the orchestrator and run until it is idle."""

    async def interrupt(self) -> None:
        """Stop the current turn as soon as possible (UI stop button)."""

    async def continue_turn(self, text: str) -> None:
        """UI Continue after a stop: go on with the stopped work. `text` says
        which jobs resumed from where (SDK: the next turn; scripted: resumes
        the suspended turn, or `text` is the next turn when none is)."""

    async def close(self) -> None: ...
