"""Human instructions from the UI to the orchestrator (spec 11.4).

The UI writes rows into the `instruction` table (RunStore.add_instruction).
The agent runtime drains them:
  - orchestrator idle  -> each pending message becomes the next user turn
  - orchestrator busy  -> the gateway appends them to the next tool result
                          (format_for_tool_result), so the model
                          sees them without waiting for the chain to finish
`stop` instructions are handled by the runtime directly (interrupt + jobs.stop_all).

The model sees each message's id: tools that act on a person's word (teach or
retire experience, record an outcome, an exfoliation a person asked for) must
cite it, and code checks that such a message exists.
"""

from __future__ import annotations

from typing import Any

from qumus2.runtime.store import RunStore


def as_turn(items: list[dict[str, Any]]) -> str:
    """Pending messages as the orchestrator's next turn, each with its id."""
    return "\n".join(f"{i['text']} [message id: {i['id']}]" for i in items)


class Inbox:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        self._seen: dict[str, set[str]] = {}  # agent -> instruction ids already shown

    def peek(self, kinds: tuple[str, ...] = ("message", "plan_change", "veto")
             ) -> list[dict[str, Any]]:
        return [i for i in self.store.pending_instructions() if i["kind"] in kinds]

    def drain(self, kinds: tuple[str, ...] = ("message", "plan_change", "veto")
              ) -> list[dict[str, Any]]:
        items = [i for i in self.store.pending_instructions() if i["kind"] in kinds]
        for i in items:
            self.store.mark_instruction(i["id"], "delivered")
        return items

    def format_for_tool_result(self) -> str | None:
        items = self.drain()
        if not items:
            return None
        lines = [f"USER INSTRUCTION ({i['kind']}, message id {i['id']}): {i['text']}"
                 for i in items]
        return "\n".join(lines) + "\nAdjust the plan (update_plan) before continuing."

    def format_heads_up(self, agent: str) -> str | None:
        """For a subagent: new instructions, each shown once, not consumed. The
        orchestrator gets them for real when the subagent returns."""
        seen = self._seen.setdefault(agent, set())
        items = [i for i in self.peek() if i["id"] not in seen]
        if not items:
            return None
        seen.update(i["id"] for i in items)
        lines = [f"USER INSTRUCTION ({i['kind']}, message id {i['id']}, also sent to the "
                 f"orchestrator): {i['text']}" for i in items]
        return "\n".join(lines) + ("\nIf it changes your task, follow it; if it changes the "
                                   "plan beyond your task, finish safely and report back.")
